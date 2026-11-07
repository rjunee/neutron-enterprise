# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed correction-signal contract: the five surface-facing payload models
(NE-064) mapping onto the frozen ``ne.schema.CorrectionSignal`` event whose
``CorrectionKind`` enum already enumerates all six kinds this flywheel
captures: thumbs up/down, draft edit-distance, wrong-source, re-query, and
approval-rejection.

Every payload model is FROZEN (pydantic, immutable) and range/emptiness
validated. :func:`build_correction_signal` is the ONLY path that turns a
payload into a durable event — it delegates to
``ne.events.new_correction_signal`` (never a hand-rolled event dict), so
config_hash stamping stays non-optional (``ConfigHashNotFrozenError``
propagates rather than an unstamped event being built).

Privacy (C12): raw draft/query text never transits or persists. Surfaces call
:func:`draft_edit_ratio` LOCALLY to reduce a before/after pair to a single
magnitude; only that magnitude plus an optional, scrubbed, capped
``diff_summary``/``comment``/``reason`` free-text field is ever stored.
"""

from __future__ import annotations

import difflib
import enum
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_core import PydanticCustomError

from ne.contracts.types import Principal
from ne.events import new_correction_signal
from ne.flywheel.hygiene import scrub_text
from ne.schema import CorrectionKind, CorrectionSignal


class SignalSurface(enum.StrEnum):
    """The surface a correction signal was emitted from."""

    CONSOLE = "console"
    SLACK = "slack"
    API = "api"


class SignalProvenance(BaseModel):
    """WHO/WHERE/WHEN a signal came from — required on every emit.

    A missing/empty ``principal.id`` (unauthenticated) or an empty
    ``session_id`` fails validation immediately (fail closed) — no signal is
    ever built without full provenance.
    """

    model_config = ConfigDict(frozen=True)

    principal: Principal
    surface: SignalSurface
    session_id: str
    turn_id: str | None = None
    message_id: str | None = None

    @field_validator("principal")
    @classmethod
    def _principal_authenticated(cls, v: Principal) -> Principal:
        if not v.id.strip():
            raise PydanticCustomError(
                "unauthenticated_principal", "principal.id must be a non-empty string"
            )
        return v

    @field_validator("session_id")
    @classmethod
    def _session_id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise PydanticCustomError("empty_session_id", "session_id must be a non-empty string")
        return v


class ThumbsSignal(BaseModel):
    """Explicit thumbs up/down feedback on an output."""

    model_config = ConfigDict(frozen=True)

    up: bool
    target_ref: str
    comment: str | None = None

    @field_validator("target_ref")
    @classmethod
    def _target_ref_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise PydanticCustomError("empty_target_ref", "target_ref must be non-empty")
        return v


class DraftEditSignal(BaseModel):
    """How much a user edited an AI-produced draft (magnitude only, no raw text)."""

    model_config = ConfigDict(frozen=True)

    draft_ref: str
    edit_ratio: float
    diff_summary: str | None = None

    @field_validator("draft_ref")
    @classmethod
    def _draft_ref_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise PydanticCustomError("empty_draft_ref", "draft_ref must be non-empty")
        return v

    @field_validator("edit_ratio")
    @classmethod
    def _edit_ratio_bounded(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise PydanticCustomError("edit_ratio_out_of_range", "edit_ratio must be in [0, 1]")
        return v


class WrongSourceSignal(BaseModel):
    """A citation on an answer was flagged as wrong or irrelevant."""

    model_config = ConfigDict(frozen=True)

    answer_ref: str
    citation_ref: str
    comment: str | None = None

    @field_validator("answer_ref")
    @classmethod
    def _answer_ref_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise PydanticCustomError("empty_answer_ref", "answer_ref must be non-empty")
        return v

    @field_validator("citation_ref")
    @classmethod
    def _citation_ref_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise PydanticCustomError("empty_citation_ref", "citation_ref must be non-empty")
        return v


class ReQuerySignal(BaseModel):
    """The user re-asked the same thing — implied dissatisfaction with the prior answer."""

    model_config = ConfigDict(frozen=True)

    original_ref: str
    requery_ref: str
    similarity: float | None = None

    @field_validator("original_ref")
    @classmethod
    def _original_ref_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise PydanticCustomError("empty_original_ref", "original_ref must be non-empty")
        return v

    @field_validator("requery_ref")
    @classmethod
    def _requery_ref_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise PydanticCustomError("empty_requery_ref", "requery_ref must be non-empty")
        return v

    @field_validator("similarity")
    @classmethod
    def _similarity_bounded(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 1.0):
            raise PydanticCustomError("similarity_out_of_range", "similarity must be in [0, 1]")
        return v


class ApprovalRejectionSignal(BaseModel):
    """A denied Tier-2/3 approval — itself a correction signal (NE-035 HITL)."""

    model_config = ConfigDict(frozen=True)

    approval_id: str
    tier: int
    reason: str | None = None

    @field_validator("approval_id")
    @classmethod
    def _approval_id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise PydanticCustomError("empty_approval_id", "approval_id must be non-empty")
        return v

    @field_validator("tier")
    @classmethod
    def _tier_bounded(cls, v: int) -> int:
        if not (0 <= v <= 3):
            raise PydanticCustomError("tier_out_of_range", "tier must be in [0, 3]")
        return v


SignalPayload = (
    ThumbsSignal | DraftEditSignal | WrongSourceSignal | ReQuerySignal | ApprovalRejectionSignal
)


def draft_edit_ratio(original: str, edited: str) -> float:
    """Return how much ``edited`` differs from ``original``, clamped to [0, 1].

    ``1.0 - difflib.SequenceMatcher(...).ratio()`` — 0.0 means identical, 1.0
    means wholly disjoint. Surfaces call this LOCALLY so raw draft text never
    transits or persists (C12/privacy): only the resulting magnitude (plus an
    optional, scrubbed, capped ``diff_summary``) travels to
    :class:`DraftEditSignal`.
    """
    ratio = difflib.SequenceMatcher(None, original, edited).ratio()
    distance = 1.0 - ratio
    return max(0.0, min(1.0, distance))


def _kind_for(payload: SignalPayload) -> CorrectionKind:
    if isinstance(payload, ThumbsSignal):
        return CorrectionKind.thumbs_up if payload.up else CorrectionKind.thumbs_down
    if isinstance(payload, DraftEditSignal):
        return CorrectionKind.edit_distance
    if isinstance(payload, WrongSourceSignal):
        return CorrectionKind.wrong_source
    if isinstance(payload, ReQuerySignal):
        return CorrectionKind.re_query
    if isinstance(payload, ApprovalRejectionSignal):
        return CorrectionKind.approval_rejection
    # pragma: no cover - SignalPayload is an exhaustive union of the five models above
    raise TypeError(f"unsupported signal payload type: {type(payload).__name__}")


def _subject_ref_for(payload: SignalPayload) -> str:
    if isinstance(payload, ThumbsSignal):
        return payload.target_ref
    if isinstance(payload, DraftEditSignal):
        return payload.draft_ref
    if isinstance(payload, WrongSourceSignal):
        return payload.citation_ref
    if isinstance(payload, ReQuerySignal):
        return payload.original_ref
    if isinstance(payload, ApprovalRejectionSignal):
        return payload.approval_id
    # pragma: no cover - SignalPayload is an exhaustive union of the five models above
    raise TypeError(f"unsupported signal payload type: {type(payload).__name__}")


def _value_for(payload: SignalPayload) -> float | None:
    if isinstance(payload, DraftEditSignal):
        return payload.edit_ratio
    return None


def _raw_comment_for(payload: SignalPayload) -> str | None:
    if isinstance(payload, ThumbsSignal):
        return payload.comment
    if isinstance(payload, DraftEditSignal):
        return payload.diff_summary
    if isinstance(payload, WrongSourceSignal):
        return payload.comment
    if isinstance(payload, ApprovalRejectionSignal):
        return payload.reason
    return None


def _kind_specific_provenance(payload: SignalPayload) -> dict[str, str]:
    if isinstance(payload, WrongSourceSignal):
        return {"answer_ref": payload.answer_ref}
    if isinstance(payload, ReQuerySignal):
        return {"requery_ref": payload.requery_ref}
    if isinstance(payload, ApprovalRejectionSignal):
        return {"approval_tier": str(payload.tier)}
    return {}


def build_correction_signal(
    provenance: SignalProvenance,
    payload: SignalPayload,
    *,
    kpi_tag: str | None = None,
    trace_id: str | None = None,
    event_id: str | None = None,
    occurred_at: datetime | None = None,
    max_text_chars: int = 4000,
) -> CorrectionSignal:
    """Build a typed, config-hash-stamped ``CorrectionSignal`` from a payload.

    Delegates ENTIRELY to ``ne.events.new_correction_signal`` — never
    hand-rolls the event dict — so config_hash stamping is non-optional
    (``ConfigHashNotFrozenError`` propagates from the frozen-hash resolver if
    the process has not frozen its config).

    The provenance map carries the canonical keys ``surface``/``principal``/
    ``session_id`` (plus ``turn_id``/``message_id`` when set) and the
    kind-specific key (``answer_ref``/``requery_ref``/``approval_tier``).
    ``comment``/``diff_summary``/``reason`` pass through
    :func:`~ne.flywheel.hygiene.scrub_text` first (C12).
    """
    prov: dict[str, str] = {
        "surface": provenance.surface.value,
        "principal": provenance.principal.id,
        "session_id": provenance.session_id,
    }
    if provenance.turn_id is not None:
        prov["turn_id"] = provenance.turn_id
    if provenance.message_id is not None:
        prov["message_id"] = provenance.message_id
    prov.update(_kind_specific_provenance(payload))

    raw_comment = _raw_comment_for(payload)
    comment = scrub_text(raw_comment, max_chars=max_text_chars) if raw_comment is not None else None

    return new_correction_signal(
        signal_kind=_kind_for(payload),
        subject_ref=_subject_ref_for(payload),
        actor=provenance.principal.id,
        value=_value_for(payload),
        comment=comment,
        kpi_tag=kpi_tag,
        trace_id=trace_id,
        provenance=prov,
        event_id=event_id,
        occurred_at=occurred_at,
    )


__all__ = [
    "ApprovalRejectionSignal",
    "DraftEditSignal",
    "ReQuerySignal",
    "SignalPayload",
    "SignalProvenance",
    "SignalSurface",
    "ThumbsSignal",
    "WrongSourceSignal",
    "build_correction_signal",
    "draft_edit_ratio",
]
