# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The approval window is the ACTION's, resolved by policy — not the login's.

Owner requirement, 2026-08-30: approval time is a configurable property of the
policy engine, set per action beside that action's risk tier. Never hard-coded,
and never tied to the auth token lifetime.

`tiers.rego` resolves `approval_window_seconds` and its own suite pins the
resolution order. This pins the CONSUMING half: the value has to survive the trip
to the durable row's `expires_at`, because a policy that declares a window
nothing reads is decoration.

Why it matters: on 2026-08-28 four runs stranded mid-approval because the
effective window was an ACCIDENT of the access-token lifespan (~15 min). Nobody
chose fifteen minutes.
"""

from __future__ import annotations

import asyncio as _asyncio
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.correlation import approval_window_scope

from .conftest import (
    ALLOWED_RECIPIENT,
    RecordingAuditSink,
    ToolLog,
    approval_request,
    approval_settings,
    make_executor,
    make_principal,
    standard_bindings,
)


def _gate(store: Any, settings: Any = None) -> DurableApprovalGate:
    return DurableApprovalGate(
        store=store, sink=RecordingAuditSink(), settings=settings or approval_settings()
    )


def _window_seconds(store: Any, request_id: str) -> float:
    with store.engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT requested_at, expires_at FROM ne_approval_requests WHERE request_id = :r"
            ),
            {"r": request_id},
        ).one()

    def _utc(value: Any) -> datetime:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)

    return (_utc(row.expires_at) - _utc(row.requested_at)).total_seconds()


def _submit(gate: Any, action: str, tier: int) -> str:
    import asyncio

    return asyncio.run(gate.submit(approval_request(action, tier)))


def test_the_policy_window_becomes_the_rows_real_deadline(approval_store: Any) -> None:
    gate = _gate(approval_store)
    with approval_window_scope(600.0):
        request_id = _submit(gate, "write.external.email.send", 2)
    assert _window_seconds(approval_store, request_id) == 600.0


def test_no_policy_window_keeps_the_deployment_default(approval_store: Any) -> None:
    """Compatibility: every caller that knows nothing about windows is unchanged."""
    settings = approval_settings()
    gate = _gate(approval_store, settings)
    request_id = _submit(gate, "write.external.email.send", 2)
    assert _window_seconds(approval_store, request_id) == float(settings.approvals.expiry_s)


def test_a_nonpositive_window_can_never_shorten_the_row(approval_store: Any) -> None:
    """Zero/negative/non-finite NEVER reach the row — they fall back to the default.

    A zero window would expire the request the instant it was created: an
    approval no human could answer, which is exactly the failure this machinery
    exists to remove. Normalised in the scope AND at the gate, because it must
    not be reachable from either end.
    """
    settings = approval_settings()
    gate = _gate(approval_store, settings)
    default_s = float(settings.approvals.expiry_s)
    for bad in (0.0, -1.0, -86400.0, float("inf"), float("nan")):
        with approval_window_scope(bad):
            request_id = _submit(gate, "write.external.email.send", 2)
        assert _window_seconds(approval_store, request_id) == default_s


def test_two_actions_carry_DIFFERENT_windows_in_one_deployment(approval_store: Any) -> None:
    """The whole point: per-ACTION, which a single global expiry_s cannot express."""
    gate = _gate(approval_store)
    with approval_window_scope(600.0):
        quick = _submit(gate, "write.external.email.send", 2)
    with approval_window_scope(86400.0):
        slow = _submit(gate, "finance.wire.send", 3)
    assert _window_seconds(approval_store, quick) == 600.0
    assert _window_seconds(approval_store, slow) == 86400.0


def test_the_scope_does_not_leak_past_its_block(approval_store: Any) -> None:
    """An ambient value that outlived its call would silently re-window the NEXT
    action — the failure mode a ContextVar earns by being ambient at all."""
    settings = approval_settings()
    gate = _gate(approval_store, settings)
    with approval_window_scope(600.0):
        inside = _submit(gate, "write.external.email.send", 2)
    after = _submit(gate, "write.external.email.send", 2)
    assert _window_seconds(approval_store, inside) == 600.0
    assert _window_seconds(approval_store, after) == float(settings.approvals.expiry_s)


# --- the DECISION carries the window to the row (ADR-0030, SPEC §12 item 5) --
#
# Everything above enters `approval_window_scope` BY HAND, so it proves the
# scope→row half and NOTHING about production: the defect this item closes was
# precisely that no production path ever bound the scope — `_approval_window_s`
# read `decision.obligations["approval_window_seconds"]`, no decision ever
# carried that obligation, and the fallback to `cfg.expiry_s` fired on every
# path that ever executed (a test that fabricated the obligation masked it).
#
# These tests therefore drive the REAL read path end-to-end: a real
# `GovernedPdp` + `OpaClient` parse a decision document shaped exactly like
# bundle 1.3.0's output (the window FIRST-CLASS, obligations WITHOUT it), the
# real `GovernedToolExecutor` computes `_approval_window_s` from the resulting
# `PolicyDecision`, binds the scope, and the real `DurableApprovalGate`
# persists the row. They CANNOT pass vacuously: `cfg.expiry_s` is pinned to a
# value distinct from every policy window asserted, and the window never
# appears in obligations — so if the middleware stops reading the field (or
# the field stops arriving), the row falls back to `cfg.expiry_s` and the
# equality below fails. Red-proved against the pre-fix middleware
# (obligations read): both fail with window == 1000.0.


def _executor_wired(
    store: Any,
    *,
    expiry_s: float = 1000.0,
    pdp_overrides: dict[str, dict[str, Any]] | None = None,
) -> tuple[Any, Any]:
    gate = DurableApprovalGate(
        store=store,
        sink=RecordingAuditSink(),
        settings=approval_settings(expiry_s=expiry_s, poll_interval_s=0.0),
    )
    log = ToolLog()
    executor = make_executor(
        standard_bindings(log),
        RecordingAuditSink(),
        approval_gate=gate,
        pdp_overrides=pdp_overrides,
    )
    return gate, executor


def _park_and_approve(store: Any, gate: Any, executor: Any) -> str:
    """Run one governed tier-2 call to the parked row, approve it, return its id."""

    async def scenario() -> str:
        task = _asyncio.create_task(
            executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": "hi"},
                principal=make_principal(),
            )
        )
        while not await store.list_pending():
            await _asyncio.sleep(0)
        request_id = (await store.list_pending())[0].request_id
        await gate.approve(request_id, approver="carol")
        assert await _asyncio.wait_for(task, timeout=10) == "sent"
        return request_id

    return _asyncio.run(scenario())


def test_the_policy_decision_windows_the_row_through_the_real_middleware(
    approval_store: Any,
) -> None:
    """Floor case: the rego-faithful PDP emits the tier-2 floor (14400) and the
    row's deadline IS that floor — not the deployment default it always was."""
    gate, executor = _executor_wired(approval_store, expiry_s=1000.0)
    request_id = _park_and_approve(approval_store, gate, executor)
    assert _window_seconds(approval_store, request_id) == 14400.0


