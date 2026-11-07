# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Module lifecycle hooks (SPEC section 4): health and schema migrations.

Consumed by the NE-069 migration runner and deploy health probes.
Implementations MUST be idempotent.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class HealthState(StrEnum):
    """Coarse health of a module."""

    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


class HealthCheck(BaseModel):
    """A point-in-time health report for a module."""

    model_config = ConfigDict(frozen=True)

    state: HealthState
    detail: str = ""
    checked_at: datetime


@runtime_checkable
class ModuleLifecycle(Protocol):
    """Health and migration hooks every deployable module exposes.

    ``schema_version`` returns None when the module owns no schema; ``migrate``
    runs any pending migrations and returns the new schema version (or None when
    there is nothing to do). Both must be idempotent.
    """

    @property
    def module_name(self) -> str: ...

    async def health(self) -> HealthCheck: ...

    def schema_version(self) -> str | None: ...

    async def migrate(self) -> str | None: ...
