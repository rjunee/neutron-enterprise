# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Flowise add-on routes (NE-088) — the governed seam for the optional Flowise add-on.

C6 STORY: a Flowise flow is **just another caller** of THE process-wide
:class:`~ne.runtime.middleware.GovernedToolExecutor`. This router creates NO new
governance path — it holds no tool handler, constructs no second executor, and
never touches the PDP, the approval gate, or the sink directly except to layer the
same redacted ``api.*`` surface record every other ne-api surface layers. It maps
the executor's library errors to HTTP status codes and nothing more. So a Flowise
tool call gets the EXACT same ``guardrails → PDP decide → stricter-of tier HITL →
audit`` path as a first-party chat-turn tool call (proven by the governance-parity
suite in ``packages/ne-api/tests/test_flowise_parity.py``):

  * a PDP-denied tool → 403 (there is no bypass);
  * a tier-2/3 tool BLOCKS until a named human approves (same DurableApprovalGate);
  * an unknown tool → 404 (the executor already audited ``tool.unknown``);
  * an invalid typed input for a write action → 422 (already audited
    ``tool.validate[denied]`` pre-gate);
  * every call is audited by the GOVERNED events the executor emits unchanged.

Like the chat route, the verified-bearer principal is set on :data:`TURN_PRINCIPAL`
before the executor call so principal-reading bindings (e.g. verified-answer tools)
behave identically to a first-party turn, and reset in ``finally``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ne.api.authz import emit_surface_audit
from ne.api.deps import ApiState, api_state, current_principal
from ne.api.tools import TURN_PRINCIPAL
from ne.contracts.types import Principal
from ne.govern.errors import GuardrailViolation
from ne.runtime.errors import (
    ActionValidationError,
    ApprovalNotGrantedError,
    ToolDeniedError,
    ToolExecutionError,
    ToolNotRegisteredError,
)
from ne.schema import AuditOutcome

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]


def _plain(value: Any) -> Any:
    """Deep-convert a frozen (``FrozenDict``/tuple) tool ``input_schema`` to plain
    JSON-serializable types — a ``ToolSpec.input_schema`` nests ``FrozenDict``s that
    pydantic cannot serialize directly."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class FlowiseToolSpec(BaseModel):
    """A governed tool's SPEC as seen by a flow author (never the handler)."""

    name: str
    description: str
    tier: int
    input_schema: dict[str, Any]


class FlowiseCatalog(BaseModel):
    """The catalog of governed tools a Flowise flow may call."""

    tools: list[FlowiseToolSpec]


class FlowiseExecuteRequest(BaseModel):
    """A Flowise tool-call request — a tool name plus its args (nothing else)."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class FlowiseExecuteResponse(BaseModel):
    """The governed tool result rendered back to the flow."""

    tool: str
    result: str


@router.get("/v1/flowise/tools")
async def list_tools(_principal: _Principal, state: _State) -> FlowiseCatalog:
    """List the governed tool SPECS (never handlers) a flow may call.

    Specs only — the ``brain-search`` read convention: no audit on the read, and
    the response carries no handler/callable field (structurally impossible).
    """
    return FlowiseCatalog(
        tools=[
            FlowiseToolSpec(
                name=spec.name,
                description=spec.description,
                tier=int(spec.tier),
                input_schema=_plain(spec.input_schema),
            )
            for spec in state.executor.tool_specs
        ]
    )


@router.post("/v1/flowise/tools/execute")
async def execute_tool(
    body: FlowiseExecuteRequest,
    principal: _Principal,
    state: _State,
) -> FlowiseExecuteResponse:
    """Run a tool through THE governed executor (C6) — Flowise is just another caller.

    The GOVERNED events (``pdp.decide`` / ``tool.validate`` / ``tool.approval`` /
    ``tool.execute`` / ``tool.unknown``) come from the executor UNCHANGED — that is
    what the parity suite compares against a first-party turn. We layer ONE redacted
    ``api.flowise.execute`` surface record on top (ids/tool/error-class only), then
    map the executor's library errors to HTTP. Unexpected errors (a fail-closed
    audit-append failure, an approval-integrity violation) propagate WITHOUT a
    misleading success record and become a 500 via the shared error handlers.
    """
    # Set the turn principal from the VERIFIED bearer so principal-reading bindings
    # behave identically to a first-party turn; reset in finally (C6 fail-closed).
    token = TURN_PRINCIPAL.set(principal)
    try:
        try:
            result = await state.executor.execute(body.tool, body.args, principal=principal)
        except ToolNotRegisteredError as exc:
            # The executor already audited tool.unknown; map to 404.
            await _surface(state, principal, body.tool, AuditOutcome.denied, exc)
            raise HTTPException(status_code=404, detail="tool not registered") from exc
        except ToolDeniedError as exc:
            await _surface(state, principal, body.tool, AuditOutcome.denied, exc)
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ApprovalNotGrantedError as exc:
            await _surface(state, principal, body.tool, AuditOutcome.denied, exc)
            raise HTTPException(status_code=403, detail=f"approval not granted: {exc}") from exc
        except GuardrailViolation as exc:
            await _surface(state, principal, body.tool, AuditOutcome.denied, exc)
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ActionValidationError as exc:
            # Pre-gate typed-input refusal (NE-ACTION-VALIDATE-BEFORE-GATE): the executor
            # already audited tool.validate[denied] BEFORE any approval row; a refusal is
            # denied, never error (error = ToolExecutionError/502). str(exc) names FIELD
            # names only (C12) — safe to surface as the detail.
            await _surface(state, principal, body.tool, AuditOutcome.denied, exc)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ToolExecutionError as exc:
            # Never leak the raw handler error — 502 with a fixed detail.
            await _surface(state, principal, body.tool, AuditOutcome.error, exc)
            raise HTTPException(status_code=502, detail="tool execution failed") from exc
        await _surface(state, principal, body.tool, AuditOutcome.success, None)
        return FlowiseExecuteResponse(tool=body.tool, result=result)
    finally:
        TURN_PRINCIPAL.reset(token)


async def _surface(
    state: ApiState,
    principal: Principal,
    tool: str,
    outcome: AuditOutcome,
    exc: Exception | None,
) -> None:
    """Emit the single redacted ``api.flowise.execute`` surface record (surface=flowise)."""
    detail: dict[str, str] = {"tool": tool}
    if exc is not None:
        detail["error_class"] = type(exc).__name__
    await emit_surface_audit(
        state.sink,
        action="api.flowise.execute",
        resource=f"flowise:{tool}",
        outcome=outcome,
        actor=principal.id,
        detail=detail,
        surface="flowise",
    )
