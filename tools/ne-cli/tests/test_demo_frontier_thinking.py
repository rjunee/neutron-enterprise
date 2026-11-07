# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The frontier lane's per-model provider params — and the guardian that keeps a
model swap from silently re-acquiring the empty-answer failure mode.

BACKGROUND. ``anthropic/claude-sonnet-5`` runs ADAPTIVE THINKING when a request
omits ``thinking``, and defaults thinking ``display`` to ``"omitted"``. That
returns a thinking block whose ``thinking`` field is the empty string plus a
signature and — intermittently — no text block at all. Nothing in this repo sends
``thinking`` or ``reasoning_effort``, so NOT asking is what selects that
behaviour. Live audit row, owner's demo question, 2026-08-26::

    ne.gateway.complete  outcome=error  resource=anthropic/claude-sonnet-5
      finish_reason=stop   output_tokens=144   input_tokens=5792
      response_channels=thinking_blocks=1,provider_specific_fields=2
      empty_completion_tokens=144

The model completed normally, billed 144 reasoning tokens, and served nothing.

WHAT IS TESTED HERE is the CONFIG half — that the right rows carry the parameter
and the wrong rows do not. That the parameter then survives all the way onto the
provider request body is the separate, load-bearing half, pinned by
``packages/ne-gateway/tests/test_extra_params_reach_wire.py``. Neither half is
sufficient alone: config that never egresses looks fixed and is not.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from ne.cli.demo import live
from ne.gateway import ModelDef

_MODELS_LINE = re.compile(r"NE_GATEWAY_MODELS:\s*'(.+?)'")

_GPU_VARIANTS = (None, "https://pod-8000.proxy.runpod.net/v1")
_FRONTIER_VARIANTS = (None, "anthropic", "openai")

_DISABLED = {"type": "disabled"}


def _models(gpu: str | None, frontier: str | None) -> dict[str, dict[str, Any]]:
    env = live._gateway_lane_env(gpu, frontier=frontier)
    match = _MODELS_LINE.search(env)
    assert match, f"_gateway_lane_env({gpu!r}, {frontier!r}) emitted no NE_GATEWAY_MODELS"
    return {m["name"]: m for m in json.loads(match.group(1))}


def _all_emitted() -> list[tuple[str, str | None, str | None, dict[str, Any]]]:
    """Every (name, gpu, frontier, row) the producer can emit."""
    out = []
    for gpu in _GPU_VARIANTS:
        for frontier in _FRONTIER_VARIANTS:
            for name, row in _models(gpu, frontier).items():
                out.append((name, gpu, frontier, row))
    return out


# -- the guardian -------------------------------------------------------------


