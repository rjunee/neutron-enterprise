# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""VerifiedProvenance + AgentRunResult.verified — additive, optional, frozen
(ADR-0009, 1.6.0).

Structural verified-answer provenance (design docs/design/
ne-verified-answer-first-chat.md §Layer-0). It must be optional on the
carrier (default None so every pre-1.6.0 AgentRunResult constructor stays
valid), frozen + extra='forbid' on the provenance model itself (Checkpoint-A
immutability + hardening for a provenance carrier), and the pre-1.6.0
exclude-none serialization must stay byte-identical (pinned against the
exact literal captured at HEAD before this change).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.contracts.runtime import AgentRunResult, VerifiedProvenance

# Pinned at HEAD 578aebd, before any NE-VAF-U0 edit:
#   AgentRunResult(output='x', session_id='s').model_dump_json(exclude_none=True)
_PRE_U0_SERIALIZATION = '{"output":"x","session_id":"s"}'


def _make_provenance(**overrides: object) -> VerifiedProvenance:
    fields: dict[str, object] = {
        "template_id": "accounts-directory",
        "template_version": 1,
        "config_hash": "hhhhhhhh",
        "row_count": 3,
        "truncated": False,
    }
    fields.update(overrides)
    return VerifiedProvenance(**fields)  # type: ignore[arg-type]


def test_verified_provenance_is_frozen() -> None:
    provenance = _make_provenance()
    with pytest.raises(ValidationError):
        provenance.template_id = "other"  # type: ignore[misc]


def test_verified_provenance_rejects_extra() -> None:
    with pytest.raises(ValidationError):
        VerifiedProvenance.model_validate(
            {
                "template_id": "accounts-directory",
                "template_version": 1,
                "config_hash": "hhhhhhhh",
                "row_count": 3,
                "truncated": False,
                "smuggled": "mallory",
            }
        )


def test_verified_provenance_round_trips() -> None:
    provenance = _make_provenance()
    dumped = provenance.model_dump()
    restored = VerifiedProvenance.model_validate(dumped)
    assert restored == provenance
    assert restored.template_id == "accounts-directory"
    assert restored.template_version == 1
    assert restored.config_hash == "hhhhhhhh"
    assert restored.row_count == 3
    assert restored.truncated is False


def test_agent_run_result_verified_defaults_to_none() -> None:
    # Backward-compat construction: the pre-1.6.0 kwarg set stays valid.
    result = AgentRunResult(output="x", session_id="s")
    assert result.verified is None


def test_agent_run_result_pre_u0_serialization_byte_identical() -> None:
    # Load-bearing byte-identity pin: proves the additive field does not
    # change the serialized shape of a pre-1.6.0-style AgentRunResult.
    result = AgentRunResult(output="x", session_id="s")
    assert result.model_dump_json(exclude_none=True) == _PRE_U0_SERIALIZATION


def test_agent_run_result_round_trips_verified() -> None:
    provenance = _make_provenance()
    result = AgentRunResult(output="x", session_id="s", verified=provenance)
    dumped = result.model_dump()
    restored = AgentRunResult.model_validate(dumped)
    assert restored.verified == provenance
    assert isinstance(restored.verified, VerifiedProvenance)


def test_agent_run_result_stays_frozen() -> None:
    result = AgentRunResult(output="x", session_id="s")
    with pytest.raises(ValidationError):
        result.verified = _make_provenance()  # type: ignore[misc]
