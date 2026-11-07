# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""revoke() — fails closed immediately; stands even when the audit sink fails."""

from __future__ import annotations

import pytest

from ne.contracts.errors import SecretNotFoundError
from ne.contracts.testing.memory import MemorySecretStore
from ne.identity.agents import (
    AgentCredentialConfig,
    AgentCredentialService,
    CredentialState,
)
from ne.identity.errors import (
    CredentialAuditError,
    CredentialNotFoundError,
    CredentialRevokedError,
)

from .conftest import (
    FailingSink,
    ListSink,
    MutableClock,
    run,
    sample_agent,
    sample_scope,
)


def test_revoke_fails_closed_immediately(agent_service: AgentCredentialService) -> None:
    agent = sample_agent()
    cred = run(agent_service.issue(agent, sample_scope("read.notes"), actor="op"))
    run(agent_service.revoke(cred.credential_id, actor="op", reason="compromise"))

    assert agent_service.credential_state(cred.credential_id) is CredentialState.REVOKED
    with pytest.raises(CredentialRevokedError):
        agent_service.validate(cred.credential_id)
    with pytest.raises(CredentialRevokedError):
        agent_service.bind_principal(cred.credential_id)
    # Secret version soft-deleted in the store -> resolve fails closed.
    with pytest.raises(CredentialRevokedError):
        run(agent_service.resolve_secret(cred.credential_id))


def test_revoked_secret_version_is_soft_deleted(
    agent_service: AgentCredentialService, agent_store: MemorySecretStore
) -> None:
    cred = run(agent_service.issue(sample_agent(), sample_scope("read.notes"), actor="op"))
    run(agent_service.revoke(cred.credential_id, actor="op"))
    with pytest.raises(SecretNotFoundError):
        run(agent_store.read(cred.secret_path, version=cred.secret_version))


def test_revoke_unknown_id_raises(agent_service: AgentCredentialService) -> None:
    with pytest.raises(CredentialNotFoundError):
        run(agent_service.revoke("nope/none/v1", actor="op"))


def test_revoke_agent_kills_active_and_grace(agent_service: AgentCredentialService) -> None:
    agent = sample_agent()
    old = run(agent_service.issue(agent, sample_scope("read.notes"), actor="op"))
    new = run(agent_service.rotate(agent, actor="op"))
    assert agent_service.credential_state(old.credential_id) is CredentialState.GRACE

    run(agent_service.revoke_agent(agent, actor="op"))
    assert agent_service.credential_state(old.credential_id) is CredentialState.REVOKED
    assert agent_service.credential_state(new.credential_id) is CredentialState.REVOKED


def test_revoke_stands_even_when_audit_fails(
    agent_store: MemorySecretStore,
    agent_clock: MutableClock,
    frozen_agent_config_hash: str,
) -> None:
    # Issue with a recording sink, then swap in a failing sink for the revoke so
    # the credential exists first.
    good = ListSink()
    svc = AgentCredentialService(agent_store, good, AgentCredentialConfig(), clock=agent_clock)
    cred = run(svc.issue(sample_agent(), sample_scope("read.notes"), actor="op"))

    svc._sink = FailingSink()  # type: ignore[attr-defined]
    with pytest.raises(CredentialAuditError):
        run(svc.revoke(cred.credential_id, actor="op"))
    # Revocation is immediate and STANDS despite the audit failure.
    assert svc.credential_state(cred.credential_id) is CredentialState.REVOKED
    with pytest.raises(CredentialRevokedError):
        svc.validate(cred.credential_id)
