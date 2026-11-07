# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The live route seam (NE-MODEL-ROUTING §5) — through the real Router.

These tests pin the four properties the governed runtime swap rests on:

1. ``route_provider=None`` is the byte-identical legacy path (the CONTROL that
   must stay green through every mutation below);
2. a provider change takes effect on the NEXT call with no reconstruction —
   the live property the whole unit exists to deliver;
3. an override may only ever select a BOOT-REGISTERED model, and an
   unresolvable name is a LOUD refusal, never a silent revert to the boot
   primary (a swap that quietly did not happen is the silent downgrade);
4. an override cannot reach a host the egress rules would refuse — the guard
   still runs per attempt on the ``ModelDef`` actually selected, and a policy
   denial propagates UNWRAPPED (no fallback, no transport).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from ne.contracts import ChatMessage, CompletionRequest, Lane
from ne.gateway import (
    EgressPolicy,
    EgressSettings,
    LiteLLMGateway,
    TaskRoute,
)
from ne.gateway.errors import AirGapViolationError, UnknownRoutedModelError
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .conftest import PASSTHROUGH_REDACTOR, RecordingCostSink, make_model, make_settings

_MSGS = (ChatMessage(role="user", content="hi"),)


def _request(task: str, **kw: Any) -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task, **kw)


def _two_model_settings() -> Any:
    """A deployment offering two boot-registered models on one task."""
    return make_settings(
        [
            make_model("private-default", mock_response="from-private-default"),
            make_model("aux-model", mock_response="from-aux-model"),
        ],
        {"agent": TaskRoute(model="private-default")},
    )


# -- 1. the legacy path is untouched (CONTROL) --------------------------------


