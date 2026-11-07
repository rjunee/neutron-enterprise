# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Filter-grammar → parameterized SQL compiler for the pgvector store (NE-091).

PURE module: no ``psycopg`` import, unit-testable without a database. Translates
the frozen ChunkStore filter grammar (the SAME grammar
:func:`ne.brain.qdrant._build_filter` compiles to a Qdrant ``Filter``) into a
fail-closed, FULLY PARAMETERIZED JSONB-containment ``WHERE`` fragment, so the
pgvector store applies the IDENTICAL ACL trim as Qdrant (C7 — never over-return).

Grammar (mirrors ``ne.brain.qdrant`` semantics field-for-field):

* keys are used VERBATIM as payload field paths — a dot means nesting
  (``"metadata.team"`` -> ``{"metadata": {"team": v}}``);
* one field-condition compiles to ``(payload @> %s::jsonb OR payload @> %s::jsonb)``
  with params ``json.dumps(nest(key, v))`` and ``json.dumps(nest(key, [v]))`` —
  the pair reproduces Qdrant ``MatchValue`` on BOTH a scalar field (equality) and
  an array field (contains), because ``jsonb @>`` treats a top-level array as
  containing its scalar elements;
* a ``list``/``tuple`` value is "any of" (Qdrant ``MatchAny``): an OR over the
  per-value pairs. An EMPTY list compiles to literal ``FALSE`` (match-nothing —
  the ``and_combine_filters`` ``[]`` contract);
* only ``str``/``int``/``bool`` scalars are supported; anything else raises
  ``ValueError`` (same as ``_field_condition``);
* :data:`ne.brain.trimming.ACL_ANY_KEY` (``"$any"``) compiles to an OR of
  sub-group ANDs, with the SAME shape validation + fail-closed ``ValueError``
  messages as ``_build_any_group``. Any OTHER ``$``-prefixed key raises
  ``ValueError`` (anti-smuggling, fail closed).

NO raw user value is EVER concatenated into the SQL text: every value travels as a
``json.dumps`` string bound through a ``%s`` placeholder. Table identifiers (which
come from config, never a caller) are validated by :func:`validate_identifier`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ne.brain.trimming import ACL_ANY_KEY

__all__ = ["SqlCondition", "compile_filters", "validate_identifier"]

_PAYLOAD_COLUMN = "payload"
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,60}$")


@dataclass(frozen=True)
class SqlCondition:
    """A parameterized SQL boolean fragment: ``sql`` + positional ``params``.

    ``params`` are ALWAYS ``json.dumps`` strings — no raw user value is ever
    interpolated into ``sql`` (every value binds through a ``%s`` placeholder).
    """

    sql: str
    params: tuple[str, ...]


def validate_identifier(name: str) -> str:
    """Return ``name`` if it is a safe SQL identifier, else raise ``ValueError``.

    Table names are config-derived (never caller input); this is fail-loud
    defense in depth so an identifier can be composed into DDL/DML without any
    quoting games. Enforces ``^[a-z_][a-z0-9_]{0,60}$``.
    """
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"invalid SQL identifier {name!r}: must match {_IDENTIFIER_RE.pattern}")
    return name


def _nest(key: str, value: Any) -> dict[str, Any]:
    """Turn a dotted field path + value into a nested dict (``a.b`` -> ``{a:{b:v}}``)."""
    doc: Any = value
    for part in reversed(key.split(".")):
        doc = {part: doc}
    return doc


def _scalar_or_raise(key: str, value: Any) -> None:
    # ``bool`` is a subclass of ``int``; both (and ``str``) are accepted — exactly
    # the ``ne.brain.qdrant._field_condition`` allow-list.
    if not isinstance(value, str | int | bool):
        raise ValueError(f"unsupported filter value for {key!r}: {value!r}")


def _pair(key: str, value: Any) -> tuple[str, tuple[str, str]]:
    """One scalar ``key -> value`` -> the ``(@> OR @>)`` clause + its two params."""
    _scalar_or_raise(key, value)
    clause = f"({_PAYLOAD_COLUMN} @> %s::jsonb OR {_PAYLOAD_COLUMN} @> %s::jsonb)"
    return clause, (json.dumps(_nest(key, value)), json.dumps(_nest(key, [value])))


def _field_condition(key: str, value: Any) -> SqlCondition:
    """Compile ONE ``key -> value`` pair (scalar equality or list any-of)."""
    if isinstance(value, list | tuple):
        if len(value) == 0:
            # Match-nothing: Qdrant ``MatchAny(any=[])`` matches zero points.
            return SqlCondition(sql="FALSE", params=())
        clauses: list[str] = []
        params: list[str] = []
        for item in value:
            clause, pair = _pair(key, item)
            clauses.append(clause)
            params.extend(pair)
        return SqlCondition(sql="(" + " OR ".join(clauses) + ")", params=tuple(params))
    clause, pair = _pair(key, value)
    return SqlCondition(sql=clause, params=pair)


def _build_any_group(key: str, value: Any) -> SqlCondition:
    """Compile the NE-087 :data:`ACL_ANY_KEY` OR-group (fail-closed shape checks).

    ``value`` MUST be a non-empty list/tuple of non-empty mappings, none of whose
    keys is ``$``-prefixed (no nested ``$any``). Each sub-mapping compiles to an
    AND of field-conditions; the group is their OR — mirroring
    ``ne.brain.qdrant._build_any_group`` semantics and error messages exactly.
    """
    if not isinstance(value, list | tuple) or not value:
        raise ValueError(f"{key!r} must be a non-empty list of sub-filters, got {value!r}")
    sub_sqls: list[str] = []
    params: list[str] = []
    for sub in value:
        if not isinstance(sub, Mapping) or not sub:
            raise ValueError(f"{key!r} sub-filter must be a non-empty mapping, got {sub!r}")
        field_sqls: list[str] = []
        for sub_key, sub_value in sub.items():
            if isinstance(sub_key, str) and sub_key.startswith("$"):
                raise ValueError(
                    f"{key!r} sub-filter may not contain a nested reserved key {sub_key!r}"
                )
            cond = _field_condition(sub_key, sub_value)
            field_sqls.append(cond.sql)
            params.extend(cond.params)
        sub_sqls.append("(" + " AND ".join(field_sqls) + ")")
    return SqlCondition(sql="(" + " OR ".join(sub_sqls) + ")", params=tuple(params))


def compile_filters(filters: Mapping[str, Any] | None) -> SqlCondition | None:
    """Translate the ChunkStore filter grammar into a parameterized ``WHERE`` fragment.

    Returns ``None`` for an empty/``None`` filter (no restriction). Top-level
    conditions are joined with ``AND``. :data:`ACL_ANY_KEY` is handled FIRST (a
    nested OR-group); any OTHER ``$``-prefixed key raises ``ValueError`` (an
    unknown reserved token fails closed — the anti-smuggling discipline).
    """
    if not filters:
        return None
    conditions: list[SqlCondition] = []
    for key, value in filters.items():
        if key == ACL_ANY_KEY:
            conditions.append(_build_any_group(key, value))
        elif isinstance(key, str) and key.startswith("$"):
            raise ValueError(
                f"unknown reserved filter key {key!r}: only {ACL_ANY_KEY!r} is a "
                "recognised '$'-prefixed filter grammar token"
            )
        else:
            conditions.append(_field_condition(key, value))
    sql = " AND ".join(c.sql for c in conditions)
    params = tuple(p for c in conditions for p in c.params)
    return SqlCondition(sql=sql, params=params)
