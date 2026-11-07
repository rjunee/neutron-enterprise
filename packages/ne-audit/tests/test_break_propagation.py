# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""BREAK PROPAGATION — a walk that disproves the chain withdraws the blessing.

THE CLASS OF DEFECT THIS FILE EXISTS TO CLOSE, stated once.  Five review rounds
on the anchored verifier found the SAME shape five times: a real break is
DETECTED somewhere and fails to PROPAGATE to something that needed it.  The
sharpest instance was server-side and is the one pinned here — a windowed
``verify_chain(start=…, end=…)`` on the audit page runs a REAL walk, can
legitimately prove the chain broken BELOW the anchor (the one class the anchored
path structurally cannot see), and used to return that verdict and tell the
process's shared :class:`~ne.audit.anchor.AnchoredChainVerifier` nothing.  The
trust root stayed blessed and the next unfiltered poll served an
``anchored-extension`` PASS over a chain a walk in the same process had just
disproved.

THE FIX IS NOT A CALL AT THAT LINE.  :func:`ne.audit.verify.verify_chain` is the
ONE function every walk in the process goes through, so the rule lives there:
any walk returning ``valid is False`` reports the break to every registered
observer, and the verifier's response is to drop the anchor.  The last test in
this file is the guardian that keeps that "ONE function" claim true.

WHAT IS DELIBERATELY *NOT* GRANTED, and it is the whole asymmetry: an observer
is never told about a PASSING walk.  A windowed proof may MAKE a finding (it is
exactly as strong as the full walk about the rows it read) and may never
WITHDRAW one (it is blind to every row it did not).  That is the same ordering
the console's ``BASIS_STRENGTH`` encodes, arrived at from the other side.
"""

from __future__ import annotations

import gc
import weakref
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

import ne.audit.anchor as anchor_module
from ne.audit import ensure_audit_schema, verify_chain
from ne.audit.anchor import AnchoredChainVerifier
from ne.audit.store import AuditRecord, append_record, ne_audit_log
from ne.audit.verify import (
    _WALK_OBSERVERS,
    ChainVerification,
    register_chain_walk_observer,
    unregister_chain_walk_observer,
    verified_extension,
)
from ne.schema import AuditEvent

#: One record per day, so a ``from``/``to`` window can select a genuine SUBSET.
_DAY_ZERO = datetime(2026, 3, 1, tzinfo=UTC)


def _at(i: int) -> datetime:
    return _DAY_ZERO + timedelta(days=i)


def _seed(engine: sa.Engine, audit: Callable[..., AuditEvent], n: int) -> None:
    with engine.begin() as conn:
        for i in range(n):
            append_record(conn, audit(event_id=f"e{i}", occurred_at=_at(i)))


def _anchored(engine: sa.Engine) -> AnchoredChainVerifier:
    """A verifier holding a blessing from a real full verify."""
    verifier = AnchoredChainVerifier(engine)
    blessing = verifier.full_verify()
    assert blessing.valid and blessing.basis == "full-chain"
    assert verifier.anchor is not None
    return verifier


def _tamper_payload(engine: sa.Engine, seq: int) -> None:
    """Edit a row's hash-covered payload WITHOUT recomputing its ``record_hash``.

    The unpropagated prefix tamper: the one class the anchored extension proof
    provably misses and a real walk — full OR windowed — provably catches.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
        row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == seq)).one()
        payload = dict(row.payload)
        payload["actor"] = "attacker"
        conn.execute(ne_audit_log.update().where(ne_audit_log.c.seq == seq).values(payload=payload))


@pytest.fixture
def second_engine(tmp_path: Path) -> Iterator[sa.Engine]:
    """A SECOND, unrelated audit database in the same process."""
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'other-audit.db'}")
    ensure_audit_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# THE DEFECT: a windowed walk proves the break, the anchor stays blessed.
# ---------------------------------------------------------------------------


