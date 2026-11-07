# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Reserved-namespace subject rejection (NE-FIX-IDENTITY P2-1).

A verified token whose ``sub`` is in the reserved ``acl:*`` / ``role:*`` namespace
(e.g. exactly ``acl:tenant-all``) must be rejected at the verifier boundary, and
:class:`JitProvisioner` must independently refuse such a subject and store
nothing — so a reserved id can never become a ``Principal.id`` and match every
TENANT_ALL-stamped chunk without being store-known (defeats CKD2).
"""

from __future__ import annotations

from typing import Any

import pytest

from ne.contracts.identity import TokenClaims
from ne.identity.config import RoleMappingConfig
from ne.identity.errors import TokenVerificationError
from ne.identity.provisioning import InMemoryPrincipalStore, JitProvisioner
from ne.identity.verifier import OidcTokenVerifier

from .conftest import TEST_ISSUER, run


def test_reserved_acl_subject_rejected_at_boundary(verifier: OidcTokenVerifier, mint: Any) -> None:
    with pytest.raises(TokenVerificationError) as excinfo:
        run(verifier.verify(mint({"sub": "acl:tenant-all"})))
    assert str(excinfo.value) == "token verification failed"


def test_reserved_role_subject_rejected(verifier: OidcTokenVerifier, mint: Any) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"sub": "role:admin"})))


def test_reserved_public_link_subject_rejected(verifier: OidcTokenVerifier, mint: Any) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"sub": "acl:public-link"})))


def test_nonstring_subject_rejected(verifier: OidcTokenVerifier, mint: Any) -> None:
    with pytest.raises(TokenVerificationError):
        run(verifier.verify(mint({"sub": 123})))


def test_provisioner_rejects_reserved_subject_and_stores_nothing() -> None:
    claims = TokenClaims(
        subject="acl:tenant-all",
        issuer=TEST_ISSUER,
        claims={"iss": TEST_ISSUER, "sub": "acl:tenant-all", "groups": ["engineering"]},
    )
    store = InMemoryPrincipalStore()
    provisioner = JitProvisioner(store, RoleMappingConfig())
    with pytest.raises(TokenVerificationError):
        run(provisioner.provision(claims))
    assert run(store.get("acl:tenant-all")) is None
