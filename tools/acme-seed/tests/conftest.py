# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared settings factories + recording MockTransports for the seeder suites.

Every transport is an :class:`httpx.MockTransport` — ZERO network sockets. Fake
creds use obviously-fake values (a ``33333333-…`` tenant, an ``xoxb-fake`` bot
token). No test ever touches a live tenant (fixture-first CI, §0.6/§1.4).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from ne.acme_seed.personas import PrincipalsMap
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.salesforce.config import SalesforceConnectorSettings, SalesforceObjectConfig
from ne.connect.slack.config import SlackConnectorSettings

# -- shared fake identity -------------------------------------------------------
M365_TENANT = "33333333-3333-3333-3333-333333333333"
M365_CLIENT = "44444444-4444-4444-4444-444444444444"
M365_SITE = "site-acme"
M365_SECRET = "fake-m365-secret-not-real"

SF_INSTANCE = "https://acme.my.salesforce.com"
SF_SECRET = "fake-sf-secret-not-real"
SF_API = "v65.0"

SLACK_TOKEN = "xoxb-fake-not-real-token"

GRAPH = "https://graph.microsoft.com/v1.0"
M365_TOKEN_URL = f"https://login.microsoftonline.com/{M365_TENANT}/oauth2/v2.0/token"


# -- settings factories ---------------------------------------------------------
def m365_settings(**overrides: object) -> M365ConnectorSettings:
    kwargs: dict[str, object] = {
        "tenant_id": M365_TENANT,
        "client_id": M365_CLIENT,
        "auth_method": "secret",
        "client_secret": M365_SECRET,
        "site_ids": (M365_SITE,),
        "owner": "acme-seed",
    }
    kwargs.update(overrides)
    return M365ConnectorSettings(**kwargs)  # type: ignore[arg-type]


def salesforce_settings(**overrides: object) -> SalesforceConnectorSettings:
    kwargs: dict[str, object] = {
        "auth_method": "client_credentials",
        "client_id": "3MVFAKE0000CLIENTID",
        "client_secret": SF_SECRET,
        "instance_url": SF_INSTANCE,
        "owner": "acme-seed",
        "objects": (SalesforceObjectConfig(name="Account", content_fields=("Name",)),),
        "requests_per_second": 6000.0,
    }
    kwargs.update(overrides)
    return SalesforceConnectorSettings(**kwargs)  # type: ignore[arg-type]


def slack_settings(**overrides: object) -> SlackConnectorSettings:
    kwargs: dict[str, object] = {
        "bot_token": SLACK_TOKEN,
        "owner": "acme-seed",
        "history_requests_per_minute": 6000.0,
        "requests_per_minute": 6000.0,
    }
    kwargs.update(overrides)
    return SlackConnectorSettings(**kwargs)  # type: ignore[arg-type]


def full_principals() -> PrincipalsMap:
    """A complete principals map: all 4 M365 groups + 4 users + 4 Slack users."""
    return PrincipalsMap.model_validate(
        {
            "m365": {
                "users": {
                    "per-0000": "u-exec@acme.example",
                    "per-0006": "u-eng@acme.example",
                    "per-0016": "u-sales@acme.example",
                    "per-0038": "u-legal@acme.example",
                },
                "groups": {
                    "executive": "g-exec-id",
                    "dept-engineering": "g-eng-id",
                    "dept-sales": "g-sales-id",
                    "dept-legal": "g-legal-id",
                },
            },
            "slack": {
                "users": {
                    "per-0000": "U0000EXEC",
                    "per-0006": "U0006ENG",
                    "per-0016": "U0016SALES",
                    "per-0038": "U0038LEGAL",
                }
            },
        }
    )


@pytest.fixture
def principals() -> PrincipalsMap:
    return full_principals()


