# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""BD-15 server authority for truthful, governed per-run model choice."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from inspect import signature
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from ne.api.inference_lifecycle import (
    InferenceLifecycleConfig,
    InferenceLifecycleService,
    InferenceTargetConfig,
    inference_target_state,
)
from ne.api.model_selection import (
    MODEL_OBSERVATION_ACTION,
    MODEL_SELECTION_ACTION,
    MODEL_SELECTION_EVIDENCE_ACTION,
    ModelSelectionEvidenceError,
    ModelSelectionPolicyRefused,
    ModelSelectionService,
    ModelSelectionUnavailable,
    UnknownModelSelection,
)
from ne.api.serving import ServingStatusService
from ne.audit import PostgresAuditSink, ensure_audit_schema, list_records
from ne.contracts.governance import (
    AuditVerification,
    DecisionEffect,
    PolicyDecision,
    PolicyRequest,
)
from ne.contracts.inference import InferenceTargetOperation, InferenceTargetState
from ne.contracts.types import ApprovalTier, Lane, Principal
from ne.events import new_audit_event
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute
from ne.gateway.gateway import LiteLLMGateway
from ne.runtime.actions import tier_for_action
from ne.schema import AuditEvent, AuditOutcome

from .conftest import run

NOW = datetime(2026, 8, 20, 17, 0, tzinfo=UTC)
CONFIG_HASH = "a" * 64
PRIOR_CONFIG_HASH = "b" * 64
TRACE_ID = "trace-model-selection"
PRINCIPAL = Principal(id="user:demo", groups=("users",))

PRIVATE = ModelDef(
    name="private-default",
    litellm_model="hosted_vllm/qwen",
    provider="hosted_vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://vllm:8000/v1",
    context_window=32768,
)
PRIVATE_PEER = ModelDef(
    name="private-peer",
    litellm_model="hosted_vllm/qwen-peer",
    provider="hosted_vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://vllm:8001/v1",
    context_window=32768,
)
SMALL = ModelDef(
    name="tiny-private",
    litellm_model="hosted_vllm/tiny",
    provider="hosted_vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://vllm:8002/v1",
    context_window=8192,
)
FRONTIER = ModelDef(
    name="frontier-anthropic",
    litellm_model="anthropic/claude-opus-4-1",
    provider="anthropic",
    lane=Lane.FRONTIER_API,
    context_window=200000,
)
SETTINGS = GatewaySettings(
    models=[PRIVATE, PRIVATE_PEER, SMALL, FRONTIER],
    tasks={"agent": TaskRoute(model=PRIVATE.name)},
)


@dataclass(frozen=True)
class ServingRow:
    model_id: str
    lane: str
    state: str = "ok"
    checked_at: datetime | None = NOW
    serves_configured_model: bool | None = True


class StubServingStatus:
    def __init__(self, rows: Sequence[ServingRow] = (), *, failure: Exception | None = None):
        self.rows = tuple(rows)
        self.failure = failure
        self.seen: list[tuple[str, ...]] = []
        self.force_refreshes: list[bool] = []

    async def observe(
        self,
        models: Sequence[ModelDef],
        *,
        force_refresh: bool = False,
    ) -> Sequence[ServingRow]:
        self.seen.append(tuple(model.name for model in models))
        self.force_refreshes.append(force_refresh)
        if self.failure is not None:
            raise self.failure
        return self.rows


