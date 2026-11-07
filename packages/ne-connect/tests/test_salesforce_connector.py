# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SalesforceConnector: crawl-once caching, Bulk pagination, fail-closed quarantine (NE-047)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ne.connect.errors import InvalidRecordError, SalesforceSyncError
from ne.connect.salesforce.acl import PERMSET_PREFIX, PROFILE_PREFIX
from ne.connect.salesforce.config import SalesforceConnectorSettings, SalesforceObjectConfig
from ne.connect.salesforce.connector import SalesforceConnector

from .salesforce_transport import (
    OBJ_ACCOUNT,
    OBJ_FORGED,
    OBJ_NOPERM,
    OBJ_OPP,
    PERMSET_ID,
    PROFILE_ID,
    build_salesforce_transport,
    salesforce_settings,
)


async def _drain(connector: SalesforceConnector):
    return [record async for record in connector.backfill()]


def _account_only_settings(**overrides: object) -> SalesforceConnectorSettings:
    return salesforce_settings(
        objects=(
            SalesforceObjectConfig(
                name=OBJ_ACCOUNT,
                content_fields=("Name", "Description"),
                metadata_fields=("Industry",),
            ),
        ),
        **overrides,
    )


def test_discover_is_config_driven_with_zero_requests() -> None:
    settings = salesforce_settings()
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder)
    connector = SalesforceConnector(settings, transport=transport)

    objects = asyncio.run(connector.discover())
    assert {o.source_id for o in objects} == {OBJ_ACCOUNT, OBJ_OPP, OBJ_NOPERM, OBJ_FORGED}
    assert all(o.kind == "sobject" for o in objects)
    assert recorder == []  # config-driven — no crawl triggered


def test_backfill_yields_five_records_with_expected_shape() -> None:
    settings = salesforce_settings()
    transport = build_salesforce_transport()
    connector = SalesforceConnector(settings, transport=transport)

    records = asyncio.run(_drain(connector))
    assert len(records) == 5
    by_source = {}
    for record in records:
        by_source.setdefault(record.source_id, []).append(record)
    assert {r.record_id for r in by_source[OBJ_ACCOUNT]} == {
        "001000000000001AAA",
        "001000000000002AAA",
        "001000000000003AAA",
    }
    assert {r.record_id for r in by_source[OBJ_OPP]} == {
        "006000000000001AAA",
        "006000000000002AAA",
    }
    acc = next(r for r in by_source[OBJ_ACCOUNT] if r.record_id == "001000000000001AAA")
    assert acc.payload["Name"] == "Acme Corp"
    assert acc.payload["Industry"] == "Manufacturing"


def test_crawl_once_cached_second_backfill_issues_no_new_requests() -> None:
    settings = salesforce_settings()
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder)
    connector = SalesforceConnector(settings, transport=transport)

    first = asyncio.run(_drain(connector))
    count_after_first = len(recorder)
    second = asyncio.run(_drain(connector))
    assert len(recorder) == count_after_first  # cache drained, no new HTTP
    assert len(first) == len(second) == 5


def test_acl_entries_match_expected_object_level_grants() -> None:
    settings = salesforce_settings()
    transport = build_salesforce_transport()
    connector = SalesforceConnector(settings, transport=transport)

    async def scenario():
        return [entry async for entry in connector.permissions()]

    entries = asyncio.run(scenario())
    by_resource = {e.resource_id: e.principals for e in entries}
    assert by_resource[OBJ_ACCOUNT] == (
        f"{PROFILE_PREFIX}{PROFILE_ID}",
        f"{PERMSET_PREFIX}{PERMSET_ID}",
    )
    assert by_resource[OBJ_OPP] == (f"{PERMSET_PREFIX}{PERMSET_ID}",)
    # Quarantined objects never get an AclEntry at all.
    assert OBJ_NOPERM not in by_resource
    assert OBJ_FORGED not in by_resource


def test_quarantined_objects_never_get_a_bulk_job_and_are_counted() -> None:
    settings = salesforce_settings()
    transport = build_salesforce_transport()  # default asserts no job for NOPERM/FORGED
    connector = SalesforceConnector(settings, transport=transport)

    asyncio.run(_drain(connector))
    assert connector.acl_quarantined == 2
    assert set(connector.acl_quarantined_ids) == {OBJ_NOPERM, OBJ_FORGED}


def test_bulk_pagination_via_sforce_locator_covers_both_account_pages() -> None:
    settings = salesforce_settings()
    transport = build_salesforce_transport()
    connector = SalesforceConnector(settings, transport=transport)

    records = asyncio.run(_drain(connector))
    account_ids = {r.record_id for r in records if r.source_id == OBJ_ACCOUNT}
    assert account_ids == {
        "001000000000001AAA",  # page 1
        "001000000000002AAA",  # page 1
        "001000000000003AAA",  # page 2 (delivered via Sforce-Locator "p2")
    }


@pytest.mark.parametrize(
    ("declared", "message"),
    ((2, "returned more rows than declared"), (4, "row count does not match")),
)
def test_bulk_declared_processed_count_must_equal_collected_rows(
    declared: int, message: str
) -> None:
    connector = SalesforceConnector(
        _account_only_settings(),
        transport=build_salesforce_transport(processed_count_overrides={OBJ_ACCOUNT: declared}),
    )

    with pytest.raises(SalesforceSyncError, match=message):
        asyncio.run(_drain(connector))
    assert connector.pending_cursors == {}


def test_bulk_repeated_locator_refuses_before_exposing_object() -> None:
    connector = SalesforceConnector(
        _account_only_settings(),
        transport=build_salesforce_transport(repeat_locator_for=OBJ_ACCOUNT),
    )

    with pytest.raises(SalesforceSyncError, match="repeated a result locator"):
        asyncio.run(_drain(connector))
    assert connector.pending_cursors == {}