# -- M365 transport -------------------------------------------------------------
_PUT_RE = re.compile(
    rf"^{re.escape(GRAPH)}/sites/[^/]+/drive/root:/Acme/(?P<doc>[^:]+)\.md:/content$"
)
_INVITE_RE = re.compile(rf"^{re.escape(GRAPH)}/sites/[^/]+/drive/items/(?P<item>[^/]+)/invite$")
_CHILDREN_RE = re.compile(rf"^{re.escape(GRAPH)}/sites/[^/]+/drive/root/children$")
_FOLDER_DELETE_RE = re.compile(rf"^{re.escape(GRAPH)}/sites/[^/]+/drive/root:/Acme:$")
_DRIVE_ROOT_RE = re.compile(rf"^{re.escape(GRAPH)}/sites/[^/]+/drive/root$")


def build_m365_transport(
    *,
    recorder: list[httpx.Request] | None = None,
    folder_conflict: bool = False,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        url = str(request.url)
        if url == M365_TOKEN_URL:
            return httpx.Response(
                200, json={"access_token": "fake-graph-token", "expires_in": 3600}
            )
        assert request.headers.get("Authorization", "").startswith("Bearer ")

        if _CHILDREN_RE.match(url) and request.method == "POST":
            if folder_conflict:
                return httpx.Response(409, json={"error": {"code": "nameAlreadyExists"}})
            return httpx.Response(201, json={"id": "folder-acme", "name": "Acme"})

        put = _PUT_RE.match(url)
        if put and request.method == "PUT":
            doc = put.group("doc")
            return httpx.Response(200, json={"id": f"item-{doc}", "name": f"{doc}.md"})

        invite = _INVITE_RE.match(url)
        if invite and request.method == "POST":
            return httpx.Response(200, json={"value": [{"id": "perm-1"}]})

        if _FOLDER_DELETE_RE.match(url) and request.method == "DELETE":
            return httpx.Response(204)

        if _DRIVE_ROOT_RE.match(url) and request.method == "GET":
            return httpx.Response(200, json={"id": "drive-root"})

        return httpx.Response(404, json={"error": {"code": "notFound"}})

    return httpx.MockTransport(handler)


# -- Salesforce transport -------------------------------------------------------
_SF_TOKEN = "/services/oauth2/token"
_SF_QUERY = f"/services/data/{SF_API}/query"
_SF_DESCRIBE_RE = re.compile(
    rf"^/services/data/{re.escape(SF_API)}/sobjects/(?P<obj>\w+)/describe$"
)
_SF_UPSERT_RE = re.compile(
    rf"^/services/data/{re.escape(SF_API)}/sobjects/(?P<obj>\w+)/Acme_Source_Id__c/(?P<val>.+)$"
)
_SF_CREATE_RE = re.compile(rf"^/services/data/{re.escape(SF_API)}/sobjects/(?P<obj>\w+)$")
_SF_DELETE_RE = re.compile(
    rf"^/services/data/{re.escape(SF_API)}/sobjects/(?P<obj>\w+)/(?P<id>[\w-]+)$"
)


def build_salesforce_transport(
    *,
    recorder: list[httpx.Request] | None = None,
    external_id_present: bool = True,
    existing_permsets: Mapping[str, str] | None = None,
    existing_case_read: bool = False,
    unseed_ids: Mapping[str, list[str]] | None = None,
) -> httpx.MockTransport:
    permsets = dict(existing_permsets or {})
    unseed = dict(unseed_ids or {})
    created_permsets: dict[str, str] = {}

    def _describe_fields(obj: str) -> dict[str, Any]:
        fields = [{"name": "Id"}, {"name": "Name"}]
        if external_id_present:
            fields.append({"name": "Acme_Source_Id__c"})
        return {"fields": fields, "name": obj}

    def _query(soql: str) -> dict[str, Any]:
        if "FROM PermissionSet" in soql:
            rows = [{"Id": pid, "Name": name} for name, pid in permsets.items()]
            return {"records": rows, "done": True, "totalSize": len(rows)}
        if "FROM ObjectPermissions" in soql and "Parent.Name" in soql and "SobjectType" in soql:
            rows: list[dict[str, Any]] = []
            if existing_case_read and "Acme_Case_Reader" in permsets:
                rows.append(
                    {
                        "Id": "ope-1",
                        "Parent": {"Name": "Acme_Case_Reader"},
                        "SobjectType": "Case",
                        "PermissionsRead": True,
                    }
                )
            return {"records": rows, "done": True, "totalSize": len(rows)}
        if "FROM ObjectPermissions" in soql:
            rows = [{"Id": rid} for rid in unseed.get("ObjectPermissions", [])]
            return {
                "records": rows,
                "done": True,
                "totalSize": len(rows),
            }
        # unseed record queries: FROM Case/Contact/Account WHERE ... != null
        match = re.search(r"FROM (\w+)", soql)
        obj = match.group(1) if match else ""
        rows = [{"Id": rid} for rid in unseed.get(obj, [])]
        return {"records": rows, "done": True, "totalSize": len(rows)}

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        path = request.url.path
        if path == _SF_TOKEN:
            return httpx.Response(200, json={"access_token": "fake-sf-token"})
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        method = request.method

        describe = _SF_DESCRIBE_RE.match(path)
        if describe and method == "GET":
            return httpx.Response(200, json=_describe_fields(describe.group("obj")))

        if path == _SF_QUERY and method == "GET":
            return httpx.Response(200, json=_query(dict(request.url.params).get("q", "")))

        upsert = _SF_UPSERT_RE.match(path)
        if upsert and method == "PATCH":
            val = upsert.group("val")
            return httpx.Response(200, json={"id": f"live-{val}", "success": True, "created": True})

        create = _SF_CREATE_RE.match(path)
        if create and method == "POST":
            obj = create.group("obj")
            if obj == "PermissionSet":
                body = json.loads(request.content.decode())
                name = body["Name"]
                pid = f"0PS-{name}"
                created_permsets[name] = pid
                permsets[name] = pid
                return httpx.Response(201, json={"id": pid, "success": True})
            return httpx.Response(201, json={"id": f"new-{obj}", "success": True})

        delete = _SF_DELETE_RE.match(path)
        if delete and method == "DELETE":
            return httpx.Response(204)

        return httpx.Response(404, json={"error": "not_found"})

    return httpx.MockTransport(handler)


# -- Slack transport ------------------------------------------------------------
def build_slack_transport(
    *,
    recorder: list[httpx.Request] | None = None,
    name_taken: frozenset[str] = frozenset(),
    existing_channels: Mapping[str, str] | None = None,
    history_markers: Mapping[str, list[str]] | None = None,
) -> httpx.MockTransport:
    existing = dict(existing_channels or {})
    markers = dict(history_markers or {})
    ts_counter = {"n": 0}

    def _body(request: httpx.Request) -> dict[str, Any]:
        if request.content:
            return json.loads(request.content.decode())
        return {}

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        method = request.url.path.removeprefix("/api/")
        params = dict(request.url.params)
        body = _body(request)

        if method == "auth.test":
            return httpx.Response(200, json={"ok": True, "user_id": "U-BOT"})

        if method == "conversations.create":
            name = str(body.get("name"))
            if name in name_taken:
                return httpx.Response(200, json={"ok": False, "error": "name_taken"})
            cid = f"C-{name}"
            return httpx.Response(200, json={"ok": True, "channel": {"id": cid, "name": name}})

        if method == "conversations.list":
            channels = [{"id": cid, "name": name} for name, cid in existing.items()]
            return httpx.Response(200, json={"ok": True, "channels": channels})

        if method == "conversations.invite":
            return httpx.Response(200, json={"ok": True, "channel": {"id": body.get("channel")}})

        if method == "conversations.history":
            cid = params.get("channel", "")
            msgs = [{"text": f"seeded [acme:{fid}]"} for fid in markers.get(cid, [])]
            return httpx.Response(200, json={"ok": True, "messages": msgs})

        if method == "chat.postMessage":
            ts_counter["n"] += 1
            return httpx.Response(
                200,
                json={"ok": True, "ts": f"ts-{ts_counter['n']}", "channel": body.get("channel")},
            )

        if method == "chat.delete":
            return httpx.Response(200, json={"ok": True})

        if method == "conversations.archive":
            return httpx.Response(200, json={"ok": True})

        return httpx.Response(200, json={"ok": False, "error": "unknown_method"})

    return httpx.MockTransport(handler)
