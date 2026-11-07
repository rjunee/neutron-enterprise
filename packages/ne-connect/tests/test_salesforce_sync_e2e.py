# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Full Salesforce pipeline over REAL components (NE-047) — no mock past the seam.

recorded Bulk backfill+ObjectPermissions crawl -> SalesforceConnector ->
SyncRunner -> KnowledgeWriter -> QdrantChunkStore(:memory:) + real sqlite audit.
Proves ACL-stamped chunks, exactly-one config_hash-stamped connector.sync event,
the acl_quarantine denied event naming both quarantined objects,
SystemModstamp-cursor persist/advance-only-on-success, an incremental second
run, and mid-run-failure resumability with zero duplicates.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.brain.current_source_authority import (
    ne_source_authority_receipts,
    ne_source_reconciliation_claims,
)
from ne.brain.testing.embedding import HashEmbedder
from ne.connect import ConnectorStateStore
from ne.connect.authority_admission import AuthorityAdmissionError
from ne.connect.errors import SalesforceSyncError
from ne.connect.runner import ConnectSyncSettings, build_ingest_stack
from ne.connect.salesforce import PERMSET_PREFIX, PROFILE_PREFIX, run_salesforce_sync
from ne.connect.salesforce.config import SalesforceObjectConfig
from ne.connect.salesforce.contract import (
    salesforce_admitted_target_digest,
    salesforce_contract_identity,
    salesforce_data_contract,
    salesforce_source_link_mapper_revision,
)
from ne.connect.salesforce.sync import make_salesforce_record_mapper
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditSink
from ne.contracts.integration import Record
from ne.schema import AuditEvent

from .conftest import WriterStack
from .salesforce_transport import (
    OBJ_ACCOUNT,
    OBJ_FORGED,
    OBJ_NOPERM,
    OBJ_OPP,
    ORGANIZATION_ID,
    PERMSET_ID,
    PROFILE_ID,
    build_salesforce_transport,
    salesforce_settings,
)

_SOURCE = "salesforce"


def _authority_settings(**overrides: object):  # type: ignore[no-untyped-def]
    return salesforce_settings(
        authority_enabled=True,
        authority_sweep_cadence_seconds=60,
        authority_sweep_batch_size=10,
        authority_sweep_max_batch_duration_seconds=5,
        authority_sweep_max_points_per_scope=100,
        **overrides,
    )


def _authority_stack(
    stack: WriterStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: object,
):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "ne.connect.runner.build_sync_settings",
        lambda source, **_: settings if source == "salesforce" else None,
    )
    return build_ingest_stack(
        chunk_store=stack.store,
        runtime_engine=stack.engine,
        sink=stack.sink,
        settings=ConnectSyncSettings(
            sources=("salesforce",),
            state_path=str(tmp_path / "authority-state.db"),
            quarantine_path=str(tmp_path / "authority-quarantine.db"),
        ),
        embedder=HashEmbedder(32),
    )


class _FailConnectorSuccessAudit:
    def __init__(self, delegate: AuditSink) -> None:
        self._delegate = delegate

    async def append(self, event: AuditEventLike) -> None:
        assert isinstance(event, AuditEvent)
        if event.action == "connector.sync" and str(event.outcome) == "success":
            raise RuntimeError("terminal audit failed")
        await self._delegate.append(event)


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


def _audit_rows(stack: WriterStack) -> list[dict[str, object]]:
    with stack.engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(sa.select(ne_audit_log).order_by(ne_audit_log.c.id)).mappings()
        ]


def _events(stack: WriterStack, action: str) -> list[dict[str, object]]:
    return [r for r in _audit_rows(stack) if r["action"] == action]


