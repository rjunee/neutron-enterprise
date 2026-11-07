# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""A dlt-resource-backed :class:`~ne.contracts.integration.Connector` (NE-040).

A dlt *resource* is a plain iterable of dicts; :class:`DltResourceConnector`
adapts a set of :class:`ResourceSpec` factories into the frozen ``Connector``
Protocol. No dlt Pipeline / destination is used — records stay in-process — so
this module imports NO dlt at all (the ``ResourceSpec.factory`` owns any dlt
usage, and would import dlt via :mod:`ne.connect._dlt`).

Protocol convention (see ``ne.contracts.integration``): ``backfill`` /
``incremental`` / ``permissions`` are SYNC methods that RETURN an
``AsyncIterator`` — each returns an inner ``async def`` generator.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ne.connect.errors import InvalidRecordError
from ne.contracts.integration import AclEntry, Record, SourceObject


@dataclass(frozen=True)
class ResourceSpec:
    """One dlt-resource-shaped stream within a source.

    ``factory`` maps an optional cursor (``None`` for a full backfill) to an
    iterable of row dicts; ``record_id_key`` names the row key carrying the
    stable record id.
    """

    name: str
    factory: Callable[[str | None], Iterable[Mapping[str, Any]]]
    record_id_key: str


class DltResourceConnector:
    """Frozen ``Connector`` over a set of :class:`ResourceSpec` factories."""

    def __init__(
        self,
        source_name: str,
        specs: Sequence[ResourceSpec],
        *,
        acl_factory: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
    ) -> None:
        self._source_name = source_name
        self._specs = tuple(specs)
        self._acl_factory = acl_factory

    def _source_id(self, spec: ResourceSpec) -> str:
        return f"{self._source_name}/{spec.name}"

    async def discover(self) -> Sequence[SourceObject]:
        return [
            SourceObject(source_id=self._source_id(spec), kind="resource", name=spec.name)
            for spec in self._specs
        ]

    def backfill(self, state: Mapping[str, Any] | None = None) -> AsyncIterator[Record]:
        return self._stream(state)

    def incremental(self, state: Mapping[str, Any]) -> AsyncIterator[Record]:
        return self._stream(state)

    def _stream(self, state: Mapping[str, Any] | None) -> AsyncIterator[Record]:
        specs = self._specs
        cursors = state or {}

        async def _gen() -> AsyncIterator[Record]:
            for spec in specs:
                source_id = self._source_id(spec)
                cursor = cursors.get(source_id)
                for row in spec.factory(cursor):
                    if spec.record_id_key not in row:
                        raise InvalidRecordError(
                            f"row from {source_id!r} is missing its record-id key "
                            f"{spec.record_id_key!r}"
                        )
                    yield Record(
                        record_id=str(row[spec.record_id_key]),
                        payload=row,
                        source_id=source_id,
                    )

        return _gen()

    def permissions(self) -> AsyncIterator[AclEntry]:
        acl_factory = self._acl_factory

        async def _gen() -> AsyncIterator[AclEntry]:
            if acl_factory is None:
                return
            for entry in acl_factory():
                yield AclEntry(
                    resource_id=str(entry["resource_id"]),
                    principals=tuple(entry["principals"]),
                )

        return _gen()
