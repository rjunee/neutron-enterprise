# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ModelDef.extra_params`` is a WIRE seam, not merely a config field.

A per-model provider parameter that silently fails to reach the provider is
worse than not setting it at all, because the config then *looks* fixed. These
tests assert against the actual HTTP request body litellm builds — the last
thing before the socket — never against the settings object that produced it.

WHY THE CAPTURE IS AT THE HTTP HANDLER and not at the ``Router.acompletion``
seam that ``test_frozen_params_egress.py`` / ``test_redaction_seam.py`` use:
``extra_params`` are DEPLOYMENT config. ``LiteLLMGateway.__init__`` splats
``**d.extra_params`` into that model's Router ``litellm_params`` entry, and the
Router merges them into the call INTERNALLY. A recorder on ``Router.acompletion``
therefore cannot see them at all — it observes the gateway's own kwargs, which is
the wrong side of the merge. Capturing lower also proves the part a kwargs
assertion never could: that litellm's per-provider param mapping FORWARDS the key
into the provider body rather than dropping it as unsupported.

The concrete motivating parameter is ``thinking: {"type": "disabled"}`` on the
frontier lane (``ne.cli.demo.live._FRONTIER_MODEL_EXTRA_PARAMS``). Nothing here
is provider-specific by design: the gateway forwards whatever the config
declares, and model-independence forbids a provider branch in gateway code. What
is pinned is the SEAM — declare a param on a model and it egresses on that
model's requests and on no other model's.

Each test is a PAIRED mutation: the frontier request carries the parameter, and
the private-vLLM request built from the same settings is byte-identical to one
built with no table entry at all.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from litellm.llms.custom_httpx import http_handler

from ne.contracts import ChatMessage, CompletionRequest, Lane
from ne.gateway import LiteLLMGateway, ModelCallFailedError, ModelDef, TaskRoute

from .conftest import PASSTHROUGH_REDACTOR, RecordingCostSink, make_settings

_MSGS = (ChatMessage(role="user", content="hi"),)

#: The shape ``ne.cli.demo.live._FRONTIER_MODEL_EXTRA_PARAMS`` declares for
#: ``anthropic/claude-sonnet-5``. For why ``allowed_openai_params`` is declared, see
#: ``test_thinking_alone_reaches_the_wire_now_that_the_pinned_map_has_the_model``.
_DISABLED_THINKING: dict[str, Any] = {
    "thinking": {"type": "disabled"},
    "allowed_openai_params": ["thinking"],
}

# Every call here reaches the real per-call instrumentation, which stamps the
# frozen config_hash and refuses (ConfigHashNotFrozenError, surfaced as a
# provider failure) when it has not been frozen. Without this the gateway fails
# BEFORE building a request and every body assertion below goes vacuous.
pytestmark = pytest.mark.usefixtures("frozen_config")


class _Captured(Exception):
    """Raised from the patched transport once the body is in hand.

    Aborting rather than returning a fake 200 is deliberate: these tests must
    never open a socket, and synthesising a provider response would drag the
    whole response-parsing path in without adding any proof about the request.
    """


