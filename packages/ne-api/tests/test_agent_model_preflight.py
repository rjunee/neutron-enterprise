# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-011D governed Agent model/strategy preflight.

The REAL ``AgentCatalog`` (over the REAL mounted ``deploy/agents/registry.toml``
definitions), the REAL ``ModelSelectionService``, the REAL ``ModelRouteStore`` and
the REAL :func:`build_route_provider` run here.  Only the genuine I/O edges are
stubbed, and they are the SAME stubs ``test_model_selection`` already uses.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from ne.api.agent_catalog import AgentCatalog, CatalogAgent
from ne.api.agent_manifest import (
    AgentBudgets,
    AgentManifestDefinition,
    AgentVisibility,
    load_agent_manifest,
)
from ne.api.agent_model_preflight import AgentModelPreflight, AgentModelPreflightService
from ne.api.model_routing import (
    ModelRouteStore,
    action_for_lane,
    build_route_provider,
    ensure_model_route_schema,
)
from ne.api.model_selection import (
    MODEL_SELECTION_ACTION,
    MODEL_SELECTION_EVIDENCE_ACTION,
    ModelSelectionEvidenceError,
    ModelSelectionPolicyRefused,
    ModelSelectionService,
    ModelSelectionUnavailable,
)
from ne.api.serving import ServingStatusService
from ne.audit import PostgresAuditSink, ensure_audit_schema, list_records
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.governance import DecisionEffect, PolicyDecision, PolicyRequest
from ne.contracts.types import Lane, Principal
from ne.gateway.config import GatewaySettings, TaskRoute, overhead_margin_for

from .conftest import run
from .test_model_selection import (
    CONFIG_HASH,
    FRONTIER,
    NOW,
    PRIVATE,
    PRIVATE_PEER,
    SETTINGS,
    SMALL,
    ScriptedPdp,
    ServingRow,
    StubServingStatus,
    _observe,
    _row,
)

_ROOT = Path(__file__).parents[3]
_REGISTRY = _ROOT / "deploy/agents/registry.toml"

ANALYST_ID = "agt_01k3meridiananalyst000000000"
HARDSHIP_ID = "agt_01k3hardshipreview0000000000"
ANALYST_POLICY = "agent.meridian-analyst.v2"
HARDSHIP_POLICY = "agent.meridian-hardship-review.v3"
SYNTHETIC_ID = "agt_01k3syntheticpolicyagent0"
UNKNOWN_ID = "agt_01k3nosuchagentanywhere00000"

#: The exact hash of the merged registry definition, pinned so that editing a
#: budget, strategy or any other declared field in ``deploy/agents/registry.toml``
#: must be a deliberate, visible change here too.
ANALYST_DEFINITION_HASH = "bb3f8341666013d46dc7777ae487bf7211e15cc802900d69829a584ac3c27960"
HARDSHIP_DEFINITION_HASH = "7ec3a575ce90e555389657f9eb78a7489de01b2e8682f28ba3aed890af2ca71b"

#: Meridian Hardship Review is group-scoped to ``ops``; the Analyst is visible to
#: every authenticated principal.  That pair is the natural visibility control.
OPS = Principal(id="user:ops", groups=("ops",))
STAFF = Principal(id="user:staff", groups=("users",))

TRACE_ID = "trace-agent-preflight"

#: The complete field set of each projection dataclass.  A structural guard over
#: these beats a substring guard over prose: adding a "refusal_reason" or a
#: "withheld_count" field fails here no matter how it is spelled.
PREFLIGHT_FIELDS = {
    "agent_id",
    "agent_version",
    "definition_hash",
    "available",
    "availability_reasons",
    "task",
    "allowed_strategies",
    "default_strategy",
    "max_steps",
    "max_tool_calls",
    "max_input_tokens",
    "max_output_tokens",
    "candidates",
    "default_model",
}
CANDIDATE_FIELDS = {
    "model",
    "provider",
    "lane",
    "evidence",
    "observed_at",
    # A property of the MODEL (the gateway's own input budget), not a fact about
    # this Agent and not a fit verdict -- see the field's docstring for why no
    # Agent-relative comparison is served here.
    "input_budget",
}

#: A deployment that routes TWO tasks, so an Agent declaring the second one is
#: proven to project THAT task's catalog and route -- not merely proven negative
#: against an unrouted policy id.
MULTI_TASK_SETTINGS = GatewaySettings(
    models=[PRIVATE, PRIVATE_PEER, SMALL, FRONTIER],
    tasks={
        "agent": TaskRoute(model=PRIVATE.name),
        ANALYST_POLICY: TaskRoute(model=PRIVATE.name),
        "meridian-review": TaskRoute(model=PRIVATE_PEER.name),
    },
)

REGISTRY_SETTINGS = SETTINGS.model_copy(
    update={
        "tasks": {
            **SETTINGS.tasks,
            ANALYST_POLICY: SETTINGS.tasks["agent"],
            HARDSHIP_POLICY: SETTINGS.tasks["agent"],
        }
    }
)


# --- real manifest definitions -----------------------------------------------------


