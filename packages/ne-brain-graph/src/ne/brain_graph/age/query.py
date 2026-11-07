# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``TrimmedCypherBuilder`` — the single place traversal cypher is generated.

C7 normative properties (tier2-spec §1.4, §3.4):

* Every builder entry point STRUCTURALLY requires ``acl_labels`` — there is no
  entry point that omits the ACL filter.
* The ACL predicate is emitted for EVERY node AND relationship variable of the
  generated cypher (a path through an invisible node/edge is untraversable even
  between visible endpoints).
* Label values travel ONLY via the ``$acl_labels`` parameter — never interpolated
  into the cypher text (injection surface). ``entity_class``/``edge_kind`` are
  cypher IDENTIFIERS (not parameterizable) and are therefore allowlist-validated
  against the generated ``ENTITY_CLASSES``/``EDGE_KINDS`` — an unknown one raises.
* Variable-length traversal is a UNION of FIXED-length named-variable patterns
  (``r1..rk``, ``m1..mk``) — never an unbounded ``[*]``.
* ``LIMIT`` is a bounds-validated inlined int.

The three predicate styles are the R1 fail-closed ladder, selected by a runtime
probe in :meth:`ne.brain_graph.age.store.AgeGraphStore.ensure_graph`:

* ``ANY`` — ``any(l IN x.acl WHERE l IN $acl_labels)`` (primary);
* ``SIZE_COMPREHENSION`` — ``size([l IN x.acl WHERE l IN $acl_labels]) > 0``;
* ``POST_FILTER`` — the predicate is OMITTED from the text and the shape flags
  adapter-side complete-path ACL filtering (rung c, always-correct fail-closed).

The canonical templates live here; ``ne.brain_graph.testing.memory`` recovers the
:class:`QueryShape` from generated text via :func:`describe_query` so the
in-memory reference store executes ONLY builder-shaped cypher (fail-closed on
anything else).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from ne.schema import EDGE_KINDS, ENTITY_CLASSES

__all__ = [
    "ACL_PARAM",
    "CompiledQuery",
    "PredicateStyle",
    "QueryShape",
    "TrimmedCypherBuilder",
    "describe_query",
    "discovery_order_key",
]

#: The one parameter name the ACL label set ever travels under.
ACL_PARAM = "acl_labels"

Direction = Literal["out", "in", "both"]
QueryKind = Literal["lookup", "discover", "neighbors", "subgraph", "paths"]

_HARD_HOP_CEILING = 6
_MAX_LIMIT = 100_000


def discovery_order_key(values: tuple[str, str, str]) -> tuple[str, str, str]:
    """Normalize the discovery key exactly like the generated AGE query."""
    return (values[0].lower(), values[1].lower(), values[2].lower())


#: Edge kinds valid for traversal that are NOT generated from the LinkML schema:
#: ``SAME_AS`` is written by the review-gated ER merge (source_system 'ne:er').
_EXTRA_EDGE_KINDS = frozenset({"SAME_AS"})
_ALLOWED_EDGE_KINDS = EDGE_KINDS | _EXTRA_EDGE_KINDS


class PredicateStyle(StrEnum):
    ANY = "any"
    SIZE_COMPREHENSION = "size_comprehension"
    POST_FILTER = "post_filter"


@dataclass(frozen=True)
class QueryShape:
    """Machine-readable description of a compiled query (for adapters/tests)."""

    kind: QueryKind
    predicate_style: PredicateStyle
    hops: int = 0
    direction: Direction = "both"
    edge_kinds: tuple[str, ...] = ()
    entity_class: str | None = None
    has_keys: bool = False
    has_name: bool = False
    has_source_refs: bool = False
    has_query: bool = False
    has_source_system: bool = False
    has_after: bool = False
    limit: int = 0


@dataclass(frozen=True)
class CompiledQuery:
    text: str
    params: dict[str, Any] = field(default_factory=dict)
    shape: QueryShape = field(default_factory=lambda: QueryShape("lookup", PredicateStyle.ANY))


def _validate_entity_class(entity_class: str | None) -> str | None:
    if entity_class is not None and entity_class not in ENTITY_CLASSES:
        raise ValueError(f"unknown entity_class {entity_class!r} (not in ENTITY_CLASSES)")
    return entity_class


def _validate_edge_kinds(edge_kinds: Sequence[str] | None) -> tuple[str, ...]:
    if not edge_kinds:
        return ()
    kinds = tuple(edge_kinds)
    unknown = [k for k in kinds if k not in _ALLOWED_EDGE_KINDS]
    if unknown:
        raise ValueError(f"unknown edge_kind(s) {unknown!r} (not in EDGE_KINDS | {{SAME_AS}})")
    return kinds


