# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Secrets domain Protocol (SPEC section 4) — Vault KV v2 semantics.

Versioned secrets with soft-delete, undelete, and permanent destroy, exactly
mirroring HashiCorp Vault's KV-v2 engine so the production adapter is a thin
mapping.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from ne.contracts.frozen import StrMap


class SecretVersion(BaseModel):
    """A single version of a secret at a path (Vault KV v2)."""

    model_config = ConfigDict(frozen=True)

    path: str
    version: int
    data: StrMap
    created_at: datetime
    deleted: bool = False


class SecretStore(Protocol):
    """Vault-KV-v2-semantics secret store.

    - ``read`` returns the latest version when ``version`` is None; raises
      :class:`~ne.contracts.errors.SecretNotFoundError` on a missing, soft-deleted,
      or destroyed version.
    - ``write`` auto-increments the version, starting at 1.
    - ``delete`` performs a SOFT delete (metadata retained, data hidden);
      ``versions=None`` deletes the latest version.
    - ``undelete`` reverses a soft delete; it cannot restore a destroyed version.
    - ``destroy`` permanently removes version data; a subsequent ``undelete``
      raises :class:`~ne.contracts.errors.SecretNotFoundError`.
    """

    async def read(self, path: str, *, version: int | None = None) -> SecretVersion: ...

    async def write(self, path: str, data: Mapping[str, str]) -> SecretVersion: ...

    async def delete(self, path: str, *, versions: Sequence[int] | None = None) -> None: ...

    async def undelete(self, path: str, versions: Sequence[int]) -> None: ...

    async def destroy(self, path: str, versions: Sequence[int]) -> None: ...

    async def list_secrets(self, prefix: str) -> Sequence[str]: ...
