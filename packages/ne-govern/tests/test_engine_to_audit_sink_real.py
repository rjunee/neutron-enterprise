# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Acceptance seam: GuardrailEngine BLOCK -> the REAL PostgresAuditSink chain (SQLite).

No mock past the seam under test (plan §1.4): the block->audit half runs against
the real ne-audit hash chain, mirroring test_pdp_to_audit_sink_real.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.audit.store import ne_audit_log
from ne.contracts import get_config_hash
from ne.govern.config import RecipientSettings
from ne.govern.errors import GuardrailViolation
from ne.govern.guardrails import (
    GuardrailEngine,
    RecipientAllowlistGuardrail,
    build_tool_call_payload,
)

from .conftest import make_principal


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_audit_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def test_block_lands_in_the_real_audit_chain(engine: sa.Engine) -> None:
    sink = PostgresAuditSink(engine)
    guardrail = RecipientAllowlistGuardrail(RecipientSettings(allowed_recipients=("ok@corp.com",)))
    gengine = GuardrailEngine([guardrail], sink)
    payload = build_tool_call_payload(
        principal=make_principal("user:ada"),
        tool="send_email",
        recipients=["evil@attacker.com"],
    )

    async def scenario() -> None:
        with pytest.raises(GuardrailViolation):
            await gengine.enforce(payload)
        verification = await sink.verify()
        assert verification.valid is True

    asyncio.run(scenario())

    with engine.connect() as conn:
        rows = conn.execute(sa.select(ne_audit_log.c.payload)).all()
    assert len(rows) == 1
    audited = dict(rows[0].payload)
    assert audited["action"] == "guardrail.block"
    assert audited["outcome"] == "denied"
    assert audited["config_hash"] == get_config_hash()
    assert audited["provenance"]["component"] == "guardrails"
    # The attacker recipient never travels into the audit payload.
    assert "attacker.com" not in audited["detail"]
