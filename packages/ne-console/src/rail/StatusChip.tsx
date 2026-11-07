// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §1.3 — THE SYSTEM STATUS CONTROL, and its popover: THE ONE HOME OF GLOBAL
// STATE.
//
// Before this, global state was scattered as permanent chrome: a chain lamp, a
// 64-char config-hash plate, an air-gap row and a twelve-row annunciator, all in
// the navigation rail, all on every route, all competing with the destination
// list for the reader's attention. It amounted to a dashboard nobody asked for,
// rendered beside the thing they did ask for.
//
// QUIET WHEN HEALTHY. Healthy draws a small filled dot and the word
// "Operational" in --text-dim: no colour alarm, no figure, nothing to read. Any
// degradation takes the fault colour and NAMES THE WORST FACT, so the chip is
// worth glancing at precisely because it is usually silent.
//
// THIN CLIENT (C7). Every value here is the server's: `verification.valid` and
// `.checked` are computed by the audit service and rendered verbatim, the
// attested tally is the server's flag over the constraint set it sent, and the
// hash is displayed — never compared. A role-refused read renders "Not visible
// to your role" and the CHIP STAYS QUIET: a role boundary is not a fault.
//
// The ONE value the chip does not rest on verbatim is a single read's `valid`:
// the server has a reproduced read-during-append race, so the resting word is
// the LATCHED verdict (audit/chainVerdict.ts) — the same reducer, over the same
// chain, that /audit's strip and /trace render. Nothing is hidden; a verdict is
// required to agree with itself before it becomes the resting state.

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router";
import { chainReading } from "../audit/chainVerdict";
import { useAuditPoll } from "../hooks/useAuditPoll";
import { usePosture } from "../posture/PostureContext";
import { HashChip } from "../primitives/HashChip";
import { formatCount } from "../primitives/format";

const NOT_VISIBLE = "Not visible to your role";
const UNREACHABLE = "Unavailable";
const PENDING = "Loading…";

/** What a posture row says when it has no posture to show.
 *
 * These are THREE different facts and the popover is the one place §1.3 makes
 * responsible for global state, so it must not blur them. Rendering
 * "Not visible to your role" while a read is in flight — or after it failed —
 * asserts an authorization boundary that does not exist. */
function postureAbsence(status: string): string {
  if (status === "refused") return NOT_VISIBLE;
  if (status === "loading") return PENDING;
  return UNREACHABLE;
}

/** "12 Jun" — the short form the Configuration row uses for the date a hash
 * came into force. */
