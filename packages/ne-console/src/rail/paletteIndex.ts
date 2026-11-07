// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §3 — WHAT THE COMMAND PALETTE MATCHES, as pure functions.
//
// Rank order, and it is the whole design:
//   1. TYPED OBJECT IDS. Someone pasting an id knows exactly what they want.
//      A grammar match is always the top result, labelled with its type. Two
//      grammars take the kind from words the operator typed (`approval <id>`,
//      `seq <n>`), one from a self-describing content-address wire form
//      (`sha256:<64 hex>`), and one — bare 32-lowercase-hex → Trace — is a
//      RETAINED SHAPE GUESS, disclosed at `idResults` and kept deliberately.
//      An approval request id is a bare `uuid4()`, a shape it shares with runs,
//      conversations, jobs and processes, so it is reached by typing
//      `approval <id>`.
//   2. DESTINATIONS. The rail's items plus in-page tabs. PREFIX MATCH BEATS
//      SUBSTRING — typing "au" should offer "Audit log" before "Evaluations".
//      Role-refused destinations are omitted, exactly as the rail omits them.
//   3. ACTIONS. Verbs that jump and focus.
//   4. RECENTS, when the input is empty.
//
// CONTENT SEARCH IS NOT HERE. Documents, entities and audit full-text need a
// server search endpoint the API does not expose. The palette says so rather
// than returning nothing and letting the reader conclude the content is absent.

import { parseAnchorParam } from "../audit/ledger";
import type { NavItem } from "./navSections";

export type ResultKind = "id" | "destination" | "action" | "recent";

/**
 * §3 rank 3 — the things an action DOES, as a closed set.
 *
 * THE ROOT CAUSE THIS TYPE EXISTS TO REMOVE. A palette row used to be able to
 * express exactly one thing — "go to this URL" — so the four actions that are
 * not navigations were written as URLs anyway: `?export=1`, `?verify=1`,
 * `#appearance`, `?tour=1`. No route parsed any of them, and nothing could:
 * they were never a contract with anything, only a plausible-looking string in
 * the one field the type offered. Enter navigated, the parameter was dropped on
 * the floor, and the palette listed four capabilities it did not have.
 *
 * A command is now a NAMED MEMBER OF THIS UNION, and every member is handled in
 * `runPaletteCommand`'s exhaustive switch — so an action that does nothing is a
 * compile error rather than a dead query string.
 */
export type PaletteCommand = "export-audit" | "verify-chain" | "switch-theme" | "open-tour";

const COMMANDS: ReadonlySet<string> = new Set<PaletteCommand>([
  "export-audit",
  "verify-chain",
  "switch-theme",
  "open-tour",
]);

export function isPaletteCommand(value: unknown): value is PaletteCommand {
  return typeof value === "string" && COMMANDS.has(value);
}

export interface PaletteResult {
  /** Stable id for listbox option identity. */
  id: string;
  /** The visible row. */
  label: string;
  /** The type label shown at the row's right: "Approval request", "Trace"… */
  kind: string;
  group: ResultKind;
  /** The screen Enter goes to, or `null` for a command that acts wherever you
   * already are (the theme, the tour). */
  to: string | null;
  /** What Enter RUNS once it is on that screen. Absent on a plain destination. */
  command?: PaletteCommand;
}

/**
 * The literal copy for a query that matched nothing (§3).
 *
 * It NAMES THE GRAMMAR, never the query. A pasted bare uuid4 lands here by
 * design — rank 1 reads no kind from a shape five issuers share — and this line
 * is the only thing on screen, so it has to carry the form that does resolve a
 * request. What it must not do is quote the id back: a message built around the
 * pasted string re-asserts in prose the "the palette knows what this is" claim
 * the grammar just refused. The copy is therefore static, and `shell.test.tsx`
 * holds it static, holds the Search-screen guidance, and types the form this
 * line teaches to prove the palette still resolves it.
 */
