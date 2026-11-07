# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-INGEST (P1-4) — the `ne connect sync` CLI contract.

The no-source path is a clean exit-0 no-op that NEVER composes the platform; a
configured fixture source runs over an injected sqlite/stub platform (written=3 then
dedup-idempotent skipped_unchanged=3); a named-unconfigured source exits 1 with a
'not configured' message; an unknown source exits 1 listing the valid sources.
"""

from __future__ import annotations

import asyncio
import importlib
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from click.testing import CliRunner

# The ``connect`` group name shadows the ``ne.cli.connect`` submodule attribute on
# the ne.cli package (the audit.py/checks.py precedent), so grab the real module via
# importlib to monkeypatch its ``_build_platform`` seam.
connect_cli = importlib.import_module("ne.cli.connect")
connect = connect_cli.connect


def _sqlite_platform(
    tmp_path: Path,
    *,
    protected_transport: bool = False,
    monkeypatch: pytest.MonkeyPatch | None = None,
):  # type: ignore[no-untyped-def]
    """Compose a REAL governed platform over sqlite + stub edges (self-contained)."""
    from ne.api.platform import AppSeams, build_platform
    from ne.api.settings import ApiSettings
    from ne.audit.config import AuditSettings
    from ne.contracts import reset_config_hash_for_testing
    from ne.contracts.plugins import PluginRegistry
    from ne.contracts.types import Lane
    from ne.gateway.config import GatewaySettings, ModelDef
    from ne.identity.config import IdentitySettings, OidcConfig
    from ne.permissions.config import PermissionsSettings
    from ne.runtime.config import RuntimeSettings
    from ne.worker.testing import (
        ISSUER,
        RegoFaithfulPdpFactory,
        StubChunkStore,
        StubEmbedder,
        StubSparseEncoder,
    )

    reset_config_hash_for_testing()
    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    key = uuid.uuid4().hex
    if protected_transport:
        assert monkeypatch is not None
        monkeypatch.setenv("NE_AGENT_PRIVATE_PAYLOAD_KEY_ID", "connect-cli-k1")
        monkeypatch.setenv(
            "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64",
            "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s",
        )
        monkeypatch.setenv("NE_AGENT_TRANSPORT_GENERATION", "agent-transport-v1")
    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-cli"),
            principal_claims=("email", "department"),
        ),
        runtime_settings=RuntimeSettings(session_database_url=f"sqlite:///{tmp_path}/rt-{key}.db"),
        audit_settings=AuditSettings(database_url=f"sqlite:///{tmp_path}/au-{key}.db"),
        permissions_settings=PermissionsSettings(database_url=f"sqlite:///{tmp_path}/pm-{key}.db"),
        gateway_settings=GatewaySettings(
            models=[
                ModelDef(
                    name="stub",
                    litellm_model="openai/stub",
                    provider="stub",
                    lane=Lane.FRONTIER_API,
                    context_window=32768,
                )
            ]
        ),
        embedder=StubEmbedder(),
        sparse_encoder=StubSparseEncoder(),
        chunk_store=StubChunkStore(),
        plugin_registry=registry,
        agent_private_payload_configured=protected_transport,
    )
    return build_platform(
        ApiSettings(
            qdrant_url="http://qdrant.test", qdrant_collection="t", tei_url="http://tei.test"
        ),
        seams,
    )


_FIXTURE_JSON = (
    '{"source": "acme-crm", '
    '"objects": [{"kind": "resource", "name": "contacts", "source_id": "acme-crm/contacts"}], '
    '"records": ['
    '{"cursor": "2026-01-01T00:00:00Z", "payload": {"name": "Ada"}, "record_id": "c1", '
    '"source_id": "acme-crm/contacts"}, '
    '{"cursor": "2026-01-02T00:00:00Z", "payload": {"name": "Grace"}, "record_id": "c2", '
    '"source_id": "acme-crm/contacts"}, '
    '{"cursor": "2026-01-03T00:00:00Z", "payload": {"name": "Kay"}, "record_id": "c3", '
    '"source_id": "acme-crm/contacts"}], '
    '"acl": [{"principals": ["oidc:sales"], "resource_id": "acme-crm/contacts"}]}'
)


def _fixture_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(_FIXTURE_JSON, encoding="utf-8")
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "q.db"))
    monkeypatch.setenv("NE_CONNECT_FIXTURE_PATH", str(fixture_path))
    monkeypatch.setenv("NE_CONNECT_FIXTURE_OWNER", "data-eng@acme.test")


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "NE_CONNECT_SYNC_SOURCES",
        "NE_CONNECT_SYNC_STATE_PATH",
        "NE_CONNECT_SYNC_QUARANTINE_PATH",
        "NE_CONNECT_FIXTURE_PATH",
        "NE_CONNECT_FIXTURE_OWNER",
    ):
        monkeypatch.delenv(var, raising=False)


# --- (t1) no sources ⇒ clean no-op, platform NEVER composed -----------------


def test_sync_no_sources_is_clean_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)

    def _boom() -> object:
        raise AssertionError("platform must not be composed")

    monkeypatch.setattr(connect_cli, "_build_platform", _boom)
    result = CliRunner().invoke(connect, ["sync"])
    assert result.exit_code == 0, result.output
    assert "no connector sources configured" in result.output


# --- (t2) fixture source: writes 3, re-run is dedup-idempotent --------------


def test_sync_fixture_source_writes_via_governed_writer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch)
    _fixture_env(monkeypatch, tmp_path)
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: _sqlite_platform(tmp_path))

    runner = CliRunner()
    first = runner.invoke(connect, ["sync", "fixture"])
    assert first.exit_code == 0, first.output
    assert "written=3" in first.output

    second = runner.invoke(connect, ["sync", "fixture"])
    assert second.exit_code == 0, second.output
    assert "skipped_unchanged=3" in second.output


# --- (t3) named-but-unconfigured source ⇒ exit 1 'not configured' -----------


def test_sync_named_unconfigured_source_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "q.db"))
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: _sqlite_platform(tmp_path))

    result = CliRunner().invoke(connect, ["sync", "fixture"])
    assert result.exit_code == 1
    assert "not configured" in result.output


# --- (t4) unknown source ⇒ exit 1 listing valid sources ---------------------


def test_sync_unknown_source_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)

    def _boom() -> object:
        raise AssertionError("platform must not be composed for an unknown source")

    monkeypatch.setattr(connect_cli, "_build_platform", _boom)
    result = CliRunner().invoke(connect, ["sync", "nosuch"])
    assert result.exit_code == 1
    assert "unknown connector source" in result.output
    assert "fixture" in result.output


# --- 015A0-AUTHORITY-M365: the ops-only authority surface -------------------


def test_the_connect_cli_source_names_no_authority_writer() -> None:
    """The CLI must not be able to construct or call a source-authority Writer.

    A NAME check over the module source, because the forbidden thing is having
    the capability at all: the CLI's only route to authority is the composer's
    ``IngestStack.authority``, which exposes no unfenced ``finalize``.
    """
    import inspect

    source = inspect.getsource(connect_cli)
    for forbidden in (
        "CurrentSourceAuthorityWriter",
        "SqlAlchemyCurrentSourceAuthorityWriter",
        "ImmutableKnowledgeArtifactStore",
        "M365AuthorityAdmission(",
    ):
        assert forbidden not in source, forbidden
    assert "build_ingest_stack" in source


def _seeded_attempt(platform: Any, attempt_id: str) -> Any:
    from ne.connect.authority_admission import (
        SqlAlchemyAdmissionAttemptStore,
        SqlAlchemySourceContractHeadStore,
        connector_instance_scope,
        drive_scope,
        ensure_admission_schema,
    )
    from ne.connect.authority_attempts import AuthorityAttemptReader
    from ne.contracts.source_authority import SourceContractIdentity

    ensure_admission_schema(platform.runtime_engine)
    store = SqlAlchemyAdmissionAttemptStore(platform.runtime_engine)
    parent = connector_instance_scope(
        deployment_id="d-1",
        connector_instance_id="c-1",
        graph_tenant_id="t-1",
    )
    head = SqlAlchemySourceContractHeadStore(platform.runtime_engine).claim_or_load(
        parent,
        source_generation=1,
        contract=SourceContractIdentity(
            source_kind="m365-sharepoint",
            descriptor_schema_version=1,
            descriptor_hash="c" * 64,
        ),
    )
    child = drive_scope(
        deployment_id="d-1",
        connector_instance_id="c-1",
        graph_tenant_id="t-1",
        drive_id="drive-1",
    )
    store.claim(
        attempt_id=attempt_id,
        scope=child,
        identity_hash="a" * 64,
        source_revision="etag-1",
        contract_epoch=head.contract_epoch,
        prior_artifact_id=None,
    )
    object.__setattr__(platform, "authority_attempts", AuthorityAttemptReader(store, (child,)))
    return store


def test_authority_inspect_prints_only_ids_hashes_and_the_durable_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    platform = _sqlite_platform(tmp_path, protected_transport=True, monkeypatch=monkeypatch)
    _seeded_attempt(platform, "adm-1")
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: platform)

    result = CliRunner().invoke(connect, ["authority", "inspect", "adm-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {
        "attempt_id",
        "source_kind",
        "source_binding",
        "scope_hash",
        "identity_hash",
        "stage",
        "source_revision",
        "contract_epoch",
        "prior_artifact_id",
        "receipt_id",
        "receipt_hash",
    }
    assert payload["stage"] == "claimed"
    assert payload["receipt_id"] is None


def test_authority_list_uses_the_composed_current_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    platform = _sqlite_platform(tmp_path, protected_transport=True, monkeypatch=monkeypatch)
    _seeded_attempt(platform, "adm-current")
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: platform)

    result = CliRunner().invoke(connect, ["authority", "list"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [item["attempt_id"] for item in payload["attempts"]] == ["adm-current"]
    assert payload["attempts"][0]["source_binding"] == "m365"


def test_authority_commands_have_no_raw_store_or_hardcoded_retry_source() -> None:
    import inspect

    inspect_source = inspect.getsource(connect_cli.authority_inspect.callback)
    retry_source = inspect.getsource(connect_cli.authority_retry.callback)
    list_source = inspect.getsource(connect_cli.authority_list.callback)
    for source in (inspect_source, retry_source, list_source):
        assert "SqlAlchemyAdmissionAttemptStore" not in source
    assert 'run_configured_sync(\n                "m365"' not in retry_source
    assert "before.source_binding" in retry_source


def test_authority_inspect_fails_loudly_on_an_unknown_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = _sqlite_platform(tmp_path)
    _seeded_attempt(platform, "adm-1")
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: platform)

    result = CliRunner().invoke(connect, ["authority", "inspect", "adm-missing"])
    assert result.exit_code != 0
    assert "authority attempt not found" in result.output


def test_authority_retry_makes_completed_attempt_indistinguishable_and_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished attempt is reported, never re-run — and never force-allowed."""
    from ne.connect.authority_admission import AdmissionStage

    _clear_env(monkeypatch)
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "q.db"))
    platform = _sqlite_platform(tmp_path)
    store = _seeded_attempt(platform, "adm-1")
    store.advance("adm-1", AdmissionStage.DELETION_VALIDATED)
    store.advance(
        "adm-1", AdmissionStage.AUTHORITY_TOMBSTONED, receipt_id="r-1", receipt_hash="d" * 64
    )
    store.advance("adm-1", AdmissionStage.CURSOR_COMMITTABLE)
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: platform)

    def _boom(**_: Any) -> Any:
        raise AssertionError("a completed attempt must not re-run a sync")

    monkeypatch.setattr("ne.connect.runner.build_ingest_stack", _boom)
    result = CliRunner().invoke(connect, ["authority", "retry", "adm-1"])
    assert result.exit_code != 0
    assert "authority attempt not found" in result.output


