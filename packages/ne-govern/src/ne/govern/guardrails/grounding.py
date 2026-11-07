# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Verified-or-refuse grounding output guard (C6/C12, NE-GA-G2).

Deterministic, sync, stateless. Activates ONLY on the ``grounded_gate`` flag
that NE-GA-G3's runtime post-step will set; ``build_tool_call_payload`` never
sets it, so this rail is inert for all normal tool-call traffic. Per C12, the
verdict detail carries no finding values — the G1 gate already reduces
findings to class+count.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ne.contracts.governance import GuardrailVerdict
from ne.govern.guardrails._common import failed, passed


class GroundingGuardrail:
    """Fails ONLY when the grounding gate marked a turn ungrounded
    (``grounding.ungrounded-claim``); passes every other payload — zero false
    blocks on normal tool calls."""

    def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict:
        if payload.get("grounded_gate") and not payload.get("grounded"):
            return failed(
                "grounding.ungrounded-claim",
                "drafted answer contained content not grounded in this turn's verified data",
            )
        if payload.get("grounded_gate"):
            return passed("grounding.grounded")
        return passed("grounding.not-applicable")
