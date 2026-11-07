# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""bind_agent_principal — agent-scoped principal, anti-escalation, scope round-trip."""

from __future__ import annotations

import pytest

from ne.contracts.types import Principal
from ne.identity.agents import (
    AgentCredentialService,
    bind_agent_principal,
    credential_id_from_principal,
    scope_from_principal,
)
from ne.identity.agents.binding import (
    CLAIM_AGENT_NAME,
    CLAIM_CREDENTIAL_ID,
    CLAIM_DEPARTMENT,
    CLAIM_ON_BEHALF_OF,
    CLAIM_SCOPE_SOURCES,
    CLAIM_SCOPE_TOOLS,
)
from ne.identity.agents.model import (
    AgentCredential,
    AgentRef,
    CredentialScope,
    CredentialState,
)
from ne.identity.errors import CredentialRevokedError

from .conftest import run, sample_agent, sample_scope


def _credential() -> AgentCredential:
    from datetime import UTC, datetime

    return AgentCredential(
        credential_id="sales/assistant/v1",
        agent=AgentRef(department="sales", agent_name="assistant"),
        scope=CredentialScope(tools=("read.notes", "read.docs"), source_systems=("crm",)),
        secret_path="agents/sales/assistant",
        secret_version=1,
        issued_at=datetime(2026, 7, 5, tzinfo=UTC),
        state=CredentialState.ACTIVE,
    )


def test_bind_produces_agent_scoped_principal() -> None:
    principal = bind_agent_principal(_credential())
    assert principal.id == "agent:sales/assistant"
    assert principal.claims[CLAIM_DEPARTMENT] == "sales"
    assert principal.claims[CLAIM_AGENT_NAME] == "assistant"
    assert principal.claims[CLAIM_CREDENTIAL_ID] == "sales/assistant/v1"
    # scopes are sorted + comma-joined for a stable round-trip.
    assert principal.claims[CLAIM_SCOPE_TOOLS] == "read.docs,read.notes"
    assert principal.claims[CLAIM_SCOPE_SOURCES] == "crm"


def test_groups_are_agent_only_no_delegator_inheritance() -> None:
    delegator = Principal(
        id="user:ada", groups=("platform-admins", "finance"), claims={"department": "sales"}
    )
    principal = bind_agent_principal(_credential(), on_behalf_of=delegator)
    # ONLY the two agent groups — the human's groups are NOT inherited (anti-escalation).
    assert principal.groups == ("agent:sales/assistant", "agent:sales")
    assert "platform-admins" not in principal.groups
    assert principal.claims[CLAIM_ON_BEHALF_OF] == "user:ada"


def test_scope_and_credential_id_round_trip() -> None:
    principal = bind_agent_principal(_credential())
    assert credential_id_from_principal(principal) == "sales/assistant/v1"
    scope = scope_from_principal(principal)
    assert scope is not None
    assert set(scope.tools) == {"read.notes", "read.docs"}
    assert scope.source_systems == ("crm",)


def test_empty_scope_round_trips_to_empty_tuple() -> None:
    cred = _credential().model_copy(update={"scope": CredentialScope()})
    principal = bind_agent_principal(cred)
    scope = scope_from_principal(principal)
    assert scope is not None
    assert scope.tools == ()
    assert scope.source_systems == ()


def test_non_agent_principal_yields_none() -> None:
    human = Principal(id="user:ada", groups=("finance",))
    assert credential_id_from_principal(human) is None
    assert scope_from_principal(human) is None


def test_service_bind_principal_validates_first(
    agent_service: AgentCredentialService,
) -> None:
    agent = sample_agent()
    cred = run(agent_service.issue(agent, sample_scope("read.notes"), actor="op"))
    principal = agent_service.bind_principal(cred.credential_id)
    assert principal.id == "agent:sales/assistant"

    run(agent_service.revoke(cred.credential_id, actor="op"))
    with pytest.raises(CredentialRevokedError):
        agent_service.bind_principal(cred.credential_id)
