# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GroundedProvenance + AgentRunResult.grounded — additive, optional, frozen
(ADR-0010, 1.7.0).

Structural grounded-answer provenance (design docs/design/
ne-grounded-agent.md §6 G0). It must be optional on the carrier (default
None so every pre-1.7.0 AgentRunResult constructor stays valid), frozen +
extra='forbid' on the provenance model itself (Checkpoint-A immutability +
hardening for a provenance carrier), and the pre-1.7.0 exclude-none
serialization must stay byte-identical (pinned against the exact literal
captured at HEAD 1ded8ae, before any NE-GA-G0 edit).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.contracts.runtime import AgentRunResult, GroundedProvenance, VerifiedProvenance

# Pinned at HEAD 1ded8ae, before any NE-GA-G0 edit:
#   AgentRunResult(output='x', session_id='s').model_dump_json(exclude_none=True)
_PRE_G0_SERIALIZATION = '{"output":"x","session_id":"s"}'


def _make_fetch(**overrides: object) -> VerifiedProvenance:
    fields: dict[str, object] = {
        "template_id": "accounts-directory",
        "template_version": 1,
        "config_hash": "hhhhhhhh",
        "row_count": 3,
        "truncated": False,
    }
    fields.update(overrides)
    return VerifiedProvenance(**fields)  # type: ignore[arg-type]


def _make_grounded(**overrides: object) -> GroundedProvenance:
    fields: dict[str, object] = {
        "fetches": (_make_fetch(),),
        "citation_count": 2,
        "gate": "ne-grounding-gate:1",
        "fallback": False,
        "assist_used": False,
        "prompt_echo_count": 0,
    }
    fields.update(overrides)
    return GroundedProvenance(**fields)  # type: ignore[arg-type]


def test_grounded_provenance_is_frozen() -> None:
    provenance = _make_grounded()
    with pytest.raises(ValidationError):
        provenance.gate = "other"  # type: ignore[misc]


def test_grounded_provenance_rejects_extra() -> None:
    with pytest.raises(ValidationError):
        GroundedProvenance.model_validate(
            {
                "fetches": (),
                "citation_count": 0,
                "gate": "ne-grounding-gate:1",
                "fallback": False,
                "assist_used": False,
                "prompt_echo_count": 0,
                "smuggled": "mallory",
            }
        )


def test_grounded_provenance_round_trips() -> None:
    provenance = _make_grounded()
    dumped = provenance.model_dump()
    restored = GroundedProvenance.model_validate(dumped)
    assert restored == provenance
    assert restored.fetches == (_make_fetch(),)
    assert restored.citation_count == 2
    assert restored.gate == "ne-grounding-gate:1"
    assert restored.fallback is False
    assert restored.assist_used is False
    assert restored.prompt_echo_count == 0


def test_grounded_provenance_fetches_round_trip_as_verified_provenance() -> None:
    provenance = _make_grounded()
    dumped = provenance.model_dump()
    restored = GroundedProvenance.model_validate(dumped)
    assert isinstance(restored.fetches, tuple)
    assert len(restored.fetches) == 1
    assert isinstance(restored.fetches[0], VerifiedProvenance)
    assert restored.fetches[0] == _make_fetch()


def test_grounded_provenance_allows_empty_fetches() -> None:
    provenance = _make_grounded(fetches=())
    assert provenance.fetches == ()
    restored = GroundedProvenance.model_validate(provenance.model_dump())
    assert restored.fetches == ()
    assert restored == provenance


def test_agent_run_result_grounded_defaults_to_none() -> None:
    # Backward-compat construction: the pre-1.7.0 kwarg set stays valid.
    result = AgentRunResult(output="x", session_id="s")
    assert result.grounded is None


def test_agent_run_result_pre_g0_serialization_byte_identical() -> None:
    # Load-bearing byte-identity pin: proves the additive field does not
    # change the serialized shape of a pre-1.7.0-style AgentRunResult.
    result = AgentRunResult(output="x", session_id="s")
    assert result.model_dump_json(exclude_none=True) == _PRE_G0_SERIALIZATION


def test_agent_run_result_round_trips_grounded() -> None:
    provenance = _make_grounded()
    result = AgentRunResult(output="x", session_id="s", grounded=provenance)
    dumped = result.model_dump()
    restored = AgentRunResult.model_validate(dumped)
    assert restored.grounded == provenance
    assert isinstance(restored.grounded, GroundedProvenance)


def test_agent_run_result_stays_frozen() -> None:
    result = AgentRunResult(output="x", session_id="s")
    with pytest.raises(ValidationError):
        result.grounded = _make_grounded()  # type: ignore[misc]


def test_strictness_defaults_to_strict() -> None:
    # Backward-compat construction: the pre-1.10.0 kwarg set (via
    # _make_grounded, which never sets strictness) stays valid and lands
    # on the pre-1.10 gate posture.
    assert _make_grounded().strictness == "strict"


def test_strictness_explicit_value_round_trips() -> None:
    provenance = _make_grounded(strictness="standard")
    restored = GroundedProvenance.model_validate(provenance.model_dump())
    assert restored == provenance
    assert restored.strictness == "standard"


def test_pre_1_10_payload_without_strictness_validates_as_strict() -> None:
    # A legacy wire payload — exactly the six pre-1.10 keys, no strictness —
    # must still validate under extra="forbid" and default to "strict".
    provenance = GroundedProvenance.model_validate(
        {
            "fetches": (),
            "citation_count": 0,
            "gate": "ne-grounding-gate:1",
            "fallback": False,
            "assist_used": False,
            "prompt_echo_count": 0,
        }
    )
    assert provenance.strictness == "strict"


def test_strictness_is_frozen() -> None:
    provenance = _make_grounded()
    with pytest.raises(ValidationError):
        provenance.strictness = "standard"  # type: ignore[misc]


def test_model_dump_includes_strictness() -> None:
    # Pins the additive wire field the SSE done-frame gains.
    assert _make_grounded().model_dump()["strictness"] == "strict"
