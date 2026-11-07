# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AgentScopeGuardrail — a C12 deterministic scope check for agent principals.

Implements the frozen, sync ``ne.contracts.governance.Guardrail`` Protocol. It is
composed into the existing NE-033 ``GuardrailEngine`` at the composition root::

    from ne.govern import create_guardrail_engine
    engine = create_guardrail_engine(GovernSettings(), sink=sink)
    # or, adding this rail to the defaults:
    engine = GuardrailEngine([*default_guardrails, AgentScopeGuardrail(service)], sink=sink)

so a scope block is AUDITED by the engine (C6/C12) before any model judgment.

Behavior:

* a non-agent principal (human/service) PASSES this rail — those subjects are
  governed by the PDP, not this scope check;
* an agent principal FAILS CLOSED — an unknown / revoked / expired credential, or
  a tool outside the credential's least-privilege scope, is blocked;
* scope is authorized against the credential's STORED scope held by the service
  (``cred.scope``, looked up via :meth:`AgentCredentialService.validate`), NEVER
  against ``Principal.claims`` — a principal-claimed scope (``agent_scope_tools``)
  is forgeable and is treated as informational only;
* ``detail`` strings carry the tool name + credential id + state only, never
  secret material.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ne.contracts.governance import GuardrailVerdict
from ne.identity.agents.binding import credential_id_from_principal
from ne.identity.agents.service import AgentCredentialService
from ne.identity.errors import (
    CredentialExpiredError,
    CredentialNotFoundError,
    CredentialRevokedError,
)

# These mirror ne.govern.guardrails.KEY_PRINCIPAL / KEY_TOOL — the canonical
# tool-call-payload keys. Duplicated as local constants (no runtime ne-govern
# dependency); a dedicated test pins them equal to the ne-govern literals.
_KEY_PRINCIPAL = "principal"
_KEY_TOOL = "tool"

_RULE_SCOPE = "agent.scope"
_RULE_CREDENTIAL = "agent.credential"


class AgentScopeGuardrail:
    """Blocks out-of-scope tools and dead credentials for agent principals."""

    def __init__(self, service: AgentCredentialService) -> None:
        self._service = service

    def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict:
        principal = payload.get(_KEY_PRINCIPAL)
        credential_id = credential_id_from_principal(principal) if principal is not None else None
        if principal is None or credential_id is None:
            # Not an agent principal — humans/services are governed by the PDP.
            return GuardrailVerdict(passed=True, rule=_RULE_SCOPE, detail="not an agent principal")

        # Authorize against the AUTHORITATIVE stored credential scope, never the
        # forgeable principal claims. validate() is sync/guardrail-safe: it admits
        # ACTIVE and GRACE and raises for unknown/REVOKED/EXPIRED (applying the same
        # lazy GRACE->EXPIRED transition the old credential_state() path did).
        try:
            cred = self._service.validate(credential_id)
        except CredentialNotFoundError:
            return GuardrailVerdict(
                passed=False,
                rule=_RULE_CREDENTIAL,
                detail=f"unknown credential {credential_id}",
            )
        except CredentialRevokedError:
            return GuardrailVerdict(
                passed=False,
                rule=_RULE_CREDENTIAL,
                detail=f"credential {credential_id} is revoked",
            )
        except CredentialExpiredError:
            return GuardrailVerdict(
                passed=False,
                rule=_RULE_CREDENTIAL,
                detail=f"credential {credential_id} is expired",
            )

        tool = payload.get(_KEY_TOOL)
        if not isinstance(tool, str) or not cred.scope.allows_tool(tool):
            return GuardrailVerdict(
                passed=False,
                rule=_RULE_SCOPE,
                detail=f"tool {tool!r} not in scope for credential {credential_id}",
            )

        return GuardrailVerdict(
            passed=True,
            rule=_RULE_SCOPE,
            detail=f"tool {tool!r} in scope for credential {credential_id}",
        )
