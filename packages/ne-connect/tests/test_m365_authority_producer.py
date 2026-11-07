# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-M365: the supported M365 pull path issuing real authority.

The producer is composed by ``build_ingest_stack`` from the ambient connector
environment — the same composer the worker cron and the CLI call — and driven
through ``run_m365_sync`` over the recorded Graph transport. Nothing here stubs
the authority Writer, the immutable artifact store or the receipt rows: they are
the real AUTHORITY-CORE adapters over a real engine.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from qdrant_client import AsyncQdrantClient

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.audit.store import ne_audit_log
from ne.brain.current_source_authority import (
    SourceAuthorityClaimConflictError,
    SourceReconciliationScopeBusyError,
    SqlAlchemyCurrentSourceAuthorityReader,
    SqlAlchemyCurrentSourceAuthorityWriter,
    claim_or_load_deployment_construction_id,
    ne_source_authority_heads,
    ne_source_authority_receipts,
    ne_source_reconciliation_claims,
)
from ne.brain.lineage import SqlAlchemyDocumentLineageStore, ensure_document_lineage_schema
from ne.brain.parsed_evidence import PARSED_RECORD_METADATA_KEY
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.retriever import HybridRetriever
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.trimming import PrincipalAclFilter
from ne.connect.authority_admission import (
    AdmissionStage,
    OverlappingDriveScopeError,
    SqlAlchemyAdmissionAttemptStore,
    SqlAlchemyHierarchicalAdmission,
    SqlAlchemySourceContractHeadStore,
    UnclaimedScopeError,
    UnreconciledDescendantError,
    connector_instance_scope,
    drive_scope,
    expected_scope_head_revision,
    observed_permissions,
    site_scope,
)
from ne.connect.bridge import canonical_item_id
from ne.connect.identity import source_acl_hash
from ne.connect.m365.contract import (
    m365_admitted_target_digest,
    m365_contract_identity,
    m365_contract_sla_seconds,
    m365_data_contract,
)
from ne.connect.m365.sync import _M365AuthorityDriver, run_m365_sync
from ne.connect.runner import ConnectSyncSettings, IngestStack, build_ingest_stack
from ne.connect.state import ConnectorStateStore
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    SourceAuthorityState,
)
from ne.contracts.types import Principal

from .m365_transport import (
    DRIVE_ID,
    SITE_ID,
    TENANT_ID,
    RecordedBodyParser,
    build_m365_transport,
    m365_settings,
)

_SOURCE = "m365-sharepoint"
_SLA_SECONDS = Decimal(3600)


@pytest.fixture(autouse=True)
def _frozen(frozen_config: str) -> str:
    """Every sync emits a config_hash-stamped audit event; freeze one for the run."""
    return frozen_config


def _authority_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: str) -> None:
    """Point the ambient connector environment at the fixture tenant."""
    env = {
        "NE_M365_TENANT_ID": TENANT_ID,
        "NE_M365_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
        "NE_M365_AUTH_METHOD": "secret",
        "NE_M365_CLIENT_SECRET": "fake-secret-not-real",
        "NE_M365_SITE_IDS": f'["{SITE_ID}"]',
        "NE_M365_OWNER": "data-eng@contoso.test",
        "NE_M365_AUTHORITY_ENABLED": "true",
        "NE_M365_AUTHORITY_SWEEP_CADENCE_SECONDS": "120",
        "NE_M365_AUTHORITY_SWEEP_BATCH_SIZE": "50",
        "NE_M365_AUTHORITY_SWEEP_MAX_BATCH_DURATION_SECONDS": "60",
        "NE_M365_AUTHORITY_SWEEP_MAX_POINTS_PER_SCOPE": "500",
        "NE_CONNECT_SYNC_SOURCES": '["m365"]',
        "NE_CONNECT_SYNC_STATE_PATH": str(tmp_path / "state.db"),
        "NE_CONNECT_SYNC_QUARANTINE_PATH": str(tmp_path / "quarantine.db"),
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@dataclass
class AuthorityStack:
    """The composed producer plus the durable surfaces a proof reads back."""

    stack: IngestStack
    engine: sa.Engine
    sink: PostgresAuditSink
    lineage: SqlAlchemyDocumentLineageStore
    state_store: ConnectorStateStore
    settings: Any
    artifacts: Any


def _compose(tmp_path: Path) -> AuthorityStack:
    client = AsyncQdrantClient(":memory:")
    store = QdrantChunkStore(
        client, f"na_{uuid.uuid4().hex}", HashEmbedder(32), HashSparseEncoder()
    )
    asyncio.run(store.ensure_collection())
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'runtime.db'}",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    ensure_audit_schema(engine)
    ensure_document_lineage_schema(engine)
    lineage = SqlAlchemyDocumentLineageStore(engine)
    stack = build_ingest_stack(
        chunk_store=store,
        runtime_engine=engine,
        sink=PostgresAuditSink(engine),
        settings=ConnectSyncSettings(),  # type: ignore[call-arg]
        embedder=HashEmbedder(32),
        document_lineage=lineage,
    )
    from ne.connect.m365.config import M365ConnectorSettings

    return AuthorityStack(
        stack=stack,
        engine=engine,
        sink=PostgresAuditSink(engine),
        lineage=lineage,
        state_store=stack.state_store,
        settings=M365ConnectorSettings(),  # type: ignore[call-arg]
        artifacts=cast(Any, stack.authority)._artifacts if stack.authority else None,
    )


def _run(composed: AuthorityStack, **kwargs: Any) -> Any:
    settings = kwargs.pop("settings", composed.settings)
    parser = kwargs.pop("parser", RecordedBodyParser())
    return asyncio.run(
        run_m365_sync(
            settings,
            writer=composed.stack.writer,
            state_store=composed.state_store,
            audit_sink=composed.sink,
            actor="connector:test",
            parser=parser,
            authority=composed.stack.authority,
            **kwargs,
        )
    )


def _receipts(engine: sa.Engine) -> list[CurrentSourceAuthorityReceipt]:
    """Read every admitted receipt back through the PRODUCTION Reader path.

    Deliberately not ``model_validate`` over the raw row: a receipt an operator
    or the Reader cannot load is not an admitted receipt, and reading the JSON
    directly here would hide exactly that failure.
    """
    reader = SqlAlchemyCurrentSourceAuthorityReader(
        engine, claim_or_load_deployment_construction_id(engine)
    )
    with engine.begin() as conn:
        ids = [
            str(row[0])
            for row in conn.execute(
                sa.select(ne_source_authority_receipts.c.receipt_id).order_by(
                    ne_source_authority_receipts.c.receipt_id
                )
            ).all()
        ]
    loaded = [reader.load_receipt(receipt_id) for receipt_id in ids]
    assert all(receipt is not None for receipt in loaded)
    return [receipt for receipt in loaded if receipt is not None]


