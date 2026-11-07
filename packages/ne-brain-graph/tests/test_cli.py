# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CLI verbs over fixtures + in-memory store, and the disabled-flag guard."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ne.brain_graph import cli
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.construct.ledger import GraphSyncLedger
from ne.brain_graph.construct.runner import GraphSyncRunner
from ne.brain_graph.testing.fixtures import (
    FixtureConnector,
    make_ledger_engine,
    salesforce_contract,
    salesforce_mapping,
)
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.connect.state import ConnectorStateStore


def test_disabled_guard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: BrainGraphSettings(enabled=False))
    assert cli.main(["verify"]) == 2
    assert "disabled" in capsys.readouterr().err


def test_verify_over_in_memory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = InMemoryGraphStore()

    async def seed() -> None:
        await store.upsert_nodes(
            [
                {
                    "key": "a1",
                    "entity_class": "Account",
                    "display_name": "Acme",
                    "source_system": "s",
                    "source_ref": "a1:a1",
                    "acl": ("g",),
                    "content_hash": "h",
                    "config_hash": "c",
                    "schema_version": "1.0.0",
                    "synced_at": "t",
                }
            ]
        )

    asyncio.run(seed())
    monkeypatch.setattr(cli, "load_settings", lambda: BrainGraphSettings(enabled=True))
    monkeypatch.setattr(cli, "store_for", lambda settings: store)
    assert cli.main(["verify"]) == 0
    out = capsys.readouterr().out
    assert "nodes=1" in out


def test_sync_backfill_over_fixtures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = InMemoryGraphStore()
    state = ConnectorStateStore(":memory:")

    def compose(settings: BrainGraphSettings, source: str) -> GraphSyncRunner:
        return GraphSyncRunner(
            FixtureConnector(),
            salesforce_contract(),
            store,
            GraphSyncLedger(make_ledger_engine()),
            state,
            salesforce_mapping(),
            actor="cli",
            config_hash="cfg",
            run_id="r1",
        )

    monkeypatch.setattr(cli, "load_settings", lambda: BrainGraphSettings(enabled=True))
    monkeypatch.setattr(cli, "compose_runner", compose)
    assert cli.main(["backfill", "--source", "salesforce"]) == 0
    assert store.node_count() == 4
    out = capsys.readouterr().out
    assert "upserted=4" in out
    # sync --mode incremental exercises the other verb.
    assert cli.main(["sync", "--mode", "incremental", "--source", "salesforce"]) == 0
    state.close()


def _merge_engine():
    import sqlalchemy as sa
    from sqlalchemy.pool import StaticPool

    from ne.audit import ensure_transactional_sqlite
    from ne.flywheel.review import ensure_review_schema

    eng = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    ensure_transactional_sqlite(eng)
    ensure_review_schema(eng)
    return eng


def test_er_commands_refuse_before_composing_any_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "load_settings", lambda: BrainGraphSettings(enabled=True, er_enabled=False)
    )
    monkeypatch.setattr(
        cli,
        "compose_er_job",
        lambda *_args, **_kwargs: pytest.fail("unsupported ER composed a job"),
    )
    monkeypatch.setattr(
        cli,
        "compose_merge_context",
        lambda *_args, **_kwargs: pytest.fail("unsupported ER composed a store"),
    )
    assert cli.main(["er-run"]) == 3
    assert cli.main(["apply-merge", "sha256:x"]) == 3
    assert cli.main(["unmerge", "edge", "--node", "node", "--confirm"]) == 3
    assert "unsupported" in capsys.readouterr().err


def test_rebuild_lightrag_refuses_when_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "load_settings", lambda: BrainGraphSettings(enabled=True, lightrag_enabled=False)
    )
    assert cli.main(["rebuild-lightrag"]) == 2
    assert "LightRAG is disabled" in capsys.readouterr().err


def test_apply_merge_non_approved_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
    from ne.flywheel.lanes import FixLane, ProposalPriority
    from ne.flywheel.proposals import FixProposal, ProposalKind, SqlAlchemyProposalStore

    reset_config_hash_for_testing()
    freeze_config_hash(BrainGraphSettings())
    try:
        engine = _merge_engine()
        proposal = FixProposal(
            proposal_id="sha256:er-x",
            kind=ProposalKind.ENTITY_MERGE_CANDIDATE,
            lane=FixLane.ENTITY_RESOLUTION,
            cluster_id="er:Person:z",
            evidence_event_ids=(),
            evidence_signal_ids=(),
            summary="candidate p1==p2",
            confidence=0.99,
            priority=ProposalPriority.HIGH,
            config_hash="cfg",
            created_at=datetime(2026, 7, 9, tzinfo=UTC),
            payload={
                "left": {"entity_class": "Person", "entity_key": "p1", "display": "A"},
                "right": {"entity_class": "Person", "entity_key": "p2", "display": "A"},
            },
        )
        with engine.begin() as conn:
            SqlAlchemyProposalStore(engine).append(conn, proposal)
        from ne.flywheel.review import ProposalReviewService

        store = InMemoryGraphStore()  # PROPOSED (never approved) -> refuse
        review = ProposalReviewService(engine)
        monkeypatch.setattr(
            cli, "load_settings", lambda: BrainGraphSettings(enabled=True, er_enabled=True)
        )
        monkeypatch.setattr(
            cli, "compose_merge_context", lambda settings: (engine, review, store, None)
        )
        assert cli.main(["apply-merge", "sha256:er-x"]) == 3
    finally:
        reset_config_hash_for_testing()


