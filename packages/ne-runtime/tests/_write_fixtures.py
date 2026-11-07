# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Local write-seam fixtures for the NE-050 reference-action tests.

ne-runtime tests may NOT import ne-connect's ``tests/`` (cross-package test imports
are banned), so this module scripts minimal ``httpx.MockTransport`` wires for the
Salesforce token+create and Graph token+create-draft routes and constructs the REAL
ne-connect write clients over them. The SQL write client rides a real in-memory sqlite
engine with a ``tickets`` table. It also builds the guardrail engine with the three
reference-action ``*_ARG_POLICY`` constants merged into
``GovernSettings.guardrails.tools`` (a tool absent from that map is rejected).

Everything ABOVE the httpx/sqlite seam runs for real — the whole point of the NE-050
acceptance is "no mock past the governance seam".
"""

from __future__ import annotations

from typing import Any

import httpx
import sqlalchemy as sa
from pydantic import SecretStr

from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.graph import GraphClient
from ne.connect.m365.write import GraphWriteClient
from ne.connect.salesforce.client import SalesforceClient
from ne.connect.salesforce.config import SalesforceConnectorSettings, SalesforceObjectConfig
from ne.connect.salesforce.write import SalesforceWriteClient
from ne.connect.sql.write import SqlWriteClient
from ne.govern import create_guardrail_engine
from ne.govern.config import (
    GovernSettings,
    GuardrailSettings,
    RateLimitRule,
    RateLimitSettings,
    RecipientSettings,
)
from ne.runtime.reference_actions import (
    CRM_NOTE_ARG_POLICY,
    EMAIL_DRAFT_ARG_POLICY,
    EMAIL_SEND_ARG_POLICY,
    TICKET_UPDATE_ARG_POLICY,
)

SF_API_VERSION = "v65.0"
SF_NOTE_ID = "00Q000000000123AAA"
SF_RECORD_ID = "001000000000001AAA"
SF_CREATE_PATH = f"/services/data/{SF_API_VERSION}/sobjects/Note"
SF_TOKEN_PATH = "/services/oauth2/token"

GRAPH_MESSAGE_ID = "AAMkAG-fake-draft-message-id"
ALLOWED_DRAFT_RECIPIENT = "ally@corp.example"
GRAPH_MAILBOX = "sender@contoso.test"


# --- Salesforce -----------------------------------------------------------------


def _salesforce_settings() -> SalesforceConnectorSettings:
    return SalesforceConnectorSettings(
        auth_method="client_credentials",
        client_id="3MVFAKECLIENTID",
        client_secret=SecretStr("fake-client-secret-not-real"),
        instance_url="https://acme.my.salesforce.com",
        owner="data-eng@acme.test",
        objects=(SalesforceObjectConfig(name="Account", content_fields=("Name",)),),
        api_version=SF_API_VERSION,
        requests_per_second=6000.0,
    )


def salesforce_transport(
    *, recorder: list[httpx.Request] | None = None, success: bool = True, create_status: int = 200
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        if request.url.path == SF_TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "fake", "token_type": "Bearer"})
        if request.url.path == SF_CREATE_PATH and request.method == "POST":
            if create_status >= 400:
                return httpx.Response(create_status, json={"error": "x"})
            return httpx.Response(create_status, json={"id": SF_NOTE_ID, "success": success})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def salesforce_write_client(
    *, recorder: list[httpx.Request] | None = None, success: bool = True, create_status: int = 200
) -> SalesforceWriteClient:
    client = SalesforceClient(
        _salesforce_settings(),
        transport=salesforce_transport(
            recorder=recorder, success=success, create_status=create_status
        ),
    )
    return SalesforceWriteClient(client, api_version=SF_API_VERSION)


# --- Microsoft Graph (M365) -----------------------------------------------------


def _m365_settings() -> M365ConnectorSettings:
    return M365ConnectorSettings(
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        auth_method="secret",
        client_secret=SecretStr("fake-secret-not-real"),
        site_ids=("site-eng",),
        owner="data-eng@contoso.test",
    )


def graph_transport(
    *, recorder: list[httpx.Request] | None = None, create_status: int = 201
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        if str(request.url).endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
        if request.url.path.endswith("/messages") and request.method == "POST":
            if create_status >= 400:
                return httpx.Response(create_status, json={"error": {"code": "x"}})
            return httpx.Response(create_status, json={"id": GRAPH_MESSAGE_ID, "isDraft": True})
        # NE-CS-SEND: the two send-leg routes. The read-back serves the draft's
        # ACTUAL recipient set (the allowlisted draft recipient); the send action
        # endpoint attests success with an empty 202, exactly as Graph does.
        if request.url.path.endswith(f"/messages/{GRAPH_MESSAGE_ID}/send") and (
            request.method == "POST"
        ):
            return httpx.Response(202)
        if request.url.path.endswith(f"/messages/{GRAPH_MESSAGE_ID}") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "isDraft": True,
                    "toRecipients": [{"emailAddress": {"address": ALLOWED_DRAFT_RECIPIENT}}],
                    "ccRecipients": [],
                    "bccRecipients": [],
                },
            )
        return httpx.Response(404, json={"error": {"code": "notFound"}})

    return httpx.MockTransport(handler)


def graph_write_client(
    *, recorder: list[httpx.Request] | None = None, create_status: int = 201
) -> GraphWriteClient:
    graph = GraphClient(
        _m365_settings(), transport=graph_transport(recorder=recorder, create_status=create_status)
    )
    return GraphWriteClient(graph)


# --- SQL ------------------------------------------------------------------------


def sql_tickets_engine() -> sa.Engine:
    """An in-memory sqlite engine with a seeded ``tickets`` table (id=42, open)."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text("CREATE TABLE tickets (id INTEGER PRIMARY KEY, status TEXT, public_reply TEXT)")
        )
        conn.execute(
            sa.text("INSERT INTO tickets (id, status, public_reply) VALUES (42, 'open', '')")
        )
    return engine


def sql_write_client(engine: sa.Engine) -> SqlWriteClient:
    return SqlWriteClient(engine, allowed_writes={"tickets": ("id", "status", "public_reply")})


def ticket_rows(engine: sa.Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as conn:
        return [
            tuple(r) for r in conn.execute(sa.text("SELECT id, status, public_reply FROM tickets"))
        ]


# --- guardrails merging the three reference-action arg policies -----------------


def write_govern_settings() -> GovernSettings:
    return GovernSettings(
        guardrails=GuardrailSettings(
            recipients=RecipientSettings(allowed_recipients=(ALLOWED_DRAFT_RECIPIENT,)),
            rate=RateLimitSettings(default=RateLimitRule(max_calls=10000, window_s=3600.0)),
            tools={
                "write.internal.crm.note": CRM_NOTE_ARG_POLICY,
                "write.external.ticket.update": TICKET_UPDATE_ARG_POLICY,
                "draft.email.create": EMAIL_DRAFT_ARG_POLICY,
                "write.external.email.send": EMAIL_SEND_ARG_POLICY,
            },
        )
    )


def write_guardrails(sink: Any) -> Any:
    return create_guardrail_engine(write_govern_settings(), sink=sink)
