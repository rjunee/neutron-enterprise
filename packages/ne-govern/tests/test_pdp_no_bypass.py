# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C6 no-bypass, by construction: audit failure or OPA failure both fail closed."""

from __future__ import annotations

import asyncio
import inspect
from typing import cast

import pytest

import ne.govern
from ne.contracts.governance import PolicyRequest
from ne.contracts.types import Principal
from ne.govern import GovernedPdp, create_pdp
from ne.govern.errors import AuditEmissionError, PolicyBackendError
from ne.schema import AuditEvent, AuditOutcome

from .conftest import (
    FailingAuditSink,
    RecordingAuditSink,
    make_opa_client,
    make_pdp,
    stub_opa_transport,
)

_GOOD = {"effect": "allow", "tier": 0, "reason": "ok", "obligations": {}, "policy_version": "1.0.0"}
_REQUEST = PolicyRequest(principal=Principal(id="u1"), action="read.x", resource="r")


def test_audit_append_failure_withholds_the_decision() -> None:
    pdp = make_pdp(_GOOD, FailingAuditSink())
    with pytest.raises(AuditEmissionError):
        asyncio.run(pdp.decide(_REQUEST))


def test_opa_failure_is_audited_as_error_then_raised() -> None:
    sink = RecordingAuditSink()
    pdp = GovernedPdp(make_opa_client(stub_opa_transport(_GOOD, status=500)), sink)
    with pytest.raises(PolicyBackendError):
        asyncio.run(pdp.decide(_REQUEST))
    assert len(sink.events) == 1
    assert cast(AuditEvent, sink.events[0]).outcome == AuditOutcome.error


def test_opa_failure_plus_audit_failure_raises_audit_emission_error() -> None:
    pdp = GovernedPdp(make_opa_client(stub_opa_transport(_GOOD, status=500)), FailingAuditSink())
    with pytest.raises(AuditEmissionError):
        asyncio.run(pdp.decide(_REQUEST))


def test_governed_pdp_without_sink_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        GovernedPdp(make_opa_client(stub_opa_transport(_GOOD)))  # type: ignore[call-arg]


def test_create_pdp_without_sink_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        create_pdp()  # type: ignore[call-arg]


def test_exactly_one_exported_class_defines_async_decide() -> None:
    deciders = []
    for name in ne.govern.__all__:
        obj = getattr(ne.govern, name)
        if isinstance(obj, type):
            decide = getattr(obj, "decide", None)
            if decide is not None and inspect.iscoroutinefunction(decide):
                deciders.append(obj)
    assert deciders == [GovernedPdp]
