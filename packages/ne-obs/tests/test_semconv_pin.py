# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pin-drift gate: our wrapper literals must match the EXACT-pinned semconv 0.64b0.

The GenAI semantic conventions are still in development (moved to the dedicated
``semantic-conventions-genai`` repo, no releases yet). If a future semconv bump
renames any of these constants, this test fails LOUDLY rather than our spans
silently drifting from the standard (SPEC §10). Our literals stay authoritative;
this gate only cross-checks them against the pinned package.
"""

from __future__ import annotations

from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as g

from ne.obs.genai import (
    ATTR_INPUT_TOKENS,
    ATTR_OPERATION_NAME,
    ATTR_OUTPUT_TOKENS,
    ATTR_PROVIDER_NAME,
    ATTR_REQUEST_MODEL,
    ATTR_RESPONSE_MODEL,
)

# Our literal -> the semconv package constant name it must equal.
_CROSS_CHECK = {
    ATTR_OPERATION_NAME: "GEN_AI_OPERATION_NAME",
    ATTR_PROVIDER_NAME: "GEN_AI_PROVIDER_NAME",
    ATTR_REQUEST_MODEL: "GEN_AI_REQUEST_MODEL",
    ATTR_RESPONSE_MODEL: "GEN_AI_RESPONSE_MODEL",
    ATTR_INPUT_TOKENS: "GEN_AI_USAGE_INPUT_TOKENS",
    ATTR_OUTPUT_TOKENS: "GEN_AI_USAGE_OUTPUT_TOKENS",
}


def test_wrapper_literals_match_pinned_semconv() -> None:
    for our_literal, const_name in _CROSS_CHECK.items():
        assert hasattr(g, const_name), (
            f"semconv 0.64b0 no longer defines {const_name}; our wrapper literal "
            f"{our_literal!r} is authoritative — review the semconv bump"
        )
        assert getattr(g, const_name) == our_literal


def test_provider_name_present_and_preferred_over_system() -> None:
    # gen_ai.provider.name (present since 0.57b0) supersedes the deprecated
    # gen_ai.system; we standardize on the former.
    assert g.GEN_AI_PROVIDER_NAME == "gen_ai.provider.name"
    assert ATTR_PROVIDER_NAME == g.GEN_AI_PROVIDER_NAME
