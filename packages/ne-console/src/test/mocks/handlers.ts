// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// MSW 2 handlers replaying the GOVERNED API shape (ne-serve/ne-gateway). These
// are the Phase-5 stand-in for the real backend; live full-stack e2e is parked
// to Phase 6. The `*` origin prefix matches whatever jsdom location is in use.
// The handlers enforce the trimming server-side: doc-secret is never returned.

import { HttpResponse, http } from "msw";
import type { RejectionCodeWire, RetrievalResult } from "../../api/types";
import {
  APPROVAL_PAYLOADS,
  AUDIT_EVENTS,
  CONNECT_QUARANTINE,
  CONNECT_STATUS,
  EVAL_RUNS,
  FULL_CORPUS,
  GRAPH_PATHS_FIXTURE,
  KPI_SUMMARY,
  NOW_ISO,
  PENDING_APPROVALS,
  PERMITTED_CHUNK_IDS,
  POSTURE,
  PRINCIPAL,
  REPLAY_RESULT,
  reviewEvent,
  RUNTIME_CONFIG,
  STEWARD_DIGEST,
  STEWARD_LATENCY,
  STEWARD_PROPOSALS,
  STEWARD_SKILLS,
  TRIMMED_CITATIONS,
  TRIMMED_RESULTS,
  VERIFICATION_OK,
  WORKFLOWS,
} from "./fixtures";
import {
  ONTOLOGY_CLASSES,
  ONTOLOGY_SHAPES,
  ONTOLOGY_VIOLATIONS,
  VERIFIED_INVOKE_RESULT,
  VERIFIED_TEMPLATES,
} from "./ontologyFixtures";
import { TRACE_ASSEMBLY, TRACE_ID } from "./traceFixtures";

/** Build an SSE ReadableStream from a list of (event, jsonData) frames. */
export function sseStream(frames: Array<{ event: string; data: unknown }>): ReadableStream {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const frame of frames) {
        const payload = `event: ${frame.event}\ndata: ${JSON.stringify(frame.data)}\n\n`;
        controller.enqueue(encoder.encode(payload));
      }
      controller.close();
    },
  });
}

/** Default streaming chat body: two token frames, a citations frame, then done. */
export function defaultChatFrames() {
  return [
    { event: "token", data: { type: "token", delta: "Hello" } },
    { event: "token", data: { type: "token", delta: ", world" } },
    {
      event: "citations",
      data: {
        type: "citations",
        citations: [
          {
            chunk_id: "chunk-alpha",
            document_id: "doc-alpha",
            source_system: "sharepoint",
            source_ref: "ref/doc-alpha",
            source_url: "https://sharepoint.example.com/doc-alpha",
          },
        ],
      },
    },
    { event: "done", data: { type: "done", session_id: "sess-1", output: "Hello, world" } },
  ];
}