export const NO_MATCH_COPY =
  "No matches. Content search runs from the Search screen. An approval request is reached by typing approval <request-id>.";

/**
 * §3 rank 1 — the id grammars the palette resolves: `approval {uuid}` for a
 * request, `seq {n}` for a ledger entry, `sha256:<64 lowercase hex>` for a
 * proposal, and bare 32-lowercase-hex for a trace.
 *
 * Deliberately NOT a catch-all "looks like an id" rule. A grammar that guesses
 * routes an operator to the wrong screen with total confidence, which is worse
 * than not matching at all — an unmatched query falls through to the
 * destinations, which is a recoverable outcome.
 *
 * A BARE CANONICAL UUID IS NOT ONE OF THESE. The runtime mints approval request
 * ids, run ids, conversation ids, job ids and process ids from the same
 * `str(uuid.uuid4())`, so the shape names five id spaces and identifies none of
 * them.
 *
 * THE 32-HEX TRACE GRAMMAR IS A RETAINED SHAPE GUESS, and this paragraph is the
 * disclosure rather than an argument that it is safe. `uuid.uuid4().hex` is the
 * mint at a dozen-plus non-test sites in id spaces an operator can see —
 * evaluation and extraction `run_id` (`ne-skills/evals.py`,
 * `ne-worker/eval_runner.py`, `brain_graph/extraction/job.py`),
 * memory-promotion `proposal_id` (`ne-runtime/memory_promotion.py`),
 * answer-ledger and memory `event_id`, brain `write_id`
 * (`ne-brain/write/writer.py`) — and `correlation.py` says so in its own module
 * docstring. So a pasted memory-promotion proposal id lands on
 * `/trace/<proposal-id>`, as the top result, labelled "Trace", with nothing on
 * screen saying a kind was inferred: the same failure `approval {uuid}` exists
 * to end, still open on this shape. It is kept for this demo lane, and it is
 * not a model for a new grammar.
 */
export function idResults(query: string): PaletteResult[] {
  const q = query.trim();
  if (q === "") return [];

  const seq = /^seq\s+(.+)$/i.exec(q);
  if (seq) {
    // The audit screen owns anchor validity. Reusing its parser keeps the
    // palette from confidently navigating to `seq 0`, a fractional/negative
    // value, or an integer JavaScript cannot represent exactly.
    const anchor = parseAnchorParam(seq[1] ?? null);
    if (anchor.kind !== "seq") return [];
    return [
      {
        id: `id:seq:${anchor.seq}`,
        label: `Entry ${anchor.seq}`,
        kind: "Audit entry",
        group: "id",
        to: `/audit?seq=${anchor.seq}`,
      },
    ];
  }

  // The KIND comes from the word the operator typed, never from the shape of
  // what follows it — exactly as `seq {n}` names the ledger rather than letting
  // a bare integer stand for it. The id itself is still checked against the
  // issued grammar (`str(uuid.uuid4())` in both approval creation paths):
  // version and variant nibbles are part of that grammar, and uppercase or
  // another UUID version would not byte-match a request in the served queue.
  const approval = /^approval\s+(.+)$/i.exec(q);
  if (approval) {
    const request = approval[1]!;
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(request)) {
      return [];
    }
    return [
      {
        id: `id:approval:${request}`,
        label: request,
        kind: "Approval request",
        group: "id",
        to: `/approvals?request=${encodeURIComponent(request)}`,
      },
    ];
  }

  // Run correlation ids and OTel `format_trace_id` are both exactly 32 lowercase
  // hex characters on the audit/trace wire — and so is every other
  // `uuid4().hex` in the tree. See this function's docstring: the kind here is
  // inferred from shape, knowingly.
  if (/^[0-9a-f]{32}$/.test(q)) {
    return [{ id: `id:trace:${q}`, label: q, kind: "Trace", group: "id", to: `/trace/${q}` }];
  }

  // The proposal stores and verified-curation seam issue a full content
  // address. Short fixture prefixes are resolver conveniences, not issued ids.
  if (/^sha256:[0-9a-f]{64}$/.test(q)) {
    return [
      {
        id: `id:proposal:${q}`,
        label: q,
        kind: "Proposal",
        group: "id",
        to: `/improvement?proposal=${encodeURIComponent(q)}`,
      },
    ];
  }
  return [];
}

