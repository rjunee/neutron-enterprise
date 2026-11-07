# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SELECT-LATENCY — :class:`ne.audit.AnchoredChainVerifier`, proved by mutation.

Anchored verification is a SECURITY property with a latency benefit, not a
performance knob, so every guard here is proved by breaking something and
naming which test goes RED.  Runs against a REAL SQLite chain built by the
production ``append_record`` (same statements, same append-only triggers).

The file is deliberately split in three:

* **DETECTS** — attacks the anchored proof catches on its own, including the
  self-consistent prefix rewrite a forward hash chain exists to make expensive;
* **MISSES** — the two tamper classes the anchored proof provably does NOT
  catch, pinned by execution so the claims in the docstrings can never quietly
  drift away from the behaviour: the unpropagated prefix tamper (which the full
  verify DOES catch — asserted), and any coherent replacement or truncation of
  the suffix at or above the anchor (which the full verify misses IDENTICALLY —
  also asserted, because that pairing is what makes it a scope limit rather
  than a regression);
* **FALLBACK** — missing / stale / unprovable / self-failing anchors, each of
  which must land on an inline full verify and must never return "verified".
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from ne.audit import compute_record_hash, ensure_audit_schema, verify_chain
from ne.audit.anchor import CHAIN_ANCHOR_MAX_AGE_S, AnchoredChainVerifier, VerifiedAnchor
from ne.audit.store import append_record, ne_audit_head, ne_audit_log
from ne.audit.verify import _DENORMALIZED_TEXT_COLUMNS, ChainVerification
from ne.schema import AuditEvent


class FakeClock:
    """A monotonic clock a test can advance by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def concurrent_reader_engine(tmp_path: Path) -> Iterator[sa.Engine]:
    """A file-backed engine whose READS do not hold the write lock.

    ``conftest``'s ``file_engine`` forces ``BEGIN IMMEDIATE`` on every
    transaction — its serialized-WRITER recipe — so ``_full_verify``'s own read
    connection holds the SQLite write lock for the whole walk.  A test that
    interleaves a second, concurrent READ inside that walk would then block on
    the FIXTURE rather than exercise the code under test.  A plain deferred
    ``BEGIN`` is what a real deployment's readers actually get (Postgres in the
    pilot; ``ne.audit.engine`` documents the same choice for SQLite), so the one
    test that needs a genuine reader interleave gets an engine shaped like that.
    """

    eng = sa.create_engine(f"sqlite:///{tmp_path / 'anchor-interleave.db'}")
    ensure_audit_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _drop_triggers(conn: sa.Connection) -> None:
    """Become the privileged attacker the chain exists to catch."""
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_delete")


def _seed(engine: sa.Engine, audit: Callable[..., AuditEvent], n: int, start: int = 0) -> None:
    with engine.begin() as conn:
        for i in range(start, start + n):
            append_record(
                conn,
                audit(event_id=f"e{i}", occurred_at=datetime(2026, 3, 1, tzinfo=UTC)),
            )


def _anchored(engine: sa.Engine, clock: FakeClock | None = None) -> AnchoredChainVerifier:
    """A verifier whose anchor has been blessed by a real full verify."""
    verifier = AnchoredChainVerifier(engine, clock=clock or FakeClock())
    blessing = verifier.full_verify()
    assert blessing.valid and blessing.basis == "full-chain"
    assert verifier.anchor is not None
    return verifier


def _rewrite_row(
    conn: sa.Connection, seq: int, payload: dict[str, object], occurred_at: datetime
) -> str:
    """Replace one row's payload and its own ``record_hash``; return the new hash.

    Both the hash-covered payload AND the denormalized ``occurred_at`` column are
    written, so the forgery also passes the column/payload cross-check — a tamper
    that failed THAT check would be caught for the wrong reason.
    """
    row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == seq)).one()
    new_hash = compute_record_hash(row.prev_hash, payload)
    conn.execute(
        ne_audit_log.update()
        .where(ne_audit_log.c.seq == seq)
        .values(payload=payload, record_hash=new_hash, occurred_at=occurred_at)
    )
    return new_hash


def _truncate_to(conn: sa.Connection, keep_through: int) -> None:
    """Delete every row above ``keep_through`` and rewind the head COHERENTLY.

    The head is rewound to the surviving tail's own ``record_hash``, so the
    ledger left behind is internally perfect — this is the *consistent*
    truncation, not a dangling one.
    """
    row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == keep_through)).one()
    conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq > keep_through))
    conn.execute(
        ne_audit_head.update()
        .where(ne_audit_head.c.id == 1)
        .values(seq=keep_through, record_hash=row.record_hash)
    )


# ---------------------------------------------------------------------------
# CONTROL — an intact chain verifies anchored, and stays green.
# ---------------------------------------------------------------------------


def test_control_intact_chain_verifies_anchored_and_stays_green(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """THE CONTROL for every mutation below: nothing is tampered, so every
    result is valid and — after the blessing — anchored rather than full.

    This test must stay GREEN under all the mutations the other tests use;
    a mutation that reds this one has broken the happy path, not proved a guard.
    """
    _seed(file_engine, audit, 20)
    verifier = _anchored(file_engine)

    first = verifier.verify()
    assert first.valid
    assert first.basis == "anchored-extension"
    assert first.checked == 0  # nothing appended since the blessing

    _seed(file_engine, audit, 5, start=100)
    second = verifier.verify()
    assert second.valid
    assert second.basis == "anchored-extension"
    assert second.checked == 5  # exactly the extension, never the whole chain
    assert second.anchor_seq == 20
    assert second.head_seq == 25


def test_control_anchored_result_never_claims_the_whole_chain(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """The claim must match the work: an anchored result reports the EXTENSION
    length, names the anchor, and says in words that the prefix was not
    re-checked here.

    MUTATION-CHECK: making ``verify()`` report ``checked=<whole chain>``, or
    reusing ``verify_chain``'s "chain intact: N record(s) verified" detail for
    an anchored proof, reds this test — which is the point, because that string
    is what ``ne audit verify`` and the console print.
    """
    _seed(file_engine, audit, 30)
    verifier = _anchored(file_engine)
    _seed(file_engine, audit, 3, start=100)

    result = verifier.verify()
    assert result.checked == 3 != 33
    assert "verified forward from seq 30" in result.detail
    assert "NOT re-checked by this call" in result.detail


# ---------------------------------------------------------------------------
# DETECTS
# ---------------------------------------------------------------------------


def test_tamper_after_the_anchor_is_detected(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """A payload edited in a record appended AFTER the blessing is caught by
    the anchored proof itself — no full verify needed.

    The forgery is written to the hash-covered PAYLOAD and to the denormalized
    column TOGETHER, leaving only ``record_hash`` stale — so the column/payload
    cross-check is satisfied and the per-row hash recompute is the ONLY check
    that can catch it.  (Tampering the column alone would be caught by the
    cross-check instead, and the mutation below would survive.)

    MUTATION: drop the ``compute_record_hash(...) != row["record_hash"]`` check
    from ``ne/audit/verify.py::_verified_extension`` → THIS TEST GOES RED.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    _seed(file_engine, audit, 5, start=100)

    with file_engine.begin() as conn:
        _drop_triggers(conn)
        row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == 13)).one()
        payload = dict(row.payload)
        payload["actor"] = "attacker-owned-the-box"
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 13)
            .values(payload=payload, actor="attacker-owned-the-box")
        )

    result = verifier.verify()
    assert not result.valid
    assert result.basis == "full-chain"  # handed to verify_chain, which NAMES it
    assert "seq 13" in result.detail


