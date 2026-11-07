// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §3 — the palette's ranking, at the pure seam.
//
// Two properties matter and both are easy to lose silently: a typed id is ALWAYS
// the top result (otherwise pasting a request id offers you the Approvals screen
// instead of the request), and a role-refused destination is never reachable
// here (otherwise the palette is a hole in the same boundary the rail respects).

import { describe, expect, it } from "vitest";
import { parseAnchorParam } from "../audit/ledger";
import { matchesProposalParam } from "../flywheel/queue";
import { hydrateRecents, idResults, PALETTE_ACTIONS, paletteResults } from "./paletteIndex";
import type { PaletteResult } from "./paletteIndex";

const DESTINATIONS = [
  { label: "Home", to: "/" },
  { label: "Governance › Audit log", to: "/audit" },
  { label: "Operations › Evaluations", to: "/evaluations" },
  { label: "Workspace › Ask", to: "/ask" },
];

const APPROVAL_ID = "a2bf6cea-2f1d-4a77-9c31-5f0e2b8d4411";
/** The only form that resolves: the operator names the kind, the palette does not. */
const APPROVAL_QUERY = `approval ${APPROVAL_ID}`;
const TRACE_ID = "a2bf6cea2f1d4a779c315f0e2b8d4411";
const PROPOSAL_ID = `sha256:${"a".repeat(64)}`;

function resolvedUrl(result: PaletteResult | undefined): URL {
  expect(result).toBeDefined();
  expect(result?.to).not.toBeNull();
  return new URL(result!.to!, "https://console.invalid");
}

