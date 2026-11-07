# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Salesforce seeder with owned-key upserts and prevalidated safe unseed."""

from __future__ import annotations

import re
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ne.acme_seed.salesforce import SeedSalesforceClient as AcmeSeedSalesforceClient
from ne.connect.salesforce.client import Client, Transport
from ne.connect.salesforce.config import SalesforceConnectorSettings
from ne.meridian_seed.errors import SeedPreflightError, SeedSafetyError
from ne.meridian_seed.manifest import SeedManifest, StageHistoryEntry
from ne.meridian_seed.slice import SliceSelection

__all__ = [
    "EXTERNAL_ID_FIELD",
    "SalesforceSeeder",
    "SeedSalesforceClient",
    "owned_external_value",
]

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SOURCE_ID = re.compile(r"^mer-(?:hh|cus|tkt|cmp|dsp)-[0-9]{3}$")
_LIVE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,80}$")
EXTERNAL_ID_FIELD = "Meridian_Source_Id__c"
_PREFIX = "meridian-v1:"
_SEED_OBJECTS: tuple[str, ...] = ("Account", "Contact", "Case")
_PERMISSION_OBJECTS: dict[str, tuple[str, ...]] = {
    "Meridian_Ops_Reader": ("Case",),
    "Meridian_Approver_Reader": ("Account", "Contact", "Case"),
    "Meridian_Frontline_Reader": ("Account", "Contact"),
}
_ELEVATED_PERMISSION_FIELDS = (
    "PermissionsCreate",
    "PermissionsEdit",
    "PermissionsDelete",
    "PermissionsViewAllRecords",
    "PermissionsModifyAllRecords",
)