def _registry_definitions() -> dict[str, AgentManifestDefinition]:
    loaded = load_agent_manifest(_REGISTRY)
    return {definition.agent_id: definition for definition in loaded.manifest.agents}


def _definition_declaring(model_policy_id: str) -> AgentManifestDefinition:
    """A visible Agent declaring ``model_policy_id`` -- routed here or not."""
    return AgentManifestDefinition(
        agent_id=SYNTHETIC_ID,
        version=3,
        label="Declared Policy Agent",
        description="Declares an arbitrary model policy id for preflight tests.",
        owner_label="Preflight Tests",
        visibility=AgentVisibility(all_authenticated=True),
        lifecycle="active",
        allowed_strategies=("agentic",),
        default_strategy="agentic",
        instructions="Answer strictly from governed evidence served this run.",
        model_policy_id=model_policy_id,
        tool_grants=(),
        knowledge_sources=(),
        skills=(),
        budgets=AgentBudgets(
            max_steps=4,
            max_tool_calls=2,
            max_input_tokens=1000,
            max_output_tokens=500,
        ),
        availability_dependencies=(),
    )


def _catalog(
    *definitions: AgentManifestDefinition,
    available: bool = True,
    availability_reasons: tuple[str, ...] = (),
    present: bool = True,
) -> AgentCatalog:
    """Availability is a PARAMETER: the boot composition ships it ``False``."""
    return AgentCatalog(
        tuple(
            CatalogAgent(
                definition=definition,
                available=available,
                availability_reasons=availability_reasons,
                present=present,
            )
            for definition in definitions
        )
    )


# --- edges ------------------------------------------------------------------------


class MappedPdp(ScriptedPdp):
    """``ScriptedPdp`` with a per-resource verdict; identical recording surface."""

    def __init__(
        self,
        effects: Mapping[str, DecisionEffect],
        *,
        default: DecisionEffect = DecisionEffect.ALLOW,
        failure: Exception | None = None,
    ) -> None:
        super().__init__(default, failure=failure)
        self._effects = dict(effects)
        self._default = default

    def set_effect(self, resource: str, effect: DecisionEffect) -> None:
        self._effects[resource] = effect

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        self.effect = self._effects.get(request.resource, self._default)
        return await super().decide(request)


@dataclass
class MutableClock:
    current: datetime = NOW

    def __call__(self) -> datetime:
        return self.current


