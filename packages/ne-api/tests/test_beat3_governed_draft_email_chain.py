# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The governed mail chain, end to end: Tier-0 draft, Tier-2 REAL send (NE-CS-SEND).

Two beats, one file (owner decision 2026-08-27):

* ``draft.email.create`` is Tier-0 — a governed turn creates ONE unsent Graph
  DRAFT autonomously (logged, no human gate, zero sends of any shape);
* ``write.external.email.send`` is Tier-2 — the follow-up turn PARKS on the real
  durable approval gate, a DISTINCT approver decides over HTTP, an approval
  resumes into the read-back-verified REAL ``POST .../messages/{id}/send``, and
  a rejection (or expiry) reaches Graph not at all.

Everything above the httpx wire runs for real here: the real
:func:`ne.api.platform.build_platform` composition, the real
``NE_API_WRITE_ACTION_TOOLS`` selector, the real
:class:`~ne.api.platform._AllowedDraftMailClient` mailbox wrapper, the real
``ne.connect.m365.write.GraphWriteClient``, the real Rego-faithful PDP, the real
guardrail engine, the real ``DurableApprovalGate`` and the real HTTP approvals
router. ONLY the Graph network edge is an ``httpx.MockTransport`` — the same
"no mock past the governance seam" discipline as
``packages/ne-runtime/tests/_write_fixtures.py`` (which ne-api tests may not
import: cross-package test imports are banned, so the two Graph routes are
re-scripted locally here).

WHAT THIS FILE IS NOT
---------------------
This is a LOGIC proof, not live acceptance evidence. It proves that when the
candidate overlay finally supplies the configuration, the code path already
works and only the configuration is new. It does not, and must never be read
to, prove that a draft was created in a real Microsoft 365 tenant. The live
beat still requires the real provider showing exactly one message with
``isDraft`` true.

