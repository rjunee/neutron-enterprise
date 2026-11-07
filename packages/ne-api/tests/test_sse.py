# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SSE turn-then-stream tests (spec §2 / R6/R7/R9)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import aclosing

import sqlalchemy as sa

from ne.api.citation_capture import citation_capture_scope, record_served_citations
from ne.api.sse import relay_event_stream, sse_frame, turn_event_stream
from ne.api.turn_wire import TurnFrame, done_frame
from ne.contracts.runtime import AgentRunResult, VerifiedProvenance
from ne.contracts.types import Chunk, Citation, RetrievalResult, ScoredChunk

from .conftest import (
    AppBuilder,
    RaisingGateway,
    SleepyGateway,
    parse_sse,
    run,
    text_completion,
)

_DEPT = "engineering"


async def _frames(*frames: TurnFrame) -> AsyncIterator[TurnFrame]:
    for frame in frames:
        yield frame


async def _collect(stream: AsyncIterator[bytes]) -> list[bytes]:
    return [chunk async for chunk in stream]


def _auth(h):  # type: ignore[no-untyped-def]
    return h.bearer("user:ada", groups=["eng"], department=_DEPT)


async def _open_session(client, h) -> str:  # type: ignore[no-untyped-def]
    created = await client.post(
        "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=_auth(h)
    )
    return created.json()["session_id"]


def test_happy_path_token_then_done(make_app: AppBuilder) -> None:
    h = make_app(completions=[text_completion("the full answer")])

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=_auth(h)
            )
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            parsed = parse_sse(r.text)
            events = [f.event for f in parsed.frames]
            assert events == ["token", "done"]
            for frame in parsed.frames:
                assert frame.data is not None
                assert frame.event == frame.data["type"]
            done = parsed.of_type("done")[0].data
            assert done is not None and done["output"] == "the full answer"
            # Plain generation has no structural provenance and therefore no
            # provenance trace link to fabricate.
            assert "trace_id" not in done

            # The API-minted turn trace is nevertheless the REAL ambient trace:
            # both the model request and terminal audit record inherit it. A
            # provenance-bearing done frame exposes this same value (pinned
            # independently below), never a client-invented lookup key.
            assert len(h.gateway.requests) == 1
            trace_id = h.gateway.requests[0].trace_id
            assert trace_id is not None and len(trace_id) == 32
            int(trace_id, 16)

            engine = sa.create_engine(h.audit_url)
            try:
                with engine.connect() as conn:
                    rows = conn.execute(
                        sa.text("SELECT trace_id FROM ne_audit_log WHERE action = 'session.run'")
                    ).fetchall()
            finally:
                engine.dispose()
            assert rows == [(trace_id,)]

            runtime_engine = sa.create_engine(h.runtime_url)
            try:
                with runtime_engine.connect() as conn:
                    stored = conn.execute(
                        sa.text(
                            "SELECT owner_principal_id, trace_id, publication_state "
                            "FROM ne_agent_conversation_turns "
                            "WHERE conversation_id = :session_id"
                        ),
                        {"session_id": sid},
                    ).fetchall()
            finally:
                runtime_engine.dispose()
            assert stored == [("user:ada", trace_id, "published")]

    run(_t())


