# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Config-driven AuditSink construction — ``ne.plugins.audit_sink`` selection."""

from __future__ import annotations

from ne.audit.config import AuditSettings
from ne.contracts.governance import AuditSink
from ne.contracts.plugins import PluginRegistry


def create_audit_sink(
    settings: AuditSettings | None = None,
    registry: PluginRegistry | None = None,
) -> AuditSink:
    """Resolve and construct the configured AuditSink adapter.

    ``settings.sink`` selects the ``ne.plugins.audit_sink`` entry point (or a
    programmatic ``registry.register`` override, which wins). If the resolved
    class exposes ``from_settings`` it is used to construct the instance;
    otherwise the class is constructed with no arguments.
    """
    settings = settings or AuditSettings()
    registry = registry or PluginRegistry()
    cls = registry.resolve("audit_sink", settings.sink)
    if hasattr(cls, "from_settings"):
        return cls.from_settings(settings)  # type: ignore[no-any-return]
    return cls()  # type: ignore[no-any-return]
