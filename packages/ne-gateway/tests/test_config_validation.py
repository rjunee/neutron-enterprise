# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Boot-time GatewaySettings validation — fail-loud with exact key paths."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import ne.contracts.tokens as ne_tokens
from ne.contracts import Lane
from ne.gateway import GatewayConfigError, GatewaySettings, ModelDef, TaskRoute
from ne.gateway.config import (
    TOOL_CATALOG_FLOOR_TOKENS,
    overhead_margin_for,
    validate_tool_catalog_budgets,
)


def _env_model_json(**overrides: object) -> str:
    entry: dict[str, object] = {
        "name": "a",
        "litellm_model": "openai/x",
        "provider": "openai",
        "lane": "frontier-api",
        "context_window": 32768,
    }
    entry.update(overrides)
    return json.dumps([entry])


def _model(name: str, lane: Lane = Lane.FRONTIER_API) -> ModelDef:
    return ModelDef(
        name=name,
        litellm_model="gpt-3.5-turbo",
        provider="openai",
        lane=lane,
        context_window=32768,
    )


def test_requires_at_least_one_model() -> None:
    with pytest.raises(GatewayConfigError) as excinfo:
        GatewaySettings(models=[])
    assert "at least one model" in str(excinfo.value)


def test_duplicate_model_names_rejected() -> None:
    with pytest.raises(GatewayConfigError) as excinfo:
        GatewaySettings(models=[_model("a"), _model("a")])
    assert "models[1].name 'a' duplicates models[0].name" in str(excinfo.value)


def test_task_primary_must_name_configured_model() -> None:
    with pytest.raises(GatewayConfigError) as excinfo:
        GatewaySettings(
            models=[_model("a")],
            tasks={"draft": TaskRoute(model="nope")},
        )
    assert "tasks.draft.model 'nope'" in str(excinfo.value)


def test_task_fallback_must_name_configured_model() -> None:
    with pytest.raises(GatewayConfigError) as excinfo:
        GatewaySettings(
            models=[_model("a")],
            tasks={"draft": TaskRoute(model="a", fallbacks=("ghost",))},
        )
    assert "tasks.draft.fallbacks[0]" in str(excinfo.value)


def test_valid_config_accepted() -> None:
    settings = GatewaySettings(
        models=[_model("a"), _model("b")],
        tasks={"draft": TaskRoute(model="a", fallbacks=("b",))},
    )
    assert [m.name for m in settings.models] == ["a", "b"]
    assert settings.tasks["draft"].fallbacks == ("b",)


def test_env_layer_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_GATEWAY_NUM_RETRIES", "5")
    settings = GatewaySettings(models=[_model("a")])
    assert settings.num_retries == 5


# -- NE-CTXB-2a: context_window field + validators ---------------------------


def test_missing_context_window_is_boot_error() -> None:
    """GUARDIAN #2: a ModelDef without context_window fails LOUDLY (required)."""
    with pytest.raises(ValidationError) as excinfo:
        ModelDef.model_validate(
            {"name": "a", "litellm_model": "openai/x", "provider": "openai", "lane": "frontier-api"}
        )
    assert "context_window" in str(excinfo.value)


def test_unknown_key_rejected_direct() -> None:
    """GUARDIAN #1 (direct): extra='forbid' rejects a typo'd key on ModelDef."""
    with pytest.raises(ValidationError) as excinfo:
        ModelDef.model_validate(
            {
                "name": "a",
                "litellm_model": "openai/x",
                "provider": "openai",
                "lane": "frontier-api",
                "context_window": 32768,
                "context_windw": 1,  # typo
            }
        )
    assert "context_windw" in str(excinfo.value)


def test_unknown_env_json_key_is_boot_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """GUARDIAN #1 (the real fix): a typo'd key in NE_GATEWAY_MODELS env JSON is
    no longer silently dropped — it raises at GatewaySettings() boot."""
    monkeypatch.setenv("NE_GATEWAY_MODELS", _env_model_json(context_windw=1))
    with pytest.raises(ValidationError) as excinfo:
        GatewaySettings(tasks={})
    assert "context_windw" in str(excinfo.value)


