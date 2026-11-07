# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Enqueue mode — principal-carriage no-bypass + byte-identical relay (NE-103c §4).

The relay is exercised WITHOUT real DBOS via AppSeams-injected fake enqueuer +
reader: the ``TurnRequest`` principal fields come ONLY from the verified bearer, the
body carries only a prompt, an unknown session 404s BEFORE any enqueue, ``turn_id``
is minted server-side, a prompt-free ``api.chat.turn_enqueued`` surface record is
emitted, and the SSE stream is byte-identical to the inline path for the same frames.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from ne.api.deps import ApiState
from ne.api.routers.chat import (
    _materialize_protected_terminal,
    _mint_turn_id,
    _trace_id_for_turn,
)
from ne.api.sse import sse_frame
from ne.api.turn_wire import (
    PROTECTED_TRANSPORT_GENERATION,
    LegacyTurnRequest,
    ProtectedTerminalPayload,
    ProtectedTurnRef,
    TurnFrame,
    TurnRequest,
    citations_frame,
    done_frame,
    error_frame,
    token_frame,
    tool_frame,
)
from ne.audit import list_records
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.runtime import VerifiedProvenance
from ne.contracts.types import Principal
from ne.runtime.private_payloads import (
    CONVERSATION_TURN_TARGET_KIND,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    conversation_turn_content_binding,
)
from ne.runtime.run_store import ProtectedTurnUnavailable
from ne.runtime.source_results import authenticated_request_scope
from ne.runtime.store import conversation_request_fingerprint, ne_agent_conversation_turns

from .conftest import ISSUER, AppBuilder, get_state, run


class FakeEnqueuer:
    latest_request: TurnRequest | None = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def enqueue(self, turn_id: str, request_json: str) -> None:
        self.calls.append((turn_id, request_json))
        FakeEnqueuer.latest_request = TurnRequest.model_validate_json(request_json)


class FakeReader:
    def __init__(self, frames: list[TurnFrame]) -> None:
        self._frames = frames
        self._state = None
        self.last_terminal: ProtectedTurnRef | None = None

    def bind(self, h: object) -> None:
        self._state = get_state(h.app)  # type: ignore[attr-defined]

    async def frames(self, turn_id: str) -> AsyncIterator[TurnFrame]:
        protected = self._state is not None
        for frame in self._frames:
            if protected and frame.type in {"token", "citations"}:
                continue
            if (
                frame.type == "done"
                and frame.protected_terminal is None
                and self._state is not None
            ):
                request = FakeEnqueuer.latest_request
                assert request is not None
                store = self._state.agent_run_store
                assert store is not None
                terminal = ProtectedTerminalPayload(
                    output=frame.output or "",
                    citations=tuple(
                        citation
                        for prior in self._frames
                        if prior.type == "citations"
                        for citation in (prior.citations or ())
                    ),
                    verified=frame.verified,
                    grounded=frame.grounded,
                    trace_id=frame.trace_id,
                    message_id=frame.message_id,
                )
                row = await store.claim_protected_payload(
                    domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                    run_id=None,
                    owner_principal_id=request.principal_id,
                    target_kind=CONVERSATION_TURN_TARGET_KIND,
                    target_binding=conversation_turn_content_binding(
                        conversation_id=request.session_id,
                        attachment_id=request.turn_id,
                        attempt_id=request.claimed_attempt_id,
                        slot="terminal",
                    ),
                    content_status=ProtectedContentStatus.SOURCE_FREE,
                    plaintext=terminal.model_dump_json().encode(),
                )
                self.last_terminal = ProtectedTurnRef(
                    protected_ref=row.row_id,
                    protected_hash=row.plaintext_hash,
                    content_status="source_free",
                )
                yield done_frame(
                    frame.seq,
                    frame.session_id or request.session_id,
                    protected_terminal=self.last_terminal,
                )
                continue
            yield frame


def _bind_protected_reader(h: object, reader: FakeReader) -> None:
    reader.bind(h)


def test_protected_turn_request_is_ref_only_and_binds_authenticated_context() -> None:
    context = AuthenticatedRequestContext(
        principal_id="user:ada",
        authorization_groups=("eng",),
        department="engineering",
        issuer=ISSUER,
        expires_at=1_900_000_000,
    )
    request = TurnRequest.from_principal(
        turn_id="turn-1",
        session_id="conversation-1",
        agent_name="console-chat",
        department="engineering",
        protected_input=ProtectedTurnRef(
            protected_ref="protected-1",
            protected_hash="a" * 64,
            content_status="source_free",
        ),
        authenticated_context=context,
        principal=Principal(id="user:ada", groups=("eng",), claims={"department": "engineering"}),
        trace_id="b" * 32,
        request_fingerprint="c" * 64,
        idempotency_fingerprint="d" * 64,
        claimed_attempt_id="attempt-1",
    )

    payload = request.model_dump_json()
    assert "SECRET-PROMPT" not in payload
    assert '"prompt"' not in payload
    assert request.transport_generation == PROTECTED_TRANSPORT_GENERATION
    assert request.authenticated_context.context_hash == context.context_hash

    with pytest.raises(ValueError, match="different principal"):
        TurnRequest.from_principal(
            turn_id="turn-1",
            session_id="conversation-1",
            agent_name="console-chat",
            department="engineering",
            protected_input=request.protected_input,
            authenticated_context=context,
            principal=Principal(id="user:grace", groups=("eng",)),
            trace_id="b" * 32,
            request_fingerprint="c" * 64,
            idempotency_fingerprint="d" * 64,
            claimed_attempt_id="attempt-1",
        )


