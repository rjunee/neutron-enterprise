# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Brain routes (C7 surface, spec §1.3) — the ACL-trimmed read path.

Both routes call ONLY ``search``/``retrieve`` on the ONE composed retriever
(``RetrievalPipeline``-wrapped when the SPEC §3.1 rerank stage is configured — the
default — else the bare ``HybridRetriever``), whose acl-trimmed ``HybridRetriever``
is constructed with ``acl_filters=PrincipalAclFilter(expander)`` (never None). NO
``filters`` kwarg is ever forwarded from HTTP — the only filter in play is the ACL
filter. Responses map through :func:`retrieval_result_view`, whose ``ChunkView``
has exactly four fields and drops ``acl`` (structurally un-serializable). A
TEI/Qdrant backend failure becomes a 502 whose detail is the error CLASS NAME
only (never a backend URL).

NE-164 read-path audit + trace threading: ``brain_retrieve`` passes
``current_run_trace_id()`` (the NE-166 run-scoped correlation id, ``None``
outside any run scope) down to a ``HybridRetriever``/``RetrievalPipeline``
delegate via ``isinstance`` narrowing, so its read-path ``AuditEvent`` carries
the caller's trace id AS DATA — ne-brain itself gains NO ``ne.runtime`` import;
only this ne-api layer (which already depends on ne-runtime) reads the
ContextVar. T2 gap: a ``GraphRetriever``-wrapped delegate (Tier-2 graph reads)
falls through to the plain ``retrieve()`` call below with no trace_id and no
read-path event of its own — an explicitly scoped follow-up, not built here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from ne.api.deps import (
    ApiState,
    api_state,
    bind_authenticated_request_context,
    current_principal,
)
from ne.api.errors import _BACKEND_ERRORS
from ne.api.wire import (
    BrainRetrieveRequest,
    BrainSearchRequest,
    RetrievalResultView,
    retrieval_result_view,
)
from ne.brain.pipeline import RetrievalPipeline
from ne.brain.retriever import HybridRetriever
from ne.contracts.types import Principal
from ne.runtime.correlation import current_run_trace_id

router = APIRouter(dependencies=[Depends(bind_authenticated_request_context)])

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]


@router.post("/v1/brain/search")
async def brain_search(
    body: BrainSearchRequest,
    principal: _Principal,
    state: _State,
) -> RetrievalResultView:
    try:
        result = await state.current_retriever.search(
            body.query, principal=principal, top_k=body.top_k
        )
    except _BACKEND_ERRORS as exc:
        raise HTTPException(status_code=502, detail=type(exc).__name__) from exc
    return retrieval_result_view(result)


@router.post("/v1/brain/retrieve")
async def brain_retrieve(
    body: BrainRetrieveRequest,
    principal: _Principal,
    state: _State,
) -> RetrievalResultView:
    retriever = state.current_retriever
    try:
        if isinstance(retriever, HybridRetriever | RetrievalPipeline):
            result = await retriever.retrieve(
                body.chunk_ids, principal=principal, trace_id=current_run_trace_id()
            )
        else:
            # T2 gap (documented, not built): a GraphRetriever-wrapped delegate
            # drops trace_id and emits no read-path event of its own here.
            result = await retriever.retrieve(body.chunk_ids, principal=principal)
    except _BACKEND_ERRORS as exc:
        raise HTTPException(status_code=502, detail=type(exc).__name__) from exc
    return retrieval_result_view(result)
