# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``MemoryGraphClient`` — the thin httpx seam to the out-of-process sidecar.

Validates ``service_url`` via the loopback guard AT CONSTRUCTION (fail before any
byte is sent — the ne-serve C2 pattern), attaches a bearer header when a token is
configured, and speaks the sidecar's small HTTP contract:

* ``POST /episodes``  — queue one episode for extraction (idempotent by name);
* ``POST /search``    — hybrid + temporal search over an agent's partition;
* ``GET  /healthz``   — driver connectivity probe.

All datetimes cross the wire as ISO-8601 UTC (the ``_as_utc`` discipline from
``memory_store.py``). ``GraphHit`` is the frozen projection of one fact edge.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, ConfigDict

from ne.memory_graph.config import MemoryGraphSettings, require_loopback_or_allowlisted

__all__ = ["GraphHit", "MemoryGraphClient"]


def _as_utc_iso(dt: datetime) -> str:
    """Serialize a datetime as ISO-8601 UTC (naive ⇒ assumed UTC)."""
    aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return aware.isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class GraphHit(BaseModel):
    """One fact edge returned by the sidecar search, projected on the wire."""

    model_config = ConfigDict(frozen=True)

    content: str
    valid_at: datetime | None
    created_at: datetime
    group_id: str
    event_ids: tuple[str, ...]
    edge_uuid: str


class MemoryGraphClient:
    """Async httpx client over the sidecar HTTP contract (loopback-guarded)."""

    def __init__(
        self,
        settings: MemoryGraphSettings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or MemoryGraphSettings()
        # Fail-closed at construction — an external/SSRF-shaped URL never gets a byte.
        self._base_url = require_loopback_or_allowlisted(
            self._settings.service_url, self._settings.allowed_service_hosts
        )
        self._transport = transport

    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self) -> dict[str, str]:
        token = self._settings.service_token
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _client(self, timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers(),
            timeout=timeout_s,
            transport=self._transport,
        )

    async def post_episode(
        self,
        *,
        group_id: str,
        name: str,
        content: str,
        kind: str,
        occurred_at: datetime,
        provenance: str,
    ) -> bool:
        """Queue one episode for ingestion. True on 2xx (incl. 200 already-ingested)."""
        body = {
            "group_id": group_id,
            "name": name,
            "content": content,
            "kind": kind,
            "occurred_at": _as_utc_iso(occurred_at),
            "provenance": provenance,
        }
        async with self._client(self._settings.ingest_timeout_s) as client:
            resp = await client.post("/episodes", json=body)
        return resp.is_success

    async def search(
        self,
        query: str,
        *,
        group_ids: list[str],
        since: datetime | None = None,
        until: datetime | None = None,
        as_of: datetime | None = None,
        limit: int = 20,
    ) -> list[GraphHit]:
        """Hybrid + temporal search; returns projected fact hits (empty on no match)."""
        body: dict[str, object] = {
            "query": query,
            "group_ids": group_ids,
            "limit": limit,
        }
        if since is not None:
            body["since"] = _as_utc_iso(since)
        if until is not None:
            body["until"] = _as_utc_iso(until)
        if as_of is not None:
            body["as_of"] = _as_utc_iso(as_of)
        async with self._client(self._settings.search_timeout_s) as client:
            resp = await client.post("/search", json=body)
        resp.raise_for_status()
        hits: list[GraphHit] = []
        for row in resp.json().get("hits", []):
            hits.append(
                GraphHit(
                    content=str(row["content"]),
                    valid_at=_parse_dt(row.get("valid_at")),
                    created_at=_parse_dt(row["created_at"]) or datetime.now(UTC),
                    group_id=str(row["group_id"]),
                    event_ids=tuple(str(e) for e in row.get("event_ids", ())),
                    edge_uuid=str(row["edge_uuid"]),
                )
            )
        return hits

    async def healthz(self) -> bool:
        """True iff the sidecar answers ``GET /healthz`` with a 2xx."""
        async with self._client(self._settings.search_timeout_s) as client:
            resp = await client.get("/healthz")
        return resp.is_success
