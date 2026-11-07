# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-GATEWAY-FROZENDICT — the egress boundary thaws nested frozen params.

``CompletionRequest.params`` is an ``AnyMap``: pydantic coerces it to a
``FrozenDict``, which — per ``_freeze_value`` — recursively freezes every
NESTED mapping value too, not just the top level. A live rig extraction leg
broke wiring a ``response_format`` param through the gateway with
``TypeError: Unsupported response_format type - FrozenDict(...)`` raised by
litellm's own ``type_to_response_format_param``
(``litellm/llms/base_llm/base_utils.py``, vendored 1.90.3), which requires
``isinstance(response_format, dict)`` and rejects any other ``Mapping`` — a
bare ``FrozenDict`` included.

TWO VACUITY-ADJACENT FACTS drove this module's design (both verified live in
this worktree before writing the fix):

1. **litellm's ``mock_response`` short-circuit is provider-family-dependent,
   NOT a blanket bypass of ``type_to_response_format_param`` — verified live
   on BOTH lanes.** For ``openai``-family models (``FRONTIER_API`` here),
   litellm's ``get_optional_params`` takes a ``provider_config`` branch that
   ``mock_response`` skips entirely, so ``gw.complete()`` with the frozen
   nested param COMPLETES pre-fix under ``mock_response`` on that lane — an
   end-to-end mock-response test IS vacuous for ``FRONTIER_API``. But for
   ``hosted_vllm`` (``PRIVATE_VLLM`` — the lane the live rig actually broke
   on), the converter runs BEFORE the ``mock_response`` short-circuit: an
   un-monkeypatched ``gw.complete()`` with the frozen nested param RAISES
   ``ModelCallFailedError`` wrapping litellm's own
   ``TypeError: Unsupported response_format type - FrozenDict(...)`` even
   pre-fix. A plain e2e mock-response test on ``PRIVATE_VLLM`` alone would
   therefore NOT have been vacuous. The reason this module still captures
   the exact per-attempt kwargs at the ``Router.acompletion`` seam (mirroring
   ``test_redaction_seam.py``'s ``_install_recorder`` pattern) rather than
   asserting on an e2e ``gw.complete()`` outcome is not "the e2e path is
   vacuous" but that the capture (a) pins the exact boundary value
   UNIFORMLY on every lane regardless of which litellm internal branch that
   lane's provider happens to take, and (b) lets the captured value be fed
   directly into the REAL litellm converter function
   (``test_captured_response_format_accepted_by_real_litellm_converter``),
   which an e2e ``gw.complete()`` assertion cannot do.

2. **``FrozenDict.__eq__`` intentionally compares equal to a plain
   ``dict``.** An equality assertion (``captured["response_format"] ==
   {...}``) would pass whether or not thaw ran, because an untouched
   ``FrozenDict`` already compares equal to its thawed shape. Every
   assertion in this module therefore checks the CONCRETE runtime type via
   ``type(x) is dict`` / ``type(x) is list``, never ``==`` alone, and does
   so at EVERY nesting level captured — see
   ``test_litellm_converter_still_rejects_frozendict_control`` for the
   control arm that makes this whole proof non-vacuous.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from litellm.llms.base_llm.base_utils import type_to_response_format_param

from ne.contracts import ChatMessage, CompletionRequest, FrozenDict, Lane
from ne.gateway import LiteLLMGateway, TaskRoute

from .conftest import RecordingCostSink, SubstringRedactor, make_model, make_settings

_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "extraction",
        "schema": {
            "type": "object",
            "properties": {"assertions": {"type": "array"}},
            # A sequence value nested two levels deep, containing a further
            # nested mapping — this is the shape the walk's `list` branch
            # must actually exercise (a bare fixture with no sequence value
            # at all leaves that branch dead code; see
            # ``_assert_concrete_builtin_tree``'s module-docstring-linked
            # coverage note below).
            "required": ["assertions", {"nested": "still-must-thaw"}],
        },
    },
}


