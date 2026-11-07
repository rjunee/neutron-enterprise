# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The inline chat turn runs under a per-turn ``action_scope`` (NE-SCOPE-CHAT,
ne-action-scope-wiring design §3.1).

A governed probe tool records the ambient scope through the REAL governed
executor (registered via ``extra_tool_bindings`` + a real PDP decision, exactly
like ``test_tool_events.py``/``test_flowise_parity.py``) — proving the scope is
observed INSIDE the governed path, not merely set beside it.

The last two tests drive ``chat.send_message``'s ``StreamingResponse`` body
generator DIRECTLY (no HTTP client) so they run in the TEST TASK's own context.
PEP 567 async-generator semantics mean the ``with action_scope(...):`` block's
set/reset execute in whichever task is actually iterating the generator — the
starlette streaming task in production, the test task here — so this is exactly
the caller-context observability the existing TURN_PRINCIPAL/TURN_TOOL_TRANSITS
pattern already relies on (chat.py:150-153), not a special case. httpx's
``ASGITransport`` buffers the ENTIRE response body before returning, so it
cannot simulate a client aborting mid-stream; the direct-generator drive with
``aclose()`` is the only way to exercise that path.

The final section ("Finding B end-to-end") proves the Idempotency-Key write
dedup contract ACROSS two separate POSTs through the REAL GovernedToolExecutor
and REAL DurableApprovalGate (full Tier-2 HITL choreography), generalizing
``test_write_actions_platform.py``'s single-turn
``test_inline_crm_note_hitl_approve_execute_audit_dedup_roundtrip`` to two turns
sharing (or not sharing) an Idempotency-Key header.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa

from ne.api import create_app
from ne.api.platform import AppSeams, WriteActionClients
from ne.api.routers import chat
from ne.api.settings import ApiSettings
from ne.api.turn_wire import (
    ProtectedTerminalPayload,
    ProtectedTurnRef,
    TurnFrame,
    TurnRequest,
    done_frame,
)
from ne.api.wire import ChatMessageRequest
from ne.audit.config import AuditSettings
from ne.audit.store import ne_audit_log
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.plugins import PluginRegistry
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Lane, Principal
from ne.gateway.config import GatewaySettings, ModelDef
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime import current_action_scope
from ne.runtime.config import ApprovalSettings, RuntimeSettings
from ne.runtime.private_payloads import (
    CONVERSATION_TURN_TARGET_KIND,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    conversation_turn_content_binding,
)
from ne.runtime.tools import ToolBinding

from .conftest import (
    ISSUER,
    AppBuilder,
    FakeTokenVerifier,
    InMemoryFilteredChunkStore,
    PassthroughReranker,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
    get_state,
    parse_sse,
    run,
    text_completion,
    tool_call_completion,
)

