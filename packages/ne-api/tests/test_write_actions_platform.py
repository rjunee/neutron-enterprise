# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-ACTION-WIRE — the 3 NE-050 reference write actions in the real platform.

Proves at the ne-api composition root: (1) default-off byte-parity (the new
``write_actions_enabled`` flag defaulted vs explicit-off produce the SAME
config_hash AND the SAME executor tool menu); (2) an injected
``AppSeams.write_action_clients`` seam is IGNORED while the flag is off (the
``rebac_client`` precedent); (3) flag-on registers exactly the 3 write tools at
their declared tiers with their ``ToolArgPolicy`` merged into
``guardrails.tools`` BEFORE the freeze, moving the config_hash honestly; (4) a
FULL inline chat round trip — NE-CS-SEND: the Tier-1 CRM note executes
AUTONOMOUSLY (no pending approval ever appears), and an identical SECOND call
in the SAME turn dedups without a second connector call (the HITL park/approve
round trip lives on the Tier-2 send —
test_beat3_governed_draft_email_chain.py); (5) the grounded agent's ``ToolsetScopedExecutor`` never
exposes the write tools even when the flag is on (structural exclusion,
regression-pinned); (6) the fail-loud boot contract when one/all connector legs
are unconfigured; (7) the happy-path boot from real per-leg env settings,
registering the SQL engine for disposal.

The governed spine is REAL; only the network edges + the 3 write-action
connector clients are faked. No pytest-asyncio (repo policy): async is driven
via the conftest ``run`` helper.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from ne.api import create_app
from ne.api.platform import (
    AppSeams,
    WriteActionClients,
    _AllowedDraftMailClient,
    build_platform,
)
from ne.api.settings import ApiSettings
from ne.audit.config import AuditSettings
from ne.audit.store import ne_audit_log
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import ApprovalTier, Lane, Principal
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import ApprovalSettings, RuntimeSettings
from ne.runtime.errors import ToolsetViolationError
from ne.runtime.factory import create_department_session
from ne.runtime.toolset import ToolsetScopedExecutor

from .conftest import (
    ISSUER,
    FakeTokenVerifier,
    InMemoryFilteredChunkStore,
    PassthroughReranker,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
    get_state,
    parse_sse,
    run,
    text_completion,
    tool_call_completion,
)

_CRM_NOTE_TOOL = "write.internal.crm.note"
_TICKET_UPDATE_TOOL = "write.external.ticket.update"
_EMAIL_DRAFT_TOOL = "draft.email.create"
_CRM_LOOKUP_TOOL = "read.internal.crm.account.lookup"
_WRITE_TOOLS = {_CRM_NOTE_TOOL, _TICKET_UPDATE_TOOL, _EMAIL_DRAFT_TOOL}
_DRAFT_MAILBOX = "draft-owner@contoso.test"


# --- in-file fakes (the narrow ne.runtime.reference_actions Protocols) --------


class _FakeCrmClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def create_record(self, sobject: str, fields: Mapping[str, str]) -> str:
        self.calls.append((sobject, dict(fields)))
        return "003000000000001AAA"


class _FakeTicketClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int | str, dict[str, str]]] = []

    async def update_record(
        self, *, table: str, key_column: str, key_value: int | str, fields: Mapping[str, str]
    ) -> int:
        self.calls.append((table, key_column, key_value, dict(fields)))
        return 1


class _FakeDraftClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def create_draft(self, *, mailbox: str, subject: str, body: str, to: str) -> str:
        self.calls.append((mailbox, subject, body, to))
        return "AAMkAG-draft-1"


def _fake_clients() -> WriteActionClients:
    return WriteActionClients(
        crm=_FakeCrmClient(), ticket=_FakeTicketClient(), draft=_FakeDraftClient()
    )


# --- self-hosted harness (the test_demo_seams.py _api_settings()/_seams() style) --


def _api_settings(**overrides: object) -> ApiSettings:
    base: dict[str, object] = {
        "qdrant_url": "http://qdrant.test",
        "qdrant_collection": "t",
        "tei_url": "http://tei.test",
    }
    base.update(overrides)
    return ApiSettings(**base)  # type: ignore[arg-type]


