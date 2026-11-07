# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-INGEST (P1-4) — the connector-sync registry + ConnectSyncSettings contract.

Registry completeness (8 pull sources, cdc excluded), lazy resolution to
(NeSettings subclass, coroutine fn), tier-1 classification of every sync tool name,
the fail-loud ConnectSyncSettings validators, the error paths of run_configured_sync,
and the import-cheapness guardian (importing ne.connect.runner drags neither dlt nor
the sql connector).
"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from ne.connect.bridge import SyncRunner, default_record_mapper
from ne.connect.contracts import OdcsDataContract, PermissionMode, PermissionModelSpec, PiiClass
from ne.connect.identity import (
    SOURCE_ACL_METADATA_KEY,
    SOURCE_SNAPSHOT_METADATA_KEY,
    source_acl_hash,
    source_snapshot_hash,
)
from ne.connect.runner import (
    SYNC_SOURCES,
    ConnectSyncSettings,
    SourceDisposition,
    SyncNotConfiguredError,
    SyncSourceEntry,
    build_ingest_stack,
    build_sync_settings,
    configured_source_name,
    read_only_source_name,
    run_configured_sync,
    sync_tool_name,
)
from ne.contracts.config import NeSettings
from ne.contracts.integration import Record
from ne.contracts.source_authority import (
    NO_SOURCE_STATUS,
    SOURCE_AUTHORITY_STAMP_METADATA_KEY,
    SOURCE_STATUS_METADATA_KEY,
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
    stamp_from_receipt,
)
from ne.contracts.types import Chunk

_EXPECTED_SOURCES = {
    "m365",
    "teams",
    "slack",
    "jira",
    "zendesk",
    "salesforce",
    "sql",
    "fixture",
    "csv",
}


def _identity_contract() -> OdcsDataContract:
    return OdcsDataContract(
        source="m365-sharepoint",
        owner="data@example.test",
        schema_def={"type": "object"},
        freshness_sla="PT1H",
        pii=PiiClass.PERSONAL,
        permissions=PermissionModelSpec(mode=PermissionMode.SOURCE_ACL),
    )


def test_source_snapshot_identity_is_exact_stable_and_stamped() -> None:
    first = Record(
        source_id="drive-a:item-1",
        record_id="item-1",
        payload={"name": "Procedure.docx", "size": 12},
    )
    reordered = Record(
        source_id="drive-a:item-1",
        record_id="item-1",
        payload={"size": 12, "name": "Procedure.docx"},
    )
    changed = first.model_copy(update={"payload": {"name": "Procedure.docx", "size": 13}})
    other_identity = first.model_copy(update={"record_id": "item-2"})

    digest = source_snapshot_hash("m365-sharepoint", first)
    assert digest == source_snapshot_hash("m365-sharepoint", reordered)
    assert digest != source_snapshot_hash("m365-sharepoint", changed)
    assert digest != source_snapshot_hash("m365-sharepoint", other_identity)

    item = default_record_mapper(first, _identity_contract(), ("ops",))
    assert item.metadata["source_ref"] == "drive-a:item-1:item-1"
    assert item.metadata[SOURCE_SNAPSHOT_METADATA_KEY] == digest
    assert item.metadata[SOURCE_ACL_METADATA_KEY] == source_acl_hash(("ops",))
    assert source_acl_hash(("ops", "finance")) == source_acl_hash(("finance", "ops"))
    assert source_acl_hash(("ops",)) != source_acl_hash(("finance",))
    assert "Procedure.docx" not in digest


def test_registry_has_exactly_the_nine_pull_sources_cdc_excluded() -> None:
    assert set(SYNC_SOURCES) == _EXPECTED_SOURCES
    assert "cdc" not in SYNC_SOURCES  # push-based webhook sink, not a pull sync


def test_every_entry_resolves_to_settings_class_and_coroutine() -> None:
    import importlib

    for source, entry in SYNC_SOURCES.items():
        smod, _, sattr = entry.settings_ref.partition(":")
        settings_cls = getattr(importlib.import_module(smod), sattr)
        assert issubclass(settings_cls, NeSettings), source
        rmod, _, rattr = entry.run_ref.partition(":")
        run_fn = getattr(importlib.import_module(rmod), rattr)
        assert inspect.iscoroutinefunction(run_fn), source


def test_every_sync_tool_name_classifies_tier_1() -> None:
    # ne-runtime is import-only test code here (the workspace installs every package).
    from ne.contracts.types import ApprovalTier
    from ne.runtime.actions import tier_for_action

    for source in SYNC_SOURCES:
        assert tier_for_action(sync_tool_name(source)) == ApprovalTier(1)


def test_settings_reject_unknown_source() -> None:
    with pytest.raises(ValueError, match="unknown connector source"):
        ConnectSyncSettings(sources=("nosuch",))


def test_settings_reject_duplicate_source() -> None:
    with pytest.raises(ValueError, match="duplicate connector source"):
        ConnectSyncSettings(sources=("fixture", "fixture"), state_path="s", quarantine_path="q")