def test_a_per_action_override_windows_the_row_through_the_real_middleware(
    approval_store: Any,
) -> None:
    """Per-action case, against the LITERAL bundle-1.3.0 document for a
    deployment that set `data.ne.config.approval_windows.actions[...] = 600`
    (that document's shape is pinned by decision_test.rego's
    `test_a_configured_action_window_reaches_the_decision` and the parity
    suite). The window rides ONLY first-class — obligations stay exactly the
    static parity value — so this fails if the middleware reads anywhere else."""
    live_doc: dict[str, Any] = {
        "effect": "require-approval",
        "tier": 2,
        "reason": "tier 2: external/PII action — per-item human approval required",
        "obligations": {"per_item_human_approval": True},
        "approval_window_seconds": 600,
        "policy_version": "1.3.0",
    }
    gate, executor = _executor_wired(
        approval_store,
        expiry_s=1000.0,
        pdp_overrides={"write.external.email.send": live_doc},
    )
    request_id = _park_and_approve(approval_store, gate, executor)
    assert _window_seconds(approval_store, request_id) == 600.0


def test_a_windowless_decision_still_falls_back_to_the_deployment_default(
    approval_store: Any,
) -> None:
    """A pre-1.3.0 bundle (no key at all) keeps today's behaviour bit-for-bit."""
    pre_130_doc: dict[str, Any] = {
        "effect": "require-approval",
        "tier": 2,
        "reason": "tier 2: external/PII action — per-item human approval required",
        "obligations": {"per_item_human_approval": True},
        "policy_version": "1.2.0",
    }
    gate, executor = _executor_wired(
        approval_store,
        expiry_s=1000.0,
        pdp_overrides={"write.external.email.send": pre_130_doc},
    )
    request_id = _park_and_approve(approval_store, gate, executor)
    assert _window_seconds(approval_store, request_id) == 1000.0


# --- the decision-level isfinite guard is load-bearing, so it is TESTED ------
#
# `inf` CAN reach the decision: a deployment data document containing `1e400`
# survives OPA and `json.loads` parses it as float('inf') — pydantic accepts it
# (floats allow inf by default), so `_approval_window_s` is the line that must
# refuse it. Without this test, deleting `math.isfinite` from that guard left
# every named suite green (`approval_window_scope` independently normalises),
# which is exactly how the original defect survived: a guard nothing exercises
# is a guard the next refactor deletes.


def test_a_nonfinite_policy_window_is_refused_at_the_decision_read() -> None:
    import json
    import math

    from ne.contracts.governance import PolicyDecision
    from ne.runtime.middleware import _approval_window_s

    # The literal pipeline shape: 1e400 in the data doc -> OPA JSON -> json.loads.
    doc = json.loads(
        '{"effect": "require-approval", "tier": 2,'
        ' "reason": "tier 2: external/PII action — per-item human approval required",'
        ' "obligations": {"per_item_human_approval": true},'
        ' "approval_window_seconds": 1e400, "policy_version": "1.3.0"}'
    )
    decision = PolicyDecision.model_validate(doc)
    assert decision.approval_window_seconds is not None
    assert math.isinf(decision.approval_window_seconds)  # it really arrives as inf
    assert _approval_window_s(decision) is None  # ...and is refused, not honoured

    nan_decision = decision.model_copy(update={"approval_window_seconds": float("nan")})
    assert _approval_window_s(nan_decision) is None
