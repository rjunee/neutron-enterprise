# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-014C per-effect policy recheck and explicit/auto fallback truth."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from ne.api.model_selection import (
    AuthorizedModelSelection,
    ModelChoice,
    ModelChoiceCatalog,
)
from ne.contracts.config import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.errors import ContextBudgetExceededError
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.types import ChatMessage, Lane, Principal
from ne.gateway import GatewaySettings, LiteLLMGateway, ModelDef, TaskRoute
from ne.gateway.errors import ModelCallFailedError
from ne.runtime.run_runner import RunExecutionContext
from ne.worker.run_execution import _RunContinuityGateway

from .test_profile_bound_run_execution import _context

_POLICY = "agent.meridian-analyst.v2"
_DEFINITION_HASH = "d" * 64
_AUTHORIZATION_HASH = "a" * 64
_NOW = datetime(2026, 8, 23, tzinfo=UTC)


def _continuity_context(
    *, requested_model: str | None, authorized_model: str | None = None
) -> RunExecutionContext:
    """``authorized_model`` overrides the ADMISSION-frozen model of an unpinned
    run (its lane is derived from the name); the default stays the private lane
    so every pre-existing caller keeps its exact context."""
    lane = (
        None
        if requested_model is None
        else Lane.PRIVATE_VLLM.value
        if requested_model == "private-default"
        else Lane.FRONTIER_API.value
    )
    context = _context(
        strategy="agentic",
        version=2,
        agent_id="agt_agentic",
        requested_model=requested_model,
        requested_model_lane=lane,
    )
    frozen = (
        authorized_model
        if authorized_model is not None
        else ("private-default" if requested_model is None else requested_model)
    )
    frozen_lane = (
        Lane.PRIVATE_VLLM.value if frozen == "private-default" else Lane.FRONTIER_API.value
    )
    return replace(
        context,
        run=replace(
            context.run,
            agent_definition_hash=_DEFINITION_HASH,
            model_policy_id=_POLICY,
            authorized_model=frozen,
            authorized_model_lane=frozen_lane,
            model_authorization_receipt_hash=_AUTHORIZATION_HASH,
            context_snapshot_hash="c" * 64,
        ),
    )


class _Catalog:
    def __init__(self) -> None:
        self.definition_hash = _DEFINITION_HASH
        self.calls = 0

    def get_visible(self, agent_id: str, _principal: Principal) -> Any:
        self.calls += 1
        return SimpleNamespace(
            present=True,
            available=True,
            definition=SimpleNamespace(
                agent_id=agent_id,
                version=2,
                definition_hash=self.definition_hash,
                model_policy_id=_POLICY,
            ),
        )


class _Selection:
    def __init__(self) -> None:
        self.authorizations: list[str] = []

    async def choices(self, task: str) -> ModelChoiceCatalog:
        assert task == _POLICY
        return ModelChoiceCatalog(
            task=task,
            effective_default="private-default",
            choices=(
                ModelChoice(
                    model="private-default",
                    provider="hosted_vllm",
                    lane=Lane.PRIVATE_VLLM,
                    evidence="private-serving-probe",
                    observed_at=_NOW,
                ),
                ModelChoice(
                    model="frontier-default",
                    provider="anthropic",
                    lane=Lane.FRONTIER_API,
                    evidence="frontier-provider-attempt",
                    observed_at=_NOW,
                ),
            ),
        )

    async def authorize(self, *, requested_model: str, **_kwargs: Any) -> AuthorizedModelSelection:
        self.authorizations.append(requested_model)
        lane = Lane.FRONTIER_API if requested_model == "frontier-default" else Lane.PRIVATE_VLLM
        return AuthorizedModelSelection(
            model=requested_model,
            lane=lane,
            observed_at=_NOW,
            policy_effect="allow",
            policy_version="policy-v1",
        )


class _RunStore:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    async def record_model_step(self, **values: Any) -> None:
        self.steps.append(values)

    async def verify_model_boundary(self, **_values: Any) -> None:
        return None