def _seams(
    tmp_path: Path,
    *,
    key: str,
    gateway: Any | None = None,
    write_action_clients: WriteActionClients | None = None,
    approvals: ApprovalSettings | None = None,
    ontology_settings: Any | None = None,
) -> tuple[AppSeams, FakeTokenVerifier, str, str]:
    registry = PluginRegistry()
    verifier = FakeTokenVerifier()
    registry.register("token_verifier", "fake", lambda oidc: verifier)
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)

    runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
    audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
    perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"

    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
            principal_claims=("email", "preferred_username", "name", "department"),
        ),
        runtime_settings=RuntimeSettings(
            session_database_url=runtime_url, approvals=approvals or ApprovalSettings()
        ),
        audit_settings=AuditSettings(database_url=audit_url),
        permissions_settings=PermissionsSettings(database_url=perms_url),
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
        gateway=gateway if gateway is not None else ScriptedGateway(),
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=InMemoryFilteredChunkStore(),
        reranker=PassthroughReranker(),
        plugin_registry=registry,
        write_action_clients=write_action_clients,
        ontology_settings=ontology_settings,
    )
    return seams, verifier, runtime_url, audit_url


def _tool_names(platform: Any) -> set[str]:
    return {s.name for s in platform.executor.tool_specs}


@pytest.fixture
def engines() -> Any:
    started: list[Any] = []
    yield started
    for engine in started:
        engine.dispose()


# --- (1) default-off byte-parity ----------------------------------------------


def test_write_actions_default_off_config_hash_and_tool_menu_byte_identical(
    tmp_path: Path, engines: list[Any]
) -> None:
    reset_config_hash_for_testing()
    seams_a, _, _, _ = _seams(tmp_path, key="ident")
    default = build_platform(_api_settings(), seams_a)
    engines.extend(default.engines)

    reset_config_hash_for_testing()
    seams_b, _, _, _ = _seams(tmp_path, key="ident")
    explicit_off = build_platform(_api_settings(write_actions_enabled=False), seams_b)
    engines.extend(explicit_off.engines)

    assert default.config_hash == explicit_off.config_hash
    assert _tool_names(default) == _tool_names(explicit_off)
    assert not (_WRITE_TOOLS & _tool_names(default))


# --- (2) the injected seam is ignored while the flag is off -------------------


def test_write_actions_seam_ignored_when_disabled(tmp_path: Path, engines: list[Any]) -> None:
    reset_config_hash_for_testing()
    seams_plain, _, _, _ = _seams(tmp_path, key="ident2")
    default = build_platform(_api_settings(), seams_plain)
    engines.extend(default.engines)

    reset_config_hash_for_testing()
    seams_seamed, _, _, _ = _seams(tmp_path, key="ident2", write_action_clients=_fake_clients())
    off_with_seam = build_platform(_api_settings(write_actions_enabled=False), seams_seamed)
    engines.extend(off_with_seam.engines)

    assert default.config_hash == off_with_seam.config_hash
    assert _tool_names(default) == _tool_names(off_with_seam)


# --- (3) flag-on catalog + the REQUIRED ARG_POLICY merge -----------------------


def test_flag_on_catalog_and_policies(tmp_path: Path, engines: list[Any]) -> None:
    reset_config_hash_for_testing()
    seams_default, _, _, _ = _seams(tmp_path, key="cat")
    default = build_platform(_api_settings(), seams_default)
    engines.extend(default.engines)

    reset_config_hash_for_testing()
    seams_on, _, _, _ = _seams(tmp_path, key="cat", write_action_clients=_fake_clients())
    withtools = build_platform(_api_settings(write_actions_enabled=True), seams_on)
    engines.extend(withtools.engines)

    names = _tool_names(withtools)
    assert names >= _WRITE_TOOLS

    tiers = {s.name: s.tier for s in withtools.executor.tool_specs}
    # NE-CS-SEND: the ladder is SPEC §4.2's own rungs now — internal note
    # autonomous+digest (1), customer-visible ticket named+second (3), draft
    # autonomous (0). The Tier-2 rung is write.external.email.send, registered
    # only when a send client is composed (the seam here injects none).
    assert tiers[_CRM_NOTE_TOOL] is ApprovalTier.TIER_1
    assert tiers[_TICKET_UPDATE_TOOL] is ApprovalTier.TIER_3
    assert tiers[_EMAIL_DRAFT_TOOL] is ApprovalTier.TIER_0

    # The REQUIRED merge — pinned per-tool so silently omitting ONE fails here.
    assert _CRM_NOTE_TOOL in withtools.govern_settings.guardrails.tools
    assert _TICKET_UPDATE_TOOL in withtools.govern_settings.guardrails.tools
    assert _EMAIL_DRAFT_TOOL in withtools.govern_settings.guardrails.tools

    # Honest stamp: the config_hash moves vs default-off.
    assert withtools.config_hash != default.config_hash


