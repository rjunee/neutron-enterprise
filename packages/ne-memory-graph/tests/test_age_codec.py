# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Always-on pins for the ``graphiti_age._codec`` agtype + datetime codec.

Ports the NE-081 ``test_agtype_codec.py`` cases (attribution: replicated because the
sidecar codec is a self-contained copy) plus the ISO-8601-UTC datetime codec pins:
naive-as-UTC, the lexicographic==chronological property (R3.iii), the TEMPORAL_PROPS
decode-back allowlist, and the embedding-extraction split.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ._sidecar_loader import load_sidecar_module

_codec = load_sidecar_module("graphiti_age/_codec.py", "_ne083c_codec")


# --- agtype decode (ported from NE-081 with attribution) ---------------------


def test_scalars() -> None:
    assert _codec.decode_agtype("1") == 1
    assert _codec.decode_agtype("1.5") == 1.5
    assert _codec.decode_agtype('"hello"') == "hello"
    assert _codec.decode_agtype("true") is True
    assert _codec.decode_agtype("null") is None


def test_numeric_annotation_stripped() -> None:
    assert _codec.decode_agtype("100.50::numeric") == 100.5


def test_list_and_map() -> None:
    assert _codec.decode_agtype('[1, 2, "x"]') == [1, 2, "x"]
    assert _codec.decode_agtype('{"a": 1, "b": "two"}') == {"a": 1, "b": "two"}


def test_vertex() -> None:
    text = '{"id": 844424930131969, "label": "Entity", "properties": {"uuid": "c1"}}::vertex'
    decoded = _codec.decode_agtype(text)
    assert decoded["label"] == "Entity"
    assert _codec.vertex_properties(decoded) == {"uuid": "c1"}


def test_edge() -> None:
    text = (
        '{"id": 1, "label": "RELATES_TO", "start_id": 2, "end_id": 3, '
        '"properties": {"uuid": "e1", "name": "WORKS_AT"}}::edge'
    )
    decoded = _codec.decode_agtype(text)
    assert decoded["label"] == "RELATES_TO"
    assert _codec.vertex_properties(decoded)["name"] == "WORKS_AT"


def test_path() -> None:
    text = (
        '[{"id": 1, "label": "Entity", "properties": {"uuid": "a1"}}::vertex, '
        '{"id": 2, "label": "RELATES_TO", "properties": {"uuid": "e1"}}::edge, '
        '{"id": 3, "label": "Entity", "properties": {"uuid": "c1"}}::vertex]::path'
    )
    decoded = _codec.decode_agtype(text)
    assert isinstance(decoded, list) and len(decoded) == 3
    assert decoded[0]["properties"]["uuid"] == "a1"


def test_annotation_token_inside_string_is_preserved() -> None:
    # A property VALUE containing a literal annotation token is user data, not an
    # annotation — stripping it would silently corrupt stored graph properties.
    text = (
        '{"id": 1, "label": "Entity", '
        '"properties": {"fact": "AGE casts use ::vertex and ::numeric tokens"}}::vertex'
    )
    decoded = _codec.decode_agtype(text)
    assert decoded["properties"]["fact"] == "AGE casts use ::vertex and ::numeric tokens"


def test_annotation_inside_string_with_escaped_quotes() -> None:
    text = '"quoted \\" then ::edge stays"::numeric'
    assert _codec.decode_agtype(text) == 'quoted " then ::edge stays'


def test_annotation_outside_strings_still_stripped_everywhere() -> None:
    # Path-style output carries multiple annotations outside strings — all stripped.
    text = '[{"id": 1, "label": "Entity", "properties": {"k": "::path in data"}}::vertex]::path'
    decoded = _codec.decode_agtype(text)
    assert decoded[0]["properties"]["k"] == "::path in data"


def test_unknown_syntax_raises() -> None:
    with pytest.raises(_codec.AgtypeDecodeError):
        _codec.decode_agtype("{not json}")
    with pytest.raises(_codec.AgtypeDecodeError):
        _codec.decode_agtype("")


def test_vertex_properties_strict() -> None:
    with pytest.raises(_codec.AgtypeDecodeError):
        _codec.vertex_properties({"no": "properties"})


# --- datetime codec ----------------------------------------------------------


def test_encode_dt_is_utc_microseconds() -> None:
    dt = datetime(2026, 7, 9, 12, 30, 0, 123456, tzinfo=UTC)
    assert _codec.encode_dt(dt) == "2026-07-09T12:30:00.123456+00:00"


def test_encode_dt_naive_treated_as_utc() -> None:
    naive = datetime(2026, 7, 9, 12, 0, 0)
    aware = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
    assert _codec.encode_dt(naive) == _codec.encode_dt(aware)


def test_encode_dt_normalizes_offset_to_utc() -> None:
    plus2 = datetime(2026, 7, 9, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert _codec.encode_dt(plus2) == "2026-07-09T12:00:00.000000+00:00"


def test_roundtrip() -> None:
    dt = datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC)
    assert _codec.parse_dt(_codec.encode_dt(dt)) == dt


def test_lexicographic_equals_chronological_property() -> None:
    rng = random.Random(83)
    base = datetime(2020, 1, 1, tzinfo=UTC)
    dts = [base + timedelta(seconds=rng.randint(0, 5 * 365 * 24 * 3600)) for _ in range(200)]
    by_time = sorted(dts)
    by_string = sorted(dts, key=_codec.encode_dt)
    assert [_codec.encode_dt(d) for d in by_time] == [_codec.encode_dt(d) for d in by_string]


def test_temporal_props_allowlist_exact_set() -> None:
    assert (
        frozenset({"valid_at", "invalid_at", "created_at", "expired_at"}) == _codec.TEMPORAL_PROPS
    )


def test_decode_temporal_props_parses_only_allowlist() -> None:
    props = {
        "valid_at": "2026-07-09T00:00:00.000000+00:00",
        "name": "not-a-date",
        "count": 3,
        "expired_at": None,
    }
    decoded = _codec.decode_temporal_props(props)
    assert isinstance(decoded["valid_at"], datetime)
    assert decoded["name"] == "not-a-date"
    assert decoded["count"] == 3
    assert decoded["expired_at"] is None


# --- sanitize_params ---------------------------------------------------------


def test_sanitize_extracts_embeddings_and_encodes_datetimes() -> None:
    params = {
        "uuid": "n1",
        "created_at": datetime(2026, 7, 9, tzinfo=UTC),
        "name_embedding": [0.1, 0.2, 0.3],
        "nested": {"valid_at": datetime(2026, 7, 10, tzinfo=UTC)},
    }
    clean, embeddings = _codec.sanitize_params(params)
    assert "name_embedding" not in clean
    assert embeddings == {"name_embedding": [0.1, 0.2, 0.3]}
    assert clean["created_at"] == "2026-07-09T00:00:00.000000+00:00"
    assert clean["nested"]["valid_at"] == "2026-07-10T00:00:00.000000+00:00"
    assert clean["uuid"] == "n1"


def test_sanitize_leaves_non_embedding_lists() -> None:
    clean, embeddings = _codec.sanitize_params({"group_ids": ["a", "b"]})
    assert clean == {"group_ids": ["a", "b"]}
    assert embeddings == {}
