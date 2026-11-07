# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Bearer auth for the ``/scim/v2`` mount — NOT the OIDC verifier.

The SCIM mount lives OUTSIDE ``build_identity``'s OIDC wrap (see ``ne.api.app``
step 12's ordering comment), so it needs its own gate: a single shared-secret
bearer token, compared via ``hmac.compare_digest`` (constant-time — no timing
oracle on a partial match). Missing/mismatched credentials return 401 with NO
distinguishing detail (``"unauthorized"`` either way) and a ``WWW-Authenticate:
Bearer`` header. Applied as a FastAPI dependency at ROUTER level so every SCIM
route is gated identically and a route cannot be added un-gated by omission.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

from fastapi import Header, HTTPException

__all__ = ["build_scim_bearer_dependency"]

_WWW_AUTHENTICATE = "Bearer"
_UNAUTHORIZED_DETAIL = "unauthorized"


def build_scim_bearer_dependency(token: str) -> Callable[..., Awaitable[None]]:
    """Return a FastAPI dependency enforcing ``Authorization: Bearer <token>``."""

    async def _require_bearer(authorization: str | None = Header(default=None)) -> None:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail=_UNAUTHORIZED_DETAIL,
                headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
            )
        supplied = authorization[len("Bearer ") :]
        if not hmac.compare_digest(supplied, token):
            raise HTTPException(
                status_code=401,
                detail=_UNAUTHORIZED_DETAIL,
                headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
            )

    return _require_bearer