The mailbox fixture is the non-secret identity already on record for the demo
(``owner@contoso.onmicrosoft.com``); the recipient is the demo's exact
pinned guardrail recipient (``owner@example.com`` — mer-cus-001's fixture email and
the owner's demo inbox). No credential,
token or secret value appears in this file.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from pydantic import SecretStr

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
from ne.connect.errors import GraphSyncError
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.graph import GraphClient
from ne.connect.m365.write import GraphWriteClient
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import ApprovalTier, Lane, Principal
from ne.gateway.config import GatewaySettings, ModelDef
from ne.govern.config import (
    GovernSettings,
    GuardrailSettings,
    RecipientSettings,
)
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import ApprovalSettings, RuntimeSettings

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

_EMAIL_DRAFT_TOOL = "draft.email.create"
_EMAIL_SEND_TOOL = "write.external.email.send"

#: The non-secret mailbox fixture on record for the pre-demo candidate overlay.
DEMO_MAILBOX = "owner@contoso.onmicrosoft.com"
#: The same identity as an Entra object GUID (the other accepted mailbox shape).
DEMO_MAILBOX_OBJECT_ID = "5973b0d2-d572-4bc2-a45f-b491b152e0aa"
#: The demo's exact pinned recipient guardrail value (also mer-cus-001's
#: fixture email — the outreach recipient is looked up, never prompt-supplied).
DEMO_RECIPIENT = "owner@example.com"
#: The id the scripted Graph edge attests for a created draft.
GRAPH_MESSAGE_ID = "AAMkAG-beat3-draft-message-id"

_TOKEN_SUFFIX = "/oauth2/v2.0/token"


# --- the Graph wire edge (the ONLY mock in this file) -------------------------


def _m365_settings() -> M365ConnectorSettings:
    """Non-secret connector settings; the credential is an obvious fake."""
    return M365ConnectorSettings(
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        auth_method="secret",
        client_secret=SecretStr("fake-secret-not-real"),
        site_ids=("site-meridian",),
        owner="data-eng@contoso.test",
    )


def _graph_transport(
    recorder: list[httpx.Request],
    *,
    is_draft: bool = True,
    message_id: str = GRAPH_MESSAGE_ID,
    draft_recipients: tuple[str, ...] = (DEMO_RECIPIENT,),
) -> httpx.MockTransport:
    """Script the exact Graph routes the two governed actions use, and no more.

    Token acquisition, message create (the draft), the per-message read-back
    (``GET .../messages/{id}`` — what the send action verifies recipients
    against), and the send action endpoint (``POST .../messages/{id}/send``,
    empty 202 — exactly Graph's attestation). ``sendMail`` stays unscripted: no
    code path may compose a fresh outbound message.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        if str(request.url).endswith(_TOKEN_SUFFIX):
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
        if request.url.path.endswith("/messages") and request.method == "POST":
            return httpx.Response(201, json={"id": message_id, "isDraft": is_draft})
        if request.url.path.endswith(f"/messages/{message_id}/send") and request.method == "POST":
            return httpx.Response(202)
        if request.url.path.endswith(f"/messages/{message_id}") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "isDraft": is_draft,
                    "toRecipients": [
                        {"emailAddress": {"address": address}} for address in draft_recipients
                    ],
                    "ccRecipients": [],
                    "bccRecipients": [],
                },
            )
        return httpx.Response(404, json={"error": {"code": "notFound"}})

    return httpx.MockTransport(handler)


def _graph_write_client(
    recorder: list[httpx.Request],
    *,
    is_draft: bool = True,
    message_id: str = GRAPH_MESSAGE_ID,
    draft_recipients: tuple[str, ...] = (DEMO_RECIPIENT,),
) -> GraphWriteClient:
    """The REAL ne-connect Graph write client over the scripted edge."""
    return GraphWriteClient(
        GraphClient(
            _m365_settings(),
            transport=_graph_transport(
                recorder,
                is_draft=is_draft,
                message_id=message_id,
                draft_recipients=draft_recipients,
            ),
        )
    )


def _send_posts(recorder: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in recorder if r.method == "POST" and r.url.path.endswith("/send")]


def _message_posts(recorder: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in recorder if r.method == "POST" and r.url.path.endswith("/messages")]


def _assert_draft_url(request: httpx.Request) -> None:
    """The URL must name the CANONICAL (normalized, percent-encoded) mailbox."""
    assert request.url.path == f"/v1.0/users/{DEMO_MAILBOX}/messages"
    assert DEMO_MAILBOX.replace("@", "%40") in str(request.url)


def _assert_zero_sends(recorder: list[httpx.Request]) -> None:
    """No request in THIS scenario may resemble a SEND.

    Used by the draft-side tests: the Tier-0 draft action must never send, and
    ``sendMail`` (composing a fresh outbound message) stays prohibited outright
    everywhere — the Tier-2 send action only ever sends an EXISTING verified
    draft via ``.../messages/{id}/send``, asserted by its own tests.
    """
    for request in recorder:
        assert "sendMail" not in str(request.url), (
            f"a send-shaped request was issued: {request.url}"
        )
        assert "/send" not in request.url.path, f"a send-shaped request was issued: {request.url}"


# --- self-hosted composition harness -----------------------------------------


class _UnusableCrmClient:
    """Present only to satisfy the 3-field seam dataclass; must never be called."""

    async def create_record(self, sobject: str, fields: Mapping[str, str]) -> str:
        raise AssertionError("beat 3 must not touch the CRM leg")


class _UnusableTicketClient:
    async def update_record(
        self, *, table: str, key_column: str, key_value: int | str, fields: Mapping[str, str]
    ) -> int:
        raise AssertionError("beat 3 must not touch the ticket leg")


def _draft_only_clients(draft: Any) -> WriteActionClients:
    return WriteActionClients(crm=_UnusableCrmClient(), ticket=_UnusableTicketClient(), draft=draft)


def _mail_clients(graph: Any) -> WriteActionClients:
    """Draft + send legs over ONE Graph client (the production composition shape)."""
    return WriteActionClients(
        crm=_UnusableCrmClient(), ticket=_UnusableTicketClient(), draft=graph, send=graph
    )


def _api_settings(**overrides: object) -> ApiSettings:
    base: dict[str, object] = {
        "qdrant_url": "http://qdrant.test",
        "qdrant_collection": "t",
        "tei_url": "http://tei.test",
    }
    base.update(overrides)
    return ApiSettings(**base)  # type: ignore[arg-type]


def _draft_settings(**overrides: object) -> ApiSettings:
    """The draft-only selection + one exact mailbox."""
    base: dict[str, object] = {
        "write_action_tools": (_EMAIL_DRAFT_TOOL,),
        "email_draft_allowed_mailboxes": (DEMO_MAILBOX,),
    }
    base.update(overrides)
    return _api_settings(**base)


def _mail_settings(**overrides: object) -> ApiSettings:
    """The NE-CS-SEND graph-mode selection: draft + send, both allowlists exact."""
    base: dict[str, object] = {
        "write_action_tools": (_EMAIL_DRAFT_TOOL, _EMAIL_SEND_TOOL),
        "email_draft_allowed_mailboxes": (DEMO_MAILBOX,),
        "email_send_allowed_recipients": (DEMO_RECIPIENT,),
    }
    base.update(overrides)
    return _api_settings(**base)


def _govern_settings() -> GovernSettings:
    """The demo's pinned recipient allowlist (deny-by-default everywhere else)."""
    return GovernSettings(
        guardrails=GuardrailSettings(
            recipients=RecipientSettings(allowed_recipients=(DEMO_RECIPIENT,))
        )
    )


def _seams(
    tmp_path: Path,
    *,
    key: str,
    gateway: Any | None = None,
    write_action_clients: WriteActionClients | None = None,
    approvals: ApprovalSettings | None = None,
    govern_settings: GovernSettings | None = None,
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
        govern_settings=govern_settings,
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
    )
    return seams, verifier, runtime_url, audit_url


@pytest.fixture
def engines() -> Any:
    started: list[Any] = []
    yield started
    for engine in started:
        engine.dispose()


# --- (A) the config precondition: selector + exact mailbox allowlist ----------


def test_candidate_overlay_env_strings_parse_to_the_exact_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overlay emits JSON strings; pin that they parse to the exact tuple.

    These are the LITERAL values ``ne.cli.demo.live`` writes into the candidate
    overlay (``NE_API_WRITE_ACTION_TOOLS`` / ``NE_API_EMAIL_DRAFT_ALLOWED_MAILBOXES``).
    A rename or a shape change on either side breaks here rather than at a live
    boot inside the transition window.
    """
    monkeypatch.setenv("NE_API_QDRANT_URL", "http://qdrant.test")
    monkeypatch.setenv("NE_API_QDRANT_COLLECTION", "t")
    monkeypatch.setenv("NE_API_TEI_URL", "http://tei.test")
    monkeypatch.setenv(
        "NE_API_WRITE_ACTION_TOOLS", json.dumps([_EMAIL_DRAFT_TOOL, _EMAIL_SEND_TOOL])
    )
    monkeypatch.setenv("NE_API_EMAIL_DRAFT_ALLOWED_MAILBOXES", json.dumps([DEMO_MAILBOX.upper()]))
    monkeypatch.setenv("NE_API_EMAIL_SEND_ALLOWED_RECIPIENTS", json.dumps([DEMO_RECIPIENT.upper()]))

    settings = ApiSettings()  # type: ignore[call-arg]

    assert settings.write_action_tools == (_EMAIL_DRAFT_TOOL, _EMAIL_SEND_TOOL)
    assert settings.write_actions_enabled is False
    # Normalization is part of the destination authority: the wrapper compares
    # casefolded, so the frozen config must already be casefolded.
    assert settings.email_draft_allowed_mailboxes == (DEMO_MAILBOX,)
    assert settings.email_send_allowed_recipients == (DEMO_RECIPIENT,)


def test_draft_selection_without_a_mailbox_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="requires at least one"):
        _api_settings(write_action_tools=(_EMAIL_DRAFT_TOOL,))


def test_mail_platform_registers_the_ladder_tiers(tmp_path: Path, engines: list[Any]) -> None:
    """NE-CS-SEND: draft registers Tier-0 (autonomous), send Tier-2 (human-gated)."""
    reset_config_hash_for_testing()
    recorder: list[httpx.Request] = []
    seams, _, _, _ = _seams(
        tmp_path,
        key="beat3-compose",
        write_action_clients=_mail_clients(_graph_write_client(recorder)),
        govern_settings=_govern_settings(),
    )
    platform = build_platform(_mail_settings(), seams)
    engines.extend(platform.engines)

    specs = {spec.name: spec for spec in platform.executor.tool_specs}
    assert _EMAIL_DRAFT_TOOL in specs
    assert specs[_EMAIL_DRAFT_TOOL].tier is ApprovalTier.TIER_0
    assert _EMAIL_SEND_TOOL in specs
    assert specs[_EMAIL_SEND_TOOL].tier is ApprovalTier.TIER_2
    # The arg policies MUST be merged before the freeze or the calls are rejected.
    assert platform.govern_settings.guardrails.tools[_EMAIL_DRAFT_TOOL]
    assert platform.govern_settings.guardrails.tools[_EMAIL_SEND_TOOL]
    assert platform.govern_settings.guardrails.recipients.allowed_recipients == (DEMO_RECIPIENT,)
    # Composition alone must not touch the network.
    assert recorder == []


def test_mailbox_allowlist_is_config_hashed(tmp_path: Path, engines: list[Any]) -> None:
    """Two deployments differing ONLY in the destination mailbox must not agree.

    Destination authority has to be visible in the frozen hash, or a
    configuration-drift check cannot tell a demo mailbox from a foreign one.
    """
    reset_config_hash_for_testing()
    seams_a, _, _, _ = _seams(
        tmp_path,
        key="beat3-hash-a",
        write_action_clients=_draft_only_clients(_graph_write_client([])),
        govern_settings=_govern_settings(),
    )
    first = build_platform(_draft_settings(), seams_a)
    engines.extend(first.engines)

    reset_config_hash_for_testing()
    seams_b, _, _, _ = _seams(
        tmp_path,
        key="beat3-hash-b",
        write_action_clients=_draft_only_clients(_graph_write_client([])),
        govern_settings=_govern_settings(),
    )
    second = build_platform(
        _draft_settings(email_draft_allowed_mailboxes=(DEMO_MAILBOX_OBJECT_ID,)), seams_b
    )
    engines.extend(second.engines)

    assert first.config_hash != second.config_hash


# --- (B) the mailbox wrapper refuses BEFORE any token acquisition -------------


def test_allowlisted_mailbox_reaches_graph_and_returns_the_draft_id() -> None:
    recorder: list[httpx.Request] = []
    client = _AllowedDraftMailClient(_graph_write_client(recorder), (DEMO_MAILBOX,))

    message_id = run(
        client.create_draft(
            # Mixed case on the way in: the wrapper normalizes before comparing
            # AND before forwarding, so the URL carries the canonical mailbox.
            mailbox=DEMO_MAILBOX.upper(),
            subject="Hardship review follow-up",
            body="A drafted follow-up for review.",
            to=DEMO_RECIPIENT,
        )
    )

    assert message_id == GRAPH_MESSAGE_ID
    posts = _message_posts(recorder)
    assert len(posts) == 1
    _assert_draft_url(posts[0])
    payload = json.loads(posts[0].content)
    assert payload["toRecipients"] == [{"emailAddress": {"address": DEMO_RECIPIENT}}]
    _assert_zero_sends(recorder)


def test_foreign_mailbox_is_refused_before_graph_token_acquisition() -> None:
    """The whole point of the allowlist: no token spend on a foreign mailbox.

    An EMPTY recorder is the proof — not merely "no message was created", but
    "no Graph conversation of any kind began", token endpoint included.
    """
    recorder: list[httpx.Request] = []
    client = _AllowedDraftMailClient(_graph_write_client(recorder), (DEMO_MAILBOX,))

    with pytest.raises(ValueError, match="outside the configured mailbox allowlist"):
        run(
            client.create_draft(
                mailbox="someone-else@contoso.onmicrosoft.com",
                subject="s",
                body="b",
                to=DEMO_RECIPIENT,
            )
        )

    assert recorder == []


def test_empty_allowlist_refuses_every_mailbox() -> None:
    recorder: list[httpx.Request] = []
    client = _AllowedDraftMailClient(_graph_write_client(recorder), ())

    with pytest.raises(ValueError, match="outside the configured mailbox allowlist"):
        run(client.create_draft(mailbox=DEMO_MAILBOX, subject="s", body="b", to=DEMO_RECIPIENT))

    assert recorder == []


def test_graph_must_attest_an_unsent_draft() -> None:
    """A 201 without ``isDraft`` true is NOT a draft, and must not be reported as one."""
    recorder: list[httpx.Request] = []
    client = _AllowedDraftMailClient(_graph_write_client(recorder, is_draft=False), (DEMO_MAILBOX,))

    with pytest.raises(GraphSyncError, match="did not attest creation of an unsent draft"):
        run(client.create_draft(mailbox=DEMO_MAILBOX, subject="s", body="b", to=DEMO_RECIPIENT))

    _assert_zero_sends(recorder)


def test_graph_must_return_a_non_empty_message_id() -> None:
    recorder: list[httpx.Request] = []
    client = _AllowedDraftMailClient(_graph_write_client(recorder, message_id=""), (DEMO_MAILBOX,))

    with pytest.raises(GraphSyncError, match="did not attest creation of an unsent draft"):
        run(client.create_draft(mailbox=DEMO_MAILBOX, subject="s", body="b", to=DEMO_RECIPIENT))

    _assert_zero_sends(recorder)


# --- (C) the full HTTP arc: park -> approve -> resume -> ONE draft ------------


def _draft_args(*, to: str = DEMO_RECIPIENT, mailbox: str = DEMO_MAILBOX) -> dict[str, str]:
    return {
        "mailbox": mailbox,
        "subject": "Hardship review follow-up",
        "body": "A drafted follow-up for review. Nothing is sent.",
        "to": to,
    }


def _drive_turn(
    app: Any,
    verifier: FakeTokenVerifier,
    *,
    expect_pending: bool = True,
    pending_action: str = _EMAIL_SEND_TOOL,
    prompt: str = "draft the hardship follow-up",
    decision: str = "approve",
) -> tuple[httpx.Response, list[dict[str, Any]]]:
    """Run one chat turn; decide the Tier-2 gate for ``pending_action`` when it appears."""
    seen: list[dict[str, Any]] = []

    async def _t() -> httpx.Response:
        loop = asyncio.get_running_loop()
        analyst_token = verifier.mint("user:analyst", groups=["eng"], department="servicing")
        analyst = {"Authorization": f"Bearer {analyst_token}"}
        approver_headers = {
            "Authorization": f"Bearer {verifier.mint('user:approver', groups=['approvers'])}"
        }
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://ne.test") as writer,
            httpx.AsyncClient(transport=transport, base_url="http://ne.test") as approver,
        ):
            created = await writer.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=analyst
            )
            assert created.status_code == 200, created.text
            sid = created.json()["session_id"]

            task = asyncio.create_task(
                writer.post(
                    f"/api/v1/chat/sessions/{sid}/messages",
                    json={"prompt": prompt},
                    headers=analyst,
                )
            )

            if expect_pending:
                deadline = loop.time() + 10.0
                pending = None
                while loop.time() < deadline:
                    listed = await approver.get(
                        "/api/v1/approvals/pending", headers=approver_headers
                    )
                    assert listed.status_code == 200, listed.text
                    match = [
                        row for row in listed.json()["approvals"] if row["action"] == pending_action
                    ]
                    if match:
                        pending = match[0]
                        break
                    await asyncio.sleep(0.05)
                assert pending is not None, "the Tier-2 approval never reached the queue"
                seen.append(pending)
                # The analyst who asked is NOT the approver who releases it.
                assert pending["principal_id"] == "user:analyst"
                decided = await approver.post(
                    f"/api/v1/approvals/{pending['request_id']}/{decision}",
                    json={"reason": "no"} if decision == "reject" else {},
                    headers=approver_headers,
                )
                assert decided.status_code == 200, decided.text
                assert decided.json()["status"] == (
                    "approved" if decision == "approve" else "rejected"
                )

            return await task

    return run(_t()), seen


def test_tier0_draft_runs_autonomously_and_creates_exactly_one_unsent_draft(
    tmp_path: Path,
) -> None:
    """NE-CS-SEND: the draft is Tier-0 — no park, no approver, one unsent draft.

    The turn completes WITHOUT any pending approval appearing; exactly ONE
    ``POST /users/{mailbox}/messages``; ``isDraft`` true; zero sends of any
    shape; the tool.execute audit row records the write at its declared tier 0.
    """
    reset_config_hash_for_testing()
    recorder: list[httpx.Request] = []
    gateway = ScriptedGateway(
        [
            tool_call_completion(_EMAIL_DRAFT_TOOL, _draft_args()),
            text_completion("drafted"),
        ]
    )
    seams, verifier, _runtime_url, audit_url = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        gateway=gateway,
        write_action_clients=_draft_only_clients(_graph_write_client(recorder)),
        approvals=ApprovalSettings(poll_interval_s=0.05),
        govern_settings=_govern_settings(),
    )
    app = create_app(_draft_settings(approver_roles=("approvers",)), seams=seams)
    state = get_state(app)

    sse, pending = _drive_turn(app, verifier, expect_pending=False)

    assert sse.status_code == 200, sse.text
    done = parse_sse(sse.text).of_type("done")
    assert done, "no done frame — the autonomous draft turn did not complete"
    assert done[0].data is not None
    assert done[0].data["output"] == "drafted"
    assert pending == []

    # Exactly ONE draft was created, at the allowlisted mailbox — and NO send.
    posts = _message_posts(recorder)
    assert len(posts) == 1, f"expected exactly one draft create, saw {len(posts)}"
    _assert_draft_url(posts[0])
    _assert_zero_sends(recorder)

    audit_engine = sa.create_engine(audit_url)
    try:
        with audit_engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    ne_audit_log.c.action,
                    ne_audit_log.c.outcome,
                    ne_audit_log.c.resource,
                    ne_audit_log.c.payload,
                )
            ).all()
    finally:
        audit_engine.dispose()
    executed = [
        row
        for row in rows
        if row.action == "tool.execute"
        and row.outcome == "success"
        and row.resource == f"tool:{_EMAIL_DRAFT_TOOL}"
    ]
    assert executed, "no tool.execute[success] audit row for tool:draft.email.create"
    detail = json.loads(executed[-1].payload["detail"])
    # C7 write metadata: the SHAPE and the target ref, never the body content.
    assert detail["action_kind"] == "write"
    assert detail["source_system"] == "m365"
    assert detail["tier"] == 0
    assert detail["target"] == GRAPH_MESSAGE_ID
    assert detail["deduplicated"] is False
    assert sorted(detail["fields"]) == ["body", "mailbox", "request_id", "subject", "to"]
    # The drafted BODY must never appear in the audit payload.
    assert "A drafted follow-up" not in json.dumps(detail)

    for engine in state.engines:
        engine.dispose()


def test_tier2_park_approve_resume_really_sends_the_draft(tmp_path: Path) -> None:
    """NE-CS-SEND, end to end, with only the Graph wire faked.

    Park (Tier-2, distinct approver) -> approve over real HTTP -> resume -> the
    action reads the draft's REAL recipients back from Graph, checks them
    against the send allowlist, and issues exactly ONE
    ``POST .../messages/{id}/send``.
    """
    reset_config_hash_for_testing()
    recorder: list[httpx.Request] = []
    gateway = ScriptedGateway(
        [
            tool_call_completion(
                _EMAIL_SEND_TOOL, {"mailbox": DEMO_MAILBOX, "draft_ref": GRAPH_MESSAGE_ID}
            ),
            text_completion("sent"),
        ]
    )
    seams, verifier, _runtime_url, audit_url = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        gateway=gateway,
        write_action_clients=_mail_clients(_graph_write_client(recorder)),
        approvals=ApprovalSettings(poll_interval_s=0.05),
        govern_settings=_govern_settings(),
    )
    app = create_app(_mail_settings(approver_roles=("approvers",)), seams=seams)
    state = get_state(app)

    sse, pending = _drive_turn(app, verifier, prompt="send the approved outreach")

    assert sse.status_code == 200, sse.text
    done = parse_sse(sse.text).of_type("done")
    assert done, "no done frame — the turn was never released by the approval"
    assert done[0].data is not None
    assert done[0].data["output"] == "sent"

    # (1) The approver saw a Tier-2 request naming the exact action.
    assert pending[0]["action"] == _EMAIL_SEND_TOOL
    assert int(pending[0]["tier"]) == 2

    # (2) Exactly ONE send was issued, for the exact draft, and no new message
    # was composed (zero /messages POSTs).
    sends = _send_posts(recorder)
    assert len(sends) == 1, f"expected exactly one send, saw {len(sends)}"
    assert f"/messages/{GRAPH_MESSAGE_ID}/send" in sends[0].url.path
    assert _message_posts(recorder) == []
    # (3) The recipient read-back happened BEFORE the send.
    reads = [r for r in recorder if r.method == "GET" and GRAPH_MESSAGE_ID in r.url.path]
    assert reads, "the send action must read the draft's recipients back from Graph"
    assert recorder.index(reads[0]) < recorder.index(sends[0])

    # (4) The audit ledger records the executed send at tier 2 with an approval.
    audit_engine = sa.create_engine(audit_url)
    try:
        with audit_engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    ne_audit_log.c.action,
                    ne_audit_log.c.outcome,
                    ne_audit_log.c.resource,
                    ne_audit_log.c.payload,
                )
            ).all()
    finally:
        audit_engine.dispose()
    executed = [
        row
        for row in rows
        if row.action == "tool.execute"
        and row.outcome == "success"
        and row.resource == f"tool:{_EMAIL_SEND_TOOL}"
    ]
    assert executed, "no tool.execute[success] audit row for the send"
    detail = json.loads(executed[-1].payload["detail"])
    assert detail["action_kind"] == "write"
    assert detail["source_system"] == "m365"
    assert detail["tier"] == 2
    assert detail["target"] == GRAPH_MESSAGE_ID
    assert sorted(detail["fields"]) == ["draft_ref", "mailbox", "request_id"]
    assert any(row.action.startswith("approval") for row in rows)

    for engine in state.engines:
        engine.dispose()


def test_rejected_send_never_reaches_graph(tmp_path: Path) -> None:
    """The honest rejection path: a human says no, and NOTHING is sent.

    The recorder must stay entirely empty — not even the recipient read-back
    runs, because the park happens at the gate, before perform.
    """
    reset_config_hash_for_testing()
    recorder: list[httpx.Request] = []
    gateway = ScriptedGateway(
        [
            tool_call_completion(
                _EMAIL_SEND_TOOL, {"mailbox": DEMO_MAILBOX, "draft_ref": GRAPH_MESSAGE_ID}
            ),
            text_completion("not sent"),
        ]
    )
    seams, verifier, _runtime_url, _audit_url = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        gateway=gateway,
        write_action_clients=_mail_clients(_graph_write_client(recorder)),
        approvals=ApprovalSettings(poll_interval_s=0.05),
        govern_settings=_govern_settings(),
    )
    app = create_app(_mail_settings(approver_roles=("approvers",)), seams=seams)
    state = get_state(app)

    sse, pending = _drive_turn(
        app, verifier, prompt="send the approved outreach", decision="reject"
    )

    assert sse.status_code == 200, sse.text
    assert pending and pending[0]["action"] == _EMAIL_SEND_TOOL
    assert _send_posts(recorder) == []
    assert recorder == [], "a rejected Tier-2 send must not touch Graph at all"

    for engine in state.engines:
        engine.dispose()


def test_send_refuses_a_draft_addressed_outside_the_send_allowlist(tmp_path: Path) -> None:
    """Approval is necessary but NOT sufficient: the recipient allowlist still runs.

    The draft's Graph-read recipient set contains an address outside the send
    allowlist, so even an APPROVED send is refused between read-back and send —
    the recorder shows the read and ZERO ``/send`` POSTs.
    """
    reset_config_hash_for_testing()
    recorder: list[httpx.Request] = []
    gateway = ScriptedGateway(
        [
            tool_call_completion(
                _EMAIL_SEND_TOOL, {"mailbox": DEMO_MAILBOX, "draft_ref": GRAPH_MESSAGE_ID}
            ),
            text_completion("refused"),
        ]
    )
    seams, verifier, _runtime_url, _audit_url = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        gateway=gateway,
        write_action_clients=_mail_clients(
            _graph_write_client(recorder, draft_recipients=("stranger@example.test",))
        ),
        approvals=ApprovalSettings(poll_interval_s=0.05),
        govern_settings=_govern_settings(),
    )
    app = create_app(_mail_settings(approver_roles=("approvers",)), seams=seams)
    state = get_state(app)

    sse, pending = _drive_turn(app, verifier, prompt="send the approved outreach")

    assert sse.status_code == 200, sse.text
    assert pending and pending[0]["action"] == _EMAIL_SEND_TOOL
    reads = [r for r in recorder if r.method == "GET" and GRAPH_MESSAGE_ID in r.url.path]
    assert reads, "the read-back is what refused the send; it must have run"
    assert _send_posts(recorder) == [], "an out-of-allowlist recipient must never be sent to"

    for engine in state.engines:
        engine.dispose()


def test_unapproved_send_never_reaches_graph(tmp_path: Path) -> None:
    """No approver, no send — and no Graph conversation at all.

    The gate expires rather than falling through. The recorder must stay empty:
    a parked Tier-2 send must not have spent a token "getting ready".
    """
    reset_config_hash_for_testing()
    recorder: list[httpx.Request] = []
    gateway = ScriptedGateway(
        [
            tool_call_completion(
                _EMAIL_SEND_TOOL, {"mailbox": DEMO_MAILBOX, "draft_ref": GRAPH_MESSAGE_ID}
            ),
            text_completion("not sent"),
        ]
    )
    seams, verifier, _runtime_url, _audit_url = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        gateway=gateway,
        write_action_clients=_mail_clients(_graph_write_client(recorder)),
        approvals=ApprovalSettings(poll_interval_s=0.05, expiry_s=1, tier2_sla_s=1),
        govern_settings=_govern_settings(),
    )
    app = create_app(_mail_settings(approver_roles=("approvers",)), seams=seams)
    state = get_state(app)

    _drive_turn(app, verifier, expect_pending=False, prompt="send the approved outreach")

    assert _send_posts(recorder) == []
    assert recorder == [], "an unapproved Tier-2 send must not touch Graph at all"

    for engine in state.engines:
        engine.dispose()


def test_recipient_outside_the_pinned_guardrail_never_reaches_graph(tmp_path: Path) -> None:
    """Guardrails run BEFORE the PDP and before the human gate.

    A recipient outside the pinned allowlist is blocked deterministically, so it
    never becomes an approval an approver could mistakenly release.
    """
    reset_config_hash_for_testing()
    recorder: list[httpx.Request] = []
    gateway = ScriptedGateway(
        [
            tool_call_completion(_EMAIL_DRAFT_TOOL, _draft_args(to="stranger@example.test")),
            text_completion("refused"),
        ]
    )
    seams, verifier, _runtime_url, audit_url = _seams(
        tmp_path,
        key=uuid.uuid4().hex,
        gateway=gateway,
        write_action_clients=_draft_only_clients(_graph_write_client(recorder)),
        approvals=ApprovalSettings(poll_interval_s=0.05),
        govern_settings=_govern_settings(),
    )
    app = create_app(_draft_settings(approver_roles=("approvers",)), seams=seams)
    state = get_state(app)

    _drive_turn(app, verifier, expect_pending=False)

    assert recorder == [], "a guardrail-blocked recipient must not reach Graph"

    audit_engine = sa.create_engine(audit_url)
    try:
        with audit_engine.connect() as conn:
            actions = {row.action for row in conn.execute(sa.select(ne_audit_log.c.action)).all()}
    finally:
        audit_engine.dispose()
    assert "guardrail.block" in actions

    for engine in state.engines:
        engine.dispose()


def _m365_authority() -> Any:
    """The minimal resolved M365 source-authority descriptor the metadata needs."""
    from ne.api.source_authority_descriptors import M365SourceAuthorityDescriptor

    return M365SourceAuthorityDescriptor(
        source_kind="m365-sharepoint",
        source_name="meridian-servicing",
        connector_instance_id="m365-primary",
        source_generation=1,
        contract_epoch=3,
        contract_identity_hash="a" * 64,
        descriptor_schema_version=1,
        descriptor_hash="b" * 64,
        head_revision="head-1",
        freshness_sla_seconds=Decimal("900"),
        resolved=True,
    )


# --- (D) the frontline half: what the composition ACTUALLY grants -------------


#: The REAL pilot persona group tuples, mirrored from
#: ``ne.cli.demo.live._MERIDIAN_OPS_GROUPS`` / ``_MERIDIAN_APPROVER_GROUPS`` /
#: ``_MERIDIAN_RESTRICTED_GROUPS`` and the committed ``ne-pilot`` realm
#: (``deploy/compose/pilot/keycloak/ne-pilot-realm.json``). Inlined rather than
#: imported: ``ne-api`` tests must not reach into ``ne-cli``.
_OPS_PERSONA_GROUPS: tuple[str, ...] = (
    "all-employees",
    "ops",
    "meridian-ops",
    "bank.customer.read",
    "bank.servicing.read",
    "bank.dispute.read",
    "sharepoint.operations.read",
)
_APPROVER_PERSONA_GROUPS: tuple[str, ...] = (
    "approver",
    "meridian-approver",
    "bank.customer.read",
    "bank.servicing.read",
    "bank.dispute.read",
    "bank.approval.read",
    "sharepoint.restricted.read",
)
_FRONTLINE_PERSONA_GROUPS: tuple[str, ...] = (
    "all-employees",
    "meridian-frontline",
    "bank.customer.read",
    "bank.account-summary.read",
    "sharepoint.frontline.read",
)


def test_draft_tool_static_visibility_is_narrowed_to_the_ops_group() -> None:
    """The persona gate the beat script claims, asserted in BOTH directions.

    This test previously pinned the GAP: ``_production_agent_capability_metadata``
    gave every non-workflow Meridian tool -- ``draft.email.create`` included --
    ``all_authenticated=True``, so a Meridian frontline persona reached a Tier-2
    external write action the moment the candidate overlay registered it. The
    Rego PDP does not close that hole: ``tiers.rego`` classifies on the action
    NAME and ``decision.rego`` rule (a) only requires a non-empty principal id,
    so approval gates EXECUTION but nothing gated who may hold the capability.

    The composition now narrows the draft action to the Meridian
    servicing-operations group -- the same ``groups=("ops",)`` narrowing the
    local-T3 workflow tools and the two deterministic workflow Agents in
    ``deploy/agents/registry.toml`` already use.

    BOTH halves are asserted against the REAL pilot persona group tuples,
    because a frontline-only assertion would be satisfied by hiding the action
    from everybody, which is not a persona gate. The paired preflight proof lives
    in ``test_pre010g_platform_capability_activation.py``'s
    ``test_production_preflight_projects_exact_workflow_reads_without_widening_visibility``.
    """
    from ne.api.platform import (
        _MERIDIAN_OPS_ONLY_TOOLS,
        _MERIDIAN_WORKFLOW_TOOLS,
        _production_agent_capability_metadata,
    )
    from ne.runtime.agent_capabilities import _is_statically_visible

    authority = _m365_authority()
    metadata = _production_agent_capability_metadata(
        registered_tools=frozenset({_EMAIL_DRAFT_TOOL}),
        admitted_sources=frozenset({("meridian-servicing", "t1")}),
        source_authority=authority,
        governed_local_t3_source_authority=None,
        skill_ready=True,
        graph_host_bound=True,
    )
    assert metadata is not None
    visibility = metadata.static_tool_visibility[_EMAIL_DRAFT_TOOL]

    # The draft action is not a workflow tool; it is narrowed by its own
    # membership of the ops-only set, so removing it there is a visible change.
    assert _EMAIL_DRAFT_TOOL not in _MERIDIAN_WORKFLOW_TOOLS
    assert _EMAIL_DRAFT_TOOL in _MERIDIAN_OPS_ONLY_TOOLS
    assert _MERIDIAN_WORKFLOW_TOOLS <= _MERIDIAN_OPS_ONLY_TOOLS
    assert visibility.all_authenticated is False
    assert visibility.groups == ("ops",)
    assert visibility.roles == ()
    assert visibility.principals == ()
    assert visibility.required_scopes == ()

    # PERMITTED: the servicing-operations persona still holds the capability.
    assert (
        _is_statically_visible(visibility, Principal(id="user:ops", groups=_OPS_PERSONA_GROUPS))
        is True
    )
    # REFUSED: the frontline persona the beat script names.
    assert (
        _is_statically_visible(
            visibility, Principal(id="user:frontline", groups=_FRONTLINE_PERSONA_GROUPS)
        )
        is False
    )
    # ALSO REFUSED, deliberately: the approver. Separation of duties is enforced
    # on the decide path (``ne.runtime.approvals.refuse_self_approval`` -- the
    # requester on an approval row can never be its approver), so this exclusion
    # is defence in depth: the approver persona never ORIGINATES the external
    # write it would then be refused from deciding. The ``["ops", "approver"]``
    # restricted ACL in ``ne.meridian_seed.slice._operational_acl`` is a
    # knowledge READ ACL and does not imply this write grant.
    assert (
        _is_statically_visible(
            visibility, Principal(id="user:approver", groups=_APPROVER_PERSONA_GROUPS)
        )
        is False
    )
    # The composition still refuses to invent a health signal for the mailbox.
    assert metadata.tool_health[_EMAIL_DRAFT_TOOL] is None


def test_unregistered_draft_tool_is_absent_from_the_capability_surface() -> None:
    """Absent for EVERYONE when unregistered -- a deployment fact, not a gate.

    Non-registration is not a governance decision and must never be narrated as
    "the frontline persona was refused the draft action". The persona gate is the
    ops-group narrowing asserted above; this test only holds the other axis
    steady, so the two can never be confused for one another.
    """
    from ne.api.platform import _production_agent_capability_metadata

    authority = _m365_authority()
    metadata = _production_agent_capability_metadata(
        registered_tools=frozenset(),
        admitted_sources=frozenset({("meridian-servicing", "t1")}),
        source_authority=authority,
        governed_local_t3_source_authority=None,
        skill_ready=True,
        graph_host_bound=True,
    )
    assert metadata is not None
    assert all(b.tool_name != _EMAIL_DRAFT_TOOL for b in metadata.tool_bindings)
    assert _EMAIL_DRAFT_TOOL not in metadata.static_tool_visibility
