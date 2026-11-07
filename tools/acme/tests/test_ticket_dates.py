# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Ticket ``resolved_at``/``updated_at`` never lands in the future (NE-CLEAN3
item 4 / NE-074 P2).

``build_tickets`` clamps ``resolved_at`` to ``BASE_TIME`` (the generation
reference time) so an arbitrary seed's RNG draw can never resolve a ticket in
the future. Proven non-vacuous: seed 0 is planning-verified to draw a
pre-clamp future ``resolved_at`` for ``tkt-0009`` — clamped to exactly
``BASE_TIME`` post-fix. The default seed (74) is unaffected — a byte-level
no-op, independently locked by ``test_fixture_freshness.py``.
"""

from __future__ import annotations

from ne.acme.generator import generate
from ne.acme.pools import BASE_TIME, DEFAULT_SEED
from ne.schema import TicketStatus

_UNRESOLVED_STATUSES = (TicketStatus.open, TicketStatus.pending)


def test_arbitrary_seeds_never_future_date_resolved_at() -> None:
    saw_clamped = False
    for seed in (0, 4, 5):
        corpus = generate(seed)
        tickets = corpus.record_set.tickets or []
        assert tickets  # sanity: the corpus always has 50 tickets
        for ticket in tickets:
            is_unresolved = ticket.status in _UNRESOLVED_STATUSES
            assert (ticket.resolved_at is None) == is_unresolved
            if ticket.resolved_at is not None:
                assert ticket.resolved_at <= BASE_TIME
                if ticket.resolved_at == BASE_TIME:
                    saw_clamped = True
            assert ticket.updated_at is not None
            assert ticket.updated_at <= BASE_TIME
        if seed == 0:
            # Planning-verified: pre-fix, seed 0 draws exactly one future
            # resolved_at (tkt-0009) — guard against a vacuous test by
            # asserting the clamp actually engaged for this seed.
            assert any(t.resolved_at == BASE_TIME for t in tickets)
    assert saw_clamped


def test_default_seed_is_untouched_by_clamp() -> None:
    """The clamp is a documented no-op for the shipped seed-74 fixtures: every
    seed-74 resolved_at is strictly BEFORE BASE_TIME (never needed clamping).
    Byte-identity of the committed fixtures themselves is independently locked
    by ``test_fixture_freshness.py``; this test documents WHY that lock holds
    across this change."""
    corpus = generate(DEFAULT_SEED)
    tickets = corpus.record_set.tickets or []
    resolved = [t.resolved_at for t in tickets if t.resolved_at is not None]
    assert resolved  # sanity: some tickets are resolved/closed
    assert all(r < BASE_TIME for r in resolved)
