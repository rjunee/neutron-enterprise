# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Acme Corp CRM data: 120 accounts (incl. Acme itself) + 80 orders = 200 records.

Each customer account is assigned a deterministic owning Sales-department
person (round-robin over Sales dept ids, in account order) so that orders and
(later) documents/tickets tied to that account are coherently authored/owned
by a real, sensible person rather than a random one. The rep mapping itself is
a *pure* function of ``org`` + the customer account id order (no RNG draw), so
``documents.py`` and ``tickets.py`` can independently recompute the exact same
mapping from ``(org, accounts)`` without threading an extra return value
through every builder — keeping each builder's signature exactly as specified.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from itertools import product

from ne.acme.models import OrgChart
from ne.acme.pools import (
    BASE_TIME,
    CUSTOMER_ADJECTIVES,
    CUSTOMER_NOUNS,
    CUSTOMER_SUFFIXES,
    INDUSTRIES,
)
from ne.acme.rand import DetRandom
from ne.schema import Account, AccountStatus, Order, OrderStatus, Person

ACME_ACCOUNT_ID = "acct-0000"

_ACCOUNT_STATUS_WEIGHTS: tuple[tuple[AccountStatus, int], ...] = (
    (AccountStatus.active, 70),
    (AccountStatus.prospect, 30),
    (AccountStatus.churned, 19),
)

_ORDER_STATUS_WEIGHTS: tuple[tuple[OrderStatus, int], ...] = (
    (OrderStatus.placed, 40),
    (OrderStatus.fulfilled, 35),
    (OrderStatus.draft, 10),
    (OrderStatus.cancelled, 10),
    (OrderStatus.refunded, 5),
)

N_CUSTOMER_ACCOUNTS = 119
N_ORDERS = 80


def _slug(text: str) -> str:
    return text.lower().replace(" ", "-")


def account_rep_map(org: OrgChart, accounts: list[Account]) -> dict[str, str]:
    """Deterministic customer-account -> Sales-dept-rep map (round-robin, no RNG).

    Pure function of ``org`` and the customer account id order, so every
    builder that needs "who owns this account" recomputes the identical
    mapping independently — no need to thread it through every signature.
    """
    sales_ids = [m.person_id for m in org.members if m.department == "Sales"]
    customer_ids = [acc.id for acc in accounts if acc.id != ACME_ACCOUNT_ID]
    return {account_id: sales_ids[i % len(sales_ids)] for i, account_id in enumerate(customer_ids)}


def build_crm(
    rng: DetRandom, people: list[Person], org: OrgChart
) -> tuple[list[Account], list[Order]]:
    """Build 120 accounts (incl. Acme) + 80 orders, in that order, deterministically."""
    del people  # person ids are looked up via org.members, not needed directly here

    acme = Account(
        id=ACME_ACCOUNT_ID,
        name="Acme Corporation",
        domain="acme.example",
        industry="Manufacturing",
        status=AccountStatus.active,
        employee_count=40,
        source_system="acme-crm",
        source_record_id="CRM-0000",
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )

    all_combos = list(product(CUSTOMER_ADJECTIVES, CUSTOMER_NOUNS))
    combos = rng.sample(all_combos, N_CUSTOMER_ACCOUNTS)

    accounts: list[Account] = [acme]

    for i, (adjective, noun) in enumerate(combos, start=1):
        account_id = f"acct-{i:04d}"
        suffix = rng.choice(CUSTOMER_SUFFIXES)
        name = f"{adjective} {noun} {suffix}"
        domain = f"{_slug(adjective)}-{_slug(noun)}.example"
        industry = rng.choice(INDUSTRIES)
        status = rng.weighted_choice(_ACCOUNT_STATUS_WEIGHTS)
        employee_count = rng.rand_int(10, 20000)
        created_days = rng.rand_int(60, 1500)
        updated_days = rng.rand_int(0, created_days)

        accounts.append(
            Account(
                id=account_id,
                name=name,
                domain=domain,
                industry=industry,
                status=status,
                employee_count=employee_count,
                source_system="acme-crm",
                source_record_id=f"CRM-{i:04d}",
                created_at=BASE_TIME - timedelta(days=created_days),
                updated_at=BASE_TIME - timedelta(days=updated_days),
            )
        )

    account_rep = account_rep_map(org, accounts)
    eligible_ids = [
        acc.id
        for acc in accounts
        if acc.id != ACME_ACCOUNT_ID and acc.status != AccountStatus.churned
    ]

    orders: list[Order] = []
    for i in range(N_ORDERS):
        account_id = rng.choice(eligible_ids)
        placed_by = account_rep[account_id]
        status = rng.weighted_choice(_ORDER_STATUS_WEIGHTS)
        dollars = Decimal(rng.rand_int(500, 250000))
        cents = Decimal(rng.rand_int(0, 99)) / Decimal(100)
        total_amount = (dollars + cents).quantize(Decimal("0.01"))
        days_ago = rng.rand_int(1, 540)
        minutes_ago = rng.rand_int(0, 1439)
        placed_at = BASE_TIME - timedelta(days=days_ago, minutes=minutes_ago)

        orders.append(
            Order(
                id=f"ord-{i:04d}",
                order_number=f"ORD-{i + 1:04d}",
                account=account_id,
                placed_by=placed_by,
                status=status,
                currency="USD",
                total_amount=total_amount,
                placed_at=placed_at,
                source_system="acme-crm",
                source_record_id=f"CRM-ORD-{i:04d}",
                created_at=placed_at,
                updated_at=placed_at,
            )
        )

    return accounts, orders