def _read(item_id: str, *, principals: tuple[str, ...] = ("u-ada",), drive: str = DRIVE_ID) -> Any:
    """A completed permission read for one point, as the connector would mint it."""
    return observed_permissions(
        drive_id=drive,
        item_id=item_id,
        principals=principals,
        pages_read=1,
        collection_complete=True,
    )


def _point(receipt: CurrentSourceAuthorityReceipt) -> M365SourcePoint:
    """The receipt's M365 point; a governed-local-T3 point here is a real defect."""
    point = receipt.source_identity.point
    assert isinstance(point, M365SourcePoint)
    return point


def _heads(engine: sa.Engine) -> dict[str, str]:
    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(
                ne_source_authority_heads.c.identity_hash,
                ne_source_authority_heads.c.current_receipt_id,
            )
        ).all()
    return {str(row.identity_hash): str(row.current_receipt_id) for row in rows}


# -- composition -------------------------------------------------------------


def test_build_ingest_stack_composes_the_one_authority_producer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    assert composed.stack.authority is not None
    assert composed.stack.authority_unavailable_reason is None


def test_a_connector_without_authority_enabled_composes_no_producer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path, NE_M365_AUTHORITY_ENABLED="false")
    composed = _compose(tmp_path)
    assert composed.stack.authority is None
    assert composed.stack.authority_unavailable_reason is None


