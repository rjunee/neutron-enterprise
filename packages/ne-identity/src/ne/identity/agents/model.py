# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Frozen value models for agent identity (NE-036).

None of these models carry secret MATERIAL — the token lives ONLY in the frozen
SecretStore (C1). An :class:`AgentCredential` carries the store PATH and VERSION
that address the secret, plus its scope/state, so callers resolve the material
lazily through :meth:`AgentCredentialService.resolve_secret` (which wraps it in a
``SecretStr``) rather than ever holding it on a model.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class CredentialState(StrEnum):
    """Lifecycle state of a per-agent credential."""

    ACTIVE = "active"
    GRACE = "grace"
    REVOKED = "revoked"
    EXPIRED = "expired"


class CredentialScope(BaseModel):
    """Least-privilege scope: the exact tools/source-systems an agent may act as.

    An EMPTY tuple authorizes NOTHING — least privilege is the default, so a
    credential with no explicit grants can call no tool.
    """

    model_config = ConfigDict(frozen=True)

    tools: tuple[str, ...] = ()
    source_systems: tuple[str, ...] = ()

    def allows_tool(self, name: str) -> bool:
        """Return True iff ``name`` is an exact member of the allowed tools."""
        return name in self.tools


class AgentRef(BaseModel):
    """A per-department agent identity key."""

    model_config = ConfigDict(frozen=True)

    department: str
    agent_name: str

    @field_validator("department", "agent_name")
    @classmethod
    def _non_empty_no_slash(cls, v: str) -> str:
        if not v:
            raise ValueError("agent identity parts must be non-empty")
        if "/" in v:
            raise ValueError("agent identity parts must not contain '/' (path separator)")
        return v

    @property
    def key(self) -> str:
        """The stable ``department/agent_name`` identity key."""
        return f"{self.department}/{self.agent_name}"


class AgentCredential(BaseModel):
    """Metadata for one issued per-agent credential — NO secret material.

    ``secret_path`` + ``secret_version`` address the token in the frozen
    SecretStore; the token itself is never a field here (C1).
    """

    model_config = ConfigDict(frozen=True)

    credential_id: str
    agent: AgentRef
    scope: CredentialScope
    secret_path: str
    secret_version: int
    issued_at: datetime
    state: CredentialState
    grace_until: datetime | None = None