def test_authority_retry_refuses_when_no_authority_producer_is_composed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "q.db"))
    platform = _sqlite_platform(tmp_path)
    _seeded_attempt(platform, "adm-1")
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: platform)
    monkeypatch.setattr(
        connect_cli,
        "_ingest_stack",
        lambda _platform, _cs: cast(
            Any,
            SimpleNamespace(authority_unavailable_for=lambda _source: "authority is not enabled"),
        ),
    )

    result = CliRunner().invoke(connect, ["authority", "retry", "adm-1"])
    assert result.exit_code != 0
    assert "source authority is not composed" in result.output


def test_sync_refuses_a_connector_whose_requested_authority_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Syncing "just without receipts" would silently add unreceipted content."""
    from ne.connect.runner import IngestStack

    _clear_env(monkeypatch)
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["m365"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / "q.db"))
    platform = _sqlite_platform(tmp_path)
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: platform)
    monkeypatch.setattr(
        connect_cli,
        "_ingest_stack",
        lambda _platform, _cs: IngestStack(
            writer=cast(Any, object()),
            state_store=cast(Any, object()),
            quarantine=None,
            authority=None,
            authority_unavailable_reason="the sweep cannot beat the SLA",
        ),
    )
    result = CliRunner().invoke(connect, ["sync"])
    assert result.exit_code != 0
    assert "the sweep cannot beat the SLA" in result.output


@pytest.mark.parametrize("named", [False, True])
def test_sync_selects_authority_for_each_exact_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: bool
) -> None:
    from ne.connect.bridge import SyncReport
    from ne.connect.runner import IngestStack

    _fixture_env(monkeypatch, tmp_path)
    targets = '["fixture","salesforce"]' if not named else '["fixture"]'
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", targets)
    platform = _sqlite_platform(tmp_path)
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: platform)
    salesforce_factory = object()
    stack = IngestStack(
        writer=cast(Any, object()),
        state_store=cast(Any, object()),
        salesforce_authority_factory=salesforce_factory,
    )
    monkeypatch.setattr(connect_cli, "_ingest_stack", lambda _platform, _cs: stack)
    observed: list[tuple[str, object | None]] = []

    async def fake_run(source: str, *, authority: object | None, **_kwargs: Any) -> SyncReport:
        observed.append((source, authority))
        return SyncReport(
            mode="backfill",
            records_seen=0,
            written=0,
            skipped_unchanged=0,
            invalid=0,
            quarantined=0,
            cursors={},
        )

    monkeypatch.setattr("ne.connect.runner.run_configured_sync", fake_run)
    args = ["sync", "salesforce"] if named else ["sync"]
    result = CliRunner().invoke(connect, args)
    assert result.exit_code == 0, result.output
    expected = (
        [("salesforce", salesforce_factory)]
        if named
        else [("fixture", None), ("salesforce", salesforce_factory)]
    )
    assert observed == expected


def test_all_configured_sources_share_the_platform_async_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A composed async client must survive collection setup and source two."""
    from ne.connect.bridge import SyncReport

    _fixture_env(monkeypatch, tmp_path)
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["fixture","salesforce"]')
    observed_loops: list[asyncio.AbstractEventLoop] = []

    class LoopBoundChunkStore:
        async def ensure_collection(self) -> None:
            observed_loops.append(asyncio.get_running_loop())

    platform = SimpleNamespace(
        chunk_store=LoopBoundChunkStore(),
        sink=None,
        rebac_client=None,
        engines=(),
    )
    stack = SimpleNamespace(
        writer=object(),
        state_store=object(),
        quarantine=None,
        authority_for=lambda _source: None,
        authority_unavailable_for=lambda _source: None,
        # The real _IngestStack carries a ChunkReader | None read seam over the
        # composed chunk store (None when no authority-bearing source needs it);
        # `sync` forwards it unconditionally, so the stub must carry the field.
        chunk_reader=None,
    )
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: platform)
    monkeypatch.setattr(connect_cli, "_ingest_stack", lambda _platform, _cs: stack)

    async def fake_run(_source: str, **_kwargs: Any) -> SyncReport:
        loop = asyncio.get_running_loop()
        if observed_loops and loop is not observed_loops[0]:
            raise RuntimeError("composed async client reused on a different event loop")
        observed_loops.append(loop)
        return SyncReport(
            mode="backfill",
            records_seen=0,
            written=0,
            skipped_unchanged=0,
            invalid=0,
            quarantined=0,
            cursors={},
        )

    monkeypatch.setattr("ne.connect.runner.run_configured_sync", fake_run)
    result = CliRunner().invoke(connect, ["sync"])
    assert result.exit_code == 0, result.output
    assert len(observed_loops) == 3
    assert all(loop is observed_loops[0] for loop in observed_loops)


