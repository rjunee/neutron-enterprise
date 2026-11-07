# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Concurrent-append serialization on a file-backed SQLite engine.

Test design informed by ABI versionstore test suite (MIT, Copyright (C) 2025
Naas.ai) — shapes only, no code copied.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import sqlalchemy as sa

from ne.audit import append_record, verify_chain
from ne.audit.store import ne_audit_log
from ne.schema import AuditEvent

THREADS = 4
PER_THREAD = 10


def test_concurrent_appends_yield_dense_chain(
    file_engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    barrier = threading.Barrier(THREADS)

    def worker(tid: int) -> None:
        barrier.wait()
        for i in range(PER_THREAD):
            with file_engine.begin() as conn:
                append_record(conn, audit(event_id=f"t{tid}-{i}"))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with file_engine.connect() as conn:
        seqs = (
            conn.execute(sa.select(ne_audit_log.c.seq).order_by(ne_audit_log.c.seq)).scalars().all()
        )

    total = THREADS * PER_THREAD
    assert seqs == list(range(1, total + 1))  # dense, no duplicates, no gaps
    assert verify_chain(file_engine).valid