_DEPT = "engineering"
# read.* classifies tier-0 in the rego-faithful PDP double (conftest._classify)
# — registration passes validate_binding and the tool is allowed autonomously,
# the same class as test_flowise_parity's read.demo.lookup.
_PROBE = "read.demo.scopeprobe"
# The exact f"turn-{uuid.uuid4()}" shape enqueue mode mints at chat.py:191.
_TURN_RE = re.compile(r"^turn-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# The exact "turn-" + 32-hex derived shape chat._mint_turn_id produces for a valid
# Idempotency-Key (NE-INV-CHAT-IDEMPOTENCY, Finding B) — disjoint from _TURN_RE.
_DERIVED_RE = re.compile(r"^turn-[0-9a-f]{32}$")


class _RecordingEnqueuer:
    """A minimal local double recording every ``(turn_id, request_json)`` enqueue
    call — kept LOCAL to this module (not imported from test_chat_enqueue.py) so this
    file's coverage of the Idempotency-Key enqueue-mode path stays self-contained."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def enqueue(self, turn_id: str, request_json: str) -> None:
        self.calls.append((turn_id, request_json))


class _StubReader:
    """A protected-transport reader backed by the request the fake enqueuer recorded."""

    def __init__(self, enqueuer: _RecordingEnqueuer) -> None:
        self._enqueuer = enqueuer
        self._state = None

    def bind(self, h: object) -> None:
        self._state = get_state(h.app)  # type: ignore[attr-defined]

    async def frames(self, turn_id: str) -> AsyncIterator[TurnFrame]:
        assert self._state is not None
        queued_turn, payload = self._enqueuer.calls[-1]
        assert queued_turn == turn_id
        request = TurnRequest.model_validate_json(payload)
        store = self._state.agent_run_store
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
            plaintext=ProtectedTerminalPayload(output="x").model_dump_json().encode(),
        )
        yield done_frame(
            0,
            request.session_id,
            protected_terminal=ProtectedTurnRef(
                protected_ref=row.row_id,
                protected_hash=row.plaintext_hash,
                content_status="source_free",
            ),
        )


def _auth(h):  # type: ignore[no-untyped-def]
    return h.bearer("user:ada", groups=["eng"], department=_DEPT)


def _probe_binding(seen: list[str | None]) -> ToolBinding:
    async def _handler(_args: Mapping[str, object]) -> str:
        seen.append(current_action_scope())
        return "ok"

    return ToolBinding(
        spec=ToolSpec(
            name=_PROBE,
            description="records the ambient action_scope for the driving test",
            tier=ApprovalTier.TIER_0,
            input_schema={
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        ),
        handler=_handler,
    )


_POLICIES = {_PROBE: ToolArgPolicy(args={"q": ArgSpec(kind=ToolArgKind.text, required=True)})}


async def _open_session(client, h) -> str:  # type: ignore[no-untyped-def]
    created = await client.post(
        "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=_auth(h)
    )
    assert created.status_code == 200
    return created.json()["session_id"]


def test_inline_tool_call_runs_under_turn_scoped_action_scope(make_app: AppBuilder) -> None:
    seen: list[str | None] = []
    h = make_app(
        completions=[tool_call_completion(_PROBE, {"q": "x"}), text_completion("ans")],
        extra_tool_bindings=(_probe_binding(seen),),
        extra_tool_policies=_POLICIES,
    )

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=_auth(h)
            )
            assert r.status_code == 200
            parsed = parse_sse(r.text)
            done = parsed.of_type("done")
            assert done and done[0].data is not None
            assert done[0].data["output"] == "ans"

            assert len(seen) == 1
            scope = seen[0]
            assert scope is not None and _TURN_RE.fullmatch(scope)

        # No ambient leak back into the consuming (test) context.
        assert current_action_scope() is None

    run(_t())


def test_action_scope_unique_per_message(make_app: AppBuilder) -> None:
    seen: list[str | None] = []
    h = make_app(
        completions=[
            tool_call_completion(_PROBE, {"q": "a"}),
            text_completion("one"),
            tool_call_completion(_PROBE, {"q": "b"}),
            text_completion("two"),
        ],
        extra_tool_bindings=(_probe_binding(seen),),
        extra_tool_policies=_POLICIES,
    )

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r1 = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=_auth(h)
            )
            assert r1.status_code == 200
            r2 = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "hi again"},
                headers=_auth(h),
            )
            assert r2.status_code == 200

    run(_t())

    assert len(seen) == 2
    first, second = seen
    assert first is not None and _TURN_RE.fullmatch(first)
    assert second is not None and _TURN_RE.fullmatch(second)
    # Per-MESSAGE scope, not per-session.
    assert first != second


def test_action_scope_reset_after_stream_completes(make_app: AppBuilder) -> None:
    h = make_app(completions=[text_completion("ans")])

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)

        principal = Principal(id="user:ada", groups=("eng",), claims={"department": _DEPT})
        state = get_state(h.app)
        resp = await chat.send_message(sid, ChatMessageRequest(prompt="hi"), principal, state)
        agen = resp.body_iterator
        assert isinstance(agen, AsyncGenerator)

        await anext(agen)
        mid = current_action_scope()
        assert mid is not None and _TURN_RE.fullmatch(mid)

        async for _ in agen:
            pass

        assert current_action_scope() is None

    run(_t())


def test_action_scope_reset_on_client_abort(make_app: AppBuilder) -> None:
    h = make_app(completions=[text_completion("first-aborted"), text_completion("retry-served")])

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)

        principal = Principal(id="user:ada", groups=("eng",), claims={"department": _DEPT})
        state = get_state(h.app)
        resp = await chat.send_message(
            sid,
            ChatMessageRequest(prompt="hi"),
            principal,
            state,
            idempotency_key="close-before-done",
        )
        agen = resp.body_iterator
        assert isinstance(agen, AsyncGenerator)

        await anext(agen)
        mid = current_action_scope()
        assert mid is not None and mid.startswith("turn-")

        # Simulate the client aborting mid-stream (httpx's ASGITransport buffers
        # the whole body and cannot do this) — GeneratorExit at the yield runs
        # action_scope.__exit__ and the existing finally.
        await agen.aclose()

        assert current_action_scope() is None
        assert (
            await state.session_store.load_conversation_history(
                sid, "user:ada", _DEPT, "console-chat"
            )
            == []
        )

        retry = await chat.send_message(
            sid,
            ChatMessageRequest(prompt="hi"),
            principal,
            state,
            idempotency_key="close-before-done",
        )
        retry_frames = [
            frame.encode() if isinstance(frame, str) else bytes(frame)
            async for frame in retry.body_iterator
        ]
        # The full terminal envelope was already audited and prepared before
        # the first token became visible. Closing before ``done`` preserves
        # that immutable envelope; retry publishes/replays it without a second
        # model/tool execution.
        assert b"first-aborted" in b"".join(retry_frames)
        assert len(h.gateway.requests) == 1
        assert "first-aborted" in str(
            await state.session_store.load_conversation_history(
                sid, "user:ada", _DEPT, "console-chat"
            )
        )

    run(_t())


def test_close_immediately_after_done_keeps_publication_monotonic_and_replayable(
    make_app: AppBuilder,
) -> None:
    h = make_app(completions=[text_completion("published-answer")])

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
        principal = Principal(id="user:ada", groups=("eng",), claims={"department": _DEPT})
        state = get_state(h.app)

        response = await chat.send_message(
            sid,
            ChatMessageRequest(prompt="hi"),
            principal,
            state,
            idempotency_key="close-after-done",
        )
        stream = response.body_iterator
        assert isinstance(stream, AsyncGenerator)
        frames: list[bytes] = []
        while True:
            content = await anext(stream)
            frame = content.encode() if isinstance(content, str) else bytes(content)
            frames.append(frame)
            if frame.startswith(b"event: done\n"):
                break
        await stream.aclose()

        history = await state.session_store.load_conversation_history(
            sid, "user:ada", _DEPT, "console-chat"
        )
        assert "published-answer" in str(history)
        replay = await chat.send_message(
            sid,
            ChatMessageRequest(prompt="hi"),
            principal,
            state,
            idempotency_key="close-after-done",
        )
        replay_frames = [frame async for frame in replay.body_iterator]
        assert replay_frames == frames
        assert len(h.gateway.requests) == 1

    run(_t())


# --- Idempotency-Key (NE-INV-CHAT-IDEMPOTENCY, Finding B) ------------------------


def test_valid_idempotency_key_scope_shape(make_app: AppBuilder) -> None:
    seen: list[str | None] = []
    h = make_app(
        completions=[tool_call_completion(_PROBE, {"q": "x"}), text_completion("ans")],
        extra_tool_bindings=(_probe_binding(seen),),
        extra_tool_policies=_POLICIES,
    )

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "hi"},
                headers={**_auth(h), "Idempotency-Key": "retry-abc.123"},
            )
            assert r.status_code == 200

    run(_t())

    assert len(seen) == 1
    scope = seen[0]
    assert scope is not None and _DERIVED_RE.fullmatch(scope)
    # Disjoint from the uuid4-mint-path namespace.
    assert not _TURN_RE.fullmatch(scope)


def test_invalid_idempotency_key_falls_back_to_fresh_uuid4(make_app: AppBuilder) -> None:
    seen: list[str | None] = []
    h = make_app(
        completions=[
            tool_call_completion(_PROBE, {"q": "a"}),
            text_completion("one"),
            tool_call_completion(_PROBE, {"q": "b"}),
            text_completion("two"),
        ],
        extra_tool_bindings=(_probe_binding(seen),),
        extra_tool_policies=_POLICIES,
    )
    # Over the 128-char length bound — invalid, so both turns fall back.
    bad_key = "x" * 129

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r1 = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "hi"},
                headers={**_auth(h), "Idempotency-Key": bad_key},
            )
            assert r1.status_code == 200
            assert parse_sse(r1.text).of_type("done")
            r2 = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "hi again"},
                headers={**_auth(h), "Idempotency-Key": bad_key},
            )
            assert r2.status_code == 200
            assert parse_sse(r2.text).of_type("done")

    run(_t())

    assert len(seen) == 2
    first, second = seen
    assert first is not None and _TURN_RE.fullmatch(first)
    assert second is not None and _TURN_RE.fullmatch(second)
    assert not _DERIVED_RE.fullmatch(first)
    assert not _DERIVED_RE.fullmatch(second)
    # Fail-safe fallback still mints a UNIQUE scope per turn (byte-identical to
    # today's no-header behavior) — never rejected outright for a bad token.
    assert first != second


def test_same_key_different_session_derives_different_turn_id(make_app: AppBuilder) -> None:
    seen: list[str | None] = []
    h = make_app(
        completions=[
            tool_call_completion(_PROBE, {"q": "a"}),
            text_completion("one"),
            tool_call_completion(_PROBE, {"q": "b"}),
            text_completion("two"),
        ],
        extra_tool_bindings=(_probe_binding(seen),),
        extra_tool_policies=_POLICIES,
    )
    token = "retry-cross-session"
    ada = h.bearer("user:ada", groups=["eng"], department="engineering")
    bob = h.bearer("user:bob", groups=["mkt"], department="marketing")

    async def _t() -> None:
        async with h.client() as client:
            sid_ada = await _open_session(client, h)  # ada's default bearer (_auth)
            created_bob = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=bob
            )
            assert created_bob.status_code == 200
            sid_bob = created_bob.json()["session_id"]
            assert sid_ada != sid_bob  # different departments -> different R3 sessions

            r1 = await client.post(
                f"/api/v1/chat/sessions/{sid_ada}/messages",
                json={"prompt": "hi"},
                headers={**ada, "Idempotency-Key": token},
            )
            assert r1.status_code == 200
            r2 = await client.post(
                f"/api/v1/chat/sessions/{sid_bob}/messages",
                json={"prompt": "hi"},
                headers={**bob, "Idempotency-Key": token},
            )
            assert r2.status_code == 200

    run(_t())

    assert len(seen) == 2
    first, second = seen
    assert first is not None and _DERIVED_RE.fullmatch(first)
    assert second is not None and _DERIVED_RE.fullmatch(second)
    # Session-prefix proof: the SAME token on two different sessions derives two
    # DIFFERENT turn ids — no cross-session collision.
    assert first != second


def test_same_key_distinct_principal_conversations_derive_different_turn_id(
    make_app: AppBuilder,
) -> None:
    seen: list[str | None] = []
    h = make_app(
        completions=[
            tool_call_completion(_PROBE, {"q": "a"}),
            text_completion("one"),
            tool_call_completion(_PROBE, {"q": "b"}),
            text_completion("two"),
        ],
        extra_tool_bindings=(_probe_binding(seen),),
        extra_tool_policies=_POLICIES,
    )
    token = "retry-cross-principal"
    # Same department still yields distinct principal-owned Conversations.
    ada = h.bearer("user:ada", groups=["eng"], department=_DEPT)
    bob = h.bearer("user:bob", groups=["eng"], department=_DEPT)

    async def _t() -> None:
        async with h.client() as client:
            sid_ada = await _open_session(client, h)
            created_bob = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=bob
            )
            assert created_bob.status_code == 200
            sid_bob = created_bob.json()["session_id"]
            assert sid_ada != sid_bob

            r1 = await client.post(
                f"/api/v1/chat/sessions/{sid_ada}/messages",
                json={"prompt": "hi"},
                headers={**ada, "Idempotency-Key": token},
            )
            assert r1.status_code == 200
            r2 = await client.post(
                f"/api/v1/chat/sessions/{sid_bob}/messages",
                json={"prompt": "hi"},
                headers={**bob, "Idempotency-Key": token},
            )
            assert r2.status_code == 200

    run(_t())

    assert len(seen) == 2
    first, second = seen
    assert first is not None and _DERIVED_RE.fullmatch(first)
    assert second is not None and _DERIVED_RE.fullmatch(second)
    # Conversation/principal scoping preserves the original idempotency
    # discrimination without relying on the removed shared-session invariant.
    assert first != second


def test_delimiter_collision_across_principal_and_key_produces_different_turn_ids() -> None:
    """Codex C1 regression (chat.py's ``_mint_turn_id``/``_netstring_encode``): a
    naive ``":"``-joined hash input (``f"{session_id}:{principal_id}:{key}"``) is
    AMBIGUOUS whenever a component itself contains ``":"`` — and both
    ``principal_id`` (e.g. ``"user:ada"``) and the idempotency key (``":"`` is in
    ``_IDEMPOTENCY_KEY_RE``'s charset) legitimately do. Under naive joining,
    ``principal_id="user:ada", idempotency_key="retry"`` and
    ``principal_id="user", idempotency_key="ada:retry"`` both join to the identical
    string ``"sess-1:user:ada:retry"`` and would hash to the SAME digest — defeating
    the per-principal isolation this derivation is meant to provide. The
    length-prefixed ``_netstring_encode`` fix makes the hash input injective, so
    these two distinct (principal_id, idempotency_key) pairs derive DIFFERENT turn
    ids despite the identical naive join.
    """
    session_id = "sess-1"
    a = chat._mint_turn_id(session_id, "user:ada", "retry")
    b = chat._mint_turn_id(session_id, "user", "ada:retry")
    assert a is not None and _DERIVED_RE.fullmatch(a)
    assert b is not None and _DERIVED_RE.fullmatch(b)
    assert a != b


def test_enqueue_mode_derives_turn_id_from_idempotency_key(make_app: AppBuilder) -> None:
    enqueuer = _RecordingEnqueuer()
    reader = _StubReader(enqueuer)
    h = make_app(turn_enqueuer=enqueuer, turn_reader=reader, protected_transport=True)
    reader.bind(h)
    headers = h.bearer("user:ada", groups=["eng"], department=_DEPT)

    async def _t() -> str:
        async with h.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=headers
            )
            assert created.status_code == 200
            sid = created.json()["session_id"]

            r1 = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "hi"},
                headers={**headers, "Idempotency-Key": "retry-enqueue"},
            )
            assert r1.status_code == 200
            r2 = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "hi again"},
                headers={**headers, "Idempotency-Key": "retry-enqueue"},
            )
            assert r2.status_code == 409
            assert r2.json() == {"detail": "idempotency key conflicts with the original request"}
            r3 = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "hi once more"},
                headers={**headers, "Idempotency-Key": "a-different-key"},
            )
            assert r3.status_code == 200
            return sid

    run(_t())

    assert len(enqueuer.calls) == 2
    dispatch_id_1, request_json_1 = enqueuer.calls[0]
    dispatch_id_3, request_json_3 = enqueuer.calls[1]

    # Reusing the same key for a different prompt is rejected before enqueue;
    # only an exact request fingerprint may replay the derived turn id.
    request = TurnRequest.model_validate_json(request_json_1)
    assert _DERIVED_RE.fullmatch(request.turn_id)
    assert dispatch_id_1 == f"{request.turn_id}:{request.claimed_attempt_id}"
    # A different key -> a different derived turn_id (no over-dedup).
    request_3 = TurnRequest.model_validate_json(request_json_3)
    assert request_3.turn_id != request.turn_id
    assert _DERIVED_RE.fullmatch(request_3.turn_id)
    assert dispatch_id_3 == f"{request_3.turn_id}:{request_3.claimed_attempt_id}"


# --- Finding B end-to-end: governed write dedup ACROSS two POSTs (real HITL) ---
#
# This section replicates (does NOT import) the write-platform harness from
# test_write_actions_platform.py::test_inline_crm_note_hitl_approve_execute_
# audit_dedup_roundtrip — that file is outside this unit's mayTouch, and this
# module's own ``_RecordingEnqueuer`` docstring already documents the "kept
# LOCAL" convention for self-contained coverage. The two tests below are the
# direct generalization of that single-turn precedent (two tool calls, ONE
# POST) to two tool calls across TWO SEPARATE POSTs (two turns), sharing or
# not sharing the same ``Idempotency-Key`` header.


class _FakeCrmClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def create_record(self, sobject: str, fields: Mapping[str, str]) -> str:
        self.calls.append((sobject, dict(fields)))
        return "003000000000001AAA"


class _FakeTicketClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int | str, dict[str, str]]] = []

    async def update_record(
        self, *, table: str, key_column: str, key_value: int | str, fields: Mapping[str, str]
    ) -> int:
        self.calls.append((table, key_column, key_value, dict(fields)))
        return 1


class _FakeDraftClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def create_draft(self, *, mailbox: str, subject: str, body: str, to: str) -> str:
        self.calls.append((mailbox, subject, body, to))
        return "AAMkAG-draft-1"


_WRITE_CRM_NOTE_TOOL = "write.internal.crm.note"


def _write_platform_app(
    tmp_path: Path, gateway: Any
) -> tuple[Any, FakeTokenVerifier, _FakeCrmClient, str]:
    """Build a real ne-api app with the 3 NE-050 write actions ON, connector
    clients faked, and the given (scripted) gateway — the exact ``_seams`` +
    ``create_app`` shape ``test_write_actions_platform.py`` uses, replicated
    locally per this file's keep-local convention."""
    reset_config_hash_for_testing()
    registry = PluginRegistry()
    verifier = FakeTokenVerifier()
    registry.register("token_verifier", "fake", lambda oidc: verifier)
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)

    key = uuid.uuid4().hex
    runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
    audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
    perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"
    fake_crm = _FakeCrmClient()
    clients = WriteActionClients(crm=fake_crm, ticket=_FakeTicketClient(), draft=_FakeDraftClient())

    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
            principal_claims=("email", "preferred_username", "name", "department"),
        ),
        runtime_settings=RuntimeSettings(
            session_database_url=runtime_url, approvals=ApprovalSettings(poll_interval_s=0.05)
        ),
        audit_settings=AuditSettings(database_url=audit_url),
        permissions_settings=PermissionsSettings(database_url=perms_url),
        gateway_settings=GatewaySettings(
            models=[
                ModelDef(
                    name="stub",
                    litellm_model="openai/stub",
                    provider="stub",
                    lane=Lane.FRONTIER_API,
                    context_window=32768,
                )
            ]
        ),
        gateway=gateway,
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=InMemoryFilteredChunkStore(),
        reranker=PassthroughReranker(),
        plugin_registry=registry,
        write_action_clients=clients,
    )
    app = create_app(
        ApiSettings(
            qdrant_url="http://qdrant.test",
            qdrant_collection="t",
            tei_url="http://tei.test",
            write_actions_enabled=True,
            approver_roles=("approvers",),
        ),  # type: ignore[call-arg]
        seams=seams,
    )
    return app, verifier, fake_crm, audit_url


