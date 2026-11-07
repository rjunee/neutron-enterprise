# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TurnRequest / TurnFrame wire pins (NE-103c §5.1/§4.3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ne.api.turn_wire import (
    ProtectedTurnRef,
    TurnFrame,
    TurnRequest,
    TurnStepResult,
    citations_frame,
    done_frame,
    error_frame,
    token_frame,
)
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.runtime import GroundedProvenance, VerifiedProvenance
from ne.contracts.types import Lane, Principal

# Pinned at HEAD 578aebd, before any NE-VAF-U0 edit:
#   done_frame(1, 'sess-1', 'full text').model_dump_json(exclude_none=True)
_PRE_U0_DONE_FRAME_JSON = '{"seq":1,"type":"done","session_id":"sess-1","output":"full text"}'

# Pinned at HEAD 1ded8ae, before any NE-GA-G0 edit:
#   done_frame(1, 'sess-1', 'full text').model_dump_json(exclude_none=True)
_PRE_G0_DONE_FRAME_JSON = '{"seq":1,"type":"done","session_id":"sess-1","output":"full text"}'
_TRACE_ID = "0123456789abcdef0123456789abcdef"
_REQUEST_FINGERPRINT = "a" * 64
_IDEMPOTENCY_FINGERPRINT = "b" * 64
_ATTEMPT_ID = "attempt-wire"


def _protected() -> ProtectedTurnRef:
    return ProtectedTurnRef(
        protected_ref="protected-input",
        protected_hash="c" * 64,
        content_status="source_free",
    )


def _context(principal: Principal, department: str) -> AuthenticatedRequestContext:
    return AuthenticatedRequestContext(
        principal_id=principal.id,
        authorization_groups=tuple(sorted(set(principal.groups))),
        department=department,
        issuer="https://issuer.test",
        expires_at=4_102_444_800,
    )


def _binding() -> dict[str, str]:
    return {
        "trace_id": _TRACE_ID,
        "request_fingerprint": _REQUEST_FINGERPRINT,
        "idempotency_fingerprint": _IDEMPOTENCY_FINGERPRINT,
        "claimed_attempt_id": _ATTEMPT_ID,
    }


def _grounded() -> GroundedProvenance:
    return GroundedProvenance(
        fetches=(
            VerifiedProvenance(
                template_id="accounts-directory",
                template_version=1,
                config_hash="hhhhhhhh",
                row_count=3,
                truncated=False,
            ),
        ),
        citation_count=2,
        gate="ne-grounding-gate:1",
        fallback=False,
        assist_used=False,
        prompt_echo_count=0,
    )


def test_principal_round_trip_is_lossless() -> None:
    principal = Principal(
        id="user:ada",
        groups=("eng", "approver"),
        claims={"department": "engineering", "sid": "s-1", "email": "ada@ne.test"},
    )
    request = TurnRequest.from_principal(
        turn_id="turn-1",
        session_id="sess-1",
        agent_name="console-chat",
        department="engineering",
        protected_input=_protected(),
        authenticated_context=_context(principal, "engineering"),
        principal=principal,
        trace_id=_TRACE_ID,
        request_fingerprint=_REQUEST_FINGERPRINT,
        idempotency_fingerprint=_IDEMPOTENCY_FINGERPRINT,
        claimed_attempt_id=_ATTEMPT_ID,
    )
    # Through JSON and back — claims are JSON-clean (they came out of a JWT).
    restored = TurnRequest.model_validate_json(request.model_dump_json())
    assert restored.to_principal() == principal
    assert restored.trace_id == "0123456789abcdef0123456789abcdef"


def test_turn_request_rejects_non_trace_correlation() -> None:
    principal = Principal(id="user:real", claims={"department": "d"})
    with pytest.raises(ValidationError):
        TurnRequest.from_principal(
            turn_id="t",
            session_id="s",
            agent_name="a",
            department="d",
            protected_input=_protected(),
            authenticated_context=_context(principal, "d"),
            principal=principal,
            trace_id="turn-client-chosen",
            request_fingerprint=_REQUEST_FINGERPRINT,
            idempotency_fingerprint=_IDEMPOTENCY_FINGERPRINT,
            claimed_attempt_id=_ATTEMPT_ID,
        )


