# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CorrectionSignalCapture acceptance spine (NE-064): real sqlite store + real
ne-events outbox, asyncio.run convention (no pytest-asyncio, repo convention).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from ne.contracts import CorrectionSignalLike, Principal, SignalEmitter
from ne.events import read_batch
from ne.flywheel.capture import CorrectionSignalCapture
from ne.flywheel.errors import SignalRejectedError
from ne.flywheel.signals import (
    ApprovalRejectionSignal,
    DraftEditSignal,
    ReQuerySignal,
    SignalProvenance,
    SignalSurface,
    ThumbsSignal,
    WrongSourceSignal,
    build_correction_signal,
)
from ne.schema import CorrectionKind, CorrectionSignal


def _fresh_consumer() -> str:
    return f"ne065-{uuid.uuid4().hex}"


@pytest.mark.parametrize(
    ("payload", "expected_kind"),
    [
        (ThumbsSignal(up=True, target_ref="ans-1"), CorrectionKind.thumbs_up),
        (ThumbsSignal(up=False, target_ref="ans-2"), CorrectionKind.thumbs_down),
        (DraftEditSignal(draft_ref="d-1", edit_ratio=0.3), CorrectionKind.edit_distance),
        (
            WrongSourceSignal(answer_ref="ans-3", citation_ref="cite-1"),
            CorrectionKind.wrong_source,
        ),
        (
            ReQuerySignal(original_ref="q-1", requery_ref="q-2"),
            CorrectionKind.re_query,
        ),
        (
            ApprovalRejectionSignal(approval_id="appr-1", tier=2, reason="risk"),
            CorrectionKind.approval_rejection,
        ),
    ],
)
def test_each_signal_kind_captured_stored_and_retrievable(
    engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
    payload: object,
    expected_kind: CorrectionKind,
) -> None:
    capture = CorrectionSignalCapture(engine)
    prov = make_provenance()

    async def go() -> str:
        return await capture.capture(prov, payload)  # type: ignore[arg-type]

    event_id = asyncio.run(go())

    rows = capture.store.read_signals()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_id == event_id
    assert row.signal_kind == expected_kind.value
    assert row.principal_id == prov.principal.id
    assert row.surface == prov.surface.value
    assert row.session_id == prov.session_id
    assert row.turn_id == prov.turn_id
    assert row.message_id == prov.message_id
    assert row.actor == prov.principal.id
    assert row.config_hash == frozen_config