def test_env_json_context_window_reaches_model_def(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_GATEWAY_MODELS", _env_model_json(context_window=65536))
    settings = GatewaySettings(tasks={})
    assert settings.models[0].context_window == 65536


def test_max_output_tokens_default_large_window() -> None:
    md = ModelDef(
        name="a",
        litellm_model="openai/x",
        provider="openai",
        lane=Lane.FRONTIER_API,
        context_window=32768,
    )
    assert md.max_output_tokens == 4096  # min(4096, 32768 // 8)


def test_max_output_tokens_default_small_window() -> None:
    md = ModelDef(
        name="a",
        litellm_model="openai/x",
        provider="openai",
        lane=Lane.FRONTIER_API,
        context_window=2048,
    )
    assert md.max_output_tokens == 256  # min(4096, 2048 // 8)


def test_explicit_max_output_tokens_at_least_half_window_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ModelDef(
            name="a",
            litellm_model="openai/x",
            provider="openai",
            lane=Lane.FRONTIER_API,
            context_window=8192,
            max_output_tokens=4096,
        )
    msg = str(excinfo.value)
    assert "4096" in msg  # both the value and the window//2 bound are 4096 here


def test_overhead_margin_policy() -> None:
    assert overhead_margin_for(2048) == 1024  # floor wins
    assert overhead_margin_for(100_000) == 5000  # 5% wins


def test_boot_rejects_window_too_small_for_reserve_and_margin() -> None:
    """A 1024 window: reserve=128 (min(4096,1024//8)), margin=1024 -> input
    budget < 0 -> GatewayConfigError naming models[0]."""
    with pytest.raises(GatewayConfigError) as excinfo:
        GatewaySettings(
            models=[
                ModelDef(
                    name="tiny",
                    litellm_model="openai/x",
                    provider="openai",
                    lane=Lane.FRONTIER_API,
                    context_window=1024,
                )
            ],
        )
    assert "models[0]" in str(excinfo.value)


def test_bytes_per_token_default_is_the_contracts_constant() -> None:
    """No-hardcode GUARDIAN: the default estimator divisor IS the module
    constant (empirical ~1.5), never a re-hardcoded 3.0/1.5 literal."""
    settings = GatewaySettings(models=[_model("a")])
    assert settings.bytes_per_token == ne_tokens.DEFAULT_BYTES_PER_TOKEN
    assert settings.per_call_overhead == ne_tokens.DEFAULT_PER_CALL_OVERHEAD
    est = settings.estimator()
    assert est.bytes_per_token == ne_tokens.DEFAULT_BYTES_PER_TOKEN
    assert est.per_call_overhead == ne_tokens.DEFAULT_PER_CALL_OVERHEAD


def test_validate_tool_catalog_budgets_pass() -> None:
    settings = GatewaySettings(
        models=[_model("a")],  # context_window=32768 -> input_budget large
        tasks={"draft": TaskRoute(model="a")},
    )
    # A small catalog easily fits.
    validate_tool_catalog_budgets(settings, {"draft": "a small tool catalog"})


def test_validate_tool_catalog_budgets_overflow() -> None:
    # Tiny 2048 window: input_budget = 2048 - 256 - 1024 = 768. estimator(1.5,8)
    # over a 2000-char ascii catalog -> ceil(2000/1.5)+8 = 1342 tokens; +512
    # floor overflows 768.
    settings = GatewaySettings(
        models=[
            ModelDef(
                name="a",
                litellm_model="openai/x",
                provider="openai",
                lane=Lane.FRONTIER_API,
                context_window=2048,
            )
        ],
        tasks={"draft": TaskRoute(model="a")},
    )
    catalog = "x" * 2000
    with pytest.raises(GatewayConfigError) as excinfo:
        validate_tool_catalog_budgets(settings, {"draft": catalog})
    msg = str(excinfo.value)
    assert "draft" in msg
    assert "input_budget=768" in msg
    assert str(TOOL_CATALOG_FLOOR_TOKENS) in msg
    # C12: the catalog TEXT never appears in the error.
    assert catalog not in msg
    assert "xxxx" not in msg
