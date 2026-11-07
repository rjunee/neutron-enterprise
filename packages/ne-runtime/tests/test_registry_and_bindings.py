# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""RuntimeToolRegistry (specs-only) + validate_binding + ToolBinding immutability."""

from __future__ import annotations

import dataclasses

import pytest

from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier
from ne.runtime.errors import ToolNotRegisteredError, ToolRegistrationError
from ne.runtime.tools import RuntimeToolRegistry, ToolBinding, validate_binding


async def _noop(args: object) -> str:
    return "ok"


def _spec(name: str, tier: ApprovalTier) -> ToolSpec:
    return ToolSpec(name=name, description="d", tier=tier)


def test_registry_register_get_list() -> None:
    registry = RuntimeToolRegistry()
    spec = _spec("read.docs.search", ApprovalTier.TIER_0)
    registry.register(spec)
    assert registry.get("read.docs.search") is spec
    assert list(registry.list_tools()) == [spec]


def test_registry_get_unknown_raises() -> None:
    registry = RuntimeToolRegistry()
    with pytest.raises(ToolNotRegisteredError):
        registry.get("nope.unknown")


def test_registry_holds_only_specs() -> None:
    # The registry surface returns ToolSpec objects — never handlers.
    registry = RuntimeToolRegistry()
    registry.register(_spec("read.x", ApprovalTier.TIER_0))
    for tool in registry.list_tools():
        assert isinstance(tool, ToolSpec)
        assert not hasattr(tool, "handler")


def test_validate_binding_accepts_classified_matching_tier() -> None:
    binding = ToolBinding(
        spec=_spec("write.external.email.send", ApprovalTier.TIER_2), handler=_noop
    )
    validate_binding(binding)  # no raise


def test_validate_binding_allows_declared_tier_above_class() -> None:
    # read.* is class tier 0; declaring TIER_2 only escalates — allowed.
    binding = ToolBinding(spec=_spec("read.docs.search", ApprovalTier.TIER_2), handler=_noop)
    validate_binding(binding)  # no raise


def test_validate_binding_rejects_unclassifiable_name() -> None:
    binding = ToolBinding(spec=_spec("mystery.action", ApprovalTier.TIER_0), handler=_noop)
    with pytest.raises(ToolRegistrationError):
        validate_binding(binding)


def test_validate_binding_rejects_declared_tier_below_class() -> None:
    # write.external.* is class tier 2; declaring TIER_0 understates control.
    binding = ToolBinding(
        spec=_spec("write.external.email.send", ApprovalTier.TIER_0), handler=_noop
    )
    with pytest.raises(ToolRegistrationError):
        validate_binding(binding)


def test_tool_binding_is_frozen() -> None:
    binding = ToolBinding(spec=_spec("read.x", ApprovalTier.TIER_0), handler=_noop)
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.handler = _noop  # type: ignore[misc]