def test_gap_after_the_anchor_is_detected(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """A record DELETED after the blessing is caught by the extension's density
    check.

    MUTATION: drop the ``int(row["seq"]) != expected_seq`` check from
    ``_verified_extension`` → THIS TEST GOES RED.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    _seed(file_engine, audit, 5, start=100)

    with file_engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq == 13))

    result = verifier.verify()
    assert not result.valid


def test_tail_truncation_after_the_anchor_is_detected(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """Records deleted off the TAIL, while the head still claims them, must be
    caught by the extension's terminal completeness check — the walk simply ends
    early, and every per-row check passes on the way.

    This is the deletion shape the per-row link check does NOT subsume: deleting
    an INTERIOR record dangles its successor's ``prev_hash``, but deleting the
    tail leaves no successor to dangle.

    MUTATION: drop ``if expected_seq != head_seq + 1 or prev_record_hash !=
    head_hash: return None`` from ``_verified_extension`` → THIS TEST GOES RED.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    _seed(file_engine, audit, 5, start=100)

    with file_engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq == 15))

    result = verifier.verify()
    assert not result.valid


def test_forged_record_beyond_the_head_is_detected(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """A chain-consistent row inserted PAST the head without advancing the head
    is caught by the beyond-head epilogue the anchored proof also runs.

    MUTATION: delete the ``if _beyond_head_break(conn, 0) is not None: return
    None`` guards from ``_verified_extension`` → THIS TEST GOES RED.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)

    with file_engine.begin() as conn:
        tail = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == 10)).one()
        forged = audit(event_id="forged", occurred_at=datetime(2026, 3, 1, tzinfo=UTC))
        payload = forged.model_dump(mode="json")
        conn.execute(
            ne_audit_log.insert().values(
                seq=11,
                event_id=payload["event_id"],
                event_type=payload["event_type"],
                config_hash=payload["config_hash"],
                actor=payload["actor"],
                occurred_at=forged.occurred_at,
                kpi_tag=payload.get("kpi_tag"),
                trace_id=payload.get("trace_id"),
                action=payload.get("action"),
                resource=payload.get("resource"),
                outcome=payload.get("outcome"),
                payload=payload,
                prev_hash=tail.record_hash,
                record_hash=compute_record_hash(tail.record_hash, payload),
            )
        )

    result = verifier.verify()
    assert not result.valid


def test_head_rewound_below_the_anchor_is_detected(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """A head rewound behind the blessed anchor cannot be blessed as an
    extension — the proof refuses and hands over to the full verify.

    This is a SELF-CONSISTENT tail truncation, which ``verify_chain`` documents
    as an inherent blind spot of a forward hash chain — asserted below, and the
    reason a bare fallback would report this ledger VALID.  The remembered
    anchor is what sees it.

    MUTATION 1: change ``if head_seq < baseline_seq: return None`` in
    ``_verified_extension`` to ``pass`` → THIS TEST GOES RED.
    MUTATION 2: drop the ``contradicted=anchor`` argument from the
    failed-extension fallback in ``AnchoredChainVerifier.verify`` → THIS TEST
    GOES RED, because the fallback full verify passes on the truncated ledger.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)

    with file_engine.begin() as conn:
        _drop_triggers(conn)
        row5 = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == 5)).one()
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq > 5))
        conn.execute(
            ne_audit_head.update()
            .where(ne_audit_head.c.id == 1)
            .values(seq=5, record_hash=row5.record_hash)
        )

    assert verify_chain(file_engine).valid, "verify_chain's documented blind spot"
    result = verifier.verify()
    assert not result.valid
    assert "does not extend the head verified at seq 10" in result.detail
    # The refusal is ONE call: the new head is adopted so a legitimate restore
    # or failover recovers without a restart.
    assert verifier.anchor is not None and verifier.anchor.seq == 5
    assert verifier.verify().valid


def test_self_consistent_prefix_rewrite_is_detected_by_the_remembered_anchor(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """THE HEADLINE SECURITY PROPERTY.

    The attack a forward hash chain exists to make expensive: edit a record
    BEFORE the anchor and then recompute every ``record_hash``/``prev_hash``
    from there to the head, plus the head row, so the ledger is internally
    PERFECT again.  ``verify_chain`` alone cannot see this (it is documented as
    an inherent limitation of a forward chain — see ``ne/audit/verify.py``'s
    module docstring), and neither can any check that re-reads the anchor hash
    from the database.

    The anchored proof catches it, because the value it links against is the
    hash this process REMEMBERS from its own full verify, not one it re-reads:
    the rewrite necessarily moves the anchor successor's ``prev_hash`` off that
    remembered value.

    MUTATION 1: in ``AnchoredChainVerifier.verify``, re-read the baseline from
    the database (``read_head``) instead of using ``anchor.record_hash`` →
    THIS TEST GOES RED (and the whole trust-boundary argument with it).
    MUTATION 2: drop the ``row["prev_hash"] != prev_record_hash`` link check
    from ``_verified_extension`` → THIS TEST GOES RED.

    Note this test also proves the SUPERIORITY of the anchored proof over a
    bare ``verify_chain`` for this attack class: the assertion below that
    ``verify_chain`` reports the rewritten ledger VALID is not incidental — it
    is the reason the remembered anchor is worth having.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    anchor = verifier.anchor
    assert anchor is not None and anchor.seq == 10
    _seed(file_engine, audit, 4, start=100)  # rows 11..14, appended after the blessing

    # Rewrite record 4 (well below the anchor) and propagate to the head.
    forged = audit(event_id="e3", occurred_at=datetime(1999, 1, 1, tzinfo=UTC))
    forged_payload = forged.model_dump(mode="json")
    with file_engine.begin() as conn:
        _drop_triggers(conn)
        prev_hash = _rewrite_row(conn, 4, forged_payload, forged.occurred_at)
        for seq in range(5, 15):
            row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == seq)).one()
            payload = dict(row.payload)
            new_hash = compute_record_hash(prev_hash, payload)
            conn.execute(
                ne_audit_log.update()
                .where(ne_audit_log.c.seq == seq)
                .values(prev_hash=prev_hash, record_hash=new_hash)
            )
            prev_hash = new_hash
        conn.execute(
            ne_audit_head.update()
            .where(ne_audit_head.c.id == 1)
            .values(seq=14, record_hash=prev_hash)
        )

    # The rewrite is internally perfect, so the full verify is fooled ...
    assert verify_chain(file_engine).valid
    # ... and the REMEMBERED anchor is not.
    result = verifier.verify()
    assert not result.valid
    assert result.basis == "full-chain"
    assert "does not extend the head verified at seq 10" in result.detail


# ---------------------------------------------------------------------------
# MISSES — the exact gap, pinned so the claim cannot drift from the behaviour.
# ---------------------------------------------------------------------------


def test_unpropagated_prefix_tamper_is_MISSED_by_the_anchored_proof(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """The documented gap, PINNED BY EXECUTION: a tamper at or before the anchor
    that is NOT propagated forward leaves the anchor successor's ``prev_hash``
    untouched, so the anchored proof cannot see it.

    This test asserts the MISS on purpose.  It exists so that the scope
    sentences in ``ne/audit/anchor.py`` and
    ``ModelSelectionService._frontier_records`` can never quietly become
    stronger than the code: if someone widens detection, this test reds and the
    docs must be re-read and rewritten deliberately, not by accident.

    The same tamper IS caught by the full verify (asserted below), which is
    what makes the background full verify a real control and not decoration.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    _seed(file_engine, audit, 3, start=100)

    with file_engine.begin() as conn:
        _drop_triggers(conn)
        # Payload edited; record_hash left stale — the ledger is now internally
        # inconsistent at seq 4, and seq 11's prev_hash is untouched.
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 4)
            .values(actor="attacker-owned-the-box")
        )

    missed = verifier.verify()
    assert missed.valid, "the documented gap: an unpropagated prefix tamper is not seen here"
    assert missed.basis == "anchored-extension"

    # The control that makes the gap bounded rather than permanent.
    caught = verifier.full_verify()
    assert not caught.valid
    assert "seq 4" in caught.detail
    assert verifier.anchor is None, "a failed full verify must CLEAR the anchor"


