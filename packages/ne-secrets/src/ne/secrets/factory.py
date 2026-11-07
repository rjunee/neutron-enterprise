# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Config-driven SecretStore construction — ``ne.plugins.secret_store`` selection."""

from __future__ import annotations

from ne.contracts.plugins import PluginRegistry
from ne.contracts.secrets import SecretStore
from ne.secrets.config import SecretsSettings


def create_secret_store(
    settings: SecretsSettings | None = None,
    registry: PluginRegistry | None = None,
) -> SecretStore:
    """Resolve and construct the configured SecretStore adapter.

    ``settings.store`` selects the ``ne.plugins.secret_store`` entry point (or
    a programmatic ``registry.register`` override, which wins). If the
    resolved class exposes ``from_settings``, it is used to construct the
    instance; otherwise the class is constructed with no arguments.
    """
    settings = settings or SecretsSettings()
    registry = registry or PluginRegistry()
    cls = registry.resolve("secret_store", settings.store)
    if hasattr(cls, "from_settings"):
        return cls.from_settings(settings)  # type: ignore[no-any-return]
    return cls()  # type: ignore[no-any-return]
