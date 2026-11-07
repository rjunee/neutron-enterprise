# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Real-sqlite-seam tests for SqlAlchemyCorrectionSignalStore (NE-064)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from ne.flywheel.signals import (
    DraftEditSignal,
    SignalProvenance,
    SignalSurface,
    ThumbsSignal,
    build_correction_signal,
)
from ne.flywheel.store import SqlAlchemyCorrectionSignalStore


def _append(
    engine: sa.Engine,
    store: SqlAlchemyCorrectionSignalStore,
    provenance: SignalProvenance,
) -> int:
    signal = build_correction_signal(provenance, ThumbsSignal(up=True, target_ref="ans-1"))
    with engine.begin() as conn:
        return store.append(
            conn,
            signal,
            surface=provenance.surface.value,
            principal_id=provenance.principal.id,
            session_id=provenance.session_id,
            turn_id=provenance.turn_id,
            message_id=provenance.message_id,
        )


def test_append_and_read_roundtrip_preserves_full_provenance(
    engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    store = SqlAlchemyCorrectionSignalStore(engine)
    prov = make_provenance(
        principal_id="user:carol@acme.com",
        session_id="sess-77",
        turn_id="turn-2",
        message_id="msg-5",
    )
    row_id = _append(engine, store, prov)

    rows = store.read_signals()
    assert len(rows) == 1
    row = rows[0]
    assert row.id == row_id
    assert row.principal_id == "user:carol@acme.com"
    assert row.session_id == "sess-77"
    assert row.turn_id == "turn-2"
    assert row.message_id == "msg-5"
    assert row.surface == "console"
    assert row.signal_kind == "thumbs_up"
    assert row.subject_ref == "ans-1"
    assert row.actor == "user:carol@acme.com"
    assert row.config_hash == frozen_config
    assert row.payload["event_id"] == row.event_id
    assert row.occurred_at.tzinfo is not None


def test_read_signals_orders_by_append_id_and_paginates_after_id(
    engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    store = SqlAlchemyCorrectionSignalStore(engine)
    ids = [_append(engine, store, make_provenance()) for _ in range(5)]
    assert ids == sorted(ids)

    all_rows = store.read_signals()
    assert [r.id for r in all_rows] == ids

    tail = store.read_signals(after_id=ids[1])
    assert [r.id for r in tail] == ids[2:]

    limited = store.read_signals(after_id=0, limit=2)
    assert [r.id for r in limited] == ids[:2]


def test_read_signals_filters_by_kind_surface_and_time_window(
    engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    store = SqlAlchemyCorrectionSignalStore(engine)

    console_prov = make_provenance(surface=SignalSurface.CONSOLE)
    slack_prov = make_provenance(surface=SignalSurface.SLACK)

    early = datetime(2026, 1, 1, tzinfo=UTC)
    late = datetime(2026, 6, 1, tzinfo=UTC)

    thumbs_signal = build_correction_signal(
        console_prov, ThumbsSignal(up=True, target_ref="ans-1"), occurred_at=early
    )
    with engine.begin() as conn:
        store.append(
            conn,
            thumbs_signal,
            surface=console_prov.surface.value,
            principal_id=console_prov.principal.id,
            session_id=console_prov.session_id,
        )

    edit_signal = build_correction_signal(
        slack_prov, DraftEditSignal(draft_ref="d-1", edit_ratio=0.5), occurred_at=late
    )
    with engine.begin() as conn:
        store.append(
            conn,
            edit_signal,
            surface=slack_prov.surface.value,
            principal_id=slack_prov.principal.id,
            session_id=slack_prov.session_id,
        )

    by_kind = store.read_signals(kinds=["edit_distance"])
    assert [r.signal_kind for r in by_kind] == ["edit_distance"]

    by_surface = store.read_signals(surface="console")
    assert [r.surface for r in by_surface] == ["console"]

    windowed = store.read_signals(since=datetime(2026, 5, 1, tzinfo=UTC))
    assert [r.signal_kind for r in windowed] == ["edit_distance"]

    windowed_early = store.read_signals(until=datetime(2026, 2, 1, tzinfo=UTC))
    assert [r.signal_kind for r in windowed_early] == ["thumbs_up"]


def test_append_is_idempotent_on_duplicate_event_id(
    engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    store = SqlAlchemyCorrectionSignalStore(engine)
    prov = make_provenance()
    signal = build_correction_signal(
        prov, ThumbsSignal(up=True, target_ref="ans-1"), event_id="ev-dup"
    )
    with engine.begin() as conn:
        first = store.append(
            conn,
            signal,
            surface=prov.surface.value,
            principal_id=prov.principal.id,
            session_id=prov.session_id,
        )
        again = store.append(
            conn,
            signal,
            surface=prov.surface.value,
            principal_id=prov.principal.id,
            session_id=prov.session_id,
        )
    assert first == again
    assert len(store.read_signals()) == 1


def test_append_rejects_naive_occurred_at(
    engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    prov = make_provenance()
    signal = build_correction_signal(prov, ThumbsSignal(up=True, target_ref="ans-1"))
    naive = signal.model_copy(update={"occurred_at": datetime(2026, 1, 1)})
    store = SqlAlchemyCorrectionSignalStore(engine)
    with engine.begin() as conn, pytest.raises(ValueError, match="timezone-aware"):
        store.append(
            conn,
            naive,
            surface=prov.surface.value,
            principal_id=prov.principal.id,
            session_id=prov.session_id,
        )


def test_read_returns_utc_aware_datetimes(
    engine: sa.Engine,
    frozen_config: str,
    make_provenance: Callable[..., SignalProvenance],
) -> None:
    store = SqlAlchemyCorrectionSignalStore(engine)
    _append(engine, store, make_provenance())
    rows = store.read_signals()
    assert rows[0].occurred_at.tzinfo is not None
    assert rows[0].recorded_at.tzinfo is not None


def test_store_mutation_surface_is_append_only(engine: sa.Engine) -> None:
    store = SqlAlchemyCorrectionSignalStore(engine)
    public_methods = {
        name for name in dir(store) if not name.startswith("_") and callable(getattr(store, name))
    }
    forbidden = {"update", "delete", "edit", "remove", "modify"}
    assert not (public_methods & forbidden)
    assert "append" in public_methods
