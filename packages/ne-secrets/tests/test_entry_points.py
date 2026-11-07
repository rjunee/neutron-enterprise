# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Proves real ``ne.plugins.secret_store`` entry-point registration (C5 plugin discipline)."""

from __future__ import annotations

from ne.contracts.plugins import PluginRegistry, iter_plugins
from ne.secrets.file_store import FileSecretStore
from ne.secrets.openbao import OpenBaoSecretStore


def test_iter_plugins_includes_both_new_adapters() -> None:
    names = set(iter_plugins("secret_store"))
    assert {"memory", "sops-file", "openbao"} <= names


def test_registry_resolves_sops_file_to_file_secret_store() -> None:
    cls = PluginRegistry().resolve("secret_store", "sops-file")
    assert cls is FileSecretStore


def test_registry_resolves_openbao_to_open_bao_secret_store() -> None:
    cls = PluginRegistry().resolve("secret_store", "openbao")
    assert cls is OpenBaoSecretStore