def test_bulk_page_bound_refuses_before_following_second_page() -> None:
    recorder: list[httpx.Request] = []
    connector = SalesforceConnector(
        _account_only_settings(bulk_max_result_pages=1),
        transport=build_salesforce_transport(recorder=recorder),
    )

    with pytest.raises(SalesforceSyncError, match="result-page bound"):
        asyncio.run(_drain(connector))
    result_gets = [request for request in recorder if request.url.path.endswith("/results")]
    assert len(result_gets) == 1
    assert connector.pending_cursors == {}


def test_bulk_record_bound_refuses_before_result_fetch() -> None:
    recorder: list[httpx.Request] = []
    connector = SalesforceConnector(
        _account_only_settings(bulk_max_records_per_job=2),
        transport=build_salesforce_transport(recorder=recorder),
    )

    with pytest.raises(SalesforceSyncError, match="invalid processed count"):
        asyncio.run(_drain(connector))
    assert not any(request.url.path.endswith("/results") for request in recorder)
    assert connector.pending_cursors == {}


def test_poll_path_covers_in_progress_then_job_complete() -> None:
    settings = salesforce_settings()
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder)
    connector = SalesforceConnector(settings, transport=transport)

    asyncio.run(_drain(connector))
    status_calls = [r for r in recorder if r.method == "GET" and r.url.path.endswith("/750FAKEACC")]
    assert len(status_calls) == 2  # InProgress once, then JobComplete


def test_job_failed_state_aborts_run_with_no_pending_cursor() -> None:
    settings = salesforce_settings(
        objects=(
            SalesforceObjectConfig(
                name=OBJ_ACCOUNT,
                content_fields=("Name", "Description"),
                metadata_fields=("Industry",),
            ),
        )
    )
    transport = build_salesforce_transport(fail_job_for=OBJ_ACCOUNT)
    connector = SalesforceConnector(settings, transport=transport)

    with pytest.raises(SalesforceSyncError):
        asyncio.run(_drain(connector))
    assert connector.pending_cursors == {}


def test_object_permissions_query_5xx_aborts_the_run() -> None:
    settings = salesforce_settings()
    # fail_after=1: the token POST (call #1) succeeds, the ObjectPermissions
    # query (call #2) gets HTTP 500.
    transport = build_salesforce_transport(fail_after=1)
    connector = SalesforceConnector(settings, transport=transport)

    with pytest.raises(SalesforceSyncError):
        asyncio.run(_drain(connector))
    assert connector.pending_cursors == {}
    assert connector.acl_quarantined == 0  # never even resolved perms


def test_csv_row_missing_id_raises_invalid_record_error() -> None:
    settings = salesforce_settings(
        objects=(SalesforceObjectConfig(name="Widget__c", content_fields=("Name",)),)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/services/oauth2/token":
            return httpx.Response(200, json={"access_token": "FAKE-TOKEN", "token_type": "Bearer"})
        if path.endswith("/jobs/query"):
            return httpx.Response(200, json={"id": "750FAKEWID", "state": "UploadComplete"})
        if path.endswith("/query"):
            return httpx.Response(
                200,
                json={
                    "totalSize": 1,
                    "done": True,
                    "records": [
                        {
                            "SobjectType": "Widget__c",
                            "PermissionsRead": True,
                            "Parent": {
                                "Id": "0PS000000000001AAA",
                                "IsOwnedByProfile": False,
                                "ProfileId": None,
                            },
                        }
                    ],
                },
            )
        if path.endswith("/750FAKEWID"):
            return httpx.Response(
                200,
                json={
                    "id": "750FAKEWID",
                    "state": "JobComplete",
                    "numberRecordsProcessed": 1,
                },
            )
        if path.endswith("/750FAKEWID/results"):
            return httpx.Response(
                200,
                text="Id,SystemModstamp,Name\n,2026-01-01T00:00:00.000Z,NoId\n",
                headers={"Sforce-Locator": "null"},
            )
        return httpx.Response(404)  # pragma: no cover

    transport = httpx.MockTransport(handler)
    connector = SalesforceConnector(settings, transport=transport)

    with pytest.raises(InvalidRecordError):
        asyncio.run(_drain(connector))


def test_incremental_with_differing_basis_recrawls_with_modstamp_where_clause() -> None:
    settings = salesforce_settings()
    recorder: list[httpx.Request] = []
    transport = build_salesforce_transport(recorder=recorder, incremental=True)
    connector = SalesforceConnector(settings, transport=transport)

    async def scenario():
        return [
            record
            async for record in connector.incremental(
                {"modstamp:Account": "2026-01-03T00:00:00.000Z"}
            )
        ]

    records = asyncio.run(scenario())
    assert {r.record_id for r in records if r.source_id == OBJ_ACCOUNT} == {"001000000000004AAA"}
    job_posts = [r for r in recorder if r.method == "POST" and r.url.path.endswith("/jobs/query")]
    account_job_bodies = [
        json.loads(r.content.decode()) for r in job_posts if "Account" in r.content.decode()
    ]
    assert any("WHERE SystemModstamp >" in body["query"] for body in account_job_bodies)


def test_pending_cursors_carry_max_modstamp_per_object() -> None:
    settings = salesforce_settings()
    transport = build_salesforce_transport()
    connector = SalesforceConnector(settings, transport=transport)

    asyncio.run(_drain(connector))
    assert connector.pending_cursors[OBJ_ACCOUNT] == "2026-01-03T00:00:00.000Z"
    assert connector.pending_cursors[OBJ_OPP] == "2026-01-02T00:00:00.000Z"