/** One §3 rank-3 verb entry: the screen it needs, then the thing it does there. */
export interface PaletteAction {
  label: string;
  /** The screen the action needs, or `null` when it acts wherever you are. */
  to: string | null;
  /** Absent for the two entries that are only a jump. */
  command?: PaletteCommand;
}

/**
 * §3 rank 3 — verb entries. Each one JUMPS AND FOCUSES; none of them performs a
 * governed write from the palette, because a write needs the screen that can
 * show what it did.
 *
 * The two audit verbs are a jump PLUS a command, and the command is the same
 * control the screen itself offers — the palette focuses and presses the
 * screen's own button rather than growing a second path to the same effect that
 * could disagree with it. The two audit verbs therefore inherit the screen's
 * conditions exactly: a role the server refuses the ledger to, or a window that
 * failed to load, has no export and no re-verification — and the reader is
 * standing on the ledger, which states the reason in its own words.
 *
 * LABELS ARE SPEC COPY, VERBATIM (§3). All six survived the repair; none had to
 * be removed, because every one of them has a real target.
 */
export const PALETTE_ACTIONS: readonly PaletteAction[] = [
  { label: "Ask a question", to: "/ask" },
  { label: "New search", to: "/search" },
  { label: "Export audit log", to: "/audit", command: "export-audit" },
  { label: "Re-verify chain", to: "/audit", command: "verify-chain" },
  { label: "Switch theme", to: null, command: "switch-theme" },
  { label: "Open guided tour", to: null, command: "open-tour" },
];

/** An action as a palette row. One builder, so a row read back from recents is
 * the SAME row the live index offers and cannot drift from it. */
function actionResult(action: PaletteAction, group: ResultKind): PaletteResult {
  return {
    id: `act:${action.label}`,
    label: action.label,
    kind: "Action",
    group,
    to: action.to,
    ...(action.command === undefined ? {} : { command: action.command }),
  };
}

const ACTIONS_BY_ID = new Map(PALETTE_ACTIONS.map((action) => [`act:${action.label}`, action]));

function canonicalTypedRecent(id: string): PaletteResult | null {
  let query: string | null = null;
  // A stored row's stable id already states its kind, so hydrating one is not a
  // guess — it is replayed through the disambiguated grammar, the same way the
  // `seq` prefix is put back before re-parsing.
  if (id.startsWith("id:approval:")) query = `approval ${id.slice("id:approval:".length)}`;
  else if (id.startsWith("id:trace:")) query = id.slice("id:trace:".length);
  else if (id.startsWith("id:proposal:")) query = id.slice("id:proposal:".length);
  else if (id.startsWith("id:seq:")) query = `seq ${id.slice("id:seq:".length)}`;
  if (query === null) return null;
  const canonical = idResults(query).find((result) => result.id === id);
  return canonical === undefined ? null : { ...canonical, group: "recent" };
}

/**
 * Recents, re-read against the CURRENT action registry.
 *
 * A stored row is a snapshot of what the palette offered when it was picked, and
 * an action's target is the palette's business, not the snapshot's. Rebuilding
 * every `act:` row from the registry means a recents list written by the build
 * that stored `Switch theme → /settings#appearance` runs the real theme command
 * today, and an action that was deleted since drops out of the list instead of
 * lingering as a row that goes nowhere. Destination snapshots are likewise
 * rebuilt from the caller's already-filtered live registry, while typed ids
 * remain independent of route visibility.
 */
