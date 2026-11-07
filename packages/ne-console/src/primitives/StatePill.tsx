// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// REDESIGN §1.4 — <StatePill>: the ONE state primitive. A glyph PLUS a word,
// drawn from ONE closed set. Colour is redundant reinforcement only; the glyph
// and the word carry the meaning, so nothing on this console is encoded by
// colour alone (§5, accessibility non-negotiable). This primitive is what kills
// every empty-checkbox glyph (☐ AIR_GAP / ☐ SANCTIONED EGRESS / ☐ REQUIRES OPS
// KEY), which read as "off/failing" while signifying the opposite.

import type { ReactNode } from "react";

/** The CLOSED state vocabulary. Nothing outside this set may render as a state. */
export const STATE_WORDS = [
  // ---- §2.3, the closed vocabulary the rebuilt screens draw from ----
  "PENDING",
  "OVERDUE",
  "EXPIRED",
  "APPROVED",
  "DENIED",
  "AWAITING SECOND",
  "ESCALATED",
  "ALLOW",
  "DENY",
  "VALID",
  "BREAK",
  "PASSED",
  "FAILED",
  "OK",
  "DEGRADED",
  "SYNCING",
  "IDLE",
  "VERIFIED",
  "MANUAL",
  "ACTIVE",
  "RETIRED",
  "QUARANTINED",
  "UNCONFIRMED",
  "UNPRICED",
  // ---- The live serving observation (Models §10). Four distinguishable
  // verdicts because they are four different operator diagnoses: up (with the
  // model it reports), no route at all, accepted-then-silent, and an endpoint
  // this deployment refuses to contact. "Answered wrong" reuses DEGRADED. ----
  "SERVING",
  "UNREACHABLE",
  "NO ANSWER",
  "NOT CHECKED",
  // ---- Server-owned inference lifecycle states. These are operational
  // verdicts, never inferred from a missing button or a browser timer. ----
  "READY",
  "STARTING",
  "STOPPING",
  "STOPPED",
  "UNKNOWN",
  "ERROR",
  "UNAVAILABLE",
  // ---- SUPERSEDED. Still rendered by screens this rebuild has not reached
  // yet. They leave with the last screen that renders one; nothing new may use
  // them, because §2.3's list is the closed set. ----
  "ATTESTED",
  "SEALED",
  "OPEN",
  "NOT CONFIGURED",
  "NOT INSTRUMENTED",
  "HELD",
  "GATED",
  "RE-VERIFYING",
  "WITHHELD",
] as const;

export type StateWord = (typeof STATE_WORDS)[number];

type Tone = "affirm" | "idle" | "refuse" | "warn" | "gated" | "success";

/** glyph + tone per word.
 *   ●  affirmative — the thing is in force
 *   ○  idle / not configured / not instrumented
 *   ◑  partial: open, pending, waiting on someone
 *   ⊘  refused / withheld
 *   ⚠  breached / broken / failed
 *
 * NOT ▣/▢. The filled-and-hollow SQUARE pair satisfied the no-colour-only rule
 * but reads as a checkbox: `▢ NOT CONFIGURED` nine times down /connect looks
 * like nine boxes nobody ticked, and `▢ OPEN` on /posture and the landing page
 * reads as off while signifying the opposite. That is the same complaint the
 * brief lodges against `☐ AIR_GAP`, and it survived the first pass because only
 * the exact ☐ codepoint was banned. Circles cannot be mistaken for a control. */