def test_protected_terminal_frame_never_renders_before_materialization() -> None:
    pointer = ProtectedTurnRef(
        protected_ref="protected-terminal-1",
        protected_hash="e" * 64,
        content_status="source_free",
    )
    frame = done_frame(1, "conversation-1", protected_terminal=pointer)
    payload = frame.model_dump_json(exclude_none=True)
    assert '"output"' not in payload
    assert "SECRET-OUTPUT" not in payload
    with pytest.raises(ValueError, match="materialized before SSE"):
        frame.sse_payload()
    with pytest.raises(ValueError, match="exactly one"):
        done_frame(1, "conversation-1", "SECRET-OUTPUT", protected_terminal=pointer)


def test_protected_terminal_is_opened_only_for_exact_conversation_target(
    make_app: AppBuilder,
) -> None:
    h = make_app(protected_transport=True)
    state = get_state(h.app)
    store = state.agent_run_store
    assert store is not None
    context = AuthenticatedRequestContext(
        principal_id="user:ada",
        authorization_groups=("eng",),
        department="engineering",
        issuer=ISSUER,
        expires_at=1_900_000_000,
    )
    terminal = ProtectedTerminalPayload(
        output="SECRET-OUTPUT",
        message_id="inner-conflict",
        trace_id="inner-conflict",
    )

    async def _t() -> None:
        claimed = await store.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            run_id=None,
            owner_principal_id="user:ada",
            target_kind=CONVERSATION_TURN_TARGET_KIND,
            target_binding=conversation_turn_content_binding(
                conversation_id="conversation-1",
                attachment_id="turn-1",
                attempt_id="attempt-1",
                slot="terminal",
            ),
            content_status=ProtectedContentStatus.SOURCE_FREE,
            plaintext=terminal.model_dump_json().encode(),
        )
        frame = done_frame(
            0,
            "conversation-1",
            protected_terminal=ProtectedTurnRef(
                protected_ref=claimed.row_id,
                protected_hash=claimed.plaintext_hash,
                content_status="source_free",
            ),
        )
        with authenticated_request_scope(context):
            frames = [
                item
                async for item in _materialize_protected_terminal(
                    frame,
                    state=state,
                    owner_principal_id="user:ada",
                    conversation_id="conversation-1",
                    attachment_id="turn-1",
                    attempt_id="attempt-1",
                    trace_id="trace-canonical",
                    message_id="turn-1",
                )
            ]
        assert [item.type for item in frames] == ["token", "done"]
        assert frames[-1].output == "SECRET-OUTPUT"
        assert frames[-1].message_id == "turn-1"
        assert frames[-1].trace_id == "trace-canonical"

        with authenticated_request_scope(context), pytest.raises(ProtectedTurnUnavailable):
            async for _ in _materialize_protected_terminal(
                frame,
                state=state,
                owner_principal_id="user:ada",
                conversation_id="conversation-1",
                attachment_id="turn-1",
                attempt_id="wrong-attempt",
                trace_id="trace-canonical",
                message_id="turn-1",
            ):
                pass

    run(_t())


def test_protected_transport_rejects_plaintext_terminal_frame(make_app: AppBuilder) -> None:
    h = make_app(protected_transport=True)
    state = get_state(h.app)

    async def _t() -> None:
        with pytest.raises(ProtectedTurnUnavailable):
            async for _ in _materialize_protected_terminal(
                done_frame(0, "conversation-1", "PLAINTEXT-MUST-NOT-CROSS"),
                state=state,
                owner_principal_id="user:ada",
                conversation_id="conversation-1",
                attachment_id="turn-1",
                attempt_id="attempt-1",
                trace_id="trace-canonical",
                message_id="turn-1",
            ):
                pass

    run(_t())


