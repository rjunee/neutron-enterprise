# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""EntraPrincipalSource over httpx.MockTransport recorded fixtures (no live Graph).

Exercises: full-sync pagination, delta-link-BEFORE-full ordering, DIRECT member
enumeration (incl. a nested group member + a skipped device — the transitive
closure is computed at query time, not flattened at sync time), delta sync with
@removed tombstoning, nested-membership revocation propagating to the ancestor
without a re-index (Codex NE-042 P1), Retry-After throttling, token caching, and
secret-free errors.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from ne.audit import PostgresAuditSink
from ne.permissions.config import PermissionsSettings
from ne.permissions.entra import EntraPrincipalSource
from ne.permissions.errors import PrincipalSyncError
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import PrincipalSnapshot
from ne.permissions.store import PrincipalStore
from ne.permissions.sync import PrincipalSyncService

GRAPH = Path(__file__).parent / "fixtures" / "graph"


def _load(name: str) -> dict[str, object]:
    return json.loads((GRAPH / f"{name}.json").read_text())


def _settings() -> PermissionsSettings:
    return PermissionsSettings(
        mode="entra",
        entra_tenant_id="00000000-0000-0000-0000-000000000000",
        entra_client_id="11111111-1111-1111-1111-111111111111",
        entra_client_secret=SecretStr("fake-sanitized-secret"),
    )


class GraphRecording:
    """Route Graph requests to committed fixtures and log the request order."""

    def __init__(self) -> None:
        self.get_urls: list[str] = []
        self.token_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and "/oauth2/v2.0/token" in url:
            self.token_calls += 1
            # The secret must be in the POST body, never the URL.
            assert "fake-sanitized-secret" not in url
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})

        self.get_urls.append(url)
        return httpx.Response(200, json=self._route(url))

    @staticmethod
    def _route(url: str) -> dict[str, object]:
        if "groups/delta" in url:
            return _load("groups_delta_latest" if "latest" in url else "groups_delta_page")
        if "users/delta" in url:
            return _load("users_delta_latest" if "latest" in url else "users_delta_page")
        if "/members" in url:
            gid = url.split("/groups/")[1].split("/members")[0]
            return _load(f"members_{gid}")
        if "/users" in url:
            return _load("users_page2" if "UPAGE2" in url else "users_page1")
        if "/groups" in url:
            return _load("groups_page2" if "GPAGE2" in url else "groups_page1")
        raise AssertionError(f"unrouted URL: {url}")


def _run(source: EntraPrincipalSource, state: dict[str, str]) -> PrincipalSnapshot:
    return asyncio.run(source.fetch(state))


def test_full_sync_paginates_and_stamps_direct_edges() -> None:
    rec = GraphRecording()
    src = EntraPrincipalSource(_settings(), transport=httpx.MockTransport(rec.handler))
    snap = _run(src, {})

    ids = {p.principal_id for p in snap.principals}
    assert {"u-ada", "u-bob", "g-eng", "g-sub"} <= ids
    assert snap.is_full is True

    edges = {(e.member_id, e.group_id) for e in snap.edges}
    # DIRECT edges only: the nested group appears as a group->group edge and the
    # device is skipped. The transitive closure (u-bob -> g-eng via g-sub) is NOT
    # flattened here — it is computed at query time by the expander (Codex P1).
    assert ("u-ada", "g-eng") in edges
    assert ("g-sub", "g-eng") in edges
    assert ("u-bob", "g-sub") in edges
    assert ("u-bob", "g-eng") not in edges  # transitive, not a direct edge
    assert not any(m == "dev-1" for m, _ in edges)

    # Principal ids stored EXACTLY as raw Graph object ids (no prefixing).
    assert "u-ada" in ids

    # Delta links captured into state.
    assert snap.state["entra:groups_delta_link"].endswith("GROUPTOKEN1")
    assert snap.state["entra:users_delta_link"].endswith("USERTOKEN1")


def test_delta_link_before_full_enumeration_ordering() -> None:
    rec = GraphRecording()
    src = EntraPrincipalSource(_settings(), transport=httpx.MockTransport(rec.handler))
    _run(src, {})

    def first_index(pred: Callable[[str], bool]) -> int:
        return next(i for i, u in enumerate(rec.get_urls) if pred(u))

    groups_delta = first_index(lambda u: "groups/delta" in u)
    users_delta = first_index(lambda u: "users/delta" in u)
    users_enum = first_index(lambda u: "/users" in u and "delta" not in u)
    groups_enum = first_index(lambda u: "/groups" in u and "delta" not in u)
    assert groups_delta < users_enum
    assert users_delta < users_enum
    assert groups_delta < groups_enum


def test_token_cached_once_per_fetch() -> None:
    rec = GraphRecording()
    src = EntraPrincipalSource(_settings(), transport=httpx.MockTransport(rec.handler))
    _run(src, {})
    assert rec.token_calls == 1


