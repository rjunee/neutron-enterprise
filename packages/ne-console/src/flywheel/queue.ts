// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §11 — IMPROVEMENT'S RULES, as pure functions.
//
// They live apart from the screens for the same reason the table standard's sort
// rules do: the rules are the part worth testing, and they are testable here
// without a DOM, a provider or a fixture. The screens render them.
//
// THIN CLIENT (C7). Nothing here decides a proposal's fate, recomputes a
// confidence, or trims a permission. Every function is either a relabelling of a
// served enum value for display, a filter over rows the server already sent, or
// display arithmetic on a served number of seconds.

import { sourceDisclosure } from "../api/client";
import type {
  FixLaneWire,
  ProposalStatusWire,
  ServedStewardProposalView,
  StewardProposalView,
} from "../api/types";
import type { StateWord } from "../primitives/StatePill";
import { ABSENT, formatCount } from "../primitives/format";

/** §11's lane filter, in the spec's order. */
export const LANES: readonly FixLaneWire[] = [
  "content",
  "retrieval",
  "schema",
  "policy",
  "entity_resolution",
  "ontology",
  "skills",
];

/**
 * A served enum value as a word: `entity_resolution` → "Entity resolution".
 *
 * §0's vocabulary rule is sentence case everywhere, and the wire's snake_case is
 * a transport convention, not a label. The value is not translated, mapped or
 * abbreviated — only its separators and its first letter change — so an
 * unrecognised lane or kind still renders itself rather than falling through to
 * some "Other" bucket that hides it.
 */
export function enumLabel(value: string): string {
  if (value === "") return ABSENT;
  const words = value.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * §11's Status column: `PROPOSED→PENDING, APPROVED`.
 *
 * The list route returns non-terminal proposals only, so those are the two
 * states a row can hold. A status outside them is NOT guessed at — it returns
 * null and the cell renders the em dash, because inventing a pill for a state
 * this screen has never seen would be a claim the server did not make.
 */
export function statusWord(status: ProposalStatusWire | string): StateWord | null {
  if (status === "proposed") return "PENDING";
  if (status === "approved") return "APPROVED";
  return null;
}

/**
 * Priority's sort order.
 *
 * `priority` is a served word, and sorting it alphabetically puts "high"
 * between "medium" and "low" — an order nobody reading a triage queue would
 * agree with. The rank is the severity order the word already names; a value
 * outside the three sorts after all of them and keeps its served position among
 * its equals (the table standard's stable sort).
 */
const PRIORITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };

export function priorityRank(priority: string): number {
  return PRIORITY_RANK[priority] ?? 3;
}

export type LaneFilter = "all" | FixLaneWire;
export type StatusFilter = "all" | "proposed" | "approved";

export interface QueueFilters {
  lane: LaneFilter;
  status: StatusFilter;
}

// ===========================================================================
// 015A0-ENFORCEMENT — the queue row, as the server was willing to disclose it
// ===========================================================================

/** The literal SPEC requires wherever a withheld proposal would have had text. */
export const WITHHELD_LABEL = "Content withheld";

/**
 * One queue row, already resolved through the server's disclosure status.
 *
 * `proposal` is null for EVERY non-available row, and that is the only way any
 * consumer here can reach the protected fields: the table cells, the
 * search index, the CSV columns, the pane and the action handlers all read
 * `proposal`, so a withheld row cannot be dereferenced by one of them and
 * withheld by another.
 *
 * The three retained fields are exactly what SPEC permits on the withheld
 * branch: the opaque proposal id, the kind-neutral safe status and the time.
 * Lane, kind, cluster, confidence, priority, evidence, artifact and skill are
 * not on this object at all when the row is withheld.
 */
export interface QueueRowView {
  proposalId: string;
  status: ProposalStatusWire;
  createdAt: string;
  proposal: StewardProposalView | null;
  /** True only for `content_withheld`; an unresolved row is also undisclosed
   * but says so differently, because the server asserted nothing about it. */
  permanentlyWithheld: boolean;
}

/**
 * Resolve one served row. Fail-closed by construction: `sourceDisclosure` is
 * the single reader of the status, and everything but a stated `available`
 * lands in the undisclosed branch — including a row that states nothing.
 */