describe("idResults — the typed-id grammars", () => {
  it("routes each deployment-issued id shape to the object, labelled with its type", () => {
    expect(idResults(APPROVAL_QUERY)[0]).toMatchObject({
      kind: "Approval request",
      to: `/approvals?request=${APPROVAL_ID}`,
    });
    expect(idResults(TRACE_ID)[0]).toMatchObject({ kind: "Trace", to: `/trace/${TRACE_ID}` });
    expect(idResults(PROPOSAL_ID)[0]).toMatchObject({
      kind: "Proposal",
      to: `/improvement?proposal=${encodeURIComponent(PROPOSAL_ID)}`,
    });
    expect(idResults("seq 4812")[0]).toMatchObject({
      kind: "Audit entry",
      to: "/audit?seq=4812",
    });
  });

  it("round-trips every result through the destination resolver it targets", () => {
    const approval = resolvedUrl(idResults(APPROVAL_QUERY)[0]!);
    expect(approval.pathname).toBe("/approvals");
    expect(approval.searchParams.get("request")).toBe(APPROVAL_ID);

    const trace = resolvedUrl(idResults(TRACE_ID)[0]!);
    expect(trace.pathname).toBe(`/trace/${TRACE_ID}`);
    expect(decodeURIComponent(trace.pathname.slice("/trace/".length))).toBe(TRACE_ID);

    const proposal = resolvedUrl(idResults(PROPOSAL_ID)[0]!);
    const proposalParam = proposal.searchParams.get("proposal");
    expect(proposalParam).toBe(PROPOSAL_ID);
    expect(matchesProposalParam(PROPOSAL_ID, proposalParam!)).toBe(true);

    const audit = resolvedUrl(idResults("seq 4812")[0]!);
    expect(parseAnchorParam(audit.searchParams.get("seq"))).toEqual({ kind: "seq", seq: 4812 });
  });

  it("rejects the fixture-only prefixes and malformed near-misses without navigating", () => {
    for (const query of [
      "req-t2-wire",
      "trace-1",
      "prop-er-4",
      `approval ${APPROVAL_ID.toUpperCase()}`,
      "approval a2bf6cea-2f1d-1a77-9c31-5f0e2b8d4411",
      "approval",
      "approval whatever",
      // Naming a kind does not widen it: the 32-hex trace grammar is a DIFFERENT
      // id space, and `approval <trace id>` must resolve to neither.
      `approval ${TRACE_ID}`,
      `${TRACE_ID.slice(0, -1)}g`,
      TRACE_ID.toUpperCase(),
      `sha256:${"a".repeat(63)}`,
      `sha256:${"A".repeat(64)}`,
      "seq 0",
      "seq -1",
      "seq 1.5",
      "seq 9007199254740992",
    ]) {
      expect(idResults(query), query).toEqual([]);
    }
  });

  it("resolves a bare canonical UUID to no object of any kind, approval requests included", () => {
    // THE DEFECT THIS OWNS. A bare `uuid4()` used to be read as an approval
    // request id and routed to `/approvals?request=…`. The runtime mints
    // approval request ids (approvals.py), run ids (run_store.py `_id`),
    // conversation ids (store.py) and job ids (scheduler.py) from that same
    // `str(uuid.uuid4())`, so the shape names four id spaces and identifies
    // none of them — pasting a run id landed on an approval lookup, confidently
    // and with nothing on screen to say a kind had been inferred.
    expect(idResults(APPROVAL_ID)).toEqual([]);
    // Not a property of THIS uuid: any canonical v4 is equally unresolvable,
    // whichever id space actually issued it.
    for (const uuid of [
      "00000000-0000-4000-8000-000000000001",
      "c21456b9-c8dc-4458-b393-bc3230db0081",
      "a522d327-30d2-4d68-9e34-a8395eed7b26",
    ]) {
      expect(idResults(uuid), uuid).toEqual([]);
    }
  });

  it("resolves the disambiguated approval form on the SAME uuid it refuses bare", () => {
    // The control that keeps the assertion above from passing by the palette
    // having simply stopped resolving anything: one string, one word of
    // difference, and only the form that names its kind navigates.
    expect(idResults(APPROVAL_ID)).toEqual([]);
    expect(idResults(APPROVAL_QUERY)).toEqual([
      {
        id: `id:approval:${APPROVAL_ID}`,
        label: APPROVAL_ID,
        kind: "Approval request",
        group: "id",
        to: `/approvals?request=${APPROVAL_ID}`,
      },
    ]);
  });

  it("does NOT guess: an unrecognised string matches no grammar", () => {
    // A catch-all "looks like an id" rule routes an operator to the wrong screen
    // with total confidence. Falling through to the destinations is recoverable.
    expect(idResults("wire transfer")).toEqual([]);
    expect(idResults("4812")).toEqual([]);
    expect(idResults("")).toEqual([]);
  });
});