def test_settings_reject_orphan_cron_override() -> None:
    with pytest.raises(ValueError, match="cron_overrides names non-runnable"):
        ConnectSyncSettings(
            sources=("fixture",),
            state_path="s",
            quarantine_path="q",
            cron_overrides={"m365": "*/5 * * * *"},
        )


def test_runnable_sources_default_to_all_configured_sources() -> None:
    settings = ConnectSyncSettings(sources=("m365", "csv"), state_path="s", quarantine_path="q")

    assert settings.sources_to_run == ("m365", "csv")


def test_runnable_sources_are_an_exact_configured_subset_in_source_order() -> None:
    settings = ConnectSyncSettings(
        sources=("m365", "csv"),
        runnable_sources=("m365", "csv"),
        state_path="s",
        quarantine_path="q",
    )

    assert settings.sources == ("m365", "csv")
    assert settings.sources_to_run == ("m365", "csv")
    with pytest.raises(ValueError, match="configured source order"):
        ConnectSyncSettings(
            sources=("m365", "csv"),
            runnable_sources=("csv", "m365"),
            state_path="s",
            quarantine_path="q",
        )
    with pytest.raises(ValueError, match="non-configured source"):
        ConnectSyncSettings(
            sources=("m365",),
            runnable_sources=("csv",),
            state_path="s",
            quarantine_path="q",
        )
    read_only = ConnectSyncSettings(
        sources=("m365",),
        runnable_sources=(),
        state_path="s",
        quarantine_path="q",
    )
    assert read_only.sources_to_run == ()
    with pytest.raises(ValueError, match="duplicate runnable"):
        ConnectSyncSettings(
            sources=("m365", "csv"),
            runnable_sources=("csv", "csv"),
            state_path="s",
            quarantine_path="q",
        )


def test_cron_override_cannot_target_configured_but_unscheduled_source() -> None:
    with pytest.raises(ValueError, match="non-runnable source"):
        ConnectSyncSettings(
            sources=("m365", "csv"),
            runnable_sources=("csv",),
            cron_overrides={"m365": "*/2 * * * *"},
            state_path="s",
            quarantine_path="q",
        )


def test_settings_require_state_and_quarantine_when_sources_set() -> None:
    with pytest.raises(ValueError, match="REQUIRED when NE_CONNECT_SYNC_SOURCES"):
        ConnectSyncSettings(sources=("fixture",))


def test_cron_for_resolves_override_then_default() -> None:
    cs = ConnectSyncSettings(
        sources=("fixture",),
        state_path="s",
        quarantine_path="q",
        cron="0 * * * *",
        cron_overrides={"fixture": "*/5 * * * *"},
    )
    assert cs.cron_for("fixture") == "*/5 * * * *"
    assert cs.cron_for("m365") == "0 * * * *"  # no override → default


def test_run_configured_sync_unknown_source_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown connector source"):
        asyncio.run(
            run_configured_sync(
                "nosuch",
                writer=cast(Any, None),
                state_store=cast(Any, None),
                audit_sink=None,
                actor="test",
            )
        )


def test_configured_source_name_distinguishes_registry_alias_from_runtime_identity() -> None:
    settings = type("Settings", (), {"source_name": "m365-sharepoint"})()
    assert configured_source_name("m365", settings=settings) == "m365-sharepoint"


def test_configured_source_name_preserves_salesforce_identity() -> None:
    settings = type("Settings", (), {"source_name": "salesforce"})()
    assert configured_source_name("salesforce", settings=settings) == "salesforce"


def test_configured_source_name_falls_back_to_registry_alias() -> None:
    class _SettingsWithoutSourceName:
        pass

    assert configured_source_name("fixture", settings=_SettingsWithoutSourceName()) == "fixture"


def test_read_only_source_name_does_not_require_unconfigured_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("NE_CONNECT_FIXTURE_PATH", "NE_CONNECT_FIXTURE_OWNER"):
        monkeypatch.delenv(var, raising=False)

    assert read_only_source_name("fixture") == "fixture"
    with pytest.raises(SyncNotConfiguredError, match="fixture"):
        configured_source_name("fixture")


def test_build_sync_settings_unconfigured_fixture_raises_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("NE_CONNECT_FIXTURE_PATH", "NE_CONNECT_FIXTURE_OWNER"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SyncNotConfiguredError, match="fixture"):
        build_sync_settings("fixture")


def test_importing_runner_drags_neither_dlt_nor_sql_connector() -> None:
    # The dlt-confinement guardian (mirrors test_dlt_confinement.py). ne.connect's
    # __init__ eagerly re-exports the dlt-FREE m365/slack connectors, so those load;
    # the meaningful invariant is that neither dlt NOR the sql connector (the sole
    # dlt-dragging pull source) is imported by ne.connect.runner.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ne.connect.runner, sys; "
            "assert 'dlt' not in sys.modules, 'dlt leaked'; "
            "assert 'ne.connect.sql' not in sys.modules, 'sql connector leaked'",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_ingest_stack_binds_lineage_to_the_shared_runtime_engine(tmp_path: Path) -> None:
    """Polling/CLI SharePoint sync cannot silently build an untracked writer."""
    import sqlalchemy as sa

    settings = ConnectSyncSettings(
        sources=("fixture",),
        state_path=str(tmp_path / "state.db"),
        quarantine_path=str(tmp_path / "quarantine.db"),
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path}/runtime.db")
    stack = build_ingest_stack(
        chunk_store=cast(Any, object()),
        runtime_engine=engine,
        sink=cast(Any, object()),
        settings=settings,
    )
    lineage = cast(Any, stack.writer)._lineage
    assert lineage is not None
    assert lineage.engine is engine


