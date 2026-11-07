// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §17 — the metric rows, as pure functions.
//
// The screen's whole job is to state what was measured and to state a gap as a
// gap. Both of those are rules over the served window, so they live here, apart
// from the component, where they are testable without a DOM:
//
//   * a tag the response does not carry renders "—" and the note
//     "Not instrumented". It is never a zero.
//   * an entry the server marks `undefined` renders "—" and names the missing
//     denominator. It is not relabelled "Not instrumented".
//   * a SERVED numeric zero always renders. The client never guesses a
//     denominator from a sibling counter and suppresses the server's value.
//   * a zero whose witness counter is non-zero in the same window renders the
//     served zero PLUS the note "Attribution gap"; the witness figure goes in
//     `title`. The value is never overwritten — that would invent a verdict in
//     the other direction.
//   * a tag outside the standard set is surfaced in its own group, never dropped.
//
// THIN CLIENT (C7): nothing here computes a metric. Every value rendered is the
// server's, and the only comparison made is between two figures it sent.

import type { KpiEntryView } from "../api/types";

/** The three groups §17 names, in order, with their tags in the order it lists. */
export const GROUPS: ReadonlyArray<{ heading: string; tags: readonly string[] }> = [
  {
    heading: "Utilization",
    tags: [
      "utilization.agent_turns",
      "utilization.tool_calls",
      "utilization.active_users",
      "utilization.sessions",
      "utilization.events_total",
    ],
  },
  {
    heading: "Proficiency",
    tags: [
      "proficiency.eval_runs",
      "proficiency.eval_pass_rate",
      "proficiency.correction_signals_total",
      "proficiency.positive_signals",
      "proficiency.negative_signals",
      "proficiency.negative_signal_rate",
    ],
  },
  {
    heading: "Business value",
    tags: ["business_value.hours_saved_floor"],
  },
];

/** The group a tag outside the standard set lands in — surfaced, never dropped. */
export const OTHER_HEADING = "Other";

/** C8 — where model spend meets the outcome it was declared against. */
export const SPEND_HEADING = "Spend by outcome";

/** A per-outcome spend tag: the suffix is the customer's DECLARED `kpi_tag`,
 * verbatim, so this screen never has to know their outcome names. */
const SPEND_OUTCOME_PREFIX = "spend.outcome.";
const SPEND_PREFIX = "spend.";

/**
 * The roll-up rows under the outcomes, in the order an operator reads them, and
 * what each says when the server did NOT send it.
 *
 * `always` marks a row that renders even when absent, because its ABSENCE is
 * itself a fact the reader needs: a missing attribution share means the server
 * could not compute one, and silently dropping the row would leave a table that
 * looks fully attributed. Rows without it are omitted when absent — there is no
 * fact to state (no unpriced calls is not a gap worth a row).
 */
const SPEND_ROLLUP: ReadonlyArray<{
  tag: string;
  label: string;
  always?: boolean;
  absentNote?: string;
  note?: string;
}> = [
  {
    tag: "spend.unattributed",
    label: "Unattributed",
    note: "No outcome was declared for this spend",
  },
  { tag: "spend.attributed_usd", label: "Attributed" },
  { tag: "spend.total_usd", label: "Total" },
  {
    tag: "spend.attributed_call_share",
    label: "Calls attributed",
    always: true,
    absentNote: "No calls recorded — no share to compute",
    note: "Share of calls carrying a declared outcome — the coverage figure",
  },
  {
    tag: "spend.attributed_share",
    label: "Attributed share (by spend)",
    always: true,
    absentNote: "No spend recorded — no share to compute",
    note: "Dollar-weighted — read it beside Calls attributed, never alone",
  },
  {
    tag: "spend.calls_missing_cost",
    label: "Calls with no recorded price",
    note: "Excluded from the totals above — no price was recorded",
  },
];

/** True for every tag the spend group owns — so none of them can also fall
 * through to `Other`, which would print the same spend twice. */
function isSpendTag(tag: string): boolean {
  return tag.startsWith(SPEND_PREFIX);
}

