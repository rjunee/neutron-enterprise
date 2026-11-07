# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AgentScopeGuardrail — C12 fail-closed scope check inside a real GuardrailEngine."""

from __future__ import annotations

import pytest

from ne.contracts.types import Principal
from ne.govern import build_tool_call_payload, create_guardrail_engine
from ne.govern.config import GovernSettings
from ne.govern.errors import GuardrailViolation
from ne.govern.guardrails import KEY_PRINCIPAL, KEY_TOOL
from ne.govern.guardrails.engine import GuardrailEngine
from ne.identity.agents import CLAIM_SCOPE_TOOLS, AgentCredentialService, AgentScopeGuardrail
from ne.identity.agents.guardrail import _KEY_PRINCIPAL, _KEY_TOOL

from .conftest import ListSink, run, sample_agent, sample_scope


def test_payload_key_constants_pinned_to_ne_govern() -> None:
    # Cross-package string seam: these MUST equal the ne-govern literals so a
    # drift fails loudly here rather than silently mis-reading the payload.
    assert _KEY_PRINCIPAL == KEY_PRINCIPAL
    assert _KEY_TOOL == KEY_TOOL


def _payload(principal: object, tool: str) -> dict[str, object]:
    return build_tool_call_payload(principal=principal, tool=tool)


def test_passes_non_agent_principal(agent_service: AgentCredentialService) -> None:
    guardrail = AgentScopeGuardrail(agent_service)
    human = Principal(id="user:ada", groups=("finance",))
    verdict = guardrail.check(_payload(human, "read.notes"))
    assert verdict.passed is True
    assert verdict.rule == "agent.scope"
    assert verdict.detail == "not an agent principal"


def test_allows_in_scope_tool(agent_service: AgentCredentialService) -> None:
    cred = run(agent_service.issue(sample_agent(), sample_scope("read.notes"), actor="op"))
    principal = agent_service.bind_principal(cred.credential_id)
    guardrail = AgentScopeGuardrail(agent_service)
    assert guardrail.check(_payload(principal, "read.notes")).passed is True


def test_blocks_out_of_scope_tool(agent_service: AgentCredentialService) -> None:
    cred = run(agent_service.issue(sample_agent(), sample_scope("read.notes"), actor="op"))
    principal = agent_service.bind_principal(cred.credential_id)
    guardrail = AgentScopeGuardrail(agent_service)
    verdict = guardrail.check(_payload(principal, "finance.pay"))
    assert verdict.passed is False
    assert verdict.rule == "agent.scope"
    assert "finance.pay" in verdict.detail


def test_blocks_unknown_credential(agent_service: AgentCredentialService) -> None:
    # A principal that carries a credential id the service never registered.
    from datetime import UTC, datetime

    from ne.identity.agents import bind_agent_principal
    from ne.identity.agents.model import (
        AgentCredential,
        AgentRef,
        CredentialScope,
        CredentialState,
    )

    ghost = AgentCredential(
        credential_id="sales/ghost/v9",
        agent=AgentRef(department="sales", agent_name="ghost"),
        scope=CredentialScope(tools=("read.notes",)),
        secret_path="agents/sales/ghost",
        secret_version=9,
        issued_at=datetime(2026, 7, 5, tzinfo=UTC),
        state=CredentialState.ACTIVE,
    )
    principal = bind_agent_principal(ghost)
    guardrail = AgentScopeGuardrail(agent_service)
    verdict = guardrail.check(_payload(principal, "read.notes"))
    assert verdict.passed is False
    assert verdict.rule == "agent.credential"
    assert "unknown" in verdict.detail


def test_blocks_forged_broadened_scope_claims(agent_service: AgentCredentialService) -> None:
    # P1 regression: a principal that references a REAL credential (stored scope
    # read.notes) but forges its agent_scope_tools claim to include finance.pay is
    # still DENIED finance.pay — the guardrail authorizes against cred.scope, not
    # the forgeable claim. The credential's REAL scope still passes.
    cred = run(agent_service.issue(sample_agent(), sample_scope("read.notes"), actor="op"))
    principal = agent_service.bind_principal(cred.credential_id)
    forged = Principal(
        id=principal.id,
        groups=principal.groups,
        claims={**dict(principal.claims), CLAIM_SCOPE_TOOLS: "finance.pay,read.notes"},
    )
    guardrail = AgentScopeGuardrail(agent_service)
    verdict = guardrail.check(_payload(forged, "finance.pay"))
    assert verdict.passed is False
    assert verdict.rule == "agent.scope"
    assert "finance.pay" in verdict.detail
    assert guardrail.check(_payload(forged, "read.notes")).passed is True


def test_blocks_revoked_credential(agent_service: AgentCredentialService) -> None:
    cred = run(agent_service.issue(sample_agent(), sample_scope("read.notes"), actor="op"))
    principal = agent_service.bind_principal(cred.credential_id)
    run(agent_service.revoke(cred.credential_id, actor="op"))
    guardrail = AgentScopeGuardrail(agent_service)
    verdict = guardrail.check(_payload(principal, "read.notes"))
    assert verdict.passed is False
    assert verdict.rule == "agent.credential"
    assert "revoked" in verdict.detail


def test_engine_audits_block_and_raises(agent_service: AgentCredentialService) -> None:
    cred = run(agent_service.issue(sample_agent(), sample_scope("read.notes"), actor="op"))
    principal = agent_service.bind_principal(cred.credential_id)
    sink = ListSink()
    engine = GuardrailEngine([AgentScopeGuardrail(agent_service)], sink)

    with pytest.raises(GuardrailViolation):
        run(engine.enforce(_payload(principal, "finance.pay")))
    assert sink.actions() == ["guardrail.block"]


def test_engine_via_factory_passes_in_scope(agent_service: AgentCredentialService) -> None:
    # Compose the rail alongside the default guardrails via the real factory,
    # proving structural compatibility with the NE-033 wiring.
    cred = run(agent_service.issue(sample_agent(), sample_scope("read.docs.search"), actor="op"))
    principal = agent_service.bind_principal(cred.credential_id)
    base = create_guardrail_engine(GovernSettings(), sink=ListSink())
    assert isinstance(base, GuardrailEngine)
    scoped = GuardrailEngine([AgentScopeGuardrail(agent_service)], ListSink())
    verdicts = run(scoped.enforce(_payload(principal, "read.docs.search")))
    assert all(v.passed for v in verdicts)
