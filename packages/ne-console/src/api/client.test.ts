// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";
import { server } from "../test/mocks/server";
import { ApiError, RUN_CREDENTIAL_MIN_REMAINING_SECONDS, createApiClient } from "./client";
import type { ApprovalPayloadDisclosureView, ApprovalPayloadView, StreamEvent } from "./types";

const TOKEN = "tok-123";

function client(onUnauthorized = () => {}) {
  return createApiClient({ getAccessToken: () => TOKEN, onUnauthorized });
}

/** Narrows a payload read to the disclosed branch, failing the test rather
 * than casting when the seam returned a shell. */
function assertPayload(body: ApprovalPayloadDisclosureView): asserts body is ApprovalPayloadView {
  expect(body).toHaveProperty("target_resource");
}

describe("createApiClient", () => {
  it("uses the canonical authority-attempt reads and a zero-body retry", async () => {
    const attempt = {
      attempt_id: "attempt/7",
      source_kind: "m365-sharepoint" as const,
      source_binding: "m365" as const,
      scope_hash: "a".repeat(64),
      identity_hash: "b".repeat(64),
      stage: "claimed" as const,
      source_revision: "etag-7",
      contract_epoch: 2,
      prior_artifact_id: null,
      receipt_id: null,
      receipt_hash: null,
    };
    const seen: Array<{ method: string; path: string; body: string }> = [];
    server.use(
      http.get("*/api/v1/connect/authority/attempts", ({ request }) => {
        seen.push({ method: request.method, path: new URL(request.url).pathname, body: "" });
        return HttpResponse.json({ attempts: [attempt] });
      }),
      http.get("*/api/v1/connect/authority/attempts/:attemptId", ({ request }) => {
        seen.push({ method: request.method, path: new URL(request.url).pathname, body: "" });
        return HttpResponse.json(attempt);
      }),
      http.post("*/api/v1/connect/authority/attempts/:attemptId/retry", async ({ request }) => {
        seen.push({
          method: request.method,
          path: new URL(request.url).pathname,
          body: await request.text(),
        });
        return HttpResponse.json(attempt, { status: 202 });
      }),
    );

    await client().authorityAttempts();
    await client().authorityAttempt("attempt/7");
    await client().retryAuthorityAttempt("attempt/7");

    expect(seen).toEqual([
      { method: "GET", path: "/api/v1/connect/authority/attempts", body: "" },
      {
        method: "GET",
        path: "/api/v1/connect/authority/attempts/attempt%2F7",
        body: "",
      },
      {
        method: "POST",
        path: "/api/v1/connect/authority/attempts/attempt%2F7/retry",
        body: "",
      },
    ]);
  });

  it("reads and exports the exact governed Run evidence routes without browser caching", async () => {
    const seen: Array<{ path: string; cache: RequestCache }> = [];
    const body = { schema_version: 1, agent_id: "agent/1", run_id: "run/1" };
    server.use(
      http.get("*/api/v1/agents/:agent/runs/:run/evidence", ({ request }) => {
        seen.push({ path: new URL(request.url).pathname, cache: request.cache });
        return HttpResponse.json(body);
      }),
      http.get("*/api/v1/agents/:agent/runs/:run/evidence/export", ({ request }) => {
        seen.push({ path: new URL(request.url).pathname, cache: request.cache });
        return HttpResponse.json(body);
      }),
    );
    await client().getAgentRunEvidence("agent/1", "run/1");
    await client().exportAgentRunEvidence("agent/1", "run/1");
    expect(seen).toEqual([
      { path: "/api/v1/agents/agent%2F1/runs/run%2F1/evidence", cache: "no-store" },
      { path: "/api/v1/agents/agent%2F1/runs/run%2F1/evidence/export", cache: "no-store" },
    ]);
  });
  it("sends Authorization: Bearer <token> on a GET", async () => {
    let seen: string | null = null;
    server.use(
      http.get("*/api/v1/me", ({ request }) => {
        seen = request.headers.get("authorization");
        return HttpResponse.json({ id: "alice", groups: [] });
      }),
    );
    await client().me();
    expect(seen).toBe(`Bearer ${TOKEN}`);
  });

  it("sends Authorization on a POST search built from the single API base", async () => {
    let seenAuth: string | null = null;
    let seenUrl = "";
    server.use(
      http.post("*/api/v1/brain/search", ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenUrl = new URL(request.url).pathname;
        return HttpResponse.json({ chunks: [], citations: [] });
      }),
    );
    await client().search("q");
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seenUrl).toBe("/api/v1/brain/search");
  });

  it("carries the bearer token on the streaming message request too", async () => {
    let seen: string | null = null;
    server.use(
      http.post("*/api/v1/chat/sessions/:id/messages", ({ request }) => {
        seen = request.headers.get("authorization");
        const stream = new ReadableStream({
          start(c) {
            c.enqueue(new TextEncoder().encode('event: done\ndata: {"type":"done"}\n\n'));
            c.close();
          },
        });
        return new HttpResponse(stream, { headers: { "Content-Type": "text/event-stream" } });
      }),
    );
    const events: StreamEvent[] = [];
    await client().sendMessage("s1", "hi", (e) => events.push(e));
    expect(seen).toBe(`Bearer ${TOKEN}`);
    expect(events.at(-1)?.type).toBe("done");
  });

  it("reads the authenticated server mapping for optional chat modes", async () => {
    let seenAuth: string | null = null;
    let seenPath = "";
    server.use(
      http.get("*/api/v1/chat/capabilities", ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json({ grounded: { agent_name: "opaque-grounded-agent" } });
      }),
    );

    await expect(client().chatCapabilities()).resolves.toEqual({
      grounded: { agent_name: "opaque-grounded-agent" },
    });
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seenPath).toBe("/api/v1/chat/capabilities");
  });

  it("sends only the selected server alias with a chat turn", async () => {
    let body: Record<string, unknown> = {};
    server.use(
      http.post("*/api/v1/chat/sessions/:id/messages", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode('event: done\ndata: {"type":"done"}\n\n'));
            controller.close();
          },
        });
        return new HttpResponse(stream, { headers: { "Content-Type": "text/event-stream" } });
      }),
    );

    await client().sendMessage("s1", "hi", () => {}, undefined, undefined, "frontier-sonnet");
    expect(body).toEqual({ prompt: "hi", requested_model: "frontier-sonnet" });
    expect(body).not.toHaveProperty("lane");
    expect(body).not.toHaveProperty("fallback");
  });

  it("reads the names-only live catalog for the exact surface with the bearer", async () => {
    let seenAuth: string | null = null;
    let seenPath = "";
    server.use(
      http.get("*/api/v1/models/choices/:surface", ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json({
          surface: "agentic-workflow",
          task: "agentic-workflow",
          choices: [],
        });
      }),
    );

    await expect(client().modelChoices("agentic-workflow")).resolves.toMatchObject({
      surface: "agentic-workflow",
      choices: [],
    });
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seenPath).toBe("/api/v1/models/choices/agentic-workflow");
  });

  it("uses only the closed lifecycle wire for generic target transitions", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    const paths: string[] = [];
    server.use(
      http.get("*/api/v1/models/lifecycle", ({ request }) => {
        paths.push(new URL(request.url).pathname);
        return HttpResponse.json({
          registry_hash: "a".repeat(64),
          transport_available: true,
          targets: [],
        });
      }),
      http.post("*/api/v1/models/lifecycle/:target/stop-confirmation", async ({ request }) => {
        paths.push(new URL(request.url).pathname);
        bodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({
          confirmation_token: "opaque-confirmation-token",
          expires_at: "2026-08-20T18:01:00Z",
        });
      }),
      http.post("*/api/v1/models/lifecycle/:target/transition", async ({ request }) => {
        paths.push(new URL(request.url).pathname);
        bodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({ status: "pending", request_id: "approval-1" });
      }),
    );

    await expect(client().modelLifecycle()).resolves.toMatchObject({ targets: [] });
    const confirmation = await client().confirmModelStop("cpu/vllm", 7);
    await client().transitionModelTarget("cpu/vllm", "ready", 7, "idem-start");
    await client().transitionModelTarget(
      "cpu/vllm",
      "stopped",
      7,
      "idem-stop",
      confirmation.confirmation_token,
    );

    expect(paths).toEqual([
      "/api/v1/models/lifecycle",
      "/api/v1/models/lifecycle/cpu%2Fvllm/stop-confirmation",
      "/api/v1/models/lifecycle/cpu%2Fvllm/transition",
      "/api/v1/models/lifecycle/cpu%2Fvllm/transition",
    ]);
    expect(bodies).toEqual([
      { expected_version: 7 },
      { desired_state: "ready", expected_version: 7, idempotency_key: "idem-start" },
      {
        desired_state: "stopped",
        expected_version: 7,
        idempotency_key: "idem-stop",
        confirmation_token: "opaque-confirmation-token",
      },
    ]);
  });

  it("carries an agentic alias but leaves the deterministic workflow body unchanged", async () => {
    const bodies: Record<string, unknown>[] = [];
    server.use(
      http.post("*/api/v1/agentic-workflows/:id/start", async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({
          run_id: "run-1",
          workflow_id: "wf-1",
          trace_id: "trace-1",
          principal_id: "user:alice",
          status: "running",
        });
      }),
      http.post("*/api/v1/workflows/:id/run", async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({ workflow_id: "wf-1", trace_id: "trace-deterministic" });
      }),
    );

    await client().startAgenticWorkflow("wf-1", "private-qwen");
    await client().runWorkflow("wf-1", 2);
    expect(bodies).toEqual([{ requested_model: "private-qwen" }, {}]);
  });

  it("invokes onUnauthorized exactly once and throws ApiError on 401", async () => {
    server.use(http.get("*/api/v1/me", () => new HttpResponse(null, { status: 401 })));
    const onUnauthorized = vi.fn();
    await expect(client(onUnauthorized).me()).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("invokes onUnauthorized exactly once and throws ApiError when the streaming endpoint returns 401", async () => {
    server.use(
      http.post(
        "*/api/v1/chat/sessions/:id/messages",
        () => new HttpResponse(null, { status: 401 }),
      ),
    );
    const onUnauthorized = vi.fn();
    const events: StreamEvent[] = [];
    const err = await client(onUnauthorized)
      .sendMessage("s1", "hi", (e) => events.push(e))
      .then(
        () => null,
        (e: unknown) => e,
      );
    // Exact parity with the JSON request() path: ApiError(401), onUnauthorized
    // fired once, and no partial stream events leaked to the collector.
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(events).toHaveLength(0);
  });

  it("does not invoke onUnauthorized on a non-401 stream failure", async () => {
    server.use(
      http.post(
        "*/api/v1/chat/sessions/:id/messages",
        () => new HttpResponse(null, { status: 500 }),
      ),
    );
    const onUnauthorized = vi.fn();
    const events: StreamEvent[] = [];
    const err = await client(onUnauthorized)
      .sendMessage("s1", "hi", (e) => events.push(e))
      .then(
        () => null,
        (e: unknown) => e,
      );
    // A non-401 failure is NOT auth: the raw SseError propagates unchanged and
    // onUnauthorized is never called (no re-auth overreach).
    expect(err).not.toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ name: "SseError", status: 500 });
    expect(onUnauthorized).not.toHaveBeenCalled();
    expect(events).toHaveLength(0);
  });

  it("preserves a selected model refusal status and safe detail as ApiError", async () => {
    server.use(
      http.post("*/api/v1/chat/sessions/:id/messages", () =>
        HttpResponse.json(
          { detail: "frontier model selection is unavailable under AIR_GAP" },
          { status: 403 },
        ),
      ),
    );
    const err = await client()
      .sendMessage("s1", "hi", () => undefined, undefined, undefined, "frontier-sonnet")
      .then(
        () => null,
        (caught: unknown) => caught,
      );
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({
      status: 403,
      detail: "frontier model selection is unavailable under AIR_GAP",
    });
  });

  it.each([
    [409, "requested model is unavailable"],
    [503, "model evidence is unavailable"],
  ])("preserves a selected model %s as a typed non-auth failure", async (status, detail) => {
    const onUnauthorized = vi.fn();
    server.use(
      http.post("*/api/v1/chat/sessions/:id/messages", () =>
        HttpResponse.json({ detail }, { status }),
      ),
    );
    const err = await client(onUnauthorized)
      .sendMessage("s1", "hi", () => undefined, undefined, undefined, "frontier-sonnet")
      .then(
        () => null,
        (caught: unknown) => caught,
      );
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status, detail });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  // THE RE-THROW MOVES THE PROVENANCE, IT DOES NOT DISCARD IT. A selected-model
  // refusal arrives as an SseError and is re-thrown as an ApiError; the raw
  // transport string has to travel on the DEVELOPER slot, or the class doc's
  // promise that it is kept "for the console and for telemetry" is false on
  // this path and the only record of the wire failure is gone. The
  // transport-provenance guardian cannot see this: it constructs errors
  // directly and never re-throws one.
  it("carries the stream's own transport string onto the re-thrown developer slot", async () => {
    server.use(
      http.post("*/api/v1/chat/sessions/:id/messages", () =>
        HttpResponse.json({ detail: "no model is currently available" }, { status: 503 }),
      ),
    );
    const err = await client()
      .sendMessage("s1", "hi", () => undefined, undefined, undefined, "frontier-sonnet")
      .then(
        () => null,
        (caught: unknown) => caught,
      );
    expect(err).toBeInstanceOf(ApiError);
    const failure = err as ApiError;
    expect(failure.developerMessage).toBe("stream failed with HTTP 503");
    // …and the reader's half is unchanged: the server's own served sentence.
    expect(failure.userMessage).toBe("no model is currently available");
    expect(failure.message).toBe("no model is currently available");
  });

  it("re-throws a detail-less stream failure with the string, not the sentence", async () => {
    // The case that shipped wrong: with no served `detail`, `message` IS the
    // house sentence, so passing `message` put a sentence in BOTH slots.
    server.use(
      http.post(
        "*/api/v1/chat/sessions/:id/messages",
        () => new HttpResponse(null, { status: 409 }),
      ),
    );
    const err = await client()
      .sendMessage("s1", "hi", () => undefined, undefined, undefined, "frontier-sonnet")
      .then(
        () => null,
        (caught: unknown) => caught,
      );
    const failure = err as ApiError;
    expect(failure.developerMessage).toBe("stream failed with HTTP 409");
    expect(failure.userMessage).toBe("Someone changed this first.");
  });

  it("retrieve posts exactly the requested chunk_ids", async () => {
    let body: unknown = null;
    server.use(
      http.post("*/api/v1/brain/retrieve", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ chunks: [], citations: [] });
      }),
    );
    await client().retrieve(["chunk-x"]);
    expect(body).toEqual({ chunk_ids: ["chunk-x"] });
  });

  // --- NE-062 ops-surface methods ----------------------------------------

  it("listPendingApprovals sends the bearer and hits GET /v1/approvals/pending", async () => {
    let seenAuth: string | null = null;
    let seenPath = "";
    server.use(
      http.get("*/api/v1/approvals/pending", ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json({ approvals: [] });
      }),
    );
    await client().listPendingApprovals();
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seenPath).toBe("/api/v1/approvals/pending");
  });

  it("readApprovalPayload sends the bearer and hits GET /v1/approvals/{id}/payload", async () => {
    let seenAuth: string | null = null;
    let seenPath = "";
    server.use(
      http.get("*/api/v1/approvals/:id/payload", ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json({
          disclosure_status: "available",
          request_id: "req 1/with?special",
          action: "finance.wire.transfer",
          tier: 3,
          principal_id: "agent://finance-ops",
          target_resource: "account:ops-payables",
          description: "Wire transfer to a named counterparty.",
          material_parameters: { amount: "250,000.00" },
          reversibility: "irreversible — financial/legal action class (SPEC §4.2 Tier-3)",
        });
      }),
    );
    const body = await client().readApprovalPayload("req 1/with?special");
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seenPath).toBe("/api/v1/approvals/req%201%2Fwith%3Fspecial/payload");
    assertPayload(body);
    expect(body.material_parameters).toEqual({ amount: "250,000.00" });
    expect(body.target_resource).toBe("account:ops-payables");
  });

  it("approve POSTs to /v1/approvals/{id}/approve with the bearer and NO approver body", async () => {
    let seenAuth: string | null = null;
    let seenPath = "";
    let body: Record<string, unknown> = {};
    server.use(
      http.post("*/api/v1/approvals/:id/approve", async ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenPath = new URL(request.url).pathname;
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ status: "approved" });
      }),
    );
    await client().approve("req 1/with?special");
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seenPath).toBe("/api/v1/approvals/req%201%2Fwith%3Fspecial/approve");
    expect(body).toEqual({});
  });

  it("reject POSTs to /v1/approvals/{id}/reject with the reason and no approver body", async () => {
    let body: Record<string, unknown> = {};
    server.use(
      http.post("*/api/v1/approvals/:id/reject", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ status: "rejected" });
      }),
    );
    await client().reject("req-1", "duplicate");
    expect(body).toEqual({ reason: "duplicate" });
  });

  it("listAuditEvents builds query params under the single API base", async () => {
    let seenAuth: string | null = null;
    let seenUrl = "";
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenUrl = request.url;
        return HttpResponse.json({
          events: [],
          verification: {
            valid: true,
            checked: 0,
            first_break_seq: null,
            first_break_event_id: null,
            reason: null,
            detail: "",
          },
        });
      }),
    );
    await client().listAuditEvents({ actor: "alice", action: "tool.invoke" });
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    const url = new URL(seenUrl);
    expect(url.pathname).toBe("/api/v1/audit/events");
    expect(url.searchParams.get("actor")).toBe("alice");
    expect(url.searchParams.get("action")).toBe("tool.invoke");
  });

  it("replayAudit / listEvalRuns / kpiSummary each send the bearer on their exact path", async () => {
    const seen: Record<string, string | null> = {};
    server.use(
      http.get("*/api/v1/audit/replay", ({ request }) => {
        seen.replay = request.headers.get("authorization");
        return HttpResponse.json({ events: [], config_hashes: [], context_ids: [] });
      }),
      http.get("*/api/v1/eval/runs", ({ request }) => {
        seen.evals = request.headers.get("authorization");
        return HttpResponse.json({ runs: [] });
      }),
      http.get("*/api/v1/kpi/summary", ({ request }) => {
        seen.kpi = request.headers.get("authorization");
        return HttpResponse.json({ kpis: [], generated_at: "2026-07-07T12:00:00Z" });
      }),
    );
    await client().replayAudit({ trace_id: "trace-1" });
    await client().listEvalRuns({ suite: "s" });
    await client().kpiSummary();
    expect(seen.replay).toBe(`Bearer ${TOKEN}`);
    expect(seen.evals).toBe(`Bearer ${TOKEN}`);
    expect(seen.kpi).toBe(`Bearer ${TOKEN}`);
  });

  it("posture GETs /v1/posture through the one governed seam with the bearer", async () => {
    let seenAuth: string | null = null;
    let seenPath = "";
    server.use(
      http.get("*/api/v1/posture", ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json({
          constraints: [],
          air_gap: true,
          egress_allowlist: [],
          model_lanes: [],
          config_hash: "c".repeat(64),
          config_hash_history: [],
          attestation_source: "build-time guardian evidence",
        });
      }),
    );
    const posture = await client().posture();
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seenPath).toBe("/api/v1/posture");
    expect(posture.air_gap).toBe(true);
  });

  it("a 401 on an ops GET invokes onUnauthorized once and throws ApiError(401)", async () => {
    server.use(
      http.get("*/api/v1/approvals/pending", () => new HttpResponse(null, { status: 401 })),
    );
    const onUnauthorized = vi.fn();
    const err = await client(onUnauthorized)
      .listPendingApprovals()
      .then(
        () => null,
        (e: unknown) => e,
      );
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  // --- NE-067 steward-surface methods -------------------------------------

  it("listStewardProposals sends the bearer and hits GET /v1/steward/proposals", async () => {
    let seenAuth: string | null = null;
    let seenPath = "";
    server.use(
      http.get("*/api/v1/steward/proposals", ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenPath = new URL(request.url).pathname;
        return HttpResponse.json({ proposals: [] });
      }),
    );
    await client().listStewardProposals();
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seenPath).toBe("/api/v1/steward/proposals");
  });

  it("approveProposal POSTs to /v1/steward/proposals/{id}/approve with the bearer and a body carrying only `note` — never an actor", async () => {
    let seenAuth: string | null = null;
    let seenPath = "";
    let body: Record<string, unknown> = {};
    server.use(
      http.post("*/api/v1/steward/proposals/:id/approve", async ({ request }) => {
        seenAuth = request.headers.get("authorization");
        seenPath = new URL(request.url).pathname;
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          review_id: "rev-1",
          proposal_id: "prop-1",
          action: "approve",
          from_status: "proposed",
          to_status: "approved",
          actor: "alice@example.com",
          surface: "console",
          session_id: "sess-1",
          rejection_code: null,
          note: null,
          applied_ref: null,
          config_hash: "cfg-1",
          created_at: "2026-07-07T12:00:00Z",
        });
      }),
    );
    await client().approveProposal("prop 1/with?special");
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
    expect(seenPath).toBe("/api/v1/steward/proposals/prop%201%2Fwith%3Fspecial/approve");
    expect(body).toEqual({ note: "" });
    expect(body).not.toHaveProperty("actor");
  });

  it("rejectProposal POSTs { code, note } with no client-supplied identity", async () => {
    let body: Record<string, unknown> = {};
    server.use(
      http.post("*/api/v1/steward/proposals/:id/reject", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          review_id: "rev-2",
          proposal_id: "prop-1",
          action: "reject",
          from_status: "proposed",
          to_status: "rejected",
          actor: "alice@example.com",
          surface: "console",
          session_id: "sess-1",
          rejection_code: "stale",
          note: "no longer relevant",
          applied_ref: null,
          config_hash: "cfg-1",
          created_at: "2026-07-07T12:00:00Z",
        });
      }),
    );
    await client().rejectProposal("prop-1", "stale", "no longer relevant");
    expect(body).toEqual({ code: "stale", note: "no longer relevant" });
    expect(body).not.toHaveProperty("actor");
  });

  it("recordApply POSTs { applied_ref, note } with no client-supplied identity", async () => {
    let body: Record<string, unknown> = {};
    server.use(
      http.post("*/api/v1/steward/proposals/:id/apply", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          review_id: "rev-3",
          proposal_id: "prop-1",
          action: "apply",
          from_status: "approved",
          to_status: "applied",
          actor: "alice@example.com",
          surface: "console",
          session_id: "sess-1",
          rejection_code: null,
          note: null,
          applied_ref: "https://github.com/example/repo/pull/9",
          config_hash: "cfg-1",
          created_at: "2026-07-07T12:00:00Z",
        });
      }),
    );
    await client().recordApply("prop-1", "https://github.com/example/repo/pull/9");
    expect(body).toEqual({ applied_ref: "https://github.com/example/repo/pull/9", note: "" });
    expect(body).not.toHaveProperty("actor");
  });

  it("stewardDigest / stewardLatency each send the bearer on their exact path", async () => {
    const seen: Record<string, string | null> = {};
    let seenUrl = "";
    server.use(
      http.get("*/api/v1/steward/digest", ({ request }) => {
        seen.digest = request.headers.get("authorization");
        seenUrl = request.url;
        return HttpResponse.json({
          week_start: "2026-06-29",
          week_end: "2026-07-05",
          generated_at: "2026-07-07T12:00:00Z",
          totals: { proposed: 0, approved: 0, rejected: 0, applied: 0 },
          by_lane: [],
        });
      }),
      http.get("*/api/v1/steward/latency", ({ request }) => {
        seen.latency = request.headers.get("authorization");
        return HttpResponse.json({
          median_wait_s: null,
          p90_wait_s: null,
          decided_count: 0,
          pending_count: 0,
          oldest_pending_age_s: null,
          generated_at: "2026-07-07T12:00:00Z",
        });
      }),
    );
    await client().stewardDigest("2026-06-29");
    await client().stewardLatency();
    expect(seen.digest).toBe(`Bearer ${TOKEN}`);
    expect(seen.latency).toBe(`Bearer ${TOKEN}`);
    expect(new URL(seenUrl).searchParams.get("week_start")).toBe("2026-06-29");
  });

  it("a 401 on listStewardProposals invokes onUnauthorized once and throws ApiError(401)", async () => {
    server.use(
      http.get("*/api/v1/steward/proposals", () => new HttpResponse(null, { status: 401 })),
    );
    const onUnauthorized = vi.fn();
    const err = await client(onUnauthorized)
      .listStewardProposals()
      .then(
        () => null,
        (e: unknown) => e,
      );
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("populates ApiError.detail from a non-2xx JSON {detail} body (approval 403 path)", async () => {
    server.use(
      http.post("*/api/v1/approvals/:id/approve", () =>
        HttpResponse.json(
          { detail: "approver is not in the Tier-3 named-approver list" },
          { status: 403 },
        ),
      ),
    );
    const err = await client()
      .approve("req-forbidden")
      .then(
        () => null,
        (e: unknown) => e,
      );
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({
      status: 403,
      detail: "approver is not in the Tier-3 named-approver list",
    });
  });
});

