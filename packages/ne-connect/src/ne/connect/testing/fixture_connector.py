# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FixtureConnector — a zero-network recorded-fixture ``Connector`` (NE-040).

Proves recorded-fixture CI mode: a connector that replays a committed JSON (or
in-memory) fixture with ZERO network I/O, so NE-044/045 connector suites are
CI-testable without live tenants. Registered under the ``ne.plugins.connector``
entry point as ``fixture``.

Fixture shape::

    {
      "source": "acme-crm",
      "objects": [{"source_id": "acme-crm/contacts", "kind": "resource",
                   "name": "contacts"}],
      "records": [{"record_id": "c1", "source_id": "acme-crm/contacts",
                   "cursor": "2026-01-01T00:00:00Z", "payload": {...}}],
      "acl": [{"resource_id": "acme-crm/contacts", "principals": ["oidc:sales"]}]
    }

Cursor comparison is LEXICOGRAPHIC (matching the state-store contract):
``incremental(state)`` yields a record when its source_id is unseen OR its
cursor is strictly greater than the stored cursor.

SANITIZATION IS THE RECORDER'S JOB — committed fixtures MUST carry innocuous
fake data only, never real credentials or PII (plan §1.4 secrets hygiene).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ne.contracts.integration import AclEntry, Record, SourceObject


class FixtureConnector:
    """Replay a recorded fixture as a frozen ``Connector`` — no network I/O."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._source: str = data["source"]
        self._objects: list[dict[str, Any]] = [dict(o) for o in data.get("objects", [])]
        self._records: list[dict[str, Any]] = [dict(r) for r in data.get("records", [])]
        self._acl: list[dict[str, Any]] = [dict(a) for a in data.get("acl", [])]

    @classmethod
    def from_data(cls, mapping: Mapping[str, Any]) -> FixtureConnector:
        return cls(mapping)

    @classmethod
    def from_path(cls, path: str | Path) -> FixtureConnector:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def source(self) -> str:
        return self._source

    async def discover(self) -> Sequence[SourceObject]:
        return [
            SourceObject(source_id=o["source_id"], kind=o["kind"], name=o["name"])
            for o in self._objects
        ]

    def backfill(self, state: Mapping[str, Any] | None = None) -> AsyncIterator[Record]:
        records = self._records

        async def _gen() -> AsyncIterator[Record]:
            for row in records:
                yield _record(row)

        return _gen()

    def incremental(self, state: Mapping[str, Any]) -> AsyncIterator[Record]:
        records = self._records

        async def _gen() -> AsyncIterator[Record]:
            for row in records:
                stored = state.get(row["source_id"])
                if stored is None or str(row["cursor"]) > str(stored):
                    yield _record(row)

        return _gen()

    def permissions(self) -> AsyncIterator[AclEntry]:
        entries = self._acl

        async def _gen() -> AsyncIterator[AclEntry]:
            for entry in entries:
                yield AclEntry(
                    resource_id=entry["resource_id"],
                    principals=tuple(entry["principals"]),
                )

        return _gen()


def _record(row: Mapping[str, Any]) -> Record:
    return Record(
        record_id=str(row["record_id"]),
        payload=row["payload"],
        source_id=row["source_id"],
    )


def dump_fixture(
    path: str | Path,
    *,
    source: str,
    objects: Iterable[Mapping[str, Any]],
    records: Iterable[Mapping[str, Any]],
    acl: Iterable[Mapping[str, Any]],
) -> None:
    """Write a canonical recorded-fixture JSON (the NE-044/045 authoring helper).

    SANITIZATION IS THE RECORDER'S JOB — pass only innocuous fake data; never
    commit credentials or PII into a fixture (plan §1.4 secrets hygiene).
    """
    payload = {
        "source": source,
        "objects": [dict(o) for o in objects],
        "records": [dict(r) for r in records],
        "acl": [dict(a) for a in acl],
    }
    Path(path).write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
