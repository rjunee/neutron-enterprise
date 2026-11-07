# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-7 Layer-3 COUNT execution — recount / mutation / C7 / replay / totals.

The RECOUNT-EQUIVALENCE + MUTATION tests are the load-bearing correctness anchor for
the declared trust downgrade (R4-P0-2): the grounding gate grounds aggregate cells by
CONTAINMENT and ``check_citations_in_dataset`` cannot ARITHMETICALLY validate a count,
so the server-side tally is verified instead by (a) matching every fixture cell to an
INDEPENDENT pure-Python row-join recount, and (b) proving that a deliberately
mis-rendered projection FAILS that recount. The C7 traps + two-execution replay pin
complete the anchor.

Canonical seed (spike-verified): accounts acct_acme[alpha]/'Acme Corp',
acct_beta[beta]/'Beta Corp', acct_both[alpha,beta]/'Both Inc'; persons p1[alpha],
p2[beta]; orders o1,o2[alpha]->acme, o3[alpha]->both, o4[beta]->both, o5[beta]->beta.
Each order + its ACCOUNT and PLACED_BY edges share the order's acl. Expected grouped
counts: alpha {acme:2, both:1}; beta {beta:1, both:1}; alpha+beta {acme:2, beta:1,
both:2} — and a PLAIN COUNT (no DISTINCT) returns both:8, the bag-union over-count
that the mutation test exploits.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from ne.brain_ontology.gateway import ValidatingTripleStore
from ne.brain_ontology.lift.graph_to_rdf import entity_iri
from ne.brain_ontology.store.protocols import DatasetSpec
from ne.brain_ontology.verified import answers as answers_mod
from ne.brain_ontology.verified.answers import (
    answers_equal,
    check_citations_in_dataset,
    execute_template,
)
from ne.brain_ontology.verified.errors import (
    VerifiedAnswerCountError,
    VerifiedAnswerScopeError,
)
from ne.brain_ontology.verified.scope import scoped_dataset
from ne.brain_ontology.verified.templates import load_template_registry, render_query

from .verified_helpers import (
    COUNT_DIR,
    RecordingStore,
    StubExpander,
    account_named,
    edge,
    make_gateway,
    order_node,
    person_node,
    principal,
    seed_graph,
)

_CFG = "a" * 64
_TPL = {t.template_id: t for t in load_template_registry(COUNT_DIR).templates}
_COUNT_TPL = _TPL["orders-per-account-count"]
_DIR_TOTAL_TPL = _TPL["accounts-directory-total"]

# --- canonical seed as pure data (the recount reads THIS, never the SPARQL) ---
_ACCOUNTS: dict[str, tuple[set[str], str]] = {
    "acct_acme": ({"alpha"}, "Acme Corp"),
    "acct_beta": ({"beta"}, "Beta Corp"),
    "acct_both": ({"alpha", "beta"}, "Both Inc"),
}
# (order_key, acl, account_key) — the ACCOUNT edge shares the order's acl.
_ORDERS: list[tuple[str, set[str], str]] = [
    ("o1", {"alpha"}, "acct_acme"),
    ("o2", {"alpha"}, "acct_acme"),
    ("o3", {"alpha"}, "acct_both"),
    ("o4", {"beta"}, "acct_both"),
    ("o5", {"beta"}, "acct_beta"),
]
_PERSON_OF = {"o1": "p1", "o2": "p1", "o3": "p1", "o4": "p2", "o5": "p2"}
_PERSON_ACL = {"p1": ["alpha"], "p2": ["beta"]}


def _recount(labels: set[str]) -> dict[str, int]:
    """INDEPENDENT pure-Python row-join recount from the seed dicts (the anchor).

    A group is visible iff its account is visible (account.acl ∩ labels); an order is
    counted iff BOTH the order (order.acl ∩ labels) AND its ACCOUNT edge (== order.acl)
    AND the account are visible. Zero-order accounts never appear (the body joins ?order).
    """
    out: dict[str, int] = {}
    for _key, acl, acct in _ORDERS:
        account_acl = _ACCOUNTS[acct][0]
        if (acl & labels) and (account_acl & labels):
            out[acct] = out.get(acct, 0) + 1
    return out