async def _run_turn(
    writer: httpx.AsyncClient,
    approver: httpx.AsyncClient,
    ada_headers: dict[str, str],
    appr_headers: dict[str, str],
    sid: str,
    idempotency_key: str,
    loop: asyncio.AbstractEventLoop,
) -> str:
    """POST one turn with the given ``Idempotency-Key`` and return its text output.

    NE-CS-SEND (``1df6a113``) moved ``write.internal.crm.note`` to **Tier 1** —
    an internal, low-risk write, per the owner's 2026-08-29 ruling ("writing an
    internal CRM note should stay tier 1 … Sending an external email remains
    tier 2"). A Tier-1 action is autonomous, so this turn crosses NO approval
    gate and there is no pending row to grant. This helper previously waited on
    a Tier-2 approval and granted it; that wait could only ever time out after
    the tier moved, which is why both callers were RED on the train.

    What these tests actually pin — idempotent dedup of a governed write across
    POSTs — is orthogonal to the tier and is asserted unchanged by the callers.
    ``approver``/``appr_headers``/``loop`` are kept in the signature so that
    restoring a gate here is a one-line change rather than a re-plumb.
    """
    resp = await writer.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        json={"prompt": "add a note"},
        headers={**ada_headers, "Idempotency-Key": idempotency_key},
    )
    assert resp.status_code == 200, resp.text
    parsed = parse_sse(resp.text)
    done = parsed.of_type("done")
    assert done and done[0].data is not None
    return done[0].data["output"]


