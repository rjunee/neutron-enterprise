# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Verified-or-refuse output guard (C12, Layer 2 of verified-answer-first).

Deterministic, sync, stateless. Activates ONLY on the ``verified_domain`` flag
that NE-VAF-U3's ``VerifiedRouter`` will set; ``build_tool_call_payload`` never
sets it, so this rail is inert for all normal tool-call traffic. The refusal is
honest (SPEC §0-law-5) and never fabricates a ``template_id``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ne.contracts.governance import GuardrailVerdict
from ne.govern.guardrails._common import failed, passed


class VerifiedDomainGuardrail:
    """Fails ONLY when a verified-domain query did not resolve to a verified
    template (``verified.unrouted``); passes every other payload — zero false
    blocks on normal tool calls."""

    def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict:
        if payload.get("verified_domain") and not payload.get("routed"):
            return failed(
                "verified.unrouted",
                "query in a verified domain did not resolve to a verified template",
            )
        if payload.get("verified_domain"):
            return passed("verified.routed")
        return passed("verified.not-applicable")
