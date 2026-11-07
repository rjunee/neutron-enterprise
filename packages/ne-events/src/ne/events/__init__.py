# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-events — the typed platform event spine transport (Postgres outbox).

Per ADR-001 §4.1 typed events travel via a Postgres **outbox table** consumed by
an exporter seam in ne-worker — there is NO message broker in v1. This package
owns that outbox: a SQLAlchemy-Core table, a same-transaction :func:`append`, and
a cursor-based :func:`read_batch`/:func:`ack`/:func:`drain_once` drain seam
implementing ABI's hard-won event-cursor edge-case discipline
(cursor-advance-only-after-durable-processing, immutable per-consumer filters,
fail-open drain on handler error → at-least-once redelivery).

The outbox lives here — not in the FROZEN, zero-heavy-deps ``ne-contracts`` (which
excludes sqlalchemy by design) and not in ``ne-obs`` (which would couple every
event producer to the OTel stack). See ADR-0002.

The concrete event *schemas* (``AuditEvent`` etc.) come from ``ne-schema``; the
:mod:`ne.events.factory` helpers construct them with config_hash/occurred_at/
provenance stamped.
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.events.correlation import (
    current_run_trace_id,
    run_trace_scope,
)
from ne.events.factory import (
    new_approval_event,
    new_audit_event,
    new_correction_signal,
    new_cost_event,
    new_shape_violation,
)
from ne.events.outbox import (
    ConsumerFilterMismatchError,
    CursorRegressionError,
    DrainResult,
    OutboxError,
    OutboxRecord,
    ack,
    append,
    drain_once,
    ensure_outbox_schema,
    prune_acked,
    read_batch,
)

__all__ = [
    "ConsumerFilterMismatchError",
    "CursorRegressionError",
    "DrainResult",
    "OutboxError",
    "OutboxRecord",
    "__version__",
    "ack",
    "append",
    "current_run_trace_id",
    "drain_once",
    "ensure_outbox_schema",
    "new_approval_event",
    "new_audit_event",
    "new_correction_signal",
    "new_cost_event",
    "new_shape_violation",
    "prune_acked",
    "read_batch",
    "run_trace_scope",
]