class TripwireAuthorize:
    """Fails loudly if a read-only preview reaches the submit-time authority."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **_kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("preflight must never call ModelSelectionService.authorize")


@dataclass
class Harness:
    preflight: AgentModelPreflightService
    selection: ModelSelectionService
    pdp: ScriptedPdp
    serving: StubServingStatus
    sink: PostgresAuditSink
    store: ModelRouteStore


@pytest.fixture
def audit_engine(tmp_path: Path) -> Iterator[sa.Engine]:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'agent-preflight.db'}")
    ensure_audit_schema(engine)
    ensure_model_route_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _harness(
    audit_engine: sa.Engine,
    *,
    definitions: Sequence[AgentManifestDefinition] | None = None,
    rows: Sequence[ServingRow] = (),
    pdp: ScriptedPdp | None = None,
    serving_failure: Exception | None = None,
    air_gap_provider: Callable[[], bool] = lambda: False,
    clock: Callable[[], datetime] = lambda: NOW,
    route_provider: Callable[[], Mapping[str, TaskRoute]] | None = None,
    settings: GatewaySettings = REGISTRY_SETTINGS,
    available: bool = True,
    availability_reasons: tuple[str, ...] = (),
    present: bool = True,
) -> Harness:
    """One PDP instance serves BOTH the preview and the submit-time authority.

    That is the production composition, and it is what lets a test flip the
    verdict between the two calls the way a real policy change would.
    """
    if definitions is None:
        definitions = tuple(_registry_definitions().values())
    sink = PostgresAuditSink(audit_engine)
    decision_point = ScriptedPdp() if pdp is None else pdp
    serving = StubServingStatus(rows, failure=serving_failure)
    selection = ModelSelectionService(
        gateway_settings=settings,
        serving_status=serving,
        audit_engine=audit_engine,
        pdp=decision_point,
        sink=sink,
        config_hash=CONFIG_HASH,
        air_gap_provider=air_gap_provider,
        clock=clock,
    )
    store = ModelRouteStore(audit_engine, settings)
    preflight = AgentModelPreflightService(
        agent_catalog=_catalog(
            *definitions,
            available=available,
            availability_reasons=availability_reasons,
            present=present,
        ),
        selection=selection,
        pdp=decision_point,
        route_provider=build_route_provider(store) if route_provider is None else route_provider,
    )
    return Harness(
        preflight=preflight,
        selection=selection,
        pdp=decision_point,
        serving=serving,
        sink=sink,
        store=store,
    )


def _preflight(harness: Harness, agent_id: str, principal: Principal) -> AgentModelPreflight | None:
    return run(harness.preflight.preflight(agent_id=agent_id, principal=principal))


def _required(harness: Harness, agent_id: str, principal: Principal) -> AgentModelPreflight:
    result = _preflight(harness, agent_id, principal)
    assert result is not None
    return result


def _models(result: AgentModelPreflight) -> list[str]:
    return [candidate.model for candidate in result.candidates]


def _selection_records(engine: sa.Engine) -> list[Any]:
    return list(list_records(engine, action=MODEL_SELECTION_EVIDENCE_ACTION, limit=1000))


def _serialized(result: AgentModelPreflight) -> str:
    return json.dumps(asdict(result), sort_keys=True, default=str)


def _repoint(harness: Harness, *, model: str) -> None:
    """Repoint the task through the REAL, approval-carrying production store."""
    freeze_config_hash(harness.store.boot_settings)
    try:
        harness.store.apply(
            task=ANALYST_POLICY,
            model=model,
            actor="user:ops",
            approver="user:approver",
            approval_id=None,
            via_action=action_for_lane(Lane.PRIVATE_VLLM),
        )
    finally:
        reset_config_hash_for_testing()


# --- 1. exact manifest facts --------------------------------------------------------


def test_analyst_and_hardship_preflights_carry_their_exact_registry_identity_and_budgets(
    audit_engine: sa.Engine,
) -> None:
    harness = _harness(audit_engine, rows=[_row()])

    analyst = _required(harness, ANALYST_ID, OPS)
    hardship = _required(harness, HARDSHIP_ID, OPS)

    # Version 15 / generation 26 (NE-SHAPE-ALLOWANCE-4): the token budget is
    # re-derived for the sixth output retry; the ceilings above it did not move.
    assert (analyst.agent_id, analyst.agent_version) == (ANALYST_ID, 16)
    assert analyst.definition_hash == ANALYST_DEFINITION_HASH
    assert analyst.task == ANALYST_POLICY
    assert analyst.allowed_strategies == ("agentic",)
    assert analyst.default_strategy == "agentic"
    assert (
        analyst.max_steps,
        analyst.max_tool_calls,
        analyst.max_input_tokens,
        analyst.max_output_tokens,
    ) == (40, 34, 3662637, 4000)

    assert (hardship.agent_id, hardship.agent_version) == (HARDSHIP_ID, 5)
    assert hardship.definition_hash == HARDSHIP_DEFINITION_HASH
    assert hardship.task == HARDSHIP_POLICY
    assert hardship.allowed_strategies == ("deterministic",)
    assert hardship.default_strategy == "deterministic"
    # The merged manifest's own field names, verbatim: 1 step, 100 tool calls,
    # the exact deterministic admission budget and a 1-token output budget.
    assert (
        hardship.max_steps,
        hardship.max_tool_calls,
        hardship.max_input_tokens,
        hardship.max_output_tokens,
    ) == (1, 100, 8210, 1)

    assert analyst.definition_hash != hardship.definition_hash
    assert analyst.definition_hash == _registry_definitions()[ANALYST_ID].definition_hash


def test_preflight_never_synthesizes_a_strategy_outside_the_agents_allowed_set(
    audit_engine: sa.Engine,
) -> None:
    harness = _harness(audit_engine, rows=[_row()])
    for agent_id in (ANALYST_ID, HARDSHIP_ID):
        result = _required(harness, agent_id, OPS)
        assert result.default_strategy in result.allowed_strategies
        assert set(result.allowed_strategies) <= {"agentic", "deterministic"}
        assert len(result.allowed_strategies) == 1


# --- 2. no existence oracle ---------------------------------------------------------


def test_hidden_agent_and_unknown_agent_id_both_return_none_without_pdp_or_serving_probe(
    audit_engine: sa.Engine,
) -> None:
    harness = _harness(audit_engine, rows=[_row()])

    hidden = _preflight(harness, HARDSHIP_ID, STAFF)
    unknown = _preflight(harness, UNKNOWN_ID, OPS)

    assert hidden is None
    assert unknown is None
    assert harness.pdp.requests == []
    assert harness.serving.seen == []
    assert _selection_records(audit_engine) == []

    # Control: the same harness DOES project an Agent this principal may see,
    # so the two ``None`` results above are visibility, not a dead service.
    visible = _required(harness, ANALYST_ID, STAFF)
    assert visible.agent_id == ANALYST_ID
    assert harness.serving.seen != []


def test_agent_whose_model_policy_names_an_unrouted_task_yields_no_candidates_or_probe(
    audit_engine: sa.Engine,
) -> None:
    definition = _definition_declaring("meridian-workflow")
    harness = _harness(audit_engine, definitions=[definition], rows=[_row()])

    result = _required(harness, SYNTHETIC_ID, STAFF)

    # The task is READ from the Agent, not assumed: a hard-coded "agent" would
    # project the routed catalog onto an Agent that declared something else.
    assert result.task == "meridian-workflow"
    assert result.agent_version == 3
    assert result.candidates == ()
    assert result.default_model is None
    assert harness.serving.seen == []
    assert harness.pdp.requests == []


# --- 1b. availability is REPORTED, never adjudicated --------------------------------


def test_unavailable_agent_still_projects_its_model_facts_with_its_exact_reasons(
    audit_engine: sa.Engine,
) -> None:
    """The boot composition ships BOTH seeded Agents ``available=False``.

    Which reasons they carry is a live fact, not a fixed one: a verified Skill
    pin MAY now be satisfied at composition, while ``build_platform`` still hands
    the catalog empty Knowledge and workflow-profile sets.  Either way the Agents
    remain unavailable, so refusing an unavailable Agent here would make this
    report return ``None`` for every Agent in the deployment.  The verdict is
    therefore carried, not acted on -- and it must be carried EXACTLY, since a
    caller cannot avoid presenting an unrunnable Agent as runnable on a verdict
    it was handed wrong.  Whether the caller uses it is not decided here.

    The reasons below are SYNTHETIC and deliberately unsorted.  That ordering is
    what makes a ``sorted()`` in the projection visible; it asserts nothing about
    the order real composition produces, and this test discriminates only that
    the tuple is projected verbatim.
    """
    reasons = ("skill-unavailable", "knowledge-unavailable")
    blocked = _harness(
        audit_engine,
        rows=[_row()],
        available=False,
        availability_reasons=reasons,
        # ``present`` stays TRUE and must differ from ``available``: the catalog
        # derives ``available = not reasons`` while ``present`` is the
        # manifest-shipped flag, and BOTH seeded Agents ship
        # ``present=True, available=False``.  Setting them alike here would let
        # ``available=agent.present`` report every unrunnable Agent as runnable.
        present=True,
    )

    result = _required(blocked, ANALYST_ID, OPS)

    assert result.available is False
    assert result.availability_reasons == reasons
    # Discriminating: the model facts are STILL reported, so this is "carried",
    # not "refused" -- and not an empty projection either.
    assert _models(result) == [PRIVATE.name]
    assert result.default_model == PRIVATE.name
    assert result.max_steps == 40

    healthy = _harness(audit_engine, rows=[_row()])
    available = _required(healthy, ANALYST_ID, OPS)
    assert available.available is True
    assert available.availability_reasons == ()


def test_agent_declaring_a_second_routed_policy_projects_that_tasks_route(
    audit_engine: sa.Engine,
) -> None:
    """Requirement 1, proven POSITIVELY: a routed non-"agent" policy id."""
    reviewer = _definition_declaring("meridian-review")
    harness = _harness(
        audit_engine,
        definitions=[reviewer, _registry_definitions()[ANALYST_ID]],
        rows=[_row(), _row(PRIVATE_PEER)],
        settings=MULTI_TASK_SETTINGS,
    )

    second = _required(harness, SYNTHETIC_ID, STAFF)
    assert second.task == "meridian-review"
    # The SECOND task's route, not the "agent" task's: a hard-coded literal
    # would report ``private-default`` here.
    assert second.default_model == PRIVATE_PEER.name

    # Control on the same deployment: the Analyst still projects its own task.
    analyst = _required(harness, ANALYST_ID, STAFF)
    assert analyst.task == ANALYST_POLICY
    assert analyst.default_model == PRIVATE.name


def test_candidate_provider_and_lane_are_plain_registry_strings(
    audit_engine: sa.Engine,
) -> None:
    harness = _harness(audit_engine, rows=[_row()])
    run(_observe(harness.sink))

    by_model = {item.model: item for item in _required(harness, ANALYST_ID, OPS).candidates}

    assert by_model[PRIVATE.name].provider == PRIVATE.provider == "hosted_vllm"
    assert by_model[FRONTIER.name].provider == FRONTIER.provider == "anthropic"
    # A plain ``str``, not the ``Lane`` member: the field's stated contract is
    # that a consumer need not import the enum to compare it.
    assert type(by_model[PRIVATE.name].lane) is str
    assert by_model[PRIVATE.name].lane == "private-vllm"
    assert by_model[FRONTIER.name].lane == "frontier-api"


# --- 3. principal-scoped, PDP-filtered candidates -----------------------------------


def test_staff_principal_is_refused_the_hardship_projection_ops_receives(
    audit_engine: sa.Engine,
) -> None:
    harness = _harness(audit_engine, rows=[_row()])

    assert _preflight(harness, HARDSHIP_ID, STAFF) is None
    assert _required(harness, HARDSHIP_ID, OPS).agent_id == HARDSHIP_ID
    # The Analyst is all-authenticated, so the two principals differ ONLY on the
    # group-scoped Agent, not on service availability.
    assert _required(harness, ANALYST_ID, STAFF).agent_id == ANALYST_ID


def test_deny_and_require_approval_candidates_are_absent_while_an_allowed_peer_survives(
    audit_engine: sa.Engine,
) -> None:
    harness = _harness(
        audit_engine,
        rows=[_row(), _row(PRIVATE_PEER)],
        pdp=MappedPdp(
            {
                f"model:{PRIVATE.name}": DecisionEffect.DENY,
                f"model:{FRONTIER.name}": DecisionEffect.REQUIRE_APPROVAL,
                f"model:{PRIVATE_PEER.name}": DecisionEffect.ALLOW,
            }
        ),
    )
    run(_observe(harness.sink))

    result = _required(harness, ANALYST_ID, OPS)

    assert _models(result) == [PRIVATE_PEER.name]
    # All three were asked, under the identical request shape ``authorize`` uses.
    assert [request.resource for request in harness.pdp.requests] == [
        f"model:{PRIVATE.name}",
        f"model:{PRIVATE_PEER.name}",
        f"model:{FRONTIER.name}",
    ]
    assert {request.action for request in harness.pdp.requests} == {MODEL_SELECTION_ACTION}
    assert dict(harness.pdp.requests[0].context) == {
        "task": ANALYST_POLICY,
        "lane": Lane.PRIVATE_VLLM.value,
        "air_gap": False,
    }
    # Withheld models are not named, tallied or explained anywhere.  Structural
    # rather than substring: the projection carries EXACTLY these fields, so a
    # refusal reason, effect or withheld-count cannot be added without failing
    # here, and no rewording of a reason string can evade it.
    assert set(asdict(result)) == PREFLIGHT_FIELDS
    assert set(asdict(result.candidates[0])) == CANDIDATE_FIELDS
    assert {candidate.model for candidate in result.candidates} == {PRIVATE_PEER.name}


# --- 4. fail the whole projection, never partially ----------------------------------


def test_pdp_error_private_probe_error_or_invalid_chain_raises_instead_of_partial_candidates(
    audit_engine: sa.Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy = _harness(audit_engine, rows=[_row(), _row(PRIVATE_PEER)])
    run(_observe(healthy.sink))
    # Control: with no fault injected this exact fixture yields three candidates,
    # so each raise below discriminates a fault from an empty deployment.
    assert _models(_required(healthy, ANALYST_ID, OPS)) == [
        PRIVATE.name,
        PRIVATE_PEER.name,
        FRONTIER.name,
    ]

    pdp_broken = _harness(
        audit_engine,
        rows=[_row(), _row(PRIVATE_PEER)],
        pdp=ScriptedPdp(failure=RuntimeError("OPA SENTINEL")),
    )
    with pytest.raises(ModelSelectionEvidenceError, match="policy is unavailable"):
        _preflight(pdp_broken, ANALYST_ID, OPS)

    probe_broken = _harness(
        audit_engine,
        rows=[_row()],
        serving_failure=RuntimeError("PROBE SENTINEL"),
    )
    with pytest.raises(ModelSelectionEvidenceError, match="private serving evidence"):
        _preflight(probe_broken, ANALYST_ID, OPS)

    chain_broken = _harness(audit_engine, rows=[_row()])

    class Invalid:
        valid = False

    # Pinned on the service's AnchoredChainVerifier verdict — the posterior
    # proof moved there (ne.audit.anchor); the assertion is unchanged.
    monkeypatch.setattr(chain_broken.selection._chain_verifier, "verify", lambda: Invalid())
    with pytest.raises(ModelSelectionEvidenceError, match="chain is invalid"):
        _preflight(chain_broken, ANALYST_ID, OPS)


def test_a_denied_candidate_does_not_stop_the_remaining_candidates_being_asked(
    audit_engine: sa.Engine,
) -> None:
    """The counterpart to failing whole: a DENY is a verdict, not a fault."""
    harness = _harness(
        audit_engine,
        rows=[_row(), _row(PRIVATE_PEER)],
        pdp=MappedPdp({f"model:{PRIVATE.name}": DecisionEffect.DENY}),
    )
    result = _required(harness, ANALYST_ID, OPS)
    assert _models(result) == [PRIVATE_PEER.name]
    assert len(harness.pdp.requests) == 2


# --- 5. private-lane evidence -------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        _row(state="unreachable", serves_configured_model=None),
        _row(serves_configured_model=False),
        _row(checked_at=NOW - timedelta(seconds=5, microseconds=1)),
        _row(checked_at=NOW + timedelta(seconds=5, microseconds=1)),
        _row(lane=Lane.FRONTIER_API.value),
        _row(model_id=SMALL.name),
    ],
    ids=["failed", "mismatch", "stale", "future", "wrong-lane", "wrong-alias"],
)
def test_private_candidate_absent_for_failed_mismatched_stale_future_or_misaddressed_row(
    audit_engine: sa.Engine,
    row: ServingRow,
) -> None:
    harness = _harness(audit_engine, rows=[row, _row(PRIVATE_PEER)])
    result = _required(harness, ANALYST_ID, OPS)
    # PRIVATE is the candidate each bad row is aimed at; its absence is the
    # property. The healthy sibling proves the projection ran and only the bad
    # row was cut.
    assert PRIVATE.name not in _models(result)
    assert PRIVATE_PEER.name in _models(result)
    # The "wrong-alias" row addresses SMALL, so since NE-CTX-IN-FORCE removed the
    # config-time budget floor that model is itself a legitimate candidate with a
    # current row — it is evidence FOR ITSELF, never for PRIVATE, which is exactly
    # what this case is testing.
    expected = [PRIVATE_PEER.name] + ([SMALL.name] if row.model_id == SMALL.name else [])
    assert sorted(_models(result)) == sorted(expected)
    assert result.default_model is None


# --- 6. frontier-lane evidence ------------------------------------------------------


@pytest.mark.parametrize(
    "observation",
    [
        {"actor": "unrelated-service"},
        {"config_hash": "b" * 64},
        {"lane": Lane.PRIVATE_VLLM.value},
        {"operation": "health-check"},
        {"operation": None},
        {"occurred_at": NOW - timedelta(seconds=60, microseconds=1)},
        {"alias": PRIVATE.name},
    ],
    ids=[
        "wrong-actor",
        "wrong-config-hash",
        "wrong-lane",
        "wrong-operation",
        "missing-operation",
        "stale",
        "wrong-alias",
    ],
)
def test_frontier_candidate_absent_for_wrong_actor_config_lane_operation_alias_or_stale_record(
    audit_engine: sa.Engine,
    observation: dict[str, Any],
) -> None:
    harness = _harness(audit_engine, rows=[_row()])
    run(_observe(harness.sink, **observation))
    result = _required(harness, ANALYST_ID, OPS)
    assert _models(result) == [PRIVATE.name]


def test_air_gap_removes_the_frontier_candidate_while_the_private_candidate_remains(
    audit_engine: sa.Engine,
) -> None:
    open_egress = _harness(audit_engine, rows=[_row()])
    run(_observe(open_egress.sink))
    assert _models(_required(open_egress, ANALYST_ID, OPS)) == [PRIVATE.name, FRONTIER.name]

    sealed = _harness(audit_engine, rows=[_row()], air_gap_provider=lambda: True)
    result = _required(sealed, ANALYST_ID, OPS)

    assert _models(result) == [PRIVATE.name]
    assert result.default_model == PRIVATE.name
    # The PDP saw the SAME posture the catalog was built from.
    assert all(request.context["air_gap"] is True for request in sealed.pdp.requests)


def test_pdp_context_air_gap_comes_from_the_single_read_that_built_the_catalog(
    audit_engine: sa.Engine,
) -> None:
    """The preview's policy question must describe the catalog it actually got.

    A second posture read would let the PDP be asked about a SEALED deployment
    for a catalog computed while egress was OPEN -- the frontier candidate would
    survive the availability rule and then be judged under the wrong posture.
    """
    reads: list[int] = []

    def flipping() -> bool:
        reads.append(len(reads))
        return len(reads) > 1  # egress OPEN on the first read, SEALED after

    harness = _harness(audit_engine, rows=[_row()], air_gap_provider=flipping)
    run(_observe(harness.sink))

    result = _required(harness, ANALYST_ID, OPS)

    assert _models(result) == [PRIVATE.name, FRONTIER.name]
    assert reads == [0]
    assert [request.context["air_gap"] for request in harness.pdp.requests] == [False, False]


# --- 7. the default follows the route in force --------------------------------------


def test_live_route_override_rather_than_the_boot_route_becomes_the_default_model(
    audit_engine: sa.Engine,
) -> None:
    harness = _harness(audit_engine, rows=[_row(), _row(PRIVATE_PEER)])
    assert _required(harness, ANALYST_ID, OPS).default_model == PRIVATE.name

    _repoint(harness, model=PRIVATE_PEER.name)

    result = _required(harness, ANALYST_ID, OPS)
    assert result.default_model == PRIVATE_PEER.name
    assert _models(result) == [PRIVATE.name, PRIVATE_PEER.name]


def test_unreadable_route_register_falls_back_to_the_boot_route_default(
    tmp_path: Path,
    audit_engine: sa.Engine,
) -> None:
    unreadable = sa.create_engine(f"sqlite:///{tmp_path / 'no-route-table.db'}")
    try:
        store = ModelRouteStore(unreadable, REGISTRY_SETTINGS)
        with pytest.raises(SQLAlchemyError):
            store.effective_routes()

        harness = _harness(
            audit_engine,
            rows=[_row(), _row(PRIVATE_PEER)],
            # The REAL provider over the broken store: its own fail-closed
            # behaviour is the thing under test, so it is not stubbed.
            route_provider=build_route_provider(store),
        )
        result = _required(harness, ANALYST_ID, OPS)
        assert result.default_model == REGISTRY_SETTINGS.tasks[ANALYST_POLICY].model == PRIVATE.name
    finally:
        unreadable.dispose()


def test_raising_route_provider_fails_the_whole_report_rather_than_omitting_a_default(
    audit_engine: sa.Engine,
) -> None:
    """The constructor's type is wider than ``build_route_provider``'s guarantee.

    The production provider never raises, so this has no production repro -- but
    an injected provider that does must not escape as a raw exception, and must
    not silently degrade to "this Agent has no default model".
    """

    def exploding() -> Mapping[str, TaskRoute]:
        raise RuntimeError("ROUTE REGISTER SENTINEL")

    harness = _harness(audit_engine, rows=[_row()], route_provider=exploding)

    with pytest.raises(ModelSelectionEvidenceError, match="route register is unavailable"):
        _preflight(harness, ANALYST_ID, OPS)


# --- 8. no substitute default -------------------------------------------------------


def test_denied_live_primary_yields_no_default_without_promoting_the_allowed_peer(
    audit_engine: sa.Engine,
) -> None:
    harness = _harness(
        audit_engine,
        rows=[_row(), _row(PRIVATE_PEER)],
        pdp=MappedPdp({f"model:{PRIVATE.name}": DecisionEffect.DENY}),
    )
    result = _required(harness, ANALYST_ID, OPS)
    assert result.default_model is None
    # Discriminating: candidates remain, so this is "no default", not "nothing".
    assert _models(result) == [PRIVATE_PEER.name]


def test_unhealthy_live_primary_yields_no_default_without_promoting_the_healthy_peer(
    audit_engine: sa.Engine,
) -> None:
    harness = _harness(
        audit_engine,
        rows=[
            _row(checked_at=NOW - timedelta(seconds=5, microseconds=1)),
            _row(PRIVATE_PEER),
        ],
    )
    result = _required(harness, ANALYST_ID, OPS)
    assert result.default_model is None
    assert _models(result) == [PRIVATE_PEER.name]


# --- 9. the preview decides nothing -------------------------------------------------


def test_preflight_appends_no_model_selection_evidence_and_never_calls_authorize(
    audit_engine: sa.Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(audit_engine, rows=[_row(), _row(PRIVATE_PEER)])
    tripwire = TripwireAuthorize()
    monkeypatch.setattr(harness.selection, "authorize", tripwire)

    result = _required(harness, ANALYST_ID, OPS)

    assert _models(result) == [PRIVATE.name, PRIVATE_PEER.name]
    assert tripwire.calls == 0
    # ``pdp.decide`` rows are legitimate governed reads and are NOT asserted away;
    # what a preview must never write is selection EVIDENCE.
    assert _selection_records(audit_engine) == []
    assert harness.pdp.requests != []


# --- 10. the submit-time gate is unchanged ------------------------------------------


def test_pdp_flipped_to_deny_after_a_preflight_still_refuses_authorize(
    audit_engine: sa.Engine,
) -> None:
    pdp = MappedPdp({})
    harness = _harness(audit_engine, rows=[_row()], pdp=pdp)
    assert _models(_required(harness, ANALYST_ID, OPS)) == [PRIVATE.name]

    pdp.set_effect(f"model:{PRIVATE.name}", DecisionEffect.DENY)

    with pytest.raises(ModelSelectionPolicyRefused):
        run(
            harness.selection.authorize(
                task=ANALYST_POLICY,
                requested_model=PRIVATE.name,
                principal=OPS,
                trace_id=TRACE_ID,
            )
        )


def test_serving_row_going_stale_after_a_preflight_still_refuses_authorize(
    audit_engine: sa.Engine,
) -> None:
    clock = MutableClock()
    harness = _harness(audit_engine, rows=[_row()], clock=clock)
    assert _models(_required(harness, ANALYST_ID, OPS)) == [PRIVATE.name]

    clock.current = NOW + timedelta(seconds=5, microseconds=1)

    with pytest.raises(ModelSelectionUnavailable, match="no current serving evidence"):
        run(
            harness.selection.authorize(
                task=ANALYST_POLICY,
                requested_model=PRIVATE.name,
                principal=OPS,
                trace_id=TRACE_ID,
            )
        )


def test_preflight_cache_boundary_does_not_false_refuse_a_healthy_auto_selection(
    audit_engine: sa.Engine,
) -> None:
    """A cached success must not straddle the selection-evidence boundary.

    Production shares one ``ServingStatusService`` between Agent preflight and
    Run admission.  The cache and model-selection evidence limit are both five
    seconds, but they read the clock separately.  Reproduce the real ordering:
    preflight primes the cache at t0; the admission cache read occurs one
    microsecond before expiry; selection evaluates that same row one
    microsecond after expiry.  Authorization must bypass that display cache for
    both automatic candidate resolution and the post-PDP evidence recheck.  The
    endpoint remains healthy throughout.
    """

    class BoundaryClock:
        def __init__(self) -> None:
            self.current = NOW
            self._script: list[datetime] = []

        def cross_five_second_boundary(self) -> None:
            self._script = [
                NOW + timedelta(seconds=5, microseconds=-1),
                NOW + timedelta(seconds=5, microseconds=1),
            ]

        def __call__(self) -> datetime:
            if self._script:
                self.current = self._script.pop(0)
            return self.current

    models_requests = 0

    def healthy_vllm(request: httpx.Request) -> httpx.Response:
        nonlocal models_requests
        if request.url.path == "/v1/models":
            models_requests += 1
            return httpx.Response(200, json={"data": [{"id": "qwen"}]})
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "test"})
        return httpx.Response(404)

    settings = GatewaySettings(
        models=[PRIVATE],
        tasks={ANALYST_POLICY: TaskRoute(model=PRIVATE.name)},
    )
    clock = BoundaryClock()
    serving = ServingStatusService(
        transport=httpx.MockTransport(healthy_vllm),
        clock=clock,
    )
    sink = PostgresAuditSink(audit_engine)
    pdp = ScriptedPdp()
    selection = ModelSelectionService(
        gateway_settings=settings,
        serving_status=serving,
        audit_engine=audit_engine,
        pdp=pdp,
        sink=sink,
        config_hash=CONFIG_HASH,
        air_gap_provider=lambda: False,
        clock=clock,
    )
    store = ModelRouteStore(audit_engine, settings)
    preflight = AgentModelPreflightService(
        agent_catalog=_catalog(_definition_declaring(ANALYST_POLICY)),
        selection=selection,
        pdp=pdp,
        route_provider=build_route_provider(store),
    )

    preview = run(preflight.preflight(agent_id=SYNTHETIC_ID, principal=OPS))
    assert preview is not None
    assert preview.default_model == PRIVATE.name
    assert models_requests == 1

    clock.cross_five_second_boundary()
    authorized = run(
        selection.authorize_auto(
            task=ANALYST_POLICY,
            principal=OPS,
            trace_id=TRACE_ID,
        )
    )

    assert authorized.model == PRIVATE.name
    assert authorized.observed_at > NOW
    assert models_requests == 3


# --- 11. determinism ----------------------------------------------------------------


def test_repeated_preflights_serialize_identically_in_gateway_registration_order(
    audit_engine: sa.Engine,
) -> None:
    harness = _harness(audit_engine, rows=[_row(), _row(PRIVATE_PEER)])
    run(_observe(harness.sink))

    first = _required(harness, ANALYST_ID, OPS)
    second = _required(harness, ANALYST_ID, OPS)

    assert _serialized(first) == _serialized(second)

    registration_order = [model.name for model in SETTINGS.models if model.name != SMALL.name]
    assert (
        _models(first)
        == registration_order
        == [
            PRIVATE.name,
            PRIVATE_PEER.name,
            FRONTIER.name,
        ]
    )
    # Discriminating: registration order is NOT alphabetical, so a hash/set or
    # sorted projection would produce a different list.
    assert _models(first) != sorted(registration_order)


def test_candidate_observed_at_is_the_evidence_timestamp_not_the_read_time(
    audit_engine: sa.Engine,
) -> None:
    observed = NOW - timedelta(seconds=3)
    harness = _harness(audit_engine, rows=[_row(checked_at=observed)])
    run(_observe(harness.sink, occurred_at=NOW - timedelta(seconds=30)))

    result = _required(harness, ANALYST_ID, OPS)

    by_model = {candidate.model: candidate for candidate in result.candidates}
    assert by_model[PRIVATE.name].observed_at == observed
    assert by_model[PRIVATE.name].evidence == "private-serving-probe"
    assert by_model[PRIVATE.name].lane == Lane.PRIVATE_VLLM.value
    assert by_model[FRONTIER.name].observed_at == NOW - timedelta(seconds=30)
    assert by_model[FRONTIER.name].evidence == "frontier-provider-attempt"
    assert by_model[FRONTIER.name].lane == Lane.FRONTIER_API.value


# --- the lane budget, reported without an arithmetic claim on top of it ----------


def test_each_candidate_carries_the_input_budget_the_gateway_enforces_against(
    audit_engine: sa.Engine,
) -> None:
    """The report used to name ``max_input_tokens`` and compare it to nothing.

    It still does not compare it -- deliberately. ``max_input_tokens`` is a
    WHOLE-TURN total spread over every request of a turn, so no serving window is
    ever asked to hold it and a comparison would be meaningless. What IS now
    reported is each candidate's own input budget: the exact quantity
    ``ne.gateway.gateway`` measures an estimated prompt against before calling a
    provider, read from the same ``GatewaySettings.context_budget_for``.

    An operator can therefore see the serving reality beside the declared budgets
    without this projection asserting a fit it cannot honestly compute -- the
    manifest budgets are in model tokens and enforcement is in
    ``ByteTokenEstimator`` tokens (pinned by ``test_agent_budget_units``).
    """
    harness = _harness(audit_engine, rows=[_row(PRIVATE), _row(PRIVATE_PEER)])
    run(_observe(harness.sink))

    result = _required(harness, ANALYST_ID, STAFF)
    assert _models(result), "this pin is vacuous without at least one candidate"

    settings = REGISTRY_SETTINGS
    by_name = {model.name: model for model in settings.models}
    for candidate in result.candidates:
        expected = settings.context_budget_for(by_name[candidate.model]).input_budget
        assert candidate.input_budget == expected
        assert candidate.input_budget is not None
        # Read, never retyped: it is window - reserve - margin, and positive.
        assert candidate.input_budget == (
            by_name[candidate.model].context_window
            - by_name[candidate.model].max_output_tokens
            - overhead_margin_for(by_name[candidate.model].context_window)
        )
        assert candidate.input_budget > 0

    # And no Agent-relative verdict rides along with it: the projection carries
    # the budget and nothing derived from the Agent's own declared numbers.
    assert not (PREFLIGHT_FIELDS & {"fits_declared_ceiling", "enforced_max_tool_calls"})
