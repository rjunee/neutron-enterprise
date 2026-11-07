# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""agtype + datetime codec for the ``graphiti_age`` sidecar driver (PURE stdlib).

Two responsibilities, both pure-python and self-contained (no intra-package or
``ne.*`` imports — the sidecar venv cannot import ``ne-brain-graph``):

1. **agtype decode** — adapted from
   ``packages/ne-brain-graph/src/ne/brain_graph/age/agtype.py`` (NE-081). Replicated
   (rather than imported) because the sidecar venv has no ``ne.*`` on its path;
   extraction-to-a-shared-wheel is deferred per the feasibility memo §5. AGE returns
   query columns as ``agtype`` — JSON with optional ``::vertex`` / ``::edge`` /
   ``::path`` / ``::numeric`` annotations. We strip the known annotations and
   ``json.loads`` the remainder; anything else raises :class:`AgtypeDecodeError`
   (strict — never a silent best-effort parse).

2. **datetime codec chokepoint** — Graphiti stores temporal validity as datetimes.
   AGE has no native timestamp type, so temporal props travel as ISO-8601 strings.
   :func:`encode_dt` pins ``timespec='microseconds'`` and normalises to UTC so the
   *lexicographic* order of the stored string is provably equal to *chronological*
   order (the R3.iii pin — as-of/validity filters compare strings correctly).
   :func:`sanitize_params` walks a Graphiti params mapping (the Neptune
   ``_sanitize_parameters`` precedent) converting datetimes to encoded strings and
   *extracting* ``*_embedding`` keys so embedding vectors NEVER enter agtype
   (feasibility row 10 — they route to the pgvector/jsonb side table instead).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "TEMPORAL_PROPS",
    "AgtypeDecodeError",
    "decode_agtype",
    "decode_temporal_props",
    "encode_dt",
    "parse_dt",
    "sanitize_params",
    "vertex_properties",
]

# Known agtype type annotations appended after a JSON value (outside strings).
# Matched ANCHORED (re.match at a position) by the string-aware scanner below —
# an annotation token INSIDE a JSON string (user content mentioning "::vertex")
# is data, never an annotation, and must survive decoding intact.
_ANNOTATION_RE = re.compile(r"::(?:vertex|edge|path|numeric)\b")

#: Graphiti property names that carry temporal validity and must round-trip as
#: aware datetimes. Extend ONLY from pinned-source evidence, listing each addition:
#: - valid_at / invalid_at — EntityEdge temporal validity (edges.py)
#: - created_at            — every node/edge creation instant (nodes.py/edges.py)
#: - expired_at            — EntityEdge expiry (edges.py)
TEMPORAL_PROPS: frozenset[str] = frozenset({"valid_at", "invalid_at", "created_at", "expired_at"})

#: Suffix marking an embedding property that must never enter agtype.
_EMBEDDING_SUFFIX = "_embedding"


class AgtypeDecodeError(ValueError):
    """The agtype text was not a well-formed, recognised agtype value."""


def _strip_annotations(text: str) -> str:
    """Remove ``::vertex``/``::edge``/``::path``/``::numeric`` annotations.

    String-aware: AGE emits annotations only OUTSIDE JSON string literals, so the
    scanner tracks double-quote string state (honouring backslash escapes) and strips
    annotation tokens only outside strings. A property value that happens to contain
    the literal text ``::vertex`` (user content) passes through untouched — stripping
    it would silently corrupt stored graph properties on read.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ":":
            match = _ANNOTATION_RE.match(text, i)
            if match is not None:
                i = match.end()
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def decode_agtype(text: str) -> Any:
    """Decode one agtype result cell to plain python; raise on bad syntax."""
    if text is None:
        raise AgtypeDecodeError("agtype cell was None")
    stripped = _strip_annotations(text).strip()
    if not stripped:
        raise AgtypeDecodeError("empty agtype cell")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise AgtypeDecodeError(f"invalid agtype {text!r}: {exc}") from exc


def vertex_properties(value: Any) -> dict[str, Any]:
    """Return the ``properties`` map of a decoded vertex/edge, strict."""
    if not isinstance(value, dict) or "properties" not in value:
        raise AgtypeDecodeError(f"expected a vertex/edge map, got {value!r}")
    props = value["properties"]
    if not isinstance(props, dict):
        raise AgtypeDecodeError(f"vertex/edge properties were not a map: {props!r}")
    return props


# --- datetime codec ----------------------------------------------------------


def encode_dt(value: datetime) -> str:
    """Encode a datetime to a canonical, lexicographically-sortable UTC string.

    A naive datetime is ASSUMED to be UTC (Graphiti hands out aware datetimes, but
    fail-safe on naive input). ``timespec='microseconds'`` is PINNED so every stored
    string has identical width — a shorter string never sorts before a longer one of
    the same instant, so ``a < b`` (string) iff ``a`` is chronologically earlier.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse_dt(value: str) -> datetime:
    """Decode an ISO-8601 string back to an aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def decode_temporal_props(props: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``props`` with :data:`TEMPORAL_PROPS` string values parsed
    back to aware datetimes (values that are absent/None/non-string pass through)."""
    out = dict(props)
    for key in TEMPORAL_PROPS:
        raw = out.get(key)
        if isinstance(raw, str) and raw:
            out[key] = parse_dt(raw)
    return out


def _encode_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return encode_dt(value)
    if isinstance(value, dict):
        return {k: _encode_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_encode_value(v) for v in value]
    if isinstance(value, tuple):
        return [_encode_value(v) for v in value]
    return value


def sanitize_params(
    params: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[float]]]:
    """Split a Graphiti params mapping into agtype-safe params + extracted embeddings.

    Returns ``(clean_params, embeddings)`` where:

    * ``clean_params`` has every datetime (at any depth) converted via
      :func:`encode_dt` and every top-level ``*_embedding`` key REMOVED.
    * ``embeddings`` maps each removed ``*_embedding`` key to its vector — the driver
      routes these to the vector side table, keyed by node/edge identity. Embeddings
      never enter agtype (feasibility row 10: a 1536-float list bloats the graph store
      and AGE cannot index it).

    Only *top-level* ``*_embedding`` keys are extracted — Graphiti passes embeddings
    as first-class query parameters (``name_embedding``, ``fact_embedding``), never
    nested inside other structures.
    """
    embeddings: dict[str, list[float]] = {}
    clean: dict[str, Any] = {}
    for key, value in params.items():
        if key.endswith(_EMBEDDING_SUFFIX) and isinstance(value, list):
            embeddings[key] = [float(x) for x in value]
            continue
        clean[key] = _encode_value(value)
    return clean, embeddings
