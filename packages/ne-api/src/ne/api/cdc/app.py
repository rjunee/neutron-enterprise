# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``build_cdc_sink_app`` — the mounted ``/cdc/v1`` change-event ASGI app (NE-084).

ONE route, ``POST /events``, gated fail-closed by a Standard-Webhooks HMAC
signature (the Debezium HTTP-sink scheme). The ordering is load-bearing:

1. an oversize body is rejected (413) BEFORE any HMAC/parse work;
2. the signature is verified (401 with a uniform ``"unauthorized"`` detail — no
   distinguishing oracle, constant-time compare inside) BEFORE the body is
   parsed or applied — an unauthenticated POST never reaches the governed writer;
3. a malformed envelope is a 400 (``"malformed change event"``);
4. ``200`` is returned ONLY AFTER the durable governed ``apply`` completes —
   Debezium retries any non-2xx, so no event is ACK'd before it is
   governed-written / quarantined (no-data-loss).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ne.connect.cdc import (
    CdcEnvelopeError,
    CdcSinkService,
    parse_change_events,
    verify_standard_webhook,
)

__all__ = ["build_cdc_sink_app"]

#: Reject bodies larger than this before any HMAC/parse work (DoS guard).
_MAX_BODY_BYTES = 8 * 1024 * 1024


class _BodyTooLarge(Exception):
    """Raised when the request body exceeds ``_MAX_BODY_BYTES``."""


async def _read_bounded_body(request: Request) -> bytes:
    """Read the request body, rejecting anything over ``_MAX_BODY_BYTES``.

    The limit is enforced BEFORE the whole payload is retained: a trustworthy
    ``Content-Length`` short-circuits an oversized (or unbounded) upload, and
    the stream is consumed chunk-by-chunk against a running byte budget so a
    lying or absent ``Content-Length`` cannot exhaust process memory (the
    unauthenticated caller never gets to buffer an arbitrarily large body).
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _MAX_BODY_BYTES:
                raise _BodyTooLarge
        except ValueError:
            # A malformed Content-Length is not trustworthy; fall through to the
            # streaming guard, which bounds memory regardless.
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            raise _BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def build_cdc_sink_app(service: CdcSinkService, secret: bytes) -> FastAPI:
    """Return the ``/cdc/v1`` FastAPI app bound to ``service`` and ``secret``."""
    app = FastAPI(
        title="Neutron Enterprise CDC sink",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.post("/events")
    async def events(request: Request) -> JSONResponse:
        try:
            body = await _read_bounded_body(request)
        except _BodyTooLarge:
            return JSONResponse({"detail": "payload too large"}, status_code=413)
        if not verify_standard_webhook(
            secret=secret,
            msg_id=request.headers.get("webhook-id"),
            timestamp=request.headers.get("webhook-timestamp"),
            signature_header=request.headers.get("webhook-signature"),
            body=body,
        ):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        try:
            changes = parse_change_events(body)
        except CdcEnvelopeError:
            return JSONResponse({"detail": "malformed change event"}, status_code=400)
        report = await service.apply(changes)
        return JSONResponse(report.model_dump(), status_code=200)

    return app