# --- a failing sync STATES ITS CAUSE (and a clean one says nothing new) -----


def _raise_readonly_state_db(*_args: Any, **_kwargs: Any) -> Any:
    """Fail the way the box failed: a real driver fault, reached through SQLAlchemy.

    ``ne demo up`` ended in ``fixture: ERROR OperationalError`` / ``csv: ERROR
    OperationalError`` — a class that names connection-refused, too-many-clients,
    deadlock, lock/statement timeout and sqlite-readonly alike, so the printed
    line was not a diagnosis and the demo beat could not be debugged live.
    """
    import sqlite3

    from sqlalchemy.exc import OperationalError

    raise OperationalError(
        "INSERT INTO cursors (source, cursor) VALUES (?, ?)",
        ("fixture", "2026-08-27T00:00:00Z"),
        sqlite3.OperationalError("attempt to write a readonly database"),
    )


def test_a_failing_sync_names_its_cause_not_just_its_class(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch)
    _fixture_env(monkeypatch, tmp_path)
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: _sqlite_platform(tmp_path))
    monkeypatch.setattr("ne.connect.runner.run_configured_sync", _raise_readonly_state_db)

    result = CliRunner().invoke(connect, ["sync", "fixture"])

    assert result.exit_code == 1, result.output
    assert "fixture: ERROR OperationalError" in result.output
    # The three things the old line threw away.
    assert "attempt to write a readonly database" in result.output
    assert "via=sqlite3.OperationalError" in result.output
    assert "statement: INSERT INTO cursors" in result.output
    # ...without the three things it was right to withhold.
    assert "2026-08-27T00:00:00Z" not in result.output
    assert "Traceback" not in result.output


