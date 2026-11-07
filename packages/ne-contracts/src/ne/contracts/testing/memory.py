# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""In-memory reference adapters that prove the registry + conformance-kit pattern.

These are dummy, dependency-free implementations used as REAL entry-point
plugins (``ne.plugins.object_store``/``secret_store`` = ``memory``). They import
no test framework and no third-party runtime deps beyond pydantic (via the
contract value models). Checkpoint A resolves them through the plugin registry
and runs them against the conformance kits.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ne.contracts.errors import (
    ObjectNotFoundError,
    SecretNotFoundError,
    WormViolationError,
)
from ne.contracts.secrets import SecretVersion
from ne.contracts.storage import ArchiveReceipt, ObjectStat


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _StoredObject:
    data: bytes
    content_type: str
    metadata: dict[str, str]
    worm_retain_until: datetime | None = None


class MemoryObjectStore:
    """In-memory :class:`~ne.contracts.storage.ObjectStore` with WORM enforcement.

    The clock is injectable so WORM-expiry can be tested deterministically:
    ``MemoryObjectStore(now=lambda: fixed_datetime)``.
    """

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now
        self._objects: dict[str, _StoredObject] = {}

    def _worm_locked(self, key: str) -> bool:
        obj = self._objects.get(key)
        if obj is None or obj.worm_retain_until is None:
            return False
        return obj.worm_retain_until > self._now()

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectStat:
        if self._worm_locked(key):
            raise WormViolationError(
                f"key '{key}' is under WORM retention until {self._objects[key].worm_retain_until}"
            )
        self._objects[key] = _StoredObject(
            data=data,
            content_type=content_type,
            metadata=dict(metadata or {}),
        )
        return await self.stat(key)

    async def get(self, key: str) -> bytes:
        obj = self._objects.get(key)
        if obj is None:
            raise ObjectNotFoundError(f"no object at key '{key}'")
        return obj.data

    async def stat(self, key: str) -> ObjectStat:
        obj = self._objects.get(key)
        if obj is None:
            raise ObjectNotFoundError(f"no object at key '{key}'")
        return ObjectStat(
            key=key,
            size=len(obj.data),
            content_type=obj.content_type,
            metadata=dict(obj.metadata),
            worm_retain_until=obj.worm_retain_until,
        )

    async def delete(self, key: str) -> None:
        if self._worm_locked(key):
            raise WormViolationError(
                f"key '{key}' is under WORM retention until {self._objects[key].worm_retain_until}"
            )
        self._objects.pop(key, None)

    async def list_objects(self, prefix: str = "") -> Sequence[ObjectStat]:
        return [await self.stat(key) for key in sorted(self._objects) if key.startswith(prefix)]

    async def archive(self, key: str, *, retain_until: datetime) -> ArchiveReceipt:
        obj = self._objects.get(key)
        if obj is None:
            raise ObjectNotFoundError(f"no object at key '{key}'")
        existing = obj.worm_retain_until
        if existing is not None and existing > self._now() and retain_until < existing:
            raise WormViolationError(
                f"key '{key}' is under WORM retention until {existing}; retention may only "
                f"be extended, not shortened to {retain_until}"
            )
        obj.worm_retain_until = retain_until
        return ArchiveReceipt(key=key, retain_until=retain_until, archived_at=self._now())


@dataclass
class _SecretEntry:
    version: int
    data: dict[str, str]
    created_at: datetime
    deleted: bool = False
    destroyed: bool = False


@dataclass
class _SecretPath:
    versions: list[_SecretEntry] = field(default_factory=list)


class MemorySecretStore:
    """In-memory :class:`~ne.contracts.secrets.SecretStore` with Vault KV v2 semantics."""

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now
        self._paths: dict[str, _SecretPath] = {}

    def _entry(self, path: str, version: int | None) -> _SecretEntry:
        sp = self._paths.get(path)
        if sp is None or not sp.versions:
            raise SecretNotFoundError(f"no secret at path '{path}'")
        if version is None:
            entry = sp.versions[-1]
        else:
            match = [e for e in sp.versions if e.version == version]
            if not match:
                raise SecretNotFoundError(f"no version {version} at path '{path}'")
            entry = match[0]
        if entry.deleted or entry.destroyed:
            state = "destroyed" if entry.destroyed else "deleted"
            raise SecretNotFoundError(f"version {entry.version} at path '{path}' is {state}")
        return entry

    async def read(self, path: str, *, version: int | None = None) -> SecretVersion:
        entry = self._entry(path, version)
        return SecretVersion(
            path=path,
            version=entry.version,
            data=dict(entry.data),
            created_at=entry.created_at,
            deleted=entry.deleted,
        )

    async def write(self, path: str, data: Mapping[str, str]) -> SecretVersion:
        sp = self._paths.setdefault(path, _SecretPath())
        next_version = (sp.versions[-1].version + 1) if sp.versions else 1
        entry = _SecretEntry(
            version=next_version,
            data=dict(data),
            created_at=self._now(),
        )
        sp.versions.append(entry)
        return SecretVersion(
            path=path,
            version=entry.version,
            data=dict(entry.data),
            created_at=entry.created_at,
            deleted=False,
        )

    def _targets(self, path: str, versions: Sequence[int] | None) -> list[_SecretEntry]:
        sp = self._paths.get(path)
        if sp is None or not sp.versions:
            raise SecretNotFoundError(f"no secret at path '{path}'")
        if versions is None:
            return [sp.versions[-1]]
        wanted = set(versions)
        matched = [e for e in sp.versions if e.version in wanted]
        missing = wanted - {e.version for e in matched}
        if missing:
            raise SecretNotFoundError(f"no versions {sorted(missing)} at path '{path}'")
        return matched

    async def delete(self, path: str, *, versions: Sequence[int] | None = None) -> None:
        for entry in self._targets(path, versions):
            if not entry.destroyed:
                entry.deleted = True

    async def undelete(self, path: str, versions: Sequence[int]) -> None:
        for entry in self._targets(path, versions):
            if entry.destroyed:
                raise SecretNotFoundError(f"version {entry.version} at path '{path}' is destroyed")
            entry.deleted = False

    async def destroy(self, path: str, versions: Sequence[int]) -> None:
        for entry in self._targets(path, versions):
            entry.destroyed = True
            entry.data = {}

    async def list_secrets(self, prefix: str) -> Sequence[str]:
        return sorted(p for p in self._paths if p.startswith(prefix))
