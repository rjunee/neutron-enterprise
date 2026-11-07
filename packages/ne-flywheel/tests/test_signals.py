# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed correction-signal contract tests (NE-064)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from ne.contracts import ConfigHashNotFrozenError, Principal
from ne.flywheel.signals import (
    ApprovalRejectionSignal,
    DraftEditSignal,
    ReQuerySignal,
    SignalProvenance,
    SignalSurface,
    ThumbsSignal,
    WrongSourceSignal,
    build_correction_signal,
    draft_edit_ratio,
)
from ne.schema import CorrectionKind


def test_thumbs_up_and_down_map_to_correct_kind(
    frozen_config: str, make_provenance: Callable[..., SignalProvenance]
) -> None:
    up = build_correction_signal(make_provenance(), ThumbsSignal(up=True, target_ref="answer-1"))
    down = build_correction_signal(make_provenance(), ThumbsSignal(up=False, target_ref="answer-2"))
    assert up.signal_kind == CorrectionKind.thumbs_up
    assert up.subject_ref == "answer-1"
    assert down.signal_kind == CorrectionKind.thumbs_down
    assert down.subject_ref == "answer-2"


def test_draft_edit_signal_maps_ratio_to_value(
    frozen_config: str, make_provenance: Callable[..., SignalProvenance]
) -> None:
    signal = build_correction_signal(
        make_provenance(), DraftEditSignal(draft_ref="draft-1", edit_ratio=0.42)
    )
    assert signal.signal_kind == CorrectionKind.edit_distance
    assert signal.subject_ref == "draft-1"
    assert signal.value == pytest.approx(0.42)


def test_draft_edit_ratio_helper_bounds() -> None:
    assert draft_edit_ratio("hello world", "hello world") == 0.0
    assert draft_edit_ratio("abcdef", "zyxwvu") == pytest.approx(1.0)
    for _ in range(5):
        ratio = draft_edit_ratio("the quick brown fox", "the slow brown ox")
        assert 0.0 <= ratio <= 1.0


@pytest.mark.parametrize(
    "build",
    [
        lambda: DraftEditSignal(draft_ref="d", edit_ratio=1.5),
        lambda: DraftEditSignal(draft_ref="d", edit_ratio=-0.1),
        lambda: ApprovalRejectionSignal(approval_id="a", tier=4),
        lambda: ApprovalRejectionSignal(approval_id="a", tier=-1),
        lambda: ThumbsSignal(up=True, target_ref=""),
        lambda: WrongSourceSignal(answer_ref="a", citation_ref=""),
        lambda: ApprovalRejectionSignal(approval_id="", tier=1),
    ],
)
def test_payload_validation_rejects_malformed(build: object) -> None:
    with pytest.raises(ValidationError):
        build()  # type: ignore[operator]


def test_wrong_source_and_re_query_stamp_kind_specific_provenance_keys(
    frozen_config: str, make_provenance: Callable[..., SignalProvenance]
) -> None:
    wrong_source = build_correction_signal(
        make_provenance(),
        WrongSourceSignal(answer_ref="answer-9", citation_ref="cite-3"),
    )
    assert wrong_source.provenance["answer_ref"] == "answer-9"
    assert wrong_source.subject_ref == "cite-3"

    re_query = build_correction_signal(
        make_provenance(),
        ReQuerySignal(original_ref="q-1", requery_ref="q-2", similarity=0.8),
    )
    assert re_query.provenance["requery_ref"] == "q-2"
    assert re_query.subject_ref == "q-1"

    rejection = build_correction_signal(
        make_provenance(),
        ApprovalRejectionSignal(approval_id="appr-7", tier=2, reason="too risky"),
    )
    assert rejection.provenance["approval_tier"] == "2"
    assert rejection.subject_ref == "appr-7"


def test_build_signal_stamps_canonical_provenance_and_config_hash(
    frozen_config: str, make_provenance: Callable[..., SignalProvenance]
) -> None:
    prov = make_provenance(
        principal_id="user:bob@acme.com",
        session_id="sess-42",
        turn_id="turn-1",
        message_id="msg-2",
    )
    signal = build_correction_signal(prov, ThumbsSignal(up=True, target_ref="ans"))
    assert signal.provenance["surface"] == "console"
    assert signal.provenance["principal"] == "user:bob@acme.com"
    assert signal.provenance["session_id"] == "sess-42"
    assert signal.provenance["turn_id"] == "turn-1"
    assert signal.provenance["message_id"] == "msg-2"
    assert signal.actor == "user:bob@acme.com"
    assert len(signal.config_hash) == 64
    int(signal.config_hash, 16)  # valid hex
    assert signal.config_hash == frozen_config


def test_build_signal_without_frozen_hash_raises(
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    from ne.contracts import reset_config_hash_for_testing

    reset_config_hash_for_testing()
    with pytest.raises(ConfigHashNotFrozenError):
        build_correction_signal(make_provenance(), ThumbsSignal(up=True, target_ref="ans"))


def test_provenance_rejects_empty_principal_and_session() -> None:
    with pytest.raises(ValidationError):
        SignalProvenance(
            principal=Principal(id=""), surface=SignalSurface.CONSOLE, session_id="sess-1"
        )
    with pytest.raises(ValidationError):
        SignalProvenance(
            principal=Principal(id="user:x@acme.com"),
            surface=SignalSurface.CONSOLE,
            session_id="",
        )
