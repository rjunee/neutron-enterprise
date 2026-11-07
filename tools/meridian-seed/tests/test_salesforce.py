# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Salesforce mapping, permission trap, idempotency, guards, and safe unseed."""

from __future__ import annotations

import asyncio
import json
import urllib.parse

import httpx
import pytest

from ne.connect.errors import SalesforceSyncError
from ne.meridian_seed.errors import SeedPreflightError, SeedSafetyError
from ne.meridian_seed.salesforce import (
    EXTERNAL_ID_FIELD,
    SalesforceSeeder,
    SeedSalesforceClient,
)
from ne.meridian_seed.slice import slice_corpus

from .conftest import build_salesforce_transport, salesforce_settings


def _sobject_requests(
    recorder: list[httpx.Request], method: str, sobject: str | None = None
) -> list[httpx.Request]:
    requests = [
        request
        for request in recorder
        if request.method == method and "/sobjects/" in request.url.path
    ]
    if sobject:
        requests = [request for request in requests if f"/sobjects/{sobject}" in request.url.path]
    return requests


def _upsert_body(recorder: list[httpx.Request], sobject: str, source_id: str) -> dict:
    suffix = f"/sobjects/{sobject}/{EXTERNAL_ID_FIELD}/meridian-v1:{source_id}"
    for request in _sobject_requests(recorder, "PATCH", sobject):
        if urllib.parse.unquote(request.url.path).endswith(suffix):
            return json.loads(request.content)
    raise AssertionError(f"missing upsert {sobject}/{source_id}")


def test_preflight_missing_external_id_fails_before_any_write() -> None:
    recorder: list[httpx.Request] = []
    seeder = SalesforceSeeder(
        salesforce_settings(),
        transport=build_salesforce_transport(
            recorder=recorder,
            external_id_present=False,
        ),
    )
    with pytest.raises(SeedPreflightError, match=EXTERNAL_ID_FIELD):
        asyncio.run(seeder.push(slice_corpus()))
    assert _sobject_requests(recorder, "PATCH") == []
    assert _sobject_requests(recorder, "POST") == []
    assert _sobject_requests(recorder, "DELETE") == []


def test_preflight_rejects_misconfigured_external_id_before_any_write() -> None:
    recorder: list[httpx.Request] = []
    seeder = SalesforceSeeder(
        salesforce_settings(),
        transport=build_salesforce_transport(
            recorder=recorder,
            external_id_valid=False,
        ),
    )
    with pytest.raises(SeedPreflightError, match="missing or misconfigured"):
        asyncio.run(seeder.push(slice_corpus()))
    assert _sobject_requests(recorder, "PATCH") == []
    assert _sobject_requests(recorder, "POST") == []
    assert _sobject_requests(recorder, "DELETE") == []


def test_push_maps_household_customer_and_all_case_types() -> None:
    recorder: list[httpx.Request] = []
    seeder = SalesforceSeeder(
        salesforce_settings(), transport=build_salesforce_transport(recorder=recorder)
    )
    manifest = asyncio.run(seeder.push(slice_corpus()))
    assert len(manifest.records) == 44
    assert [row.stage for row in manifest.stage_history] == ["initial"]

    account = _upsert_body(recorder, "Account", "mer-hh-001")
    assert account["Name"] == "Meridian Household 001"
    contact = _upsert_body(recorder, "Contact", "mer-cus-001")
    # mer-cus-001 carries the owner's demo inbox (NE-CS-SEND fixture decision).
    assert contact["Email"] == "owner@example.com"
    assert contact["AccountId"] == "live-mer-hh-001"
    servicing = _upsert_body(recorder, "Case", "mer-tkt-001")
    complaint = _upsert_body(recorder, "Case", "mer-cmp-001")
    dispute = _upsert_body(recorder, "Case", "mer-dsp-001")
    assert servicing["Type"] == "Servicing"
    assert complaint["Type"] == "Complaint"
    assert dispute["Type"] == "Reg E Dispute"
    assert "provisional_credit_issue_date: ABSENT" in dispute["Description"]
    assert all(
        f"/{EXTERNAL_ID_FIELD}/meridian-v1:" in urllib.parse.unquote(request.url.path)
        for request in _sobject_requests(recorder, "PATCH")
    )


def test_permission_sets_are_read_only_and_frontline_has_no_case() -> None:
    recorder: list[httpx.Request] = []
    manifest = asyncio.run(
        SalesforceSeeder(
            salesforce_settings(), transport=build_salesforce_transport(recorder=recorder)
        ).push(slice_corpus())
    )
    assert manifest.personas == {
        "ops": "Meridian_Ops_Reader",
        "approver": "Meridian_Approver_Reader",
        "frontline": "Meridian_Frontline_Reader",
    }
    rows = [
        json.loads(request.content)
        for request in _sobject_requests(recorder, "POST", "ObjectPermissions")
    ]
    by_parent = {(row["ParentId"], row["SobjectType"]): row for row in rows}
    assert ("ps-Meridian_Ops_Reader", "Case") in by_parent
    assert ("ps-Meridian_Approver_Reader", "Case") in by_parent
    assert ("ps-Meridian_Frontline_Reader", "Case") not in by_parent
    assert {key[1] for key in by_parent if key[0] == "ps-Meridian_Frontline_Reader"} == {
        "Account",
        "Contact",
    }
    for row in rows:
        assert row["PermissionsRead"] is True
        assert row["PermissionsCreate"] is False
        assert row["PermissionsEdit"] is False
        assert row["PermissionsDelete"] is False
        assert row["PermissionsModifyAllRecords"] is False


