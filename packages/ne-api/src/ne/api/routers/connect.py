# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Connector-sync status route (NE-132) — GET /v1/connect/status.

Surfaces per-connector freshness as TWO facts (when the sync cycle last RAN =
``last_checked_age_s``, and how old the newest record seen is = ``cursor_age_s``
— SPEC §12 item 9), the fail-closed ACL-mirror health, the last-sync audit seq,
and per-connector quarantine counts + the sanitized reason-category ledger — all
derived from the REAL connector-sync state (see :mod:`ne.api.connect_status`).
This is the live source behind the /connect Connectors view + its quarantine
vessel.

ACCESS DECISION (documented), per surface:

* ``GET /v1/connect/status`` — ANY authenticated principal. The rail routes a
  governed connector event to this stage on every authed view (design §1.6), and the
  payload carries deployment-level connector-health facts ONLY — no user data, no
  ACLs, no chunk content, no raw quarantine reason text (reasons are bucketed to an
  allowlisted category). This mirrors the posture.py / kpi.py precedent.
* ``GET /v1/connect/quarantine`` (NE-CONNECT-CSV) — ``ApiSettings.ops_roles`` ONLY,
  403 + audited otherwise (``api.connect.quarantine.unauthorized`` — the audit-log
  route's fail-closed model). A parked write is CUSTOMER DATA that failed
  validation; the drill-down serves the raw validator reason, the record identity,
  and the parked row (string values bounded server-side) because an inspectable
  quarantine is the lane's entire point — and serves them to nobody else.

Both read-only: no tool execution, no write path, requests have no body (C12). Like
the graph.py / posture.py read routes, the authn 401/403 auditing rides the identity
middleware + authz layer — no explicit sink append on the success path.

HONESTY (§0 law 5): every datum is derived from a real state source. A listed but
unconfigured connector remains inspectable with all-null health and no executable
Sync capability; a configured-but-never-synced connector also carries all-null
health. An unreadable state/quarantine file degrades to honest nulls — no code path
fabricates 'ok', a fresh age, or a zero count.
"""

from __future__ import annotations

import functools
import json
from typing import Annotated
from uuid import UUID

import anyio.to_thread
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response

from ne.api.authz import require_role
from ne.api.connect_status import derive_connect_status, derive_quarantine_items
from ne.api.connect_sync import sync_request_tool_name
from ne.api.deps import ApiState, api_state, current_principal
from ne.api.wire import (
    AuthorityAttemptsView,
    AuthorityAttemptView,
    ConnectorStatusView,
    ConnectQuarantineView,
    ConnectStatusView,
    ConnectSyncCountsView,
    ConnectSyncRequestView,
    ConnectSyncTriggerRequest,
    QuarantinedItemView,
    QuarantineReasonView,
)
from ne.connect.authority_attempts import (
    AuthorityAttemptNotPendingError,
    AuthorityAttemptRead,
    UnknownAuthorityAttemptError,
)
from ne.contracts.types import Principal
from ne.govern.errors import GuardrailViolation
from ne.runtime.errors import (
    ActionValidationError,
    ToolDeniedError,
    ToolExecutionError,
)
from ne.runtime.scheduler import ManualJobRequest

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]
_NO_STORE = "private, no-store"


def _attempt_not_found() -> HTTPException:
    """Keep unknown, foreign, stale and completed ids indistinguishable."""
    return HTTPException(
        status_code=404,
        detail="authority attempt not found",
        headers={"Cache-Control": _NO_STORE},
    )


def _authority_attempt_view(attempt: AuthorityAttemptRead) -> AuthorityAttemptView:
    """Project the closed safe metadata set field-by-field."""
    return AuthorityAttemptView.model_validate(
        {
            "attempt_id": attempt.attempt_id,
            "source_kind": attempt.source_kind,
            "source_binding": attempt.source_binding,
            "scope_hash": attempt.scope_hash,
            "identity_hash": attempt.identity_hash,
            "stage": attempt.stage.value,
            "source_revision": attempt.source_revision,
            "contract_epoch": attempt.contract_epoch,
            "prior_artifact_id": attempt.prior_artifact_id,
            "receipt_id": attempt.receipt_id,
            "receipt_hash": attempt.receipt_hash,
        }
    )


async def _require_authority_ops(principal: Principal, state: ApiState) -> None:
    await require_role(
        principal,
        state.settings.ops_roles,
        sink=state.sink,
        audit_action="api.connect.authority.unauthorized",
        resource="connect://authority-attempts",
        http_detail="not authorized for authority attempts",
    )


@router.get("/v1/connect/authority/attempts")
async def list_authority_attempts(
    response: Response,
    principal: _Principal,
    state: _State,
) -> AuthorityAttemptsView:
    """List only current pending attempts in stable server-owned order."""
    response.headers["Cache-Control"] = _NO_STORE
    await _require_authority_ops(principal, state)
    reader = state.authority_attempts
    if reader is None:
        return AuthorityAttemptsView(attempts=[])
    attempts = await anyio.to_thread.run_sync(reader.pending)
    return AuthorityAttemptsView(attempts=[_authority_attempt_view(item) for item in attempts])


@router.get("/v1/connect/authority/attempts/{attempt_id}")
async def read_authority_attempt(
    attempt_id: str,
    response: Response,
    principal: _Principal,
    state: _State,
) -> AuthorityAttemptView:
    """Read one current configured attempt without revealing misses."""
    response.headers["Cache-Control"] = _NO_STORE
    await _require_authority_ops(principal, state)
    reader = state.authority_attempts
    if reader is None:
        raise _attempt_not_found()
    try:
        attempt = await anyio.to_thread.run_sync(reader.read, attempt_id)
    except (UnknownAuthorityAttemptError, AuthorityAttemptNotPendingError) as exc:
        raise _attempt_not_found() from exc
    return _authority_attempt_view(attempt)


@router.post("/v1/connect/authority/attempts/{attempt_id}/retry", status_code=202)
async def retry_authority_attempt(
    attempt_id: str,
    response: Response,
    principal: _Principal,
    state: _State,
    _body: Annotated[ConnectSyncTriggerRequest | None, Body()] = None,
) -> AuthorityAttemptView:
    """Queue the canonical source sync, then re-read the same current attempt."""
    response.headers["Cache-Control"] = _NO_STORE
    await _require_authority_ops(principal, state)
    reader = state.authority_attempts
    if reader is None:
        raise _attempt_not_found()
    try:
        before = await anyio.to_thread.run_sync(reader.read, attempt_id)
    except (UnknownAuthorityAttemptError, AuthorityAttemptNotPendingError) as exc:
        raise _attempt_not_found() from exc

    action = sync_request_tool_name(before.source_binding)
    executable_actions = frozenset(spec.name for spec in state.executor.tool_specs)
    if action not in executable_actions:
        raise _attempt_not_found()
    try:
        output = await state.executor.execute(action, {}, principal=principal)
        queued = json.loads(output)
        if not isinstance(queued, dict):
            raise TypeError("sync request metadata must be an object")
        if not bool(queued.get("created")):
            response.status_code = 200
    except (ToolDeniedError, GuardrailViolation) as exc:
        raise HTTPException(
            status_code=403,
            detail=str(exc),
            headers={"Cache-Control": _NO_STORE},
        ) from exc
    except ActionValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
            headers={"Cache-Control": _NO_STORE},
        ) from exc
    except (ToolExecutionError, TypeError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=503,
            detail="the authority retry could not be queued",
            headers={"Cache-Control": _NO_STORE},
        ) from exc

    # Queue BEFORE the final read: the response reflects the attempt after the
    # retry request was durably accepted, never a pre-queue snapshot presented as
    # proof that retry happened.
    try:
        after = await anyio.to_thread.run_sync(reader.read, attempt_id)
    except (UnknownAuthorityAttemptError, AuthorityAttemptNotPendingError) as exc:
        raise _attempt_not_found() from exc
    return _authority_attempt_view(after)


@router.get("/v1/connect/status")
async def connect_status(
    principal: _Principal,
    state: _State,
) -> ConnectStatusView:
    # ONE threadpool hop — the derivation opens local sqlite files + queries the
    # audit engine (blocking IO); never block the event loop (the posture.py hop).
    status = await anyio.to_thread.run_sync(
        functools.partial(
            derive_connect_status,
            state.connect_sync_settings,
            state.settings,
            state.audit_engine,
        )
    )
    executable_actions = frozenset(spec.name for spec in state.executor.tool_specs)
    sync_allowed = bool(
        state.settings.ops_roles and not set(principal.groups).isdisjoint(state.settings.ops_roles)
    )
    return ConnectStatusView(
        connectors=[
            ConnectorStatusView(
                id=c.id,
                kind=c.kind,
                posture=c.posture,
                cursor_age_s=c.cursor_age_s,
                last_checked_age_s=c.last_checked_age_s,
                acl_mirror=c.acl_mirror,
                last_sync_seq=c.last_sync_seq,
                quarantined_count=c.quarantined_count,
                sync_allowed=sync_allowed and sync_request_tool_name(c.id) in executable_actions,
            )
            for c in status.connectors
        ],
        quarantine_total=status.quarantine_total,
        quarantine_reasons=[
            QuarantineReasonView(reason=reason, count=count)
            for reason, count in status.quarantine_reasons
        ],
    )


def _public_source_for_request(request: ManualJobRequest, state: ApiState) -> str | None:
    settings = state.connect_sync_settings
    if settings is None:
        return None
    expected_job = request.name.removeprefix("connect-sync-")
    if expected_job in settings.sources and request.name == f"connect-sync-{expected_job}":
        return expected_job
    return None


def _sync_request_view(request: ManualJobRequest, *, source: str) -> ConnectSyncRequestView:
    counts: ConnectSyncCountsView | None = None
    error_class = request.error_class
    if request.status == "succeeded" and request.output:
        try:
            counts = ConnectSyncCountsView.model_validate(json.loads(request.output))
        except (TypeError, ValueError):
            # A terminal green request without the connector's exact counts-only
            # report is not a successful Sync now result.
            error_class = "InvalidTerminalResult"
    status = request.status
    if status == "succeeded" and counts is None:
        status = "failed"
    return ConnectSyncRequestView(
        request_id=request.request_id,
        source=source,
        status=status,
        created_at=request.created_at,
        expires_at=request.expires_at,
        counts=counts,
        error_class=error_class,
    )


@router.post("/v1/connect/{source}/sync", status_code=202)
async def request_connect_sync(
    source: str,
    response: Response,
    principal: _Principal,
    state: _State,
    _body: Annotated[ConnectSyncTriggerRequest | None, Body()] = None,
) -> ConnectSyncRequestView:
    """Queue one deterministic, governed sync on the existing worker scheduler."""
    await require_role(
        principal,
        state.settings.ops_roles,
        sink=state.sink,
        audit_action="api.connect.sync.unauthorized",
        resource="connect://sync",
        http_detail="not authorized to sync sources",
    )
    settings = state.connect_sync_settings
    store = state.connect_sync_requests
    executable_actions = frozenset(spec.name for spec in state.executor.tool_specs)
    if (
        settings is None
        or store is None
        or source not in settings.sources
        or sync_request_tool_name(source) not in executable_actions
    ):
        raise HTTPException(status_code=404, detail="source is not syncable")
    try:
        output = await state.executor.execute(
            sync_request_tool_name(source), {}, principal=principal
        )
        metadata = json.loads(output)
        request = await anyio.to_thread.run_sync(store.get, str(metadata["request_id"]))
    except (ToolDeniedError, GuardrailViolation) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ActionValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ToolExecutionError, TypeError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=503,
            detail="the sync request could not be recorded",
        ) from exc
    if request is None:
        raise HTTPException(status_code=503, detail="the sync request is not readable")
    if not bool(metadata.get("created")):
        response.status_code = 200
    return _sync_request_view(request, source=source)


@router.get("/v1/connect/sync/{request_id}")
async def read_connect_sync(
    request_id: UUID,
    principal: _Principal,
    state: _State,
) -> ConnectSyncRequestView:
    """Poll a request as its requester or an ops-authorized principal."""
    store = state.connect_sync_requests
    if store is None:
        raise HTTPException(status_code=404, detail="sync request not found")
    request = await anyio.to_thread.run_sync(store.get, str(request_id))
    if request is None:
        raise HTTPException(status_code=404, detail="sync request not found")
    is_ops = bool(
        state.settings.ops_roles and not set(principal.groups).isdisjoint(state.settings.ops_roles)
    )
    if principal.id != request.requester_id and not is_ops:
        # 404-equivalence: an unauthorized caller learns nothing about whether
        # this opaque request exists or is active.
        raise HTTPException(status_code=404, detail="sync request not found")
    source = _public_source_for_request(request, state)
    if source is None:
        raise HTTPException(status_code=404, detail="sync request not found")
    return _sync_request_view(request, source=source)


@router.get("/v1/connect/quarantine")
async def connect_quarantine(
    principal: _Principal,
    state: _State,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ConnectQuarantineView:
    # Fail-closed role pre-check FIRST (403 + audited) — a parked write is
    # customer data; see the module docstring's access decision.
    await require_role(
        principal,
        state.settings.ops_roles,
        sink=state.sink,
        audit_action="api.connect.quarantine.unauthorized",
        resource="connect://quarantine",
        http_detail="not authorized for the quarantine drill-down",
    )
    result = await anyio.to_thread.run_sync(
        functools.partial(
            derive_quarantine_items,
            state.connect_sync_settings,
            state.settings,
            limit=limit,
        )
    )
    return ConnectQuarantineView(
        items=[
            QuarantinedItemView(
                item_id=item.item_id,
                source=item.source,
                record_id=item.record_id,
                reason=item.reason,
                category=item.category,
                shape=item.shape,
                created_at=item.created_at,
                payload=item.payload,
                payload_truncated=item.payload_truncated,
            )
            for item in result.items
        ],
        total=result.total,
    )
