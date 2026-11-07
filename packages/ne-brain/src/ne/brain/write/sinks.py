# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ShapeViolation event sinks for the KnowledgeWrite path (NE-023).

A :class:`ShapeViolationSink` is where the writer hands a fully-built
``ShapeViolation`` (constructed via the ne-events ``new_shape_violation``
factory, so config_hash is stamped from the frozen process hash). The production
sink, :class:`OutboxShapeViolationSink`, writes it through the ne-events Postgres
outbox in one transaction — a line-for-line mirror of ne-gateway's
``CostSink``/``OutboxCostSink`` (C6/§1.4 event spine: every event travels the
outbox, no hand-rolled event dicts).
"""

from __future__ import annotations

from typing import Protocol

import sqlalchemy as sa

from ne.events import append
from ne.schema import ShapeViolation


class ShapeViolationSink(Protocol):
    """Where the writer emits a :class:`ShapeViolation` per failed write."""

    def emit(self, event: ShapeViolation) -> None: ...


class OutboxShapeViolationSink:
    """A :class:`ShapeViolationSink` that writes each event through the outbox."""

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    def emit(self, event: ShapeViolation) -> None:
        with self._engine.begin() as conn:
            append(conn, event)


__all__ = [
    "OutboxShapeViolationSink",
    "ShapeViolationSink",
]