def test_per_action_selection_is_canonical_and_unambiguous() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        _api_settings(
            write_actions_enabled=True,
            write_action_tools=(_EMAIL_DRAFT_TOOL,),
            email_draft_allowed_mailboxes=(_DRAFT_MAILBOX,),
        )
    with pytest.raises(ValueError, match="sorted and duplicate-free"):
        _api_settings(
            write_action_tools=(_EMAIL_DRAFT_TOOL, _EMAIL_DRAFT_TOOL),
            email_draft_allowed_mailboxes=(_DRAFT_MAILBOX,),
        )
    with pytest.raises(ValueError, match="requires at least one"):
        _api_settings(write_action_tools=(_EMAIL_DRAFT_TOOL,))
    with pytest.raises(ValueError, match="write_action_tools"):
        _api_settings(write_action_tools=("write.external.mail.send",))  # type: ignore[arg-type]


def test_draft_only_selection_registers_exact_tool_and_policy(
    tmp_path: Path, engines: list[Any]
) -> None:
    reset_config_hash_for_testing()
    seams, _, _, _ = _seams(tmp_path, key="draft-only", write_action_clients=_fake_clients())
    platform = build_platform(
        _api_settings(
            write_action_tools=(_EMAIL_DRAFT_TOOL,),
            email_draft_allowed_mailboxes=(_DRAFT_MAILBOX,),
        ),
        seams,
    )
    engines.extend(platform.engines)

    names = _tool_names(platform)
    assert names & (_WRITE_TOOLS | {_CRM_LOOKUP_TOOL}) == {_EMAIL_DRAFT_TOOL}
    assert platform.govern_settings.guardrails.tools[_EMAIL_DRAFT_TOOL]
    tiers = {spec.name: spec.tier for spec in platform.executor.tool_specs}
    assert tiers[_EMAIL_DRAFT_TOOL] is ApprovalTier.TIER_0


def test_draft_mailbox_allowlist_blocks_before_client_call() -> None:
    fake = _FakeDraftClient()
    client = _AllowedDraftMailClient(fake, (_DRAFT_MAILBOX,))

    message_id = run(
        client.create_draft(
            mailbox=_DRAFT_MAILBOX.upper(),
            subject="subject",
            body="body",
            to="owner@example.com",
        )
    )
    assert message_id == "AAMkAG-draft-1"
    assert fake.calls == [(_DRAFT_MAILBOX, "subject", "body", "owner@example.com")]

    with pytest.raises(ValueError, match="outside the configured mailbox allowlist"):
        run(
            client.create_draft(
                mailbox="other@contoso.test",
                subject="subject",
                body="body",
                to="owner@example.com",
            )
        )
    assert len(fake.calls) == 1


# --- (4) the full inline round trip: HITL x2 -> execute once -> dedup ---------