const SPEC: Record<StateWord, { glyph: string; tone: Tone }> = {
  EXPIRED: { glyph: "⚠", tone: "refuse" },
  ESCALATED: { glyph: "◑", tone: "warn" },
  OK: { glyph: "●", tone: "success" },
  VERIFIED: { glyph: "●", tone: "affirm" },
  ACTIVE: { glyph: "●", tone: "success" },
  RETIRED: { glyph: "○", tone: "idle" },
  QUARANTINED: { glyph: "⊘", tone: "refuse" },
  UNCONFIRMED: { glyph: "○", tone: "idle" },
  // UNPRICED is not a fault and not a zero — it is the absence of a price, and
  // §2.7 forbids rendering that absence as $0.00.
  UNPRICED: { glyph: "○", tone: "idle" },
  // The serving observation's verdicts (Models §10). NO ANSWER is caution, not
  // fault: the budget expired, which is a different diagnosis from a refused
  // connection. NOT CHECKED is neutral — a refusal to probe is not an outage.
  SERVING: { glyph: "●", tone: "success" },
  UNREACHABLE: { glyph: "⚠", tone: "refuse" },
  "NO ANSWER": { glyph: "◑", tone: "warn" },
  "NOT CHECKED": { glyph: "○", tone: "idle" },
  READY: { glyph: "●", tone: "success" },
  STARTING: { glyph: "◑", tone: "warn" },
  STOPPING: { glyph: "◑", tone: "warn" },
  STOPPED: { glyph: "○", tone: "idle" },
  UNKNOWN: { glyph: "○", tone: "idle" },
  ERROR: { glyph: "⚠", tone: "refuse" },
  UNAVAILABLE: { glyph: "⊘", tone: "refuse" },
  ATTESTED: { glyph: "●", tone: "affirm" },
  MANUAL: { glyph: "○", tone: "idle" },
  SEALED: { glyph: "●", tone: "affirm" },
  OPEN: { glyph: "◑", tone: "warn" },
  VALID: { glyph: "●", tone: "affirm" },
  BREAK: { glyph: "⚠", tone: "refuse" },
  ALLOW: { glyph: "●", tone: "affirm" },
  DENY: { glyph: "⊘", tone: "refuse" },
  // PENDING is the QUEUE AT REST, not an exception, so it draws in the neutral
  // pill and not in amber (design/round3/screens/NOTES.md, "Named divergences
  // from the round-2 CSS baseline"). A queue whose every waiting row is amber
  // spends the caution colour on the normal case and has none left for the two
  // rows an approver must act on today — OVERDUE and ESCALATED, which keep it.
  // The glyph is unchanged: ◑ is still "partial, waiting on someone".
  PENDING: { glyph: "◑", tone: "idle" },
  APPROVED: { glyph: "●", tone: "affirm" },
  DENIED: { glyph: "⊘", tone: "refuse" },
  "AWAITING SECOND": { glyph: "◑", tone: "warn" },
  OVERDUE: { glyph: "⚠", tone: "warn" },
  IDLE: { glyph: "○", tone: "idle" },
  SYNCING: { glyph: "●", tone: "affirm" },
  DEGRADED: { glyph: "⚠", tone: "warn" },
  "NOT CONFIGURED": { glyph: "○", tone: "idle" },
  "NOT INSTRUMENTED": { glyph: "○", tone: "idle" },
  HELD: { glyph: "⊘", tone: "refuse" },
  // GATED — the server will not open this surface to this ROLE. Distinct from
  // HELD (a fault or a hard stop) in word, glyph and colour, because a
  // correctly-enforced permission boundary drawn in the fault colour reads as an
  // outage: the reader's home screen was three red 403 blocks deep.
  GATED: { glyph: "⊟", tone: "gated" },
  // RE-VERIFYING — a chain verdict that has not agreed with itself yet. Never a
  // resting state: the strip promotes to VALID or BREAK once N consecutive polls
  // agree (hooks/useAuditPoll.tsx explains the latch).
  "RE-VERIFYING": { glyph: "◑", tone: "gated" },
  WITHHELD: { glyph: "⊘", tone: "refuse" },
  PASSED: { glyph: "●", tone: "affirm" },
  FAILED: { glyph: "⚠", tone: "refuse" },
};

export function StatePill({
  word,
  detail,
  size = "normal",
}: {
  word: StateWord;
  /** Optional trailing clause, e.g. "at seq 4021". Rendered beside the word. */
  detail?: ReactNode;
  size?: "normal" | "large";
}) {
  const { glyph, tone } = SPEC[word];
  return (
    <span
      className={`ne-pill ne-pill--${tone}${size === "large" ? " ne-pill--large" : ""}`}
      aria-label={detail ? `${word} — ${String(detail)}` : word}
    >
      <span className="ne-pill__glyph" aria-hidden="true">
        {glyph}
      </span>
      <span className="ne-pill__word">{word}</span>
      {detail ? <span className="ne-pill__detail">{detail}</span> : null}
    </span>
  );
}

/** §2.3's name for the same primitive. The status pill IS the state pill —
 * exported under both names so a screen built against §2.3 does not introduce a
 * second, drifting implementation of the one thing this console has exactly one
 * of. */
export { StatePill as StatusPill };