def test_a_sweep_that_cannot_beat_the_sla_leaves_the_scope_unadvertised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An infeasible sweep yields no producer AND a stated reason, never an exception."""
    _authority_env(
        monkeypatch,
        tmp_path,
        NE_M365_AUTHORITY_SWEEP_CADENCE_SECONDS="3600",
    )
    composed = _compose(tmp_path)
    assert composed.stack.authority is None
    assert "does not fit inside" in (composed.stack.authority_unavailable_reason or "")


def test_the_composed_contract_head_starts_at_epoch_one_for_the_configured_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    head = SqlAlchemySourceContractHeadStore(composed.engine).load(
        connector_instance_scope(
            deployment_id=cast(Any, composed.stack.authority).deployment_id,
            connector_instance_id=cast(Any, composed.stack.authority).connector_instance_id,
            graph_tenant_id=TENANT_ID,
        )
    )
    assert head is not None
    assert head.contract_epoch == 1
    assert head.source_contract == m365_contract_identity(composed.settings)


def test_the_connector_instance_survives_a_client_secret_rotation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    first = _compose(tmp_path)
    before = cast(Any, first.stack.authority).connector_instance_id
    monkeypatch.setenv("NE_M365_CLIENT_SECRET", "rotated-secret-not-real")
    second = build_ingest_stack(
        chunk_store=cast(Any, first.stack.writer)._store,
        runtime_engine=first.engine,
        sink=PostgresAuditSink(first.engine),
        settings=ConnectSyncSettings(),  # type: ignore[call-arg]
        embedder=HashEmbedder(32),
        document_lineage=first.lineage,
    )
    assert cast(Any, second.authority).connector_instance_id == before


def test_the_admitted_target_digest_carries_no_credential_value() -> None:
    settings = m365_settings(authority_enabled=False)
    digest = m365_admitted_target_digest(settings)
    rotated = m365_admitted_target_digest(
        m365_settings(client_secret="a-completely-different-secret")
    )
    assert digest == rotated
    for changed in (
        m365_settings(tenant_id="99999999-9999-9999-9999-999999999999"),
        m365_settings(site_ids=("other-site",)),
        m365_settings(site_ids=(SITE_ID,), drive_ids=("drive-x",)),
        m365_settings(graph_base_url="https://graph.microsoft.us/v1.0"),
    ):
        assert m365_admitted_target_digest(changed) != digest


def test_the_contract_sla_is_the_exact_fixed_duration_in_seconds() -> None:
    assert m365_contract_sla_seconds(m365_settings()) == _SLA_SECONDS
    assert m365_contract_sla_seconds(m365_settings(freshness_sla="PT15M")) == Decimal(900)


def test_a_calendar_ambiguous_sla_cannot_produce_authority_seconds() -> None:
    with pytest.raises(ValueError):
        m365_contract_sla_seconds(m365_settings(freshness_sla="P1M"))


# -- a real admitted crawl ---------------------------------------------------


def test_every_written_item_receives_a_present_receipt_bound_to_its_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    outcome = _run(
        composed,
        transport=build_m365_transport(),
        settings=composed.settings.model_copy(
            update={"content_allowed_hosts": ("contoso.sharepoint.com",)}
        ),
    )
    present = [r for r in _receipts(composed.engine) if r.state is SourceAuthorityState.PRESENT]
    assert len(present) == outcome.report.written
    for receipt in present:
        assert receipt.artifact_identity is not None
        assert receipt.artifact_identity.acl_snapshot_hash == receipt.acl_snapshot_hash
        assert ":" not in receipt.artifact_identity.artifact_id


def test_an_admitted_receipt_carries_the_configured_contract_hash_epoch_and_sla(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    _run(
        composed,
        transport=build_m365_transport(),
        settings=composed.settings.model_copy(
            update={"content_allowed_hosts": ("contoso.sharepoint.com",)}
        ),
    )
    expected = m365_contract_identity(composed.settings)
    for receipt in _receipts(composed.engine):
        assert receipt.source_contract == expected
        assert receipt.contract_epoch == 1
        assert receipt.freshness_sla_seconds == _SLA_SECONDS


def test_the_admitted_identity_names_the_exact_tenant_site_drive_and_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    _run(
        composed,
        transport=build_m365_transport(),
        settings=composed.settings.model_copy(
            update={"content_allowed_hosts": ("contoso.sharepoint.com",)}
        ),
    )
    points = {
        (
            _point(r).graph_tenant_id,
            _point(r).site_id,
            _point(r).drive_id,
            _point(r).item_id,
        )
        for r in _receipts(composed.engine)
        if r.state is SourceAuthorityState.PRESENT
    }
    assert (TENANT_ID, SITE_ID, DRIVE_ID, "item-user") in points
    assert all(kind == TENANT_ID for kind, _, _, _ in points)


def test_the_head_selects_one_receipt_per_admitted_point(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    _run(
        composed,
        transport=build_m365_transport(),
        settings=composed.settings.model_copy(
            update={"content_allowed_hosts": ("contoso.sharepoint.com",)}
        ),
    )
    heads = _heads(composed.engine)
    receipts = {r.receipt_id: r for r in _receipts(composed.engine)}
    assert set(heads.values()) <= set(receipts)
    assert len(heads) == len({r.source_identity.identity_hash for r in receipts.values()})


def test_t1_lineage_retains_the_exact_artifact_identity_and_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    _run(
        composed,
        transport=build_m365_transport(),
        settings=composed.settings.model_copy(
            update={"content_allowed_hosts": ("contoso.sharepoint.com",)}
        ),
    )
    present = [r for r in _receipts(composed.engine) if r.state is SourceAuthorityState.PRESENT]
    assert present
    retained = [
        composed.lineage.retained_source_authority(document_id, 1)
        for document_id in _document_ids(composed.engine)
    ]
    bound = [row for row in retained if row is not None]
    assert len(bound) == len(present)
    by_receipt = {row.receipt_id: row for row in bound}
    for receipt in present:
        row = by_receipt[receipt.receipt_id]
        assert receipt.artifact_identity is not None
        assert row.artifact_id == receipt.artifact_identity.artifact_id
        assert row.receipt_hash == receipt.receipt_hash
        assert row.source_identity_hash == receipt.source_identity.identity_hash


def test_m365_version_and_acl_contraction_remain_exact_through_current_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One source point keeps v1 evidence while v2 contracts current access.

    The recorded provider response supplies ACL facts only.  The querying
    principal is supplied independently to the real retrieval boundary below,
    so this fixture cannot select or impersonate the reader whose access is
    being proved.
    """
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    settings = composed.settings.model_copy(
        update={"content_allowed_hosts": ("contoso.sharepoint.com",)}
    )
    document_id = canonical_item_id(
        _SOURCE,
        f"{DRIVE_ID}:item-user",
        "item-user",
    )
    v1_acl = ("u-ada", "u-bob")
    v2_acl = ("u-ada",)
    v2_body = "Expedited tier: provisional credit within 5 business days for eligible disputes."
    v1_permissions = {
        "value": [
            {
                "id": "perm-user-v1",
                "roles": ["read"],
                "grantedToIdentitiesV2": [{"user": {"id": principal}} for principal in v1_acl],
            }
        ]
    }

    first = _run(
        composed,
        transport=build_m365_transport(permission_payload_overrides={"item-user": v1_permissions}),
        settings=settings,
    )
    assert first.mode == "backfill"
    first_journey = composed.lineage.journey(document_id, v1_acl)
    assert first_journey is not None
    assert first_journey.selected.version_seq == 1
    assert first_journey.selected.acl == v1_acl
    first_receipt = next(
        receipt
        for receipt in _receipts(composed.engine)
        if receipt.state is SourceAuthorityState.PRESENT
        and _point(receipt).item_id == "item-user"
        and receipt.source_revision == '"etag-user-v1"'
    )
    assert first_receipt.artifact_identity is not None
    assert first_receipt.acl_snapshot_hash == source_acl_hash(v1_acl)
    first_retained = composed.lineage.retained_source_authority(document_id, 1)
    assert first_retained is not None
    assert first_retained.receipt_id == first_receipt.receipt_id
    assert first_retained.artifact_id == first_receipt.artifact_identity.artifact_id

    second = _run(
        composed,
        transport=build_m365_transport(),
        settings=settings,
        parser=RecordedBodyParser(item_user_text=v2_body),
    )
    assert second.mode == "incremental"
    second_receipt = next(
        receipt
        for receipt in _receipts(composed.engine)
        if receipt.state is SourceAuthorityState.PRESENT
        and _point(receipt).item_id == "item-user"
        and receipt.source_revision == '"etag-user-v2"'
    )
    assert second_receipt.artifact_identity is not None
    assert second_receipt.acl_snapshot_hash == source_acl_hash(v2_acl)
    assert second_receipt.acl_snapshot_hash != first_receipt.acl_snapshot_hash
    assert (
        second_receipt.artifact_identity.source_snapshot_hash
        != first_receipt.artifact_identity.source_snapshot_hash
    )
    assert second_receipt.artifact_identity.artifact_id != first_retained.artifact_id

    # The removed principal keeps its attributable historical v1 view, but a
    # current read cannot silently fall back to that superseded version.
    removed_history = composed.lineage.journey(document_id, ("u-bob",), version_seq=1)
    assert removed_history is not None
    assert removed_history.selected.version_seq == 1
    assert removed_history.selected.acl == v1_acl
    assert composed.lineage.journey(document_id, ("u-bob",)) is None
    assert composed.lineage.retained_source_authority(document_id, 1) == first_retained

    current = composed.lineage.journey(document_id, ("u-ada",))
    assert current is not None
    assert current.selected.version_seq == 2
    assert current.selected.acl == v2_acl
    assert current.selected.content == v2_body
    second_retained = composed.lineage.retained_source_authority(document_id, 2)
    assert second_retained is not None
    assert second_retained.receipt_id == second_receipt.receipt_id
    assert second_retained.artifact_id == second_receipt.artifact_identity.artifact_id

    class _IdentityOnlyExpander:
        def expand(self, principal: Principal | str) -> frozenset[str]:
            principal_id = principal.id if isinstance(principal, Principal) else principal
            return frozenset({principal_id})

    store = cast(Any, composed.stack.writer)._store
    retriever = HybridRetriever(
        store,
        HashEmbedder(32),
        HashSparseEncoder(),
        acl_filters=PrincipalAclFilter(_IdentityOnlyExpander()),
        audit_sink=None,
    )

    async def current_reads() -> None:
        removed = await retriever.retrieve([document_id], principal=Principal(id="u-bob"))
        assert removed.chunks == ()
        retained = await retriever.retrieve([document_id], principal=Principal(id="u-ada"))
        assert len(retained.chunks) == 1
        chunk = retained.chunks[0].chunk
        assert chunk.acl == v2_acl
        # Parsed-document chunks carry the record envelope in metadata; the
        # text is the document prose (see PARSED_RECORD_METADATA_KEY).
        assert chunk.metadata[PARSED_RECORD_METADATA_KEY]["etag"] == '"etag-user-v2"'
        assert v2_body in chunk.text

    asyncio.run(current_reads())

    # Replaying the now-empty delta cannot mint v3 or replace either retained
    # version receipt.  A quiet authority re-observation may lawfully refresh a
    # current head, so idempotency is asserted at the document/artifact seam.
    provider_calls = composed.artifacts.provider_call_count
    replay = _run(
        composed,
        transport=build_m365_transport(),
        settings=settings,
        parser=RecordedBodyParser(item_user_text=v2_body),
    )
    assert replay.mode == "incremental"
    replayed = composed.lineage.journey(document_id, ("u-ada",))
    assert replayed is not None
    assert [version.version_seq for version in replayed.versions] == [1, 2]
    assert composed.lineage.retained_source_authority(document_id, 1) == first_retained
    replay_retained = composed.lineage.retained_source_authority(document_id, 2)
    assert replay_retained is not None
    assert replay_retained.artifact_id == second_retained.artifact_id
    replay_receipt = next(
        receipt
        for receipt in _receipts(composed.engine)
        if receipt.receipt_id == replay_retained.receipt_id
    )
    assert replay_receipt.source_revision == '"etag-user-v2"'
    assert replay_receipt.acl_snapshot_hash == source_acl_hash(v2_acl)
    assert composed.artifacts.provider_call_count == provider_calls