def _install_recorder(monkeypatch: pytest.MonkeyPatch, gw: LiteLLMGateway) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append({"model": model, "messages": messages, **kwargs})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            _hidden_params={"response_cost": 0.0},
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    return calls


def _assert_concrete_builtin_tree(value: Any) -> None:
    """Recursively assert every Mapping-shaped node is a literal ``dict`` and
    every sequence-shaped node is a literal ``list`` — the concrete-type walk
    that a `==`-based assertion cannot substitute for (see module docstring
    vacuity fact 2).

    The ``else`` branch below is load-bearing, not a catch-all: a ``tuple``/
    ``set``/``frozenset``/other-``Mapping`` node is neither ``dict`` nor
    ``list``, so an unfixed thaw that stops one layer early (or a sequence
    branch that regresses to identity) produces exactly such a node here.
    Without an explicit rejection, that node would fall through silently and
    NEVER BE TRAVERSED — every frozen value nested inside it would be
    invisible to this walk even though the walk's own docstring/module
    docstring claims coverage "at EVERY nesting level captured". The
    explicit ``assert not isinstance(value, (tuple, set, frozenset, Mapping))``
    below turns that silent miss into an immediate, loud failure at the
    exact node that was missed, instead of a false pass two frames up.
    """
    if isinstance(value, dict):
        assert type(value) is dict
        for v in value.values():
            _assert_concrete_builtin_tree(v)
    elif isinstance(value, list):
        assert type(value) is list
        for v in value:
            _assert_concrete_builtin_tree(v)
    else:
        # Not a container: must not be a raw tuple/set/frozenset/Mapping
        # (FrozenDict included — it is itself a Mapping) that thaw failed to
        # normalize into dict/list. Those types would otherwise slip through
        # this else branch undetected — see the docstring above and the
        # Argus-review P1 this guards against.
        assert not isinstance(value, (tuple, set, frozenset, Mapping))


# -- THE boundary proof: PRIVATE_VLLM (byte-identical pass-through) ----------


