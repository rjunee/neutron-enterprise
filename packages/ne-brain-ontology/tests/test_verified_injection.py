# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-085c C12 proof: arg values bind as RDF literals via n3, never as query syntax."""

from __future__ import annotations

import asyncio

import pytest

from ne.brain_ontology import load_template_registry
from ne.brain_ontology.verified import execute_template, render_query
from ne.brain_ontology.verified.errors import TemplateRegistryError, VerifiedAnswerArgsError

from .verified_helpers import (
    FIXTURES_DIR,
    FREEFORM_DIR,
    RecordingStore,
    StubExpander,
    account,
    make_gateway,
    principal,
    seed_accounts,
)

_FREEFORM = {t.template_id: t for t in load_template_registry(FREEFORM_DIR).templates}
_GOLDEN = {t.template_id: t for t in load_template_registry(FIXTURES_DIR).templates}

_MALICIOUS = [
    '"} UNION { ?s ?p ?o } #',
    'x"\nSELECT ?z WHERE { ?z ?p ?o }',
    "a\\b`c^d|e",
    "' } } DELETE { ?s ?p ?o }",
]


@pytest.mark.parametrize("evil", _MALICIOUS)
def test_malicious_arg_binds_as_literal_no_widening(evil: str) -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_a", ["alpha"]), account("acc_b", ["beta"])])
        tpl = _FREEFORM["accounts-search"]
        # The FINAL rendered query still parses (render_query re-parses; would raise if not).
        rendered = render_query(tpl, {"q_name": evil}, 500)
        assert "VALUES" in rendered
        ans = await execute_template(
            tpl,
            {"q_name": evil},
            store=gw,
            expander=StubExpander(),
            principal=principal("user:a", ("alpha",)),
            config_hash="a" * 64,
            max_rows=500,
        )
        # No account is named the injection payload → zero rows, no widening.
        assert ans.rows == ()

    asyncio.run(scenario())


def test_pattern_violation_raises_before_any_store_call() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_a", ["alpha"])])
        rec = RecordingStore(gw)
        tpl = _GOLDEN["accounts-by-name"]  # has a strict pattern
        with pytest.raises(VerifiedAnswerArgsError):
            await execute_template(
                tpl,
                {"q_name": '"} UNION {'},
                store=rec,
                expander=StubExpander(),
                principal=principal("user:a", ("alpha",)),
                config_hash="a" * 64,
                max_rows=500,
            )
        assert rec.scoped_calls == 0  # rejected BEFORE any query reached the store

    asyncio.run(scenario())


def test_unknown_extra_arg_rejected() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_a", ["alpha"])])
        rec = RecordingStore(gw)
        with pytest.raises(VerifiedAnswerArgsError):
            await execute_template(
                _FREEFORM["accounts-search"],
                {"q_name": "acc_a Corp", "surprise": "x"},
                store=rec,
                expander=StubExpander(),
                principal=principal("user:a", ("alpha",)),
                config_hash="a" * 64,
                max_rows=500,
            )
        assert rec.scoped_calls == 0

    asyncio.run(scenario())


def test_iri_arg_with_forbidden_char_rejected() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_a", ["alpha"])])
        rec = RecordingStore(gw)
        # An iri arg containing '>' / whitespace is refused at render (before execution).
        with pytest.raises(TemplateRegistryError, match="forbidden character"):
            render_query(_GOLDEN["entity-by-iri"], {"account": "urn:ne:x> ?s ?p ?o"}, 500)
        assert rec.scoped_calls == 0

    asyncio.run(scenario())
