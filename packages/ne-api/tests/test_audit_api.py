# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Audit read surface — ops-gated, payload-authoritative, server-computed verdict.

Proves ops_roles restriction (empty ⇒ nobody, audited), the actor filter over the
PAYLOAD actor with a per-page ChainVerificationView, that a direct SQL UPDATE of a
payload flips verification.valid false (served verbatim), malformed from/limit →
422, and replay: no selector → 422, unknown → empty 200, ops-gated.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from ne.audit import AnchoredChainVerifier
from ne.audit.anchor import CHAIN_ANCHOR_MAX_AGE_S
from ne.audit.store import compute_record_hash
from ne.events import new_audit_event
from ne.schema import AuditOutcome

from .conftest import AppBuilder, get_state, run


def _seed(h: Any, n: int) -> None:
    state = get_state(h.app)
    actors = ["user:ada", "user:bob"]

    async def _append() -> None:
        for i in range(n):
            await state.sink.append(
                new_audit_event(
                    action="thing.happened",
                    resource="r",
                    outcome=AuditOutcome.success,
                    actor=actors[i % len(actors)],
                )
            )

    run(_append())


def test_events_requires_ops_role(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(h, 2)

    async def _t() -> None:
        async with h.client() as client:
            # non-ops → 403
            r = await client.get("/api/v1/audit/events", headers=h.bearer("u", groups=["x"]))
            assert r.status_code == 403

    run(_t())


def test_events_empty_ops_roles_fails_closed_and_audits(make_app: AppBuilder) -> None:
    # empty ops_roles ⇒ 403 for everyone, and the attempt is audited.
    h = make_app()  # ops_roles default ()
    _seed(h, 1)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/audit/events", headers=h.bearer("rich", groups=["ops", "admins"])
            )
            assert r.status_code == 403

    run(_t())

    engine = sa.create_engine(h.audit_url)
    try:
        from ne.audit import list_records

        rows = list_records(engine, action="api.audit.unauthorized")
    finally:
        engine.dispose()
    assert len(rows) >= 1
    assert rows[0].actor == "rich"


def test_approval_decision_lookup_finds_exact_terminal_event_beyond_newest_page(
    make_app: AppBuilder,
) -> None:
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)

    async def _t() -> None:
        await state.sink.append(
            new_audit_event(
                action="approval.granted",
                resource="tool:crm.create_note",
                outcome=AuditOutcome.success,
                actor="user:approver",
                provenance={"approval_id": "req-1", "approver": "user:approver"},
            )
        )
        # A terminal near-match plus >25 newer same-id joins cannot collide or
        # retarget the receipt away from the decision event above.
        await state.sink.append(
            new_audit_event(
                action="approval.rejected",
                resource="tool:crm.create_note",
                outcome=AuditOutcome.denied,
                actor="user:approver",
                provenance={"approval_id": "req-10", "approver": "user:approver"},
            )
        )
        for index in range(30):
            await state.sink.append(
                new_audit_event(
                    action="tool.execute" if index % 2 == 0 else "approval.payload_read",
                    resource="tool:crm.create_note",
                    outcome=AuditOutcome.success,
                    actor="system",
                    provenance={"approval_id": "req-1"},
                )
            )

        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            response = await client.get("/api/v1/audit/approval-decisions/req-1", headers=headers)
            assert response.status_code == 200
            body = response.json()
            assert body["action"] == "approval.granted"
            assert body["payload"]["provenance"]["approval_id"] == "req-1"

    run(_t())


def test_approval_decision_lookup_serves_explicit_miss_and_uses_ops_gate(
    make_app: AppBuilder,
) -> None:
    h = make_app(ops_roles=("ops",))

    async def _t() -> None:
        async with h.client() as client:
            ops = h.bearer("user:ops", groups=["ops"])
            missing = await client.get("/api/v1/audit/approval-decisions/req-missing", headers=ops)
            assert missing.status_code == 404
            assert missing.json() == {"detail": "approval decision audit entry not found"}

            refused = await client.get(
                "/api/v1/audit/approval-decisions/req-missing",
                headers=h.bearer("user:reader", groups=["readers"]),
            )
            assert refused.status_code == 403

    run(_t())


