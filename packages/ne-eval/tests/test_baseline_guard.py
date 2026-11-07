# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the NE-CKC6 fail-closed Checkpoint-C guard.

Exercises :func:`conftest.skip_or_fail_baseline` (via the ``baseline_guard``
fixture) directly: the ``NE_REQUIRE_PUBLIC_BASELINE`` flag flips the guard between
skip-if-unavailable (local dev, flag unset) and hard-fail (CI, flag == ``"1"``).
Only the literal ``"1"`` arms fail-closed; every other value keeps the skip.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

import pytest


def test_guard_skips_when_flag_unset(
    baseline_guard: Callable[[str], NoReturn],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NE_REQUIRE_PUBLIC_BASELINE", raising=False)
    with pytest.raises(pytest.skip.Exception) as ei:
        baseline_guard("bm25 gone")
    assert "LIVE-PARKED" in str(ei.value)
    assert "bm25 gone" in str(ei.value)


def test_guard_fails_when_flag_set(
    baseline_guard: Callable[[str], NoReturn],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NE_REQUIRE_PUBLIC_BASELINE", "1")
    with pytest.raises(pytest.fail.Exception) as ei:
        baseline_guard("bm25 gone")
    assert "NE_REQUIRE_PUBLIC_BASELINE=1" in str(ei.value)
    assert "bm25 gone" in str(ei.value)


def test_guard_skips_on_non_one_values(
    baseline_guard: Callable[[str], NoReturn],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value in ("0", ""):
        monkeypatch.setenv("NE_REQUIRE_PUBLIC_BASELINE", value)
        with pytest.raises(pytest.skip.Exception) as ei:
            baseline_guard("bm25 gone")
        assert "LIVE-PARKED" in str(ei.value)
