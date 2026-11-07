# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ToolsetScopedExecutor — per-agent/per-job toolset gating (NE-034), ENFORCED not advisory.

A handler-less wrapper around the ONE :class:`GovernedToolExecutor` (or another
scoped wrapper) that RESTRICTS the callable tools to an explicit granted set. It is
the enforcement point for cron-as-isolated-agent per-job toolset gating and for a
sub-agent's toolset — an out-of-set call is audited + PDP-consulted + terminally
refused, and the handler is NEVER reached.

No-new-bypass argument (C6): this wrapper holds ZERO tool handlers and ZERO
bindings. Its ONLY route to running a tool is ``await self._inner.execute(...)``,
which is the single governed pipeline (guardrails → PDP → HITL → handler → audit).
An in-set call therefore runs the FULL C6 governance unchanged; an out-of-set call
never delegates and never reaches a handler. There is thus no code path off this
object that executes a tool without the middleware — the guardian's handler-call
source-scan stays middleware.py-only by construction.

Gating happens BEFORE delegation (C12 ordering preserved for in-set calls: the
delegated ``execute`` runs guardrails FIRST then the PDP exactly as NE-033 shipped
it — this wrapper never reorders or wraps guardrail outcomes).

Out-of-set semantics MIRROR the middleware unknown-tool branch: (1) emit
``tool.toolset_denied`` (fail-closed — an audit-append failure raises
:class:`RuntimeAuditError` and aborts BEFORE the PDP), (2) consult the PDP for the
governance record (``GovernedPdp`` audits ``pdp.decide`` by construction), then (3)
raise :class:`ToolsetViolationError` REGARDLESS of the decision effect — an
out-of-set tool never executes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from ne.contracts.governance import AuditSink, PolicyDecisionPoint, PolicyRequest
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import Principal
from ne.events import new_audit_event
from ne.runtime.errors import (
    RuntimeAuditError,
    ToolsetConfigurationError,
    ToolsetViolationError,
)
from ne.schema import AuditOutcome

_PROVENANCE_SOURCE = {"source": "ne-runtime", "component": "toolset"}


class _ToolExecutor(Protocol):
    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]: ...

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str: ...


class ToolsetScopedExecutor:
    """A handler-less, toolset-gated view over the ONE governed executor (C6)."""

    def __init__(
        self,
        inner: _ToolExecutor,
        *,
        granted: Iterable[str],
        sink: AuditSink,
        scope: str,
        pdp: PolicyDecisionPoint,
    ) -> None:
        if sink is None:
            raise TypeError("ToolsetScopedExecutor requires a non-None audit sink (C6)")
        if pdp is None:
            raise TypeError("ToolsetScopedExecutor requires a non-None pdp (C6)")
        self._inner = inner
        self._sink = sink
        self._scope = scope
        self._pdp = pdp
        available = {s.name for s in inner.tool_specs}
        granted_set = tuple(dict.fromkeys(granted))  # de-dup, preserve order
        # Fail loud: a grant naming a tool the inner executor does not expose is a
        # configuration error (a job/sub-agent must never silently lose a tool). An
        # EMPTY granted set is VALID and means ZERO tools (fail closed).
        unknown = [name for name in granted_set if name not in available]
        if unknown:
            raise ToolsetConfigurationError(
                f"toolset grant for scope {scope!r} references unregistered tools "
                f"{unknown!r}; grants must be a subset of the executor's tools "
                f"{sorted(available)!r}"
            )
        self._granted: frozenset[str] = frozenset(granted_set)

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """The inner specs FILTERED to the granted set (never handlers)."""
        return tuple(s for s in self._inner.tool_specs if s.name in self._granted)

    def _toolset_authority(self) -> tuple[AuditSink, PolicyDecisionPoint]:
        """Carry the same governance pair when toolset scopes are nested."""
        return self._sink, self._pdp

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        """Run ``tool`` IFF it is in the granted set; otherwise audit + PDP + refuse.

        In-set: delegates to the inner governed executor (the FULL C6 pipeline runs
        there). Out-of-set: mirrors the middleware unknown-tool branch — fail-closed
        ``tool.toolset_denied`` audit FIRST, then a PDP consultation for the record,
        then a terminal :class:`ToolsetViolationError` (the handler is unreachable).
        """
        if tool in self._granted:
            return await self._inner.execute(tool, args, principal=principal)

        # (a) Audit the out-of-set call FIRST (fail closed: if this append fails,
        # RuntimeAuditError propagates and we abort BEFORE the PDP or anything else).
        await self._emit(
            action="tool.toolset_denied",
            resource=f"tool:{tool}",
            outcome=AuditOutcome.denied,
            actor=principal.id,
            detail={"reason": "tool not in granted toolset", "scope": self._scope},
        )
        # (b) Consult governance for the record — GovernedPdp audits pdp.decide by
        # construction. The effect is IGNORED: an out-of-set tool never executes.
        await self._pdp.decide(
            PolicyRequest(
                principal=principal,
                action=tool,
                resource=f"tool:{tool}",
                context={"toolset_gated": True},
            )
        )
        # (c) Terminal refusal regardless of the decision effect (fail closed).
        raise ToolsetViolationError(
            f"tool {tool!r} is not in the granted toolset for scope {self._scope!r}; "
            "the call was audited and never executed (C6 fail closed)"
        )

    async def _emit(
        self,
        *,
        action: str,
        resource: str,
        outcome: AuditOutcome,
        actor: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        # Copies the middleware._emit pattern: event construction (which resolves the
        # FROZEN config hash) is OUTSIDE the try so ConfigHashNotFrozenError
        # propagates rather than being swallowed. detail carries ONLY tool names /
        # scopes / statuses — never raw arg values.
        event = new_audit_event(
            action=action,
            resource=resource,
            outcome=outcome,
            actor=actor,
            detail=json.dumps(detail) if detail is not None else None,
            provenance=dict(_PROVENANCE_SOURCE),
        )
        try:
            await self._sink.append(event)
        except Exception as exc:
            raise RuntimeAuditError(
                f"audit append failed for {action!r} on {resource!r}; "
                "result withheld (C6 fail closed)"
            ) from exc
