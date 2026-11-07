# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C6 audit coupling for the cedarpy PDP backend (mirrors test_pdp_audit_coupling.py)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from ne.contracts.governance import DecisionEffect, PolicyRequest
from ne.contracts.types import Principal
from ne.govern import GovernedPdp, create_pdp
from ne.govern.cedar.evaluator import CedarEvaluator
from ne.govern.cedar.pdp import CedarGovernedPdp
from ne.govern.config import GovernSettings
from ne.govern.errors import AuditEmissionError, PolicyBackendError
from ne.schema import AuditEvent, AuditOutcome

from .conftest import FailingAuditSink, RecordingAuditSink, load_vectors

_VECTORS = load_vectors()


def request_from(vec_input: dict[str, Any]) -> PolicyRequest:
    return PolicyRequest(
        principal=Principal(id=vec_input["principal"]["id"]),
        action=vec_input["action"],
        resource=vec_input["resource"],
        context=vec_input.get("context", {}),
    )


def _overlapping_bundle(tmp_path: Path) -> Path:
    text = """// test overlap bundle

@id("a")
@ne_effect("deny")
@ne_tier("null")
@ne_reason("a")
@ne_obligations("{}")
@policy_version("1.0.0")
forbid (principal, action, resource) when {
\ttrue
};

@id("b")
@ne_effect("deny")
@ne_tier("null")
@ne_reason("b")
@ne_obligations("{}")
@policy_version("1.0.0")
forbid (principal, action, resource) when {
\ttrue
};

@id("c")
@ne_effect("allow")
@ne_tier("0")
@ne_reason("c")
@ne_obligations("{}")
@policy_version("1.0.0")
permit (principal, action, resource) when {
\tfalse
};

@id("d")
@ne_effect("allow")
@ne_tier("1")
@ne_reason("d")
@ne_obligations("{}")
@policy_version("1.0.0")
permit (principal, action, resource) when {
\tfalse
};

@id("e")
@ne_effect("require-approval")
@ne_tier("2")
@ne_reason("e")
@ne_obligations("{}")
@policy_version("1.0.0")
forbid (principal, action, resource) when {
\tfalse
};

@id("f")
@ne_effect("require-approval")
@ne_tier("3")
@ne_reason("f")
@ne_obligations("{}")
@policy_version("1.0.0")
forbid (principal, action, resource) when {
\tfalse
};

@id("g")
@ne_effect("require-approval")
@ne_tier("2")
@ne_reason("g")
@ne_obligations("{}")
@policy_version("1.0.0")
forbid (principal, action, resource) when {
\tfalse
};

@id("h")
@ne_effect("require-approval")
@ne_tier("3")
@ne_reason("h")
@ne_obligations("{}")
@policy_version("1.0.0")
forbid (principal, action, resource) when {
\tfalse
};
"""
    path = tmp_path / "overlap.cedar"
    path.write_text(text, encoding="utf-8")
    return path


# --- entry-point resolution --------------------------------------------------


def test_create_pdp_resolves_the_cedar_entry_point() -> None:
    pdp = create_pdp(GovernSettings(pdp="cedar"), sink=RecordingAuditSink())
    assert isinstance(pdp, CedarGovernedPdp)
    asyncio.run(pdp.aclose())


def test_default_settings_still_resolve_opa() -> None:
    settings = GovernSettings()
    assert settings.pdp == "opa"
    pdp = create_pdp(settings, sink=RecordingAuditSink())
    assert isinstance(pdp, GovernedPdp)


# --- audit coupling -----------------------------------------------------------


def test_decide_appends_audit_before_returning() -> None:
    vector = next(v for v in _VECTORS if v["name"] == "tier0-read")
    sink = RecordingAuditSink()
    pdp = CedarGovernedPdp(CedarEvaluator(), sink)
    decision = asyncio.run(pdp.decide(request_from(vector["input"])))
    assert decision.effect == DecisionEffect.ALLOW

    assert len(sink.events) == 1
    event = cast(AuditEvent, sink.events[0])
    assert event.action == "pdp.decide"
    assert event.outcome == AuditOutcome.success
    detail = json.loads(event.detail or "{}")
    assert set(detail) == {"effect", "reason", "tier", "policy_version"}


def test_require_approval_audits_success_outcome() -> None:
    vector = next(v for v in _VECTORS if v["name"] == "tier2-external-write")
    sink = RecordingAuditSink()
    pdp = CedarGovernedPdp(CedarEvaluator(), sink)
    decision = asyncio.run(pdp.decide(request_from(vector["input"])))
    assert decision.effect == DecisionEffect.REQUIRE_APPROVAL

    event = cast(AuditEvent, sink.events[0])
    assert event.outcome == AuditOutcome.success
    detail = json.loads(event.detail or "{}")
    assert detail["effect"] == "require-approval"


def test_deny_audits_denied_outcome() -> None:
    vector = next(v for v in _VECTORS if v["name"] == "unclassified-deny")
    sink = RecordingAuditSink()
    pdp = CedarGovernedPdp(CedarEvaluator(), sink)
    decision = asyncio.run(pdp.decide(request_from(vector["input"])))
    assert decision.effect == DecisionEffect.DENY

    event = cast(AuditEvent, sink.events[0])
    assert event.outcome == AuditOutcome.denied


def test_failing_sink_withholds_decision() -> None:
    vector = next(v for v in _VECTORS if v["name"] == "tier0-read")
    pdp = CedarGovernedPdp(CedarEvaluator(), FailingAuditSink())
    with pytest.raises(AuditEmissionError):
        asyncio.run(pdp.decide(request_from(vector["input"])))


def test_backend_error_audits_error_and_reraises(tmp_path: Path) -> None:
    path = _overlapping_bundle(tmp_path)
    sink = RecordingAuditSink()
    pdp = CedarGovernedPdp(CedarEvaluator(policy_path=path), sink)
    request = PolicyRequest(principal=Principal(id="u1"), action="read.document", resource="doc:1")
    with pytest.raises(PolicyBackendError):
        asyncio.run(pdp.decide(request))

    assert len(sink.events) == 1
    event = cast(AuditEvent, sink.events[0])
    assert event.outcome == AuditOutcome.error
    detail = json.loads(event.detail or "{}")
    assert detail["policy_version"] == "unavailable"
    assert detail["effect"] == "error"


def test_context_never_leaks_into_audit() -> None:
    sink = RecordingAuditSink()
    pdp = CedarGovernedPdp(CedarEvaluator(), sink)
    request = PolicyRequest(
        principal=Principal(id="u1"),
        action="read.document",
        resource="doc:1",
        context={"tier": 2, "secret": "s3cr3t-value"},
    )
    asyncio.run(pdp.decide(request))
    event = cast(AuditEvent, sink.events[0])
    serialized = json.dumps(dict(event.provenance)) + str(event.detail)
    assert "s3cr3t-value" not in serialized
    assert "secret" not in serialized