def test_events_actor_filter_and_verdict(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(h, 6)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get("/api/v1/audit/events?actor=user:ada", headers=headers)
            assert r.status_code == 200
            body = r.json()
            assert set(body) == {"events", "verification", "anchor"}
            # An UNANCHORED read never fabricates an anchor verdict.
            assert body["anchor"] is None
            events = body["events"]
            assert events  # some ada rows
            assert all(e["actor"] == "user:ada" for e in events)
            # newest-first (seq DESC)
            seqs = [e["seq"] for e in events]
            assert seqs == sorted(seqs, reverse=True)
            v = body["verification"]
            assert set(v) == {
                "valid",
                "checked",
                "first_break_seq",
                "first_break_event_id",
                "reason",
                "detail",
                "basis",
                "contradiction",
            }
            assert v["valid"] is True

    run(_t())


def test_events_tampered_row_flips_verdict(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(h, 3)

    # Tamper: drop the append-only UPDATE trigger, then mutate a payload (actor).
    engine = sa.create_engine(h.audit_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
            row = conn.execute(sa.text("SELECT payload FROM ne_audit_log WHERE seq = 1")).first()
            assert row is not None
            payload = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
            payload["actor"] = "forged"
            conn.execute(
                sa.text("UPDATE ne_audit_log SET payload = :p WHERE seq = 1"),
                {"p": json.dumps(payload)},
            )
    finally:
        engine.dispose()

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get("/api/v1/audit/events", headers=headers)
            assert r.status_code == 200
            v = r.json()["verification"]
            assert v["valid"] is False
            assert v["first_break_seq"] == 1
            assert isinstance(v["detail"], str) and v["detail"]

    run(_t())


def test_events_malformed_from_is_422(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(h, 1)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get("/api/v1/audit/events?from=not-a-date", headers=headers)
            assert r.status_code == 422
            assert isinstance(r.json()["detail"], str)

            naive = await client.get(
                "/api/v1/audit/events", headers=headers, params={"from": "2026-07-20T09:00"}
            )
            assert naive.status_code == 422
            assert naive.json()["detail"] == (
                "from: timezone offset required; local wall-clock text is ambiguous"
            )

            reversed_window = await client.get(
                "/api/v1/audit/events",
                headers=headers,
                params={
                    "from": "2026-07-20T10:00:00-04:00",
                    "to": "2026-07-20T09:00:00-04:00",
                },
            )
            assert reversed_window.status_code == 422
            assert reversed_window.json()["detail"] == "from: must not be later than to"

    run(_t())


def test_events_offset_bounds_select_the_intended_instants(make_app: AppBuilder) -> None:
    """Non-UTC wall-clock bounds with explicit offsets select their intended
    instants, including rows exactly on both inclusive boundaries."""
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)

    async def _append() -> None:
        for event_id, occurred_at in (
            ("before", datetime(2026, 7, 20, 13, 14, tzinfo=UTC)),
            ("start", datetime(2026, 7, 20, 13, 15, tzinfo=UTC)),
            ("inside", datetime(2026, 7, 20, 14, 0, tzinfo=UTC)),
            ("end", datetime(2026, 7, 20, 14, 45, tzinfo=UTC)),
            ("after", datetime(2026, 7, 20, 14, 46, tzinfo=UTC)),
        ):
            await state.sink.append(
                new_audit_event(
                    event_id=event_id,
                    action="thing.happened",
                    resource="r",
                    outcome=AuditOutcome.success,
                    actor="user:ada",
                    occurred_at=occurred_at,
                )
            )

    run(_append())

    async def _t() -> None:
        async with h.client() as client:
            response = await client.get(
                "/api/v1/audit/events",
                headers=h.bearer("user:ops", groups=["ops"]),
                params={
                    "from": "2026-07-20T09:15:00-04:00",
                    "to": "2026-07-20T10:45:00-04:00",
                },
            )
            assert response.status_code == 200
            assert [event["event_id"] for event in response.json()["events"]] == [
                "end",
                "inside",
                "start",
            ]

    run(_t())


def test_events_limit_bounds(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(h, 1)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r0 = await client.get("/api/v1/audit/events?limit=0", headers=headers)
            assert r0.status_code == 422
            r501 = await client.get("/api/v1/audit/events?limit=501", headers=headers)
            assert r501.status_code == 422

    run(_t())


def test_replay_no_selector_422(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get("/api/v1/audit/replay", headers=headers)
            assert r.status_code == 422
            assert isinstance(r.json()["detail"], str)

    run(_t())


def test_replay_unknown_trace_returns_empty_200(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(h, 1)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get("/api/v1/audit/replay?trace_id=nope", headers=headers)
            assert r.status_code == 200
            assert r.json() == {"events": [], "config_hashes": [], "context_ids": []}

    run(_t())


def test_replay_requires_ops_role(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/audit/replay?trace_id=x", headers=h.bearer("u", groups=["nope"])
            )
            assert r.status_code == 403

    run(_t())


# ---------------------------------------------------------------------------
# anchor_seq — the ?seq= deep link's anchored read
# ---------------------------------------------------------------------------
def test_events_anchored_read_returns_the_page_containing_the_entry(
    make_app: AppBuilder,
) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(h, 12)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            # The defect this read fixes: seq 4 is older than a 5-row newest
            # page, so the unanchored read cannot contain it...
            r0 = await client.get("/api/v1/audit/events?limit=5", headers=headers)
            assert 4 not in [e["seq"] for e in r0.json()["events"]]
            # ...and the anchored read returns the page it sits in, mid-window.
            r = await client.get("/api/v1/audit/events?limit=5&anchor_seq=4", headers=headers)
            assert r.status_code == 200
            body = r.json()
            assert set(body) == {"events", "verification", "anchor"}
            assert [e["seq"] for e in body["events"]] == [6, 5, 4, 3, 2]
            assert body["anchor"] == {"seq": 4, "status": "found", "head_seq": 12}

    run(_t())


def test_events_anchor_past_the_head_is_not_found_and_names_the_head(
    make_app: AppBuilder,
) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(h, 6)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get("/api/v1/audit/events?limit=5&anchor_seq=9999", headers=headers)
            assert r.status_code == 200
            body = r.json()
            # The SERVER states the miss and where the log actually ends (C7:
            # the console renders this verdict, it never infers one from rows).
            assert body["anchor"] == {"seq": 9999, "status": "not_found", "head_seq": 6}
            # The screen stays useful: the nearest window still comes back.
            assert [e["seq"] for e in body["events"]] == [6, 5, 4, 3, 2]

    run(_t())


def test_events_anchor_excluded_by_filters_is_filtered_out(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(h, 8)  # actors alternate: odd seqs user:ada, even seqs user:bob

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(
                "/api/v1/audit/events?actor=user:ada&limit=5&anchor_seq=4", headers=headers
            )
            assert r.status_code == 200
            body = r.json()
            # Seq 4 exists (it is bob's) — the filter hides it. Saying
            # "not found" here would be a false statement about the chain.
            assert body["anchor"] == {"seq": 4, "status": "filtered_out", "head_seq": 8}
            assert all(e["actor"] == "user:ada" for e in body["events"])

    run(_t())


def test_events_anchored_read_requires_ops_role(make_app: AppBuilder) -> None:
    # The anchored read sits behind the IDENTICAL chokepoint as the unanchored
    # page — anchoring must not become a side-door onto the chain.
    h = make_app(ops_roles=("ops",))
    _seed(h, 3)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/audit/events?anchor_seq=2", headers=h.bearer("u", groups=["x"])
            )
            assert r.status_code == 403

    run(_t())


def test_events_anchor_seq_zero_is_422(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(h, 1)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get("/api/v1/audit/events?anchor_seq=0", headers=headers)
            assert r.status_code == 422

    run(_t())


# ---------------------------------------------------------------------------
# NE-SELECT-LATENCY — the UNFILTERED page verifies by anchored extension.
#
# The unfiltered read used to run an unbounded ``verify_chain`` on every request:
# MEASURED 35.44 s at 357,821 rows on the live demo (and 5.567 s at 360,000 rows
# on a local SQLite chain), which is what parked the console on "Loading the
# audit log". It now proves, in O(rows appended since the last full verify), that
# the chain still extends a head a real ``verify_chain`` blessed.
#
# ⚠ THAT IS A DIFFERENT CLAIM, AND THESE TESTS PIN BOTH HALVES OF IT — the class
# it still catches on the request itself AND the ONE class it trades away. The
# miss is asserted DELIBERATELY (paired with an assertion that ``verify_chain``
# on the same database DOES catch it) so the docstrings can never quietly drift
# away from the behaviour. Read ``ne.audit.anchor``'s module docstring for the
# full boundary; these tests prove it through the actual HTTP surface.
# ---------------------------------------------------------------------------


class _FakeClock:
    """A monotonic clock a test can advance by hand (mirrors test_anchored_verify)."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tamper_payload_in_place(audit_url: str, seq: int) -> None:
    """Edit one row's hash-covered payload WITHOUT recomputing its ``record_hash``.

    This is the "unpropagated modification" of ``ne.audit.anchor``'s MISSED list:
    it leaves the ledger internally INCONSISTENT at ``seq``, so ``verify_chain``
    names it — and an extension proof that never re-reads ``seq`` cannot.
    """
    engine = sa.create_engine(audit_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
            row = conn.execute(
                sa.text("SELECT payload FROM ne_audit_log WHERE seq = :s"), {"s": seq}
            ).first()
            assert row is not None
            payload = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
            payload["actor"] = "forged"
            conn.execute(
                sa.text("UPDATE ne_audit_log SET payload = :p WHERE seq = :s"),
                {"p": json.dumps(payload), "s": seq},
            )
    finally:
        engine.dispose()


def _full_verify_says(audit_url: str) -> tuple[bool, int | None]:
    """``verify_chain``'s own verdict on the database — the control for every miss."""
    from ne.audit import verify_chain

    engine = sa.create_engine(audit_url)
    try:
        cv = verify_chain(engine)
        return cv.valid, cv.first_break_seq
    finally:
        engine.dispose()


async def _append(h: Any, n: int) -> None:
    """``_seed``'s body, awaitable — ``_seed`` wraps ``asyncio.run`` and cannot be
    called from inside an already-running loop."""
    state = get_state(h.app)
    actors = ["user:ada", "user:bob"]
    for i in range(n):
        await state.sink.append(
            new_audit_event(
                action="thing.happened",
                resource="r",
                outcome=AuditOutcome.success,
                actor=actors[i % len(actors)],
            )
        )


async def _page(h: Any, client: Any, **params: Any) -> dict[str, Any]:
    headers = h.bearer("user:ops", groups=["ops"])
    r = await client.get("/api/v1/audit/events", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_app_composes_one_chain_verifier_shared_with_model_selection(
    make_app: AppBuilder,
) -> None:
    """ONE :class:`ne.audit.AnchoredChainVerifier` per process per engine.

    The verifier's own contract: a second instance would carry a second,
    independently aged anchor and pay a second O(chain) full verify for no gain.
    The audit page and the selection authority verify the SAME ledger, so they
    must share the SAME object — and the background loop that re-blesses it
    (``ModelSelectionService.run_chain_anchor_refresher``) hangs off the
    selection service, so a split would leave the audit page's anchor with no
    refresher at all.

    MUTATION: constructing the verifier inside ``ModelSelectionService`` again
    (dropping ``chain_verifier=chain_verifier`` from either construction in
    ``ne.api.app``) → THIS TEST GOES RED.
    """
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)
    assert state.chain_verifier is not None
    assert state.model_selection is not None
    assert state.chain_verifier is state.model_selection._chain_verifier


def test_events_unfiltered_page_verifies_the_WHOLE_chain_by_extension(
    make_app: AppBuilder,
) -> None:
    """The verdict still covers the whole ledger; only the NEW rows are re-hashed.

    This is the guarantee that must not be weakened: ``checked`` is the number of
    records the verdict COVERS (all of them), never the 25 the page returns and
    never the handful re-hashed on this call. ``detail`` carries the split.

    MUTATION: reporting ``av.checked`` (the extension length) as the wire
    ``checked`` → the second assertion drops from 7 to 2 and THIS TEST GOES RED.
    """
    h = make_app(ops_roles=("ops",))
    _seed(h, 5)

    async def _t() -> None:
        async with h.client() as client:
            # Cold process: no anchor, so this call pays the inline full verify
            # and blesses the head it walked. Loud, slow, and the same verdict.
            cold = (await _page(h, client))["verification"]
            assert cold["valid"] is True
            assert cold["checked"] == 5
            assert cold["detail"] == "chain intact: 5 record(s) verified"

            await _append(h, 2)
            warm = (await _page(h, client))["verification"]
            assert warm["valid"] is True
            # The verdict covers all SEVEN records...
            assert warm["checked"] == 7
            # ...while naming, in words, that only the two new ones were re-read
            # here and that the prefix was verified in full earlier.
            assert "chain extends verified anchor" in warm["detail"]
            assert "2 record(s) verified forward from seq 5 to head seq 7" in warm["detail"]
            assert "is NOT re-checked by this call" in warm["detail"]

    run(_t())


def test_events_tamper_ABOVE_the_anchor_flips_the_verdict_on_the_request_itself(
    make_app: AppBuilder,
) -> None:
    """A row tampered in the EXTENSION is caught by the extension proof, now.

    No clock advance, no background verify: the rows above the anchor are
    re-hashed on every call, so this is the class the anchored path keeps at full
    strength — and the break is still NAMED, because the failed extension proof
    falls back to a ``verify_chain`` that names it.

    MUTATION: dropping the ``record_hash`` recomputation from
    ``ne.audit.verify._verified_extension`` → THIS TEST GOES RED.
    """
    h = make_app(ops_roles=("ops",))
    _seed(h, 4)

    async def _t() -> None:
        async with h.client() as client:
            blessed = (await _page(h, client))["verification"]
            assert blessed["valid"] is True  # anchor now at seq 4

            await _append(h, 3)  # seqs 5..7 — ABOVE the anchor
            _tamper_payload_in_place(h.audit_url, 6)

            v = (await _page(h, client))["verification"]
            assert v["valid"] is False
            assert v["first_break_seq"] == 6
            assert v["reason"] == "record tampered"
            assert v["first_break_event_id"] is not None

    run(_t())


def test_events_tamper_BELOW_the_anchor_is_MISSED_while_the_blessing_is_FRESH(
    make_app: AppBuilder,
) -> None:
    """⚠ THE HONEST MISS — asserted on purpose, never papered over.

    An UNPROPAGATED edit at ``seq <= anchor`` (payload changed, ``record_hash``
    left alone) leaves the ledger internally inconsistent BELOW the anchor. The
    extension proof never re-reads those rows, so the page reports VALID — while
    ``verify_chain`` on the very same database reports the break at seq 1. Both
    halves are asserted, because the pairing is what makes this a bounded scope
    limit rather than a silent hole: this is the ONE class the anchor trades
    away, and ``ne.audit.CHAIN_ANCHOR_MAX_AGE_S`` is what bounds it (see the two
    tests below, which each close it by a different route).

    If a future change makes the request path catch this, DELETE this test rather
    than weakening it — but do not "fix" it by asserting the opposite without
    making the code do it.
    """
    h = make_app(ops_roles=("ops",))
    _seed(h, 5)

    async def _t() -> None:
        async with h.client() as client:
            assert (await _page(h, client))["verification"]["valid"] is True  # anchor at 5

            _tamper_payload_in_place(h.audit_url, 1)  # BELOW the anchor
            await _append(h, 1)  # a real append, so the extension proof has work to do

            v = (await _page(h, client))["verification"]
            # THE MISS.
            assert v["valid"] is True
            assert "chain extends verified anchor" in v["detail"]
            # THE CONTROL — the full walk catches exactly what the page did not.
            assert _full_verify_says(h.audit_url) == (False, 1)

    run(_t())


def test_events_tamper_BELOW_the_anchor_flips_the_verdict_once_the_anchor_AGES_OUT(
    make_app: AppBuilder,
) -> None:
    """The bound on the miss above, enforced in code on the REQUEST path.

    ``CHAIN_ANCHOR_MAX_AGE_S`` is a staleness ceiling on REUSE, not a
    time-to-detection: elapsed time detects nothing on its own, but no read taken
    past the ceiling may be served off the stale blessing. It pays the full
    ``verify_chain`` inline, and the tamper surfaces THERE — named.

    MUTATION: removing the ``age_s > self.max_anchor_age_s`` branch from
    ``AnchoredChainVerifier.verify`` → THIS TEST GOES RED.
    """
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)
    clock = _FakeClock()
    state.chain_verifier = AnchoredChainVerifier(state.audit_engine, clock=clock)
    _seed(h, 5)

    async def _t() -> None:
        async with h.client() as client:
            assert (await _page(h, client))["verification"]["valid"] is True  # anchor at 5
            _tamper_payload_in_place(h.audit_url, 1)
            # Still inside the ceiling: the miss above.
            assert (await _page(h, client))["verification"]["valid"] is True

            clock.advance(CHAIN_ANCHOR_MAX_AGE_S + 1.0)

            v = (await _page(h, client))["verification"]
            assert v["valid"] is False
            assert v["first_break_seq"] == 1
            assert v["reason"] == "record tampered"

    run(_t())


def test_events_tamper_BELOW_the_anchor_flips_the_verdict_when_the_BACKGROUND_verify_runs(
    make_app: AppBuilder,
) -> None:
    """The other route out of the miss — and the one that makes ~300 s a real
    time-to-detection in the API process, because the refresher loop is itself a
    caller.

    This calls exactly what ``ModelSelectionService.run_chain_anchor_refresher``
    calls on its timer. A failing full verify CLEARS the anchor, so the very next
    page read runs its own full verify inline and reports the named break — no
    clock advance needed.

    MUTATION: dropping the ``self._clear()`` from the ``not cv.valid`` branch of
    ``AnchoredChainVerifier._full_verify`` → THIS TEST GOES RED.
    """
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)
    assert state.chain_verifier is not None
    _seed(h, 5)

    async def _t() -> None:
        async with h.client() as client:
            assert (await _page(h, client))["verification"]["valid"] is True
            _tamper_payload_in_place(h.audit_url, 1)
            assert (await _page(h, client))["verification"]["valid"] is True  # the miss

            # What the background loop does on its tick.
            assert state.chain_verifier is not None
            sweep = state.chain_verifier.full_verify()
            assert sweep.valid is False
            assert sweep.basis == "full-chain"
            assert state.chain_verifier.anchor is None  # cleared, fail-closed

            v = (await _page(h, client))["verification"]
            assert v["valid"] is False
            assert v["first_break_seq"] == 1

    run(_t())


def test_events_FILTERED_page_keeps_the_windowed_verify_and_never_consults_the_anchor(
    make_app: AppBuilder,
) -> None:
    """``from``/``to`` is untouched: still ``verify_chain(start, end)``.

    Proved two ways at once. (1) The windowed verify catches a below-the-anchor
    tamper IMMEDIATELY — the exact case the unfiltered read misses — so the
    bounded path demonstrably did not go through the extension proof. (2) A
    windowed read over an INTACT chain leaves the anchor byte-identical, so the
    bounded read neither consulted nor advanced it.

    ⚠ (2) IS ABOUT A PASSING WINDOWED READ ONLY, and it used to be asserted
    across the tamper as well — which pinned the defect rather than the rule.
    A windowed walk that proves the chain BROKEN now withdraws the anchor
    (``ne.audit.verify.register_chain_walk_observer``); that is
    ``test_a_windowed_read_that_proves_a_break_WITHDRAWS_the_shared_anchor``,
    and it is a different claim from "the verdict did not come from the anchor",
    which is what this test is for.

    MUTATION: routing the ``start``/``end`` branch of ``_page_verification``
    through the verifier → THIS TEST GOES RED on the first assertion.
    """
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)
    _seed(h, 5)

    async def _t() -> None:
        async with h.client() as client:
            assert (await _page(h, client))["verification"]["valid"] is True
            assert state.chain_verifier is not None
            anchor_before = state.chain_verifier.anchor
            assert anchor_before is not None

            intact = await _page(
                h,
                client,
                **{"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"},
            )
            assert intact["verification"]["valid"] is True
            assert intact["verification"]["basis"] == "windowed"
            assert state.chain_verifier.anchor is anchor_before

            _tamper_payload_in_place(h.audit_url, 1)

            window = await _page(
                h,
                client,
                **{"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"},
            )
            v = window["verification"]
            assert v["valid"] is False
            assert v["first_break_seq"] == 1

    run(_t())


def test_a_windowed_read_that_proves_a_break_WITHDRAWS_the_shared_anchor(
    make_app: AppBuilder,
) -> None:
    """THE ROUTE-LEVEL END OF ``ne.audit.verify.register_chain_walk_observer``.

    A windowed read runs a REAL walk and can legitimately prove the chain broken
    BELOW the anchor — the exact class the unfiltered anchored path structurally
    cannot see. It used to return that verdict and tell the process's shared
    verifier nothing, so the trust root stayed blessed and the NEXT unfiltered
    poll served an ``anchored-extension`` PASS over a chain a walk in this same
    process had just disproved. Both halves are asserted here, in that order,
    because the second one is the damage and the first one is only the setup.

    ⚠ The unfiltered read BEFORE the windowed one is not scaffolding: it is the
    proof that the anchored path really was answering ``valid`` over the tampered
    ledger, so the flip afterwards can only have come from the windowed read's
    finding reaching the anchor.

    MUTATION: remove the ``_report_chain_walk`` call from ``verify_chain``'s
    bounded branch → RED at ``basis != "anchored-extension"``.
    """
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)
    assert state.chain_verifier is not None
    _seed(h, 5)

    async def _t() -> None:
        async with h.client() as client:
            assert (await _page(h, client))["verification"]["valid"] is True
            assert state.chain_verifier is not None
            assert state.chain_verifier.anchor is not None

            _tamper_payload_in_place(h.audit_url, 1)

            # THE MISS, still a miss: the anchored poll cannot see a prefix
            # tamper and says so by passing.
            missed = (await _page(h, client))["verification"]
            assert missed["valid"] is True
            assert missed["basis"] == "anchored-extension"

            windowed = (
                await _page(
                    h,
                    client,
                    **{"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"},
                )
            )["verification"]
            assert windowed["valid"] is False
            assert windowed["basis"] == "windowed"
            assert windowed["first_break_seq"] == 1

            # THE PROPAGATION, at the route: the blessing is gone…
            assert state.chain_verifier.anchor is None
            # …so the very next UNFILTERED poll cannot serve an anchored pass.
            after = (await _page(h, client))["verification"]
            assert after["valid"] is False
            assert after["basis"] == "full-chain"
            assert after["first_break_seq"] == 1

    run(_t())


def test_a_windowed_read_over_an_INTACT_chain_never_costs_the_next_poll_its_anchor(
    make_app: AppBuilder,
) -> None:
    """The control for the rule above, and the answer to "is this a denial of
    service?" at the layer an ops user actually touches.

    A windowed read is a caller-chosen scope, so if ANY windowed read flushed the
    anchor, an ops key could put an O(chain) walk on every subsequent poll just
    by filtering. It cannot: only a walk that DISPROVES the chain reports
    anything, and no ``from``/``to`` makes an intact ledger answer invalid. Ten
    windowed reads later the next unfiltered poll is still anchored.

    (And the cost if it could: one inline full walk — which this same route
    already hands any ops caller on request via ``?full_verify=true``. The rule
    grants no capability that was not already on the wire.)

    MUTATION: making the withdrawal two-way, which takes BOTH guards — drop
    ``if cv.valid: return cv`` from ``ne.audit.verify._report_chain_walk`` AND
    ``cv.valid or`` from ``AnchoredChainVerifier.observe_chain_walk`` → RED at
    ``basis == "anchored-extension"``.  ⚠ Stated as a pair on purpose: either
    guard ALONE absorbs the other's removal, so no single-point mutation can
    reach this route-level test, and claiming one could would be claiming a
    discrimination it does not have.  Each guard is pinned individually one
    layer down —
    ``test_break_propagation.py::test_a_PASSING_walk_is_never_even_REPORTED``
    and ``::test_the_verifier_REFUSES_a_passing_verdict_even_if_it_is_handed_one``.
    """
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)
    _seed(h, 5)

    async def _t() -> None:
        async with h.client() as client:
            assert (await _page(h, client))["verification"]["valid"] is True
            assert state.chain_verifier is not None
            blessed = state.chain_verifier.anchor
            assert blessed is not None

            for _ in range(10):
                v = (
                    await _page(
                        h,
                        client,
                        **{"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"},
                    )
                )["verification"]
                assert v["valid"] is True
                assert v["basis"] == "windowed"

            assert state.chain_verifier.anchor is blessed
            assert (await _page(h, client))["verification"]["basis"] == "anchored-extension"

    run(_t())


def test_events_without_a_chain_verifier_still_runs_the_full_verify(
    make_app: AppBuilder,
) -> None:
    """A missing verifier must cost LATENCY, never the guarantee.

    ``ApiState.chain_verifier`` is trailing-defaulted ``None`` so no existing
    construction breaks; that arm falls back to the unbounded ``verify_chain``.
    It is the slow path and it is never a weaker answer.

    MUTATION: making the ``verifier is None`` arm of ``_page_verification``
    return a valid verdict without verifying → THIS TEST GOES RED.
    """
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)
    state.chain_verifier = None
    _seed(h, 3)
    _tamper_payload_in_place(h.audit_url, 1)

    async def _t() -> None:
        async with h.client() as client:
            v = (await _page(h, client))["verification"]
            assert v["valid"] is False
            assert v["first_break_seq"] == 1
            assert v["detail"].startswith("chain broken at seq 1")

    run(_t())


def _delete_head_row(audit_url: str) -> None:
    """Delete the ``ne_audit_head`` row — a tamper class with no DB trigger on it.

    ``ne_audit_log`` carries no-update/no-delete triggers; ``ne_audit_head`` does
    not, so an attacker who cannot edit a record CAN still drop the head that
    pins the chain's length.  ``verify_chain`` treats that as a named break
    (``first_break_seq=0``, ``audit head missing (ne_audit_head row absent)``)
    rather than as an error, and the API page must report exactly that.
    """
    engine = sa.create_engine(audit_url)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM ne_audit_head"))
    finally:
        engine.dispose()


_HEAD_MISSING = "audit head missing (ne_audit_head row absent)"


def test_events_UNFILTERED_page_names_the_break_when_the_HEAD_ROW_is_deleted(
    make_app: AppBuilder,
) -> None:
    """The anchored request path must fail CLOSED and NAMED on a deleted head row.

    This is the regression the anchored page introduced: ``_full_verify`` read
    its extension baseline with ``ne.audit.store.read_head``, whose ``.one()``
    RAISES ``NoResultFound`` when the row is absent — and its ``except`` ends in
    a bare ``raise``, so the endpoint 500'd.  ``ne-api`` registers no exception
    handler, so the console's poll saw a 500, rendered no verdict strip at all,
    and a named tamper accusation became indistinguishable from the database
    being down.  ``verify_chain`` has ALWAYS named this case
    (``ne.audit.verify.read_head_or_none`` exists precisely for it), so the
    page must too.

    MUTATION 1: restoring ``read_head`` as ``_full_verify``'s baseline accessor
    → THIS TEST GOES RED (the request raises instead of returning 200).
    MUTATION 2: dropping ``chain=cv`` (or replacing ``detail=cv.detail`` with a
    generic sentence) from ``_full_verify``'s ``not cv.valid`` return → THIS
    TEST GOES RED, because the verdict stops NAMING the break.
    """
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)
    assert state.chain_verifier is not None
    _seed(h, 5)

    async def _t() -> None:
        async with h.client() as client:
            assert (await _page(h, client))["verification"]["valid"] is True
            assert state.chain_verifier is not None
            assert state.chain_verifier.anchor is not None

            _delete_head_row(h.audit_url)

            # The anchor is still warm, so the extension proof runs first, fails
            # to find its successor link, and falls back to the full walk — which
            # is the path that used to raise.
            v = (await _page(h, client))["verification"]
            assert v["valid"] is False
            assert v["first_break_seq"] == 0
            assert v["reason"] == _HEAD_MISSING
            # ...and the blessing is withdrawn, so every later read is inline.
            assert state.chain_verifier.anchor is None

            # Still named on the next call, with no warm anchor at all.
            again = (await _page(h, client))["verification"]
            assert again["valid"] is False
            assert again["first_break_seq"] == 0
            assert again["reason"] == _HEAD_MISSING

    run(_t())


