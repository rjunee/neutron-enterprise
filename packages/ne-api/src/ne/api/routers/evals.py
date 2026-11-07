# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Eval routes (spec §1.6 #11) — read-only eval-run listing, any authenticated user.

``GET /v1/eval/runs`` delegates to :meth:`ne.eval.store.EvalRunStore.list_runs`
(newest-first, already ordered server-side) via ``anyio.to_thread`` (the store is
sync). Eval results carry no user data, so ANY authenticated principal may read
(``current_principal`` present but no role gate). When the eval read engine is
unconfigured the route answers 503 (fail LOUD — never a silently-empty list).
"""

from __future__ import annotations

import functools
from typing import Annotated

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, Query

from ne.api.deps import ApiState, api_state, current_principal
from ne.api.wire import EvalRunsView, eval_run_view
from ne.contracts.types import Principal

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]


@router.get("/v1/eval/runs")
async def eval_runs(
    principal: _Principal,
    state: _State,
    suite: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> EvalRunsView:
    if state.eval_store is None:
        raise HTTPException(status_code=503, detail="eval store not configured")
    runs = await anyio.to_thread.run_sync(
        functools.partial(state.eval_store.list_runs, suite=suite, limit=limit)
    )
    return EvalRunsView(runs=[eval_run_view(r) for r in runs])
