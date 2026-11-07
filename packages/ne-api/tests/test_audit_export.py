# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GET /v1/audit/export — the governed, streamed CSV export (NE-AUDIT-EXPORT).

THE GUARD: the exported row set equals the ``GET /v1/audit/events`` filtered
row set, EXACTLY, by ordered event_id identity — proven against a fixture that
straddles every filter boundary (inside/outside the time window, matching/
non-matching actor), with each excluded id asserted ABSENT from the response
text (not merely uncounted). Also proves: bounded keyset streaming (strict
``<`` cursor) at the HTTP seam, truncation is never silent (and CAN say
"false" — the bidirectional control), the export shares the ledger's ops-gate
and refusal-audit chokepoint verbatim, the success path writes nothing to the
chain, the embedded verdict is the SAME server-computed chain verification the
events route serves, malformed params 422, and the exact wire column shape.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest
import sqlalchemy as sa

from ne.audit import list_records
from ne.events import new_audit_event
from ne.schema import AuditOutcome

from .conftest import AppBuilder, Harness, get_state, run


def _parse_comment_block(lines: list[str], start: int) -> tuple[dict[str, str], int]:
    """Parse a contiguous run of ``# key: value`` lines starting at ``start``;
    return the map and the index just past the block."""
    block: dict[str, str] = {}
    idx = start
    while idx < len(lines) and lines[idx].startswith("# "):
        content = lines[idx][2:]
        if ":" in content:
            key, _, value = content.partition(":")
            block[key.strip()] = value.strip()
        idx += 1
    return block, idx


def _parse_export(text: str) -> tuple[dict[str, str], dict[str, str], list[list[str]]]:
    """Split the export body into (preamble map, trailer map, CSV rows)
    (``rows[0]`` is the header, ``rows[1:]`` the data rows).

    The trailer is the DEFINITIVE completion sentinel appended after the last
    data row (see ``_trailer_lines`` in ``routers/audit.py``) — a contiguous
    run of ``# `` comment lines at the very END of the response, found by
    scanning backward from EOF so it is never confused with a CSV cell that
    happens to start with ``#``.
    """
    lines = text.split("\r\n")
    while lines and lines[-1] == "":
        lines.pop()
    preamble, header_idx = _parse_comment_block(lines, 0)

    trailer_start = len(lines)
    while trailer_start > header_idx and lines[trailer_start - 1].startswith("# "):
        trailer_start -= 1
    trailer, _ = _parse_comment_block(lines, trailer_start)

    rest = "\r\n".join(lines[header_idx:trailer_start])
    rows = [row for row in csv.reader(io.StringIO(rest)) if row]
    return preamble, trailer, rows


_ALL_BOUNDARY_IDS = ("e1", "e2", "e3", "e4", "e5", "e6", "e7", "e8", "e9")
_EXCLUDED_BOUNDARY_IDS = ("e4", "e5", "e6", "e7", "e8", "e9")


def _seed_boundary(h: Harness) -> None:
    """Straddles every boundary a compliance reviewer's filters could draw:

    * e1, e2, e3 — actor=user:ada, action=thing.happened, INSIDE the
      2026-02-01..2026-02-28 window (the row set the guard filter matches);
    * e4 — actor=user:ada, BEFORE the window;
    * e5 — actor=user:ada, AFTER the window;
    * e6, e7 — actor=user:bob, INSIDE the window (excluded by the actor filter);
    * e8, e9 — actor=user:ada, INSIDE the window, action=other.thing (excluded
      by the ACTION filter — the export must not quietly ignore ``action``).

    Appended in id order, so seq mirrors id order (e1=seq1 .. e9=seq9).
    """
    state = get_state(h.app)
    rows = [
        ("e1", "user:ada", "thing.happened", datetime(2026, 2, 5, tzinfo=UTC)),
        ("e2", "user:ada", "thing.happened", datetime(2026, 2, 10, tzinfo=UTC)),
        ("e3", "user:ada", "thing.happened", datetime(2026, 2, 15, tzinfo=UTC)),
        ("e4", "user:ada", "thing.happened", datetime(2026, 1, 15, tzinfo=UTC)),
        ("e5", "user:ada", "thing.happened", datetime(2026, 3, 15, tzinfo=UTC)),
        ("e6", "user:bob", "thing.happened", datetime(2026, 2, 7, tzinfo=UTC)),
        ("e7", "user:bob", "thing.happened", datetime(2026, 2, 20, tzinfo=UTC)),
        ("e8", "user:ada", "other.thing", datetime(2026, 2, 8, tzinfo=UTC)),
        ("e9", "user:ada", "other.thing", datetime(2026, 2, 18, tzinfo=UTC)),
    ]

    async def _append() -> None:
        for event_id, actor, action, occurred_at in rows:
            await state.sink.append(
                new_audit_event(
                    event_id=event_id,
                    actor=actor,
                    action=action,
                    resource="r",
                    outcome=AuditOutcome.success,
                    occurred_at=occurred_at,
                )
            )

    run(_append())


