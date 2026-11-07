# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""issue() — scoped per-agent credential written ONLY to the SecretStore (C1)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ne.contracts.testing.memory import MemorySecretStore
from ne.identity.agents import AgentCredentialService, CredentialState
from ne.identity.errors import CredentialAlreadyProvisionedError

from .conftest import run, sample_agent, sample_scope


def test_issue_writes_secret_only_into_store_at_v1(
    agent_service: AgentCredentialService, agent_store: MemorySecretStore
) -> None:
    agent = sample_agent()
    scope = sample_scope("read.notes")
    cred = run(agent_service.issue(agent, scope, actor="operator"))

    # Exactly one KV-v2 path holding one version, at agents/<dept>/<agent>.
    assert run(agent_store.list_secrets("")) == ["agents/sales/assistant"]
    version = run(agent_store.read("agents/sales/assistant"))
    assert version.version == 1
    assert cred.secret_path == "agents/sales/assistant"
    assert cred.secret_version == 1
    assert cred.credential_id == "sales/assistant/v1"
    assert cred.state is CredentialState.ACTIVE
    # The token is present in the STORE only.
    assert version.data["secret"]


def test_issued_credential_is_frozen_and_carries_no_secret_material(
    agent_service: AgentCredentialService, agent_store: MemorySecretStore
) -> None:
    cred = run(agent_service.issue(sample_agent(), sample_scope("read.notes"), actor="op"))
    token = run(agent_service.resolve_secret(cred.credential_id)).get_secret_value()

    # Frozen model.
    with pytest.raises(ValidationError):
        cred.state = CredentialState.REVOKED  # type: ignore[misc]

    # No secret material in repr or serialized model.
    assert token not in repr(cred)
    assert token not in json.dumps(cred.model_dump(mode="json"))
    assert cred.scope.tools == ("read.notes",)


def test_empty_scope_allows_no_tool(agent_service: AgentCredentialService) -> None:
    cred = run(agent_service.issue(sample_agent(), sample_scope(), actor="op"))
    assert cred.scope.tools == ()
    assert cred.scope.allows_tool("read.notes") is False
    assert cred.scope.allows_tool("anything") is False


def test_second_issue_for_live_agent_raises(agent_service: AgentCredentialService) -> None:
    agent = sample_agent()
    run(agent_service.issue(agent, sample_scope("read.notes"), actor="op"))
    with pytest.raises(CredentialAlreadyProvisionedError):
        run(agent_service.issue(agent, sample_scope("read.notes"), actor="op"))


def test_resolve_secret_roundtrips_and_masks(agent_service: AgentCredentialService) -> None:
    cred = run(agent_service.issue(sample_agent(), sample_scope("read.notes"), actor="op"))
    secret = run(agent_service.resolve_secret(cred.credential_id))
    # The masked repr never shows the value; get_secret_value round-trips the store token.
    assert repr(secret) == "SecretStr('**********')"
    assert secret.get_secret_value()
    assert secret.get_secret_value() not in repr(secret)