def test_salesforce_authority_factory_binds_provider_org_and_claims_before_read(
    writer_stack: WriterStack,
    frozen_config: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del frozen_config
    settings = _authority_settings()
    stack = _authority_stack(writer_stack, tmp_path, monkeypatch, settings)
    factory = stack.authority_for("salesforce")
    assert factory is not None
    assert stack.authority is None
    expected_target = salesforce_admitted_target_digest(settings, ORGANIZATION_ID)
    with pytest.raises(AuthorityAdmissionError, match="foreign admitted target"):
        factory.create(
            organization_id="00D000000000002EAA",
            admitted_target_digest=expected_target,
        )
    observed_claim_counts: list[int] = []

    def observe(request: httpx.Request) -> None:
        if request.url.path.endswith("/query") and "ObjectPermissions" in request.url.params.get(
            "q", ""
        ):
            # The durable target is already bound and every object scope is already
            # claimed before the first permissions/content request.
            target_row = (
                cast(Any, stack.state_store)
                ._conn.execute(
                    "SELECT target_digest FROM connector_target_bindings WHERE source = ?",
                    (_SOURCE,),
                )
                .fetchone()
            )
            assert target_row is not None and target_row[0] == expected_target
            with writer_stack.engine.connect() as conn:
                observed_claim_counts.append(
                    int(
                        conn.execute(
                            sa.select(sa.func.count()).select_from(ne_source_reconciliation_claims)
                        ).scalar_one()
                    )
                )

    outcome = asyncio.run(
        run_salesforce_sync(
            settings,
            writer=stack.writer,
            state_store=stack.state_store,
            audit_sink=writer_stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(request_observer=observe),
            authority=factory,
        )
    )
    assert outcome.report.written == 5
    assert observed_claim_counts == [len(settings.objects)]
    with writer_stack.engine.connect() as conn:
        payloads = conn.execute(sa.select(ne_source_authority_receipts.c.payload)).scalars().all()
    assert len(payloads) == 5
    expected_contract = salesforce_contract_identity(settings, ORGANIZATION_ID).model_dump(
        mode="json"
    )
    assert all(
        payload["source_identity"]["point"]["organization_id"] == ORGANIZATION_ID
        and payload["source_contract"] == expected_contract
        for payload in payloads
    )


def test_salesforce_authority_enabled_without_factory_refuses_before_provider_io(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    del frozen_config
    requests: list[httpx.Request] = []

    with pytest.raises(SalesforceSyncError, match="producer is unavailable"):
        asyncio.run(
            run_salesforce_sync(
                _authority_settings(),
                writer=writer_stack.writer,
                state_store=ConnectorStateStore(":memory:"),
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_salesforce_transport(request_observer=requests.append),
            )
        )

    assert requests == []


def test_salesforce_authority_failure_releases_claims_and_restart_reclaims(
    writer_stack: WriterStack,
    frozen_config: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del frozen_config
    settings = _authority_settings()
    stack = _authority_stack(writer_stack, tmp_path, monkeypatch, settings)
    factory = stack.authority_for("salesforce")
    assert factory is not None

    with pytest.raises(SalesforceSyncError):
        asyncio.run(
            run_salesforce_sync(
                settings,
                writer=stack.writer,
                state_store=stack.state_store,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_salesforce_transport(fail_after=1),
                authority=factory,
            )
        )

    retry = asyncio.run(
        run_salesforce_sync(
            settings,
            writer=stack.writer,
            state_store=stack.state_store,
            audit_sink=writer_stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(),
            authority=factory,
        )
    )
    assert retry.report.written == 5

    restarted_stack = _authority_stack(writer_stack, tmp_path, monkeypatch, settings)
    restarted_factory = restarted_stack.authority_for("salesforce")
    assert restarted_factory is not None
    assert restarted_factory is not factory
    restarted = asyncio.run(
        run_salesforce_sync(
            settings,
            writer=restarted_stack.writer,
            state_store=restarted_stack.state_store,
            audit_sink=writer_stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(incremental=True),
            authority=restarted_factory,
        )
    )
    assert restarted.mode == "incremental"
    assert restarted.report.written == 1


def test_target_binding_digest_is_canonical_and_credential_free() -> None:
    first = salesforce_settings(
        client_id="client-a",
        client_secret="secret-a",
        objects=tuple(reversed(salesforce_settings().objects)),
        instance_allowed_hosts=(".my.salesforce.com",),
        login_allowed_hosts=("login.salesforce.com",),
        source_link_allowed_hosts=("source-a.example",),
    )
    reordered_with_other_credentials = salesforce_settings(
        client_id="client-b",
        client_secret="secret-b",
        instance_url="https://ACME.MY.SALESFORCE.COM/",
        login_base_url="https://LOGIN.SALESFORCE.COM/",
        instance_allowed_hosts=("acme.my.salesforce.com",),
        login_allowed_hosts=("login.salesforce.com", "other-login.example"),
        source_link_allowed_hosts=("other-source.example",),
    )
    digest = salesforce_admitted_target_digest(first, ORGANIZATION_ID)
    assert digest == salesforce_admitted_target_digest(
        reordered_with_other_credentials, ORGANIZATION_ID
    )

    base = salesforce_settings()
    account = base.objects[0]
    changed_account = account.model_copy(update={"content_fields": ("Name",)})
    for changed in (
        salesforce_settings(source_name="other-source"),
        salesforce_settings(instance_url="https://other.my.salesforce.com"),
        salesforce_settings(login_base_url="https://test.salesforce.com"),
        salesforce_settings(api_version="v64.0"),
        salesforce_settings(objects=(changed_account, *base.objects[1:])),
        salesforce_settings(objects=base.objects[:-1]),
    ):
        assert digest != salesforce_admitted_target_digest(changed, ORGANIZATION_ID)
    assert digest != salesforce_admitted_target_digest(first, "00D000000000002EAA")


def test_target_mismatch_and_legacy_state_fail_before_provider_io(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    del frozen_config

    async def expect_no_io(state_store: ConnectorStateStore, error: str) -> None:
        requests: list[httpx.Request] = []
        with pytest.raises(RuntimeError, match=error):
            await run_salesforce_sync(
                salesforce_settings(instance_url="https://other.my.salesforce.com"),
                writer=writer_stack.writer,
                state_store=state_store,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_salesforce_transport(recorder=requests),
            )
        assert [request.url.path for request in requests] == ["/services/oauth2/token"]

    bound = ConnectorStateStore()
    bound.claim_target_binding(
        _SOURCE,
        salesforce_admitted_target_digest(salesforce_settings(), ORGANIZATION_ID),
    )
    asyncio.run(expect_no_io(bound, "does not match its binding"))
    bound.close()

    legacy = ConnectorStateStore()
    legacy.set_cursor(_SOURCE, f"modstamp:{OBJ_ACCOUNT}", "2026-01-03T00:00:00.000Z")
    asyncio.run(expect_no_io(legacy, "non-empty and has no target binding"))
    legacy.close()


def test_provider_organization_change_refuses_existing_binding(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    del frozen_config
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        await run_salesforce_sync(
            salesforce_settings(),
            writer=writer_stack.writer,
            state_store=state_store,
            audit_sink=writer_stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(),
        )
        with pytest.raises(RuntimeError, match="does not match its binding"):
            await run_salesforce_sync(
                salesforce_settings(),
                writer=writer_stack.writer,
                state_store=state_store,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_salesforce_transport(organization_id="00D000000000002EAA"),
            )

    asyncio.run(scenario())
    state_store.close()


async def _acl_of(stack: WriterStack, source_id: str, record_id: str) -> tuple[str, ...]:
    chunks = await stack.store.get([_item_id(source_id, record_id)])
    assert len(chunks) == 1
    return chunks[0].acl


def test_backfill_stamps_acls_and_emits_single_sync_event(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()
    transport = build_salesforce_transport()

    async def scenario() -> None:
        outcome = await run_salesforce_sync(
            salesforce_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=transport,
        )
        assert outcome.mode == "backfill"
        assert outcome.report.written == 5
        assert outcome.acl_quarantined == 2

        # (a) chunks stamped exactly as the ObjectPermissions fixture grants.
        assert await _acl_of(stack, OBJ_ACCOUNT, "001000000000001AAA") == (
            f"{PROFILE_PREFIX}{PROFILE_ID}",
            f"{PERMSET_PREFIX}{PERMSET_ID}",
        )
        assert await _acl_of(stack, OBJ_OPP, "006000000000001AAA") == (
            f"{PERMSET_PREFIX}{PERMSET_ID}",
        )
        account_chunk = (await stack.store.get([_item_id(OBJ_ACCOUNT, "001000000000001AAA")]))[0]
        assert account_chunk.metadata["source_url"] == (
            "https://acme.my.salesforce.com/lightning/r/Account/001000000000001AAA/view"
        )

        # (b) exactly one connector.sync event, config_hash-stamped, chain valid.
        sync_events = _events(stack, "connector.sync")
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        assert sync_events[0]["outcome"] == "success"
        verification = await stack.sink.verify()
        assert verification.valid

        # (c) one acl_quarantine denied event naming both quarantined objects.
        quarantine_events = _events(stack, "connector.acl_quarantine")
        assert len(quarantine_events) == 1
        assert quarantine_events[0]["outcome"] == "denied"
        payload = quarantine_events[0]["payload"]
        assert isinstance(payload, dict)
        detail = str(payload["detail"])
        assert "quarantined=2" in detail
        for name in (OBJ_NOPERM, OBJ_FORGED):
            assert name in detail

        # (d) modstamp cursors persisted for the 2 surviving objects.
        assert state_store.load_state(_SOURCE) == {
            f"modstamp:{OBJ_ACCOUNT}": "2026-01-03T00:00:00.000Z",
            f"modstamp:{OBJ_OPP}": "2026-01-02T00:00:00.000Z",
        }
        assert dict(outcome.report.cursors) == state_store.load_state(_SOURCE)

    asyncio.run(scenario())


def test_source_link_authority_revision_reprocesses_identical_salesforce_payload(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()
    account_id = _item_id(OBJ_ACCOUNT, "001000000000001AAA")

    async def run_with(hosts: tuple[str, ...]):  # type: ignore[no-untyped-def]
        return await run_salesforce_sync(
            salesforce_settings(source_link_allowed_hosts=hosts),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(),
        )

    async def scenario() -> None:
        disabled = await run_with(())
        assert disabled.mode == "backfill"
        assert disabled.report.written == 5
        disabled_revision = state_store.load_mapper_revision(_SOURCE)
        assert disabled_revision is not None
        [first] = await stack.store.get([account_id])
        assert "source_url" not in first.metadata

        enabled = await run_with((".my.salesforce.com",))
        assert enabled.mode == "backfill"
        assert enabled.report.written == 5
        enabled_revision = state_store.load_mapper_revision(_SOURCE)
        assert enabled_revision is not None and enabled_revision != disabled_revision
        [after_enable] = await stack.store.get([account_id])
        assert after_enable.metadata["source_url"] == (
            "https://acme.my.salesforce.com/lightning/r/Account/001000000000001AAA/view"
        )

        revoked = await run_salesforce_sync(
            salesforce_settings(source_link_allowed_hosts=()),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(),
        )
        assert revoked.mode == "backfill"
        assert revoked.report.written == 5
        assert state_store.load_mapper_revision(_SOURCE) == disabled_revision
        [after_revoke] = await stack.store.get([account_id])
        assert after_revoke.chunk_id == first.chunk_id
        assert after_revoke.text == first.text
        # Revoking the presentation authority reprocesses the exact bound crawl
        # target and removes its obsolete external locator.
        assert "source_url" not in after_revoke.metadata

        unchanged = await run_salesforce_sync(
            salesforce_settings(source_link_allowed_hosts=()),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(),
        )
        assert unchanged.mode == "incremental"
        assert unchanged.report.written == 0
        assert unchanged.report.records_seen == 2
        assert unchanged.report.skipped_unchanged == 2

    asyncio.run(scenario())


def test_terminal_audit_failure_retry_keeps_valid_salesforce_source_link(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()
    settings = salesforce_settings()
    account_id = _item_id(OBJ_ACCOUNT, "001000000000001AAA")

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="terminal audit failed"):
            await run_salesforce_sync(
                settings,
                writer=stack.writer,
                state_store=state_store,
                audit_sink=cast(AuditSink, _FailConnectorSuccessAudit(stack.sink)),
                actor="connector:test",
                transport=build_salesforce_transport(),
            )
        assert state_store.load_mapper_revision(_SOURCE) is None

        retry = await run_salesforce_sync(
            settings,
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(),
        )
        assert retry.mode == "backfill"
        [chunk] = await stack.store.get([account_id])
        assert chunk.metadata["source_url"] == (
            "https://acme.my.salesforce.com/lightning/r/Account/001000000000001AAA/view"
        )

    asyncio.run(scenario())


def test_salesforce_mapper_omits_link_for_invalid_identity_or_origin() -> None:
    settings = salesforce_settings()
    contract = salesforce_data_contract(settings)
    invalid_id = Record(
        record_id="not-a-salesforce-id",
        source_id=OBJ_ACCOUNT,
        payload={"Name": "Still governed", "Description": "No unsafe locator"},
    )
    item = make_salesforce_record_mapper(settings)(invalid_id, contract, ("group:finance",))
    assert item.content == "Still governed\n\nNo unsafe locator"
    assert item.acl == ("group:finance",)
    assert "source_url" not in item.metadata

    hostile_settings = salesforce_settings(instance_url="https://evil.example")
    hostile_contract = salesforce_data_contract(hostile_settings)
    valid_id = invalid_id.model_copy(update={"record_id": "001000000000001AAA"})
    hostile_item = make_salesforce_record_mapper(hostile_settings)(
        valid_id, hostile_contract, ("group:finance",)
    )
    assert "source_url" not in hostile_item.metadata


def test_salesforce_mapper_revision_binds_exact_valid_or_invalid_origin_decision() -> None:
    valid = salesforce_settings()
    valid_revision = salesforce_source_link_mapper_revision(valid)
    record = Record(
        record_id="001000000000001AAA",
        source_id=OBJ_ACCOUNT,
        payload={"Name": "Governed account", "Description": "Same payload"},
    )
    contract = salesforce_data_contract(valid)
    assert "source_url" in make_salesforce_record_mapper(valid)(record, contract, ()).metadata

    invalid_revisions: set[str] = set()
    for instance_url in (
        "https://acme.my.salesforce.com/path",
        "https://acme.my.salesforce.com?redirect=elsewhere",
        "https://acme.my.salesforce.com#fragment",
        "https://user:secret@acme.my.salesforce.com",
        "https://ac\tme.my.salesforce.com",
        "https://ac\nme.my.salesforce.com",
        "https://ac\rme.my.salesforce.com",
    ):
        invalid = valid.model_copy(update={"instance_url": instance_url})
        revision = salesforce_source_link_mapper_revision(invalid)
        invalid_revisions.add(revision)
        assert revision != valid_revision
        item = make_salesforce_record_mapper(invalid)(record, salesforce_data_contract(invalid), ())
        assert "source_url" not in item.metadata
        assert "secret" not in revision

    # Every rejected shape maps to one explicit non-secret invalid decision;
    # clean <-> invalid always moves the durable run-scope revision.
    assert len(invalid_revisions) == 1


def test_salesforce_mapper_never_accepts_source_row_url_override() -> None:
    settings = salesforce_settings(
        objects=(
            SalesforceObjectConfig(
                name=OBJ_ACCOUNT,
                content_fields=("Name",),
                metadata_fields=("source_url",),
            ),
        )
    )
    record = Record(
        record_id="001000000000001AAA",
        source_id=OBJ_ACCOUNT,
        payload={
            "Name": "Governed account",
            "source_url": "https://evil.example/credential?token=secret",
        },
    )
    item = make_salesforce_record_mapper(settings)(
        record, salesforce_data_contract(settings), ("group:finance",)
    )
    assert item.metadata["source_url"] == (
        "https://acme.my.salesforce.com/lightning/r/Account/001000000000001AAA/view"
    )
    assert "evil.example" not in str(item.metadata)
    assert "token=secret" not in str(item.metadata)

    hostile_settings = settings.model_copy(update={"instance_url": "https://evil.example"})
    hostile_item = make_salesforce_record_mapper(hostile_settings)(
        record, salesforce_data_contract(hostile_settings), ("group:finance",)
    )
    assert "source_url" not in hostile_item.metadata
    assert "evil.example" not in str(hostile_item.metadata)
    assert "token=secret" not in str(hostile_item.metadata)


def test_second_run_is_incremental_and_writes_only_the_new_record(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        first = await run_salesforce_sync(
            salesforce_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(),
        )
        assert first.report.written == 5

        second = await run_salesforce_sync(
            salesforce_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(incremental=True),
        )
        assert second.mode == "incremental"
        assert second.report.written == 1  # only the new Account record
        assert (
            state_store.load_state(_SOURCE)[f"modstamp:{OBJ_ACCOUNT}"] == "2026-01-10T00:00:00.000Z"
        )
        chunk = (await stack.store.get([_item_id(OBJ_ACCOUNT, "001000000000004AAA")]))[0]
        assert "New Startup" in chunk.text

    asyncio.run(scenario())


def test_mid_run_failure_does_not_advance_cursor_and_rerun_has_no_duplicates(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        # Run 1: the ObjectPermissions query 500s BEFORE any object is even
        # resolved — the run aborts BEFORE any cursor commits.
        with pytest.raises(SalesforceSyncError):
            await run_salesforce_sync(
                salesforce_settings(),
                writer=stack.writer,
                state_store=state_store,
                audit_sink=stack.sink,
                actor="connector:test",
                transport=build_salesforce_transport(fail_after=1),
            )
        assert state_store.load_state(_SOURCE) == {}  # cursor NOT advanced

        # Run 2 (retry, clean): completes the backfill and writes every item once.
        retry = await run_salesforce_sync(
            salesforce_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(),
        )
        assert retry.report.written == 5
        assert state_store.load_state(_SOURCE) == {
            f"modstamp:{OBJ_ACCOUNT}": "2026-01-03T00:00:00.000Z",
            f"modstamp:{OBJ_OPP}": "2026-01-02T00:00:00.000Z",
        }
        committed_revision = state_store.load_mapper_revision(_SOURCE)
        assert committed_revision is not None

        with pytest.raises(SalesforceSyncError):
            await run_salesforce_sync(
                salesforce_settings(source_link_allowed_hosts=()),
                writer=stack.writer,
                state_store=state_store,
                audit_sink=stack.sink,
                actor="connector:test",
                transport=build_salesforce_transport(fail_after=1),
            )
        assert state_store.load_mapper_revision(_SOURCE) == committed_revision
        assert state_store.load_state(_SOURCE) == {
            f"modstamp:{OBJ_ACCOUNT}": "2026-01-03T00:00:00.000Z",
            f"modstamp:{OBJ_OPP}": "2026-01-02T00:00:00.000Z",
        }

        refreshed = await run_salesforce_sync(
            salesforce_settings(source_link_allowed_hosts=()),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(),
        )
        assert refreshed.mode == "backfill"
        assert refreshed.report.written == 5
        assert state_store.load_mapper_revision(_SOURCE) != committed_revision

        # Run 3 (incremental) writes the one new record.
        third = await run_salesforce_sync(
            salesforce_settings(source_link_allowed_hosts=()),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(incremental=True),
        )
        assert third.report.written == 1

        # Rewind the Account ledger to before the incremental record and
        # re-run: the same content is now dedup-skipped — ZERO duplicate writes.
        state_store.set_cursor(_SOURCE, f"modstamp:{OBJ_ACCOUNT}", "2026-01-03T00:00:00.000Z")
        fourth = await run_salesforce_sync(
            salesforce_settings(source_link_allowed_hosts=()),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_salesforce_transport(incremental=True),
        )
        assert fourth.report.written == 0
        assert fourth.report.skipped_unchanged == 1

        # The 5 backfilled + 1 incremental records exist exactly once each.
        for source_id, record_id in (
            (OBJ_ACCOUNT, "001000000000001AAA"),
            (OBJ_ACCOUNT, "001000000000002AAA"),
            (OBJ_ACCOUNT, "001000000000003AAA"),
            (OBJ_ACCOUNT, "001000000000004AAA"),
            (OBJ_OPP, "006000000000001AAA"),
            (OBJ_OPP, "006000000000002AAA"),
        ):
            assert len(await stack.store.get([_item_id(source_id, record_id)])) == 1

    asyncio.run(scenario())