describe("paletteResults", () => {
  it("puts a typed id above every destination and action", () => {
    const out = paletteResults({ query: APPROVAL_QUERY, destinations: DESTINATIONS, recents: [] });
    expect(out[0]?.group).toBe("id");
  });

  it("offers a bare canonical UUID no row onto Approvals, with the screen reachable", () => {
    // Reachability is the point of the setup: Approvals is in the caller's
    // filtered list, so a row onto it is available to leak and is absent
    // because the grammar refuses to guess, not because the screen is hidden.
    const withApprovals = [...DESTINATIONS, { label: "Governance › Approvals", to: "/approvals" }];
    const bare = paletteResults({ query: APPROVAL_ID, destinations: withApprovals, recents: [] });
    expect(bare).toEqual([]);

    // The control on the same reachable list: naming the kind resolves, so the
    // emptiness above is this query's, not the fixture's.
    const named = paletteResults({
      query: APPROVAL_QUERY,
      destinations: withApprovals,
      recents: [],
    });
    expect(named[0]).toMatchObject({
      group: "id",
      kind: "Approval request",
      to: `/approvals?request=${APPROVAL_ID}`,
    });
  });

  it("indexes only the Agents destination, never an Agent, Conversation or Run instance", () => {
    const destinations = [...DESTINATIONS, { label: "Execution › Agents", to: "/agents" }];
    expect(paletteResults({ query: "agents", destinations, recents: [] })).toEqual([
      {
        id: "dest:/agents",
        label: "Execution › Agents",
        kind: "Destination",
        group: "destination",
        to: "/agents",
      },
    ]);
    expect(paletteResults({ query: APPROVAL_ID, destinations, recents: [] })).toEqual([]);
    expect(paletteResults({ query: "Meridian Analyst", destinations, recents: [] })).toEqual([]);
  });

  it("ranks a prefix match above a substring match", () => {
    // "Audit log" STARTS WITH "a"; "Search" merely contains one. Typing "a"
    // should offer the destination whose name begins that way first — the
    // opposite order is what makes a palette feel like it is guessing.
    const both = [
      { label: "Search", to: "/search" },
      { label: "Governance › Audit log", to: "/audit" },
    ];
    const order = paletteResults({ query: "a", destinations: both, recents: [] }).map((r) => r.to);
    expect(order).toContain("/audit");
    expect(order).toContain("/search");
    expect(order.indexOf("/audit")).toBeLessThan(order.indexOf("/search"));
  });

  it("cannot offer a destination the caller filtered out", () => {
    // The caller passes the SAME filtered sections the rail renders, so a
    // role-refused destination is absent by construction rather than by a second
    // rule that could drift from the first.
    const withoutAudit = DESTINATIONS.filter((d) => d.to !== "/audit");
    const out = paletteResults({ query: "audit", destinations: withoutAudit, recents: [] });
    expect(out.map((r) => r.to)).not.toContain("/audit");
  });

  it("cannot offer an ACTION onto a destination the caller filtered out", () => {
    // Rank 3 is a second door onto rank 2's screens. "Export audit log" lands on
    // the audit screen exactly as "Audit log" does, so a principal the rail does
    // not show the ledger to must not be offered the verb either — otherwise the
    // boundary leaks one rank further down.
    const withoutAudit = DESTINATIONS.filter((d) => d.to !== "/audit");
    const labels = (query: string, destinations: typeof DESTINATIONS) =>
      paletteResults({ query, destinations, recents: [] }).map((r) => r.label);
    expect(labels("audit", withoutAudit)).not.toContain("Export audit log");
    expect(labels("chain", withoutAudit)).not.toContain("Re-verify chain");
    // The control: with the ledger visible, both verbs come back on the SAME
    // queries. Without it the assertions above would also pass if the two
    // actions had simply been deleted, or if the queries matched nothing.
    expect(labels("audit", DESTINATIONS)).toContain("Export audit log");
    expect(labels("chain", DESTINATIONS)).toContain("Re-verify chain");
  });

  it("still offers the shell preferences, which belong to no screen", () => {
    // The theme and the tour are chrome, not a destination — nothing to inherit
    // visibility from, and nothing the rail could have hidden.
    const labels = paletteResults({ query: "theme", destinations: [], recents: [] }).map(
      (r) => r.label,
    );
    expect(labels).toContain("Switch theme");
  });

  it("shows recents, capped at ten, when the input is empty", () => {
    const recents = Array.from({ length: 14 }, (_, i) => ({
      id: `r${i}`,
      label: `r${i}`,
      kind: "Destination",
      group: "recent" as const,
      to: `/r${i}`,
    }));
    expect(paletteResults({ query: "", destinations: DESTINATIONS, recents })).toHaveLength(10);
  });

  it("returns nothing for a query that matches nothing (the screen renders the copy)", () => {
    expect(paletteResults({ query: "zzzzzzz", destinations: DESTINATIONS, recents: [] })).toEqual(
      [],
    );
  });
});

