# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-5 §4.4: the trusted deterministic ORDER BY makes the kept prefix under
truncation engine-plan-INDEPENDENT.

Red-first evidence (recorded in the PR body, not in code): with STEP 2's ORDER BY
injection reverted in ``render_query`` (drop the ``\\n{order_by}`` fragment so the
query is just ``body`` + ``LIMIT n+1``), this test FAILS — the permuted
insertion orders yield different kept prefixes under the row cap. With the trusted
ORDER BY appended before the LIMIT, every permutation yields the byte-identical
kept rows + citations.

WHY A FAKE STORE, NOT THE OXIGRAPH GATEWAY (Argus P1, iteration 1): ``OxigraphBackend``
returns SELECT rows in a stable, sorted-index order REGARDLESS of insertion order, so
an oxigraph-backed permutation test can never exhibit store-iteration nondeterminism —
reverting the ORDER BY would leave it GREEN, making the "fails today, passes after"
claim false. This test therefore drives ``execute_template`` through
``_InsertionOrderStore``: a minimal :class:`VerifiedReadStore` that returns rows in
the SEEDED insertion order UNLESS the query carries an ``ORDER BY`` (the honest model
of a store whose engine plan is insertion-driven, e.g. a naive triple store or a
Fuseki plan without a covering index). Only the trusted ORDER BY injected by
``render_query`` can then make the kept prefix permutation-independent — so reverting
that injection genuinely flips this test red.
"""

from __future__ import annotations

import asyncio
import re

from ne.brain_ontology import load_template_registry
from ne.brain_ontology.ontology import OntologyVersion
from ne.brain_ontology.store.protocols import DatasetSpec
from ne.brain_ontology.verified import execute_template

from .verified_helpers import (
    FIXTURES_DIR,
    StubExpander,
    make_gateway,
    principal,
)

_TPL = {t.template_id: t for t in load_template_registry(FIXTURES_DIR).templates}

#: A real :class:`OntologyVersion` (manifest pins + shapes hash — data-independent),
#: borrowed once so the fake store can satisfy the ``VerifiedReadStore`` protocol
#: without standing up a backend.
_ONTOLOGY_VERSION = make_gateway().ontology_version()


def _account_row(key: str) -> dict[str, str]:
    """One ``accounts-directory`` result row: citation IRI + display name."""
    return {"account": f"urn:ne:entity:{key}", "name": f"{key} Corp"}


class _InsertionOrderStore:
    """A ``VerifiedReadStore`` that returns rows in SEEDED insertion order.

    The adversarial counterpart to ``OxigraphBackend``: it honours an ``ORDER BY``
    clause when present (sorting ascending by the named vars) but otherwise returns
    rows in the exact order they were seeded — so a permuted seed order is visible
    at the read seam. This is what makes the trusted ORDER BY load-bearing for
    determinism: without it, ``execute_template`` keeps a permutation-dependent
    prefix; with it, every permutation keeps the same rows.
    """

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = list(rows)  # preserve the seeded (permuted) order verbatim

    async def sparql_scoped(self, query: str, *, dataset: DatasetSpec) -> list[dict[str, str]]:
        # The provenance double-check probe (SELECT DISTINCT ?s ... ?s ?p ?o):
        # return every seeded subject so any kept citation is found in-scope.
        if "?s ?p ?o" in query:
            return [{"s": row["account"]} for row in self._rows]

        rows = list(self._rows)
        order_match = re.search(r"ORDER BY\s+(.+)", query)
        if order_match is not None:
            order_vars = re.findall(r"\?(\w+)", order_match.group(1))
            rows.sort(key=lambda r: tuple(r.get(var, "") for var in order_vars))
        limit_match = re.search(r"LIMIT\s+(\d+)", query)
        if limit_match is not None:
            rows = rows[: int(limit_match.group(1))]
        return rows

    def ontology_version(self) -> OntologyVersion:
        return _ONTOLOGY_VERSION


def test_shuffled_store_yields_byte_identical_kept_set() -> None:
    async def kept_for(order: list[str]) -> tuple[tuple[tuple[str, ...], ...], tuple[str, ...]]:
        store = _InsertionOrderStore([_account_row(key) for key in order])
        ans = await execute_template(
            _TPL["accounts-directory"],
            {},
            store=store,
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash="a" * 64,
            max_rows=2,  # force a truncated 2-row kept prefix out of 3 accounts
        )
        assert ans.truncated
        assert ans.truncation_reason == "rows"
        return ans.rows, ans.citations

    async def scenario() -> None:
        permutations = [
            ["acc_a", "acc_b", "acc_c"],
            ["acc_c", "acc_b", "acc_a"],
            ["acc_b", "acc_c", "acc_a"],
        ]
        results = [await kept_for(order) for order in permutations]
        first = results[0]
        # With the trusted ORDER BY the kept prefix is the two lexicographically
        # smallest citation IRIs (acc_a, acc_b) for EVERY permutation.
        assert first[0] == (
            ("urn:ne:entity:acc_a", "acc_a Corp"),
            ("urn:ne:entity:acc_b", "acc_b Corp"),
        )
        for other in results[1:]:
            assert other == first  # byte-identical kept rows + citations across permutations

    asyncio.run(scenario())