def test_full_verify_names_the_deleted_head_row_instead_of_raising(
    make_app: AppBuilder,
) -> None:
    """The verifier ITSELF — what the background refresher and
    ``ModelSelectionService``'s frontier-evidence tick call — must return a named
    closed verdict here, not propagate ``NoResultFound``.

    Fixing this in the router would have left every OTHER caller of
    ``full_verify`` raising; this pins that the fix is at the verifier.

    MUTATION 1: restoring ``read_head(conn)`` as the baseline read in
    ``_full_verify`` → THIS TEST GOES RED with ``NoResultFound``.
    MUTATION 2: dropping ``chain=cv`` from that method's ``not cv.valid`` return
    → THIS TEST GOES RED on the ``first_break_seq``/``reason`` assertions.
    """
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)
    assert state.chain_verifier is not None
    _seed(h, 3)

    assert state.chain_verifier.full_verify().valid is True
    assert state.chain_verifier.anchor is not None

    _delete_head_row(h.audit_url)

    av = state.chain_verifier.full_verify()
    assert av.valid is False
    assert av.basis == "full-chain"
    assert av.chain is not None
    assert av.chain.first_break_seq == 0
    assert av.chain.reason == _HEAD_MISSING
    assert av.detail.startswith("chain broken at seq 0")
    assert state.chain_verifier.anchor is None  # fail-closed: blessing withdrawn