def test_terminal_materialization_is_zero_margin_in_both_directions(
    make_app: AppBuilder,
) -> None:
    """The terminal check is DISCLOSURE, not admission, and is pinned as such.

    NE-CRED-MARGIN floored the two ADMISSION legs (`RunSubmissionService.
    _authenticated_context` and `chat._protect_legacy_input`) and deliberately
    left this one at zero margin. "Deliberately" is only worth the word if a
    move in EITHER direction reds, and until now neither did -- so both are
    pinned here.

    `mutant:` add the admission floor to this check (`context.expires_at - now
    < RUN_CREDENTIAL_FLOOR_BOUND_SECONDS`) -> the SECOND leg below reds. A
    caller holding five seconds is asking for the terminal of a turn that has
    ALREADY finished; refusing it would withhold an answer the deployment has
    already computed, and would do so for a full 420 s window. Nothing is
    frozen here and nothing runs afterwards, so there is no future window for a
    floor to protect.

    `mutant:` grant any grace past expiry -- 24 h, an hour, one second -> the
    FIRST leg below reds. The credential is verified-expired and the payload is
    protected turn content, so disclosure stops at the instant of expiry,
    exactly as `ProtectedSourceResultGuard.authorization` does.

    Five seconds rather than one: small enough that ANY floor above four
    seconds reds here, large enough that the comparison cannot race the clock
    between the line that mints the context and the line that checks it. The
    expired leg runs FIRST so that the disclosing leg cannot be satisfied by a
    row a graced first call had already consumed.
    """

    h = make_app(protected_transport=True)
    state = get_state(h.app)
    store = state.agent_run_store
    assert store is not None
    terminal = ProtectedTerminalPayload(
        output="SECRET-OUTPUT",
        message_id="turn-1",
        trace_id="trace-canonical",
    )

    def _context(*, remaining_s: int) -> AuthenticatedRequestContext:
        return AuthenticatedRequestContext(
            principal_id="user:ada",
            authorization_groups=("eng",),
            department="engineering",
            issuer=ISSUER,
            expires_at=int(time.time()) + remaining_s,
        )

    async def _t() -> None:
        claimed = await store.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            run_id=None,
            owner_principal_id="user:ada",
            target_kind=CONVERSATION_TURN_TARGET_KIND,
            target_binding=conversation_turn_content_binding(
                conversation_id="conversation-1",
                attachment_id="turn-1",
                attempt_id="attempt-1",
                slot="terminal",
            ),
            content_status=ProtectedContentStatus.SOURCE_FREE,
            plaintext=terminal.model_dump_json().encode(),
        )
        frame = done_frame(
            0,
            "conversation-1",
            protected_terminal=ProtectedTurnRef(
                protected_ref=claimed.row_id,
                protected_hash=claimed.plaintext_hash,
                content_status="source_free",
            ),
        )

        async def _materialize(context: AuthenticatedRequestContext) -> list[TurnFrame]:
            with authenticated_request_scope(context):
                return [
                    item
                    async for item in _materialize_protected_terminal(
                        frame,
                        state=state,
                        owner_principal_id="user:ada",
                        conversation_id="conversation-1",
                        attachment_id="turn-1",
                        attempt_id="attempt-1",
                        trace_id="trace-canonical",
                        message_id="turn-1",
                    )
                ]

        # red (grace direction): one second PAST expiry discloses NOTHING.
        with pytest.raises(ProtectedTurnUnavailable):
            await _materialize(_context(remaining_s=-1))

        # red (floor direction): five seconds of credential still discloses.
        frames = await _materialize(_context(remaining_s=5))
        assert [item.type for item in frames] == ["token", "done"]
        assert frames[-1].output == "SECRET-OUTPUT"

    run(_t())


def _make_session(h: object, headers: dict[str, str]) -> str:
    async def _t() -> str:
        async with h.client() as client:  # type: ignore[attr-defined]
            r = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=headers
            )
            assert r.status_code == 200, r.text
            return r.json()["session_id"]

    return run(_t())


def test_legacy_enqueue_uses_rollback_wire_while_protected_wire_stays_plaintext_free(
    make_app: AppBuilder,
) -> None:
    class RecordingEnqueuer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def enqueue(self, dispatch_id: str, request_json: str) -> None:
            self.calls.append((dispatch_id, request_json))

    class LegacyReader:
        async def frames(self, _dispatch_id: str) -> AsyncIterator[TurnFrame]:
            yield done_frame(0, "legacy-session", "legacy answer")

    enqueuer = RecordingEnqueuer()
    h = make_app(turn_enqueuer=enqueuer, turn_reader=LegacyReader())
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)

    async def scenario() -> bytes:
        async with h.client() as client:
            response = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "ROLLBACK-PLAINTEXT"},
                headers=headers,
            )
        assert response.status_code == 200, response.text
        return response.content

    assert b"event: done" in run(scenario())
    request = LegacyTurnRequest.model_validate_json(enqueuer.calls[0][1])
    assert request.prompt == "ROLLBACK-PLAINTEXT"
    assert request.principal_id == "user:ada"
    assert "protected_input" not in enqueuer.calls[0][1]