def _document_ids(engine: sa.Engine) -> list[str]:
    with engine.begin() as conn:
        rows = conn.execute(sa.text("SELECT document_id FROM ne_document_versions")).all()
    return [str(row[0]) for row in rows]


# -- identity-bearing deletion ----------------------------------------------


def test_a_graph_delta_deletion_is_preserved_with_its_drive_item_and_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deletion must name what was deleted; a count names nothing."""
    from ne.connect.m365.connector import M365GraphConnector

    _authority_env(monkeypatch, tmp_path)
    connector = M365GraphConnector(
        m365_settings(content_allowed_hosts=("contoso.sharepoint.com",)),
        transport=build_m365_transport(),
        parser=RecordedBodyParser(),
    )
    asyncio.run(_drain(connector))
    assert [(o.drive_id, o.item_id) for o in connector.deleted_observations] == [
        (DRIVE_ID, "del-1")
    ]
    assert connector.deleted_observations[0].source_revision
    assert connector.deleted_count == len(connector.deleted_observations)


async def _drain(connector: Any) -> None:
    async for _ in connector.backfill():
        pass


def test_a_deletion_is_tombstoned_and_its_attempt_reaches_cursor_committable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    _run(
        composed,
        transport=build_m365_transport(),
        settings=composed.settings.model_copy(
            update={"content_allowed_hosts": ("contoso.sharepoint.com",)}
        ),
    )
    deleted = [r for r in _receipts(composed.engine) if r.state is SourceAuthorityState.DELETED]
    assert [_point(r).item_id for r in deleted] == ["del-1"]

    attempts = SqlAlchemyAdmissionAttemptStore(composed.engine)
    with composed.engine.begin() as conn:
        attempt_ids = [
            str(row[0])
            for row in conn.execute(
                sa.text("SELECT attempt_id FROM ne_source_admission_attempts")
            ).all()
        ]
    assert len(attempt_ids) == 1
    attempt = attempts.inspect(attempt_ids[0])
    assert attempt is not None
    assert attempt.stage is AdmissionStage.CURSOR_COMMITTABLE
    assert attempt.receipt_id == deleted[0].receipt_id


def test_the_delta_cursor_is_only_persisted_once_the_tombstone_is_durable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    _run(
        composed,
        transport=build_m365_transport(),
        settings=composed.settings.model_copy(
            update={"content_allowed_hosts": ("contoso.sharepoint.com",)}
        ),
    )
    assert composed.state_store.load_state(_SOURCE).get(f"delta:{DRIVE_ID}")
    assert any(r.state is SourceAuthorityState.DELETED for r in _receipts(composed.engine))


# -- claim-before-I/O --------------------------------------------------------


def test_no_drives_discovery_request_is_made_before_the_site_parent_is_claimed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    admission = cast(Any, composed.stack.authority)
    requests: list[Any] = []

    async def _refuse(scope: Any, *, lease_seconds: int) -> Any:
        raise RuntimeError("claim refused")

    monkeypatch.setattr(admission, "claim_scope", _refuse)
    with pytest.raises(RuntimeError):
        _run(
            composed,
            transport=build_m365_transport(recorder=requests),
            settings=composed.settings.model_copy(
                update={"content_allowed_hosts": ("contoso.sharepoint.com",)}
            ),
        )
    assert not [r for r in requests if "/sites/" in str(r.url)]
    assert not [r for r in requests if "/root/delta" in str(r.url)]


def test_no_delta_or_permission_request_is_made_before_the_drive_is_claimed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    admission = cast(Any, composed.stack.authority)
    requests: list[Any] = []
    original = admission.claim_scope

    async def _refuse_drive(scope: Any, *, lease_seconds: int) -> Any:
        if scope.path[-2] == "drive":
            raise RuntimeError("drive claim refused")
        return await original(scope, lease_seconds=lease_seconds)

    monkeypatch.setattr(admission, "claim_scope", _refuse_drive)
    with pytest.raises(RuntimeError):
        _run(
            composed,
            transport=build_m365_transport(recorder=requests),
            settings=composed.settings.model_copy(
                update={"content_allowed_hosts": ("contoso.sharepoint.com",)}
            ),
        )
    assert [r for r in requests if "/sites/" in str(r.url)]
    assert not [r for r in requests if "/root/delta" in str(r.url)]
    assert not [r for r in requests if "/permissions" in str(r.url)]


def test_a_drive_both_configured_and_discovered_fails_before_drive_processing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path, NE_M365_DRIVE_IDS=f'["{DRIVE_ID}"]')
    composed = _compose(tmp_path)
    requests: list[Any] = []
    with pytest.raises(OverlappingDriveScopeError):
        _run(
            composed,
            transport=build_m365_transport(recorder=requests),
            settings=composed.settings.model_copy(
                update={"content_allowed_hosts": ("contoso.sharepoint.com",)}
            ),
        )
    assert not [r for r in requests if "/root/delta" in str(r.url)]
    assert _receipts(composed.engine) == []


def test_a_previously_admitted_drive_omitted_by_discovery_blocks_the_site(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    admission = cast(Any, composed.stack.authority)
    hierarchy = SqlAlchemyHierarchicalAdmission(composed.engine)
    parent = site_scope(
        deployment_id=admission.deployment_id,
        connector_instance_id=admission.connector_instance_id,
        graph_tenant_id=TENANT_ID,
        site_id=SITE_ID,
    )
    vanished = drive_scope(
        deployment_id=admission.deployment_id,
        connector_instance_id=admission.connector_instance_id,
        graph_tenant_id=TENANT_ID,
        drive_id="drive-vanished",
    )
    hierarchy.record_descendants(parent, [("drive-vanished", vanished)])
    with pytest.raises(UnreconciledDescendantError):
        _run(
            composed,
            transport=build_m365_transport(),
            settings=composed.settings.model_copy(
                update={"content_allowed_hosts": ("contoso.sharepoint.com",)}
            ),
        )


def test_authority_is_never_advanced_for_an_unclaimed_drive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    admission = cast(Any, composed.stack.authority)
    from ne.contracts.integration import Record

    record = Record(
        record_id="item-user",
        source_id=f"{DRIVE_ID}:item-user",
        payload={"id": "item-user"},
    )
    with pytest.raises(UnclaimedScopeError):
        asyncio.run(
            admission.prepare(
                record,
                acl=("u-ada",),
                canonical_bytes=b"{}",
                item_id="i-1",
                evidence=_read("item-user"),
            )
        )


# -- the quiet identical sweep ----------------------------------------------


def test_an_identical_artifact_is_reused_without_a_second_embedding_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    admission = cast(Any, composed.stack.authority)
    from ne.contracts.integration import Record

    claim = asyncio.run(
        admission.claim_scope(
            drive_scope(
                deployment_id=admission.deployment_id,
                connector_instance_id=admission.connector_instance_id,
                graph_tenant_id=TENANT_ID,
                drive_id=DRIVE_ID,
            ),
            lease_seconds=60,
        )
    )
    admission.bind_drive_claim(DRIVE_ID, claim, site_id=SITE_ID)
    record = Record(
        record_id="item-user",
        source_id=f"{DRIVE_ID}:item-user",
        payload={"id": "item-user", "etag": "etag-1"},
    )
    first = asyncio.run(
        admission.prepare(
            record,
            acl=("u-ada",),
            canonical_bytes=b'{"a":1}',
            item_id="i-1",
            evidence=_read("item-user"),
        )
    )
    calls = composed.artifacts.provider_call_count
    second = asyncio.run(
        admission.prepare(
            record,
            acl=("u-ada",),
            canonical_bytes=b'{"a":1}',
            item_id="i-1",
            evidence=_read("item-user"),
        )
    )
    assert first.artifact_identity == second.artifact_identity
    assert first.create_receipt.created is True
    assert second.create_receipt.created is False
    assert composed.artifacts.provider_call_count == calls


def test_an_acl_only_change_creates_a_distinct_artifact_with_no_new_embedding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    admission = cast(Any, composed.stack.authority)
    from ne.contracts.integration import Record

    claim = asyncio.run(
        admission.claim_scope(
            drive_scope(
                deployment_id=admission.deployment_id,
                connector_instance_id=admission.connector_instance_id,
                graph_tenant_id=TENANT_ID,
                drive_id=DRIVE_ID,
            ),
            lease_seconds=60,
        )
    )
    admission.bind_drive_claim(DRIVE_ID, claim, site_id=SITE_ID)
    record = Record(
        record_id="item-user",
        source_id=f"{DRIVE_ID}:item-user",
        payload={"id": "item-user", "etag": "etag-1"},
    )
    first = asyncio.run(
        admission.prepare(
            record,
            acl=("u-ada",),
            canonical_bytes=b'{"a":1}',
            item_id="i-1",
            evidence=_read("item-user"),
        )
    )
    calls = composed.artifacts.provider_call_count
    widened = asyncio.run(
        admission.prepare(
            record,
            acl=("u-ada", "u-bob"),
            canonical_bytes=b'{"a":1}',
            item_id="i-1",
            evidence=_read("item-user", principals=("u-ada", "u-bob")),
        )
    )
    assert widened.artifact_identity.artifact_id != first.artifact_identity.artifact_id
    assert widened.acl_snapshot_hash != first.acl_snapshot_hash
    assert composed.artifacts.provider_call_count == calls


# -- the bounded rotating permission sweep -----------------------------------


def _live_settings(composed: AuthorityStack) -> Any:
    return composed.settings.model_copy(
        update={"content_allowed_hosts": ("contoso.sharepoint.com",)}
    )


def _as_of_by_item(engine: sa.Engine) -> dict[str, Any]:
    """The current head receipt's ``authority_as_of`` per admitted item."""
    heads = _heads(engine)
    receipts = {r.receipt_id: r for r in _receipts(engine)}
    return {
        _point(receipts[receipt_id]).item_id: receipts[receipt_id].authority_as_of
        for receipt_id in heads.values()
        if receipts[receipt_id].state is SourceAuthorityState.PRESENT
    }