class _Gateway:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = set() if failures is None else failures
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        model = str(request.requested_model)
        if model in self.failures:
            raise ModelCallFailedError(((model, RuntimeError("provider unavailable")),))
        return Completion(
            text="served",
            model_id=model,
            usage=Usage(prompt_tokens=1, completion_tokens=1),
        )

    def stream(self, request: CompletionRequest) -> Any:
        del request
        raise NotImplementedError


class _CostSink:
    def emit(self, event: Any) -> None:
        del event
        return None


def _request(*, messages: tuple[ChatMessage, ...] = ()) -> CompletionRequest:
    return CompletionRequest(messages=messages, task=_POLICY, principal_id="owner-1")


def test_explicit_failure_never_falls_back_and_definition_drift_makes_zero_calls() -> None:
    selection = _Selection()
    catalog = _Catalog()
    store = _RunStore()
    inner = _Gateway({"frontier-default"})
    gateway = _RunContinuityGateway(
        inner,
        context=_continuity_context(requested_model="frontier-default"),
        principal=Principal(id="owner-1"),
        catalog=catalog,
        selection=selection,  # type: ignore[arg-type]
        run_store=store,
    )
    with pytest.raises(ModelCallFailedError):
        asyncio.run(gateway.complete(_request()))
    assert [request.requested_model for request in inner.requests] == ["frontier-default"]
    assert selection.authorizations == ["frontier-default"]
    assert store.steps == []

    catalog.definition_hash = "e" * 64
    with pytest.raises(RuntimeError, match="drifted before model effect"):
        asyncio.run(gateway.complete(_request()))
    assert len(inner.requests) == 1


def test_auto_reauthorizes_each_candidate_and_records_exact_requested_served_truth() -> None:
    selection = _Selection()
    catalog = _Catalog()
    store = _RunStore()
    inner = _Gateway({"private-default"})
    gateway = _RunContinuityGateway(
        inner,
        context=_continuity_context(requested_model=None),
        principal=Principal(id="owner-1"),
        catalog=catalog,
        selection=selection,  # type: ignore[arg-type]
        run_store=store,
    )
    completion = asyncio.run(gateway.complete(_request()))

    assert completion.model_id == "frontier-default"
    assert selection.authorizations == ["private-default", "frontier-default"]
    assert [request.requested_model for request in inner.requests] == [
        "private-default",
        "frontier-default",
    ]
    assert store.steps == [
        {
            "run_id": "run-1",
            "attempt_id": "attempt-1",
            "ordinal": 0,
            "model_policy_id": _POLICY,
            "request_mode": "auto",
            "requested_model": None,
            "served_model": "frontier-default",
            "served_lane": Lane.FRONTIER_API.value,
            # THE CHAIN MOVED, AND THE STEP SAYS SO. `private-default` was
            # reached for first and did not answer; recording only
            # `served_model` here would leave an involuntary provider fallback
            # indistinguishable from a route whose first attempt worked.
            "route_position": "fallback",
            "fallback_from": "private-default",
            "authorization_receipt_hash": _AUTHORIZATION_HASH,
        }
    ]
    # Initial Run/request validation plus one exact recheck before each provider effect.
    assert catalog.calls == 3


def test_auto_records_primary_when_the_first_candidate_attempted_serves() -> None:
    """`primary` is a POSITIVE claim and has to be earned by an actual attempt."""
    selection = _Selection()
    catalog = _Catalog()
    store = _RunStore()
    inner = _Gateway(set())
    gateway = _RunContinuityGateway(
        inner,
        context=_continuity_context(requested_model=None),
        principal=Principal(id="owner-1"),
        catalog=catalog,
        selection=selection,  # type: ignore[arg-type]
        run_store=store,
    )
    completion = asyncio.run(gateway.complete(_request()))

    assert completion.model_id == "private-default"
    assert store.steps[0]["route_position"] == "primary"
    # Omitted rather than emptied: an empty string would read as a name.
    assert store.steps[0]["fallback_from"] is None


