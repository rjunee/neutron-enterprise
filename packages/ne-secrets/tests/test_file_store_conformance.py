# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Conformance kit run for the plain-codec FileSecretStore.

Runs UNCONDITIONALLY in CI (no external binary/service prerequisite) — this is
the SPEC-mandated proof that the SOPS/env adapter runs fully in CI.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ne.contracts.plugins import PluginRegistry
from ne.contracts.secrets import SecretStore
from ne.contracts.testing.kits.secret_store import SecretStoreContractKit
from ne.secrets.codecs import PlainJsonCodec


class TestFileSecretStorePlainConformance(SecretStoreContractKit):
    def create_store(self) -> SecretStore:
        adapter_cls: type[SecretStore] = PluginRegistry().resolve("secret_store", "sops-file")
        path = Path(tempfile.mkdtemp()) / "secrets.json"
        return adapter_cls(path, codec=PlainJsonCodec())  # type: ignore[call-arg]