function shortDate(iso: string | null): string | null {
  if (iso === null) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

interface Fact {
  /** The chip's own summary when this fact is the worst one on the deployment. */
  headline: string;
}

export function StatusChip() {
  const audit = useAuditPoll();
  const { status: postureStatus, posture } = usePosture();
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const popoverId = useId();

  // Click-away and Escape close it. A popover that only closes by clicking its
  // own trigger is a popover people leave open by accident.
  useEffect(() => {
    if (!open) return;
    function onDocDown(event: MouseEvent) {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const verification = audit.verification;
  const chainRefused = audit.status === "degraded";
  const auditUnreachable = audit.status === "error";
  const postureUnreachable = postureStatus === "error";

  /**
   * THE WORD THIS CHIP RESTS ON — the LATCHED verdict off the shared poll, run
   * through the same `chainReading` mapping /audit and /trace render.
   *
   * It used to render this poll's raw `verification.valid`. Roughly half of the
   * reads taken while the chain is being appended to come back
   * `{"valid": false, "reason": "head mismatch (truncated or forged tail)"}`,
   * and the console's own polling is one of the things appending — so the chip
   * accused the deployment of a forged tail and quietly withdrew it on the next
   * poll. Once /audit's strip was routed through the latch, the two said
   * opposite things ON ONE SCREEN: the chip naming a break at an entry while
   * the strip below it said the reading had not been confirmed yet.
   *
   * There is ONE read here — the poll's — so the word and the numbers beside it
   * are the same read's by construction. That is not luck: /audit has to pair
   * them deliberately, because a record count taken off a read that answered
   * INVALID is a false sentence.
   *
   * AND A CONFIRMED BREAK IS DECIDED BEFORE THE READ IS CONSULTED. This chip
   * used to resolve to `null` — no word at all — the moment `verification` was
   * null, which is what a single failed poll produces: the chip fell back to
   * "Audit unavailable" and /audit's strip, reading its own passing window,
   * fell all the way back to `● VALID`. Between them a confirmed break left the
   * screen over one 502. `chainReading` answers BREAK from the latch and uses
   * the read only to name the entry.
   */
  const { word: chain, breakSeq } = chainReading(audit.verdict, verification, audit.observedBreaks);
  const chainBroken = chain === "BREAK";

  // THE WORST FACT, in order. A refusal is NOT a fault and never appears here.
  let fault: Fact | null = null;
  if (chainBroken) {
    fault = {
      headline: breakSeq === null ? "Chain break" : `Chain break at entry ${formatCount(breakSeq)}`,
    };
  } else if (auditUnreachable) {
    fault = { headline: "Audit unavailable" };
  } else if (postureUnreachable) {
    fault = { headline: "Controls unreachable" };
  }

  /**
   * A DISAGREEING READ TAKES WORDS BUT NOT THE COLOUR.
   *
   * "Operational" over a chain whose verdict has not agreed with itself would
   * vouch for something nothing has confirmed, and it would contradict the
   * strip on /audit at the same instant. The fault colour is the other error:
   * the latch exists because an unconfirmed disagreement is not yet an
   * accusation, and spending the alarm on it is what made the indicator
   * worthless in the first place. So the chip stays quiet and says what is
   * actually happening.
   */
  const headline = fault
    ? fault.headline
    : chain === "RE-VERIFYING"
      ? "Re-checking the audit chain"
      : "Operational";

  /** The Audit chain row: the CONFIRMED verdict first, then the same three-way
   * absence split `postureAbsence` documents, then the latched reading — never
   * one read's raw `valid`.
   *
   * THE BREAK IS TESTED FIRST, and the order is the fix. It used to sit below
   * both absence branches, so a refused or failed read overwrote a standing
   * break with "Not visible to your role" / "Unavailable" — an absence quietly
   * replacing tamper-evidence the console had already confirmed. A break this
   * console observed is a fact about the chain, not about the current read. */
  let chainValue: string;
  if (chain === "BREAK") {
    // THIS ARM RUNS. It was labelled "DEFENSIVE, and unreachable on today's
    // wire" — the argument being that `_break` (verify.py:180) takes
    // `seq: int`, so an invalid verdict always names an entry. That is still
    // true of an ORDINARY break and it is no longer the whole wire: the
    // CONTRADICTION verdict (`AnchoredChainVerifier`) refuses while the
    // `verify_chain` beneath it PASSES — the ledger is internally consistent,
    // it simply is not the ledger this process verified — so it has no
    // `first_break_seq` to give, and it latches on SIGHT. The same correction
    // was made in audit/chainVerdict.ts, audit/spine.ts, AuditPage.tsx and
    // TracePage.tsx; this was the last copy still calling it hypothetical.
    // A break the server did not locate says exactly that rather than printing
    // "Break at entry —", which reads as a sentence that lost its number.
    chainValue =
      breakSeq === null
        ? "Break confirmed — the server did not name the entry"
        : `Break at entry ${formatCount(breakSeq)}`;
  } else if (chainRefused) {
    // ONLY the server's refusal ("degraded" — the poll's 403 arm). A read in
    // flight or a failed poll rendered as a role boundary is a false statement
    // about authorization.
    chainValue = NOT_VISIBLE;
  } else if (verification === null) {
    chainValue = audit.status === "loading" ? PENDING : UNREACHABLE;
  } else if (chain === "RE-VERIFYING") {
    chainValue = "Checking again — this reading has not been confirmed yet";
  } else {
    chainValue = `Valid · ${formatCount(verification.checked)} records`;
  }

  const toggle = useCallback(() => setOpen((v) => !v), []);

  return (
    <div className="ne-status" ref={wrapRef}>
      <button
        type="button"
        className={`ne-status__chip${fault ? " ne-status__chip--fault" : ""}`}
        aria-expanded={open}
        aria-controls={popoverId}
        onClick={toggle}
      >
        <span className="ne-status__dot" aria-hidden="true" />
        <span>{headline}</span>
      </button>
      {open ? (
        <div className="ne-status__popover" id={popoverId} role="group" aria-label="System status">
          <StatusRow
            label="Audit chain"
            value={chainValue}
            to={breakSeq === null ? "/audit" : `/audit?seq=${breakSeq}`}
          />
          <StatusRow
            label="Controls"
            value={
              posture === null
                ? postureAbsence(postureStatus)
                : `${posture.constraints.filter((c) => c.attested).length} of ${posture.constraints.length} verified`
            }
            to="/posture"
          />
          <StatusRow
            label="Network"
            value={
              posture === null
                ? postureAbsence(postureStatus)
                : posture.air_gap
                  ? "Air gap on"
                  : `${formatCount(posture.egress_allowlist.length)} hosts allowed`
            }
            to="/posture"
          />
          <StatusRow
            label="Configuration"
            value={
              posture === null ? (
                postureAbsence(postureStatus)
              ) : (
                <span className="ne-status__config">
                  <HashChip hash={posture.config_hash} />
                  {(() => {
                    const inForce = posture.config_hash_history.find((s) => s.to === null);
                    const since = shortDate(inForce?.from ?? null);
                    return since === null ? null : <span> · in force since {since}</span>;
                  })()}
                </span>
              )
            }
            to="/posture#configuration"
          />
        </div>
      ) : null}
    </div>
  );
}

function StatusRow({ label, value, to }: { label: string; value: React.ReactNode; to: string }) {
  return (
    <Link className="ne-status__row" to={to}>
      <span className="ne-status__rowlabel">{label}</span>
      <span className="ne-status__rowvalue">{value}</span>
    </Link>
  );
}
