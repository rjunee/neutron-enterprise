// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The shared audit poll (design §6.7). Proves routeForAuditEvent's mapping table,
// that the provider exposes the server verification + latestSeq, that first load
// pulses NOTHING, that a later higher-seq event pulses its mapped route, and the
// role-degradation / error paths (403 → degraded with a stretched interval and
// recovery; 5xx → error with backoff). Real timers + a small intervalMs + waitFor
// per the brief (MSW + fake timers interact badly).

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { HttpResponse, http } from "msw";
import { AuthContext } from "react-oidc-context";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PrincipalState } from "../auth/PrincipalContext";
import { AuthStub, makeAuthValue } from "../test/authStub";
import { AUDIT_EVENTS, VERIFICATION_BROKEN, VERIFICATION_OK } from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import {
  AuditPollProvider,
  routeForAuditEvent,
  TRANSIT_ROUTES,
  useApprovalDecisionAudit,
  useAuditPoll,
} from "./useAuditPoll";

const principalState = vi.hoisted(() => ({
  current: {
    status: "ok" as const,
    principal: { id: "alice@example.com", groups: ["ops"] },
  } as PrincipalState,
}));

vi.mock("../auth/PrincipalContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../auth/PrincipalContext")>()),
  usePrincipal: () => principalState.current,
}));

beforeEach(() => {
  principalState.current = {
    status: "ok",
    principal: { id: "alice@example.com", groups: ["ops"] },
  };
});