export function queueRowView(served: ServedStewardProposalView): QueueRowView {
  const disclosure = sourceDisclosure(served);
  return {
    proposalId: served.proposal_id,
    status: served.status,
    createdAt: served.created_at,
    proposal: disclosure.kind === "available" ? disclosedProposal(disclosure.value) : null,
    permanentlyWithheld: disclosure.kind === "withheld",
  };
}

/**
 * The server's status, and ONLY the server's status, decides which branch of
 * the union a row is. This is reached solely from the `available` branch of
 * `sourceDisclosure`; it never inspects which fields arrived, because field
 * presence is what a console must not read a disclosure decision out of.
 */
function disclosedProposal(row: ServedStewardProposalView): StewardProposalView {
  return row as StewardProposalView;
}

/** Every served row as a resolved row, in the server's order. */
export function queueRowViews(served: readonly ServedStewardProposalView[]): QueueRowView[] {
  return served.map(queueRowView);
}

export const NO_FILTERS: QueueFilters = { lane: "all", status: "all" };

/** The toolbar's filter controls, applied to the fetched window. Search-within
 * is applied separately by the table standard's own `filterRows`. */
export function applyFilters(
  rows: readonly QueueRowView[],
  filters: QueueFilters,
): readonly QueueRowView[] {
  return rows.filter((row) => {
    // A lane filter narrows to a lane the SERVER disclosed. A withheld row has
    // no lane, so it matches no lane filter and is not smuggled into one — and
    // it is not guessed into "other" either.
    if (filters.lane !== "all" && row.proposal?.lane !== filters.lane) return false;
    if (filters.status !== "all" && row.status !== filters.status) return false;
    return true;
  });
}

/** §11's live count. It describes the rendered rows, never the fetched ones. */
export function proposalCount(n: number): string {
  return `${formatCount(n)} proposals`;
}

export function skillCount(n: number): string {
  return `${formatCount(n)} skills`;
}

/**
 * §11's Skills status pill, from the served `status` word. Outside the three
 * lifecycle states the pill is omitted and the served word renders as itself —
 * a state pill is drawn from §2.3's CLOSED vocabulary, so an unknown state
 * cannot be given one.
 */
export function skillStatusWord(status: string): StateWord | null {
  if (status === "active") return "ACTIVE";
  if (status === "retired") return "RETIRED";
  if (status === "quarantined") return "QUARANTINED";
  return null;
}

const HOUR_S = 3600;

/**
 * §11's Review latency block renders its server-computed seconds in hours.
 * `null` is the absence of a measurement — zero decided proposals produce no
 * median — and renders the em dash, never `0.0 h`.
 */
export function formatHours(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return ABSENT;
  return `${(seconds / HOUR_S).toFixed(1)} h`;
}

/**
 * §11's week navigation: the `week_start` param, moved whole weeks from the week
 * the server just described. Parsed and rebuilt in UTC so a browser west of
 * Greenwich cannot land the request on the previous day.
 */
export function shiftWeek(weekStart: string, weeks: number): string | null {
  const t = Date.parse(`${weekStart}T00:00:00Z`);
  if (!Number.isFinite(t)) return null;
  const at = new Date(t + weeks * 7 * 24 * 3600 * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${at.getUTCFullYear()}-${pad(at.getUTCMonth() + 1)}-${pad(at.getUTCDate())}`;
}

/**
 * The `?proposal=` deep link matches either byte-exact (a short id) or by the
 * content-addressed grammar's `sha256:<full-hash>` prefix — the 16-character
 * digest a citation carries is a PREFIX of the full proposal id, never the whole
 * of it. Both forms are already in the product (Home's queue row, the palette,
 * an approval's source reference), so both keep working.
 */
export function matchesProposalParam(proposalId: string, param: string): boolean {
  return proposalId === param || proposalId.startsWith(`sha256:${param}`);
}

/** The row the `?proposal=` param names, or null. */
export function findProposal(
  rows: readonly QueueRowView[],
  param: string | null,
): QueueRowView | null {
  if (param === null) return null;
  return rows.find((row) => matchesProposalParam(row.proposalId, param)) ?? null;
}

/** The kinds whose proposal carries a forged skill candidate. */
const SKILL_KINDS = new Set(["skill_candidate", "skill_update_candidate"]);

export function isSkillProposal(proposal: StewardProposalView | null): boolean {
  if (proposal === null) return false;
  return SKILL_KINDS.has(proposal.kind) && (proposal.skill ?? null) !== null;
}
