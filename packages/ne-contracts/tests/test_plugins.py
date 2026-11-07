# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Plugin registry + entry-point discovery via REAL installed metadata.

Requires ``uv sync --all-packages`` so ne-contracts' own entry points
(``ne.plugins.object_store``/``secret_store`` = ``memory``) are installed.
"""

from __future__ import annotations

import pytest

from ne.contracts import (
    PLUGIN_GROUP_PREFIX,
    PluginNotFoundError,
    PluginRegistry,
    iter_plugins,
    load_plugin,
)
from ne.contracts.testing.memory import MemoryObjectStore, MemorySecretStore


def test_group_prefix() -> None:
    assert PLUGIN_GROUP_PREFIX == "ne.plugins."


def test_iter_plugins_finds_memory_object_store() -> None:
    assert "memory" in iter_plugins("object_store")


def test_iter_plugins_finds_memory_secret_store() -> None:
    assert "memory" in iter_plugins("secret_store")


def test_load_plugin_returns_adapter_classes() -> None:
    assert load_plugin("object_store", "memory") is MemoryObjectStore
    assert load_plugin("secret_store", "memory") is MemorySecretStore


def test_load_missing_plugin_raises_with_available_names() -> None:
    with pytest.raises(PluginNotFoundError) as excinfo:
        load_plugin("object_store", "nope")
    assert "memory" in str(excinfo.value)
    # "s3" joined "memory" with NE-S3-OBJECTSTORE (the production Object Lock
    # WORM adapter in packages/ne-storage), and "azure_blob" joined both with
    # NE-AZBLOB-OBJECTSTORE (the Azure immutability-policy WORM adapter in the
    # same package — Azure Blob is not S3-compatible, so C9 parity needs its
    # own adapter). All three are registered object_store plugins; the list is
    # sorted, so "azure_blob" sorts first.
    assert excinfo.value.available == ["azure_blob", "memory", "s3"]


def test_registry_resolves_entry_point() -> None:
    registry = PluginRegistry()
    assert registry.resolve("object_store", "memory") is MemoryObjectStore


def test_programmatic_registration_overrides_entry_point() -> None:
    registry = PluginRegistry()

    class Fake:
        pass

    registry.register("object_store", "memory", Fake)
    assert registry.resolve("object_store", "memory") is Fake
    assert "memory" in registry.available("object_store")


def test_available_is_union_and_sorted() -> None:
    registry = PluginRegistry()

    class Fake:
        pass

    registry.register("object_store", "aaa", Fake)
    avail = registry.available("object_store")
    assert avail == sorted(avail)
    assert {"aaa", "memory"} <= set(avail)