def test_a_WINDOWED_walk_that_proves_a_break_WITHDRAWS_the_anchor(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """The instance this file was written for, end to end in one process.

    The window covers ONE day and the tamper is BELOW the anchor, so this is
    precisely the pairing the old code got wrong: the strongest instrument in
    the process (a real walk) proves the ledger broken while the cheapest one
    (the extension proof) cannot see it, and the answer served to the next
    caller used to be the cheap one's.

    MUTATION: delete the ``_report_chain_walk`` call from ``verify_chain``'s
    bounded branch (or make ``observe_chain_walk`` return early) → RED at
    ``verifier.anchor is None``, and RED again at ``basis == "full-chain"``.
    """
    _seed(file_engine, audit, 6)
    verifier = _anchored(file_engine)
    blessed = verifier.anchor
    assert blessed is not None and blessed.seq == 6
    # The window really is a WINDOW: one row of six, so the verdict below is a
    # bounded instrument's, not a full walk wearing a window's name.
    assert verify_chain(file_engine, start=_at(0), end=_at(0)).checked == 1

    # The extension proof cannot see a prefix tamper — that is its documented
    # scope limit, and it is what makes the windowed walk's finding load-bearing.
    _tamper_payload(file_engine, 1)
    missed = verifier.verify()
    assert missed.valid is True
    assert missed.basis == "anchored-extension"
    assert verifier.anchor is not None

    windowed = verify_chain(file_engine, start=_at(0), end=_at(0))
    assert windowed.valid is False
    assert windowed.first_break_seq == 1

    # THE PROPAGATION. The blessing is gone, so the next request-path call
    # cannot answer off it.
    assert verifier.anchor is None
    after = verifier.verify()
    assert after.valid is False
    assert after.basis == "full-chain"
    assert after.chain is not None and after.chain.first_break_seq == 1


def test_a_windowed_walk_over_an_INTACT_chain_leaves_the_blessing_ALONE(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """THE CONTROL, and it is not a formality: the rule is one-way.

    A passing walk is never reported at all, so a windowed read cannot advance,
    renew or replace the blessing — only withdraw it, and only by disproving the
    chain. An implementation that reported every verdict would pass the test
    above and fail this one.

    MUTATION: drop the ``if cv.valid: return cv`` guard in
    ``_report_chain_walk`` and clear unconditionally in ``observe_chain_walk``
    → RED at ``verifier.anchor is blessed``.
    """
    _seed(file_engine, audit, 6)
    verifier = _anchored(file_engine)
    blessed = verifier.anchor

    assert verify_chain(file_engine, start=_at(0), end=_at(2)).valid is True
    assert verify_chain(file_engine).valid is True

    assert verifier.anchor is blessed  # byte-identical: untouched, not re-blessed
    assert verifier.verify().basis == "anchored-extension"


def test_a_PASSING_walk_is_never_even_REPORTED(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """GUARD 1 OF 2, pinned on its own.

    The one-way rule is enforced in two independent places, and belt-and-braces
    means neither is provable by mutating the other — a mutation that matches
    nothing is a meaningless pass, and so is one the other guard silently
    absorbs. This pins the reporting end: ``_report_chain_walk`` returns before
    it so much as reads the observer list for a passing verdict.

    MUTATION: delete ``if cv.valid: return cv`` from ``_report_chain_walk`` →
    RED (the recorder sees three verdicts instead of one).
    """
    seen: list[ChainVerification] = []

    class Recorder:
        def observe_chain_walk(self, engine: sa.Engine, cv: ChainVerification) -> None:
            seen.append(cv)

    _seed(file_engine, audit, 4)
    recorder = Recorder()
    register_chain_walk_observer(recorder)
    try:
        assert verify_chain(file_engine).valid is True
        assert verify_chain(file_engine, start=_at(0), end=_at(3)).valid is True
        assert seen == []

        _tamper_payload(file_engine, 1)
        assert verify_chain(file_engine).valid is False
        assert [cv.valid for cv in seen] == [False]
    finally:
        unregister_chain_walk_observer(recorder)


def test_the_verifier_REFUSES_a_passing_verdict_even_if_it_is_handed_one(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """GUARD 2 OF 2, pinned on its own.

    ``observe_chain_walk`` is a public method on a shared object; it does not get
    to assume its only caller filtered for it. Handed a PASSING verdict directly
    it must do nothing — a walk that liked the ledger is not evidence this
    anchor should move, and "clears on any report" would make the rule two-way
    the moment guard 1 changed.

    MUTATION: ``if cv.valid or engine is not self._engine`` →
    ``if engine is not self._engine`` in ``observe_chain_walk`` → RED.
    """
    _seed(file_engine, audit, 4)
    verifier = _anchored(file_engine)
    blessed = verifier.anchor

    verifier.observe_chain_walk(
        file_engine,
        ChainVerification(
            valid=True,
            checked=4,
            first_break_seq=None,
            first_break_event_id=None,
            reason=None,
            detail="chain intact: 4 record(s) verified",
        ),
    )

    assert verifier.anchor is blessed
    assert verifier.verify().basis == "anchored-extension"


def test_an_EMPTY_window_cannot_manufacture_a_break_on_an_intact_chain(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """THE DENIAL-OF-SERVICE QUESTION, answered by execution rather than by
    reading ``_verify_bounded``.

    Clearing the anchor on any invalid walk would be dangerous if a caller could
    PROVOKE an invalid walk with parameters. It cannot: the ``lo is None`` branch
    of ``_verify_bounded`` fails only on a missing head row, a GLOBAL density
    break, or the beyond-head epilogue — every one of them a whole-chain fact
    about a genuinely broken ledger, none of them reachable by choosing a
    ``from``/``to``. A window selecting no rows, a window entirely before the
    chain, and a window entirely after it all answer VALID here.

    MUTATION: none needed — this test is the counterexample hunt itself. It goes
    RED if ``_verify_bounded`` ever starts treating "selected nothing" as a
    break, which would turn the propagation rule into a remote anchor-flush.
    """
    _seed(file_engine, audit, 6)
    verifier = _anchored(file_engine)
    blessed = verifier.anchor

    before_everything = verify_chain(file_engine, start=_at(-1000), end=_at(-900))
    after_everything = verify_chain(file_engine, start=_at(900), end=_at(1000))
    inverted_by_selection = verify_chain(file_engine, start=_at(900), end=None)

    for cv in (before_everything, after_everything, inverted_by_selection):
        assert cv.valid is True, cv.detail
        assert cv.checked == 0

    assert verifier.anchor is blessed
    assert verifier.verify().basis == "anchored-extension"


def test_an_empty_window_over_a_BROKEN_chain_still_withdraws_the_anchor(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """The other half of the DoS answer: the empty-window branch is not inert.

    A gap below the head is a whole-chain fact, and the global density pre-check
    sees it even when the window selects nothing. So "windows cannot manufacture
    a break" is not "windows are harmless" — a caller that asks about an empty
    window of a broken ledger still gets the truth, and the anchor still goes.
    """
    _seed(file_engine, audit, 6)
    verifier = _anchored(file_engine)
    with file_engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_delete")
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq == 3))

    empty = verify_chain(file_engine, start=_at(900), end=_at(1000))
    assert empty.valid is False
    assert empty.checked == 0
    assert verifier.anchor is None


# ---------------------------------------------------------------------------
# EVERY WALKER, not every CALL SITE.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "walk",
    [
        pytest.param(lambda eng: verify_chain(eng), id="full-chain-engine"),
        pytest.param(
            lambda eng: verify_chain(eng, start=_at(0), end=_at(5)),
            id="windowed-engine",
        ),
    ],
)
def test_every_ENGINE_shaped_walk_propagates(
    file_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    walk: Callable[[sa.Engine], ChainVerification],
) -> None:
    """Both branches of ``verify_chain``'s engine arm report their breaks.

    The two are parametrised rather than asserted together because a coarse
    mutation that neuters both at once masks a per-branch gap — the lesson the
    ``_beyond_head_break`` call sites already learned in this repo.
    """
    _seed(file_engine, audit, 6)
    verifier = _anchored(file_engine)
    _tamper_payload(file_engine, 2)

    assert walk(file_engine).valid is False
    assert verifier.anchor is None


@pytest.mark.parametrize(
    "walk",
    [
        pytest.param(lambda conn: verify_chain(conn), id="full-chain-connection"),
        pytest.param(
            lambda conn: verify_chain(conn, start=_at(0), end=_at(5)),
            id="windowed-connection",
        ),
    ],
)
def test_every_CONNECTION_shaped_walk_propagates(
    file_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    walk: Callable[[sa.Connection], ChainVerification],
) -> None:
    """A caller holding its own connection (``/trace``'s index read does) is
    still a walker, and ``conn.engine`` is what identifies the chain it walked.

    MUTATION: report only on the engine arm of ``verify_chain`` → RED here and
    GREEN above, which is exactly the per-branch gap the split is for.
    """
    _seed(file_engine, audit, 6)
    verifier = _anchored(file_engine)
    _tamper_payload(file_engine, 2)

    with file_engine.connect() as conn:
        assert walk(conn).valid is False
    assert verifier.anchor is None


def test_a_break_in_ANOTHER_database_does_not_withdraw_this_anchor(
    file_engine: sa.Engine, second_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """The rule is per-CHAIN, not per-process.

    The observer list is global (that is what makes the rule total), so the
    engine has to be part of the report or one ledger's break would flush an
    unrelated ledger's blessing — a fail-closed direction, but a false one, and
    on a shared box it would be permanent.

    MUTATION: drop the ``engine is not self._engine`` guard in
    ``observe_chain_walk`` → RED.
    """
    _seed(file_engine, audit, 6)
    _seed(second_engine, audit, 6)
    verifier = _anchored(file_engine)
    blessed = verifier.anchor

    _tamper_payload(second_engine, 1)
    assert verify_chain(second_engine).valid is False

    assert verifier.anchor is blessed
    assert verifier.verify().basis == "anchored-extension"


# ---------------------------------------------------------------------------
# THE INSTALL RACE — a clear that an in-flight walk would have undone.
# ---------------------------------------------------------------------------


def test_a_break_seen_DURING_a_full_verify_refuses_to_install_its_anchor(
    file_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Withdrawing the blessing is worth nothing if a walk already in flight can
    hand it straight back.

    ``_full_verify`` runs UNLOCKED, so a walk that started before the break was
    observed — it read an earlier state, or a replica that had not caught up —
    would otherwise install a fresh blessing a moment AFTER the break and the
    finding would be gone again. This is the same defect class one layer down,
    so it is closed the same way: an anchor is installed only if nothing
    disproved the chain while its blessing walk was in flight.

    The interleave is forced by observing a break from inside the walk itself
    (a second engine's verdict reported against THIS engine is exactly what a
    concurrent windowed request would have delivered).

    MUTATION: drop the ``raced_a_break`` check in ``_full_verify`` → RED at
    ``verifier.anchor is None``.
    """
    _seed(file_engine, audit, 6)
    verifier = AnchoredChainVerifier(file_engine)
    broken = ChainVerification(
        valid=False,
        checked=1,
        first_break_seq=1,
        first_break_event_id="e0",
        reason="record tampered",
        detail="chain broken at seq 1 (event_id=e0): record tampered",
    )

    def _walk_then_break(*args: object, **kwargs: object) -> ChainVerification:
        cv = verify_chain(*args, **kwargs)  # type: ignore[arg-type]
        verifier.observe_chain_walk(file_engine, broken)
        return cv

    monkeypatch.setattr(anchor_module, "verify_chain", _walk_then_break)
    result = verifier.full_verify()
    monkeypatch.undo()

    assert result.valid is False
    assert result.basis == "full-chain"
    assert result.contradiction is True  # one-shot: a consumer may believe it once
    assert result.chain is not None and result.chain.valid is True
    assert verifier.anchor is None

    # AND IT DOES NOT WEDGE: the next full verify snapshots the bumped counter
    # and installs normally.
    recovered = verifier.full_verify()
    assert recovered.valid is True
    assert verifier.anchor is not None


def test_an_IN_FLIGHT_extension_proof_does_not_RETURN_a_pass_after_a_break(
    file_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same defect class as the test above, on the OTHER path — and it is the
    path an auditor actually reads.

    ``verify`` snapshots the anchor and then runs ``verified_extension``
    UNLOCKED.  While it runs, a windowed or full walk elsewhere in this process
    enters ``observe_chain_walk``, proves a tamper BELOW the anchor — the one
    class the extension proof structurally cannot see — and clears ``_anchor``.
    The install was already guarded (an identity CAS), and it correctly declined.
    The RETURN was not: the call completed against its stale snapshot and handed
    back ``valid=True, basis="anchored-extension"``.  A PASSING AUDIT VERDICT
    EMITTED AFTER THIS PROCESS HAD ALREADY DISPROVED THE CHAIN.

    Closed the way ``_full_verify`` closes its half — the same ``_breaks_seen``
    counter, snapshotted in the SAME lock acquisition as the anchor read (so a
    break landing between those two reads cannot be missed either) and
    re-checked under the lock at return time.  No lock spans a DB call: the
    snapshot and the re-check touch memory only, and the fallback walk runs
    outside both.

    MUTATION: in ``anchor.py``, ``if raced_a_break:`` → ``if False and
    raced_a_break:`` (1 match) → RED at ``raced.valid is False``.
    MUTATION: ``raced_a_break = self._breaks_seen != breaks_at_start`` →
    ``= False`` (1 match) → RED at the same assertion.
    """
    _seed(file_engine, audit, 6)
    verifier = _anchored(file_engine)
    assert verifier.anchor is not None and verifier.anchor.seq == 6

    # A REAL prefix tamper, below the anchor. The extension proof passes over it
    # by construction, which is exactly why the other walk's finding is the only
    # thing standing between this call and a false PASS.
    _tamper_payload(file_engine, 1)

    # CONTROL, and it is the whole discrimination: with nothing racing, this
    # call returns the anchored pass. The fix must change ONLY the raced case.
    missed = verifier.verify()
    assert missed.valid is True
    assert missed.basis == "anchored-extension"
    assert verifier.anchor is not None

    broken = ChainVerification(
        valid=False,
        checked=1,
        first_break_seq=1,
        first_break_event_id="e0",
        reason="record tampered",
        detail="chain broken at seq 1 (event_id=e0): record tampered",
    )
    fired: list[int] = []

    def _extend_then_break(
        engine_or_conn: sa.Engine | sa.Connection,
        *,
        baseline_seq: int,
        baseline_hash: str,
    ) -> tuple[int, str, tuple[AuditRecord, ...]] | None:
        # The REAL function, imported directly, so the monkeypatch below cannot
        # make this recurse.
        proved = verified_extension(
            engine_or_conn, baseline_seq=baseline_seq, baseline_hash=baseline_hash
        )
        # The interleave, forced, and ONE-SHOT — one concurrent walk, not one
        # per extension proof. (`_full_verify` runs its own `verified_extension`
        # to confirm its baseline, and a wrapper that fired there too would be
        # testing a second race instead of this one.)
        if not fired:
            fired.append(1)
            assert proved is not None, "the extension proof must PASS, or there is no defect"
            verifier.observe_chain_walk(file_engine, broken)
        return proved

    monkeypatch.setattr(anchor_module, "verified_extension", _extend_then_break)
    raced = verifier.verify()
    monkeypatch.undo()

    # NOT a pass, and not a bare refusal either: it fell back to the walk that
    # NAMES the break, which is what the unanchored path would have returned.
    assert raced.valid is False
    assert raced.basis == "full-chain"
    assert raced.chain is not None and raced.chain.valid is False
    assert raced.chain.first_break_seq == 1
    assert verifier.anchor is None

    # AND THE REFUSAL IS NOT SPECULATIVE. The very next request-path call, with
    # no monkeypatch and no race, reports the same break off the full path.
    after = verifier.verify()
    assert after.valid is False
    assert after.basis == "full-chain"
    assert after.chain is not None and after.chain.first_break_seq == 1


def test_the_extension_return_guard_does_not_WEDGE_an_intact_chain(
    file_engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE CONTROL for the guard above: it is one-shot, like every other refusal
    in this module.

    A break observed during one extension proof refuses THAT call.  It does not
    poison the verifier: the counter is re-snapshotted on every ``verify``, so
    once a full walk re-blesses a chain that is actually intact, the request
    path returns to ``anchored-extension``.

    MUTATION: make the guard latch (compare against a counter snapshotted in
    ``__init__`` instead of per-call) → RED at the final ``basis`` assertion.
    """
    _seed(file_engine, audit, 6)
    verifier = _anchored(file_engine)
    broken = ChainVerification(
        valid=False,
        checked=1,
        first_break_seq=1,
        first_break_event_id="e0",
        reason="record tampered",
        detail="chain broken at seq 1 (event_id=e0)",
    )
    fired: list[int] = []

    def _extend_then_break(
        engine_or_conn: sa.Engine | sa.Connection,
        *,
        baseline_seq: int,
        baseline_hash: str,
    ) -> tuple[int, str, tuple[AuditRecord, ...]] | None:
        # The REAL function, imported directly, so the monkeypatch below cannot
        # make this recurse.
        proved = verified_extension(
            engine_or_conn, baseline_seq=baseline_seq, baseline_hash=baseline_hash
        )
        if not fired:  # one concurrent walk, not one per extension proof
            fired.append(1)
            verifier.observe_chain_walk(file_engine, broken)
        return proved

    monkeypatch.setattr(anchor_module, "verified_extension", _extend_then_break)
    # The chain is INTACT; only the reported verdict is not. The fallback walk
    # therefore passes and re-blesses, which is the honest answer — a fresh full
    # verify ran on this call and found nothing.
    raced = verifier.verify()
    monkeypatch.undo()
    assert raced.valid is True
    assert raced.basis == "full-chain"

    assert verifier.anchor is not None
    recovered = verifier.verify()
    assert recovered.valid is True
    assert recovered.basis == "anchored-extension"


# ---------------------------------------------------------------------------
# THE REGISTRY ITSELF.
# ---------------------------------------------------------------------------


def test_the_observer_registry_holds_its_observers_WEAKLY(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """Registration happens in ``AnchoredChainVerifier.__init__`` and nothing
    ever unregisters, so holding observers strongly would pin every verifier —
    and through it every engine — for the life of the process. Every test in this
    repo builds its own.
    """
    _seed(file_engine, audit, 3)
    verifier = AnchoredChainVerifier(file_engine)
    ref = weakref.ref(verifier)
    assert verifier in _WALK_OBSERVERS

    del verifier
    gc.collect()
    assert ref() is None


def test_an_observer_that_RAISES_never_turns_a_break_into_an_exception(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """The verdict outranks the notification.

    A walker is in the middle of reporting a break; an observer blowing up would
    turn that into a 500, which the audit page renders as "unavailable" — weaker
    than the accusation it was carrying. So observers are skipped, not allowed
    to propagate.
    """

    class Exploding:
        def observe_chain_walk(self, engine: sa.Engine, cv: ChainVerification) -> None:
            raise RuntimeError("boom")

    _seed(file_engine, audit, 4)
    verifier = _anchored(file_engine)
    exploding = Exploding()
    register_chain_walk_observer(exploding)
    try:
        _tamper_payload(file_engine, 1)
        cv = verify_chain(file_engine)
        assert cv.valid is False
        assert cv.first_break_seq == 1
        # …and the well-behaved observer beside it still got told.
        assert verifier.anchor is None
    finally:
        unregister_chain_walk_observer(exploding)


def test_no_caller_bypasses_the_verify_chain_choke_point() -> None:
    """THE GUARDIAN. The rule is only total while ``verify_chain`` is the only
    way to run a walk.

    ``_verify_full`` and ``_verify_bounded`` do the actual walking and do NOT
    report. A future caller reaching past ``verify_chain`` to either of them
    would be a walk whose breaks go nowhere — the sixth instance of the defect
    this whole file exists to make unreachable — and it would not fail any other
    test in this repo. So it fails this one, by source scan, in the only place
    that can see it before it is written.
    """
    root = Path(__file__).resolve().parents[3]
    offenders: list[str] = []
    for path in sorted(root.glob("packages/*/src/**/*.py")) + sorted(
        root.glob("tools/*/src/**/*.py")
    ):
        if path.name == "verify.py" and path.parent.name == "audit":
            continue
        text = path.read_text(encoding="utf-8")
        for symbol in ("_verify_full(", "_verify_bounded("):
            if symbol in text:
                offenders.append(f"{path.relative_to(root)}: {symbol}")
    assert offenders == [], (
        "these call a raw chain walk directly instead of ne.audit.verify.verify_chain, "
        "so any break they find is reported to nobody: " + "; ".join(offenders)
    )