// ===========================================================================
// 015A0-ENFORCEMENT — the two server-authored disclosure seams at the client
// ===========================================================================

describe("the authorized-retrieval envelope on a search response", () => {
  function respondWith(envelope: Record<string, unknown> | undefined) {
    server.use(
      http.post("*/api/v1/brain/search", () =>
        HttpResponse.json({
          chunks: [],
          citations: [],
          graph_paths: [],
          provisional_source_count: 0,
          authorized_retrieval: envelope,
        }),
      ),
    );
  }

  it("returns the server's partial status and all three bounds unchanged", async () => {
    respondWith({ status: "partial", scanned_count: 400, returned_count: 3, scan_limit: 400 });
    const result = await client().search("q");
    expect(result.authorized_retrieval).toEqual({
      status: "partial",
      scanned_count: 400,
      returned_count: 3,
      scan_limit: 400,
    });
  });

  it("returns exhausted with its bounds rather than the complete a zero-chunk page could suggest", async () => {
    respondWith({ status: "exhausted", scanned_count: 500, returned_count: 0, scan_limit: 500 });
    const result = await client().search("q");
    expect(result.authorized_retrieval).toEqual({
      status: "exhausted",
      scanned_count: 500,
      returned_count: 0,
      scan_limit: 500,
    });
  });

  it("reports no status at all for a response that carries no envelope", async () => {
    respondWith(undefined);
    const result = await client().search("q");
    expect(result.authorized_retrieval).toBeUndefined();
  });

  it("fails the read when the envelope states a status this console cannot interpret", async () => {
    respondWith({ status: "trimmed", scanned_count: 1, returned_count: 1, scan_limit: 1 });
    await expect(client().search("q")).rejects.toThrow(TypeError);
  });

  it("fails the read when a bound is missing, rather than serving the rows without it", async () => {
    respondWith({ status: "partial", scanned_count: 400, returned_count: 3 });
    await expect(client().search("q")).rejects.toThrow(TypeError);
  });

  it("applies the same envelope contract to retrieve", async () => {
    server.use(
      http.post("*/api/v1/brain/retrieve", () =>
        HttpResponse.json({
          chunks: [],
          citations: [],
          graph_paths: [],
          provisional_source_count: 0,
          authorized_retrieval: {
            status: "partial",
            scanned_count: 12,
            returned_count: 2,
            scan_limit: 12,
          },
        }),
      ),
    );
    const result = await client().retrieve(["c1"]);
    expect(result.authorized_retrieval?.status).toBe("partial");
  });
});