def test_protected_materialization_failure_returns_safe_terminal_sse(
    make_app: AppBuilder,
) -> None:
    class UnavailableReader:
        async def frames(self, _dispatch_id: str) -> AsyncIterator[TurnFrame]:
            yield done_frame(
                0,
                "conversation",
                protected_terminal=ProtectedTurnRef(
                    protected_ref="missing-protected-terminal",
                    protected_hash="f" * 64,
                    content_status="legacy_unavailable",
                ),
            )

    enqueuer = FakeEnqueuer()
    h = make_app(
        turn_enqueuer=enqueuer,
        turn_reader=UnavailableReader(),
        protected_transport=True,
    )
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)

    async def scenario() -> bytes:
        async with h.client() as client:
            response = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "MUST-NOT-LEAK"},
                headers=headers,
            )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        return response.content

    body = run(scenario())
    assert body == sse_frame("error", {"type": "error", "detail": "ProtectedTurnUnavailable"})
    assert b"MUST-NOT-LEAK" not in body


def test_null_done_from_production_reader_becomes_safe_terminal_sse(
    make_app: AppBuilder,
) -> None:
    class NullDoneReader:
        async def frames(self, _dispatch_id: str) -> AsyncIterator[TurnFrame]:
            yield TurnFrame.model_validate(
                {"seq": 0, "type": "done", "session_id": "conversation", "output": None}
            )

    h = make_app(
        turn_enqueuer=FakeEnqueuer(),
        turn_reader=NullDoneReader(),
        protected_transport=True,
    )
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)

    async def scenario() -> bytes:
        async with h.client() as client:
            response = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "MUST-NOT-LEAK"},
                headers=headers,
            )
        assert response.status_code == 200
        return response.content

    body = run(scenario())
    assert body == sse_frame("error", {"type": "error", "detail": "ValidationError"})
    assert b"MUST-NOT-LEAK" not in body


def test_enqueue_principal_from_bearer_and_byte_identical(make_app: AppBuilder) -> None:
    enqueuer = FakeEnqueuer()
    reader = FakeReader([error_frame(0, RuntimeError("turn unavailable"))])
    h = make_app(turn_enqueuer=enqueuer, turn_reader=reader, protected_transport=True)
    _bind_protected_reader(h, reader)
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)
    # Patch the reader's done session_id to the real one for realism (not required).

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "SECRET-PROMPT hello"},
                headers=headers,
            )
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.content  # frames streamed

    run(_t())

    # One enqueue happened, with a server-minted turn_id.
    assert len(enqueuer.calls) == 1
    workflow_id, request_json = enqueuer.calls[0]
    assert workflow_id.startswith("turn-")
    request = TurnRequest.model_validate_json(request_json)
    assert workflow_id == f"{request.turn_id}:{request.claimed_attempt_id}"
    assert request.session_id == session_id
    # Principal fields are the bearer's, NOT anything on the wire.
    assert request.principal_id == "user:ada"
    assert request.principal_claims.get("department") == "engineering"
    assert request.protected_input.content_status == "source_free"
    assert request.authenticated_context.principal_id == "user:ada"
    assert "SECRET-PROMPT hello" not in request_json
    assert '"prompt"' not in request_json
    assert request.trace_id is not None
    assert len(request.trace_id) == 32
    int(request.trace_id, 16)


def test_sse_bytes_identical_to_inline(make_app: AppBuilder) -> None:
    frames = [token_frame(0, "answer text"), done_frame(1, "SID", "answer text")]
    enqueuer = FakeEnqueuer()
    reader = FakeReader(list(frames))
    h = make_app(turn_enqueuer=enqueuer, turn_reader=reader, protected_transport=True)
    _bind_protected_reader(h, reader)
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)

    async def _t() -> bytes:
        async with h.client() as client:
            r = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "q"},
                headers=headers,
            )
            return r.content

    body = run(_t())
    request = FakeEnqueuer.latest_request
    assert request is not None
    expected = sse_frame("token", {"type": "token", "delta": "answer text"}) + sse_frame(
        "done",
        {
            "type": "done",
            "session_id": "SID",
            "output": "answer text",
            "message_id": request.turn_id,
            "trace_id": request.trace_id,
        },
    )
    assert body == expected


