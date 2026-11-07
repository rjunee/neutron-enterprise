# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ApprovalOutcome.decision_event_id`` — additive, defaulted (ADR-0017, 1.14.0).

Backward-compat: a pre-1.14.0-shaped payload (no ``decision_event_id`` key)
validates with the field ``None``. A supplied value round-trips exactly. The
model stays frozen.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.contracts.governance import ApprovalOutcome, ApprovalStatus


def test_decision_event_id_defaults_to_none() -> None:
    outcome = ApprovalOutcome(status=ApprovalStatus.APPROVED, approver="a")
    assert outcome.decision_event_id is None

    # A 1.13-shaped dump (no `decision_event_id` key at all) still validates,
    # and the field lands on the default, not merely "not present".
    pre_1_14_payload = {"status": "approved", "approver": "a", "decided_at": None}
    rehydrated = ApprovalOutcome.model_validate(pre_1_14_payload)
    assert rehydrated.decision_event_id is None


def test_decision_event_id_round_trips() -> None:
    outcome = ApprovalOutcome(
        status=ApprovalStatus.REJECTED,
        approver="carol",
        decision_event_id="8f14e45f-ceea-467e-adc9-15476f4a0a3c",
    )
    dumped = outcome.model_dump()
    rehydrated = ApprovalOutcome.model_validate(dumped)
    assert rehydrated.decision_event_id == "8f14e45f-ceea-467e-adc9-15476f4a0a3c"
    assert rehydrated.decision_event_id == outcome.decision_event_id


def test_outcome_stays_frozen() -> None:
    outcome = ApprovalOutcome(status=ApprovalStatus.APPROVED, approver="a")
    with pytest.raises(ValidationError):
        outcome.decision_event_id = "some-other-id"  # type: ignore[misc]
