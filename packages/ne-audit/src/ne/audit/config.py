# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Layered settings for ne-audit — selects and configures the AuditSink adapter."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import SettingsConfigDict

from ne.audit.engine import EngineTimeouts
from ne.contracts.config import NeSettings


class CheckpointConfig(BaseModel):
    """Signed-checkpoint policy (see :mod:`ne.audit.checkpoint`)."""

    key_path: str = "ne/audit/checkpoint-signing"
    prefix: str = "audit/checkpoints/"
    retention_days: int = 365
    every_n_records: int = 1000


class AuditSettings(NeSettings):
    """Layered settings selecting and configuring the AuditSink adapter."""

    model_config = SettingsConfigDict(
        env_prefix="NE_AUDIT_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    sink: str = "postgres"
    #: Empty means the caller must supply an engine directly.
    database_url: str = ""
    checkpoint: CheckpointConfig = CheckpointConfig()
    #: Connect/statement/lock/pool timeouts for the audit engine (P2-16). Adding
    #: this nested model changes the ne-audit module-subset config_hash — expected
    #: (the DispatchSettings/memory_backend precedent); the per-test freeze fixture
    #: re-freezes settings fresh.
    engine_timeouts: EngineTimeouts = EngineTimeouts()