def test_the_failure_line_survives_a_credential_bearing_driver_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DSN in the driver message is redacted; the diagnosis around it is kept."""

    def _raise_with_dsn(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError(
            "could not connect to postgresql://ne:s3cr3t-pw@postgres:5432/neutron: "
            "too many clients already"
        )

    _clear_env(monkeypatch)
    _fixture_env(monkeypatch, tmp_path)
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: _sqlite_platform(tmp_path))
    monkeypatch.setattr("ne.connect.runner.run_configured_sync", _raise_with_dsn)

    result = CliRunner().invoke(connect, ["sync", "fixture"])

    assert result.exit_code == 1
    assert "s3cr3t-pw" not in result.output
    assert "too many clients already" in result.output


def test_traceback_is_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    _fixture_env(monkeypatch, tmp_path)
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: _sqlite_platform(tmp_path))
    monkeypatch.setattr("ne.connect.runner.run_configured_sync", _raise_readonly_state_db)

    result = CliRunner().invoke(connect, ["sync", "fixture", "--traceback"])

    assert result.exit_code == 1
    assert "Traceback" in result.output
    assert "attempt to write a readonly database" in result.output


def test_a_clean_sync_says_nothing_new(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The counterpart: a SUCCEEDING sync's line is the counts line, unchanged.

    Diagnosis is added to the failure path only — a green run must not start
    printing driver noise, or the demo beat gains a new way to look broken.
    """
    _clear_env(monkeypatch)
    _fixture_env(monkeypatch, tmp_path)
    monkeypatch.setattr(connect_cli, "_build_platform", lambda: _sqlite_platform(tmp_path))

    result = CliRunner().invoke(connect, ["sync", "fixture"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == (
        "fixture: mode=backfill seen=3 written=3 skipped_unchanged=0 invalid=0 quarantined=0"
    )
