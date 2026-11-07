# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The inspectable KnowledgeWrite quarantine lane (NE-023).

A write that fails its shape contract is NOT silently dropped: its full
validated-payload dict is parked here, inspectable, alongside the reason and the
shape it violated. Mirrors :mod:`ne.brain.ingest.ledger` exactly (synchronous
stdlib ``sqlite3``, single connection, ``_now_iso()`` UTC, committed
transactions) — the production process swaps a Postgres-backed lane in at the
:class:`WriteQuarantine` Protocol seam.

Keying contrast with NE-026 (DELIBERATE)
----------------------------------------
NE-026's ingest quarantine keys on ``(document_id, content_hash)`` so a
document's per-version failure HISTORY persists. NE-023 is item-granular and
keys on ``item_id`` alone with ``INSERT OR REPLACE`` (latest-failure-wins): the
lane reflects the writes CURRENTLY parked, and a subsequent successful upsert of
the same id clears its row (see :meth:`SqliteWriteQuarantine.remove`, driven by
the writer after a durable store write).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ne.contracts.frozen import AnyMap, FrozenDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class QuarantinedWrite(_Frozen):
    """One inspectable quarantine-lane entry.

    ``payload`` is the FULL validated-payload dict that failed — inspectability
    means the DATA is parked, not merely the id.
    """

    item_id: str
    reason: str
    payload: AnyMap = FrozenDict()
    provenance: str
    shape: str
    created_at: str


@runtime_checkable
class WriteQuarantine(Protocol):
    """Durable, inspectable quarantine lane for failed writes (synchronous seam)."""

    def record(self, item: QuarantinedWrite) -> None: ...

    def get(self, item_id: str) -> QuarantinedWrite | None: ...

    def remove(self, item_id: str) -> None: ...

    def list_quarantined(self) -> tuple[QuarantinedWrite, ...]: ...


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _to_plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_plain(v) for v in value]
    return value


#: SQLite busy-wait for this lane. The stdlib default is FIVE seconds, which is
#: shorter than a real connector sync holds the write lock on a shared quarantine
#: file (the demo stack bind-mounts one ``/connect/quarantine.db`` into ne-api and
#: ne-worker and also writes it from the host seed).
BUSY_TIMEOUT_S = 30.0


class SqliteWriteQuarantine:
    """A durable :class:`WriteQuarantine` over stdlib ``sqlite3``.

    Accepts a filesystem :class:`~pathlib.Path` (or ``":memory:"``). Uses a
    single connection; every mutation is a committed transaction so the lane
    survives a process restart.
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        self._conn = sqlite3.connect(
            str(path) if isinstance(path, Path) else path,
            check_same_thread=False,
            timeout=BUSY_TIMEOUT_S,
        )
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS write_quarantine (
                item_id TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                payload TEXT NOT NULL,
                provenance TEXT NOT NULL,
                shape TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def record(self, item: QuarantinedWrite) -> None:
        # Latest-failure-wins (id-granular): a re-failure of the same id replaces
        # the parked row rather than accumulating history (see module docstring).
        self._conn.execute(
            "INSERT OR REPLACE INTO write_quarantine "
            "(item_id, reason, payload, provenance, shape, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                item.item_id,
                item.reason,
                json.dumps(_to_plain(item.payload)),
                item.provenance,
                item.shape,
                item.created_at,
            ),
        )
        self._conn.commit()

    def get(self, item_id: str) -> QuarantinedWrite | None:
        row = self._conn.execute(
            "SELECT * FROM write_quarantine WHERE item_id = ?", (item_id,)
        ).fetchone()
        return None if row is None else self._row_to_model(row)

    def remove(self, item_id: str) -> None:
        self._conn.execute("DELETE FROM write_quarantine WHERE item_id = ?", (item_id,))
        self._conn.commit()

    def list_quarantined(self) -> tuple[QuarantinedWrite, ...]:
        rows = self._conn.execute(
            "SELECT * FROM write_quarantine ORDER BY created_at, item_id"
        ).fetchall()
        return tuple(self._row_to_model(row) for row in rows)

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> QuarantinedWrite:
        return QuarantinedWrite(
            item_id=row["item_id"],
            reason=row["reason"],
            payload=json.loads(row["payload"]),
            provenance=row["provenance"],
            shape=row["shape"],
            created_at=row["created_at"],
        )


__all__ = [
    "QuarantinedWrite",
    "SqliteWriteQuarantine",
    "WriteQuarantine",
]
