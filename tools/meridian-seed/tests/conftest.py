# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Mock-only transports and obviously fake configuration for Meridian seed tests.

The M365 transport models the Graph drive/folder/item surface the seeder walks:
site drive resolution, Meridian folder probe/create, item GET/content PUT, and
per-item permission invite/delete.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.salesforce.config import SalesforceConnectorSettings, SalesforceObjectConfig
from ne.meridian_seed.personas import PrincipalsMap
from ne.meridian_seed.salesforce import EXTERNAL_ID_FIELD

M365_TENANT = "77777777-7777-7777-7777-777777777777"
M365_CLIENT = "88888888-8888-8888-8888-888888888888"
M365_SITE = "site-meridian"
M365_SECRET = "fake-meridian-m365-secret-not-real"
GRAPH = "https://graph.microsoft.com/v1.0"
M365_TOKEN_URL = f"https://login.microsoftonline.com/{M365_TENANT}/oauth2/v2.0/token"
_GRAPH_PATH = urllib.parse.urlparse(GRAPH).path.rstrip("/")

SF_INSTANCE = "https://meridian.my.salesforce.com"
SF_SECRET = "fake-meridian-sf-secret-not-real"
SF_API = "v65.0"


def m365_settings(**overrides: object) -> M365ConnectorSettings:
    values: dict[str, object] = {
        "tenant_id": M365_TENANT,
        "client_id": M365_CLIENT,
        "auth_method": "secret",
        "client_secret": M365_SECRET,
        "site_ids": (M365_SITE,),
        "owner": "meridian-seed-test",
        "requests_per_second": 6000.0,
    }
    values.update(overrides)
    return M365ConnectorSettings(**values)  # type: ignore[arg-type]


def salesforce_settings(**overrides: object) -> SalesforceConnectorSettings:
    values: dict[str, object] = {
        "auth_method": "client_credentials",
        "client_id": "3MVFAKE-MERIDIAN-CLIENT",
        "client_secret": SF_SECRET,
        "instance_url": SF_INSTANCE,
        "owner": "meridian-seed-test",
        "objects": (SalesforceObjectConfig(name="Account", content_fields=("Name",)),),
        "requests_per_second": 6000.0,
    }
    values.update(overrides)
    return SalesforceConnectorSettings(**values)  # type: ignore[arg-type]


@pytest.fixture
def principals() -> PrincipalsMap:
    return PrincipalsMap.model_validate(
        {
            "m365": {
                "users": {
                    "ops": "ops@meridian.example",
                    "approver": "approver@meridian.example",
                    "frontline": "frontline@meridian.example",
                },
                "groups": {
                    "meridian-ops": "group-ops",
                    "meridian-approver": "group-approver",
                    "meridian-frontline": "group-frontline",
                },
            }
        }
    )