@pytest.mark.parametrize("pinned", [None, "anthropic/claude-sonnet-5"])
def test_no_emitted_frontier_model_silently_thinks_by_default(
    pinned: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE GUARDIAN. Any frontier model whose provider thinks-unless-told-not-to
    must either carry the disable or be one the provider refuses the disable on.

    This is the check that would have caught the live incident, and the one that
    catches the NEXT model bump: changing ``_FRONTIER_LITELLM_MODEL`` to a
    thinking-by-default id without adding a table entry fails here rather than in
    production.

    PARAMETRIZED OVER THE PIN, and that is what keeps it honest. Against this
    branch's real pin (``None``) the guardian sweeps rows but finds no
    thinking-by-default model, so it would pass no matter what the table said —
    a vacuous guard. The second case swaps the pin to the id the table is keyed
    on, so the assertion below actually has something to reject. Mutation-checked:
    emptying ``_FRONTIER_MODEL_EXTRA_PARAMS`` fails the second case (and only the
    second).
    """
    if pinned is not None:
        monkeypatch.setitem(live._FRONTIER_LITELLM_MODEL, "anthropic", pinned)
        assert any(
            row["litellm_model"] in live._ADAPTIVE_THINKING_DEFAULT_ON
            and row["litellm_model"] not in live._THINKING_DISABLE_REFUSED
            for _, _, _, row in _all_emitted()
            if row["lane"] == "frontier-api"
        ), "non-vacuity: this case must emit at least one row the guardian can reject"

    for name, gpu, frontier, row in _all_emitted():
        if row["lane"] != "frontier-api":
            continue
        model = row["litellm_model"]
        if model not in live._ADAPTIVE_THINKING_DEFAULT_ON:
            continue
        if model in live._THINKING_DISABLE_REFUSED:
            continue
        thinking = row.get("extra_params", {}).get("thinking")
        assert thinking == _DISABLED, (
            f"{name!r} (gpu={gpu!r}, frontier={frontier!r}) serves {model}, which "
            "thinks unless told not to, but carries no thinking disable — this is "
            "the empty-answer failure mode. Add an entry to "
            "_FRONTIER_MODEL_EXTRA_PARAMS, or to _THINKING_DISABLE_REFUSED if the "
            "provider rejects the disable on it."
        )


def test_refused_and_required_sets_are_disjoint_in_effect() -> None:
    """A model may not be BOTH told to disable thinking and known to 400 on it.

    Without this, adding a model to both tables would emit a row that is
    guaranteed to fail every call while the guardian above waves it through.
    """
    for model in live._THINKING_DISABLE_REFUSED:
        declared = live._FRONTIER_MODEL_EXTRA_PARAMS.get(model, {})
        assert "thinking" not in declared, (
            f"{model} is in _THINKING_DISABLE_REFUSED but _FRONTIER_MODEL_EXTRA_PARAMS "
            "declares a thinking param for it — the provider would 400 every call"
        )


def test_every_declared_thinking_disable_targets_a_thinking_model() -> None:
    """The table must not carry a disable for a model that never thinks anyway —
    that would be an unexplained provider param riding the wire for no reason."""
    for model, params in live._FRONTIER_MODEL_EXTRA_PARAMS.items():
        if "thinking" not in params:
            continue
        assert model in live._ADAPTIVE_THINKING_DEFAULT_ON, (
            f"{model} declares a thinking disable but is not in "
            "_ADAPTIVE_THINKING_DEFAULT_ON; either it does not need the param or "
            "the set is stale"
        )


# -- what this branch actually emits -------------------------------------------


def test_this_branch_emits_the_disable_on_the_live_lane_only() -> None:
    """WHAT THIS BRANCH ACTUALLY EMITS — and the config_hash flag.

    ``_FRONTIER_LITELLM_MODEL`` pins ``anthropic/claude-sonnet-5``, so
    ``frontier-default`` DOES carry the params and the emitted
    ``NE_GATEWAY_MODELS`` JSON changes. That is a config_hash-affecting,
    RELEASE-LEVEL change, deliberately taken.

    Everything else emits nothing: the two private rows, the openai swap target,
    and ``frontier-best`` (``claude-fable-5``, which the provider 400s on a
    thinking disable). Pinned exhaustively so the blast radius is exactly one row.
    """
    for name, gpu, frontier, row in _all_emitted():
        expected_live = name == "frontier-default" and frontier == "anthropic"
        if expected_live:
            assert row["extra_params"] == {
                "thinking": _DISABLED,
                "allowed_openai_params": ["thinking"],
            }, f"{name!r} (gpu={gpu!r}) emitted {row.get('extra_params')!r}"
        else:
            assert "extra_params" not in row, (
                f"{name!r} (gpu={gpu!r}, frontier={frontier!r}) emits extra_params "
                f"{row.get('extra_params')!r} but must emit none"
            )


def test_frontier_best_is_exempt_because_the_provider_refuses_the_disable() -> None:
    """``frontier-best`` names ``anthropic/claude-fable-5``. Anthropic rejects
    ``thinking: {"type": "disabled"}`` on Fable 5 with a 400 — thinking is
    structurally always-on there — so this row must stay bare.

    This is the reason ``frontier-best`` and ``frontier-default`` DIVERGE rather
    than carrying the same parameter. It is a provider fact, not a preference:
    writing the disable onto this row would convert an inert config-only row into
    a guaranteed 400 the moment the NE-161 runtime gate ever opened.
    """
    best = _models(None, "anthropic")["frontier-best"]
    assert best["litellm_model"] == "anthropic/claude-fable-5"
    assert best["litellm_model"] in live._THINKING_DISABLE_REFUSED
    assert "extra_params" not in best


def test_absent_rather_than_empty() -> None:
    """``_frontier_extra_params`` returns ``{}``, never ``{"extra_params": {}}``.

    An empty dict would still change the emitted JSON bytes — and so the
    config_hash — for a model that needs nothing at all."""
    assert live._frontier_extra_params("anthropic/claude-sonnet-4-5") == {}
    assert live._frontier_extra_params("openai/gpt-5.5") == {}


# -- the swap target: what the table does when the pin moves to sonnet-5 --------


@pytest.fixture
def sonnet5(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the anthropic swap target at the id the table has an entry for.

    This is the ONLY way to exercise the live path on this branch: the entry is
    keyed by ``anthropic/claude-sonnet-5``, which this branch's pin deliberately
    does not name (it is outside pinned litellm's local cost map). Monkeypatching
    the pin — rather than adding a second, parallel fake table — keeps the test
    driving the REAL producer end to end.
    """
    monkeypatch.setitem(live._FRONTIER_LITELLM_MODEL, "anthropic", "anthropic/claude-sonnet-5")


def test_swap_target_carries_the_disable_and_validates_as_a_modeldef(sonnet5: None) -> None:
    """The whole config path: table -> emitted NE_GATEWAY_MODELS JSON -> ModelDef.

    Validating as a real ``ModelDef`` matters because ``extra="forbid"`` means a
    misspelled key here would be a LOUD boot failure, not a silent drop."""
    row = _models(None, "anthropic")["frontier-default"]
    assert row["extra_params"]["thinking"] == _DISABLED
    # Required alongside `thinking`: litellm gates the param on the model's
    # supports_reasoning, read from the packaged cost map that the C3 chokepoint
    # forces, and this id is not in it. Without the escape hatch litellm raises
    # UnsupportedParamsError before building a request and EVERY call on the lane
    # fails. Proven in packages/ne-gateway/tests/test_extra_params_reach_wire.py.
    assert row["extra_params"]["allowed_openai_params"] == ["thinking"]

    model = ModelDef.model_validate(row)
    assert model.extra_params["thinking"] == _DISABLED
    assert model.lane.value == "frontier-api"


def test_private_lane_rows_are_untouched_by_the_swap(sonnet5: None) -> None:
    """THE PAIRED MUTATION. The private lane serves a hybrid-thinking model whose
    reasoning shares its ONE completion budget; disabling thinking there is a
    different decision with different consequences and is out of scope.

    Compared byte-for-byte against the same rows emitted with no frontier swap at
    all, so this fails if the frontier entry perturbs the private rows in ANY
    way, not merely if it adds a thinking key.
    """
    for gpu in _GPU_VARIANTS:
        swapped = _models(gpu, "anthropic")
        baseline = _models(gpu, None)
        for name in ("private-default",):
            assert json.dumps(swapped[name], sort_keys=True) == json.dumps(
                baseline[name], sort_keys=True
            ), f"{name!r} (gpu={gpu!r}) changed when the frontier lane was swapped"
            assert "extra_params" not in swapped[name]


def test_best_and_default_agree_when_they_name_the_same_model(sonnet5: None) -> None:
    """``frontier-best`` and ``frontier-default`` are consistent BY CONSTRUCTION:
    both read the one table, keyed by the model each names. Same model, same
    params; different model, whatever that model needs.

    On this branch ``frontier-best`` names ``claude-sonnet-4-5`` while the swapped
    ``frontier-default`` names ``claude-sonnet-5``, so they legitimately differ —
    what is pinned is that the difference tracks the MODEL and nothing else.
    """
    models = _models(None, "anthropic")
    best, default = models["frontier-best"], models["frontier-default"]
    for row in (best, default):
        expected = live._frontier_extra_params(row["litellm_model"])
        assert row.get("extra_params", None) == expected.get("extra_params", None)