def test_capture_appends_to_outbox_atomically(
    engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    capture = CorrectionSignalCapture(engine)
    prov = make_provenance()

    async def go() -> str:
        return await capture.capture(prov, ThumbsSignal(up=True, target_ref="ans-1"))

    event_id = asyncio.run(go())

    with engine.begin() as conn:
        records, _ = read_batch(conn, _fresh_consumer(), event_types=["correction_signal"])
    assert len(records) == 1
    assert records[0].event_id == event_id

    store_rows = capture.store.read_signals()
    assert len(store_rows) == 1
    assert store_rows[0].event_id == event_id


def test_recapture_same_event_id_is_idempotent_in_both_tables(
    engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    capture = CorrectionSignalCapture(engine)
    prov = make_provenance()

    async def go() -> tuple[str, str]:
        first = await capture.capture(
            prov, ThumbsSignal(up=True, target_ref="ans-1"), event_id="ev-recapture"
        )
        second = await capture.capture(
            prov, ThumbsSignal(up=True, target_ref="ans-1"), event_id="ev-recapture"
        )
        return first, second

    first, second = asyncio.run(go())
    assert first == second == "ev-recapture"

    assert len(capture.store.read_signals()) == 1
    with engine.begin() as conn:
        records, _ = read_batch(conn, _fresh_consumer(), event_types=["correction_signal"])
    assert len(records) == 1


def test_unauthenticated_capture_rejected(
    engine: sa.Engine,
    frozen_config: str,
) -> None:
    capture = CorrectionSignalCapture(engine)

    # An unauthenticated (empty-principal) provenance is rejected fail-closed
    # AT construction — capture() is never even reached, so nothing about a
    # missing-principal signal is ever built, stored, or emitted.
    with pytest.raises(ValidationError):
        SignalProvenance(
            principal=Principal(id=""), surface=SignalSurface.CONSOLE, session_id="sess-1"
        )

    assert capture.store.read_signals() == []
    with engine.begin() as conn:
        records, _ = read_batch(conn, _fresh_consumer(), event_types=["correction_signal"])
    assert records == []


def _hand_build_signal(
    *, provenance: dict[str, str] | None = None, **overrides: object
) -> CorrectionSignal:
    fields: dict[str, object] = {
        "event_id": str(uuid.uuid4()),
        "event_type": "correction_signal",
        "config_hash": "a" * 64,
        "actor": "user:hand@acme.com",
        "provenance": provenance if provenance is not None else {},
        "occurred_at": datetime.now(UTC),
        "signal_kind": CorrectionKind.thumbs_up,
        "subject_ref": "ans-1",
    }
    fields.update(overrides)
    return CorrectionSignal(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "provenance",
    [
        {"principal": "user:x@acme.com", "session_id": "sess-1"},  # missing surface
        {"surface": "console", "session_id": "sess-1"},  # missing principal
        {"surface": "console", "principal": "user:x@acme.com"},  # missing session_id
    ],
)
def test_low_level_emit_rejects_missing_surface_or_principal_or_session(
    engine: sa.Engine, provenance: dict[str, str]
) -> None:
    capture = CorrectionSignalCapture(engine)
    signal = _hand_build_signal(provenance=provenance)

    async def go() -> None:
        await capture.emit(signal)

    with pytest.raises(SignalRejectedError):
        asyncio.run(go())

    assert capture.store.read_signals() == []
    with engine.begin() as conn:
        records, _ = read_batch(conn, _fresh_consumer(), event_types=["correction_signal"])
    assert records == []


def test_low_level_emit_rejects_unknown_kind_and_wrong_event_type(
    engine: sa.Engine,
) -> None:
    capture = CorrectionSignalCapture(engine)
    full_provenance = {
        "surface": "console",
        "principal": "user:x@acme.com",
        "session_id": "sess-1",
    }

    wrong_type = _hand_build_signal(provenance=full_provenance, event_type="something_else")

    async def emit_wrong_type() -> None:
        await capture.emit(wrong_type)

    with pytest.raises(SignalRejectedError):
        asyncio.run(emit_wrong_type())

    # An "unknown kind" duck-typed object: bypass pydantic enum validation via
    # model_construct so the belt-and-braces runtime check in emit() is
    # actually exercised (a real CorrectionSignal cannot hold an out-of-enum
    # signal_kind through normal validated construction).
    unknown_kind = CorrectionSignal.model_construct(
        event_id=str(uuid.uuid4()),
        event_type="correction_signal",
        config_hash="a" * 64,
        actor="user:x@acme.com",
        provenance=full_provenance,
        occurred_at=datetime.now(UTC),
        signal_kind="not_a_real_kind",
        subject_ref="ans-1",
        value=None,
        comment=None,
        kpi_tag=None,
        trace_id=None,
    )

    async def emit_unknown_kind() -> None:
        await capture.emit(unknown_kind)

    with pytest.raises(SignalRejectedError):
        asyncio.run(emit_unknown_kind())

    assert capture.store.read_signals() == []


def test_comment_secrets_redacted_in_store_row_and_outbox_payload(
    engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    capture = CorrectionSignalCapture(engine)
    prov = make_provenance()
    secret_comment = "leaked token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 in here"

    async def go() -> str:
        return await capture.capture(
            prov,
            WrongSourceSignal(answer_ref="ans-1", citation_ref="cite-1", comment=secret_comment),
        )

    event_id = asyncio.run(go())

    row = capture.store.read_signals()[0]
    assert row.comment is not None
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in row.comment
    assert "[REDACTED]" in row.comment
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in row.payload.get("comment", "")

    with engine.begin() as conn:
        records, _ = read_batch(conn, _fresh_consumer(), event_types=["correction_signal"])
    outbox_comment = records[0].payload.get("comment") or ""
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in outbox_comment
    assert "[REDACTED]" in outbox_comment
    assert records[0].event_id == event_id


def test_max_text_chars_cap_truncates_stored_comment(
    engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    # Codex P2: the FlywheelSettings.max_text_chars privacy/storage cap must
    # actually reduce persisted free-text — a lowered cap threaded through the
    # capture service truncates the stored comment (belt-and-braces at the emit seam).
    capture = CorrectionSignalCapture(engine, max_text_chars=20)
    prov = make_provenance()
    long_comment = "x" * 500

    async def go() -> None:
        await capture.capture(
            prov,
            WrongSourceSignal(answer_ref="ans-1", citation_ref="cite-1", comment=long_comment),
        )

    asyncio.run(go())
    row = capture.store.read_signals()[0]
    assert row.comment is not None
    assert len(row.comment) == 20
    assert len(row.payload.get("comment", "")) == 20


def test_capture_satisfies_signal_emitter_protocol(
    engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    capture = CorrectionSignalCapture(engine)
    # Typed assignment: pyright enforces CorrectionSignalCapture's structural
    # conformance to the frozen SignalEmitter Protocol at this line.
    emitter: SignalEmitter = capture
    prov = make_provenance()
    signal = build_correction_signal(prov, ThumbsSignal(up=True, target_ref="ans-1"))

    async def go() -> None:
        await emitter.emit(signal)

    asyncio.run(go())
    rows = capture.store.read_signals()
    assert len(rows) == 1
    assert rows[0].event_id == signal.event_id
    assert isinstance(signal, CorrectionSignalLike)