def test_inline_crm_note_tier1_autonomous_execute_audit_dedup_roundtrip(tmp_path: Path) -> None:
    reset_config_hash_for_testing()
    fake_crm = _FakeCrmClient()
    clients = WriteActionClients(crm=fake_crm, ticket=_FakeTicketClient(), draft=_FakeDraftClient())

    call_args = {"record_id": "001000000000001AAA", "title": "t", "body": "b"}
    gateway = ScriptedGateway(
        [
            tool_call_completion(_CRM_NOTE_TOOL, call_args),
            tool_call_completion(_CRM_NOTE_TOOL, call_args),
            text_completion("done"),
        ]
    )

    seams, verifier, _runtime_url, audit_url = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        gateway=gateway,
        write_action_clients=clients,
        approvals=ApprovalSettings(poll_interval_s=0.05),
    )
    app = create_app(
        _api_settings(write_actions_enabled=True, approver_roles=("approvers",)),
        seams=seams,
    )
    state = get_state(app)

    async def _t() -> httpx.Response:
        ada_token = verifier.mint("user:ada", groups=["eng"], department="engineering")
        appr_token = verifier.mint("user:approver", groups=["approvers"])
        ada = {"Authorization": f"Bearer {ada_token}"}
        appr = {"Authorization": f"Bearer {appr_token}"}
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://ne.test") as writer,
            httpx.AsyncClient(transport=transport, base_url="http://ne.test") as approver,
        ):
            created = await writer.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=ada
            )
            assert created.status_code == 200, created.text
            sid = created.json()["session_id"]

            # NE-CS-SEND: the internal CRM note is Tier-1 — autonomous + digest.
            # The turn completes with NO human in the loop, and the pending queue
            # stays empty throughout.
            response = await writer.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "add a note"},
                headers=ada,
            )
            listed = await approver.get("/api/v1/approvals/pending", headers=appr)
            assert listed.status_code == 200
            assert [x for x in listed.json()["approvals"] if x["action"] == _CRM_NOTE_TOOL] == [], (
                "a Tier-1 internal write must not queue a human approval"
            )
            return response

    sse = run(_t())
    assert sse.status_code == 200, sse.text
    parsed = parse_sse(sse.text)
    done = parsed.of_type("done")
    assert done, "no done frame — the autonomous Tier-1 turn did not complete"
    assert done[0].data is not None
    assert done[0].data["output"] == "done"

    # The connector was called EXACTLY once — the second call deduplicated.
    assert len(fake_crm.calls) == 1

    audit_engine = sa.create_engine(audit_url)
    try:
        with audit_engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_audit_log.c.outcome, ne_audit_log.c.payload).where(
                    ne_audit_log.c.action == "tool.execute"
                )
            ).all()
    finally:
        audit_engine.dispose()
    dedup_hits = [
        row
        for row in rows
        if row.outcome == "success"
        and json.loads(row.payload.get("detail") or "{}").get("deduplicated") is True
    ]
    assert dedup_hits, "expected a tool.execute[success] audit record marked deduplicated=true"

    for engine in state.engines:
        engine.dispose()


# --- (5) grounded-agent structural exclusion, even with the flag on -----------


_ACCOUNTS_DIRECTORY_TEMPLATE = """
template_id = "accounts-directory"
version = 1
description = "Every account visible to the caller, with its name."
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  ?account a <https://neutron.enterprise/schema/operational/Account> .
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
}
\"\"\"

[scope]
graph_classes = ["all-employees"]
result_shape = "enumerable"

[answer]
columns = ["account", "name"]
citation_var = "account"
"""


def test_grounded_agent_excluded_even_when_enabled(tmp_path: Path, engines: list[Any]) -> None:
    from ne.brain_ontology.config import OntologySettings

    reset_config_hash_for_testing()
    directory = tmp_path / "verified-templates"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "accounts_directory.toml").write_text(
        _ACCOUNTS_DIRECTORY_TEMPLATE, encoding="utf-8"
    )
    ontology_settings = OntologySettings(
        verified_answers_enabled=True,
        verified_templates_dir=directory,
        include_ontology_graph=False,
        backend="oxigraph",
        grounded_agent_enabled=True,
    )

    fake_crm = _FakeCrmClient()
    clients = WriteActionClients(crm=fake_crm, ticket=_FakeTicketClient(), draft=_FakeDraftClient())
    seams, _, _, _ = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        write_action_clients=clients,
        ontology_settings=ontology_settings,
    )
    platform = build_platform(_api_settings(write_actions_enabled=True), seams)
    engines.extend(platform.engines)

    # The ONE platform executor DOES carry the 3 write tools.
    assert _tool_names(platform) >= _WRITE_TOOLS
    grounding = platform.grounding
    grounded_agent_name = platform.grounded_agent_name
    assert grounding is not None
    assert grounded_agent_name is not None

    # The EXACT same recipe create_department_session uses internally
    # (ne.runtime.factory.create_department_session) to wrap a grounded agent's
    # executor — reconstructed here to assert its tool_specs directly.
    scoped = ToolsetScopedExecutor(
        platform.executor,
        granted=grounding.granted_tools,
        sink=grounding.sink,
        scope=f"grounded:{grounded_agent_name}",
        pdp=grounding.pdp,
    )
    assert not (_WRITE_TOOLS & {s.name for s in scoped.tool_specs})

    principal = Principal(
        id="user:g", groups=("all-employees",), claims={"department": "engineering"}
    )
    with pytest.raises(ToolsetViolationError):
        run(
            scoped.execute(
                _CRM_NOTE_TOOL, {"record_id": "001", "title": "t", "body": "b"}, principal=principal
            )
        )
    assert fake_crm.calls == []

    # Also exercise the REAL composition-root call site (create_department_session)
    # so the wiring itself — not just a hand-rolled equivalent — is regression-pinned.
    async def _open() -> Any:
        return await create_department_session(
            grounded_agent_name,
            "engineering",
            owner_principal_id="user:ada",
            store=platform.session_store,
            gateway=ScriptedGateway(),
            executor=platform.executor,
            settings=platform.runtime_settings,
            grounding=grounding,
        )

    session = run(_open())
    assert session is not None  # a session was opened without raising


