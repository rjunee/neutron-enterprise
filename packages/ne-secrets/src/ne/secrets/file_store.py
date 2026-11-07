# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FileSecretStore — the pilot-default :class:`~ne.contracts.secrets.SecretStore`.

A versioned KV-v2 JSON document store behind a codec seam
(:class:`~ne.secrets.codecs.PlainJsonCodec` for dev/CI,
:class:`~ne.secrets.codecs.SopsCodec` for the customer-KMS envelope). Semantics
mirror :class:`ne.contracts.testing.memory.MemorySecretStore` exactly — that
class is the reference this adapter is built against.

On-disk document schema::

    {
        "format": "ne-secrets/kv2",
        "format_version": 1,
        "secrets": {
            "<path>": [
                {
                    "version": int,
                    "created_at": "<UTC ISO-8601>",
                    "deleted": bool,
                    "destroyed": bool,
                    "data": {str: str},   # ABSENT once destroyed
                },
                ...
            ]
        }
    }

HARD RULE: ``destroy()`` sets ``destroyed=true`` AND physically removes the
``"data"`` key from the entry before persisting — a destroyed secret must not
exist on disk in any form.

This adapter is single-process: coordination is a single ``asyncio.Lock``,
which is sufficient for the pilot profile but not for concurrent multi-process
access (that is the OpenBao prod profile's job).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ne.contracts.errors import SecretNotFoundError
from ne.contracts.secrets import SecretVersion
from ne.secrets.codecs import PlainJsonCodec, SecretFileCodec, SopsCodec
from ne.secrets.config import SecretsSettings


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FileSecretStore:
    """Versioned JSON-document :class:`~ne.contracts.secrets.SecretStore`."""

    def __init__(self, path: Path | str, codec: SecretFileCodec | None = None) -> None:
        self._path = Path(path)
        self._codec = codec or PlainJsonCodec()
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: SecretsSettings) -> FileSecretStore:
        cfg = settings.file
        codec: SecretFileCodec
        if cfg.mode == "plain":
            codec = PlainJsonCodec()
        else:
            codec = SopsCodec(
                binary=cfg.sops_binary,
                config=cfg.sops_config,
                timeout=cfg.sops_timeout_seconds,
            )
        return cls(cfg.path, codec=codec)

    async def _load(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._codec.load, self._path)

    async def _dump(self, doc: dict[str, Any]) -> None:
        await asyncio.to_thread(self._codec.dump, doc, self._path)

    def _entry(self, doc: dict[str, Any], path: str, version: int | None) -> dict[str, Any]:
        versions: list[dict[str, Any]] = doc["secrets"].get(path, [])
        if not versions:
            raise SecretNotFoundError(f"no secret at path '{path}'")
        if version is None:
            entry = versions[-1]
        else:
            matches = [e for e in versions if e["version"] == version]
            if not matches:
                raise SecretNotFoundError(f"no version {version} at path '{path}'")
            entry = matches[0]
        if entry["deleted"] or entry["destroyed"]:
            state = "destroyed" if entry["destroyed"] else "deleted"
            raise SecretNotFoundError(f"version {entry['version']} at path '{path}' is {state}")
        return entry

    def _targets(
        self, doc: dict[str, Any], path: str, versions: Sequence[int] | None
    ) -> list[dict[str, Any]]:
        all_versions: list[dict[str, Any]] = doc["secrets"].get(path, [])
        if not all_versions:
            raise SecretNotFoundError(f"no secret at path '{path}'")
        if versions is None:
            return [all_versions[-1]]
        wanted = set(versions)
        matched = [e for e in all_versions if e["version"] in wanted]
        missing = wanted - {e["version"] for e in matched}
        if missing:
            raise SecretNotFoundError(f"no versions {sorted(missing)} at path '{path}'")
        return matched

    async def read(self, path: str, *, version: int | None = None) -> SecretVersion:
        async with self._lock:
            doc = await self._load()
            entry = self._entry(doc, path, version)
            return SecretVersion(
                path=path,
                version=entry["version"],
                data=dict(entry["data"]),
                created_at=entry["created_at"],
                deleted=entry["deleted"],
            )

    async def write(self, path: str, data: Mapping[str, str]) -> SecretVersion:
        async with self._lock:
            doc = await self._load()
            versions: list[dict[str, Any]] = doc["secrets"].setdefault(path, [])
            next_version = (versions[-1]["version"] + 1) if versions else 1
            created_at = _utcnow()
            entry = {
                "version": next_version,
                "created_at": created_at.isoformat(),
                "deleted": False,
                "destroyed": False,
                "data": dict(data),
            }
            versions.append(entry)
            await self._dump(doc)
            return SecretVersion(
                path=path,
                version=next_version,
                data=dict(data),
                created_at=created_at,
                deleted=False,
            )

    async def delete(self, path: str, *, versions: Sequence[int] | None = None) -> None:
        async with self._lock:
            doc = await self._load()
            for entry in self._targets(doc, path, versions):
                if not entry["destroyed"]:
                    entry["deleted"] = True
            await self._dump(doc)

    async def undelete(self, path: str, versions: Sequence[int]) -> None:
        async with self._lock:
            doc = await self._load()
            for entry in self._targets(doc, path, versions):
                if entry["destroyed"]:
                    raise SecretNotFoundError(
                        f"version {entry['version']} at path '{path}' is destroyed"
                    )
                entry["deleted"] = False
            await self._dump(doc)

    async def destroy(self, path: str, versions: Sequence[int]) -> None:
        async with self._lock:
            doc = await self._load()
            for entry in self._targets(doc, path, versions):
                entry["destroyed"] = True
                entry.pop("data", None)
            await self._dump(doc)

    async def list_secrets(self, prefix: str) -> Sequence[str]:
        async with self._lock:
            doc = await self._load()
            return sorted(p for p in doc["secrets"] if p.startswith(prefix))
