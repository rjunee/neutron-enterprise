# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""OpaClient wire-protocol tests (MockTransport, no live OPA)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ne.contracts.governance import PolicyDecision
from ne.govern.errors import PolicyBackendError
from ne.govern.opa import OpaClient, _RawOpaResult

from .conftest import make_opa_client, stub_opa_transport

_GOOD = {
    "effect": "allow",
    "tier": 0,
    "reason": "ok",
    "obligations": {},
    "policy_version": "1.0.0",
}


def _evaluate(client: OpaClient, doc: dict) -> _RawOpaResult:
    async def run() -> _RawOpaResult:
        try:
            return await client.evaluate(doc)
        finally:
            await client.aclose()

    return asyncio.run(run())


def test_posts_input_to_decision_path_and_parses_result() -> None:
    client = make_opa_client(stub_opa_transport(_GOOD))
    raw = _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})
    assert isinstance(raw, _RawOpaResult)
    assert raw.effect == "allow"
    assert raw.tier == 0
    assert raw.policy_version == "1.0.0"


def test_evaluate_result_is_not_a_policy_decision() -> None:
    client = make_opa_client(stub_opa_transport(_GOOD))
    raw = _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})
    assert not isinstance(raw, PolicyDecision)


def test_http_500_fails_closed() -> None:
    client = make_opa_client(stub_opa_transport(_GOOD, status=500))
    with pytest.raises(PolicyBackendError):
        _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})


def test_connect_error_fails_closed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = make_opa_client(httpx.MockTransport(handler))
    with pytest.raises(PolicyBackendError):
        _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})


def test_undefined_result_fails_closed() -> None:
    client = make_opa_client(stub_opa_transport(omit_result=True))
    with pytest.raises(PolicyBackendError):
        _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})


def test_unknown_effect_fails_closed() -> None:
    bad = {**_GOOD, "effect": "maybe"}
    client = make_opa_client(stub_opa_transport(bad))
    with pytest.raises(PolicyBackendError):
        _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})


def test_missing_policy_version_fails_closed() -> None:
    bad = {**_GOOD, "policy_version": ""}
    client = make_opa_client(stub_opa_transport(bad))
    with pytest.raises(PolicyBackendError):
        _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})


def test_timeout_retries_once_then_succeeds() -> None:
    """A transport timeout (no answer at all) is re-asked exactly once.

    The production failure shape (NE demo, 2026-08-28): OPA answers in ~1ms but
    the CLIENT process stalls past the 5s budget, the request times out with an
    empty message, and an immediate re-ask succeeds. The retry converts that
    transient into the answer OPA was already serving. Fail-closed semantics are
    untouched: only httpx.ReadTimeout retries, and only once. (ReadTimeout is the
    ONE shape where the request was written and the answer never came back --
    the measured client stall. ConnectTimeout is a blackholed SYN, i.e. a total
    outage; PoolTimeout means the pool was exhausted and the request was never
    sent, so re-entering it adds load during exactly the saturation that caused
    it; WriteTimeout means the socket is already wedged. All three raise on the
    first attempt via the HTTPError arm, which the parametrised test below pins
    for every transport failure.)
    """
    state = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ReadTimeout("")
        return httpx.Response(200, json={"result": _GOOD})

    client = make_opa_client(httpx.MockTransport(handler))
    raw = _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})
    assert raw.effect == "allow"
    assert state["n"] == 2


def test_timeout_on_every_attempt_still_fails_closed_and_names_the_class() -> None:
    state = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        raise httpx.ReadTimeout("")

    client = make_opa_client(httpx.MockTransport(handler))
    with pytest.raises(PolicyBackendError) as excinfo:
        _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})
    assert state["n"] == 2
    # The audit reason must never be empty again: the class is named.
    assert "ReadTimeout" in str(excinfo.value)
    assert "2 attempts" in str(excinfo.value)


def test_http_500_is_not_retried() -> None:
    """A real answer — even a 500 — is never re-asked; only timeouts retry."""
    state = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(500, text="boom")

    client = make_opa_client(httpx.MockTransport(handler))
    with pytest.raises(PolicyBackendError):
        _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})
    assert state["n"] == 1


def test_connect_error_is_not_retried_and_names_the_class() -> None:
    state = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        raise httpx.ConnectError("connection refused")

    client = make_opa_client(httpx.MockTransport(handler))
    with pytest.raises(PolicyBackendError) as excinfo:
        _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})
    assert state["n"] == 1
    assert "ConnectError" in str(excinfo.value)


