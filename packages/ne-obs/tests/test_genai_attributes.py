# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""genai_attributes(): attribute mapping, None-omission, config_hash stamping."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ne.contracts import (
    ConfigHashNotFrozenError,
    freeze_config_hash,
    reset_config_hash_for_testing,
)
from ne.contracts.config import NeSettings
from ne.obs import genai_attributes
from ne.obs.genai import (
    ATTR_INPUT_TOKENS,
    ATTR_NE_CONFIG_HASH,
    ATTR_NE_COST_USD,
    ATTR_NE_KPI_TAG,
    ATTR_NE_LANE,
    ATTR_OPERATION_NAME,
    ATTR_OUTPUT_TOKENS,
    ATTR_PROVIDER_NAME,
    ATTR_REQUEST_MODEL,
    ATTR_RESPONSE_MODEL,
)


class _DemoSettings(NeSettings):
    demo: int = 3


@pytest.fixture
def frozen_config() -> Iterator[str]:
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


def test_full_attribute_mapping(frozen_config: str) -> None:
    attrs = genai_attributes(
        operation="chat",
        provider="anthropic",
        request_model="claude-opus",
        response_model="claude-opus-2026",
        input_tokens=100,
        output_tokens=50,
        lane="frontier-api",
        cost_usd=0.01,
        kpi_tag="latency",
    )
    assert attrs[ATTR_OPERATION_NAME] == "chat"
    assert attrs[ATTR_PROVIDER_NAME] == "anthropic"
    assert attrs[ATTR_REQUEST_MODEL] == "claude-opus"
    assert attrs[ATTR_RESPONSE_MODEL] == "claude-opus-2026"
    assert attrs[ATTR_INPUT_TOKENS] == 100
    assert attrs[ATTR_OUTPUT_TOKENS] == 50
    assert attrs[ATTR_NE_LANE] == "frontier-api"
    assert attrs[ATTR_NE_COST_USD] == 0.01
    assert attrs[ATTR_NE_KPI_TAG] == "latency"
    assert attrs[ATTR_NE_CONFIG_HASH] == frozen_config


def test_none_entries_are_omitted(frozen_config: str) -> None:
    attrs = genai_attributes(operation="embeddings")
    assert set(attrs) == {ATTR_OPERATION_NAME, ATTR_NE_CONFIG_HASH}
    assert ATTR_PROVIDER_NAME not in attrs
    assert ATTR_INPUT_TOKENS not in attrs


def test_explicit_config_hash_used_without_freeze() -> None:
    reset_config_hash_for_testing()
    attrs = genai_attributes(operation="chat", config_hash="d" * 64)
    assert attrs[ATTR_NE_CONFIG_HASH] == "d" * 64


def test_unfrozen_config_hash_raises() -> None:
    reset_config_hash_for_testing()
    with pytest.raises(ConfigHashNotFrozenError):
        genai_attributes(operation="chat")


def test_provider_name_is_not_deprecated_system() -> None:
    # SPEC §10 / ADR: we standardize on gen_ai.provider.name, never gen_ai.system.
    assert ATTR_PROVIDER_NAME == "gen_ai.provider.name"