def test_private_lane_forwards_nested_params_as_literal_builtins(
    sink: RecordingCostSink, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRIVATE_VLLM is the lane the live rig actually broke on: NE-162's
    ``_prepare_egress_body`` forwards ``merged`` byte-identical for this lane
    (no redaction rebuild), so this test isolates ``_call_params``'s thaw as
    the ONLY thing that can be normalizing the nested param."""
    settings = make_settings(
        [
            make_model(
                "vllm",
                lane=Lane.PRIVATE_VLLM,
                litellm_model="hosted_vllm/x",
                provider="vllm",
                api_base="http://vllm:8000/v1",
            )
        ],
        {"extract": TaskRoute(model="vllm")},
    )
    gw = LiteLLMGateway(settings, sink)
    calls = _install_recorder(monkeypatch, gw)

    request = CompletionRequest(
        messages=(ChatMessage(role="user", content="extract"),),
        task="extract",
        params={"response_format": _RESPONSE_FORMAT},
    )
    asyncio.run(gw.complete(request))

    assert len(calls) == 1
    captured = calls[0]["response_format"]
    _assert_concrete_builtin_tree(captured)
    assert captured == _RESPONSE_FORMAT


# -- sibling path (plan Section 2.1 symmetry): FRONTIER_API redacted path ----


def test_frontier_lane_forwards_nested_params_as_literal_builtins(
    sink: RecordingCostSink, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling coverage for the redacted FRONTIER_API path: the redactor
    rebuilds the messages body, but ``merged`` params flow through
    ``_call_params``'s thaw exactly like the private lane."""
    settings = make_settings(
        [make_model("front", lane=Lane.FRONTIER_API)],
        {"extract": TaskRoute(model="front")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=SubstringRedactor())
    calls = _install_recorder(monkeypatch, gw)

    request = CompletionRequest(
        messages=(ChatMessage(role="user", content="extract, no secrets here"),),
        task="extract",
        params={"response_format": _RESPONSE_FORMAT},
    )
    asyncio.run(gw.complete(request))

    assert len(calls) == 1
    captured = calls[0]["response_format"]
    _assert_concrete_builtin_tree(captured)
    assert captured == _RESPONSE_FORMAT


# -- streaming entry point: _call_params is a SHARED helper, but the seam ---
# -- capture above only exercises complete()'s call site. This closes that --
# -- coverage gap (Argus-review P3) rather than trusting the shared-helper --
# -- argument without a witness on the second call site. ---------------------


def test_stream_lane_forwards_nested_params_as_literal_builtins(
    sink: RecordingCostSink, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_stream`` calls the same ``_call_params`` helper as ``complete()``
    (``ne.gateway.gateway.LiteLLMGateway._stream``), but at a SEPARATE call
    site -- a regression that broke only the streaming site (e.g. a future
    refactor that inlines a fresh, un-thawed ``merged`` dict into ``_stream``
    alone) would not be caught by the ``complete()``-only tests above. This
    test is that second witness, on the same PRIVATE_VLLM byte-identical
    lane as the load-bearing ``complete()`` test."""
    settings = make_settings(
        [
            make_model(
                "vllm",
                lane=Lane.PRIVATE_VLLM,
                litellm_model="hosted_vllm/x",
                provider="vllm",
                api_base="http://vllm:8000/v1",
            )
        ],
        {"extract": TaskRoute(model="vllm")},
    )
    gw = LiteLLMGateway(settings, sink)
    calls: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append({"model": model, "messages": messages, **kwargs})

        async def gen() -> Any:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok"))])

        return gen()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)

    request = CompletionRequest(
        messages=(ChatMessage(role="user", content="extract"),),
        task="extract",
        params={"response_format": _RESPONSE_FORMAT},
    )

    async def _drain() -> None:
        async for _ in gw.stream(request):
            pass

    asyncio.run(_drain())

    assert len(calls) == 1
    captured = calls[0]["response_format"]
    _assert_concrete_builtin_tree(captured)
    assert captured == _RESPONSE_FORMAT


# -- the captured value round-trips through the REAL litellm converter -------


def test_captured_response_format_accepted_by_real_litellm_converter(
    sink: RecordingCostSink, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No mock of the seam under test: feeds the value actually captured at
    the router boundary into litellm's real
    ``type_to_response_format_param`` and asserts it returns a dict without
    raising — the exact function/call the live rig failed on."""
    settings = make_settings(
        [
            make_model(
                "vllm",
                lane=Lane.PRIVATE_VLLM,
                litellm_model="hosted_vllm/x",
                provider="vllm",
                api_base="http://vllm:8000/v1",
            )
        ],
        {"extract": TaskRoute(model="vllm")},
    )
    gw = LiteLLMGateway(settings, sink)
    calls = _install_recorder(monkeypatch, gw)

    request = CompletionRequest(
        messages=(ChatMessage(role="user", content="extract"),),
        task="extract",
        params={"response_format": _RESPONSE_FORMAT},
    )
    asyncio.run(gw.complete(request))

    captured = calls[0]["response_format"]
    result = type_to_response_format_param(captured)
    assert type(result) is dict
    assert result == _RESPONSE_FORMAT


# -- CONTROL ARM: the tripwire that keeps this proof non-vacuous -------------


def test_litellm_converter_still_rejects_frozendict_control() -> None:
    """This control is what makes the whole boundary proof non-vacuous.

    Without it, a future litellm release that relaxes
    ``type_to_response_format_param``'s ``isinstance(x, dict)`` check to
    accept any ``Mapping`` would silently turn every other test in this
    module into a tautology — they would stay green whether or not
    ``_call_params`` still thaws anything, because litellm would no longer
    care. This test asserts the REJECTION still happens, so that relaxation
    flips this test RED and forces re-derivation of this whole proof at the
    next litellm pin bump, instead of letting the boundary silently rot.
    """
    frozen = FrozenDict(_RESPONSE_FORMAT)
    with pytest.raises(TypeError, match="Unsupported response_format type"):
        type_to_response_format_param(frozen)  # type: ignore[arg-type]