def build_m365_transport(
    *,
    recorder: list[httpx.Request] | None = None,
    folder_conflict: bool = False,
    fail_put_number: int | None = None,
    folder_exists: bool = False,
    folder_present_unowned: bool = False,
    folder_get_malformed: bool = False,
    folder_live_id: str = "folder-meridian",
    folder_create_name: str = "Meridian",
    folder_create_has_folder_facet: bool = True,
    drive_id: str = "drive-meridian",
    drive_missing_id: bool = False,
    drive_malformed: bool = False,
    sop_item_id: str = "item-dispute-sop",
    sop_missing: bool = False,
    sop_id_changed: bool = False,
    sop_malformed: bool = False,
    sop_name_changed: bool = False,
    put_name_changed: bool = False,
    put_redirect: bool = False,
    delete_redirect: bool = False,
    folder_children: list[dict[str, object]] | None = None,
    children_next_link: bool = False,
) -> httpx.MockTransport:
    put_counter = 0
    folder_created = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal folder_created, put_counter
        if recorder is not None:
            recorder.append(request)
        if str(request.url) == M365_TOKEN_URL:
            return httpx.Response(
                200, json={"access_token": "fake-graph-token", "expires_in": 3600}
            )
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        path = urllib.parse.unquote(request.url.path)
        if path == f"{_GRAPH_PATH}/sites/{M365_SITE}/drive" and request.method == "GET":
            if drive_malformed:
                return httpx.Response(200, json=["not-an-object"])
            if drive_missing_id:
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"id": drive_id})
        if path == f"{_GRAPH_PATH}/drives/{drive_id}/root/children" and request.method == "POST":
            if folder_conflict:
                return httpx.Response(409, json={"error": {"code": "nameAlreadyExists"}})
            folder_created = True
            payload: dict[str, object] = {
                "id": "folder-meridian",
                "name": folder_create_name,
            }
            if folder_create_has_folder_facet:
                payload["folder"] = {}
            return httpx.Response(201, json=payload)
        if path == f"{_GRAPH_PATH}/drives/{drive_id}/root:/Meridian" and request.method == "GET":
            if folder_present_unowned:
                return httpx.Response(
                    200, json={"id": "folder-stranger", "name": "Meridian", "folder": {}}
                )
            if folder_get_malformed:
                return httpx.Response(200, json=[])
            if folder_exists or folder_created:
                return httpx.Response(
                    200, json={"id": folder_live_id, "name": "Meridian", "folder": {}}
                )
            return httpx.Response(404, json={"error": {"code": "notFound"}})
        if (
            path == f"{_GRAPH_PATH}/drives/{drive_id}/items/{folder_live_id}/children"
            and request.method == "GET"
        ):
            payload: dict[str, object] = {"value": folder_children or []}
            if children_next_link:
                payload["@odata.nextLink"] = "https://graph.microsoft.com/next"
            return httpx.Response(200, json=payload)
        prefix = f"{_GRAPH_PATH}/drives/{drive_id}/root:/Meridian/"
        if path.startswith(prefix) and path.endswith(":/content") and request.method == "PUT":
            put_counter += 1
            if fail_put_number is not None and put_counter >= fail_put_number:
                return httpx.Response(500, json={"error": {"code": "forcedFailure"}})
            sharepoint_path = path[len(prefix) :].removesuffix(":/content")
            item_id = (
                sop_item_id
                if sharepoint_path == "Debit Card Dispute Handling SOP.docx"
                else "item-" + re.sub(r"[^a-z0-9]+", "-", sharepoint_path.lower()).strip("-")
            )
            name = "lookalike.docx" if put_name_changed else sharepoint_path
            if put_redirect:
                return httpx.Response(307, json={"id": item_id, "name": name})
            return httpx.Response(200, json={"id": item_id, "name": name})
        if path.startswith(prefix) and request.method == "GET":
            name = path[len(prefix) :]
            if name != "Debit Card Dispute Handling SOP.docx":
                return httpx.Response(404, json={"error": {"code": "notFound"}})
            if sop_missing:
                return httpx.Response(404, json={"error": {"code": "notFound"}})
            if sop_malformed:
                return httpx.Response(200, json="not-an-object")
            if sop_id_changed:
                return httpx.Response(200, json={"id": "item-lookalike", "name": name})
            if sop_name_changed:
                return httpx.Response(200, json={"id": sop_item_id, "name": "lookalike.docx"})
            return httpx.Response(200, json={"id": sop_item_id, "name": name})
        items_prefix = f"{_GRAPH_PATH}/drives/{drive_id}/items/"
        if path.startswith(items_prefix) and path.endswith("/invite") and request.method == "POST":
            return httpx.Response(200, json={"value": [{"id": "permission-1"}]})
        if path.startswith(items_prefix) and request.method == "DELETE":
            if delete_redirect:
                return httpx.Response(307, headers={"Location": "https://example.invalid"})
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"code": "notFound"}})

    return httpx.MockTransport(handler)


