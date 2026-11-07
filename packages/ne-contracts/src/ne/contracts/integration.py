# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Integration domain Protocols (SPEC section 4).

Source connectors, typed write-back action tools, and ODCS-shaped data
contracts. Connector's ``backfill`` / ``incremental`` / ``permissions`` are
sync methods that RETURN ``AsyncIterator`` (the same streaming convention as
``ModelGateway.stream``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from ne.contracts.frozen import AnyMap
from ne.contracts.types import ApprovalTier, Principal


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class SourceObject(_Frozen):
    """A discoverable object in a source system."""

    source_id: str
    kind: str
    name: str


class Record(_Frozen):
    """A single record pulled from a source system."""

    record_id: str
    payload: AnyMap
    source_id: str


class AclEntry(_Frozen):
    """An access-control mapping mirrored from a source system."""

    resource_id: str
    principals: tuple[str, ...]


class ActionResult(_Frozen):
    """Outcome of a typed write-back action."""

    succeeded: bool
    detail: str = ""
    source_ref: str | None = None


class Connector(Protocol):
    """Source connector: discover, backfill, incremental, permissions (SPEC section 4)."""

    async def discover(self) -> Sequence[SourceObject]: ...

    def backfill(self, state: Mapping[str, Any] | None = None) -> AsyncIterator[Record]: ...

    def incremental(self, state: Mapping[str, Any]) -> AsyncIterator[Record]: ...

    def permissions(self) -> AsyncIterator[AclEntry]: ...


class ActionTool(Protocol):
    """Typed write-back tool, PDP-gated per C6, tier per SPEC section 4.2."""

    @property
    def name(self) -> str: ...

    @property
    def tier(self) -> ApprovalTier: ...

    @property
    def input_schema(self) -> Mapping[str, Any]: ...

    async def execute(self, args: Mapping[str, Any], *, principal: Principal) -> ActionResult: ...


class DataContract(Protocol):
    """ODCS-shaped surface describing a data source's contract.

    ``schema_def`` is deliberately named to avoid clashing with pydantic's
    ``BaseModel.schema`` and the ``schema`` builtin conventions.
    """

    @property
    def source(self) -> str: ...

    @property
    def owner(self) -> str: ...

    @property
    def schema_def(self) -> Mapping[str, Any]: ...

    @property
    def freshness_sla(self) -> str: ...

    @property
    def pii_class(self) -> str: ...

    @property
    def permission_model(self) -> str: ...

    def validate(self, record: Mapping[str, Any]) -> Sequence[str]: ...
