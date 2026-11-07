# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Qdrant per-collection snapshot backup/restore over loopback HTTP.

This is the ONLY module in ne-backup permitted to import an outbound-HTTP client
(the C2 egress guardian enforces the single-importer allowlist). The endpoint is
validated with :func:`ne.net.validate_in_vpc_endpoint` at ``__init__`` time —
mirroring ne-brain's C2 pattern — so a client can never POST to a non-in-VPC
host. Snapshot files are OPAQUE: we never extract them; integrity is proven by
SHA-256 + the NEB1 AEAD only.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from ne.backup.errors import NeBackupError
from ne.net import UnsafeEndpointError, validate_in_vpc_endpoint

_TIMEOUT = httpx.Timeout(30.0, read=600.0)


class Qdrant:
    def __init__(
        self, url: str = "http://127.0.0.1:6333", client: httpx.Client | None = None
    ) -> None:
        # C2: validate at construction — before any request can leave.
        self._base_url = validate_in_vpc_endpoint(url, error_cls=UnsafeEndpointError)
        self._client = client or httpx.Client(timeout=_TIMEOUT)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def list_collections(self) -> list[str]:
        resp = self._client.get(self._url("/collections"))
        resp.raise_for_status()
        collections = resp.json()["result"]["collections"]
        return sorted(c["name"] for c in collections)

    def collection_exists(self, name: str) -> bool:
        resp = self._client.get(self._url(f"/collections/{name}/exists"))
        resp.raise_for_status()
        return bool(resp.json()["result"]["exists"])

    def points_count(self, name: str) -> int:
        resp = self._client.get(self._url(f"/collections/{name}"))
        resp.raise_for_status()
        return int(resp.json()["result"]["points_count"])

    def create_snapshot(self, name: str) -> str:
        resp = self._client.post(self._url(f"/collections/{name}/snapshots?wait=true"))
        resp.raise_for_status()
        return str(resp.json()["result"]["name"])

    def download_snapshot(self, name: str, snapshot: str, dest: Path) -> None:
        url = self._url(f"/collections/{name}/snapshots/{snapshot}")
        with self._client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as out:
                for chunk in resp.iter_bytes(1024 * 1024):
                    out.write(chunk)

    def delete_snapshot(self, name: str, snapshot: str) -> None:
        resp = self._client.delete(self._url(f"/collections/{name}/snapshots/{snapshot}"))
        resp.raise_for_status()

    def upload_snapshot(self, name: str, src: Path) -> None:
        """Restore (create/replace) a collection from a snapshot file via the
        multipart UPLOAD endpoint (``priority=snapshot`` — the snapshot's data
        wins over any live collection)."""
        url = self._url(f"/collections/{name}/snapshots/upload?priority=snapshot")
        with src.open("rb") as handle:
            files = {"snapshot": (src.name, handle, "application/octet-stream")}
            resp = self._client.post(url, files=files)
        if resp.status_code >= 400:
            raise NeBackupError(
                f"qdrant snapshot upload for {name!r} failed ({resp.status_code}): {resp.text}"
            )