def _tool_execute_success_rows(audit_url: str) -> list[Any]:
    engine = sa.create_engine(audit_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_audit_log.c.outcome, ne_audit_log.c.payload).where(
                    ne_audit_log.c.action == "tool.execute"
                )
            ).all()
    finally:
        engine.dispose()
    return [row for row in rows if row.outcome == "success"]


def test_same_idempotency_key_dedups_governed_write_across_posts(tmp_path: Path) -> None:
    call_args = {"record_id": "001000000000001AAA", "title": "t", "body": "b"}
    gateway = ScriptedGateway(
        [
            tool_call_completion(_WRITE_CRM_NOTE_TOOL, call_args),
            text_completion("one"),
            tool_call_completion(_WRITE_CRM_NOTE_TOOL, call_args),
            text_completion("two"),
        ]
    )
    app, verifier, fake_crm, audit_url = _write_platform_app(tmp_path, gateway)

    async def _t() -> None:
        loop = asyncio.get_running_loop()
        ada_token = verifier.mint("user:ada", groups=["eng"], department="engineering")
        appr_token = verifier.mint("user:approver", groups=["approvers"])
        ada_headers = {"Authorization": f"Bearer {ada_token}"}
        appr_headers = {"Authorization": f"Bearer {appr_token}"}
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://ne.test") as writer,
            httpx.AsyncClient(transport=transport, base_url="http://ne.test") as approver,
        ):
            created = await writer.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=ada_headers
            )
            assert created.status_code == 200, created.text
            sid = created.json()["session_id"]

            output_1 = await _run_turn(
                writer, approver, ada_headers, appr_headers, sid, "retry-same-key", loop
            )
            assert output_1 == "one"

            # A successful retry replays the exact published served turn. It
            # does not rerun the gateway/tool or mint a second approval.
            replay = await writer.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "add a note"},
                headers={**ada_headers, "Idempotency-Key": "retry-same-key"},
            )
            assert replay.status_code == 200
            replayed = parse_sse(replay.text).of_type("done")
            assert replayed and replayed[0].data is not None
            assert replayed[0].data["output"] == "one"

    run(_t())

    # The connector and model loop each ran exactly once.
    assert len(fake_crm.calls) == 1
    assert len(gateway.requests) == 2

    success_rows = _tool_execute_success_rows(audit_url)
    assert len(success_rows) == 1

    for engine in get_state(app).engines:
        engine.dispose()