function OwnerAuth({ token, children }: { token: string; children: ReactNode }) {
  const base = makeAuthValue();
  return (
    <AuthContext.Provider
      value={{ ...base, user: { ...base.user!, access_token: token } as typeof base.user }}
    >
      {children}
    </AuthContext.Provider>
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((ok) => {
    resolve = ok;
  });
  return { promise, resolve };
}

function Probe() {
  const poll = useAuditPoll();
  return (
    <div>
      <span data-testid="status">{poll.status}</span>
      <span data-testid="seq">{poll.latestSeq ?? "null"}</span>
      <span data-testid="valid">
        {poll.verification ? String(poll.verification.valid) : "none"}
      </span>
      <span data-testid="transits">{poll.transits.join(",")}</span>
      <span data-testid="transit-events">
        {poll.transitEvents.map((t) => `${t.seq}:${t.route}`).join(",")}
      </span>
    </div>
  );
}

function renderPoll(intervalMs = 15) {
  return render(
    <AuthStub>
      <AuditPollProvider intervalMs={intervalMs}>
        <Probe />
      </AuditPollProvider>
    </AuthStub>,
  );
}

/** Folds one read on demand and shows what `observe` said about it — the answer
 * /trace bounds its re-reads on. */
function ObserveProbe({ record }: { record: (folded: boolean) => void }) {
  const poll = useAuditPoll();
  return (
    <div>
      <span data-testid="verdict">{poll.verdict}</span>
      <button type="button" onClick={() => record(poll.observe(VERIFICATION_BROKEN))}>
        Fold
      </button>
    </div>
  );
}

function renderObserveProbe(record: (folded: boolean) => void) {
  return render(
    <AuthStub>
      <AuditPollProvider intervalMs={60_000}>
        <ObserveProbe record={record} />
      </AuditPollProvider>
    </AuthStub>,
  );
}

/** The other discard: a caller with no verification in hand at all. */
function NullObserveProbe({ record }: { record: (folded: boolean) => void }) {
  const { observe } = useAuditPoll();
  return (
    <button type="button" onClick={() => record(observe(null))}>
      Fold null
    </button>
  );
}

function DecisionProbe({ requestId, enabled = true }: { requestId: string; enabled?: boolean }) {
  const decision = useApprovalDecisionAudit(requestId, enabled);
  return (
    <div>
      <span data-testid="decision-status">{decision.status}</span>
      <span data-testid="decision-seq">{decision.status === "found" ? decision.seq : "none"}</span>
    </div>
  );
}

function renderDecisionProbe(requestId = "req-1", enabled = true) {
  return render(
    <AuthStub>
      <DecisionProbe requestId={requestId} enabled={enabled} />
    </AuthStub>,
  );
}

describe("routeForAuditEvent (§6.7 mapping table)", () => {
  // The expected routes are the CURRENT §1.2 nav destinations, not the
  // pre-rename paths (/connect, /chat, …), which survive only as App.tsx
  // redirects. The mapping feeds the rail's transit pulse: a route no nav item
  // carries is a pulse that can never render.
  const cases: Array<[string, string | null, string]> = [
    ["connector.sync", "ingest", "/sources"],
    ["brain.search", "query", "/search"],
    ["graph.sync", null, "/knowledge/graph"],
    ["ontology.read", "unauthorized", "/knowledge/ontology"],
    ["knowledge.verified_read", null, "/verified-queries"],
    ["verified.invoke", null, "/verified-queries"],
    ["knowledge.retrieve", null, "/search"],
    ["knowledge.upsert", null, "/knowledge/documents"],
    ["document.journey", null, "/knowledge/documents"],
    // The chat/agent/session stage transits to /agents: Ask left the rail on
    // 2026-08-27 and a pulse keyed to a non-destination can never render --
    // which is the exact defect this table was written to pin.
    ["chat.turn", "session.start", "/agents"],
    ["approval.decide", "approve", "/approvals"],
    ["steward.review", "proposal.approve", "/improvement"],
    ["eval.run", null, "/evaluations"],
    ["kpi.record", null, "/insights"],
    ["config.freeze", "posture", "/posture"],
    ["something.unknown", null, "/audit"],
  ];
  it.each(cases)("maps %s/%s → %s", (event_type, action, expected) => {
    expect(routeForAuditEvent({ event_type, action })).toBe(expected);
  });

  it("covers the exact derived transit codomain", () => {
    expect(new Set(cases.map(([, , route]) => route))).toEqual(new Set(TRANSIT_ROUTES));
    expect(TRANSIT_ROUTES).toHaveLength(13);
  });
});

describe("AuditPollProvider", () => {
  it("waits for the exact principal owner, then starts polling on loading to ready", async () => {
    let calls = 0;
    server.use(
      http.get("*/api/v1/audit/events", () => {
        calls += 1;
        return HttpResponse.json({ events: AUDIT_EVENTS, verification: VERIFICATION_OK });
      }),
    );
    principalState.current = { status: "loading", principal: null };
    const authValue = makeAuthValue();
    const tree = () => (
      <AuthContext.Provider value={authValue}>
        <AuditPollProvider intervalMs={100_000}>
          <Probe />
        </AuditPollProvider>
      </AuthContext.Provider>
    );
    const view = render(tree());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(calls).toBe(0);
    expect(screen.getByTestId("status")).toHaveTextContent("loading");

    principalState.current = {
      status: "ok",
      principal: { id: "alice@example.com", groups: ["ops"] },
    };
    view.rerender(tree());
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("ok"));
    expect(calls).toBe(1);
    expect(screen.getByTestId("seq")).toHaveTextContent("3");
  });

  it("polls and exposes the server verification + latestSeq, with NO pulses on first load", async () => {
    renderPoll();
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("ok"));
    expect(screen.getByTestId("valid")).toHaveTextContent("true");
    expect(screen.getByTestId("seq")).toHaveTextContent("3");
    // First load never fabricates a transit pulse.
    expect(screen.getByTestId("transits")).toHaveTextContent("");
    // …nor a per-event transit entry.
    expect(screen.getByTestId("transit-events")).toHaveTextContent("");
  });

  it("surfaces one transitEvent PER fresh event while transits dedupes routes", async () => {
    renderPoll();
    await waitFor(() => expect(screen.getByTestId("seq")).toHaveTextContent("3"));

    // Two NEW same-route events (seq 4 and 5) arrive on the next poll — both
    // steward acts routing to /improvement.
    const steward = (seq: number) => ({
      ...AUDIT_EVENTS[0],
      id: seq,
      seq,
      event_id: `evt-${seq}`,
      event_type: "steward.proposal.approved",
      action: "steward.approve",
    });
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json(
          { events: [steward(4), steward(5)], verification: VERIFICATION_OK },
          { status: 200 },
        ),
      ),
    );

    // transitEvents carries BOTH events (count-accurate); transits dedupes to one route.
    //
    // BOTH assertions live inside ONE waitFor deliberately. `transits` and
    // `transitEvents` are single-poll pulses: the very next poll sees nothing
    // newer than seq 5, so `freshEvents` is empty and the same setState writes
    // both back to []. Asserting the second one on a later line races that tick
    // — it read "" under load while passing on an idle machine, which is a flake
    // waiting to happen in CI rather than a fact about the hook.
    await waitFor(() => {
      expect(screen.getByTestId("transit-events")).toHaveTextContent(
        "4:/improvement,5:/improvement",
      );
      expect(screen.getByTestId("transits")).toHaveTextContent("/improvement");
    });
  });

  it("pulses the mapped route when a later poll surfaces a higher-seq event", async () => {
    renderPoll();
    await waitFor(() => expect(screen.getByTestId("seq")).toHaveTextContent("3"));

    // A NEW event (seq 4) arrives on the next poll — a connector sync → /sources.
    const newEvent = {
      ...AUDIT_EVENTS[0],
      id: 4,
      seq: 4,
      event_id: "evt-4",
      event_type: "connector.sync",
      action: "sync",
    };
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({ events: [newEvent], verification: VERIFICATION_OK }),
      ),
    );

    await waitFor(() => expect(screen.getByTestId("transits")).toHaveTextContent("/sources"));
    expect(screen.getByTestId("seq")).toHaveTextContent("4");
  });

  it("clears a prior verdict on 403 and recovers when the handler succeeds again", async () => {
    renderPoll();
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("ok"));
    expect(screen.getByTestId("valid")).toHaveTextContent("true");

    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({ detail: "requires ops role" }, { status: 403 }),
      ),
    );
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("degraded"));
    expect(screen.getByTestId("valid")).toHaveTextContent("none");

    // Recovery: restore the default success handler.
    server.resetHandlers();
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("ok"), {
      timeout: 2000,
    });
    expect(screen.getByTestId("valid")).toHaveTextContent("true");
  });

  it("clears a prior verdict and goes to 'error' on a 5xx", async () => {
    renderPoll();
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("ok"));
    expect(screen.getByTestId("valid")).toHaveTextContent("true");

    server.use(http.get("*/api/v1/audit/events", () => HttpResponse.json({}, { status: 500 })));
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("error"));
    expect(screen.getByTestId("valid")).toHaveTextContent("none");
  });

  // -------------------------------------------------------------------------
  // `observe` REPORTS WHETHER IT FOLDED — the answer a caller's own re-read
  // budget has to be spent on.
  //
  // The latch is session-bound, so a read taken before /v1/me resolves is
  // discarded here (it would be dropped by the next render anyway). /trace
  // re-reads on a bounded budget to settle a disagreeing verdict, and it was
  // counting ATTEMPTS: on a cold deep link where /v1/me was slow the budget was
  // gone before a single countable read had been taken. A caller cannot see the
  // discard from the outside, so this call has to say so.
  // -------------------------------------------------------------------------

  it("reports a read taken before the principal resolved as NOT folded", async () => {
    principalState.current = { status: "loading", principal: null };
    const folded: boolean[] = [];
    renderObserveProbe((v) => folded.push(v));

    // No session yet, so nothing here is bound to one.
    expect(screen.getByTestId("verdict")).toHaveTextContent("unknown");
    await userEvent.click(screen.getByRole("button", { name: "Fold" }));

    expect(folded).toEqual([false]);
    // …and the discard is real: the latch did not move.
    expect(screen.getByTestId("verdict")).toHaveTextContent("unknown");
  });

  it("reports a read folded under a resolved principal as folded", async () => {
    const folded: boolean[] = [];
    renderObserveProbe((v) => folded.push(v));

    await userEvent.click(screen.getByRole("button", { name: "Fold" }));

    expect(folded).toEqual([true]);
    // The disagreeing read landed in the run, which is what makes it countable.
    await waitFor(() => expect(screen.getByTestId("verdict")).toHaveTextContent("reverifying"));
  });

  it("reports a null read as not folded — there was nothing to fold", async () => {
    const folded: boolean[] = [];
    render(
      <AuthStub>
        <AuditPollProvider intervalMs={60_000}>
          <NullObserveProbe record={(v) => folded.push(v)} />
        </AuditPollProvider>
      </AuthStub>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Fold null" }));
    expect(folded).toEqual([false]);
  });
});

