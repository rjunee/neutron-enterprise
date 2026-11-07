# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Storage domain Protocol (SPEC section 4) — S3-compatible object store.

Put/get/stat/delete/list plus WORM archive. After ``archive(key,
retain_until=...)``, any ``put`` or ``delete`` against that key raises
:class:`~ne.contracts.errors.WormViolationError` until ``retain_until`` has
passed — write-once-read-many compliance storage.

``archive()`` may only *extend* an active retention: calling it with a
``retain_until`` earlier than the current unexpired value raises
:class:`~ne.contracts.errors.WormViolationError` (equal = idempotent, later =
extend); after expiry the key may be re-archived freely.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from ne.contracts.frozen import FrozenDict, StrMap


class ObjectStat(BaseModel):
    """Metadata about a stored object."""

    model_config = ConfigDict(frozen=True)

    key: str
    size: int
    content_type: str
    metadata: StrMap = FrozenDict()
    worm_retain_until: datetime | None = None


class ArchiveReceipt(BaseModel):
    """Receipt confirming a key is now under WORM retention."""

    model_config = ConfigDict(frozen=True)

    key: str
    retain_until: datetime
    archived_at: datetime


class ObjectStore(Protocol):
    """S3-compatible object store with WORM archive semantics.

    ``archive()`` may only extend an active retention: calling it with a
    ``retain_until`` earlier than the current unexpired value raises
    :class:`~ne.contracts.errors.WormViolationError` (equal = idempotent, later =
    extend); after expiry the key may be re-archived freely.
    """

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectStat: ...

    async def get(self, key: str) -> bytes: ...

    async def stat(self, key: str) -> ObjectStat: ...

    async def delete(self, key: str) -> None: ...

    async def list_objects(self, prefix: str = "") -> Sequence[ObjectStat]: ...

    async def archive(self, key: str, *, retain_until: datetime) -> ArchiveReceipt: ...
