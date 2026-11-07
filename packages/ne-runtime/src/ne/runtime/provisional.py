# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Turn-scoped provisional-context accumulator (design §6.1/§6.3, NE-T2X-5, A-P1-5).

Modeled on :mod:`~ne.runtime.correlation`'s ``run_trace_scope``
(inherit-if-ambient ContextVar) and :mod:`~ne.runtime.grounding`'s per-run
recorder (a fresh accumulator every turn, published for the duration of the
call so a deeply-nested tool handler can record into it): this module answers
ONE question for the PDP-input-threading seam in :mod:`~ne.runtime.middleware`
— "did THIS turn's retrieval touch any unconfirmed, LLM-extracted graph fact,
and if so, how many / which refs / under what extraction mode?"

Derive, don't transliterate (this repo's recurring rule): the recorded
``provisional_source_count`` is SUPPLIED BY THE CALLER — the same count the
retrieval/wire layer already derives (ne-api's ``wire.py::_provisional_source_count``,
landed in NE-T2X-4, pins the ONE authoritative computation: counting hops/refs
whose ``assurance`` is not in ``_VOUCHED_ASSURANCE``). This module NEVER
re-derives a rival count from graph objects — it only carries the caller's
number, sums repeated recordings (a turn may fetch more than once), and
reduces to a boolean for the PDP input. A hand-rolled re-computation that
could disagree with the real one is exactly the defect class this discipline
exists to prevent (NE-BEW-GROUPS-ZEROGROUPS: a fire predicate re-implemented
``map_groups_to_roles`` and disagreed with it on real inputs).

WIRED (NE-T2X-5-RECORDER-WIRE): the production seam that calls
``current_provisional_recorder()`` from inside a verified-read tool handler is
``ne.api.tools.build_chat_bindings``' ``read.knowledge.search`` handler (the
``_search`` closure) — mirroring how ``current_answer_recorder()`` is bound as
data into the grounding recorder's tool handlers. It is called INSIDE the
handler, at request time (never at ``build_chat_bindings`` build time), so a
turn's retrieval of assurance-labeled graph elements records into THIS turn's
accumulator; the count/refs it records are derived from the ONE pinned
``ne.api.wire.provisional_summary`` traversal — never a second, hand-rolled
count. Pinned end to end (retrieval -> recorder -> accumulator -> middleware
threading -> PDP escalation -> approval queue) by
``packages/ne-api/tests/test_provisional_recorder_wiring.py`` — see that
file's module docstring for the value-discriminating extracted-vs-backbone
proof through the real app. Everything downstream of the recorder (the
accumulator semantics, the middleware threading, the PDP escalation, the
approval-queue read-model) is ALSO exercised by this package's own test
suites against a synthetic recorder.

