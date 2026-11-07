# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Plugin registry and entry-point loading (SPEC section 4).

Adapters register via Python entry points under ``ne.plugins.<slot>``; selection
is config-driven, e.g. ``registry.resolve("vector_store", settings.brain.vector_store)``
with ``brain.vector_store = "qdrant"``.

A :class:`PluginRegistry` also supports in-process programmatic registration
(for tests and embedding), which OVERRIDES entry points of the same name.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

from ne.contracts.errors import PluginNotFoundError

PLUGIN_GROUP_PREFIX = "ne.plugins."


def _group(slot: str) -> str:
    return f"{PLUGIN_GROUP_PREFIX}{slot}"


def iter_plugins(slot: str) -> dict[str, importlib.metadata.EntryPoint]:
    """Return ``{name: EntryPoint}`` for every plugin installed in ``slot``."""
    eps = importlib.metadata.entry_points(group=_group(slot))
    return {ep.name: ep for ep in eps}


def load_plugin(slot: str, name: str) -> Any:
    """Load and return the object an entry point points to.

    Raises :class:`PluginNotFoundError` (listing the available names) when no
    plugin ``name`` is installed in ``slot``.
    """
    plugins = iter_plugins(slot)
    ep = plugins.get(name)
    if ep is None:
        raise PluginNotFoundError(slot, name, sorted(plugins))
    return ep.load()


class PluginRegistry:
    """Config-driven plugin resolution: programmatic table first, then entry points."""

    def __init__(self) -> None:
        self._programmatic: dict[str, dict[str, Any]] = {}

    def register(self, slot: str, name: str, factory: Any) -> None:
        """Register a plugin in-process. Overrides an entry point of the same name.

        Intended for tests and embedding scenarios where installing a real
        distribution entry point is unnecessary.
        """
        self._programmatic.setdefault(slot, {})[name] = factory

    def resolve(self, slot: str, name: str) -> Any:
        """Resolve a plugin: programmatic registrations win, then entry points."""
        table = self._programmatic.get(slot, {})
        if name in table:
            return table[name]
        return load_plugin(slot, name)

    def available(self, slot: str) -> list[str]:
        """Return the sorted union of programmatic and entry-point plugin names."""
        names = set(self._programmatic.get(slot, {}))
        names.update(iter_plugins(slot))
        return sorted(names)