# --- (6) fail-loud boot: missing connector legs --------------------------------


def _clear_connector_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in [k for k in os.environ if k.startswith(("NE_SQL_", "NE_SALESFORCE_", "NE_M365_"))]:
        monkeypatch.delenv(name, raising=False)


def _sql_env(tmp_path: Path) -> dict[str, str]:
    return {
        "NE_SQL_CONNECTION_URL": f"sqlite:///{tmp_path}/tickets-{uuid.uuid4().hex}.db",
        "NE_SQL_ALLOW_SQLITE": "true",
        "NE_SQL_OWNER": "data-eng",
        "NE_SQL_TABLES": (
            '[{"table": "tickets", "id_column": "id", "content_columns": ["status"], '
            '"acl": {"principals": ["alice@example.com"]}}]'
        ),
    }


_SF_ENV: dict[str, str] = {
    "NE_SALESFORCE_CLIENT_ID": "cid",
    "NE_SALESFORCE_OWNER": "data-eng",
    "NE_SALESFORCE_INSTANCE_URL": "https://acme.my.salesforce.com",
    "NE_SALESFORCE_AUTH_METHOD": "client_credentials",
    "NE_SALESFORCE_CLIENT_SECRET": "s",
    "NE_SALESFORCE_OBJECTS": '[{"name": "Account", "content_fields": ["Name"]}]',
}

_M365_ENV: dict[str, str] = {
    "NE_M365_TENANT_ID": "tid",
    "NE_M365_CLIENT_ID": "cid",
    "NE_M365_OWNER": "data-eng",
    "NE_M365_DRIVE_IDS": '["d1"]',
    "NE_M365_AUTH_METHOD": "secret",
    "NE_M365_CLIENT_SECRET": "s",
}


def test_boot_without_salesforce_env_serves_crm_internally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engines: list[Any]
) -> None:
    """NE-S5-INTERNAL-CRM: the CRM legs need NO Salesforce env at all.

    The note lands in the platform's own runtime database and the lookups read
    the synced mirror in the chunk store, so a deployment with only the sql and
    m365 legs configured boots cleanly and registers every CRM tool.
    """
    _clear_connector_env(monkeypatch)
    for k, v in _sql_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    for k, v in _M365_ENV.items():
        monkeypatch.setenv(k, v)
    reset_config_hash_for_testing()
    seams, _, _, _ = _seams(tmp_path, key=uuid.uuid4().hex)
    platform = build_platform(_api_settings(write_actions_enabled=True), seams)
    engines.extend(platform.engines)
    assert _tool_names(platform) >= _WRITE_TOOLS | {
        "read.external.crm.account",
        "read.external.crm.contact",
    }


