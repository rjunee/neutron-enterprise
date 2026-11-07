# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Salesforce seeder (NE-102) — upsert-by-external-id + the object-level Case trap.

:class:`SeedSalesforceClient` SUBCLASSES the connector's
:class:`~ne.connect.salesforce.client.SalesforceClient`, adding the PATCH-upsert
and DELETE verbs the seed needs (the shipped ``SalesforceWriteClient`` is
create-only) IN THE TOOL — ``packages/ne-connect`` gains no write surface. Every
sObject/field NAME is identifier-validated (a local 5-line copy of the
connector's module-private ``_check_identifier``, deliberately NOT imported
cross-package) BEFORE any request; VALUES travel only in the JSON body (no SOQL
value interpolation anywhere).

Idempotency rides a custom External-Id field ``Acme_Source_Id__c`` on Account /
Contact / Case: the corpus id (``acct-0000`` …) is the external key, so a re-run
PATCH-upserts instead of duplicating. The owner must create that field first —
:meth:`SalesforceSeeder.check` fails with the precise add-list if it is absent.

The trap is OBJECT-LEVEL ONLY (connector v1 mirrors ObjectPermissions -> the
``sf-permset:<Id>`` labels in ``salesforce/acl.py``; it does NOT model record
sharing / OWD / role hierarchy): two permission sets — ``Acme_Case_Reader``
(reads Case) and ``Acme_No_Case`` (cannot) — so the trap is "permset A reads
Case, permset B cannot". No record-owner/sharing trap is built.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ne.acme_seed.errors import SeedPreflightError
from ne.acme_seed.manifest import SeedManifest
from ne.acme_seed.slice import SliceSelection
from ne.connect.salesforce.client import Client, SalesforceClient, Transport
from ne.connect.salesforce.config import SalesforceConnectorSettings

__all__ = ["EXTERNAL_ID_FIELD", "SalesforceSeeder", "SeedSalesforceClient"]

#: Local copy of the connector's module-private identifier gate (NOT imported
#: cross-package — the one deliberate 5-line duplication per the design).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _check_identifier(name: str, field: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(
            f"{field} {name!r} is not a valid bare Salesforce API name "
            "(must match ^[A-Za-z][A-Za-z0-9_]*$)"
        )
    return name


EXTERNAL_ID_FIELD = "Acme_Source_Id__c"

_SEED_OBJECTS: tuple[str, ...] = ("Account", "Contact", "Case")
_PERMSET_CASE_READER = "Acme_Case_Reader"
_PERMSET_NO_CASE = "Acme_No_Case"

_STATUS_MAP = {"open": "New", "pending": "Working", "closed": "Closed"}
_PRIORITY_MAP = {"low": "Low", "normal": "Medium", "high": "High", "urgent": "High"}


