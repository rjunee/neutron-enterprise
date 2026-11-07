# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""S5 with NO Salesforce: internal CRM reads/writes + park → approve → SEND.

NE-S5-INTERNAL-CRM (owner decision 2026-08-28): the demo org's Salesforce REST
quota is exhausted (``REQUEST_LIMIT_EXCEEDED``, rolling 24h window), so a fresh
S5 run died on its FIRST step — the live ``read.external.crm.contact`` SOQL
call — before it could even park at the Tier-2 send gate. The fix is not a
retry: the records the demo needs were already synced into the platform's own
chunk store, so the CRM legs now read/write INTERNAL storage and no live
Salesforce call exists on the S5 path at all.

What this file proves, through the real composition:

1. ``test_contact_read_is_served_internally_and_note_lands_durably`` — the
   platform builds the internal CRM clients ITSELF (no seam injection, no
   ``NE_SALESFORCE_*`` env anywhere): the governed contact lookup resolves the
   synced-mirror Contact document to the recorded email, and the governed
   ``write.internal.crm.note`` lands a durable row in the platform's own
   runtime database — the first real execution shape this tool has on the box.

2. ``test_s5_park_approve_send_end_to_end`` — the whole S5 acceptance ladder in
   one run: the internal contact read serves ``owner@example.com``; the REAL
   ``draft.email.create`` action creates the draft over the REAL ne-connect
   ``GraphWriteClient`` (only the Graph HTTP wire is an ``httpx.MockTransport``
   — the beat-3 discipline); the run PARKS on a REAL durable Tier-2
   ``write.external.email.send`` approval; the approver approves over the REAL
   HTTP route; and the resumed continuation executes the REAL send action —
   the mocked wire records exactly one ``POST .../messages/{id}/send`` and the
   run reaches ``completed``. The recipient the send verifies against the
   draft comes from the SAME allowlist the live overlay pins.

The park leg mirrors ``test_run_approval_resume_continuation._park`` (the
sanctioned shape: the adapter parks on the exact server-minted identity with
the real ``create_wait``); everything decision-side and resume-side is the
real chain — real checkpoint store, real gate/PDP/guardrails/executor, real
workflow service, real HTTP approvals router. The continuation adapter mirrors
``ne.worker.run_execution.ProfileBoundRunExecutionAdapter``'s branch, including
its ambient ``action_scope`` (the worker wraps every attempt in one, which is
what gives the send action its derived idempotency identity on resume).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from ne.api.internal_crm import (
    InternalCrmLookupClient,
    InternalCrmNoteClient,
    ensure_internal_crm_schema,
    ne_internal_crm_notes,
)
from ne.api.platform import WriteActionClients
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.graph import GraphClient
from ne.connect.m365.write import GraphWriteClient
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import canonical_source_authority_stamps
from ne.contracts.types import Chunk, Principal
from ne.events import new_approval_event
from ne.govern.config import GovernSettings, GuardrailSettings, RecipientSettings
from ne.runtime.approval_store import ne_approval_requests
from ne.runtime.approvals import canonical_args_hash
from ne.runtime.correlation import action_scope
from ne.runtime.run_checkpoint import (
    RunApprovalWaiting,
    ne_agent_run_continuation_checkpoints,
)
from ne.runtime.run_finalizer import RunTurnTarget
from ne.runtime.run_runner import RunExecutionContext, RunExecutionResult
from ne.runtime.run_store import RunState, ne_agent_runs
from ne.runtime.run_workflow import (
    RunWorkflowRequest,
    RunWorkflowService,
    RunWorkflowStateStore,
)
from ne.runtime.source_results import (
    authenticated_request_scope,
    complete_run_direct_publication_observation,
    direct_publication_scope,
)
from ne.schema import ApprovalDecision

from .conftest import ISSUER, AppBuilder, admit_current_source, get_state, run

OWNER = "user:ops"
DEPARTMENT = "operations"
APPROVER = "user:carol"
APPROVER_GROUP = "approvers"
AGENT = "agt_customersupport"

