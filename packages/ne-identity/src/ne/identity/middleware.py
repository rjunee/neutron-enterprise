# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pure-ASGI OIDC authentication middleware.

Authenticates the bearer JWT on every non-exempt HTTP request, resolves it to a
:class:`ne.contracts.types.Principal` via JIT provisioning, and attaches it to
``scope["state"]["principal"]`` (surfaced as ``request.state.principal``; read
only through the :func:`current_principal` dependency). Production verifier
results also author the minimal hash-bound ``AuthenticatedRequestContext`` read
only through :func:`current_authenticated_context`; legacy verifier results with
no verified expiry remain synchronous-only.

Fail-closed HTTP contract (ABI RBAC/security test taxonomy, RFC 6750):

* The token is read from the ``Authorization`` header ONLY — query-param tokens
  (``?token=`` / ``?access_token=``) are NEVER honored.
* Exempt paths are matched by EXACT string equality on ``scope["path"]`` — no
  prefix matching (``/healthX`` still requires auth).
* Every auth failure is a 401 with ``WWW-Authenticate: Bearer`` and a body that
  carries NO failure reason, exception text, or token (no auth oracle).
* A non-:class:`TokenVerificationError` from verify/provision propagates (500) —
  never silently treated as an auth pass.
"""

from __future__ import annotations

import calendar
import json

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from ne.contracts.identity import AuthenticatedRequestContext, TokenVerifier
from ne.contracts.types import Principal
from ne.identity.errors import TokenVerificationError
from ne.identity.provisioning import JitProvisioner

_PRINCIPAL_KEY = "principal"
_AUTHENTICATED_CONTEXT_KEY = "authenticated_context"


def _extract_bearer(scope: Scope) -> str | None:
    """Return the bearer token from the Authorization header, or None.

    Requires the scheme ``bearer`` (case-insensitive) and exactly one token
    part. Any other shape returns None (-> 401).
    """
    headers = scope.get("headers") or []
    raw: bytes | None = None
    for name, value in headers:  # type: ignore[misc]
        if name == b"authorization":
            raw = value
            break
    if raw is None:
        return None
    try:
        decoded = raw.decode("latin-1")
    except (UnicodeDecodeError, AttributeError):
        return None
    parts = decoded.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    return parts[1]


class OidcAuthMiddleware:
    """Pure-ASGI middleware enforcing OIDC bearer authentication."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        verifier: TokenVerifier,
        provisioner: JitProvisioner,
        exempt_paths: tuple[str, ...] = ("/health",),
    ) -> None:
        self._app = app
        self._verifier = verifier
        self._provisioner = provisioner
        self._exempt = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        if scope.get("path") in self._exempt:
            await self._app(scope, receive, send)
            return

        token = _extract_bearer(scope)
        if token is None:
            await self._send_401(send, "Not authenticated", error=None)
            return

        try:
            claims = await self._verifier.verify(token)
        except TokenVerificationError:
            await self._send_401(send, "Invalid authentication credentials", error="invalid_token")
            return

        # Non-verification errors (provisioning I/O, bugs) propagate as 500.
        principal = await self._provisioner.provision(claims)

        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state[_PRINCIPAL_KEY] = principal
            # A legacy/nondurable TokenVerifier remains valid for synchronous
            # current_principal routes, but cannot authorize delayed work.
            state.pop(_AUTHENTICATED_CONTEXT_KEY, None)
            if claims.verified_expires_at is not None:
                department = principal.claims.get("department")
                state[_AUTHENTICATED_CONTEXT_KEY] = AuthenticatedRequestContext(
                    principal_id=principal.id,
                    authorization_groups=tuple(sorted(set(principal.groups))),
                    department=department,
                    issuer=claims.issuer,
                    expires_at=calendar.timegm(claims.verified_expires_at.utctimetuple()),
                )
        await self._app(scope, receive, send)

    @staticmethod
    async def _send_401(send: Send, detail: str, *, error: str | None) -> None:
        challenge = b"Bearer"
        if error is not None:
            challenge = f'Bearer error="{error}"'.encode("latin-1")
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", challenge),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def current_principal(request: Request) -> Principal:
    """FastAPI dependency: return the authenticated Principal or 401.

    The ONLY sanctioned read path for the request principal. Raises 401 if no
    principal is attached (i.e. the route was reached without authentication).
    """
    principal = getattr(request.state, _PRINCIPAL_KEY, None)
    if not isinstance(principal, Principal):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return principal


def current_authenticated_context(request: Request) -> AuthenticatedRequestContext:
    """Return the verifier-authored delayed-work authorization snapshot.

    This deliberately does not reconstruct a context from ``Principal.claims``.
    A legacy verifier result with no verified expiry remains sufficient only for
    synchronous routes and fails closed here.
    """

    context = getattr(request.state, _AUTHENTICATED_CONTEXT_KEY, None)
    principal = getattr(request.state, _PRINCIPAL_KEY, None)
    if (
        not isinstance(context, AuthenticatedRequestContext)
        or not isinstance(principal, Principal)
        or principal.id != context.principal_id
    ):
        raise HTTPException(status_code=401, detail="Authenticated context unavailable")
    return context