class SeedSalesforceClient(SalesforceClient):
    """Salesforce client + PATCH-upsert / DELETE verbs (validated, guard-inherited)."""

    async def patch_json(self, client: Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._call(client, "PATCH", path, json_body=body)
        return resp.json()

    async def delete_path(self, client: Client, path: str) -> None:
        # DELETE returns 204 with NO body — never .json() it.
        await self._call(client, "DELETE", path)

    async def upsert_by_external_id(
        self,
        client: Client,
        sobject: str,
        external_value: str,
        fields: dict[str, Any],
        *,
        api_version: str,
    ) -> str:
        """PATCH-upsert ``sobject`` keyed by ``EXTERNAL_ID_FIELD`` -> live record id.

        The sObject name, the external-id field, and every field NAME are
        identifier-validated BEFORE the URL is built (injection defense at the
        boundary); values go only in the JSON body.
        """
        _check_identifier(sobject, "sobject")
        _check_identifier(EXTERNAL_ID_FIELD, "external id field")
        for name in fields:
            _check_identifier(name, "field name")
        quoted = urllib.parse.quote(external_value, safe="")
        path = f"/services/data/{api_version}/sobjects/{sobject}/{EXTERNAL_ID_FIELD}/{quoted}"
        payload = await self.patch_json(client, path, fields)
        return str(payload["id"])


class SalesforceSeeder:
    """Upsert the record slice + provision the two Case-read permission sets."""

    def __init__(
        self,
        settings: SalesforceConnectorSettings,
        *,
        transport: Transport | None = None,
    ) -> None:
        self._settings = settings
        self._api = settings.api_version
        self._client = SeedSalesforceClient(settings, transport=transport)

    # -- preflight ------------------------------------------------------------
    async def check(self, client: Client) -> None:
        """Verify ``Acme_Source_Id__c`` exists on all 3 objects (read-only)."""
        for sobject in _SEED_OBJECTS:
            _check_identifier(sobject, "sobject")
            payload = await self._client.get_json(
                client, f"/services/data/{self._api}/sobjects/{sobject}/describe"
            )
            names = {str(f.get("name")) for f in payload.get("fields", [])}
            if EXTERNAL_ID_FIELD not in names:
                raise SeedPreflightError(
                    f"Salesforce object {sobject} is missing the external-id field "
                    f"{EXTERNAL_ID_FIELD}. OWNER ACTION: create custom field "
                    f"{EXTERNAL_ID_FIELD}: Text(64), External ID, Unique on Account, "
                    "Contact, Case + grant field-level security to the integration "
                    "user's profile."
                )

    # -- permission sets (the object-level trap) ------------------------------
    async def _ensure_permsets(self, client: Client, manifest: SeedManifest) -> None:
        rows = await self._client.rest_query(
            client,
            "SELECT Id, Name FROM PermissionSet WHERE Name IN "
            f"('{_PERMSET_CASE_READER}', '{_PERMSET_NO_CASE}')",
        )
        existing = {str(r["Name"]): str(r["Id"]) for r in rows}
        for name in (_PERMSET_CASE_READER, _PERMSET_NO_CASE):
            if name in existing:
                manifest.permsets[name] = existing[name]
            else:
                created = await self._client.post_json(
                    client,
                    f"/services/data/{self._api}/sobjects/PermissionSet",
                    {"Name": name, "Label": name},
                )
                manifest.permsets[name] = str(created["id"])

        # Acme_Case_Reader needs ONE ObjectPermissions row granting Case read;
        # Acme_No_Case gets none (that asymmetry IS the trap). Query-first for
        # idempotency (static SOQL — no value interpolation).
        obj_rows = await self._client.rest_query(
            client,
            "SELECT Id, Parent.Name, SobjectType, PermissionsRead FROM ObjectPermissions "
            f"WHERE Parent.Name IN ('{_PERMSET_CASE_READER}', '{_PERMSET_NO_CASE}') "
            "AND SobjectType = 'Case'",
        )
        has_reader_case = any(
            (r.get("Parent") or {}).get("Name") == _PERMSET_CASE_READER
            and r.get("SobjectType") == "Case"
            and r.get("PermissionsRead")
            for r in obj_rows
        )
        if not has_reader_case:
            await self._client.post_json(
                client,
                f"/services/data/{self._api}/sobjects/ObjectPermissions",
                {
                    "ParentId": manifest.permsets[_PERMSET_CASE_READER],
                    "SobjectType": "Case",
                    "PermissionsRead": True,
                },
            )

    # -- push -----------------------------------------------------------------
    async def push(self, selection: SliceSelection) -> SeedManifest:
        manifest = SeedManifest(
            tenant="salesforce",
            seeded_at=datetime.now(UTC).isoformat(),
            corpus_version=selection.corpus_version,
            # No PermissionSetAssignment (dev-org licenses) — personas map
            # LOGICALLY: P2 reads Case, P3 does not; P1/P4 have no SF principal.
            personas={
                "per-0006": _PERMSET_CASE_READER,
                "per-0016": _PERMSET_NO_CASE,
                "per-0000": None,
                "per-0038": None,
            },
        )
        async with self._client.session() as http:
            await self.check(http)
            await self._ensure_permsets(http, manifest)

            account_live: dict[str, str] = {}
            for acct in selection.accounts:
                live = await self._client.upsert_by_external_id(
                    http,
                    "Account",
                    str(acct["id"]),
                    {
                        "Name": acct["name"],
                        "Industry": acct.get("industry"),
                        "NumberOfEmployees": acct.get("employee_count"),
                    },
                    api_version=self._api,
                )
                account_live[str(acct["id"])] = live
                manifest.records[str(acct["id"])] = live

            for person in selection.people:
                live = await self._client.upsert_by_external_id(
                    http,
                    "Contact",
                    str(person["id"]),
                    {
                        "FirstName": person.get("given_name"),
                        "LastName": person.get("family_name"),
                        "Email": person.get("email"),
                        "Title": person.get("title"),
                        "AccountId": account_live.get(str(person.get("works_at"))),
                    },
                    api_version=self._api,
                )
                manifest.records[str(person["id"])] = live

            for ticket in selection.tickets:
                status = _STATUS_MAP.get(str(ticket.get("status")), "New")
                priority = _PRIORITY_MAP.get(str(ticket.get("priority")), "Medium")
                live = await self._client.upsert_by_external_id(
                    http,
                    "Case",
                    str(ticket["id"]),
                    {
                        "Subject": ticket.get("subject"),
                        "Description": ticket.get("description"),
                        "Status": status,
                        "Priority": priority,
                        "AccountId": account_live.get(str(ticket.get("belongs_to"))),
                    },
                    api_version=self._api,
                )
                manifest.records[str(ticket["id"])] = live
        return manifest

    # -- unseed ---------------------------------------------------------------
    async def unseed(self, manifest_dir: Path | None = None) -> None:
        async with self._client.session() as http:
            # Reverse FK order: Case -> Contact -> Account.
            for sobject in reversed(_SEED_OBJECTS):
                _check_identifier(sobject, "sobject")
                rows = await self._client.rest_query(
                    http,
                    f"SELECT Id FROM {sobject} WHERE {EXTERNAL_ID_FIELD} != null",
                )
                for row in rows:
                    rid = str(row["Id"])
                    await self._client.delete_path(
                        http, f"/services/data/{self._api}/sobjects/{sobject}/{rid}"
                    )
            # Remove ObjectPermissions rows first, then the two permsets.
            obj_rows = await self._client.rest_query(
                http,
                "SELECT Id FROM ObjectPermissions WHERE Parent.Name IN "
                f"('{_PERMSET_CASE_READER}', '{_PERMSET_NO_CASE}')",
            )
            for row in obj_rows:
                rid = str(row["Id"])
                await self._client.delete_path(
                    http, f"/services/data/{self._api}/sobjects/ObjectPermissions/{rid}"
                )
            ps_rows = await self._client.rest_query(
                http,
                "SELECT Id FROM PermissionSet WHERE Name IN "
                f"('{_PERMSET_CASE_READER}', '{_PERMSET_NO_CASE}')",
            )
            for row in ps_rows:
                rid = str(row["Id"])
                await self._client.delete_path(
                    http, f"/services/data/{self._api}/sobjects/PermissionSet/{rid}"
                )
        if manifest_dir is not None:
            SeedManifest.delete(manifest_dir, "salesforce")