@pytest.fixture
def wire_bodies(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the JSON body of every outbound provider request.

    Patches ``AsyncHTTPHandler.post`` — the single call litellm's async
    providers funnel through — so what is asserted is real litellm request
    construction, not a re-implementation of it.
    """
    bodies: list[dict[str, Any]] = []

    async def _post(self: Any, url: str, *args: Any, **kw: Any) -> Any:
        # litellm spells the payload ``data=`` on some providers and ``json=`` on
        # others. Read both and fail LOUDLY on neither, so a litellm re-pin that
        # switches spelling breaks this test instead of silently capturing None
        # and turning every assertion below vacuous.
        raw = kw.get("data")
        if raw is None:
            raw = kw.get("json")
        if raw is None and args:
            raw = args[0]
        assert raw is not None, f"no request payload captured for {url}"
        if isinstance(raw, bytes | str):
            raw = json.loads(raw)
        bodies.append({"url": url, "body": raw})
        raise _Captured(url)

    monkeypatch.setattr(http_handler.AsyncHTTPHandler, "post", _post, raising=True)
    return bodies


def _model(
    name: str,
    *,
    lane: Lane,
    litellm_model: str,
    provider: str,
    api_base: str | None = None,
    extra_params: dict[str, Any] | None = None,
) -> ModelDef:
    """A ModelDef with NO ``mock_response``.

    ``conftest.make_model`` injects one, and litellm short-circuits a mocked
    deployment BEFORE it builds a request body — a mocked model could therefore
    never prove anything about the wire.
    """
    return ModelDef(
        name=name,
        litellm_model=litellm_model,
        provider=provider,
        lane=lane,
        api_base=api_base,
        extra_params={"api_key": "test-key", **(extra_params or {})},
        context_window=200_000,
    )


def _frontier(extra_params: dict[str, Any] | None) -> ModelDef:
    return _model(
        "frontier-default",
        lane=Lane.FRONTIER_API,
        litellm_model="anthropic/claude-sonnet-5",
        provider="anthropic",
        extra_params=extra_params,
    )


def _private() -> ModelDef:
    return _model(
        "private-default",
        lane=Lane.PRIVATE_VLLM,
        litellm_model="hosted_vllm/qwen3.6-35b-a3b",
        provider="hosted_vllm",
        api_base="http://vllm:8000/v1",
    )


_TASKS = {
    "agent": TaskRoute(model="frontier-default"),
    "reason": TaskRoute(model="private-default"),
}


def _gateway(frontier_extra: dict[str, Any] | None) -> LiteLLMGateway:
    return LiteLLMGateway(
        make_settings([_frontier(frontier_extra), _private()], _TASKS, num_retries=0),
        RecordingCostSink(),
        redactor=PASSTHROUGH_REDACTOR,
    )


def _call(gw: LiteLLMGateway, task: str) -> None:
    """Drive one completion to the transport and swallow the resulting failure.

    Every candidate fails (the transport always raises), so the gateway raises
    its all-candidates-failed terminal. Asserting that SPECIFIC terminal — rather
    than a blind ``Exception`` — keeps the helper honest: a config or wiring
    mistake that made the gateway fail for some unrelated reason would surface
    here instead of being swallowed and leaving the body assertions vacuous.
    """
    with pytest.raises(ModelCallFailedError):
        asyncio.run(gw.complete(CompletionRequest(messages=_MSGS, task=task)))


def test_frontier_extra_param_reaches_the_wire_body(wire_bodies: list[dict[str, Any]]) -> None:
    """THE proof: a ``thinking`` disable declared on the frontier ModelDef is
    present in the JSON body sent to the provider. The config did not stop at
    being config."""
    _call(_gateway(_DISABLED_THINKING), "agent")
    assert wire_bodies, "no provider request was built"
    sent = wire_bodies[-1]
    assert sent["body"]["thinking"] == {"type": "disabled"}, (
        f"thinking did not reach the wire; body keys were {sorted(sent['body'])}"
    )
    assert "api.anthropic.com" in sent["url"]
    # ``allowed_openai_params`` is a litellm CONTROL key, not a provider field:
    # it must be consumed on the way and never egress.
    assert "allowed_openai_params" not in sent["body"]


def test_thinking_alone_reaches_the_wire_now_that_the_pinned_map_has_the_model(
    wire_bodies: list[dict[str, Any]],
) -> None:
    """Records how the pinned litellm treats a bare ``thinking`` for this model.

    litellm gates ``thinking`` on the target model's ``supports_reasoning``, read
    from its cost map, and the C3 chokepoint forces
    ``LITELLM_LOCAL_MODEL_COST_MAP=True``. The packaged map in litellm 1.100.4
    contains ``anthropic/claude-sonnet-5``, so a bare ``thinking`` is accepted and
    reaches the wire without the ``allowed_openai_params`` escape hatch. The hatch
    in ``ne.cli.demo.live._FRONTIER_MODEL_EXTRA_PARAMS`` is therefore redundant on
    this pin but harmless (it is a litellm control key that never egresses, see
    ``test_frontier_extra_param_reaches_the_wire_body``). If a future re-pin drops
    the id from the map, this test fails and the hatch becomes load-bearing again.
    """
    _call(_gateway({"thinking": {"type": "disabled"}}), "agent")
    assert wire_bodies, (
        "litellm rejected a bare `thinking` for this model — the pinned map no "
        "longer has the id; the `allowed_openai_params` escape hatch in "
        "ne.cli.demo.live._FRONTIER_MODEL_EXTRA_PARAMS is load-bearing again"
    )
    assert wire_bodies[-1]["body"]["thinking"] == {"type": "disabled"}


def test_no_table_entry_means_the_key_is_absent(wire_bodies: list[dict[str, Any]]) -> None:
    """CONTROL ARM — without it the test above proves nothing, because a body
    that always carried ``thinking`` would satisfy it too.

    A model with no table entry emits the request it emitted before the table
    existed: the key is ABSENT, not present-and-null. This is also what keeps
    the change inert for every frontier model that must not receive it — notably
    ``anthropic/claude-fable-5``, which the provider 400s on a thinking disable.
    """
    _call(_gateway(None), "agent")
    assert "thinking" not in wire_bodies[-1]["body"]


def test_private_lane_body_unchanged_by_the_frontier_param(
    wire_bodies: list[dict[str, Any]],
) -> None:
    """The PAIRED mutation.

    The private lane serves a hybrid-thinking model whose reasoning shares its
    ONE completion budget; disabling thinking there is a different decision with
    different consequences and is explicitly out of scope. Declaring the param on
    the frontier model must not perturb the private lane's request at all.
    """
    _call(_gateway(_DISABLED_THINKING), "reason")
    treated = json.dumps(wire_bodies[-1]["body"], sort_keys=True)

    wire_bodies.clear()
    _call(_gateway(None), "reason")
    baseline = json.dumps(wire_bodies[-1]["body"], sort_keys=True)

    assert treated == baseline
    assert "thinking" not in json.loads(treated), (
        "the private lane acquired a thinking parameter it must never carry"
    )


def test_extra_params_do_not_leak_across_models_in_one_gateway(
    wire_bodies: list[dict[str, Any]],
) -> None:
    """Declared per-MODEL, not per-gateway: a param on the frontier deployment
    must not appear on a different deployment served by the same Router."""
    gw = _gateway(_DISABLED_THINKING)
    _call(gw, "agent")
    frontier_body = wire_bodies[-1]["body"]
    _call(gw, "reason")
    private_body = wire_bodies[-1]["body"]
    assert frontier_body["thinking"] == {"type": "disabled"}
    assert "thinking" not in private_body