CONTACT_TOOL = "read.external.crm.contact"
DRAFT_TOOL = "draft.email.create"
SEND_TOOL = "write.external.email.send"
NOTE_TOOL = "write.internal.crm.note"

#: The demo's exact identities: the send mailbox and the looked-up recipient.
DEMO_MAILBOX = "owner@contoso.onmicrosoft.com"
DEMO_RECIPIENT = "owner@example.com"
GRAPH_MESSAGE_ID = "AAMkAG-s5-draft-message-id"
_TOKEN_SUFFIX = "/oauth2/v2.0/token"

#: The Salesforce ids the synced mirror carries for the demo customer.
CONTACT_RECORD_ID = "003AveryArbor00001"
ACCOUNT_RECORD_ID = "001MeridianHh00001"


def _contact_chunk(*, connector: str = "salesforce") -> Chunk:
    """A synced-mirror Contact document, exactly as the connector mapper shapes it.

    ``make_salesforce_record_mapper`` with the demo's Contact config
    (``content_fields=[FirstName,LastName,Email,Phone]``, Phone unseeded):
    ``text`` joins the fields with ``\\n\\n``; the ids ride stamped metadata.
    """
    return Chunk(
        chunk_id="s5-contact-avery",
        document_id="s5-contact-avery",
        text=f"Avery\n\nArbor\n\n{DEMO_RECIPIENT}",
        metadata={
            "connector": connector,
            "source_id": "Contact",
            "record_id": CONTACT_RECORD_ID,
            "pii_class": "personal",
            "object": "Contact",
            "AccountId": ACCOUNT_RECORD_ID,
            "Meridian_Source_Id__c": "mer-cus-001",
        },
        acl=("ops",),
    )


def _account_chunk(*, connector: str = "salesforce") -> Chunk:
    return Chunk(
        chunk_id="s5-account-hh",
        document_id="s5-account-hh",
        text="Arbor Household",
        metadata={
            "connector": connector,
            "source_id": "Account",
            "record_id": ACCOUNT_RECORD_ID,
            "pii_class": "personal",
            "object": "Account",
            "Meridian_Source_Id__c": "mer-hh-001",
        },
        acl=("ops",),
    )


# --- (1) the internal wiring, composed by the platform itself -----------------


