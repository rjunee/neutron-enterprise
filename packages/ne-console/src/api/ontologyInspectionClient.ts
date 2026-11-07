// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { ApiError } from "./client";
import { API_BASE } from "./config";
import type {
  OntologyInspectionResponse,
  OntologyIssueDetail,
  OntologyIssueList,
} from "../knowledge/ontology/types";

export interface OntologyInspectionClientOptions {
  getAccessToken: () => string | undefined;
  onUnauthorized: () => void;
}

export interface OntologyInspectionClient {
  readModel(signal?: AbortSignal): Promise<OntologyInspectionResponse>;
  listIssues(signal?: AbortSignal): Promise<OntologyIssueList>;
  readIssue(issueId: string, signal?: AbortSignal): Promise<OntologyIssueDetail>;
}

export function createOntologyInspectionClient(
  options: OntologyInspectionClientOptions,
): OntologyInspectionClient {
  async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
    const token = options.getAccessToken();
    const response = await fetch(`${API_BASE}${path}`, {
      method: "GET",
      signal,
      headers: {
        Accept: "application/json",
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
        // A status remains authoritative when a proxy served no JSON body.
      }
      throw new ApiError(
        `Ontology inspection request failed with HTTP ${response.status}`,
        response.status,
        detail,
      );
    }
    return (await response.json()) as T;
  }

  return {
    readModel(signal) {
      return read<OntologyInspectionResponse>("/v1/ontology/model", signal);
    },
    listIssues(signal) {
      return read<OntologyIssueList>("/v1/ontology/violations", signal);
    },
    readIssue(issueId, signal) {
      return read<OntologyIssueDetail>(
        `/v1/ontology/violations/${encodeURIComponent(issueId)}`,
        signal,
      );
    },
  };
}
