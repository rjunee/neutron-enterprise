# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""JiraConnector: crawl-once caching, fail-closed project + per-item quarantine (NE-093)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ne.connect.errors import InvalidRecordError, JiraSyncError
from ne.connect.jira.config import JiraProjectConfig
from ne.connect.jira.connector import JiraConnector

from .jira_transport import (
    ACME_PROJECT_ID,
    GROUP_ID,
    PROJECT_ACME,
    PROJECT_FORGED,
    PROJECT_SECRET,
    ROLE_ID,
    USER_ID,
    build_jira_transport,
    jira_settings,
)


async def _drain(connector: JiraConnector) -> list:
    return [record async for record in connector.backfill()]


def test_discover_is_config_driven_with_zero_requests() -> None:
    recorder: list[httpx.Request] = []
    connector = JiraConnector(jira_settings(), transport=build_jira_transport(recorder=recorder))
    objects = asyncio.run(connector.discover())
    assert {o.source_id for o in objects} == {
        f"project/{PROJECT_ACME}",
        f"project/{PROJECT_SECRET}",
        f"project/{PROJECT_FORGED}",
    }
    assert all(o.kind == "project" for o in objects)
    assert recorder == []


def test_quarantined_projects_fetch_nothing_and_are_counted() -> None:
    # The transport asserts NO search/comment/board/sprint call for SECRET/FORGED.
    connector = JiraConnector(jira_settings(), transport=build_jira_transport())
    asyncio.run(_drain(connector))
    assert connector.acl_quarantined == 2
    assert set(connector.acl_quarantined_ids) == {PROJECT_SECRET, PROJECT_FORGED}


def test_surviving_project_acl_has_grants_plus_jira_project_label() -> None:
    connector = JiraConnector(jira_settings(), transport=build_jira_transport())

    async def scenario() -> dict[str, tuple[str, ...]]:
        _ = await _drain(connector)
        return {e.resource_id: e.principals async for e in connector.permissions()}

    by_resource = asyncio.run(scenario())
    expected = (
        f"jira-group:{GROUP_ID}",
        f"jira-role:{ACME_PROJECT_ID}:{ROLE_ID}",
        f"jira-user:{USER_ID}",
        f"jira-project:{ACME_PROJECT_ID}",
    )
    for source_id in ("project/ACME", "issues/ACME", "comments/ACME", "sprints/ACME"):
        assert by_resource[source_id] == expected
    # Quarantined projects never get an AclEntry.
    assert not any(r.endswith(("/SECRET", "/FORGED")) for r in by_resource)


def test_security_level_issue_is_quarantined_per_item() -> None:
    connector = JiraConnector(jira_settings(), transport=build_jira_transport())
    records = asyncio.run(_drain(connector))
    issue_ids = {r.record_id for r in records if r.source_id == "issues/ACME"}
    assert issue_ids == {"ACME-1", "ACME-3"}  # ACME-2 (security level) excluded
    assert "ACME-2" in connector.items_quarantined_ids


def test_restricted_comment_is_quarantined_per_item() -> None:
    connector = JiraConnector(jira_settings(), transport=build_jira_transport())
    records = asyncio.run(_drain(connector))
    comment_ids = {r.record_id for r in records if r.source_id == "comments/ACME"}
    assert comment_ids == {"40001"}  # 40002 (visibility-restricted) excluded
    assert "comment/40002" in connector.items_quarantined_ids


def test_project_and_sprint_records_present() -> None:
    connector = JiraConnector(jira_settings(), transport=build_jira_transport())
    records = asyncio.run(_drain(connector))
    by_source: dict[str, set[str]] = {}
    for r in records:
        by_source.setdefault(r.source_id, set()).add(r.record_id)
    assert by_source["project/ACME"] == {ACME_PROJECT_ID}
    assert by_source["sprints/ACME"] == {"501", "502"}


def test_per_project_cursor_captures_max_updated_minute() -> None:
    connector = JiraConnector(jira_settings(), transport=build_jira_transport())
    asyncio.run(_drain(connector))
    assert connector.pending_cursors == {"ACME": "2026-01-03 10:30"}


def test_crawl_once_cached_second_backfill_issues_no_new_requests() -> None:
    recorder: list[httpx.Request] = []
    connector = JiraConnector(jira_settings(), transport=build_jira_transport(recorder=recorder))
    first = asyncio.run(_drain(connector))
    count = len(recorder)
    second = asyncio.run(_drain(connector))
    assert len(recorder) == count
    assert len(first) == len(second) == 6


def test_incremental_with_differing_basis_recrawls_with_updated_where_clause() -> None:
    recorder: list[httpx.Request] = []
    connector = JiraConnector(
        jira_settings(), transport=build_jira_transport(recorder=recorder, incremental=True)
    )

    async def scenario() -> list:
        return [
            record async for record in connector.incremental({"updated:ACME": "2026-01-03 10:30"})
        ]

    records = asyncio.run(scenario())
    issue_ids = {r.record_id for r in records if r.source_id == "issues/ACME"}
    assert issue_ids == {"ACME-4"}  # only the incremental delta
    search_requests = [r for r in recorder if r.url.path == "/rest/api/3/search/jql"]
    assert any(
        'updated >= "2026-01-03 10:30"' in dict(r.url.params).get("jql", "")
        for r in search_requests
    )


def test_malformed_cursor_is_refused_before_jql_interpolation() -> None:
    connector = JiraConnector(jira_settings(), transport=build_jira_transport())

    async def scenario() -> list:
        return [record async for record in connector.incremental({"updated:ACME": 'x" OR "1"="1'})]

    with pytest.raises(JiraSyncError):
        asyncio.run(scenario())


def test_missing_issue_key_raises_invalid_record_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/permissionscheme"):
            return httpx.Response(
                200,
                json={
                    "permissions": [
                        {
                            "permission": "BROWSE_PROJECTS",
                            "holder": {"type": "group", "group": {"groupId": "g1"}},
                        }
                    ]
                },
            )
        if path == "/rest/api/3/project/ACME":
            return httpx.Response(200, json={"id": "10001", "key": "ACME", "name": "Acme"})
        if path == "/rest/api/3/search/jql":
            return httpx.Response(200, json={"issues": [{"id": "1", "fields": {}}], "isLast": True})
        return httpx.Response(404)

    settings = jira_settings(projects=(JiraProjectConfig(key="ACME"),))
    connector = JiraConnector(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(InvalidRecordError):
        asyncio.run(_drain(connector))