def test_contact_read_is_served_internally_and_note_lands_durably(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Salesforce env, no injected clients: the platform wires internal CRM.

    Selecting ONLY ``write.internal.crm.note`` requires no connector leg at
    all now. The contact lookup serves the synced mirror; the note write lands
    a durable row in the runtime database.
    """
    for name in [k for k in os.environ if k.startswith("NE_SALESFORCE_")]:
        monkeypatch.delenv(name, raising=False)
    h = make_app(
        chunks=(_contact_chunk(), _account_chunk()),
        write_action_tools=(NOTE_TOOL,),
    )
    state = get_state(h.app)
    registered = {s.name for s in state.executor.tool_specs}
    assert {NOTE_TOOL, CONTACT_TOOL, "read.external.crm.account"} <= registered

    principal = Principal(id=OWNER, groups=())

    async def _drive() -> tuple[str, str]:
        with action_scope(f"s5-internal:{uuid.uuid4().hex}"):
            contact = await state.executor.execute(
                CONTACT_TOOL, {"name": "Avery Arbor"}, principal=principal
            )
            note = await state.executor.execute(
                NOTE_TOOL,
                {
                    "record_id": ACCOUNT_RECORD_ID,
                    "title": "Hardship outreach email sent",
                    "body": "Outreach email sent to the customer after approval.",
                },
                principal=principal,
            )
        return contact, note

    contact_json, note_json = run(_drive())
    contact = json.loads(contact_json)
    assert contact == {
        "name": "Avery Arbor",
        "found": True,
        "record_id": CONTACT_RECORD_ID,
        "email": DEMO_RECIPIENT,
        "account_id": ACCOUNT_RECORD_ID,
    }
    note = json.loads(note_json)
    assert note["succeeded"] is True

    # The note is DURABLE: a real row in the platform's own runtime database.
    engine = sa.create_engine(h.runtime_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.select(ne_internal_crm_notes)).mappings().all()
    finally:
        engine.dispose()
    assert len(rows) == 1
    assert rows[0]["parent_record_id"] == ACCOUNT_RECORD_ID
    assert rows[0]["sobject"] == "Note"
    assert "Outreach email sent" in rows[0]["body"]


def test_a_foreign_connector_row_cannot_poison_the_governed_contact_read(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connector pin, end to end through the platform-composed tool.

    The metadata namespace is shared: the SQL sync stamps any configured
    ``metadata_columns`` verbatim (a source column named ``object`` lands as
    ``metadata.object``) and the m365 sync moves item metadata whole. Without
    the ``metadata.connector`` term this same-shaped SharePoint row either
    serves ITS email into the outreach draft or trips the ambiguity refusal and
    denies S5 outright. Pinned here at the tool boundary, not just the client.
    """
    for name in [k for k in os.environ if k.startswith("NE_SALESFORCE_")]:
        monkeypatch.delenv(name, raising=False)
    poisoned = Chunk(
        chunk_id="sp-contact-avery",
        document_id="sp-contact-avery",
        text="Avery\n\nArbor\n\nattacker@evil.test",
        metadata={
            "connector": "m365",
            "source_id": "sharepoint-list",
            "record_id": "003Poisoned000001",
            "object": "Contact",
            "AccountId": "001Poisoned000001",
        },
        acl=("ops",),
    )
    h = make_app(
        chunks=(_contact_chunk(), _account_chunk(), poisoned),
        write_action_tools=(NOTE_TOOL,),
    )
    state = get_state(h.app)
    principal = Principal(id=OWNER, groups=())

    async def _drive() -> str:
        with action_scope(f"s5-pin:{uuid.uuid4().hex}"):
            return await state.executor.execute(
                CONTACT_TOOL, {"name": "Avery Arbor"}, principal=principal
            )

    contact = json.loads(run(_drive()))
    assert contact["found"] is True
    assert contact["email"] == DEMO_RECIPIENT
    assert contact["record_id"] == CONTACT_RECORD_ID


# --- (1b) the connector pin is resolved from HASHED settings, not raw env -----


def _configure_salesforce_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, source_name: str | None
) -> None:
    """List + fully configure the Salesforce sync source so it RESOLVES.

    Mirrors ``test_connect_sync_platform``'s fixture shape. Only a source the
    platform resolves lands in ``connect_source_settings`` and therefore in the
    frozen ``ne-connect-source-salesforce`` hash module.
    """
    key = uuid.uuid4().hex
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["salesforce"]')
    monkeypatch.setenv("NE_CONNECT_SYNC_STATE_PATH", str(tmp_path / f"sf-state-{key}.db"))
    monkeypatch.setenv("NE_CONNECT_SYNC_QUARANTINE_PATH", str(tmp_path / f"sf-q-{key}.db"))
    monkeypatch.setenv("NE_SALESFORCE_AUTH_METHOD", "client_credentials")
    monkeypatch.setenv("NE_SALESFORCE_CLIENT_ID", "client-id")
    monkeypatch.setenv("NE_SALESFORCE_CLIENT_SECRET", "secret-value")
    monkeypatch.setenv("NE_SALESFORCE_INSTANCE_URL", "https://acme.my.salesforce.com")
    monkeypatch.setenv("NE_SALESFORCE_OWNER", "platform-test")
    monkeypatch.setenv(
        "NE_SALESFORCE_OBJECTS",
        '[{"name":"Contact","content_fields":["FirstName","LastName","Email"]}]',
    )
    if source_name is not None:
        monkeypatch.setenv("NE_SALESFORCE_SOURCE_NAME", source_name)


def _lookup_one_contact(h: Any) -> dict[str, Any]:
    state = get_state(h.app)
    principal = Principal(id=OWNER, groups=())

    async def _drive() -> str:
        with action_scope(f"s5-pin:{uuid.uuid4().hex}"):
            return await state.executor.execute(
                CONTACT_TOOL, {"name": "Avery Arbor"}, principal=principal
            )

    return json.loads(run(_drive()))