async def _seed(gw: ValidatingTripleStore) -> None:
    nodes: list[dict[str, Any]] = [
        account_named(key, sorted(acl), name) for key, (acl, name) in _ACCOUNTS.items()
    ]
    nodes.append(person_node("p1", ["alpha"]))
    nodes.append(person_node("p2", ["beta"]))
    edges: list[dict[str, Any]] = []
    for i, (key, acl, acct) in enumerate(_ORDERS, start=1):
        nodes.append(order_node(key, sorted(acl)))
        edges.append(edge("ACCOUNT", f"ea{i}", key, acct, sorted(acl)))
        edges.append(edge("PLACED_BY", f"ep{i}", key, _PERSON_OF[key], sorted(acl)))
    await seed_graph(gw, nodes, edges)


def _cells(ans: Any) -> dict[str, int]:
    """The aggregate answer as {account_key: count}, from the (account, name, n) rows."""
    out: dict[str, int] = {}
    for row in ans.rows:
        acct = row[0].replace("urn:ne:entity:", "")
        out[acct] = int(row[-1])
    return out


async def _run_count(
    gw: Any,
    groups: Sequence[str],
    *,
    max_rows: int = 500,
    store: Any | None = None,
    count_timeout_s: float = 2.0,
) -> Any:
    return await execute_template(
        _COUNT_TPL,
        {},
        store=store if store is not None else gw,
        expander=StubExpander(),
        principal=principal(f"user:{'-'.join(groups)}", tuple(groups)),
        config_hash=_CFG,
        max_rows=max_rows,
        count_timeout_s=count_timeout_s,
    )


class DatasetRecordingStore:
    """Wraps a gateway, recording (query, dataset) per call — the C7 identical-dataset pin."""

    def __init__(self, inner: ValidatingTripleStore) -> None:
        self._inner = inner
        self.calls: list[tuple[str, DatasetSpec]] = []

    async def sparql_scoped(
        self, query: str, *, dataset: DatasetSpec
    ) -> Sequence[Mapping[str, str]]:
        self.calls.append((query, dataset))
        return await self._inner.sparql_scoped(query, dataset=dataset)

    def ontology_version(self) -> Any:
        return self._inner.ontology_version()


class _CountQueryStore:
    """Delegates rows/citation reads to the gateway; intercepts ONLY the ne_total probe."""

    def __init__(self, inner: ValidatingTripleStore) -> None:
        self._inner = inner

    async def _on_count(self, query: str, dataset: DatasetSpec) -> Sequence[Mapping[str, str]]:
        raise NotImplementedError

    async def sparql_scoped(
        self, query: str, *, dataset: DatasetSpec
    ) -> Sequence[Mapping[str, str]]:
        if "ne_total" in query:
            return await self._on_count(query, dataset)
        return await self._inner.sparql_scoped(query, dataset=dataset)

    def ontology_version(self) -> Any:
        return self._inner.ontology_version()


# --------------------------------------------------------------------------- recount


def test_recount_equivalence_python_row_join() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await _seed(gw)
        expected = {
            frozenset({"alpha"}): {"acct_acme": 2, "acct_both": 1},
            frozenset({"beta"}): {"acct_beta": 1, "acct_both": 1},
            frozenset({"alpha", "beta"}): {"acct_acme": 2, "acct_beta": 1, "acct_both": 2},
        }
        for groups, want in [
            (["alpha"], expected[frozenset({"alpha"})]),
            (["beta"], expected[frozenset({"beta"})]),
            (["alpha", "beta"], expected[frozenset({"alpha", "beta"})]),
        ]:
            ans = await _run_count(gw, groups)
            cells = _cells(ans)
            assert cells == _recount(set(groups))  # INDEPENDENT recount
            assert cells == want  # hard-frozen spike expectation

    asyncio.run(scenario())


