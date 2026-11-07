# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end proof of the registry + conformance-kit pattern for SecretStore.

The dummy adapter is discovered via a REAL ``ne.plugins.secret_store`` entry
point, resolved through :class:`PluginRegistry`, then run against the full
:class:`SecretStoreContractKit` (Vault KV v2 semantics).
"""

from __future__ import annotations

from ne.contracts import PluginRegistry
from ne.contracts.secrets import SecretStore
from ne.contracts.testing.kits.secret_store import SecretStoreContractKit


class TestMemorySecretStoreConformance(SecretStoreContractKit):
    def create_store(self) -> SecretStore:
        adapter_cls: type[SecretStore] = PluginRegistry().resolve("secret_store", "memory")
        return adapter_cls()
