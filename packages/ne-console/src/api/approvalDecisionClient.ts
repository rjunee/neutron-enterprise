// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Bounded approval-receipt read seam. The shared ApiClient is intentionally not
// widened while another campaign owns it; this module preserves the same API
// base, bearer, error-detail, and 401 redirect contract for the server's exact
// approval-decision lookup.

import { ApiError } from "./client";
import { API_BASE } from "./config";
import type { AuditRecordView } from "./types";

export interface ApprovalDecisionClientOptions {
  getAccessToken: () => string | undefined;
  onUnauthorized: () => void;
}

export interface ApprovalDecisionClient {
  read(requestId: string, signal?: AbortSignal): Promise<AuditRecordView>;
}

export function createApprovalDecisionClient(
  options: ApprovalDecisionClientOptions,
): ApprovalDecisionClient {
  const { getAccessToken, onUnauthorized } = options;

  return {
    async read(requestId, signal) {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      const token = getAccessToken();
      if (token) headers.Authorization = `Bearer ${token}`;

      const path = `/v1/audit/approval-decisions/${encodeURIComponent(requestId)}`;
      const response = await fetch(`${API_BASE}${path}`, { method: "GET", headers, signal });
      if (response.status === 401) {
        onUnauthorized();
        throw new ApiError("unauthorized", 401);
      }
      if (!response.ok) {
        let detail: string | undefined;
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") detail = body.detail;
        } catch {
          // Non-JSON/empty body: the typed status remains authoritative.
        }
        throw new ApiError(
          `request to ${path} failed with HTTP ${response.status}`,
          response.status,
          detail,
        );
      }
      return (await response.json()) as AuditRecordView;
    },
  };
}
