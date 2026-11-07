# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Salesforce seeder: external-id preflight, upsert-not-create, the Case trap, unseed."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ne.acme_seed.errors import SeedPreflightError
from ne.acme_seed.salesforce import SalesforceSeeder, SeedSalesforceClient
from ne.acme_seed.slice import slice_corpus

from .conftest import build_salesforce_transport, salesforce_settings


def _bodies(recorder: list[httpx.Request], method: str) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for r in recorder:
        if r.method == method and "/sobjects/" in r.url.path:
            out.append((r.url.path, json.loads(r.content.decode()) if r.content else {}))
    return out


def _upsert_body(recorder: list[httpx.Request], obj: str, corpus_id: str) -> dict:
    for path, body in _bodies(recorder, "PATCH"):
        if path.endswith(f"/sobjects/{obj}/Acme_Source_Id__c/{corpus_id}"):
            return body
    raise AssertionError(f"no upsert for {obj}/{corpus_id}")


def test_check_fails_with_owner_action_when_external_id_field_missing() -> None:
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder, external_id_present=False)
    seeder = SalesforceSeeder(salesforce_settings(), transport=transport)
    with pytest.raises(SeedPreflightError) as exc:
        asyncio.run(seeder.push(slice_corpus()))
    message = str(exc.value)
    assert "Acme_Source_Id__c" in message
    assert "Account" in message and "Contact" in message and "Case" in message
    # Zero write requests: no PATCH, no POST/DELETE to any sobject.
    assert _bodies(recorder, "PATCH") == []
    assert _bodies(recorder, "POST") == []
    assert _bodies(recorder, "DELETE") == []


def test_push_upserts_by_external_id_not_create() -> None:
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder)
    seeder = SalesforceSeeder(salesforce_settings(), transport=transport)
    asyncio.run(seeder.push(slice_corpus()))

    # Every record write is a PATCH upsert-by-external-id.
    for path, _ in _bodies(recorder, "PATCH"):
        assert "/Acme_Source_Id__c/" in path
    # ZERO POST record-creates for the seeded objects (permsets/objectperms are OK).
    creates = [
        path
        for path, _ in _bodies(recorder, "POST")
        if path.endswith(("/sobjects/Account", "/sobjects/Contact", "/sobjects/Case"))
    ]
    assert creates == []


def test_push_maps_fields_account_contact_case() -> None:
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder)
    seeder = SalesforceSeeder(salesforce_settings(), transport=transport)
    asyncio.run(seeder.push(slice_corpus()))

    account = _upsert_body(recorder, "Account", "acct-0000")
    assert account["Name"] == "Acme Corporation"
    assert account["NumberOfEmployees"] == 40

    # Contact.AccountId is wired from THIS run's account live-id map.
    contact = _upsert_body(recorder, "Contact", "per-0000")
    assert contact["LastName"] == "Uribe"
    assert contact["AccountId"] == "live-acct-0000"  # per-0000 works_at acct-0000

    # Case: Status/Priority mapping + AccountId from belongs_to.
    case = _upsert_body(recorder, "Case", "tkt-0000")
    assert case["Status"] == "Closed"  # ticket status 'closed'
    assert case["Priority"] == "Medium"  # ticket priority 'normal'
    assert case["AccountId"] == "live-acct-0043"  # tkt-0000 belongs_to acct-0043


def test_push_creates_permsets_once_and_case_trap_shape() -> None:
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder)
    seeder = SalesforceSeeder(salesforce_settings(), transport=transport)
    manifest = asyncio.run(seeder.push(slice_corpus()))

    permset_posts = [
        body for path, body in _bodies(recorder, "POST") if path.endswith("/sobjects/PermissionSet")
    ]
    assert {p["Name"] for p in permset_posts} == {"Acme_Case_Reader", "Acme_No_Case"}

    objperm_posts = [
        body
        for path, body in _bodies(recorder, "POST")
        if path.endswith("/sobjects/ObjectPermissions")
    ]
    # EXACTLY one ObjectPermissions row — for Acme_Case_Reader, Case read.
    assert len(objperm_posts) == 1
    row = objperm_posts[0]
    assert row["SobjectType"] == "Case"
    assert row["PermissionsRead"] is True
    assert row["ParentId"] == "0PS-Acme_Case_Reader"

    assert manifest.personas["per-0006"] == "Acme_Case_Reader"
    assert manifest.personas["per-0016"] == "Acme_No_Case"

    # Idempotent re-provision: permsets + Case row already present -> no creates.
    recorder2: list[httpx.Request] = []
    transport2 = build_salesforce_transport(
        recorder=recorder2,
        existing_permsets={"Acme_Case_Reader": "0PS-1", "Acme_No_Case": "0PS-2"},
        existing_case_read=True,
    )
    asyncio.run(SalesforceSeeder(salesforce_settings(), transport=transport2).push(slice_corpus()))
    assert [p for p, _ in _bodies(recorder2, "POST") if p.endswith("/sobjects/PermissionSet")] == []
    assert [
        p for p, _ in _bodies(recorder2, "POST") if p.endswith("/sobjects/ObjectPermissions")
    ] == []


def test_unseed_deletes_seeded_records_and_permsets() -> None:
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(
        recorder=recorder,
        existing_permsets={"Acme_Case_Reader": "0PS-1", "Acme_No_Case": "0PS-2"},
        unseed_ids={
            "Account": ["a1"],
            "Contact": ["c1"],
            "Case": ["k1"],
            "ObjectPermissions": ["ope1"],
        },
    )
    seeder = SalesforceSeeder(salesforce_settings(), transport=transport)
    asyncio.run(seeder.unseed())

    deletes = [r.url.path for r in recorder if r.method == "DELETE"]
    # Reverse FK order, then ObjectPermissions, then the 2 permsets.
    assert any(p.endswith("/sobjects/Case/k1") for p in deletes)
    assert any(p.endswith("/sobjects/Contact/c1") for p in deletes)
    assert any(p.endswith("/sobjects/Account/a1") for p in deletes)
    assert deletes.index(next(p for p in deletes if p.endswith("/Case/k1"))) < deletes.index(
        next(p for p in deletes if p.endswith("/Account/a1"))
    )
    assert any(p.endswith("/sobjects/ObjectPermissions/ope1") for p in deletes)
    assert any(p.endswith("/sobjects/PermissionSet/0PS-1") for p in deletes)
    assert any(p.endswith("/sobjects/PermissionSet/0PS-2") for p in deletes)


def test_injection_shaped_identifiers_refused() -> None:
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder)
    client = SeedSalesforceClient(salesforce_settings(), transport=transport)

    async def _run() -> None:
        async with client.session() as http:
            await client.upsert_by_external_id(
                http, "Account WHERE 1=1", "acct-0000", {"Name": "x"}, api_version="v65.0"
            )

    with pytest.raises(ValueError):
        asyncio.run(_run())
    # Rejected at the boundary — not even a token POST left.
    assert recorder == []
