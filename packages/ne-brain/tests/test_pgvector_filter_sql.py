# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ALWAYS-ON: the pgvector filter-grammar -> SQL compiler (no DB required).

Proves :func:`ne.brain.pgvector.filters.compile_filters` reproduces the SAME
grammar semantics as :func:`ne.brain.qdrant._build_filter` (acl any-of, scalar/
int/bool equality, ``metadata.*`` nesting, empty-list = match-nothing, NE-087
``$any`` OR-group, ``$``-anti-smuggling, unsupported-value fail-closed), with
FULL parameterization — no user value ever appears in the SQL text.
"""

from __future__ import annotations

import json

import pytest

from ne.brain.pgvector.filters import compile_filters, validate_identifier


def _all_params(sql: str, params: tuple[str, ...]) -> None:
    """Every param is a json string; no param value leaks into the SQL text."""
    for p in params:
        assert isinstance(p, str)
        json.loads(p)  # each param is valid JSON
        assert p not in sql


def test_none_and_empty_filter_compile_to_none() -> None:
    assert compile_filters(None) is None
    assert compile_filters({}) is None


def test_acl_any_of_compiles_to_or_of_pairs_fully_parameterized() -> None:
    cond = compile_filters({"acl": ["g-eng", "u-ada"]})
    assert cond is not None
    # Two values -> two @>-pairs OR'd -> four params.
    assert cond.sql.count("@>") == 4
    assert cond.params == (
        json.dumps({"acl": "g-eng"}),
        json.dumps({"acl": ["g-eng"]}),
        json.dumps({"acl": "u-ada"}),
        json.dumps({"acl": ["u-ada"]}),
    )
    _all_params(cond.sql, cond.params)


def test_scalar_string_equality() -> None:
    cond = compile_filters({"document_id": "d1"})
    assert cond is not None
    assert cond.params == (json.dumps({"document_id": "d1"}), json.dumps({"document_id": ["d1"]}))
    _all_params(cond.sql, cond.params)


def test_scalar_int_and_bool_equality() -> None:
    cond = compile_filters({"metadata.rank": 3, "metadata.active": True})
    assert cond is not None
    assert json.dumps({"metadata": {"rank": 3}}) in cond.params
    assert json.dumps({"metadata": {"active": True}}) in cond.params
    _all_params(cond.sql, cond.params)


def test_metadata_dotted_key_nests() -> None:
    cond = compile_filters({"metadata.team": "sales"})
    assert cond is not None
    assert cond.params[0] == json.dumps({"metadata": {"team": "sales"}})


def test_empty_list_is_match_nothing_false() -> None:
    cond = compile_filters({"acl": []})
    assert cond is not None
    assert cond.sql == "FALSE"
    assert cond.params == ()


def test_top_level_multiple_keys_joined_with_and() -> None:
    cond = compile_filters({"acl": ["g-a"], "document_id": "d1"})
    assert cond is not None
    assert " AND " in cond.sql


def test_unsupported_value_type_raises() -> None:
    with pytest.raises(ValueError, match="unsupported filter value"):
        compile_filters({"metadata.score": 1.5})
    with pytest.raises(ValueError, match="unsupported filter value"):
        compile_filters({"metadata.obj": {"nested": "map"}})


# --- NE-087 $any OR-group (mirrors test_build_filter_any_group.py) ----------


def test_any_group_compiles_to_or_of_sub_ands() -> None:
    cond = compile_filters({"$any": [{"acl": ["g-a"]}, {"document_id": ["d1", "d2"]}]})
    assert cond is not None
    assert " OR " in cond.sql
    # acl(1 value)=2 params + document_id(2 values)=4 params = 6.
    assert len(cond.params) == 6
    _all_params(cond.sql, cond.params)


def test_plain_key_alongside_any_is_anded() -> None:
    cond = compile_filters(
        {"metadata.team": "sales", "$any": [{"acl": ["g-a"]}, {"document_id": ["d1"]}]}
    )
    assert cond is not None
    assert " AND " in cond.sql
    assert " OR " in cond.sql


def test_unknown_dollar_key_raises() -> None:
    with pytest.raises(ValueError, match="unknown reserved filter key"):
        compile_filters({"$foo": [{"acl": ["g-a"]}]})


def test_nested_any_inside_sub_mapping_raises() -> None:
    with pytest.raises(ValueError, match="nested reserved key"):
        compile_filters({"$any": [{"$any": [{"acl": ["g-a"]}]}]})


def test_empty_any_list_raises() -> None:
    with pytest.raises(ValueError, match="non-empty list of sub-filters"):
        compile_filters({"$any": []})


def test_non_mapping_any_element_raises() -> None:
    with pytest.raises(ValueError, match="non-empty mapping"):
        compile_filters({"$any": ["notamap"]})


def test_empty_mapping_any_element_raises() -> None:
    with pytest.raises(ValueError, match="non-empty mapping"):
        compile_filters({"$any": [{}]})


# --- identifier validation --------------------------------------------------


def test_validate_identifier_accepts_valid() -> None:
    assert validate_identifier("ne_chunks") == "ne_chunks"
    assert validate_identifier("ne_chunks_sparse") == "ne_chunks_sparse"


@pytest.mark.parametrize("bad", ["bad;drop", "Bad", "1table", "a" * 62, "has space", ""])
def test_validate_identifier_rejects_bad(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid SQL identifier"):
        validate_identifier(bad)
