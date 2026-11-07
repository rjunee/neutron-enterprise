# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-identity."""


def test_import_and_version() -> None:
    import ne.identity

    assert ne.identity.__version__ == "0.1.0"


def test_import_agents_subpackage() -> None:
    from ne.identity.agents import (
        AgentCredential,
        AgentCredentialService,
        AgentScopeGuardrail,
        binding,
        config,
        create_agent_credential_service,
        guardrail,
        model,
        pool,
        service,
    )

    assert service.AgentCredentialService is AgentCredentialService
    assert model.AgentCredential is AgentCredential
    assert guardrail.AgentScopeGuardrail is AgentScopeGuardrail
    assert callable(create_agent_credential_service)
    assert binding.CLAIM_DEPARTMENT == "department"
    assert config.AgentCredentialConfig().secret_prefix == "agents"
    assert pool.CooldownReason.MANUAL == "manual"


def test_agents_reexported_from_top_level() -> None:
    import ne.identity as ni

    for name in (
        "AgentCredential",
        "AgentCredentialConfig",
        "AgentCredentialService",
        "AgentIdentitySettings",
        "AgentRef",
        "AgentScopeGuardrail",
        "CredentialScope",
        "CredentialState",
        "bind_agent_principal",
        "create_agent_credential_service",
    ):
        assert name in ni.__all__
        assert hasattr(ni, name)
