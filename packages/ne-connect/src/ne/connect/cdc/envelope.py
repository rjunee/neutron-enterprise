# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Parse Debezium change-event envelopes (NE-084) — pure, no I/O.

Debezium Server's HTTP sink delivers change events as JSON. Two on-the-wire
shapes are supported by ``debezium.format.value.schemas.enable``:

* ``false`` (recommended, the overlay default) — the event body IS the payload:
  ``{"before": ..., "after": ..., "source": {...}, "op": "c|u|d|r", "ts_ms": N}``.
* ``true`` — the payload is wrapped: ``{"schema": {...}, "payload": {<above>}}``.

The HTTP sink may also POST a JSON ARRAY of such events (batch mode). A
``null`` body (Debezium never actually sends this over HTTP — tombstones are
value-null RECORDS the HTTP sink drops before send — but we tolerate it) parses
to an empty tuple.

Every failure raises :class:`CdcEnvelopeError` whose message NEVER contains a
row value (only structural facts), so no captured data leaks into a log/error.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CdcChange",
    "CdcEnvelopeError",
    "parse_change_events",
    "table_identity",
]


class CdcEnvelopeError(Exception):
    """A change-event body was malformed / not a recognizable Debezium envelope."""


@dataclass(frozen=True)
class CdcChange:
    """One parsed Debezium change event.

    ``op`` is the Debezium operation: ``c`` (create/insert), ``u`` (update),
    ``d`` (delete), ``r`` (read/snapshot), ``t`` (truncate), ``m`` (message).
    ``table`` is the resolved :func:`table_identity`.
    """

    op: str
    table: str
    before: Mapping[str, Any] | None
    after: Mapping[str, Any] | None
    source: Mapping[str, Any]
    ts_ms: int | None


def table_identity(source: Mapping[str, Any]) -> str:
    """Resolve the ``schema.table`` (or ``db.table``) identity from ``source``.

    Postgres / SQL Server carry a non-empty ``schema``; MySQL carries only
    ``db``. A missing/empty ``table`` is a :class:`CdcEnvelopeError`.
    """
    table = source.get("table")
    if not isinstance(table, str) or not table:
        raise CdcEnvelopeError("change event source has no 'table' identity")
    schema = source.get("schema")
    if isinstance(schema, str) and schema:
        return f"{schema}.{table}"
    db = source.get("db")
    if isinstance(db, str) and db:
        return f"{db}.{table}"
    raise CdcEnvelopeError("change event source has neither 'schema' nor 'db' namespace")


def _parse_one(obj: Any) -> CdcChange:
    if not isinstance(obj, dict):
        raise CdcEnvelopeError(f"change event is not a JSON object (got {type(obj).__name__})")
    # schemas.enable=true wraps the event under 'payload'.
    if "schema" in obj and "payload" in obj and "op" not in obj:
        return _parse_one(obj["payload"])
    op = obj.get("op")
    if not isinstance(op, str) or not op:
        raise CdcEnvelopeError("change event has no 'op' field")
    source = obj.get("source")
    if not isinstance(source, dict):
        raise CdcEnvelopeError("change event has no 'source' object")
    before = obj.get("before")
    after = obj.get("after")
    if before is not None and not isinstance(before, dict):
        raise CdcEnvelopeError("change event 'before' is present but not an object")
    if after is not None and not isinstance(after, dict):
        raise CdcEnvelopeError("change event 'after' is present but not an object")
    ts_ms = obj.get("ts_ms")
    if ts_ms is not None and not isinstance(ts_ms, int):
        ts_ms = None
    return CdcChange(
        op=op,
        table=table_identity(source),
        before=before,
        after=after,
        source=source,
        ts_ms=ts_ms,
    )


def parse_change_events(body: bytes | str) -> tuple[CdcChange, ...]:
    """Parse an HTTP-sink body into a tuple of :class:`CdcChange`.

    * A JSON ``null`` (or empty body) → ``()``.
    * A single event object → one-tuple.
    * A JSON array → each element parsed the same way (HTTP-sink batch mode).

    Any structural violation raises :class:`CdcEnvelopeError`; the message never
    contains a row value.
    """
    if isinstance(body, (bytes, bytearray)):
        try:
            text = bytes(body).decode("utf-8", errors="strict") if body else ""
        except UnicodeDecodeError:
            # Value-free by construction: never echo the undecodable bytes.
            raise CdcEnvelopeError("change-event body is not valid UTF-8") from None
    else:
        text = body
    if not text.strip():
        return ()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CdcEnvelopeError(f"change-event body is not valid JSON: {exc.msg}") from None
    if parsed is None:
        return ()
    if isinstance(parsed, list):
        return tuple(_parse_one(item) for item in parsed)
    return (_parse_one(parsed),)
