# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""rotate() — new ACTIVE version, old lingers in GRACE, in-flight sessions unbroken."""

from __future__ import annotations

import pytest

from ne.identity.agents import AgentCredentialService, CredentialScope, CredentialState
from ne.identity.errors import CredentialExpiredError

from .conftest import MutableClock, run, sample_agent, sample_scope


def test_rotate_creates_active_v2_old_to_grace(
    agent_service: AgentCredentialService,
) -> None:
    agent = sample_agent()
    old = run(agent_service.issue(agent, sample_scope("read.notes"), actor="op"))
    new = run(agent_service.rotate(agent, actor="op"))

    assert new.secret_version == 2
    assert new.credential_id == "sales/assistant/v2"
    assert new.state is CredentialState.ACTIVE
    assert agent_service.credential_state(old.credential_id) is CredentialState.GRACE


def test_old_credential_valid_during_grace(agent_service: AgentCredentialService) -> None:
    agent = sample_agent()
    old = run(agent_service.issue(agent, sample_scope("read.notes"), actor="op"))
    old_token = run(agent_service.resolve_secret(old.credential_id)).get_secret_value()
    new = run(agent_service.rotate(agent, actor="op"))

    # DURING grace both old and new validate + resolve (in-flight sessions unbroken).
    assert agent_service.validate(old.credential_id).state is CredentialState.GRACE
    assert agent_service.validate(new.credential_id).state is CredentialState.ACTIVE
    assert run(agent_service.resolve_secret(old.credential_id)).get_secret_value() == old_token
    new_token = run(agent_service.resolve_secret(new.credential_id)).get_secret_value()
    assert new_token != old_token


def test_old_credential_expires_past_grace(
    agent_service: AgentCredentialService, agent_clock: MutableClock
) -> None:
    agent = sample_agent()
    old = run(agent_service.issue(agent, sample_scope("read.notes"), actor="op"))
    run(agent_service.rotate(agent, actor="op"))

    agent_clock.advance(301.0)  # past grace_seconds=300
    assert agent_service.credential_state(old.credential_id) is CredentialState.EXPIRED
    with pytest.raises(CredentialExpiredError):
        agent_service.validate(old.credential_id)
    with pytest.raises(CredentialExpiredError):
        run(agent_service.resolve_secret(old.credential_id))


def test_rotation_due_flips_after_interval(
    agent_service: AgentCredentialService, agent_clock: MutableClock
) -> None:
    agent = sample_agent()
    run(agent_service.issue(agent, sample_scope("read.notes"), actor="op"))
    assert agent_service.rotation_due(agent) is False
    assert agent_service.rotation_due_agents() == ()

    agent_clock.advance(86400.0 + 1.0)  # past rotate_after_seconds
    assert agent_service.rotation_due(agent) is True
    assert tuple(a.key for a in agent_service.rotation_due_agents()) == (agent.key,)


def test_scope_carries_over_and_can_be_narrowed(
    agent_service: AgentCredentialService,
) -> None:
    agent = sample_agent()
    run(agent_service.issue(agent, sample_scope("read.notes", "read.docs"), actor="op"))

    carried = run(agent_service.rotate(agent, actor="op"))
    assert set(carried.scope.tools) == {"read.notes", "read.docs"}

    narrowed = run(
        agent_service.rotate(agent, actor="op", scope=CredentialScope(tools=("read.notes",)))
    )
    assert narrowed.scope.tools == ("read.notes",)
