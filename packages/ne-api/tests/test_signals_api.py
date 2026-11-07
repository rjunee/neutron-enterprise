# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Chat correction-signal intake (NE-137) — thumbs / wrong-source through the REAL flywheel.

The console thumbs-up/down + wrong-source flag persist REAL ``CorrectionSignal`` rows
through the SAME ``CorrectionSignalCapture`` the steward flywheel uses (store + outbox
dual-write). Identity is NEVER on the wire: a smuggled ``surface``/``actor``/
``session_id`` is a LOUD 422; provenance (principal, surface=console, verified chat
session) is server-built. Unknown session ⇒ 404; no department claim ⇒ 422;
flywheel unconfigured ⇒ 503.
"""

from __future__ import annotations

import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, UserPromptPart

from ne.runtime.store import ne_agent_conversation_turns

from .conftest import (
    AppBuilder,
    chunk,
    get_state,
    parse_sse,
    run,
    text_completion,
    tool_call_completion,
)

_DEPT = "engineering"


def _auth(h):  # type: ignore[no-untyped-def]
    return h.bearer("user:ada", groups=["eng"], department=_DEPT)


async def _open_session(client, h) -> str:  # type: ignore[no-untyped-def]
    created = await client.post(
        "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=_auth(h)
    )
    session_id = created.json()["session_id"]
    await get_state(h.app).session_store.append_conversation_turn(
        session_id,
        "user:ada",
        _DEPT,
        "console-chat",
        [ModelRequest(parts=[UserPromptPart(content="question")])],
        served_output="answer",
        trace_id="m2",
        attachment_id="m2",
        attempt_id="attempt-m2",
    )
    await get_state(h.app).session_store.publish_conversation_turn(
        session_id, "user:ada", "m2", "attempt-m2", ["chunk-alpha"]
    )
    return session_id


def _signals(h):  # type: ignore[no-untyped-def]
    capture = get_state(h.app).signal_capture
    assert capture is not None
    return capture.store.read_signals()


def test_thumbs_down_happy_path_persists_real_signal(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/signals/thumbs",
                json={
                    "up": False,
                    "target_ref": f"chat:{sid}:m2",
                    "message_id": "m2",
                },
                headers=_auth(h),
            )
            assert r.status_code == 200
            event_id = r.json()["event_id"]
            assert isinstance(event_id, str) and event_id

        rows = _signals(h)
        assert len(rows) == 1
        row = rows[0]
        assert row.event_id == event_id
        assert row.signal_kind == "thumbs_down"
        assert row.surface == "console"
        assert row.principal_id == "user:ada"
        assert row.session_id == sid  # the VERIFIED chat session, server-derived
        assert row.message_id == "m2"
        assert row.subject_ref == f"chat:{sid}:m2"

    run(_t())


def test_wrong_source_happy_path_persists_citation(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/signals/wrong-source",
                json={
                    "answer_ref": f"chat:{sid}:m2",
                    "citation_ref": "chunk-alpha",
                    "message_id": "m2",
                },
                headers=_auth(h),
            )
            assert r.status_code == 200

        rows = _signals(h)
        assert len(rows) == 1
        row = rows[0]
        assert row.signal_kind == "wrong_source"
        assert row.surface == "console"
        assert row.subject_ref == f"chat:{sid}:m2:citation:chunk-alpha"
        assert row.payload["provenance"]["answer_ref"] == f"chat:{sid}:m2"

    run(_t())


def test_smuggled_identity_field_is_422(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            for smuggle in ({"surface": "slack"}, {"actor": "mallory"}, {"session_id": "other"}):
                body = {"up": True, "target_ref": "chat:x:1", **smuggle}
                r = await client.post(
                    f"/api/v1/chat/sessions/{sid}/signals/thumbs", json=body, headers=_auth(h)
                )
                assert r.status_code == 422, smuggle
        # No signal was ever persisted (fail-closed before any write).
        assert _signals(h) == []

    run(_t())


def test_unknown_session_is_404(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/chat/sessions/bogus/signals/thumbs",
                json={"up": True, "target_ref": "chat:x:1"},
                headers=_auth(h),
            )
            assert r.status_code == 404
            assert isinstance(r.json()["detail"], str)

    run(_t())


def test_no_department_claim_is_422(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        headers = h.bearer("user:nodept", groups=["eng"])  # no department claim
        async with h.client() as client:
            r = await client.post(
                "/api/v1/chat/sessions/any/signals/thumbs",
                json={"up": True, "target_ref": "chat:x:1"},
                headers=headers,
            )
            assert r.status_code == 422

    run(_t())


def test_unconfigured_flywheel_is_503(make_app: AppBuilder) -> None:
    h = make_app(kpi_configured=False)  # no flywheel signal database ⇒ capture is None

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/signals/thumbs",
                json={"up": True, "target_ref": f"chat:{sid}:m2", "message_id": "m2"},
                headers=_auth(h),
            )
            assert r.status_code == 503
            assert isinstance(r.json()["detail"], str)

    run(_t())


def test_cross_conversation_attachment_and_citation_refs_never_poison_signals(
    make_app: AppBuilder,
) -> None:
    h = make_app()
    state = get_state(h.app)

    async def _t() -> None:
        bob_headers = h.bearer("user:bob", groups=["eng"], department=_DEPT)
        async with h.client() as client:
            ada_sid = await _open_session(client, h)
            created = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=bob_headers,
            )
            bob_sid = created.json()["session_id"]
            await state.session_store.append_conversation_turn(
                bob_sid,
                "user:bob",
                _DEPT,
                "console-chat",
                [ModelRequest(parts=[UserPromptPart(content="bob")])],
                served_output="bob answer",
                trace_id="bob-trace",
                attachment_id="bob-message",
                attempt_id="attempt-bob-message",
            )
            await state.session_store.publish_conversation_turn(
                bob_sid,
                "user:bob",
                "bob-message",
                "attempt-bob-message",
                ["bob-citation"],
            )
            invalid_bodies = [
                (
                    f"/api/v1/chat/sessions/{bob_sid}/signals/thumbs",
                    {
                        "up": False,
                        "target_ref": f"chat:{bob_sid}:m2",
                        "message_id": "m2",
                    },
                ),
                (
                    f"/api/v1/chat/sessions/{bob_sid}/signals/thumbs",
                    {
                        "up": False,
                        "target_ref": f"chat:{ada_sid}:m2",
                        "message_id": "bob-message",
                    },
                ),
                (
                    f"/api/v1/chat/sessions/{bob_sid}/signals/wrong-source",
                    {
                        "answer_ref": f"chat:{ada_sid}:m2",
                        "citation_ref": "bob-citation",
                        "message_id": "bob-message",
                    },
                ),
                (
                    f"/api/v1/chat/sessions/{bob_sid}/signals/wrong-source",
                    {
                        "answer_ref": f"chat:{bob_sid}:bob-message",
                        "citation_ref": "chunk-alpha",
                        "message_id": "bob-message",
                    },
                ),
            ]
            for path, body in invalid_bodies:
                response = await client.post(path, json=body, headers=bob_headers)
                assert response.status_code == 404
                assert response.json() == {"detail": "unknown session"}
            assert ada_sid != bob_sid
        assert _signals(h) == []

    run(_t())


def test_default_trace_absent_ask_emits_attachment_for_real_signal_round_trip(
    make_app: AppBuilder,
) -> None:
    h = make_app(
        completions=[
            tool_call_completion("read.knowledge.search", {"query": "source"}),
            text_completion("served answer"),
        ],
        chunks=[chunk("served-citation", acl=["eng"])],
    )

    async def _t() -> None:
        async with h.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=_auth(h),
            )
            sid = created.json()["session_id"]
            answer = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "find the source"},
                headers={**_auth(h), "Idempotency-Key": "cited-retry"},
            )
            retried = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages",
                json={"prompt": "find the source"},
                headers={**_auth(h), "Idempotency-Key": "cited-retry"},
            )
            original_terminal = [
                event
                for event in parse_sse(answer.text).frames
                if event.event in {"token", "citations", "done"}
            ]
            replay_terminal = [
                event
                for event in parse_sse(retried.text).frames
                if event.event in {"token", "citations", "done"}
            ]
            assert replay_terminal == original_terminal
            parsed = parse_sse(answer.text)
            done = parsed.of_type("done")[0].data
            assert done is not None
            assert "trace_id" not in done
            attachment_id = done["message_id"]
            assert isinstance(attachment_id, str) and attachment_id.startswith("turn-")
            thumbs = await client.post(
                f"/api/v1/chat/sessions/{sid}/signals/thumbs",
                json={
                    "up": True,
                    "target_ref": f"chat:{sid}:{attachment_id}",
                    "message_id": attachment_id,
                },
                headers=_auth(h),
            )
            wrong = await client.post(
                f"/api/v1/chat/sessions/{sid}/signals/wrong-source",
                json={
                    "answer_ref": f"chat:{sid}:{attachment_id}",
                    "citation_ref": "served-citation",
                    "message_id": attachment_id,
                },
                headers=_auth(h),
            )
            assert thumbs.status_code == wrong.status_code == 200
        assert len(h.gateway.requests) == 2
        with get_state(h.app).session_store.engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_agent_conversation_turns.c.publication_state).where(
                    ne_agent_conversation_turns.c.conversation_id == sid
                )
            ).all()
        assert rows == [("published",)]
        assert len(_signals(h)) == 2

    run(_t())
