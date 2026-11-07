# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""tier_for_action mirrors ``ne-govern/policies/ne/govern/tiers.rego`` exactly."""

from __future__ import annotations

import pytest

from ne.contracts.types import ApprovalTier
from ne.runtime.actions import tier_for_action
from ne.runtime.errors import ToolRegistrationError


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("read.docs.search", ApprovalTier.TIER_0),
        ("draft.email.compose", ApprovalTier.TIER_0),
        ("write.internal.notes.append", ApprovalTier.TIER_1),
        ("write.external.email.send", ApprovalTier.TIER_2),
        ("crm.pii.lookup", ApprovalTier.TIER_2),
        ("finance.payment.initiate", ApprovalTier.TIER_3),
        ("legal.contract.sign", ApprovalTier.TIER_3),
        # max-of-matches: external + pii still 2
        ("write.external.crm.pii.update", ApprovalTier.TIER_2),
        # max-of-matches: finance + pii => 3 (finance wins)
        ("finance.report.pii.export", ApprovalTier.TIER_3),
    ],
)
def test_tier_classification(action: str, expected: ApprovalTier) -> None:
    assert tier_for_action(action) == expected


def test_unclassified_action_raises() -> None:
    with pytest.raises(ToolRegistrationError):
        tier_for_action("mystery.action")


def test_returned_value_is_an_approval_tier() -> None:
    assert isinstance(tier_for_action("read.x"), ApprovalTier)