def _swept_items(requests: list[Any]) -> list[str]:
    """Items the SWEEP re-fetched, identified by the bare driveItem metadata GET.

    Only ``fetch_point_record`` requests ``/drives/{d}/items/{id}`` with no
    suffix; the delta crawl never does. That makes this an exact discriminator
    between a swept point and a point the delta happened to carry — including
    when the sweep's turn lands on an item the delta also touched.
    """
    return [
        str(r.url).rsplit("/items/", 1)[-1]
        for r in requests
        if "/items/" in str(r.url) and "/" not in str(r.url).rsplit("/items/", 1)[-1]
    ]


def _permission_reads(requests: list[Any]) -> list[str]:
    return [
        str(r.url).rsplit("/items/", 1)[-1].removesuffix("/permissions")
        for r in requests
        if str(r.url).endswith("/permissions")
    ]


def test_a_quiet_point_absent_from_the_delta_is_actually_re_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The second run's delta carries one item; the quiet ones are still re-read.

    Their permission resources appear in the recorded Graph traffic of a run
    whose delta never mentioned them, which is the difference between a sweep
    and a claim that a sweep happened.
    """
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    _run(composed, transport=build_m365_transport(), settings=_live_settings(composed))

    requests: list[Any] = []
    _run(
        composed,
        transport=build_m365_transport(recorder=requests),
        settings=_live_settings(composed),
    )
    read = _permission_reads(requests)
    assert "item-user" in read  # the delta's own item
    for quiet in ("item-anon", "item-org", "item-everyone"):
        assert quiet in read, quiet


def test_a_re_read_quiet_point_gets_a_later_authority_as_of(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A genuine re-read refreshes freshness; the head moves to the new receipt.

    The pause is the SQLite clock, not the product: ``authority_as_of`` is the
    DB-authoritative instant, and SQLite's ``now()`` advances once per second, so
    two runs inside one second legitimately observe the SAME instant and are one
    observation. On the deployed Postgres the gap is microseconds.
    """
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    _run(composed, transport=build_m365_transport(), settings=_live_settings(composed))
    before = _as_of_by_item(composed.engine)

    time.sleep(1.1)
    _run(composed, transport=build_m365_transport(), settings=_live_settings(composed))
    after = _as_of_by_item(composed.engine)

    quiet = "item-anon"
    assert quiet in before and quiet in after
    assert after[quiet] > before[quiet]