describe("PRE-012 Agent API boundary", () => {
  it("uses the canonical owner-scoped read routes and preserves opaque cursors", async () => {
    const paths: string[] = [];
    server.use(
      http.get("*/api/v1/agents", ({ request }) => {
        paths.push(new URL(request.url).pathname + new URL(request.url).search);
        return HttpResponse.json({ items: [], next_cursor: null });
      }),
      http.get("*/api/v1/agents/:agentId", ({ request }) => {
        paths.push(new URL(request.url).pathname);
        return HttpResponse.json({ agent_id: "meridian" });
      }),
      http.get("*/api/v1/agents/:agentId/conversations", ({ request }) => {
        paths.push(new URL(request.url).pathname + new URL(request.url).search);
        return HttpResponse.json({ items: [], next_cursor: null });
      }),
      http.get("*/api/v1/agents/:agentId/conversations/:conversationId", ({ request }) => {
        paths.push(new URL(request.url).pathname);
        return HttpResponse.json({ conversation_id: "conversation/one" });
      }),
      http.get("*/api/v1/agents/:agentId/conversations/:conversationId/runs", ({ request }) => {
        paths.push(new URL(request.url).pathname + new URL(request.url).search);
        return HttpResponse.json({ items: [], next_cursor: null });
      }),
      http.get("*/api/v1/agents/:agentId/conversations/:conversationId/turns", ({ request }) => {
        paths.push(new URL(request.url).pathname + new URL(request.url).search);
        return HttpResponse.json({ items: [], next_cursor: null });
      }),
      http.get("*/api/v1/agents/:agentId/workflow-profile", ({ request }) => {
        paths.push(new URL(request.url).pathname);
        return HttpResponse.json({ workflow_id: "workflow-one" });
      }),
      http.get("*/api/v1/agents/:agentId/runs", ({ request }) => {
        paths.push(new URL(request.url).pathname + new URL(request.url).search);
        return HttpResponse.json({ items: [], next_cursor: null });
      }),
      http.get("*/api/v1/agents/:agentId/runs/:runId", ({ request }) => {
        paths.push(new URL(request.url).pathname);
        return HttpResponse.json({ run_id: "run/one" });
      }),
    );

    const api = client();
    await api.listAgents("agent cursor/2");
    await api.getAgent("meridian/analyst");
    await api.listAgentConversations("meridian/analyst", "conversation cursor/2");
    await api.getConversation("meridian/analyst", "conversation/one");
    await api.listConversationRuns("meridian/analyst", "conversation/one", "run cursor/2");
    await api.listConversationTurns("meridian/analyst", "conversation/one", "turn cursor/2");
    await api.getAgentWorkflowProfile("meridian/analyst");
    await api.listAgentRuns("meridian/analyst", "all runs cursor/2");
    await api.getAgentRun("meridian/analyst", "run/one");

    expect(paths).toEqual([
      "/api/v1/agents?cursor=agent+cursor%2F2",
      "/api/v1/agents/meridian%2Fanalyst",
      "/api/v1/agents/meridian%2Fanalyst/conversations?cursor=conversation+cursor%2F2",
      "/api/v1/agents/meridian%2Fanalyst/conversations/conversation%2Fone",
      "/api/v1/agents/meridian%2Fanalyst/conversations/conversation%2Fone/runs?cursor=run+cursor%2F2",
      "/api/v1/agents/meridian%2Fanalyst/conversations/conversation%2Fone/turns?cursor=turn+cursor%2F2",
      "/api/v1/agents/meridian%2Fanalyst/workflow-profile",
      "/api/v1/agents/meridian%2Fanalyst/runs?cursor=all+runs+cursor%2F2",
      "/api/v1/agents/meridian%2Fanalyst/runs/run%2Fone",
    ]);
  });

  it("creates a Conversation from bearer and path identity with no client-authored body", async () => {
    let body = "not observed";
    server.use(
      http.post("*/api/v1/agents/:agentId/conversations", async ({ request }) => {
        body = await request.text();
        return HttpResponse.json(
          { conversation_id: "conversation-1", agent_id: "meridian" },
          { status: 201 },
        );
      }),
    );
    await expect(client().createAgentConversation("meridian")).resolves.toMatchObject({
      conversation_id: "conversation-1",
    });
    expect(body).toBe("");
  });

  it("preserves the exact Attention/Recent envelope and binds its cursor to the window query", async () => {
    let seen = "";
    server.use(
      http.get("*/api/v1/agents/updates", ({ request }) => {
        seen = request.url;
        return HttpResponse.json({
          view: "attention",
          window_start: "2026-08-22T00:00:00Z",
          window_end: "2026-08-23T00:00:00Z",
          items: [],
          next_cursor: "server-cursor",
        });
      }),
    );
    const result = await client().listAgentRunUpdates(
      "attention",
      "2026-08-22T00:00:00Z",
      "2026-08-23T00:00:00Z",
      "opaque cursor",
    );
    expect(result.next_cursor).toBe("server-cursor");
    expect([...new URL(seen).searchParams.entries()]).toEqual([
      ["view", "attention"],
      ["window_start", "2026-08-22T00:00:00Z"],
      ["window_end", "2026-08-23T00:00:00Z"],
      ["cursor", "opaque cursor"],
    ]);
  });

  it("joins a capability preflight to the exact hash without adding identity", async () => {
    let seenUrl = "";
    let seenAuth: string | null = null;
    server.use(
      http.get("*/api/v1/agents/:agentId/preflight", ({ request }) => {
        seenUrl = request.url;
        seenAuth = request.headers.get("authorization");
        return HttpResponse.json({ agent_id: "meridian", capabilities: null });
      }),
    );

    await client().getAgentPreflight("meridian", "capability hash/one");
    const url = new URL(seenUrl);
    expect(url.pathname).toBe("/api/v1/agents/meridian/preflight");
    expect([...url.searchParams.entries()]).toEqual([["capability_hash", "capability hash/one"]]);
    expect(seenAuth).toBe(`Bearer ${TOKEN}`);
  });

  it("sends one exact start body and Idempotency-Key with no principal injection", async () => {
    let seenAuthorization: string | null = null;
    let seenIdempotencyKey: string | null = null;
    let seenBody: unknown;
    server.use(
      http.post("*/api/v1/agents/:agentId/runs", async ({ request }) => {
        seenAuthorization = request.headers.get("authorization");
        seenIdempotencyKey = request.headers.get("idempotency-key");
        seenBody = await request.json();
        return HttpResponse.json(
          {
            action: "start",
            run_id: "run-1",
            attempt_id: "attempt-1",
            version: 1,
            status: "submitted",
            enqueue_id: "enqueue-1",
            replayed: false,
          },
          { status: 202 },
        );
      }),
    );
    const body = {
      conversation_id: "conversation-1",
      expected_version: 9,
      agent_version: 7,
      strategy: "agentic" as const,
      config_hash: "f".repeat(64),
      capability_snapshot_hash: "c".repeat(64),
      input: "Review renewal risk",
      requested_model: "frontier-primary",
      requested_model_lane: "frontier-api",
    };

    await expect(client().startAgentRun("meridian", body, "pre012-start-1")).resolves.toMatchObject(
      { run_id: "run-1", status: "submitted" },
    );
    expect(seenAuthorization).toBe(`Bearer ${TOKEN}`);
    expect(seenIdempotencyKey).toBe("pre012-start-1");
    expect(seenBody).toEqual(body);
    expect(seenBody).not.toHaveProperty("principal_id");
    expect(seenBody).not.toHaveProperty("department");
    expect(seenBody).toHaveProperty("requested_model", "frontier-primary");
  });

  it("reuses the caller's key and byte-equivalent body on an explicit retry", async () => {
    const requests: { key: string | null; body: string }[] = [];
    server.use(
      http.post("*/api/v1/agents/:agentId/runs", async ({ request }) => {
        requests.push({
          key: request.headers.get("idempotency-key"),
          body: await request.text(),
        });
        return HttpResponse.json({ detail: "temporarily unavailable" }, { status: 503 });
      }),
    );
    const body = {
      conversation_id: "conversation-1",
      expected_version: 9,
      agent_version: 7,
      strategy: "agentic" as const,
      config_hash: "f".repeat(64),
      capability_snapshot_hash: "c".repeat(64),
      input: "Review renewal risk",
      requested_model: "frontier-primary",
      requested_model_lane: "frontier-api",
    };
    const api = client();
    await expect(api.startAgentRun("meridian", body, "stable-key")).rejects.toBeInstanceOf(
      ApiError,
    );
    await expect(api.startAgentRun("meridian", body, "stable-key")).rejects.toBeInstanceOf(
      ApiError,
    );
    expect(requests).toHaveLength(2);
    expect(requests[1]).toEqual(requests[0]);
  });
});