def test_self_consistent_rewrite_ABOVE_the_anchor_is_missed_by_BOTH(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """The SECOND missed class, and the one the docstrings used to deny outright.

    ``test_self_consistent_prefix_rewrite_is_detected_by_the_remembered_anchor``
    above covers ``k <= A`` — the PREFIX case — and the anchor genuinely beats
    ``verify_chain`` there.  This is the OTHER half of the same attack, and the
    half the scope sentences drifted on: rewrite a record STRICTLY ABOVE the
    anchor, self-consistently, all the way to a coherent new head.

    Row ``A+1``'s ``prev_hash`` is not touched, so it still equals the
    remembered ``H_A`` and the anchor's own link check is satisfied; every row
    above re-hashes correctly because the attacker recomputed them.  (The
    ``prev_hash`` is what survives, not the row: for ``k = A+1`` the anchor's
    successor IS the rewritten row, and the attack still works, because the
    rewrite recomputes k's ``record_hash`` FROM its existing ``prev_hash``.)  The forward walk
    has nothing left to object to.

    ⚠ THE POINT OF THIS TEST IS THE PAIR OF ASSERTIONS, NOT EITHER ALONE.
    ``verify_chain`` is asserted to be fooled by the SAME forgery.  That is what
    makes this a documentation-scope defect rather than a regression: the
    anchored proof gives up nothing here, because the O(chain) walk it replaced
    would have blessed this ledger too.  It is the inherent blind spot of a
    forward hash chain with no external anchor (``ne/audit/verify.py``'s
    "inherent limitation" paragraph), and its control is
    :mod:`ne.audit.checkpoint`'s signed WORM heads, not a longer walk.

    This test exists so the corrected scope text in ``ne/audit/anchor.py``
    (answer (2)'s MISSED list), ``packages/ne-audit/README.md`` and
    ``ModelSelectionService._frontier_records`` is pinned by execution.  Before
    the correction those documents listed *"any tamper of a record at ``seq >
    A`` — payload edited, ``record_hash`` recomputed, ``prev_hash`` relinked, a
    denormalized query column swapped"* under DETECTED, which is an exact
    description of what this test does, and it is NOT detected.

    MUTATION: re-widen any of those DETECTED lists back to "any tamper at
    ``seq > A``" → the text and this test contradict each other again, and the
    ``anchored.valid`` assertion below is the proof of which one is lying.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    anchor = verifier.anchor
    assert anchor is not None and anchor.seq == 10
    _seed(file_engine, audit, 4, start=100)  # rows 11..14, appended after the blessing

    # Rewrite record 12 — STRICTLY ABOVE the anchor — and propagate to the head.
    # Every one of the nine denormalized text columns plus ``occurred_at`` is
    # rewritten too, so the forgery also survives the column/payload cross-check
    # (a tamper caught by THAT check would prove nothing about the link check).
    forged = audit(event_id="e101", actor="attacker-owned-the-box")
    forged_payload = forged.model_dump(mode="json")
    with file_engine.begin() as conn:
        _drop_triggers(conn)
        prev_hash = _rewrite_row(conn, 12, forged_payload, forged.occurred_at)
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 12)
            .values({name: forged_payload.get(name) for name in _DENORMALIZED_TEXT_COLUMNS})
        )
        for seq in (13, 14):
            row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == seq)).one()
            payload = dict(row.payload)
            new_hash = compute_record_hash(prev_hash, payload)
            conn.execute(
                ne_audit_log.update()
                .where(ne_audit_log.c.seq == seq)
                .values(prev_hash=prev_hash, record_hash=new_hash)
            )
            prev_hash = new_hash
        conn.execute(
            ne_audit_head.update()
            .where(ne_audit_head.c.id == 1)
            .values(seq=14, record_hash=prev_hash)
        )

    # The anchor's successor is untouched, which is WHY the extension proof passes.
    with file_engine.connect() as conn:
        successor = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == 11)).one()
    assert successor.prev_hash == anchor.record_hash

    # HALF ONE: the full walk this proof replaced is fooled ...
    assert verify_chain(file_engine).valid, (
        "a self-consistent rewrite above the anchor is verify_chain's own documented "
        "blind spot; if this ever goes RED the anchored claim must be re-widened too"
    )
    # HALF TWO: ... and so is the anchored proof. Same forgery, same verdict.
    anchored = verifier.verify()
    assert anchored.valid, "the second documented gap: NOT detected, and not a regression"
    assert anchored.basis == "anchored-extension"

    # And the control that IS supposed to catch it is not this module: a signed
    # WORM checkpoint taken before the rewrite pins the head hash the rewrite moved.
    # (See ne.audit.checkpoint; asserted there, not here.)


@pytest.mark.parametrize(
    ("keep_through", "expect_missed"),
    [
        (12, True),  # head left ABOVE the anchor  -> valid SHORTER extension
        (11, True),  # head left at A+1, minimal   -> valid SHORTER extension
        (10, True),  # head left exactly AT A      -> valid EMPTY extension
        (9, False),  # head rewound BELOW A        -> refused; the buy-back
    ],
)
def test_coherent_suffix_truncation_at_or_above_the_anchor_is_missed_by_BOTH(
    file_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    keep_through: int,
    expect_missed: bool,
) -> None:
    """The other shape of the second missed class: TRUNCATION, not rewrite.

    Deleting the suffix above the anchor and rewinding ``ne_audit_head``
    coherently leaves a ledger that is internally perfect and still links onto
    the remembered anchor hash.  Rows that really existed are simply gone, and
    a forward hash chain cannot prove a record once existed past a
    self-consistent head — so neither proof can object.

    ⚠ THE PARAMETRISATION IS THE POINT, because the boundary is STRICT and it
    is easy to state wrongly in either direction:

    * ``t > A`` — a valid SHORTER extension. MISSED by both.
    * ``t == A`` — a valid EMPTY extension: the truncation restores exactly the
      head the anchor blessed, so there is nothing left to disagree with.
      MISSED by both.  A claim of the form "truncation back to or below the
      anchor is detected" is WRONG here by exactly one row.
    * ``t < A`` — REFUSED, because the head is now behind a head this process
      verified (``if head_seq < baseline_seq`` in ``_verified_extension``).
      This is a genuine gain over ``verify_chain``, which blesses it — and it
      is the case ``test_head_rewound_below_the_anchor_is_detected`` covers in
      the DETECTED section.

    ``verify_chain`` is asserted VALID in every one of the four cases, which is
    what makes the three misses a shared scope limit rather than a regression:
    the O(chain) walk this proof replaced would have blessed each of them too.
    The control is :mod:`ne.audit.checkpoint`'s signed WORM heads.

    MUTATION (run, and the result is more interesting than expected): changing
    ``if head_seq < baseline_seq`` to ``<=`` in ``_verified_extension`` turns
    ALL FOUR cases RED — not just ``keep_through=10`` — and they fail in
    ``_anchored``, before any truncation happens, with *"full chain verify
    passed ... but its head snapshot at seq 10 could not be confirmed"*.

    ⭐ That is the structural reason ``t == A`` cannot simply be tightened into
    the DETECTED column: ``AnchoredChainVerifier._full_verify`` CONFIRMS every
    candidate anchor by asking ``verified_extension`` for an empty extension at
    ``head == baseline``.  The rule that makes a coherent truncation back to
    exactly ``A`` indistinguishable from an intact chain is the same rule that
    installs anchors at all.  Detecting it needs an external anchor
    (:mod:`ne.audit.checkpoint`), not a tighter comparison — so the strictness
    here is load-bearing, not an off-by-one anyone should "fix".

    The ``<`` direction is pinned from the other side by
    ``test_head_rewound_below_the_anchor_is_detected``'s own mutation.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    assert verifier.anchor is not None and verifier.anchor.seq == 10
    _seed(file_engine, audit, 4, start=100)  # rows 11..14, appended after the blessing

    with file_engine.begin() as conn:
        _drop_triggers(conn)
        _truncate_to(conn, keep_through)

    # The full walk this proof replaced is fooled in EVERY case, including the
    # one the anchor catches — that asymmetry is the whole value of the anchor.
    assert verify_chain(file_engine).valid, (
        "a coherent truncation is verify_chain's own documented blind spot; if this "
        "goes RED the anchored claims must be re-read, not merely re-widened"
    )

    result = verifier.verify()
    if expect_missed:
        assert result.valid, "the second documented gap: NOT detected, and not a regression"
        assert result.basis == "anchored-extension"
    else:
        assert not result.valid
        assert result.basis == "full-chain"
        assert "does not extend the head verified at seq 10" in result.detail


def test_the_missed_tamper_is_caught_once_the_anchor_ages_out(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """The gap above is BOUNDED IN CODE, not merely by a background task that
    might not run: once the blessing is older than ``max_anchor_age_s`` the
    verifier refuses the anchor and runs the full verify inline, which catches
    the tamper on the request path itself.

    MUTATION: delete the ``if age_s > self.max_anchor_age_s`` branch from
    ``AnchoredChainVerifier.verify`` → THIS TEST GOES RED, and the undetected
    window becomes unbounded.
    """
    clock = FakeClock()
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine, clock)

    with file_engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 4)
            .values(actor="attacker-owned-the-box")
        )

    assert verifier.verify().valid  # inside the window: missed, as documented
    clock.advance(CHAIN_ANCHOR_MAX_AGE_S + 0.1)
    aged = verifier.verify()
    assert not aged.valid
    assert aged.basis == "full-chain"