def _check_identifier(name: str, field: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise SeedSafetyError(f"{field} {name!r} is not a safe Salesforce identifier")
    return name


def _check_live_id(value: str) -> str:
    if not _LIVE_ID.fullmatch(value):
        raise SeedSafetyError(f"unsafe Salesforce live id {value!r}")
    return value


def owned_external_value(source_id: str) -> str:
    if not _SOURCE_ID.fullmatch(source_id):
        raise SeedSafetyError(f"source id {source_id!r} is outside the Meridian seed namespace")
    return _PREFIX + source_id


def _validate_owned_row(row: dict[str, Any]) -> tuple[str, str]:
    live_id = _check_live_id(str(row.get("Id", "")))
    external = str(row.get(EXTERNAL_ID_FIELD, ""))
    if not external.startswith(_PREFIX):
        raise SeedSafetyError(
            f"refusing to unseed Salesforce row {live_id!r} without Meridian ownership prefix"
        )
    owned_external_value(external.removeprefix(_PREFIX))
    return live_id, external


class SeedSalesforceClient(AcmeSeedSalesforceClient):
    async def upsert_by_external_id(
        self,
        client: Client,
        sobject: str,
        external_value: str,
        fields: dict[str, Any],
        *,
        api_version: str,
    ) -> str:
        _check_identifier(sobject, "sobject")
        _check_identifier(EXTERNAL_ID_FIELD, "external id field")
        for name in fields:
            _check_identifier(name, "field name")
        external = urllib.parse.quote(owned_external_value(external_value), safe="")
        payload = await self.patch_json(
            client,
            f"/services/data/{api_version}/sobjects/{sobject}/{EXTERNAL_ID_FIELD}/{external}",
            fields,
        )
        return _check_live_id(str(payload["id"]))


class SalesforceSeeder:
    def __init__(
        self,
        settings: SalesforceConnectorSettings,
        *,
        transport: Transport | None = None,
    ) -> None:
        self._settings = settings
        self._api = settings.api_version
        self._client = SeedSalesforceClient(settings, transport=transport)

    async def check(self, client: Client) -> None:
        invalid: list[str] = []
        for sobject in _SEED_OBJECTS:
            payload = await self._client.get_json(
                client, f"/services/data/{self._api}/sobjects/{sobject}/describe"
            )
            field = next(
                (
                    row
                    for row in payload.get("fields", [])
                    if str(row.get("name")) == EXTERNAL_ID_FIELD
                ),
                None,
            )
            if field is None or not (
                field.get("type") == "string"
                and field.get("length") == 128
                and field.get("externalId") is True
                and field.get("unique") is True
            ):
                invalid.append(sobject)
        if invalid:
            raise SeedPreflightError(
                f"Salesforce objects {', '.join(invalid)} are missing or misconfigured: "
                f"{EXTERNAL_ID_FIELD}. OWNER ACTION: create Text(128), External ID, Unique on "
                "Account, Contact, and Case, then grant field-level access to the integration "
                "profile."
            )

    async def _ensure_permissions(self, client: Client, manifest: SeedManifest) -> None:
        names = tuple(_PERMISSION_OBJECTS)
        quoted_names = ", ".join(f"'{name}'" for name in names)
        rows = await self._client.rest_query(
            client, f"SELECT Id, Name FROM PermissionSet WHERE Name IN ({quoted_names})"
        )
        existing = {str(row["Name"]): _check_live_id(str(row["Id"])) for row in rows}

        permission_fields = ", ".join(_ELEVATED_PERMISSION_FIELDS)
        rows = await self._client.rest_query(
            client,
            "SELECT Id, Parent.Name, SobjectType, PermissionsRead, "
            f"{permission_fields} FROM ObjectPermissions "
            f"WHERE Parent.Name IN ({quoted_names})",
        )
        expected_permissions = {
            (permset, sobject)
            for permset, objects in _PERMISSION_OBJECTS.items()
            for sobject in objects
        }
        existing_permissions: set[tuple[str, str]] = set()
        for row in rows:
            key = (
                str((row.get("Parent") or {}).get("Name")),
                str(row.get("SobjectType")),
            )
            if (
                key not in expected_permissions
                or row.get("PermissionsRead") is not True
                or any(row.get(field) is not False for field in _ELEVATED_PERMISSION_FIELDS)
            ):
                raise SeedPreflightError(
                    "existing Meridian ObjectPermissions are broader than the exact read-only "
                    "persona matrix; refusing before mutation"
                )
            existing_permissions.add(key)

        for name in names:
            if name in existing:
                manifest.permsets[name] = existing[name]
            else:
                created = await self._client.post_json(
                    client,
                    f"/services/data/{self._api}/sobjects/PermissionSet",
                    {"Name": name, "Label": name},
                )
                manifest.permsets[name] = _check_live_id(str(created["id"]))

        for permset, objects in _PERMISSION_OBJECTS.items():
            for sobject in objects:
                if (permset, sobject) in existing_permissions:
                    continue
                await self._client.post_json(
                    client,
                    f"/services/data/{self._api}/sobjects/ObjectPermissions",
                    {
                        "ParentId": manifest.permsets[permset],
                        "SobjectType": sobject,
                        "PermissionsRead": True,
                        "PermissionsCreate": False,
                        "PermissionsEdit": False,
                        "PermissionsDelete": False,
                        "PermissionsViewAllRecords": False,
                        "PermissionsModifyAllRecords": False,
                    },
                )

    async def push(self, selection: SliceSelection) -> SeedManifest:
        # Validate every source identifier before the preflight's first token request.
        for source_id in (
            *(record.source_id for record in selection.accounts),
            *(record.source_id for record in selection.contacts),
            *(record.source_id for record in selection.cases),
        ):
            owned_external_value(source_id)
        manifest = SeedManifest(
            tenant="salesforce",
            seeded_at=datetime.now(UTC).isoformat(),
            corpus_version=selection.corpus_version,
            personas={
                "ops": "Meridian_Ops_Reader",
                "approver": "Meridian_Approver_Reader",
                "frontline": "Meridian_Frontline_Reader",
            },
        )
        async with self._client.session() as http:
            await self.check(http)
            await self._ensure_permissions(http, manifest)
            account_live: dict[str, str] = {}
            for account in selection.accounts:
                live = await self._client.upsert_by_external_id(
                    http,
                    "Account",
                    account.source_id,
                    {"Name": account.name},
                    api_version=self._api,
                )
                account_live[account.source_id] = live
                manifest.records[account.source_id] = live
            for contact in selection.contacts:
                live = await self._client.upsert_by_external_id(
                    http,
                    "Contact",
                    contact.source_id,
                    {
                        "FirstName": contact.first_name,
                        "LastName": contact.last_name,
                        "Email": contact.email,
                        "AccountId": account_live[contact.account_source_id],
                    },
                    api_version=self._api,
                )
                manifest.records[contact.source_id] = live
            for case in selection.cases:
                live = await self._client.upsert_by_external_id(
                    http,
                    "Case",
                    case.source_id,
                    {
                        "Type": case.case_type,
                        "Subject": case.subject,
                        "Description": case.description,
                        "Status": case.status,
                        "Priority": case.priority,
                        "AccountId": account_live[case.account_source_id],
                    },
                    api_version=self._api,
                )
                manifest.records[case.source_id] = live
        manifest.stage_history.append(
            StageHistoryEntry(stage="initial", completed_at=datetime.now(UTC).isoformat())
        )
        return manifest

    async def unseed(self, manifest_dir: Path | None = None) -> None:
        async with self._client.session() as http:
            # Resolve and validate the complete deletion set before the first DELETE.
            owned_rows: dict[str, list[str]] = {}
            for sobject in reversed(_SEED_OBJECTS):
                rows = await self._client.rest_query(
                    http,
                    f"SELECT Id, {EXTERNAL_ID_FIELD} FROM {sobject} "
                    f"WHERE {EXTERNAL_ID_FIELD} LIKE '{_PREFIX}%'",
                )
                owned_rows[sobject] = [_validate_owned_row(dict(row))[0] for row in rows]

            names = tuple(_PERMISSION_OBJECTS)
            quoted_names = ", ".join(f"'{name}'" for name in names)
            permission_rows = await self._client.rest_query(
                http,
                "SELECT Id, Parent.Name FROM ObjectPermissions "
                f"WHERE Parent.Name IN ({quoted_names})",
            )
            checked_permission_ids: list[str] = []
            for row in permission_rows:
                if str((row.get("Parent") or {}).get("Name")) not in _PERMISSION_OBJECTS:
                    raise SeedSafetyError("Salesforce returned an unowned ObjectPermissions row")
                checked_permission_ids.append(_check_live_id(str(row["Id"])))
            permset_rows = await self._client.rest_query(
                http, f"SELECT Id, Name FROM PermissionSet WHERE Name IN ({quoted_names})"
            )
            checked_permset_ids: list[str] = []
            for row in permset_rows:
                if str(row.get("Name")) not in _PERMISSION_OBJECTS:
                    raise SeedSafetyError("Salesforce returned an unowned PermissionSet row")
                checked_permset_ids.append(_check_live_id(str(row["Id"])))

            for sobject in reversed(_SEED_OBJECTS):
                for live_id in owned_rows[sobject]:
                    await self._client.delete_path(
                        http, f"/services/data/{self._api}/sobjects/{sobject}/{live_id}"
                    )
            for live_id in checked_permission_ids:
                await self._client.delete_path(
                    http,
                    f"/services/data/{self._api}/sobjects/ObjectPermissions/{live_id}",
                )
            for live_id in checked_permset_ids:
                await self._client.delete_path(
                    http, f"/services/data/{self._api}/sobjects/PermissionSet/{live_id}"
                )
        if manifest_dir is not None:
            SeedManifest.delete(manifest_dir, "salesforce")
