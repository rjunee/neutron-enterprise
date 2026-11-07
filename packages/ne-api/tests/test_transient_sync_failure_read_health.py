# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""A transient connector-sync error must not black out a readable corpus.

Live incident this file reproduces (2026-08-28, `ne_audit_log` on the demo box):
the m365-sharepoint lane recorded `success` at 02:18:30 and 02:20:07, ONE
`error` at 02:22:22 (`error_class=GraphTransportError` — a TCP connection reset
mid `/content` fetch against real Microsoft Graph), then `success` again at
02:25:56. Qdrant held all 68 m365 chunks throughout; nothing about the DATA
became unavailable. But `degraded_sync_sources` keyed on "the LATEST sync fact
is non-success", so for 3.5 minutes every m365 chunk was withheld: one S1 run
answered "the governed eligibility criteria were not retrievable in this run
due to a source outage", and two earlier runs died of AgenticNoProgressError
re-querying a corpus that was 100% withheld.

The policy under test (ne.api.connect_status.sync_source_read_health) splits a
failing lane's read handling in two:

* UNAVAILABLE (withheld, exactly as before): the latest fact is ``denied``
  (AclResolutionError — the ACL mirror could not be resolved), or ANY ``denied``
  occurred since the last success, or the lane has NEVER synced successfully,
  or the failures have persisted (``TRANSIENT_SYNC_FAILURE_LIMIT`` consecutive
  non-success facts since the last success — at that point the ACL mirror
  itself is unverifiably stale and fail-closed is honest again).
* STALE (served): a short run of plain ``error`` facts after a success. That is
  a freshness risk, not an availability fact — the corpus on disk is the one
  the last VERIFIED sync admitted — so the read path serves it and the render
  layer states the staleness instead of silently withholding.

The guardian tests below pin the boundary the fix must NOT move: ``denied``
still withholds immediately, persistence still withholds, and a lane with no
verified sync ever still withholds.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from ne.audit.store import ensure_audit_schema
from ne.contracts.types import Chunk
from ne.events import new_audit_event
from ne.schema import AuditOutcome

from .conftest import AppBuilder, get_state, run

_FIXTURE = "connector://fixture"