def test_a_quiet_re_read_reuses_the_artifact_with_zero_further_provider_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Identical content and ACL select the SAME artifact and embed nothing again.

    Asserted as a provider CALL COUNT rather than as an absence of side effects:
    a cache that silently re-embedded would leave every other observable
    property unchanged.
    """
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    # Two runs to reach steady state: the backfill, then the incremental delta
    # that carries a CHANGED item-user. The third run's delta is empty, so every
    # Graph request and every provider call in it belongs to the sweep alone.
    _run(composed, transport=build_m365_transport(), settings=_live_settings(composed))
    _run(composed, transport=build_m365_transport(), settings=_live_settings(composed))
    artifacts_before = _artifact_ids(composed.engine)
    calls = composed.artifacts.provider_call_count

    requests: list[Any] = []
    _run(
        composed,
        transport=build_m365_transport(recorder=requests),
        settings=_live_settings(composed),
    )

    assert _permission_reads(requests), "the steady-state run must still re-read"
    quiet_after = _artifact_by_item(composed.engine, "item-anon")
    assert quiet_after in artifacts_before
    assert composed.artifacts.provider_call_count == calls


def _artifact_ids(engine: sa.Engine) -> set[str]:
    return {
        r.artifact_identity.artifact_id
        for r in _receipts(engine)
        if r.artifact_identity is not None
    }


def _artifact_by_item(engine: sa.Engine, item_id: str) -> str:
    heads = _heads(engine)
    receipts = {r.receipt_id: r for r in _receipts(engine)}
    for receipt_id in heads.values():
        receipt = receipts[receipt_id]
        if receipt.artifact_identity is not None and _point(receipt).item_id == item_id:
            return receipt.artifact_identity.artifact_id
    raise AssertionError(f"no current artifact for {item_id!r}")


def test_a_bounded_sweep_advances_through_its_points_across_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One point per run, in deterministic order, with no point taken twice.

    Each swept point is also proven to have had its permission resource read in
    the same run: a sweep that re-fetched content without re-reading permissions
    would refresh a point on stale access.
    """
    # batch_size 1 only stays feasible against the PT1H SLA with a smaller
    # admitted-point ceiling: ceil(10 / 1) * (120 + 60) = 1800 < 3600.
    _authority_env(
        monkeypatch,
        tmp_path,
        NE_M365_AUTHORITY_SWEEP_BATCH_SIZE="1",
        NE_M365_AUTHORITY_SWEEP_MAX_POINTS_PER_SCOPE="10",
    )
    composed = _compose(tmp_path)
    _run(composed, transport=build_m365_transport(), settings=_live_settings(composed))

    swept: list[str] = []
    for _ in range(3):
        requests: list[Any] = []
        _run(
            composed,
            transport=build_m365_transport(recorder=requests),
            settings=_live_settings(composed),
        )
        batch = _swept_items(requests)
        assert len(batch) == 1, batch
        assert batch[0] in _permission_reads(requests), batch
        swept.append(batch[0])
    assert len(set(swept)) == 3, swept


def test_a_point_outside_its_sla_refuses_the_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A scope the sweep could not keep inside its SLA stays Unknown."""
    from ne.connect.authority_admission import UnsweptPointError, ne_source_scope_points

    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    _run(composed, transport=build_m365_transport(), settings=_live_settings(composed))
    with composed.engine.begin() as conn:
        conn.execute(
            sa.update(ne_source_scope_points).values(
                last_swept_at=datetime(2020, 1, 1, tzinfo=UTC),
                last_swept_epoch=0,
            )
        )
    monkeypatch.setenv("NE_M365_AUTHORITY_SWEEP_BATCH_SIZE", "1")
    with pytest.raises(UnsweptPointError):
        _run(
            composed,
            transport=build_m365_transport(),
            settings=_live_settings(composed).model_copy(update={"authority_sweep_batch_size": 1}),
        )


def test_changed_content_follows_the_existing_immutable_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Changed content is a DISTINCT artifact and does pay one provider call."""
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    admission = cast(Any, composed.stack.authority)
    from ne.contracts.integration import Record

    claim = asyncio.run(
        admission.claim_scope(
            drive_scope(
                deployment_id=admission.deployment_id,
                connector_instance_id=admission.connector_instance_id,
                graph_tenant_id=TENANT_ID,
                drive_id=DRIVE_ID,
            ),
            lease_seconds=60,
        )
    )
    admission.bind_drive_claim(DRIVE_ID, claim, site_id=SITE_ID)
    record = Record(
        record_id="item-user",
        source_id=f"{DRIVE_ID}:item-user",
        payload={"id": "item-user", "etag": "etag-1"},
    )
    first = asyncio.run(
        admission.prepare(
            record,
            acl=("u-ada",),
            canonical_bytes=b'{"a":1}',
            item_id="i-1",
            evidence=_read("item-user"),
        )
    )
    calls = composed.artifacts.provider_call_count
    changed = asyncio.run(
        admission.prepare(
            record,
            acl=("u-ada",),
            canonical_bytes=b'{"a":2}',
            item_id="i-1",
            evidence=_read("item-user"),
        )
    )
    assert changed.artifact_identity.artifact_id != first.artifact_identity.artifact_id
    assert changed.create_receipt.created is True
    assert composed.artifacts.provider_call_count == calls + 1