def build_salesforce_transport(
    *,
    recorder: list[httpx.Request] | None = None,
    external_id_present: bool = True,
    external_id_valid: bool = True,
    existing_permission_rows: list[dict[str, Any]] | None = None,
    unseed_rows: Mapping[str, list[dict[str, Any]]] | None = None,
    unseed_permission_rows: list[dict[str, Any]] | None = None,
    unseed_permset_rows: list[dict[str, Any]] | None = None,
) -> httpx.MockTransport:
    permsets: dict[str, str] = {}
    permissions: set[tuple[str, str]] = set()
    record_rows = dict(unseed_rows or {})

    def query(soql: str) -> dict[str, Any]:
        if "FROM PermissionSet" in soql:
            if unseed_permset_rows is not None and "SELECT Id, Name" in soql:
                rows = unseed_permset_rows
            else:
                rows = [{"Id": live_id, "Name": name} for name, live_id in permsets.items()]
            return {"records": rows, "done": True, "totalSize": len(rows)}
        if "FROM ObjectPermissions" in soql:
            if "PermissionsCreate" in soql and existing_permission_rows is not None:
                return {
                    "records": existing_permission_rows,
                    "done": True,
                    "totalSize": len(existing_permission_rows),
                }
            if unseed_permission_rows is not None and "SELECT Id, Parent.Name" in soql:
                return {
                    "records": unseed_permission_rows,
                    "done": True,
                    "totalSize": len(unseed_permission_rows),
                }
            rows = [
                {
                    "Id": f"op-{index}",
                    "Parent": {"Name": permset},
                    "SobjectType": sobject,
                    "PermissionsRead": True,
                    "PermissionsCreate": False,
                    "PermissionsEdit": False,
                    "PermissionsDelete": False,
                    "PermissionsViewAllRecords": False,
                    "PermissionsModifyAllRecords": False,
                }
                for index, (permset, sobject) in enumerate(sorted(permissions), start=1)
            ]
            return {"records": rows, "done": True, "totalSize": len(rows)}
        match = re.search(r"FROM (Account|Contact|Case)", soql)
        rows = record_rows.get(match.group(1), []) if match else []
        return {"records": rows, "done": True, "totalSize": len(rows)}

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        path = urllib.parse.unquote(request.url.path)
        if path == "/services/oauth2/token":
            return httpx.Response(200, json={"access_token": "fake-salesforce-token"})
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        if path.endswith("/describe") and request.method == "GET":
            fields: list[dict[str, Any]] = [{"name": "Id"}, {"name": "Name"}]
            if external_id_present:
                fields.append(
                    {
                        "name": EXTERNAL_ID_FIELD,
                        "type": "string" if external_id_valid else "int",
                        "length": 128,
                        "externalId": True,
                        "unique": True,
                    }
                )
            return httpx.Response(200, json={"fields": fields})
        if path == f"/services/data/{SF_API}/query" and request.method == "GET":
            return httpx.Response(200, json=query(dict(request.url.params).get("q", "")))
        upsert = re.match(
            rf"^/services/data/{SF_API}/sobjects/(Account|Contact|Case)/"
            rf"{EXTERNAL_ID_FIELD}/meridian-v1:(?P<source>.+)$",
            path,
        )
        if upsert and request.method == "PATCH":
            return httpx.Response(
                200, json={"id": f"live-{upsert.group('source')}", "success": True}
            )
        create = re.match(rf"^/services/data/{SF_API}/sobjects/(\w+)$", path)
        if create and request.method == "POST":
            body = json.loads(request.content.decode())
            if create.group(1) == "PermissionSet":
                name = body["Name"]
                live_id = f"ps-{name}"
                permsets[name] = live_id
                return httpx.Response(201, json={"id": live_id, "success": True})
            if create.group(1) == "ObjectPermissions":
                parent = next(
                    name for name, live_id in permsets.items() if live_id == body["ParentId"]
                )
                permissions.add((parent, body["SobjectType"]))
                return httpx.Response(201, json={"id": f"op-{len(permissions)}", "success": True})
        delete = re.match(rf"^/services/data/{SF_API}/sobjects/\w+/[A-Za-z0-9_-]+$", path)
        if delete and request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "not_found"})

    return httpx.MockTransport(handler)