describe("hydrateRecents", () => {
  /** Exactly what the build that shipped `?export=1` wrote to localStorage. */
  const STALE: PaletteResult = {
    id: "act:Switch theme",
    label: "Switch theme",
    kind: "Action",
    group: "action",
    to: "/settings#appearance",
  };

  it("re-reads a stored action against the CURRENT registry", () => {
    // A stored row is a snapshot of what was offered, and the target is the
    // palette's business, not the snapshot's — otherwise everyone who used the
    // broken build keeps a dead row in their recents forever.
    expect(hydrateRecents([STALE], [])).toEqual([
      {
        id: "act:Switch theme",
        label: "Switch theme",
        kind: "Action",
        group: "recent",
        to: null,
        command: "switch-theme",
      },
    ]);
  });

  it("drops an action this build no longer has", () => {
    const gone: PaletteResult = { ...STALE, id: "act:Delegate", label: "Delegate", to: "/x" };
    expect(hydrateRecents([gone], [])).toEqual([]);
    // Control: a row whose label IS still in the registry survives the same call.
    expect(hydrateRecents([gone, STALE], [])).toHaveLength(1);
  });

  it("drops a stored action when its live destination is filtered out", () => {
    const auditAction: PaletteResult = {
      id: "act:Export audit log",
      label: "Export audit log",
      kind: "Action",
      group: "recent",
      to: "/audit",
      command: "export-audit",
    };
    expect(hydrateRecents([auditAction], [])).toEqual([]);
    expect(
      hydrateRecents([auditAction], [{ label: "Governance › Audit log", to: "/audit" }]),
    ).toEqual([auditAction]);
  });

  it("rebuilds live destinations, drops stale/refused ones, and keeps typed ids independent", () => {
    const dest: PaletteResult = {
      id: "dest:/audit",
      label: "STALE audit label",
      kind: "Destination",
      group: "destination",
      to: "/audit",
    };
    const id: PaletteResult = {
      id: `id:approval:${APPROVAL_ID}`,
      label: APPROVAL_ID,
      kind: "Approval request",
      group: "id",
      to: `/approvals?request=${APPROVAL_ID}`,
    };
    const staleLibrary: PaletteResult = {
      ...dest,
      id: "dest:/library",
      label: "Workspace › Library",
      to: "/library",
    };
    expect(
      hydrateRecents([dest, staleLibrary, id], [{ label: "Governance › Audit log", to: "/audit" }]),
    ).toEqual([
      { ...dest, label: "Governance › Audit log", group: "recent" },
      { ...id, group: "recent" },
    ]);
    expect(hydrateRecents([dest, id], [])).toEqual([{ ...id, group: "recent" }]);
  });

  it("treats the recognized stable id as the only stored authority", () => {
    const poisoned: PaletteResult[] = [
      {
        id: "dest:/audit",
        label: "Open attacker site",
        kind: "Action",
        group: "action",
        to: "javascript:alert(1)",
      },
      {
        id: "act:Switch theme",
        label: "Export secrets",
        kind: "Destination",
        group: "destination",
        to: "https://attacker.invalid",
        command: "export-audit",
      },
      {
        id: `id:approval:${APPROVAL_ID}`,
        label: "Forged approval",
        kind: "Destination",
        group: "destination",
        to: "javascript:alert(2)",
      },
      {
        id: "id:approval:not-issued",
        label: "Unknown id",
        kind: "Approval request",
        group: "id",
        to: "/approvals?request=not-issued",
      },
    ];
    expect(hydrateRecents(poisoned, [{ label: "Governance › Audit log", to: "/audit" }])).toEqual([
      {
        id: "dest:/audit",
        label: "Governance › Audit log",
        kind: "Destination",
        group: "recent",
        to: "/audit",
      },
      {
        id: "act:Switch theme",
        label: "Switch theme",
        kind: "Action",
        group: "recent",
        to: null,
        command: "switch-theme",
      },
      { ...idResults(APPROVAL_QUERY)[0]!, group: "recent" },
    ]);
  });

  it("hands back rows the live index would produce for the same action", () => {
    // The registry is the single source: a recents row and a searched row for
    // one action are the same object, so they cannot drift apart.
    const searched = paletteResults({
      query: "switch theme",
      destinations: [],
      recents: [],
    })[0];
    expect(hydrateRecents([STALE], [])[0]).toEqual({ ...searched!, group: "recent" });
    expect(PALETTE_ACTIONS.map((a) => a.label)).toContain("Switch theme");
  });
});