def test_captured_citations_emit_once_between_token_and_done() -> None:
    citation = Citation(
        chunk_id="chunk-1",
        document_id="doc-1",
        source_system="wiki",
        source_ref="ref-1",
    )
    result = RetrievalResult(
        chunks=(
            ScoredChunk(
                chunk=Chunk(
                    chunk_id="chunk-1",
                    document_id="doc-1",
                    text="served",
                    metadata={
                        "connector": "wiki",
                        "source_system": "wiki",
                        "source_ref": "ref-1",
                        "source_url": "https://wiki.example.com/records/doc-1",
                    },
                ),
                score=1.0,
            ),
        ),
        citations=(citation, citation),
    )

    async def _result() -> AgentRunResult:
        record_served_citations(result)
        return AgentRunResult(output="answer", session_id="sess-1")

    async def _drive() -> list[bytes]:
        with citation_capture_scope():
            return await _collect(turn_event_stream(_result(), keepalive_s=15.0, timeout_s=5.0))

    assert run(_drive()) == [
        sse_frame("token", {"type": "token", "delta": "answer"}),
        sse_frame(
            "citations",
            {
                "type": "citations",
                "citations": [
                    {
                        "chunk_id": "chunk-1",
                        "document_id": "doc-1",
                        "source_system": "wiki",
                        "source_ref": "ref-1",
                        "source_url": "https://wiki.example.com/records/doc-1",
                    }
                ],
            },
        ),
        sse_frame("done", {"type": "done", "session_id": "sess-1", "output": "answer"}),
    ]


def test_terminal_publication_hook_runs_only_after_token_and_before_done() -> None:
    published: list[str] = []

    async def _result() -> AgentRunResult:
        return AgentRunResult(output="answer", session_id="sess-publish")

    async def _publish() -> None:
        published.append("published")

    async def _cancel_before_publication() -> None:
        async with aclosing(
            turn_event_stream(
                _result(),
                keepalive_s=15.0,
                timeout_s=5.0,
                before_done=_publish,
            )
        ) as stream:
            assert await anext(stream) == sse_frame("token", {"type": "token", "delta": "answer"})
            assert published == []

    run(_cancel_before_publication())
    assert published == []
    frames = run(
        _collect(
            turn_event_stream(
                _result(),
                keepalive_s=15.0,
                timeout_s=5.0,
                before_done=_publish,
            )
        )
    )
    assert published == ["published"]
    assert frames[-1] == sse_frame(
        "done", {"type": "done", "session_id": "sess-publish", "output": "answer"}
    )


def test_captured_citation_conflict_fails_closed_to_no_locator() -> None:
    citation = Citation(
        chunk_id="chunk-1",
        document_id="doc-1",
        source_system="wiki",
        source_ref="ref-1",
    )

    def _result(source_url: str) -> RetrievalResult:
        return RetrievalResult(
            chunks=(
                ScoredChunk(
                    chunk=Chunk(
                        chunk_id="chunk-1",
                        document_id="doc-1",
                        text="served",
                        metadata={
                            "connector": "wiki",
                            "source_system": "wiki",
                            "source_ref": "ref-1",
                            "source_url": source_url,
                        },
                    ),
                    score=1.0,
                ),
            ),
            citations=(citation,),
        )

    with citation_capture_scope() as capture:
        record_served_citations(_result("https://wiki.example.com/records/doc-1"))
        record_served_citations(_result("https://wiki.example.com/records/doc-2"))
        assert capture.snapshot() == [
            {
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "source_system": "wiki",
                "source_ref": "ref-1",
            }
        ]


def test_inline_and_relay_done_frames_carry_the_same_server_trace() -> None:
    trace_id = "0123456789abcdef0123456789abcdef"
    verified = VerifiedProvenance(
        template_id="accounts-directory",
        template_version=1,
        config_hash="config-hash",
        row_count=2,
        truncated=False,
    )

    async def _result() -> AgentRunResult:
        return AgentRunResult(output="answer", session_id="sess-trace", verified=verified)

    expected = sse_frame(
        "done",
        {
            "type": "done",
            "session_id": "sess-trace",
            "output": "answer",
            "verified": verified.model_dump(mode="json"),
            "trace_id": trace_id,
        },
    )
    inline = run(
        _collect(
            turn_event_stream(
                _result(),
                keepalive_s=15.0,
                timeout_s=5.0,
                trace_id=trace_id,
            )
        )
    )
    relay = run(
        _collect(
            relay_event_stream(
                _frames(
                    done_frame(
                        1,
                        "sess-trace",
                        "answer",
                        verified=verified,
                        trace_id="stale-worker-trace",
                    )
                ),
                keepalive_s=15.0,
                timeout_s=5.0,
                trace_id=trace_id,
            )
        )
    )
    assert inline[-1] == relay[-1] == expected