Fail-toward-flagging throughout: counts SUM rather than max/replace (over-
counting across retrievals can only make a turn MORE likely to be flagged,
never less); the mode-conflict rule prefers any non-"autonomous" mode over
"autonomous" (a turn that touched provisional context under more than one
mode is treated as NOT YOLO-exempt, since YOLO's declared meaning is "no
escalation" and that must never be granted by an accident of ordering); and
``current_provisional_context()`` returns ``None`` outside any scope, which
:mod:`~ne.runtime.middleware` treats as "no provisional context to thread" —
byte-identical to today's behavior (design §2.5 default-identity discipline).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

__all__ = [
    "ProvisionalContext",
    "ProvisionalContextAccumulator",
    "current_provisional_context",
    "current_provisional_recorder",
    "provisional_context_scope",
]


@dataclass(frozen=True)
class ProvisionalContext:
    """An immutable snapshot of one turn's accumulated provisional-context state.

    ``provisional`` is the single boolean the PDP input actually needs
    (design §6.3): count > 0 OR at least one ref was recorded (a caller might
    record refs without a count in a future extension; either signal alone is
    sufficient to flag the turn — fail toward escalation).
    """

    provisional_source_count: int
    provisional_source_refs: tuple[str, ...]
    extraction_mode: str | None

    @property
    def provisional(self) -> bool:
        return self.provisional_source_count > 0 or len(self.provisional_source_refs) > 0


class ProvisionalContextAccumulator:
    """Per-turn accumulator: SUMS counts, ORDERED-DEDUPS refs, resolves mode conflicts.

    MUST be constructed FRESH per turn (see the module docstring's "fresh
    per turn" discipline) — a shared/reused instance would let a prior turn's
    provisional-ness taint this turn, exactly the bug
    :class:`~ne.runtime.grounding.GroundingRecorder` structurally forbids for
    fetched answers.
    """

    def __init__(self) -> None:
        self._count = 0
        self._refs: list[str] = []
        self._seen_refs: set[str] = set()
        self._mode: str | None = None

    def record(
        self,
        *,
        provisional_source_count: int,
        provisional_source_refs: Sequence[str] = (),
        extraction_mode: str,
    ) -> None:
        """Record one retrieval's provisional contribution to this turn.

        ``provisional_source_count`` is summed across every call in the turn
        (fail toward flagging — see module docstring). ``provisional_source_refs``
        are appended in ORDER, deduplicated (a ref recorded twice contributes
        once to the ordered ref list). ``extraction_mode`` is REQUIRED
        (the caller — the retrieval binding — always knows the deployment's
        configured mode); the mode-conflict rule lives in :meth:`snapshot`.
        """
        if provisional_source_count < 0:
            raise ValueError(
                f"provisional_source_count must be >= 0, got {provisional_source_count}"
            )
        self._count += provisional_source_count
        for ref in provisional_source_refs:
            if ref not in self._seen_refs:
                self._seen_refs.add(ref)
                self._refs.append(ref)
        if self._mode is None:
            self._mode = extraction_mode
        elif self._mode == "autonomous" and extraction_mode != "autonomous":
            # Mode-conflict rule: the first NON-"autonomous" mode recorded
            # wins over an earlier "autonomous" recording — a turn that
            # touched provisional context under more than one mode is treated
            # as NOT YOLO-exempt (fail toward escalation: YOLO's
            # no-escalation meaning must never apply to a turn that ALSO
            # touched a stricter-mode retrieval). Once a non-"autonomous"
            # mode has won, it is never displaced back to "autonomous".
            self._mode = extraction_mode

    def snapshot(self) -> ProvisionalContext:
        """An immutable snapshot of the accumulator's current state."""
        return ProvisionalContext(
            provisional_source_count=self._count,
            provisional_source_refs=tuple(self._refs),
            extraction_mode=self._mode,
        )


#: The ACTIVE per-turn accumulator, published for the duration of a
#: :func:`provisional_context_scope` block (inherit-if-ambient — a sub-agent
#: dispatched inside a provisional turn shares the parent's accumulator, so
#: poisoned context follows the call tree exactly like
#: :func:`~ne.runtime.correlation.run_trace_scope`'s trace id).
_ACTIVE_ACCUMULATOR: ContextVar[ProvisionalContextAccumulator | None] = ContextVar(
    "ne_runtime_active_provisional_accumulator", default=None
)


def current_provisional_recorder() -> ProvisionalContextAccumulator | None:
    """The active accumulator, or ``None`` outside any :func:`provisional_context_scope`.

    This is the seam a verified-read tool handler binds AS DATA (the
    ``current_answer_recorder`` precedent) — WIRED (NE-T2X-5-RECORDER-WIRE, see
    the module docstring): ``ne.api.tools.build_chat_bindings``' retrieval
    handler is the caller that invokes ``.record(...)`` on the returned
    accumulator, called fresh on every request.
    """
    return _ACTIVE_ACCUMULATOR.get()


def current_provisional_context() -> ProvisionalContext | None:
    """A snapshot of the active accumulator, or ``None`` outside any scope.

    ``None`` here means "no provisional-context scope is active" — the
    :mod:`~ne.runtime.middleware` threading seam treats this as "thread
    nothing new", so a PDP input built with no active scope is byte-identical
    to today (design §2.5).
    """
    accumulator = _ACTIVE_ACCUMULATOR.get()
    return accumulator.snapshot() if accumulator is not None else None


@contextmanager
def provisional_context_scope() -> Iterator[ProvisionalContextAccumulator]:
    """Establish (or inherit) the turn-scoped provisional accumulator for the block.

    INHERIT-IF-AMBIENT, mirroring :func:`~ne.runtime.correlation.run_trace_scope`:
    if an accumulator is ALREADY active (a sub-agent dispatched inside a
    parent's provisional turn), the ambient accumulator is yielded UNCHANGED —
    poisoned context follows the call tree. Otherwise a FRESH accumulator is
    minted for the duration of the block and reset back to the prior value
    (``None`` at the top level) on exit.
    """
    ambient = _ACTIVE_ACCUMULATOR.get()
    if ambient is not None:
        yield ambient
        return
    accumulator = ProvisionalContextAccumulator()
    token = _ACTIVE_ACCUMULATOR.set(accumulator)
    try:
        yield accumulator
    finally:
        _ACTIVE_ACCUMULATOR.reset(token)