# -- NE-T2X-2: extraction CLI verbs ------------------------------------------


def test_extraction_run_refuses_when_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "load_settings", lambda: BrainGraphSettings(enabled=True, extraction_enabled=False)
    )
    assert cli.main(["extraction", "run"]) == 2
    assert "LLM extraction is disabled" in capsys.readouterr().err


def test_extraction_run_over_scripted_composition(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Report:
        run_id = "r1"
        chunks_scanned = 3
        batches_built = 1
        proposals_appended = 1
        mentions = 2
        discarded_below_threshold = 0
        quarantined_invalid = 0

    class _StubJob:
        async def run(self, *, source: str | None = None) -> _Report:
            return _Report()

    monkeypatch.setattr(
        cli, "load_settings", lambda: BrainGraphSettings(enabled=True, extraction_enabled=True)
    )
    monkeypatch.setattr(cli, "compose_extraction_job", lambda settings: _StubJob())
    assert cli.main(["extraction", "run"]) == 0
    out = capsys.readouterr().out
    assert "batches_built=1" in out
    assert "proposals_appended=1" in out
    assert "chunks_scanned=3" in out


def test_extraction_run_refuses_for_unimplemented_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex C1, CLI-side UX: the job's ``ExtractionModeNotImplementedError``
    (the source of truth) surfaces as a clean stderr message + exit code,
    the same shape as ``apply-merge``'s ``MergeGateError`` handling — not an
    uncaught traceback."""
    from ne.brain_graph.extraction.job import ExtractionModeNotImplementedError

    class _StubJob:
        async def run(self, *, source: str | None = None) -> Any:
            raise ExtractionModeNotImplementedError(
                "ExtractionJob.run refused: extraction_mode='read_flagged' is not "
                "implemented by this unit"
            )

    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: BrainGraphSettings(
            enabled=True, extraction_enabled=True, extraction_mode="read_flagged"
        ),
    )
    monkeypatch.setattr(cli, "compose_extraction_job", lambda settings: _StubJob())
    assert cli.main(["extraction", "run"]) == 3
    err = capsys.readouterr().err
    assert "extraction-run refused" in err
    assert "read_flagged" in err


def test_extraction_purge_requires_confirm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "load_settings", lambda: BrainGraphSettings(enabled=True, extraction_enabled=True)
    )
    assert cli.main(["extraction", "purge"]) == 2
    err = capsys.readouterr().err
    assert "--confirm" in err
    assert "SAME_AS" in err  # P3-2: purge DETACH collateral disclosure (test-locked)


def test_extraction_purge_deletes_ne_llm_and_audits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    from ne.contracts import freeze_config_hash, reset_config_hash_for_testing

    reset_config_hash_for_testing()
    freeze_config_hash(BrainGraphSettings())
    try:
        store = InMemoryGraphStore()

        async def seed() -> None:
            await store.upsert_nodes(
                [
                    {
                        "key": "backbone1",
                        "entity_class": "Account",
                        "display_name": "Real",
                        "source_system": "salesforce",
                        "source_ref": "a:a",
                        "acl": ("g",),
                        "content_hash": "h",
                        "config_hash": "c",
                        "schema_version": "1.0.0",
                        "synced_at": "t",
                    },
                    {
                        "key": "llm1",
                        "entity_class": "Account",
                        "display_name": "Extracted",
                        "source_system": "ne:llm",
                        "source_ref": "llm:aaaa:c1",
                        "acl": ("g",),
                        "assurance": "extracted",
                        "evidence": ["p1|c1"],
                        "content_hash": "h",
                        "config_hash": "c",
                        "schema_version": "1.0.0",
                        "synced_at": "t",
                    },
                ]
            )

        asyncio.run(seed())

        class _RecordingAudit:
            def __init__(self) -> None:
                self.events: list[Any] = []

            async def append(self, event: Any) -> None:
                self.events.append(event)

            async def verify(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
                raise NotImplementedError

        audit = _RecordingAudit()
        monkeypatch.setattr(
            cli, "load_settings", lambda: BrainGraphSettings(enabled=True, extraction_enabled=True)
        )
        monkeypatch.setattr(
            cli, "compose_extraction_purge_context", lambda settings: (store, audit)
        )
        assert cli.main(["extraction", "purge", "--confirm"]) == 0
        assert store.raw_node("backbone1") is not None
        assert store.raw_node("llm1") is None
        out = capsys.readouterr().out
        assert "nodes=1" in out
        actions = [getattr(e, "action", "") for e in audit.events]
        assert "graph.extraction_purge" in actions
    finally:
        reset_config_hash_for_testing()
