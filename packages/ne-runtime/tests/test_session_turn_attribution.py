# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Production binding for per-turn asking principal and exact trace identity."""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap

import sqlalchemy as sa

from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.store import (
    SqlAlchemySessionStore,
    ensure_runtime_schema,
    ne_agent_conversation_turns,
)

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_executor,
    make_principal,
    standard_bindings,
    text_completion,
)


def test_every_production_append_site_binds_principal_and_trace() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(DepartmentAgentSession.run)))
    appends = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_append_served_turn"
    ]
    # Router short-circuit, grounded pass, grounded fallback, ordinary model.
    assert len(appends) == 4
    for call in appends:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        assert ast.dump(keywords["principal"]) == ast.dump(ast.Name(id="principal"))
        assert ast.dump(keywords["trace_id"]) == ast.dump(ast.Name(id="trace_id"))


def test_real_session_append_carries_verified_principal_and_session_run_trace() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    try:
        store = SqlAlchemySessionStore(engine)
        sink = RecordingAuditSink()
        principal = make_principal("user:ada", department="eng")
        session = DepartmentAgentSession(
            session_id=asyncio.run(
                store.create_conversation("assistant", "eng", "user:ada")
            ).conversation_id,
            agent_name="assistant",
            department="eng",
            owner_principal_id="user:ada",
            store=store,
            executor=make_executor(
                standard_bindings(ToolLog()),
                sink,
                approval_gate=NamedApproverGate("alice"),
            ),
            gateway=ScriptedGateway([text_completion("ANSWER-SENTINEL")]),
            settings=RuntimeSettings(),
            sink=sink,
        )

        asyncio.run(session.run("QUESTION-SENTINEL", principal=principal))
        terminal = sink.with_action("session.run")
        with store.engine.connect() as conn:
            turn = conn.execute(sa.select(ne_agent_conversation_turns)).one()

        assert len(terminal) == 1
        assert turn.owner_principal_id == "user:ada"
        assert turn.trace_id == terminal[0].trace_id
        assert turn.trace_id is not None
        assert turn.publication_state == "published"
        assert "QUESTION-SENTINEL" in turn.private_context
        assert "ANSWER-SENTINEL" in turn.private_context
    finally:
        engine.dispose()