def test_deny_answer_is_not_retried() -> None:
    """A deny is a decision, not a failure — it flows through exactly once."""
    state = {"n": 0}
    deny = {**_GOOD, "effect": "deny", "reason": "no"}

    def handler(_request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(200, json={"result": deny})

    client = make_opa_client(httpx.MockTransport(handler))
    raw = _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})
    assert raw.effect == "deny"
    assert state["n"] == 1


# --- which transport failures are re-asked (F1) ---------------------------
#
# httpx 0.28.1 gives TimeoutException FOUR subclasses. Only ReadTimeout is the
# measured "client stalled after the request went out" shape the retry exists
# for; the other three are either a total outage (ConnectTimeout) or a
# never-sent request (PoolTimeout/WriteTimeout) and must fail closed on the
# first attempt. Every one of them still fails CLOSED — the question these
# pin is only how many times OPA is asked.

_TRANSPORT_FAILURES = [
    pytest.param(httpx.ReadTimeout(""), 2, id="ReadTimeout-retried"),
    pytest.param(httpx.ConnectTimeout(""), 1, id="ConnectTimeout-not-retried"),
    pytest.param(httpx.PoolTimeout(""), 1, id="PoolTimeout-not-retried"),
    pytest.param(httpx.WriteTimeout(""), 1, id="WriteTimeout-not-retried"),
    pytest.param(httpx.ConnectError("refused"), 1, id="ConnectError-not-retried"),
]


@pytest.mark.parametrize(("failure", "expected_attempts"), _TRANSPORT_FAILURES)
def test_only_read_timeout_is_re_asked(failure: Exception, expected_attempts: int) -> None:
    """Exactly one transport shape is retried; all of them fail closed."""
    state = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        raise failure

    client = make_opa_client(httpx.MockTransport(handler))
    with pytest.raises(PolicyBackendError) as excinfo:
        _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})
    assert state["n"] == expected_attempts
    # The class is always named, retried or not — the audit reason is never empty.
    assert type(failure).__name__ in str(excinfo.value)


# --- the swallowed attempt leaves a trace (F2) ----------------------------


def test_swallowed_retry_emits_a_warning_naming_the_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A retried-then-succeeded timeout MUST leave a server-side record.

    The audit ledger sees one clean success row (correct — the retry is not a
    decision), so the log line is the ONLY evidence the PDP failed to answer
    first time. Without it, worsening saturation where every first attempt
    times out reads as 100% clean decisions at 2x PDP load.
    """
    state = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ReadTimeout("")
        return httpx.Response(200, json={"result": _GOOD})

    client = make_opa_client(httpx.MockTransport(handler))
    with caplog.at_level("WARNING", logger="ne.govern.opa"):
        raw = _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})

    assert raw.effect == "allow"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "ReadTimeout" in message
    assert "ne/govern/decision" in message


def test_a_first_time_answer_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """No timeout, no warning — the log line means something when it appears."""
    client = make_opa_client(stub_opa_transport(_GOOD))
    with caplog.at_level("DEBUG", logger="ne.govern.opa"):
        _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})
    assert [r.getMessage() for r in caplog.records] == []


# --- no assert on the PDP path (F3) ---------------------------------------


def test_no_attempts_configured_still_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty attempt range must raise PolicyBackendError, not AttributeError.

    ``assert response is not None`` is stripped under PYTHONOPTIMIZE, and an
    AttributeError is NOT caught by ``GovernedPdp.decide``'s ``except
    PolicyBackendError`` — so it would escape with NO audit event at all.
    """
    monkeypatch.setattr("ne.govern.opa._TIMEOUT_ATTEMPTS", 0)
    client = make_opa_client(stub_opa_transport(_GOOD))
    with pytest.raises(PolicyBackendError):
        _evaluate(client, {"principal": {"id": "u1"}, "action": "read.x", "resource": "r"})


# --- approval_window_seconds parsing (ADR-0030, bundle 1.3.0) ---------------


def _req() -> dict:
    return {"principal": {"id": "u1"}, "action": "write.external.x", "resource": "r"}


def test_window_number_is_parsed_first_class() -> None:
    doc = dict(_GOOD, effect="require-approval", tier=2, approval_window_seconds=14400)
    raw = _evaluate(make_opa_client(stub_opa_transport(doc)), _req())
    assert raw.approval_window_seconds == 14400.0
    # NOT smuggled through obligations — that is the static Cedar parity surface.
    assert "approval_window_seconds" not in raw.obligations


def test_window_null_and_absent_both_mean_no_policy_window() -> None:
    explicit_null = dict(_GOOD, approval_window_seconds=None)
    raw = _evaluate(make_opa_client(stub_opa_transport(explicit_null)), _req())
    assert raw.approval_window_seconds is None
    # A pre-1.3.0 bundle omits the key entirely; that must parse identically.
    raw = _evaluate(make_opa_client(stub_opa_transport(dict(_GOOD))), _req())
    assert raw.approval_window_seconds is None


