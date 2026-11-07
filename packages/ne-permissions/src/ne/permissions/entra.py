# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""EntraPrincipalSource — Microsoft Graph app-only principal sync (thin httpx).

Syncs users, groups, and transitive group memberships from Entra ID via the Graph
REST v1.0 API using a client-credentials (app-only) token. Deliberately a THIN
``httpx`` client, NOT ``msgraph-sdk`` (orchestrator build-vs-adopt directive — no
new C4 dependency surface).

Algorithm lifted from PipesHub (``pipeshub-ai/pipeshub-ai`` v0.5.0, Apache-2.0 —
see NOTICE): the delta-link-BEFORE-full-enumeration ordering so directory changes
that occur DURING a full crawl are captured by the next delta run, plus the
``groups/delta`` + per-group member enumeration. Only the algorithm/vocabulary is
lifted; PipesHub's Arango/Mongo/Redis-coupled code is not.

Membership edges are stamped as DIRECT ``member -> group`` relations (via
``groups/{id}/members``), NOT the flattened transitive closure: the store keeps
direct edges and :class:`~ne.permissions.expand.PrincipalExpander` computes the
transitive closure at query time (BFS). This is a CORRECTNESS requirement for
delta sync, not merely an optimisation — Graph's ``groups/delta`` reports the
single group whose DIRECT membership changed, never every ancestor whose
*transitive* member list shifted. If we stored flattened transitive edges, a
change deep in a nested group (e.g. a user dropped from ``g-sub`` where
``g-sub`` ∈ ``g-eng``) would leave the stale ``user -> g-eng`` edge behind until
the next full sync — over-granting access to the ancestor's chunks past the
revocation-lag SLA. Storing direct edges makes each delta's per-group recompute
sufficient (Codex NE-042 P1).

Fail-closed sovereignty (C2): the config-supplied Graph and login base URLs are
validated at CONSTRUCTION (https-only, allowlisted hosts, no creds/IP/packed-IPv4
/metadata) BEFORE any I/O; under ``AIR_GAP`` construction raises
``AirGapViolationError`` (Entra sync is egress — oidc-claims/fixture modes remain
usable). The client secret only ever appears in the token POST body — never in a
log line, an exception, or a URL (C12).

Per-request re-guard (C2): construction-time validation only covers the two base
URLs. Every URL actually fetched — a response-supplied ``@odata.nextLink`` /
``deltaLink`` OR a persisted delta cursor read back from ``ne_permissions_sync_
state`` — is re-validated against the SINGLE configured Graph host by
:meth:`_guard_graph_url` as the first statement of ``_get_json`` (before any token
fetch or attach), so a tampered Graph response or sync-state row can neither
exfiltrate the app-only token to an off-host URL nor probe an in-VPC/metadata
endpoint. Persisted cursors are additionally re-guarded on read in ``fetch`` so a
poisoned row is rejected before ANY I/O. ``_get_paginated`` caps at ``_MAX_PAGES``
and RAISES (never truncates — a truncated full sync would mass-tombstone) so a
cyclic ``@odata.nextLink`` cannot pin an infinite loop.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from ne.permissions.config import PermissionsSettings
from ne.permissions.errors import PrincipalSyncError
from ne.permissions.models import (
    MembershipEdge,
    PrincipalKind,
    PrincipalSnapshot,
    SyncedPrincipal,
)
from ne.permissions.sovereignty import (
    AirGapViolationError,
    UnsafeEndpointError,
    air_gap_engaged,
    validate_external_https_url,
)

__all__ = ["EntraPrincipalSource"]

_MAX_ATTEMPTS = 5
_RETRY_AFTER_CAP = 60
_RETRYABLE = frozenset({429, 503})

# Cycle-breaker for hostile/broken @odata.nextLink chains: at Graph's ~100-item
# default page size this allows ~1M objects per collection — generous for real
# tenants, fatal to a cycle. _get_paginated RAISES (never truncates) at the cap.
_MAX_PAGES = 10_000

_GROUPS_DELTA_KEY = "entra:groups_delta_link"
_USERS_DELTA_KEY = "entra:users_delta_link"

_USER_TYPE = "#microsoft.graph.user"
_GROUP_TYPE = "#microsoft.graph.group"


