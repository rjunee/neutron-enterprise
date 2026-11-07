# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Durable ingest ledger seam (NE-026).

The pipeline spine needs a small amount of durable per-document state so that
ingestion is IDEMPOTENT (re-ingesting identical bytes is a no-op), RESUMABLE (a
crash mid-pipeline resumes without redoing completed stages), DOC-VERSIONED (a
changed document supersedes the prior version), and QUARANTINE-aware (a
document-caused failure is recorded and inspectable, not silently dropped).

:class:`IngestLedger` is a **package-local** Protocol (like
:class:`~ne.brain.ingest.contracts.ParsedDocument`, NOT a ne-contracts addition —
ne-contracts stays FROZEN). Its methods are SYNCHRONOUS: the shipped
:class:`SqliteIngestLedger` is a local, single-connection stdlib-``sqlite3``
store with no I/O to await. The production ne-ingest process is expected to swap
in a Postgres-backed ledger at this exact seam later; the spine depends only on
the Protocol.

State model per document:

* ``documents`` — the furthest-reached stage for the CURRENT ``content_hash``,
  plus the ``indexed_content_hash`` / ``indexed_chunk_ids`` of whatever version
  is currently live in the vector index (preserved across a version change so
  supersede can delete the stale chunks).
* ``artifacts`` — the persisted output of each completed stage, keyed by
  ``(document_id, content_hash, stage)`` so a resume replays byte-identical
  stage output and a version change never reads the old version's artifacts.
  Pruned in the same transaction that records a successful INDEXED.
* ``quarantine`` — the quarantine lane, keyed by ``(document_id, content_hash)``;
  rows persist for inspection even after a later version of the same document
  indexes successfully.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ne.contracts.frozen import AnyMap, FrozenDict


class DocStage(StrEnum):
    """The furthest pipeline stage a document version has reached."""

    PARSED = "parsed"
    CHUNKED = "chunked"
    ENRICHED = "enriched"
    INDEXED = "indexed"
    QUARANTINED = "quarantined"
    FAILED = "failed"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class LedgerState(_Frozen):
    """The durable state of one document in the ledger."""

    document_id: str
    content_hash: str
    stage: DocStage | None
    """Furthest stage reached for ``content_hash``; ``None`` between
    :meth:`IngestLedger.begin` and the first checkpoint."""
    reason: str | None = None
    indexed_content_hash: str | None = None
    """Content hash of the version currently live in the vector index."""
    indexed_chunk_ids: tuple[str, ...] = ()
    """Chunk ids of the version currently live in the vector index."""
    indexed_acl: tuple[str, ...] = ()
    """ACL labels of the version currently live in the vector index."""
    indexed_metadata: AnyMap = FrozenDict()
    """IngestSource.metadata of the version currently live in the vector index —
    the caller-supplied source labels last merged into indexed chunks under the
    reserved 'source' key, NOT each chunk's full computed metadata; see spine.py."""


class QuarantineRecord(_Frozen):
    """One inspectable quarantine-lane entry."""

    document_id: str
    content_hash: str
    reason: str
    created_at: str