def test_boot_error_m365_leg_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_connector_env(monkeypatch)
    for k, v in _sql_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    for k, v in _SF_ENV.items():
        monkeypatch.setenv(k, v)
    reset_config_hash_for_testing()
    seams, _, _, _ = _seams(tmp_path, key=uuid.uuid4().hex)
    with pytest.raises(ValueError) as excinfo:
        build_platform(_api_settings(write_actions_enabled=True), seams)
    message = str(excinfo.value)
    assert "m365" in message
    assert "NE_M365_" in message
    assert "salesforce" not in message
    assert "sql" not in message

    # Symmetric: with NO connector env at all, ONE boot cycle names BOTH
    # remaining connector legs (NE-S5-INTERNAL-CRM: salesforce is no longer a
    # write-action leg and must not be demanded).
    _clear_connector_env(monkeypatch)
    reset_config_hash_for_testing()
    seams_none, _, _, _ = _seams(tmp_path, key=uuid.uuid4().hex)
    with pytest.raises(ValueError) as excinfo_none:
        build_platform(_api_settings(write_actions_enabled=True), seams_none)
    message_none = str(excinfo_none.value)
    assert "sql" in message_none and "NE_SQL_" in message_none
    assert "salesforce" not in message_none and "NE_SALESFORCE_" not in message_none
    assert "m365" in message_none and "NE_M365_" in message_none


def test_draft_only_boot_validates_only_m365(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_connector_env(monkeypatch)
    monkeypatch.setenv("NE_SQL_CONNECTION_URL", "hostile-not-a-dsn")
    monkeypatch.setenv("NE_SALESFORCE_AUTH_METHOD", "hostile-invalid")
    reset_config_hash_for_testing()
    seams, _, _, _ = _seams(tmp_path, key=uuid.uuid4().hex)
    with pytest.raises(ValueError) as excinfo:
        build_platform(
            _api_settings(
                write_action_tools=(_EMAIL_DRAFT_TOOL,),
                email_draft_allowed_mailboxes=(_DRAFT_MAILBOX,),
            ),
            seams,
        )
    message = str(excinfo.value)
    assert "m365" in message and "NE_M365_" in message
    assert "sql" not in message
    assert "salesforce" not in message


def test_draft_only_hashes_only_m365_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engines: list[Any]
) -> None:
    _clear_connector_env(monkeypatch)
    for key, value in _M365_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("NE_SQL_CONNECTION_URL", "hostile-not-a-dsn")
    monkeypatch.setenv("NE_SALESFORCE_AUTH_METHOD", "hostile-invalid")
    key = uuid.uuid4().hex

    def build() -> Any:
        reset_config_hash_for_testing()
        seams, _, _, _ = _seams(tmp_path, key=key)
        platform = build_platform(
            _api_settings(
                write_action_tools=(_EMAIL_DRAFT_TOOL,),
                email_draft_allowed_mailboxes=(_DRAFT_MAILBOX,),
            ),
            seams,
        )
        engines.extend(platform.engines)
        return platform

    first = build()
    assert _tool_names(first) & (_WRITE_TOOLS | {_CRM_LOOKUP_TOOL}) == {_EMAIL_DRAFT_TOOL}
    assert not any("tickets" in str(engine.url) for engine in first.engines)

    monkeypatch.setenv("NE_SQL_CONNECTION_URL", "still-hostile")
    monkeypatch.setenv("NE_SALESFORCE_AUTH_METHOD", "still-hostile")
    unrelated_changed = build()
    assert unrelated_changed.config_hash == first.config_hash

    monkeypatch.setenv("NE_M365_TENANT_ID", "tid-2")
    destination_changed = build()
    assert destination_changed.config_hash != first.config_hash


# --- (7) boot happy path: all 3 legs from real per-leg env settings -----------


def test_boot_happy_path_all_legs_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engines: list[Any]
) -> None:
    _clear_connector_env(monkeypatch)
    sql_env = _sql_env(tmp_path)
    for k, v in sql_env.items():
        monkeypatch.setenv(k, v)
    for k, v in _SF_ENV.items():
        monkeypatch.setenv(k, v)
    for k, v in _M365_ENV.items():
        monkeypatch.setenv(k, v)
    reset_config_hash_for_testing()
    seams, _, _, _ = _seams(tmp_path, key=uuid.uuid4().hex)
    platform = build_platform(_api_settings(write_actions_enabled=True), seams)
    engines.extend(platform.engines)

    assert _tool_names(platform) >= _WRITE_TOOLS

    # Disposal parity: the SQL leg's engine is registered on platform.engines
    # alongside every other platform engine.
    sql_db_name = sql_env["NE_SQL_CONNECTION_URL"].rsplit("/", 1)[-1]
    assert any(sql_db_name in str(e.url) for e in platform.engines)


# --- (8) config_hash regression lock: write-action connector destinations MUST
# be stamped (Argus re-review, post-Codex-C1-fix) ------------------------------


