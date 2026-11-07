// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";
import { server } from "../test/mocks/server";
import { ApiError } from "./client";
import { createApprovalDecisionClient } from "./approvalDecisionClient";
import type { AuditRecordView } from "./types";

const TOKEN = "approval-receipt-token";

function record(): AuditRecordView {
  return {
    id: 41,
    seq: 4812,
    event_id: "decision-1",
    event_type: "approval",
    config_hash: "a".repeat(64),
    actor: "user:approver",
    occurred_at: "2026-08-18T12:00:00Z",
    kpi_tag: null,
    trace_id: null,
    action: "approval.granted",
    resource: "tool:crm.create_note",
    outcome: "success",
    payload: { provenance: { approval_id: "req 1/with?special" } },
    prev_hash: "b".repeat(64),
    record_hash: "c".repeat(64),
  };
}

function client(onUnauthorized = () => {}) {
  return createApprovalDecisionClient({
    getAccessToken: () => TOKEN,
    onUnauthorized,
  });
}

describe("createApprovalDecisionClient", () => {
  it("reads the encoded exact-request route with the primary bearer", async () => {
    let seenAuth: string | null = null;
    let seenPath = "";
    server.use(
      http.get("*/api/v1/audit/approval-decisions/:id", ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json(record());
      }),
    );

    const found = await client().read("req 1/with?special");
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seenPath).toBe("/api/v1/audit/approval-decisions/req%201%2Fwith%3Fspecial");
    expect(found).toEqual(record());
  });

  it("preserves the server's explicit 404 detail", async () => {
    server.use(
      http.get("*/api/v1/audit/approval-decisions/:id", () =>
        HttpResponse.json({ detail: "approval decision audit entry not found" }, { status: 404 }),
      ),
    );
    const error = await client()
      .read("missing")
      .then(
        () => null,
        (caught: unknown) => caught,
      );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 404,
      detail: "approval decision audit entry not found",
    });
  });

  it("invokes the primary re-auth path exactly once on 401", async () => {
    server.use(
      http.get(
        "*/api/v1/audit/approval-decisions/:id",
        () => new HttpResponse(null, { status: 401 }),
      ),
    );
    const onUnauthorized = vi.fn();
    await expect(client(onUnauthorized).read("req-1")).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});
