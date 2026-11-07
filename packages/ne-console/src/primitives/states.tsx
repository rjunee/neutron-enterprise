// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §2.4 — THE FOUR STATES, ONE GRAMMAR: fact, then next action, ≤ 15 words, no
// arguing.
//
// These are the only templates. A screen instantiates them with its own noun;
// it does not write its own paragraph. That is the whole point: the shipped
// console explained itself in a different voice on every route — how the
// feature works, what the server would have done, how many audited attempts a
// refusal produced — and none of it was the fact the reader came for.
//
// THE REFUSAL IS NEUTRAL, NEVER A FAULT. A role boundary correctly enforced is
// not an outage, and drawing it in the fault colour made three properly-refused
// reads look like three broken ones on one screen.
//
// The copy below is the spec's, verbatim. `thing`/`things` are the only holes.

import type { ReactNode } from "react";
import { Link } from "react-router";
import { StatePill } from "./StatePill";

/** Standalone (non-table) load: "Loading…" once, 13px dim (§2.4). Tables use
 * <DataTable status="loading">, which renders skeleton rows in its own
 * geometry — never a spinner paragraph. */
export function Loading({ things }: { things: string }) {
  return (
    <p className="ne-state ne-state--loading">
      Loading…
      <span className="ne-visually-hidden" role="status">
        Loading {things}
      </span>
    </p>
  );
}

/**
 * Empty: one sentence naming the fact, plus at most one action. No explanation
 * of how the feature works — that lives in the guided tour and nowhere else.
 */
export function Empty({ message, action }: { message: string; action?: ReactNode }) {
  return (
    <div className="ne-state ne-state--empty">
      <p className="ne-state__line">{message}</p>
      {action ? <div className="ne-state__action">{action}</div> : null}
    </div>
  );
}

/**
 * Refused (403). The server said no; this renders that, and nothing else.
 *
 * `thing` completes "Your role doesn't include {thing}." — a lowercase noun
 * phrase, e.g. "the approvals queue".
 * `page` is the page name shown in the neutral pill.
 */
export function Refused({ thing, page }: { thing: string; page: string }) {
  return (
    <div className="ne-state ne-state--refused">
      <StatePill word="GATED" detail={page} />
      <p className="ne-state__line">
        Your role doesn&rsquo;t include {thing}. Contact your administrator for access.
      </p>
    </div>
  );
}

/**
 * Error (network / 5xx) on a READ. `things` completes "Couldn't load {things}."
 * The Retry button is not optional: an error with no way out is a dead end.
 */
export function LoadError({ things, onRetry }: { things: string; onRetry: () => void }) {
  return (
    <div className="ne-state ne-state--error" role="alert">
      <p className="ne-state__line">Couldn&rsquo;t load {things}.</p>
      <button type="button" className="ne-btn ne-btn--quiet" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

/**
 * Error on a WRITE, rendered inline where the submit happened (§2.5).
 *
 * `detail` is the SERVER's string, rendered verbatim as secondary text when
 * present. It is never rewritten, never summarised, and never replaced by a
 * status code — the server's sentence is the only account of what happened that
 * is not the console guessing.
 */
export function SaveError({ detail }: { detail?: string | null }) {
  return (
    <div className="ne-state ne-state--error" role="alert">
      <p className="ne-state__line">Couldn&rsquo;t save. Try again.</p>
      {detail ? <p className="ne-state__detail">{detail}</p> : null}
    </div>
  );
}

/**
 * Denied by policy (§2.5 step 4) — rendered inline where the submit happened.
 * `decision` is an optional "View decision" link to the audit record, shown
 * only when this key can read it.
 *
 * THE GATE'S OWN SENTENCE OR NOTHING (approvals/detail.ts states the same rule
 * for the same reason). `reason` is verbatim server text, and the line above it
 * attributes it: "Denied by policy." followed by a sentence reads as the gate's
 * account of the denial. So a caller that has no served reason passes null and
 * the detail line is not drawn — a 403 with no `detail` says "Denied by
 * policy." and stops there, which is the whole of what is known. A house
 * fallback here would be the console stating a reason on the gate's behalf.
 */
export function PolicyDenied({
  reason,
  decision,
}: {
  reason?: string | null;
  decision?: ReactNode;
}) {
  return (
    <div className="ne-state ne-state--denied" role="alert">
      <StatePill word="DENIED" />
      <p className="ne-state__line">Denied by policy.</p>
      {reason ? <p className="ne-state__detail">{reason}</p> : null}
      {decision ? <div className="ne-state__action">{decision}</div> : null}
    </div>
  );
}

/**
 * §2.5 step 3 — PENDING IS RENDERED ON THE OBJECT, NOT AS A TOAST.
 *
 * The sub-line goes under the object's row and links to the request. The OLD
 * VALUE REMAINS THE DISPLAYED VALUE: the console never pre-renders an unapplied
 * change, because a value on screen that no approver has agreed to is the same
 * defect as a fabricated one — it is just fabricated by optimism.
 *
 * The marker survives a reload because it is read back from the server, never
 * held in client state.
 */
export function PendingMarker({ by, requestId }: { by: string; requestId: string }) {
  return (
    <span className="ne-pending">
      <StatePill word="PENDING" />
      <span className="ne-pending__line">
        Change requested by {by} · awaiting approval{" "}
        <Link to={`/approvals?request=${requestId}`}>View request</Link>
      </span>
    </span>
  );
}