def test_events_FILTERED_page_still_names_the_deleted_head_row(
    make_app: AppBuilder,
) -> None:
    """The bounded ``from``/``to`` path was never broken and must stay unchanged.

    NOT discriminated by routing the ``start``/``end`` branch through the
    verifier — post-fix BOTH paths name this break, which is the point.  What it
    pins is that the bounded walk's own head read stays fail-CLOSED.

    MUTATION: in ``ne.audit.verify._verify_bounded``, the non-empty-window
    ``if head is None:`` arm returning ``_ok(0)`` instead of the named break →
    THIS TEST GOES RED (``valid`` comes back ``True``).  Verified: the
    ``verifier is None`` test below stays GREEN under it (it walks
    ``_verify_full``), so the two are independently pinned.
    """
    h = make_app(ops_roles=("ops",))
    _seed(h, 4)
    _delete_head_row(h.audit_url)

    async def _t() -> None:
        async with h.client() as client:
            v = (
                await _page(
                    h,
                    client,
                    **{"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"},
                )
            )["verification"]
            assert v["valid"] is False
            assert v["first_break_seq"] == 0
            assert v["reason"] == _HEAD_MISSING

    run(_t())


def test_events_without_a_chain_verifier_still_names_the_deleted_head_row(
    make_app: AppBuilder,
) -> None:
    """The ``verifier is None`` fallback was never broken and must stay unchanged.

    MUTATION: making the ``verifier is None`` arm of ``_page_verification``
    return ``_ok(0)`` without verifying → THIS TEST GOES RED.  Verified: the
    bounded test above stays GREEN under it, so the two arms are independently
    pinned.
    """
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)
    state.chain_verifier = None
    _seed(h, 3)
    _delete_head_row(h.audit_url)

    async def _t() -> None:
        async with h.client() as client:
            v = (await _page(h, client))["verification"]
            assert v["valid"] is False
            assert v["first_break_seq"] == 0
            assert v["reason"] == _HEAD_MISSING
            assert v["detail"].startswith("chain broken at seq 0")

    run(_t())


