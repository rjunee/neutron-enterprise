# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Acme Corp's 50 support tickets — each tied to a real customer account,
requested by that account's Sales rep, and worked by a real Support-dept
assignee.
"""

from __future__ import annotations

from datetime import timedelta

from ne.acme.crm import ACME_ACCOUNT_ID, account_rep_map
from ne.acme.models import OrgChart
from ne.acme.pools import BASE_TIME, PRODUCTS, TICKET_SUBJECT_TEMPLATES
from ne.acme.rand import DetRandom
from ne.schema import Account, Person, Ticket, TicketPriority, TicketStatus

N_TICKETS = 50

_STATUS_WEIGHTS: tuple[tuple[TicketStatus, int], ...] = (
    (TicketStatus.open, 25),
    (TicketStatus.pending, 20),
    (TicketStatus.resolved, 30),
    (TicketStatus.closed, 25),
)

_PRIORITY_WEIGHTS: tuple[tuple[TicketPriority, int], ...] = (
    (TicketPriority.low, 30),
    (TicketPriority.normal, 40),
    (TicketPriority.high, 20),
    (TicketPriority.urgent, 10),
)

_RESOLVED_STATUSES = (TicketStatus.resolved, TicketStatus.closed)


def build_tickets(
    rng: DetRandom, people: list[Person], org: OrgChart, accounts: list[Account]
) -> list[Ticket]:
    """Build exactly 50 tickets, deterministically."""
    del people  # person ids resolved via org.members / account_rep_map

    customer_accounts = [a for a in accounts if a.id != ACME_ACCOUNT_ID]
    account_rep = account_rep_map(org, accounts)
    support_ids = [m.person_id for m in org.members if m.department == "Support"]

    tickets: list[Ticket] = []
    for i in range(N_TICKETS):
        account = rng.choice(customer_accounts)
        product = rng.choice(PRODUCTS)
        subject = rng.choice(TICKET_SUBJECT_TEMPLATES).format(product=product)
        description = (
            f"Customer {account.name} reports an issue with their {product} order. "
            f"Please investigate and respond with next steps."
        )
        requester = account_rep[account.id]
        assignee = rng.choice(support_ids)
        status = rng.weighted_choice(_STATUS_WEIGHTS)
        priority = rng.weighted_choice(_PRIORITY_WEIGHTS)

        opened_days = rng.rand_int(1, 400)
        opened_hours = rng.rand_int(0, 23)
        opened_at = BASE_TIME - timedelta(days=opened_days, hours=opened_hours)

        resolved_at = None
        if status in _RESOLVED_STATUSES:
            # Clamp to the generation reference time (NE-CLEAN3 item 4): for
            # arbitrary seeds, an ``opened_at`` drawn near the end of the
            # 1-400 day window plus a large resolution-window draw can land
            # AFTER ``BASE_TIME`` — a resolved ticket must never resolve in
            # the future. RNG draw order is unchanged (the clamp is applied
            # to the computed value, not the draw itself); verified a byte-
            # identical no-op for DEFAULT_SEED (74) — the committed fixtures
            # are untouched (see test_fixture_freshness.py + tkt-date tests).
            resolved_at = min(opened_at + timedelta(hours=rng.rand_int(4, 240)), BASE_TIME)

        tickets.append(
            Ticket(
                id=f"tkt-{i:04d}",
                subject=subject,
                description=description,
                status=status,
                priority=priority,
                belongs_to=account.id,
                requester=requester,
                assignee=assignee,
                opened_at=opened_at,
                resolved_at=resolved_at,
                source_system="acme-desk",
                source_record_id=f"TKT-{i:04d}",
                created_at=opened_at,
                updated_at=resolved_at if resolved_at is not None else opened_at,
            )
        )

    return tickets
