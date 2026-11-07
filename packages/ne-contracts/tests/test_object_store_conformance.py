# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end proof of the registry + conformance-kit pattern (Checkpoint A).

The dummy adapter is discovered via a REAL ``ne.plugins.object_store`` entry
point, resolved through :class:`PluginRegistry`, then run against the full
:class:`ObjectStoreContractKit`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ne.contracts import PluginRegistry
from ne.contracts.storage import ObjectStore
from ne.contracts.testing.kits.object_store import ObjectStoreContractKit


class TestMemoryObjectStoreConformance(ObjectStoreContractKit):
    def create_store(self) -> ObjectStore:
        adapter_cls = PluginRegistry().resolve("object_store", "memory")
        return adapter_cls()

    def create_store_at(self, now: Callable[[], datetime]) -> ObjectStore:
        adapter_cls = PluginRegistry().resolve("object_store", "memory")
        return adapter_cls(now=now)