def test_mutation_broken_projection_fails_recount(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await _seed(gw)
        real = render_query  # the genuine renderer (monkeypatch replaces the answers name)

        # Variant 1: drop DISTINCT — the C7 bag union over-counts acct_both as 8.
        def broken_no_distinct(tpl: Any, args: Any, mr: int) -> str:
            return real(tpl, args, mr).replace("COUNT(DISTINCT ?order)", "COUNT(?order)")

        monkeypatch.setattr(answers_mod, "render_query", broken_no_distinct)
        cells = _cells(await _run_count(gw, ["alpha", "beta"]))
        assert cells["acct_both"] == 8  # the bag-semantics over-count (spike-verified)
        assert cells != _recount({"alpha", "beta"})  # recount equivalence now FAILS

        # Variant 2: count the wrong variable — every group collapses to 1.
        def broken_wrong_var(tpl: Any, args: Any, mr: int) -> str:
            return real(tpl, args, mr).replace("COUNT(DISTINCT ?order)", "COUNT(DISTINCT ?account)")

        monkeypatch.setattr(answers_mod, "render_query", broken_wrong_var)
        cells2 = _cells(await _run_count(gw, ["alpha", "beta"]))
        assert cells2 != _recount({"alpha", "beta"})

    asyncio.run(scenario())


# ------------------------------------------------------------------------------ C7


def test_count_c7_outsider_sees_smaller_grouped_counts() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await _seed(gw)
        alpha = _cells(await _run_count(gw, ["alpha"]))
        beta = _cells(await _run_count(gw, ["beta"]))
        both = _cells(await _run_count(gw, ["alpha", "beta"]))
        # acct_both's count grows as the principal's visibility grows.
        assert alpha["acct_both"] == 1
        assert beta["acct_both"] == 1
        assert both["acct_both"] == 2
        # A foreign-only account is entirely absent from the outsider's rows.
        assert "acct_beta" not in alpha
        assert "acct_acme" not in beta
        # An empty class intersection fails closed (existing behavior).
        with pytest.raises(VerifiedAnswerScopeError):
            await _run_count(gw, ["gamma"])

    asyncio.run(scenario())


def test_total_rows_probe_rows_kind_and_c7() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await _seed(gw)

        # Untruncated: complete enumeration, NO probe — exactly 2 reads (rows + citation).
        untrunc = DatasetRecordingStore(gw)
        ans_u = await execute_template(
            _DIR_TOTAL_TPL,
            {},
            store=untrunc,
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash=_CFG,
            max_rows=500,
        )
        assert ans_u.truncated is False
        assert len(untrunc.calls) == 2

        # Truncated: the probe fires (3rd read); total_rows is the exact solution count.
        trunc = DatasetRecordingStore(gw)
        ans_t = await execute_template(
            _DIR_TOTAL_TPL,
            {},
            store=trunc,
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash=_CFG,
            max_rows=1,
        )
        assert ans_t.truncated is True
        assert ans_t.total_rows == 2  # alpha directory: acme + both
        assert len(trunc.calls) == 3
        datasets = [d for _q, d in trunc.calls]
        assert all(d == datasets[0] for d in datasets)  # C7: identical DatasetSpec

        # Insider (alpha+beta) sees a STRICTLY LARGER total than the outsider (alpha).
        ins = DatasetRecordingStore(gw)
        ans_ab = await execute_template(
            _DIR_TOTAL_TPL,
            {},
            store=ins,
            expander=StubExpander(),
            principal=principal("user:ab", ("alpha", "beta")),
            config_hash=_CFG,
            max_rows=1,
        )
        # Bag semantics: acct_both's type+name triples each duplicate across its 2 ACL
        # graphs, so it contributes 2x2=4 solutions (== the rows a full enumeration
        # returns for it — the multi-label duplicate-row behavior is PRE-EXISTING and
        # out of scope here); acme(1) + beta(1) + both(4) = 6.
        assert ans_ab.total_rows == 6
        assert ans_t.total_rows < ans_ab.total_rows  # outsider total strictly smaller

    asyncio.run(scenario())


def test_total_rows_probe_count_kind_group_total() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await _seed(gw)
        ans = await _run_count(gw, ["alpha", "beta"], max_rows=1)
        assert ans.truncated is True
        assert ans.total_rows == 3  # the DISTINCT group count (acme, beta, both)

    asyncio.run(scenario())


def test_total_rows_probe_timeout_leaves_unknown() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await _seed(gw)

        class _Sleeper(_CountQueryStore):
            async def _on_count(
                self, query: str, dataset: DatasetSpec
            ) -> Sequence[Mapping[str, str]]:
                await asyncio.sleep(10)
                return []

        ans = await execute_template(
            _DIR_TOTAL_TPL,
            {},
            store=_Sleeper(gw),
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash=_CFG,
            max_rows=1,
            count_timeout_s=0.05,
        )
        assert ans.truncated is True
        assert ans.total_rows is None  # honest unknown, never fabricated

    asyncio.run(scenario())


def test_total_rows_probe_malformed_raises() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await _seed(gw)
        payloads: list[list[dict[str, str]]] = [
            [],
            [{"ne_total": "x"}],
            [{"ne_total": "1"}, {"ne_total": "2"}],
        ]

        class _Malformed(_CountQueryStore):
            def __init__(self, inner: ValidatingTripleStore, payload: Sequence[Any]) -> None:
                super().__init__(inner)
                self._payload = payload

            async def _on_count(
                self, query: str, dataset: DatasetSpec
            ) -> Sequence[Mapping[str, str]]:
                return self._payload

        for payload in payloads:
            with pytest.raises(VerifiedAnswerCountError):
                await execute_template(
                    _DIR_TOTAL_TPL,
                    {},
                    store=_Malformed(gw, payload),
                    expander=StubExpander(),
                    principal=principal("user:a", ("alpha",)),
                    config_hash=_CFG,
                    max_rows=1,
                )

    asyncio.run(scenario())


# -------------------------------------------------------------------------- replay


def test_replay_pin_with_count_on() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await _seed(gw)
        # Untruncated: two executions are procedurally identical.
        a1 = await _run_count(gw, ["alpha", "beta"])
        a2 = await _run_count(gw, ["alpha", "beta"])
        assert answers_equal(a1, a2)
        # Truncated with the total probe ON: still identical (static fixture store).
        t1 = await _run_count(gw, ["alpha", "beta"], max_rows=1)
        t2 = await _run_count(gw, ["alpha", "beta"], max_rows=1)
        assert answers_equal(t1, t2)
        # A store write between runs breaks replay equality (a new visible group).
        await seed_graph(
            gw,
            [
                account_named("acct_new", ["alpha"], "New Inc"),
                person_node("p3", ["alpha"]),
                order_node("o6", ["alpha"]),
            ],
            [
                edge("ACCOUNT", "ea6", "o6", "acct_new", ["alpha"]),
                edge("PLACED_BY", "ep6", "o6", "p3", ["alpha"]),
            ],
        )
        a3 = await _run_count(gw, ["alpha", "beta"])
        assert not answers_equal(a1, a3)

    asyncio.run(scenario())


# ----------------------------------------------------------------------- citations


def test_count_citations_are_group_key_iris() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await _seed(gw)
        ans = await _run_count(gw, ["alpha", "beta"])
        account_col = tuple(row[0] for row in ans.rows)
        assert ans.citations == account_col  # per-group citation == the group-key IRI
        # The citation double-check RAN on the aggregate path (rows + probe == 2 reads).
        rec = RecordingStore(gw)
        await execute_template(
            _COUNT_TPL,
            {},
            store=rec,
            expander=StubExpander(),
            principal=principal("user:ab", ("alpha", "beta")),
            config_hash=_CFG,
            max_rows=500,
        )
        assert rec.scoped_calls == 2
        # C7 belt kept: a beta citation checked against an alpha-only dataset still raises.
        alpha_ds = scoped_dataset(principal("user:a", ("alpha",)), StubExpander(), _DIR_TOTAL_TPL)
        with pytest.raises(VerifiedAnswerScopeError):
            await check_citations_in_dataset(gw, alpha_ds, [entity_iri("acct_beta")])

    asyncio.run(scenario())


def test_count_answer_shape_matches_redteam_fixture() -> None:
    # Shape-parity with packages/ne-govern/tests/test_grounding_redteam.py FIXTURE A
    # (template 'accounts-orders-count', columns account/name/n_orders): the merged
    # red-team suite proves the count sentence PASSes and wrong-row/wrong-count/
    # unseen-row sentences FAIL on EXACTLY this shape. That suite is contract law here
    # — coupled by shape parity + this comment, never edited, never imported (C3 lane).
    async def scenario() -> None:
        gw = make_gateway()
        await _seed(gw)
        ans = await _run_count(gw, ["alpha", "beta"])
        dumped = ans.model_dump(mode="json")
        assert {
            "template_id",
            "template_version",
            "config_hash",
            "columns",
            "rows",
            "citations",
            "truncated",
            "args",
            "returned_rows",
            "total_rows",
            "row_limit",
        } <= set(dumped)
        assert dumped["columns"] == ["account", "name", "n_orders"]
        for row in ans.rows:
            assert re.match(r"^\d+$", row[-1])  # plain decimal string, no thousands sep
        assert ans.citations == tuple(row[0] for row in ans.rows)
        assert ans.truncated is False
        assert ans.args == {}

    asyncio.run(scenario())