describe("useApprovalDecisionAudit", () => {
  it("keeps the same request id's late Alice audit row out of Bob's receipt", async () => {
    const alice = deferred<Response>();
    const bob = deferred<Response>();
    server.use(
      http.get("*/api/v1/audit/approval-decisions/:id", async ({ request }) =>
        request.headers.get("authorization")?.endsWith("alice-token") ? alice.promise : bob.promise,
      ),
    );
    const tree = (token: string) => (
      <OwnerAuth token={token}>
        <DecisionProbe requestId="shared-r" />
      </OwnerAuth>
    );
    const view = render(tree("alice-token"));
    expect(screen.getByTestId("decision-status")).toHaveTextContent("checking");

    principalState.current = {
      status: "ok",
      principal: { id: "bob@example.com", groups: ["ops"] },
    };
    view.rerender(tree("bob-token"));
    expect(screen.getByTestId("decision-status")).toHaveTextContent("checking");
    alice.resolve(HttpResponse.json({ ...AUDIT_EVENTS[0], seq: 111 }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByTestId("decision-status")).toHaveTextContent("checking");
    expect(screen.getByTestId("decision-seq")).toHaveTextContent("none");

    bob.resolve(HttpResponse.json({ ...AUDIT_EVENTS[0], seq: 222 }));
    await waitFor(() => expect(screen.getByTestId("decision-status")).toHaveTextContent("found"));
    expect(screen.getByTestId("decision-seq")).toHaveTextContent("222");
  });

  it("renders the exact server-served chain sequence", async () => {
    server.use(
      http.get("*/api/v1/audit/approval-decisions/:id", () =>
        HttpResponse.json({ ...AUDIT_EVENTS[0], seq: 4812 }),
      ),
    );
    renderDecisionProbe();
    await waitFor(() => expect(screen.getByTestId("decision-status")).toHaveTextContent("found"));
    expect(screen.getByTestId("decision-seq")).toHaveTextContent("4812");
  });

  it("distinguishes the server's 404 miss from a lookup failure", async () => {
    server.use(
      http.get("*/api/v1/audit/approval-decisions/:id", () =>
        HttpResponse.json({ detail: "not found" }, { status: 404 }),
      ),
    );
    const view = renderDecisionProbe();
    await waitFor(() =>
      expect(screen.getByTestId("decision-status")).toHaveTextContent("not_found"),
    );

    server.use(
      http.get("*/api/v1/audit/approval-decisions/:id", () =>
        HttpResponse.json({}, { status: 500 }),
      ),
    );
    view.rerender(
      <AuthStub>
        <DecisionProbe requestId="req-2" />
      </AuthStub>,
    );
    await waitFor(() => expect(screen.getByTestId("decision-status")).toHaveTextContent("failure"));
  });

  it("does not make the governed read when chain access is unavailable", () => {
    let requests = 0;
    server.use(
      http.get("*/api/v1/audit/approval-decisions/:id", () => {
        requests += 1;
        return HttpResponse.json({ ...AUDIT_EVENTS[0], seq: 4812 });
      }),
    );
    renderDecisionProbe("req-1", false);
    expect(screen.getByTestId("decision-status")).toHaveTextContent("idle");
    expect(requests).toBe(0);
  });
});

/* ===========================================================================
 * THE RE-VERIFY SWEEP ASKS FOR A DIFFERENT PROOF, NOT JUST ANOTHER READ.
 *
 * The ordinary poll gets an `anchored-extension` verdict — the rows appended
 * since a head a full `verify_chain` blessed up to fifteen minutes ago, and
 * nothing else. Every break this console can latch is confirmed off a
 * `full-chain` verdict, and `clearLatch` refuses to withdraw a finding on a
 * weaker proof, so a sweep taken on the default read could never clear the very
 * latch the operator clicked the button about.
 * ======================================================================== */

describe("the sweep requests the full walk", () => {
  /** Reads the poll, and exposes the one control that can clear a latch. */
  function SweepProbe() {
    const poll = useAuditPoll();
    return (
      <div>
        <span data-testid="status">{poll.status}</span>
        <button type="button" onClick={poll.reverify}>
          Re-verify
        </button>
      </div>
    );
  }

  function renderSweepProbe(intervalMs = 60_000) {
    return render(
      <AuthStub>
        <AuditPollProvider intervalMs={intervalMs}>
          <SweepProbe />
        </AuditPollProvider>
      </AuthStub>,
    );
  }

  it("carries full_verify ONLY on the sweep, never on the interval poll", async () => {
    const asked: (string | null)[] = [];
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        asked.push(new URL(request.url).searchParams.get("full_verify"));
        return HttpResponse.json({ events: AUDIT_EVENTS, verification: VERIFICATION_OK });
      }),
    );
    const user = userEvent.setup();
    renderSweepProbe();
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("ok"));
    // A poll on a timer must never put an O(chain) walk on the server — and it
    // does not even send the parameter, so the server's default is the only
    // thing that can answer it.
    expect(asked).toEqual([null]);

    await user.click(screen.getByRole("button", { name: "Re-verify" }));
    await waitFor(() => expect(asked.length).toBeGreaterThan(1));
    expect(asked[asked.length - 1]).toBe("true");
  });

  it("a sweep that never reached a verdict is not spent", async () => {
    // The request is what carries the flag, so a failed sweep must put itself
    // back — otherwise the retry silently downgrades to an anchored read and the
    // operator's click is consumed by a poll that could not have cleared
    // anything.
    let reads = 0;
    const asked: (string | null)[] = [];
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        reads += 1;
        const full = new URL(request.url).searchParams.get("full_verify");
        if (full === "true") {
          asked.push(full);
          if (asked.length === 1) return HttpResponse.json({ detail: "boom" }, { status: 502 });
        }
        return HttpResponse.json({ events: AUDIT_EVENTS, verification: VERIFICATION_OK });
      }),
    );
    const user = userEvent.setup();
    renderSweepProbe(15);
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("ok"));

    await user.click(screen.getByRole("button", { name: "Re-verify" }));
    await waitFor(() => expect(asked.length).toBeGreaterThan(1), { timeout: 3000 });
    expect(reads).toBeGreaterThan(2);
  });
});
