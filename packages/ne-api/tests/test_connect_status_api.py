# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GET /v1/connect/status (NE-132) — the connector-sync health wire surface.

Proves the SPEC-row shape + the §0-law-5 honesty contract: an unconfigured
connector is ABSENT, a configured-but-never-synced one is all-null (honestly idle),
and REAL seeded cursor / quarantine / audit rows round-trip into cursor_age_s /
quarantined_count / last_sync_seq + the fail-closed ACL-mirror enum. The reason
category is an allowlisted fixed label (never a fragment of the raw reason text).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from ne.audit.store import ensure_audit_schema, ne_audit_log
from ne.brain.write import QuarantinedWrite, SqliteWriteQuarantine
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.connect.cdc import CdcApplyReport, CdcSinkConfig, ingest_detail
from ne.connect.runner import ConnectSyncSettings
from ne.connect.state import ConnectorStateStore
from ne.contracts.types import Chunk
from ne.events import new_audit_event
from ne.schema import AuditOutcome

from .conftest import AppBuilder, Harness, get_state, run
from .test_cdc_sink_api import _sign


def test_configured_m365_remains_visible_when_only_csv_is_scheduled(tmp_path: object) -> None:
    from ne.api.connect_status import derive_connect_status

    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    ensure_audit_schema(engine)
    settings = ConnectSyncSettings(
        sources=("m365", "csv"),
        runnable_sources=("csv",),
        state_path=f"{tmp_path}/absent-state.db",
        quarantine_path=f"{tmp_path}/absent-quarantine.db",
    )
    api_settings = SimpleNamespace(
        cdc_webhook_secret=SecretStr(""),
        cdc_tables="",
        cdc_quarantine_path="",
    )

    status = derive_connect_status(settings, api_settings, engine)  # type: ignore[arg-type]

    assert [(row.id, row.kind) for row in status.connectors] == [
        ("m365", "m365"),
        ("csv", "csv"),
    ]
    assert [(row.id, row.posture) for row in status.connectors] == [
        ("m365", "snapshot"),
        ("csv", "live"),
    ]
    assert settings.sources_to_run == ("csv",)


_TOP_KEYS = {"connectors", "quarantine_total", "quarantine_reasons"}
_CONNECTOR_KEYS = {
    "id",
    "kind",
    "posture",
    "cursor_age_s",
    "last_checked_age_s",
    "acl_mirror",
    "last_sync_seq",
    "quarantined_count",
    "sync_allowed",
}

# The valid CDC secret + table map from test_cdc_sink_api.py (source_name debezium).
_CDC_SECRET = "whsec_" + base64.b64encode(b"cdc-webhook-key-32-bytes-long!!!!").decode()
_CDC_TABLES = json.dumps(
    {
        "source_name": "debezium",
        "tables": {
            "public.customers": {
                "key_columns": ["id"],
                "content_columns": ["name"],
                "acl": {"principal_column": "acl_group"},
            }
        },
    }
)


