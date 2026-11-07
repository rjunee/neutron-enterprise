# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ne-runtime error family is catchable at the API's RuntimeError seams.

U2 adversarial review, F8: ``NeRuntimeError`` subclassed plain ``Exception``,
so a typed refusal like ``AuthorizationExpiredError`` sailed past every
``except RuntimeError`` handler in ne-api (approvals.py approve/reject/payload)
and surfaced as an unaudited 500. These pins keep the hierarchy honest.
"""

from __future__ import annotations

from ne.runtime.errors import (
    ApprovalSourceWithheldFromApproverError,
    NeRuntimeError,
)
from ne.runtime.source_results import AuthorizationExpiredError


def test_ne_runtime_errors_are_runtime_errors() -> None:
    assert issubclass(NeRuntimeError, RuntimeError)
    assert issubclass(AuthorizationExpiredError, RuntimeError)


def test_source_withheld_from_approver_is_distinguishable_before_the_generic_catch() -> None:
    # The API catches this subclass FIRST (403, request stays pending) and only
    # then falls back to the generic RuntimeError mapping (503 retryable) — the
    # subclass relationship must support that ordering.
    assert issubclass(ApprovalSourceWithheldFromApproverError, NeRuntimeError)
    assert issubclass(ApprovalSourceWithheldFromApproverError, RuntimeError)
