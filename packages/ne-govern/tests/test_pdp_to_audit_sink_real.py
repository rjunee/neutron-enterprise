# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Acceptance seam: GovernedPdp -> the REAL PostgresAuditSink hash chain (SQLite).

No mock past the seam under test (plan §1.4): the decision->audit half runs
against the real ne-audit chain, only the OPA HTTP wire is stubbed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.audit.store import ne_audit_log
from ne.contracts import get_config_hash
from ne.contracts.governance import PolicyRequest
from ne.contracts.types import Principal

from .conftest import make_pdp

_DOC = {
    "effect": "require-approval",
    "tier": 3,
    "reason": "tier 3: finance/legal",
    "obligations": {"named_approver": True, "second_check": True},
    "policy_version": "1.0.0",
}


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_audit_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def test_decision_lands_in_the_real_audit_chain(engine: sa.Engine) -> None:
    sink = PostgresAuditSink(engine)
    pdp = make_pdp(_DOC, sink)
    request = PolicyRequest(
        principal=Principal(id="user:ada"),
        action="finance.wire",
        resource="acct:1",
    )

    async def scenario() -> None:
        decision = await pdp.decide(request)
        assert decision.effect.value == "require-approval"
        verification = await sink.verify()
        assert verification.valid is True

    asyncio.run(scenario())

    with engine.connect() as conn:
        rows = conn.execute(sa.select(ne_audit_log.c.payload)).all()
    assert len(rows) == 1
    payload = dict(rows[0].payload)
    assert payload["action"] == "pdp.decide"
    assert payload["config_hash"] == get_config_hash()
    provenance = payload["provenance"]
    assert provenance["effect"] == "require-approval"
    assert provenance["policy_version"] == "1.0.0"
    # The decision effect + policy_version are recoverable from the hash-covered
    # payload — the audit trail carries WHAT was decided and by WHICH bundle.
    assert "require-approval" in payload["detail"]
    assert "1.0.0" in payload["detail"]
