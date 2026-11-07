# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exception handlers — the library-error → HTTP-status map (spec §1).

Every non-2xx body is ``{"detail": "<string>"}`` because the console parses ONLY
a string ``detail`` (client.ts:137-148). FastAPI's default 422 body carries
``detail`` as a LIST — so a :class:`RequestValidationError` handler flattens it to
a SINGLE string. No catch-all ``Exception`` handler is registered: a genuine 500
stays Starlette's opaque default (no reason oracle, no stack traces on the wire).
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from ne.brain.reranker import RerankResponseError
from ne.brain_graph.errors import GraphStoreError
from ne.net.airgap import AirGapViolationError
from ne.runtime.errors import SessionDepartmentMismatchError

#: Backend-failure classes the brain router maps to a 502 (detail = class name
#: ONLY — never backend URLs / reprs). NE-BRAIN-T2-WIRING adds the Tier-2
#: ``GraphStoreError`` (its ``GraphAclError`` subclass surfaces the same way —
#: fail-closed) so a graph-backend fault is a 502, not an opaque 500.
#:
#: NE-SEARCH-LATENCY adds the two ways the rerank stage can refuse:
#: ``RerankResponseError`` (a malformed rerank-server response — a ``ValueError``
#: subclass, which was NOT in this tuple and so escaped as an opaque 500) and
#: ``AirGapViolationError`` (the per-request air-gap re-check declining to send
#: query/chunk text to an allowlisted external host while the gap is engaged).
#: Both carry field-only messages, and the wire sees only the class name anyway.
_BACKEND_ERRORS = (
    httpx.HTTPError,
    OSError,
    GraphStoreError,
    RerankResponseError,
    AirGapViolationError,
)


def _flatten_validation(exc: RequestValidationError) -> str:
    """Render a validation error as a SINGLE string (console parses string detail)."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = str(err.get("msg", "invalid"))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "validation error"


def install_error_handlers(app: FastAPI) -> None:
    """Register the ne-api exception handlers on ``app`` (the inner FastAPI app)."""

    async def _on_validation(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        return JSONResponse(status_code=422, content={"detail": _flatten_validation(exc)})

    async def _on_department_mismatch(_request: Request, exc: Exception) -> JSONResponse:
        # The library message is deliberately safe (department names only).
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    app.add_exception_handler(RequestValidationError, _on_validation)
    app.add_exception_handler(SessionDepartmentMismatchError, _on_department_mismatch)