const KNOWN_TAGS = new Set(GROUPS.flatMap((g) => g.tags));

/**
 * The zeros this screen will call an attribution gap, and the counter that
 * contradicts each. Deliberately narrow: a turn must have had an actor and must
 * have run inside a session, so 0 of either beside a non-zero turn count is a
 * gap in attribution. "Positive signals 0" is NOT — nobody may have pressed the
 * button, and flagging that would invent a fault.
 */
const WITNESS: Readonly<Record<string, string>> = {
  "utilization.active_users": "utilization.agent_turns",
  "utilization.sessions": "utilization.agent_turns",
};

/** "utilization.agent_turns" → "Agent turns". Sentence case (§0). */
export function labelFor(tag: string): string {
  const dot = tag.indexOf(".");
  const suffix = dot >= 0 ? tag.slice(dot + 1) : tag;
  const spaced = suffix.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export interface MetricRow {
  /** The served tag. Carried for the export, never rendered as a label. */
  tag: string;
  label: string;
  /** The served datapoint, or null when the response does not carry the tag. */
  entry: KpiEntryView | null;
  /** True when the server served an explicitly undefined measurement. */
  suppressed: boolean;
  /** The one-line row note, or null. */
  note: string | null;
  /** The note's `title`, when the note has a figure behind it. */
  noteTitle: string | null;
}

const UNDEFINED_NOTES: Readonly<Record<string, string>> = {
  "proficiency.eval_pass_rate": "No eval runs recorded — no rate to compute",
  "proficiency.negative_signal_rate": "No agent turns recorded — no rate to compute",
};

function rowFor(tag: string, byTag: Map<string, KpiEntryView>): MetricRow {
  const label = labelFor(tag);
  const entry = byTag.get(tag) ?? null;
  if (entry === null) {
    return {
      tag,
      label,
      entry: null,
      suppressed: false,
      note: "Not instrumented",
      noteTitle: null,
    };
  }

  if (entry.measurement_status === "undefined" || entry.value === null) {
    return {
      tag,
      label,
      entry,
      suppressed: true,
      note: UNDEFINED_NOTES[tag] ?? "No denominator recorded — no value to report",
      noteTitle: null,
    };
  }

  if (tag === "utilization.events_total") {
    return {
      tag,
      label,
      entry,
      suppressed: false,
      note: "Includes this console's own reads.",
      noteTitle: null,
    };
  }

  if (tag === "business_value.hours_saved_floor") {
    return {
      tag,
      label,
      entry,
      suppressed: false,
      note: "Floor estimate — configured rate assumptions are applied by the server.",
      noteTitle: null,
    };
  }

  const witnessTag = WITNESS[tag];
  if (entry.value === 0 && witnessTag !== undefined) {
    const witness = byTag.get(witnessTag);
    if (witness !== undefined && witness.value !== 0) {
      return {
        tag,
        label,
        entry,
        suppressed: false,
        note: "Attribution gap",
        noteTitle: `0 recorded while ${witness.value} ${labelFor(witnessTag).toLowerCase()} were counted`,
      };
    }
  }

  return { tag, label, entry, suppressed: false, note: null, noteTitle: null };
}

export interface MetricGroup {
  heading: string;
  rows: MetricRow[];
  /** The group-level note §17 gives the non-standard set. */
  note: string | null;
}

/** The served call count for a spend row, rendered as the note beside the money.
 * The figure is the SERVER'S, carried on `dimensions` — this screen counts
 * nothing (C7). */
function callsNote(entry: KpiEntryView): string | null {
  const calls = entry.dimensions.calls;
  return calls === undefined ? null : `${calls} calls`;
}

/**
 * The C8 spend group, or `null` when the response carries no spend tag at all.
 *
 * Returning `null` rather than a table of "Not instrumented" rows is the same
 * rule the whole screen runs on: a response with no spend datapoints says one
 * thing, and five rows would say something it does not.
 *
 * THE RULE THIS FUNCTION EXISTS TO KEEP: untagged spend is rendered as its own
 * row, with its own label, and is never added into a named outcome — not to
 * make a total tie out, not to fill an empty table. Nothing here divides,
 * totals, or apportions; every figure rendered is one the server sent.
 */
function buildSpendGroup(kpis: readonly KpiEntryView[]): MetricGroup | null {
  const spendEntries = kpis.filter((k) => isSpendTag(k.kpi_tag));
  if (spendEntries.length === 0) return null;

  const byTag = new Map(spendEntries.map((k) => [k.kpi_tag, k]));
  const rows: MetricRow[] = [];

  for (const entry of spendEntries) {
    if (!entry.kpi_tag.startsWith(SPEND_OUTCOME_PREFIX)) continue;
    rows.push({
      tag: entry.kpi_tag,
      // The DECLARED outcome name, verbatim — never prettified, because it is
      // the customer's own string and must match what their config declares.
      label: entry.kpi_tag.slice(SPEND_OUTCOME_PREFIX.length),
      entry,
      suppressed: false,
      note: callsNote(entry),
      noteTitle: null,
    });
  }

  for (const spec of SPEND_ROLLUP) {
    const entry = byTag.get(spec.tag) ?? null;
    if (entry === null) {
      if (!spec.always) continue;
      rows.push({
        tag: spec.tag,
        label: spec.label,
        entry: null,
        suppressed: false,
        note: spec.absentNote ?? "Not instrumented",
        noteTitle: null,
      });
      continue;
    }
    // The served call count is the row's NOTE, not a `title` tooltip: on the
    // unattributed row it is the figure a buyer most needs, and a tooltip is
    // invisible in a screenshot, in print, and on touch.
    const calls = callsNote(entry);
    const note = spec.note ?? null;
    rows.push({
      tag: spec.tag,
      label: spec.label,
      entry,
      suppressed: false,
      note: calls === null ? note : note === null ? calls : `${calls} · ${note}`,
      noteTitle: null,
    });
  }

  // A served spend tag this screen does not recognise (a newer server, or the
  // by-department/by-task breakdowns C8 also names) is SURFACED here rather than
  // dropped. `buildGroups` excludes every spend tag from `Other`, so without this
  // the row would render nowhere at all — a hole in the "surfaced, never dropped"
  // guarantee, punched in the one group that reports money.
  const claimed = new Set(rows.map((r) => r.tag));
  for (const entry of spendEntries) {
    if (claimed.has(entry.kpi_tag)) continue;
    rows.push({
      tag: entry.kpi_tag,
      label: entry.kpi_tag.slice(SPEND_PREFIX.length),
      entry,
      suppressed: false,
      note: "Not in the standard spend set",
      noteTitle: callsNote(entry),
    });
  }

  return { heading: SPEND_HEADING, rows, note: null };
}

/** Every group in §17's order, plus `Other` when the response carries a tag
 * outside the standard set. */
export function buildGroups(kpis: readonly KpiEntryView[]): MetricGroup[] {
  const byTag = new Map(kpis.map((k) => [k.kpi_tag, k]));
  const groups: MetricGroup[] = GROUPS.map((g) => ({
    heading: g.heading,
    rows: g.tags.map((tag) => rowFor(tag, byTag)),
    note: null,
  }));
  const spendGroup = buildSpendGroup(kpis);
  if (spendGroup !== null) groups.push(spendGroup);
  // A spend tag belongs to the spend group and NOWHERE else — letting one also
  // fall through to `Other` would print the same money twice on one screen.
  const others = kpis.filter((k) => !KNOWN_TAGS.has(k.kpi_tag) && !isSpendTag(k.kpi_tag));
  if (others.length > 0) {
    groups.push({
      heading: OTHER_HEADING,
      rows: others.map((entry) => ({
        tag: entry.kpi_tag,
        label: labelFor(entry.kpi_tag),
        entry,
        suppressed: false,
        note: null,
        noteTitle: null,
      })),
      note: "Not in the standard metric set",
    });
  }
  return groups;
}

/** The served period label, or null when the response carries no datapoint. */
export function periodOf(kpis: readonly KpiEntryView[]): string | null {
  return kpis[0]?.dimensions.period ?? null;
}
