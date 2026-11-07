# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CorrectionSignalCapture — the emit service surfaces call (NE-064).

:class:`CorrectionSignalCapture` structurally satisfies the FROZEN
``ne.contracts.evalobs.SignalEmitter`` Protocol (``async def emit(self, signal:
CorrectionSignalLike) -> None``). It validates provenance (non-empty
principal/surface/session_id), an in-range ``signal_kind``, and the
``correction_signal`` event type BEFORE any write (:class:`SignalRejectedError`
on failure, fail closed — nothing is persisted). It then appends the store row
AND the ne-events outbox row in ONE transaction on ONE connection: the
dual-write is atomic by construction, so a correction signal is never durable
in one place but not the other.

Audit stance: this outbox emission of the ``CorrectionSignal`` event itself IS
"the audit/event where the pattern fits" for this unit — there is deliberately
NO separate ne-audit hash-chain write here. Correction signals are customer
FEEDBACK data, not privileged actions; an approval-rejection signal in
particular is already audited at source by NE-035's HITL gate (the rejection
itself produces an ``ApprovalEvent``/audit record there) — this capture merely
also records it as flywheel input. Recording the same fact twice through two
different audit-grade channels would not add governance value.

Async methods make DIRECT sync SQLAlchemy calls on short-lived transactions —
the established ``memory_store``/outbox pattern; there is no actual async I/O
here (no network calls), so this is deliberately not a thread-pool wrapper.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa

from ne.audit import ensure_transactional_sqlite
from ne.contracts import CorrectionSignalLike
from ne.events import append as outbox_append
from ne.flywheel.errors import SignalRejectedError
from ne.flywheel.hygiene import scrub_text
from ne.flywheel.signals import (
    SignalPayload,
    SignalProvenance,
    SignalSurface,
    build_correction_signal,
)
from ne.flywheel.store import SqlAlchemyCorrectionSignalStore
from ne.schema import CorrectionKind

_VALID_KINDS = {k.value for k in CorrectionKind}
_VALID_SURFACES = {s.value for s in SignalSurface}


def _validate(signal: CorrectionSignalLike) -> tuple[str, str, str, str | None, str | None]:
    """Validate ``signal`` for emit; return (surface, principal, session_id, turn_id, message_id).

    Raises :class:`SignalRejectedError` (fail closed, nothing persisted) when
    ``event_type`` is not ``"correction_signal"``, ``signal_kind`` is not a
    valid :class:`~ne.schema.CorrectionKind`, ``actor`` is empty, or the
    provenance map is missing a non-empty ``surface``/``principal``/
    ``session_id`` (or ``surface`` is not one of console/slack/api).
    """
    duck: Any = signal  # structural/duck-typed access — a caller may hand any
    # CorrectionSignalLike-shaped object, not necessarily a validated pydantic
    # CorrectionSignal (e.g. one built via model_construct, bypassing pydantic
    # enum validation) — this belt-and-braces check must tolerate that.
    if signal.event_type != "correction_signal":
        raise SignalRejectedError(
            f"expected event_type 'correction_signal', got {signal.event_type!r}"
        )
    signal_kind: Any = getattr(duck, "signal_kind", None)
    kind_value: Any = signal_kind.value if hasattr(signal_kind, "value") else signal_kind
    if kind_value not in _VALID_KINDS:
        raise SignalRejectedError(f"unknown signal_kind: {kind_value!r}")
    if not signal.actor or not signal.actor.strip():
        raise SignalRejectedError("signal.actor must be a non-empty string (unauthenticated)")

    provenance = dict(signal.provenance)
    surface = str(provenance.get("surface") or "").strip()
    principal = str(provenance.get("principal") or "").strip()
    session_id = str(provenance.get("session_id") or "").strip()
    if not principal:
        raise SignalRejectedError("provenance missing non-empty 'principal' (unauthenticated)")
    if not surface:
        raise SignalRejectedError("provenance missing non-empty 'surface'")
    if surface not in _VALID_SURFACES:
        raise SignalRejectedError(f"provenance 'surface' {surface!r} is not a valid SignalSurface")
    if not session_id:
        raise SignalRejectedError("provenance missing non-empty 'session_id'")

    turn_id_raw = provenance.get("turn_id")
    message_id_raw = provenance.get("message_id")
    turn_id = str(turn_id_raw) if turn_id_raw else None
    message_id = str(message_id_raw) if message_id_raw else None
    return surface, principal, session_id, turn_id, message_id


class CorrectionSignalCapture:
    """Emit + capture correction signals; structurally satisfies ``SignalEmitter``.

    Both ``ensure_flywheel_schema`` AND ``ensure_outbox_schema`` MUST have been
    run on ``engine`` by the caller/factory before use (see
    :func:`ne.flywheel.factory.capture_from_settings`).
    """

    def __init__(
        self,
        engine: sa.Engine,
        *,
        store: SqlAlchemyCorrectionSignalStore | None = None,
        max_text_chars: int = 4000,
    ) -> None:
        ensure_transactional_sqlite(engine)
        self._engine = engine
        self._store = store if store is not None else SqlAlchemyCorrectionSignalStore(engine)
        # C12/privacy cap for stored free-text (comment/diff/reason). Threaded from
        # FlywheelSettings.max_text_chars so lowering NE_FLYWHEEL_MAX_TEXT_CHARS
        # actually reduces how much customer content is persisted (Codex P2).
        self._max_text_chars = max_text_chars

    @property
    def store(self) -> SqlAlchemyCorrectionSignalStore:
        return self._store

    async def emit(self, signal: CorrectionSignalLike) -> None:
        """Low-level emit: validate ``signal``, then dual-write store + outbox atomically.

        ``signal`` MUST already be a fully-built ``CorrectionSignal`` (e.g. from
        :func:`~ne.flywheel.signals.build_correction_signal`). Raises
        :class:`SignalRejectedError` before any write on a validation failure
        (unknown kind, wrong event_type, missing provenance/actor). The
        ``comment`` field is scrubbed defensively again here (belt-and-braces
        for callers that bypass ``build_correction_signal``) before either
        write, so a caller cannot smuggle unredacted secrets past this seam.
        """
        surface, principal, session_id, turn_id, message_id = _validate(signal)

        comment = getattr(signal, "comment", None)
        if comment is not None:
            comment = scrub_text(comment, max_chars=self._max_text_chars)
            signal = signal.model_copy(update={"comment": comment})  # type: ignore[union-attr]

        with self._engine.begin() as conn:
            self._store.append(
                conn,
                signal,  # type: ignore[arg-type]
                surface=surface,
                principal_id=principal,
                session_id=session_id,
                turn_id=turn_id,
                message_id=message_id,
            )
            outbox_append(conn, signal)

    async def capture(
        self,
        provenance: SignalProvenance,
        payload: SignalPayload,
        *,
        kpi_tag: str | None = None,
        trace_id: str | None = None,
        event_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> str:
        """Typed convenience API: build the signal, then route through :meth:`emit`.

        Returns the built ``event_id``. This is what console/Slack/API surfaces
        actually call (the follow-on UI wiring is out of scope for this unit;
        see the package README for the emit contract).
        """
        signal = build_correction_signal(
            provenance,
            payload,
            kpi_tag=kpi_tag,
            trace_id=trace_id,
            event_id=event_id,
            occurred_at=occurred_at,
            max_text_chars=self._max_text_chars,
        )
        await self.emit(signal)
        return signal.event_id