class ScriptedPdp:
    def __init__(
        self,
        effect: DecisionEffect = DecisionEffect.ALLOW,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.effect = effect
        self.failure = failure
        self.requests: list[PolicyRequest] = []

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return PolicyDecision(
            effect=self.effect,
            policy_version="selection-policy-1",
            reason="scripted",
        )


class FailSelectionSink:
    """Delegates every event except the authority's final evidence append."""

    def __init__(self, delegate: PostgresAuditSink) -> None:
        self.delegate = delegate

    async def append(self, event: AuditEvent) -> None:
        if event.action == MODEL_SELECTION_EVIDENCE_ACTION:
            raise RuntimeError("selection audit unavailable")
        await self.delegate.append(event)

    async def verify(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> AuditVerification:
        return await self.delegate.verify(start=start, end=end)


@pytest.fixture
def audit_engine(tmp_path: Path) -> Iterator[sa.Engine]:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'model-selection.db'}")
    sink = PostgresAuditSink(engine)
    ensure_audit_schema(engine)
    assert sink.engine is engine
    try:
        yield engine
    finally:
        engine.dispose()


def _row(model: ModelDef = PRIVATE, **changes: Any) -> ServingRow:
    return replace(
        ServingRow(model_id=model.name, lane=model.lane.value),
        **changes,
    )


def _service(
    audit_engine: sa.Engine,
    *,
    rows: Sequence[ServingRow] = (),
    pdp_effect: DecisionEffect = DecisionEffect.ALLOW,
    pdp_failure: Exception | None = None,
    serving_failure: Exception | None = None,
    air_gap_provider: Callable[[], bool] = lambda: False,
    clock: Callable[[], datetime] = lambda: NOW,
    sink: Any | None = None,
    lifecycle_availability: Callable[[str], tuple[bool, str | None]] | None = None,
) -> tuple[ModelSelectionService, ScriptedPdp, StubServingStatus, PostgresAuditSink]:
    real_sink = PostgresAuditSink(audit_engine)
    pdp = ScriptedPdp(pdp_effect, failure=pdp_failure)
    serving = StubServingStatus(rows, failure=serving_failure)
    service = ModelSelectionService(
        gateway_settings=SETTINGS,
        serving_status=serving,
        audit_engine=audit_engine,
        pdp=pdp,
        sink=real_sink if sink is None else sink,
        config_hash=CONFIG_HASH,
        air_gap_provider=air_gap_provider,
        clock=clock,
        lifecycle_availability=lifecycle_availability,
    )
    return service, pdp, serving, real_sink


def test_lifecycle_not_ready_removes_alias_before_serving_probe(
    audit_engine: sa.Engine,
) -> None:
    service, _pdp, serving, _sink = _service(
        audit_engine,
        rows=(_row(), _row(PRIVATE_PEER)),
        lifecycle_availability=lambda alias: (
            (False, "lifecycle-stopped") if alias == PRIVATE.name else (True, None)
        ),
    )
    catalog = run(service.choices("agent"))
    assert [choice.model for choice in catalog.choices] == [PRIVATE_PEER.name]
    # `tiny-private` is probed alongside it since NE-CTX-IN-FORCE removed the
    # config-time budget floor (it has no serving row, so it is not offered). The
    # property under test is that the LIFECYCLE-stopped alias is gone BEFORE the
    # probe — asserted by PRIVATE.name's absence, not by the tuple's length.
    assert serving.seen == [(PRIVATE_PEER.name, SMALL.name)]


def test_future_lifecycle_observation_cannot_make_model_selectable(
    audit_engine: sa.Engine,
) -> None:
    lifecycle = InferenceLifecycleService(
        engine=audit_engine,
        config=InferenceLifecycleConfig(
            targets=(
                InferenceTargetConfig(
                    target_id="cpu-vllm",
                    display_name="CPU vLLM",
                    adapter_id="compose-v1",
                    aliases=(PRIVATE.name,),
                    capabilities=(InferenceTargetOperation.STATUS,),
                    resource_id="compose:vllm-cpu",
                ),
            )
        ),
        operator=None,
        air_gap_provider=lambda: False,
        config_hash=CONFIG_HASH,
        clock=lambda: NOW,
    )
    future = NOW + timedelta(days=1)
    with audit_engine.begin() as conn:
        conn.execute(
            inference_target_state.update()
            .where(inference_target_state.c.target_id == "cpu-vllm")
            .values(
                state=InferenceTargetState.READY.value,
                desired_state=InferenceTargetState.READY.value,
                manifest_hash="c" * 64,
                observed_at=future,
                updated_at=future,
            )
        )
    service, _pdp, serving, _sink = _service(
        audit_engine,
        rows=(_row(), _row(PRIVATE_PEER)),
        lifecycle_availability=lifecycle.availability,
    )

    catalog = run(service.choices("agent"))

    assert [choice.model for choice in catalog.choices] == [PRIVATE_PEER.name]
    # See the note in the test above: the smaller-window model is probed now, and
    # PRIVATE.name's absence is what this test is about.
    assert serving.seen == [(PRIVATE_PEER.name, SMALL.name)]


async def _observe(
    sink: PostgresAuditSink,
    model: ModelDef = FRONTIER,
    *,
    outcome: AuditOutcome = AuditOutcome.success,
    occurred_at: datetime = NOW,
    config_hash: str = CONFIG_HASH,
    lane: str | None = None,
    alias: str | None = None,
    include_lane: bool = True,
    actor: str = "ne-gateway",
    operation: str | None = "complete",
) -> None:
    provenance = {
        "task": "agent",
        "model_name": model.name if alias is None else alias,
    }
    if operation is not None:
        provenance["operation"] = operation
    if include_lane:
        provenance["lane"] = model.lane.value if lane is None else lane
    await sink.append(
        new_audit_event(
            action=MODEL_OBSERVATION_ACTION,
            resource=model.litellm_model,
            outcome=outcome,
            actor=actor,
            trace_id="trace-provider-attempt",
            config_hash=config_hash,
            occurred_at=occurred_at,
            provenance=provenance,
        )
    )


def _names(service: ModelSelectionService) -> tuple[str, ...]:
    catalog = run(service.choices("agent"))
    return tuple(choice.model for choice in catalog.choices)


def _selection_records(engine: sa.Engine) -> list[Any]:
    return list(list_records(engine, action=MODEL_SELECTION_EVIDENCE_ACTION, limit=1000))


def test_selection_action_is_classified_by_the_existing_read_policy_rule() -> None:
    # The Python mirror is drift-guarded against both OPA and Cedar wildcard
    # policy bundles elsewhere; this exact action therefore needs no policy edit.
    assert tier_for_action(MODEL_SELECTION_ACTION) is ApprovalTier.TIER_0


def test_frontier_evidence_actor_matches_the_production_gateway_default() -> None:
    assert signature(LiteLLMGateway.__init__).parameters["actor"].default == "ne-gateway"


def test_current_route_model_is_available_and_authorized_by_exact_pdp_identity(
    audit_engine: sa.Engine,
) -> None:
    service, pdp, serving, _sink = _service(audit_engine, rows=[_row()])

    selection = run(
        service.authorize(
            task="agent",
            requested_model=PRIVATE.name,
            principal=PRINCIPAL,
            trace_id=TRACE_ID,
        )
    )

    assert selection.model == PRIVATE.name
    assert selection.lane is Lane.PRIVATE_VLLM
    assert selection.observed_at == NOW
    assert selection.policy_effect == DecisionEffect.ALLOW.value
    # NE-CTX-IN-FORCE: the smaller-window model is probed WITH the others now.
    # It used to be excluded before probing by the config-time budget floor; that
    # floor is gone because the runtime trim guard re-derives its budget from the
    # model in force, so a narrower model is a legitimate choice that simply gets
    # a narrower budget.
    assert serving.seen == [
        (PRIVATE.name, PRIVATE_PEER.name, SMALL.name),
    ]
    [request] = pdp.requests
    assert request.action == MODEL_SELECTION_ACTION
    assert request.resource == f"model:{PRIVATE.name}"
    assert dict(request.context) == {
        "task": "agent",
        "lane": Lane.PRIVATE_VLLM.value,
        "air_gap": False,
    }
    [record] = _selection_records(audit_engine)
    assert record.outcome == AuditOutcome.success.value
    assert record.trace_id == TRACE_ID
    assert record.config_hash == CONFIG_HASH
    assert record.payload["provenance"]["requested_model"] == PRIVATE.name
    assert record.payload["provenance"]["pdp_effect"] == DecisionEffect.ALLOW.value
    assert record.payload["provenance"]["policy_version"] == "selection-policy-1"


def test_smaller_context_window_is_offered_and_authorized(
    audit_engine: sa.Engine,
) -> None:
    """NE-CTX-IN-FORCE — the direct inverse of the rule this test used to pin.

    A model with a SMALLER serving window than the task's boot primary used to be
    refused outright ("task-incompatible"), because ``RuntimeSettings.context_
    budget`` was derived from that primary once at boot and the trim guard would
    otherwise have passed a prompt the chosen model could not hold. The guard now
    re-derives its budget per request from the model IN FORCE
    (``GatewayModel._turn_budget``), so choosing a narrower model simply means a
    narrower budget — an honest, audited, per-turn refusal if a turn does not fit
    it, instead of a config-time veto that forced every lane to be declared at the
    narrowest lane's window.

    Availability is unchanged and still measured: this model is offered because it
    has a current serving row, exactly like ``private-default``.
    """
    service, _pdp, _serving, _sink = _service(
        audit_engine,
        rows=[_row(), _row(SMALL)],
    )

    assert _names(service) == (PRIVATE.name, SMALL.name)
    selection = run(
        service.authorize(
            task="agent",
            requested_model=SMALL.name,
            principal=PRINCIPAL,
            trace_id=TRACE_ID,
        )
    )
    assert selection.model == SMALL.name
    [record] = _selection_records(audit_engine)
    assert record.outcome == AuditOutcome.success.value
    assert record.payload["provenance"]["requested_model"] == SMALL.name


def test_a_pin_the_current_snapshot_overflows_is_disabled_not_offered(
    audit_engine: sa.Engine,
) -> None:
    """SPEC PRE-DEMO-REHEARSAL-014: "A model whose context window cannot
    safely accept the current snapshot is absent/disabled unless a real governed
    compaction operation exists. ... There is no silent truncation, new
    Conversation, cache-reset disguise or fallback."

    Round 7: the selection authority never consulted the CURRENT conversation
    snapshot, so a conversation
    grown on the wide in-force window was offered — and explicitly authorized
    onto — a narrow target that could not accept it. This test FAILS on
    58c294d3, where neither the catalog nor ``authorize`` can even be TOLD the
    snapshot size and ``tiny-private`` stays offered regardless. No compaction
    operation exists, so the target must vanish from the catalog and an
    explicit authorize of it must refuse BY NAME, before any provider call.
    """
    service, _pdp, _serving, _sink = _service(
        audit_engine,
        rows=[_row(), _row(SMALL)],
    )
    over = SETTINGS.context_budget_for(SMALL).input_budget + 1
    # The scenario is wide-current → narrow-target: the same snapshot fits the
    # in-force primary, which therefore stays offered.
    assert over <= SETTINGS.context_budget_for(PRIVATE).input_budget

    display = tuple(
        choice.model for choice in service.configured_catalog("agent", snapshot_tokens=over).choices
    )
    assert SMALL.name not in display and PRIVATE.name in display
    evidenced = run(service.choices("agent", snapshot_tokens=over))
    assert tuple(choice.model for choice in evidenced.choices) == (PRIVATE.name,)

    with pytest.raises(ModelSelectionUnavailable) as refused:
        run(
            service.authorize(
                task="agent",
                requested_model=SMALL.name,
                principal=PRINCIPAL,
                trace_id=TRACE_ID,
                snapshot_tokens=over,
            )
        )
    assert refused.value.model == SMALL.name
    [record] = _selection_records(audit_engine)
    assert record.outcome == AuditOutcome.denied.value
    assert record.payload["provenance"]["reason"] == "task-incompatible"


def test_a_pin_the_current_snapshot_fits_stays_offered_and_authorized(
    audit_engine: sa.Engine,
) -> None:
    """The control for the snapshot gate: a target that CAN accept the current
    snapshot stays offered and authorized WITH the snapshot passed — this is a
    per-conversation fit rule, never a blanket refusal of narrow models (the
    deleted budget-width floor must not return through it)."""
    service, _pdp, _serving, _sink = _service(
        audit_engine,
        rows=[_row(), _row(SMALL)],
    )
    fits = SETTINGS.context_budget_for(SMALL).input_budget

    display = service.configured_catalog("agent", snapshot_tokens=fits)
    assert SMALL.name in tuple(choice.model for choice in display.choices)
    selection = run(
        service.authorize(
            task="agent",
            requested_model=SMALL.name,
            principal=PRINCIPAL,
            trace_id=TRACE_ID,
            snapshot_tokens=fits,
        )
    )
    assert selection.model == SMALL.name
    [record] = _selection_records(audit_engine)
    assert record.outcome == AuditOutcome.success.value


@pytest.mark.parametrize(
    "row",
    [
        _row(state="unreachable", serves_configured_model=None),
        _row(serves_configured_model=False),
        _row(checked_at=NOW - timedelta(seconds=5, microseconds=1)),
        _row(checked_at=NOW + timedelta(seconds=5, microseconds=1)),
        _row(lane=Lane.FRONTIER_API.value),
        _row(model_id=PRIVATE_PEER.name),
    ],
    ids=["failed", "mismatch", "stale", "future", "wrong-lane", "wrong-alias"],
)
def test_private_choice_requires_current_exact_successful_observation(
    audit_engine: sa.Engine,
    row: ServingRow,
) -> None:
    service, _pdp, _serving, _sink = _service(audit_engine, rows=[row])
    assert PRIVATE.name not in _names(service)


def test_private_missing_or_ambiguous_evidence_is_not_current_success(
    audit_engine: sa.Engine,
) -> None:
    missing, *_ = _service(audit_engine)
    assert PRIVATE.name not in _names(missing)

    ambiguous, *_ = _service(audit_engine, rows=[_row(), _row()])
    assert PRIVATE.name not in _names(ambiguous)


def test_auto_authorization_bypasses_display_cache_and_failed_refresh_stays_closed(
    audit_engine: sa.Engine,
) -> None:
    @dataclass
    class Clock:
        current: datetime = NOW

        def __call__(self) -> datetime:
            return self.current

    class Endpoint:
        def __init__(self) -> None:
            self.available = True
            self.model_reads = 0

        def respond(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                self.model_reads += 1
                if not self.available:
                    raise httpx.ConnectError("scripted unavailable", request=request)
                return httpx.Response(
                    200,
                    json={"data": [{"id": "qwen", "max_model_len": 32768}]},
                )
            if request.url.path == "/version":
                return httpx.Response(200, json={"version": "test"})
            return httpx.Response(404)

    clock = Clock()
    endpoint = Endpoint()
    serving = ServingStatusService(
        transport=httpx.MockTransport(endpoint.respond),
        clock=clock,
    )
    settings = GatewaySettings(
        models=[PRIVATE],
        tasks={"agent": TaskRoute(model=PRIVATE.name)},
    )
    service = ModelSelectionService(
        gateway_settings=settings,
        serving_status=serving,
        audit_engine=audit_engine,
        pdp=ScriptedPdp(),
        sink=PostgresAuditSink(audit_engine),
        config_hash=CONFIG_HASH,
        air_gap_provider=lambda: False,
        clock=clock,
    )

    first = run(service.choices("agent"))
    assert first.effective_default == PRIVATE.name
    assert endpoint.model_reads == 1

    # Candidate resolution and the post-PDP recheck each bypass the display
    # cache. The admission therefore never borrows preflight's aging verdict.
    clock.current += timedelta(seconds=4, microseconds=999_999)
    authorized = run(
        service.authorize_auto(
            task="agent",
            principal=PRINCIPAL,
            trace_id=TRACE_ID,
        )
    )
    assert authorized.model == PRIVATE.name
    assert authorized.observed_at == clock.current
    assert endpoint.model_reads == 3

    # A failed forced refresh cannot fall back to the earlier success or its
    # last_ok_at history, even though the display cache remains populated.
    endpoint.available = False
    with pytest.raises(ModelSelectionUnavailable):
        run(
            service.authorize_auto(
                task="agent",
                principal=PRINCIPAL,
                trace_id=TRACE_ID,
            )
        )
    assert endpoint.model_reads == 4


def test_slow_frontier_evidence_is_acquired_before_fresh_private_observation(
    audit_engine: sa.Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class ScriptedClock:
        current: datetime = NOW

        def __call__(self) -> datetime:
            return self.current

    clock = ScriptedClock()
    service, _pdp, _serving, _sink = _service(audit_engine, clock=clock)

    class CurrentServingStatus:
        async def observe(
            self,
            models: Sequence[ModelDef],
            *,
            force_refresh: bool = False,
        ) -> Sequence[ServingRow]:
            del force_refresh
            return tuple(_row(model, checked_at=clock.current) for model in models)

    service._serving = CurrentServingStatus()
    read_frontier = service._frontier_records

    def slow_frontier(models: Sequence[ModelDef]) -> dict[str, Any]:
        # Deterministic stand-in for a replay/full-chain scan that takes longer
        # than the entire private evidence window; no wall-clock sleep required.
        clock.current = NOW + timedelta(seconds=6)
        return read_frontier(models)

    monkeypatch.setattr(service, "_frontier_records", slow_frontier)

    assert PRIVATE.name in _names(service)


def test_known_attempt_records_private_evidence_failure_before_withholding(
    audit_engine: sa.Engine,
) -> None:
    service, _pdp, _serving, _sink = _service(
        audit_engine,
        rows=[_row()],
        serving_failure=RuntimeError("PRIVATE PROBE SENTINEL"),
    )
    with pytest.raises(ModelSelectionEvidenceError, match="private serving evidence"):
        run(
            service.authorize(
                task="agent",
                requested_model=PRIVATE.name,
                principal=PRINCIPAL,
                trace_id=TRACE_ID,
            )
        )
    [record] = _selection_records(audit_engine)
    serialized = json.dumps(record.payload, sort_keys=True)
    assert record.outcome == AuditOutcome.error.value
    assert "evidence-error" in serialized
    assert "PRIVATE PROBE SENTINEL" not in serialized


def test_frontier_latest_error_overrides_earlier_success(audit_engine: sa.Engine) -> None:
    service, _pdp, _serving, sink = _service(audit_engine)
    run(_observe(sink, occurred_at=NOW - timedelta(seconds=2)))
    run(
        _observe(
            sink,
            outcome=AuditOutcome.error,
            occurred_at=NOW - timedelta(seconds=1),
        )
    )
    assert FRONTIER.name not in _names(service)


@pytest.mark.parametrize("operation", ["complete", "stream"])
def test_frontier_later_success_recovers_from_an_error(
    audit_engine: sa.Engine,
    operation: str,
) -> None:
    service, _pdp, _serving, sink = _service(audit_engine)
    run(
        _observe(
            sink,
            outcome=AuditOutcome.error,
            occurred_at=NOW - timedelta(seconds=2),
        )
    )
    run(_observe(sink, occurred_at=NOW - timedelta(seconds=1), operation=operation))
    catalog = run(service.choices("agent"))
    choice = next(item for item in catalog.choices if item.model == FRONTIER.name)
    assert choice.observed_at == NOW - timedelta(seconds=1)
    assert choice.evidence == "frontier-provider-attempt"


@pytest.mark.parametrize(
    "observation",
    [
        {"occurred_at": NOW - timedelta(seconds=60, microseconds=1)},
        {"occurred_at": NOW + timedelta(seconds=5, microseconds=1)},
        {"config_hash": PRIOR_CONFIG_HASH},
        {"include_lane": False},
        {"lane": Lane.PRIVATE_VLLM.value},
        {"alias": PRIVATE.name},
        {"actor": "unrelated-service"},
        {"operation": None},
        {"operation": "health-check"},
    ],
    ids=[
        "stale",
        "future",
        "prior-config",
        "malformed",
        "wrong-lane",
        "wrong-alias",
        "unrelated-actor",
        "missing-operation",
        "invalid-operation",
    ],
)
def test_frontier_excludes_noncurrent_or_nonmatching_observation(
    audit_engine: sa.Engine,
    observation: dict[str, Any],
) -> None:
    service, _pdp, _serving, sink = _service(audit_engine)
    run(_observe(sink, **observation))
    assert FRONTIER.name not in _names(service)


def test_frontier_missing_observation_is_not_available(audit_engine: sa.Engine) -> None:
    service, _pdp, _serving, _sink = _service(audit_engine)
    assert FRONTIER.name not in _names(service)


def test_invalid_chain_or_audit_query_error_never_returns_partial_frontier_success(
    audit_engine: sa.Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _pdp, _serving, sink = _service(audit_engine)
    run(_observe(sink))

    class Invalid:
        valid = False

    # The posterior chain proof now runs through the service's
    # AnchoredChainVerifier (ne.audit.anchor), so the refusal is pinned on the
    # verifier's verdict rather than on verify_chain directly. What is asserted
    # is unchanged: an invalid chain refuses instead of returning partial
    # frontier success.
    monkeypatch.setattr(service._chain_verifier, "verify", lambda: Invalid())
    with pytest.raises(ModelSelectionEvidenceError, match="chain is invalid"):
        _names(service)

    monkeypatch.setattr(service._chain_verifier, "verify", lambda: InvalidValid())

    def fail_query(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("SELECT", {}, RuntimeError("db unavailable"))

    monkeypatch.setattr("ne.api.model_selection.replay", fail_query)
    with pytest.raises(ModelSelectionEvidenceError, match="frontier serving evidence"):
        _names(service)


class InvalidValid:
    valid = True


def test_air_gap_and_posture_read_failure_hide_frontier_but_keep_current_private(
    audit_engine: sa.Engine,
) -> None:
    for provider in (
        lambda: True,
        lambda: (_ for _ in ()).throw(RuntimeError("posture store unavailable")),
    ):
        service, _pdp, _serving, sink = _service(
            audit_engine,
            rows=[_row()],
            air_gap_provider=provider,
        )
        run(_observe(sink))
        assert _names(service) == (PRIVATE.name,)
        with pytest.raises(ModelSelectionPolicyRefused, match="AIR_GAP"):
            run(
                service.authorize(
                    task="agent",
                    requested_model=FRONTIER.name,
                    principal=PRINCIPAL,
                    trace_id=TRACE_ID,
                )
            )


def test_posture_is_read_once_so_the_reported_air_gap_matches_the_computed_catalog(
    audit_engine: sa.Engine,
) -> None:
    # The effective posture is a LIVE read in production
    # (``effective_egress`` ORs in the ``ne.net.airgap`` process global, which
    # flips at runtime), so a second read can straddle a flip and describe a
    # catalog that was never computed.  A caller that must send its own PDP
    # request about these choices needs the posture they were built under.
    reads: list[int] = []

    def flipping() -> bool:
        reads.append(len(reads))
        return len(reads) > 1  # egress OPEN on the first read, SEALED after

    service, _pdp, _serving, sink = _service(
        audit_engine,
        rows=[_row()],
        air_gap_provider=flipping,
    )
    run(_observe(sink))

    catalog, air_gap = run(service.choices_with_air_gap("agent"))

    assert FRONTIER.name in [choice.model for choice in catalog.choices]
    assert air_gap is False
    assert reads == [0]


@pytest.mark.parametrize(
    "effect",
    [DecisionEffect.DENY, DecisionEffect.REQUIRE_APPROVAL],
)
def test_only_pdp_allow_can_authorize(
    audit_engine: sa.Engine,
    effect: DecisionEffect,
) -> None:
    service, pdp, _serving, _sink = _service(
        audit_engine,
        rows=[_row()],
        pdp_effect=effect,
    )
    with pytest.raises(ModelSelectionPolicyRefused):
        run(
            service.authorize(
                task="agent",
                requested_model=PRIVATE.name,
                principal=PRINCIPAL,
                trace_id=TRACE_ID,
            )
        )
    assert len(pdp.requests) == 1
    [record] = _selection_records(audit_engine)
    assert record.outcome == AuditOutcome.denied.value
    assert record.payload["provenance"]["pdp_effect"] == effect.value


def test_pdp_failure_is_named_only_and_never_authorizes(audit_engine: sa.Engine) -> None:
    service, _pdp, _serving, _sink = _service(
        audit_engine,
        rows=[_row()],
        pdp_failure=RuntimeError("OPA SENTINEL RESPONSE"),
    )
    with pytest.raises(ModelSelectionEvidenceError, match="policy is unavailable"):
        run(
            service.authorize(
                task="agent",
                requested_model=PRIVATE.name,
                principal=PRINCIPAL,
                trace_id=TRACE_ID,
            )
        )
    [record] = _selection_records(audit_engine)
    serialized = json.dumps(record.payload, sort_keys=True)
    assert record.outcome == AuditOutcome.error.value
    assert "policy-error" in serialized
    assert "OPA SENTINEL RESPONSE" not in serialized


def test_selection_append_failure_withholds_an_otherwise_valid_choice(
    audit_engine: sa.Engine,
) -> None:
    real_sink = PostgresAuditSink(audit_engine)
    service, _pdp, _serving, _sink = _service(
        audit_engine,
        rows=[_row()],
        sink=FailSelectionSink(real_sink),
    )
    with pytest.raises(ModelSelectionEvidenceError, match="could not be recorded"):
        run(
            service.authorize(
                task="agent",
                requested_model=PRIVATE.name,
                principal=PRINCIPAL,
                trace_id=TRACE_ID,
            )
        )
    assert _selection_records(audit_engine) == []


def test_unknown_client_value_never_reaches_pdp_or_audit_text(audit_engine: sa.Engine) -> None:
    sentinel = "CUSTOMER-PROMPT-MODEL-SENTINEL"
    service, pdp, _serving, _sink = _service(audit_engine, rows=[_row()])
    with pytest.raises(UnknownModelSelection, match="not registered"):
        run(
            service.authorize(
                task="agent",
                requested_model=sentinel,
                principal=PRINCIPAL,
                trace_id=TRACE_ID,
            )
        )
    assert pdp.requests == []
    [record] = _selection_records(audit_engine)
    serialized = json.dumps(record.payload, sort_keys=True)
    assert record.resource == "model:unregistered"
    assert sentinel not in serialized
    assert sentinel not in str(record.resource)