# -- 015A0-AUTHORITY-M365: the pull-path authority chokepoint -----------------


class _StubConnector:
    """A minimal Connector: fixed records and one ACL entry per record."""

    def __init__(self, records: list[Record], *, principals: tuple[str, ...] = ("u-ada",)) -> None:
        self._records = records
        self._principals = principals
        self.permission_calls = 0

    async def discover(self) -> list[Any]:
        return []

    def backfill(self, state: Any = None) -> Any:
        return self._stream()

    def incremental(self, state: Any) -> Any:
        return self._stream()

    def permissions(self) -> Any:
        return self._acl()

    async def _stream(self) -> Any:
        for record in self._records:
            yield record

    async def _acl(self) -> Any:
        from ne.contracts.integration import AclEntry

        self.permission_calls += 1
        for record in self._records:
            yield AclEntry(resource_id=record.source_id, principals=self._principals)


class _StubWriter:
    """A KnowledgeWrite that accepts everything and records call ORDER."""

    def __init__(self, trace: list[str], *, reject: set[str] | None = None) -> None:
        self._trace = trace
        self._reject = reject or set()
        self.curated: list[tuple[str, dict[str, Any], str]] = []

    async def upsert(self, items: Any) -> Any:
        from ne.contracts.knowledge import UpsertReceipt

        accepted = [item.item_id for item in items if item.item_id not in self._reject]
        quarantined = [item.item_id for item in items if item.item_id in self._reject]
        for item_id in accepted:
            self._trace.append(f"write:{item_id}")
        return UpsertReceipt(accepted=tuple(accepted), quarantined=tuple(quarantined))

    async def curate(self, item_id: str, patch: Any, *, provenance: str) -> Any:
        self._trace.append(f"curate:{item_id}")
        self.curated.append((item_id, dict(patch.get("metadata", {})), provenance))


