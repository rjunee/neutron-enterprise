# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SqlConnector — the frozen Connector Protocol over dlt's ``sql_table`` (NE-046).

Implements ``discover``/``backfill``/``incremental``/``permissions`` for a
config-driven set of tables: each configured table is read once via
``ne.connect._dlt.sql_table`` (dlt's core ``sql_database`` source, reached ONLY
through the ``_dlt`` seam), shaped into a source :class:`Record` per row, and
ACL-resolved fail-closed per :mod:`ne.connect.sql.acl` /
:class:`~ne.connect.sql.config.SqlTableAclConfig`.

ONE crawl per instance (instance == one sync run), same crawl-once-cached shape
as :class:`ne.connect.m365.connector.M365GraphConnector`: the crawl is lazy and
cached, ``permissions()`` (called first by the ``SyncRunner``) triggers it, and
``backfill``/``incremental`` replay the cache. ``incremental(state)`` honors a
DIFFERING cursor basis by re-crawling — so the conformance kit's
incremental-subset test exercises real ``>=`` cursor semantics.

Fail-closed ACL (the unit's security core): a row is QUARANTINED (no ``Record``,
no ``AclEntry``, counted, surfaced by ``sql/sync.py`` as one
``connector.acl_quarantine`` denied audit event — never emitted world-readable)
whenever its resolved principal set is empty OR tainted (a reserved label
forged in source data — NE-CKD2). A missing/``NULL`` id-column value is a
CONFIG bug, not a data-quality one, and aborts the run LOUDLY via
``InvalidRecordError`` rather than silently skipping the row. A SQLAlchemy read
failure aborts the run via ``SqlSyncError`` (C12: exception class name + table_ref
only, never the driver's own args) — no cursor commits for that table (or any
other still-pending table), so the durable ledger is untouched and a retry
re-reads cleanly.

v1 limitations (documented, shared with the M365 connector): the crawl is
memory-cached (bounded-corpus assumption); an ``>=`` incremental boundary
re-fetches the boundary row, which the ``(source, source_id, record_id)`` dedup
ledger absorbs as a no-op; ACL-quarantined rows STILL advance the cursor (a
config fix requires a ledger reset to resync — see the package README).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from ne.connect import _dlt
from ne.connect.errors import InvalidRecordError, SqlSyncError
from ne.connect.sql.acl import resolve_row_principals
from ne.connect.sql.config import SqlConnectorSettings, SqlTableConfig
from ne.connect.sql.engine import create_validated_engine
from ne.contracts.integration import AclEntry, Record, SourceObject

__all__ = ["SqlConnector"]

_QUARANTINE_ID_CAP = 100
_CURSOR_PREFIX = "cursor:"


def _decode_cursor(value: str, cursor_type: str) -> object:
    """Decode a persisted cursor string into the typed value dlt compares against."""
    if cursor_type == "int":
        return int(value)
    if cursor_type == "datetime":
        return datetime.fromisoformat(value)
    return value


def _encode_cursor(value: object, cursor_type: str) -> str:
    """Encode a raw row cursor value into the opaque string the ledger persists."""
    if cursor_type == "datetime" and isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _cursor_sort_key(value: object, cursor_type: str) -> Any:
    """A comparable TYPED key for max-tracking — never a lexicographic string
    compare (an int cursor of ``10`` must sort after ``9``, not before)."""
    if cursor_type == "int":
        return int(value)  # type: ignore[arg-type]
    if cursor_type == "datetime":
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return str(value)


def _make_row_filter_adapter(row_filter: str) -> Callable[[Any, Any], Any]:
    """Build the ``query_adapter_callback`` implementing a table's ``row_filter``.

    PROVEN semantics (executed against the locked dlt version): the callback
    receives ``(Select, Table)`` and must return the (possibly modified)
    ``Select`` — ``table_adapter_callback`` was rejected during planning because
    a bare ``Table`` has no ``append_whereclause``.
    """

    def _adapt(query: Any, _table: Any) -> Any:
        return query.where(sa.text(row_filter))

    return _adapt