describe("the approval payload disclosure status", () => {
  function respondWith(body: Record<string, unknown>) {
    server.use(http.get("*/api/v1/approvals/:id/payload", () => HttpResponse.json(body)));
  }

  const PROTECTED = {
    action: "wire.transfer",
    tier: 3,
    principal_id: "agent://finance-ops",
    target_resource: "account:ops-payables",
    description: "Wire transfer to a named counterparty.",
    material_parameters: { amount: "250,000.00" },
    reversibility: "irreversible",
  };

  it("returns the payload when the gate states available", async () => {
    respondWith({ request_id: "req-1", disclosure_status: "available", ...PROTECTED });
    const body = await client().readApprovalPayload("req-1");
    assertPayload(body);
    expect(body.target_resource).toBe("account:ops-payables");
  });

  it("drops every protected field a content_withheld body carried alongside its status", async () => {
    respondWith({ request_id: "req-1", disclosure_status: "content_withheld", ...PROTECTED });
    const body = await client().readApprovalPayload("req-1");
    expect(body).toEqual({ request_id: "req-1", disclosure_status: "content_withheld" });
  });

  it("drops every protected field a retryable_unknown body carried alongside its status", async () => {
    respondWith({ request_id: "req-1", disclosure_status: "retryable_unknown", ...PROTECTED });
    const body = await client().readApprovalPayload("req-1");
    expect(body).toEqual({ request_id: "req-1", disclosure_status: "retryable_unknown" });
  });

  it("does not read a body with an unrecognised status as an available payload", async () => {
    respondWith({ request_id: "req-1", disclosure_status: "partially_available", ...PROTECTED });
    const body = await client().readApprovalPayload("req-1");
    expect(body).toEqual({ request_id: "req-1", disclosure_status: "retryable_unknown" });
  });

  it("fails closed on a body that states no disclosure status at all", async () => {
    respondWith({ request_id: "req-1", ...PROTECTED });
    const body = await client().readApprovalPayload("req-1");
    expect(body).toEqual({ request_id: "req-1", disclosure_status: "retryable_unknown" });
  });

  it("keys the shell to the requested id, not to an id the response supplied", async () => {
    respondWith({ request_id: "req-other", disclosure_status: "content_withheld" });
    const body = await client().readApprovalPayload("req-mine");
    expect(body).toEqual({ request_id: "req-mine", disclosure_status: "content_withheld" });
  });
});