def test_legacy_request_payload_is_rejected_without_attempt_binding() -> None:
    with pytest.raises(ValidationError):
        TurnRequest.model_validate(
            {
                "turn_id": "turn-old",
                "session_id": "sess-old",
                "agent_name": "console-chat",
                "department": "engineering",
                "protected_input": _protected().model_dump(),
                "authenticated_context": _context(
                    Principal(id="user:ada", groups=("eng",)), "engineering"
                ).model_dump(),
                "principal_id": "user:ada",
                "principal_groups": ["eng"],
                "principal_claims": {"department": "engineering"},
            }
        )


def test_server_owned_turn_carriers_round_trip_exactly() -> None:
    request = TurnRequest.from_principal(
        turn_id="turn-model",
        session_id="sess-model",
        agent_name="console-chat",
        department="engineering",
        protected_input=_protected(),
        authenticated_context=_context(
            Principal(id="user:ada", claims={"department": "engineering"}), "engineering"
        ),
        principal=Principal(id="user:ada", claims={"department": "engineering"}),
        trace_id="abcdef0123456789abcdef0123456789",
        requested_model="frontier-anthropic",
        requested_model_lane=Lane.FRONTIER_API,
        request_fingerprint=_REQUEST_FINGERPRINT,
        idempotency_fingerprint=_IDEMPOTENCY_FINGERPRINT,
        claimed_attempt_id=_ATTEMPT_ID,
    )
    restored = TurnRequest.model_validate_json(request.model_dump_json())
    assert restored.trace_id == "abcdef0123456789abcdef0123456789"
    assert restored.requested_model == "frontier-anthropic"
    assert restored.requested_model_lane is Lane.FRONTIER_API


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        TurnRequest.model_validate(
            {
                "turn_id": "t",
                "session_id": "s",
                "agent_name": "a",
                "department": "d",
                "protected_input": _protected().model_dump(),
                "authenticated_context": _context(Principal(id="u"), "d").model_dump(),
                "principal_id": "u",
                "approver": "mallory",  # smuggled identity field
                **_binding(),
            }
        )


def test_from_principal_ignores_wire_identity() -> None:
    # The ONLY identity source is the Principal; there is no path to set principal_id
    # from anything but the bearer-derived principal.
    principal = Principal(id="user:real", groups=(), claims={"department": "d"})
    request = TurnRequest.from_principal(
        turn_id="t",
        session_id="s",
        agent_name="a",
        department="d",
        protected_input=_protected(),
        authenticated_context=_context(principal, "d"),
        principal=principal,
        trace_id=_TRACE_ID,
        request_fingerprint=_REQUEST_FINGERPRINT,
        idempotency_fingerprint=_IDEMPOTENCY_FINGERPRINT,
        claimed_attempt_id=_ATTEMPT_ID,
    )
    assert request.principal_id == "user:real"


def test_frame_sse_payloads_strip_seq() -> None:
    tok = token_frame(0, "hello")
    assert tok.sse_payload() == {"type": "token", "delta": "hello"}
    done = done_frame(1, "sess-1", "full text")
    assert done.sse_payload() == {"type": "done", "session_id": "sess-1", "output": "full text"}
    err = error_frame(0, ValueError("boom"))
    # Safe detail: the error CLASS NAME only (never the message for an unknown class).
    assert err.sse_payload() == {"type": "error", "detail": "ValueError"}


def test_citations_frame_and_step_carrier_round_trip() -> None:
    citations = [
        {
            "chunk_id": "c1",
            "document_id": "d1",
            "source_system": "wiki",
            "source_ref": "ref-1",
            "source_url": "https://wiki.example.com/records/d1",
        }
    ]
    frame = citations_frame(1, citations)
    restored_frame = TurnFrame.model_validate(frame.model_dump(mode="json"))
    assert restored_frame.sse_payload() == {"type": "citations", "citations": citations}

    restored_step = TurnStepResult.model_validate_json(
        TurnStepResult(output="answer", citations=citations).model_dump_json()
    )
    assert restored_step.citations == citations
    assert TurnStepResult.model_validate({"output": "legacy"}).citations == []


