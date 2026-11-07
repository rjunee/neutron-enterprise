# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Settings for the optional OpenFGA ReBAC module (NE-087 §4.3).

``RebacSettings`` is an :class:`~ne.contracts.config.NeSettings` subclass so
values layer init > ``NE_`` env > TOML > defaults, and ``api_token`` is a
``SecretStr`` (masked by ``compute_config_hash``'s ``model_dump``).

``enabled=False`` (the default) means the platform C7 filter is IDENTICAL to
today — the module is simply not wired. ``enabled=True`` with an empty
``store_id`` or ``authorization_model_id`` fails at construction (fail-closed:
you cannot enable ReBAC without a pinned store + model).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings

__all__ = ["RebacSettings"]


class RebacSettings(NeSettings):
    """Configuration for ne-permissions-rebac (optional OpenFGA sharing module)."""

    # Override the base ``NE_`` env prefix so ``NE_REBAC_*`` env drives this model
    # (NE-CLEAN4 PermissionsSettings precedent: without this the class silently
    # read the bare ``NE_ENABLED`` / ``NE_API_URL`` etc., so an ``NE_REBAC_ENABLED``
    # env would be ignored and the O-4 compose wiring would be a no-op).
    model_config = SettingsConfigDict(
        env_prefix="NE_REBAC_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    #: Optional module master switch. False = the C7 filter is identical to today.
    enabled: bool = False

    #: In-VPC OpenFGA sidecar URL — validated at client construction (sovereignty).
    #: 8080 is the HTTP API port the SDK speaks; 8081 is the gRPC port (health-probe
    #: only). Wiring MUST target 8080 or every call fails.
    api_url: str = "http://openfga:8080"

    #: Store id from bootstrap; required (non-empty) when ``enabled``.
    store_id: str = ""

    #: PINNED authorization-model id; every API call passes it. Required when
    #: ``enabled`` — an empty pin while enabled is a construction error.
    authorization_model_id: str = ""

    #: Preshared key between ne-api and the OpenFGA sidecar (never logged).
    api_token: SecretStr = SecretStr("")

    #: Per-request timeout (seconds).
    timeout_s: float = Field(default=3.0, gt=0)

    #: Backend-outage posture (§3.6). ``degrade`` (default): the rebac leg becomes
    #: ∅ (never wider, revocation-safe). ``deny``: the query raises.
    unavailable_mode: Literal["degrade", "deny"] = "degrade"

    #: ``ListObjects`` page-through cap; overflow truncates (over-restriction).
    max_shared_documents: int = Field(default=10_000, ge=1)

    @model_validator(mode="after")
    def _validate_enabled_requirements(self) -> RebacSettings:
        if self.enabled and (not self.store_id or not self.authorization_model_id):
            raise ValueError(
                "enabled=True requires a non-empty store_id and authorization_model_id "
                "(a pinned store + model); enable ReBAC only after bootstrap"
            )
        return self
