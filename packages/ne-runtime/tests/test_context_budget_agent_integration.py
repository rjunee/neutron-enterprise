# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-3 DoD (b): the trim partition validated against a REAL pydantic-ai
Agent.run tool loop (design §3.5, §7.1, §9 residual risk 7).

This test drives a full ``DepartmentAgentSession.run`` (a real ``pydantic_ai
Agent.run`` loop over the governed tool executor) with the context-budget trim
guard ENABLED, INCLUDING the unknown-tool path (a model-emitted unregistered name
→ the ``on_unknown_tool`` governance hook → pydantic-ai's ``RetryPromptPart`` →
recovery to a final text answer) and FUZZED prior histories built from real prior
``result.new_messages()``. A small budget forces trims MID-LOOP. On every
gateway-received request it asserts: no dangling ``ToolReturnPart`` (pairing), the
protected head/instructions/current-prompt survive, and the loop still terminates
with the scripted final answer.

PIN (design §9 residual risk 7): the protected-suffix partition is validated ONLY
against the LOCKED pydantic-ai 2.5.0 (``uv.lock``). ``ModelRequest``'s dataclass
shape and the RetryPromptPart loop mechanics are version-specific — a pydantic-ai
BUMP MUST re-run this test, and any partition break re-checked, before the pin
moves. Do not skip this test to unblock a bump.
"""

from __future__ import annotations

import asyncio
import random

import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    make_budget,
    make_executor,
    make_principal,
    resp_text,
    standard_bindings,
    text_completion,
    tool_call_completion,
    turn,
)

_BUDGET = make_budget(context_window=4000 + 100 + 1024, max_output_tokens=100, overhead_margin=1024)
_INSTR = "AGENT-INSTRUCTIONS"


def _assert_pairing_and_protected(gw: ScriptedGateway) -> None:
    assert gw.requests, "the loop reached the gateway at least once"
    for req in gw.requests:
        roles = [m.role for m in req.messages]
        for i, role in enumerate(roles):
            if role == "tool":
                assert i > 0 and roles[i - 1] == "assistant", "dangling tool return in a request"
        joined = "\n".join(m.content for m in req.messages)
        assert _INSTR in joined  # mapped instructions survive every trimmed request
        assert "CURRENT-PROMPT" in joined  # current turn prompt survives every request


def _session(
    store: SqlAlchemySessionStore, gw: ScriptedGateway, sink: RecordingAuditSink, session_id: str
) -> DepartmentAgentSession:
    executor = make_executor(
        standard_bindings_log(), sink, approval_gate=NamedApproverGate("alice")
    )
    return DepartmentAgentSession(
        session_id=session_id,
        agent_name="assistant",
        department="eng",
        owner_principal_id="user:ada",
        store=store,
        executor=executor,
        gateway=gw,
        settings=RuntimeSettings(context_budget=_BUDGET),
        instructions=_INSTR,
        sink=sink,
    )


def standard_bindings_log():
    from .conftest import ToolLog

    return standard_bindings(ToolLog())


def test_real_agent_loop_trims_and_recovers_from_unknown_tool() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        ensure_runtime_schema(engine)
        store = SqlAlchemySessionStore(engine)
        session_id = asyncio.run(
            store.create_conversation("assistant", "eng", "user:ada")
        ).conversation_id

        # Seed a LARGE prior history so the trim guard must drop oldest turns.
        big = "h" * 1500
        seed: list = []
        for i in range(8):
            seed.extend(turn(f"HIST{i} " + big, answer=f"a{i} " + big))

        async def seed_history() -> None:
            await store.append_conversation_turn(
                session_id,
                "user:ada",
                "eng",
                "assistant",
                seed,
                served_output="seed",
                trace_id="seed-trace",
                attachment_id="seed-turn",
                attempt_id="attempt-seed-turn",
            )
            await store.publish_conversation_turn(
                session_id, "user:ada", "seed-turn", "attempt-seed-turn", []
            )

        asyncio.run(seed_history())

        # Script a real tool loop: known tool → unknown tool (RetryPromptPart) → final.
        gw = ScriptedGateway(
            [
                tool_call_completion("read.docs.search", {"q": "x"}),
                tool_call_completion("totally.unknown", {}),
                text_completion("FINAL-ANSWER"),
            ]
        )
        sink = RecordingAuditSink()
        session = _session(store, gw, sink, session_id)

        result = asyncio.run(
            session.run("CURRENT-PROMPT", principal=make_principal(department="eng"))
        )

        assert result.output == "FINAL-ANSWER"  # loop terminated through the retry path
        _assert_pairing_and_protected(gw)
        # The unknown-tool governance path fired (tool.unknown audited) AND the trim
        # guard audited at least one drop over the big seeded history.
        assert sink.with_action("tool.unknown")
        assert sink.with_action("context.budget")
    finally:
        engine.dispose()


def test_fuzzed_turn_boundaries_over_real_new_messages() -> None:
    # Build the prior history from REAL prior-run result.new_messages() across a
    # fuzzed number of preceding turns, then trim on the next turn.
    rng = random.Random(4242)
    for _ in range(8):
        engine = sa.create_engine("sqlite://")
        try:
            ensure_runtime_schema(engine)
            store = SqlAlchemySessionStore(engine)
            session_id = asyncio.run(
                store.create_conversation("assistant", "eng", "user:ada")
            ).conversation_id

            n_prior = rng.randint(1, 4)
            for _ in range(n_prior):
                gw_prior = ScriptedGateway([text_completion("ok " + "p" * rng.randint(200, 1800))])
                sink_prior = RecordingAuditSink()
                sess_prior = _session(store, gw_prior, sink_prior, session_id)
                asyncio.run(
                    sess_prior.run(
                        "prior " + "q" * rng.randint(100, 1500),
                        principal=make_principal(department="eng"),
                    )
                )

            # Also seed a couple of big raw turns to guarantee trimming happens.
            async def seed_big_history(
                owned_store: SqlAlchemySessionStore = store,
                owned_session_id: str = session_id,
            ) -> None:
                await owned_store.append_conversation_turn(
                    owned_session_id,
                    "user:ada",
                    "eng",
                    "assistant",
                    [
                        *turn("BIG " + "b" * 1500, answer="a " + "b" * 1500),
                        *turn("BIG2 " + "b" * 1500, answer="a2 " + "b" * 1500),
                    ],
                    served_output="seed",
                    trace_id="seed-big-trace",
                    attachment_id="seed-big-turn",
                    attempt_id="attempt-seed-big-turn",
                )
                await owned_store.publish_conversation_turn(
                    owned_session_id,
                    "user:ada",
                    "seed-big-turn",
                    "attempt-seed-big-turn",
                    [],
                )

            asyncio.run(seed_big_history())

            gw = ScriptedGateway([text_completion("DONE")])
            sink = RecordingAuditSink()
            session = _session(store, gw, sink, session_id)
            result = asyncio.run(
                session.run("CURRENT-PROMPT", principal=make_principal(department="eng"))
            )
            assert result.output == "DONE"
            _assert_pairing_and_protected(gw)
        finally:
            engine.dispose()


def test_seed_helpers_shape() -> None:
    # Guard the fixture builders themselves: a seed turn is a request+response pair.
    msgs = turn("u", answer="a")
    assert isinstance(msgs[0], ModelRequest)
    assert any(isinstance(p, UserPromptPart) for p in msgs[0].parts)
    assert isinstance(msgs[1], ModelResponse)
    assert isinstance(resp_text("x").parts[0], TextPart)
