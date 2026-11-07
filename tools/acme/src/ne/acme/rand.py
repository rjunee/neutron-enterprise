# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""A deterministic, cross-version-stable random helper for the Acme generator.

CPython's ``random`` module documents cross-version stability ONLY for
``Random.random()`` given a fixed seed — the higher-level convenience methods
(``randint``, ``choice``, ``shuffle``, ``sample``, ...) are explicitly NOT
guaranteed to produce the same sequence across Python versions because their
internal algorithms have changed historically (e.g. ``shuffle``'s algorithm,
``choice``'s index computation). Since NE-074's core invariant is BYTE-IDENTICAL
output for a given seed, :class:`DetRandom` is built entirely on top of
``Random.random()`` and reimplements the higher-level operations itself so the
exact sequence of ``.random()`` calls (and therefore the output bytes) never
depends on which primitive CPython happens to use internally for ``randint``
et al. in a given release.

Never call ``self._r.randint`` / ``.choice`` / ``.shuffle`` / ``.sample``
directly anywhere in this package — only ``.random()`` is used here.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


class DetRandom:
    """Deterministic RNG wrapper built only on ``random.Random.random()``."""

    def __init__(self, seed: int) -> None:
        self._r = random.Random(seed)

    def random(self) -> float:
        """The one version-stable primitive; everything else builds on this."""
        return self._r.random()

    def rand_int(self, lo: int, hi: int) -> int:
        """Return an integer in ``[lo, hi]`` inclusive, clamped to ``hi``."""
        if hi < lo:
            raise ValueError(f"hi ({hi}) must be >= lo ({lo})")
        span = hi - lo + 1
        value = lo + int(self._r.random() * span)
        return min(value, hi)

    def choice(self, seq: Sequence[T]) -> T:
        """Return a single element of ``seq`` chosen uniformly at random."""
        if len(seq) == 0:
            raise ValueError("choice() called on an empty sequence")
        return seq[self.rand_int(0, len(seq) - 1)]

    def sample(self, seq: Sequence[T], k: int) -> list[T]:
        """Return ``k`` distinct elements of ``seq`` (order = selection order).

        Implemented as an explicit partial Fisher-Yates shuffle over a working
        copy so the exact sequence of ``rand_int`` (hence ``.random()``) calls
        is fully determined by this module, not by ``random.Random.sample``.
        """
        if k < 0 or k > len(seq):
            raise ValueError(f"sample size {k} out of range for sequence of length {len(seq)}")
        pool = list(seq)
        n = len(pool)
        for i in range(n - 1, n - 1 - k, -1):
            j = self.rand_int(0, i)
            pool[i], pool[j] = pool[j], pool[i]
        return pool[n - k :][::-1]

    def shuffle(self, items: list[T]) -> None:
        """Shuffle ``items`` in place via an explicit Fisher-Yates loop."""
        n = len(items)
        for i in range(n - 1, 0, -1):
            j = self.rand_int(0, i)
            items[i], items[j] = items[j], items[i]

    def weighted_choice(self, items: Sequence[tuple[T, int]]) -> T:
        """Return one value from ``items`` (value, weight) pairs, by cumulative weight."""
        if not items:
            raise ValueError("weighted_choice() called on an empty sequence")
        total = sum(weight for _, weight in items)
        if total <= 0:
            raise ValueError("weighted_choice() requires a positive total weight")
        pick = self.rand_int(0, total - 1)
        cumulative = 0
        for value, weight in items:
            cumulative += weight
            if pick < cumulative:
                return value
        # Unreachable given the invariant above, but keeps the type-checker happy
        # and fails loudly rather than silently returning the wrong element.
        raise AssertionError("weighted_choice() cumulative weight mismatch")
