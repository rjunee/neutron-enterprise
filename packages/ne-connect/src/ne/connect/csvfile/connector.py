# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CsvConnector — a polled CSV file as a frozen ``Connector`` (NE-CONNECT-CSV).

Reading discipline — the data is presented to the contract with its
SURROUNDING transport slop removed and nothing else touched:

* Values stay STRINGS (a CSV cell is a string; pretending otherwise would hide
  ``"N/A"``-where-a-number-belongs from the validator).
* SURROUNDING invisible characters are trimmed from every header name and cell
  value before the contract sees them: Unicode whitespace (space, tab, CR from
  a CRLF export, NBSP — everything ``str.isspace`` accepts) plus the zero-width
  artifacts ZWSP U+200B, WORD JOINER U+2060, and ZWNBSP/BOM U+FEFF (Excel and
  PowerShell prepend the BOM to UTF-8 exports, where it would otherwise corrupt
  the FIRST header name). Edge whitespace belongs to the delimiter layer — a
  sheet cannot MEAN it — so trimming it is normalisation, not interpretation.
  The line is drawn there: nothing INSIDE a value is ever altered (no
  case-folding, no digit-separator coercion, no Unicode normal-form rewriting,
  no interior-whitespace collapse), so every stored value is a contiguous
  substring of what the file said.
* A whitespace-only cell trims to the EMPTY STRING and stays PRESENT: presence
  is column structure, and only a structurally missing cell is absent — so a
  required field typed as spaces fails the contract as ``''`` (the validator
  names the empty value) exactly the way a blank cell does, never as a
  misleading ``'<column>' is a required property``.
* Undecodable bytes (a cp1252 paste into a UTF-8 export — the classic hand-
  maintained-sheet corruption) are decoded with ``errors='backslashreplace'``:
  no data is lost, the corruption becomes a VISIBLE ``\\x92``-style escape in
  the cell value, and the contract's field pattern rejects it through the SAME
  validator path as every other violation — no special corruption code path.
* A short row (someone deleted a cell) simply lacks the missing columns in its
  payload (``restval=None`` values are dropped), so the contract reports the
  REAL problem: ``'<column>' is a required property``. Surplus unnamed cells
  land under ``_extra_columns`` and fail ``additionalProperties: false``.
* A row whose ``id_column`` is blank/missing still gets a STABLE identity
  (``row-<line>``) so it parks inspectably and reconciles away when fixed.

``backfill`` and ``incremental`` are the SAME full-snapshot read: a file has no
server-side delta API; the bridge's content-hash dedup ledger is what makes an
unchanged re-poll a no-op, and snapshot reconciliation is what lets a fixed or
deleted row leave quarantine.
"""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ne.connect.errors import CsvSyncError
from ne.contracts.integration import AclEntry, Record, SourceObject

__all__ = ["CsvConnector"]

#: Surplus unnamed cells (a row LONGER than the header) land under this payload
#: key so the contract's ``additionalProperties: false`` names the defect.
_EXTRA_COLUMNS_KEY = "_extra_columns"

#: Zero-width transport artifacts trimmed from field EDGES alongside Unicode
#: whitespace (module doc, "reading discipline"): ZWSP, WORD JOINER, ZWNBSP/BOM.
#: ``str.strip()`` does not touch these — they are Cf (format) characters, not
#: whitespace — and a single pass of either strip cannot see through the other's
#: characters (``'x\\xa0\\u200b'.strip()`` is a no-op: the NBSP hides behind the
#: ZWSP), hence the fixed-point loop in :func:`_trim_surrounding`.
_ZERO_WIDTH = "\u200b\u2060\ufeff"


def _trim_surrounding(value: str) -> str:
    """Strip surrounding Unicode whitespace + zero-width artifacts to a fixed point."""
    while True:
        trimmed = value.strip().strip(_ZERO_WIDTH)
        if trimmed == value:
            return value
        value = trimmed


class CsvConnector:
    """Replay one CSV file as a full-snapshot ``Connector`` — no network I/O."""

    def __init__(self, path: str | Path, *, source: str, id_column: str) -> None:
        self._path = Path(path)
        self._source = source
        self._id_column = id_column

    @property
    def source(self) -> str:
        return self._source

    @property
    def source_id(self) -> str:
        """The single source object's id: ``<source>/<filename>``."""
        return f"{self._source}/{self._path.name}"

    def file_mtime_iso(self) -> str:
        """The file's mtime as ISO-8601 UTC — the lane's lexicographic cursor."""
        try:
            mtime = self._path.stat().st_mtime
        except OSError as exc:
            raise CsvSyncError(
                f"csv source file {self._path} is not readable ({type(exc).__name__})"
            ) from exc
        return datetime.fromtimestamp(mtime, tz=UTC).isoformat()

    async def discover(self) -> Sequence[SourceObject]:
        return [SourceObject(source_id=self.source_id, kind="file", name=self._path.name)]

    def backfill(self, state: Mapping[str, Any] | None = None) -> AsyncIterator[Record]:
        return self._stream()

    def incremental(self, state: Mapping[str, Any]) -> AsyncIterator[Record]:
        return self._stream()

    def permissions(self) -> AsyncIterator[AclEntry]:
        async def _gen() -> AsyncIterator[AclEntry]:
            return
            yield  # pragma: no cover — a file carries no source ACL entries

        return _gen()

    def _rows(self) -> Iterator[tuple[int, dict[str, Any]]]:
        """Yield ``(line_number, payload)`` per data row (header is line 1)."""
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            raise CsvSyncError(
                f"csv source file {self._path} is not readable ({type(exc).__name__})"
            ) from exc
        # backslashreplace: keep every byte visible — corruption must surface
        # through the contract validator, never crash or silently vanish.
        text = raw.decode("utf-8", errors="backslashreplace")
        reader = csv.DictReader(io.StringIO(text), restkey=_EXTRA_COLUMNS_KEY, restval=None)
        for line_number, row in enumerate(reader, start=2):
            payload: dict[str, Any] = {}
            for key, value in row.items():
                if value is None:
                    continue  # restval: a short row's missing cells stay ABSENT
                if key == _EXTRA_COLUMNS_KEY:
                    # restkey: surplus unnamed cells, a list under OUR key.
                    payload[key] = [_trim_surrounding(cell) for cell in value]
                else:
                    payload[_trim_surrounding(key)] = _trim_surrounding(value)
            yield line_number, payload

    def _stream(self) -> AsyncIterator[Record]:
        async def _gen() -> AsyncIterator[Record]:
            for line_number, payload in self._rows():
                record_id = payload.get(self._id_column)
                if not isinstance(record_id, str) or not record_id.strip():
                    # A row with no usable id still needs a STABLE identity so
                    # its park is inspectable and reconciles away when fixed.
                    record_id = f"row-{line_number}"
                yield Record(record_id=record_id, payload=payload, source_id=self.source_id)

        return _gen()