@runtime_checkable
class IngestLedger(Protocol):
    """Durable per-document ingest state (synchronous; local-sqlite now).

    The production ne-ingest process swaps a Postgres-backed ledger in at this
    seam; the spine depends only on this Protocol.
    """

    def get_state(self, document_id: str) -> LedgerState | None: ...

    def begin(self, document_id: str, content_hash: str) -> None:
        """Start (or resume) work on ``(document_id, content_hash)``.

        Same-hash: no-op (an in-flight run resumes). Different-hash: restart
        cleanly — reset stage/reason and drop the old version's artifacts — while
        PRESERVING ``indexed_content_hash``/``indexed_chunk_ids`` so a crash
        before the new version indexes can still supersede the old one.
        """
        ...

    def checkpoint(self, document_id: str, content_hash: str, stage: DocStage) -> None: ...

    def save_artifact(
        self, document_id: str, content_hash: str, stage: DocStage, payload: str
    ) -> None: ...

    def load_artifact(self, document_id: str, content_hash: str, stage: DocStage) -> str | None: ...

    def record_indexed(
        self,
        document_id: str,
        content_hash: str,
        chunk_ids: Sequence[str],
        *,
        acl: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Atomically mark INDEXED, record the live version (chunk ids plus the
        acl/source-metadata now baked into every indexed chunk), and prune artifacts."""
        ...

    def quarantine(self, document_id: str, content_hash: str, reason: str) -> None: ...

    def record_failure(self, document_id: str, content_hash: str, reason: str) -> None:
        """Record a retryable (infrastructure-caused) failure; stage is unchanged."""
        ...

    def list_quarantined(self) -> tuple[QuarantineRecord, ...]: ...


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _json_default(value: Any) -> Any:
    """JSON-safety hook for ``indexed_metadata`` serialization.

    ``IngestSource.metadata`` is an ``AnyMap`` and may in principle hold nested
    frozen structures (a ``FrozenDict``, or a ``frozenset`` a caller supplied),
    which stdlib ``json`` cannot serialize on its own. Normalize the ones we
    expect; anything else is a genuine bug and raises ``TypeError``.
    """
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"cannot JSON-serialize {value!r} of type {type(value).__name__}")


class SqliteIngestLedger:
    """A durable :class:`IngestLedger` over stdlib ``sqlite3``.

    Accepts a filesystem :class:`~pathlib.Path` (or ``":memory:"``). Uses a
    single connection; every mutation is a committed transaction, so ledger
    state survives a process restart (proven by
    ``test_ingest_ledger.py::test_state_roundtrip_persists_across_reopen``).
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        self._conn = sqlite3.connect(
            str(path) if isinstance(path, Path) else path, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                stage TEXT,
                reason TEXT,
                indexed_content_hash TEXT,
                indexed_chunk_ids TEXT NOT NULL DEFAULT '[]',
                indexed_acl TEXT NOT NULL DEFAULT '[]',
                indexed_metadata TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                document_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                stage TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (document_id, content_hash, stage)
            );
            CREATE TABLE IF NOT EXISTS quarantine (
                document_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (document_id, content_hash)
            );
            """
        )
        self._conn.commit()

    def get_state(self, document_id: str) -> LedgerState | None:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if row is None:
            return None
        stage_value = row["stage"]
        return LedgerState(
            document_id=row["document_id"],
            content_hash=row["content_hash"],
            stage=DocStage(stage_value) if stage_value is not None else None,
            reason=row["reason"],
            indexed_content_hash=row["indexed_content_hash"],
            indexed_chunk_ids=tuple(json.loads(row["indexed_chunk_ids"])),
            indexed_acl=tuple(json.loads(row["indexed_acl"])),
            indexed_metadata=json.loads(row["indexed_metadata"]),
        )

    def begin(self, document_id: str, content_hash: str) -> None:
        row = self._conn.execute(
            "SELECT content_hash FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        now = _now_iso()
        if row is None:
            self._conn.execute(
                "INSERT INTO documents "
                "(document_id, content_hash, stage, reason, "
                " indexed_content_hash, indexed_chunk_ids, "
                " indexed_acl, indexed_metadata, updated_at) "
                "VALUES (?, ?, NULL, NULL, NULL, '[]', '[]', '{}', ?)",
                (document_id, content_hash, now),
            )
        elif row["content_hash"] != content_hash:
            # Changed document: restart cleanly for the new hash but PRESERVE the
            # live-in-index version so a crash before re-index can still supersede.
            self._conn.execute("DELETE FROM artifacts WHERE document_id = ?", (document_id,))
            self._conn.execute(
                "UPDATE documents SET content_hash = ?, stage = NULL, reason = NULL, "
                "updated_at = ? WHERE document_id = ?",
                (content_hash, now, document_id),
            )
        # Same hash: no-op resume.
        self._conn.commit()

    def checkpoint(self, document_id: str, content_hash: str, stage: DocStage) -> None:
        self._conn.execute(
            "UPDATE documents SET stage = ?, reason = NULL, content_hash = ?, "
            "updated_at = ? WHERE document_id = ?",
            (stage.value, content_hash, _now_iso(), document_id),
        )
        self._conn.commit()

    def save_artifact(
        self, document_id: str, content_hash: str, stage: DocStage, payload: str
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO artifacts (document_id, content_hash, stage, payload) "
            "VALUES (?, ?, ?, ?)",
            (document_id, content_hash, stage.value, payload),
        )
        self._conn.commit()

    def load_artifact(self, document_id: str, content_hash: str, stage: DocStage) -> str | None:
        row = self._conn.execute(
            "SELECT payload FROM artifacts "
            "WHERE document_id = ? AND content_hash = ? AND stage = ?",
            (document_id, content_hash, stage.value),
        ).fetchone()
        return None if row is None else row["payload"]

    def record_indexed(
        self,
        document_id: str,
        content_hash: str,
        chunk_ids: Sequence[str],
        *,
        acl: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        # ONE transaction: mark INDEXED, record the now-live version (chunk ids
        # plus the acl/source-metadata now baked into every indexed chunk), prune
        # the document's stage artifacts. Committing atomically means the ledger's
        # INDEXED state implies the index holds exactly this version's chunks.
        self._conn.execute(
            "UPDATE documents SET stage = ?, reason = NULL, content_hash = ?, "
            "indexed_content_hash = ?, indexed_chunk_ids = ?, "
            "indexed_acl = ?, indexed_metadata = ?, updated_at = ? "
            "WHERE document_id = ?",
            (
                DocStage.INDEXED.value,
                content_hash,
                content_hash,
                json.dumps(list(chunk_ids)),
                json.dumps(list(acl)),
                json.dumps(dict(metadata) if metadata is not None else {}, default=_json_default),
                _now_iso(),
                document_id,
            ),
        )
        self._conn.execute("DELETE FROM artifacts WHERE document_id = ?", (document_id,))
        self._conn.commit()

    def quarantine(self, document_id: str, content_hash: str, reason: str) -> None:
        now = _now_iso()
        # The quarantine lane (source of truth for list_quarantined); rows persist
        # for inspection even after a later version indexes (different PK hash).
        self._conn.execute(
            "INSERT OR REPLACE INTO quarantine "
            "(document_id, content_hash, reason, created_at) VALUES (?, ?, ?, ?)",
            (document_id, content_hash, reason, now),
        )
        # Best-effort documents-row mirror (the unreadable-source case reaches
        # here before begin(), so upsert rather than assume a row exists).
        self._conn.execute(
            "INSERT INTO documents "
            "(document_id, content_hash, stage, reason, "
            " indexed_content_hash, indexed_chunk_ids, "
            " indexed_acl, indexed_metadata, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, '[]', '[]', '{}', ?) "
            "ON CONFLICT(document_id) DO UPDATE SET "
            "stage = excluded.stage, reason = excluded.reason, "
            "content_hash = excluded.content_hash, updated_at = excluded.updated_at",
            (document_id, content_hash, DocStage.QUARANTINED.value, reason, now),
        )
        self._conn.commit()

    def record_failure(self, document_id: str, content_hash: str, reason: str) -> None:
        # Retryable: stage stays at the last checkpoint; only the reason is set.
        self._conn.execute(
            "UPDATE documents SET reason = ?, content_hash = ?, updated_at = ? "
            "WHERE document_id = ?",
            (reason, content_hash, _now_iso(), document_id),
        )
        self._conn.commit()

    def list_quarantined(self) -> tuple[QuarantineRecord, ...]:
        rows = self._conn.execute(
            "SELECT document_id, content_hash, reason, created_at FROM quarantine "
            "ORDER BY created_at, document_id"
        ).fetchall()
        return tuple(
            QuarantineRecord(
                document_id=row["document_id"],
                content_hash=row["content_hash"],
                reason=row["reason"],
                created_at=row["created_at"],
            )
            for row in rows
        )