def test_the_pin_ignores_env_that_never_entered_the_frozen_config(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The serious one: unhashed env MUST NOT move the served corpus.

    With the Salesforce sync NOT a configured source, nothing resolves and
    nothing lands in the frozen config_hash preimage — so ``config_hash`` cannot
    move. If the lookup derived its pin from raw ``NE_SALESFORCE_SOURCE_NAME``
    anyway, flipping that one env var would change WHICH RECORDS the governed
    CRM read serves while the platform's provenance hash stayed byte-identical
    (SPEC's config_hash is "SHA-256 over the fully-resolved, canonicalized
    configuration tree"). The pin falls back to the code constant instead.
    """
    for name in [k for k in os.environ if k.startswith("NE_CONNECT_SYNC_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NE_SALESFORCE_SOURCE_NAME", "sf-prod")

    h = make_app(
        chunks=(_contact_chunk(), _account_chunk()),  # stamped "salesforce"
        write_action_tools=(NOTE_TOOL,),
    )
    contact = _lookup_one_contact(h)
    assert contact["found"] is True
    assert contact["email"] == DEMO_RECIPIENT


def test_the_pin_matches_the_resolved_source_name_verbatim(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unstripped, exactly as ``configured_source_name`` returns it.

    ``ne.connect.runner.configured_source_name`` uses ``.strip()`` only to test
    emptiness and returns the name VERBATIM, so the mapper stamps the padded
    value. A pin that stripped it would search for something no document carries.
    """
    _configure_salesforce_sync(monkeypatch, tmp_path, source_name=" sf-prod ")
    h = make_app(
        chunks=(
            _contact_chunk(connector=" sf-prod "),
            _account_chunk(connector=" sf-prod "),
        ),
        write_action_tools=(NOTE_TOOL,),
    )
    contact = _lookup_one_contact(h)
    assert contact["found"] is True
    assert contact["email"] == DEMO_RECIPIENT


def test_a_non_default_resolved_source_name_is_honoured(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_salesforce_sync(monkeypatch, tmp_path, source_name="sf-prod")
    h = make_app(
        chunks=(_contact_chunk(connector="sf-prod"), _account_chunk(connector="sf-prod")),
        write_action_tools=(NOTE_TOOL,),
    )
    assert _lookup_one_contact(h)["found"] is True


def test_a_second_connector_claiming_the_pinned_source_name_fails_boot(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """metadata.connector carries a NAME, not a kind — so the name must be unique.

    A second configured connector claiming the pinned ``source_name`` makes the
    governed CRM read unable to attribute its own records. Refuse at boot.
    """
    _configure_salesforce_sync(monkeypatch, tmp_path, source_name=None)
    monkeypatch.setenv("NE_CONNECT_SYNC_SOURCES", '["salesforce","zendesk"]')
    monkeypatch.setenv("NE_ZENDESK_BASE_URL", "https://acme.zendesk.com")
    monkeypatch.setenv("NE_ZENDESK_EMAIL", "ops@acme.test")
    monkeypatch.setenv("NE_ZENDESK_API_TOKEN", "token-value")
    monkeypatch.setenv("NE_ZENDESK_OWNER", "platform-test")
    monkeypatch.setenv("NE_ZENDESK_SOURCE_NAME", "salesforce")

    with pytest.raises(ValueError, match="SAME source_name"):
        make_app(
            chunks=(_contact_chunk(), _account_chunk()),
            write_action_tools=(NOTE_TOOL,),
        )


# --- (2) the S5 acceptance ladder: park → approve → the send REALLY happens ---


def _graph_transport(recorder: list[httpx.Request]) -> httpx.MockTransport:
    """The beat-3 Graph wire: token, draft create, read-back, send. Nothing else."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        if str(request.url).endswith(_TOKEN_SUFFIX):
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
        if request.url.path.endswith("/messages") and request.method == "POST":
            return httpx.Response(201, json={"id": GRAPH_MESSAGE_ID, "isDraft": True})
        if (
            request.url.path.endswith(f"/messages/{GRAPH_MESSAGE_ID}/send")
            and request.method == "POST"
        ):
            return httpx.Response(202)
        if request.url.path.endswith(f"/messages/{GRAPH_MESSAGE_ID}") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "isDraft": True,
                    "toRecipients": [{"emailAddress": {"address": DEMO_RECIPIENT}}],
                    "ccRecipients": [],
                    "bccRecipients": [],
                },
            )
        return httpx.Response(404, json={"error": {"code": "notFound"}})

    return httpx.MockTransport(handler)


def _m365_settings() -> M365ConnectorSettings:
    return M365ConnectorSettings(
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        auth_method="secret",
        client_secret=SecretStr("fake-secret-not-real"),
        site_ids=("site-meridian",),
        owner="data-eng@contoso.test",
    )


class _UnusableTicketClient:
    async def update_record(self, **_kw: Any) -> int:
        raise AssertionError("S5 must not touch the ticket leg")


_SEND_ARGS: dict[str, object] = {"draft_ref": GRAPH_MESSAGE_ID}


def _send_posts(recorder: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in recorder if r.method == "POST" and r.url.path.endswith("/send")]


def _statuses(runtime_url: str, approval_id: str, run_id: str) -> tuple[str, str, str]:
    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            approval = conn.execute(
                sa.select(ne_approval_requests.c.status).where(
                    ne_approval_requests.c.request_id == approval_id
                )
            ).scalar_one()
            checkpoint = conn.execute(
                sa.select(ne_agent_run_continuation_checkpoints.c.status).where(
                    ne_agent_run_continuation_checkpoints.c.run_id == run_id
                )
            ).scalar_one()
            run_status = conn.execute(
                sa.select(ne_agent_runs.c.status).where(ne_agent_runs.c.run_id == run_id)
            ).scalar_one()
        return str(approval), str(checkpoint), str(run_status)
    finally:
        engine.dispose()


def _completed_envelope(output: bytes) -> Any:
    import hashlib

    from ne.runtime.run_finalizer import StructuredRunOutputEnvelope, claim_set_hash

    output_hash = hashlib.sha256(output).hexdigest()
    return StructuredRunOutputEnvelope(
        discriminator="structured_run_output",
        served_output_hash=output_hash,
        claim_count=0,
        claim_set_hash=claim_set_hash(output_hash, ()),
        claims=(),
        disposition="completed",
    )


def _admit_four_sources(state: Any) -> Any:
    labels = (APPROVER_GROUP, OWNER)
    stamps = []
    for index in range(4):
        _receipt, stamp = admit_current_source(
            state.source_authority_reader,
            key=f"s5-src-{index}-{time.monotonic_ns()}",
            labels=labels,
            content=f"protected evidence {index}".encode(),
        )
        stamps.append(stamp)
    return canonical_source_authority_stamps(stamps)


def _build_service(state: Any, runtime_url: str, adapter: Any) -> tuple[Any, Any]:
    engine = sa.create_engine(runtime_url)
    service = RunWorkflowService(
        state=RunWorkflowStateStore(engine, run_store=state.agent_run_store),
        run_store=state.agent_run_store,
        sink=state.sink,
        audit_engine=state.audit_engine,
        gateway=state.gateway,
        executor=state.executor,
        adapter=adapter,
        config_hash=state.config_hash,
        checkpoints=state.gate._run_approval_checkpoints,
        source_guard=state.source_guard,
    )
    return service, engine


def _workflow_request(parked: Any) -> RunWorkflowRequest:
    return RunWorkflowRequest(
        dispatch_id=parked.dispatch_id,
        run_id=parked.run_id,
        owner_principal_id=OWNER,
        config_hash=parked.config_hash,
        target=RunTurnTarget(
            conversation_id=parked.conversation_id,
            run_id=parked.run_id,
            turn_id=parked.turn_id,
            turn_sequence=parked.turn_sequence,
            attachment_id=parked.attachment_id,
            attempt_id=parked.attempt_id,
        ),
        authenticated_context=parked.owner_context,
    )


def _load_target(state: Any, parked: Any) -> None:
    from ne.runtime.run_store import ne_agent_run_turn_targets

    engine = state.agent_run_store._engine
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_agent_run_turn_targets).where(
                ne_agent_run_turn_targets.c.run_id == parked.run_id,
                ne_agent_run_turn_targets.c.attempt_id == parked.attempt_id,
            )
        ).one()
    parked.turn_id = str(row.turn_id)
    parked.turn_sequence = int(row.turn_sequence)
    parked.attachment_id = str(row.attachment_id)


async def _admit_run(state: Any) -> Any:
    conversation = await state.session_store.create_conversation(AGENT, DEPARTMENT, OWNER)
    owner_context = AuthenticatedRequestContext(
        principal_id=OWNER,
        authorization_groups=(),
        department=DEPARTMENT,
        issuer=ISSUER,
        expires_at=int(time.time()) + 3600,
    )
    accepted = await state.agent_run_store.admit_start(
        SimpleNamespace(
            idempotency_key=f"s5-{uuid.uuid4().hex}",
            request_hash="1" * 64,
            conversation_id=conversation.conversation_id,
            expected_version=0,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            conversation_agent_name=AGENT,
            agent_id=AGENT,
            agent_version=3,
            strategy="agentic",
            config_hash=state.config_hash,
            capability_snapshot_hash="2" * 64,
            capability_snapshot_receipt_id="capability-receipt",
            input_plaintext=b"S5: send the outreach email for mer-loan-001",
            agent_definition_hash="d" * 64,
            model_policy_id="agent.meridian-analyst.v2",
            authorized_model="private-primary",
            authorized_model_lane="private-vllm",
            model_authorization_receipt_hash="e" * 64,
            target_input_budget=16_384,
            requested_model=None,
            requested_model_lane=None,
            authenticated_context=owner_context,
        )
    )
    return SimpleNamespace(
        owner_context=owner_context,
        run_id=accepted.run_id,
        attempt_id=accepted.attempt_id,
        dispatch_id=accepted.enqueue_id,
        conversation_id=conversation.conversation_id,
        config_hash=state.config_hash,
        approval_id="",
        turn_id="",
        turn_sequence=0,
        attachment_id="",
    )


def test_s5_park_approve_send_end_to_end(make_app: AppBuilder, tmp_path: Path) -> None:
    """Fresh S5: internal contact read → real draft → PARK → approve → SEND.

    No Salesforce client exists anywhere in this composition. The send is the
    REAL ``email_send_definition`` over the REAL ``GraphWriteClient``; the only
    double is the Graph HTTP wire itself, which records the send POST.
    """
    graph_requests: list[httpx.Request] = []
    graph = GraphWriteClient(
        GraphClient(_m365_settings(), transport=_graph_transport(graph_requests))
    )

    # The REAL internal lookup — over the SAME chunk store instance the app
    # composes (make_app hands the store to the platform and to us).
    from .conftest import InMemoryFilteredChunkStore

    store = InMemoryFilteredChunkStore((_contact_chunk(), _account_chunk()))
    lookup = InternalCrmLookupClient(store)
    # The REAL internal note client over its own durable engine (the platform
    # composes it over the runtime engine; the seam form is identical).
    note_engine = sa.create_engine(f"sqlite:///{tmp_path}/s5-notes-{uuid.uuid4().hex}.db")

    h = make_app(
        chunk_store=store,
        approver_roles=(APPROVER_GROUP,),
        protected_transport=True,
        write_action_tools=(DRAFT_TOOL, SEND_TOOL, NOTE_TOOL),
        email_draft_allowed_mailboxes=(DEMO_MAILBOX,),
        email_send_allowed_recipients=(DEMO_RECIPIENT,),
        write_action_clients=WriteActionClients(
            crm=InternalCrmNoteClient(note_engine),
            ticket=_UnusableTicketClient(),
            draft=graph,
            send=graph,
            lookup=lookup,
            contact_lookup=lookup,
        ),
        govern_settings=GovernSettings(
            guardrails=GuardrailSettings(
                recipients=RecipientSettings(allowed_recipients=(DEMO_RECIPIENT,))
            )
        ),
    )
    try:
        state = get_state(h.app)

        class _RecordingEnqueuer:
            """The DBOS transport double: records the resume request (the worker
            side is driven explicitly below, as in the resume-continuation tests)."""

            def __init__(self) -> None:
                self.requests: list[Any] = []

            async def enqueue_approval_resume(self, request: Any) -> None:
                self.requests.append(request)

        state.agent_run_enqueuer = _RecordingEnqueuer()
        ensure_internal_crm_schema(note_engine)
        registered = {s.name for s in state.executor.tool_specs}
        assert {CONTACT_TOOL, DRAFT_TOOL, SEND_TOOL, NOTE_TOOL} <= registered

        stamps = _admit_four_sources(state)
        checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]
        assert checkpoints is not None
        parked = run(_admit_run(state))
        _load_target(state, parked)

        looked_up: list[str] = []

        class _S5ParkingAdapter:
            """The S5 pre-park ladder, executed through the REAL governed executor.

            Contact read (Tier-0, INTERNAL) then draft create (real Graph write
            action) then the Tier-2 send — parked on the exact server-minted
            identity of the attempt's THIRD governed call, with the real
            ``create_wait``, exactly as the production gate parks it.
            """

            def __init__(self) -> None:
                self.joined: Any = None

            async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
                principal = Principal(id=context.run.owner_principal_id, groups=())
                now = datetime.now(UTC)
                with action_scope(f"agent-run:{context.run.run_id}:{context.attempt_id}"):
                    contact_json = await context.executor.execute(
                        CONTACT_TOOL, {"name": "Avery Arbor"}, principal=principal
                    )
                    contact = json.loads(contact_json)
                    assert contact["found"] is True
                    looked_up.append(contact["email"])
                    draft_json = await context.executor.execute(
                        DRAFT_TOOL,
                        {
                            "subject": "Meridian hardship outreach",
                            "body": "We can help — reply to this email.",
                            "to": contact["email"],
                        },
                        principal=principal,
                    )
                    draft = json.loads(draft_json)
                    assert draft["succeeded"] is True
                    assert draft["source_ref"] == GRAPH_MESSAGE_ID
                    # The send is the attempt's THIRD governed call: ordinal 2.
                    step_id = "run-tool"
                    tool_call_id = f"{step_id}:tool:2"
                    self.joined = await checkpoints.create_wait(
                        run_id=context.run.run_id,
                        attempt_id=context.attempt_id,
                        step_id=step_id,
                        tool_call_id=tool_call_id,
                        action=SEND_TOOL,
                        action_hash=canonical_args_hash(_SEND_ARGS),
                        tier=2,
                        principal_id=OWNER,
                        policy_hash="e" * 64,
                        invocation_plaintext=json.dumps(
                            _SEND_ARGS, sort_keys=True, separators=(",", ":")
                        ).encode("utf-8"),
                        source_stamps=stamps,
                        stamp_receipt_id=f"stamp-{context.run.run_id}",
                        stamp_receipt_hash="f" * 64,
                        requested_at=now,
                        sla_deadline=now + timedelta(minutes=5),
                        expires_at=now + timedelta(hours=1),
                        event=new_approval_event(
                            approval_id="allocated-inside-transaction",
                            tier=2,
                            decision=ApprovalDecision.requested,
                            subject_ref=f"tool:{SEND_TOOL}",
                            actor=OWNER,
                        ),
                    )
                    raise RunApprovalWaiting(self.joined)

        adapter = _S5ParkingAdapter()
        service, engine = _build_service(state, h.runtime_url, adapter)
        try:
            with authenticated_request_scope(parked.owner_context):
                result = run(service.execute(_workflow_request(parked)))
            assert result.outcome == "waiting_approval"
        finally:
            engine.dispose()
        parked.approval_id = adapter.joined.approval_id

        # The contact was read INTERNALLY and no wire request went anywhere but
        # the Graph edge: exactly one draft POST so far, zero sends.
        assert looked_up == [DEMO_RECIPIENT]
        assert _send_posts(graph_requests) == []

        # --- approve over the REAL HTTP route ---------------------------------
        async def _approve() -> Any:
            async with h.client() as client:
                headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])
                return await client.post(
                    f"/api/v1/approvals/{parked.approval_id}/approve", json={}, headers=headers
                )

        decided = run(_approve())
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "approved"

        # The worker's resume-side CAS: approved -> resumed.
        async def _resume_checkpoint() -> None:
            joined = await checkpoints.get_join(parked.approval_id)
            await checkpoints.resume(
                parked.approval_id,
                expected_action_hash=joined.action_hash,
                principal_id=joined.principal_id,
                policy_hash=joined.policy_hash,
                resumed_at=datetime.now(UTC),
            )

        run(_resume_checkpoint())

        class _ContinuationAdapter:
            """Mirrors ``ProfileBoundRunExecutionAdapter._execute_effect_continuation``,
            including the worker's ambient ``action_scope`` wrap."""

            def __init__(self) -> None:
                self.continuations = 0
                self.loop_entries = 0

            async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
                principal = Principal(id=context.run.owner_principal_id, groups=())
                continuation = getattr(context, "continuation", None)
                if continuation is None:
                    self.loop_entries += 1
                    raise AssertionError("the resume must never re-enter the agent loop")
                self.continuations += 1
                with (
                    action_scope(f"agent-run:{context.run.run_id}:{context.attempt_id}"),
                    direct_publication_scope(
                        workflow_execution_id=context.run.run_id,
                        publication_attempt_id=context.attempt_id,
                    ),
                ):
                    await context.executor.execute(
                        continuation.action, continuation.arguments, principal=principal
                    )
                    complete_run_direct_publication_observation()
                output = b"the approved action was executed after human approval"
                return RunExecutionResult(
                    outcome="execution_complete",
                    output_bytes=output,
                    structured_envelope=_completed_envelope(output),
                )

        resume_adapter = _ContinuationAdapter()
        service, engine = _build_service(state, h.runtime_url, resume_adapter)
        try:
            with authenticated_request_scope(parked.owner_context):
                resumed = run(service.execute(_workflow_request(parked)))
        finally:
            engine.dispose()

        # THE EMAIL REALLY SENT: exactly one POST .../messages/{id}/send on the
        # wire, released by the approval, addressed to the looked-up recipient.
        #
        # POST_DEMO item 4 changed this word, and S5 is exactly the shape it is
        # about: the run parked on a Tier-2 send, a human approved, and the resume
        # executed the pinned effect WITHOUT replaying the agent loop — which the
        # very next assertion pins as (1 continuation, 0 loop entries). So whatever
        # the plan intended after the send did not run, and `execution_complete` —
        # the word a run that finished its whole plan uses — claimed more than
        # happened. `status` is untouched; only the record of HOW it completed is
        # more honest than it was.
        assert resumed.outcome == "approved_effect_complete"
        assert (resume_adapter.continuations, resume_adapter.loop_entries) == (1, 0)
        sends = _send_posts(graph_requests)
        assert len(sends) == 1
        assert f"/messages/{GRAPH_MESSAGE_ID}/send" in sends[0].url.path
        assert DEMO_MAILBOX.replace("@", "%40") in str(sends[0].url)

        approval, checkpoint, run_status = _statuses(
            h.runtime_url, parked.approval_id, parked.run_id
        )
        assert approval == "approved"
        # `consumed`, not `resumed`: the continuation's ONE permitted execution
        # crossed its durable fence before the send — a `resumed` row left
        # behind is exactly what allowed one approval to send N times.
        assert checkpoint == "consumed"
        assert run_status == RunState.COMPLETED.value
    finally:
        note_engine.dispose()
