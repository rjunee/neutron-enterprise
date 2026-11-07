# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Anchored chain verification — O(extension) on the request path, O(chain) off it.

:func:`ne.audit.verify.verify_chain` is and remains THE authority for full-chain
integrity.  It is also O(chain): a strictly sequential, pure-Python re-hash of
every record.  MEASURED at 203,343 rows (SQLite, this repo's own corpus builder):
**4.05 s**, of which the database scan is a small minority — the cost is
``compute_record_hash`` per row, and it grows with the ledger forever.  On the
pilot's Postgres the same walk measures **9.77 s** and the chain grows
~20,850 rows/day, i.e. **~1.0 s/day**.  Calling that on a user-facing path means
the ledger's own success is what makes the product slow.

This module makes the *request path* cost O(rows appended since the last proof)
while keeping full-chain verification a real, scheduled, enforced event:

* :meth:`AnchoredChainVerifier.full_verify` runs the real
  :func:`~ne.audit.verify.verify_chain` and, on success, records a
  :class:`VerifiedAnchor` — the ``(seq, record_hash)`` of the head that verify
  blessed.  This is what a BACKGROUND task calls, never a request.
* :meth:`AnchoredChainVerifier.verify` is the request path.  It proves, via
  :func:`~ne.audit.verify.verified_extension`, that the chain is still an
  append-only extension of that anchor — one indexed ``seq > anchor`` range scan.

WHAT THIS IS NOT
----------------
It is **not** a flag that turns verification off, and it is **not** a claim that
the full chain was checked.  Every result carries :attr:`AnchoredVerification.
basis` naming which proof actually ran, and the ``detail`` string states the
scope in words.  A caller that prints ``detail`` prints the truth.

THE ANCHOR IS A TRUST BOUNDARY — read this before using it
----------------------------------------------------------
**1. Where the anchor lives.**  In THIS PROCESS'S MEMORY, and nowhere else.  It
is never written to the database, never read from the database, never
serialised, never sent over the wire.  It is only ever created as the OUTPUT of
a :func:`~ne.audit.verify.verify_chain` this process ran, and only ever advanced
by a :func:`~ne.audit.verify.verified_extension` proof this process ran.

That placement is the whole point.  An attacker holding write access to
``ne_audit_log`` AND ``ne_audit_head`` — the exact adversary the hash chain
exists to catch — cannot forge the value this verifier links against, because
that value is not stored anywhere they can reach.  In particular the classic
attack a forward hash chain is built to make expensive, the **self-consistent
rewrite** (edit record k, then recompute every ``record_hash``/``prev_hash``
from k to the head so the ledger is internally perfect again), IS caught here
**when the rewritten record is at or below the anchor** (``k <= A``): such a
rewrite necessarily changes the ``prev_hash`` of the anchor's successor row,
and that is compared against the REMEMBERED anchor hash, not a re-read one.
A self-consistent rewrite lying ENTIRELY ABOVE the anchor (``k > A``) is not
caught — and neither is it by ``verify_chain``, which misses it identically;
see answer (2)'s MISSED list and :mod:`ne.audit.verify`'s own "inherent
limitation" paragraph.

What it does NOT buy, stated plainly:

* It is process-local and non-durable.  A restart destroys it and the next
  :meth:`verify` pays a full ``verify_chain``.  That is a cost property, not a
  hole — but it means a fleet of N replicas needs N full verifies, and a
  process that has NEVER completed one has no anchor and never uses this path.
* It gives ZERO protection against an adversary who can execute code inside
  this process.  Such an adversary owns the verdict outright, with or without
  an anchor.
* It does NOT authenticate the WRITER of a well-formed append.  Neither does
  ``verify_chain``.  Append authenticity comes from the append-only triggers and
  database access control, identically for both.
* It is NOT a cryptographic anchor and is NOT a substitute for one.  The durable,
  attacker-resistant anchor in this system is :mod:`ne.audit.checkpoint` —
  HMAC-signed heads archived to WORM — which is what catches a self-consistent
  rewrite performed while NO process holds an in-memory anchor (e.g. across a
  restart).  That loop is **default-off**
  (``NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE``); a deployment that has not
  enabled it has no durable tamper anchor at all, and this module does not
  change that either way.

**2. What verify-forward-from-anchor detects, and what it MISSES.**  Let the
anchor be ``(A, H_A)`` and the current head ``(S, H_S)``.

DETECTED (:meth:`verify` returns invalid, or falls back to a naming full verify):

* any tamper of a record at ``seq > A`` that leaves the segment ``(A, S]``
  internally INCONSISTENT — a payload edited without recomputing its
  ``record_hash``, a ``record_hash`` edited in place, a ``prev_hash`` relinked
  without recomputing the tail above it, a denormalized query column swapped.
  That is exactly the set ``verify_chain`` catches over the same segment, by
  the same per-row checks (:func:`~ne.audit.verify._walk_rows`'s), so nothing
  is given up here.  A rewrite at ``seq > A`` that is propagated all the way to
  a self-consistent new head is a different case and is MISSED — by this proof
  and by ``verify_chain`` alike; see below;
* any gap or reorder at ``seq > A``;
* a truncated or forged tail, and a record inserted PAST the head (the same
  race-free :func:`~ne.audit.verify._beyond_head_break` epilogue the full verify
  runs);
* a head rewound to or below ``A`` with a hash that is not ``H_A``;
* **a prefix tamper at ``seq <= A`` that was PROPAGATED FORWARD** to keep the
  ledger internally self-consistent — because propagating it changes the
  anchor successor's ``prev_hash`` away from the remembered ``H_A``;
* **a self-consistent tail truncation that rewinds the head STRICTLY BELOW
  ``A``** (delete the tail AND rewind ``ne_audit_head`` to a coherent earlier
  state at ``seq < A``).  ``verify_chain`` documents this as an inherent blind
  spot of a forward hash chain — it reports the truncated ledger VALID — and
  the remembered anchor is what sees it, because the head is now BEHIND a head
  this process verified.  ⚠ **The boundary is STRICT**, and it is literally
  ``if head_seq < baseline_seq`` in
  :func:`~ne.audit.verify._verified_extension`: a coherent truncation that
  leaves the head at ``seq == A`` restores exactly the state the anchor blessed
  and proves as a valid EMPTY extension, and one that leaves it at ``seq > A``
  proves as a valid SHORTER extension.  Neither is detected — see the MISSED
  list's second entry.  Only ``head < A`` is the buy-back.  See
  :meth:`AnchoredChainVerifier._full_verify` for why such a contradiction
  refuses the call but adopts the new head rather than wedging the process, and
  why closing the window for good is :mod:`ne.audit.checkpoint`'s job.

MISSED — two classes, and which of them ``verify_chain`` would have caught is
the whole difference between a trade-off and a hole:

* **an UNPROPAGATED modification at ``seq <= A``**: a payload edited without
  updating its ``record_hash``, a ``record_hash`` edited in place, a
  denormalized column swapped, or a prefix record DELETED (leaving a gap below
  the anchor).  Each of these leaves the ledger internally INCONSISTENT at or
  below the anchor, and this proof never re-reads rows at or below the anchor,
  so it cannot see it.  ``verify_chain`` catches every one of them; this proof
  catches none of them.  **This is the ONE class the anchor trades away**, and
  it is the class answer (3)'s age bound exists to bound.  Pinned by
  ``test_unpropagated_prefix_tamper_is_MISSED_by_the_anchored_proof``.
* **any COHERENT REPLACEMENT OR TRUNCATION of the suffix above the anchor** —
  that is, replacing the rows at ``seq > A`` with a DIFFERENT but internally
  self-consistent sequence that still links onto ``H_A`` and still ends on a
  matching ``ne_audit_head``.  It is not only rewrites; two shapes, both
  reproduced against a real chain:

  - **rewrite**: edit record k (``A < k <= S``), recompute its ``record_hash``,
    rewrite its denormalized columns to match, relink every ``prev_hash`` from
    ``k+1`` to the head, and advance ``ne_audit_head``.  Note the link back to
    ``H_A`` survives even when ``k = A+1`` and the anchor's successor row is
    therefore itself the edited row: what the rewrite leaves alone is that
    row's ``prev_hash``, not the row (it recomputes k's ``record_hash`` FROM
    the existing ``prev_hash``).  Pinned by
    ``test_self_consistent_rewrite_ABOVE_the_anchor_is_missed_by_BOTH``.
  - **truncation**: delete every row above some ``t >= A`` and rewind
    ``ne_audit_head`` to row ``t`` coherently.  For ``t > A`` this proves as a
    valid SHORTER extension; for ``t == A`` it proves as a valid EMPTY
    extension, because it restores exactly the head the anchor blessed.  Rows
    that existed and were deleted are gone with no trace either proof can read.
    Only ``t < A`` is caught (DETECTED list above) — the boundary is strict.
    ⭐ **And it cannot simply be tightened**: :meth:`_full_verify` confirms
    every candidate anchor by asking :func:`~ne.audit.verify.verified_extension`
    for an EMPTY extension at ``head == baseline``, so the rule that makes
    ``t == A`` indistinguishable from an intact chain is the same rule that
    installs anchors at all.  Making ``head_seq == baseline_seq`` refuse
    (verified by mutation) stops any anchor from ever being installed.  Closing
    this needs an external anchor, not a tighter comparison.  Pinned by
    ``test_coherent_suffix_truncation_at_or_above_the_anchor_is_missed_by_BOTH``.

  In every shape the forward walk is satisfied at every row it reads.
  **``verify_chain`` MISSES ALL OF THEM IDENTICALLY** — they are the
  "*consistent* tail-truncation ... or a full chain rewrite performed AFTER the
  last signed checkpoint" that :mod:`ne.audit.verify`'s module docstring names
  as an inherent limitation of a forward hash chain with no external anchor.
  So the anchored proof gives up NOTHING here: walking the whole ledger on
  every request would not catch it either.  The control that does catch it is
  :mod:`ne.audit.checkpoint`'s HMAC-signed WORM heads, which pin the head hash
  at signing time (and which is default-off — see answer (1)'s last bullet).

So the honest characterisation, stated as a DIFFERENCE from ``verify_chain``
rather than as an absolute: *verify-forward-from-anchor trades away exactly one
class — a tamper at or below the anchor that leaves the ledger
self-INCONSISTENT there — and buys back one class ``verify_chain`` cannot see:
a coherent truncation that rewinds the head to ``seq < A``, strictly below the
remembered anchor.  A coherent replacement or truncation of the suffix that
leaves the head at ``seq >= A`` is invisible to BOTH, and closing it needs a
signed checkpoint, not a longer walk.*

**3. How full-chain integrity is still established, and how often.**  By
:meth:`full_verify`, driven by a background caller on
:data:`CHAIN_ANCHOR_FULL_VERIFY_INTERVAL_S` (default 300 s).  The anchor is
REFUSED once it is older than :data:`CHAIN_ANCHOR_MAX_AGE_S` (default 3 x the
interval = 900 s).  ⚠ **``max_anchor_age_s`` IS A STALENESS CEILING ON REUSE,
NOT A TIME-TO-DETECTION, and the difference is not pedantry.**  The age check
lives inside :meth:`verify` — nothing in this module runs on a clock of its
own.  So what the 900 s buys is precisely: *no verification taken after the
anchor passes that age can reuse the anchor; every such call runs a full
``verify_chain`` inline, and the FIRST missed class — the unpropagated prefix
tamper, the only one ``verify_chain`` would have caught — is detected on the
first such call.*  If nothing calls :meth:`verify` for an hour, nothing is
detected for an hour; elapsed time alone detects nothing.  That is still a real
guarantee, because it is exactly the reads that would otherwise have been
served on a stale blessing that are forced to pay for a fresh one — a tamper
can never be laundered through an anchored answer past the ceiling — and it is
enforced in code, not merely intended.  The SECOND missed class (a coherent
replacement or truncation of the suffix at or above the anchor) is not bounded
by this number at all, because no amount of ``verify_chain`` detects it either;
:mod:`ne.audit.checkpoint` is its control.

A background :meth:`full_verify` loop is what turns the ceiling into an actual
time-to-detection, because the loop IS a caller: it walks the ledger every
:data:`CHAIN_ANCHOR_FULL_VERIFY_INTERVAL_S` whether or not any request arrives,
so in a process that runs it the first missed class is genuinely detected
within ~300 s.  That figure is therefore weaker in kind than the ceiling: it
holds only where the loop runs.  In this repo the loop is wired in the API
process only
(``ne.api.app`` -> :meth:`ne.api.model_selection.ModelSelectionService.run_chain_anchor_refresher`);
the worker builds its own ``ModelSelectionService`` without one, so in the
worker there is NO active detection at all — only the reuse ceiling, taking
effect on that process's next :meth:`verify` call after 900 s.
When the background full verify FAILS,
the anchor is CLEARED and the failure is logged at ERROR with
``verify_chain``'s named break; the very next :meth:`verify` then runs its own
full verify inline and returns invalid, so callers fail closed on their normal
"chain is invalid" path.  Nothing keeps serving on a stale blessing.

⚠ **"Fails" covers a RAISE as well as an invalid verdict** — a dropped
connection, a statement timeout, a failover mid-walk.  Such a walk proves
nothing about the chain, but it does withdraw the support for the standing
claim the anchor makes, so the anchor is cleared there too — UNCONDITIONALLY,
not by the identity CAS :meth:`verify` uses in its own except clause, because a
concurrent request-path extension carries that same blessing forward in a NEW
object and a CAS would no-op against exactly the anchor it must wipe — and the
exception propagates to the loop.  Not clearing would only postpone the same outcome by at most
:data:`CHAIN_ANCHOR_MAX_AGE_S` while serving anchored answers in the meantime;
see :meth:`AnchoredChainVerifier._full_verify` for the cost that was weighed.

**4. Anchor missing, stale, or failing its own check.**  All fall back to an
inline full ``verify_chain`` on that call — LOUDLY, and the returned
:attr:`AnchoredVerification.basis` is ``"full-chain"``, never
``"anchored-extension"``.  The fallback is SLOW (that is the point: it is
today's cost, not a weaker answer) and it is never a silent pass.  There is
deliberately no configuration that makes a missing or stale anchor return
"verified".  The three cases differ in what they can conclude:

* **missing** (cold process, or a prior failure cleared it) — WARNING; the full
  verify's own verdict stands, and its head becomes the new anchor.
* **stale** (blessing older than ``max_anchor_age_s``) — WARNING; nothing has
  been disproved, so again the full verify's verdict stands.  This is the path
  that BOUNDS the missed class in answer (2), and it is enforced here in code,
  not merely intended by a background schedule.
* **contradicted** (the extension proof failed) — ERROR, and the call REFUSES
  even if the full walk passes, because only "extends what I verified" was
  disproved and a self-consistent truncation would otherwise be reported valid.
  The newly verified head is adopted so the refusal is one call, not a wedged
  process; see :meth:`AnchoredChainVerifier._full_verify`.
* **self-failing** (a full verify that passes but whose head snapshot cannot be
  confirmed as an extension baseline) — ERROR, no anchor installed, invalid
  returned.  The anchor hash is the one value everything else depends on, so it
  is never installed unconfirmed.

A fifth case reaches no verdict at all: the full walk RAISES.  There is no
:class:`AnchoredVerification` to return, so the exception propagates to the
caller — but the anchor is cleared on the way out, so the state left behind is
the ``missing`` case above and the next :meth:`verify` behaves accordingly.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import sqlalchemy as sa

from ne.audit.verify import (
    ChainVerification,
    read_head_or_none,
    register_chain_walk_observer,
    verified_extension,
    verify_chain,
)

_LOG = logging.getLogger("ne.audit.anchor")

#: How often a background caller should run :meth:`AnchoredChainVerifier.full_verify`.
#:
#: This is the ONLY place full-chain integrity is established once the request
#: path is anchored, so it is a security parameter, not a tuning knob.  At the
#: measured pilot cost (9.77 s per full verify, growing ~1.0 s/day) a 300 s
#: interval is a **3.3% duty cycle** on one core — against the ~59% the previous
#: arrangement burned (two full verifies per ~33 s refresher cycle).  The
#: interval stays feasible until the full verify itself approaches 300 s, i.e.
#: ~290 days of growth at the measured rate; past that the anchor simply ages
#: out and :meth:`AnchoredChainVerifier.verify` degrades LOUDLY to the inline
#: full verify it does today.
CHAIN_ANCHOR_FULL_VERIFY_INTERVAL_S = 300.0

#: The hard upper bound on how old the blessing full verify may be before
#: :meth:`AnchoredChainVerifier.verify` REFUSES the anchor.
#:
#: DERIVED as 3 x the interval, never written as a literal, so the two cannot
#: drift apart.  Three intervals is one tick of slack for a slow or failed
#: background run plus one for the run after it — enough that a single blip does
#: not put a multi-second stall on a user's turn, and small enough to be a
#: number an auditor can be told.  ⚠ State it as what it is — a **staleness
#: ceiling on REUSE**, not a time-to-detection: **no verification taken more
#: than 15 minutes after the blessing may reuse the anchor; it runs the full
#: ``verify_chain`` inline instead, and the unpropagated-prefix tamper of the
#: module docstring's answer (2) is caught on that first subsequent call.**
#: Elapsed time alone detects nothing — a process that is never asked to verify
#: never verifies.  Active detection comes only from a background
#: :meth:`AnchoredChainVerifier.full_verify` loop, which IS a caller: where one
#: runs (in this repo, the API process) the real figure is ~5 minutes; the
#: worker's verifier has no loop and gets the reuse ceiling only.  Either way
#: this covers the FIRST missed class alone; the second (a coherent replacement
#: or truncation of the suffix at or above the anchor) is invisible to
#: ``verify_chain`` too and is :mod:`ne.audit.checkpoint`'s to close.
CHAIN_ANCHOR_MAX_AGE_S = 3 * CHAIN_ANCHOR_FULL_VERIFY_INTERVAL_S


@dataclass(frozen=True)
class VerifiedAnchor:
    """A chain head a full :func:`~ne.audit.verify.verify_chain` blessed.

    Held ONLY in :class:`AnchoredChainVerifier`'s memory — see the module
    docstring's answer (1) for why that placement is the security property and
    what it does not buy.
    """

    #: The blessed head's sequence number.
    seq: int
    #: The blessed head's ``record_hash``.  This is the value every later
    #: extension proof links against, and it is never re-read from the database.
    record_hash: str
    #: Monotonic clock reading taken BEFORE the blessing full verify began — so
    #: the age bound conservatively includes the verify's own duration.
    blessed_at: float
    #: Records the blessing full verify walked (for the ``detail`` string).
    blessed_checked: int
    #: How many extension proofs have advanced this anchor since the blessing.
    #: Advancing ``seq``/``record_hash`` NEVER advances ``blessed_at``: the
    #: security bound is the age of the FULL verify, and no number of forward
    #: proofs renews it.
    extensions: int


#: Which proof actually ran.  ``"full-chain"`` means a real
#: :func:`~ne.audit.verify.verify_chain` walked the whole ledger on this call;
#: ``"anchored-extension"`` means only the rows after the anchor were checked.
VerificationBasis = Literal["full-chain", "anchored-extension"]


@dataclass(frozen=True)
class AnchoredVerification:
    """A verification verdict that names its own scope.

    ``valid`` alone is deliberately NOT the whole result: an anchored proof and
    a full-chain proof are different claims, and a caller that renders one as
    the other is the bug.  :attr:`basis` names the proof and :attr:`detail`
    states its scope in words.
    """

    valid: bool
    basis: VerificationBasis
    #: Records THIS call walked — the extension length for an anchored proof,
    #: the whole ledger for a full one.
    checked: int
    #: The anchor this call proved forward from (``None`` for a full verify).
    anchor_seq: int | None
    #: The head this call proved up to, when known.
    head_seq: int | None
    #: Age of the blessing FULL verify at the time of this call (``None`` for a
    #: full verify, whose age is zero by construction).
    anchor_age_s: float | None
    detail: str
    #: The underlying :class:`~ne.audit.verify.ChainVerification` when a real
    #: full ``verify_chain`` ran on this call (``basis == "full-chain"``);
    #: ``None`` for an anchored proof, which never runs one.
    #:
    #: It exists because :attr:`detail` alone LOSES the machine-readable break:
    #: ``verify_chain`` names the exact record (``first_break_seq``,
    #: ``first_break_event_id``, ``reason``) and a caller that renders a break
    #: — the audit console names the entry the break is at — needs those
    #: fields, not a sentence to parse.  ⚠ It is NOT a second verdict:
    #: :attr:`valid` is the verdict, and the two deliberately DISAGREE on the
    #: ``contradicted`` and ``unconfirmed`` paths, where the ledger is
    #: internally consistent (``chain.valid`` true) but this call still refuses
    #: (``valid`` false) because "extends the head I verified" was disproved.
    #: Read :attr:`valid`; read this only for the break's identity.
    chain: ChainVerification | None = None
    #: This verdict is a REFUSAL THE READ-DURING-APPEND RACE CANNOT PRODUCE.
    #:
    #: It is set on exactly the paths where the full walk found the ledger
    #: internally CONSISTENT and this verifier still refused, because "the
    #: ledger extends the head I verified" was disproved:
    #: :func:`~ne.audit.verify.verified_extension` returned ``None`` against a
    #: blessed anchor (the ``contradicted`` fallback), the freshly walked head
    #: could not be confirmed as an extension baseline, or the ``ne_audit_head``
    #: row was ABSENT when the baseline snapshot was taken and present again by
    #: the time the walk read it.  All three are race-free by construction —
    #: ``verified_extension`` bounds every check to a head snapshot read FIRST
    #: and closes the tail with the same race-free
    #: :func:`~ne.audit.verify._beyond_head_break` epilogue ``verify_chain``
    #: uses, and a legitimate append never removes the head row — so a
    #: concurrent legitimate append cannot manufacture one.
    #:
    #: ⚠ **It is also STRUCTURALLY ONE-SHOT**, which is why it has to be on the
    #: wire.  The ``contradicted`` path installs the freshly verified head
    #: before it returns, so the very next :meth:`AnchoredChainVerifier.verify`
    #: proceeds against the new anchor and PASSES; the unconfirmed path and the
    #: absent-baseline path clear the anchor, and the next full verify — whose
    #: snapshot and walk both see one head — installs one and passes.  A
    #: consumer that requires N
    #: consecutive disagreeing reads before it believes a break — the console's
    #: flap suppression does, and it must, because the head-mismatch arm of an
    #: ordinary ``verify_chain`` break genuinely does flap — can therefore
    #: NEVER see this one.  The strongest signal this module has would be the
    #: one signal that structurally cannot reach a screen.  A consumer reads
    #: this flag to know the verdict is not a flap and may be believed on its
    #: first and only appearance.
    #:
    #: ``False`` on an ORDINARY break (``chain.valid`` is itself false): that
    #: verdict IS the flapping one and the N-count is exactly right for it.
    contradiction: bool = False


class AnchoredChainVerifier:
    """Sub-second chain verification anchored on a periodic full verify.

    ONE instance per process per audit engine: the anchor is the process's
    single trust root, and a second instance would carry a second, independently
    aged anchor with no benefit.  Both methods are thread-safe and are meant to
    be called from worker threads (``anyio.to_thread.run_sync``); the internal
    lock guards the anchor FIELD ONLY and is never held across a database read,
    so a request-path :meth:`verify` never queues behind the background
    :meth:`full_verify`.
    """

    def __init__(
        self,
        engine: sa.Engine,
        *,
        full_verify_interval_s: float = CHAIN_ANCHOR_FULL_VERIFY_INTERVAL_S,
        max_anchor_age_s: float = CHAIN_ANCHOR_MAX_AGE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_anchor_age_s < full_verify_interval_s:
            raise ValueError(
                "max_anchor_age_s must be >= full_verify_interval_s, else the anchor "
                "expires before the background full verify that renews it can run "
                f"(got max_anchor_age_s={max_anchor_age_s}, "
                f"full_verify_interval_s={full_verify_interval_s})"
            )
        self._engine = engine
        self.full_verify_interval_s = full_verify_interval_s
        self.max_anchor_age_s = max_anchor_age_s
        self._clock = clock
        self._lock = threading.Lock()
        self._anchor: VerifiedAnchor | None = None
        #: How many BREAKS this process has observed, from ANY chain walk over
        #: this engine.  Only ever incremented, only under ``_lock``, and read by
        #: :meth:`_full_verify` to refuse an anchor whose blessing walk started
        #: before a break it did not see.  A counter rather than a clock reading
        #: because the clock is injectable and a frozen one would wedge the
        #: comparison forever.
        self._breaks_seen = 0
        # THE PROCESS-WIDE RULE, JOINED HERE (ne.audit.verify.
        # register_chain_walk_observer): any walk that disproves this chain
        # withdraws this anchor, whoever ran it.  Registration is WEAK, so a
        # verifier that goes out of scope drops out on its own.
        register_chain_walk_observer(self)

    @property
    def anchor(self) -> VerifiedAnchor | None:
        """The current anchor, or ``None`` when the process holds no blessing."""
        with self._lock:
            return self._anchor

    def observe_chain_walk(self, engine: sa.Engine, cv: ChainVerification) -> None:
        """A chain walk ON THIS VERIFIER'S OWN ENGINE disproved the chain — drop
        the blessing.

        ⚠ SCOPED BY ENGINE IDENTITY, not by database. A walker holding a
        DIFFERENT ``Engine`` over the SAME database does NOT propagate here —
        ``PostgresAuditSink`` builds its own engine and would be missed. No
        in-process caller does that today (the sink's only callers are the
        separate ne-mcp demo processes, which have no verifier), and the
        choke-point guardian does NOT pin this — it pins direct calls to the
        private walkers, not engine identity. Add an in-process ``sink.verify()``
        and it fails silently.
        blessing.

        ⭐ **THE ANCHOR IS A CLAIM, AND THIS IS THE ONE RULE THAT KEEPS THE CLAIM
        HONEST NO MATTER WHO DOES THE WALKING.**  The claim is precisely "a full
        ``verify_chain`` passed over the prefix, and everything since extends
        it".  A walk that returns ``valid is False`` — the audit page's
        ``from``/``to`` read, the export preamble, ``/trace``'s unbounded walk,
        the trace index, the context ledger — has DISPROVED the chain, and a
        disproved chain cannot have been good up to a point.  So the claim is
        withdrawn, and the next :meth:`verify` pays a full inline walk and
        reports what it finds instead of serving an ``anchored-extension`` pass
        over a ledger this process just proved broken.

        WHY A WINDOWED VERDICT IS ALLOWED TO DO THIS, when the console refuses
        to let one CLEAR a break:

        * a bounded walk is weaker only about what it did NOT look at.  About the
          rows it did read, its start anchor, its global density pre-check and
          its race-free beyond-head epilogue, it is exactly as strong as the full
          walk — :func:`~ne.audit.verify._verify_bounded` shares all three.  So
          it can MAKE a finding it cannot WITHDRAW, which is the same asymmetry
          from the other side;
        * it cannot be provoked into a false one.  An empty or non-overlapping
          window takes the ``lo is None`` branch, whose only failing returns are
          a missing head row, a GLOBAL density break and the beyond-head
          epilogue — every one of them a whole-chain fact.  There is no
          ``from``/``to`` a caller can send that makes an intact chain answer
          invalid;
        * and it grants no new denial of service.  The cost of a cleared anchor
          is one inline full walk, which the SAME ops-gated route already hands
          any caller on request (``?full_verify=true``).  Against that: the
          behaviour without this rule is that a proven break leaves the trust
          root installed and the passes keep coming.

        ⚠ It is DELIBERATELY not conditional on which anchor is standing.  A
        request-path extension carries the same blessing forward in a NEW
        object, so an identity CAS would no-op against exactly the anchor it has
        to wipe — the same reason :meth:`_full_verify` clears unconditionally.

        Not raising is part of the contract (:meth:`ne.audit.verify.
        ChainWalkObserver.observe_chain_walk`): this runs inside another
        caller's verification, and turning their break verdict into a 500 would
        be strictly weaker than the verdict itself.
        """
        if cv.valid or engine is not self._engine:
            return
        with self._lock:
            self._breaks_seen += 1
            standing = self._anchor
            self._anchor = None
        if standing is None:
            return
        _LOG.error(
            "a chain walk in this process proved the ledger BROKEN (%s); the verified "
            "anchor at seq %d has been CLEARED, so the next verification runs the full "
            "chain INLINE instead of proving an extension of a head that is no longer "
            "supported",
            cv.detail,
            standing.seq,
        )

    def verify(self) -> AnchoredVerification:
        """Prove the chain — O(extension) when anchored, O(chain) when not.

        Callers on a request path use THIS.  It returns an anchored proof when
        (and only when) the process holds a blessing younger than
        :attr:`max_anchor_age_s` AND the chain provably extends it.  Anything
        else falls back to an inline full ``verify_chain`` and says so in
        :attr:`AnchoredVerification.basis`.

        SCOPE, precisely: an ``"anchored-extension"`` result proves that records
        ``anchor.seq+1 .. head`` are a valid, dense, unbroken extension of a head
        whose ``record_hash`` a full verify blessed ``anchor_age_s`` seconds ago,
        and that no record exists past that head.  It does NOT re-check records
        at or before ``anchor.seq``.  See the module docstring's answer (2) for
        the exact tamper class that leaves undetected, and answer (3) for the
        bound on how long it stays undetected.
        """
        # BOTH IN ONE ACQUISITION, on purpose. The break counter is what says
        # "another walk in this process disproved the chain since this call
        # started"; snapshotting it AFTER reading the anchor leaves a window in
        # which the break lands between the two reads, so this call holds a
        # reference to an anchor that has already been withdrawn AND a counter
        # that has already been bumped — and the re-check below would then see
        # no race and hand back the stale pass. Nothing here touches the
        # database, so the lock discipline of this file (no `with self._lock:`
        # spans a `verify_chain`, a `connect()` or a `verified_extension`) holds.
        with self._lock:
            anchor = self._anchor
            breaks_at_start = self._breaks_seen
        if anchor is None:
            _LOG.warning(
                "audit chain verification has NO anchor (cold process, or a prior "
                "verification cleared it) — running a full O(chain) verify_chain INLINE "
                "on this call; this is the slow path and it is doing the whole ledger"
            )
            return self._full_verify()

        age_s = self._clock() - anchor.blessed_at
        if age_s > self.max_anchor_age_s:
            self._clear(anchor)
            _LOG.warning(
                "audit chain anchor at seq %d is STALE (%.1fs old > %.1fs max): the "
                "background full verify is not keeping up. Refusing the anchor and "
                "running a full O(chain) verify_chain INLINE on this call",
                anchor.seq,
                age_s,
                self.max_anchor_age_s,
            )
            return self._full_verify()

        try:
            extension = verified_extension(
                self._engine,
                baseline_seq=anchor.seq,
                baseline_hash=anchor.record_hash,
            )
        except Exception:
            self._clear(anchor)
            _LOG.error(
                "audit chain extension proof RAISED against the anchor at seq %d; "
                "falling back to a full verify_chain, which names any break",
                anchor.seq,
                exc_info=True,
            )
            return self._full_verify()

        if extension is None:
            # A CONTRADICTION, not merely a miss: this process verified a head
            # and the ledger no longer extends it. A tamper, a restored dump, a
            # failover to a lagging replica and a genuine break all land here,
            # and verify_chain alone cannot separate them — a self-consistent
            # tail truncation is its documented blind spot, so a bare fallback
            # would report the very state the anchor just disproved as VALID
            # and throw away the strongest signal this module has. So the
            # fallback runs (verify_chain is still the authority that NAMES an
            # ordinary break) but its verdict is forced closed for THIS call.
            self._clear(anchor)
            _LOG.error(
                "audit chain does NOT provably extend the verified anchor at seq %d — "
                "refusing this verification and falling back to a full verify_chain, "
                "which names any ordinary break",
                anchor.seq,
            )
            return self._full_verify(contradicted=anchor)

        head_seq, head_hash, rows = extension
        advanced = VerifiedAnchor(
            seq=head_seq,
            record_hash=head_hash,
            # NOT self._clock(): advancing the anchor's position must never renew
            # the age of the FULL verify that blessed it, or the max-age bound —
            # the whole security budget of this module — would be unbounded.
            blessed_at=anchor.blessed_at,
            blessed_checked=anchor.blessed_checked,
            extensions=anchor.extensions + 1,
        )
        with self._lock:
            # ⭐ THE RETURN IS GUARDED, NOT ONLY THE WRITE. This was a CAS on the
            # install alone, and the install is not what an auditor reads: a
            # windowed or full walk elsewhere in this process could enter
            # `observe_chain_walk`, prove a below-the-anchor tamper and clear
            # `_anchor` while `verified_extension` ran UNLOCKED above — the CAS
            # then correctly declined to re-install, and this method went on to
            # return `valid=True, basis="anchored-extension"` anyway. A PASSING
            # AUDIT VERDICT EMITTED AFTER THIS PROCESS HAD ALREADY DISPROVED THE
            # CHAIN, off a snapshot taken before the disproof. The extension
            # proof is not wrong about the rows it read; it is answering a
            # question — "is the chain sound?" — that a proof it cannot see has
            # already answered no, and an anchored proof is blind to exactly the
            # tamper class (below the anchor) that the other walk found.
            #
            # `_full_verify` has snapshotted this same counter since the same
            # hazard was closed on the INSTALL path; this is the READ path's
            # half of it, on the same counter, and it is one-shot for the same
            # reason: the next call has no anchor and takes the full path.
            raced_a_break = self._breaks_seen != breaks_at_start
            # Identity CAS: a concurrent full_verify may have installed a FRESHER
            # anchor (or cleared it) while this proof ran unlocked. Writing back
            # unconditionally would rewind that newer blessing to this older one's
            # blessed_at, or resurrect an anchor a failed verify just cleared.
            # The proof itself stands either way, so only the write is conditional.
            if not raced_a_break and self._anchor is anchor:
                self._anchor = advanced
        if raced_a_break:
            # Refuse by FALLING BACK, not by fabricating a verdict: `verify_chain`
            # is the authority that NAMES an ordinary break, and this is exactly
            # the state `verify()` is in when it holds no anchor at all — which
            # is the state the observer just put this verifier into. The call is
            # made OUTSIDE the lock; `_full_verify` opens a connection and walks
            # the chain, and no lock in this file spans that.
            _LOG.error(
                "audit chain extension proof passed against the anchor at seq %d, but "
                "another chain walk in this process proved the ledger BROKEN while it "
                "was running — REFUSING to return an anchored pass over a ledger this "
                "process has already disproved; running a full verify_chain INLINE, "
                "which names any break",
                anchor.seq,
            )
            return self._full_verify()
        return AnchoredVerification(
            valid=True,
            basis="anchored-extension",
            checked=len(rows),
            anchor_seq=anchor.seq,
            head_seq=head_seq,
            anchor_age_s=age_s,
            detail=(
                f"chain extends verified anchor: {len(rows)} record(s) verified forward "
                f"from seq {anchor.seq} to head seq {head_seq}; the prefix at or before "
                f"seq {anchor.seq} was verified in full {age_s:.1f}s ago "
                f"({anchor.blessed_checked} record(s)) and is NOT re-checked by this call"
            ),
        )

    def full_verify(self) -> AnchoredVerification:
        """Run the real full ``verify_chain`` and (re)bless the anchor.

        This is what a BACKGROUND caller runs on
        :attr:`full_verify_interval_s`.  It is O(chain) by design — that cost is
        the price of the guarantee, and this method exists so it is paid off the
        request path instead of on it.

        On success the anchor is replaced with the freshly blessed head.  On
        FAILURE the anchor is CLEARED — and "failure" means BOTH shapes: a walk
        that returns invalid, and a walk that RAISES before it reaches a verdict
        (a dropped connection, a statement timeout, a failover).  Either way the
        next :meth:`verify` cannot keep serving on a blessing that is no longer
        supported; it runs its own full verify inline and reports what it finds.
        See :meth:`_full_verify` for why a raise clears too, and what that costs.
        """
        return self._full_verify()

    def _full_verify(self, *, contradicted: VerifiedAnchor | None = None) -> AnchoredVerification:
        """The full walk, plus anchor installation.

        ``contradicted`` is the anchor whose extension proof just FAILED, when
        that is why this fallback is running.  Its presence forces the verdict
        closed even if the full walk passes, because "internally self-consistent"
        and "an append-only extension of what I verified" are different claims
        and only the second one was disproved.  The freshly blessed head is
        still installed, so the refusal is ONE call rather than a wedged
        process: a legitimate restore or failover recovers on the next
        verification, and a genuine attacker gains only what
        :func:`~ne.audit.verify.verify_chain` would already have given them
        (it cannot see a self-consistent truncation at all).  Closing that
        remaining window is :mod:`ne.audit.checkpoint`'s job — signed heads in
        WORM — not this module's.

        **A RAISE ALSO CLEARS, and the reasoning is not the same as an invalid
        verdict's.**  An exception out of the walk proves nothing about the
        chain: it says the process could not look.  But an anchor is not a
        memory of a past success, it is a STANDING CLAIM that a full
        ``verify_chain`` passed over the prefix within
        :data:`CHAIN_ANCHOR_MAX_AGE_S`, and every anchored verdict served off it
        renders that claim to a caller.  A failed renewal withdraws the support
        for the claim, so the claim is withdrawn — the same fail-closed rule
        :meth:`verify` already applies when the EXTENSION proof raises
        (``self._clear(anchor)`` there).  The clear here is UNCONDITIONAL rather
        than that method's identity CAS, and the difference is not cosmetic: the
        anchor this walk set out to renew can be EXTENDED by a concurrent
        request-path :meth:`verify` while this walk runs unlocked, and an
        extension installs a NEW object carrying the SAME ``blessed_at``.  A CAS
        on the snapshot therefore misses exactly the object it must wipe — the
        same blessing, at a further seq, whose renewal has just failed.  So this
        follows the rule :meth:`_clear`'s own docstring already states for this
        method: a failed full verify contradicts EVERY anchor, not just one.

        ⚠ **What it costs, stated rather than waved at.**  Clearing puts an
        inline O(chain) walk on the next :meth:`verify`, so a fault that keeps
        raising puts one on every read — a thundering herd on an already sick
        database.  That is a real cost and it was weighed:

        * for a PERSISTENT fault it changes nothing but timing.  Not clearing
          would still hit the identical state — every read paying, and failing,
          an inline walk — within :data:`CHAIN_ANCHOR_MAX_AGE_S`, because the
          age bound is enforced in :meth:`verify` and no successful tick is left
          to renew the blessing.  Keeping the anchor buys no availability; it
          buys up to 900 s of answers served off a blessing whose renewal is
          known to be failing, which in an audit product is the wrong thing to
          buy;
        * for a TRANSIENT fault (one bad tick, the next one fine) the cost is a
          burst of inline walks bounded by request concurrency, self-healing the
          moment the first one completes and installs a fresh anchor.  That is
          the same cost this module already accepts, by design, on every cold
          start and every age-out.

        Pinned by ``test_a_full_verify_that_RAISES_clears_the_anchor`` and, at
        the loop level, by
        ``test_frontier_warm.py::test_the_background_refresher_clears_the_anchor_when_the_full_verify_RAISES``.

        **A DELETED ``ne_audit_head`` ROW IS NOT ONE OF THOSE RAISES.**  It is a
        tamper class in its own right — that table carries no trigger — and
        :func:`~ne.audit.verify.verify_chain` has always NAMED it
        (``first_break_seq=0``, ``audit head missing (ne_audit_head row
        absent)``).  So the baseline snapshot below is taken with
        :func:`~ne.audit.verify.read_head_or_none`, never
        ``ne.audit.store.read_head``: routing a named verdict into this bare
        ``raise`` made the verifier the ONLY path that turned a tamper
        accusation into a 500, which the audit page renders as "unavailable" —
        strictly weaker than the unanchored path it replaced, and a direct
        contradiction of ``ne.api.routers.audit._page_verification``'s rule that
        a verification path must never be the weaker answer.  Pinned by
        ``test_a_DELETED_head_row_is_a_NAMED_break_out_of_full_verify_not_a_raise``,
        ``test_the_request_path_falls_back_to_a_NAMED_break_when_the_head_row_is_deleted``
        and, at the API layer,
        ``test_audit_api.py::test_events_UNFILTERED_page_names_the_break_when_the_HEAD_ROW_is_deleted``.
        """
        started_at = self._clock()
        # The anchor this walk is renewing, snapshotted BEFORE the walk purely so
        # the except clause below can NAME it in the ERROR log. It is not used to
        # decide what to clear: that clear is unconditional (see the comment
        # there for why a CAS on this snapshot was wrong).
        renewing = self.anchor
        # THE CLEAR MUST NOT BE UNDONE BY A WALK THAT STARTED BEFORE THE BREAK.
        # `observe_chain_walk` withdraws the blessing the instant any walk in
        # this process disproves the chain — but this method's own walk runs
        # UNLOCKED, so a walk that began before that break and passed (it read an
        # earlier state, or a replica that had not caught up) would install a
        # fresh blessing a moment after the break was seen and the finding would
        # be gone again. Snapshotting the break counter here and re-checking it
        # under the lock at install time closes that window: an anchor is
        # installed only if nothing disproved the chain while its blessing walk
        # was in flight. It cannot wedge — the NEXT full verify snapshots the
        # bumped counter and installs normally.
        with self._lock:
            breaks_at_start = self._breaks_seen
        try:
            with self._engine.connect() as conn:
                # The head is read BEFORE verify_chain so it can only be BEHIND (or
                # equal to) the head verify_chain anchors itself on — the chain head
                # is monotone (see ne.audit.verify's module docstring). A candidate
                # head that verify_chain's own walk did not reach is therefore
                # impossible, which is what lets the confirmation below be sound.
                #
                # FAIL-CLOSED, not `.one()`-raising (ne.audit.store.read_head). A
                # DELETED ne_audit_head row is a tamper class in its own right — no
                # DB trigger guards that table — and verify_chain has always NAMED
                # it (first_break_seq=0, "audit head missing"). Taking the baseline
                # with the raising accessor made the verifier the ONLY path that
                # turned that named accusation into an exception: out of here it
                # became a 500 on the console's audit poll, which renders no verdict
                # strip at all and is indistinguishable from the database being
                # down. `read_head_or_none` uses `.first()`, so it separates "no
                # row" from "could not read" BY CONSTRUCTION: a refused connection
                # or a missing table still raises out of conn.execute and still
                # clears the anchor below. Only the absent row becomes a verdict.
                candidate = read_head_or_none(conn)
                cv: ChainVerification = verify_chain(conn)
                if not cv.valid:
                    self._clear()
                    _LOG.error(
                        "FULL audit chain verification FAILED: %s — the verified anchor has "
                        "been cleared; every subsequent verification runs the full chain "
                        "inline and reports this break until it is resolved",
                        cv.detail,
                    )
                    return AnchoredVerification(
                        valid=False,
                        basis="full-chain",
                        checked=cv.checked,
                        anchor_seq=None,
                        head_seq=None,
                        anchor_age_s=None,
                        detail=cv.detail,
                        chain=cv,
                    )
                # verify_chain blessed 1..head' for some head' >= candidate_seq, but it
                # does not hand back that head. Confirming the candidate as an
                # extension baseline closes the gap: the extension proves record
                # candidate_seq+1's prev_hash == candidate_hash (or, for an empty
                # extension, that the live head hash is still candidate_hash), while
                # the full walk proved that same prev_hash equals record
                # candidate_seq's own record_hash. Together they prove candidate_hash
                # IS the verified hash of record candidate_seq. Cost is O(rows appended
                # during the verify) — one indexed range scan, typically empty.
                #
                # candidate is None only when the head row was ABSENT at the
                # snapshot above yet verify_chain found one afterwards — a restore
                # (or a re-seed) landing inside this method. verify_chain's verdict
                # covers the chain it saw, but there is no baseline this process
                # took to confirm as an extension of it, so the blessing has nothing
                # to stand on and none is installed. Same fail-closed shape as an
                # unconfirmable baseline below; the NEXT full verify, whose snapshot
                # and walk both see the restored head, installs one normally.
                #
                # AND IT IS A CONTRADICTION, on exactly the criterion the field
                # states: the full walk found the ledger internally CONSISTENT
                # (cv.valid is true — the `if not cv.valid` arm above already
                # returned) and this verifier refuses anyway, because the head it
                # snapshotted to reason from was not there. A DELETED
                # ne_audit_head row is a tamper class in its own right (that table
                # carries no trigger) and `_verify_full` NAMES it, so reaching
                # here means the row was absent and then present again inside one
                # method — a restore, a re-seed, or a deletion being covered up.
                # It is also STRUCTURALLY ONE-SHOT, which is what makes the flag
                # load-bearing rather than decorative: the anchor is cleared and
                # the next full verify's snapshot and walk both see the restored
                # head, so it installs an anchor and PASSES. A consumer requiring
                # BREAK_CONFIRMATIONS consecutive disagreements — the console
                # does, and must, because an ordinary head-mismatch break really
                # does flap — could therefore NEVER see this one. Without the
                # flag this refusal is emitted once and discarded, which is the
                # blind spot `contradiction` exists to close.
                if candidate is None:
                    self._clear()
                    _LOG.error(
                        "FULL audit chain verification passed but the ne_audit_head row was "
                        "ABSENT when its baseline snapshot was taken — refusing to install an "
                        "anchor; verification stays on the full path"
                    )
                    return AnchoredVerification(
                        valid=False,
                        basis="full-chain",
                        checked=cv.checked,
                        anchor_seq=None,
                        head_seq=None,
                        anchor_age_s=None,
                        detail=(
                            f"full chain verify passed ({cv.checked} record(s)) but the "
                            f"ne_audit_head row was absent when its baseline was taken; no "
                            f"anchor was installed"
                        ),
                        chain=cv,
                        contradiction=True,
                    )
                candidate_seq, candidate_hash = candidate
                confirmed = verified_extension(
                    conn, baseline_seq=candidate_seq, baseline_hash=candidate_hash
                )
        except Exception:
            # A RAISE is "I could not look", NOT "the chain is broken" — and that
            # is precisely why the blessing cannot stand. An anchor is a standing
            # claim that a full verify passed over the prefix within
            # max_anchor_age_s; the renewal of that claim has just failed, so the
            # claim is no longer supported and reads must stop being served off
            # it.
            #
            # UNCONDITIONAL, not an identity CAS, and that is the FIX for a
            # silent no-op. A CAS on `renewing` was justified as sparing "a
            # blessing a concurrent caller installed", but the concurrent caller
            # this races is the REQUEST path and what it installs is an
            # EXTENSION — a NEW VerifiedAnchor object that deliberately carries
            # the SAME blessed_at forward, because advancing the position must
            # never renew the age of the full verify that blessed it. An
            # extension is not a new blessing; it is THIS blessing at a further
            # seq. `_clear` compares by IDENTITY, so the CAS no-opped against
            # exactly the object it had to wipe, and reads kept being served off
            # the blessing whose renewal had just failed for up to
            # CHAIN_ANCHOR_MAX_AGE_S. `_clear`'s own docstring already states
            # the rule followed here — "_full_verify, whose failure contradicts
            # EVERY anchor, not just one" — and both sibling call sites in this
            # method pass None too.
            self._clear()
            _LOG.error(
                "FULL audit chain verification RAISED before reaching a verdict; the "
                "anchor it was renewing (seq %s) has been CLEARED, so the next "
                "verification runs the full chain INLINE instead of serving off a "
                "blessing whose renewal is failing",
                renewing.seq if renewing is not None else "<none>",
                exc_info=True,
            )
            raise
        if confirmed is None:
            self._clear()
            _LOG.error(
                "FULL audit chain verification passed but the head snapshot it was taken "
                "against (seq %d) could not be confirmed as an extension baseline — "
                "refusing to install an anchor; verification stays on the full path",
                candidate_seq,
            )
            return AnchoredVerification(
                valid=False,
                basis="full-chain",
                checked=cv.checked,
                anchor_seq=None,
                head_seq=None,
                anchor_age_s=None,
                detail=(
                    f"full chain verify passed ({cv.checked} record(s)) but its head "
                    f"snapshot at seq {candidate_seq} could not be confirmed; no anchor "
                    f"was installed"
                ),
                chain=cv,
                # Same class as the contradicted path: the walk passed, the
                # extension proof over the head it was taken against did not.
                # A legitimate concurrent append cannot produce it (the
                # extension covers rows appended during the walk), so it is not
                # a flap and a consumer must not require a repeat it will not
                # get.
                contradiction=True,
            )
        head_seq, head_hash, _rows = confirmed
        with self._lock:
            raced_a_break = self._breaks_seen != breaks_at_start
            if not raced_a_break:
                self._anchor = VerifiedAnchor(
                    seq=head_seq,
                    record_hash=head_hash,
                    blessed_at=started_at,
                    blessed_checked=cv.checked,
                    extensions=0,
                )
        if raced_a_break:
            # Same shape as the unconfirmable-baseline path below: the walk
            # passed, and this verifier still refuses, because something it is
            # bound to was disproved while the walk was in flight. It is a
            # CONTRADICTION on exactly the stated criterion (the walk found the
            # ledger internally consistent and the refusal comes from elsewhere),
            # and it is one-shot — the next full verify installs normally — so a
            # consumer that requires consecutive disagreements must not be made
            # to wait for a repeat that will not come.
            _LOG.error(
                "FULL audit chain verification passed (%s) but another chain walk in "
                "this process proved the ledger BROKEN while it was running — refusing "
                "to install an anchor; verification stays on the full path",
                cv.detail,
            )
            return AnchoredVerification(
                valid=False,
                basis="full-chain",
                checked=cv.checked,
                anchor_seq=None,
                head_seq=head_seq,
                anchor_age_s=None,
                detail=(
                    f"full chain verify passed ({cv.checked} record(s)) but another "
                    f"chain walk in this process found the ledger broken while it ran; "
                    f"no anchor was installed"
                ),
                chain=cv,
                contradiction=True,
            )
        if contradicted is not None:
            _LOG.error(
                "audit ledger is internally valid (%s) but is NOT an append-only "
                "extension of the head this process verified at seq %d — a restored "
                "database, a lagging replica, or a self-consistent tail rewrite (which "
                "verify_chain cannot see). REFUSING this verification; the newly "
                "verified head at seq %d has been adopted, so the next verification "
                "proceeds against it",
                cv.detail,
                contradicted.seq,
                head_seq,
            )
            return AnchoredVerification(
                valid=False,
                basis="full-chain",
                checked=cv.checked,
                anchor_seq=contradicted.seq,
                head_seq=head_seq,
                anchor_age_s=None,
                # The ledger is internally consistent and this call still
                # refuses — see AnchoredVerification.contradiction. It is also
                # the LAST call that will say so: the freshly verified head is
                # installed above, so the next verification passes against it.
                contradiction=True,
                detail=(
                    f"chain does not extend the head verified at seq {contradicted.seq}: "
                    f"the ledger is internally consistent ({cv.checked} record(s) "
                    f"verified, now at head seq {head_seq}) but it is not an append-only "
                    f"continuation of the state this process verified"
                ),
                chain=cv,
            )
        return AnchoredVerification(
            valid=True,
            basis="full-chain",
            checked=cv.checked,
            anchor_seq=None,
            head_seq=head_seq,
            anchor_age_s=None,
            detail=cv.detail,
            chain=cv,
        )

    def _clear(self, expected: VerifiedAnchor | None = None) -> None:
        """Drop the anchor, unless a concurrent caller already installed a newer one.

        ``expected`` is the anchor the caller was reasoning about; passing it
        makes the clear an identity CAS so a request-path failure cannot wipe a
        blessing the background full verify installed a moment later.  Passing
        ``None`` clears unconditionally — used by :meth:`_full_verify`, whose
        failure contradicts EVERY anchor, not just one.
        """
        with self._lock:
            if expected is None or self._anchor is expected:
                self._anchor = None