def test_different_idempotency_keys_are_two_real_writes(tmp_path: Path) -> None:
    call_args = {"record_id": "001000000000001AAA", "title": "t", "body": "b"}
    gateway = ScriptedGateway(
        [
            tool_call_completion(_WRITE_CRM_NOTE_TOOL, call_args),
            text_completion("one"),
            tool_call_completion(_WRITE_CRM_NOTE_TOOL, call_args),
            text_completion("two"),
        ]
    )
    app, verifier, fake_crm, audit_url = _write_platform_app(tmp_path, gateway)

    async def _t() -> None:
        loop = asyncio.get_running_loop()
        ada_token = verifier.mint("user:ada", groups=["eng"], department="engineering")
        appr_token = verifier.mint("user:approver", groups=["approvers"])
        ada_headers = {"Authorization": f"Bearer {ada_token}"}
        appr_headers = {"Authorization": f"Bearer {appr_token}"}
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://ne.test") as writer,
            httpx.AsyncClient(transport=transport, base_url="http://ne.test") as approver,
        ):
            created = await writer.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=ada_headers
            )
            assert created.status_code == 200, created.text
            sid = created.json()["session_id"]

            output_1 = await _run_turn(
                writer, approver, ada_headers, appr_headers, sid, "retry-key-a", loop
            )
            assert output_1 == "one"

            # DIFFERENT Idempotency-Key on the second POST -> its own independent
            # approval -> a REAL second connector call (no over-dedup).
            output_2 = await _run_turn(
                writer, approver, ada_headers, appr_headers, sid, "retry-key-b", loop
            )
            assert output_2 == "two"

    run(_t())

    assert len(fake_crm.calls) == 2
    # NE-CRM-NOTE-PROVENANCE: the stamp lands only when an approval WAS
    # consumed. `write.internal.crm.note` is Tier 1 (autonomous) since NE-CS-SEND
    # `1df6a113`, so no approval exists and `_provenance_stamp` returns None —
    # "No approval => no stamp AT ALL - a ``Requested by``-only stamp is
    # forbidden" (crm_note.py:119-135). Assert that documented behaviour rather
    # than the two-distinct-approval-ids property, which only holds at Tier 2.
    # If this action is ever returned to Tier 2, restore the id-inequality check.
    (sobject_a, fields_a), (sobject_b, fields_b) = fake_crm.calls
    assert sobject_a == sobject_b
    for fields in (fields_a, fields_b):
        assert "[Governed by Neutron]" not in fields["Body"]
        assert "Approval ID:" not in fields["Body"]
    normalize = lambda f: dict(f)  # noqa: E731
    assert normalize(fields_a) == normalize(fields_b)

    success_rows = _tool_execute_success_rows(audit_url)
    assert len(success_rows) == 2
    dedup_hits = [
        row
        for row in success_rows
        if json.loads(row.payload.get("detail") or "{}").get("deduplicated") is True
    ]
    assert dedup_hits == [], "different Idempotency-Keys must never over-dedup"

    for engine in get_state(app).engines:
        engine.dispose()
