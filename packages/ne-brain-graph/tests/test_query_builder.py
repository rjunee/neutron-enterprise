# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ACL predicate lands on EVERY node + edge variable of every query shape.

Labels are parameterized (never interpolated), identifiers are allowlist-
validated, hops are bounded (never ``[*``), and both the ANY and
SIZE_COMPREHENSION renderings are pinned.
"""

from __future__ import annotations

import re

import pytest

from ne.brain_graph.age.query import (
    ACL_PARAM,
    PredicateStyle,
    TrimmedCypherBuilder,
    describe_query,
)

_LABELS = ["group-a", "u-1"]


def _node_vars(text: str) -> set[str]:
    return set(re.findall(r"\b([am]\d|n0)\b", text)) - {""}


def _edge_vars(text: str) -> set[str]:
    return set(re.findall(r"\b(r\d)\b", text))


def _predicate_vars(text: str) -> list[str]:
    # every predicate references its variable as `<var>.acl`
    return re.findall(r"\b([a-z]\d|n0)\.acl", text)


@pytest.mark.parametrize("style", [PredicateStyle.ANY, PredicateStyle.SIZE_COMPREHENSION])
def test_lookup_predicate_on_node(style: PredicateStyle) -> None:
    builder = TrimmedCypherBuilder(style)
    compiled = builder.build_lookup(_LABELS, keys=["k1"], entity_class="Account")
    assert "n0.acl" in compiled.text
    assert compiled.params[ACL_PARAM] == _LABELS
    # No raw label ever appears in the text.
    for label in _LABELS:
        assert label not in compiled.text


@pytest.mark.parametrize("style", [PredicateStyle.ANY, PredicateStyle.SIZE_COMPREHENSION])
def test_discovery_is_parameterized_acl_trimmed_and_strictly_ordered(
    style: PredicateStyle,
) -> None:
    compiled = TrimmedCypherBuilder(style).build_discovery(
        _LABELS,
        query="Acme' OR true",
        entity_class="Account",
        source_system="salesforce",
        after=("Acme", "Account", "a-1"),
        limit=26,
    )
    assert "MATCH (n0:Account)" in compiled.text
    assert "n0.key = $query" in compiled.text
    assert "toLower(n0.display_name) CONTAINS toLower($query)" in compiled.text
    assert "n0.source_system = $source_system" in compiled.text
    assert "toLower(n0.key) > toLower($after_key)" in compiled.text
    assert (
        "ORDER BY toLower(n0.display_name) ASC, toLower(n0.entity_class) ASC, toLower(n0.key) ASC"
    ) in compiled.text
    assert "assurance" not in compiled.text
    assert "Acme' OR true" not in compiled.text
    assert compiled.params["query"] == "Acme' OR true"
    assert compiled.params[ACL_PARAM] == _LABELS
    assert compiled.shape.kind == "discover"


def test_discovery_rejects_unknown_class_and_recovers_exact_shape() -> None:
    builder = TrimmedCypherBuilder()
    with pytest.raises(ValueError, match="unknown entity_class"):
        builder.build_discovery(_LABELS, entity_class="NotAClass")
    compiled = builder.build_discovery(_LABELS, query="acme", source_system="crm", limit=7)
    recovered = describe_query(compiled.text)
    assert recovered == compiled.shape


@pytest.mark.parametrize("style", [PredicateStyle.ANY, PredicateStyle.SIZE_COMPREHENSION])
@pytest.mark.parametrize("hops", [1, 2, 3])
def test_neighbors_predicate_on_every_variable(style: PredicateStyle, hops: int) -> None:
    builder = TrimmedCypherBuilder(style)
    compiled = builder.build_neighbors(_LABELS, ["k1"], hops=hops, max_hops=3)
    text = compiled.text
    # Split into UNION arms; the LONGEST arm has all node/edge vars.
    for var in {*_node_vars(text), *_edge_vars(text)}:
        assert f"{var}.acl" in text, f"missing ACL predicate for {var}"
    assert "[*" not in text
    for label in _LABELS:
        assert label not in text
    assert compiled.params[ACL_PARAM] == _LABELS


@pytest.mark.parametrize("hops", [1, 2, 3])
def test_paths_predicate_on_every_variable(hops: int) -> None:
    builder = TrimmedCypherBuilder(PredicateStyle.ANY)
    compiled = builder.build_paths(_LABELS, "a", "b", max_hops=hops, hop_ceiling=3)
    text = compiled.text
    for var in {*_node_vars(text), *_edge_vars(text)}:
        assert f"{var}.acl" in text
    assert "[*" not in text


def test_any_rendering_pinned() -> None:
    builder = TrimmedCypherBuilder(PredicateStyle.ANY)
    text = builder.build_lookup(_LABELS, keys=["k"]).text
    assert f"any(l IN n0.acl WHERE l IN ${ACL_PARAM})" in text


def test_size_comprehension_rendering_pinned() -> None:
    builder = TrimmedCypherBuilder(PredicateStyle.SIZE_COMPREHENSION)
    text = builder.build_lookup(_LABELS, keys=["k"]).text
    assert f"size([l IN n0.acl WHERE l IN ${ACL_PARAM}]) > 0" in text


def test_post_filter_omits_predicate_but_flags_shape() -> None:
    builder = TrimmedCypherBuilder(PredicateStyle.POST_FILTER)
    compiled = builder.build_neighbors(_LABELS, ["k"], hops=2, max_hops=3)
    assert "any(" not in compiled.text
    assert "size([" not in compiled.text
    assert compiled.shape.predicate_style is PredicateStyle.POST_FILTER


def test_unknown_entity_class_raises() -> None:
    builder = TrimmedCypherBuilder()
    with pytest.raises(ValueError, match="unknown entity_class"):
        builder.build_lookup(_LABELS, entity_class="NotAClass")


def test_unknown_edge_kind_raises() -> None:
    builder = TrimmedCypherBuilder()
    with pytest.raises(ValueError, match="unknown edge_kind"):
        builder.build_neighbors(_LABELS, ["k"], edge_kinds=["NOPE"])


def test_hop_cap_enforced() -> None:
    builder = TrimmedCypherBuilder()
    compiled = builder.build_neighbors(_LABELS, ["k"], hops=99, max_hops=3)
    assert compiled.shape.hops == 3


def test_limit_bounds_validated() -> None:
    builder = TrimmedCypherBuilder()
    with pytest.raises(ValueError):
        builder.build_lookup(_LABELS, limit=0)
    with pytest.raises(ValueError):
        builder.build_lookup(_LABELS, limit=10**9)


def test_shape_recovery_roundtrips() -> None:
    builder = TrimmedCypherBuilder(PredicateStyle.ANY)
    # subgraph and neighbors are textually identical traversals (the in-memory
    # store executes them the same way), so recovery normalises both to a keyed
    # traverse; lookup/paths recover exactly.
    equivalence = {"subgraph": "neighbors"}
    for compiled in (
        builder.build_lookup(_LABELS, keys=["k"], entity_class="Account"),
        builder.build_neighbors(_LABELS, ["k"], hops=2, edge_kinds=["WORKS_AT"], direction="out"),
        builder.build_subgraph(_LABELS, ["k"], hops=3),
        builder.build_paths(_LABELS, "a", "b", max_hops=2),
    ):
        shape = describe_query(compiled.text)
        assert shape is not None
        expected = equivalence.get(compiled.shape.kind, compiled.shape.kind)
        assert shape.kind == expected
        assert shape.hops == compiled.shape.hops


def test_edge_kind_filter_in_pattern() -> None:
    builder = TrimmedCypherBuilder()
    compiled = builder.build_neighbors(
        _LABELS, ["k"], hops=1, edge_kinds=["WORKS_AT", "BELONGS_TO"]
    )
    assert ":WORKS_AT|BELONGS_TO]" in compiled.text
    shape = describe_query(compiled.text)
    assert shape is not None
    assert set(shape.edge_kinds) == {"WORKS_AT", "BELONGS_TO"}


def test_direction_rendering() -> None:
    builder = TrimmedCypherBuilder()
    out = builder.build_neighbors(_LABELS, ["k"], hops=1, direction="out")
    assert "]->" in out.text
    incoming = builder.build_neighbors(_LABELS, ["k"], hops=1, direction="in")
    assert "<-[" in incoming.text


def test_same_as_accepted_as_edge_kind() -> None:
    builder = TrimmedCypherBuilder(PredicateStyle.ANY)
    compiled = builder.build_neighbors(_LABELS, ["k"], hops=1, edge_kinds=["SAME_AS"])
    assert "SAME_AS" in compiled.text


def test_unknown_edge_kind_still_raises() -> None:
    builder = TrimmedCypherBuilder(PredicateStyle.ANY)
    with pytest.raises(ValueError, match="unknown edge_kind"):
        builder.build_neighbors(_LABELS, ["k"], hops=1, edge_kinds=["NOPE"])