def test_an_admission_cannot_ride_a_read_performed_for_another_point(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Evidence binds to its point AND its result; neither is assumed."""
    from ne.connect.authority_admission import AuthorityAdmissionError
    from ne.contracts.integration import Record

    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    admission = cast(Any, composed.stack.authority)
    claim = asyncio.run(
        admission.claim_scope(
            drive_scope(
                deployment_id=admission.deployment_id,
                connector_instance_id=admission.connector_instance_id,
                graph_tenant_id=TENANT_ID,
                drive_id=DRIVE_ID,
            ),
            lease_seconds=60,
        )
    )
    admission.bind_drive_claim(DRIVE_ID, claim, site_id=SITE_ID)
    record = Record(
        record_id="item-user",
        source_id=f"{DRIVE_ID}:item-user",
        payload={"id": "item-user", "etag": "etag-1"},
    )
    with pytest.raises(AuthorityAdmissionError):
        asyncio.run(
            admission.prepare(
                record,
                acl=("u-ada",),
                canonical_bytes=b"{}",
                item_id="i-1",
                evidence=_read("item-other"),
            )
        )
    with pytest.raises(AuthorityAdmissionError):
        asyncio.run(
            admission.prepare(
                record,
                acl=("u-ada", "u-bob"),
                canonical_bytes=b"{}",
                item_id="i-1",
                evidence=_read("item-user"),
            )
        )


def test_the_same_attempt_retried_reproduces_the_receipt_and_does_not_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Idempotency binds to ONE persisted attempt, not to wall time.

    Two ``prepare``/``finalize`` cycles inside the same claim are one attempt
    retried: they must produce the identical receipt, and must NOT look like a
    second observation.

    The two cycles deliberately straddle a clock tick. Without that pause an
    implementation that simply read the clock on every call would pass here on
    SQLite, whose ``now()`` advances once per second — the control would be
    satisfied by the coarseness of the test clock rather than by the attempt
    binding it exists to prove.
    """
    from ne.contracts.integration import Record

    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    admission = cast(Any, composed.stack.authority)
    claim = asyncio.run(
        admission.claim_scope(
            drive_scope(
                deployment_id=admission.deployment_id,
                connector_instance_id=admission.connector_instance_id,
                graph_tenant_id=TENANT_ID,
                drive_id=DRIVE_ID,
            ),
            lease_seconds=60,
        )
    )
    admission.bind_drive_claim(DRIVE_ID, claim, site_id=SITE_ID)
    record = Record(
        record_id="item-user",
        source_id=f"{DRIVE_ID}:item-user",
        payload={"id": "item-user", "etag": "etag-1"},
    )

    def _admit_once() -> Any:
        prepared = asyncio.run(
            admission.prepare(
                record,
                acl=("u-ada",),
                canonical_bytes=b'{"a":1}',
                item_id="i-1",
                evidence=_read("item-user"),
            )
        )
        return asyncio.run(admission.finalize(prepared))

    first = _admit_once()
    time.sleep(1.1)
    second = _admit_once()
    assert second.receipt_id == first.receipt_id
    assert second.receipt_hash == first.receipt_hash
    assert second.authority_as_of == first.authority_as_of


# -- a firing that lands on a scope its predecessor still holds ---------------


def _drive_scope_of(composed: AuthorityStack) -> Any:
    admission = cast(Any, composed.stack.authority)
    return drive_scope(
        deployment_id=admission.deployment_id,
        connector_instance_id=admission.connector_instance_id,
        graph_tenant_id=TENANT_ID,
        drive_id=DRIVE_ID,
    )


def test_a_firing_that_finds_the_scope_held_skips_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The overlapping-crawl case: no work, no error, and no claim of success.

    A crawl regularly outlives the cron interval that starts it, and a crawl
    killed by a restart leaves its lease standing for the rest of the window. Both
    put a firing in front of a scope another reconciler holds. Reporting that as a
    failed sync is what made a healthy connector look broken every two minutes.

    The lease here is taken through the writer's own governed ``claim`` — the same
    call the previous crawl would have made — not by planting a row.
    """
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    settings = _live_settings(composed)
    _run(composed, transport=build_m365_transport(), settings=settings)
    heads_before = _heads(composed.engine)
    receipts_before = len(_receipts(composed.engine))
    cursors_before = composed.state_store.load_state(m365_data_contract(settings).source)
    assert heads_before

    holder = SqlAlchemyCurrentSourceAuthorityWriter(
        composed.engine,
        claim_or_load_deployment_construction_id(composed.engine),
        freshness_sla_seconds=m365_contract_sla_seconds(settings),
    )
    scope = _drive_scope_of(composed)
    asyncio.run(
        holder.claim(
            scope,
            expected_head_revision=expected_scope_head_revision(composed.engine, scope),
            lease_seconds=600,
        )
    )

    outcome = _run(composed, transport=build_m365_transport(), settings=settings)

    assert outcome.scope_busy is True
    # Zeros that mean NOT ATTEMPTED, and nothing durable moved to contradict them:
    # a skipped firing must not be able to pass itself off as a successful empty one.
    assert outcome.report.records_seen == 0
    assert outcome.report.written == 0
    assert _heads(composed.engine) == heads_before
    assert len(_receipts(composed.engine)) == receipts_before
    assert composed.state_store.load_state(m365_data_contract(settings).source) == cursors_before


def test_a_conflict_that_is_not_a_busy_scope_still_fails_the_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The paired refusal: a WRONG view of authority is never skipped.

    Nothing holds the scope here — the claim is refused because the run presents a
    head revision the scope has moved past. That means this reconciler has not
    read the authority it is about to advance, so it must fail the run rather than
    quietly skip and let the next firing inherit the same wrong view.
    """
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    settings = _live_settings(composed)
    _run(composed, transport=build_m365_transport(), settings=settings)

    # Falsify only the caller's VIEW of the head; the writer's CAS is untouched.
    monkeypatch.setattr(
        "ne.connect.authority_admission.expected_scope_head_revision",
        lambda engine, scope: "a-head-this-scope-never-sat-at",
    )
    with pytest.raises(SourceAuthorityClaimConflictError) as raised:
        _run(composed, transport=build_m365_transport(), settings=settings)
    assert not isinstance(raised.value, SourceReconciliationScopeBusyError)
    assert "head revision" in str(raised.value)


# -- the lease is sized to the WORK, and a skip is not a health fact ----------


def _sync_outcomes(engine: sa.Engine) -> list[str]:
    """Every terminal ``connector.sync`` fact for this source, oldest first.

    This is the exact ledger ``ne.api.connect_status.sync_source_read_health``
    reads: a ``denied`` row, a never-succeeded lane, or a persisted run of
    non-success rows makes the connector unavailable, and
    ``ne.brain.source_health.chunk_source_is_healthy`` then withholds every chunk
    carrying that connector's stamp (a SHORT plain-``error`` run after a success
    is instead served as stale). What lands in this list decides whether the
    corpus behind the connector is visible at all.
    """
    with engine.connect() as conn:
        return [
            str(row.outcome)
            for row in conn.execute(
                sa.select(ne_audit_log.c.outcome)
                .where(
                    ne_audit_log.c.action == "connector.sync",
                    ne_audit_log.c.resource == f"connector://{_SOURCE}",
                )
                .order_by(ne_audit_log.c.seq)
            )
        ]


def _standing_lease(engine: sa.Engine, scope: Any) -> tuple[datetime, datetime]:
    """The scope's persisted ``(lease_expires_at, now)`` as the database sees them."""
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_source_reconciliation_claims.c.lease_expires_at).where(
                ne_source_reconciliation_claims.c.scope_hash == scope.scope_hash
            )
        ).one()
        now = conn.execute(sa.select(sa.func.now())).scalar_one()
    expires = row.lease_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if isinstance(now, str):  # sqlite returns a string from ``now()``
        now = datetime.fromisoformat(now).replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return expires, now


def test_a_skipped_firing_writes_no_sync_health_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sync that never reached the source states NOTHING about its health.

    This is the half that darkened the corpus. ``run_m365_sync`` skipping is not
    enough on its own: the claim is taken from INSIDE ``SyncRunner.run``, so by
    the time the skip is decided the shared runner has already written its
    terminal ``connector.sync`` event — and an ``error`` there is read by
    ``degraded_sync_sources`` as a degraded connector and withholds every chunk
    the connector stamped. Measured live 2026-08-27: seven such rows in twelve
    minutes, and /v1/brain/search returned zero m365 chunks for the whole window.

    So the ledger must be UNCHANGED across a skipped firing — not "changed to
    success", which would be the opposite lie (a firing that read nothing cannot
    report a clean read).
    """
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    settings = _live_settings(composed)
    _run(composed, transport=build_m365_transport(), settings=settings)
    before = _sync_outcomes(composed.engine)
    assert before and before[-1] == "success"

    holder = SqlAlchemyCurrentSourceAuthorityWriter(
        composed.engine,
        claim_or_load_deployment_construction_id(composed.engine),
        freshness_sla_seconds=m365_contract_sla_seconds(settings),
    )
    scope = _drive_scope_of(composed)
    asyncio.run(
        holder.claim(
            scope,
            expected_head_revision=expected_scope_head_revision(composed.engine, scope),
            lease_seconds=600,
        )
    )

    outcome = _run(composed, transport=build_m365_transport(), settings=settings)

    assert outcome.scope_busy is True
    assert _sync_outcomes(composed.engine) == before


def test_a_conflict_that_is_not_a_busy_scope_still_degrades_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The paired mutation: silence is ONLY for the busy scope.

    Withholding a genuinely unhealthy source's chunks is a real control and is
    not being weakened here. A conflict that means the caller's view of authority
    is WRONG still writes the ``error`` row into the ledger
    ``sync_source_read_health`` reads: a persisted run of them (or any
    ``denied``) withholds the connector's chunks, while a single lost race —
    whose WINNING writer already advanced the authority record, which every
    disclosure re-resolves against at read time — is served as stale rather
    than blacking out the corpus.
    """
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    settings = _live_settings(composed)
    _run(composed, transport=build_m365_transport(), settings=settings)
    before = _sync_outcomes(composed.engine)

    monkeypatch.setattr(
        "ne.connect.authority_admission.expected_scope_head_revision",
        lambda engine, scope: "a-head-this-scope-never-sat-at",
    )
    with pytest.raises(SourceAuthorityClaimConflictError):
        _run(composed, transport=build_m365_transport(), settings=settings)

    after = _sync_outcomes(composed.engine)
    assert after == [*before, "error"]


