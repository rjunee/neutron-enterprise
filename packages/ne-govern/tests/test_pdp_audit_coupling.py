# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C6 audit-coupling: every decide() emits exactly one well-formed AuditEvent."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest

from ne.contracts.events import AuditEventLike
from ne.contracts.governance import PolicyRequest
from ne.contracts.types import Principal
from ne.govern.errors import PolicyBackendError
from ne.govern.pdp import GovernedPdp
from ne.schema import AuditEvent, AuditOutcome

from .conftest import (
    RecordingAuditSink,
    load_vectors,
    make_opa_client,
    make_pdp,
    opa_doc_for,
)

_VECTORS = load_vectors()

_EXPECTED_OUTCOME = {
    "allow": AuditOutcome.success,
    "require-approval": AuditOutcome.success,
    "deny": AuditOutcome.denied,
}


def request_from(vec_input: dict[str, Any]) -> PolicyRequest:
    return PolicyRequest(
        principal=Principal(id=vec_input["principal"]["id"]),
        action=vec_input["action"],
        resource=vec_input["resource"],
        context=vec_input.get("context", {}),
    )


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_decide_emits_one_event_with_correct_fields(
    vector: dict[str, Any], frozen_config_hash: str
) -> None:
    sink = RecordingAuditSink()
    pdp = make_pdp(opa_doc_for(vector["expected"]), sink)
    asyncio.run(pdp.decide(request_from(vector["input"])))

    assert len(sink.events) == 1
    event = cast(AuditEvent, sink.events[0])
    assert event.action == "pdp.decide"
    assert event.resource == vector["input"]["resource"]
    assert event.actor == vector["input"]["principal"]["id"]
    assert event.outcome == _EXPECTED_OUTCOME[vector["expected"]["effect"]]
    assert event.config_hash == frozen_config_hash
    assert event.provenance["source"] == "ne-govern"
    assert event.provenance["policy_version"] == "1.0.0"
    assert event.provenance["requested_action"] == vector["input"]["action"]
    assert event.provenance["effect"] == vector["expected"]["effect"]


def test_event_satisfies_audit_event_like_structurally() -> None:
    vector = _VECTORS[0]
    sink = RecordingAuditSink()
    pdp = make_pdp(opa_doc_for(vector["expected"]), sink)
    asyncio.run(pdp.decide(request_from(vector["input"])))
    assert isinstance(sink.events[0], AuditEventLike)


def test_request_context_is_never_leaked_to_audit() -> None:
    # REDACTION lift-pattern: context values must not travel to the sink.
    sink = RecordingAuditSink()
    pdp = make_pdp(opa_doc_for({"effect": "allow", "tier": 0, "obligations": {}}), sink)
    request = PolicyRequest(
        principal=Principal(id="u1"),
        action="read.document",
        resource="doc:1",
        context={"tier": 0, "secret_note": "SENSITIVE-DO-NOT-LOG"},
    )
    asyncio.run(pdp.decide(request))
    event = cast(AuditEvent, sink.events[0])
    serialized = json.dumps(dict(event.provenance)) + str(event.detail)
    assert "SENSITIVE-DO-NOT-LOG" not in serialized
    assert "secret_note" not in serialized


# --- what the retry does and does not put in the ledger --------------------
#
# The OpaClient retry (opa.py) swallows one ReadTimeout. These pin the audit
# consequences of that at the GovernedPdp boundary, because the ledger is a
# consumed product surface: ne-api's tool_transit reasons explicitly about
# "pdp.decide outcome=error is a backend failure, not a decision".

_ALLOW_DOC = {
    "effect": "allow",
    "tier": 0,
    "reason": "ok",
    "obligations": {},
    "policy_version": "1.0.0",
}

_A_REQUEST = PolicyRequest(
    principal=Principal(id="u1"),
    action="read.document",
    resource="doc:1",
    context={},
)


def _pdp_over(handler: Callable[[httpx.Request], httpx.Response], sink: Any) -> GovernedPdp:
    return GovernedPdp(make_opa_client(httpx.MockTransport(handler)), sink)


def test_retry_then_success_emits_exactly_one_success_row() -> None:
    """The swallowed attempt is NOT a decision — it must not enter the ledger."""
    state = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ReadTimeout("")
        return httpx.Response(200, json={"result": _ALLOW_DOC})

    sink = RecordingAuditSink()
    decision = asyncio.run(_pdp_over(handler, sink).decide(_A_REQUEST))

    assert decision.effect.value == "allow"
    assert state["n"] == 2
    assert len(sink.events) == 1
    event = cast(AuditEvent, sink.events[0])
    assert event.outcome == AuditOutcome.success
    assert event.provenance["effect"] == "allow"


def test_exhausted_retries_emit_one_error_row_and_re_raise() -> None:
    """Fail closed: an exhausted retry is audited as ``error`` and re-raised."""
    state = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        raise httpx.ReadTimeout("")

    sink = RecordingAuditSink()
    with pytest.raises(PolicyBackendError):
        asyncio.run(_pdp_over(handler, sink).decide(_A_REQUEST))

    assert state["n"] == 2
    assert len(sink.events) == 1
    event = cast(AuditEvent, sink.events[0])
    assert event.outcome == AuditOutcome.error
    assert event.provenance["effect"] == "error"
    assert event.provenance["policy_version"] == "unavailable"
    assert "ReadTimeout" in str(event.detail)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(httpx.ConnectTimeout(""), id="ConnectTimeout"),
        pytest.param(httpx.PoolTimeout(""), id="PoolTimeout"),
        pytest.param(httpx.WriteTimeout(""), id="WriteTimeout"),
    ],
)
def test_a_not_retried_timeout_emits_one_error_row_after_one_attempt(
    failure: Exception,
) -> None:
    """The non-ReadTimeout timeout shapes refuse immediately, and are audited."""
    state = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        raise failure

    sink = RecordingAuditSink()
    with pytest.raises(PolicyBackendError):
        asyncio.run(_pdp_over(handler, sink).decide(_A_REQUEST))

    assert state["n"] == 1
    assert len(sink.events) == 1
    event = cast(AuditEvent, sink.events[0])
    assert event.outcome == AuditOutcome.error
    assert type(failure).__name__ in str(event.detail)