def test_done_frame_trace_round_trips_and_legacy_done_stays_absent() -> None:
    traced = done_frame(1, "sess-1", "full text", trace_id="trace-model-choice")
    restored = TurnFrame.model_validate(traced.model_dump(mode="json"))
    assert restored.trace_id == "trace-model-choice"
    assert restored.sse_payload() == {
        "type": "done",
        "session_id": "sess-1",
        "output": "full text",
        "trace_id": "trace-model-choice",
    }
    assert "trace_id" not in done_frame(1, "sess-1", "full text").sse_payload()


def test_frame_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        TurnFrame.model_validate({"seq": 0, "type": "token", "delta": "x", "smuggled": 1})


def test_non_done_frame_cannot_carry_a_protected_terminal() -> None:
    with pytest.raises(ValidationError, match="only a done frame"):
        TurnFrame(
            seq=0,
            type="token",
            delta="must-not-cross",
            protected_terminal=_protected(),
        )


def test_done_frame_cannot_claim_success_with_null_output() -> None:
    with pytest.raises(ValidationError, match="done frame requires exactly one"):
        TurnFrame.model_validate({"seq": 0, "type": "done", "session_id": "sess-1", "output": None})


def test_done_frame_without_verified_byte_identical() -> None:
    # Load-bearing byte-identity pin (NE-VAF-U0): proves the additive
    # ``verified`` field does not change the exclude-none serialization of a
    # done frame built the pre-U0 way (no verified kwarg at all).
    frame = done_frame(1, "sess-1", "full text")
    assert frame.model_dump_json(exclude_none=True) == _PRE_U0_DONE_FRAME_JSON


def test_done_frame_dbos_round_trip_verified_none() -> None:
    # The actual DBOS wire shape: relay.py:53 -> turn_relay.py:97, i.e.
    # model_dump(mode="json") then TurnFrame.model_validate on the other side.
    frame = done_frame(1, "sess-1", "full text")
    restored = TurnFrame.model_validate(frame.model_dump(mode="json"))
    assert restored == frame
    assert restored.verified is None


def test_done_frame_with_verified_round_trips() -> None:
    provenance = VerifiedProvenance(
        template_id="accounts-directory",
        template_version=1,
        config_hash="hhhhhhhh",
        row_count=3,
        truncated=False,
    )
    frame = done_frame(1, "sess-1", "full text", verified=provenance)
    assert frame.verified == provenance
    restored = TurnFrame.model_validate(frame.model_dump(mode="json"))
    assert restored.verified == provenance


def test_done_sse_payload_includes_verified_when_present() -> None:
    # U4 deliberately flips the U0 pin: the done payload now surfaces structural
    # provenance to the browser (enqueue==inline relay parity).
    provenance = VerifiedProvenance(
        template_id="accounts-directory",
        template_version=1,
        config_hash="hhhhhhhh",
        row_count=3,
        truncated=False,
    )
    frame = done_frame(1, "sess-1", "full text", verified=provenance)
    assert frame.sse_payload() == {
        "type": "done",
        "session_id": "sess-1",
        "output": "full text",
        "verified": {
            "template_id": "accounts-directory",
            "template_version": 1,
            "config_hash": "hhhhhhhh",
            "row_count": 3,
            "truncated": False,
        },
    }


def test_done_sse_payload_omits_verified_when_none() -> None:
    # Byte-identical default-off: no ``verified`` key when the field is None.
    frame = done_frame(1, "sess-1", "full text")
    assert frame.sse_payload() == {"type": "done", "session_id": "sess-1", "output": "full text"}