def test_published_protected_retry_replays_only_exact_materialized_terminal(
    make_app: AppBuilder,
) -> None:
    enqueuer = FakeEnqueuer()
    reader = FakeReader([done_frame(0, "SID", "PROTECTED-REPLAY")])
    h = make_app(turn_enqueuer=enqueuer, turn_reader=reader, protected_transport=True)
    _bind_protected_reader(h, reader)
    state = get_state(h.app)
    headers = {
        **h.bearer("user:ada", groups=["eng"], department="engineering"),
        "Idempotency-Key": "protected-replay",
    }
    session_id = _make_session(h, headers)

    async def _drive() -> None:
        endpoint = f"/api/v1/chat/sessions/{session_id}/messages"
        async with h.client() as client:
            first = await client.post(endpoint, json={"prompt": "SECRET-PROMPT"}, headers=headers)
            assert first.status_code == 200
            assert b"PROTECTED-REPLAY" in first.content
            request = TurnRequest.model_validate_json(enqueuer.calls[0][1])
            terminal = reader.last_terminal
            assert terminal is not None
            assert state.agent_run_store is not None
            await state.session_store.append_conversation_turn(
                session_id,
                request.principal_id,
                request.department,
                request.agent_name,
                [
                    ModelRequest(parts=[UserPromptPart(content="SECRET-PROMPT")]),
                    ModelResponse(
                        parts=[TextPart(content="PROTECTED-REPLAY")], model_name="worker"
                    ),
                ],
                served_output="PROTECTED-REPLAY",
                trace_id=request.trace_id,
                attachment_id=request.turn_id,
                attempt_id=request.claimed_attempt_id,
                request_fingerprint=request.request_fingerprint,
                idempotency_fingerprint=request.idempotency_fingerprint,
            )
            await state.agent_run_store.migrate_conversation_turn_to_protected_refs(
                conversation_id=session_id,
                owner_principal_id=request.principal_id,
                run_id=None,
                attachment_id=request.turn_id,
                attempt_id=request.claimed_attempt_id,
                input_status=request.protected_input.content_status,
                protected_input_ref=request.protected_input.protected_ref,
                protected_input_hash=request.protected_input.protected_hash,
                terminal_status=terminal.content_status,
                protected_terminal_ref=terminal.protected_ref,
                protected_terminal_hash=terminal.protected_hash,
            )
            await state.session_store.publish_conversation_turn(
                session_id,
                request.principal_id,
                request.turn_id,
                request.claimed_attempt_id,
                [],
            )
            replay = await client.post(endpoint, json={"prompt": "SECRET-PROMPT"}, headers=headers)
        assert replay.status_code == 200, replay.text
        assert b"PROTECTED-REPLAY" in replay.content
        assert len(enqueuer.calls) == 1

    run(_drive())
    records = list_records(state.audit_engine, action="api.chat.turn_replayed", limit=10)
    assert len(records) == 1
    assert "SECRET-PROMPT" not in str(records[0].payload)


@pytest.mark.parametrize("recovery_state", ["staged", "prepared"])
def test_protected_retry_never_adopts_legacy_plaintext_recovery_envelope(
    make_app: AppBuilder,
    recovery_state: str,
) -> None:
    enqueuer = FakeEnqueuer()
    h = make_app(
        turn_enqueuer=enqueuer,
        turn_reader=FakeReader([]),
        protected_transport=True,
    )
    state = get_state(h.app)
    headers = {
        **h.bearer("user:ada", groups=["eng"], department="engineering"),
        "Idempotency-Key": f"protected-{recovery_state}-retry",
    }
    session_id = _make_session(h, headers)
    turn_id = _mint_turn_id(session_id, "user:ada", headers["Idempotency-Key"])
    trace_id = _trace_id_for_turn(turn_id)
    fingerprint = conversation_request_fingerprint(
        session_id,
        "user:ada",
        "engineering",
        "console-chat",
        "same secret prompt",
        None,
        None,
        trace_id,
        ("eng",),
        {"department": "engineering", "iss": ISSUER},
    )

    async def scenario() -> bytes:
        await state.session_store.append_conversation_turn(
            session_id,
            "user:ada",
            "engineering",
            "console-chat",
            [
                ModelRequest(parts=[UserPromptPart(content="same secret prompt")]),
                ModelResponse(
                    parts=[TextPart(content="LEGACY-RECOVERY-MUST-NOT-LEAK")],
                    model_name="legacy",
                ),
            ],
            served_output="LEGACY-RECOVERY-MUST-NOT-LEAK",
            trace_id=trace_id,
            attachment_id=turn_id,
            attempt_id="attempt-legacy-recovery",
            request_fingerprint=fingerprint,
            idempotency_fingerprint=fingerprint,
            terminal_audited=recovery_state == "prepared",
        )
        with state.session_store.engine.begin() as conn:
            conn.execute(
                ne_agent_conversation_turns.update()
                .where(ne_agent_conversation_turns.c.attachment_id == turn_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        async with h.client() as client:
            response = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "same secret prompt"},
                headers=headers,
            )
        assert response.status_code == 404
        return response.content

    body = run(scenario())
    assert b"LEGACY-RECOVERY-MUST-NOT-LEAK" not in body
    assert enqueuer.calls == []


def test_enqueue_relays_server_citation_frame_without_inference(make_app: AppBuilder) -> None:
    citations = [
        {
            "chunk_id": "c1",
            "document_id": "d1",
            "source_system": "wiki",
            "source_ref": "ref-1",
        }
    ]
    reader = FakeReader(
        [
            token_frame(0, "plain text"),
            citations_frame(1, citations),
            done_frame(2, "SID", "plain text"),
        ]
    )
    enqueuer = FakeEnqueuer()
    h = make_app(turn_enqueuer=enqueuer, turn_reader=reader, protected_transport=True)
    _bind_protected_reader(h, reader)
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)

    async def _t() -> bytes:
        async with h.client() as client:
            response = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "q"},
                headers=headers,
            )
            return response.content

    body = run(_t())
    request = FakeEnqueuer.latest_request
    assert request is not None
    assert body == (
        sse_frame("token", {"type": "token", "delta": "plain text"})
        + sse_frame("citations", {"type": "citations", "citations": citations})
        + sse_frame(
            "done",
            {
                "type": "done",
                "session_id": "SID",
                "output": "plain text",
                "message_id": request.turn_id,
                "trace_id": request.trace_id,
            },
        )
    )


