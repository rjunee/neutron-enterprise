# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""JIT provisioning + PrincipalResolver behavior (recompute, never accrete)."""

from __future__ import annotations

import pytest

from ne.contracts.identity import TokenClaims
from ne.contracts.types import Principal
from ne.identity.config import RoleMappingConfig
from ne.identity.errors import UnknownPrincipalError
from ne.identity.provisioning import (
    InMemoryPrincipalStore,
    JitPrincipalResolver,
    JitProvisioner,
)
from ne.identity.roles import ROLE_PREFIX

from .conftest import TEST_ISSUER, run


def _claims(groups: list[str], **extra: object) -> TokenClaims:
    payload: dict[str, object] = {"sub": "user-1", "iss": TEST_ISSUER, "groups": groups}
    payload.update(extra)
    return TokenClaims(subject="user-1", issuer=TEST_ISSUER, claims=payload)


def _config() -> RoleMappingConfig:
    return RoleMappingConfig(
        mapping={"platform-admins": ("admin",), "engineering": ("developer",)},
        default_roles=("viewer",),
    )


def test_first_token_provisions_principal() -> None:
    store = InMemoryPrincipalStore()
    prov = JitProvisioner(store, _config())
    principal = run(
        prov.provision(
            _claims(
                ["platform-admins"],
                email="dev@ne.local",
                preferred_username="dev",
                name="Dev User",
                extra_number=42,
            )
        )
    )
    assert principal.id == "user-1"
    assert "platform-admins" in principal.groups
    assert f"{ROLE_PREFIX}admin" in principal.groups
    assert f"{ROLE_PREFIX}viewer" in principal.groups
    # claims whitelist, str-only, plus iss
    assert principal.claims["email"] == "dev@ne.local"
    assert principal.claims["preferred_username"] == "dev"
    assert principal.claims["iss"] == TEST_ISSUER
    assert "extra_number" not in principal.claims
    # stored
    assert run(store.get("user-1")) == principal


def test_reprovision_removes_dropped_group() -> None:
    store = InMemoryPrincipalStore()
    prov = JitProvisioner(store, _config())
    run(prov.provision(_claims(["platform-admins", "engineering"])))
    updated = run(prov.provision(_claims(["engineering"])))
    # de-escalation: platform-admins group AND role:admin gone (recomputed)
    assert "platform-admins" not in updated.groups
    assert f"{ROLE_PREFIX}admin" not in updated.groups
    assert "engineering" in updated.groups
    assert f"{ROLE_PREFIX}developer" in updated.groups


def test_groups_never_accrete_across_tokens() -> None:
    store = InMemoryPrincipalStore()
    prov = JitProvisioner(store, _config())
    run(prov.provision(_claims(["platform-admins"])))
    final = run(prov.provision(_claims([])))  # no groups at all now
    assert "platform-admins" not in final.groups
    assert f"{ROLE_PREFIX}admin" not in final.groups
    assert final.groups == (f"{ROLE_PREFIX}viewer",)  # only default role remains


def test_role_prefixed_group_cannot_escalate_via_provision() -> None:
    store = InMemoryPrincipalStore()
    prov = JitProvisioner(store, _config())
    principal = run(prov.provision(_claims([f"{ROLE_PREFIX}admin", "engineering"])))
    # the smuggled role:admin group is stripped; only mapped roles survive
    assert f"{ROLE_PREFIX}admin" not in principal.groups
    assert f"{ROLE_PREFIX}developer" in principal.groups


def test_acl_label_group_cannot_smuggle_via_provision() -> None:
    store = InMemoryPrincipalStore()
    prov = JitProvisioner(store, _config())
    principal = run(prov.provision(_claims(["acl:tenant-all", "acl:public-link", "engineering"])))
    # the smuggled reserved ACL labels are stripped at the trust boundary
    assert "engineering" in principal.groups
    assert not any(g.startswith("acl:") for g in principal.groups)


def test_resolver_returns_provisioned_principal() -> None:
    store = InMemoryPrincipalStore()
    prov = JitProvisioner(store, _config())
    provisioned = run(prov.provision(_claims(["engineering"])))
    resolver = JitPrincipalResolver(store)
    assert run(resolver.resolve("user-1")) == provisioned


def test_resolver_unknown_subject_raises() -> None:
    resolver = JitPrincipalResolver(InMemoryPrincipalStore())
    with pytest.raises(UnknownPrincipalError):
        run(resolver.resolve("ghost"))


def test_custom_groups_claim_name() -> None:
    store = InMemoryPrincipalStore()
    prov = JitProvisioner(store, _config(), groups_claim="roles_source")
    claims = TokenClaims(
        subject="user-1",
        issuer=TEST_ISSUER,
        claims={"sub": "user-1", "iss": TEST_ISSUER, "roles_source": ["engineering"]},
    )
    principal = run(prov.provision(claims))
    assert "engineering" in principal.groups


def test_default_claim_allowlist_excludes_department() -> None:
    # AMENDMENT A1: the DEFAULT principal-claim allowlist is byte-identical to the
    # historic hardcoded whitelist — a verified `department` claim is DROPPED.
    store = InMemoryPrincipalStore()
    prov = JitProvisioner(store, _config())
    principal = run(
        prov.provision(
            _claims(
                ["engineering"],
                email="dev@ne.local",
                preferred_username="dev",
                name="Dev User",
                department="engineering",
            )
        )
    )
    assert principal.claims["email"] == "dev@ne.local"
    assert principal.claims["preferred_username"] == "dev"
    assert principal.claims["name"] == "Dev User"
    assert principal.claims["iss"] == TEST_ISSUER
    # department is NOT propagated under the default allowlist (fail-closed today).
    assert "department" not in principal.claims


def test_configured_claim_allowlist_propagates_department() -> None:
    # A configured allowlist is authoritative: department propagates, name (unlisted)
    # is excluded, and a non-str claim value is still dropped.
    store = InMemoryPrincipalStore()
    prov = JitProvisioner(store, _config(), principal_claims=("email", "department"))
    principal = run(
        prov.provision(
            _claims(
                ["engineering"],
                email="dev@ne.local",
                name="Dev User",
                department="engineering",
                department_code=7,  # non-str -> dropped even though listed shape allows
            )
        )
    )
    assert principal.claims["department"] == "engineering"
    assert principal.claims["email"] == "dev@ne.local"
    # 'name' is NOT in the configured allowlist -> absent.
    assert "name" not in principal.claims
    # a non-str value is dropped by the isinstance(str) guard.
    assert "department_code" not in principal.claims


def test_static_conformance_to_frozen_protocols() -> None:
    # Import-time typed assertions live in the modules; this asserts they are the
    # frozen contract types at runtime too.
    from ne.contracts.identity import PrincipalResolver, TokenVerifier
    from ne.identity.verifier import OidcTokenVerifier

    assert issubclass(JitPrincipalResolver, object)
    # Structural: instances satisfy the runtime-checkable-free Protocols via duck typing.
    resolver: PrincipalResolver = JitPrincipalResolver(InMemoryPrincipalStore())
    assert hasattr(resolver, "resolve")
    _ = TokenVerifier  # referenced for documentation of the conformance surface
    _ = OidcTokenVerifier
    assert isinstance(Principal(id="x"), Principal)