def _configure_fixture_source(
    monkeypatch: pytest.MonkeyPatch, *, state_path: str, quarantine_path: str
) -> None:
    """Set the NE_CONNECT_SYNC_* env for a single 'fixture' pull source.

    Must run BEFORE make_app() — build_platform constructs ConnectSyncSettings() from
    env, and the validator REQUIRES both a state + quarantine path when sources set.
    """
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["fixture"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", state_path)
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", quarantine_path)
    # The API resolves the connector's canonical runtime identity at boot. The
    # fixture settings only require a non-empty replay path/owner here; these
    # status tests never execute the connector or read the replay file.
    monkeypatch.setenv("NE_CONNECT_FIXTURE_PATH", f"{state_path}.fixture.json")
    monkeypatch.setenv("NE_CONNECT_FIXTURE_OWNER", "connect-status-test")


def _seq_for(state: object, resource: str) -> int:
    audit_engine = state.audit_engine  # type: ignore[attr-defined]
    with audit_engine.connect() as conn:
        seq = conn.execute(
            sa.select(sa.func.max(ne_audit_log.c.seq)).where(ne_audit_log.c.resource == resource)
        ).scalar()
    assert seq is not None
    return int(seq)


def _knowledge_chunk(chunk_id: str, connector: str | None) -> Chunk:
    metadata = {
        "source_system": "fixture",
        "source_ref": f"ref-{chunk_id}",
        # These manually seeded status fixtures carry no external source
        # identity. Connector-health filtering remains active below this marker.
        "source_status": "no_source",
    }
    if connector is not None:
        metadata["connector"] = connector
    return Chunk(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        text=chunk_id,
        metadata=metadata,
        acl=("eng",),
    )


def test_requires_auth(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/connect/status")
            assert r.status_code == 401

    run(_t())


def test_shape_and_honest_empty(make_app: AppBuilder) -> None:
    # No NE_CONNECT_SYNC_* env, no CDC ⇒ no connectors, and NO quarantine store exists
    # so quarantine_total is honestly unknown (None), not a fake zero.
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/connect/status", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            body = r.json()
            assert set(body) == _TOP_KEYS
            assert body["connectors"] == []
            assert body["quarantine_total"] is None
            assert body["quarantine_reasons"] == []

    run(_t())


def test_configured_connector_honestly_idle(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    # State + quarantine paths point at NONEXISTENT files and no sync ever ran ⇒ the
    # lane is present but ALL health fields are null (honestly idle, never fake-green).
    _configure_fixture_source(
        monkeypatch,
        state_path=f"{tmp_path}/missing-state.db",
        quarantine_path=f"{tmp_path}/missing-q.db",
    )
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/connect/status", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            body = r.json()
            assert len(body["connectors"]) == 1
            c = body["connectors"][0]
            assert set(c) == _CONNECTOR_KEYS
            assert c["id"] == "fixture" and c["kind"] == "fixture"
            assert c["cursor_age_s"] is None
            # Never checked ≠ checked-and-found-nothing: with NO sync event the
            # run age is honestly absent, never a fabricated 0.0 ("just checked").
            assert c["last_checked_age_s"] is None
            assert c["acl_mirror"] is None
            assert c["last_sync_seq"] is None
            assert c["quarantined_count"] is None
            assert c["sync_allowed"] is False
            assert body["quarantine_total"] is None

    run(_t())


def test_real_state_derivation(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    state_path = f"{tmp_path}/state.db"
    quarantine_path = f"{tmp_path}/quarantine.db"
    _configure_fixture_source(monkeypatch, state_path=state_path, quarantine_path=quarantine_path)
    h = make_app()
    state = get_state(h.app)

    # Seed a REAL cursor, a REAL quarantine row, and a REAL success sync audit event.
    ConnectorStateStore(state_path).set_cursor("fixture", "main", "2026-07-11T00:00:00+00:00")
    # A REALISTIC writer shape-rejection reason: the raw text is a jsonschema message
    # built as f"{json_path}: {message}" (ne.brain.write.writer), embedding a customer
    # field name + offending value. The wire must NEVER echo any of it — only the
    # allowlisted 'schema-validation' category may surface (C7 leak guard).
    _RAW_REASON = "$.metadata.patient_ssn: '123-45-6789' is not valid under any schema"
    SqliteWriteQuarantine(quarantine_path).record(
        QuarantinedWrite(
            item_id="fixture:r1",
            reason=_RAW_REASON,
            payload={},
            provenance="connector:fixture:r1",
            shape="connector.record",
            created_at=datetime.now(UTC).isoformat(),
        )
    )

    async def _seed() -> None:
        await state.sink.append(
            new_audit_event(
                action="connector.sync",
                resource="connector://fixture",
                outcome=AuditOutcome.success,
                actor="t",
            )
        )

    run(_seed())
    seq = _seq_for(state, "connector://fixture")

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/connect/status", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            body = r.json()
            c = body["connectors"][0]
            assert isinstance(c["cursor_age_s"], float) and c["cursor_age_s"] > 0
            assert c["acl_mirror"] == "ok"
            assert c["last_sync_seq"] == seq
            assert c["quarantined_count"] == 1
            assert body["quarantine_total"] == 1
            # Category is the ALLOWLISTED fixed label, NOT any fragment of the raw
            # reason — the customer field name + offending value never reach the wire.
            assert body["quarantine_reasons"] == [{"reason": "schema-validation", "count": 1}]
            assert "patient_ssn" not in r.text
            assert "123-45-6789" not in r.text

    run(_t())


def test_m365_public_alias_reads_canonical_runtime_state(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    state_path = f"{tmp_path}/m365-state.db"
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["m365"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", state_path)
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", f"{tmp_path}/m365-q.db")
    monkeypatch.setenv("NE_M365_TENANT_ID", "test-tenant")
    monkeypatch.setenv("NE_M365_CLIENT_ID", "test-client")
    monkeypatch.setenv("NE_M365_AUTH_METHOD", "secret")
    monkeypatch.setenv("NE_M365_CLIENT_SECRET", "test-only-secret")
    monkeypatch.setenv("NE_M365_OWNER", "m365-status-test")
    monkeypatch.setenv("NE_M365_SITE_IDS", '["site-scope"]')
    h = make_app()
    state = get_state(h.app)

    ConnectorStateStore(state_path).set_cursor(
        "m365-sharepoint", "main", "2026-08-19T00:00:00+00:00"
    )

    async def _seed() -> None:
        await state.sink.append(
            new_audit_event(
                action="connector.sync",
                resource="connector://m365-sharepoint",
                outcome=AuditOutcome.success,
                actor="test",
            )
        )

    run(_seed())
    canonical_seq = _seq_for(state, "connector://m365-sharepoint")

    async def _t() -> None:
        async with h.client() as client:
            response = await client.get("/api/v1/connect/status", headers=h.bearer("u", groups=[]))
            assert response.status_code == 200
            connector = response.json()["connectors"][0]
            assert connector["id"] == "m365"
            assert connector["kind"] == "m365"
            assert connector["cursor_age_s"] is not None
            assert connector["last_checked_age_s"] is not None
            assert connector["last_sync_seq"] == canonical_seq

    run(_t())


def test_checked_recently_but_no_new_records_serves_both_facts(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """SPEC §12 item 9, pinned: when-we-last-looked and how-fresh-the-newest-record-is
    are TWO facts, and the wire must carry both.

    The live-box shape: the cron ran a sync cycle two minutes ago (a real
    ``connector.sync`` success event) and found NOTHING new — the cursor last
    advanced ~25 h ago. ``last_checked_age_s`` must be the RECENT run age and
    ``cursor_age_s`` must stay the OLD record age. Against the single-field
    implementation this test fails (no ``last_checked_age_s`` on the wire) —
    which is the defect: the pane read "Last sync: yesterday" while the
    connector was polling every fifteen minutes.
    """
    state_path = f"{tmp_path}/state.db"
    _configure_fixture_source(
        monkeypatch, state_path=state_path, quarantine_path=f"{tmp_path}/q.db"
    )
    h = make_app()
    state = get_state(h.app)

    # A REAL cursor row whose updated_at is ~25 h old (the newest record seen).
    ConnectorStateStore(state_path).set_cursor("fixture", "main", "2026-07-11T00:00:00+00:00")
    import sqlite3 as _sqlite3

    stale = (datetime.now(UTC) - timedelta(seconds=90_000)).isoformat()
    conn = _sqlite3.connect(state_path)
    conn.execute("UPDATE cursors SET updated_at = ?", (stale,))
    conn.commit()
    conn.close()

    # A REAL sync run 120 s ago that found no changes (success, cursor untouched).
    async def _seed() -> None:
        await state.sink.append(
            new_audit_event(
                action="connector.sync",
                resource="connector://fixture",
                outcome=AuditOutcome.success,
                actor="t",
                occurred_at=datetime.now(UTC) - timedelta(seconds=120),
            )
        )

    run(_seed())

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/connect/status", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            c = r.json()["connectors"][0]
            # The run fact: checked ~2 min ago (bounded above well under the
            # cursor age, so a collapse of the two fields cannot pass).
            assert isinstance(c["last_checked_age_s"], float)
            assert 115 <= c["last_checked_age_s"] < 1_000
            # The record fact: the newest record is still ~25 h old.
            assert isinstance(c["cursor_age_s"], float)
            assert c["cursor_age_s"] >= 85_000

    run(_t())


def test_degraded_acl_mirror_fail_closed(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    _configure_fixture_source(
        monkeypatch, state_path=f"{tmp_path}/state.db", quarantine_path=f"{tmp_path}/q.db"
    )
    h = make_app(
        chunks=[
            _knowledge_chunk("fixture-result", "fixture"),
            _knowledge_chunk("healthy-sibling", "salesforce"),
            _knowledge_chunk("curated", None),
        ]
    )
    state = get_state(h.app)

    async def _seed_denied() -> None:
        await state.sink.append(
            new_audit_event(
                action="connector.sync",
                resource="connector://fixture",
                outcome=AuditOutcome.denied,
                actor="t",
            )
        )

    run(_seed_denied())

    # A later foreign action on the SAME resource must not collide with the
    # connector.sync fact. Reported health and enforcement are both conjunctive
    # on action + resource, so this success cannot erase the denied sync.
    async def _seed_colliding_foreign_action() -> None:
        await state.sink.append(
            new_audit_event(
                action="cdc.ingest",
                resource="connector://fixture",
                outcome=AuditOutcome.success,
                actor="t",
            )
        )

    run(_seed_colliding_foreign_action())

    async def _degraded() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/connect/status", headers=h.bearer("u", groups=[]))
            c = r.json()["connectors"][0]
            assert c["acl_mirror"] == "degraded"
            # A FAILED cycle still ran: the run age is served (the failure is
            # acl_mirror's fact), so a failing connector never renders as
            # never-checked.
            assert isinstance(c["last_checked_age_s"], float)
            assert c["last_checked_age_s"] < 60

            headers = h.bearer("u", groups=["eng"])
            search = await client.post(
                "/api/v1/brain/search",
                json={"query": "q", "top_k": 10},
                headers=headers,
            )
            assert search.status_code == 200, search.text
            assert {row["chunk"]["chunk_id"] for row in search.json()["chunks"]} == {
                "healthy-sibling",
                "curated",
            }
            replay = await client.post(
                "/api/v1/brain/retrieve",
                json={"chunk_ids": ["fixture-result", "healthy-sibling", "curated"]},
                headers=headers,
            )
            assert replay.status_code == 200, replay.text
            assert {row["chunk"]["chunk_id"] for row in replay.json()["chunks"]} == {
                "healthy-sibling",
                "curated",
            }

    run(_degraded())

    # A LATER success event flips the mirror back to 'ok' (latest-event-wins).
    async def _seed_success() -> None:
        await state.sink.append(
            new_audit_event(
                action="connector.sync",
                resource="connector://fixture",
                outcome=AuditOutcome.success,
                actor="t",
            )
        )

    run(_seed_success())
    success_seq = _seq_for(state, "connector://fixture")

    async def _ok() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/connect/status", headers=h.bearer("u", groups=[]))
            c = r.json()["connectors"][0]
            assert c["acl_mirror"] == "ok"
            assert c["last_sync_seq"] == success_seq

            search = await client.post(
                "/api/v1/brain/search",
                json={"query": "q", "top_k": 10},
                headers=h.bearer("u", groups=["eng"]),
            )
            assert search.status_code == 200, search.text
            assert {row["chunk"]["chunk_id"] for row in search.json()["chunks"]} == {
                "fixture-result",
                "healthy-sibling",
                "curated",
            }

    run(_ok())


def test_degraded_source_withholds_graph_rows_and_success_restores_them(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    _configure_fixture_source(
        monkeypatch,
        state_path=f"{tmp_path}/state.db",
        quarantine_path=f"{tmp_path}/q.db",
    )
    store = InMemoryGraphStore()

    async def _seed_graph() -> None:
        await store.upsert_nodes(
            [
                make_node(
                    "a", "Account", ("eng",), source_system="fixture", source_status="no_source"
                ),
                make_node(
                    "b", "Account", ("eng",), source_system="fixture", source_status="no_source"
                ),
                make_node(
                    "m", "Account", ("eng",), source_system="manual", source_status="no_source"
                ),
                make_node(
                    "n", "Account", ("eng",), source_system="manual", source_status="no_source"
                ),
            ]
        )
        fixture_edge = make_edge("LINK", "a", "b", ("eng",), key="a_b")
        fixture_edge["source_system"] = "fixture"
        fixture_edge["source_status"] = "no_source"
        manual_edge = make_edge("LINK", "m", "n", ("eng",), key="m_n")
        manual_edge["source_system"] = "manual"
        manual_edge["source_status"] = "no_source"
        await store.upsert_edges([fixture_edge, manual_edge])

    run(_seed_graph())
    h = make_app(
        brain_graph_settings=BrainGraphSettings(enabled=True),
        graph_store=store,
    )
    state = get_state(h.app)

    async def _seed(outcome: AuditOutcome) -> None:
        await state.sink.append(
            new_audit_event(
                action="connector.sync",
                resource="connector://fixture",
                outcome=outcome,
                actor="t",
            )
        )

    run(_seed(AuditOutcome.denied))

    async def _query(expected: set[str]) -> None:
        async with h.client() as client:
            response = await client.post(
                "/api/v1/graph/query",
                json={"anchor_keys": ["a", "m"], "hops": 1},
                headers=h.bearer("u", groups=["eng"]),
            )
            assert response.status_code == 200, response.text
            assert {node["key"] for node in response.json()["nodes"]} == expected

    run(_query({"m", "n"}))
    run(_seed(AuditOutcome.success))
    run(_query({"a", "b", "m", "n"}))


async def _post_cdc_heartbeat(h: Harness) -> None:
    """Drive a REAL empty authenticated webhook through the mounted /cdc/v1 app.

    The Debezium heartbeat shape: an authenticated empty body reaches
    ``apply(())`` (app.py -> ``parse_change_events(b"") == ()``) and the sink
    emits its unconditional ``cdc.ingest`` success summary with ``written=0`` —
    a batch that RAN but accepted NOTHING.
    """
    async with h.client() as client:
        r = await client.post("/cdc/v1/events", content=b"", headers=_sign(b""))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["received"] == 0 and body["written"] == 0


def test_cdc_status_reparse_failure_keeps_the_default_sink_resource(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """A post-boot parse failure must still query the sink's real default lane.

    Factory composition parsed this same config successfully and built the CDC
    service. The status reader defensively re-parses it on every request. Force
    only that later read to fail after a REAL sink heartbeat has emitted
    ``cdc://debezium``; the fallback must retain that event's served evidence,
    never invent a separate idle ``cdc://cdc`` lane.
    """
    default_tables = json.loads(_CDC_TABLES)
    del default_tables["source_name"]
    h = make_app(
        cdc_webhook_secret=_CDC_SECRET,
        cdc_tables=json.dumps(default_tables),
        cdc_quarantine_path=f"{tmp_path}/cdc-default-q.db",
    )
    state = get_state(h.app)
    run(_post_cdc_heartbeat(h))
    heartbeat_seq = _seq_for(state, "cdc://debezium")

    def _reject_status_reparse(_text: str) -> CdcSinkConfig:
        raise ValueError("synthetic post-boot reparse failure")

    monkeypatch.setattr(CdcSinkConfig, "from_json", _reject_status_reparse)

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/connect/status", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            cdc = [c for c in r.json()["connectors"] if c["kind"] == "cdc"]
            assert len(cdc) == 1
            lane = cdc[0]
            assert lane["id"] == "debezium"
            assert lane["last_sync_seq"] == heartbeat_seq
            assert isinstance(lane["last_checked_age_s"], float)
            assert lane["last_checked_age_s"] < 600
            assert lane["acl_mirror"] == "ok"
            # The real producer event was a heartbeat: the lane ran but accepted
            # no record, so record freshness stays honestly absent.
            assert lane["cursor_age_s"] is None

    run(_t())


def test_cdc_lane(make_app: AppBuilder, tmp_path: object) -> None:
    """The CDC lane's two freshness facts are DERIVED APART (SPEC §12 item 9).

    An hour-old ingest batch ACCEPTED a record (its counts detail built by the
    REAL producer, ``ne.connect.cdc.ingest_detail``); a real empty authenticated
    webhook (Debezium heartbeat) then lands moments before the read. "Last
    checked" must advance to the heartbeat — the governed apply RAN — while
    "Newest record" must stay the hour-old accepting batch: an empty batch's
    ingest event asserts no record freshness. Against an implementation that
    serves the latest event age for BOTH fields, the ``cursor_age_s`` floor
    below fails RED (the heartbeat is seconds old, not an hour).
    """
    cdc_quarantine_path = f"{tmp_path}/cdc-quarantine.db"
    h = make_app(
        cdc_webhook_secret=_CDC_SECRET,
        cdc_tables=_CDC_TABLES,
        cdc_quarantine_path=cdc_quarantine_path,
    )
    state = get_state(h.app)

    # A REAL cdc-provenance quarantine row (the count fact, unchanged).
    SqliteWriteQuarantine(cdc_quarantine_path).record(
        QuarantinedWrite(
            item_id="cdc:debezium:public.customers:1",
            reason="cdc-acl-tainted",
            payload={},
            provenance="cdc:debezium:public.customers",
            shape="connector.record",
            created_at=datetime.now(UTC).isoformat(),
        )
    )

    # The hour-old ACCEPTING batch: detail from the producer helper — the same
    # function ``CdcSinkService.apply`` emits through (pinned by the ne-connect
    # round-trip tests), so this seed cannot drift from the live format.
    async def _seed_accepting() -> None:
        await state.sink.append(
            new_audit_event(
                action="cdc.ingest",
                resource="cdc://debezium",
                outcome=AuditOutcome.success,
                actor="t",
                detail=ingest_detail(CdcApplyReport(received=1, written=1)),
                occurred_at=datetime.now(UTC) - timedelta(seconds=3600),
            )
        )

    run(_seed_accepting())
    run(_post_cdc_heartbeat(h))
    heartbeat_seq = _seq_for(state, "cdc://debezium")

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/connect/status", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            body = r.json()
            cdc = [c for c in body["connectors"] if c["kind"] == "cdc"]
            assert len(cdc) == 1
            c = cdc[0]
            assert c["id"] == "debezium"
            # The run fact: the heartbeat, moments ago (generous ceiling for a
            # loaded box — still far under the accepting batch's hour).
            assert isinstance(c["last_checked_age_s"], float)
            assert c["last_checked_age_s"] < 600
            # The record fact: the heartbeat did NOT advance it — the newest
            # ACCEPTED record is still the hour-old batch's.
            assert isinstance(c["cursor_age_s"], float)
            assert c["cursor_age_s"] >= 3500
            # The lane's sync record is the LATEST run (the heartbeat), so the
            # two facts provably come from DIFFERENT events.
            assert c["last_sync_seq"] == heartbeat_seq
            assert c["acl_mirror"] == "ok"
            assert c["quarantined_count"] == 1
            assert body["quarantine_total"] == 1

    run(_t())


def test_cdc_lane_heartbeats_only_serves_no_record_freshness(
    make_app: AppBuilder, tmp_path: object
) -> None:
    """A CDC lane that has processed batches but never ACCEPTED a record.

    The failure scenario the split exists to kill, on the push lane: an
    authenticated empty webhook (heartbeat / tombstone-only batch) on a lane
    holding no records. "Last checked" advances — the governed apply ran, that
    fact is honest and useful — but "Newest record" must be honestly ABSENT
    (the pull lanes' never-accepted idiom), never "moments ago".

    Also seeds a detail-less ``cdc.ingest`` row (a foreign/hand-emitted event —
    the exact seed the previous version of this test used to PIN the collapse):
    an event whose detail does not parse as the sink's counts shape must count
    as NOT accepting — fail toward absence, never toward freshness.
    """
    h = make_app(
        cdc_webhook_secret=_CDC_SECRET,
        cdc_tables=_CDC_TABLES,
        cdc_quarantine_path=f"{tmp_path}/cdc-q.db",
    )
    state = get_state(h.app)

    async def _seed_bare() -> None:
        await state.sink.append(
            new_audit_event(
                action="cdc.ingest",
                resource="cdc://debezium",
                outcome=AuditOutcome.success,
                actor="t",
                occurred_at=datetime.now(UTC) - timedelta(seconds=45),
            )
        )

    run(_seed_bare())
    run(_post_cdc_heartbeat(h))

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/connect/status", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            cdc = [c for c in r.json()["connectors"] if c["kind"] == "cdc"]
            assert len(cdc) == 1
            c = cdc[0]
            # The lane HAS been checked — that stays served.
            assert isinstance(c["last_checked_age_s"], float)
            assert c["last_checked_age_s"] < 600
            assert c["acl_mirror"] == "ok"
            assert c["last_sync_seq"] is not None
            # But it is not known to hold ANY record: no fresh timestamp.
            assert c["cursor_age_s"] is None

    run(_t())