def test_events_puts_the_verdicts_BASIS_on_the_wire(make_app: AppBuilder) -> None:
    """Three requests, three DIFFERENT proofs, three different ``basis`` values.

    The one thing a console cannot compute for itself: ``checked`` and ``valid``
    look identical whichever proof ran, so the reach of the verdict is only
    knowable if the server states it.

    MUTATION: hardcoding ``basis="full-chain"`` in
    ``_anchored_verification_view`` → the warm assertion GOES RED.
    MUTATION: passing no ``basis`` on the windowed arm of ``_page_verification``
    (taking ``chain_verification_view``'s default) → the windowed assertion
    GOES RED.
    """
    h = make_app(ops_roles=("ops",))
    _seed(h, 5)

    async def _t() -> None:
        async with h.client() as client:
            # Cold process: no anchor, so this call really does walk the ledger.
            cold = (await _page(h, client))["verification"]
            assert cold["valid"] is True
            assert cold["basis"] == "full-chain"
            assert cold["contradiction"] is False

            await _append(h, 2)
            warm = (await _page(h, client))["verification"]
            assert warm["valid"] is True
            # Same `checked`-covers-everything number as the cold read, and a
            # WEAKER proof behind it. That is the whole reason this field exists.
            assert warm["checked"] == 7
            assert warm["basis"] == "anchored-extension"
            assert warm["contradiction"] is False

            windowed = (
                await _page(
                    h,
                    client,
                    **{"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"},
                )
            )["verification"]
            assert windowed["valid"] is True
            # NOT "full-chain": a bounded walk is blind to every row outside the
            # window by construction, and calling it a whole-chain proof on the
            # wire would be the same overclaim one layer down.
            assert windowed["basis"] == "windowed"

    run(_t())


