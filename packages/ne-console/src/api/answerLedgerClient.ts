// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { ApiError } from "./client";
import { API_BASE } from "./config";
import type { AnswerLedgerView } from "../answer-ledger/types";

export interface AnswerLedgerClientOptions {
  getAccessToken: () => string | undefined;
  onUnauthorized: () => void;
}

export interface AnswerLedgerClient {
  read(signal?: AbortSignal): Promise<AnswerLedgerView>;
  pin(question: string): Promise<AnswerLedgerView>;
  reask(): Promise<AnswerLedgerView>;
}

export function createAnswerLedgerClient(options: AnswerLedgerClientOptions): AnswerLedgerClient {
  async function request(path: string, init: RequestInit): Promise<AnswerLedgerView> {
    const token = options.getAccessToken();
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(token === undefined ? {} : { Authorization: `Bearer ${token}` }),
      },
    });
    if (response.status === 401) {
      options.onUnauthorized();
      throw new ApiError("unauthorized", 401);
    }
    if (!response.ok) {
      let detail: string | undefined;
      try {
        const body = (await response.json()) as { detail?: unknown };
        if (typeof body.detail === "string") detail = body.detail;
      } catch {
        // The status remains authoritative when a proxy served no JSON body.
      }
      throw new ApiError(
        `Answer Ledger request failed with HTTP ${response.status}`,
        response.status,
        detail,
      );
    }
    return (await response.json()) as AnswerLedgerView;
  }

  return {
    read(signal) {
      return request("/v1/answer-ledger", { method: "GET", signal });
    },
    pin(question) {
      return request("/v1/answer-ledger/pin", {
        method: "POST",
        body: JSON.stringify({ question }),
      });
    },
    reask() {
      return request("/v1/answer-ledger/reask", { method: "POST" });
    },
  };
}
