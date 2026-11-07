# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Governed, reference-bound human principal attribution.

The reverted PR #400 accepted arbitrary principal ids and became a directory
existence/name oracle.  This route accepts only opaque references to records the
reader's existing surface role may read.  It derives the principals server-side
from the pending approval queue or audit chain, then consults the existing
``PrincipalStore`` mirror.  There is no list/search/raw-id path.
"""

from __future__ import annotations

import functools
import json
from typing import Annotated

import anyio.to_thread
from fastapi import APIRouter, Depends

from ne.api.authz import emit_surface_audit, require_role
from ne.api.deps import ApiState, api_state, current_principal
from ne.api.wire import (
    ResolvedPrincipalsView,
    ResolvedPrincipalView,
    ResolvePrincipalReferencesRequest,
)
from ne.audit import records_by_event_ids
from ne.contracts.types import Principal
from ne.schema import AuditOutcome

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]
_RESOURCE = "api:principal"


def _directory_roles(state: ApiState) -> tuple[str, ...]:
    return tuple(dict.fromkeys(state.settings.ops_roles + state.settings.approver_roles))


async def _require_some_directory_scope(principal: _Principal, state: _State) -> None:
    """Reject callers outside both owning surfaces before body validation."""
    await require_role(
        principal,
        _directory_roles(state),
        sink=state.sink,
        audit_action="api.principal.unauthorized",
        resource=_RESOURCE,
        http_detail="not authorized to resolve visible principals",
    )


async def _visible_principals(
    body: ResolvePrincipalReferencesRequest,
    principal: Principal,
    state: ApiState,
) -> tuple[list[str], list[str]]:
    references = sorted(set(body.references))
    if body.scope == "approvals":
        await require_role(
            principal,
            state.settings.approver_roles,
            sink=state.sink,
            audit_action="api.principal.unauthorized",
            resource=_RESOURCE,
            http_detail="not authorized to resolve approval principals",
        )
        rows = await state.gate.list_pending()
        by_reference = {row.request_id: row.principal_id for row in rows}
    else:
        await require_role(
            principal,
            state.settings.ops_roles,
            sink=state.sink,
            audit_action="api.principal.unauthorized",
            resource=_RESOURCE,
            http_detail="not authorized to resolve audit principals",
        )
        records = await anyio.to_thread.run_sync(
            functools.partial(records_by_event_ids, state.audit_engine, references)
        )
        by_reference = {record.event_id: record.actor for record in records}
    visible_references = [reference for reference in references if reference in by_reference]
    principal_ids = sorted({by_reference[reference] for reference in visible_references})
    return visible_references, principal_ids


@router.post(
    "/v1/principals/resolve",
    dependencies=[Depends(_require_some_directory_scope)],
)
async def resolve_principals(
    body: ResolvePrincipalReferencesRequest,
    principal: _Principal,
    state: _State,
) -> ResolvedPrincipalsView:
    requested_references = sorted(set(body.references))
    visible_references, principal_ids = await _visible_principals(body, principal, state)
    names = await anyio.to_thread.run_sync(
        functools.partial(state.perm_store.display_names, principal_ids)
    )
    resolved = [
        ResolvedPrincipalView(id=pid, display_name=names[pid][0], removed=names[pid][1])
        for pid in principal_ids
        if pid in names
    ]
    # Exact probed-record attribution, unlike the counts-only receipt reverted
    # in PR #401. Values remain redacted to opaque ids; no names cross the chain.
    await emit_surface_audit(
        state.sink,
        action="api.principal.resolve",
        resource=_RESOURCE,
        outcome=AuditOutcome.success,
        actor=principal.id,
        detail={
            "scope": body.scope,
            "references": json.dumps(requested_references, separators=(",", ":")),
            "visible_references": json.dumps(visible_references, separators=(",", ":")),
            "principal_ids": json.dumps(principal_ids, separators=(",", ":")),
            "resolved": str(len(resolved)),
        },
    )
    return ResolvedPrincipalsView(resolved=resolved)
