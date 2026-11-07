# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Acceptance seam: GovernedToolExecutor -> the REAL PostgresAuditSink chain (SQLite).

No mock past the seam under test (plan §1.4): the middleware's full audit trail —
guardrail passes, PDP decisions, and tool.execute events — lands in the real
ne-audit hash chain; only the OPA HTTP wire is stubbed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.audit.store import ne_audit_log
from ne.runtime.errors import ToolDeniedError

from .conftest import (
    NamedApproverGate,
    ToolLog,
    make_executor,
    make_principal,
    standard_bindings,
)


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_audit_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def test_allow_and_deny_trail_lands_in_the_real_chain(engine: sa.Engine) -> None:
    sink = PostgresAuditSink(engine)
    log = ToolLog()
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=NamedApproverGate(),
        pdp_overrides={
            "write.internal.notes.append": {
                "effect": "deny",
                "tier": 1,
                "reason": "blocked for test",
                "obligations": {},
                "policy_version": "1.0.0",
            }
        },
    )
    principal = make_principal()

    async def scenario() -> None:
        # allow path
        result = await executor.execute("read.docs.search", {"q": "x"}, principal=principal)
        assert result == "docs: 3 results"
        # deny path
        with pytest.raises(ToolDeniedError):
            await executor.execute(
                "write.internal.notes.append", {"note": "y"}, principal=principal
            )
        verification = await sink.verify()
        assert verification.valid is True

    asyncio.run(scenario())

    with engine.connect() as conn:
        actions = [
            dict(row.payload)["action"]
            for row in conn.execute(sa.select(ne_audit_log.c.payload)).all()
        ]
    # allow: pdp.decide + tool.execute ; deny: pdp.decide(denied)
    assert actions == ["pdp.decide", "tool.execute", "pdp.decide"]
