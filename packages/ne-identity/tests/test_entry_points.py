# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Proves real ``ne.plugins.*`` entry-point registration (C5 plugin discipline)."""

from __future__ import annotations

from ne.contracts.plugins import PluginRegistry, load_plugin
from ne.identity.provisioning import JitPrincipalResolver
from ne.identity.verifier import OidcTokenVerifier


def test_token_verifier_entry_point() -> None:
    assert load_plugin("token_verifier", "keycloak-oidc") is OidcTokenVerifier


def test_principal_resolver_entry_point() -> None:
    assert load_plugin("principal_resolver", "jit") is JitPrincipalResolver


def test_registry_resolves_both() -> None:
    reg = PluginRegistry()
    assert reg.resolve("token_verifier", "keycloak-oidc") is OidcTokenVerifier
    assert reg.resolve("principal_resolver", "jit") is JitPrincipalResolver