def test_events_full_verify_asks_for_the_REAL_walk_and_catches_what_the_anchor_misses(
    make_app: AppBuilder,
) -> None:
    """``?full_verify`` is the console's Re-verify sweep, and it is not a relabel.

    Proved by the one tamper class that separates the two proofs: a row edited
    at or below the anchor WITHOUT propagating its hash. The anchored extension
    proof never re-reads that row, so the ordinary page read passes over it
    (pinned as an honest miss by
    ``test_events_tamper_BELOW_the_anchor_is_MISSED_while_the_blessing_is_FRESH``)
    — and the sweep names it on the same request, with no clock advance and no
    background verify.

    THIS IS WHAT MAKES THE CONSOLE'S RE-VERIFY USABLE AT ALL. A break is only
    ever confirmed off a full-chain verdict, and the console refuses to withdraw
    a standing break on a weaker proof, so without this parameter the one control
    an operator has to clear a latch could never produce a verdict strong enough
    to clear it.

    MUTATION: dropping ``full`` from ``_page_verification`` (always calling
    ``verifier.verify()``) → THIS TEST GOES RED on ``valid``.
    """
    h = make_app(ops_roles=("ops",))
    _seed(h, 5)

    async def _t() -> None:
        async with h.client() as client:
            assert (await _page(h, client))["verification"]["valid"] is True  # anchor at 5
            await _append(h, 2)
            _tamper_payload_in_place(h.audit_url, 2)  # BELOW the anchor

            missed = (await _page(h, client))["verification"]
            assert missed["valid"] is True
            assert missed["basis"] == "anchored-extension"

            swept = (await _page(h, client, full_verify=True))["verification"]
            assert swept["valid"] is False
            assert swept["basis"] == "full-chain"
            assert swept["first_break_seq"] == 2
            assert swept["reason"] == "record tampered"

    run(_t())


