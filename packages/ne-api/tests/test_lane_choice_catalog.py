# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-LANE-CHOICE: the tool catalog says enough to pick the right lane.

THE DEFECT. The Analyst holds a document-search tool and seven verified-answer
tools over the same corpus, and the catalog is the ONLY thing it sees when it
picks between them. Live 2026-08-27, asked for a total recorded amount, it
searched documents, found process prose, and reported the figure unavailable --
while a granted verified-answer tool held the rows. A neighbouring question on
the SAME build called the verified tool and got them, so the data was reachable
and nothing was wrong but the choice.

THE READING. ``read.knowledge.search`` was described as "Search the governed
knowledge base": a description of the STORE, and a store said to hold the
knowledge is the obvious first call for every question there is. The
verified-answer entries, meanwhile, said WHICH records they read but never what
SHAPE came back, so the words a record question is built from -- amounts, dates,
ids, statuses -- appeared nowhere in that lane's catalog text at all.

WHAT IS FIXED, AND WHAT IS DELIBERATELY NOT. Both sides now describe their RETURN
VALUE, which is what the choice actually turns on and what a description field is
for. No imperative was added, no tool is named in another's description, no
ranking is stated and the system prompt is untouched: the model still chooses, it
just chooses informed. These tests hold that line in both directions -- the
descriptions must carry the discriminating facts, and must not carry a decision.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from ne.api.tools import TOOL_NAME, build_chat_bindings
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.ontology import OntologyVersion
from ne.brain_ontology.verified import build_verified_answer_tools
from ne.brain_ontology.verified.meridian import load_meridian_template_registry
from ne.brain_ontology.verified.tools import CATALOG_SHAPE_CLAUSE
from ne.contracts.types import Principal, RetrievalResult
from ne.runtime.model import catalog_text_for_tool_specs

#: The words a question about records is actually built from. Not an exhaustive
#: list and not a routing rule -- the point is only that this vocabulary must
#: reach the lane that can answer such a question.
_RECORD_VOCABULARY = ("ids", "amounts", "dates", "statuses")


class _StubRetriever:
    """Minimal ``KnowledgeRead``: this suite reads the tool's SPEC, never its results."""

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        return RetrievalResult(chunks=(), citations=())

    async def retrieve(self, chunk_ids: Sequence[str], *, principal: Principal) -> RetrievalResult:
        raise NotImplementedError


def _search_spec() -> Any:
    (binding,) = build_chat_bindings(_StubRetriever(), extraction_mode="ingest_review")
    return binding.spec


class _SpecOnlyStore:
    """The only thing the BUILDER asks a store for is the ontology version stamp.

    Handlers are never invoked here, so a store that answers that one question is
    the whole dependency -- and keeping it that small is what lets this test
    exercise the real builder rather than a re-implementation of it.
    """

    def ontology_version(self) -> OntologyVersion:
        return OntologyVersion(sources={}, file_sha256={}, shapes_hash="0" * 64)


def _verified_specs() -> tuple[Any, ...]:
    registry = load_meridian_template_registry()
    defs = build_verified_answer_tools(
        registry=registry,
        store=cast(Any, _SpecOnlyStore()),
        expander=cast(Any, None),
        principal_provider=lambda: None,
        settings=OntologySettings(),
        config_hash="a" * 64,
    )
    return tuple(d.spec for d in defs)


def test_the_document_lane_describes_what_it_returns_not_the_store_it_reads() -> None:
    """It says prose comes back, and says the one thing that does not.

    Naming the exclusion is the load-bearing half. "Returns prose" alone still
    leaves a model free to assume prose is where an amount is written down; saying
    that stored field values are NOT what comes back is the fact the misroute
    turned on, and it is a true statement about this tool rather than a hint about
    another one.
    """
    description = _search_spec().description
    lowered = description.lower()
    assert "prose" in lowered
    assert "returns" in lowered
    assert "not a record's stored field values" in lowered
    # A description, not a directive: no imperative mood, and no other tool named.
    assert "read.kg" not in lowered
    assert "instead" not in lowered
    assert "use " not in lowered


def test_every_verified_answer_tool_states_its_row_shape() -> None:
    """The clause is appended by the BUILDER, so no template can be missing it.

    Writing it into each template's own ``description`` would put it in the hands
    of whoever adds the next template, and the one that forgot would be exactly
    the one that never gets called. Every template here is ``result_shape=
    'enumerable'`` by load-time refusal, so the sentence is true of all of them by
    construction and a template added tomorrow inherits it.
    """
    specs = _verified_specs()
    assert len(specs) >= 7
    registry_descriptions = {
        t.template_id: t.description for t in load_meridian_template_registry().templates
    }
    for spec in specs:
        assert spec.description.endswith(CATALOG_SHAPE_CLAUSE)
        # The template's own words are kept whole and lead: the clause ADDS the
        # shape, it never replaces what the template says it reads.
        own = spec.description[: -len(CATALOG_SHAPE_CLAUSE)]
        assert own in registry_descriptions.values()


def test_the_record_vocabulary_reaches_the_lane_that_holds_the_records() -> None:
    """The catalog the model is actually sent, checked as one document.

    Rendered through the real renderer rather than asserted spec by spec, because
    the catalog is what the model reads and a fact that survives in isolation but
    not in the rendering is not a fact the model has.
    """
    catalog = catalog_text_for_tool_specs((_search_spec(), *_verified_specs()))
    entries = {
        entry["name"]: entry["description"]
        for entry in (
            json.loads(line) for line in catalog.splitlines() if line.startswith('{"description"')
        )
    }
    assert TOOL_NAME in entries
    kg_entries = {name: text for name, text in entries.items() if name.startswith("read.kg.")}
    assert len(kg_entries) >= 7

    for word in _RECORD_VOCABULARY:
        assert all(word in text for text in kg_entries.values()), word
        # And it stays OFF the prose lane, which is what makes it discriminating
        # rather than decorative.
        assert word not in entries[TOOL_NAME]


def test_neither_lane_makes_the_choice_for_the_model() -> None:
    """No description ranks itself, forbids the other, or names it.

    A catalog that said "use this one for amounts" would answer the demo question
    in the prompt instead of letting the agent answer it, and would stop being an
    answer the run can be audited for. The line held here is that every added word
    is a fact about the tool's own return value.
    """
    entries = (_search_spec(), *_verified_specs())
    names = {spec.name for spec in entries}
    for spec in entries:
        lowered = spec.description.lower()
        for other in names - {spec.name}:
            assert other.lower() not in lowered
        for directive in ("prefer", "always", "never use", "you should", "first call"):
            assert directive not in lowered