def _configure_fixture_source(
    monkeypatch: pytest.MonkeyPatch, *, state_path: str, quarantine_path: str
) -> None:
    """Mirror of test_connect_status_api's env setup for one 'fixture' source."""
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["fixture"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", state_path)
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", quarantine_path)
    monkeypatch.setenv("NE_CONNECT_FIXTURE_PATH", f"{state_path}.fixture.json")
    monkeypatch.setenv("NE_CONNECT_FIXTURE_OWNER", "transient-health-test")


def _knowledge_chunk(chunk_id: str, connector: str | None) -> Chunk:
    metadata: dict[str, str] = {
        "source_system": "fixture",
        "source_ref": f"ref-{chunk_id}",
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


def _app(make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object):
    _configure_fixture_source(
        monkeypatch, state_path=f"{tmp_path}/state.db", quarantine_path=f"{tmp_path}/q.db"
    )
    return make_app(
        chunks=[
            _knowledge_chunk("fixture-result", "fixture"),
            _knowledge_chunk("healthy-sibling", "salesforce"),
            _knowledge_chunk("curated", None),
        ]
    )


def _seed(h: object, *outcomes: AuditOutcome) -> None:
    state = get_state(h.app)  # type: ignore[attr-defined]

    async def _append() -> None:
        for outcome in outcomes:
            await state.sink.append(
                new_audit_event(
                    action="connector.sync",
                    resource=_FIXTURE,
                    outcome=outcome,
                    actor="t",
                )
            )

    run(_append())


def _search_ids(h: object) -> set[str]:
    ids: set[str] = set()

    async def _t() -> None:
        async with h.client() as client:  # type: ignore[attr-defined]
            r = await client.post(
                "/api/v1/brain/search",
                json={"query": "q", "top_k": 10},
                headers=h.bearer("u", groups=["eng"]),  # type: ignore[attr-defined]
            )
            assert r.status_code == 200, r.text
            ids.update(row["chunk"]["chunk_id"] for row in r.json()["chunks"])

    run(_t())
    return ids


_ALL = {"fixture-result", "healthy-sibling", "curated"}
_WITHOUT_FIXTURE = {"healthy-sibling", "curated"}


# --- the incident: one transient error between successes ----------------------


def test_single_transient_sync_error_does_not_black_out_the_corpus(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """The 02:22:22 shape: success, success, ONE error. The corpus stays readable."""
    h = _app(make_app, monkeypatch, tmp_path)
    _seed(h, AuditOutcome.success, AuditOutcome.success, AuditOutcome.error)
    assert _search_ids(h) == _ALL


def test_two_consecutive_errors_still_serve_the_corpus(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    h = _app(make_app, monkeypatch, tmp_path)
    _seed(h, AuditOutcome.success, AuditOutcome.error, AuditOutcome.error)
    assert _search_ids(h) == _ALL


# --- the guardians: every case where withholding is CORRECT -------------------


def test_persistent_sync_failure_still_withholds(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """At TRANSIENT_SYNC_FAILURE_LIMIT consecutive errors the mirror is unverifiable."""
    h = _app(make_app, monkeypatch, tmp_path)
    _seed(
        h,
        AuditOutcome.success,
        AuditOutcome.error,
        AuditOutcome.error,
        AuditOutcome.error,
    )
    assert _search_ids(h) == _WITHOUT_FIXTURE


def test_denied_still_withholds_immediately(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """A genuine ACL/authority failure withholds on the FIRST fact, no grace."""
    h = _app(make_app, monkeypatch, tmp_path)
    _seed(h, AuditOutcome.success, AuditOutcome.denied)
    assert _search_ids(h) == _WITHOUT_FIXTURE


def test_denied_followed_by_transient_error_still_withholds(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """A later plain error must not launder an unresolved denied into 'stale'."""
    h = _app(make_app, monkeypatch, tmp_path)
    _seed(h, AuditOutcome.success, AuditOutcome.denied, AuditOutcome.error)
    assert _search_ids(h) == _WITHOUT_FIXTURE


def test_never_succeeded_failing_lane_is_withheld(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """No verified sync has EVER admitted this corpus; there is no mirror to trust."""
    h = _app(make_app, monkeypatch, tmp_path)
    _seed(h, AuditOutcome.error)
    assert _search_ids(h) == _WITHOUT_FIXTURE


def test_recovery_success_restores_full_service(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    h = _app(make_app, monkeypatch, tmp_path)
    _seed(
        h,
        AuditOutcome.success,
        AuditOutcome.error,
        AuditOutcome.error,
        AuditOutcome.error,
        AuditOutcome.success,
    )
    assert _search_ids(h) == _ALL


# --- the derivation itself ----------------------------------------------------


def _audit_engine() -> sa.Engine:
    # new_audit_event stamps the process config hash; the autouse conftest
    # fixture resets it around every test, so freeze a deterministic one here.
    from ne.contracts import freeze_config_hash
    from ne.contracts.config import NeSettings

    class _UnitSettings(NeSettings):
        unit: int = 1

    freeze_config_hash(_UnitSettings())
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    ensure_audit_schema(engine)
    return engine


def _append_sync(engine: sa.Engine, *outcomes: AuditOutcome) -> None:
    from ne.audit.store import append_record

    with engine.begin() as conn:
        for outcome in outcomes:
            append_record(
                conn,
                new_audit_event(
                    action="connector.sync",
                    resource=_FIXTURE,
                    outcome=outcome,
                    actor="t",
                ),
            )


def test_read_health_classifies_short_error_run_as_stale() -> None:
    from ne.api.connect_status import sync_source_read_health

    engine = _audit_engine()
    _append_sync(engine, AuditOutcome.success, AuditOutcome.error)
    health = sync_source_read_health(engine, [_FIXTURE])
    assert health.unavailable == frozenset()
    assert health.stale == frozenset({"fixture"})


def test_read_health_never_synced_lane_is_neither_stale_nor_unavailable() -> None:
    from ne.api.connect_status import sync_source_read_health

    health = sync_source_read_health(_audit_engine(), [_FIXTURE])
    assert health.unavailable == frozenset()
    assert health.stale == frozenset()


def test_read_health_latest_success_is_healthy() -> None:
    from ne.api.connect_status import sync_source_read_health

    engine = _audit_engine()
    _append_sync(engine, AuditOutcome.error, AuditOutcome.error, AuditOutcome.success)
    health = sync_source_read_health(engine, [_FIXTURE])
    assert health.unavailable == frozenset()
    assert health.stale == frozenset()


def test_read_health_denied_since_success_is_unavailable_not_stale() -> None:
    from ne.api.connect_status import sync_source_read_health

    engine = _audit_engine()
    _append_sync(engine, AuditOutcome.success, AuditOutcome.denied, AuditOutcome.error)
    health = sync_source_read_health(engine, [_FIXTURE])
    assert health.unavailable == frozenset({"fixture"})
    assert health.stale == frozenset()


def test_read_health_limit_boundary() -> None:
    from ne.api.connect_status import TRANSIENT_SYNC_FAILURE_LIMIT, sync_source_read_health

    engine = _audit_engine()
    _append_sync(engine, AuditOutcome.success)
    _append_sync(engine, *([AuditOutcome.error] * (TRANSIENT_SYNC_FAILURE_LIMIT - 1)))
    below = sync_source_read_health(engine, [_FIXTURE])
    assert below.unavailable == frozenset()
    assert below.stale == frozenset({"fixture"})
    _append_sync(engine, AuditOutcome.error)
    at_limit = sync_source_read_health(engine, [_FIXTURE])
    assert at_limit.unavailable == frozenset({"fixture"})
    assert at_limit.stale == frozenset()


def test_degraded_sync_sources_is_the_unavailable_set() -> None:
    from ne.api.connect_status import degraded_sync_sources

    engine = _audit_engine()
    _append_sync(engine, AuditOutcome.success, AuditOutcome.error)
    assert degraded_sync_sources(engine, [_FIXTURE]) == frozenset()
    _append_sync(engine, AuditOutcome.denied)
    assert degraded_sync_sources(engine, [_FIXTURE]) == frozenset({"fixture"})


# --- the rendered caveat ------------------------------------------------------


def test_stale_note_renders_and_survives_the_workflow_parser() -> None:
    """The served-stale caveat must not break parse_retrieved_text's envelope.

    The header regex is strict and the body parser treats any line containing
    ``": "`` as a chunk line, so the note must parse as NEITHER — it may not
    shift the counts and may not be ingested as chunk text.
    """
    from ne.api.tools import _result_header, _stale_note
    from ne.api.workflows import parse_retrieved_text
    from ne.brain.withholding import SourceHealthWithholding

    withholding = SourceHealthWithholding()
    withholding.record(candidates=3, withheld=0)
    withholding.record_stale_served(2)
    header = _result_header(shown=3, total=3, withholding=withholding)
    note = _stale_note(withholding)
    assert note is not None
    assert ": " not in note
    rendered = "\n".join([header, "c1: alpha", "c2: beta", "c3: gamma", note])
    parsed = parse_retrieved_text(rendered)
    assert parsed.shown == 3
    assert parsed.withheld == 0
    assert parsed.texts == ("alpha", "beta", "gamma")


def test_no_stale_note_when_nothing_served_is_stale() -> None:
    from ne.api.tools import _stale_note
    from ne.brain.withholding import SourceHealthWithholding

    assert _stale_note(SourceHealthWithholding()) is None