def test_events_a_full_verify_sweep_is_still_ops_gated(make_app: AppBuilder) -> None:
    """The sweep grants no authority the ordinary read does not.

    It changes the VERDICT and nothing else — same chokepoint, same rows, same
    projection — so a non-ops principal is refused identically.

    MUTATION: reading ``full_verify`` before ``_require_ops`` → THIS TEST GOES
    RED.
    """
    h = make_app(ops_roles=("ops",))
    _seed(h, 3)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get(
                "/api/v1/audit/events",
                headers=h.bearer("user:nobody", groups=["viewer"]),
                params={"full_verify": True},
            )
            assert r.status_code == 403

    run(_t())


def test_events_a_windowed_read_stays_windowed_even_when_full_verify_is_asked_for(
    make_app: AppBuilder,
) -> None:
    """Both narrow the walk, and a bounded walk is never also a whole-chain proof.

    The window wins and the wire SAYS so, rather than a verdict that walked a
    slice going out labelled as one that walked the ledger.

    MUTATION: giving ``full`` precedence over the ``start``/``end`` branch in
    ``_page_verification`` → THIS TEST GOES RED.
    """
    h = make_app(ops_roles=("ops",))
    _seed(h, 4)

    async def _t() -> None:
        async with h.client() as client:
            v = (
                await _page(
                    h,
                    client,
                    full_verify=True,
                    **{"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"},
                )
            )["verification"]
            assert v["basis"] == "windowed"

    run(_t())


