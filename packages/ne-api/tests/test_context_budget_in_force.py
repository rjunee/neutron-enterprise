# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTX-IN-FORCE: the trim budget belongs to the model IN FORCE for the turn.

THE DEFECT. There was exactly ONE context-trim budget per deployment. The
composition root derived it at boot from the primary of ``RuntimeSettings.task``
(``ne.api.platform``) and it could not be re-derived live, so
``ne.api.model_routing`` refused any repoint/selection target whose input budget
fell below that boot primary's. The only way to keep a wide frontier lane and a
narrow private lane in one deployment was therefore to declare the WIDE model at
the NARROW model's window — which is what ``ne.cli.demo.live`` did. Measured
consequence on the sealed box: ``claude-sonnet-5`` (1M window) declared 32768 and,
after a 12288 output reserve sized against a 262144 GPU window, had **18841**
input tokens — NARROWER than the CPU lane's 27033. A real agentic run died
``context_exhausted`` at 18928 estimated tokens, 87 over, on a model serving 1M.

THE FIX, and what these tests pin. ``GatewayModel`` asks the gateway, per
request, for the budget of the model it is about to route to
(:class:`ne.contracts.models.TurnContextBudget`). That model is the governed
per-run selection when one is pinned, else the task's primary under the CURRENT
route table — the two things that can change without a restart. Every assertion
below is made against the REAL ``LiteLLMGateway`` route resolution, never a
re-implementation of it, and each blocking case sits beside its control
(docs/testing/vacuous-guards.md).

