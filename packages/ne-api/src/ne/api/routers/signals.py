# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Chat correction-signal intake (NE-137) — the flywheel's console feedback surface.

``POST /v1/chat/sessions/{id}/signals/thumbs`` and ``.../signals/wrong-source`` turn a
console thumbs-up/down or wrong-source flag into a REAL ``CorrectionSignal`` through
the SAME :class:`ne.flywheel.capture.CorrectionSignalCapture` the steward flywheel
uses (atomic store + outbox dual-write). Identity is NEVER on the wire: the request
bodies are ``extra='forbid'`` and the provenance is server-built by the ONE
``console_provenance`` (principal = the verified bearer, surface = console). The chat
session is verified via the SAME R3 lookup ``send_message`` uses (a foreign
department's id can never match — no existence leak; a miss is a 404). The
free-text ``comment`` is scrubbed + capped SERVER-side by capture (C12) — never here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from ne.api.deps import ApiState, api_state, current_principal
from ne.api.provenance import console_provenance
from ne.api.routers.chat import _require_department, resolve_session
from ne.api.wire import SignalReceiptView, ThumbsSignalRequest, WrongSourceSignalRequest
from ne.contracts.types import Principal
from ne.flywheel.capture import CorrectionSignalCapture
from ne.flywheel.errors import SignalRejectedError
from ne.flywheel.signals import SignalPayload, SignalProvenance, ThumbsSignal, WrongSourceSignal

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]


async def _prepare(
    session_id: str,
    message_id: str | None,
    principal: Principal,
    state: ApiState,
) -> tuple[CorrectionSignalCapture, SignalProvenance]:
    """Verify the session (R3, 404 on miss) + the flywheel (503), build provenance.

    Guard order mirrors the chat surface: department fail-closed (422) → R3 session
    match (404, no existence leak) → flywheel configured (503). The provenance is
    ALWAYS server-built from the verified bearer + the verified chat session_id.
    """
    department = _require_department(principal)
    matched = await resolve_session(session_id, principal.id, department, state)
    if matched is None:
        raise HTTPException(status_code=404, detail="unknown session")
    if message_id is None or not await state.session_store.conversation_has_attachment(
        session_id,
        principal.id,
        department,
        matched.agent_name,
        message_id,
    ):
        raise HTTPException(status_code=404, detail="unknown session")
    if state.signal_capture is None:
        raise HTTPException(status_code=503, detail="signal capture not configured")
    provenance = console_provenance(principal, session_id=session_id, message_id=message_id)
    return state.signal_capture, provenance


async def _capture(
    capture: CorrectionSignalCapture, provenance: SignalProvenance, payload: SignalPayload
) -> SignalReceiptView:
    """Route the built payload through the REAL capture; map rejection → 422."""
    try:
        event_id = await capture.capture(provenance, payload)
    except SignalRejectedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SignalReceiptView(event_id=event_id)


@router.post("/v1/chat/sessions/{session_id}/signals/thumbs")
async def signal_thumbs(
    session_id: str,
    body: ThumbsSignalRequest,
    principal: _Principal,
    state: _State,
) -> SignalReceiptView:
    capture, provenance = await _prepare(session_id, body.message_id, principal, state)
    expected = f"chat:{session_id}:{body.message_id}"
    if body.target_ref != expected:
        raise HTTPException(status_code=404, detail="unknown session")
    payload = ThumbsSignal(up=body.up, target_ref=expected, comment=body.comment)
    return await _capture(capture, provenance, payload)


@router.post("/v1/chat/sessions/{session_id}/signals/wrong-source")
async def signal_wrong_source(
    session_id: str,
    body: WrongSourceSignalRequest,
    principal: _Principal,
    state: _State,
) -> SignalReceiptView:
    capture, provenance = await _prepare(session_id, body.message_id, principal, state)
    expected = f"chat:{session_id}:{body.message_id}"
    if body.answer_ref != expected:
        raise HTTPException(status_code=404, detail="unknown session")
    matched = await resolve_session(session_id, principal.id, _require_department(principal), state)
    if (
        matched is None
        or body.message_id is None
        or not await state.session_store.conversation_has_attachment(
            session_id,
            principal.id,
            _require_department(principal),
            matched.agent_name,
            body.message_id,
            body.citation_ref,
        )
    ):
        raise HTTPException(status_code=404, detail="unknown session")
    payload = WrongSourceSignal(
        answer_ref=expected,
        citation_ref=f"{expected}:citation:{body.citation_ref}",
        comment=body.comment,
    )
    return await _capture(capture, provenance, payload)
