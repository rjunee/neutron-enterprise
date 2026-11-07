# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The inspectable SHACL write-quarantine lane (NE-085a).

An instance-data batch that fails SHACL validation is NOT silently dropped: its
serialized delta (N-Triples) is parked here alongside the violation strings, the
shapes hash it was checked against, and its provenance — inspectable and
replayable. Mirrors :mod:`ne.brain.write.quarantine` EXACTLY in style
(synchronous stdlib ``sqlite3``, single connection, ``_now_iso()`` UTC, frozen
pydantic models); the production process swaps a Postgres-backed lane in at the
:class:`TripleQuarantine` Protocol seam (T1 precedent, documented).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class QuarantinedTriples(_Frozen):
    """One inspectable quarantine-lane entry — the rejected delta + why."""

    quarantine_id: str
    delta_nt: str
    violations: tuple[str, ...]
    provenance: str
    shapes_hash: str
    created_at: str
    graph_iri: str | None = None
    """Target named graph the delta was destined for (NE-085b RDF lift).

    Additive/defaulted: pre-085b rows (and the default gateway write path) leave
    it ``None`` and release into ``settings.data_graph_iri``; a quarantined lift
    batch records its ACL-partitioned graph here so :meth:`release` re-inserts
    into the ORIGINAL graph, not the default.
    """


@runtime_checkable
class TripleQuarantine(Protocol):
    """Durable, inspectable quarantine lane for failed triple writes (sync seam)."""

    def record(self, item: QuarantinedTriples) -> None: ...

    def get(self, quarantine_id: str) -> QuarantinedTriples | None: ...

    def list(self) -> tuple[QuarantinedTriples, ...]: ...

    def remove(self, quarantine_id: str) -> None: ...


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SqliteTripleQuarantine:
    """A durable :class:`TripleQuarantine` over stdlib ``sqlite3``.

    ``path=None`` → in-memory. A single connection; every mutation is a committed
    transaction so the lane survives a process restart.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._conn = sqlite3.connect(
            str(path) if path is not None else ":memory:", check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS triple_quarantine (
                quarantine_id TEXT PRIMARY KEY,
                delta_nt TEXT NOT NULL,
                violations TEXT NOT NULL,
                provenance TEXT NOT NULL,
                shapes_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                graph_iri TEXT
            );
            """
        )
        # Additive migration (NE-085b): a pre-085b DB has no graph_iri column.
        # ADD COLUMN it (NULL default) so old rows survive and new rows persist it.
        info = self._conn.execute("PRAGMA table_info(triple_quarantine)")
        columns = {row["name"] for row in info}
        if "graph_iri" not in columns:
            self._conn.execute("ALTER TABLE triple_quarantine ADD COLUMN graph_iri TEXT")
        self._conn.commit()

    def record(self, item: QuarantinedTriples) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO triple_quarantine "
            "(quarantine_id, delta_nt, violations, provenance, shapes_hash, created_at, graph_iri) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                item.quarantine_id,
                item.delta_nt,
                json.dumps(list(item.violations)),
                item.provenance,
                item.shapes_hash,
                item.created_at,
                item.graph_iri,
            ),
        )
        self._conn.commit()

    def get(self, quarantine_id: str) -> QuarantinedTriples | None:
        row = self._conn.execute(
            "SELECT * FROM triple_quarantine WHERE quarantine_id = ?", (quarantine_id,)
        ).fetchone()
        return None if row is None else self._row_to_model(row)

    def list(self) -> tuple[QuarantinedTriples, ...]:
        rows = self._conn.execute(
            "SELECT * FROM triple_quarantine ORDER BY created_at, quarantine_id"
        ).fetchall()
        return tuple(self._row_to_model(row) for row in rows)

    def remove(self, quarantine_id: str) -> None:
        self._conn.execute(
            "DELETE FROM triple_quarantine WHERE quarantine_id = ?", (quarantine_id,)
        )
        self._conn.commit()

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> QuarantinedTriples:
        return QuarantinedTriples(
            quarantine_id=row["quarantine_id"],
            delta_nt=row["delta_nt"],
            violations=tuple(json.loads(row["violations"])),
            provenance=row["provenance"],
            shapes_hash=row["shapes_hash"],
            created_at=row["created_at"],
            graph_iri=row["graph_iri"],
        )


__all__ = [
    "QuarantinedTriples",
    "SqliteTripleQuarantine",
    "TripleQuarantine",
]
