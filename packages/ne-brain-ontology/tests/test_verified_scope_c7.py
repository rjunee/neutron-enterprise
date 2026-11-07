# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085c C7 acceptance: the planted hidden-entity non-leak probe + fail-closed scope."""

from __future__ import annotations

import asyncio

import pytest

from ne.brain_ontology import graphs_for_labels, load_template_registry
from ne.brain_ontology.gateway import ValidatingTripleStore
from ne.brain_ontology.lift.graph_to_rdf import entity_iri
from ne.brain_ontology.verified import (
    check_citations_in_dataset,
    execute_template,
    principal_labels,
    scoped_dataset,
)
from ne.brain_ontology.verified.errors import VerifiedAnswerScopeError

from .verified_helpers import (
    FIXTURES_DIR,
    EmptyExpander,
    RaisingExpander,
    StubExpander,
    account,
    make_gateway,
    principal,
    seed_accounts,
)

_TPL = {t.template_id: t for t in load_template_registry(FIXTURES_DIR).templates}


async def _seed() -> ValidatingTripleStore:
    gw = make_gateway()
    await seed_accounts(
        gw,
        [
            account("acc_alpha", ["alpha"]),
            account("acc_beta", ["beta"]),
            account("acc_both", ["alpha", "beta"]),
        ],
    )
    return gw


def test_enumerable_returns_only_own_label_rows() -> None:
    async def scenario() -> None:
        gw = await _seed()
        ans = await execute_template(
            _TPL["accounts-directory"],
            {},
            store=gw,
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash="a" * 64,
            max_rows=500,
        )
        seen = {row[0] for row in ans.rows}
        assert entity_iri("acc_alpha") in seen
        assert entity_iri("acc_both") in seen
        # The beta-only entity NEVER appears for an alpha principal.
        assert entity_iri("acc_beta") not in seen
        # citations are exactly the visible entity IRIs.
        assert set(ans.citations) == seen

    asyncio.run(scenario())


def test_fetch_by_known_hidden_iri_returns_zero_rows() -> None:
    async def scenario() -> None:
        gw = await _seed()
        # The SAME entity-by-iri template, called with the KNOWN hidden beta IRI.
        ans = await execute_template(
            _TPL["entity-by-iri"],
            {"account": entity_iri("acc_beta")},
            store=gw,
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash="a" * 64,
            max_rows=500,
        )
        assert ans.rows == ()

    asyncio.run(scenario())


def test_broken_and_empty_expander_never_empty_never_full_union() -> None:
    p = principal("user:a", ("alpha",))
    # Empty expander → still unions {principal.id}, never empty.
    labels = principal_labels(p, EmptyExpander())
    assert labels == frozenset({"user:a"})
    # An expander exception PROPAGATES (deny-by-erroring).
    with pytest.raises(RuntimeError, match="backend down"):
        principal_labels(p, RaisingExpander())


def test_empty_class_intersection_raises_fail_closed() -> None:
    # principal holds only 'gamma'; template graph_classes are alpha/beta → empty ∩.
    p = principal("user:c", ("gamma",))
    with pytest.raises(VerifiedAnswerScopeError, match="fail-closed"):
        scoped_dataset(p, StubExpander(), _TPL["accounts-directory"])


def test_prefix_label_does_not_match_graph_class_exact_only() -> None:
    # A principal whose only label SHARES A PREFIX with a template graph_class
    # ('alphabet' vs 'alpha') must NOT gain access — the intersection is exact.
    p = principal("user:d", ("alphabet",))
    with pytest.raises(VerifiedAnswerScopeError, match="fail-closed"):
        scoped_dataset(p, StubExpander(), _TPL["accounts-directory"])
    # The reverse (graph_class is a prefix of the held label already covered) and a
    # genuine exact match still resolves to that principal's own graph only.
    exact = scoped_dataset(
        principal("user:e", ("alpha",)), StubExpander(), _TPL["accounts-directory"]
    )
    assert exact.default_graphs == graphs_for_labels(["alpha"]).default_graphs


def test_provenance_double_check_raises_on_out_of_dataset_citation() -> None:
    async def scenario() -> None:
        gw = await _seed()
        # Narrow the dataset to alpha only, but claim a beta-only citation → must raise.
        narrow = graphs_for_labels(["alpha"])
        with pytest.raises(VerifiedAnswerScopeError, match="provenance"):
            await check_citations_in_dataset(gw, narrow, [entity_iri("acc_beta")])
        # A visible citation passes cleanly.
        await check_citations_in_dataset(gw, narrow, [entity_iri("acc_alpha")])

    asyncio.run(scenario())
