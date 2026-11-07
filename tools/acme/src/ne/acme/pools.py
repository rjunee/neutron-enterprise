# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fixed constant word-pools and templates for the Acme corpus generator.

Data only — no logic. Using fixed stdlib pools (instead of a name-generation
library such as Faker) is what makes byte-identical fixtures possible across
dependency upgrades: a library's bundled locale data can change between
releases even when the seed and call sequence do not.
"""

from __future__ import annotations

from datetime import UTC, datetime

ACME_DOMAIN = "acme.example"
BASE_TIME = datetime(2026, 1, 5, 9, 0, 0, tzinfo=UTC)
DEFAULT_SEED = 74
CORPUS_VERSION = "acme-v1"

# 40 ordinary given names (obviously-synthetic, no real-person association).
GIVEN_NAMES: tuple[str, ...] = (
    "Alex",
    "Blair",
    "Casey",
    "Dana",
    "Elliot",
    "Frankie",
    "Gale",
    "Harper",
    "Iris",
    "Jordan",
    "Kai",
    "Lane",
    "Morgan",
    "Nico",
    "Oakley",
    "Parker",
    "Quinn",
    "Riley",
    "Sam",
    "Taylor",
    "Uma",
    "Val",
    "Winter",
    "Xan",
    "Yael",
    "Zion",
    "Ash",
    "Brooke",
    "Cleo",
    "Drew",
    "Emery",
    "Finley",
    "Grey",
    "Hollis",
    "Indigo",
    "Jules",
    "Kit",
    "Lior",
    "Marlowe",
    "Noor",
)

# 40 surnames (obviously-synthetic).
FAMILY_NAMES: tuple[str, ...] = (
    "Anders",
    "Bishop",
    "Carver",
    "Dalton",
    "Ellison",
    "Fenwick",
    "Grover",
    "Hollis",
    "Ingram",
    "Jasper",
    "Keane",
    "Lindqvist",
    "Mercer",
    "Nakamura",
    "Osei",
    "Prescott",
    "Quimby",
    "Renner",
    "Sorensen",
    "Thackeray",
    "Uribe",
    "Vance",
    "Whitfield",
    "Xiong",
    "Yates",
    "Zeller",
    "Ashworth",
    "Blackwood",
    "Chappell",
    "Delgado",
    "Eastman",
    "Farrow",
    "Gallo",
    "Hargrove",
    "Inoue",
    "Jarrett",
    "Kowalski",
    "Larkspur",
    "Monteiro",
    "Novak",
)

CUSTOMER_ADJECTIVES: tuple[str, ...] = (
    "Northwind",
    "Summit",
    "Cascade",
    "Harbor",
    "Granite",
    "Silverline",
    "Meridian",
    "Ironclad",
    "Bluepeak",
    "Amberfield",
    "Cobalt",
    "Redshift",
)

CUSTOMER_NOUNS: tuple[str, ...] = (
    "Metals",
    "Logistics",
    "Robotics",
    "Foods",
    "Analytics",
    "Freight",
    "Dynamics",
    "Textiles",
    "Systems",
    "Ventures",
    "Holdings",
    "Aerospace",
)

CUSTOMER_SUFFIXES: tuple[str, ...] = ("Inc", "LLC", "GmbH", "Ltd")

INDUSTRIES: tuple[str, ...] = (
    "Manufacturing",
    "Logistics",
    "Retail",
    "Healthcare",
    "Financial Services",
    "Aerospace",
    "Agriculture",
    "Energy",
    "Construction",
    "Telecommunications",
    "Hospitality",
    "Education",
)

# Obviously-fictional Acme joke products.
PRODUCTS: tuple[str, ...] = (
    "Anvil Pro X",
    "Rocket Skates 2000",
    "Portable Hole Kit",
    "Giant Rubber Band",
    "Earthquake Pills",
    "Dehydrated Boulders",
    "Instant Tunnel Paint",
    "Jet-Propelled Pogo Stick",
)

TICKET_SUBJECT_TEMPLATES: tuple[str, ...] = (
    "{product} arrived with a dent",
    "{product} won't power on",
    "Need replacement part for {product}",
    "{product} shipment delayed",
    "How do I return a {product}?",
    "{product} makes a strange noise",
    "Billing question about {product} order",
    "{product} missing installation guide",
)

DOC_TITLE_TEMPLATES: dict[str, tuple[str, ...]] = {
    "policy": (
        "{year} Travel & Expense Policy",
        "{year} Remote Work Policy",
        "{department} Data Handling Policy",
        "{year} Code of Conduct",
        "{department} Security Policy",
        "{year} Leave & PTO Policy",
    ),
    "report": (
        "{department} Quarterly Report {year}",
        "{year} Annual {department} Summary",
        "{account} Account Health Report",
        "{department} Headcount Report {year}",
        "{year} {department} Incident Report",
    ),
    "contract": (
        "Master Services Agreement — {account}",
        "{product} Supply Contract — {account}",
        "{year} Renewal Agreement — {account}",
        "Non-Disclosure Agreement — {account}",
    ),
    "correspondence": (
        "Re: {product} order for {account}",
        "Follow-up: {account} account review",
        "{account} — shipment confirmation",
        "Re: {department} question from {account}",
    ),
    "presentation": (
        "{department} All-Hands Deck {year}",
        "{product} Product Overview",
        "{year} Strategy Presentation",
        "{department} Roadmap {year}",
    ),
}

# Fact templates: (statement_template, question_template, answer) — the answer
# is a literal fragment that must also be embedded verbatim in the generated
# body via the matching body-paragraph template (see documents.py).
#
# Coherence rule (ground-truth invariant): account/order-specific fact
# templates (ACCOUNT_FACT_TEMPLATES) may only ever be formatted for documents
# whose `about` actually links the account being cited — never sampled onto
# an unrelated document. `renewal_year` is a pure function of the account id
# (see documents._renewal_year), not an RNG draw, so every document about the
# same account states the exact same renewal date: the renewal question has
# exactly one corpus-wide answer per account, and (combined with the
# about-gating above) exactly one answer overall once paired with a specific
# account's name. General templates (GENERAL_FACT_TEMPLATES) never reference
# {account} and are eligible on every document.
GENERAL_FACT_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    (
        "The {year} travel policy caps daily per-diem at $85.",
        "What is the daily per-diem cap in the {year} travel policy?",
        "$85",
    ),
    (
        "The {department} department requires security training within 30 days of hire.",
        "How many days does a new {department} hire have to complete security training?",
        "30 days",
    ),
    (
        "The {year} remote work policy allows up to 3 remote days per week.",
        "How many remote days per week does the {year} remote work policy allow?",
        "3 remote days",
    ),
    (
        "The {department} incident response SLA is 4 hours for urgent issues.",
        "What is the {department} incident response SLA for urgent issues?",
        "4 hours",
    ),
    (
        "{product} carries a list price of $1,299.",
        "What is the list price of {product}?",
        "$1,299",
    ),
    (
        "The {year} code of conduct requires annual re-certification by all employees.",
        "How often must employees re-certify under the {year} code of conduct?",
        "annual",
    ),
)

ACCOUNT_FACT_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    (
        "{account}'s {product} order includes a 2-year warranty.",
        "How long is the warranty on {account}'s {product} order?",
        "2-year",
    ),
    (
        "{account}'s account renewal is scheduled for Q3 {renewal_year}.",
        "When is {account}'s account renewal scheduled?",
        "Q3 {renewal_year}",
    ),
)
