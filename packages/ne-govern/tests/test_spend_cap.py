# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SpendCapGuardrail (C12): per-principal/department USD ceilings, exact Decimal."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ne.govern.config import SpendCapSettings
from ne.govern.errors import GuardrailConfigError
from ne.govern.guardrails import InMemorySpendLedger, SpendCapGuardrail, build_tool_call_payload

from .conftest import make_principal


def _guardrail(settings: SpendCapSettings, ledger: InMemorySpendLedger | None = None):
    return SpendCapGuardrail(settings, ledger or InMemorySpendLedger())


def _payload(principal, estimate):
    return build_tool_call_payload(
        principal=principal, tool="model.call", estimated_cost_usd=estimate
    )


def test_cap_exceeded_denies() -> None:
    g = _guardrail(SpendCapSettings(default_cap_usd="5.00"))
    verdict = g.check(_payload(make_principal(), "10.00"))
    assert not verdict.passed
    assert verdict.rule == "spend.cap-exceeded"


def test_under_cap_passes() -> None:
    g = _guardrail(SpendCapSettings(default_cap_usd="5.00"))
    assert g.check(_payload(make_principal(), "3.00")).passed


def test_principal_specific_cap_beats_default() -> None:
    settings = SpendCapSettings(default_cap_usd="100", principal_caps_usd={"user:1": "1"})
    g = _guardrail(settings)
    assert not g.check(_payload(make_principal("user:1"), "2")).passed
    assert g.check(_payload(make_principal("user:2"), "2")).passed


def test_department_cap_via_principal_claim() -> None:
    settings = SpendCapSettings(department_caps_usd={"eng": "2.00"})
    g = _guardrail(settings)
    principal = make_principal("user:1", department="eng")
    verdict = g.check(_payload(principal, "3.00"))
    assert not verdict.passed
    assert verdict.rule == "spend.cap-exceeded"


def test_missing_estimate_with_cap_configured_denies() -> None:
    g = _guardrail(SpendCapSettings(default_cap_usd="5.00"))
    verdict = g.check(build_tool_call_payload(principal=make_principal(), tool="model.call"))
    assert not verdict.passed
    assert verdict.rule == "spend.missing-estimate"


def test_negative_estimate_denies() -> None:
    g = _guardrail(SpendCapSettings(default_cap_usd="5.00"))
    verdict = g.check(_payload(make_principal(), "-1.00"))
    assert not verdict.passed
    assert verdict.rule == "spend.invalid-estimate"


def test_nan_estimate_denies() -> None:
    g = _guardrail(SpendCapSettings(default_cap_usd="5.00"))
    verdict = g.check(_payload(make_principal(), "NaN"))
    assert not verdict.passed
    assert verdict.rule == "spend.invalid-estimate"


def test_decimal_arithmetic_is_exact_no_float_drift() -> None:
    # 0.1 + 0.2 == 0.3 exactly in Decimal; a float sum would exceed the cap.
    ledger = InMemorySpendLedger()
    ledger.record("principal:user:1", Decimal("0.1"))
    g = _guardrail(SpendCapSettings(default_cap_usd="0.3"), ledger)
    assert g.check(_payload(make_principal("user:1"), "0.2")).passed


def test_no_caps_configured_passes() -> None:
    g = _guardrail(SpendCapSettings())
    assert g.check(_payload(make_principal(), "999999")).passed


def test_recorded_spend_accumulates_toward_cap() -> None:
    ledger = InMemorySpendLedger()
    ledger.record("principal:user:1", Decimal("4"))
    g = _guardrail(SpendCapSettings(default_cap_usd="5"), ledger)
    assert not g.check(_payload(make_principal("user:1"), "2")).passed
    assert g.check(_payload(make_principal("user:1"), "1")).passed


def test_malformed_default_cap_rejected_at_construction() -> None:
    with pytest.raises(GuardrailConfigError) as excinfo:
        SpendCapSettings(default_cap_usd="10O")
    assert "guardrails.spend.default_cap_usd" in str(excinfo.value)
    assert "10O" in str(excinfo.value)


def test_malformed_principal_cap_override_rejected_at_construction() -> None:
    with pytest.raises(GuardrailConfigError) as excinfo:
        SpendCapSettings(principal_caps_usd={"user:1": "1O.00"})
    assert "guardrails.spend.principal_caps_usd['user:1']" in str(excinfo.value)


def test_malformed_department_cap_override_rejected_at_construction() -> None:
    with pytest.raises(GuardrailConfigError) as excinfo:
        SpendCapSettings(department_caps_usd={"eng": "nope"})
    assert "guardrails.spend.department_caps_usd['eng']" in str(excinfo.value)


def test_nan_cap_rejected_at_construction() -> None:
    with pytest.raises(GuardrailConfigError) as excinfo:
        SpendCapSettings(default_cap_usd="NaN")
    assert "guardrails.spend.default_cap_usd" in str(excinfo.value)


def test_infinite_cap_rejected_at_construction() -> None:
    with pytest.raises(GuardrailConfigError):
        SpendCapSettings(default_cap_usd="Infinity")


def test_well_formed_cap_still_works() -> None:
    # Regression guard: the fix must not reject legitimate caps.
    settings = SpendCapSettings(
        default_cap_usd="100.00",
        principal_caps_usd={"user:1": "5.00"},
        department_caps_usd={"eng": "50.00"},
    )
    g = _guardrail(settings)
    assert not g.check(_payload(make_principal("user:1"), "6.00")).passed
    assert g.check(_payload(make_principal("user:2"), "99.00")).passed


def test_absent_cap_is_legitimately_uncapped_not_rejected() -> None:
    # 'No cap configured' (default_cap_usd=None, no override entries) is a valid,
    # documented config — distinct from a malformed cap string — and must still
    # pass straight through construction and evaluate as uncapped.
    settings = SpendCapSettings()  # every field at its None/{} default
    assert settings.default_cap_usd is None
    g = _guardrail(settings)
    verdict = g.check(_payload(make_principal(), "999999999"))
    assert verdict.passed
    assert verdict.rule == "spend.no-cap"


def test_parse_cap_raises_not_silently_uncapped_defense_in_depth() -> None:
    # Simulates a malformed cap slipping past construction (e.g. a dict mutated
    # after the settings object was built) reaching `_parse_cap` directly: it
    # must raise, never return None-as-uncapped.
    from ne.govern.guardrails.spend import _parse_cap

    with pytest.raises(ValueError, match="malformed spend cap"):
        _parse_cap("10O")
    with pytest.raises(ValueError, match="non-finite spend cap"):
        _parse_cap("NaN")
    assert _parse_cap(None) is None
    parsed = _parse_cap("5.00")
    assert parsed is not None and parsed.is_finite()