class SqlConnector:
    """Frozen ``Connector`` over a config-driven set of SQL tables."""

    def __init__(
        self,
        settings: SqlConnectorSettings,
        *,
        engine: sa.Engine | None = None,
        cursor_state: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine if engine is not None else create_validated_engine(settings)
        self._cursor_state: dict[str, str] = dict(cursor_state or {})

        self._crawled = False
        self._records: list[Record] = []
        self._acl: list[AclEntry] = []
        self.pending_cursors: dict[str, str] = {}
        self.acl_quarantined = 0
        self.acl_quarantined_ids: list[str] = []

    # -- Connector Protocol --------------------------------------------------
    async def discover(self) -> Sequence[SourceObject]:
        """Config-driven — NO database I/O."""
        return [
            SourceObject(source_id=cfg.table_ref, kind="table", name=cfg.table_ref)
            for cfg in self._settings.tables
        ]

    def backfill(self, state: Mapping[str, Any] | None = None) -> AsyncIterator[Record]:
        async def _gen() -> AsyncIterator[Record]:
            self._ensure_crawled()
            for record in self._records:
                yield record

        return _gen()

    def incremental(self, state: Mapping[str, Any]) -> AsyncIterator[Record]:
        basis = {
            key.removeprefix(_CURSOR_PREFIX): str(value)
            for key, value in state.items()
            if key.startswith(_CURSOR_PREFIX)
        }

        async def _gen() -> AsyncIterator[Record]:
            if basis and basis != self._cursor_state:
                self._recrawl(basis)
            self._ensure_crawled()
            for record in self._records:
                yield record

        return _gen()

    def permissions(self) -> AsyncIterator[AclEntry]:
        async def _gen() -> AsyncIterator[AclEntry]:
            self._ensure_crawled()
            for entry in self._acl:
                yield entry

        return _gen()

    # -- crawl ---------------------------------------------------------------
    def _recrawl(self, basis: Mapping[str, str]) -> None:
        """Reset cached state and re-crawl from a different cursor basis."""
        self._cursor_state = dict(basis)
        self._crawled = False
        self._records = []
        self._acl = []
        self.pending_cursors = {}
        self.acl_quarantined = 0
        self.acl_quarantined_ids = []

    def _quarantine(self, source_id: str) -> None:
        """Fail-closed quarantine bookkeeping: count + (capped) capture the id."""
        self.acl_quarantined += 1
        if len(self.acl_quarantined_ids) < _QUARANTINE_ID_CAP:
            self.acl_quarantined_ids.append(source_id)

    def _ensure_crawled(self) -> None:
        if self._crawled:
            return
        for cfg in self._settings.tables:
            self._crawl_table(cfg)
        self._crawled = True

    def _crawl_table(self, cfg: SqlTableConfig) -> None:
        table_ref = cfg.table_ref
        cursor_str = self._cursor_state.get(table_ref)
        inc = None
        if cfg.cursor_column and cursor_str is not None:
            inc = _dlt.incremental(
                cfg.cursor_column,
                initial_value=_decode_cursor(cursor_str, cfg.cursor_type),
                last_value_func="max",
            )

        principal_column = cfg.acl.principal_column
        included: list[str] = [cfg.id_column]
        if cfg.cursor_column:
            included.append(cfg.cursor_column)
        if principal_column:
            included.append(principal_column)
        included.extend(cfg.content_columns)
        included.extend(cfg.metadata_columns)
        included = list(dict.fromkeys(included))

        query_adapter: Callable[[Any, Any], Any] | None = None
        if cfg.row_filter is not None:
            query_adapter = _make_row_filter_adapter(cfg.row_filter)

        try:
            resource = _dlt.sql_table(
                credentials=self._engine,
                table=cfg.table,
                schema=cfg.schema_name,
                incremental=inc,
                chunk_size=self._settings.chunk_size,
                included_columns=included,
                query_adapter_callback=query_adapter,
            )
            rows = list(resource)
        except SQLAlchemyError as exc:
            raise SqlSyncError(
                f"SQL read failed for table {table_ref!r}: {type(exc).__name__}"
            ) from None

        for item in rows:
            if isinstance(item, Mapping):
                self._ingest_row(cfg, dict(item))
            elif isinstance(item, list):
                for row in item:
                    self._ingest_row(cfg, dict(row))
            else:
                # Defensive: sql_table yields dicts (proven against the locked dlt
                # version) — any other shape is a library-behavior surprise, not a
                # data-quality one, so this fails loud rather than silently drops.
                raise SqlSyncError(
                    f"SQL read for table {table_ref!r} yielded an unexpected item "
                    f"type {type(item).__name__}"
                )

    def _ingest_row(self, cfg: SqlTableConfig, row: dict[str, Any]) -> None:
        table_ref = cfg.table_ref
        rid_raw = row.get(cfg.id_column)
        if rid_raw is None:
            raise InvalidRecordError(
                f"row from table {table_ref!r} is missing its id column {cfg.id_column!r}"
            )
        record_id = str(rid_raw)
        source_id = f"{table_ref}:{record_id}"

        if cfg.acl.principal_column is not None:
            outcome = resolve_row_principals(
                row.get(cfg.acl.principal_column), separator=cfg.acl.principal_separator
            )
            if outcome.tainted or not outcome.principals:
                self._quarantine(source_id)
                self._advance_cursor(cfg, row)  # documented v1 limitation
                return
            principals = outcome.principals
        else:
            principals = cfg.acl.principals

        self._records.append(Record(record_id=record_id, payload=row, source_id=source_id))
        self._acl.append(AclEntry(resource_id=source_id, principals=principals))
        self._advance_cursor(cfg, row)

    def _advance_cursor(self, cfg: SqlTableConfig, row: Mapping[str, Any]) -> None:
        if not cfg.cursor_column:
            return
        raw_value = row.get(cfg.cursor_column)
        if raw_value is None:
            return
        key = _cursor_sort_key(raw_value, cfg.cursor_type)
        table_ref = cfg.table_ref
        current_encoded = self.pending_cursors.get(table_ref)
        if current_encoded is not None:
            current_key = _cursor_sort_key(
                _decode_cursor(current_encoded, cfg.cursor_type), cfg.cursor_type
            )
            if key <= current_key:
                return
        self.pending_cursors[table_ref] = _encode_cursor(raw_value, cfg.cursor_type)