describe("pre-submit run-credential freshness (RUN_CREDENTIAL_MIN_REMAINING_SECONDS)", () => {
  // A Run freezes the submitting bearer's exp into its authorization snapshot
  // at admission; the worker fail-closed re-checks it before every protected
  // disclosure. Run-STARTING requests therefore renew first — and only they:
  // instantaneous reads/controls need an unexpired token, not a long-lived one.

  const runBody = {
    conversation_id: "conversation-1",
    expected_version: 9,
    agent_version: 7,
    strategy: "agentic" as const,
    config_hash: "f".repeat(64),
    capability_snapshot_hash: "c".repeat(64),
    input: "Review renewal risk",
    requested_model: "frontier-primary",
    requested_model_lane: "frontier-api",
  };
  const accepted = {
    run_id: "run-1",
    conversation_id: "conversation-1",
    version: 1,
    status: "submitted",
    enqueue_id: "enqueue-1",
    replayed: false,
  };
  const controlBody = {
    expected_version: 1,
    agent_version: 7,
    strategy: "agentic" as const,
    config_hash: "f".repeat(64),
    capability_snapshot_hash: "c".repeat(64),
  };

  function sseOk() {
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('event: done\ndata: {"type":"done"}\n\n'));
        controller.close();
      },
    });
    return new HttpResponse(stream, { headers: { "Content-Type": "text/event-stream" } });
  }

  /** Records the ORDER of the renew hook vs. the network so a test can prove
   * the renew happened strictly before the bearer was read and sent. */
  function orderedClient() {
    const order: string[] = [];
    const api = createApiClient({
      getAccessToken: () => {
        order.push("read-token");
        return TOKEN;
      },
      onUnauthorized: () => {},
      ensureFreshAccessToken: (minRemainingSeconds: number) => {
        order.push(`renew:${minRemainingSeconds}`);
        return Promise.resolve();
      },
    });
    return { api, order };
  }

  it("startAgentRun renews (with the 420s floor) strictly before reading the bearer", async () => {
    server.use(
      http.post("*/api/v1/agents/:agentId/runs", () =>
        HttpResponse.json(accepted, { status: 202 }),
      ),
    );
    const { api, order } = orderedClient();
    await api.startAgentRun("meridian", runBody, "key-1");
    expect(order[0]).toBe(`renew:${RUN_CREDENTIAL_MIN_REMAINING_SECONDS}`);
    expect(order).toContain("read-token");
    expect(RUN_CREDENTIAL_MIN_REMAINING_SECONDS).toBeGreaterThan(400);
  });

  it("retryAgentRun renews strictly before reading the bearer", async () => {
    server.use(
      http.post("*/api/v1/agents/:agentId/runs/:runId/retry", () =>
        HttpResponse.json(accepted, { status: 202 }),
      ),
    );
    const { api, order } = orderedClient();
    await api.retryAgentRun("meridian", "run-1", controlBody, "key-2");
    expect(order[0]).toBe(`renew:${RUN_CREDENTIAL_MIN_REMAINING_SECONDS}`);
    expect(order).toContain("read-token");
  });

  it("sendMessage (a chat turn starts a run) renews strictly before the stream opens", async () => {
    server.use(http.post("*/api/v1/chat/sessions/:id/messages", () => sseOk()));
    const { api, order } = orderedClient();
    await api.sendMessage("s1", "hi", () => {});
    expect(order[0]).toBe(`renew:${RUN_CREDENTIAL_MIN_REMAINING_SECONDS}`);
    expect(order).toContain("read-token");
  });

  it("a MISSING hook changes nothing — the legacy options shape still submits", async () => {
    let seen: string | null = null;
    server.use(
      http.post("*/api/v1/agents/:agentId/runs", ({ request }) => {
        seen = request.headers.get("authorization");
        return HttpResponse.json(accepted, { status: 202 });
      }),
    );
    await expect(client().startAgentRun("meridian", runBody, "key-3")).resolves.toMatchObject({
      run_id: "run-1",
    });
    expect(seen).toBe(`Bearer ${TOKEN}`);
  });

  it("a THROWING hook never blocks the submission (fail-open to the current token)", async () => {
    let seen: string | null = null;
    server.use(
      http.post("*/api/v1/agents/:agentId/runs", ({ request }) => {
        seen = request.headers.get("authorization");
        return HttpResponse.json(accepted, { status: 202 });
      }),
    );
    const api = createApiClient({
      getAccessToken: () => TOKEN,
      onUnauthorized: () => {},
      ensureFreshAccessToken: () => Promise.reject(new Error("renew unavailable")),
    });
    await expect(api.startAgentRun("meridian", runBody, "key-4")).resolves.toMatchObject({
      run_id: "run-1",
    });
    expect(seen).toBe(`Bearer ${TOKEN}`);
  });

  it("an instantaneous read does NOT renew — the guard is for run starts only", async () => {
    server.use(
      http.get("*/api/v1/chat/capabilities", () =>
        HttpResponse.json({ grounded: { agent_name: "opaque-grounded-agent" } }),
      ),
    );
    const { api, order } = orderedClient();
    await api.chatCapabilities();
    expect(order).toEqual(["read-token"]);
  });
});