def test_delta_sync_applies_change_and_removal() -> None:
    rec = GraphRecording()
    src = EntraPrincipalSource(_settings(), transport=httpx.MockTransport(rec.handler))
    state = {
        "entra:groups_delta_link": "https://graph.microsoft.com/v1.0/groups/delta?$deltatoken=GROUPTOKEN1",
        "entra:users_delta_link": "https://graph.microsoft.com/v1.0/users/delta?$deltatoken=USERTOKEN1",
    }
    snap = _run(src, state)

    assert snap.is_full is False
    assert "g-eng" in snap.replaced_groups  # direct members re-applied on change
    assert ("u-ada", "g-eng") in {(e.member_id, e.group_id) for e in snap.edges}
    assert "g-old" in snap.removed_principal_ids  # @removed group tombstoned
    assert snap.state["entra:groups_delta_link"].endswith("GROUPTOKEN2")
    assert snap.state["entra:users_delta_link"].endswith("USERTOKEN2")


def test_retry_after_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("ne.permissions.entra.asyncio.sleep", record_sleep)
    state = {"fired": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/oauth2/" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if not state["fired"]:
            state["fired"] = True  # first Graph GET throttles once
            return httpx.Response(429, headers={"Retry-After": "1"}, json={})
        return httpx.Response(200, json={"value": []})

    src = EntraPrincipalSource(_settings(), transport=httpx.MockTransport(handler))
    snap = asyncio.run(src.fetch({}))  # completes despite the 429 (retried)
    assert state["fired"] is True
    assert sleeps == [1]  # Retry-After honored exactly once
    assert snap.is_full is True


def test_five_consecutive_429s_raise_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ne.permissions.entra.asyncio.sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/oauth2/" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(429, headers={"Retry-After": "0"}, json={})

    src = EntraPrincipalSource(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(PrincipalSyncError) as exc:
        asyncio.run(src.fetch({}))
    msg = str(exc.value)
    assert "fake-sanitized-secret" not in msg
    assert "tok" not in msg


async def _no_sleep(_seconds: float) -> None:
    return None


class _NestedGraph:
    """Full sync establishes u-bob ∈ g-sub ∈ g-eng; a later delta drops u-bob from
    g-sub. Graph's ``groups/delta`` reports ONLY g-sub (the group whose DIRECT
    membership changed), never the ancestor g-eng — so if edges were stored
    flattened, the stale ``u-bob -> g-eng`` edge would survive and keep granting
    access to g-eng chunks. This is the Codex NE-042 P1 scenario.
    """

    def __init__(self) -> None:
        self.gsub_members: list[dict[str, str]] = [
            {"@odata.type": "#microsoft.graph.user", "id": "u-bob", "displayName": "Bob"}
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if "groups/delta" in url:
            if "latest" in url:  # full-sync bootstrap: capture the delta link
                return httpx.Response(
                    200, json={"value": [], "@odata.deltaLink": _dl("groups", "GT1")}
                )
            # delta run: ONLY g-sub is reported as changed.
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "g-sub", "displayName": "Sub"}],
                    "@odata.deltaLink": _dl("groups", "GT2"),
                },
            )
        if "users/delta" in url:
            token = "UT1" if "latest" in url else "UT2"
            return httpx.Response(200, json={"value": [], "@odata.deltaLink": _dl("users", token)})
        if "/members" in url:
            gid = url.split("/groups/")[1].split("/members")[0]
            if gid == "g-eng":
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "@odata.type": "#microsoft.graph.user",
                                "id": "u-ada",
                                "displayName": "Ada",
                            },
                            {
                                "@odata.type": "#microsoft.graph.group",
                                "id": "g-sub",
                                "displayName": "Sub",
                            },
                        ]
                    },
                )
            return httpx.Response(200, json={"value": self.gsub_members})
        if "/users" in url:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "u-ada", "displayName": "Ada"},
                        {"id": "u-bob", "displayName": "Bob"},
                    ]
                },
            )
        if "/groups" in url:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "g-eng", "displayName": "Eng"},
                        {"id": "g-sub", "displayName": "Sub"},
                    ]
                },
            )
        raise AssertionError(f"unrouted URL: {url}")


def _dl(resource: str, token: str) -> str:
    return f"https://graph.microsoft.com/v1.0/{resource}/delta?$deltatoken={token}"


def test_delta_nested_membership_revocation_removes_ancestor_grant(
    store: PrincipalStore, audit_sink: PostgresAuditSink, frozen_config: str
) -> None:
    """Revoking a nested-group membership via delta drops the ancestor grant too.

    Because edges are stored DIRECT (not flattened) and the closure is computed at
    query time, the delta's per-group recompute of g-sub is sufficient — no stale
    ``u-bob -> g-eng`` edge survives, and no re-index is needed. Regression guard
    for the Codex NE-042 P1 over-granting bug.
    """
    graph = _NestedGraph()
    src = EntraPrincipalSource(_settings(), transport=httpx.MockTransport(graph.handler))
    service = PrincipalSyncService(store, audit_sink)
    expander = PrincipalExpander(store)

    # Full sync: u-bob is a transitive member of g-eng via g-sub.
    asyncio.run(service.run_once(src))
    assert {"g-sub", "g-eng"} <= expander.expand("u-bob")

    # Delta: u-bob dropped from g-sub; Graph reports ONLY g-sub as changed.
    graph.gsub_members = []
    asyncio.run(service.run_once(src))

    expanded = expander.expand("u-bob")
    assert "g-sub" not in expanded  # direct edge recomputed away
    assert "g-eng" not in expanded  # ancestor grant gone — NO stale flattened edge
    assert "u-bob" in expanded  # fail-closed: result is never empty

    # u-ada's independent direct membership in g-eng is untouched by the revocation.
    assert "g-eng" in expander.expand("u-ada")