def _authority_receipt(
    record_id: str, *, receipt_id: str = "receipt-1", present: bool = True
) -> CurrentSourceAuthorityReceipt:
    """A real receipt of the shape ``M365AuthorityAdmission.finalize`` returns."""
    identity = SourceIdentity(
        deployment_id="dep-1",
        source_kind="m365-sharepoint",
        connector_instance_id="inst-1",
        point=M365SourcePoint(
            graph_tenant_id="tenant-1", site_id="site-1", drive_id="drive-1", item_id=record_id
        ),
        generation=1,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint", descriptor_schema_version=1, descriptor_hash="1" * 64
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=identity,
        source_snapshot_hash="2" * 64,
        content_snapshot_hash="3" * 64,
        acl_snapshot_hash="4" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return CurrentSourceAuthorityReceipt(
        receipt_id=receipt_id,
        source_identity=identity,
        artifact_identity=artifact if present else None,
        source_revision="rev-1",
        source_contract=contract,
        contract_epoch=1,
        acl_snapshot_hash="4" * 64,
        state=SourceAuthorityState.PRESENT if present else SourceAuthorityState.DELETED,
        admitted_at=now,
        authority_as_of=now,
        audit_event_id="event-1",
        lineage_id="lineage-1",
        freshness_sla_seconds=Decimal("3600"),
    )


class _RecordingAuthority:
    """A ``PullAuthorityAdmission`` that records what the bridge asked it to do."""

    def __init__(self, trace: list[str], *, present: bool = True) -> None:
        self._trace = trace
        self._present = present
        self.prepared: list[tuple[str, tuple[str, ...], bytes]] = []
        self.finalized: list[str] = []
        self.receipts: dict[str, CurrentSourceAuthorityReceipt] = {}
        self.receipt_id = "receipt-1"

    async def prepare(
        self,
        record: Record,
        *,
        acl: tuple[str, ...],
        canonical_bytes: bytes,
        item_id: str,
        evidence: Any,
    ) -> Any:
        assert evidence.matches(record.source_id)
        self._trace.append(f"artifact:{item_id}")
        self.prepared.append((item_id, acl, canonical_bytes))
        return cast(Any, _Prepared(item_id=item_id))

    async def finalize(self, preparation: Any) -> Any:
        self._trace.append(f"authority:{preparation.item_id}")
        self.finalized.append(preparation.item_id)
        receipt = _authority_receipt(
            preparation.item_id, receipt_id=self.receipt_id, present=self._present
        )
        self.receipts[preparation.item_id] = receipt
        return receipt


class _Prepared:
    def __init__(self, item_id: str) -> None:
        self.item_id = item_id


def _authority_records() -> list[Record]:
    return [
        Record(record_id="item-a", source_id="drive-1:item-a", payload={"content": "a"}),
        Record(record_id="item-b", source_id="drive-1:item-b", payload={"content": "b"}),
    ]


def _evidence_for(records: list[Record], *, principals: tuple[str, ...] = ("u-ada",)) -> dict:
    """A completed permission read per record — what the connector would produce."""
    from ne.connect.authority_admission import observed_permissions

    evidence = {}
    for record in records:
        drive_id, _, item_id = record.source_id.partition(":")
        evidence[record.source_id] = observed_permissions(
            drive_id=drive_id,
            item_id=item_id,
            principals=principals,
            pages_read=1,
            collection_complete=True,
        )
    return evidence


def _run_bridge(
    records: list[Record],
    *,
    authority: Any | None,
    state_store: Any,
    trace: list[str] | None = None,
    reject: set[str] | None = None,
    evidence: dict | None = None,
) -> Any:
    from ne.connect.bridge import SyncRunner

    runner = SyncRunner(
        cast(Any, _StubConnector(records)),
        _identity_contract(),
        cast(Any, _StubWriter(trace if trace is not None else [], reject=reject)),
        state_store,
        actor="test",
        authority=authority,
        permission_evidence=_evidence_for(records) if evidence is None else evidence,
    )
    return asyncio.run(runner.run(mode="backfill"))


def test_the_bridge_admits_authority_only_after_the_governed_write_succeeds() -> None:
    from ne.connect.state import ConnectorStateStore

    trace: list[str] = []
    authority = _RecordingAuthority(trace)
    report = _run_bridge(
        _authority_records(),
        authority=cast(Any, authority),
        state_store=ConnectorStateStore(),
        trace=trace,
    )
    assert report.written == 2
    assert authority.finalized == [_item_id_for("item-a"), _item_id_for("item-b")]
    for item_id in (_item_id_for("item-a"), _item_id_for("item-b")):
        assert trace.index(f"artifact:{item_id}") < trace.index(f"write:{item_id}")
        assert trace.index(f"write:{item_id}") < trace.index(f"authority:{item_id}")


def test_an_item_the_writer_refused_never_advances_authority() -> None:
    from ne.connect.state import ConnectorStateStore

    trace: list[str] = []
    authority = _RecordingAuthority(trace)
    item_id = _item_id_for("item-b")
    _run_bridge(
        _authority_records(),
        authority=cast(Any, authority),
        state_store=ConnectorStateStore(),
        trace=trace,
        reject={item_id},
    )
    assert [prepared[0] for prepared in authority.prepared] == [
        _item_id_for("item-a"),
        item_id,
    ]
    assert authority.finalized == [_item_id_for("item-a")]


def _item_id_for(record_id: str) -> str:
    from ne.connect.bridge import canonical_item_id

    return canonical_item_id("m365-sharepoint", f"drive-1:{record_id}", record_id)


def test_a_quiet_unchanged_record_is_re_admitted_with_its_resolved_acl() -> None:
    """A dedup-skipped record must still prove its ACL and re-admit its artifact.

    Without this the only thing that could refresh a quiet item's as-of would be
    the passage of time — exactly the false-fresh receipt the sweep exists to
    prevent.
    """
    from ne.connect.state import ConnectorStateStore

    store = ConnectorStateStore()
    first = _RecordingAuthority([])
    _run_bridge(_authority_records(), authority=cast(Any, first), state_store=store)

    second = _RecordingAuthority([])
    report = _run_bridge(_authority_records(), authority=cast(Any, second), state_store=store)
    assert report.skipped_unchanged == 2
    assert report.written == 0
    assert second.finalized == [_item_id_for("item-a"), _item_id_for("item-b")]
    assert [prepared[1] for prepared in second.prepared] == [("u-ada",), ("u-ada",)]
    assert [prepared[2] for prepared in second.prepared] == [
        prepared[2] for prepared in first.prepared
    ]


def test_without_the_authority_seam_a_dedup_skipped_record_resolves_no_acl() -> None:
    """The control: a sibling connector keeps its existing skip-before-resolve order.

    The reorder above is scoped to the authority path, so this unit changes no
    other connector's abort semantics. An unresolvable ACL on an unchanged record
    would raise here if the reorder had leaked.
    """
    from ne.connect.bridge import SyncRunner
    from ne.connect.state import ConnectorStateStore

    store = ConnectorStateStore()
    records = _authority_records()
    _run_bridge(records, authority=None, state_store=store)

    connector = _StubConnector(records, principals=())
    runner = SyncRunner(
        cast(Any, connector),
        _identity_contract(),
        cast(Any, _StubWriter([])),
        store,
        actor="test",
    )
    report = asyncio.run(runner.run(mode="backfill"))
    assert report.skipped_unchanged == 2
    assert report.written == 0


def test_only_the_m365_registry_entry_carries_authority_capability() -> None:
    authority_bearing = {name for name, entry in SYNC_SOURCES.items() if entry.authority_bearing}
    assert authority_bearing == {"m365", "salesforce"}


def test_run_configured_sync_passes_authority_only_to_an_authority_bearing_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ne.connect.authority_admission import M365AuthorityAdmission
    from ne.connect.salesforce.authority import SalesforceAuthorityFactory

    seen: dict[str, dict[str, Any]] = {}

    class _Outcome:
        report = "report-sentinel"

    async def _fake_run(settings: Any, **kwargs: Any) -> Any:
        seen[str(settings)] = kwargs
        return _Outcome()

    monkeypatch.setattr("ne.connect.runner._load_ref", lambda dotted: _fake_run)
    m365 = object.__new__(M365AuthorityAdmission)
    salesforce = object.__new__(SalesforceAuthorityFactory)
    for source, capability in (("m365", m365), ("salesforce", salesforce), ("csv", None)):
        asyncio.run(
            run_configured_sync(
                source,
                writer=cast(Any, None),
                state_store=cast(Any, None),
                audit_sink=None,
                actor="test",
                settings=source,
                authority=capability,
            )
        )
    assert seen["m365"]["authority"] is m365
    assert seen["salesforce"]["authority"] is salesforce
    assert "authority" not in seen["csv"]


def test_run_configured_sync_refuses_cross_source_authority_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ne.connect.authority_admission import M365AuthorityAdmission
    from ne.connect.salesforce.authority import SalesforceAuthorityFactory

    monkeypatch.setattr("ne.connect.runner._load_ref", lambda dotted: None)
    m365 = object.__new__(M365AuthorityAdmission)
    salesforce = object.__new__(SalesforceAuthorityFactory)
    for source, foreign in (("m365", salesforce), ("salesforce", m365)):
        with pytest.raises(TypeError, match="foreign source-authority"):
            asyncio.run(
                run_configured_sync(
                    source,
                    writer=cast(Any, None),
                    state_store=cast(Any, None),
                    audit_sink=None,
                    actor="test",
                    settings=source,
                    authority=foreign,
                )
            )


def test_an_absent_authority_is_not_passed_even_to_the_m365_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class _Outcome:
        report = "report-sentinel"

    async def _fake_run(settings: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return _Outcome()

    monkeypatch.setattr("ne.connect.runner._load_ref", lambda dotted: _fake_run)
    asyncio.run(
        run_configured_sync(
            "m365",
            writer=cast(Any, None),
            state_store=cast(Any, None),
            audit_sink=None,
            actor="test",
            settings="m365",
        )
    )
    assert "authority" not in seen


def test_an_unconfigured_connector_root_composes_no_authority(tmp_path: Path) -> None:
    import sqlalchemy as sa

    from ne.contracts.knowledge import ChunkStore

    engine = sa.create_engine("sqlite://")
    stack = build_ingest_stack(
        chunk_store=cast(ChunkStore, cast(Any, object())),
        runtime_engine=engine,
        sink=cast(Any, _NullAuditSink()),
        settings=ConnectSyncSettings(
            sources=("fixture",),
            state_path=str(tmp_path / "s.db"),
            quarantine_path=str(tmp_path / "q.db"),
        ),
    )
    assert stack.authority is None
    assert stack.authority_unavailable_reason is None
    assert stack.authority_for("salesforce") is None
    assert stack.authority_for("fixture") is None


def test_ingest_stack_selects_distinct_source_specific_authorities() -> None:
    from ne.connect.runner import IngestStack

    m365 = object()
    salesforce = object()
    stack = IngestStack(
        writer=cast(Any, object()),
        state_store=cast(Any, object()),
        authority=cast(Any, m365),
        salesforce_authority_factory=salesforce,
        authority_unavailable_reason="m365 unavailable",
        salesforce_authority_unavailable_reason="salesforce unavailable",
    )
    assert stack.authority_for("m365") is m365
    assert stack.authority_for("salesforce") is salesforce
    assert stack.authority_for("csv") is None
    assert stack.authority_unavailable_for("m365") == "m365 unavailable"
    assert stack.authority_unavailable_for("salesforce") == "salesforce unavailable"
    assert stack.authority_unavailable_for("csv") is None


def test_combined_root_composes_distinct_m365_and_salesforce_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlalchemy as sa

    from ne.brain.testing.embedding import HashEmbedder
    from ne.connect.authority_admission import M365AuthorityAdmission
    from ne.connect.salesforce.authority import SalesforceAuthorityFactory

    from .m365_transport import m365_settings
    from .salesforce_transport import salesforce_settings

    configured = {
        "m365": m365_settings(
            authority_enabled=True,
            authority_sweep_cadence_seconds=60,
            authority_sweep_batch_size=10,
            authority_sweep_max_batch_duration_seconds=5,
            authority_sweep_max_points_per_scope=100,
        ),
        "salesforce": salesforce_settings(
            authority_enabled=True,
            authority_sweep_cadence_seconds=60,
            authority_sweep_batch_size=10,
            authority_sweep_max_batch_duration_seconds=5,
            authority_sweep_max_points_per_scope=100,
        ),
    }
    monkeypatch.setattr(
        "ne.connect.runner.build_sync_settings", lambda source, **_: configured[source]
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path}/combined-runtime.db")
    stack = build_ingest_stack(
        chunk_store=cast(Any, object()),
        runtime_engine=engine,
        sink=cast(Any, _NullAuditSink()),
        settings=ConnectSyncSettings(
            sources=("m365", "salesforce"),
            state_path=str(tmp_path / "combined-state.db"),
            quarantine_path=str(tmp_path / "combined-quarantine.db"),
        ),
        embedder=HashEmbedder(32),
    )
    assert isinstance(stack.authority_for("m365"), M365AuthorityAdmission)
    assert isinstance(stack.authority_for("salesforce"), SalesforceAuthorityFactory)
    assert stack.authority_for("m365") is not stack.authority_for("salesforce")


def test_salesforce_authority_missing_embedder_is_source_specific_and_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlalchemy as sa

    from .salesforce_transport import salesforce_settings

    configured = salesforce_settings(
        authority_enabled=True,
        authority_sweep_cadence_seconds=60,
        authority_sweep_batch_size=10,
        authority_sweep_max_batch_duration_seconds=5,
        authority_sweep_max_points_per_scope=100,
    )
    monkeypatch.setattr("ne.connect.runner.build_sync_settings", lambda source, **_: configured)
    stack = build_ingest_stack(
        chunk_store=cast(Any, object()),
        runtime_engine=sa.create_engine("sqlite://"),
        sink=cast(Any, _NullAuditSink()),
        settings=ConnectSyncSettings(
            sources=("salesforce",),
            state_path=str(tmp_path / "missing-state.db"),
            quarantine_path=str(tmp_path / "missing-quarantine.db"),
        ),
        embedder=None,
    )
    assert stack.authority_for("salesforce") is None
    assert "needs the composed embedder" in str(stack.authority_unavailable_for("salesforce"))
    assert stack.authority_unavailable_for("m365") is None


class _NullAuditSink:
    """An AuditSink that swallows events — the writer refuses ``None`` outright."""

    async def append(self, event: Any) -> None:
        return None


def test_a_record_with_no_completed_permission_read_never_advances_authority() -> None:
    """The bridge admits from EVIDENCE, not from having produced a record.

    A record whose permission collection was never read to completion has no
    entry in the connector's evidence map, so it is written to the governed
    corpus and simply carries no authority — the fail-closed outcome — rather
    than being admitted on the strength of its content alone.
    """
    from ne.connect.state import ConnectorStateStore

    trace: list[str] = []
    authority = _RecordingAuthority(trace)
    report = _run_bridge(
        _authority_records(),
        authority=cast(Any, authority),
        state_store=ConnectorStateStore(),
        trace=trace,
        evidence={},
    )
    assert report.written == 2
    assert authority.prepared == []
    assert authority.finalized == []


# -- 015A0-ENFORCEMENT: the server-authored source-authority class -------------


def test_every_registry_entry_declares_an_explicit_source_disposition() -> None:
    """The class of a source is decided in reviewed code, exhaustively.

    A new connector added without a decision inherits the fail-closed middle
    case, so it is withheld at read rather than disclosed.
    """
    assert {name: entry.disposition for name, entry in SYNC_SOURCES.items()} == {
        "m365": SourceDisposition.SUPPORTED_SOURCE_BEARING,
        "salesforce": SourceDisposition.SUPPORTED_SOURCE_BEARING,
        "teams": SourceDisposition.UNSUPPORTED_SOURCE_BEARING,
        "slack": SourceDisposition.UNSUPPORTED_SOURCE_BEARING,
        "jira": SourceDisposition.UNSUPPORTED_SOURCE_BEARING,
        "zendesk": SourceDisposition.UNSUPPORTED_SOURCE_BEARING,
        "sql": SourceDisposition.UNSUPPORTED_SOURCE_BEARING,
        "fixture": SourceDisposition.SOURCE_FREE,
        "csv": SourceDisposition.SOURCE_FREE,
    }
    assert SyncSourceEntry("a:b", "c:d").disposition is (
        SourceDisposition.UNSUPPORTED_SOURCE_BEARING
    )
    # The two capabilities are derived from the ONE decision, never set apart.
    assert {name for name, e in SYNC_SOURCES.items() if e.authority_bearing} == {
        "m365",
        "salesforce",
    }
    assert {name for name, e in SYNC_SOURCES.items() if e.source_free} == {"fixture", "csv"}


def test_run_configured_sync_grants_source_free_only_to_a_source_free_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, dict[str, Any]] = {}

    class _Outcome:
        report = "report-sentinel"

    async def _fake_run(settings: Any, **kwargs: Any) -> Any:
        seen[str(settings)] = kwargs
        return _Outcome()

    monkeypatch.setattr("ne.connect.runner._load_ref", lambda dotted: _fake_run)
    for source in ("fixture", "csv", "jira", "m365"):
        asyncio.run(
            run_configured_sync(
                source,
                writer=cast(Any, None),
                state_store=cast(Any, None),
                audit_sink=None,
                actor="test",
                settings=source,
            )
        )
    assert seen["fixture"]["source_free"] is True
    assert seen["csv"]["source_free"] is True
    # An external source with no authority producer is NOT source-free: it is
    # source-bearing-but-unsupported, and stays withheld.
    assert "source_free" not in seen["jira"]
    assert "source_free" not in seen["m365"]


class _CapturingWriter:
    """A KnowledgeWrite that accepts everything and retains the written items."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    async def upsert(self, items: Any) -> Any:
        from ne.contracts.knowledge import UpsertReceipt

        self.items.extend(items)
        return UpsertReceipt(accepted=tuple(item.item_id for item in items), quarantined=())

    async def curate(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("this bridge never curates")


def _written_metadata(*, source_free: bool, mapper: Any = None) -> dict[str, Any]:
    from ne.connect.bridge import SyncRunner
    from ne.connect.state import ConnectorStateStore

    writer = _CapturingWriter()
    runner = SyncRunner(
        cast(Any, _StubConnector(_authority_records()[:1])),
        _identity_contract(),
        cast(Any, writer),
        ConnectorStateStore(),
        actor="test",
        source_free=source_free,
        **({} if mapper is None else {"mapper": mapper}),
    )
    asyncio.run(runner.run(mode="backfill"))
    assert len(writer.items) == 1
    return dict(writer.items[0].metadata)


def test_the_governed_write_marks_a_source_free_lane_and_nothing_else() -> None:
    """The paired mutation: the mark is written ONLY under the registry grant.

    With the grant the chunk is disclosable without a source-authority stamp;
    without it the chunk carries no class at all and the read filter refuses it,
    which is what a source whose authority cannot be established must get.
    """
    granted = _written_metadata(source_free=True)
    assert granted[SOURCE_STATUS_METADATA_KEY] == NO_SOURCE_STATUS
    ungranted = _written_metadata(source_free=False)
    assert SOURCE_STATUS_METADATA_KEY not in ungranted
    # Everything else about the two writes is identical — the class is the only
    # difference, so a lane cannot be smuggled open by any other metadata.
    assert {k: v for k, v in granted.items() if k != SOURCE_STATUS_METADATA_KEY} == ungranted


def test_a_mapper_can_neither_assert_nor_keep_a_source_free_mark() -> None:
    """A source that could label itself source-free would be its own authority."""

    def _self_labelling_mapper(record: Record, contract: Any, acl: tuple[str, ...]) -> Any:
        item = default_record_mapper(record, contract, acl)
        return item.model_validate(
            {
                **item.model_dump(),
                "metadata": {**item.metadata, SOURCE_STATUS_METADATA_KEY: NO_SOURCE_STATUS},
            }
        )

    stripped = _written_metadata(source_free=False, mapper=_self_labelling_mapper)
    assert SOURCE_STATUS_METADATA_KEY not in stripped

    def _garbage_mapper(record: Record, contract: Any, acl: tuple[str, ...]) -> Any:
        item = default_record_mapper(record, contract, acl)
        return item.model_validate(
            {
                **item.model_dump(),
                "metadata": {**item.metadata, SOURCE_STATUS_METADATA_KEY: "definitely-fine"},
            }
        )

    overwritten = _written_metadata(source_free=True, mapper=_garbage_mapper)
    assert overwritten[SOURCE_STATUS_METADATA_KEY] == NO_SOURCE_STATUS


# -- 015A0-ENFORCEMENT: the disclosure stamp the read filter requires ---------


def test_the_stamp_is_written_only_after_authority_has_moved() -> None:
    """ORDERING IS THE CONTROL. The stamp cannot exist before ``finalize``.

    ``artifact`` (prepare) → ``write`` (governed upsert, so lineage and audit
    land) → ``authority`` (finalize moves the head) → ``curate`` (the stamp).
    Nothing may reorder: the receipt id and hash the stamp carries do not exist
    until finalize returns.
    """
    from ne.connect.state import ConnectorStateStore

    trace: list[str] = []
    writer_trace = trace
    authority = _RecordingAuthority(trace)
    writer = _StubWriter(writer_trace)
    runner = SyncRunner(
        cast(Any, _StubConnector(_authority_records())),
        _identity_contract(),
        cast(Any, writer),
        ConnectorStateStore(),
        actor="test",
        authority=cast(Any, authority),
        permission_evidence=_evidence_for(_authority_records()),
    )
    asyncio.run(runner.run(mode="backfill"))

    for record_id in ("item-a", "item-b"):
        item_id = _item_id_for(record_id)
        assert trace.index(f"artifact:{item_id}") < trace.index(f"write:{item_id}")
        assert trace.index(f"write:{item_id}") < trace.index(f"authority:{item_id}")
        assert trace.index(f"authority:{item_id}") < trace.index(f"curate:{item_id}")

    # The stamp is the canonical derivation of the receipt finalize returned.
    for item_id, metadata, provenance in writer.curated:
        expected = stamp_from_receipt(authority.receipts[item_id])
        assert expected is not None
        assert metadata == {SOURCE_AUTHORITY_STAMP_METADATA_KEY: expected.model_dump(mode="json")}
        assert provenance == "connector:m365-sharepoint:source-authority-stamp"


def test_an_item_whose_authority_never_moved_is_never_stamped() -> None:
    """The paired mutation: authority that cannot be established stays refused."""
    from ne.connect.state import ConnectorStateStore

    trace: list[str] = []
    authority = _RecordingAuthority(trace)
    writer = _StubWriter(trace, reject={_item_id_for("item-b")})
    runner = SyncRunner(
        cast(Any, _StubConnector(_authority_records())),
        _identity_contract(),
        cast(Any, writer),
        ConnectorStateStore(),
        actor="test",
        authority=cast(Any, authority),
        permission_evidence=_evidence_for(_authority_records()),
    )
    asyncio.run(runner.run(mode="backfill"))
    # The quarantined item never finalized, so it never acquired a stamp.
    assert authority.finalized == [_item_id_for("item-a")]
    assert [item_id for item_id, _, _ in writer.curated] == [_item_id_for("item-a")]


def test_a_withdrawn_source_is_not_stamped_into_disclosability() -> None:
    """A non-present admission has no artifact, so there is nothing to disclose."""
    from ne.connect.state import ConnectorStateStore

    trace: list[str] = []
    authority = _RecordingAuthority(trace, present=False)
    writer = _StubWriter(trace)
    runner = SyncRunner(
        cast(Any, _StubConnector(_authority_records())),
        _identity_contract(),
        cast(Any, writer),
        ConnectorStateStore(),
        actor="test",
        authority=cast(Any, authority),
        permission_evidence=_evidence_for(_authority_records()),
    )
    asyncio.run(runner.run(mode="backfill"))
    assert authority.finalized == [_item_id_for("item-a"), _item_id_for("item-b")]
    assert writer.curated == []
    assert stamp_from_receipt(_authority_receipt("item-a", present=False)) is None


def test_a_retained_corpus_acquires_its_stamp_without_being_rewritten() -> None:
    """The live-corpus path: an UNCHANGED record still gets stamped.

    Chunks ingested before stamping existed are content-unchanged, so they never
    reach the write path again. The re-admission every quiet sweep already
    performs is what carries the stamp to them — no forced rewrite, no re-embed.
    """
    from ne.connect.state import ConnectorStateStore

    store = ConnectorStateStore()
    records = _authority_records()
    first = _RecordingAuthority([])
    first_writer = _StubWriter([])
    runner = SyncRunner(
        cast(Any, _StubConnector(records)),
        _identity_contract(),
        cast(Any, first_writer),
        store,
        actor="test",
        authority=cast(Any, first),
        permission_evidence=_evidence_for(records),
    )
    asyncio.run(runner.run(mode="backfill"))

    second = _RecordingAuthority([])
    second_writer = _StubWriter([])
    quiet = SyncRunner(
        cast(Any, _StubConnector(records)),
        _identity_contract(),
        cast(Any, second_writer),
        store,
        actor="test",
        authority=cast(Any, second),
        permission_evidence=_evidence_for(records),
    )
    report = asyncio.run(quiet.run(mode="backfill"))
    assert (report.written, report.skipped_unchanged) == (0, 2)
    assert [item_id for item_id, _, _ in second_writer.curated] == [
        _item_id_for("item-a"),
        _item_id_for("item-b"),
    ]


def test_a_chunk_that_already_names_this_truth_is_not_rewritten_every_sweep() -> None:
    """Receipt rotation is freshness evidence, not truth — do not churn on it.

    ``SqlAlchemyCurrentSourceAuthorityReader`` keeps an older authentic stamp
    authorized against a newly minted receipt for byte-identical truth. Rewriting
    the chunk anyway would cost one store write and one audit row per record per
    sweep and change nothing a reader can observe.
    """
    from ne.connect.state import ConnectorStateStore

    records = _authority_records()
    stamped: dict[str, Any] = {}

    async def _reader(item_id: str) -> Any:
        return stamped.get(item_id)

    def _run(receipt_id: str) -> _StubWriter:
        writer = _StubWriter([])
        authority = _RecordingAuthority([])
        authority.receipt_id = receipt_id
        runner = SyncRunner(
            cast(Any, _StubConnector(records)),
            _identity_contract(),
            cast(Any, writer),
            ConnectorStateStore(),
            actor="test",
            authority=cast(Any, authority),
            permission_evidence=_evidence_for(records),
            chunk_reader=_reader,
        )
        asyncio.run(runner.run(mode="backfill"))
        for item_id, metadata, _ in writer.curated:
            stamped[item_id] = Chunk(
                chunk_id=item_id, document_id=item_id, text="x", metadata=metadata
            )
        return writer

    first = _run("receipt-1")
    assert len(first.curated) == 2
    # A LATER sweep mints a NEW receipt id over identical truth. The retained
    # stamp stays authorized against it, so nothing is rewritten.
    second = _run("receipt-2")
    assert second.curated == []

    # But a chunk whose stored stamp names DIFFERENT truth is re-stamped.
    for item_id in list(stamped):
        stamped[item_id] = Chunk(
            chunk_id=item_id,
            document_id=item_id,
            text="x",
            metadata={SOURCE_AUTHORITY_STAMP_METADATA_KEY: {"stale": True}},
        )
    third = _run("receipt-3")
    assert len(third.curated) == 2