def _validate_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(f"limit must be an int, got {limit!r}")
    if limit < 1 or limit > _MAX_LIMIT:
        raise ValueError(f"limit {limit} out of bounds [1, {_MAX_LIMIT}]")
    return limit


def _clamp_hops(hops: int, max_hops: int) -> int:
    if not isinstance(hops, int) or isinstance(hops, bool):
        raise ValueError(f"hops must be an int, got {hops!r}")
    ceiling = min(max_hops, _HARD_HOP_CEILING)
    if hops < 1:
        raise ValueError("hops must be >= 1")
    return min(hops, ceiling)


def _predicate(var: str, style: PredicateStyle) -> str | None:
    if style is PredicateStyle.ANY:
        return f"any(l IN {var}.acl WHERE l IN ${ACL_PARAM})"
    if style is PredicateStyle.SIZE_COMPREHENSION:
        return f"size([l IN {var}.acl WHERE l IN ${ACL_PARAM}]) > 0"
    return None


def _arrow(direction: Direction, rel: str) -> tuple[str, str]:
    """Return the (left, right) connector text around a relationship pattern."""
    if direction == "out":
        return f"-[{rel}]->", ""
    if direction == "in":
        return f"<-[{rel}]-", ""
    return f"-[{rel}]-", ""


def _rel_pattern(index: int, edge_kinds: tuple[str, ...], direction: Direction) -> str:
    label = ""
    if edge_kinds:
        label = ":" + "|".join(edge_kinds)
    left, _ = _arrow(direction, f"r{index}{label}")
    return left