def _seed_linear(h: Harness, *, prefix: str, n: int) -> None:
    """``n`` same-actor rows, oldest to newest, ids ``{prefix}1..{prefix}n``."""
    state = get_state(h.app)

    async def _append() -> None:
        for i in range(1, n + 1):
            await state.sink.append(
                new_audit_event(
                    event_id=f"{prefix}{i}",
                    actor="user:ada",
                    action="thing.happened",
                    resource="r",
                    outcome=AuditOutcome.success,
                    occurred_at=datetime(2026, 2, i, tzinfo=UTC),
                )
            )

    run(_append())


_WINDOW = {"from": "2026-02-01T00:00:00+00:00", "to": "2026-02-28T00:00:00+00:00"}


def test_export_equals_filtered_ledger_rows_by_identity(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed_boundary(h)
    # ALL FOUR export filters, so no filter dimension is untested: actor
    # excludes e6/e7 (bob), the window excludes e4/e5, and action excludes
    # e8/e9 (other.thing) — an export that silently dropped the `action`
    # filter server-side would leak e8/e9 into the export while the events
    # route (also filtered) would not, so the identity comparison below would
    # catch it even without the explicit exclusion assertions.
    params = {"actor": "user:ada", "action": "thing.happened", **_WINDOW}

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            events_r = await client.get("/api/v1/audit/events", headers=headers, params=params)
            assert events_r.status_code == 200
            events_body = events_r.json()["events"]
            events_ids = [e["event_id"] for e in events_body]
            assert events_ids == ["e3", "e2", "e1"]

            export_r = await client.get("/api/v1/audit/export", headers=headers, params=params)
            assert export_r.status_code == 200
            _preamble, _trailer, rows = _parse_export(export_r.text)
            header, data_rows = rows[0], rows[1:]
            export_ids = [row[header.index("event_id")] for row in data_rows]

            # Identity, order-sensitive: not a count, not a set.
            assert export_ids == events_ids

            # Every excluded row is truly ABSENT from the exported event_id
            # column (window-excluded e4/e5, actor-excluded e6/e7,
            # ACTION-excluded e8/e9) — checked against the parsed identity
            # column, not a raw substring scan of the response text (a hex
            # hash cell can coincidentally CONTAIN an excluded id's
            # characters, e.g. "...e5e459..."; that is not the row being
            # present).
            export_id_set = set(export_ids)
            for excluded_id in _EXCLUDED_BOUNDARY_IDS:
                assert excluded_id not in export_id_set

            # Value-level equality for one row (cells, not presence).
            events_row = next(e for e in events_body if e["event_id"] == "e2")
            export_row = data_rows[export_ids.index("e2")]
            assert export_row[header.index("actor")] == events_row["actor"]
            assert export_row[header.index("action")] == events_row["action"]
            assert int(export_row[header.index("seq")]) == events_row["seq"]
            assert export_row[header.index("record_hash")] == events_row["record_hash"]
            assert json.loads(export_row[header.index("payload")]) == events_row["payload"]

    run(_t())


def test_export_streams_in_batches_without_full_materialisation(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",), audit_export_batch_rows=2)
    _seed_linear(h, prefix="b", n=5)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(
                "/api/v1/audit/export", headers=headers, params={"actor": "user:ada"}
            )
            assert r.status_code == 200
            _preamble, trailer, rows = _parse_export(r.text)
            header, data_rows = rows[0], rows[1:]
            ids = [row[header.index("event_id")] for row in data_rows]
            # 5 rows over 2-row batches -> >=3 keyset batches; newest-first, exhaustive.
            assert ids == ["b5", "b4", "b3", "b2", "b1"]
            # The trailer's actual-written count also proves no batch was
            # dropped or duplicated at the HTTP seam.
            assert trailer["rows_written"] == "5"
            assert trailer["export_complete"] == "true"
            assert trailer["truncated_confirmed"] == "false"

    run(_t())


def test_export_truncation_is_loud(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",), audit_export_max_rows=3)
    _seed_linear(h, prefix="t", n=5)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(
                "/api/v1/audit/export", headers=headers, params={"actor": "user:ada"}
            )
            assert r.status_code == 200
            assert r.headers["X-Neutron-Export-Truncated"] == "true"
            preamble, trailer, rows = _parse_export(r.text)
            header, data_rows = rows[0], rows[1:]
            ids = [row[header.index("event_id")] for row in data_rows]
            # Exactly the 3 NEWEST, by identity.
            assert ids == ["t5", "t4", "t3"]
            assert preamble["truncated"] == "true"
            assert preamble["rows_matched"] == "5"
            assert preamble["rows_exported"] == "3"
            # The trailer confirms truncation as a FACT (one extra keyset read
            # anchored at the last row actually sent), not the preamble's
            # up-front estimate — see _trailer_lines.
            assert trailer["rows_written"] == "3"
            assert trailer["truncated_confirmed"] == "true"
            assert trailer["export_complete"] == "true"

    run(_t())


def test_export_under_cap_not_truncated(make_app: AppBuilder) -> None:
    # CONTROL: 5 rows under the default 100_000 cap -> never truncated, and the
    # signal must be able to SAY "false" (not just default-omit truncated=true).
    h = make_app(ops_roles=("ops",))
    _seed_linear(h, prefix="u", n=5)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(
                "/api/v1/audit/export", headers=headers, params={"actor": "user:ada"}
            )
            assert r.status_code == 200
            assert r.headers["X-Neutron-Export-Truncated"] == "false"
            preamble, trailer, rows = _parse_export(r.text)
            header, data_rows = rows[0], rows[1:]
            ids = [row[header.index("event_id")] for row in data_rows]
            assert ids == ["u5", "u4", "u3", "u2", "u1"]
            assert preamble["truncated"] == "false"
            # CONTROL: the definitive trailer must also be able to say "false"
            # (not just default-omit truncated_confirmed=true).
            assert trailer["truncated_confirmed"] == "false"
            assert trailer["export_complete"] == "true"

    run(_t())


def test_export_limit_param_caps_result_by_identity(make_app: AppBuilder) -> None:
    """ARGUS P2: ``limit`` is one of the five export filter dimensions and had
    ZERO coverage — a mutation that silently ignored the caller's ``limit``
    (``effective_cap = max_rows`` unconditionally) passed the whole suite.
    Proves ``limit`` narrows the export EXACTLY like it narrows
    ``/v1/audit/events`` (identity, not count), and that the preamble/trailer
    truncation signals reflect the CALLER'S cap, not the server default."""
    h = make_app(ops_roles=("ops",))
    _seed_linear(h, prefix="lm", n=5)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            params = {"actor": "user:ada", "limit": 2}
            events_r = await client.get("/api/v1/audit/events", headers=headers, params=params)
            assert events_r.status_code == 200
            events_ids = [e["event_id"] for e in events_r.json()["events"]]
            assert events_ids == ["lm5", "lm4"]

            export_r = await client.get("/api/v1/audit/export", headers=headers, params=params)
            assert export_r.status_code == 200
            preamble, trailer, rows = _parse_export(export_r.text)
            header, data_rows = rows[0], rows[1:]
            export_ids = [row[header.index("event_id")] for row in data_rows]

            # Identity, order-sensitive — matches the events API AND is exactly
            # the two newest rows, not merely "two rows".
            assert export_ids == events_ids
            assert export_ids == ["lm5", "lm4"]

            # The caller's limit (2), not the server default (100_000), governs
            # the truncation signals: 5 matching rows, capped to 2 by `limit`.
            assert preamble["rows_matched"] == "5"
            assert preamble["rows_exported"] == "2"
            assert preamble["truncated"] == "true"
            assert export_r.headers["X-Neutron-Export-Truncated"] == "true"
            assert trailer["rows_written"] == "2"
            assert trailer["truncated_confirmed"] == "true"

    run(_t())


def test_export_requires_ops_role_and_audits_attempt(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed_boundary(h)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/audit/export", headers=h.bearer("rich", groups=["nope"]))
            assert r.status_code == 403
            body = r.text
            # Zero chain bytes in the refusal — every seeded identity absent, and
            # no evidence field leaked (entitlement is key-level: total refusal).
            for seeded_id in _ALL_BOUNDARY_IDS:
                assert seeded_id not in body
            assert "record_hash" not in body

    run(_t())

    engine = sa.create_engine(h.audit_url)
    try:
        rows = list_records(engine, action="api.audit.unauthorized")
    finally:
        engine.dispose()
    assert len(rows) >= 1
    assert rows[0].actor == "rich"


def test_export_is_read_only(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed_boundary(h)

    engine = sa.create_engine(h.audit_url)
    try:
        before = [(r.event_id, r.record_hash) for r in list_records(engine, limit=1000)]
    finally:
        engine.dispose()

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(
                "/api/v1/audit/export", headers=headers, params={"actor": "user:ada"}
            )
            assert r.status_code == 200

    run(_t())

    engine = sa.create_engine(h.audit_url)
    try:
        after = [(r.event_id, r.record_hash) for r in list_records(engine, limit=1000)]
    finally:
        engine.dispose()
    # C10: a SUCCESSFUL export appends nothing to the chain.
    assert after == before


def test_export_header_reflects_tamper_verdict(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed_linear(h, prefix="e", n=3)

    # Tamper recipe (test_audit_api.py::test_events_tampered_row_flips_verdict,
    # verbatim): drop the append-only UPDATE trigger, then mutate a payload.
    engine = sa.create_engine(h.audit_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
            row = conn.execute(sa.text("SELECT payload FROM ne_audit_log WHERE seq = 1")).first()
            assert row is not None
            payload = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
            payload["actor"] = "forged"
            conn.execute(
                sa.text("UPDATE ne_audit_log SET payload = :p WHERE seq = 1"),
                {"p": json.dumps(payload)},
            )
    finally:
        engine.dispose()

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get("/api/v1/audit/export", headers=headers)
            assert r.status_code == 200
            preamble, _trailer, _rows = _parse_export(r.text)
            assert preamble["verification_valid"] == "false"
            assert preamble["verification_first_break_seq"] == "1"
            assert preamble["verification_detail"] == (
                "chain broken at seq 1 (event_id=e1): record tampered"
            )

    run(_t())


def test_export_preamble_escapes_crlf_in_tampered_event_id(make_app: AppBuilder) -> None:
    """A stored identifier cannot forge a verdict line via verification detail.

    The export exists to expose database tampering, so the identifier named by
    the failed chain verification is attacker-controlled under this threat
    model.  Its CR/LF bytes must be escaped before the detail is interpolated
    into the preamble, just like the separately emitted first-break id.
    """
    h = make_app(ops_roles=("ops",))
    _seed_linear(h, prefix="e", n=1)
    forged_event_id = "stored\r\n# verification_valid: true"

    engine = sa.create_engine(h.audit_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
            conn.execute(
                sa.text("UPDATE ne_audit_log SET event_id = :event_id WHERE seq = 1"),
                {"event_id": forged_event_id},
            )
    finally:
        engine.dispose()

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get("/api/v1/audit/export", headers=headers)
            assert r.status_code == 200

            # Restrict the physical-line assertion to the leading comment
            # block.  The same identifier is also a correctly quoted CSV cell
            # later in the response, where embedded newlines are legal data.
            lines = r.text.split("\r\n")
            preamble_lines: list[str] = []
            for line in lines:
                if not line.startswith("# "):
                    break
                preamble_lines.append(line)
            verdict_lines = [
                line for line in preamble_lines if line.startswith("# verification_valid:")
            ]
            assert verdict_lines == ["# verification_valid: false"]

            preamble, _trailer, _rows = _parse_export(r.text)
            assert preamble["verification_first_break_event_id"] == (
                "stored\\r\\n# verification_valid: true"
            )
            assert preamble["verification_detail"] == (
                "chain broken at seq 1 "
                "(event_id=stored\\r\\n# verification_valid: true): "
                "column/payload mismatch: event_id"
            )

    run(_t())


def test_export_verdict_is_scoped_to_the_filter_window(make_app: AppBuilder) -> None:
    """ARGUS P3: the embedded verdict must be ``verify_chain`` over the SAME
    ``from``/``to`` window as the filter — not a fresh, unwindowed full-chain
    walk. Seeds a row AFTER the requested window and tampers ONLY its payload
    (leaving its ``prev_hash``/``record_hash`` columns untouched, so the
    windowed successor-link check — which compares hashes only, never
    recomputes the successor's own hash — cannot see it): a FULL verify would
    flip to ``invalid`` at that row, but the windowed verdict for a filter that
    excludes it must stay ``valid: true``."""
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)

    async def _append() -> None:
        for event_id, occurred_at in (
            ("w1", datetime(2026, 1, 15, tzinfo=UTC)),
            ("w2", datetime(2026, 2, 5, tzinfo=UTC)),
            ("w3", datetime(2026, 2, 15, tzinfo=UTC)),
            ("w4", datetime(2026, 3, 15, tzinfo=UTC)),
        ):
            await state.sink.append(
                new_audit_event(
                    event_id=event_id,
                    actor="user:ada",
                    action="thing.happened",
                    resource="r",
                    outcome=AuditOutcome.success,
                    occurred_at=occurred_at,
                )
            )

    run(_append())

    # Tamper w4 (seq 4, AFTER the _WINDOW) — payload only, so its own
    # record_hash column silently disagrees with its payload (a FULL walk
    # catches this), but prev_hash/record_hash are untouched so the windowed
    # successor-link check (which only compares hash COLUMNS) cannot see it.
    engine = sa.create_engine(h.audit_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
            row = conn.execute(sa.text("SELECT payload FROM ne_audit_log WHERE seq = 4")).first()
            assert row is not None
            payload = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
            payload["actor"] = "forged"
            conn.execute(
                sa.text("UPDATE ne_audit_log SET payload = :p WHERE seq = 4"),
                {"p": json.dumps(payload)},
            )
    finally:
        engine.dispose()

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(
                "/api/v1/audit/export",
                headers=headers,
                params={"actor": "user:ada", **_WINDOW},
            )
            assert r.status_code == 200
            preamble, _trailer, rows = _parse_export(r.text)
            header, data_rows = rows[0], rows[1:]
            ids = [row[header.index("event_id")] for row in data_rows]
            assert ids == ["w3", "w2"]
            # The tampered row (w4) sits outside the requested window, so the
            # WINDOWED verdict stays valid even though a full-chain walk would not.
            assert preamble["verification_valid"] == "true"
            assert preamble["verification_checked"] == "2"

    run(_t())


def test_export_malformed_params_are_422(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r_from = await client.get(
                "/api/v1/audit/export", headers=headers, params={"from": "not-a-date"}
            )
            assert r_from.status_code == 422
            r_naive = await client.get(
                "/api/v1/audit/export", headers=headers, params={"to": "2026-02-28T00:00"}
            )
            assert r_naive.status_code == 422
            assert r_naive.json()["detail"] == (
                "to: timezone offset required; local wall-clock text is ambiguous"
            )
            r_reversed = await client.get(
                "/api/v1/audit/export",
                headers=headers,
                params={
                    "from": "2026-02-28T00:00:00+00:00",
                    "to": "2026-02-01T00:00:00+00:00",
                },
            )
            assert r_reversed.status_code == 422
            assert r_reversed.json()["detail"] == "from: must not be later than to"
            r_limit = await client.get("/api/v1/audit/export", headers=headers, params={"limit": 0})
            assert r_limit.status_code == 422

    run(_t())


def test_export_preamble_normalizes_offset_bounds_to_utc(make_app: AppBuilder) -> None:
    """The CSV scope is an instant window, never an unzoned copy of local
    wall-clock text; non-UTC bounds are echoed canonically and select the same
    row identities as the screen's events request."""
    h = make_app(ops_roles=("ops",))
    _seed_boundary(h)
    params = {
        "actor": "user:ada",
        "action": "thing.happened",
        "from": "2026-01-31T19:00:00-05:00",
        "to": "2026-02-27T19:00:00-05:00",
    }

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            events = await client.get("/api/v1/audit/events", headers=headers, params=params)
            export = await client.get("/api/v1/audit/export", headers=headers, params=params)
            assert events.status_code == export.status_code == 200
            preamble, _trailer, rows = _parse_export(export.text)
            header, data_rows = rows[0], rows[1:]
            assert [row[header.index("event_id")] for row in data_rows] == [
                event["event_id"] for event in events.json()["events"]
            ]
            assert preamble["filter_from"] == "2026-02-01T00:00:00+00:00"
            assert preamble["filter_to"] == "2026-02-28T00:00:00+00:00"

    run(_t())


def test_export_csv_shape(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed_boundary(h)

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            events_r = await client.get(
                "/api/v1/audit/events", headers=headers, params={"actor": "user:ada"}
            )
            export_r = await client.get(
                "/api/v1/audit/export", headers=headers, params={"actor": "user:ada"}
            )
            assert export_r.status_code == 200
            _preamble, _trailer, rows = _parse_export(export_r.text)
            header = rows[0]
            assert header == [
                "seq",
                "id",
                "event_id",
                "event_type",
                "config_hash",
                "actor",
                "occurred_at",
                "kpi_tag",
                "trace_id",
                "action",
                "resource",
                "outcome",
                "payload",
                "prev_hash",
                "record_hash",
            ]

            events_body = events_r.json()["events"]
            known = next(e for e in events_body if e["event_id"] == "e1")
            data_rows = rows[1:]
            export_row = next(row for row in data_rows if row[header.index("event_id")] == "e1")
            payload_cell = export_row[header.index("payload")]
            assert json.loads(payload_cell) == known["payload"]

    run(_t())


def test_export_preamble_escapes_crlf_in_filters(make_app: AppBuilder) -> None:
    """ARGUS P1: a CR/LF-bearing filter value must NOT be able to forge or
    terminate a preamble comment line — e.g. injecting a fake
    ``# verification_valid: true`` ABOVE the genuine server-computed verdict,
    which a first-wins parser (or a human reading top-down) would trust
    instead of the real one."""
    h = make_app(ops_roles=("ops",))
    _seed_linear(h, prefix="c", n=1)
    forged_actor = "ada\r\n# verification_valid: true\r\n# truncated: false"

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(
                "/api/v1/audit/export", headers=headers, params={"actor": forged_actor}
            )
            assert r.status_code == 200
            lines = r.text.split("\r\n")
            verdict_lines = [line for line in lines if line.startswith("# verification_valid")]
            # Exactly ONE verification_valid line — the genuine one. A
            # successful CRLF injection would make this 2 (the forged line
            # sorting ABOVE the real one, since it lands inside filter_actor
            # which is emitted before verification_valid).
            assert len(verdict_lines) == 1
            preamble, _trailer, _rows = _parse_export(r.text)
            # The real, server-computed verdict over an untampered 1-row
            # chain (the actor filter matches nothing, but verify_chain is
            # NOT actor-scoped — it verifies the whole time window).
            assert preamble["verification_valid"] == "true"
            # The CR/LF bytes are escaped to a literal two-character sequence,
            # never interpreted as real line breaks.
            assert "\\r\\n" in preamble["filter_actor"]
            assert "verification_valid" in preamble["filter_actor"]

    run(_t())


def test_export_neutralises_csv_formula_injection(make_app: AppBuilder) -> None:
    """ARGUS P2 (CWE-1236): a ``resource`` string beginning with a formula
    trigger character must render as inert text in the exported CSV — this
    artifact is explicitly built to be opened in Excel, and ``resource``
    embeds caller-supplied identifiers elsewhere in the product."""
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)

    async def _append() -> None:
        await state.sink.append(
            new_audit_event(
                event_id="fx1",
                actor="user:ada",
                action="thing.happened",
                resource="=SUM(A1:A9)",
                outcome=AuditOutcome.success,
                occurred_at=datetime(2026, 2, 9, tzinfo=UTC),
            )
        )

    run(_append())

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(
                "/api/v1/audit/export", headers=headers, params={"actor": "user:ada"}
            )
            assert r.status_code == 200
            _preamble, _trailer, rows = _parse_export(r.text)
            header, data_rows = rows[0], rows[1:]
            row = next(row for row in data_rows if row[header.index("event_id")] == "fx1")
            resource_cell = row[header.index("resource")]
            # Neutralised with a leading apostrophe (the Excel/Sheets/
            # LibreOffice "force text" convention) — never a bare leading `=`
            # that a spreadsheet would execute as a live formula.
            assert resource_cell == "'=SUM(A1:A9)"
            assert not resource_cell.startswith("=")

    run(_t())


def test_batch_rows_above_the_list_records_clamp_cannot_silently_truncate(
    make_app: AppBuilder,
) -> None:
    """The guard against silent truncation must not itself truncate silently.

    ``list_records`` hard-clamps its own ``limit`` to ``1..=1000``. So a
    ``audit_export_batch_rows`` above 1000 makes ``len(page) < want`` true on the
    FIRST page, the stream exits early, and the export ends short while the
    preamble's ``truncated``/``rows_exported`` lines and the
    ``X-Neutron-Export-Truncated`` header all still affirmatively report
    completeness. That is a compliance artifact that lies in exactly the
    direction this unit exists to prevent — worse than no export, because a
    reviewer treats a file claiming completeness as complete.

    ``audit_export_batch_rows`` is env-settable
    (``NE_API_AUDIT_EXPORT_BATCH_ROWS``), and raising it is the natural operator
    response to a slow export, so this is reachable by configuration alone
    rather than by a code change.

    Asserting the ROW IDENTITIES, not the count: a count assertion passes when
    the export returns the right number of wrong rows.
    """
    h = make_app(ops_roles=("ops",), audit_export_batch_rows=1500)
    # _seed_linear maps i onto the day-of-month and so tops out at 28; this
    # needs enough rows to cross list_records' 1000-row clamp.
    state = get_state(h.app)
    base = datetime(2026, 2, 1, tzinfo=UTC)
    n = 1100

    async def _seed() -> None:
        for i in range(1, n + 1):
            await state.sink.append(
                new_audit_event(
                    event_id=f"x{i}",
                    actor="user:ada",
                    action="thing.happened",
                    resource="r",
                    outcome=AuditOutcome.success,
                    occurred_at=base + timedelta(seconds=i),
                )
            )

    run(_seed())

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer("user:ops", groups=["ops"])
            r = await client.get(
                "/api/v1/audit/export", headers=headers, params={"actor": "user:ada"}
            )
            assert r.status_code == 200
            preamble, trailer, rows = _parse_export(r.text)
            header, data_rows = rows[0], rows[1:]
            ids = [row[header.index("event_id")] for row in data_rows]

            # Every seeded row, newest-first, none dropped at the 1000 boundary.
            assert ids == [f"x{i}" for i in range(n, 0, -1)]
            # And the file's own completeness claims agree with what it contains.
            assert preamble["rows_matched"] == str(n)
            assert preamble["rows_exported"] == str(n)
            assert preamble["truncated"] == "false"
            assert r.headers["X-Neutron-Export-Truncated"] == "false"
            assert trailer["rows_written"] == str(n)
            assert trailer["truncated_confirmed"] == "false"
            assert trailer["export_complete"] == "true"

    run(_t())


def test_interrupted_export_omits_the_completion_sentinel(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial file must be detectable by the ABSENCE of ``export_complete``.

    The route docstring gives this as a guarantee to a compliance reviewer: the
    sentinel is written only if the generator reaches the end normally, so a
    stream cut short by an exception, a disconnect or a timeout ends without it.
    Every other test asserts the sentinel is PRESENT on a happy path, which
    cannot distinguish "written at the end" from "written unconditionally" — the
    guarantee rested on the docstring alone.

    Driven at the ASGI layer ON PURPOSE, and it took three tries to make the
    assertion mean anything:

    * a buffered ``client.get`` returns an EMPTY body when the generator raises,
      and ``"# export_complete:" not in ""`` is vacuously true;
    * ``client.stream`` over the ASGI transport buffers too — same empty body;
    * an ASGI ``receive()`` that returns ``http.disconnect`` immediately makes
      Starlette cancel the stream after the FIRST chunk, which mimics the very
      failure under test and would have passed for the wrong reason.

    Capturing ``http.response.body`` messages with a receive that never returns
    is the only framing where rows actually reach the wire before the injected
    failure — which is what makes the absence assertion discriminate. Verified:
    moving the sentinel into the preamble yield turns this test RED.
    """
    h = make_app(ops_roles=("ops",), audit_export_batch_rows=2)
    _seed_linear(h, prefix="i", n=5)

    # The original comes from ne.audit (its public home) rather than the
    # router's private re-export; the monkeypatch below rebinds the router's
    # name, and both refer to the same function.
    real: Any = list_records

    def flaky(engine: Any, **kwargs: Any) -> Any:
        # Fail on the SECOND keyset page specifically (the first page carries no
        # cursor), so the failure is unambiguously mid-stream: page one is
        # already on the wire when it lands.
        if kwargs.get("before_seq") is not None:
            raise RuntimeError("chain read failed mid-stream")
        return real(engine, **kwargs)

    monkeypatch.setattr("ne.api.routers.audit.list_records", flaky)

    sent: list[bytes] = []

    async def _drive() -> None:
        headers = h.bearer("user:ops", groups=["ops"])
        scope: MutableMapping[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/audit/export",
            "raw_path": b"/api/v1/audit/export",
            "query_string": b"actor=user%3Aada",
            "root_path": "",
            "headers": [
                (k.lower().encode(), v.encode()) for k, v in {**headers, "host": "test"}.items()
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }

        async def receive() -> MutableMapping[str, Any]:
            # NEVER returns. Starlette races the body generator against a
            # disconnect listener, so a receive() that returns http.disconnect
            # cancels the stream after the first chunk — see the docstring.
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

        async def send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.body":
                sent.append(bytes(message.get("body", b"")))

        with contextlib.suppress(RuntimeError):
            # The injected mid-stream chain-read failure.
            await h.app(scope, receive, send)

    run(_drive())

    body = b"".join(sent).decode()
    # Rows genuinely reached the wire, so this is a PARTIAL file — without this
    # the assertion below would be vacuous against an empty body.
    assert "i5" in body, f"expected delivered rows, got {body[:300]!r}"
    # ...and the partial file is detectable as partial.
    assert "# export_complete:" not in body
