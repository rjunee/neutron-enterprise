# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Production-composition control for PRE-011C Knowledge evidence authoring."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ne.api.tools import TOOL_NAME, TURN_PRINCIPAL
from ne.contracts.types import Principal
from ne.runtime.errors import ToolExecutionError

from .conftest import get_state


def test_protected_platform_composes_existing_run_guard_before_knowledge_read(
    make_app: Any,
) -> None:
    harness = make_app(protected_transport=True)
    platform = get_state(harness.app)
    assert platform.agent_run_store is not None
    assert TOOL_NAME in {spec.name for spec in platform.executor.tool_specs}
    principal = Principal(id="user:ada", groups=("engineering",))

    async def invoke() -> str:
        token = TURN_PRINCIPAL.set(principal)
        try:
            return await platform.executor.execute(
                TOOL_NAME, {"query": "must not reach retrieval"}, principal=principal
            )
        finally:
            TURN_PRINCIPAL.reset(token)

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(invoke())
    assert isinstance(exc_info.value.__cause__, LookupError)
    assert "existing canonical Run evidence context" in str(exc_info.value.__cause__)


def test_unconfigured_private_store_preserves_single_search_tool_compatibility(
    make_app: Any,
) -> None:
    harness = make_app(protected_transport=False)
    platform = get_state(harness.app)
    principal = Principal(id="user:ada", groups=("engineering",))

    async def invoke() -> str:
        token = TURN_PRINCIPAL.set(principal)
        try:
            return await platform.executor.execute(
                TOOL_NAME, {"query": "source-free empty control"}, principal=principal
            )
        finally:
            TURN_PRINCIPAL.reset(token)

    assert asyncio.run(invoke()) == "[showing 0 of 0 retrieved chunks]"
