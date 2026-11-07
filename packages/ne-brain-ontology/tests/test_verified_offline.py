# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085c C2: a verified answer runs end-to-end on oxigraph with sockets disabled."""

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


def test_end_to_end_grounded_cited_answer_no_sockets(no_network: None) -> None:
    async def scenario() -> None:
        reg = load_template_registry(FIXTURES_DIR)
        tpl = {t.template_id: t for t in reg.templates}
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_alpha", ["alpha"])])
        ans = await execute_template(
            tpl["accounts-directory"],
            {},
            store=gw,
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash="a" * 64,
            max_rows=500,
        )
        assert ans.rows == ((entity_iri("acc_alpha"), "acc_alpha Corp"),)
        assert ans.citations == (entity_iri("acc_alpha"),)

    asyncio.run(scenario())