class EntraPrincipalSource:
    """Sync principals + transitive memberships from Entra ID via Microsoft Graph."""

    def __init__(
        self,
        settings: PermissionsSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Fail-closed BEFORE any I/O.
        if air_gap_engaged():
            raise AirGapViolationError(
                "AIR_GAP engaged: Entra ID sync is egress and is not permitted "
                "(use oidc-claims or fixture mode under AIR_GAP)"
            )
        self._graph = validate_external_https_url(
            settings.graph_base_url, allowed_hosts=settings.graph_allowed_hosts
        )
        self._login = validate_external_https_url(
            settings.login_base_url, allowed_hosts=settings.login_allowed_hosts
        )
        self._tenant_id = settings.entra_tenant_id
        self._client_id = settings.entra_client_id
        self._client_secret = settings.entra_client_secret
        self._transport = transport

        # Pin every FOLLOWED Graph link to the configured cloud's SINGLE host AND
        # port: the multi-cloud graph_allowed_hosts allowlist is construction-time-
        # only, so a link to a DIFFERENT valid sovereign cloud is refused by
        # _guard_graph_url; the port pin additionally refuses a same-host link to a
        # different port (e.g. graph.microsoft.com:8443), which the host allowlist
        # alone would accept — that would let a tampered cursor/nextLink probe an
        # unexpected service on the pinned host and receive the bearer token (Codex
        # P1). None means the https default (443).
        _graph_parsed = urlparse(self._graph)
        self._graph_host = _graph_parsed.hostname or ""
        self._graph_port = _graph_parsed.port if _graph_parsed.port is not None else 443
        self._scope = f"https://{self._graph_host}/.default"
        self._token_url = f"{self._login}/{self._tenant_id}/oauth2/v2.0/token"

        self._token: str | None = None
        self._token_expires_at = 0.0

    @property
    def name(self) -> str:
        return "entra"

    async def fetch(self, state: Mapping[str, str]) -> PrincipalSnapshot:
        """Full sync when no delta links are stored; otherwise a delta sync."""
        groups_link = state.get(_GROUPS_DELTA_KEY)
        users_link = state.get(_USERS_DELTA_KEY)
        if groups_link and users_link:
            # Validate-on-read from sync-state: a tampered ne_permissions_sync_state
            # row is rejected BEFORE any I/O (including the token POST), not just
            # when it re-enters _get_json. This is the explicit validate-on-read the
            # SSRF finding requires — defense-in-depth with the _get_json re-guard.
            self._guard_graph_url(groups_link)
            self._guard_graph_url(users_link)
        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            if groups_link and users_link:
                return await self._delta_sync(client, groups_link, users_link)
            return await self._full_sync(client)

    def _guard_graph_url(self, url: str) -> None:
        """Full-policy SSRF re-guard for EVERY Graph URL we are about to fetch.

        Runs on every response-supplied ``@odata.nextLink`` / ``deltaLink`` and on
        every persisted delta cursor (which re-enter via :meth:`_get_json`). A delta
        token legitimately lives in the query string, so only the reconstructed
        ``scheme://netloc`` (netloc retains userinfo + port) goes through the FULL
        external policy — https-only, no userinfo, malformed port, no IP-literal /
        packed-IPv4 / cloud-metadata host, exact host allowlist. The host is pinned
        to the SINGLE configured ``graph_base_url`` host: the multi-cloud
        ``graph_allowed_hosts`` list is construction-time-only, so a link to a
        DIFFERENT valid Microsoft sovereign cloud is refused here before any
        token-bearing request. The effective port is ALSO pinned to the configured
        ``graph_base_url`` port (normally 443): the host allowlist alone accepts any
        syntactically valid port, so without this a tampered cursor/nextLink of the
        form ``https://graph.microsoft.com:8443/...`` would pass the host check and
        send the bearer token to an unexpected service on the pinned host (Codex P1).
        The error text interpolates only the validator's hostname-only message and
        the port numbers — NEVER the full URL / token / secret (C12).
        """
        parsed = urlparse(url)
        try:
            validate_external_https_url(
                f"{parsed.scheme}://{parsed.netloc}", allowed_hosts=(self._graph_host,)
            )
        except UnsafeEndpointError as exc:
            # validate_external_https_url names only the host (C12-safe) — NEVER the url.
            raise PrincipalSyncError(
                self.name, f"refusing to follow unsafe Graph link: {exc}"
            ) from exc
        # Port pin: the validator accepted the host but does not check the port; a
        # link to the pinned host on a DIFFERENT port must not receive the token.
        # `parsed.port` is safe here — the validator above already rejected a
        # malformed/out-of-range port on this same netloc. Names only the port
        # numbers (not a secret, never the URL) — C12-safe.
        link_port = parsed.port if parsed.port is not None else 443
        if link_port != self._graph_port:
            raise PrincipalSyncError(
                self.name,
                f"refusing to follow Graph link on port {link_port} "
                f"outside the configured Graph port {self._graph_port}",
            )

    # -- full sync -----------------------------------------------------------
    async def _full_sync(self, client: httpx.AsyncClient) -> PrincipalSnapshot:
        # (1) Capture delta links FIRST (PipesHub ordering) so changes during the
        # crawl are picked up by the next delta run rather than being lost.
        _, groups_delta_link = await self._get_paginated(
            client, f"{self._graph}/groups/delta?$deltaToken=latest"
        )
        _, users_delta_link = await self._get_paginated(
            client, f"{self._graph}/users/delta?$deltaToken=latest"
        )

        principals: list[SyncedPrincipal] = []

        # (2) Users.
        user_items, _ = await self._get_paginated(
            client, f"{self._graph}/users?$select=id,displayName,userPrincipalName"
        )
        for item in user_items:
            principals.append(
                SyncedPrincipal(
                    principal_id=item["id"],
                    kind=PrincipalKind.USER,
                    display_name=item.get("displayName") or "",
                )
            )

        # (3) Groups.
        group_items, _ = await self._get_paginated(
            client, f"{self._graph}/groups?$select=id,displayName"
        )
        for item in group_items:
            principals.append(
                SyncedPrincipal(
                    principal_id=item["id"],
                    kind=PrincipalKind.GROUP,
                    display_name=item.get("displayName") or "",
                )
            )

        # (4) DIRECT members per group → edges (transitive closure is query-time).
        edges: list[MembershipEdge] = []
        replaced_groups: list[str] = []
        for group in group_items:
            gid = group["id"]
            replaced_groups.append(gid)
            edges.extend(await self._direct_edges(client, gid))

        new_state: dict[str, str] = {}
        if groups_delta_link:
            new_state[_GROUPS_DELTA_KEY] = groups_delta_link
        if users_delta_link:
            new_state[_USERS_DELTA_KEY] = users_delta_link

        return PrincipalSnapshot(
            principals=tuple(principals),
            edges=tuple(edges),
            replaced_groups=tuple(replaced_groups),
            is_full=True,
            state=new_state,
        )

    # -- delta sync ----------------------------------------------------------
    async def _delta_sync(
        self, client: httpx.AsyncClient, groups_link: str, users_link: str
    ) -> PrincipalSnapshot:
        principals: list[SyncedPrincipal] = []
        edges: list[MembershipEdge] = []
        replaced_groups: list[str] = []
        removed: list[str] = []

        # Changed users.
        user_items, new_users_link = await self._get_paginated(client, users_link)
        for item in user_items:
            if "@removed" in item:
                removed.append(item["id"])
            else:
                principals.append(
                    SyncedPrincipal(
                        principal_id=item["id"],
                        kind=PrincipalKind.USER,
                        display_name=item.get("displayName") or "",
                    )
                )

        # Changed groups: re-fetch DIRECT members for each changed group. Graph
        # reports the single group whose direct membership changed, so recomputing
        # that group's direct edges is sufficient — ancestors keep their (still
        # correct) direct edges and the transitive closure is recomputed at query
        # time, so no stale ancestor edge can survive (Codex NE-042 P1).
        group_items, new_groups_link = await self._get_paginated(client, groups_link)
        for item in group_items:
            gid = item["id"]
            if "@removed" in item:
                removed.append(gid)
                continue
            principals.append(
                SyncedPrincipal(
                    principal_id=gid,
                    kind=PrincipalKind.GROUP,
                    display_name=item.get("displayName") or "",
                )
            )
            replaced_groups.append(gid)
            edges.extend(await self._direct_edges(client, gid))

        new_state: dict[str, str] = {
            _GROUPS_DELTA_KEY: new_groups_link or groups_link,
            _USERS_DELTA_KEY: new_users_link or users_link,
        }

        return PrincipalSnapshot(
            principals=tuple(principals),
            edges=tuple(edges),
            replaced_groups=tuple(replaced_groups),
            removed_principal_ids=tuple(removed),
            is_full=False,
            state=new_state,
        )

    async def _direct_edges(self, client: httpx.AsyncClient, group_id: str) -> list[MembershipEdge]:
        """DIRECT members of ``group_id`` → edges (transitive closure is query-time).

        Uses ``/groups/{id}/members`` (direct membership) rather than
        ``/transitiveMembers``: nested group members appear as ``group -> group``
        edges and the expander walks them at query time. See the module docstring
        for why the flattened representation is delta-incorrect (Codex NE-042 P1).
        """
        items, _ = await self._get_paginated(
            client, f"{self._graph}/groups/{group_id}/members?$select=id,displayName"
        )
        edges: list[MembershipEdge] = []
        for member in items:
            member_type = member.get("@odata.type")
            if member_type in (_USER_TYPE, _GROUP_TYPE):
                edges.append(MembershipEdge(member_id=member["id"], group_id=group_id))
            # Unknown @odata.type (device / servicePrincipal / orgContact) is
            # skipped — not an error (it just cannot be an ACL principal here).
        return edges

    # -- HTTP plumbing -------------------------------------------------------
    async def _get_paginated(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Follow ``@odata.nextLink`` pages; return ``(items, deltaLink | None)``."""
        items: list[dict[str, Any]] = []
        delta_link: str | None = None
        next_url: str | None = url
        pages = 0
        while next_url:
            pages += 1
            if pages > _MAX_PAGES:
                # MUST raise, never break/truncate: a silently-truncated FULL sync
                # would feed sync.py an incomplete `present` set and mass-tombstone
                # live principals — turning an SSRF nuisance into an availability
                # incident. A finite generous cap merely breaks a hostile cycle.
                raise PrincipalSyncError(
                    self.name,
                    f"pagination exceeded {_MAX_PAGES} pages; possible @odata.nextLink cycle",
                )
            data = await self._get_json(client, next_url)
            items.extend(data.get("value", []))
            delta = data.get("@odata.deltaLink")
            if delta:
                delta_link = delta
            next_url = data.get("@odata.nextLink")
        return items, delta_link

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> dict[str, Any]:
        # MUST stay first: a hostile link never triggers a token fetch or carries the
        # token. Covers initial URLs (self._graph-derived, always pass), every
        # response-supplied nextLink/deltaLink, and persisted cursors (re-entrant).
        self._guard_graph_url(url)
        token = await self._get_token(client)
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code in _RETRYABLE:
                if attempt == _MAX_ATTEMPTS:
                    raise PrincipalSyncError(
                        self.name,
                        f"Graph returned HTTP {resp.status_code} after {attempt} attempts",
                    )
                await asyncio.sleep(self._retry_after(resp))
                continue
            if resp.status_code >= 400:
                raise PrincipalSyncError(self.name, f"Graph returned HTTP {resp.status_code}")
            return resp.json()
        raise PrincipalSyncError(self.name, "exhausted retries")  # pragma: no cover

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        now = time.monotonic()
        if self._token is not None and now < self._token_expires_at:
            return self._token
        resp = await client.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret.get_secret_value(),
                "scope": self._scope,
            },
        )
        if resp.status_code >= 400:
            # No body/secret echoed — only the status code (C12).
            raise PrincipalSyncError(self.name, f"token endpoint returned HTTP {resp.status_code}")
        payload = resp.json()
        token = str(payload["access_token"])
        self._token = token
        self._token_expires_at = now + max(int(payload.get("expires_in", 3600)) - 60, 0)
        return token

    @staticmethod
    def _retry_after(resp: httpx.Response) -> int:
        raw = resp.headers.get("Retry-After")
        try:
            secs = int(raw) if raw is not None else 1
        except ValueError:
            secs = 1
        return max(0, min(secs, _RETRY_AFTER_CAP))
