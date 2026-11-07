# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Eval / observability domain Protocols (SPEC section 4).

Eval suites (recorded with the process ``config_hash``), correction-signal
emission, and KPI recording (C8 — KPI-first instrumentation).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from ne.contracts.events import CorrectionSignalLike
from ne.contracts.frozen import FloatMap, FrozenDict


class EvalRunResult(BaseModel):
    """Result of an eval suite run, stamped with the config_hash it ran under."""

    model_config = ConfigDict(frozen=True)

    suite: str
    config_hash: str
    metrics: FloatMap
    thresholds: FloatMap = FrozenDict()
    passed: bool


class EvalSuite(Protocol):
    """A named evaluation suite. Runs are recorded with config_hash (SPEC section 4)."""

    @property
    def name(self) -> str: ...

    async def run(self, *, config_hash: str) -> EvalRunResult: ...


class SignalEmitter(Protocol):
    """Emit correction signals into the flywheel."""

    async def emit(self, signal: CorrectionSignalLike) -> None: ...


class KpiRecorder(Protocol):
    """C8 — KPI-first instrumentation: record a KPI value with optional dimensions."""

    async def record(
        self,
        kpi_tag: str,
        value: float,
        *,
        dimensions: Mapping[str, str] | None = None,
    ) -> None: ...