export function hydrateRecents(
  stored: readonly PaletteResult[],
  destinations: readonly NavItem[],
): PaletteResult[] {
  const out: PaletteResult[] = [];
  const reachable = new Set(destinations.map((destination) => destination.to));
  for (const row of stored) {
    if (row.id.startsWith("dest:")) {
      // Rebuild destination snapshots from the caller's already-filtered live
      // registry. Removed/refused routes disappear and renamed labels refresh;
      // this pure seam invents no access decision of its own.
      const route = row.id.slice("dest:".length);
      const destination = destinations.find((item) => item.to === route);
      if (destination !== undefined) {
        out.push({
          id: `dest:${destination.to}`,
          label: destination.label,
          kind: "Destination",
          group: "recent",
          to: destination.to,
        });
      }
      continue;
    }
    if (row.id.startsWith("id:")) {
      const canonical = canonicalTypedRecent(row.id);
      if (canonical !== null) out.push(canonical);
      continue;
    }
    const action = ACTIONS_BY_ID.get(row.id);
    if (action === undefined) continue;
    if (action.to !== null && !reachable.has(action.to)) continue;
    out.push(actionResult(action, "recent"));
  }
  return out;
}

/** Prefix match ranks above substring match; everything else is dropped. */
function rank(label: string, needle: string): number | null {
  const l = label.toLowerCase();
  if (needle === "") return 2;
  if (l.startsWith(needle)) return 0;
  // A group-qualified destination ("Governance › Audit log") should still match
  // on its leaf name's prefix, which is what someone actually types.
  const leaf = l.split("›").pop()?.trim() ?? l;
  if (leaf.startsWith(needle)) return 1;
  if (l.includes(needle)) return 2;
  return null;
}

/**
 * The full ranked result list for a query.
 *
 * `destinations` must already be role-filtered by the caller (it passes the same
 * filtered sections the rail renders), so this function cannot leak a hidden
 * destination even if it wanted to.
 *
 * AN ACTION INHERITS ITS SCREEN'S VISIBILITY, from that same list. "Export audit
 * log" is a second door onto the audit screen, and a door is a door: offering it
 * to a principal the rail does not show the ledger to would be the boundary
 * leaking through rank 3 instead of rank 2. The rule is derived from the
 * caller's filtered list rather than restated as a second entitlement check
 * here — a second check is a thing that can drift from the first, and the
 * console does not decide authorization in either place (the server refuses on
 * its own terms regardless; this is legibility, per navSections.ts).
 */
export function paletteResults({
  query,
  destinations,
  recents,
}: {
  query: string;
  destinations: readonly NavItem[];
  recents: readonly PaletteResult[];
}): PaletteResult[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return recents.slice(0, 10);

  const scored: Array<{ result: PaletteResult; score: number; order: number }> = [];
  let order = 0;

  for (const item of destinations) {
    const score = rank(item.label, needle);
    if (score === null) continue;
    scored.push({
      result: {
        id: `dest:${item.to}`,
        label: item.label,
        kind: "Destination",
        group: "destination",
        to: item.to,
      },
      score,
      order: order++,
    });
  }
  const reachable = new Set(destinations.map((item) => item.to));
  for (const action of PALETTE_ACTIONS) {
    // `to: null` is a shell preference (the theme, the tour) — no screen, so no
    // screen's visibility to inherit.
    if (action.to !== null && !reachable.has(action.to)) continue;
    const score = rank(action.label, needle);
    if (score === null) continue;
    scored.push({
      result: actionResult(action, "action"),
      // Actions rank below destinations at equal match quality: a destination is
      // where you were going; an action is what you meant to do when you got there.
      score: score + 0.5,
      order: order++,
    });
  }

  scored.sort((a, b) => a.score - b.score || a.order - b.order);
  return [...idResults(query), ...scored.map((s) => s.result)];
}
