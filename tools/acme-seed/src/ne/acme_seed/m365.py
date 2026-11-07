# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""M365 / SharePoint seeder (NE-102) — upload slice docs + grant per-item ACLs.

:class:`SeedGraphClient` SUBCLASSES the connector's
:class:`~ne.connect.m365.graph.GraphClient`, adding the two NEW write verbs the
seed needs (``put_bytes`` for a driveItem content upload, ``delete`` for the
unseed) IN THE TOOL — ``packages/ne-connect`` gains no write surface and its C2
egress guardian stays byte-identical. Each new verb is a structural MIRROR of the
inherited ``post_json``: it SSRF-re-guards the URL FIRST (a hostile URL never
triggers a token POST), then runs the inherited token flow, throttle, and the
same retry loop, raising a STATUS-ONLY :class:`GraphSyncError` (C12). Folder
create + per-item ``invite`` reuse the inherited ``post_json`` directly.

The ACL trap is the point: a ``named`` doc is granted ONLY to the persona user(s)
in its ``allowed_people`` — never a group, never P1 — so an exec cannot see it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ne.acme_seed.errors import SeedError
from ne.acme_seed.manifest import SeedManifest
from ne.acme_seed.personas import GROUPS, PERSONAS, PrincipalsMap
from ne.acme_seed.slice import SliceDoc, SliceSelection
from ne.connect.errors import GraphSyncError
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.graph import Client, GraphClient, Transport

__all__ = ["M365Seeder", "SeedGraphClient"]

#: Same transient statuses the parent client retries (mirror of graph.py).
_RETRYABLE = frozenset({429, 503, 504})
_FOLDER = "Acme"


class SeedGraphClient(GraphClient):
    """Graph client + the tool-local PUT/DELETE write verbs (guard-first mirror)."""

    async def put_bytes(
        self, client: Client, url: str, body: bytes, content_type: str
    ) -> dict[str, Any]:
        """PUT raw ``body`` to a driveItem ``:/content`` URL (idempotent replace)."""
        self._guard_graph_url(url)  # MUST stay first: a hostile URL never triggers a token POST.
        token = await self._get_token(client)
        max_attempts = self._settings.max_attempts
        cap = self._settings.retry_after_cap_seconds
        for attempt in range(1, max_attempts + 1):
            await self._throttle.acquire()
            resp = await client.put(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
                content=body,
            )
            status = resp.status_code
            if status in _RETRYABLE:
                if attempt == max_attempts:
                    raise GraphSyncError(
                        f"Graph returned HTTP {status} after {attempt} attempts", status=status
                    )
                await self._throttle.wait_retry(attempt, resp.headers.get("Retry-After"), cap)
                continue
            if status >= 400:
                raise GraphSyncError(f"Graph returned HTTP {status}", status=status)
            return resp.json()
        raise GraphSyncError("exhausted Graph retries")  # pragma: no cover

    async def delete(self, client: Client, url: str) -> None:
        """DELETE a driveItem/folder URL; 204 AND 404 are both success (idempotent)."""
        self._guard_graph_url(url)  # MUST stay first.
        token = await self._get_token(client)
        max_attempts = self._settings.max_attempts
        cap = self._settings.retry_after_cap_seconds
        for attempt in range(1, max_attempts + 1):
            await self._throttle.acquire()
            resp = await client.request("DELETE", url, headers={"Authorization": f"Bearer {token}"})
            status = resp.status_code
            if status in _RETRYABLE:
                if attempt == max_attempts:
                    raise GraphSyncError(
                        f"Graph returned HTTP {status} after {attempt} attempts", status=status
                    )
                await self._throttle.wait_retry(attempt, resp.headers.get("Retry-After"), cap)
                continue
            if status in (200, 204, 404):
                return
            if status >= 400:
                raise GraphSyncError(f"Graph returned HTTP {status}", status=status)
            return
        raise GraphSyncError("exhausted Graph retries")  # pragma: no cover


class M365Seeder:
    """Push the slice into SharePoint's default document library under ``/Acme/``."""

    def __init__(
        self,
        settings: M365ConnectorSettings,
        principals: PrincipalsMap,
        *,
        transport: Transport | None = None,
    ) -> None:
        # Fail CLOSED before any client construction: the ACL trap needs every
        # group + persona user present (a missing one silently weakens the trap).
        principals.require_m365()
        if not settings.site_ids:
            raise SeedError("M365 seeder requires a configured site (set NE_M365_SITE_IDS)")
        self._settings = settings
        self._principals = principals
        self._site_id = settings.site_ids[0]
        self._client = SeedGraphClient(settings, transport=transport)

    @property
    def _drive_root(self) -> str:
        return f"{self._client.graph_base}/sites/{self._site_id}/drive/root"

    def _grant_recipients(self, doc: SliceDoc) -> list[str]:
        """The live principal ids a doc is granted to, by visibility class."""
        p = self._principals
        if doc.visibility == "public":
            return [p.m365_group_id(g) for g in GROUPS]
        if doc.visibility == "department":
            return [p.m365_group_id(g) for g in doc.allowed_groups if g in GROUPS]
        if doc.visibility == "exec_only":
            return [p.m365_group_id("executive")]
        if doc.visibility == "named":
            # ONLY the persona user(s) in allowed_people — never a group, never P1.
            personas = [pid for pid in doc.allowed_people if pid in PERSONAS]
            return [p.m365_user_id(pid) for pid in personas]
        raise SeedError(f"unknown visibility {doc.visibility!r}")

    async def push(self, selection: SliceSelection) -> SeedManifest:
        manifest = SeedManifest(
            tenant="m365",
            seeded_at=datetime.now(UTC).isoformat(),
            corpus_version=selection.corpus_version,
            personas={p: self._principals.m365_user_id(p) for p in PERSONAS},
        )
        async with self._client.session() as http:
            # (1) ensure the /Acme/ folder (409 nameAlreadyExists == already there).
            try:
                await self._client.post_json(
                    http,
                    f"{self._drive_root}/children",
                    {
                        "name": _FOLDER,
                        "folder": {},
                        "@microsoft.graph.conflictBehavior": "fail",
                    },
                )
            except GraphSyncError as exc:
                if exc.status != 409:
                    raise

            # (2) upload each doc as a .md driveItem (PUT replace == idempotent).
            for doc in selection.docs:
                url = f"{self._drive_root}:/{_FOLDER}/{doc.doc_id}.md:/content"
                item = await self._client.put_bytes(
                    http, url, doc.content.encode("utf-8"), "text/markdown"
                )
                item_id = str(item["id"])
                manifest.docs[doc.doc_id] = item_id

                # (3) grant the per-item ACL keyed by visibility.
                recipients = self._grant_recipients(doc)
                manifest.grants[doc.doc_id] = list(recipients)
                if recipients:
                    await self._client.post_json(
                        http,
                        f"{self._client.graph_base}/sites/{self._site_id}/drive/items/{item_id}/invite",
                        {
                            "recipients": [{"objectId": rid} for rid in recipients],
                            "requireSignIn": True,
                            "sendInvitation": False,
                            "roles": ["read"],
                        },
                    )
        return manifest

    async def unseed(self, manifest_dir: Path | None = None) -> None:
        """DELETE the whole ``/Acme/`` folder (path-addressed); 404 tolerated."""
        url = f"{self._drive_root}:/{_FOLDER}:"
        async with self._client.session() as http:
            await self._client.delete(http, url)
        if manifest_dir is not None:
            SeedManifest.delete(manifest_dir, "m365")
