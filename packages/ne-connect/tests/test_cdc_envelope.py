# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fixture-driven parsing of Debezium change-event envelopes (NE-084)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ne.connect.cdc import CdcEnvelopeError, parse_change_events, table_identity

_FIX = Path(__file__).parent / "fixtures" / "cdc"


def _load(name: str) -> bytes:
    return (_FIX / name).read_bytes()


def test_parse_single_payload_only_insert() -> None:
    (change,) = parse_change_events(_load("postgres_insert.json"))
    assert change.op == "c"
    assert change.table == "public.customers"
    assert change.before is None
    assert change.after is not None
    assert change.after["id"] == 1
    assert change.ts_ms == 1720000000000


def test_parse_schema_wrapped_envelope() -> None:
    (change,) = parse_change_events(_load("postgres_insert_schema_wrapped.json"))
    assert change.op == "c"
    assert change.table == "public.customers"
    assert change.after is not None
    assert change.after["id"] == 2


def test_parse_batch_array() -> None:
    changes = parse_change_events(_load("batch_mixed.json"))
    assert [c.op for c in changes] == ["c", "u", "d"]
    assert all(c.table == "public.customers" for c in changes)
    # The delete carries before (keys), no after.
    assert changes[2].after is None
    assert changes[2].before is not None


def test_parse_json_null_returns_empty() -> None:
    assert parse_change_events(b"null") == ()
    assert parse_change_events(b"") == ()
    assert parse_change_events(b"   ") == ()


def test_malformed_body_raises() -> None:
    with pytest.raises(CdcEnvelopeError):
        parse_change_events(b"{not json")
    with pytest.raises(CdcEnvelopeError):
        parse_change_events(b'"a bare string"')
    with pytest.raises(CdcEnvelopeError):
        parse_change_events(b"123")


def test_invalid_utf8_body_raises_envelope_error() -> None:
    # Invalid UTF-8 must surface as CdcEnvelopeError (route → 400), never as a
    # raw UnicodeDecodeError (route → 500 + a Debezium poison-retry loop), and
    # the message must not echo the undecodable bytes.
    with pytest.raises(CdcEnvelopeError) as exc:
        parse_change_events(b'{"op": "c"\xff\xfe}')
    assert "\\xff" not in str(exc.value)


def test_missing_op_raises() -> None:
    with pytest.raises(CdcEnvelopeError):
        parse_change_events(b'{"after": {"id": 1}, "source": {"schema": "public", "table": "c"}}')


def test_missing_source_raises() -> None:
    with pytest.raises(CdcEnvelopeError):
        parse_change_events(b'{"op": "c", "after": {"id": 1}}')


def test_table_identity_postgres_mysql_sqlserver() -> None:
    # Postgres: schema.table
    (pg,) = parse_change_events(_load("postgres_insert.json"))
    assert pg.table == "public.customers"
    # MySQL: db.table (no schema)
    (my,) = parse_change_events(_load("mysql_insert.json"))
    assert my.table == "appdb.customers"
    # SQL Server: schema.table (dbo)
    (ss,) = parse_change_events(_load("sqlserver_insert.json"))
    assert ss.table == "dbo.customers"


def test_table_identity_missing_table_raises() -> None:
    with pytest.raises(CdcEnvelopeError):
        table_identity({"schema": "public"})


def test_table_identity_missing_namespace_raises() -> None:
    with pytest.raises(CdcEnvelopeError):
        table_identity({"table": "customers"})


def test_envelope_error_text_carries_no_row_value() -> None:
    # A malformed 'after' must not leak the offending value into the error text.
    secret = "SENTINEL-SECRET-VALUE"
    body = ('{"op":"c","source":{"schema":"public","table":"c"},"after":"' + secret + '"}').encode()
    with pytest.raises(CdcEnvelopeError) as exc:
        parse_change_events(body)
    assert secret not in str(exc.value)
