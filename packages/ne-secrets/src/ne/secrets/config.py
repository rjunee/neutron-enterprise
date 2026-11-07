# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Layered settings for ne-secrets — selects and configures the SecretStore adapter.

SECURITY DECISION: the OpenBao token is NEVER a settings field. Settings only
ever hold ``token_env_var`` (the NAME of an environment variable); the adapter
factory reads ``os.environ[token_env_var]`` at construction time. This keeps
tokens out of TOML files and out of the ``config_hash`` preimage (SPEC §4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings


class FileStoreConfig(BaseModel):
    """Configuration for the ``sops-file`` (pilot default) adapter."""

    path: Path = Path("secrets.ne.json")
    mode: Literal["plain", "sops"] = "sops"
    sops_binary: str = "sops"
    sops_config: Path | None = None
    # Wall-clock cap for each sops subprocess. Under AIR_GAP a KMS-unreachable
    # sops retries network I/O forever (or an age/PGP backend prompts on stdin);
    # without a cap the to_thread worker never returns and FileSecretStore's lock
    # is held forever, wedging every subsequent read/write. Surfaced as
    # NE_SECRETS_FILE__SOPS_TIMEOUT_SECONDS.
    sops_timeout_seconds: float = 30.0


class OpenBaoConfig(BaseModel):
    """Configuration for the ``openbao`` (prod profile) adapter.

    ``token_env_var`` names the environment variable holding the OpenBao/Vault
    token — the token value itself is never a settings field.
    """

    url: str = "http://localhost:8200"
    mount: str = "secret"
    base_path: str = ""
    token_env_var: str = "BAO_TOKEN"
    timeout_seconds: float = 5.0


class SecretsSettings(NeSettings):
    """Layered settings selecting and configuring the SecretStore adapter."""

    model_config = SettingsConfigDict(
        env_prefix="NE_SECRETS_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    store: str = "sops-file"
    file: FileStoreConfig = FileStoreConfig()
    openbao: OpenBaoConfig = OpenBaoConfig()
