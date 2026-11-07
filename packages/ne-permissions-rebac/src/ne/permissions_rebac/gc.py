# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Reconciliation GC sweep for orphaned document tuples (NE-087 §3.7).

Orphaned tuples (a share whose document was later deleted) are HYGIENE, not a
security hole — a deleted document has no chunks to match, so its viewer tuples
grant access to nothing. But they distort share listings and ``ListObjects``
size, so the sweep drops them. Same audit-then-mutate discipline as
``ShareService``: one ``rebac.gc.removed`` event is appended BEFORE each delete;
the sink is required, never optional.

Live wiring (``ne rebac gc`` CLI, a real ``DocumentExists`` over the brain delete
path) is O-4 — ``tools/ne-cli`` is out of this unit's scope.
"""

from __future__ import annotations

from ne.contracts.governance import AuditSink
from ne.events import new_audit_event
from ne.permissions_rebac.errors import RebacAuditError
from ne.permissions_rebac.ports import DocumentExists, OpenFgaTupleClient, Tuple3
from ne.schema import AuditOutcome

__all__ = ["run_gc_sweep"]

_DOCUMENT_PREFIX = "document:"
_GC_ACTOR = "system:rebac-gc"


async def run_gc_sweep(client: OpenFgaTupleClient, exists: DocumentExists, sink: AuditSink) -> int:
    """Delete tuples of documents that no longer exist; return the removed count.

    For every distinct ``document:<id>`` object with tuples, probe
    ``exists.exists(id)``; for each missing document, append one
    ``rebac.gc.removed`` audit event per tuple BEFORE deleting it. A sink failure
    raises :class:`RebacAuditError` with the remaining tuples intact.
    """
    all_tuples = await client.read_tuples()
    doc_tuples: list[Tuple3] = [t for t in all_tuples if t[2].startswith(_DOCUMENT_PREFIX)]

    # Distinct document ids, resolved once each.
    doc_ids = sorted({t[2][len(_DOCUMENT_PREFIX) :] for t in doc_tuples})
    missing: set[str] = set()
    for doc_id in doc_ids:
        if not await exists.exists(doc_id):
            missing.add(doc_id)

    removed = 0
    for tup in doc_tuples:
        doc_id = tup[2][len(_DOCUMENT_PREFIX) :]
        if doc_id not in missing:
            continue
        try:
            await sink.append(
                new_audit_event(
                    action="rebac.gc.removed",
                    resource=tup[2],
                    outcome=AuditOutcome.success,
                    actor=_GC_ACTOR,
                    provenance={"user": tup[0], "relation": tup[1]},
                )
            )
        except Exception as exc:
            raise RebacAuditError("gc audit append failed; sweep aborted") from exc
        await client.delete_tuples([tup])
        removed += 1
    return removed
