# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ApprovalOutcome.approval_id`` — additive, defaulted (ADR-0019, 1.16.0).

Backward-compat: a pre-1.16.0-shaped payload (no ``approval_id`` key)
validates with the field ``None``. A supplied value round-trips exactly. The
model stays frozen.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.contracts.governance import ApprovalOutcome, ApprovalStatus


def test_approval_id_defaults_to_none() -> None:
    outcome = ApprovalOutcome(status=ApprovalStatus.APPROVED, approver="a")
    assert outcome.approval_id is None

    # A 1.15-shaped dump (no `approval_id` key at all) still validates, and
    # the field lands on the default, not merely "not present".
    pre_1_16_payload = {
        "status": "approved",
        "approver": "a",
        "decided_at": None,
        "decision_event_id": None,
    }
    rehydrated = ApprovalOutcome.model_validate(pre_1_16_payload)
    assert rehydrated.approval_id is None


def test_approval_id_round_trips() -> None:
    outcome = ApprovalOutcome(
        status=ApprovalStatus.APPROVED,
        approver="carol",
        approval_id="2b7e1516-28ae-d2a6-abf7-158809cf4f3c",
    )
    dumped = outcome.model_dump()
    rehydrated = ApprovalOutcome.model_validate(dumped)
    assert rehydrated.approval_id == "2b7e1516-28ae-d2a6-abf7-158809cf4f3c"
    assert rehydrated.approval_id == outcome.approval_id


def test_outcome_stays_frozen() -> None:
    outcome = ApprovalOutcome(status=ApprovalStatus.APPROVED, approver="a")
    with pytest.raises(ValidationError):
        outcome.approval_id = "some-other-id"  # type: ignore[misc]