def test_a_completed_sync_releases_its_lease_so_the_next_firing_claims_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Release is not best-effort: a finished crawl frees the scope immediately.

    The CAS is on epoch AND fencing token, and both are the ones ``claim``
    granted this run — so on a normal completion it always wins, and the standing
    lease collapses to the release instant rather than running its window out.
    Proven by taking the scope straight afterwards through the governed writer:
    the next sweep gets it, with no waiting.
    """
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    settings = _live_settings(composed)
    _run(composed, transport=build_m365_transport(), settings=settings)

    scope = _drive_scope_of(composed)
    expires, now = _standing_lease(composed.engine, scope)
    assert expires <= now

    holder = SqlAlchemyCurrentSourceAuthorityWriter(
        composed.engine,
        claim_or_load_deployment_construction_id(composed.engine),
        freshness_sla_seconds=m365_contract_sla_seconds(settings),
    )
    claim = asyncio.run(
        holder.claim(
            scope,
            expected_head_revision=expected_scope_head_revision(composed.engine, scope),
            lease_seconds=600,
        )
    )
    assert claim.reconciliation_epoch > 1


def test_an_abandoned_crawl_gives_the_scope_up_within_two_sweep_cadences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The lease's OTHER job is a dead-man switch, and it is sized as one.

    A worker killed mid-crawl never reaches its ``finally``, so the only thing
    that frees the scope is the lease lapsing on wall-clock. That window is
    therefore the cost of every crash, and it is bounded here against the cadence
    that decides how many firings fall into it: at 900s against a ``*/2`` cron it
    cost seven consecutive skipped firings; within two cadences it costs at most
    one.

    Read off the lease the PRODUCTION driver actually takes, not off the constant.
    """
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    settings = _live_settings(composed)
    admission = cast(Any, composed.stack.authority)
    driver = _M365AuthorityDriver(
        admission,
        engine=admission.engine,
        settings=settings,
        deployment_id=admission.deployment_id,
        connector_instance_id=admission.connector_instance_id,
    )

    async def _claim_then_abandon() -> None:
        await driver.before_site_discovery(SITE_ID)
        # What a killed process does: the heartbeat stops and NOTHING releases.
        await driver._stop_renewals()

    asyncio.run(_claim_then_abandon())

    scope = site_scope(
        deployment_id=admission.deployment_id,
        connector_instance_id=admission.connector_instance_id,
        graph_tenant_id=TENANT_ID,
        site_id=SITE_ID,
    )
    expires, now = _standing_lease(composed.engine, scope)
    cadence = Decimal(settings.authority_sweep_cadence_seconds)
    assert expires > now, "an abandoned lease must still hold the scope for a while"
    assert (expires - now).total_seconds() <= float(cadence) * 2


def test_a_crawl_that_outlives_its_lease_keeps_the_scope_by_renewing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A LIVE crawl holds the scope by proving liveness, not by a long lease.

    This is what lets the dead-man window be short. The crawl here runs well past
    the lease it was granted; a competing reconciler is still refused the scope,
    and refused as a distinguishable BUSY scope rather than as a stale claim. The
    moment the crawl releases, the competitor gets it — the paired mutation, and
    the one that decides whether the next sweep has to wait.
    """
    _authority_env(monkeypatch, tmp_path)
    composed = _compose(tmp_path)
    settings = _live_settings(composed)
    # Margins, not milliseconds. The PROPERTY under test is "the crawl outlives
    # its lease and still holds the scope, because it renews" -- it does not
    # depend on the lease being short in absolute terms, only on the sleep below
    # exceeding it. The original 1s lease / 0.2s renew / 1.8s sleep left the
    # renewal task ~0.8s of slack, and on a loaded shared CI runner that task got
    # starved past the lease: the lease genuinely expired, the competing claim
    # genuinely succeeded, and the test failed as "DID NOT RAISE" -- a real
    # outcome of a margin too tight to survive the hardware, not a real defect.
    # Scaled 3x: same assertions, same ordering, ~3.5s of slack instead of 0.8s.
    monkeypatch.setattr("ne.connect.m365.sync._AUTHORITY_LEASE_SECONDS", 3)
    monkeypatch.setattr("ne.connect.m365.sync._AUTHORITY_LEASE_RENEW_SECONDS", 0.5)
    admission = cast(Any, composed.stack.authority)
    driver = _M365AuthorityDriver(
        admission,
        engine=admission.engine,
        settings=settings,
        deployment_id=admission.deployment_id,
        connector_instance_id=admission.connector_instance_id,
    )
    scope = site_scope(
        deployment_id=admission.deployment_id,
        connector_instance_id=admission.connector_instance_id,
        graph_tenant_id=TENANT_ID,
        site_id=SITE_ID,
    )
    holder = SqlAlchemyCurrentSourceAuthorityWriter(
        composed.engine,
        claim_or_load_deployment_construction_id(composed.engine),
        freshness_sla_seconds=m365_contract_sla_seconds(settings),
    )

    async def _crawl_long_then_release() -> None:
        await driver.before_site_discovery(SITE_ID)
        await asyncio.sleep(6.5)  # well past the 3s lease this claim was granted
        with pytest.raises(SourceReconciliationScopeBusyError):
            await holder.claim(
                scope,
                expected_head_revision=expected_scope_head_revision(composed.engine, scope),
                lease_seconds=3,
            )
        await driver.release()
        # Released, so the next firing takes it with no wait at all.
        await holder.claim(
            scope,
            expected_head_revision=expected_scope_head_revision(composed.engine, scope),
            lease_seconds=3,
        )

    asyncio.run(_crawl_long_then_release())
