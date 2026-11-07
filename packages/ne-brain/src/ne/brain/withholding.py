# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""One read's source-health withholding, carried out to the caller that renders it.

WHY THIS MODULE EXISTS. ``HybridRetriever._filter_source_health`` already knows
the one fact a barren read needs in order to be explicable: how many candidates
it dropped because their connector's latest sync fact is ``degraded``. It
AUDITED that number (``knowledge.withhold``, counts only) and then THREW IT
AWAY — ``RetrievalResult`` carries chunks and citations, nothing about what was
withheld on the way. So ``read.knowledge.search``'s handler could not tell these
two apart:

* nothing in the corpus matched the query, and
* every match was withheld because its governed source is currently degraded.

Both arrived as ``result.chunks == ()`` and both rendered as
``[showing 0 of 0 retrieved chunks]``. That is a true count and a false
impression: the second case retrieved plenty and disclosed none of it. A model
reading "0 of 0" concludes its QUERY was wrong and broadens — three times, in
the Run this module was written for — while the no-progress rule correctly stops
it, and the Run's failure reason says "Nothing was retrieved" when the truth was
"everything retrieved was withheld".

WHAT THIS IS NOT. It is not a widening: nothing here changes which chunks are
served, and a degraded source is still withheld in full. It is not a second
audit path: ``_emit_source_health_audit`` remains the record of what happened.
It carries a COUNT and nothing else — never a connector name, a chunk id, a
label, or any content — so a caller can say a true sentence about a zero.

THE CARRIER. :data:`SOURCE_HEALTH_WITHHOLDING` is an ambient carrier shaped like
``ne.api.knowledge_scope.RUN_KNOWLEDGE_SCOPE`` and
``ne.runtime``'s provisional recorder: the CALLER opens a scope around the read
it is about to render, the retriever records into whatever scope is ambient, and
``None`` (no scope open) is a byte-identical no-op. Recording is additive across
the reads inside one scope, so a composed pipeline (rerank over hybrid, graph
over rerank) that filters more than once still reports one total.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class SourceHealthWithholding:
    """How much one rendered read withheld for connector health, and out of how many.

    ``candidates`` is every chunk the health filter INSPECTED; ``withheld`` is how
    many of those it dropped. ``withheld == candidates`` with a non-zero
    ``candidates`` is the total-blackout case: the read matched content and
    disclosed none of it.
    """

    candidates: int = 0
    withheld: int = 0
    #: How many SERVED chunks came from a source whose latest sync attempt
    #: failed but which is still readable (the ``stale`` leg of
    #: ``ne.brain.source_health.SourceHealthSnapshot``). Serving them is
    #: correct — the corpus is the last verified sync's admission — but silent
    #: serving would hide a true freshness fact, so the count rides along for
    #: the renderer to state as a caveat. A COUNT and nothing else, exactly
    #: like ``withheld``.
    stale_served: int = 0

    @property
    def total_blackout(self) -> bool:
        """Every candidate this read inspected was withheld (and there was one)."""
        return self.withheld > 0 and self.withheld == self.candidates

    def record(self, *, candidates: int, withheld: int) -> None:
        """Accumulate one health-filter pass (a pipeline may filter more than once)."""
        if candidates < 0 or withheld < 0 or withheld > candidates:
            raise ValueError(
                f"impossible withholding counts: candidates={candidates} withheld={withheld}"
            )
        self.candidates += candidates
        self.withheld += withheld

    def record_stale_served(self, count: int) -> None:
        """Accumulate served-from-a-stale-source chunks for this render."""
        if count < 0:
            raise ValueError(f"impossible stale-served count: {count}")
        self.stale_served += count


@dataclass(frozen=True, slots=True)
class _NullObservation:
    """The no-scope sink. Exists so the retriever never branches on ``None``."""

    def record(self, *, candidates: int, withheld: int) -> None:
        return None

    def record_stale_served(self, count: int) -> None:
        return None


#: The observation the current render is accumulating into. ``None`` ⇒ nobody asked.
SOURCE_HEALTH_WITHHOLDING: ContextVar[SourceHealthWithholding | None] = ContextVar(
    "ne_brain_source_health_withholding", default=None
)

_NULL = _NullObservation()


@contextmanager
def observe_source_health_withholding() -> Iterator[SourceHealthWithholding]:
    """Open a scope collecting the health withholding of the reads inside it.

    The yielded object is filled DURING the enclosed reads and is safe to inspect
    after the block. Scopes nest: an inner scope shadows an outer one for its
    duration rather than double-counting into both, which keeps one rendered
    read's numbers exactly that read's.
    """
    observation = SourceHealthWithholding()
    token = SOURCE_HEALTH_WITHHOLDING.set(observation)
    try:
        yield observation
    finally:
        SOURCE_HEALTH_WITHHOLDING.reset(token)


def begin_source_health_observation() -> SourceHealthWithholding:
    """Start a FRESH observation for the read this caller is about to make.

    The flat begin/read pair exists alongside
    :func:`observe_source_health_withholding` for one reason: a handler that must
    not re-indent the ``await`` it is measuring (an unwrapped call site merges
    cleanly across the stacked demo trains, a re-indented one does not). It is
    exactly as safe as the context manager in the setting it is used in — asyncio
    copies the ``Context`` per Task, so one Task's observation is invisible to
    every other, and each call REPLACES the previous observation rather than
    accumulating into it.
    """
    observation = SourceHealthWithholding()
    SOURCE_HEALTH_WITHHOLDING.set(observation)
    return observation


def record_source_health_withholding(*, candidates: int, withheld: int) -> None:
    """Record one health-filter pass into the ambient scope; a no-op without one."""
    observation = SOURCE_HEALTH_WITHHOLDING.get()
    (observation if observation is not None else _NULL).record(
        candidates=candidates, withheld=withheld
    )


def record_source_health_stale_served(count: int) -> None:
    """Record served-stale chunks into the ambient scope; a no-op without one."""
    observation = SOURCE_HEALTH_WITHHOLDING.get()
    (observation if observation is not None else _NULL).record_stale_served(count)


__all__ = [
    "SOURCE_HEALTH_WITHHOLDING",
    "SourceHealthWithholding",
    "begin_source_health_observation",
    "observe_source_health_withholding",
    "record_source_health_stale_served",
    "record_source_health_withholding",
]
