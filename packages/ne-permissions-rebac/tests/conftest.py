# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the ne-permissions-rebac tests (in-memory fakes only)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.permissions_rebac.config import RebacSettings
from ne.permissions_rebac.testing.fakes import (
    FakeGroupExpansion,
    FakeKnownPrincipals,
    FakeReadProbe,
    InMemoryOpenFga,
    RecordingAuditSink,
    StaticPdp,
)


class _DemoSettings(NeSettings):
    demo: int = 7


@pytest.fixture(autouse=True)
def frozen_config() -> Iterator[str]:
    """Freeze a deterministic config_hash so ``new_audit_event`` can stamp it."""
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


@pytest.fixture
def enabled_settings() -> RebacSettings:
    return RebacSettings(enabled=True, store_id="s1", authorization_model_id="m1")


@pytest.fixture
def client() -> InMemoryOpenFga:
    return InMemoryOpenFga()


@pytest.fixture
def sink() -> RecordingAuditSink:
    return RecordingAuditSink()


@pytest.fixture
def pdp_allow() -> StaticPdp:
    from ne.contracts.governance import DecisionEffect

    return StaticPdp(DecisionEffect.ALLOW)


@pytest.fixture
def known() -> FakeKnownPrincipals:
    return FakeKnownPrincipals({"jane", "bob", "proj-x", "u-sharer"})


@pytest.fixture
def expander() -> FakeGroupExpansion:
    return FakeGroupExpansion({})


@pytest.fixture
def read_probe() -> FakeReadProbe:
    return FakeReadProbe(set())