WHAT IS DELIBERATELY *NOT* CLAIMED: the guard is not weakened by the removal of
the routing floor. ``test_repointing_DOWN_narrows_the_guard_instead_of_bypassing_it``
is the load-bearing one — a repoint to a smaller model makes the guard STRICTER,
which is the property that let the floor be deleted at all.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from pydantic_ai.messages import (
    InstructionPart,
    ModelMessage,
    ModelRequest,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

from ne.contracts import Lane
from ne.contracts.errors import ContextBudgetExceededError
from ne.contracts.governance import RedactionResult
from ne.contracts.models import TurnContextBudget
from ne.gateway import GatewaySettings, LiteLLMGateway, ModelDef, TaskRoute
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import run_principal_scope, run_requested_model_scope
from ne.runtime.model import GatewayModel

from ne.contracts import freeze_config_hash, reset_config_hash_for_testing  # isort: skip

TASK = "agent"

#: The Run owner the receipt gate admits against.
OWNER = "owner-1"

#: The two lanes the defect is about: a wide external model and a narrow
#: internal one, in ONE deployment. Windows are the point, so they are the only
#: thing that differs.
NARROW = ModelDef(
    name="private-default",
    litellm_model="hosted_vllm/qwen",
    provider="hosted_vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://vllm:8000/v1",
    context_window=32768,
    max_output_tokens=12288,
    extra_params={"mock_response": "ok"},
)
WIDE = ModelDef(
    name="frontier-default",
    litellm_model="anthropic/claude-sonnet-5",
    provider="anthropic",
    lane=Lane.FRONTIER_API,
    context_window=1_000_000,
    max_output_tokens=12288,
    extra_params={"api_key": "test", "mock_response": "ok"},
)

#: A single un-droppable CURRENT turn of 28400 prompt bytes estimates at 18996
#: tokens — within 68 of the 18928 the owner's live run died at, and 155 over the
#: 18841 the capped frontier lane offered. One message on purpose: with no
#: droppable middle the guard can only FIT or REFUSE, so every assertion below is
#: about the budget rather than about how much history got dropped.
TURN_BYTES = 28_400


class _PassRedactor:
    def redact(self, text: str, *, categories: Any = None) -> RedactionResult:
        return RedactionResult(text=text)


class _CostSink:
    def emit(self, event: Any) -> None:
        return None


class _AuditSink:
    """Records the mandatory counts-only ``context.budget`` events (C10)."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, *, start: Any = None, end: Any = None) -> Any:
        from ne.contracts.governance import AuditVerification

        return AuditVerification(valid=True, checked=len(self.events))

    def details(self) -> list[dict[str, Any]]:
        return [json.loads(e.detail) for e in self.events]


async def _noop_hook(tool_name: str, args: Mapping[str, Any]) -> None:
    return None


def _settings(primary: str) -> GatewaySettings:
    return GatewaySettings(models=[NARROW, WIDE], tasks={TASK: TaskRoute(model=primary)})


class _MutableRoutes:
    """The live route register, in the shape the gateway actually reads it.

    ``LiteLLMGateway(route_provider=...)`` calls this on EVERY request — the same
    per-call seam ``ne.api.model_routing.build_route_provider`` fills from the
    durable governed register — so flipping ``model`` here is exactly what a
    governed repoint does to a running process.
    """

    def __init__(self, model: str) -> None:
        self.model = model

    def __call__(self) -> Mapping[str, TaskRoute]:
        return {TASK: TaskRoute(model=self.model)}


def _gateway(settings: GatewaySettings, routes: _MutableRoutes | None = None) -> LiteLLMGateway:
    return LiteLLMGateway(
        settings,
        _CostSink(),
        redactor=_PassRedactor(),
        route_provider=routes,
    )


def _turn(user_bytes: int = TURN_BYTES) -> list[ModelMessage]:
    """The current turn alone — structurally un-trimmable (design §3.5-2)."""
    return [ModelRequest(parts=[UserPromptPart(content="CURRENT " + "c" * user_bytes)])]


@pytest.fixture(autouse=True)
def _frozen_config_hash() -> Iterator[None]:
    """Every ``context.budget`` audit event stamps the frozen hash (C10)."""
    reset_config_hash_for_testing()
    freeze_config_hash(RuntimeSettings())
    yield
    reset_config_hash_for_testing()


PARAMS = ModelRequestParameters(
    function_tools=[], instruction_parts=[InstructionPart(content="INSTR")]
)


def _model(
    gateway: LiteLLMGateway, boot: GatewaySettings, sink: _AuditSink, primary: str
) -> GatewayModel:
    """A ``GatewayModel`` composed exactly as ``build_platform`` composes it.

    ``budget=`` is the BOOT primary's — the value the platform freezes into
    ``RuntimeSettings`` and stamps into ``config_hash``. Every test below leaves
    it alone; what changes is the model in force.
    """
    budget = boot.context_budget_for(next(m for m in boot.models if m.name == primary))
    rt = RuntimeSettings(
        task=TASK,
        bytes_per_token=boot.bytes_per_token,
        per_call_overhead=boot.per_call_overhead,
        context_budget=budget,
    )
    return GatewayModel(gateway, rt, on_unknown_tool=_noop_hook, budget=budget, sink=sink)


def _run(model: GatewayModel, messages: list[ModelMessage]) -> None:
    async def _go() -> None:
        await model.request(list(messages), None, PARAMS)

    asyncio.run(_go())


def _resolve(gateway: Any, task: str, **kw: Any) -> Any:
    """One await of the (round-4 async) capability, from sync test bodies."""
    return asyncio.run(gateway.context_budget_for_turn(task, **kw))


# --- the capability itself --------------------------------------------------


def test_the_real_gateway_answers_with_the_in_force_models_budget() -> None:
    """``LiteLLMGateway`` satisfies :class:`TurnContextBudget`, and its answer
    tracks BOTH live inputs: the route register and the per-run selection."""
    boot = _settings(NARROW.name)
    routes = _MutableRoutes(NARROW.name)
    gw = _gateway(boot, routes)
    assert isinstance(gw, TurnContextBudget)

    resolved = _resolve(gw, TASK)
    assert resolved is not None and resolved.model == NARROW.name
    assert resolved.budget == boot.context_budget_for(NARROW)
    # (a) a governed per-run SELECTION.
    selected = _resolve(gw, TASK, requested_model=WIDE.name)
    assert selected is not None and selected.model == WIDE.name
    assert selected.budget == boot.context_budget_for(WIDE)
    # (b) a governed REPOINT of the task itself.
    routes.model = WIDE.name
    repointed = _resolve(gw, TASK)
    assert repointed is not None and repointed.model == WIDE.name
    assert repointed.budget == boot.context_budget_for(WIDE)

    # It names nothing it cannot resolve, and it never raises: the unroutable
    # task and the unregistered model are ``complete``'s typed errors to report.
    assert _resolve(gw, "no-such-task") is None
    assert _resolve(gw, TASK, requested_model="no-such-model") is None


# --- the defect, at the layer that enforces it ------------------------------


def test_a_19k_token_turn_fits_a_wide_lane_that_a_boot_narrow_budget_refused() -> None:
    """THE OWNER'S FAILURE, reproduced and then fixed, on the same turn.

    A turn a 1M-window model can trivially hold used to be refused because the
    deployment's single budget had been derived from a 32k boot primary. Both
    halves are asserted against the SAME messages and the SAME
    ``GatewayModel``-composed-from-boot, so the only variable is which model is
    in force.
    """
    boot = _settings(NARROW.name)
    routes = _MutableRoutes(NARROW.name)
    gw = _gateway(boot, routes)
    messages = _turn()

    # The exact inversion the cap produced, in the emitter's own arithmetic: the
    # 1M model's budget is ~50x the 32k model's, and the turn sits between them.
    assert boot.context_budget_for(NARROW).input_budget == 18841
    assert boot.context_budget_for(WIDE).input_budget == 937_712

    # BEFORE (the control): the narrow model really is in force, and the guard
    # really does refuse — this is not a vacuous pass.
    sink = _AuditSink()
    with pytest.raises(ContextBudgetExceededError) as refused:
        _run(_model(gw, boot, sink, NARROW.name), messages)
    # ``stage`` matters: the gateway has its OWN per-candidate budget pre-flight,
    # and a refusal from THERE would prove nothing about the runtime trim guard.
    assert refused.value.stage == "runtime-trim"
    assert refused.value.context_window == NARROW.context_window
    assert refused.value.input_budget == 18841
    # Same shape as the owner's live failure: a hair over, on a model that
    # serves 1M. 18928 was the measured estimate; 18996 is this fixture's.
    assert 18_841 < refused.value.estimated_tokens < 19_200
    assert sink.details()[-1]["context_window"] == NARROW.context_window

    # AFTER: one governed repoint, no restart, no re-composition — the SAME
    # GatewayModel construction (still carrying the boot-narrow budget) now
    # measures the turn against the model actually serving it.
    routes.model = WIDE.name
    sink = _AuditSink()
    _run(_model(gw, boot, sink, NARROW.name), messages)
    assert sink.details() == [], "a turn that fits must not trim and must not refuse"


def test_a_per_run_selection_moves_the_guard_without_touching_the_route() -> None:
    """The other live input: ``current_run_requested_model()``.

    The route register is untouched here — only the governed per-run selection
    scope — so this pins the selection path independently of the repoint path.
    """
    boot = _settings(NARROW.name)
    routes = _MutableRoutes(NARROW.name)
    gw = _gateway(boot, routes)
    messages = _turn()

    sink = _AuditSink()
    with run_requested_model_scope(WIDE.name, lane=Lane.FRONTIER_API):
        _run(_model(gw, boot, sink, NARROW.name), messages)
    assert sink.details() == []

    # CONTROL — outside the scope the very same turn is refused again, so the
    # scope is what moved the budget rather than something ambient.
    sink = _AuditSink()
    with pytest.raises(ContextBudgetExceededError) as refused:
        _run(_model(gw, boot, sink, NARROW.name), messages)
    assert refused.value.stage == "runtime-trim"
    assert refused.value.context_window == NARROW.context_window


def test_repointing_DOWN_narrows_the_guard_instead_of_bypassing_it() -> None:
    """WHY the routing input-budget floor could be deleted (``ne.api.model_routing``).

    The floor refused a target narrower than the boot primary because the guard
    would otherwise have passed a prompt the new model could not hold. That is
    now structurally impossible: repointing DOWN makes the guard STRICTER, and
    the refusal names the NEW model's window, not the boot one's.

    Without this property the floor's removal would be a silent regression — a
    conversation exceeding the guard — which is exactly the outcome this test
    exists to make loud.
    """
    boot = _settings(WIDE.name)  # boot primary is the WIDE lane this time
    routes = _MutableRoutes(WIDE.name)
    gw = _gateway(boot, routes)
    messages = _turn()

    # CONTROL — under the boot-wide primary the turn sails through.
    sink = _AuditSink()
    _run(_model(gw, boot, sink, WIDE.name), messages)
    assert sink.details() == []

    # The repoint the old floor forbade. The composed budget is still the WIDE
    # one; the guard tightens anyway.
    routes.model = NARROW.name
    sink = _AuditSink()
    with pytest.raises(ContextBudgetExceededError) as refused:
        _run(_model(gw, boot, sink, WIDE.name), messages)
    # ``runtime-trim``, not the gateway's own pre-flight: the LAYER-2 guard is
    # what had to learn the new window. Dropping the stage assertion would let a
    # gateway-side refusal pass this test and hide a regression here.
    assert refused.value.stage == "runtime-trim"
    assert refused.value.context_window == NARROW.context_window
    assert sink.details()[-1]["context_window"] == NARROW.context_window


def test_a_gateway_without_the_capability_keeps_the_composed_budget() -> None:
    """The fallback is the PREVIOUS behaviour exactly, never "no budget".

    A wrapping decorator or a test double that does not implement
    ``TurnContextBudget`` leaves the composed boot budget in force — the guard
    stays ARMED in every configuration it was armed in before. Only WHICH window
    it is armed at can vary.
    """
    from .conftest import ScriptedGateway

    boot = _settings(NARROW.name)
    plain = ScriptedGateway()
    assert not isinstance(plain, TurnContextBudget)

    sink = _AuditSink()
    with pytest.raises(ContextBudgetExceededError) as refused:
        _run(_model(plain, boot, sink, NARROW.name), _turn())  # type: ignore[arg-type]
    assert refused.value.stage == "runtime-trim"
    assert refused.value.context_window == NARROW.context_window


# --- cross-process (SPEC §6.2/Q5) -------------------------------------------


def test_ne_api_and_ne_worker_derive_identical_per_turn_budgets() -> None:
    """§6.2/Q5 parity, at the only place the number is now computed.

    ne-api and ne-worker run ONE composition recipe over the same env and read
    ONE route register on the shared audit database. The per-turn budget is a
    pure function of exactly those two things — the frozen ``GatewaySettings``
    (already hashed under 'ne-gateway', windows and reserves included) and the
    in-force model NAME — so two independently composed processes cannot
    disagree. Modelled here as two gateways built separately from the same env
    and sharing one register, and checked across a repoint so the agreement is
    not merely an agreement about the boot state.
    """
    env = json.loads(json.dumps([m.model_dump(mode="json") for m in (NARROW, WIDE)]))
    routes = _MutableRoutes(NARROW.name)

    def compose() -> LiteLLMGateway:
        settings = GatewaySettings(
            models=[ModelDef.model_validate(m) for m in env],
            tasks={TASK: TaskRoute(model=NARROW.name)},
        )
        return _gateway(settings, routes)

    api, worker = compose(), compose()
    for model in (None, NARROW.name, WIDE.name):
        assert _resolve(api, TASK, requested_model=model) == _resolve(
            worker, TASK, requested_model=model
        )

    routes.model = WIDE.name
    assert _resolve(api, TASK) == _resolve(worker, TASK)
    assert _resolve(api, TASK) is not None


def test_the_boot_budget_is_still_frozen_into_the_config_hash() -> None:
    """The hash stays honest: it exists so two processes can PROVE they agree.

    ``RuntimeSettings.context_budget`` remains a hashed field carrying the
    composed boot budget — unchanged preimage, unchanged drift tripwire. What
    NE-CTX-IN-FORCE changed is that the ENFORCED number is re-derived from
    already-hashed inputs plus a governed, audited model name, not that the hash
    stopped covering anything.
    """
    boot = _settings(NARROW.name)
    budget = boot.context_budget_for(NARROW)
    reset_config_hash_for_testing()
    with_budget = freeze_config_hash(RuntimeSettings(task=TASK, context_budget=budget))
    reset_config_hash_for_testing()
    without = freeze_config_hash(RuntimeSettings(task=TASK))
    assert with_budget != without


# --- the capability must SURVIVE the wrappers (the inert-mechanism defect) ----


def test_the_boot_budget_is_the_deployments_WIDEST_when_the_default_task_is_frontier() -> None:
    """WHY a lost capability is not a safe failure any more.

    ``ne.cli.demo.live`` routes ``tasks["agent"] = frontier-default`` with no
    fallback, and NE-CTX-IN-FORCE uncapped that row to the window its model
    really serves. So the budget the composition root derives at boot — the one
    every ``_turn_budget`` fallback lands on — went from the deployment's
    NARROWEST to its WIDEST. Before, a fallback was narrower than everything and
    safe in every direction by construction; now it is wider than the private
    lane by a factor of 50. That is what makes a dropped capability a live
    defect rather than a missed optimisation, and it is asserted here rather
    than asserted in prose.
    """
    boot = _settings(WIDE.name)
    assert boot.context_budget_for(WIDE).input_budget == 937_712
    assert boot.context_budget_for(NARROW).input_budget == 18_841


def test_a_gateway_without_the_capability_keeps_a_boot_budget_WIDER_than_the_in_force_model() -> (
    None
):
    """THE INVERTED DIRECTION — the one that actually bites.

    Its sibling above composes with a NARROW boot primary, so a lost capability
    merely refuses a turn that would have fit: annoying, not unsafe, and a test
    that only covers that direction passes on the broken build. Invert it — boot
    WIDE, in force NARROW, capability absent — and the guard measures a turn the
    serving model cannot hold against a window 50x too large, waves it through,
    and the refusal lands at the gateway's own pre-flight instead of at the
    Layer-2 guard that was supposed to TRIM it.

    Pinned as the documented fallback, not as an aspiration: this is what a
    genuine test double still does. The point of the assertion is the STAGE — it
    is the tripwire for any wrapper that silently drops the capability, because
    a wrapper that carries it produces ``runtime-trim`` here instead.
    """
    from .conftest import ScriptedGateway

    boot = _settings(WIDE.name)
    plain = ScriptedGateway()
    assert not isinstance(plain, TurnContextBudget)

    # No refusal at the Layer-2 guard at all: the boot-WIDE budget swallows a
    # turn that ``private-default`` could never hold, and nothing was trimmed.
    sink = _AuditSink()
    _run(_model(plain, boot, sink, WIDE.name), _turn())  # type: ignore[arg-type]
    assert sink.details() == []

    # And the CONTROL that makes the number mean something: the very same turn
    # against the same model's REAL window is over budget by the guard's own
    # arithmetic. So what the paragraph above describes is a real overshoot,
    # not a turn that happened to fit anyway.
    narrow_budget = boot.context_budget_for(NARROW)
    assert narrow_budget.input_budget == 18_841
    with pytest.raises(ContextBudgetExceededError):
        _run(
            _model(plain, _settings(NARROW.name), _AuditSink(), NARROW.name),  # type: ignore[arg-type]
            _turn(),
        )


class _ReceiptSink:
    """The pre-effect Run receipt the gate persists before any model effect."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)


class _ReceiptAudit:
    def __init__(self) -> None:
        self.sink = _ReceiptSink()

    def for_target(self, **_kwargs: Any) -> _ReceiptSink:
        return self.sink


def _receipt_gated(inner: Any, owner: str = OWNER) -> Any:
    """The wrapper that sits on EVERY canonical Run's model effect."""
    from ne.runtime.run_runner import ReceiptGatedGateway

    return ReceiptGatedGateway(
        inner,
        audit=_ReceiptAudit(),  # type: ignore[arg-type]
        owner_principal_id=owner,
        step_id="run-model",
    )


def test_the_receipt_gate_forwards_the_capability_so_the_guard_is_not_inert() -> None:
    """THE DEFECT: ``TurnContextBudget`` is a method-presence Protocol.

    ``GatewayModel._turn_budget`` gates on ``isinstance(self._gateway,
    TurnContextBudget)``. ``ReceiptGatedGateway`` is unconditionally interposed
    on every Run model effect (``ne.runtime.run_workflow``), so a wrapper
    without the method made ``isinstance`` False and the whole mechanism a
    no-op on exactly the path the owner's ``context_exhausted`` incident used —
    silently, because the fallback is the documented, quiet behaviour.

    Driven in the DANGEROUS direction (boot WIDE, in force NARROW) so a
    regression cannot pass by refusing for the wrong reason: the assertion is
    that the refusal comes from the Layer-2 guard naming the NARROW window. With
    the capability dropped this same turn reaches the gateway instead.
    """
    boot = _settings(WIDE.name)
    routes = _MutableRoutes(WIDE.name)
    gated = _receipt_gated(_gateway(boot, routes))
    assert isinstance(gated, TurnContextBudget)

    # CONTROL — under the boot-wide primary the turn sails THROUGH the gate: it
    # is admitted, its pre-effect receipt is written, and the model is called.
    # Without this leg the test could pass on a build where the guard refuses
    # everything.
    sink = _AuditSink()
    with run_principal_scope(OWNER):
        _run(_model(gated, boot, sink, WIDE.name), _turn())  # type: ignore[arg-type]
    assert sink.details() == []

    routes.model = NARROW.name
    sink = _AuditSink()
    with pytest.raises(ContextBudgetExceededError) as refused, run_principal_scope(OWNER):
        _run(_model(gated, boot, sink, WIDE.name), _turn())  # type: ignore[arg-type]
    assert refused.value.stage == "runtime-trim"
    assert refused.value.context_window == NARROW.context_window
    assert sink.details()[-1]["context_window"] == NARROW.context_window


def test_the_demo_hallucination_seam_forwards_the_capability_too() -> None:
    """The other process-wide wrapper, and the reason it matters.

    ``DemoGroundedHallucinationGateway`` wraps the COMPOSED gateway for the
    whole process when its flag is on, and promises unmarked traffic is
    delegated byte-transparently. A dropped capability would make that flag
    change the enforced context window of every unmarked turn in the deployment.
    """
    from ne.api.demo_seams import DemoGroundedHallucinationGateway

    boot = _settings(NARROW.name)
    routes = _MutableRoutes(NARROW.name)
    seam = DemoGroundedHallucinationGateway(_gateway(boot, routes))
    assert isinstance(seam, TurnContextBudget)
    before = _resolve(seam, TASK)
    assert before is not None and before.model == NARROW.name
    assert before.budget == boot.context_budget_for(NARROW)
    routes.model = WIDE.name
    after = _resolve(seam, TASK)
    assert after is not None and after.model == WIDE.name
    assert after.budget == boot.context_budget_for(WIDE)

    # Stacked exactly as production stacks them, and still capable.
    assert isinstance(_receipt_gated(seam), TurnContextBudget)


def test_a_wrapper_over_a_capability_less_gateway_answers_None_not_a_guess() -> None:
    """The forward is a FORWARD: it never invents a budget the inner cannot name.

    ``None`` sends ``_turn_budget`` to the composed default, which is the
    documented fallback. Inventing one here would let a wrapper widen a guard
    its inner gateway never agreed to.
    """
    from .conftest import ScriptedGateway

    gated = _receipt_gated(ScriptedGateway())
    assert _resolve(gated, TASK) is None
    assert _resolve(gated, TASK, requested_model=WIDE.name) is None


# --- the lane pin (codex P1, round 3 of resolver-vs-attempt divergence) ------


def _lane_pinned_settings() -> GatewaySettings:
    """The air-gap beat's route shape: a wide frontier PRIMARY whose one
    fallback is the narrow private lane."""
    return GatewaySettings(
        models=[NARROW, WIDE],
        tasks={TASK: TaskRoute(model=WIDE.name, fallbacks=(NARROW.name,))},
    )


def test_a_lane_pin_budgets_the_first_candidate_the_pin_actually_yields() -> None:
    """THE FINDING: ``context_budget_for_turn`` budgeted the route PRIMARY, but
    ``_candidates`` filters candidates by the request's lane pin BEFORE the
    first attempt — so with the private lane pinned the frontier primary is not
    "a fallback deliberately not considered" (design §2.2's secondary-roll
    caveat), it is NOT A CANDIDATE AT ALL, and the budget named a model that
    would never be called. Every assertion is parity with the REAL
    ``_candidates`` rule, never a re-implementation of it.
    """
    boot = _lane_pinned_settings()
    gw = _gateway(boot)

    # The pin excludes the primary entirely: budget the candidate actually
    # attempted first, which is the NARROW private fallback.
    pinned = _resolve(gw, TASK, lane=Lane.PRIVATE_VLLM)
    assert pinned is not None and pinned.model == NARROW.name
    assert pinned.budget == boot.context_budget_for(NARROW)
    # CONTROLS — no pin, and a pin the primary matches, both name the primary.
    unpinned = _resolve(gw, TASK)
    assert unpinned is not None and unpinned.model == WIDE.name
    assert unpinned.budget == boot.context_budget_for(WIDE)
    frontier_pin = _resolve(gw, TASK, lane=Lane.FRONTIER_API)
    assert frontier_pin is not None and frontier_pin.model == WIDE.name
    assert frontier_pin.budget == boot.context_budget_for(WIDE)
    # An EXPLICIT model off the pinned lane is _candidates' BD-15 refusal
    # (LaneMismatchError), never a quiet swap to a matching fallback — so there
    # is no model to budget, and None keeps the composed default armed.
    assert _resolve(gw, TASK, requested_model=WIDE.name, lane=Lane.PRIVATE_VLLM) is None
    # A pin NOTHING matches: None, never a raise (complete raises, audited).
    solo = GatewaySettings(models=[WIDE], tasks={TASK: TaskRoute(model=WIDE.name)})
    assert _resolve(_gateway(solo), TASK, lane=Lane.PRIVATE_VLLM) is None


def test_a_pinned_private_lane_trims_to_the_private_window_and_serves() -> None:
    """END TO END, the user-visible symptom on the demo path: the air-gap beat
    pins ``RuntimeSettings.lane`` to the private lane while the route primary is
    the wide frontier model. Before the fix ``_turn_budget`` sized the trim at
    the frontier window (937712 input tokens), dropped NOTHING, and the
    gateway's per-candidate pre-flight refused the turn its only real candidate
    could not hold — a ``gateway-preflight`` ContextBudgetExceededError instead
    of a served, trimmed turn. Now the guard trims at the private window and the
    private lane serves.
    """
    from pydantic_ai.messages import ModelResponse, TextPart

    boot = _lane_pinned_settings()
    gw = _gateway(boot)
    # Exactly what build_platform composes at boot: the route PRIMARY's budget.
    budget = boot.context_budget_for(WIDE)
    rt = RuntimeSettings(
        task=TASK,
        bytes_per_token=boot.bytes_per_token,
        per_call_overhead=boot.per_call_overhead,
        context_budget=budget,
        lane=Lane.PRIVATE_VLLM.value,
    )
    sink = _AuditSink()
    model = GatewayModel(gw, rt, on_unknown_tool=_noop_hook, budget=budget, sink=sink)

    # One droppable prior turn over the private input budget + a small current
    # turn: fits the private window ONLY after the old turn is dropped.
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="TURN1 " + "h" * TURN_BYTES)]),
        ModelResponse(parts=[TextPart(content="ans1")]),
        ModelRequest(parts=[UserPromptPart(content="CURRENT")]),
    ]
    _run(model, messages)

    # The trim HAPPENED, at the PRIVATE window — the budget followed the pin.
    detail = sink.details()[-1]
    assert detail["context_window"] == NARROW.context_window
    assert detail["dropped_turns"] == 1
