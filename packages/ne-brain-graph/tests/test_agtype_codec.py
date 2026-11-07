# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pin the agtype codec against documented AGE 1.6/1.7 output shapes."""

from __future__ import annotations

import pytest

from ne.brain_graph.age.agtype import AgtypeDecodeError, decode_agtype, vertex_properties


def test_scalars() -> None:
    assert decode_agtype("1") == 1
    assert decode_agtype("1.5") == 1.5
    assert decode_agtype('"hello"') == "hello"
    assert decode_agtype("true") is True
    assert decode_agtype("null") is None


def test_numeric_annotation_stripped() -> None:
    assert decode_agtype("100.50::numeric") == 100.5


def test_list() -> None:
    assert decode_agtype('[1, 2, "x"]') == [1, 2, "x"]


def test_map() -> None:
    assert decode_agtype('{"a": 1, "b": "two"}') == {"a": 1, "b": "two"}


def test_vertex() -> None:
    text = '{"id": 844424930131969, "label": "Person", "properties": {"key": "c1"}}::vertex'
    decoded = decode_agtype(text)
    assert decoded["label"] == "Person"
    assert vertex_properties(decoded) == {"key": "c1"}


def test_edge() -> None:
    text = (
        '{"id": 1, "label": "WORKS_AT", "start_id": 2, "end_id": 3, '
        '"properties": {"key": "e1", "edge_kind": "WORKS_AT"}}::edge'
    )
    decoded = decode_agtype(text)
    assert decoded["label"] == "WORKS_AT"
    assert vertex_properties(decoded)["edge_kind"] == "WORKS_AT"


def test_path() -> None:
    text = (
        '[{"id": 1, "label": "Account", "properties": {"key": "a1"}}::vertex, '
        '{"id": 2, "label": "WORKS_AT", "properties": {"key": "e1"}}::edge, '
        '{"id": 3, "label": "Person", "properties": {"key": "c1"}}::vertex]::path'
    )
    decoded = decode_agtype(text)
    assert isinstance(decoded, list)
    assert len(decoded) == 3
    assert decoded[0]["properties"]["key"] == "a1"


def test_unknown_syntax_raises() -> None:
    with pytest.raises(AgtypeDecodeError):
        decode_agtype("{not json}")
    with pytest.raises(AgtypeDecodeError):
        decode_agtype("")


def test_vertex_properties_strict() -> None:
    with pytest.raises(AgtypeDecodeError):
        vertex_properties({"no": "properties"})
