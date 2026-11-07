# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Vector-driven GovernedPdp.decide tests — SPEC §4.2 effect + obligations."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ne.contracts.governance import DecisionEffect, PolicyDecision, PolicyRequest
from ne.contracts.types import Principal

from .conftest import RecordingAuditSink, load_vectors, make_pdp, opa_doc_for

_VECTORS = load_vectors()


def request_from(vec_input: dict[str, Any]) -> PolicyRequest:
    return PolicyRequest(
        principal=Principal(id=vec_input["principal"]["id"]),
        action=vec_input["action"],
        resource=vec_input["resource"],
        context=vec_input.get("context", {}),
    )


def _decide(vector: dict[str, Any]) -> PolicyDecision:
    doc = opa_doc_for(vector["expected"])
    pdp = make_pdp(doc, RecordingAuditSink())
    return asyncio.run(pdp.decide(request_from(vector["input"])))


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_decide_matches_vector(vector: dict[str, Any]) -> None:
    decision = _decide(vector)
    assert isinstance(decision, PolicyDecision)
    assert decision.effect == DecisionEffect(vector["expected"]["effect"])
    assert dict(decision.obligations) == vector["expected"]["obligations"]
    # NE-CKD3: GovernedPdp carries the OPA-decided tier for EVERY effect, including
    # null on the deny paths (proves tier is surfaced and None-safe).
    assert decision.tier == vector["expected"]["tier"]


def test_decided_tier_surfaces_with_obligations() -> None:
    # The tier-3 finance vector: the decision carries tier==3 AND the exact
    # obligation pair (named_approver + second_check) the middleware will honor.
    vector = next(v for v in _VECTORS if v["name"] == "tier3-finance")
    decision = _decide(vector)
    assert decision.tier == 3
    assert dict(decision.obligations) == {"named_approver": True, "second_check": True}


def test_policy_version_is_stamped_from_the_bundle() -> None:
    vector = next(v for v in _VECTORS if v["name"] == "tier0-read")
    doc = opa_doc_for(vector["expected"], policy_version="9.9.9")
    pdp = make_pdp(doc, RecordingAuditSink())
    decision = asyncio.run(pdp.decide(request_from(vector["input"])))
    assert decision.policy_version == "9.9.9"