def test_real_gateway_receives_exact_cross_run_sentinel_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker guard preserves Run A history at the real gateway boundary."""
    sentinel = "RUN-A-WORKER-GATEWAY-SENTINEL-9c4817"
    settings = GatewaySettings(
        models=[
            ModelDef(
                name="private-default",
                litellm_model="hosted_vllm/private-default",
                provider="hosted_vllm",
                lane=Lane.PRIVATE_VLLM,
                api_base="http://vllm.internal:8000/v1",
                extra_params={"api_key": "test"},
                context_window=32_768,
            )
        ],
        tasks={_POLICY: TaskRoute(model="private-default")},
        num_retries=0,
    )
    inner = LiteLLMGateway(
        settings,
        _CostSink(),
    )
    provider_messages: list[dict[str, Any]] = []

    async def provider_call(*, model: str, messages: list[dict[str, Any]], **_kwargs: Any) -> Any:
        assert model == "private-default"
        provider_messages.extend(messages)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Run B preserved the sentinel."),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=5),
            _hidden_params={"response_cost": 0.0},
            model="private-provider-model",
        )

    monkeypatch.setattr(inner._router, "acompletion", provider_call)
    store = _RunStore()
    gateway = _RunContinuityGateway(
        inner,
        context=_continuity_context(requested_model="private-default"),
        principal=Principal(id="owner-1"),
        catalog=_Catalog(),
        selection=_Selection(),  # type: ignore[arg-type]
        run_store=store,
    )
    messages = (
        ChatMessage(role="user", content="Run A: preserve the random sentinel."),
        ChatMessage(role="assistant", content=sentinel),
        ChatMessage(role="user", content="Run B: return the sentinel."),
    )

    reset_config_hash_for_testing()
    freeze_config_hash(settings)
    try:
        completion = asyncio.run(gateway.complete(_request(messages=messages)))
    finally:
        reset_config_hash_for_testing()

    assert completion.model_id == "private-default"
    assert [(row["role"], row["content"]) for row in provider_messages] == [
        ("user", "Run A: preserve the random sentinel."),
        ("assistant", sentinel),
        ("user", "Run B: return the sentinel."),
    ]
    assert store.steps[0]["requested_model"] == "private-default"
    assert store.steps[0]["served_model"] == "private-default"


# --- POST_DEMO item 12, increment 1c ----------------------------------------


def test_the_policy_recheck_boundary_fires_BEFORE_the_inner_gateway_on_BOTH_routes() -> None:
    """The marker that stops `profile:agent-built` lying on a continuity run.

    `agent-built → run-model:before:0` was described as "everything pydantic-ai
    does before its first model request". On a continuity-active run it is not:
    THIS gateway sits in between and does policy work first — `choices()` on the
    automatic route, `verify_model_boundary()` and `authorize()` on both — and
    that work is UNMEASURED.

    ⛔ THIS DOCSTRING USED TO QUANTIFY IT, and the number came from a
    configuration that does not run. It cited the NE-CONTINUITY-PROBE-REUSE
    comment's "seconds of the inter-turn gap" — but those seconds measure the
    FORCED probe pair, which `force_refresh_private=False` disables. The live
    path uses the bounded serving cache and its cost is unknown, which is the
    reason to MARK it, not a number to quote. The same commit retracts that
    quote at the emission site and in the record; this copy survived, which
    makes it the eighth time in this PR that a corrected claim was left standing
    somewhere a reader meets it.

    `run-model:before:0` is emitted by the INNER gateway this wraps, so one
    marker at the top of `complete()` divides the edge. BEFORE it is pydantic-ai's
    graph startup AND, dominating it, the protected-history authorization: the
    left side is `materialize_history` (a PDP decision per protected reference),
    the context-budget trim, and `reauthorize_history`, which mints the durable
    delivery receipts. AFTER it is the model-policy recheck.

    BOTH routes are asserted because the marker is placed above the
    `requested_model` branch precisely so one line covers two return paths — and
    an assertion on only the automatic route would leave the explicit route free
    to drift below it.
    """

    class _RecordingBoundary:
        def __init__(self) -> None:
            self.stages: list[str] = []

        async def heartbeat(self, stage: str) -> None:
            self.stages.append(stage)

        async def checkpoint(self, *, stage: str, safe_detail: Any) -> None:
            return None

        async def await_inflight(self, operation: Any, *, stage: str) -> Any:
            return await operation

        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

    for requested_model in ("private-default", None):
        boundary = _RecordingBoundary()

        class _TimelineGateway(_Gateway):
            """Records the inner completion ONTO THE BOUNDARY'S OWN LIST.

            ⚠️ WITHOUT THIS THE ORDERING WAS NEVER ASSERTED, and an adversarial
            pass showed the exact inversion that survived: move the heartbeat out
            of `complete()` and into `_explicit()` immediately AFTER
            `self._inner.complete(candidate)`, and a test that checks
            `boundary.stages == [marker]` and separately `inner.requests` still
            passes — two lists, two timelines, no relation between them — while
            the marker now fires AFTER the model request and the split it exists
            to make is inverted.

            Putting both sides on ONE list is the same fix used a package over in
            `test_the_agent_built_boundary_fires_before_the_first_model_request`.
            It was in hand and I did not apply it here.
            """

            def __init__(self, failures: set[str], timeline: list[str]) -> None:
                super().__init__(failures)
                self._timeline = timeline

            async def complete(self, request: Any) -> Any:
                self._timeline.append("inner-complete")
                return await super().complete(request)

        class _TimelineCatalog(_Catalog):
            """Records the frozen-policy recheck onto the SAME list.

            ⛔ WITHOUT THIS THE MOVE WAS UNPINNED. The marker was originally
            emitted BELOW `self._definition()`, which performs exactly the
            catalog-visibility and frozen-policy recheck this marker exists to
            delimit — so policy work sat on the wrong side of it and a failure in
            there emitted no marker at all. Codex caught the placement; I moved
            it to the first statement of `complete()` and then checked whether
            anything would catch it moving back. Nothing did. So the recheck now
            lands on the boundary's own timeline and the assertion is an
            ordering.
            """

            def __init__(self, timeline: list[str]) -> None:
                super().__init__()
                self._timeline = timeline

            def get_visible(self, agent_id: str, _principal: Principal) -> Any:
                self._timeline.append("definition-recheck")
                return super().get_visible(agent_id, _principal)

        # No failures: the first candidate serves on both routes, so exactly one
        # inner completion happens and the marker count is unambiguous.
        inner = _TimelineGateway(set(), boundary.stages)
        context = replace(_continuity_context(requested_model=requested_model), boundary=boundary)
        gateway = _RunContinuityGateway(
            inner,
            context=context,
            principal=Principal(id="owner-1"),
            catalog=_TimelineCatalog(boundary.stages),
            selection=_Selection(),  # type: ignore[arg-type]
            run_store=_RunStore(),
        )
        asyncio.run(gateway.complete(_request()))

        route = "explicit" if requested_model is not None else "automatic"
        # ONE list, so the ordering is an ordering and not two coincidences.
        # The marker must be FIRST — ahead of the frozen-policy recheck it exists
        # to delimit, and ahead of the model request. An exact list is not used
        # here because the two routes call `_definition()` a different number of
        # times; what is pinned is the property, not the arithmetic.
        assert boundary.stages[0] == "profile:policy-recheck-before", (
            f"the {route} route emitted policy work before the marker that is "
            f"supposed to delimit it; got {boundary.stages}"
        )
        assert boundary.stages.count("profile:policy-recheck-before") == 1
        assert "definition-recheck" in boundary.stages, (
            f"the {route} route never performed the frozen-policy recheck; got {boundary.stages}"
        )
        assert "inner-complete" in boundary.stages, (
            f"the {route} route never reached the inner gateway; got {boundary.stages}"
        )

        # ONCE PER RUN, not once per request. `complete()` is called for every
        # pydantic-ai request including its output-contract retries, so an
        # ungated marker would write an uncounted extra span PER HOP into a table
        # with no reaper. That row cost is the whole reason for the gate.
        #
        # ⛔ THIS COMMENT USED TO ADD "and each one is a blocking FOR UPDATE
        # transaction on the shared DBOS loop" — a claim the SAME COMMIT retracts
        # at the emission site, because `_LeaseExecutionBoundary` already runs
        # that transaction unconditionally on a ~1 s poll for the run's whole
        # duration. A test that pins a gate must not disagree with the source
        # about WHY the gate exists: the next person removing it would read one
        # or the other and get a false reason either way.
        # ONCE PER RUN: a second request adds another inner completion and
        # another recheck, and NOT a second marker.
        before = len(boundary.stages)
        asyncio.run(gateway.complete(_request()))
        assert len(boundary.stages) > before, "the second request did nothing"
        assert boundary.stages.count("profile:policy-recheck-before") == 1, (
            f"the {route} route re-measured the recheck on a later request; got {boundary.stages}"
        )


# --- NE-CTX-IN-FORCE on the canonical Run path (the air-gap beat) ------------
#
# THE FAILING SEQUENCE. A Run is admitted while the analyst policy's primary is
# the frontier lane, so its trim budget is pinned once, at admission, to the
# frontier model (`run_execution.py`, `context_budget(authorized_model)`). The
# conversation then grows past the PRIVATE lane's window — newly possible,
# because NE-CTX-IN-FORCE uncapped the frontier row to the 1M its model really
# serves. The air gap engages. `_auto` rolls onto `private-default`. A trimmer
# still holding the frontier window drops nothing, the gateway's per-candidate
# pre-flight refuses, and because `_explicit` pins `requested_model` to the
# candidate the pre-flight BREAKS instead of rolling — so the run dies on the
# exact beat whose script is "now watch the same agent run on the private lane".

_NARROW = ModelDef(
    name="private-default",
    litellm_model="hosted_vllm/qwen",
    provider="hosted_vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://vllm:8000/v1",
    context_window=32768,
    max_output_tokens=12288,
)
_WIDE = ModelDef(
    name="frontier-default",
    litellm_model="anthropic/claude-sonnet-5",
    provider="anthropic",
    lane=Lane.FRONTIER_API,
    context_window=1_000_000,
    max_output_tokens=12288,
)
#: The analyst policy exactly as `ne.cli.demo.live` emits it: a frontier primary
#: with the private lane as its ONE authorized fallback — the air-gap beat.
_BEAT_SETTINGS = GatewaySettings(
    models=[_NARROW, _WIDE],
    tasks={_POLICY: TaskRoute(model=_WIDE.name, fallbacks=(_NARROW.name,))},
)


def _beat_selection(air_gap: Callable[[], bool]) -> Any:
    """A REAL :class:`ModelSelectionService` for the two seams the fix reads.

    ``configured_catalog`` and ``context_budget`` are the real posture rule and
    the real budget arithmetic — the point of using the real service rather than
    a double is that the fix must not re-implement "which model is in force".
    ``choices``/``authorize`` are the async evidence-gathering halves; they are
    replaced so the roll can be driven without a serving probe, an audit chain
    and a PDP, and they are NOT what is under test here.
    """
    from ne.api.model_selection import ModelSelectionService

    service = ModelSelectionService(
        gateway_settings=_BEAT_SETTINGS,
        serving_status=SimpleNamespace(),  # type: ignore[arg-type]
        audit_engine=SimpleNamespace(),  # type: ignore[arg-type]
        pdp=SimpleNamespace(),  # type: ignore[arg-type]
        sink=SimpleNamespace(),  # type: ignore[arg-type]
        config_hash="c" * 64,
        air_gap_provider=air_gap,
    )
    scripted = _Selection()

    async def choices(task: str) -> ModelChoiceCatalog:
        # Availability, filtered by the SAME posture the real catalog uses, so
        # the roll this drives is the roll the deployment would take.
        return service.configured_catalog(task)

    service.choices = choices  # type: ignore[method-assign]
    service.authorize = scripted.authorize  # type: ignore[method-assign]
    return service


def _beat_gateway(inner: Any, selection: Any) -> _RunContinuityGateway:
    return _RunContinuityGateway(
        inner,
        context=_continuity_context(requested_model=None),
        principal=Principal(id="owner-1"),
        catalog=_Catalog(),
        selection=selection,
        run_store=_RunStore(),
    )


def test_the_run_trim_budget_follows_the_air_gap_roll_instead_of_the_admission_pin() -> None:
    """The budget is re-derived per turn from the posture in force RIGHT NOW.

    Note what the answer is NOT: forwarding to the inner gateway would give the
    ROUTE PRIMARY's budget, and the route primary is still ``frontier-default``
    under an engaged gap — the gap removes the candidate from the SELECTION
    catalog, not from the routing table. So a plain delegation would have left
    this defect exactly where it was, which is why this wrapper answers for
    itself.

    The run is ADMITTED on the frontier lane — the section narrative's exact
    sequence, and (round 3) the shape that matters: while the gap is open the
    frozen frontier primary is still selectable and is what ``_auto`` attempts
    first, so the wide budget belongs to IT; the engaged gap removes it from
    the catalog, and only then does the effective default's narrow window take
    over.
    """
    from ne.contracts.models import TurnContextBudget

    gap = {"on": False}
    selection = _beat_selection(lambda: gap["on"])
    gateway = _RunContinuityGateway(
        _Gateway(),
        context=_continuity_context(requested_model=None, authorized_model="frontier-default"),
        principal=Principal(id="owner-1"),
        catalog=_Catalog(),
        selection=selection,
        run_store=_RunStore(),
    )
    assert isinstance(gateway, TurnContextBudget)

    # CONTROL — gap open: the frontier lane is in force and gets its own window.
    wide = asyncio.run(gateway.context_budget_for_turn(_POLICY))
    assert wide is not None and wide.model == _WIDE.name
    assert wide.budget == selection.context_budget(_WIDE.name)
    assert wide.budget.context_window == 1_000_000

    # The beat. No restart, no re-admission, no change to the route table.
    gap["on"] = True
    narrow = asyncio.run(gateway.context_budget_for_turn(_POLICY))
    assert narrow is not None and narrow.model == _NARROW.name
    assert narrow.budget == selection.context_budget(_NARROW.name)
    assert narrow.budget.context_window == 32768

    # And the route table really is untouched — the thing a plain forward reads.
    assert _BEAT_SETTINGS.tasks[_POLICY].model == _WIDE.name


def test_the_run_trim_budget_follows_the_frozen_primary_while_it_remains_selectable() -> None:
    """codex P1, round 3 of the resolver-vs-attempt divergence, on the worker.

    ``_auto`` prepends ``run.authorized_model`` to its candidate order whenever
    that model is still in the catalog — the frozen primary is attempted FIRST,
    ahead of the catalog's ``effective_default``. The resolver budgeted only the
    effective default, so on an UNPINNED run whose admission model differs from
    the default in force (a live repoint, or simply a route whose primary is
    another lane), the budget named a candidate that is attempted SECOND: a
    narrower new default dropped history the serving model could hold, and a
    wider one had the frozen primary pre-flight-skipped instead of trimmed for.

    Here the run was admitted on ``private-default`` while the catalog's
    effective default is the WIDE frontier primary and BOTH are selectable. The
    proof is parity by execution: the model ``_auto`` actually serves first and
    the model the budget names must be the same one.
    """
    selection = _beat_selection(lambda: False)  # gap open: both lanes selectable
    gateway = _beat_gateway(_Gateway(), selection)  # admitted on private-default

    # The divergence precondition, asserted so this cannot go vacuous: the
    # effective default is NOT the admission-frozen model, which stays listed.
    catalog = selection.configured_catalog(_POLICY)
    assert catalog.effective_default == _WIDE.name
    assert _NARROW.name in {choice.model for choice in catalog.choices}

    # What _auto ACTUALLY attempts first is the frozen primary...
    completion = asyncio.run(gateway.complete(_request()))
    assert completion.model_id == _NARROW.name

    # ...so that is the model the turn must be measured against — 32768, not
    # the 1M the effective default would (dangerously) widen the trim to.
    resolved = asyncio.run(gateway.context_budget_for_turn(_POLICY))
    assert resolved is not None and resolved.model == completion.model_id
    assert resolved.budget == selection.context_budget(_NARROW.name)
    assert resolved.budget.context_window == 32768


def test_the_budget_follows_the_evidence_filtered_catalog_not_the_configured_one() -> None:
    """Round 4, finding A: the resolver must read the catalog ``_auto`` reads.

    ``_auto`` walks ``choices()`` — posture AND lifecycle AND serving evidence.
    The round-3 resolver read ``configured_catalog()`` — posture ONLY, no
    liveness claim by design. The moment serving/lifecycle evidence removes the
    admission-frozen model, the two disagree: the frozen model is still LISTED
    by the configured catalog (so the resolver budgeted its wide window) while
    ``_auto`` can only attempt the narrower survivor. Nothing was trimmed, the
    survivor's gateway pre-flight refused, and with no later candidate the
    automatic fallback failed a run that trimming would have served.

    Staged exactly so: admitted on the WIDE frontier primary, gap open (posture
    still lists frontier — asserted, so the divergence cannot go vacuous), but
    the evidence-filtered catalog has dropped it. Parity is by execution: the
    model ``_auto`` actually serves and the model the budget names must be the
    same one, and its window is the narrow 32768, not the frozen 1M.
    """
    selection = _beat_selection(lambda: False)  # gap open: posture lists BOTH lanes

    async def evidence_filtered_choices(task: str) -> ModelChoiceCatalog:
        # What `choices()` really does when serving/lifecycle evidence has
        # removed the frontier model: the configured (posture) catalog minus
        # the model no evidence supports, with the effective default re-derived
        # over the survivors — the same rule `_choices` applies.
        catalog = selection.configured_catalog(task)
        kept = tuple(choice for choice in catalog.choices if choice.model != _WIDE.name)
        names = {choice.model for choice in kept}
        default = next(
            (n for n in (_WIDE.name, _NARROW.name) if n in names),
            None,
        )
        return ModelChoiceCatalog(task=catalog.task, choices=kept, effective_default=default)

    selection.choices = evidence_filtered_choices  # type: ignore[method-assign]
    gateway = _RunContinuityGateway(
        _Gateway(),
        context=_continuity_context(requested_model=None, authorized_model="frontier-default"),
        principal=Principal(id="owner-1"),
        catalog=_Catalog(),
        selection=selection,
        run_store=_RunStore(),
    )

    # The divergence precondition, asserted so this cannot go vacuous: the
    # POSTURE catalog (the round-3 resolver's authority) still lists the frozen
    # frontier model — only EVIDENCE removed it.
    configured = selection.configured_catalog(_POLICY)
    assert _WIDE.name in {choice.model for choice in configured.choices}
    assert configured.effective_default == _WIDE.name

    # What _auto ACTUALLY serves is the narrow evidence-backed survivor...
    completion = asyncio.run(gateway.complete(_request()))
    assert completion.model_id == _NARROW.name

    # ...and the budget names THAT model and ITS window — not the frozen
    # primary's 1M, which is what the configured catalog would have said.
    resolved = asyncio.run(gateway.context_budget_for_turn(_POLICY))
    assert resolved is not None and resolved.model == completion.model_id
    assert resolved.budget == selection.context_budget(_NARROW.name)
    assert resolved.budget.context_window == 32768


def test_an_explicitly_pinned_run_is_measured_against_its_one_authorized_model() -> None:
    """`_explicit` attempts exactly that model and fails the Run if it does not
    serve, so there is no other model the turn could honestly be measured
    against — including under an engaged gap, where the pin is what the refusal
    must name."""
    selection = _beat_selection(lambda: True)
    gateway = _RunContinuityGateway(
        _Gateway(),
        context=_continuity_context(requested_model="frontier-default"),
        principal=Principal(id="owner-1"),
        catalog=_Catalog(),
        selection=selection,
        run_store=_RunStore(),
    )
    resolved = asyncio.run(gateway.context_budget_for_turn(_POLICY))
    assert resolved is not None and resolved.model == _WIDE.name
    assert resolved.budget == selection.context_budget(_WIDE.name)


def test_the_capability_answers_None_rather_than_raising_on_an_unroutable_policy() -> None:
    """The contract is "never raises": the routing fault is ``complete``'s to
    report, typed and audited, an instant later. Answering ``None`` keeps the
    composed boot budget armed instead of turning it into a trim fault."""
    selection = _beat_selection(lambda: False)
    gateway = _beat_gateway(_Gateway(), selection)
    assert asyncio.run(gateway.context_budget_for_turn("agent.no-such-policy.v9")) is None


def test_a_budget_refusal_from_one_candidate_no_longer_aborts_the_auto_roll() -> None:
    """`_explicit` pins ``requested_model``, and the gateway pre-flight BREAKS
    rather than rolling on an explicitly-named model — correct where a human
    named it, wrong here where this loop did. A candidate whose window cannot
    hold the turn is now PASSED OVER like any other candidate that did not
    answer, and the fact is recorded in the step row."""
    selection = _beat_selection(lambda: False)
    store = _RunStore()
    inner = _Gateway()
    over_budget = ContextBudgetExceededError(
        stage="gateway-preflight",
        estimated_tokens=30_000,
        input_budget=18_841,
        context_window=32_768,
        dropped_turns=0,
    )

    calls: list[str] = []

    async def complete(request: CompletionRequest) -> Completion:
        model = str(request.requested_model)
        calls.append(model)
        if model == "private-default":
            raise over_budget
        return Completion(
            text="served", model_id=model, usage=Usage(prompt_tokens=1, completion_tokens=1)
        )

    inner.complete = complete  # type: ignore[method-assign]
    gateway = _RunContinuityGateway(
        inner,
        context=_continuity_context(requested_model=None),
        principal=Principal(id="owner-1"),
        catalog=_Catalog(),
        selection=selection,
        run_store=store,
    )
    completion = asyncio.run(gateway.complete(_request()))

    # It rolled PAST the candidate that could not hold the turn and served.
    assert calls == ["private-default", "frontier-default"]
    assert completion.model_id == "frontier-default"
    assert store.steps[0]["route_position"] == "fallback"
    assert store.steps[0]["fallback_from"] == "private-default"


def test_when_no_candidate_can_hold_the_turn_the_budget_refusal_is_what_raises() -> None:
    """Rolling past a budget refusal must not turn a governed, counts-bearing
    refusal into a vaguer error: with nothing left to try, the last one is
    re-raised with its counts intact."""
    selection = _beat_selection(lambda: False)
    inner = _Gateway()

    async def complete(request: CompletionRequest) -> Completion:
        raise ContextBudgetExceededError(
            stage="gateway-preflight",
            estimated_tokens=30_000,
            input_budget=18_841,
            context_window=32_768,
            dropped_turns=0,
        )

    inner.complete = complete  # type: ignore[method-assign]
    gateway = _beat_gateway(inner, selection)
    with pytest.raises(ContextBudgetExceededError) as refused:
        asyncio.run(gateway.complete(_request()))
    assert refused.value.estimated_tokens == 30_000
    assert refused.value.input_budget == 18_841
