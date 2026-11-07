# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The trace read routes (NE-167a + B11) — ops-gated, server-authoritative.

``GET /v1/trace/{trace_id}`` reuses the SAME ``_require_ops`` dependency as
``/v1/audit/*`` (empty ``ApiSettings.ops_roles`` ⇒ 403 for everyone, fail closed,
audited ``api.audit.unauthorized``) and returns the assembled
:class:`~ne.api.trace_assembly.TraceAssemblyView` for one agent run: a nested
typed span tree, per-model cost/token aggregates, a run rollup, and a
server-computed chain-verification verdict.

``GET /v1/traces`` (B11, observability plan §6 U1) is the run INDEX over the
same chain — same ``_require_ops`` gate, same one-hop threadpool discipline.
Query params, response fields, and every derivation rule are specified in
:mod:`ne.api.trace_index` (the module docstring is the contract). The keyset
cursor ``before`` is ``"{started_at ISO-8601}~{trace_id}"`` — the last row's
own two fields joined with ``~`` (a character no ISO timestamp contains);
malformed ⇒ 422, never a silent full restart.

Thin-client / server-authoritative (SPEC C7): every cost sum, duration, nesting
decision, and verification verdict is computed here, in
:func:`ne.api.trace_assembly.assemble_trace` /
:func:`ne.api.trace_index.query_trace_index` — the response is render-only
data. Both read ONLY enumerated identifier/enum/count/delta/model-lane/cost
keys off the underlying audit payloads; they never surface ACL label values,
principal labels, query text, or message content (C7, no-ACL-to-client; C12 —
the index additionally carries no free-text field at all).

Sync SQL (``replay`` + ``verify_chain``, inside ``assemble_trace`` /
``query_trace_index``) runs in ONE ``anyio.to_thread`` hop, the
``routers/audit.py`` discipline.
"""

from __future__ import annotations

import functools
from datetime import datetime
from typing import Annotated

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, Query

from ne.api.deps import ApiState, api_state, current_principal
from ne.api.routers.audit import _parse_iso, _require_ops
from ne.api.trace_assembly import TraceAssemblyView, assemble_trace
from ne.api.trace_index import OUTCOME_FILTERS, query_trace_index
from ne.api.wire import TraceIndexView
from ne.contracts.types import Principal

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]


def _parse_before(value: str) -> tuple[datetime, str]:
    """Parse the ``before`` keyset cursor (``"{started_at}~{trace_id}"``).

    ``partition`` on the FIRST ``~`` is unambiguous: ISO-8601 never contains
    ``~``, so a trace_id containing one still round-trips intact.
    """
    iso, sep, trace_id = value.partition("~")
    if not sep or not trace_id:
        raise HTTPException(status_code=422, detail="before: expected '{started_at}~{trace_id}'")
    return _parse_iso(iso, "before"), trace_id


@router.get("/v1/traces")
async def list_traces(
    principal: _Principal,
    state: _State,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    actor: str | None = None,
    task: str | None = None,
    model: str | None = None,
    lane: str | None = None,
    config_hash: str | None = None,
    outcome: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: str | None = None,
) -> TraceIndexView:
    await _require_ops(principal, state)
    start = _parse_iso(from_, "from") if from_ is not None else None
    end = _parse_iso(to, "to") if to is not None else None
    if outcome is not None and outcome not in OUTCOME_FILTERS:
        raise HTTPException(
            status_code=422,
            detail=f"outcome: must be one of {', '.join(OUTCOME_FILTERS)}",
        )
    cursor = _parse_before(before) if before is not None else None
    return await anyio.to_thread.run_sync(
        functools.partial(
            query_trace_index,
            state.audit_engine,
            start=start,
            end=end,
            actor=actor,
            task=task,
            model=model,
            lane=lane,
            config_hash=config_hash,
            outcome=outcome,
            limit=limit,
            before=cursor,
        )
    )


@router.get("/v1/trace/{trace_id}")
async def get_trace(
    principal: _Principal,
    state: _State,
    trace_id: str,
) -> TraceAssemblyView:
    await _require_ops(principal, state)
    view = await anyio.to_thread.run_sync(
        functools.partial(assemble_trace, state.audit_engine, trace_id)
    )
    if view is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return view