class TrimmedCypherBuilder:
    """Generate C7-trimmed cypher. Every entry point requires ``acl_labels``."""

    def __init__(self, style: PredicateStyle = PredicateStyle.ANY) -> None:
        self._style = style

    @property
    def style(self) -> PredicateStyle:
        return self._style

    def _preds(self, variables: Sequence[str]) -> list[str]:
        out: list[str] = []
        for var in variables:
            pred = _predicate(var, self._style)
            if pred is not None:
                out.append(pred)
        return out

    def build_lookup(
        self,
        acl_labels: Sequence[str],
        *,
        keys: Sequence[str] | None = None,
        entity_class: str | None = None,
        name_query: str | None = None,
        source_refs: Sequence[str] | None = None,
        limit: int = 50,
    ) -> CompiledQuery:
        entity_class = _validate_entity_class(entity_class)
        limit = _validate_limit(limit)
        params: dict[str, Any] = {ACL_PARAM: list(acl_labels)}
        conds: list[str] = []
        if keys is not None:
            params["keys"] = list(keys)
            conds.append("n0.key IN $keys")
        if name_query is not None:
            params["name_prefix"] = name_query
            conds.append("n0.display_name STARTS WITH $name_prefix")
        if source_refs is not None:
            params["source_refs"] = list(source_refs)
            conds.append("n0.source_ref IN $source_refs")
        conds.extend(self._preds(["n0"]))
        label = f":{entity_class}" if entity_class else ""
        where = f"\nWHERE {' AND '.join(conds)}" if conds else ""
        text = f"MATCH (n0{label}){where}\nRETURN properties(n0) AS node\nLIMIT {limit}"
        shape = QueryShape(
            kind="lookup",
            predicate_style=self._style,
            entity_class=entity_class,
            has_keys=keys is not None,
            has_name=name_query is not None,
            has_source_refs=source_refs is not None,
            limit=limit,
        )
        return CompiledQuery(text=text, params=params, shape=shape)

    def build_discovery(
        self,
        acl_labels: Sequence[str],
        *,
        query: str | None = None,
        entity_class: str | None = None,
        source_system: str | None = None,
        after: tuple[str, str, str] | None = None,
        limit: int = 101,
    ) -> CompiledQuery:
        """Build the ordered entity-discovery scan used by the Graph entry rail.

        ``assurance`` is deliberately absent: it is a derived read-model value,
        not a trusted stored property, and is filtered only after
        :func:`ne.brain_graph.read._assurance_of` has classified the node.
        """
        entity_class = _validate_entity_class(entity_class)
        limit = _validate_limit(limit)
        params: dict[str, Any] = {ACL_PARAM: list(acl_labels)}
        conds: list[str] = []
        if query is not None:
            params["query"] = query
            conds.append("(n0.key = $query OR toLower(n0.display_name) CONTAINS toLower($query))")
        if source_system is not None:
            params["source_system"] = source_system
            conds.append("n0.source_system = $source_system")
        if after is not None:
            params.update(
                {
                    "after_display_name": after[0],
                    "after_entity_class": after[1],
                    "after_key": after[2],
                }
            )
            conds.append(
                "(toLower(n0.display_name) > toLower($after_display_name) OR "
                "(toLower(n0.display_name) = toLower($after_display_name) AND "
                "toLower(n0.entity_class) > toLower($after_entity_class)) OR "
                "(toLower(n0.display_name) = toLower($after_display_name) AND "
                "toLower(n0.entity_class) = toLower($after_entity_class) AND "
                "toLower(n0.key) > toLower($after_key)))"
            )
        conds.extend(self._preds(["n0"]))
        label = f":{entity_class}" if entity_class else ""
        where = f"\nWHERE {' AND '.join(conds)}" if conds else ""
        text = (
            f"MATCH (n0{label}){where}\n"
            "RETURN properties(n0) AS node\n"
            "ORDER BY toLower(n0.display_name) ASC, toLower(n0.entity_class) ASC, "
            "toLower(n0.key) ASC\n"
            f"LIMIT {limit}"
        )
        return CompiledQuery(
            text=text,
            params=params,
            shape=QueryShape(
                kind="discover",
                predicate_style=self._style,
                entity_class=entity_class,
                has_query=query is not None,
                has_source_system=source_system is not None,
                has_after=after is not None,
                limit=limit,
            ),
        )

    def _traverse_arms(
        self,
        *,
        anchor_cond: str,
        hops: int,
        edge_kinds: tuple[str, ...],
        direction: Direction,
        tail_cond: str | None,
    ) -> str:
        arms: list[str] = []
        for k in range(1, hops + 1):
            node_vars = ["a0", *[f"m{i}" for i in range(1, k + 1)]]
            rel_vars = [f"r{i}" for i in range(1, k + 1)]
            pattern = "(a0)"
            for i in range(1, k + 1):
                pattern += _rel_pattern(i, edge_kinds, direction) + f"(m{i})"
            conds = [anchor_cond]
            if tail_cond is not None:
                conds.append(tail_cond.replace("{tail}", f"m{k}"))
            conds.extend(self._preds([*node_vars, *rel_vars]))
            ret_nodes = ", ".join(f"properties({v})" for v in node_vars)
            ret_rels = ", ".join(f"properties({v})" for v in rel_vars)
            arm = (
                f"MATCH {pattern}\nWHERE {' AND '.join(conds)}\n"
                f"RETURN [{ret_nodes}] AS path_nodes, [{ret_rels}] AS path_rels"
            )
            arms.append(arm)
        return "\nUNION\n".join(arms)

    def build_neighbors(
        self,
        acl_labels: Sequence[str],
        keys: Sequence[str],
        *,
        hops: int = 1,
        edge_kinds: Sequence[str] | None = None,
        direction: Direction = "both",
        limit: int = 200,
        max_hops: int = 3,
    ) -> CompiledQuery:
        return self._build_traverse(
            "neighbors", acl_labels, keys, hops, edge_kinds, direction, limit, max_hops
        )

    def build_subgraph(
        self,
        acl_labels: Sequence[str],
        keys: Sequence[str],
        *,
        hops: int = 2,
        edge_kinds: Sequence[str] | None = None,
        limit: int = 2000,
        max_hops: int = 3,
    ) -> CompiledQuery:
        return self._build_traverse(
            "subgraph", acl_labels, keys, hops, edge_kinds, "both", limit, max_hops
        )

    def _build_traverse(
        self,
        kind: QueryKind,
        acl_labels: Sequence[str],
        keys: Sequence[str],
        hops: int,
        edge_kinds: Sequence[str] | None,
        direction: Direction,
        limit: int,
        max_hops: int,
    ) -> CompiledQuery:
        hops = _clamp_hops(hops, max_hops)
        kinds = _validate_edge_kinds(edge_kinds)
        limit = _validate_limit(limit)
        params: dict[str, Any] = {ACL_PARAM: list(acl_labels), "keys": list(keys)}
        body = self._traverse_arms(
            anchor_cond="a0.key IN $keys",
            hops=hops,
            edge_kinds=kinds,
            direction=direction,
            tail_cond=None,
        )
        text = f"{body}\nLIMIT {limit}"
        shape = QueryShape(
            kind=kind,
            predicate_style=self._style,
            hops=hops,
            direction=direction,
            edge_kinds=kinds,
            has_keys=True,
            limit=limit,
        )
        return CompiledQuery(text=text, params=params, shape=shape)

    def build_paths(
        self,
        acl_labels: Sequence[str],
        from_key: str,
        to_key: str,
        *,
        max_hops: int = 4,
        limit: int = 20,
        hop_ceiling: int = 6,
    ) -> CompiledQuery:
        """Compile a bounded path query between two endpoint SELECTORS.

        NE-GRAPH-ANCHOR: each endpoint matches a node by its internal UUID
        ``key``, by its source-record ``id`` (the identifier a caller actually
        holds, e.g. ``mer-loan-001``), or by its ``entity_class`` (e.g.
        ``Customer`` — "the nearest node of this class"). Callers hold record
        ids and class names, never the store's UUIDv5 node keys, so a key-only
        anchor made the tool unusable from served evidence. Selection only:
        every ACL/visibility/authority predicate still applies to every matched
        node and edge exactly as before.
        """
        hops = _clamp_hops(max_hops, hop_ceiling)
        limit = _validate_limit(limit)
        params: dict[str, Any] = {
            ACL_PARAM: list(acl_labels),
            "from_key": from_key,
            "to_key": to_key,
        }
        body = self._traverse_arms(
            anchor_cond="(a0.key = $from_key OR a0.id = $from_key OR a0.entity_class = $from_key)",
            hops=hops,
            edge_kinds=(),
            direction="both",
            tail_cond=(
                "({tail}.key = $to_key OR {tail}.id = $to_key OR {tail}.entity_class = $to_key)"
            ),
        )
        text = f"{body}\nLIMIT {limit}"
        shape = QueryShape(
            kind="paths",
            predicate_style=self._style,
            hops=hops,
            direction="both",
            limit=limit,
        )
        return CompiledQuery(text=text, params=params, shape=shape)