@pytest.mark.parametrize("bad", ["14400", True, False, [], {}])
def test_window_non_number_fails_closed(bad: object) -> None:
    doc = dict(_GOOD, effect="require-approval", tier=2, approval_window_seconds=bad)
    with pytest.raises(PolicyBackendError):
        _evaluate(make_opa_client(stub_opa_transport(doc)), _req())


# --- approval_window_declared parsing (bundle 1.4.0) ------------------------


def test_declared_bit_is_parsed_and_defaults_to_not_declared() -> None:
    """The provenance bit rides beside the window; absent means NOT declared.

    Absent is the pre-1.4.0 bundle. It must read FALSE rather than True,
    because the one consumer refuses on True and warns-and-proceeds on False:
    an old bundle has to degrade to the behaviour that keeps parks working, not
    to refusing parks it cannot justify.
    """
    declared = dict(
        _GOOD,
        effect="require-approval",
        tier=2,
        approval_window_seconds=180,
        approval_window_declared=True,
    )
    raw = _evaluate(make_opa_client(stub_opa_transport(declared)), _req())
    assert raw.approval_window_seconds == 180.0
    assert raw.approval_window_declared is True

    floor = dict(_GOOD, effect="require-approval", tier=2, approval_window_seconds=14400)
    raw = _evaluate(make_opa_client(stub_opa_transport(floor)), _req())
    assert raw.approval_window_seconds == 14400.0
    assert raw.approval_window_declared is False


@pytest.mark.parametrize("bad", ["true", "false", 1, 0, [], {}])
def test_declared_non_bool_fails_closed(bad: object) -> None:
    """Only a JSON boolean counts.

    ``bool("false")`` is True in Python, so a producer that serialised this bit
    as text would invert the check and start refusing parks that succeed today.
    """
    doc = dict(
        _GOOD,
        effect="require-approval",
        tier=2,
        approval_window_seconds=180,
        approval_window_declared=bad,
    )
    with pytest.raises(PolicyBackendError):
        _evaluate(make_opa_client(stub_opa_transport(doc)), _req())


def test_a_declaration_without_a_window_is_normalised_away() -> None:
    """No bundle rule can emit this pair, and a refusal must not rest on it.

    ``declared`` exists to justify refusing a park; with no window there is
    nothing to refuse against, so the incoherent pair is normalised to
    not-declared here rather than carried into the runtime.
    """
    doc = dict(_GOOD, approval_window_seconds=None, approval_window_declared=True)
    raw = _evaluate(make_opa_client(stub_opa_transport(doc)), _req())
    assert raw.approval_window_seconds is None
    assert raw.approval_window_declared is False


# --- standing_approval_seconds parsing (F1, bundle 1.5.0) -------------------
#
# The SECOND clock, parsed with the same discipline as the window. It has to be
# its own set of cases: a client that read one key into both fields would pass
# every window test above while collapsing two independent policy answers.


def test_standing_lifetime_number_is_parsed_first_class() -> None:
    doc = dict(
        _GOOD,
        effect="require-approval",
        tier=2,
        approval_window_seconds=14400,
        standing_approval_seconds=604800,
    )
    raw = _evaluate(make_opa_client(stub_opa_transport(doc)), _req())
    assert raw.standing_approval_seconds == 604800.0
    # The two clocks are distinct values, not one value read twice.
    assert raw.approval_window_seconds == 14400.0
    assert "standing_approval_seconds" not in raw.obligations


def test_standing_lifetime_null_and_absent_both_mean_no_standing_approval() -> None:
    explicit_null = dict(_GOOD, standing_approval_seconds=None)
    raw = _evaluate(make_opa_client(stub_opa_transport(explicit_null)), _req())
    assert raw.standing_approval_seconds is None
    # A pre-1.5.0 bundle omits the key entirely; that must parse identically,
    # and the runtime then mints NO standing approval (the fail-safe reading).
    raw = _evaluate(make_opa_client(stub_opa_transport(dict(_GOOD))), _req())
    assert raw.standing_approval_seconds is None


@pytest.mark.parametrize("bad", ["604800", True, False, [], {}])
def test_standing_lifetime_non_number_fails_closed(bad: object) -> None:
    doc = dict(_GOOD, effect="require-approval", tier=2, standing_approval_seconds=bad)
    with pytest.raises(PolicyBackendError):
        _evaluate(make_opa_client(stub_opa_transport(doc)), _req())
