# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C1 redaction ratchet — the issued token appears in no repr/event/error, ever."""

from __future__ import annotations

import json

from ne.identity.agents import AgentCredentialService
from ne.identity.errors import (
    CredentialExpiredError,
    CredentialRevokedError,
    CredentialScopeError,
)

from .conftest import ListSink, MutableClock, run, sample_agent, sample_scope


def test_token_never_leaks_across_lifecycle(
    agent_service: AgentCredentialService, agent_sink: ListSink, agent_clock: MutableClock
) -> None:
    agent = sample_agent()
    cred = run(agent_service.issue(agent, sample_scope("read.notes"), actor="op"))
    secret = run(agent_service.resolve_secret(cred.credential_id))
    token = secret.get_secret_value()
    assert token  # sanity: we actually captured the material

    # Rotate (old -> grace), then drive the old credential to expiry, then revoke.
    run(agent_service.rotate(agent, actor="op"))
    pool_state = agent_service._pools[agent.key]  # type: ignore[attr-defined]

    haystacks: list[str] = [
        repr(agent_service),
        repr(cred),
        json.dumps(cred.model_dump(mode="json")),
        repr(pool_state),
        str(agent_service.select_credential(agent)),
    ]

    # Every recorded audit event (all serialized fields).
    for ev in agent_sink.events:
        haystacks.append(
            json.dumps(
                {
                    "action": ev.action,
                    "resource": ev.resource,
                    "actor": ev.actor,
                    "detail": ev.detail,
                    "provenance": dict(ev.provenance),
                }
            )
        )

    # Error messages raised across the lifecycle.
    agent_clock.advance(301.0)  # expire the grace credential
    try:
        agent_service.validate(cred.credential_id)
    except CredentialExpiredError as exc:
        haystacks.append(str(exc))

    revocable = agent_service.select_credential(agent).credential_id
    run(agent_service.revoke(revocable, actor="op"))
    try:
        agent_service.validate(revocable)
    except CredentialRevokedError as exc:
        haystacks.append(str(exc))

    haystacks.append(str(CredentialScopeError(f"tool 'x' not in scope for {revocable}")))

    # The SecretStr repr/str are masked.
    haystacks.append(repr(secret))
    haystacks.append(str(secret))

    for hay in haystacks:
        assert token not in hay, f"token leaked into: {hay[:80]}..."
