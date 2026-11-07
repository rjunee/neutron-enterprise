# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Every issue/rotate/revoke appends exactly one config_hash-stamped AuditEvent."""

from __future__ import annotations

import pytest

from ne.contracts import reset_config_hash_for_testing
from ne.contracts.errors import ConfigHashNotFrozenError, SecretNotFoundError
from ne.contracts.testing.memory import MemorySecretStore
from ne.identity.agents import AgentCredentialConfig, AgentCredentialService, CredentialState
from ne.identity.errors import CredentialAuditError, CredentialNotFoundError

from .conftest import (
    FailingSink,
    ListSink,
    MutableClock,
    run,
    sample_agent,
    sample_scope,
)


def test_lifecycle_emits_one_event_each(
    agent_service: AgentCredentialService, agent_sink: ListSink, frozen_agent_config_hash: str
) -> None:
    agent = sample_agent()
    cred = run(agent_service.issue(agent, sample_scope("read.notes"), actor="alice"))
    new = run(agent_service.rotate(agent, actor="bob"))
    run(agent_service.revoke(new.credential_id, actor="carol", reason="rotation-drill"))

    assert agent_sink.actions() == [
        "agent.credential.issue",
        "agent.credential.rotate",
        "agent.credential.revoke",
    ]
    issue_ev, rotate_ev, revoke_ev = agent_sink.events
    assert issue_ev.resource == f"agent-credential:{cred.credential_id}"
    assert rotate_ev.resource == f"agent-credential:{new.credential_id}"
    assert issue_ev.actor == "alice"
    assert rotate_ev.actor == "bob"
    assert revoke_ev.actor == "carol"
    for ev in agent_sink.events:
        assert ev.config_hash == frozen_agent_config_hash
        assert ev.config_hash
        assert dict(ev.provenance)["source"] == "ne-identity"


def test_unaudited_issue_is_unwound(
    agent_store: MemorySecretStore, agent_clock: MutableClock, frozen_agent_config_hash: str
) -> None:
    svc = AgentCredentialService(
        agent_store, FailingSink(), AgentCredentialConfig(), clock=agent_clock
    )
    agent = sample_agent()
    with pytest.raises(CredentialAuditError):
        run(svc.issue(agent, sample_scope("read.notes"), actor="op"))

    # The credential is NOT usable, and its secret version was destroyed.
    with pytest.raises(CredentialNotFoundError):
        svc.validate("sales/assistant/v1")
    with pytest.raises(SecretNotFoundError):
        run(agent_store.read("agents/sales/assistant", version=1))


def test_unfrozen_config_hash_propagates(
    agent_store: MemorySecretStore, agent_clock: MutableClock
) -> None:
    # No config hash frozen: event CONSTRUCTION must raise ConfigHashNotFrozenError,
    # NOT be swallowed into CredentialAuditError.
    reset_config_hash_for_testing()
    svc = AgentCredentialService(
        agent_store, ListSink(), AgentCredentialConfig(), clock=agent_clock
    )
    with pytest.raises(ConfigHashNotFrozenError):
        run(svc.issue(sample_agent(), sample_scope("read.notes"), actor="op"))


def test_unfrozen_config_hash_issue_is_unwound(
    agent_store: MemorySecretStore, agent_clock: MutableClock
) -> None:
    # P2 regression (issue path): _emit raising ConfigHashNotFrozenError during
    # event CONSTRUCTION (before sink.append) must fully unwind the just-issued
    # credential — no audit event, not selectable, secret version destroyed.
    reset_config_hash_for_testing()
    sink = ListSink()
    svc = AgentCredentialService(agent_store, sink, AgentCredentialConfig(), clock=agent_clock)
    agent = sample_agent()
    with pytest.raises(ConfigHashNotFrozenError):
        run(svc.issue(agent, sample_scope("read.notes"), actor="op"))

    assert sink.events == []  # nothing audited
    with pytest.raises(CredentialNotFoundError):
        svc.validate("sales/assistant/v1")
    with pytest.raises(CredentialNotFoundError):
        svc.select_credential(agent)  # NOT selectable — the live-unaudited-cred gap
    with pytest.raises(SecretNotFoundError):
        run(agent_store.read("agents/sales/assistant", version=1))


def test_unfrozen_config_hash_rotate_is_unwound(
    agent_service: AgentCredentialService,
    agent_store: MemorySecretStore,
    agent_sink: ListSink,
    frozen_agent_config_hash: str,
) -> None:
    # P2 regression (rotate path): the same failure during rotate unwinds the NEW
    # version only — the old credential reverts to ACTIVE and stays selectable.
    agent = sample_agent()
    old = run(agent_service.issue(agent, sample_scope("read.notes"), actor="op"))
    reset_config_hash_for_testing()  # unfreeze AFTER a successfully audited issue
    with pytest.raises(ConfigHashNotFrozenError):
        run(agent_service.rotate(agent, actor="op"))

    assert agent_sink.actions() == ["agent.credential.issue"]  # rotate emitted nothing
    assert agent_service.validate(old.credential_id).state is CredentialState.ACTIVE
    assert agent_service.select_credential(agent).credential_id == old.credential_id
    with pytest.raises(CredentialNotFoundError):
        agent_service.validate("sales/assistant/v2")
    with pytest.raises(SecretNotFoundError):
        run(agent_store.read("agents/sales/assistant", version=2))


def test_revoke_agent_fails_closed_across_all_live_credentials(
    agent_store: MemorySecretStore, agent_clock: MutableClock, frozen_agent_config_hash: str
) -> None:
    # P1 regression (bulk-revoke path): an audit failure while revoking ONE
    # credential must NOT abort the sweep and leave the agent's other live
    # credentials usable. Issue + rotate (ACTIVE + GRACE) under a good sink, then
    # swap in a sink whose append always raises. revoke_agent must revoke EVERY
    # live credential before surfacing the audit failure.
    good = ListSink()
    svc = AgentCredentialService(agent_store, good, AgentCredentialConfig(), clock=agent_clock)
    agent = sample_agent()
    old = run(svc.issue(agent, sample_scope("read.notes"), actor="op"))
    new = run(svc.rotate(agent, actor="op"))
    assert svc.credential_state(old.credential_id) is CredentialState.GRACE
    assert svc.credential_state(new.credential_id) is CredentialState.ACTIVE

    svc._sink = FailingSink()  # type: ignore[attr-defined]
    with pytest.raises(CredentialAuditError):
        run(svc.revoke_agent(agent, actor="op"))

    # BOTH live credentials are revoked despite the audit outage — no live
    # credential survives, so the agent has nothing selectable.
    assert svc.credential_state(old.credential_id) is CredentialState.REVOKED
    assert svc.credential_state(new.credential_id) is CredentialState.REVOKED
    with pytest.raises(CredentialNotFoundError):
        svc.select_credential(agent)
