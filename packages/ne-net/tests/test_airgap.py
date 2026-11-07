# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Token matrix + re-export identity for the ONE AIR_GAP guard (NE-CKC10 / P2-23)."""

from __future__ import annotations

import pytest

import ne.net
import ne.net.airgap
from ne.net.airgap import AirGapViolationError, air_gap_engaged


@pytest.mark.parametrize("token", ["1", "true", "yes", "on", "TRUE", " on ", "Yes"])
def test_air_gap_truthy_tokens(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("AIR_GAP", token)
    assert air_gap_engaged() is True


@pytest.mark.parametrize("token", ["0", "false", "no", "off", "", "  ", "disabled"])
def test_air_gap_falsy_tokens(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("AIR_GAP", token)
    assert air_gap_engaged() is False


def test_air_gap_unset_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIR_GAP", raising=False)
    assert air_gap_engaged() is False


def test_air_gap_violation_error_is_runtime_error() -> None:
    assert issubclass(AirGapViolationError, RuntimeError)


def test_package_reexport_identity() -> None:
    # The ne.net package re-export is the SAME object as ne.net.airgap's.
    assert ne.net.air_gap_engaged is ne.net.airgap.air_gap_engaged
    assert ne.net.AirGapViolationError is ne.net.airgap.AirGapViolationError