def test_keepalive_during_slow_turn(make_app: AppBuilder) -> None:
    h = make_app(gateway=SleepyGateway(0.3, text="slow answer"), sse_keepalive_s=0.05)

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=_auth(h)
            )
            assert r.status_code == 200
            parsed = parse_sse(r.text)
            assert parsed.comment_lines >= 1  # at least one keep-alive comment
            assert parsed.of_type("done")  # and the turn still completed

    run(_t())


def test_post_stream_error_frame(make_app: AppBuilder) -> None:
    h = make_app(gateway=RaisingGateway())

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=_auth(h)
            )
            assert r.status_code == 200  # the stream opened before the failure
            errors = parse_sse(r.text).of_type("error")
            assert len(errors) == 1
            data = errors[0].data
            assert data is not None
            detail = data["detail"]
            # A safe error class name / message — never a traceback or URL.
            assert isinstance(detail, str) and detail
            assert "\n" not in detail
            assert "Traceback" not in detail
            assert not parse_sse(r.text).of_type("citations")

    run(_t())


def test_turn_timeout_error_frame(make_app: AppBuilder) -> None:
    h = make_app(gateway=SleepyGateway(5.0), sse_keepalive_s=0.05, turn_timeout_s=0.1)

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=_auth(h)
            )
            assert r.status_code == 200
            errors = parse_sse(r.text).of_type("error")
            assert len(errors) == 1
            data = errors[0].data
            assert data is not None and "timed out" in data["detail"]
            assert not parse_sse(r.text).of_type("citations")

    run(_t())


def test_turn_timeout_fires_when_shorter_than_keepalive(make_app: AppBuilder) -> None:
    # Regression (Codex P2): a turn_timeout_s SHORTER than the keepalive interval
    # must still fire promptly. The old loop blocked for the full keepalive before
    # checking the deadline, so a 0.1s timeout would not fire until ~5s (the
    # keepalive). The wait is now capped by the remaining timeout budget.
    h = make_app(gateway=SleepyGateway(5.0), sse_keepalive_s=5.0, turn_timeout_s=0.1)

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            start = time.monotonic()
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=_auth(h)
            )
            elapsed = time.monotonic() - start
            assert r.status_code == 200
            errors = parse_sse(r.text).of_type("error")
            assert len(errors) == 1
            data = errors[0].data
            assert data is not None and "timed out" in data["detail"]
            # Would be ~5s (the keepalive) under the bug; the fix returns near 0.1s.
            assert elapsed < 3.0, f"timeout not enforced before keepalive: {elapsed:.2f}s"

    run(_t())


def test_pre_stream_no_bearer_is_json_401(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post("/api/v1/chat/sessions/any/messages", json={"prompt": "hi"})
            assert r.status_code == 401
            assert isinstance(r.json()["detail"], str)

    run(_t())


def test_pre_stream_unknown_session_is_json_404(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.post(
                "/api/v1/chat/sessions/bogus/messages", json={"prompt": "hi"}, headers=_auth(h)
            )
            assert r.status_code == 404
            assert isinstance(r.json()["detail"], str)

    run(_t())


def test_pre_stream_empty_prompt_is_json_422(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            sid = await _open_session(client, h)
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "   "}, headers=_auth(h)
            )
            assert r.status_code == 422
            assert isinstance(r.json()["detail"], str)

    run(_t())


def test_pre_stream_missing_department_is_json_422(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        # A verified principal with NO department claim cannot chat (fail closed).
        headers = h.bearer("user:nodept", groups=["eng"])
        async with h.client() as client:
            r = await client.post(
                "/api/v1/chat/sessions/any/messages", json={"prompt": "hi"}, headers=headers
            )
            assert r.status_code == 422
            assert isinstance(r.json()["detail"], str)

    run(_t())
