# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CONNECT-CSV — the polled-spreadsheet connector, proven over the real stack.

Runs the COMMITTED demo sheet (demo/acme/renewals/) through the governed write
path (real :memory: Qdrant, real sqlite quarantine, real audit chain — no mock
past the seam) and pins the honesty constraints bidirectionally:

* every intended-bad row is parked with the CONTRACT VALIDATOR'S OWN message
  (exact-string pins — a reworded validator or a scripted-reason table turns
  these RED), and every intended-good row is written and retrievable (the
  control direction);
* an unchanged re-poll writes nothing and leaves the quarantine row-set
  identical; a FIXED row leaves quarantine and ingests; a DELETED row's park
  reconciles away; a duplicated key never crashes the run and parks fail-closed;
* the quarantine parks carry the ``connector:csv:`` provenance the /connect
  status lane attributes counts by.

Mutation ledger (each verified by hand in this unit, with the unmutated tree as
control):
  M1 swap ``OdcsDataContract.validate``'s message assembly (drop the json_path
     prefix) -> the exact-reason pins here go RED; control: untouched tree green.
  M2 delete the bridge's duplicate-identity branch -> the writer's
     duplicate-item_ids ValueError aborts the run -> duplicate test RED;
     control: single-occurrence rows still written (same test's written count).
  M3 delete the bridge's invalid-record park (count-only, pre-unit behaviour)
     -> quarantined-row assertions here go RED; control: written count
     unchanged.
  M4 make ``is_unchanged`` always False -> the re-poll no-op test's
     ``written == 0`` goes RED; control: first-run written count in the same
     test.
  M5 sever the snapshot reconciliation (``reconciled = 0`` in place of the
     ``_reconcile_snapshot`` call) -> the deleted-row test goes RED; control:
     the fixed-row half of the same test (writer accept-path park clearing is a
     different mechanism and stays green under M5's first probe run).
  M6 sever ``_trim_surrounding`` (identity return) -> 5 tests RED (the
     committed-sheet counts, the trailing-space decisive pair, the unicode-edge
     sheet); control: unmutated tree green, and the in-suite ASAP/'' parks
     prove the validator is live in BOTH worlds (trim on, trim severed).
  M7 sever the HEADER-name trim only (``payload[key] = _trim_surrounding(value)``)
     -> exactly ``test_unicode_whitespace_edges_...`` RED (the BOM'd first
     header + padded ``'status '`` header have their own offender); control:
     the committed-sheet tests stay green (their header is clean), so the RED
     is attributable to the header trim alone.
  M8 reintroduce the jira/zendesk ``if report.quarantined == 0`` cursor-commit
     gate -> the two ``test_parked_record_never_stalls_...`` tests and the
     zendesk mid-export-duplicate test go RED (ledgers in those suites);
     control: unmutated tree green.
  M9 sever the bridge's empty-snapshot reconciliation guard (drop
     ``records_seen == 0``) -> ``test_empty_snapshot_is_never_authoritative_
     absence`` RED (parks falsely cleared by a header-only read); control: the
     same test's one-row-deleted half still reconciles on the unmutated tree.
  M10 revert the duplicate park reason to "the first occurrence was kept" ->
     the exact-reason pins here and in the zendesk duplicate test go RED.
  M11 revert /connect/quarantine payload bounding to top-level-only ->
     ``test_nested_payload_strings_are_bounded_too`` (ne-api) RED; control:
     the flat-payload bounding test stays green.

(The validator-message SWAP is additionally a live in-suite probe:
``test_invalid_record_parks_with_the_contract_validators_reason`` patches
``OdcsDataContract.validate`` and asserts the surfaced reason follows it.)
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path

import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.connect import ConnectorStateStore
from ne.connect.csvfile import CsvConnectorSettings, run_csv_sync

from .conftest import WriterStack

_REPO_ROOT = Path(__file__).parents[3]
_RENEWALS_DIR = _REPO_ROOT / "demo" / "acme" / "renewals"
_SHEET = _RENEWALS_DIR / "acme-renewals.csv"
_SCHEMA = _RENEWALS_DIR / "renewals.schema.json"

_SOURCE = "csv"
_ACL = ("grp:acme-demo-all",)

#: The committed sheet's shape: 19 data rows, 13 clean, 6 defective.
#: RNW-1013 is clean BY NORMALISATION: its status is ``'renewing '`` in the
#: file (the trailing space is kept there deliberately — it exercises the
#: surrounding-whitespace trim) and ingests as ``'renewing'``.
_GOOD_IDS = [
    "RNW-1001",
    "RNW-1002",
    "RNW-1003",
    "RNW-1004",
    "RNW-1005",
    "RNW-1006",
    "RNW-1008",
    "RNW-1009",
    "RNW-1011",
    "RNW-1012",
    "RNW-1013",
    "RNW-1014",
    "RNW-1016",
]

#: record_id -> the EXACT reason a viewer reads (the validator's own output;
#: the duplicate row's reason is the bridge's fail-closed duplicate message).
_EXPECTED_REASONS = {
    "RNW-1007": "$.contract_end: 'ASAP' does not match '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'",
    "RNW-1010": "$.annual_value_usd: 'N/A' does not match '^[0-9]+(\\\\.[0-9]{1,2})?$'",
    "row-13": "$.renewal_id: '' does not match '^RNW-[0-9]{4}$'",
    "RNW-1015": (
        "$: 'seats' is a required property; $: 'owner_email' is a required property; "
        "$: 'status' is a required property"
    ),
    "RNW-1017": (
        "$.owner_email: 'j.mu\\\\xf1oz@acme.example' does not match "
        "'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\\\.[A-Za-z]{2,}$'"
    ),
}


def _settings(sheet: Path = _SHEET) -> CsvConnectorSettings:
    return CsvConnectorSettings(
        path=str(sheet),
        schema_path=str(_SCHEMA),
        owner="acme-demo-fixture",
        id_column="renewal_id",
        default_acl=_ACL,
    )


def _source_id(sheet: Path = _SHEET) -> str:
    return f"{_SOURCE}/{sheet.name}"


def _item_id(record_id: str, sheet: Path = _SHEET) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{_source_id(sheet)}\n{record_id}".encode()).hexdigest()


def _dup_id(record_id: str, sheet: Path = _SHEET) -> str:
    return hashlib.sha256(
        f"{_SOURCE}\n{_source_id(sheet)}\n{record_id}\nduplicate".encode()
    ).hexdigest()


async def _run(stack: WriterStack, state_store: ConnectorStateStore, sheet: Path = _SHEET):
    return await run_csv_sync(
        _settings(sheet),
        writer=stack.writer,
        state_store=state_store,
        audit_sink=stack.sink,
        actor="connector:test",
        quarantine=stack.quarantine,
    )


def test_bad_rows_park_with_the_validators_own_reason_and_good_rows_ingest(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        outcome = await _run(stack, state_store)
        report = outcome.report
        assert report.mode == "backfill"
        assert report.records_seen == 19
        assert report.written == 13
        assert report.invalid == 6  # 5 contract violations + 1 duplicate identity
        assert report.quarantined == 6

        # Every intended-bad row parked, with the EXACT validator reason.
        parked = {q.item_id: q for q in stack.quarantine.list_quarantined()}
        assert len(parked) == 6
        for record_id, expected_reason in _EXPECTED_REASONS.items():
            row = parked[_item_id(record_id)]
            assert row.reason == expected_reason, record_id
            # Lane attribution: the /connect status counts by this prefix.
            assert row.provenance == f"connector:csv:{record_id}"
            # Inspectability: the park carries the row itself, not just an id.
            assert dict(row.payload)  # non-empty payload dict

        # The duplicate: parked under its own id, earliest occurrence written.
        # The reason claims ONLY what is invariantly true (the earliest
        # occurrence may itself have parked; later occurrences collapse into
        # one park) — pinned exactly so a drift back toward "the first was
        # kept" goes RED.
        dup = parked[_dup_id("RNW-1003")]
        assert dup.reason == (
            "duplicate-identity: record_id 'RNW-1003' from source_id "
            "'csv/acme-renewals.csv' appeared more than once in one sync run; "
            "the earliest occurrence was processed on its own merits and every "
            "later occurrence is rejected fail-closed into this single park "
            "(payload = the last occurrence seen)"
        )
        assert dup.payload["annual_value_usd"] == "47500"  # the LATER row parked

        # Control direction: every intended-good row is in the store, ACL-stamped,
        # and none of them is in quarantine.
        chunks = await stack.store.get([_item_id(r) for r in _GOOD_IDS])
        assert len(chunks) == 13
        for chunk in chunks:
            assert chunk.acl == _ACL
        assert not (set(parked) & {_item_id(r) for r in _GOOD_IDS})
        first = (await stack.store.get([_item_id("RNW-1003")]))[0]
        assert '"annual_value_usd": "45000"' in first.text  # FIRST occurrence kept

        # The lane cursor: the file's reconciled-as-of mtime, committed post-run.
        assert _source_id() in report.cursors

        # Exactly one connector.sync event, stamped, chain valid.
        with stack.engine.begin() as conn:
            events = [
                dict(r)
                for r in conn.execute(
                    sa.select(ne_audit_log).order_by(ne_audit_log.c.id)
                ).mappings()
                if r["action"] == "connector.sync"
            ]
        assert len(events) == 1
        assert events[0]["config_hash"] == frozen_config
        assert events[0]["outcome"] == "success"
        assert "written=13" in str(events[0]["payload"])
        assert (await stack.sink.verify()).valid

    asyncio.run(scenario())


def test_unchanged_repoll_writes_nothing_and_quarantine_rowset_is_stable(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        first = await _run(stack, state_store)
        assert first.report.written == 13  # control: the mechanism CAN write
        before = {(q.item_id, q.reason) for q in stack.quarantine.list_quarantined()}

        second = await _run(stack, state_store)
        assert second.report.written == 0
        assert second.report.skipped_unchanged == 13
        after = {(q.item_id, q.reason) for q in stack.quarantine.list_quarantined()}
        assert after == before  # same rows, same reasons — nothing re-quarantined anew
        assert len(after) == 6

    asyncio.run(scenario())


def test_fixed_row_leaves_quarantine_and_ingests_and_deleted_row_reconciles(
    writer_stack: WriterStack, frozen_config: str, tmp_path: Path
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()
    sheet = tmp_path / "acme-renewals.csv"
    shutil.copyfile(_SHEET, sheet)

    async def scenario() -> None:
        first = await _run(stack, state_store, sheet)
        assert first.report.quarantined == 6
        assert _item_id("RNW-1007", sheet) in {
            q.item_id for q in stack.quarantine.list_quarantined()
        }

        # The operator edit: fix the ASAP date, delete the stale duplicate line.
        raw = sheet.read_bytes()
        raw = raw.replace(
            b"RNW-1007,Meridian Logistics Ltd,ASAP,",
            b"RNW-1007,Meridian Logistics Ltd,2026-12-01,",
        )
        raw = raw.replace(
            b"RNW-1003,Cobalt Metals Inc,2026-09-30,47500,50,"
            b"d.kaplan@acme.example,active,Updated value after true-up\r\n",
            b"",
        )
        sheet.write_bytes(raw)

        second = await _run(stack, state_store, sheet)
        parked = {q.item_id for q in stack.quarantine.list_quarantined()}

        # Fixed row: written, and its park cleared by the writer's accept path.
        assert second.report.written == 1
        assert _item_id("RNW-1007", sheet) not in parked
        fixed = (await stack.store.get([_item_id("RNW-1007", sheet)]))[0]
        assert '"contract_end": "2026-12-01"' in fixed.text

        # Deleted duplicate: its park reconciled away (snapshot semantics).
        assert _dup_id("RNW-1003", sheet) not in parked

        # Control: the four still-broken rows remain parked.
        assert len(parked) == 4

    asyncio.run(scenario())


def test_trailing_space_status_ingests_as_the_trimmed_value_and_real_defects_still_park(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    """The decisive pair for surrounding-whitespace normalisation (committed sheet).

    RNW-1013's status is ``'renewing '`` IN THE FILE — it must ingest, and the
    STORED value must be the trimmed ``'renewing'`` (a bare "it ingested" would
    also pass against a connector that stopped validating). The control is
    RNW-1007 in the same run: ``'ASAP'`` in a date field is genuinely invalid
    and must still park with the validator's own reason.
    """
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        await _run(stack, state_store)

        # Ingested, and stored TRIMMED — the closing quote anchors exactness.
        chunk = (await stack.store.get([_item_id("RNW-1013")]))[0]
        assert '"status": "renewing"' in chunk.text
        assert '"status": "renewing "' not in chunk.text
        parked = {q.item_id: q for q in stack.quarantine.list_quarantined()}
        assert _item_id("RNW-1013") not in parked

        # Control: trimming did not disarm the validator — the same run still
        # parks the genuinely-unparseable date, with the validator's reason.
        assert parked[_item_id("RNW-1007")].reason == _EXPECTED_REASONS["RNW-1007"]
        assert await stack.store.get([_item_id("RNW-1007")]) == []

    asyncio.run(scenario())


def test_unicode_whitespace_edges_are_trimmed_headers_included_and_the_line_stops_there(
    writer_stack: WriterStack, frozen_config: str, tmp_path: Path
) -> None:
    """Surrounding-trim over the cases ``str.strip()`` alone does NOT settle.

    A purpose-built sheet: UTF-8 BOM on the file (would otherwise corrupt the
    first HEADER name), a padded header, NBSP / tab / ZWSP / quoted-CR edges,
    an NBSP hiding behind a ZWSP (the fixed-point case a single strip pass
    misses), and a padded id (identity must be the trimmed id). Two rows pin
    where normalisation STOPS: a whitespace-only required cell parks as ``''``
    (present-but-empty — the deliberate choice, never 'is a required
    property'), and an invalid value inside trimmed edges still parks, with
    the TRIMMED value in the validator's reason.
    """
    stack = writer_stack
    state_store = ConnectorStateStore()
    sheet = tmp_path / "edges.csv"
    rows = [
        # padded header: 'status ' must map to 'status'.
        "renewal_id,account,contract_end,annual_value_usd,seats,owner_email,status ,notes",
        "RNW-2001,Nimbus Aero,2026-12-01,10000,10,a@x.example,renewing\u00a0,",  # NBSP
        "RNW-2002,Nimbus Aero,2026-12-02,10000,10,a@x.example,\tactive,",  # tab
        "RNW-2003,Nimbus Aero,2026-12-03,10000,10,a@x.example,lapsed\u200b,",  # ZWSP
        'RNW-2004,Nimbus Aero,2026-12-04,10000,10,a@x.example,"active\r",',  # CR (CRLF relic)
        " RNW-2005 ,Nimbus Aero,2026-12-05,10000,10,a@x.example,active,",  # padded id
        # NBSP hiding behind ZWSP \u2014 the fixed-point case one strip pass misses.
        "RNW-2006,Nimbus Aero,2026-12-08,10000,10,a@x.example,renewing\u00a0\u200b,",
        "RNW-2101,Nimbus Aero,   ,10000,10,a@x.example,active,",  # whitespace-only required cell
        "RNW-2102,Nimbus Aero,\u00a0ASAP ,10000,10,a@x.example,active,",  # control: still invalid
    ]
    sheet.write_bytes(b"\xef\xbb\xbf" + "\n".join(rows).encode("utf-8") + b"\n")

    async def scenario() -> None:
        outcome = await _run(stack, state_store, sheet)
        assert outcome.report.records_seen == 8
        assert outcome.report.written == 6
        assert outcome.report.quarantined == 2

        stored = {
            "RNW-2001": '"status": "renewing"',
            "RNW-2002": '"status": "active"',
            "RNW-2003": '"status": "lapsed"',
            "RNW-2004": '"status": "active"',
            "RNW-2005": '"renewal_id": "RNW-2005"',  # identity AND value are the trimmed id
            "RNW-2006": '"status": "renewing"',
        }
        for record_id, expected in stored.items():
            chunk = (await stack.store.get([_item_id(record_id, sheet)]))[0]
            assert expected in chunk.text, record_id

        parked = {q.item_id: q for q in stack.quarantine.list_quarantined()}
        assert len(parked) == 2
        # Whitespace-only trims to the EMPTY STRING and stays PRESENT: the
        # validator names '' — never "'contract_end' is a required property".
        assert (
            parked[_item_id("RNW-2101", sheet)].reason
            == "$.contract_end: '' does not match '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'"
        )
        # Control: an invalid value inside trimmed edges still parks, and the
        # reason shows the TRIMMED value (what validation actually saw).
        assert (
            parked[_item_id("RNW-2102", sheet)].reason
            == "$.contract_end: 'ASAP' does not match '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'"
        )

    asyncio.run(scenario())


def test_empty_snapshot_is_never_authoritative_absence(
    writer_stack: WriterStack, frozen_config: str, tmp_path: Path
) -> None:
    """A truncated (header-only or zero-byte) sheet must NOT clear the parks.

    A producer that truncates-then-rewrites its export presents an empty file
    for a window; treating that as a real snapshot would reconcile every park
    away and falsely report the defects gone on the ops surfaces. An empty
    read keeps every park (fail-safe: stale-but-visible beats false-clean);
    the control is a genuine one-row-deleted snapshot afterwards, which
    reconciles normally.
    """
    stack = writer_stack
    state_store = ConnectorStateStore()
    sheet = tmp_path / "acme-renewals.csv"
    shutil.copyfile(_SHEET, sheet)

    async def scenario() -> None:
        first = await _run(stack, state_store, sheet)
        assert first.report.quarantined == 6
        before = {(q.item_id, q.reason) for q in stack.quarantine.list_quarantined()}

        # The truncate-then-rewrite race, mid-truncate: header only.
        header = _SHEET.read_bytes().split(b"\r\n", 1)[0] + b"\r\n"
        sheet.write_bytes(header)
        empty_run = await _run(stack, state_store, sheet)
        assert empty_run.report.records_seen == 0
        after_empty = {(q.item_id, q.reason) for q in stack.quarantine.list_quarantined()}
        assert after_empty == before  # every park kept — nothing falsely cleared

        # Zero-byte (no header at all): same rule.
        sheet.write_bytes(b"")
        zero_run = await _run(stack, state_store, sheet)
        assert zero_run.report.records_seen == 0
        assert {(q.item_id, q.reason) for q in stack.quarantine.list_quarantined()} == before

        # Control: a real snapshot with ONE row deleted reconciles that row's
        # park and only that one — the guard did not disable reconciliation.
        raw = _SHEET.read_bytes().replace(
            b"RNW-1003,Cobalt Metals Inc,2026-09-30,47500,50,"
            b"d.kaplan@acme.example,active,Updated value after true-up\r\n",
            b"",
        )
        sheet.write_bytes(raw)
        control = await _run(stack, state_store, sheet)
        assert control.report.records_seen == 18
        parked = {q.item_id for q in stack.quarantine.list_quarantined()}
        assert _dup_id("RNW-1003", sheet) not in parked  # the deleted row reconciled
        assert len(parked) == 5  # the five still-broken rows remain

    asyncio.run(scenario())


def test_missing_file_fails_loud_never_a_clean_empty_sync(
    writer_stack: WriterStack, frozen_config: str, tmp_path: Path
) -> None:
    import pytest

    from ne.connect.errors import CsvSyncError

    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        with pytest.raises(CsvSyncError, match="not readable"):
            await _run(stack, state_store, tmp_path / "nope.csv")

    asyncio.run(scenario())
