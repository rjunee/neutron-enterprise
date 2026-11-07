# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Action -> approval-tier classification (a code mirror of the Rego).

The single source of truth for tier classification at *decision* time is the OPA
Rego bundle: ``packages/ne-govern/policies/ne/govern/tiers.rego``
(``candidate_tiers`` + ``class_tier = max(candidate_tiers)``). :func:`tier_for_action`
is an EXACT mirror of that ``candidate_tiers`` logic, used here only for two
registration-time concerns that must fail loud before any decision is made:

  * validating that a tool binding's declared tier is not *below* its action
    class tier (:func:`ne.runtime.tools.validate_binding`), and
  * computing the :class:`~ne.contracts.governance.ApprovalRequest` tier when the
    PDP returns REQUIRE_APPROVAL.

The Rego stays AUTHORITATIVE at decision time — the PDP still decides allow /
deny / require-approval. This mirror never grants; an action matching no class
raises (deny-by-default, mirrored as a fail-loud registration error).
"""

from __future__ import annotations

from ne.contracts.types import ApprovalTier
from ne.runtime.errors import ToolRegistrationError


def tier_for_action(action: str) -> ApprovalTier:
    """Return the action-class tier (max-of-matches), mirroring ``tiers.rego``.

    Prefix/substring rules (identical to ``candidate_tiers`` in the Rego):

    * ``read.`` / ``draft.``            -> tier 0
    * ``write.internal.``               -> tier 1
    * ``write.external.`` / ``.pii.``   -> tier 2
    * ``finance.`` / ``legal.``         -> tier 3

    The effective tier is the STRICTEST (max) matching class. An action matching
    no class raises :class:`ToolRegistrationError` — deny-by-default, the same
    posture the Rego takes when ``candidate_tiers`` is empty (unclassified =>
    the PDP denies). Note the Rego also lets a caller-declared tier *escalate* an
    already-classified action; that escalation is the PDP's job at decision time,
    not this registration-time mirror's.
    """
    candidates: set[int] = set()
    if action.startswith("read.") or action.startswith("draft."):
        candidates.add(0)
    if action.startswith("write.internal."):
        candidates.add(1)
    if action.startswith("write.external."):
        candidates.add(2)
    if ".pii." in action:
        candidates.add(2)
    if action.startswith("finance.") or action.startswith("legal."):
        candidates.add(3)
    if not candidates:
        raise ToolRegistrationError(
            f"action {action!r} matches no approval-tier class (deny-by-default); "
            "tool names must classify under tiers.rego (read./draft./write.internal./"
            "write.external./*.pii.*/finance./legal.)"
        )
    return ApprovalTier(max(candidates))
