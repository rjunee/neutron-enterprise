# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Governed durable connector Sync now API surface."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from ne.api.connect_sync import sync_request_tool_name
from ne.audit.store import ne_audit_log
from ne.govern import GovernedPdp
from ne.govern.opa import OpaClient
from ne.runtime.scheduler import ne_job_requests, ne_job_runs, ne_scheduled_jobs

from .conftest import AppBuilder, Harness, _rego_faithful_doc, get_state, run


def _configure_fixture_source(
    monkeypatch: pytest.MonkeyPatch, *, state_path: str, quarantine_path: str
) -> None:
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["fixture"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", state_path)
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", quarantine_path)
    monkeypatch.setenv("NE_CONNECT_FIXTURE_PATH", f"{state_path}.fixture.json")
    monkeypatch.setenv("NE_CONNECT_FIXTURE_OWNER", "connect-sync-api-test")


def _seed_job(h: Harness) -> str:
    return _seed_named_job(h, "fixture")


def _seed_named_job(h: Harness, source: str) -> str:
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    engine = sa.create_engine(h.runtime_url)
    with engine.begin() as conn:
        conn.execute(
            ne_scheduled_jobs.insert().values(
                job_id=job_id,
                name=f"connect-sync-{source}",
                cron="*/15 * * * *",
                agent=f"connect-sync-{source}",
                toolset=json.dumps([f"write.internal.connect.sync_{source}"]),
                created_at=now,
                next_run_at=now + timedelta(minutes=15),
            )
        )
    engine.dispose()
    return job_id


def _configure_graph_visible_csv_scheduled(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["m365","csv"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_RUNNABLE_SOURCES", '["csv"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", f"{tmp_path}/state.db")
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", f"{tmp_path}/q.db")
    monkeypatch.setenv("NE_M365_TENANT_ID", "tenant-id")
    monkeypatch.setenv("NE_M365_CLIENT_ID", "client-id")
    monkeypatch.setenv("NE_M365_AUTH_METHOD", "secret")
    monkeypatch.setenv("NE_M365_CLIENT_SECRET", "test-only-secret")
    monkeypatch.setenv("NE_M365_OWNER", "m365-status-test")
    monkeypatch.setenv("NE_M365_DRIVE_IDS", '["drive-1"]')
    monkeypatch.setenv("NE_CONNECT_CSV_PATH", f"{tmp_path}/records.csv")
    monkeypatch.setenv("NE_CONNECT_CSV_SCHEMA_PATH", f"{tmp_path}/schema.json")
    monkeypatch.setenv("NE_CONNECT_CSV_OWNER", "csv-status-test")
    monkeypatch.setenv("NE_CONNECT_CSV_DEFAULT_ACL", '["grp:demo-all"]')


_T1_COUNTS: dict[str, Any] = {
    "mode": "backfill",
    "records_seen": 3,
    "written": 2,
    "skipped_unchanged": 1,
    "invalid": 0,
    "quarantined": 0,
}

# The Tier-2 projection report a Graph-enabled source appends to the SAME
# terminal payload (ne.worker.ingest._sync_handler sets result["graph"] from
# GraphSyncReport.model_dump). Values are deliberately all-distinct so an
# equality assertion cannot pass on a transposed or zero-filled projection.
_GRAPH_COUNTS: dict[str, Any] = {
    "mode": "delta",
    "seen": 9,
    "upserted": 4,
    "unchanged": 3,
    "acl_healed": 2,
    "quarantined": 1,
    "unmapped": 6,
    "pruned": 5,
    "pending_edges": 7,
    "evidence_pending": 8,
}


def _terminal_success(h: Harness, request_id: str, payload: dict[str, Any] | None = None) -> None:
    now = datetime.now(UTC)
    output = json.dumps(_T1_COUNTS if payload is None else payload)
    engine = sa.create_engine(h.runtime_url)
    with engine.begin() as conn:
        job_id = conn.execute(
            sa.select(ne_job_requests.c.job_id).where(ne_job_requests.c.request_id == request_id)
        ).scalar_one()
        conn.execute(
            ne_job_runs.insert().values(
                run_id=request_id,
                job_id=job_id,
                started_at=now,
                finished_at=now,
                succeeded=True,
                output=output,
            )
        )
        conn.execute(
            ne_job_requests.update()
            .where(ne_job_requests.c.request_id == request_id)
            .values(
                status="succeeded",
                run_id=request_id,
                finished_at=now,
                succeeded=True,
            )
        )
    engine.dispose()


def _rewrite_terminal_output(h: Harness, request_id: str, output: str) -> None:
    """Replace the terminal run output in place (the run row is the wire source)."""
    engine = sa.create_engine(h.runtime_url)
    with engine.begin() as conn:
        conn.execute(
            ne_job_runs.update().where(ne_job_runs.c.run_id == request_id).values(output=output)
        )
    engine.dispose()


def _string_values(value: Any) -> list[str]:
    """Every string reachable in a decoded JSON value, at any depth."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for child in value.values() for s in _string_values(child)]
    if isinstance(value, list):
        return [s for child in value for s in _string_values(child)]
    return []


def test_status_advises_visibility_but_post_role_gate_is_authoritative(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _configure_fixture_source(
        monkeypatch,
        state_path=f"{tmp_path}/state.db",
        quarantine_path=f"{tmp_path}/q.db",
    )
    h = make_app(ops_roles=("ops",))
    _seed_job(h)

    async def _t() -> None:
        async with h.client() as client:
            hidden = await client.get(
                "/api/v1/connect/status", headers=h.bearer("user:reader", groups=[])
            )
            assert hidden.status_code == 200
            assert hidden.json()["connectors"][0]["sync_allowed"] is False

            refused = await client.post(
                "/api/v1/connect/fixture/sync",
                headers=h.bearer("user:reader", groups=[]),
            )
            assert refused.status_code == 403

            visible = await client.get(
                "/api/v1/connect/status", headers=h.bearer("user:operator", groups=["ops"])
            )
            assert visible.json()["connectors"][0]["sync_allowed"] is True

        engine = sa.create_engine(h.runtime_url)
        with engine.connect() as conn:
            request_count = conn.execute(
                sa.select(sa.func.count()).select_from(ne_job_requests)
            ).scalar()
            assert request_count == 0
        engine.dispose()

    run(_t())


def test_unscheduled_m365_is_visible_but_not_syncable_while_csv_is_syncable(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _configure_graph_visible_csv_scheduled(monkeypatch, tmp_path)
    h = make_app(ops_roles=("ops",))
    _seed_named_job(h, "csv")

    async def _t() -> None:
        async with h.client() as client:
            operator = h.bearer("user:operator", groups=["ops"])
            status = await client.get("/api/v1/connect/status", headers=operator)
            assert status.status_code == 200
            connectors = {row["id"]: row for row in status.json()["connectors"]}
            assert set(connectors) == {"m365", "csv"}
            assert connectors["m365"]["sync_allowed"] is False
            assert connectors["csv"]["sync_allowed"] is True

            m365 = await client.post("/api/v1/connect/m365/sync", headers=operator)
            assert m365.status_code == 404
            csv = await client.post("/api/v1/connect/csv/sync", headers=operator)
            assert csv.status_code == 202

    run(_t())


def test_unconfigured_source_is_readable_but_has_no_executable_sync_capability(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["fixture"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", f"{tmp_path}/state.db")
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", f"{tmp_path}/q.db")
    monkeypatch.delenv("NE_CONNECT_FIXTURE_PATH", raising=False)
    monkeypatch.delenv("NE_CONNECT_FIXTURE_OWNER", raising=False)
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)
    action = sync_request_tool_name("fixture")

    assert action not in {spec.name for spec in state.executor.tool_specs}

    async def _t() -> None:
        async with h.client() as client:
            operator = h.bearer("user:operator", groups=["ops"])
            status = await client.get("/api/v1/connect/status", headers=operator)
            assert status.status_code == 200
            assert status.json()["connectors"][0]["id"] == "fixture"
            assert status.json()["connectors"][0]["sync_allowed"] is False

            quarantine = await client.get("/api/v1/connect/quarantine", headers=operator)
            assert quarantine.status_code == 200
            assert quarantine.json()["total"] is None

            refused = await client.post("/api/v1/connect/fixture/sync", headers=operator)
            assert refused.status_code == 404

    run(_t())


def test_request_is_durable_deduped_and_counts_only_poll_is_scoped(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _configure_fixture_source(
        monkeypatch,
        state_path=f"{tmp_path}/state.db",
        quarantine_path=f"{tmp_path}/q.db",
    )
    h = make_app(ops_roles=("ops",))
    _seed_job(h)
    sentinel = "CUSTOMER-BODY-MUST-NOT-PERSIST"

    async def _t() -> None:
        async with h.client() as client:
            operator = h.bearer("user:operator", groups=["ops"])
            smuggled = await client.post(
                "/api/v1/connect/fixture/sync",
                headers=operator,
                json={"ignored_customer_body": sentinel},
            )
            assert smuggled.status_code == 422
            assert sentinel not in smuggled.text

            first = await client.post(
                "/api/v1/connect/fixture/sync",
                headers=operator,
            )
            assert first.status_code == 202
            body = first.json()
            assert body["source"] == "fixture"
            assert body["status"] == "queued"
            assert body["counts"] is None
            assert sentinel not in first.text

            joined = await client.post("/api/v1/connect/fixture/sync", headers=operator)
            assert joined.status_code == 200
            assert joined.json()["request_id"] == body["request_id"]

            hidden = await client.get(
                f"/api/v1/connect/sync/{body['request_id']}",
                headers=h.bearer("user:someone-else", groups=[]),
            )
            assert hidden.status_code == 404

            _terminal_success(h, body["request_id"])
            done = await client.get(f"/api/v1/connect/sync/{body['request_id']}", headers=operator)
            assert done.status_code == 200
            assert done.json()["status"] == "succeeded"
            # A source with no Tier-2 projection reports graph=None — absent,
            # never a zero-filled projection that never ran.
            assert done.json()["counts"] == {**_T1_COUNTS, "graph": None}

        engine = sa.create_engine(h.runtime_url)
        with engine.connect() as conn:
            request_count = conn.execute(
                sa.select(sa.func.count()).select_from(ne_job_requests)
            ).scalar()
            assert request_count == 1
            events = conn.execute(
                sa.select(ne_audit_log.c.action, ne_audit_log.c.actor, ne_audit_log.c.payload)
            ).all()
        engine.dispose()
        assert any(
            action == "job.request" and actor == "user:operator" for action, actor, _ in events
        )
        assert sentinel not in json.dumps([payload for _, _, payload in events])

    run(_t())


class _DenySyncPdpFactory:
    @staticmethod
    def from_settings(_settings: Any, *, sink: Any) -> GovernedPdp:
        def _handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            action = payload["input"]["action"]
            document = _rego_faithful_doc(action, payload["input"].get("context") or {})
            if action == "write.internal.connect.request_sync_fixture":
                document = {
                    "effect": "deny",
                    "tier": 1,
                    "reason": "sync request revoked",
                    "obligations": {},
                    "policy_version": "test",
                }
            return httpx.Response(200, json={"result": document})

        client = httpx.AsyncClient(
            base_url="http://opa.test", transport=httpx.MockTransport(_handler)
        )
        return GovernedPdp(OpaClient("http://opa.test", client=client), sink)


def test_post_rechecks_pdp_after_role_gate(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _configure_fixture_source(
        monkeypatch,
        state_path=f"{tmp_path}/state.db",
        quarantine_path=f"{tmp_path}/q.db",
    )
    h = make_app(ops_roles=("ops",), pdp_factory=_DenySyncPdpFactory)
    _seed_job(h)

    async def _t() -> None:
        async with h.client() as client:
            refused = await client.post(
                "/api/v1/connect/fixture/sync",
                headers=h.bearer("user:operator", groups=["ops"]),
            )
            assert refused.status_code == 403
        engine = sa.create_engine(h.runtime_url)
        with engine.connect() as conn:
            request_count = conn.execute(
                sa.select(sa.func.count()).select_from(ne_job_requests)
            ).scalar()
            assert request_count == 0
        engine.dispose()

    run(_t())


def test_graph_enabled_terminal_payload_is_projected_with_exact_graph_counts(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A terminal payload carrying the Tier-2 graph report is a SUCCESS.

    ``ne.worker.ingest._sync_handler`` appends ``result["graph"]`` for a
    Graph-enabled source, so the terminal payload of a real m365 sync is the T1
    counts PLUS that report. The wire declares it; it is neither refused as an
    invalid terminal nor projected with the graph counts dropped.
    """
    _configure_fixture_source(
        monkeypatch,
        state_path=f"{tmp_path}/state.db",
        quarantine_path=f"{tmp_path}/q.db",
    )
    h = make_app(ops_roles=("ops",))
    _seed_job(h)

    async def _t() -> None:
        async with h.client() as client:
            operator = h.bearer("user:operator", groups=["ops"])
            first = await client.post("/api/v1/connect/fixture/sync", headers=operator)
            assert first.status_code == 202
            request_id = first.json()["request_id"]

            _terminal_success(h, request_id, {**_T1_COUNTS, "graph": _GRAPH_COUNTS})
            done = await client.get(f"/api/v1/connect/sync/{request_id}", headers=operator)

            assert done.status_code == 200
            body = done.json()
            assert body["status"] == "succeeded"
            assert body["error_class"] is None
            # Exact equality on BOTH levels: every T1 count survives the graph
            # payload, and every graph count is the producer's own value.
            assert body["counts"] == {**_T1_COUNTS, "graph": _GRAPH_COUNTS}

    run(_t())


def test_graph_projection_carries_only_counts_and_mode_labels_by_value(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The projected graph object holds integers plus the two mode labels — nothing else.

    Asserted over VALUES and types rather than a forbidden key-name list: a
    node key, display name, ``source_ref``, locator or extracted snippet is a
    string, and the only strings allowed to reach this wire are the two run
    modes the producer reported.
    """
    _configure_fixture_source(
        monkeypatch,
        state_path=f"{tmp_path}/state.db",
        quarantine_path=f"{tmp_path}/q.db",
    )
    h = make_app(ops_roles=("ops",))
    _seed_job(h)

    async def _t() -> None:
        async with h.client() as client:
            operator = h.bearer("user:operator", groups=["ops"])
            first = await client.post("/api/v1/connect/fixture/sync", headers=operator)
            request_id = first.json()["request_id"]
            _terminal_success(h, request_id, {**_T1_COUNTS, "graph": _GRAPH_COUNTS})

            done = await client.get(f"/api/v1/connect/sync/{request_id}", headers=operator)
            graph = done.json()["counts"]["graph"]

            assert _string_values(graph) == [_GRAPH_COUNTS["mode"]]
            assert all(
                isinstance(value, int) and not isinstance(value, bool)
                for key, value in graph.items()
                if key != "mode"
            )
            assert _string_values(done.json()["counts"]) == [
                _T1_COUNTS["mode"],
                _GRAPH_COUNTS["mode"],
            ]

    run(_t())


def test_graph_content_bearing_terminal_payloads_are_refused_not_stripped(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Graph CONTENT in the terminal payload fails the terminal; it is never quietly dropped.

    Each payload is otherwise a well-formed Graph-enabled success, so nothing
    but the content-bearing member distinguishes it from the accepted case.
    Silently discarding the member would hide a producer that started emitting
    node text, a ``source_ref``, or a locator; the response must instead be a
    refused terminal, and the sentinel must appear nowhere in it.
    """
    _configure_fixture_source(
        monkeypatch,
        state_path=f"{tmp_path}/state.db",
        quarantine_path=f"{tmp_path}/q.db",
    )
    h = make_app(ops_roles=("ops",))
    _seed_job(h)
    sentinel = "GRAPH-CONTENT-MUST-NOT-REACH-THE-WIRE"
    leaks: tuple[dict[str, Any], ...] = (
        {**_GRAPH_COUNTS, "source_ref": sentinel},
        {**_GRAPH_COUNTS, "source_system": sentinel},
        {**_GRAPH_COUNTS, "display_name": sentinel},
        {**_GRAPH_COUNTS, "locator": sentinel},
        {**_GRAPH_COUNTS, "nodes": [{"key": sentinel, "text": sentinel}]},
        {**_GRAPH_COUNTS, "edges": [{"from_key": sentinel, "to_key": sentinel}]},
    )

    async def _t() -> None:
        async with h.client() as client:
            operator = h.bearer("user:operator", groups=["ops"])
            first = await client.post("/api/v1/connect/fixture/sync", headers=operator)
            request_id = first.json()["request_id"]
            _terminal_success(h, request_id, {**_T1_COUNTS, "graph": _GRAPH_COUNTS})

            for leak in leaks:
                _rewrite_terminal_output(h, request_id, json.dumps({**_T1_COUNTS, "graph": leak}))
                refused = await client.get(f"/api/v1/connect/sync/{request_id}", headers=operator)
                assert refused.status_code == 200
                assert refused.json()["error_class"] == "InvalidTerminalResult"
                assert refused.json()["status"] == "failed"
                assert refused.json()["counts"] is None
                assert sentinel not in refused.text

    run(_t())


def test_malformed_terminal_payloads_still_raise_invalid_terminal_result(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Declaring ``graph`` widened the wire by ONE field, not into anything-goes.

    The positive control for the Graph-enabled acceptance above, and deliberately
    free of any success assertion so it stays green under a revert of that fix:
    if the fix had instead been "stop validating", every payload here would
    project as a success. Its discriminating counterpart — that these refusals
    are not simply unconditional — is
    :func:`test_graph_enabled_terminal_payload_is_projected_with_exact_graph_counts`,
    which drives the same seam to ``succeeded``.
    """
    _configure_fixture_source(
        monkeypatch,
        state_path=f"{tmp_path}/state.db",
        quarantine_path=f"{tmp_path}/q.db",
    )
    h = make_app(ops_roles=("ops",))
    _seed_job(h)
    malformed: tuple[str, ...] = (
        # Not the counts report at all.
        "not json at all",
        json.dumps([_T1_COUNTS]),
        json.dumps("backfill"),
        # A cursor token riding out on the terminal payload stays forbidden.
        json.dumps({**_T1_COUNTS, "cursors": {"deltaLink": "SECRET-DELTA-TOKEN"}}),
        # T1 counts malformed.
        json.dumps({k: v for k, v in _T1_COUNTS.items() if k != "written"}),
        json.dumps({**_T1_COUNTS, "records_seen": "three"}),
        # The graph member itself malformed.
        json.dumps({**_T1_COUNTS, "graph": {}}),
        json.dumps({**_T1_COUNTS, "graph": "backfill"}),
        json.dumps({**_T1_COUNTS, "graph": [_GRAPH_COUNTS]}),
        json.dumps(
            {
                **_T1_COUNTS,
                "graph": {k: v for k, v in _GRAPH_COUNTS.items() if k != "evidence_pending"},
            }
        ),
        json.dumps({**_T1_COUNTS, "graph": {**_GRAPH_COUNTS, "upserted": "four"}}),
    )

    async def _t() -> None:
        async with h.client() as client:
            operator = h.bearer("user:operator", groups=["ops"])
            first = await client.post("/api/v1/connect/fixture/sync", headers=operator)
            request_id = first.json()["request_id"]
            _terminal_success(h, request_id, {**_T1_COUNTS, "graph": _GRAPH_COUNTS})

            for output in malformed:
                _rewrite_terminal_output(h, request_id, output)
                refused = await client.get(f"/api/v1/connect/sync/{request_id}", headers=operator)
                assert refused.status_code == 200, output
                assert refused.json()["error_class"] == "InvalidTerminalResult", output
                assert refused.json()["status"] == "failed", output
                assert refused.json()["counts"] is None, output
                assert "SECRET-DELTA-TOKEN" not in refused.text

    run(_t())


def test_graph_counts_wire_declares_every_graph_sync_report_count_and_invents_none() -> None:
    """Bind the wire to its PRODUCER instead of asserting the mirror in prose.

    Two directions, and they are deliberately not the same claim:

    * every field the wire declares is a real ``GraphSyncReport`` field, so the
      projection cannot invent a datum the projection never reported; and
    * every INT field of the report is declared, so a count added upstream is
      surfaced rather than silently dropped by ``extra="forbid"``.

    Non-int report fields are deliberately NOT force-declared by this test: if
    the report ever grows a content-bearing member, the correct outcome is the
    fail-closed one — the payload becomes an ``InvalidTerminalResult`` until a
    human decides, rather than the member being auto-admitted to a public wire.
    """
    from ne.api.wire import ConnectSyncGraphCountsView
    from ne.brain_graph.construct.runner import GraphSyncReport

    wire_fields = set(ConnectSyncGraphCountsView.model_fields)
    report_fields = GraphSyncReport.model_fields

    assert wire_fields <= set(report_fields)
    assert {name for name, field in report_fields.items() if field.annotation is int} <= wire_fields

    report = GraphSyncReport(
        mode="delta",
        seen=9,
        upserted=4,
        unchanged=3,
        acl_healed=2,
        quarantined=1,
        unmapped=6,
        pruned=5,
        pending_edges=7,
        evidence_pending=8,
    )
    dumped = report.model_dump(mode="json")
    assert dumped == _GRAPH_COUNTS
    assert ConnectSyncGraphCountsView.model_validate(dumped).model_dump(mode="json") == dumped