def test_write_action_connector_destination_change_moves_config_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engines: list[Any]
) -> None:
    """A deployment whose write actions target a DIFFERENT external system must
    freeze to a DIFFERENT config_hash — the whole point of stamping connector
    settings into extra_modules (Codex C1). Holds the seams key AND every other
    env value fixed; varies ONLY the M365 tenant id, so a config_hash collision
    here can only be explained by the destination-stamping being dropped.
    """
    _clear_connector_env(monkeypatch)
    sql_env = _sql_env(tmp_path)
    key = uuid.uuid4().hex

    for k, v in sql_env.items():
        monkeypatch.setenv(k, v)
    for k, v in _SF_ENV.items():
        monkeypatch.setenv(k, v)
    for k, v in _M365_ENV.items():
        monkeypatch.setenv(k, v)
    reset_config_hash_for_testing()
    seams_a, _, _, _ = _seams(tmp_path, key=key)
    platform_a = build_platform(_api_settings(write_actions_enabled=True), seams_a)
    engines.extend(platform_a.engines)

    monkeypatch.setenv("NE_M365_TENANT_ID", "tid-2")
    reset_config_hash_for_testing()
    seams_b, _, _, _ = _seams(tmp_path, key=key)
    platform_b = build_platform(_api_settings(write_actions_enabled=True), seams_b)
    engines.extend(platform_b.engines)

    assert platform_a.config_hash != platform_b.config_hash


# --- NE-BUNDLE-GATEWAY-CONFIG: the gateway MENU must be config_hash-covered ---


def test_gateway_menu_change_moves_config_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engines: list[Any]
) -> None:
    """Swapping the gateway MODEL MENU must move the composed ``config_hash``.

    Argus P3 on NE-BUNDLE-GATEWAY-CONFIG: that unit's own
    ``test_swap_changes_config_hash`` hand-builds
    ``freeze_platform_config(extra_modules={"ne-gateway": ...})`` — it
    TRANSLITERATES ``platform.py``'s composition instead of DERIVING from it.
    If a future edit dropped ``"ne-gateway"`` from that mapping (:792), the
    unit test would stay GREEN while the demo docs' claim that "a frontier swap
    changes config_hash" silently became FALSE. This pins it BEHAVIOURALLY
    through the real ``build_platform``, mirroring
    ``test_write_action_connector_destination_change_moves_config_hash`` above.

    Note ``_seams`` pins ``gateway_settings`` explicitly, so the menu must be
    varied through THAT seam — an ``NE_GATEWAY_MODELS`` env variation is
    silently ignored here and both builds would use the identical stub. The
    orchestrator's first draft did exactly that and saw two identical hashes,
    which momentarily looked like a product defect (the menu not being
    hash-covered) and was in fact fixture misuse; chased to ground rather than
    filed as a finding.
    """
    _clear_connector_env(monkeypatch)
    for k, v in _sql_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    key = uuid.uuid4().hex

    def menu(model_id: str) -> GatewaySettings:
        return GatewaySettings(
            models=[
                ModelDef(
                    name="private-default",
                    litellm_model=f"hosted_vllm/{model_id}",
                    provider="hosted_vllm",
                    lane=Lane.PRIVATE_VLLM,
                    api_base="http://vllm:8000/v1",
                    context_window=32768,
                )
            ]
        )

    def build(model_id: str) -> str:
        reset_config_hash_for_testing()
        seams, _a, _b, _c = _seams(tmp_path, key=key)
        platform = build_platform(_api_settings(), replace(seams, gateway_settings=menu(model_id)))
        engines.extend(platform.engines)
        return platform.config_hash

    hash_a = build("m-a")
    hash_b = build("m-b")
    hash_a_again = build("m-a")

    assert hash_a != hash_b, (
        "the gateway model menu is not covered by the composed config_hash — a "
        "model swap would be invisible to the hash, making the demo's 'the swap "
        "changes config_hash' claim false"
    )
    # Control: an IDENTICAL menu must reproduce the SAME hash, so the assertion
    # above discriminates rather than passing because any two builds differ.
    assert hash_a == hash_a_again, (
        "two builds with an identical gateway menu hashed differently — the "
        "assertion above then proves nothing about the menu"
    )
