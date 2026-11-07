# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Registry -> gateway bridge: build_gateway_settings constructs NE-010's
GatewaySettings from registered entries + deployment bindings."""

from __future__ import annotations

import asyncio

import pytest

from ne.contracts import ChatMessage, CompletionRequest, Lane
from ne.gateway import (
    GatewayConfigError,
    LiteLLMGateway,
    ModelBinding,
    RegistryValidationError,
    TaskRoute,
    UnknownModelIdError,
    build_gateway_settings,
    default_vllm_litellm_model,
    entry_to_model_def,
    load_menu,
)
from ne.gateway.menus import MENU_CAPABILITY_FIRST

from .conftest import RecordingCostSink

_MSGS = (ChatMessage(role="user", content="hi"),)


def test_build_gateway_settings_from_menu() -> None:
    reg = load_menu(MENU_CAPABILITY_FIRST)
    bindings = {
        entry.model_id: ModelBinding(
            litellm_model=default_vllm_litellm_model(entry),
            api_base="http://vllm:8000",
        )
        for entry in reg.list_models()
    }
    tasks = {"draft": TaskRoute(model="glm-5.2", fallbacks=("deepseek-v4-flash",))}
    settings = build_gateway_settings(reg, bindings=bindings, tasks=tasks)

    by_name = {m.name: m for m in settings.models}
    glm = by_name["glm-5.2"]
    assert glm.name == "glm-5.2"
    assert glm.provider == "zai-org"
    assert glm.lane == Lane.PRIVATE_VLLM
    assert glm.litellm_model == "hosted_vllm/glm-5.2"
    assert glm.api_base == "http://vllm:8000"


def test_binding_unknown_model_id_rejected() -> None:
    reg = load_menu(MENU_CAPABILITY_FIRST)
    bindings = {
        "ghost": ModelBinding(litellm_model="hosted_vllm/ghost"),
    }
    with pytest.raises(UnknownModelIdError) as exc_info:
        build_gateway_settings(reg, bindings=bindings, tasks={})
    msg = str(exc_info.value)
    assert "ghost" in msg
    assert "glm-5.2" in msg


def test_entry_to_model_def_lane_must_be_member() -> None:
    reg = load_menu(MENU_CAPABILITY_FIRST)
    entry = reg.get("glm-5.2")
    with pytest.raises(RegistryValidationError) as exc_info:
        entry_to_model_def(entry, litellm_model="anthropic/glm", lane=Lane.FRONTIER_API)
    msg = str(exc_info.value)
    assert "glm-5.2" in msg
    assert "private-vllm" in msg

    # No explicit lane: defaults to entry.lanes[0].
    model_def = entry_to_model_def(entry, litellm_model="hosted_vllm/glm-5.2")
    assert model_def.lane == Lane.PRIVATE_VLLM


def test_unbound_task_reference_fails_gateway_boot_validation() -> None:
    reg = load_menu(MENU_CAPABILITY_FIRST)
    entry = reg.get("glm-5.2")
    bindings = {
        "glm-5.2": ModelBinding(litellm_model=default_vllm_litellm_model(entry)),
    }
    tasks = {"draft": TaskRoute(model="qwen3.5-397b-a17b")}
    with pytest.raises(GatewayConfigError) as exc_info:
        build_gateway_settings(reg, bindings=bindings, tasks=tasks)
    msg = str(exc_info.value)
    assert "tasks.draft.model" in msg
    assert "qwen3.5-397b-a17b" in msg


def test_built_settings_drive_real_gateway_end_to_end(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    reg = load_menu(MENU_CAPABILITY_FIRST)
    entry = reg.get("glm-5.2")
    bindings = {
        "glm-5.2": ModelBinding(
            litellm_model=default_vllm_litellm_model(entry),
            extra_params={"api_key": "test", "mock_response": "ok"},
        ),
    }
    tasks = {"draft": TaskRoute(model="glm-5.2")}
    settings = build_gateway_settings(reg, bindings=bindings, tasks=tasks)

    gw = LiteLLMGateway(settings, sink)
    result = asyncio.run(gw.complete(CompletionRequest(messages=_MSGS, task="draft")))
    assert result.text == "ok"


# -- NE-CTXB-2a: context_window threading + ModelBinding narrowing -----------


def test_entry_to_model_def_threads_context_window() -> None:
    reg = load_menu(MENU_CAPABILITY_FIRST)
    entry = reg.get("glm-5.2")  # context_window=1_000_000
    md = entry_to_model_def(entry, litellm_model="hosted_vllm/glm-5.2")
    assert md.context_window == entry.context_window == 1_000_000
    # Default reserve = min(4096, cw // 8) -> 4096.
    assert md.max_output_tokens == 4096


def test_binding_context_window_narrowing_ok() -> None:
    reg = load_menu(MENU_CAPABILITY_FIRST)
    entry = reg.get("glm-5.2")
    md = entry_to_model_def(entry, litellm_model="hosted_vllm/glm-5.2", context_window=32768)
    assert md.context_window == 32768
    # Reserve default now derives from the NARROWED window: min(4096, 32768//8)=4096.
    assert md.max_output_tokens == 4096


def test_binding_context_window_widening_rejected() -> None:
    reg = load_menu(MENU_CAPABILITY_FIRST)
    entry = reg.get("qwen3.5-397b-a17b")  # context_window=262_144
    with pytest.raises(RegistryValidationError) as exc_info:
        entry_to_model_def(
            entry,
            litellm_model="hosted_vllm/qwen3.5-397b-a17b",
            context_window=300_000,
        )
    msg = str(exc_info.value)
    assert "qwen3.5-397b-a17b" in msg
    assert "262144" in msg
    assert "300000" in msg


def test_binding_max_output_tokens_override_threads() -> None:
    reg = load_menu(MENU_CAPABILITY_FIRST)
    entry = reg.get("glm-5.2")
    md = entry_to_model_def(
        entry,
        litellm_model="hosted_vllm/glm-5.2",
        context_window=32768,
        max_output_tokens=1000,
    )
    assert md.context_window == 32768
    assert md.max_output_tokens == 1000


def test_build_gateway_settings_carries_window_and_binding_overrides() -> None:
    reg = load_menu(MENU_CAPABILITY_FIRST)
    entry = reg.get("glm-5.2")
    bindings = {
        "glm-5.2": ModelBinding(
            litellm_model=default_vllm_litellm_model(entry),
            context_window=65536,
            max_output_tokens=2048,
        ),
    }
    tasks = {"draft": TaskRoute(model="glm-5.2")}
    settings = build_gateway_settings(reg, bindings=bindings, tasks=tasks)
    glm = {m.name: m for m in settings.models}["glm-5.2"]
    assert glm.context_window == 65536
    assert glm.max_output_tokens == 2048