def test_enqueue_provenance_done_uses_the_enqueued_trace(make_app: AppBuilder) -> None:
    provenance = VerifiedProvenance(
        template_id="accounts-directory",
        template_version=1,
        config_hash="config-abc",
        row_count=3,
        truncated=False,
    )
    enqueuer = FakeEnqueuer()
    reader = FakeReader([done_frame(0, "SID", "verified answer", verified=provenance)])
    h = make_app(turn_enqueuer=enqueuer, turn_reader=reader, protected_transport=True)
    _bind_protected_reader(h, reader)
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)

    async def _t() -> str:
        async with h.client() as client:
            response = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "q"},
                headers=headers,
            )
            assert response.status_code == 200
            return response.text

    body = run(_t())
    request = FakeEnqueuer.latest_request
    assert request is not None
    request = TurnRequest.model_validate_json(enqueuer.calls[0][1])
    assert request.trace_id is not None
    done = next(
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ") and '"type":"done"' in line
    )
    assert done["trace_id"] == request.trace_id
    assert done["verified"]["template_id"] == "accounts-directory"


def test_unknown_session_404s_before_enqueue(make_app: AppBuilder) -> None:
    enqueuer = FakeEnqueuer()
    reader = FakeReader([done_frame(0, "SID", "x")])
    h = make_app(turn_enqueuer=enqueuer, turn_reader=reader, protected_transport=True)
    _bind_protected_reader(h, reader)
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")

    async def _t() -> int:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/chat/sessions/turn-bogus/messages",
                json={"prompt": "q"},
                headers=headers,
            )
            return r.status_code

    assert run(_t()) == 404
    assert enqueuer.calls == []  # no enqueue on a pre-stream 404


def test_surface_audit_record_carries_no_prompt(make_app: AppBuilder) -> None:
    enqueuer = FakeEnqueuer()
    reader = FakeReader([done_frame(0, "SID", "x")])
    h = make_app(turn_enqueuer=enqueuer, turn_reader=reader, protected_transport=True)
    _bind_protected_reader(h, reader)
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)

    async def _t() -> None:
        async with h.client() as client:
            await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "SECRET-PROMPT do-not-log"},
                headers=headers,
            )

    run(_t())

    engine = sa.create_engine(h.audit_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT action, payload FROM ne_audit_log WHERE action = :a"),
                {"a": "api.chat.turn_enqueued"},
            ).fetchall()
    finally:
        engine.dispose()
    assert rows, "no api.chat.turn_enqueued surface record was emitted"
    blob = json.dumps([dict(action=r[0], payload=r[1]) for r in rows])
    assert "SECRET-PROMPT" not in blob
    assert session_id in blob  # ids ARE present (correlation)


def test_tool_frames_relay_byte_identical_to_inline(make_app: AppBuilder) -> None:
    """NE-TURN-PROGRESS: the worker's per-hop ``tool`` frames reach the browser
    through the relay, byte-identical to the inline ``ToolTransit`` encoding
    (same key set; ``approval_id`` only when this exact transition carries it),
    BEFORE the terminal pair."""
    frames = [
        tool_frame(0, name="write.internal.demo.note", tier=1, decision="allow", audit_seq=41),
        tool_frame(
            1,
            name="write.external.demo.send",
            tier=2,
            decision="pending",
            approval_id="req-9",
            audit_seq=42,
        ),
        tool_frame(
            2,
            name="write.external.demo.send",
            tier=2,
            decision="allow",
            approval_id="req-9",
            audit_seq=43,
        ),
        tool_frame(
            3,
            name="write.external.demo.expiring",
            tier=2,
            decision="expired",
            approval_id="req-10",
            audit_seq=44,
        ),
        tool_frame(
            4,
            name="write.external.demo.failing",
            tier=2,
            decision="failed",
            approval_id="req-11",
            audit_seq=45,
        ),
        token_frame(5, "answer text"),
        done_frame(6, "SID", "answer text"),
    ]
    enqueuer = FakeEnqueuer()
    reader = FakeReader(list(frames))
    h = make_app(turn_enqueuer=enqueuer, turn_reader=reader, protected_transport=True)
    _bind_protected_reader(h, reader)
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)

    async def _t() -> bytes:
        async with h.client() as client:
            r = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "q"},
                headers=headers,
            )
            return r.content

    body = run(_t())
    request = FakeEnqueuer.latest_request
    assert request is not None
    expected = (
        sse_frame(
            "tool",
            {
                "type": "tool",
                "name": "write.internal.demo.note",
                "tier": 1,
                "decision": "allow",
                "audit_seq": 41,
            },
        )
        + sse_frame(
            "tool",
            {
                "type": "tool",
                "name": "write.external.demo.send",
                "tier": 2,
                "decision": "pending",
                "audit_seq": 42,
                "approval_id": "req-9",
            },
        )
        + sse_frame(
            "tool",
            {
                "type": "tool",
                "name": "write.external.demo.send",
                "tier": 2,
                "decision": "allow",
                "audit_seq": 43,
                "approval_id": "req-9",
            },
        )
        + sse_frame(
            "tool",
            {
                "type": "tool",
                "name": "write.external.demo.expiring",
                "tier": 2,
                "decision": "expired",
                "audit_seq": 44,
                "approval_id": "req-10",
            },
        )
        + sse_frame(
            "tool",
            {
                "type": "tool",
                "name": "write.external.demo.failing",
                "tier": 2,
                "decision": "failed",
                "audit_seq": 45,
                "approval_id": "req-11",
            },
        )
        + sse_frame("token", {"type": "token", "delta": "answer text"})
        + sse_frame(
            "done",
            {
                "type": "done",
                "session_id": "SID",
                "output": "answer text",
                "message_id": request.turn_id,
                "trace_id": request.trace_id,
            },
        )
    )
    assert body == expected


