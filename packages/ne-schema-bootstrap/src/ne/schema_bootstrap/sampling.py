# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic head+reservoir row sampling (design §3.3).

``sample_stream`` caps a table's rows at ``cap`` while staying deterministic
across re-runs: the first half is a plain head (guarantees early rows always
appear), the second half is an Algorithm-R reservoir sample seeded ONLY from
the table name (never wall-clock, never a module-level RNG), so re-running
bootstrap over an unchanged sample directory reproduces byte-identical
selections. Reservoir survivors are re-emitted in ORIGINAL row order.

``sample_stream`` consumes its ``rows`` argument LAZILY and never holds more
than ``cap`` rows (plus O(1) bookkeeping) in memory, regardless of how many
rows the source actually produces — this is what lets
:mod:`ne.schema_bootstrap.readers` bound memory on an arbitrarily large
sample file (Argus P2: the previous design required the FULL row list
in memory before any cap could apply). ``sample_rows`` is the convenience
wrapper for callers that already hold a list.

Pure function: no module-level random state, no I/O.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable


def _seed_for(table_name: str) -> int:
    digest = hashlib.sha256(table_name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def sample_stream[T](table_name: str, rows: Iterable[T], *, cap: int) -> tuple[list[T], int]:
    """Streaming head+reservoir sample: returns ``(sampled_rows, row_count_seen)``.

    Consumes ``rows`` via a single pass over its iterator. The first ``cap``
    rows are buffered as they arrive; if the source is exhausted at or
    before that point, every row is returned unchanged (mirrors ``rows`` as
    a whole, in original order) — no head/reservoir split at all, matching
    the "at or under cap" behavior of the previous list-based algorithm.

    Once a ``cap + 1``-th row appears, the buffered rows split into a fixed
    ``head`` (the first ``cap // 2``) and an initial reservoir (the
    remaining ``cap - cap // 2`` buffered rows, by original position), then
    Algorithm R continues one row at a time as the rest of the iterator is
    consumed — each candidate stored as ``(original_tail_index, row)`` so
    the survivors can be re-sorted into original relative order at the end
    (a streaming reservoir has no random access back into a materialized
    ``tail_pool`` to re-derive order from, unlike the list-based version).

    This produces the IDENTICAL selection ``sample_rows(table_name,
    list(rows), cap=cap)`` would for the same effective input: the RNG is
    consulted exactly once per tail-pool position, in increasing-index
    order, with the same ``random.Random(seed)`` — the same call sequence
    either way.
    """
    head_size = cap // 2
    reservoir_size = cap - head_size

    it = iter(rows)
    buffer: list[T] = []
    seen = 0
    overflow_row: T | None = None
    has_overflow = False
    for row in it:
        seen += 1
        if seen <= cap:
            buffer.append(row)
        else:
            overflow_row = row
            has_overflow = True
            break

    if not has_overflow:
        return buffer, seen

    head = buffer[:head_size]
    # (tail_index, row) pairs, tail_index 0-based into the notional
    # `rows[head_size:]` — identity mapping for the initial reservoir_size
    # buffered rows, exactly matching the list-based algorithm's
    # `reservoir_indices = list(range(reservoir_size))` starting state.
    reservoir: list[tuple[int, T]] = [(i, buffer[head_size + i]) for i in range(reservoir_size)]
    rng = random.Random(_seed_for(table_name))

    def _consider(tail_index: int, row: T) -> None:
        j = rng.randint(0, tail_index)
        if j < reservoir_size:
            reservoir[j] = (tail_index, row)

    assert overflow_row is not None  # has_overflow implies this; narrows the type for below.
    _consider(reservoir_size, overflow_row)
    tail_index = reservoir_size + 1
    for row in it:
        seen += 1
        _consider(tail_index, row)
        tail_index += 1

    reservoir.sort(key=lambda item: item[0])
    return head + [row for _, row in reservoir], seen


def sample_rows[T](table_name: str, rows: list[T], *, cap: int) -> list[T]:
    """Return at most ``cap`` rows from ``rows``, deterministically.

    ``rows`` unchanged (a copy) when ``len(rows) <= cap``. Otherwise: head =
    ``rows[:cap // 2]``, plus an Algorithm-R reservoir of size
    ``cap - cap // 2`` drawn from the remainder, using
    ``random.Random(seed)`` where ``seed`` is derived solely from
    ``table_name`` via SHA-256. Reservoir rows are returned in their
    original relative order (indices tracked, then sorted) so output order
    is fully deterministic, not merely the *set* of selected rows.

    Defined in terms of :func:`sample_stream` (the single source of truth
    for the algorithm) for callers that already hold a full list.
    """
    sampled, _ = sample_stream(table_name, rows, cap=cap)
    return sampled