# -- SSRF / token-exfil hardening (bulletproofing P2-5) ----------------------


def test_offhost_nextlink_refused_before_token_attached() -> None:
    """A response-supplied @odata.nextLink to an off-host URL is refused before the
    token is ever attached to a request bound for that host (SSRF / token exfil)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})
        if "/delta" in url:  # let the full sync capture delta links and proceed
            return httpx.Response(200, json={"value": [], "@odata.deltaLink": _dl("x", "T")})
        if "/users" in url:  # first users page hands back a hostile nextLink
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "u-1", "displayName": "U"}],
                    "@odata.nextLink": "https://evil.example.com/steal?page=2",
                },
            )
        return httpx.Response(200, json={"value": []})

    src = EntraPrincipalSource(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(PrincipalSyncError) as exc:
        asyncio.run(src.fetch({}))

    # The token was NEVER attached to a request to the hostile host: no recorded
    # request (GET or otherwise) ever targeted evil.example.com.
    assert not any("evil.example.com" in u for u in seen)
    msg = str(exc.value)
    assert "fake-sanitized-secret" not in msg  # no client secret
    assert "tok-123" not in msg  # no bearer token
    assert "steal?page=2" not in msg  # no path/query — hostname-only C12 discipline
    assert "evil.example.com" in msg  # the validator names the host (and only the host)


def test_offport_nextlink_refused_before_token_attached() -> None:
    """A response-supplied @odata.nextLink to the PINNED host on a DIFFERENT port is
    refused before the token is attached: the host allowlist alone would accept it,
    so the port pin is what stops the bearer token reaching an unexpected service on
    the same host (Codex P1)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})
        if "/delta" in url:
            return httpx.Response(200, json={"value": [], "@odata.deltaLink": _dl("x", "T")})
        if "/users" in url and ":8443" not in url:  # first users page → off-port nextLink
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "u-1", "displayName": "U"}],
                    "@odata.nextLink": "https://graph.microsoft.com:8443/v1.0/users?$skiptoken=P2",
                },
            )
        return httpx.Response(200, json={"value": []})

    src = EntraPrincipalSource(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(PrincipalSyncError) as exc:
        asyncio.run(src.fetch({}))

    # The token was NEVER attached to a request on the off-port endpoint.
    assert not any(":8443" in u for u in seen)
    msg = str(exc.value)
    assert "8443" in msg  # names the offending port
    assert "fake-sanitized-secret" not in msg  # no client secret
    assert "tok-123" not in msg  # no bearer token
    assert "skiptoken=P2" not in msg  # no path/query — C12 discipline


def test_cyclic_nextlink_terminates_at_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A same-host self-referencing @odata.nextLink chain terminates at the page cap
    (the cap, not the guard, breaks the cycle — the loop link is on the pinned host)."""
    monkeypatch.setattr("ne.permissions.entra._MAX_PAGES", 5)
    graph_gets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        graph_gets.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "value": [],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=LOOP",
            },
        )

    src = EntraPrincipalSource(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(PrincipalSyncError) as exc:
        asyncio.run(src.fetch({}))

    msg = str(exc.value)
    assert "pages" in msg
    assert "cycle" in msg
    # It did NOT run unbounded: at most ~cap graph GETs before the cap raises.
    assert len(graph_gets) <= 5 + 2


@pytest.mark.parametrize(
    "tampered",
    [
        "http://graph.microsoft.com/v1.0/groups/delta?$deltatoken=X",  # scheme downgrade
        "https://evil.example.com/groups/delta?$deltatoken=X",  # off-host
        "https://graph.microsoft.us/v1.0/groups/delta?$deltatoken=X",  # different cloud
        "https://u:p@graph.microsoft.com/v1.0/groups/delta?$deltatoken=X",  # userinfo
        "https://169.254.169.254/latest/meta-data",  # metadata IP
        "https://graph.microsoft.com:8443/v1.0/groups/delta?$deltatoken=X",  # off-port
    ],
)
def test_tampered_persisted_delta_link_rejected_on_read_before_any_io(tampered: str) -> None:
    """A tampered persisted delta cursor is rejected on read from sync-state, before
    ANY I/O (no token POST, no GET) — validate-on-read, not just validate-on-follow."""
    rec = GraphRecording()
    src = EntraPrincipalSource(_settings(), transport=httpx.MockTransport(rec.handler))
    state = {
        "entra:groups_delta_link": tampered,
        "entra:users_delta_link": "https://graph.microsoft.com/v1.0/users/delta?$deltatoken=UT1",
    }
    with pytest.raises(PrincipalSyncError):
        asyncio.run(src.fetch(state))

    assert rec.token_calls == 0
    assert rec.get_urls == []
