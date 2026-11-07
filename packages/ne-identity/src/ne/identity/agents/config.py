# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Settings for agent identity (NE-036) — the per-agent credential lifecycle.

``AgentCredentialConfig`` carries the SecretStore path prefix, the generated
token size, the rotation cadence + grace overlap, and the Hermes credential_pool
tuning (selection strategy + per-failure-class cooldowns + consecutive-failure
park). Every numeric field is validated positive at construction (fail-loud), so
a mis-typed ``ne.toml`` can never silently disable a control (C12 posture).

``AgentIdentitySettings`` is a ``NeSettings`` subclass constructible with no args
(so tests can freeze a config_hash from ``AgentIdentitySettings()``); it is a
SEPARATE settings surface from the frozen NE-060 ``IdentitySettings`` (whose
tests must stay green), so nothing here touches the OIDC config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings

Strategy = Literal["fill_first", "round_robin", "random", "least_used"]


class AgentCredentialConfig(BaseModel):
    """Tuning for issuance, rotation, and the Hermes credential_pool.

    Defaults mirror the Hermes credential_pool values (COOLDOWN_429=60s,
    402=30min, 401=5min, MAX_CONSECUTIVE_FAILURES=5 -> 1h park).
    """

    #: SecretStore path prefix; the secret for an agent lives at
    #: ``{secret_prefix}/{department}/{agent_name}`` (one KV-v2 path per agent).
    secret_prefix: str = "agents"
    #: Bytes of entropy in the generated token (``secrets.token_urlsafe``).
    secret_bytes: int = 32
    #: Seconds after issue at which an active credential is due for rotation.
    rotate_after_seconds: float = 86400.0
    #: Overlap window: a rotated-out credential stays valid this long (in-flight
    #: sessions are not broken mid-run).
    grace_seconds: float = 300.0
    #: Pool selection strategy across an agent's live credentials.
    strategy: Strategy = "fill_first"
    #: Cooldown after a 429 (rate-limit) failure, unless retry-after overrides.
    cooldown_429_seconds: float = 60.0
    #: Cooldown after a 402 (billing) failure.
    cooldown_402_seconds: float = 1800.0
    #: Cooldown after a 401 (auth) failure.
    cooldown_401_seconds: float = 300.0
    #: Consecutive failures that park a credential (the strike limit).
    max_consecutive_failures: int = 5
    #: Park duration once the strike limit is hit.
    consecutive_cooldown_seconds: float = 3600.0

    @field_validator(
        "secret_bytes",
        "rotate_after_seconds",
        "grace_seconds",
        "cooldown_429_seconds",
        "cooldown_402_seconds",
        "cooldown_401_seconds",
        "max_consecutive_failures",
        "consecutive_cooldown_seconds",
    )
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("value must be positive")
        return v

    @field_validator("secret_prefix")
    @classmethod
    def _non_empty_prefix(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("secret_prefix must be a non-empty path segment")
        return v

    @field_validator("secret_bytes")
    @classmethod
    def _min_secret_bytes(cls, v: int) -> int:
        if v < 16:
            raise ValueError("secret_bytes must be >= 16 (sufficient entropy)")
        return v

    @model_validator(mode="after")
    def _grace_below_rotation(self) -> AgentCredentialConfig:
        if self.grace_seconds >= self.rotate_after_seconds:
            raise ValueError(
                "grace_seconds must be shorter than rotate_after_seconds "
                "(the grace overlap must close before the next rotation is due)"
            )
        return self


class AgentIdentitySettings(NeSettings):
    """Layered settings for the agent-identity credential lifecycle."""

    model_config = SettingsConfigDict(
        env_prefix="NE_AGENT_IDENTITY_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    credentials: AgentCredentialConfig = AgentCredentialConfig()