export const handlers = [
  // NE-CONSOLE-ISSUER: the runtime OIDC config the SPA bootstraps from. This
  // default handler is LOAD-BEARING — setup.ts runs onUnhandledRequest:"error",
  // so any suite that renders AppBootstrap needs it to stay green.
  http.get("*/config.json", () => HttpResponse.json(RUNTIME_CONFIG)),

  http.get("*/api/v1/me", () => HttpResponse.json(PRINCIPAL)),

  // Optional and server-authored. Most tests exercise the ordinary deployment;
  // grounding-mode tests override this exact capability response.
  http.get("*/api/v1/chat/capabilities", () => HttpResponse.json({ grounded: null })),

  http.post("*/api/v1/chat/sessions", () =>
    HttpResponse.json({ session_id: "sess-1", agent_name: "console-chat" }),
  ),

  http.post("*/api/v1/chat/sessions/:id/messages", () => {
    return new HttpResponse(sseStream(defaultChatFrames()), {
      headers: { "Content-Type": "text/event-stream" },
    });
  }),

  // BD-15: LOAD-BEARING default for every /ask render under
  // onUnhandledRequest:"error". This is a server-shaped names-only catalog;
  // tests that exercise empty/error/frontier states override the whole served
  // response. No endpoint or client-computed availability appears here.
  http.get("*/api/v1/models/choices/:surface", ({ params }) => {
    const surface = params.surface as "ask" | "agentic-workflow";
    return HttpResponse.json({
      surface,
      task: surface === "ask" ? "reason" : "agentic-workflow",
      effective_default: "private-default",
      choices: [
        {
          model: "private-default",
          provider: "vllm",
          lane: "private-vllm",
          evidence: "private-serving-probe",
          observed_at: "2026-08-18T18:00:00Z",
        },
      ],
    });
  }),

  // C7 crux: the trimmed search NEVER returns doc-secret / its sentinel text.
  http.post("*/api/v1/brain/search", () => {
    const body: RetrievalResult = {
      chunks: TRIMMED_RESULTS,
      citations: TRIMMED_CITATIONS,
      graph_paths: GRAPH_PATHS_FIXTURE,
      provisional_source_count: 2,
      // 015A0-ENFORCEMENT: the server states completeness EXPLICITLY. The
      // console fails closed on a response that does not, so a central fixture
      // that omitted this would be asserting the pre-ENFORCEMENT wire.
      authorized_retrieval: {
        status: "complete",
        scanned_count: TRIMMED_RESULTS.length,
        returned_count: TRIMMED_RESULTS.length,
        scan_limit: 500,
      },
    };
    return HttpResponse.json(body);
  }),

  // Principal-scoped evidence replay: only permitted chunk_ids resolve; a
  // forbidden id (e.g. chunk-secret) yields an EMPTY RetrievalResult.
  http.post("*/api/v1/brain/retrieve", async ({ request }) => {
    const { chunk_ids } = (await request.json()) as { chunk_ids: string[] };
    const chunks = FULL_CORPUS.filter(
      (s) => chunk_ids.includes(s.chunk.chunk_id) && PERMITTED_CHUNK_IDS.has(s.chunk.chunk_id),
    );
    const body: RetrievalResult = {
      chunks,
      citations: [],
      graph_paths: [],
      provisional_source_count: 0,
      authorized_retrieval: {
        status: "complete",
        scanned_count: chunks.length,
        returned_count: chunks.length,
        scan_limit: 500,
      },
    };
    return HttpResponse.json(body);
  }),

  // --- NE-062 ops surfaces -------------------------------------------------
  // Handlers stay STATELESS: per-test list mutations use server.use() overrides
  // so a test proves a decided approval leaves the list only via a REFETCHED
  // server response, never a local splice.

  http.get("*/api/v1/approvals/pending", () => HttpResponse.json({ approvals: PENDING_APPROVALS })),

  // Owner ruling 2026-07-31 (NE-APPROVAL-PAYLOAD): the governed, per-request
  // disclosure read. 'req-payload-withheld' is the non-named-approver control
  // — the server names the policy in `detail`, verbatim, and carries NO
  // payload fields. Every other id falls through to its APPROVAL_PAYLOADS
  // fixture (404 if unseeded — no silent empty-body 200).
  http.get("*/api/v1/approvals/:id/payload", ({ params }) => {
    const id = params.id as string;
    if (id === "req-payload-withheld") {
      return HttpResponse.json(
        {
          detail:
            "payload withheld: not a named approver for this request (Tier-3 payload is " +
            "disclosed only to the configured named-approver list, size 2; other approvers " +
            "may see the entry exists but not read it)",
        },
        { status: 403 },
      );
    }
    const fixture = APPROVAL_PAYLOADS[id];
    if (!fixture) {
      return HttpResponse.json({ detail: "approval request not found" }, { status: 404 });
    }
    return HttpResponse.json(fixture);
  }),

  http.post("*/api/v1/approvals/:id/approve", ({ params }) => {
    const id = params.id as string;
    if (id === "req-forbidden") {
      // The server rejects an approver not authorized for this request; the
      // reason travels in `detail`. Tier-3 authority stays IN the gate.
      return HttpResponse.json(
        { detail: "approver is not in the Tier-3 named-approver list" },
        { status: 403 },
      );
    }
    if (id === "req-t3-wire") {
      // Tier-3 first check: the gate returns None → wire status pending_second
      // (awaiting the distinct second approver). No approver decided yet.
      return HttpResponse.json({ status: "pending_second", approver: null, decided_at: null });
    }
    return HttpResponse.json({
      status: "approved",
      approver: "alice@example.com",
      decided_at: NOW_ISO,
    });
  }),

  http.post("*/api/v1/approvals/:id/reject", () =>
    HttpResponse.json({ status: "rejected", approver: "alice@example.com", decided_at: NOW_ISO }),
  ),

  http.get("*/api/v1/audit/events", () =>
    HttpResponse.json({ events: AUDIT_EVENTS, verification: VERIFICATION_OK }),
  ),

  http.get("*/api/v1/audit/replay", () => HttpResponse.json(REPLAY_RESULT)),

  // DirectoryProvider mounts in the authenticated shell. Individual directory
  // tests override this with named rows; the global default discloses nothing.
  http.post("*/api/v1/principals/resolve", () => HttpResponse.json({ resolved: [] })),

  // NE-WORKFLOWS: the list this deployment offers. The RUN is deliberately NOT
  // handled by default — it blocks for ~30s per subject against the real
  // server, so a test that wants one states so and serves its own.
  http.get("*/api/v1/workflows", () => HttpResponse.json(WORKFLOWS)),

  // NE-167b: GET /v1/trace/{trace_id} — the Logfire-style trace assembly.
  // Unknown ids 404 honestly (§0 law 5); TRACE_ID is the only seeded run.
  http.get("*/api/v1/trace/:traceId", ({ params }) =>
    params.traceId === TRACE_ID
      ? HttpResponse.json(TRACE_ASSEMBLY)
      : HttpResponse.json({ detail: "trace not found" }, { status: 404 }),
  ),

  http.get("*/api/v1/eval/runs", ({ request }) => {
    const suite = new URL(request.url).searchParams.get("suite");
    const runs = suite ? EVAL_RUNS.filter((r) => r.suite === suite) : EVAL_RUNS;
    return HttpResponse.json({ runs });
  }),

  http.get("*/api/v1/kpi/summary", () => HttpResponse.json(KPI_SUMMARY)),

  // NE-131: LOAD-BEARING default — PostureProvider mounts in the authed shell and
  // setup.ts runs onUnhandledRequest:"error", so every App-rendering suite hits it.
  http.get("*/api/v1/posture", () => HttpResponse.json(POSTURE)),

  // NE-SERVING-STATUS: LOAD-BEARING default — the Models screen's serving panel
  // asks this read wherever /models renders (onUnhandledRequest:"error"). The
  // default deployment shape is "no probeable lane" ⇒ the panel renders nothing
  // and every pre-existing /models assertion is untouched; suites that exercise
  // the panel serve their own lanes via server.use() or a stubbed client.
  http.get("*/api/v1/models/serving", () => HttpResponse.json({ lanes: [] })),

  // NE-132: LOAD-BEARING default — ConnectPage mounts on /connect and setup.ts runs
  // onUnhandledRequest:"error", so the App /connect suite needs it to stay green.
  http.get("*/api/v1/connect/status", () => HttpResponse.json(CONNECT_STATUS)),

  // Governed Sync now default: metadata/counts only. Tests that prove queued
  // polling and refusal override this exact path.
  http.post("*/api/v1/connect/:source/sync", ({ params }) =>
    HttpResponse.json(
      {
        request_id: "00000000-0000-4000-8000-000000000001",
        source: String(params.source),
        status: "succeeded",
        created_at: "2026-08-19T12:00:00Z",
        expires_at: "2026-08-19T12:05:00Z",
        counts: {
          mode: "incremental",
          records_seen: 1,
          written: 1,
          skipped_unchanged: 0,
          invalid: 0,
          quarantined: 0,
        },
        error_class: null,
      },
      { status: 202 },
    ),
  ),

  // NE-CONNECT-CSV: LOAD-BEARING default — ConnectPage also issues the ops-gated
  // drill-down read on mount (onUnhandledRequest:"error"). The default serves the
  // rows behind CONNECT_STATUS's counts; suites proving the refused rendering
  // override with a 403 via server.use().
  http.get("*/api/v1/connect/quarantine", () => HttpResponse.json(CONNECT_QUARANTINE)),

  // --- NE-067 steward surfaces ---------------------------------------------
  // Handlers stay STATELESS: per-test list mutations use server.use()
  // overrides so a test proves a decided proposal leaves the queue only via a
  // REFETCHED server response, never a local splice (mirrors NE-062).

  http.get("*/api/v1/steward/proposals", () => HttpResponse.json({ proposals: STEWARD_PROPOSALS })),

  http.post("*/api/v1/steward/proposals/:id/approve", ({ params }) => {
    const id = params.id as string;
    if (id === "prop-conflict") {
      // Models InvalidTransitionError: illegal transition from the current
      // (already-terminal) status.
      return HttpResponse.json(
        {
          detail:
            "cannot approve proposal 'prop-conflict' from status 'rejected' (illegal transition)",
        },
        { status: 409 },
      );
    }
    return HttpResponse.json(
      reviewEvent({
        proposal_id: id,
        action: "approve",
        from_status: "proposed",
        to_status: "approved",
      }),
    );
  }),

  http.post("*/api/v1/steward/proposals/:id/reject", async ({ params, request }) => {
    const id = params.id as string;
    const body = (await request.json()) as { code: RejectionCodeWire; note?: string };
    return HttpResponse.json(
      reviewEvent({
        proposal_id: id,
        action: "reject",
        from_status: "proposed",
        to_status: "rejected",
        rejection_code: body.code,
        note: body.note ?? null,
      }),
    );
  }),

  http.post("*/api/v1/steward/proposals/:id/apply", async ({ params, request }) => {
    const id = params.id as string;
    const body = (await request.json()) as { applied_ref: string; note?: string };
    if (!body.applied_ref) {
      return HttpResponse.json(
        { detail: "applied_ref must be a non-empty reference" },
        { status: 422 },
      );
    }
    return HttpResponse.json(
      reviewEvent({
        proposal_id: id,
        action: "apply",
        from_status: "approved",
        to_status: "applied",
        applied_ref: body.applied_ref,
        note: body.note ?? null,
      }),
    );
  }),

  http.post("*/api/v1/steward/proposals/:id/apply-skill", async ({ params, request }) => {
    const id = params.id as string;
    const body = (await request.json()) as {
      acl_labels: string[];
      global_visibility: boolean;
      note?: string;
    };
    // R-22: labels XOR global (mirrors the server's fail-closed refusal).
    if (body.acl_labels.length > 0 === body.global_visibility) {
      return HttpResponse.json(
        { detail: "supply EITHER acl_labels OR global_visibility" },
        { status: 422 },
      );
    }
    return HttpResponse.json({
      review: reviewEvent({
        proposal_id: id,
        action: "apply",
        from_status: "approved",
        to_status: "applied",
        applied_ref: "skill:deploy-runbook@v1",
        note: body.note ?? null,
      }),
      name: "deploy-runbook",
      version: 1,
      content_hash: "deadbeef",
      acl_labels: body.acl_labels,
    });
  }),

  http.get("*/api/v1/steward/digest", () => HttpResponse.json(STEWARD_DIGEST)),

  http.get("*/api/v1/steward/latency", () => HttpResponse.json(STEWARD_LATENCY)),

  http.get("*/api/v1/steward/skills", () => HttpResponse.json({ skills: STEWARD_SKILLS })),

  // --- NE-136 T3 drawer surfaces -------------------------------------------
  // LOAD-BEARING defaults: OntologyTab / VerifiedAnswersTab / ConformanceTab
  // fetch on mount, so EVERY suite that renders KnowledgePage (KnowledgePage
  // .test.tsx, trace.test.tsx, App.test.tsx /knowledge) now hits these routes
  // under onUnhandledRequest:"error". Per-test overrides use server.use().
  http.get("*/api/v1/ontology/classes", () => HttpResponse.json(ONTOLOGY_CLASSES)),

  http.get("*/api/v1/ontology/shapes", () => HttpResponse.json(ONTOLOGY_SHAPES)),

  http.get("*/api/v1/ontology/violations", () => HttpResponse.json(ONTOLOGY_VIOLATIONS)),

  http.get("*/api/v1/verified/templates", () => HttpResponse.json(VERIFIED_TEMPLATES)),

  http.post("*/api/v1/verified/invoke", () => HttpResponse.json(VERIFIED_INVOKE_RESULT)),
];