# --- Shape recovery (used ONLY by the in-memory reference store) --------------

_LOOKUP_RE = re.compile(r"^MATCH \(n0(?::(?P<label>\w+))?\)")
_REL_INDEX_RE = re.compile(r"\[r(\d+)")
_LIMIT_RE = re.compile(r"LIMIT (\d+)\s*$")
_REL_LABEL_RE = re.compile(r"\[r1(?::([\w|]+))?\]")


def _detect_direction(text: str) -> Direction:
    if "<-[r1" in text:
        return "in"
    if "-[r1" in text and "]->" in text:
        return "out"
    return "both"


def _detect_style(text: str) -> PredicateStyle:
    if "any(l IN " in text:
        return PredicateStyle.ANY
    if "size([l IN " in text:
        return PredicateStyle.SIZE_COMPREHENSION
    return PredicateStyle.POST_FILTER


def describe_query(text: str) -> QueryShape | None:
    """Recover the :class:`QueryShape` from builder-generated text, or None.

    Fail-closed: text that does not match a canonical template returns ``None``
    so the in-memory store can reject it (an unknown cypher is never executed).
    """
    style = _detect_style(text)
    limit_match = _LIMIT_RE.search(text)
    if limit_match is None:
        return None
    limit = int(limit_match.group(1))

    if text.startswith("MATCH (n0") and "-[" not in text:
        m = _LOOKUP_RE.match(text)
        if m is None:
            return None
        if "ORDER BY toLower(n0.display_name) ASC" in text:
            return QueryShape(
                kind="discover",
                predicate_style=style,
                entity_class=m.group("label"),
                has_query="n0.key = $query" in text,
                has_source_system="n0.source_system = $source_system" in text,
                has_after="$after_display_name" in text,
                limit=limit,
            )
        return QueryShape(
            kind="lookup",
            predicate_style=style,
            entity_class=m.group("label"),
            has_keys="n0.key IN $keys" in text,
            has_name="n0.display_name STARTS WITH $name_prefix" in text,
            has_source_refs="n0.source_ref IN $source_refs" in text,
            limit=limit,
        )

    rel_indices = [int(i) for i in _REL_INDEX_RE.findall(text)]
    if not rel_indices:
        return None
    hops = max(rel_indices)
    direction = _detect_direction(text)
    label_match = _REL_LABEL_RE.search(text)
    edge_kinds: tuple[str, ...] = ()
    if label_match and label_match.group(1):
        edge_kinds = tuple(label_match.group(1).split("|"))

    if "$from_key" in text and "$to_key" in text:
        kind: QueryKind = "paths"
    elif "a0.key IN $keys" in text:
        kind = "neighbors"
    else:
        return None
    return QueryShape(
        kind=kind,
        predicate_style=style,
        hops=hops,
        direction=direction,
        edge_kinds=edge_kinds,
        has_keys="$keys" in text,
        limit=limit,
    )


def acl_labels_from_params(params: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Extract the ACL label set from a cypher params mapping (empty if absent)."""
    if not params:
        return ()
    labels = params.get(ACL_PARAM)
    if not labels:
        return ()
    return tuple(labels)