def test_turn_step_result_round_trips() -> None:
    provenance = VerifiedProvenance(
        template_id="accounts-directory",
        template_version=1,
        config_hash="hhhhhhhh",
        row_count=3,
        truncated=False,
    )
    with_verified = TurnStepResult(output="x", verified=provenance)
    restored = TurnStepResult.model_validate_json(with_verified.model_dump_json())
    assert restored.output == "x"
    assert restored.verified == provenance

    without_verified = TurnStepResult(output="x")
    restored_none = TurnStepResult.model_validate_json(without_verified.model_dump_json())
    assert restored_none.verified is None

    with pytest.raises(ValidationError):
        TurnStepResult.model_validate({"output": "x", "smuggled": 1})


def test_done_frame_without_grounded_byte_identical() -> None:
    # Load-bearing byte-identity pin (NE-GA-G0): proves the additive
    # ``grounded`` field does not change the exclude-none serialization of a
    # done frame built the pre-G0 way (no grounded kwarg at all).
    frame = done_frame(1, "sess-1", "full text")
    assert frame.model_dump_json(exclude_none=True) == _PRE_G0_DONE_FRAME_JSON


def test_done_frame_dbos_round_trip_grounded_none() -> None:
    # The actual DBOS wire shape: relay.py -> turn_relay.py, i.e.
    # model_dump(mode="json") then TurnFrame.model_validate on the other side.
    frame = done_frame(1, "sess-1", "full text")
    restored = TurnFrame.model_validate(frame.model_dump(mode="json"))
    assert restored == frame
    assert restored.grounded is None


def test_done_frame_with_grounded_round_trips() -> None:
    provenance = _grounded()
    frame = done_frame(1, "sess-1", "full text", grounded=provenance)
    assert frame.grounded == provenance
    restored = TurnFrame.model_validate(frame.model_dump(mode="json"))
    assert restored.grounded == provenance


def test_done_sse_payload_includes_grounded_when_present() -> None:
    # NE-GA-G0 ships the sse_payload surfacing immediately (mirroring the
    # verified mechanism, which already surfaces): the done payload carries
    # structural grounded-answer provenance to the browser when present.
    provenance = _grounded()
    frame = done_frame(1, "sess-1", "full text", grounded=provenance)
    assert frame.sse_payload() == {
        "type": "done",
        "session_id": "sess-1",
        "output": "full text",
        "grounded": {
            "fetches": [
                {
                    "template_id": "accounts-directory",
                    "template_version": 1,
                    "config_hash": "hhhhhhhh",
                    "row_count": 3,
                    "truncated": False,
                }
            ],
            "citation_count": 2,
            "gate": "ne-grounding-gate:1",
            "fallback": False,
            "assist_used": False,
            "prompt_echo_count": 0,
            "strictness": "strict",
        },
    }


def test_done_sse_payload_omits_grounded_when_none() -> None:
    # Byte-identical default-off: no ``grounded`` key when the field is None.
    frame = done_frame(1, "sess-1", "full text")
    assert frame.sse_payload() == {"type": "done", "session_id": "sess-1", "output": "full text"}


def test_done_frame_with_verified_and_grounded_coexist() -> None:
    verified_provenance = VerifiedProvenance(
        template_id="accounts-directory",
        template_version=1,
        config_hash="hhhhhhhh",
        row_count=3,
        truncated=False,
    )
    grounded_provenance = _grounded()
    frame = done_frame(
        1, "sess-1", "full text", verified=verified_provenance, grounded=grounded_provenance
    )
    dumped = frame.model_dump(mode="json")
    restored = TurnFrame.model_validate(dumped)
    assert restored.verified == verified_provenance
    assert restored.grounded == grounded_provenance
    payload = frame.sse_payload()
    assert "verified" in payload
    assert "grounded" in payload


def test_turn_step_result_round_trips_grounded() -> None:
    provenance = _grounded()
    with_grounded = TurnStepResult(output="x", grounded=provenance)
    restored = TurnStepResult.model_validate_json(with_grounded.model_dump_json())
    assert restored.output == "x"
    assert restored.grounded == provenance

    without_grounded = TurnStepResult(output="x")
    restored_none = TurnStepResult.model_validate_json(without_grounded.model_dump_json())
    assert restored_none.grounded is None
