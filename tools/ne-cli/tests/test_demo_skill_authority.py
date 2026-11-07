# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused proof for the Pilot Meridian derived Skill authority projector."""

from __future__ import annotations

import inspect
from typing import Any

import httpx
import pytest

from ne.cli.demo import live, skill_authority


class _Response:
    def __init__(self, payload: Any = None, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://keycloak.invalid/redacted")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "authority request failed", request=request, response=response
            )


class _Client:
    def __init__(
        self,
        source: set[str],
        target: set[str],
        *,
        ambiguous: str | None = None,
        malformed_member: bool = False,
        duplicate_member: bool = False,
        fail_page: int | None = None,
    ) -> None:
        self.members = {"source-id": set(source), "target-id": set(target)}
        self.ambiguous = ambiguous
        self.malformed_member = malformed_member
        self.duplicate_member = duplicate_member
        self.fail_page = fail_page
        self.writes: list[tuple[str, str]] = []
        self.reads: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, *, params: dict[str, Any], headers: dict[str, str]) -> _Response:
        self.reads.append((url, params))
        if url.endswith("/groups"):
            name = params["search"]
            group_id = "source-id" if name == skill_authority.SOURCE_GROUP else "target-id"
            rows = [{"id": group_id, "name": name}]
            if self.ambiguous == name:
                rows.append({"id": f"{group_id}-duplicate", "name": name})
            return _Response(rows)

        group_id = url.split("/groups/", 1)[1].split("/members", 1)[0]
        first = params["first"]
        if self.fail_page == first:
            return _Response(status_code=503)
        ordered = sorted(self.members[group_id])
        page = ordered[first : first + params["max"]]
        rows: list[dict[str, Any]] = [{"id": member_id} for member_id in page]
        if self.malformed_member and group_id == "source-id" and first == 0:
            rows = [{"username": "sensitive@example.invalid"}]
        if self.duplicate_member and group_id == "source-id" and first > 0 and rows:
            rows[0] = {"id": ordered[0]}
        return _Response(rows)

    def put(self, url: str, *, headers: dict[str, str]) -> _Response:
        member_id, group_id = _membership_parts(url)
        self.writes.append(("PUT", member_id))
        self.members[group_id].add(member_id)
        return _Response()

    def delete(self, url: str, *, headers: dict[str, str]) -> _Response:
        member_id, group_id = _membership_parts(url)
        self.writes.append(("DELETE", member_id))
        self.members[group_id].discard(member_id)
        return _Response()


def _membership_parts(url: str) -> tuple[str, str]:
    tail = url.split("/users/", 1)[1]
    member_id, group_id = tail.split("/groups/", 1)
    return member_id, group_id


def _project(client: _Client) -> skill_authority.SkillAuthorityReceipt:
    return skill_authority.project_servicing_skill_authority(
        client, base="https://keycloak.invalid", headers={"Authorization": "Bearer secret-token"}
    )


def test_add_noop_remove_restore_and_unrelated_memberships_are_exact() -> None:
    client = _Client({"ops-subject"}, {"stale-frontline"})

    receipt = _project(client)
    assert receipt == skill_authority.SkillAuthorityReceipt(
        status="synchronized", added=1, removed=1, unchanged=0
    )
    assert client.members["target-id"] == {"ops-subject"}
    assert client.writes == [("PUT", "ops-subject"), ("DELETE", "stale-frontline")]

    client.writes.clear()
    no_op = _project(client)
    assert no_op.added == no_op.removed == 0
    assert client.writes == []

    client.members["source-id"].remove("ops-subject")
    removal = _project(client)
    assert removal.removed == 1
    assert client.members["target-id"] == set()

    client.members["source-id"].add("ops-subject")
    restoration = _project(client)
    assert restoration.added == 1
    assert client.members["target-id"] == {"ops-subject"}
    # The source group is read-only and unrelated memberships are outside the only
    # target-group URLs the projector can emit.
    assert client.members["source-id"] == {"ops-subject"}
    assert {operation for operation, _member_id in client.writes} == {"PUT", "DELETE"}


def test_complete_multipage_sets_are_read_before_first_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skill_authority, "_PAGE_SIZE", 2)
    client = _Client({"a", "b", "c"}, {"a", "stale"})

    receipt = _project(client)

    assert receipt == skill_authority.SkillAuthorityReceipt(
        status="synchronized", added=2, removed=1, unchanged=1
    )
    member_reads = [(url, params["first"]) for url, params in client.reads if "/members" in url]
    assert member_reads == [
        ("https://keycloak.invalid/admin/realms/ne-pilot/groups/source-id/members", 0),
        ("https://keycloak.invalid/admin/realms/ne-pilot/groups/source-id/members", 2),
        ("https://keycloak.invalid/admin/realms/ne-pilot/groups/target-id/members", 0),
        ("https://keycloak.invalid/admin/realms/ne-pilot/groups/target-id/members", 2),
    ]


@pytest.mark.parametrize(
    ("client", "message"),
    [
        (_Client(set(), set(), ambiguous=skill_authority.SOURCE_GROUP), "uniquely"),
        (_Client({"private-user"}, set(), malformed_member=True), "malformed"),
        (_Client({"a", "b", "c"}, set(), duplicate_member=True), "duplicated"),
        (_Client({"a", "b", "c"}, set(), fail_page=2), "authority request failed"),
    ],
)
def test_invalid_or_incomplete_authority_is_content_closed_and_zero_write(
    monkeypatch: pytest.MonkeyPatch, client: _Client, message: str
) -> None:
    monkeypatch.setattr(skill_authority, "_PAGE_SIZE", 2)

    with pytest.raises((skill_authority.SkillAuthorityError, httpx.HTTPStatusError)) as exc:
        _project(client)

    rendered = str(exc.value)
    assert message in rendered
    assert "private-user" not in rendered
    assert "sensitive@example.invalid" not in rendered
    assert "secret-token" not in rendered
    assert client.writes == []


def test_live_integration_is_post_source_reconciliation_and_has_no_direct_target_grant() -> None:
    source = inspect.getsource(live._provision_keycloak)
    reconcile = "reconcile_exact_groups(ops_id, _MERIDIAN_OPS_GROUPS)"
    project = "project_servicing_skill_authority(client, base=base, headers=headers)"

    assert source.index(reconcile) < source.index(project)
    assert '"department:servicing"' in source  # target group is ensured
    assert "department:servicing" not in repr(live._MERIDIAN_OPS_GROUPS)
    assert "department:servicing" not in repr(live._MERIDIAN_RESTRICTED_GROUPS)
    assert source.count(project) == 1
