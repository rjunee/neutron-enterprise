# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""A small, strict codec for Apache AGE ``agtype`` result text.

AGE returns query columns as ``agtype`` — JSON with optional ``::vertex`` /
``::edge`` / ``::path`` / ``::numeric`` type annotations appended to the JSON
value. This codec strips the known annotations and parses the remainder,
yielding plain python scalars / ``list`` / ``dict``. Vertices and edges decode to
``{"id", "label", "properties"}`` maps; a path decodes to the alternating
``[vertex, edge, vertex, ...]`` list. Unknown/instructural syntax raises
:class:`ValueError` (strict — never a silent best-effort parse).

It is test-pinned against documented AGE 1.6/1.7 output shapes
(``tests/test_agtype_codec.py``). The codec lives beside the store so the LGPL
psycopg surface stays confined to ``_pg`` while result decoding is pure-python.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["AgtypeDecodeError", "decode_agtype"]

# Known agtype type annotations appended after a JSON value (outside strings).
_ANNOTATION_RE = re.compile(r"::(?:vertex|edge|path|numeric)\b")


class AgtypeDecodeError(ValueError):
    """The agtype text was not a well-formed, recognised agtype value."""


def _strip_annotations(text: str) -> str:
    """Remove ``::vertex``/``::edge``/``::path``/``::numeric`` annotations.

    The regex only matches the reserved annotation tokens; it never rewrites the
    payload otherwise. AGE never emits these tokens inside JSON string content in
    the vertex/edge/path shapes this codec targets.
    """
    return _ANNOTATION_RE.sub("", text)


def decode_agtype(text: str) -> Any:
    """Decode one agtype result cell to plain python; raise on bad syntax."""
    if text is None:  # pragma: no cover - guarded; psycopg yields str for agtype
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