def test_extension_proofs_never_renew_the_blessing_age(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """An anchor advanced by proof carries its ORIGINAL ``blessed_at``.

    This is what keeps the undetected-prefix-tamper window bounded on a busy
    deployment: without it, a chain that is appended to constantly would renew
    its own blessing forever and the full verify would never be required again.

    MUTATION: set ``blessed_at=self._clock()`` when building ``advanced`` in
    ``AnchoredChainVerifier.verify`` → THIS TEST GOES RED.
    """
    clock = FakeClock()
    _seed(file_engine, audit, 5)
    verifier = _anchored(file_engine, clock)
    blessed_at = verifier.anchor.blessed_at if verifier.anchor else None
    assert blessed_at is not None

    for i in range(5):
        clock.advance(CHAIN_ANCHOR_MAX_AGE_S / 10)
        _seed(file_engine, audit, 1, start=200 + i)
        result = verifier.verify()
        assert result.valid and result.basis == "anchored-extension"
        anchor = verifier.anchor
        assert anchor is not None
        assert anchor.seq == 6 + i, "the anchor DOES advance in position"
        assert anchor.blessed_at == blessed_at, "but never in blessing age"
        assert anchor.extensions == i + 1

    clock.advance(CHAIN_ANCHOR_MAX_AGE_S)
    assert verifier.verify().basis == "full-chain"


# ---------------------------------------------------------------------------
# FALLBACK — never silently "verified".
# ---------------------------------------------------------------------------


def test_missing_anchor_runs_a_full_verify_inline(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """A cold process holds no blessing, so its first verify is a full one.

    MUTATION: make ``verify()`` return a valid ``anchored-extension`` result
    when ``self._anchor is None`` → THIS TEST GOES RED.
    """
    _seed(file_engine, audit, 8)
    verifier = AnchoredChainVerifier(file_engine, clock=FakeClock())
    assert verifier.anchor is None

    result = verifier.verify()
    assert result.valid
    assert result.basis == "full-chain"
    assert result.checked == 8, "a full verify walks the whole ledger, and says so"
    assert verifier.anchor is not None, "and it installs the anchor for later calls"


def test_missing_anchor_on_a_BROKEN_chain_refuses(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """The cold path must fail closed, not merely be slow.

    MUTATION: return ``valid=True`` from ``_full_verify`` regardless of
    ``cv.valid`` → THIS TEST GOES RED.
    """
    _seed(file_engine, audit, 8)
    with file_engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.update().where(ne_audit_log.c.seq == 3).values(actor="attacker"))

    verifier = AnchoredChainVerifier(file_engine, clock=FakeClock())
    result = verifier.verify()
    assert not result.valid
    assert verifier.anchor is None


def test_a_corrupted_anchor_hash_never_blesses_the_chain(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """Corrupt the anchor ITSELF (the value the whole scheme trusts) and the
    verifier must refuse to bless off it: the extension proof fails, the anchor
    is dropped, and the verdict comes from a real full verify instead.

    MUTATION: make ``verify()`` return valid when ``verified_extension``
    returns ``None`` → THIS TEST GOES RED.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    good = verifier.anchor
    assert good is not None
    with verifier._lock:
        verifier._anchor = VerifiedAnchor(
            seq=good.seq,
            record_hash="f" * 64,
            blessed_at=good.blessed_at,
            blessed_checked=good.blessed_checked,
            extensions=0,
        )

    result = verifier.verify()
    assert result.basis == "full-chain", "a bad anchor must never yield an anchored verdict"
    assert not result.valid, "a contradicted anchor refuses the call, it does not shrug"
    # The bad anchor was replaced by a freshly blessed one, not kept, so the
    # process recovers on the next call instead of wedging.
    fresh = verifier.anchor
    assert fresh is not None and fresh.record_hash != "f" * 64
    assert verifier.verify().valid


def test_a_corrupted_anchor_SEQ_never_blesses_the_chain(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """An anchor pointing past the real head is refused the same way.

    MUTATION: drop ``if head_seq < baseline_seq: return None`` from
    ``_verified_extension`` → THIS TEST GOES RED.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    good = verifier.anchor
    assert good is not None
    with verifier._lock:
        verifier._anchor = VerifiedAnchor(
            seq=9_999,
            record_hash=good.record_hash,
            blessed_at=good.blessed_at,
            blessed_checked=good.blessed_checked,
            extensions=0,
        )

    contradicted = verifier.verify()
    assert contradicted.basis == "full-chain"
    assert not contradicted.valid
    assert verifier.verify().valid, "and the adopted head lets the next call proceed"


def test_a_failed_full_verify_clears_the_anchor(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """A background full verify that finds a break must not leave the previous
    blessing standing — otherwise the request path keeps serving anchored
    verdicts off a blessing the system has since contradicted.

    MUTATION: delete the ``self._clear()`` call on the ``not cv.valid`` branch
    of ``_full_verify`` → THIS TEST GOES RED.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    assert verifier.anchor is not None

    with file_engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.update().where(ne_audit_log.c.seq == 2).values(actor="attacker"))

    assert not verifier.full_verify().valid
    assert verifier.anchor is None
    # ... and the next request-path call now refuses, rather than serving anchored.
    after = verifier.verify()
    assert not after.valid
    assert after.basis == "full-chain"


def test_a_full_verify_that_RAISES_clears_the_anchor(
    file_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A walk that never reaches a verdict must withdraw the blessing too.

    A DB fault mid-walk (dropped connection, statement timeout, failover) is not
    evidence that the chain is broken — it is evidence that nobody can currently
    say it is intact.  The anchor is a STANDING CLAIM that a full verify passed
    within ``max_anchor_age_s``, so a failed renewal withdraws it: fail-closed,
    and symmetric with ``verify()``, whose own ``except Exception`` around the
    extension proof has always cleared.  Without this, reads keep being served
    ``anchored-extension`` verdicts off a blessing whose renewal is failing,
    for up to ``CHAIN_ANCHOR_MAX_AGE_S`` — and the refresher's ERROR log tells
    the operator the opposite.

    The clear is UNCONDITIONAL, exactly like the ``not cv.valid`` branch: a
    failed full verify contradicts every anchor, which is the rule ``_clear``'s
    own docstring states for this method.  ``test_a_RAISING_full_verify_clears
    _an_anchor_a_concurrent_EXTENSION_advanced`` is why it cannot be an identity
    CAS on the snapshot.

    MUTATION-CHECK: delete the ``self._clear()`` line from ``_full_verify``'s
    ``except Exception`` handler → THIS TEST GOES RED at ``assert
    verifier.anchor is None`` with ``VerifiedAnchor(...) is not None``, and the
    follow-up ``verify()`` returns ``basis == "anchored-extension"`` instead of
    the full walk.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    blessed = verifier.anchor
    assert blessed is not None

    boom = OperationalError("SELECT 1", {}, Exception("canceling statement: timeout"))
    monkeypatch.setattr("ne.audit.anchor.verify_chain", lambda _conn: (_ for _ in ()).throw(boom))
    with pytest.raises(OperationalError):
        verifier.full_verify()

    assert verifier.anchor is None, (
        "a full verify that RAISED left the blessing standing — evidence reads "
        "would keep being served anchored verdicts off it"
    )

    # ... and once the walk is possible again, the request path pays the full
    # inline walk exactly as the refresher's log claims, then re-anchors.
    monkeypatch.undo()
    after = verifier.verify()
    assert after.valid
    assert after.basis == "full-chain", "the fallback must be the FULL walk, not the anchor"
    assert after.checked == 10
    assert verifier.anchor is not None, "and a successful inline walk re-blesses"


def test_a_RAISING_full_verify_clears_an_anchor_a_concurrent_EXTENSION_advanced(
    concurrent_reader_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: a CAS on the snapshot SILENTLY NO-OPS against an extension.

    ``_full_verify`` snapshots the anchor it is renewing BEFORE its walk, and
    that walk runs UNLOCKED.  A concurrent request-path :meth:`verify` in the
    same window takes the extension path and installs a **new** ``VerifiedAnchor``
    object — deliberately carrying the SAME ``blessed_at``, because advancing
    the position must never renew the blessing's age.  ``_clear`` compares by
    IDENTITY, so an identity CAS on the snapshot matched nothing and the failed
    renewal withdrew NOTHING: reads kept being served ``anchored-extension``
    verdicts off the very blessing whose renewal had just failed, for up to
    ``CHAIN_ANCHOR_MAX_AGE_S``.

    ⭐ THE CAS SPARED EXACTLY THE OBJECT IT HAD TO WIPE.  Its rationale was
    "spare a blessing a concurrent caller installed" — but an extension is NOT a
    new blessing.  It is the same blessing at a further seq, and it is only ever
    as trustworthy as the full verify that produced its ``blessed_at``: the one
    that just failed to renew.

    Nothing is stubbed except ``verify_chain`` itself (the fault being
    simulated): the interleaved extension is driven through the REAL
    :meth:`verify` on the REAL classes, so the anchor object it installs is the
    genuine article rather than a hand-built stand-in.  Forcing the interleave
    from inside the stub, rather than racing two threads for it, is what makes
    it deterministic; the branch exercised is identical.

    MUTATION-CHECK: restore the identity CAS —
    ``if renewing is not None: self._clear(renewing)`` — in ``_full_verify``'s
    ``except Exception`` handler → THIS TEST GOES RED at ``assert
    verifier.anchor is None`` with the extension's ``VerifiedAnchor(...)`` still
    standing, and the follow-up ``verify()`` returns ``anchored-extension``.
    """
    clock = FakeClock()
    _seed(concurrent_reader_engine, audit, 10)
    verifier = _anchored(concurrent_reader_engine, clock)
    blessed = verifier.anchor
    assert blessed is not None and blessed.seq == 10

    # Rows the concurrent reader's extension proof will carry the anchor over.
    _seed(concurrent_reader_engine, audit, 4, start=10)

    extended: list[VerifiedAnchor] = []

    def _extend_concurrently_then_fail(_conn: sa.Connection) -> ChainVerification:
        # The REQUEST path, mid-walk: a real anchored verify that advances the
        # anchor by proof and installs a NEW object with the SAME blessed_at.
        result = verifier.verify()
        assert result.valid and result.basis == "anchored-extension"
        advanced = verifier.anchor
        assert advanced is not None
        assert advanced is not blessed, "the extension must install a NEW object"
        assert advanced.seq == 14
        assert advanced.blessed_at == blessed.blessed_at, (
            "an extension carries the blessing age forward — which is exactly why "
            "an identity CAS on the snapshot is the wrong test"
        )
        extended.append(advanced)
        raise OperationalError("SELECT 1", {}, Exception("connection reset"))

    monkeypatch.setattr("ne.audit.anchor.verify_chain", _extend_concurrently_then_fail)
    with pytest.raises(OperationalError):
        verifier.full_verify()

    assert extended, "the interleaved extension never ran — the test proves nothing"
    assert verifier.anchor is None, (
        "the failed renewal left the extended blessing standing: reads keep being "
        "served anchored verdicts off a blessing whose renewal is known to be failing"
    )

    # ... and the next read pays the full inline walk the refusal promised.
    monkeypatch.undo()
    after = verifier.verify()
    assert after.valid
    assert after.basis == "full-chain", "the fallback must be the FULL walk, not the anchor"
    assert after.checked == 14


def test_a_RAISING_full_verify_also_clears_a_fresher_concurrent_blessing(
    file_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The over-clear is DECLARED, not discovered: it is a cost, not a hole.

    The unconditional clear also drops an anchor a concurrent ``_full_verify``
    genuinely re-blessed while this walk was faulting.  That is real and it is
    accepted rather than optimized away: the cost is ONE extra inline O(chain)
    walk on the next read, which then re-anchors — it never serves a weaker
    claim, and it is the same self-healing cost this module already pays on
    every cold start and age-out.  The alternative (a ``blessed_at``-keyed CAS)
    buys that one walk back at the price of a rule with two cases, in the branch
    whose whole job is to fail closed.

    This test exists so the trade cannot be made silently in either direction.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    stale = verifier.anchor
    assert stale is not None
    fresh = VerifiedAnchor(
        seq=stale.seq,
        record_hash=stale.record_hash,
        blessed_at=stale.blessed_at + 1.0,
        blessed_checked=stale.blessed_checked,
        extensions=0,
    )

    def _install_fresh_then_fail(_conn: sa.Connection) -> ChainVerification:
        with verifier._lock:
            verifier._anchor = fresh
        raise OperationalError("SELECT 1", {}, Exception("connection reset"))

    monkeypatch.setattr("ne.audit.anchor.verify_chain", _install_fresh_then_fail)
    with pytest.raises(OperationalError):
        verifier.full_verify()

    assert verifier.anchor is None, (
        "the unconditional clear is the contract — a failed full verify "
        "contradicts EVERY anchor, and the cost is one inline walk, not a weaker claim"
    )


def test_stale_anchor_is_refused_even_when_the_chain_is_fine(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """The age bound is unconditional: a stale blessing is refused on a healthy
    chain too, so the full verify genuinely recurs rather than being skipped
    whenever nothing looks wrong.

    MUTATION: only apply the age check when the extension proof fails →
    THIS TEST GOES RED.
    """
    clock = FakeClock()
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine, clock)
    assert verifier.verify().basis == "anchored-extension"

    clock.advance(CHAIN_ANCHOR_MAX_AGE_S + 1)
    result = verifier.verify()
    assert result.valid
    assert result.basis == "full-chain"
    assert result.checked == 10


def test_max_age_below_the_full_verify_interval_is_a_construction_error() -> None:
    """A configuration whose anchor expires before the loop that renews it can
    run would put the inline full verify on EVERY request — the exact failure
    this module exists to remove — while still looking configured.

    MUTATION: delete the ``ValueError`` guard in ``__init__`` → THIS TEST GOES RED.
    """
    engine = sa.create_engine("sqlite://")
    with pytest.raises(ValueError, match="max_anchor_age_s must be >="):
        AnchoredChainVerifier(engine, full_verify_interval_s=300.0, max_anchor_age_s=60.0)


def test_an_unconfirmable_head_snapshot_never_becomes_an_anchor(
    file_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anchor hash is the ONE value every later proof depends on, so a
    candidate head that cannot be tied back to the walk that blessed it must
    never be installed — even when the walk itself passed.

    The candidate is forced wrong here (a stubbed ``read_head_or_none``) because the
    natural way to reach this branch is a race: an append landing between the
    candidate read and ``verify_chain``'s own head read.  Racing it is
    non-deterministic; forcing it is not, and it exercises the same branch.

    MUTATION: replace the ``verified_extension`` confirmation in
    ``AnchoredChainVerifier._full_verify`` with
    ``confirmed = (candidate_seq, candidate_hash, ())`` → THIS TEST GOES RED,
    because a head snapshot that no proof ties to the chain is installed as the
    process's trust root anyway.
    """
    _seed(file_engine, audit, 10)
    monkeypatch.setattr("ne.audit.anchor.read_head_or_none", lambda _conn: (10, "e" * 64))

    verifier = AnchoredChainVerifier(file_engine, clock=FakeClock())
    result = verifier.full_verify()
    assert not result.valid
    assert verifier.anchor is None, "an unconfirmed head must never become the trust root"
    assert "could not be confirmed" in result.detail


def test_the_blessed_anchor_is_confirmed_against_the_chain_it_blessed(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """``_full_verify`` must not install a head snapshot it cannot tie back to
    the walk that blessed it — the anchor hash is the single value everything
    later depends on, so it is confirmed as an extension baseline before use.

    MUTATION: replace the ``verified_extension`` confirmation in
    ``_full_verify`` with ``confirmed = (candidate_seq, candidate_hash, ())``
    → THIS TEST GOES RED, because the head row is lying about its hash and the
    anchor would be installed anyway.
    """
    _seed(file_engine, audit, 10)
    with file_engine.begin() as conn:
        # The head ROW disagrees with the chain's real tail hash. verify_chain
        # itself catches this (head mismatch), so the anchor must never install.
        conn.execute(
            ne_audit_head.update().where(ne_audit_head.c.id == 1).values(record_hash="b" * 64)
        )

    verifier = AnchoredChainVerifier(file_engine, clock=FakeClock())
    result = verifier.full_verify()
    assert not result.valid
    assert verifier.anchor is None


# ---------------------------------------------------------------------------
# THE NAMED BREAK SURVIVES THE FALLBACK.
#
# `detail` is a sentence; `first_break_seq` / `first_break_event_id` / `reason`
# are the machine-readable identity of the break, and a caller that RENDERS a
# break needs the latter. The audit console prints "Break at entry N" from
# `first_break_seq`; before `AnchoredVerification.chain` existed, routing that
# console through this verifier would have downgraded every named break to "the
# server did not name the entry" — a real loss of information dressed up as a
# latency fix.
# ---------------------------------------------------------------------------


def test_a_full_chain_fallback_carries_the_NAMED_break_not_just_a_sentence(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """A ``basis="full-chain"`` verdict hands back ``verify_chain``'s own result.

    MUTATION: drop ``chain=cv`` from the ``not cv.valid`` return in
    ``_full_verify`` → THIS TEST GOES RED.
    """
    _seed(file_engine, audit, 8)
    with file_engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.update().where(ne_audit_log.c.seq == 3).values(actor="attacker"))

    result = AnchoredChainVerifier(file_engine, clock=FakeClock()).verify()

    assert not result.valid
    assert result.basis == "full-chain"
    assert result.chain is not None
    assert result.chain.valid is False
    assert result.chain.first_break_seq == 3
    assert result.chain.first_break_event_id is not None
    assert result.chain.reason == "column/payload mismatch: actor"
    # `detail` is the SAME sentence verify_chain produced — the structured
    # fields are an addition to it, never a replacement.
    assert result.detail == result.chain.detail


def test_an_ANCHORED_proof_carries_no_chain_result_because_it_ran_no_full_walk(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """``chain`` is present exactly when a real ``verify_chain`` ran on this call.

    It is evidence of which proof executed, so an anchored result must not
    fabricate one — a caller reading ``chain is not None`` as "the whole ledger
    was walked here" has to be right.

    MUTATION: setting ``chain=`` to anything but ``None`` on ``verify``'s
    anchored-extension return → THIS TEST GOES RED.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    _seed(file_engine, audit, 3, start=100)

    result = verifier.verify()

    assert result.valid
    assert result.basis == "anchored-extension"
    assert result.chain is None


def test_a_VALID_full_verify_still_carries_its_chain_result(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """The happy cold path carries it too — ``chain`` tracks WHICH PROOF RAN, not
    whether it failed.

    ⚠ This is why ``chain.valid`` must never be read as the verdict: on the
    ``contradicted`` and ``unconfirmed`` paths it is TRUE while
    :attr:`AnchoredVerification.valid` is FALSE. ``valid`` is the verdict.
    """
    _seed(file_engine, audit, 6)
    result = AnchoredChainVerifier(file_engine, clock=FakeClock()).verify()

    assert result.valid
    assert result.basis == "full-chain"
    assert result.chain is not None
    assert result.chain.valid is True
    assert result.chain.checked == 6
    assert result.chain.first_break_seq is None


def test_an_UNCONFIRMED_head_refuses_while_its_chain_result_reads_VALID(
    file_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disagreement, pinned: the ledger walks CLEAN and the call still refuses.

    ``_full_verify``'s unconfirmed-head branch returns ``valid=False`` while the
    ``verify_chain`` it just ran returned ``valid=True``.  A caller that rendered
    ``chain.valid`` instead of ``valid`` would report this state as VERIFIED —
    the exact misreading :attr:`AnchoredVerification.chain`'s docstring warns
    about — so it is proved by execution rather than asserted in prose.

    Same forcing as ``test_an_unconfirmable_head_snapshot_never_becomes_an_anchor``
    (a stubbed ``read_head_or_none``): the natural route to this branch is a race, and
    racing it is non-deterministic.

    MUTATION: read ``result.chain.valid`` as the verdict anywhere a caller
    renders one → this test is the demonstration that it is wrong.
    """
    _seed(file_engine, audit, 10)
    monkeypatch.setattr("ne.audit.anchor.read_head_or_none", lambda _conn: (10, "e" * 64))

    result = AnchoredChainVerifier(file_engine, clock=FakeClock()).full_verify()

    assert result.valid is False  # THE VERDICT
    assert result.chain is not None
    assert result.chain.valid is True  # ...and the walk it ran disagrees
    assert result.chain.first_break_seq is None


def _delete_head_row(engine: sa.Engine) -> None:
    """Drop the ``ne_audit_head`` row.

    ``ne_audit_log`` carries no-update/no-delete triggers; ``ne_audit_head`` does
    NOT, so this is the one tamper an attacker can perform with no trigger in the
    way — and it is exactly the length of the chain that it unpins.
    """
    with engine.begin() as conn:
        conn.execute(sa.delete(ne_audit_head))


def test_a_DELETED_head_row_is_a_NAMED_break_out_of_full_verify_not_a_raise(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """``verify_chain`` has always named this; the verifier must not downgrade it.

    ``_full_verify`` takes its extension baseline BEFORE the walk.  Taking it
    with ``ne.audit.store.read_head`` (``.one()``) made an absent head row an
    ``sqlalchemy.exc.NoResultFound`` out of a method whose ``except`` ends in a
    bare ``raise`` — so the one caller that matters most, the audit page, 500'd
    instead of rendering "Break at entry 0 / audit head missing".  A named tamper
    accusation became indistinguishable from the database being down.  The
    accusation itself is asserted equal to ``verify_chain``'s, field by field, so
    this cannot be satisfied by a generic sentence.

    MUTATION 1: ``candidate = read_head_or_none(conn)`` →
    ``candidate = read_head(conn)`` (re-importing it) → THIS TEST GOES RED with
    ``NoResultFound``.
    MUTATION 2: dropping ``chain=cv``/``detail=cv.detail`` from ``_full_verify``'s
    ``not cv.valid`` return in favour of a generic sentence → THIS TEST GOES RED,
    which is what stops the fix from being satisfied by an unnamed refusal.
    """
    _seed(file_engine, audit, 6)
    verifier = _anchored(file_engine)

    _delete_head_row(file_engine)

    result = verifier.full_verify()

    assert result.valid is False
    assert result.basis == "full-chain"
    assert verifier.anchor is None, "the blessing must be withdrawn, not kept"
    assert result.chain is not None
    assert result.chain.first_break_seq == 0
    assert result.chain.reason == "audit head missing (ne_audit_head row absent)"

    # Field-for-field the SAME accusation the unanchored path makes.
    with file_engine.connect() as conn:
        direct = verify_chain(conn)
    assert result.chain == direct
    assert result.detail == direct.detail


def test_the_request_path_falls_back_to_a_NAMED_break_when_the_head_row_is_deleted(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """End-to-end on :meth:`AnchoredChainVerifier.verify` — the REQUEST path.

    A warm anchor does not rescue this: the extension proof cannot confirm a head
    that is not there, so it returns ``None`` and the verifier falls back to the
    full walk.  That fallback is ``_full_verify``, i.e. the path that used to
    raise, which is why the request path inherited the regression even though it
    never reads the head itself.

    MUTATION: ``candidate = read_head(conn)`` in ``_full_verify`` → THIS TEST
    GOES RED with ``NoResultFound``.
    """
    _seed(file_engine, audit, 6)
    verifier = _anchored(file_engine)
    assert verifier.verify().valid is True

    _delete_head_row(file_engine)

    result = verifier.verify()
    assert result.valid is False
    assert result.basis == "full-chain"
    assert result.chain is not None
    assert result.chain.first_break_seq == 0
    assert result.chain.reason == "audit head missing (ne_audit_head row absent)"

    # And with no warm anchor left, the very next call says the same thing.
    assert verifier.anchor is None
    repeat = verifier.verify()
    assert repeat.valid is False
    assert repeat.chain is not None
    assert repeat.chain.first_break_seq == 0


def test_a_head_row_that_APPEARS_only_after_the_baseline_read_installs_no_anchor(
    file_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restore-mid-verify race: absent at the snapshot, present for the walk.

    ``verify_chain`` then passes over a chain this process has no baseline for.
    There is nothing to confirm as an extension baseline, so no anchor may be
    installed and the call must refuse — the same fail-closed shape as an
    unconfirmable baseline, for the same reason (the anchor hash is the single
    value every later proof chains from).

    Forced with a stubbed ``read_head_or_none`` rather than raced, for the same
    reason ``test_an_unconfirmable_head_snapshot_never_becomes_an_anchor`` forces
    its branch: racing it is non-deterministic, forcing it is not.

    MUTATION: make the ``candidate is None`` branch fall through to
    ``verified_extension`` (or return ``valid=cv.valid``) → THIS TEST GOES RED.
    """
    _seed(file_engine, audit, 10)
    monkeypatch.setattr("ne.audit.anchor.read_head_or_none", lambda _conn: None)

    verifier = AnchoredChainVerifier(file_engine, clock=FakeClock())
    result = verifier.full_verify()

    assert result.valid is False  # THE VERDICT
    assert result.basis == "full-chain"
    assert verifier.anchor is None, "no baseline means no trust root"
    assert result.chain is not None
    assert result.chain.valid is True  # ...and the walk it ran disagrees
    assert "ne_audit_head row was absent" in result.detail


def test_the_CONTRADICTED_refusal_lasts_exactly_ONE_call(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """Why the flag has to exist, proved by execution rather than argued.

    The same coherent prefix rewrite as
    ``test_self_consistent_prefix_rewrite_is_detected_by_the_remembered_anchor``.
    The refusal is real and it is the strongest signal here — ``verify_chain``
    cannot see this attack at all — and the SECOND call passes, because the
    fallback adopted the freshly verified head on its way out (deliberately: the
    alternative is a wedged process on a legitimate restore or failover).

    So a consumer that waits for a second disagreeing read waits forever. That
    is not a tuning problem, it is a structural one, and the only fix at this
    layer is to say on the verdict that it will not repeat.

    MUTATION: ``contradiction=False`` on ``_full_verify``'s contradicted return
    → the first assertion GOES RED.
    MUTATION: dropping the anchor installation before the contradicted return
    (so the refusal repeats) → the last assertion GOES RED, and the flag would
    no longer be necessary.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    anchor = verifier.anchor
    assert anchor is not None and anchor.seq == 10

    forged = audit(event_id="e3", occurred_at=datetime(1999, 1, 1, tzinfo=UTC))
    forged_payload = forged.model_dump(mode="json")
    with file_engine.begin() as conn:
        _drop_triggers(conn)
        prev_hash = _rewrite_row(conn, 4, forged_payload, forged.occurred_at)
        for seq in range(5, 11):
            row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == seq)).one()
            payload = dict(row.payload)
            new_hash = compute_record_hash(prev_hash, payload)
            conn.execute(
                ne_audit_log.update()
                .where(ne_audit_log.c.seq == seq)
                .values(prev_hash=prev_hash, record_hash=new_hash)
            )
            prev_hash = new_hash
        conn.execute(
            ne_audit_head.update()
            .where(ne_audit_head.c.id == 1)
            .values(seq=10, record_hash=prev_hash)
        )

    first = verifier.verify()
    assert first.valid is False
    assert first.contradiction is True
    # The walk UNDER it passed — that is what makes this verdict unlike every
    # other invalid one, and why it names no record.
    assert first.chain is not None and first.chain.valid is True

    second = verifier.verify()
    assert second.valid is True
    assert second.contradiction is False


def test_an_ORDINARY_break_is_NOT_marked_a_contradiction(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """The flag is NARROW, and that is the point.

    A genuine break found by ``verify_chain`` IS the verdict the
    read-during-append race produces (the head-mismatch arm names the moving
    head), so it is exactly what a consumer's consecutive-reads rule is for. If
    this flag were set here it would turn one straddled append into a latched
    global BREAK — the flap this whole arrangement exists to suppress — so a
    consumer would be right to ignore it.

    MUTATION: defaulting ``AnchoredVerification.contradiction`` to ``True``, or
    setting it on ``_full_verify``'s ``not cv.valid`` return → THIS TEST GOES
    RED.
    """
    _seed(file_engine, audit, 10)
    verifier = _anchored(file_engine)
    _seed(file_engine, audit, 5, start=100)

    with file_engine.begin() as conn:
        _drop_triggers(conn)
        row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == 13)).one()
        payload = dict(row.payload)
        payload["actor"] = "attacker-owned-the-box"
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 13)
            .values(payload=payload, actor="attacker-owned-the-box")
        )

    result = verifier.verify()
    assert result.valid is False
    assert result.chain is not None and result.chain.valid is False  # a REAL break
    assert result.contradiction is False


def test_an_UNCONFIRMED_head_is_also_a_contradiction(
    file_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same shape, same reason: the walk passed and the call still refuses.

    ``_full_verify``'s unconfirmed-head branch means the head snapshot the walk
    was taken against could not be confirmed as an extension baseline. A
    legitimate concurrent append cannot produce it (the extension covers every
    row appended during the walk), and the next call re-walks rather than
    repeating this verdict — so it is one-shot for the same reason and must be
    believed on sight for the same reason.

    MUTATION: leaving ``contradiction`` off the unconfirmed return → THIS TEST
    GOES RED.
    """
    _seed(file_engine, audit, 10)
    monkeypatch.setattr("ne.audit.anchor.read_head_or_none", lambda _conn: (10, "e" * 64))

    result = AnchoredChainVerifier(file_engine, clock=FakeClock()).full_verify()

    assert result.valid is False
    assert result.chain is not None and result.chain.valid is True
    assert result.contradiction is True


def test_an_ABSENT_baseline_head_is_also_a_contradiction(
    file_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third path, on the same criterion, and it was the one left off.

    ``_full_verify``'s ``candidate is None`` branch: the ``ne_audit_head`` row
    was ABSENT when the baseline snapshot was taken and the walk that ran
    afterwards found one and PASSED.  So the ledger is internally consistent and
    this verifier refuses anyway — the exact shape
    ``AnchoredVerification.contradiction`` documents.  A legitimate append never
    removes that row, and ``verify_chain`` NAMES an absent head, so reaching
    here means the row went away and came back inside one method: a restore, a
    re-seed, or a deletion being covered up.

    AND IT IS ONE-SHOT, proved below rather than argued: the anchor is cleared,
    and the next full verify — whose snapshot and walk both see one head —
    installs one and passes.  Without the flag a consumer requiring
    ``BREAK_CONFIRMATIONS`` consecutive disagreements could never see this
    verdict at all, which is the blind spot the flag exists to close.

    MUTATION: leaving ``contradiction`` off the absent-baseline return → THIS
    TEST GOES RED.
    """
    _seed(file_engine, audit, 10)
    monkeypatch.setattr("ne.audit.anchor.read_head_or_none", lambda _conn: None)

    verifier = AnchoredChainVerifier(file_engine, clock=FakeClock())
    result = verifier.full_verify()

    assert result.valid is False
    assert result.chain is not None and result.chain.valid is True
    assert result.contradiction is True
    assert verifier.anchor is None

    # ONE-SHOT: with the head readable again the very next call passes, so a
    # consumer counting consecutive disagreements would be counting to three on
    # a verdict that appears exactly once.
    monkeypatch.undo()
    again = verifier.full_verify()
    assert again.valid is True
    assert again.contradiction is False


def test_an_INTACT_chain_never_reports_a_contradiction(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """CONTROL. Nothing routine sets it — not the cold full verify, not the warm
    anchored extension proof, not a quiet ledger with nothing appended.

    A flag that latches a BREAK on sight is only safe while it is unreachable by
    a healthy deployment, and this is what says so.

    MUTATION: defaulting ``contradiction`` to ``True`` → THIS TEST GOES RED.
    """
    _seed(file_engine, audit, 6)
    verifier = _anchored(file_engine)

    cold = verifier.full_verify()
    assert cold.valid is True and cold.contradiction is False

    _seed(file_engine, audit, 3, start=100)
    warm = verifier.verify()
    assert warm.valid is True
    assert warm.basis == "anchored-extension"
    assert warm.contradiction is False

    quiet = verifier.verify()
    assert quiet.valid is True and quiet.contradiction is False
