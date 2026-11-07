# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Event *surfaces* — the Protocol shape that platform events satisfy.

The concrete, field-complete event **schemas** (``AuditEvent``,
``CorrectionSignal``, ``ShapeViolation``, ``ApprovalEvent``, ``CostEvent``) are
defined in LinkML by NE-005. This module defines only the structural
:class:`EventLike` surface (and five named placeholder subsurfaces) that those
generated classes will satisfy *without inheritance* — proving the seam is
structural, so NE-005 need not import anything from here.

Every event carries ``config_hash``, an optional ``kpi_tag``, an actor, and
provenance, per SPEC section 4.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EventLike(Protocol):
    """Structural surface every platform event satisfies (SPEC section 4)."""

    @property
    def event_type(self) -> str: ...

    @property
    def config_hash(self) -> str: ...

    @property
    def kpi_tag(self) -> str | None: ...

    @property
    def actor(self) -> str: ...

    @property
    def provenance(self) -> Mapping[str, Any]: ...

    @property
    def occurred_at(self) -> datetime: ...


@runtime_checkable
class AuditEventLike(EventLike, Protocol):
    """Placeholder surface until NE-005's LinkML AuditEvent lands; structural, additive."""


@runtime_checkable
class CorrectionSignalLike(EventLike, Protocol):
    """Placeholder surface until NE-005's LinkML CorrectionSignal lands; structural, additive."""


@runtime_checkable
class ShapeViolationLike(EventLike, Protocol):
    """Placeholder surface until NE-005's LinkML ShapeViolation lands; structural, additive."""


@runtime_checkable
class ApprovalEventLike(EventLike, Protocol):
    """Placeholder surface until NE-005's LinkML ApprovalEvent lands; structural, additive."""


@runtime_checkable
class CostEventLike(EventLike, Protocol):
    """Placeholder surface until NE-005's LinkML CostEvent lands; structural, additive."""