def test_terminal_error_is_delivered_despite_a_colliding_seq(make_app: AppBuilder) -> None:
    """NE-TURN-PROGRESS requirement 4 at the relay: a raised step cannot report how
    many step-context tool frames it wrote, so the worker's terminal ``error``
    carries seq 0 — BEHIND the tool frames' seqs. The monotonic dedup used to
    swallow it, freezing the reader at the last hop forever; terminals are now
    delivered regardless of seq (and the stream still stops at the first one)."""
    frames = [
        tool_frame(0, name="write.internal.demo.note", tier=1, decision="allow", audit_seq=41),
        error_frame(0, RuntimeError("model backend fell over mid-turn")),
    ]
    enqueuer = FakeEnqueuer()
    reader = FakeReader(list(frames))
    h = make_app(turn_enqueuer=enqueuer, turn_reader=reader, protected_transport=True)
    _bind_protected_reader(h, reader)
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)

    async def _t() -> bytes:
        async with h.client() as client:
            r = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "q"},
                headers=headers,
            )
            return r.content

    body = run(_t())
    assert b"event: tool" in body
    # The terminal error frame RENDERS — the reader sees the death, not a freeze.
    assert b"event: error" in body
    assert b"RuntimeError" in body  # safe detail: the class name, never a traceback


def test_duplicate_seq_non_terminal_is_still_deduped(make_app: AppBuilder) -> None:
    """Control for the terminal-bypass: the monotonic dedup still guards
    NON-terminal frames — a DBOS re-delivery of token seq 0 is dropped."""
    dup_reader = FakeReader(
        [token_frame(0, "first"), token_frame(0, "duplicate"), done_frame(1, "SID", "first")]
    )
    h = make_app(turn_enqueuer=FakeEnqueuer(), turn_reader=dup_reader, protected_transport=True)
    _bind_protected_reader(h, dup_reader)
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)

    async def _t() -> bytes:
        async with h.client() as client:
            r = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "q"},
                headers=headers,
            )
            return r.content

    body = run(_t())
    assert b"duplicate" not in body
    assert b"first" in body


def test_inline_mode_untouched(make_app: AppBuilder) -> None:
    # No enqueuer/reader → the in-process turn path (passthrough default) is used.
    h = make_app()
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)

    async def _t() -> bytes:
        async with h.client() as client:
            r = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "q"},
                headers=headers,
            )
            assert r.status_code == 200
            return r.content

    body = run(_t())
    # Inline path emits a token then a done frame (the scripted gateway → "done").
    assert b"event: token" in body
    assert b"event: done" in body


