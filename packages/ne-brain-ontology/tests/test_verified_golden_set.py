# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085c golden verified-answer regression set (the §7.4-step-5 migration gate seed).

Fixture templates + a fixed seeded store → byte-exact expected STRUCTURED results.
Order-independent by design (SPARQL result order is unspecified): rows/citations are
compared as sorted tuples, which is the stable structured contract a migration gate
diffs against.
"""

from __future__ import annotations

import asyncio

from ne.brain_ontology import load_template_registry
from ne.brain_ontology.lift.graph_to_rdf import entity_iri
from ne.brain_ontology.verified import execute_template

from .verified_helpers import (
    FIXTURES_DIR,
    StubExpander,
    account,
    make_gateway,
    principal,
    seed_accounts,
)

_TPL = {t.template_id: t for t in load_template_registry(FIXTURES_DIR).templates}


def _sorted_rows(ans: object) -> tuple[tuple[str, ...], ...]:
    return tuple(sorted(ans.rows))  # type: ignore[attr-defined]


def test_golden_directory_alpha_principal() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(
            gw,
            [
                account("acc_alpha", ["alpha"]),
                account("acc_beta", ["beta"]),
                account("acc_both", ["alpha", "beta"]),
            ],
        )
        ans = await execute_template(
            _TPL["accounts-directory"],
            {},
            store=gw,
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash="c" * 64,
            max_rows=500,
        )
        assert _sorted_rows(ans) == (
            (entity_iri("acc_alpha"), "acc_alpha Corp"),
            (entity_iri("acc_both"), "acc_both Corp"),
        )
        assert tuple(sorted(ans.citations)) == (
            entity_iri("acc_alpha"),
            entity_iri("acc_both"),
        )
        assert ans.config_hash == "c" * 64
        assert len(ans.ontology_version) == 64  # sha256 hex digest of the version registry

    asyncio.run(scenario())


def test_golden_by_name_and_fetch_by_iri() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_alpha", ["alpha"]), account("acc_beta", ["beta"])])
        p = principal("user:a", ("alpha",))
        by_name = await execute_template(
            _TPL["accounts-by-name"],
            {"q_name": "acc_alpha Corp"},
            store=gw,
            expander=StubExpander(),
            principal=p,
            config_hash="c" * 64,
            max_rows=500,
        )
        assert by_name.rows == ((entity_iri("acc_alpha"), "acc_alpha Corp"),)
        by_iri = await execute_template(
            _TPL["entity-by-iri"],
            {"account": entity_iri("acc_alpha")},
            store=gw,
            expander=StubExpander(),
            principal=p,
            config_hash="c" * 64,
            max_rows=500,
        )
        assert by_iri.rows == ((entity_iri("acc_alpha"), "acc_alpha Corp"),)

    asyncio.run(scenario())
