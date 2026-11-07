# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The run-scoped correlation id seam (NE-166, design doc §10.1).

THE ONE ``trace_id`` ContextVar for the whole platform. It lives HERE — next to
:func:`ne.events.factory.new_audit_event`, the factory that stamps it onto every
event — and NOT in ``ne.runtime``, because ``ne.runtime`` sits ABOVE the event
writers that need to read it: ``ne-runtime`` depends on ``ne-govern``, so
``ne.govern``'s ``pdp.decide`` writer could not import ``ne.runtime.correlation``
without a circular package dependency (NE-DEMO-PDP-TRACE). ``ne-events`` is a
dependency of BOTH, and is stdlib-only for this module, so the carrier is
reachable from every audit-event writer at or below the runtime layer.

:mod:`ne.runtime.correlation` re-exports these two names UNCHANGED and remains
the documented import site for runtime-layer callers — there is exactly ONE
:class:`~contextvars.ContextVar` object, so a value set through either import
path is visible through the other.

A single explicit ``trace_id`` is minted at an agent RUN ROOT (a session run, an
isolated/cron run, or a sub-agent dispatch) and threaded through as ordinary
data — onto ``CompletionRequest.trace_id`` and onto every audit event — so all
events produced during one run share ONE non-null id EVEN UNDER THE DEFAULT
NO-OP OTEL TRACER (which would otherwise yield ``None`` for every event,
defeating correlation).

The id is an OPAQUE 32-hex string (``uuid.uuid4().hex`` — the same SHAPE as an
OTel trace id, but never derived from a span). It carries no PII: it is a bare
random identifier, never task text, arguments, or any user-supplied value.

A nested run (e.g. a sub-agent dispatched from inside a parent run) INHERITS the
ambient id unchanged — the correlation spine follows the call tree, not process
boundaries.

ABSENCE IS HONEST. ``current_run_trace_id()`` returns ``None`` outside any run
scope, and a writer that reads ``None`` MUST stamp ``None``: a decision taken
outside any run (a bare API call, a preflight, a health probe) genuinely belongs
to no trace. No writer may mint, default, or otherwise invent an id to fill the
gap — a fabricated id would put a FALSE association into the hash-chained audit
record, which is strictly worse than an absent one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = [
    "current_run_trace_id",
    "run_trace_scope",
]

_run_trace_id: ContextVar[str | None] = ContextVar("ne_run_trace_id", default=None)


def current_run_trace_id() -> str | None:
    """The ambient run-scoped trace id, or ``None`` outside any run scope.

    ``None`` is a HONEST value, not a hole to be filled: see the module
    docstring's ABSENCE IS HONEST paragraph.
    """
    return _run_trace_id.get()


@contextmanager
def run_trace_scope(trace_id: str | None = None) -> Iterator[str]:
    """Establish (or inherit) the run-scoped correlation id for the block.

    If ``trace_id`` is ``None`` and an ambient id already exists (a nested run
    inside a parent's scope), the ambient id is yielded UNCHANGED — a sub-agent
    dispatched inside a parent run inherits the parent's id (the NE-166
    invariant). Otherwise a fresh id is minted (``uuid.uuid4().hex``) unless
    ``trace_id`` was explicitly supplied, and it is set as the new ambient id
    for the duration of the block, reset back to the prior value on exit.
    """
    ambient = _run_trace_id.get()
    if trace_id is None and ambient is not None:
        yield ambient
        return
    value = trace_id if trace_id is not None else uuid.uuid4().hex
    token = _run_trace_id.set(value)
    try:
        yield value
    finally:
        _run_trace_id.reset(token)