@pytest.mark.parametrize(
    "permission",
    [
        {
            "Id": "op-frontline-case",
            "Parent": {"Name": "Meridian_Frontline_Reader"},
            "SobjectType": "Case",
            "PermissionsRead": True,
            "PermissionsCreate": False,
            "PermissionsEdit": False,
            "PermissionsDelete": False,
            "PermissionsViewAllRecords": False,
            "PermissionsModifyAllRecords": False,
        },
        {
            "Id": "op-approver-write",
            "Parent": {"Name": "Meridian_Approver_Reader"},
            "SobjectType": "Account",
            "PermissionsRead": True,
            "PermissionsCreate": True,
            "PermissionsEdit": False,
            "PermissionsDelete": False,
            "PermissionsViewAllRecords": False,
            "PermissionsModifyAllRecords": False,
        },
    ],
)
def test_existing_broader_permission_fails_before_any_write(permission: dict) -> None:
    recorder: list[httpx.Request] = []
    seeder = SalesforceSeeder(
        salesforce_settings(),
        transport=build_salesforce_transport(
            recorder=recorder,
            existing_permission_rows=[permission],
        ),
    )
    with pytest.raises(SeedPreflightError, match="broader than the exact read-only"):
        asyncio.run(seeder.push(slice_corpus()))
    assert _sobject_requests(recorder, "POST") == []
    assert _sobject_requests(recorder, "PATCH") == []


def test_second_push_is_idempotent_for_records_and_permissions() -> None:
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder)
    seeder = SalesforceSeeder(salesforce_settings(), transport=transport)

    async def run():
        first = await seeder.push(slice_corpus())
        first_permission_posts = len(_sobject_requests(recorder, "POST"))
        second = await seeder.push(slice_corpus())
        return first, second, first_permission_posts

    first, second, first_permission_posts = asyncio.run(run())
    assert first.records == second.records
    assert len(_sobject_requests(recorder, "PATCH")) == 88
    assert len(_sobject_requests(recorder, "POST")) == first_permission_posts


def test_unseed_validates_complete_owned_set_before_first_delete() -> None:
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(
        recorder=recorder,
        unseed_rows={
            "Case": [{"Id": "case-1", EXTERNAL_ID_FIELD: "meridian-v1:mer-dsp-001"}],
            "Contact": [{"Id": "contact-1", EXTERNAL_ID_FIELD: "not-meridian:mer-cus-001"}],
            "Account": [{"Id": "account-1", EXTERNAL_ID_FIELD: "meridian-v1:mer-hh-001"}],
        },
    )
    with pytest.raises(SeedSafetyError, match="without Meridian ownership prefix"):
        asyncio.run(SalesforceSeeder(salesforce_settings(), transport=transport).unseed())
    assert _sobject_requests(recorder, "DELETE") == []


def test_unseed_deletes_owned_rows_in_reverse_fk_order() -> None:
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(
        recorder=recorder,
        unseed_rows={
            "Case": [{"Id": "case-1", EXTERNAL_ID_FIELD: "meridian-v1:mer-dsp-001"}],
            "Contact": [{"Id": "contact-1", EXTERNAL_ID_FIELD: "meridian-v1:mer-cus-001"}],
            "Account": [{"Id": "account-1", EXTERNAL_ID_FIELD: "meridian-v1:mer-hh-001"}],
        },
    )
    asyncio.run(SalesforceSeeder(salesforce_settings(), transport=transport).unseed())
    deletes = [
        urllib.parse.unquote(request.url.path) for request in recorder if request.method == "DELETE"
    ]
    assert deletes == [
        "/services/data/v65.0/sobjects/Case/case-1",
        "/services/data/v65.0/sobjects/Contact/contact-1",
        "/services/data/v65.0/sobjects/Account/account-1",
    ]


def test_unseed_removes_only_exact_meridian_permissions_after_records() -> None:
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(
        recorder=recorder,
        unseed_permission_rows=[{"Id": "op-owned", "Parent": {"Name": "Meridian_Ops_Reader"}}],
        unseed_permset_rows=[{"Id": "ps-owned", "Name": "Meridian_Ops_Reader"}],
    )
    asyncio.run(SalesforceSeeder(salesforce_settings(), transport=transport).unseed())
    deletes = [request.url.path for request in recorder if request.method == "DELETE"]
    assert deletes == [
        "/services/data/v65.0/sobjects/ObjectPermissions/op-owned",
        "/services/data/v65.0/sobjects/PermissionSet/ps-owned",
    ]


def test_source_identifier_and_hostile_path_fail_before_token_call() -> None:
    recorder: list[httpx.Request] = []
    client = SeedSalesforceClient(
        salesforce_settings(), transport=build_salesforce_transport(recorder=recorder)
    )

    async def bad_source() -> None:
        async with client.session() as http:
            await client.upsert_by_external_id(
                http,
                "Account",
                "mer-hh-001/../../victim",
                {"Name": "x"},
                api_version="v65.0",
            )

    with pytest.raises(SeedSafetyError, match="outside the Meridian seed namespace"):
        asyncio.run(bad_source())
    assert recorder == []

    async def hostile_path() -> None:
        async with client.session() as http:
            await client.patch_json(http, "https://evil.example.invalid/services/data", {})

    with pytest.raises(SalesforceSyncError, match="outside '/services/'"):
        asyncio.run(hostile_path())
    assert recorder == []


def test_salesforce_fails_closed_in_air_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_GAP", "true")
    with pytest.raises(RuntimeError, match="AIR_GAP"):
        SalesforceSeeder(salesforce_settings(), transport=build_salesforce_transport())