def test_events_carries_the_one_shot_CONTRADICTION_to_the_wire(make_app: AppBuilder) -> None:
    """The strongest signal the verifier has, and it survives exactly one response.

    A coherent rewrite at or below the anchor leaves the ledger internally
    PERFECT — ``verify_chain`` is documented as blind to it — so the refusal
    comes from the remembered anchor alone, and the verifier re-anchors on the
    way out. The very next read PASSES. A console that requires N consecutive
    disagreeing reads before it believes a break therefore cannot ever see this
    one unless the response says what it is.

    Both halves are asserted: the flag on the refusal, and the pass immediately
    after it that makes the flag necessary.

    MUTATION: dropping ``contradiction=av.contradiction`` from
    ``_anchored_verification_view`` (hardcoding ``False``) → THIS TEST GOES RED.
    """
    h = make_app(ops_roles=("ops",))
    _seed(h, 4)

    async def _t() -> None:
        async with h.client() as client:
            assert (await _page(h, client))["verification"]["valid"] is True  # anchor at 4
            _coherently_rewrite_prefix(h.audit_url, seq=2)

            # `verify_chain` alone is fooled — that is the documented limit of a
            # forward hash chain, and the reason this verdict is worth shipping.
            refused = (await _page(h, client))["verification"]
            assert refused["valid"] is False
            assert refused["basis"] == "full-chain"
            assert refused["contradiction"] is True
            # It named no entry: the walk under it PASSED. The console's
            # seq-less break arm is what renders this, and the server's own
            # sentence is all it has to go on.
            assert refused["first_break_seq"] is None
            assert "does not extend the head verified at seq 4" in refused["detail"]

            # ONE SHOT. The freshly verified head was adopted, so this is VALID.
            after = (await _page(h, client))["verification"]
            assert after["valid"] is True
            assert after["contradiction"] is False

    run(_t())


def _coherently_rewrite_prefix(audit_url: str, *, seq: int) -> None:
    """Rewrite one row BELOW the head and repair every hash from there up.

    The attack a forward hash chain cannot see: after this the ledger is
    internally perfect and ``verify_chain`` calls it valid. Only a head hash a
    process REMEMBERS from its own full verify disproves it.
    """
    engine = sa.create_engine(audit_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
            conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_head_no_rewind")
            row = conn.execute(
                sa.text("SELECT payload, prev_hash FROM ne_audit_log WHERE seq = :s"), {"s": seq}
            ).first()
            assert row is not None
            payload = dict(json.loads(row[0]) if isinstance(row[0], str) else row[0])
            payload["resource"] = "FORGED"
            prev_hash = str(row[1])
            head_seq = int(
                conn.execute(sa.text("SELECT max(seq) FROM ne_audit_log")).scalar_one_or_none() or 0
            )
            for s in range(seq, head_seq + 1):
                if s == seq:
                    current = payload
                else:
                    raw = conn.execute(
                        sa.text("SELECT payload FROM ne_audit_log WHERE seq = :s"), {"s": s}
                    ).scalar_one()
                    current = dict(json.loads(raw) if isinstance(raw, str) else raw)
                record_hash = compute_record_hash(prev_hash, current)
                conn.execute(
                    sa.text(
                        "UPDATE ne_audit_log SET payload = :p, prev_hash = :ph, "
                        "record_hash = :rh, resource = :res WHERE seq = :s"
                    ),
                    {
                        "p": json.dumps(current),
                        "ph": prev_hash,
                        "rh": record_hash,
                        # The denormalized column moves WITH the payload, or the
                        # column/payload cross-check names an ordinary break and
                        # this stops being the invisible-to-verify_chain attack
                        # it is here to be.
                        "res": current["resource"],
                        "s": s,
                    },
                )
                prev_hash = record_hash
            conn.execute(
                sa.text("UPDATE ne_audit_head SET seq = :s, record_hash = :rh WHERE id = 1"),
                {"s": head_seq, "rh": prev_hash},
            )
    finally:
        engine.dispose()
