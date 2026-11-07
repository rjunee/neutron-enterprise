# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Module lifecycle: runtime_checkable ModuleLifecycle + HealthCheck round-trip."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ne.contracts import HealthCheck, HealthState, ModuleLifecycle


class SampleModule:
    """A concrete module that structurally satisfies ModuleLifecycle."""

    @property
    def module_name(self) -> str:
        return "sample"

    async def health(self) -> HealthCheck:
        return HealthCheck(state=HealthState.OK, checked_at=datetime.now(UTC))

    def schema_version(self) -> str | None:
        return "0001"

    async def migrate(self) -> str | None:
        return None


def test_sample_satisfies_runtime_checkable_protocol() -> None:
    assert isinstance(SampleModule(), ModuleLifecycle)


def test_object_missing_method_is_not_lifecycle() -> None:
    class NotAModule:
        module_name = "x"

    assert not isinstance(NotAModule(), ModuleLifecycle)


def test_health_and_migrate_behaviour() -> None:
    mod = SampleModule()

    async def scenario() -> None:
        hc = await mod.health()
        assert hc.state is HealthState.OK
        assert mod.schema_version() == "0001"
        assert await mod.migrate() is None

    asyncio.run(scenario())


def test_health_check_model_round_trips() -> None:
    now = datetime.now(UTC)
    hc = HealthCheck(state=HealthState.DEGRADED, detail="slow", checked_at=now)
    dumped = hc.model_dump()
    restored = HealthCheck(**dumped)
    assert restored == hc