def test_route_provider_none_serves_the_boot_primary(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    """CONTROL. No provider ⇒ the boot route decides, exactly as before."""
    gw = LiteLLMGateway(_two_model_settings(), sink, redactor=PASSTHROUGH_REDACTOR)
    result = asyncio.run(gw.complete(_request("agent")))
    assert result.model_id == "private-default"
    assert result.text == "from-private-default"


# -- 2. live: the swap takes effect on the next call --------------------------


def test_route_provider_repoints_the_task_primary_without_reconstruction(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    """THE unit's reason to exist: turn N and turn N+1 hit DIFFERENT models
    from ONE gateway instance, with no restart and no re-composition."""
    override: dict[str, str] = {}

    def routes() -> dict[str, TaskRoute]:
        boot = {"agent": TaskRoute(model="private-default")}
        if "agent" in override:
            return {"agent": TaskRoute(model=override["agent"])}
        return boot

    gw = LiteLLMGateway(
        _two_model_settings(), sink, redactor=PASSTHROUGH_REDACTOR, route_provider=routes
    )

    first = asyncio.run(gw.complete(_request("agent")))
    assert first.model_id == "private-default"

    override["agent"] = "aux-model"

    second = asyncio.run(gw.complete(_request("agent")))
    assert second.model_id == "aux-model", (
        "the SAME gateway instance must serve the overridden model on the very "
        "next call — a route change that needs a restart is the defect this unit fixes"
    )
    assert second.text == "from-aux-model"


def test_live_repoint_preserves_history_and_traces_each_turn_to_its_serving_model(
    sink: RecordingCostSink,
    audit_sink: Any,
    monkeypatch: pytest.MonkeyPatch,
    frozen_config: str,
) -> None:
    """A route flip changes only the next call's model, not its conversation.

    Session persistence deliberately stores model-independent message payloads.
    This gateway boundary test proves the resulting history reaches the new
    model intact, while each turn remains attributable by trace and model.
    """
    override: dict[str, str] = {}

    def routes() -> dict[str, TaskRoute]:
        return {"agent": TaskRoute(model=override["agent"])} if override else {}

    settings = make_settings(
        [
            make_model(
                "qwen3.6-35b-a3b",
                litellm_model="hosted_vllm/qwen3.6-35b-a3b",
                provider="hosted_vllm",
                context_window=262_144,
            ),
            make_model(
                "aux-model",
                litellm_model="hosted_vllm/qwen-small",
                provider="hosted_vllm",
                context_window=262_144,
            ),
        ],
        {"agent": TaskRoute(model="qwen3.6-35b-a3b")},
    )
    gw = LiteLLMGateway(
        settings,
        sink,
        audit_sink=audit_sink,
        redactor=PASSTHROUGH_REDACTOR,
        route_provider=routes,
    )
    calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append((model, messages))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=f"from-{model}"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
            _hidden_params={"response_cost": 0.0},
            model=model,
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    first_messages = (ChatMessage(role="user", content="remember alpha"),)
    first = asyncio.run(
        gw.complete(CompletionRequest(messages=first_messages, task="agent", trace_id="turn-1"))
    )

    override["agent"] = "aux-model"
    # Persist turn N in the real session tables, then build turn N+1 from the
    # store's readback. This is the model-independent history path production
    # AgentSession uses; no model column or hand-copied prior turn is involved.
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    store = SqlAlchemySessionStore(engine)

    async def persisted_second_turn() -> tuple[ChatMessage, ...]:
        session_id = await store.open_session("assistant", "eng")
        await store.append_turn(
            session_id,
            [
                ModelRequest(parts=[UserPromptPart(content="remember alpha")]),
                ModelResponse(parts=[TextPart(content=first.text)], model_name=first.model_id),
            ],
        )
        history = await store.load_history(session_id)
        messages: list[ChatMessage] = []
        for message in history:
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    messages.append(ChatMessage(role="user", content=str(part.content)))
                elif isinstance(part, TextPart):
                    messages.append(ChatMessage(role="assistant", content=part.content))
        messages.append(ChatMessage(role="user", content="what should you remember?"))
        return tuple(messages)

    second_messages = asyncio.run(persisted_second_turn())
    engine.dispose()
    asyncio.run(
        gw.complete(CompletionRequest(messages=second_messages, task="agent", trace_id="turn-2"))
    )

    assert calls[0][0] == "qwen3.6-35b-a3b"
    assert calls[1] == ("aux-model", [message.model_dump() for message in second_messages])
    assert [(event.model, event.trace_id) for event in sink.events] == [
        ("hosted_vllm/qwen3.6-35b-a3b", "turn-1"),
        ("hosted_vllm/qwen-small", "turn-2"),
    ]
    assert [
        (event.provenance["model_name"], event.trace_id)
        for event in audit_sink.events
        if event.action == "ne.gateway.complete"
    ] == [("qwen3.6-35b-a3b", "turn-1"), ("aux-model", "turn-2")]


def test_override_carries_the_boot_fallbacks_and_params_unchanged(
    sink: RecordingCostSink, monkeypatch: pytest.MonkeyPatch, frozen_config: str
) -> None:
    """An override replaces the PRIMARY only. It can neither drop a fallback nor
    inject route params — both stay whatever the deployment booted with."""
    settings = make_settings(
        [
            make_model("private-default"),
            make_model("aux-model"),
            make_model("backup"),
        ],
        {"agent": TaskRoute(model="private-default", fallbacks=("backup",), params={"top_p": 0.5})},
    )

    def routes() -> dict[str, TaskRoute]:
        # A hostile provider trying to widen the route: no fallbacks, new params.
        return {"agent": TaskRoute(model="aux-model", fallbacks=(), params={"top_p": 0.99})}

    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR, route_provider=routes)

    captured: dict[str, Any] = {}

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        captured["model"] = model
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
            _hidden_params={"response_cost": 0.0},
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(gw.complete(_request("agent")))

    assert captured["model"] == "aux-model"
    assert captured["top_p"] == 0.5, (
        "route params must come from the BOOT route; an override may not inject them"
    )
    # The boot fallback is still a candidate.
    assert [d.name for d in gw._candidates("agent", None)] == ["aux-model", "backup"]


def test_override_to_a_boot_fallback_deduplicates_failover_candidates(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    """Selecting fallback B as primary yields B once, then advances to C."""
    settings = make_settings(
        [make_model("primary"), make_model("backup-b"), make_model("backup-c")],
        {"agent": TaskRoute(model="primary", fallbacks=("backup-b", "backup-c"))},
    )
    gw = LiteLLMGateway(
        settings,
        sink,
        redactor=PASSTHROUGH_REDACTOR,
        route_provider=lambda: {"agent": TaskRoute(model="backup-b")},
    )

    assert [model.name for model in gw._candidates("agent", None)] == [
        "backup-b",
        "backup-c",
    ]


# -- 3. an override may only select a boot-registered model -------------------


def test_override_naming_an_unregistered_model_raises_and_does_not_serve_the_boot_model(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    """A name outside the BOOT model set is refused LOUDLY.

    Two claims, deliberately both asserted: (a) the error names the offending
    model and the registered alternatives, and (b) nothing was served — a
    silent revert to the boot primary would look identical to a successful
    swap from the caller's side, which is the silent-downgrade failure.
    """

    def routes() -> dict[str, TaskRoute]:
        return {"agent": TaskRoute(model="attacker-model")}

    gw = LiteLLMGateway(
        _two_model_settings(), sink, redactor=PASSTHROUGH_REDACTOR, route_provider=routes
    )
    with pytest.raises(UnknownRoutedModelError) as excinfo:
        asyncio.run(gw.complete(_request("agent")))
    msg = str(excinfo.value)
    assert "attacker-model" in msg
    assert "private-default" in msg and "aux-model" in msg
    assert sink.events == [], "a refused route must not emit a cost event"


def test_override_cannot_introduce_a_task_that_the_boot_config_does_not_route(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    """The override table keys off EXISTING tasks. A provider naming a task the
    deployment never routed does not mint a route — the unknown-task refusal is
    unchanged, so no new capability can appear at runtime."""
    from ne.gateway import UnknownTaskError

    def routes() -> dict[str, TaskRoute]:
        return {"agent": TaskRoute(model="aux-model"), "smuggled": TaskRoute(model="aux-model")}

    gw = LiteLLMGateway(
        _two_model_settings(), sink, redactor=PASSTHROUGH_REDACTOR, route_provider=routes
    )
    with pytest.raises(UnknownTaskError):
        asyncio.run(gw.complete(_request("smuggled")))


# -- 4. an override cannot reach a host the egress rules refuse ---------------


def test_override_to_a_frontier_model_under_air_gap_is_refused_with_no_transport(
    sink: RecordingCostSink, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE reach question, answered by running it.

    The route is repointed at a boot-registered FRONTIER model while the air gap
    is engaged. Three claims: the call raises ``AirGapViolationError``; the
    in-VPC fallback is NOT silently used (a policy denial propagates unwrapped —
    the NE-012 seam, so the swap does not quietly succeed against a different
    model); and litellm was never reached.
    """
    settings = make_settings(
        [
            make_model(
                "private-default",
                lane=Lane.PRIVATE_VLLM,
                provider="hosted_vllm",
                api_base="http://vllm:8000/v1",
            ),
            make_model("frontier-best", provider="anthropic"),
        ],
        {"agent": TaskRoute(model="private-default", fallbacks=("private-default",))},
    )
    guard = EgressPolicy(
        EgressSettings(air_gap=True, allowed_frontier_hosts=("api.anthropic.com",))
    )

    def routes() -> dict[str, TaskRoute]:
        return {"agent": TaskRoute(model="frontier-best")}

    gw = LiteLLMGateway(
        settings,
        sink,
        redactor=PASSTHROUGH_REDACTOR,
        egress_guard=guard,
        route_provider=routes,
    )

    calls: list[str] = []

    async def never_called(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append(model)
        raise AssertionError("the provider must never be reached under AIR_GAP")

    monkeypatch.setattr(gw._router, "acompletion", never_called)

    with pytest.raises(AirGapViolationError) as excinfo:
        asyncio.run(gw.complete(_request("agent")))
    assert "frontier-best" in str(excinfo.value)
    assert calls == [], "AIR_GAP must stop the call before any transport"
    assert sink.events == []


def test_override_to_a_frontier_model_with_the_gap_open_and_host_allowlisted_serves(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    """CONTROL for the test above: the SAME override, the SAME guard object
    shape, only the posture differs — so the refusal is attributable to the
    air gap and not to the override mechanism being broken."""
    settings = make_settings(
        [
            make_model(
                "private-default",
                lane=Lane.PRIVATE_VLLM,
                provider="hosted_vllm",
                api_base="http://vllm:8000/v1",
            ),
            make_model("frontier-best", provider="anthropic", mock_response="from-frontier"),
        ],
        {"agent": TaskRoute(model="private-default")},
    )
    guard = EgressPolicy(
        EgressSettings(air_gap=False, allowed_frontier_hosts=("api.anthropic.com",))
    )

    def routes() -> dict[str, TaskRoute]:
        return {"agent": TaskRoute(model="frontier-best")}

    gw = LiteLLMGateway(
        settings, sink, redactor=PASSTHROUGH_REDACTOR, egress_guard=guard, route_provider=routes
    )
    result = asyncio.run(gw.complete(_request("agent")))
    assert result.model_id == "frontier-best"


def test_override_to_a_frontier_model_off_the_allowlist_is_blocked(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    """Air gap disengaged, but the target's host is not allowlisted: still
    refused. The allowlist half of the reach question, not only the air gap."""
    from ne.gateway.errors import EgressBlockedError

    settings = make_settings(
        [
            make_model("private-default", provider="anthropic"),
            make_model("frontier-best", provider="openai"),
        ],
        {"agent": TaskRoute(model="private-default")},
    )
    guard = EgressPolicy(
        EgressSettings(air_gap=False, allowed_frontier_hosts=("api.anthropic.com",))
    )

    def routes() -> dict[str, TaskRoute]:
        return {"agent": TaskRoute(model="frontier-best")}

    gw = LiteLLMGateway(
        settings, sink, redactor=PASSTHROUGH_REDACTOR, egress_guard=guard, route_provider=routes
    )
    with pytest.raises(EgressBlockedError) as excinfo:
        asyncio.run(gw.complete(_request("agent")))
    assert "api.openai.com" in str(excinfo.value)


# -- 5. one snapshot per call -------------------------------------------------


def test_route_is_snapshotted_once_per_call(
    sink: RecordingCostSink, monkeypatch: pytest.MonkeyPatch, frozen_config: str
) -> None:
    """A concurrent flip must not split ONE call's candidates and its params
    across two different routes. The provider is read exactly once per
    ``complete``; a second read inside the same call would see the flipped
    value and the params would disagree with the model."""
    reads: list[int] = []
    flip = {"n": 0}

    def routes() -> dict[str, TaskRoute]:
        flip["n"] += 1
        reads.append(flip["n"])
        # Every read after the first returns a DIFFERENT primary. If complete()
        # read twice, the params leg and the candidates leg would disagree.
        name = "private-default" if flip["n"] == 1 else "aux-model"
        return {
            "agent": TaskRoute(
                model=name, params={"top_p": 0.1 if name == "private-default" else 0.9}
            )
        }

    settings = make_settings(
        [make_model("private-default"), make_model("aux-model")],
        {"agent": TaskRoute(model="private-default", params={"top_p": 0.1})},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR, route_provider=routes)

    captured: dict[str, Any] = {}

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        captured["model"] = model
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
            _hidden_params={"response_cost": 0.0},
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(gw.complete(_request("agent")))
    assert len(reads) == 1, f"the route provider must be read once per call, got {len(reads)}"
    assert captured["model"] == "private-default"


# -- 6. which model served this turn is answerable per call -------------------


def test_the_served_model_is_named_on_every_call_regardless_of_the_route(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    """The design's honesty claim, verified rather than asserted in prose.

    The runtime route is deliberately OUTSIDE the frozen ``config_hash`` (a hash
    that moved under a running process would be worse than one that never
    claimed to cover runtime state), so "which model served this turn" must be
    answerable some other way. It already is: the gateway emits a ``CostEvent``
    per call naming the litellm model, the provider, the lane and the task —
    and it names the model that ACTUALLY served, not the boot route's.
    """
    override: dict[str, str] = {}

    def routes() -> dict[str, TaskRoute]:
        return {"agent": TaskRoute(model=override["agent"])} if override else {}

    settings = make_settings(
        [
            make_model("private-default", litellm_model="hosted_vllm/qwen", provider="hosted_vllm"),
            make_model("aux-model", litellm_model="hosted_vllm/qwen-small", provider="hosted_vllm"),
        ],
        {"agent": TaskRoute(model="private-default")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR, route_provider=routes)

    asyncio.run(gw.complete(_request("agent")))
    override["agent"] = "aux-model"
    asyncio.run(gw.complete(_request("agent")))

    served = [(e.model, e.task) for e in sink.events]
    assert served == [
        ("hosted_vllm/qwen", "agent"),
        ("hosted_vllm/qwen-small", "agent"),
    ], (
        "the cost event must name the model that actually served each call — "
        "otherwise a live route change would make the audit trail ambiguous"
    )