def test_queued_same_key_joins_attempt_stream_beyond_claim_ttl_without_reenqueue(
    make_app: AppBuilder,
) -> None:
    class BlockingReader:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.state: ApiState | None = None

        async def frames(self, workflow_id: str) -> AsyncIterator[TurnFrame]:
            self.calls.append(workflow_id)
            if len(self.calls) >= 2:
                self.started.set()
            await self.release.wait()
            request = FakeEnqueuer.latest_request
            assert request is not None and self.state is not None
            store = self.state.agent_run_store
            assert store is not None
            row = await store.claim_protected_payload(
                domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                run_id=None,
                owner_principal_id=request.principal_id,
                target_kind=CONVERSATION_TURN_TARGET_KIND,
                target_binding=conversation_turn_content_binding(
                    conversation_id=request.session_id,
                    attachment_id=request.turn_id,
                    attempt_id=request.claimed_attempt_id,
                    slot="terminal",
                ),
                content_status=ProtectedContentStatus.SOURCE_FREE,
                plaintext=ProtectedTerminalPayload(output="QUEUED-JOIN-ANSWER")
                .model_dump_json()
                .encode(),
            )
            yield done_frame(
                0,
                "SID",
                protected_terminal=ProtectedTurnRef(
                    protected_ref=row.row_id,
                    protected_hash=row.plaintext_hash,
                    content_status="source_free",
                ),
            )

    enqueuer = FakeEnqueuer()
    reader = BlockingReader()
    h = make_app(turn_enqueuer=enqueuer, turn_reader=reader, protected_transport=True)
    reader.state = get_state(h.app)
    state = get_state(h.app)
    state.session_store._conversation_lease_seconds = 0.04
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)

    async def _drive() -> None:
        endpoint = f"/api/v1/chat/sessions/{session_id}/messages"
        request_headers = {**headers, "Idempotency-Key": "queued-join"}
        async with h.client() as client:
            first = asyncio.create_task(
                client.post(endpoint, json={"prompt": "same prompt"}, headers=request_headers)
            )
            while not enqueuer.calls:
                await asyncio.sleep(0)
            await asyncio.sleep(0.08)
            second = asyncio.create_task(
                client.post(endpoint, json={"prompt": "same prompt"}, headers=request_headers)
            )
            await asyncio.wait_for(reader.started.wait(), timeout=1)
            reader.release.set()
            one, two = await asyncio.gather(first, second)
        assert one.content == two.content
        assert b"QUEUED-JOIN-ANSWER" in one.content

    run(_drive())
    assert len(enqueuer.calls) == 1
    assert len(reader.calls) == 2
    assert reader.calls[0] == reader.calls[1] == enqueuer.calls[0][0]


def test_enqueue_failure_releases_exact_key_for_one_new_attempt(make_app: AppBuilder) -> None:
    class FailOnceEnqueuer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def enqueue(self, turn_id: str, request_json: str) -> None:
            self.calls.append((turn_id, request_json))
            FakeEnqueuer.latest_request = TurnRequest.model_validate_json(request_json)
            if len(self.calls) == 1:
                raise RuntimeError("queue unavailable")

    enqueuer = FailOnceEnqueuer()
    reader = FakeReader([done_frame(0, "SID", "queued after retry")])
    h = make_app(turn_enqueuer=enqueuer, turn_reader=reader, protected_transport=True)
    _bind_protected_reader(h, reader)
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)

    async def _drive() -> None:
        endpoint = f"/api/v1/chat/sessions/{session_id}/messages"
        request_headers = {**headers, "Idempotency-Key": "enqueue-retry"}
        body = {"prompt": "same queued prompt"}
        async with h.client() as client:
            with pytest.raises(RuntimeError, match="queue unavailable"):
                await client.post(endpoint, json=body, headers=request_headers)
            retried = await client.post(endpoint, json=body, headers=request_headers)
            assert retried.status_code == 200
            assert b"queued after retry" in retried.content

    run(_drive())
    assert len(enqueuer.calls) == 2
    assert enqueuer.calls[0][0] != enqueuer.calls[1][0]
    first = TurnRequest.model_validate_json(enqueuer.calls[0][1])
    second = TurnRequest.model_validate_json(enqueuer.calls[1][1])
    assert first.turn_id == second.turn_id
    assert first.request_fingerprint == second.request_fingerprint
    assert first.claimed_attempt_id != second.claimed_attempt_id


def test_predispatch_claim_never_joins_a_phantom_workflow(make_app: AppBuilder) -> None:
    enqueuer = FakeEnqueuer()
    reader = FakeReader([done_frame(0, "SID", "claimed after predispatch loss")])
    h = make_app(turn_enqueuer=enqueuer, turn_reader=reader, protected_transport=True)
    _bind_protected_reader(h, reader)
    state = get_state(h.app)
    headers = h.bearer("user:ada", groups=["eng"], department="engineering")
    session_id = _make_session(h, headers)
    key = "predispatch-loss"
    turn_id = _mint_turn_id(session_id, "user:ada", key)
    trace_id = _trace_id_for_turn(turn_id)
    fingerprint = conversation_request_fingerprint(
        session_id,
        "user:ada",
        "engineering",
        "console-chat",
        "same queued prompt",
        None,
        None,
        trace_id,
        ("eng",),
        {
            "department": "engineering",
            "iss": ISSUER,
        },
    )

    async def _drive() -> None:
        first = await state.session_store.claim_conversation_attachment(
            session_id,
            "user:ada",
            "engineering",
            "console-chat",
            turn_id,
            fingerprint,
            trace_id,
            lease_seconds=0.02,
        )
        assert first.disposition == "owned" and first.dispatch_id is None
        async with h.client() as client:
            response = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "same queued prompt"},
                headers={**headers, "Idempotency-Key": key},
            )
        assert response.status_code == 200, response.text
        assert b"claimed after predispatch loss" in response.content

    run(_drive())
    assert len(enqueuer.calls) == 1
