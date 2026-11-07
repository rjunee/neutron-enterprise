# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""openCypher-over-AGE dialect helpers for ``graphiti_age`` (PURE stdlib).

Self-contained (no ``ne.*`` / psycopg / graphiti_core imports). Holds the pieces the
driver needs to bridge Graphiti's Neo4j-dialect cypher and the four feasibility shims
onto Apache AGE:

* :func:`validate_label` — a regex identifier guard for the dynamic entity labels
  Graphiti interpolates (the FalkorDB-branch posture: guard, not a fixed allowlist).
* :class:`PredicateStyle` + :data:`PREDICATE_CANARIES` — the R1 fail-closed predicate
  ladder (ANY → SIZE_COMPREHENSION → POST_FILTER), probed at runtime and APPLIED in the
  query path by :func:`apply_predicate_style` (ANY passthrough / SIZE-comprehension
  rewrite / predicate-stripped POST_FILTER with an in-driver
  :func:`passes_label_post_filter`).
* :func:`bounded_vle` / :func:`union_arms` — variable-length traversal as a bounded
  ``*1..n`` (n clamped to :data:`HOP_CEILING`) OR a UNION of fixed-length arms; NEVER
  an unbounded ``[*]``.
* :class:`TraversalStyle` + :data:`TRAVERSAL_CANARY` — the R1 fail-closed traversal
  ladder (DIRECT_VLE → UNION_ARMS), probed at runtime and APPLIED in the query path by
  :func:`apply_traversal_style` (clamped direct VLE, or the bounded union-of-fixed-length
  arms rewrite for the two Graphiti BFS shapes).
* :func:`has_vle_segment` / :func:`pop_trailing_limit` — degraded-rung LIMIT correctness:
  the driver pops a trailing ``LIMIT`` off the cypher on the POST_FILTER / UNION_ARMS
  rungs and enforces it in-driver AFTER the label filter / union (Neo4j filter-then-limit
  parity — never an under-count from limiting the unfiltered set, never ``arms x limit``);
  a non-trailing ``LIMIT`` / any ``SKIP`` fails loud (NE-083c-FU2).
* fulltext shim — ``{graph}_ft`` tsvector table DDL + upsert + ``websearch_to_tsquery``
  search SQL (ts_rank, not BM25 — acceptable under RRF rank fusion, feasibility row 9).
* vector shim — ``{graph}_vec`` table DDL (undimensioned pgvector ``vector`` if the
  extension is present, else ``jsonb`` + client-side cosine — the Neptune-blessed
  fail-closed rung). Created up front at build-indices time (so a fresh-graph search
  returns empty, not ``UndefinedTable``), with ``source_uuid`` / ``target_uuid``
  endpoint columns so edge-similarity searches filter on the node pair IN SQL.
* :func:`rewrite_cypher` — residual Neo4j→AGE text rewrites; an unrecognised
  ``CALL db.*`` / unsupported construct raises :class:`AgeDialectError` (fail-LOUD).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

__all__ = [
    "HOP_CEILING",
    "PREDICATE_CANARIES",
    "TRAVERSAL_CANARY",
    "AgeDialectError",
    "LabelPostFilter",
    "PredicateStyle",
    "TraversalStyle",
    "VectorBackend",
    "apply_predicate_style",
    "apply_traversal_style",
    "bounded_vle",
    "cosine",
    "filter_edge_candidates",
    "ft_ddl",
    "ft_search_sql",
    "ft_upsert_sql",
    "has_vle_segment",
    "passes_label_post_filter",
    "plan_side_tables",
    "pop_trailing_limit",
    "return_aliases",
    "rewrite_cypher",
    "union_arms",
    "validate_graph_name",
    "validate_label",
    "vec_client_ddl",
    "vec_client_search_sql",
    "vec_pgvector_ddl",
    "vec_pgvector_search_sql",
    "vec_table_exists_sql",
]

#: Hard ceiling on variable-length traversal depth (never exceeded, even if a caller
#: asks for more — an unbounded expansion is a fail-open hazard on a shared Postgres).
HOP_CEILING = 6

_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")


class AgeDialectError(ValueError):
    """A cypher construct AGE cannot express reached translation (fail-loud)."""


def validate_label(label: str) -> str:
    """Return ``label`` iff it is a safe cypher identifier, else raise.

    Graphiti entity labels are dynamic (user entity types), so we regex-guard rather
    than allowlist (the FalkorDB-branch posture). Rejects quotes, backticks, spaces,
    dashes, semicolons, and a leading digit — anything that could break out of the
    label position in interpolated cypher.
    """
    if not _LABEL_RE.match(label):
        raise AgeDialectError(f"unsafe cypher label {label!r} (identifier guard)")
    return label


def validate_graph_name(name: str) -> str:
    """Guard a graph name before it is interpolated into a side-table identifier."""
    if not _LABEL_RE.match(name):
        raise AgeDialectError(f"unsafe graph name {name!r} (identifier guard)")
    return name


# --- R1 predicate ladder -----------------------------------------------------


class PredicateStyle(StrEnum):
    ANY = "any"
    SIZE_COMPREHENSION = "size_comprehension"
    POST_FILTER = "post_filter"


#: Probe canaries (copied in shape from NE-081 store.py:134-156). Each is run at
#: build_indices time; the first that executes selects the rung. POST_FILTER is the
#: always-correct fallback (predicate omitted, adapter filters afterward).
PREDICATE_CANARIES: dict[PredicateStyle, str] = {
    PredicateStyle.ANY: (
        "MATCH (n) WHERE any(l IN n.labels WHERE l IN $probe_labels) RETURN 1 AS probe LIMIT 1"
    ),
    PredicateStyle.SIZE_COMPREHENSION: (
        "MATCH (n) WHERE size([l IN n.labels WHERE l IN $probe_labels]) > 0 "
        "RETURN 1 AS probe LIMIT 1"
    ),
}


# --- R1 predicate ladder: query-path application (NE-083c-FOLLOWUP) -----------

# The ONE canonical Graphiti label predicate `any(l IN n.labels WHERE l IN $param)`,
# captured as (iter_var, node_var, param). The degraded rungs rewrite EXACTLY this shape;
# any other `any(… .labels …)` is a shape they cannot express (fail-loud on a degraded
# rung). Non-label `any(...)` (e.g. over `e.episodes`) never matches, so it is untouched.
_ANY_LABELS_RE = re.compile(
    r"\bany\s*\(\s*([A-Za-z_]\w*)\s+IN\s+([A-Za-z_]\w*)\.labels\s+WHERE\s+\1\s+IN\s+"
    r"\$([A-Za-z_]\w*)\s*\)",
    re.IGNORECASE,
)
# A residual (non-canonical) `any(… .labels …)` a degraded rung cannot express.
_RESIDUAL_LABEL_ANY_RE = re.compile(r"\bany\s*\([^()]*\.labels", re.IGNORECASE)

#: (record alias, param name) — one in-driver label filter the POST_FILTER rung emits.
LabelPostFilter = tuple[str, str]


def _resolve_label_alias(cypher: str, node_var: str) -> str:
    """Return the RETURN alias projecting ``node_var`` WHOLE (for the POST_FILTER rung).

    The label filter runs in-driver over each returned record, so the node the predicate
    constrained must be projected as a whole vertex — either bare (``RETURN n`` → key
    ``n``) or aliased (``RETURN n AS node`` → key ``node``). A projection of only a
    property (``RETURN n.uuid AS uuid``) leaves the driver unable to read the node's
    labels, so it raises rather than return silently-UNFILTERED rows (fail-loud).
    """
    match = _RETURN_RE.search(cypher)
    if match is not None:
        for raw in _split_top_level(match.group(1)):
            item = raw.strip()
            if item == node_var:
                return node_var
            alias = re.fullmatch(
                rf"{re.escape(node_var)}\s+AS\s+([A-Za-z_]\w*)", item, re.IGNORECASE
            )
            if alias is not None:
                return alias.group(1)
    raise AgeDialectError(
        f"POST_FILTER label predicate needs {node_var!r} projected whole in RETURN "
        "(fail-loud: never return silently-unfiltered rows)"
    )


def apply_predicate_style(text: str, style: PredicateStyle) -> tuple[str, list[LabelPostFilter]]:
    """Apply the probed predicate rung to a cypher string (NE-083c-FOLLOWUP wiring).

    * ANY → passthrough (the direct rung selected on AGE 1.6.0): returns ``text``
      unchanged with no post-filter specs.
    * SIZE_COMPREHENSION → rewrite each canonical ``any(l IN n.labels WHERE l IN $p)`` to
      ``size([l IN n.labels WHERE l IN $p]) > 0`` (an AGE build that rejects ``any()`` but
      accepts list comprehensions).
    * POST_FILTER → replace each canonical predicate with the literal ``true`` and emit a
      ``(alias, param)`` spec per match; the driver drops non-matching rows after the
      query runs (the always-correct rung for a build that rejects both forms).

    Fail-loud: on a degraded rung a residual non-canonical ``any(… .labels …)`` the rung
    cannot express raises :class:`AgeDialectError`. Non-label ``any(...)`` is untouched on
    every rung.
    """
    if style is PredicateStyle.ANY:
        return text, []
    specs: list[LabelPostFilter] = []
    if style is PredicateStyle.SIZE_COMPREHENSION:

        def _size(match: re.Match[str]) -> str:
            iv, nv, param = match.group(1), match.group(2), match.group(3)
            return f"size([{iv} IN {nv}.labels WHERE {iv} IN ${param}]) > 0"

        out = _ANY_LABELS_RE.sub(_size, text)
    else:  # POST_FILTER

        def _strip(match: re.Match[str]) -> str:
            specs.append((_resolve_label_alias(text, match.group(2)), match.group(3)))
            return "true"

        out = _ANY_LABELS_RE.sub(_strip, text)
    if _RESIDUAL_LABEL_ANY_RE.search(out):
        raise AgeDialectError(
            f"non-canonical label any() cannot be expressed by the {style} rung: {out[:120]!r}"
        )
    return out, specs


def passes_label_post_filter(
    record: Mapping[str, Any], spec: LabelPostFilter, params: Mapping[str, Any]
) -> bool:
    """True iff ``record``'s projected node shares ≥1 label with the predicate param.

    Parity with Neo4j's ``any(l IN n.labels WHERE l IN $p)``: a node whose ``labels`` is
    null/absent yields ``any(...) = null`` (falsy) and is filtered OUT — so a missing or
    malformed record / labels / param here drops the row (fail-closed == parity, never a
    silently-unfiltered result).
    """
    alias, param = spec
    node = record.get(alias)
    if not isinstance(node, dict):
        return False
    props = node.get("properties")
    if not isinstance(props, dict):
        return False
    labels = props.get("labels")
    wanted = params.get(param)
    if not isinstance(labels, list) or not isinstance(wanted, list):
        return False
    return any(label in wanted for label in labels)


# --- traversal ---------------------------------------------------------------


def _clamp_hops(hops: int) -> int:
    if not isinstance(hops, int) or isinstance(hops, bool):
        raise AgeDialectError(f"hops must be an int, got {hops!r}")
    if hops < 1:
        raise AgeDialectError("hops must be >= 1")
    return min(hops, HOP_CEILING)


def bounded_vle(hops: int) -> str:
    """Return the bounded variable-length-edge pattern ``*1..n`` (n clamped).

    NEVER emits an unbounded ``*`` — the ceiling is enforced here so no caller can
    expand the graph without bound.
    """
    return f"*1..{_clamp_hops(hops)}"


def union_arms(hops: int, *, direction: str = "out") -> str:
    """Build the fixed-arm UNION fallback for VLE when AGE rejects ``*1..n``.

    Mirrors the NE-081 query.py:211-238 ``_traverse_arms`` pattern: one MATCH arm per
    hop length ``1..n`` with named node/rel variables, UNION-ed. Fail-closed correct
    (never an unbounded expansion).
    """
    n = _clamp_hops(hops)
    if direction == "out":
        left, right = "-", "->"
    elif direction == "in":
        left, right = "<-", "-"
    else:
        left, right = "-", "-"
    arms: list[str] = []
    for k in range(1, n + 1):
        pattern = "(a0)"
        for i in range(1, k + 1):
            pattern += f"{left}[r{i}]{right}(m{i})"
        arms.append(f"MATCH {pattern} RETURN m{k} AS node")
    return "\nUNION\n".join(arms)


# --- R1 traversal ladder: query-path application (NE-083c-FOLLOWUP) -----------


class TraversalStyle(StrEnum):
    DIRECT_VLE = "direct_vle"
    UNION_ARMS = "union_arms"


#: Build-indices canary: run at build time; if AGE accepts a variable-length ``*1..2`` the
#: DIRECT_VLE rung is selected, else the fail-closed UNION_ARMS rewrite.
TRAVERSAL_CANARY = "MATCH (a)-[*1..2]->(b) RETURN 1 AS probe LIMIT 1"

# One variable-length-edge segment: (lhs-arrow)[ optional-var optional-:TypeList *
# optional-bounds ](rhs-arrow). The ``*`` is mandatory, so ONLY VLE segments match (a
# plain ``-[e:RELATES_TO {uuid: rel.uuid}]-`` never does).
_VLE_SEGMENT_RE = re.compile(
    r"(?P<lhs><-|-)\["
    r"(?P<var>[A-Za-z_]\w*)?"
    r"(?P<types>:\s*[A-Za-z_]\w*(?:\s*\|\s*[A-Za-z_]\w*)*)?"
    r"\s*\*\s*(?P<lower>\d+)?(?P<range>\.\.)?(?P<upper>\d+)?\s*"
    r"\](?P<rhs>->|-)"
)
# A path-variable assignment `MATCH path = (…)` — the discriminator for the edge-BFS shape.
_PATH_ASSIGN_RE = re.compile(r"\bMATCH\s+([A-Za-z_]\w*)\s*=\s*")

# --- degraded-rung LIMIT correctness (NE-083c-FU2) ---------------------------
# A trailing `LIMIT <param|literal>` at the very end of the cypher (the only shape the
# degraded rungs can pop and re-enforce in-driver). A non-trailing LIMIT or any SKIP
# cannot be made correct on a degraded rung, so the pop fails loud (see below).
_TRAILING_LIMIT_RE = re.compile(r"\bLIMIT\s+(\$[A-Za-z_]\w*|\d+)\s*$", re.IGNORECASE)
_LIMIT_OR_SKIP_RE = re.compile(r"\b(?:LIMIT|SKIP)\b", re.IGNORECASE)
_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)


def has_vle_segment(text: str) -> bool:
    """True iff ``text`` contains a variable-length-edge segment (``[*…]``).

    The driver-side trigger for popping a trailing LIMIT on the UNION_ARMS rung: only a
    query that actually carries a VLE segment gets expanded into arms, so only then must
    the trailing LIMIT be popped ahead of expansion (a non-VLE query on the UNION_ARMS
    rung passes straight through :func:`apply_traversal_style` and keeps its LIMIT).
    """
    return _VLE_SEGMENT_RE.search(text) is not None


def pop_trailing_limit(text: str, params: Mapping[str, Any]) -> tuple[str, int | None]:
    """Strip a trailing ``LIMIT n`` off ``text`` and return ``(text_without_limit, n)``.

    The degraded predicate/traversal rungs cannot let an in-query ``LIMIT`` reach AGE:

    * POST_FILTER strips the label predicate to ``true`` and filters IN-DRIVER after the
      query returns, so an AGE-side ``LIMIT`` would bound the UNFILTERED candidate set —
      the driver could then UNDER-return (fewer than ``n``, or zero) even though more
      matching nodes exist beyond AGE's cut (FIX-1).
    * UNION_ARMS duplicates the whole query — LIMIT included — into every hop-depth arm,
      so a ``LIMIT 10`` across 6 arms could OVER-return up to 60 rows (FIX-2).

    Both are fixed by popping the trailing LIMIT here and enforcing it in-driver AFTER the
    label filter / union, giving Neo4j's filter-then-limit parity (up to ``n`` ACTUALLY
    matching / globally-bounded rows).

    Resolution + fail-loud contract: a ``$name`` token is looked up in ``params`` (missing
    key → :class:`AgeDialectError`); a literal is ``int``-parsed. The value must be a
    non-bool ``int >= 0`` else raise. Finally, if a ``LIMIT`` or ``SKIP`` still remains
    after the pop (a NON-trailing LIMIT, or any SKIP), the shape cannot be made correct on
    a degraded rung, so raise :class:`AgeDialectError` — fail-loud, never silently wrong.
    """
    match = _TRAILING_LIMIT_RE.search(text)
    if match is not None:
        token = match.group(1)
        if token.startswith("$"):
            key = token[1:]
            if key not in params:
                raise AgeDialectError(
                    f"trailing LIMIT param ${key} missing from params (degraded-rung pop)"
                )
            value: Any = params[key]
        else:
            value = int(token)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AgeDialectError(
                f"trailing LIMIT must be a non-bool int >= 0, got {value!r} (degraded-rung pop)"
            )
        out = text[: match.start()].rstrip()
        limit: int | None = value
    else:
        out = text
        limit = None
    if _LIMIT_OR_SKIP_RE.search(out):
        raise AgeDialectError(
            "a non-trailing LIMIT or any SKIP cannot be made correct on a degraded rung "
            f"(fail-loud, never silently wrong): {out[:120]!r}"
        )
    return out, limit


def _vle_bounds(match: re.Match[str]) -> tuple[int, int, bool]:
    """Parse+clamp a VLE segment's bounds → ``(lower, upper, single)``; fail-loud unbounded.

    ``*n`` → ``(n, n, True)``; ``*a..b`` → ``(a, b, False)``; bare ``*`` / ``*a..`` (no
    upper) → :class:`AgeDialectError` (never an unbounded expansion — HOP_CEILING
    posture). Upper is clamped to :data:`HOP_CEILING`, lower to ``1 <= lower <= upper``.
    """
    lower_s = match.group("lower")
    upper_s = match.group("upper")
    has_range = match.group("range") is not None
    if not has_range:
        if lower_s is None:
            raise AgeDialectError("unbounded '*' variable-length traversal rejected (HOP_CEILING)")
        upper = lower = int(lower_s)
        single = True
    else:
        if upper_s is None:
            raise AgeDialectError(
                "unbounded-upper variable-length traversal rejected (HOP_CEILING)"
            )
        upper = int(upper_s)
        lower = int(lower_s) if lower_s is not None else 1
        single = False
    upper = min(upper, HOP_CEILING)
    lower = max(1, min(lower, upper))
    return lower, upper, single


def apply_traversal_style(text: str, style: TraversalStyle) -> str:
    r"""Apply the probed traversal rung to a cypher string (NE-083c-FOLLOWUP wiring).

    DIRECT_VLE (the rung selected on AGE 1.6.0) re-emits each VLE segment with its bounds
    clamped to :data:`HOP_CEILING` — a pure passthrough when already ``<= 6``. UNION_ARMS
    (for an AGE build that rejects ``*a..b``) expands the ONE supported VLE segment into a
    ``\nUNION\n`` of fixed-length arms, one per hop count ``lower..upper``.

    UNION_ARMS supports EXACTLY the two graphiti-core 0.29.2 NEO4J BFS shapes that can
    reach ``execute_query``: the plain node-BFS segment, and the path-bound edge-BFS shape
    (``MATCH path = (…)-[:T*a..b]->(…) … relationships(path)`` — the chain rels are named
    ``__vle_r1..__vle_rk`` and ``relationships(path)`` becomes the literal rel list). It
    requires exactly one outward (``-…->``) segment with NO bound rel var, else raises
    :class:`AgeDialectError` (fail-loud, never a silently-wrong rewrite). The driver pops a
    trailing ``LIMIT`` via :func:`pop_trailing_limit` BEFORE expansion and enforces it
    in-driver over the unioned result; a ``LIMIT``/``SKIP`` still present here would be
    duplicated per arm (``arms x limit`` over-return) and an ``ORDER BY`` cannot be
    preserved across arms, so both raise :class:`AgeDialectError` (NE-083c-FU2). A bare
    ``*`` / ``*a..`` fails loud on BOTH rungs (no unbounded traversal ever).
    """
    matches = list(_VLE_SEGMENT_RE.finditer(text))
    if not matches:
        return text
    if style is TraversalStyle.DIRECT_VLE:

        def _direct(match: re.Match[str]) -> str:
            lower, upper, single = _vle_bounds(match)
            star = f"*{upper}" if single else f"*{lower}..{upper}"
            var = match.group("var") or ""
            types = match.group("types") or ""
            return f"{match.group('lhs')}[{var}{types}{star}]{match.group('rhs')}"

        return _VLE_SEGMENT_RE.sub(_direct, text)
    # UNION_ARMS
    if len(matches) != 1:
        raise AgeDialectError(f"UNION_ARMS supports exactly one VLE segment, found {len(matches)}")
    seg_match = matches[0]
    if seg_match.group("lhs") != "-" or seg_match.group("rhs") != "->":
        raise AgeDialectError("UNION_ARMS requires an outward '-…->' VLE segment")
    if seg_match.group("var"):
        raise AgeDialectError("UNION_ARMS cannot expand a VLE segment with a bound rel var")
    if _ORDER_BY_RE.search(text):
        raise AgeDialectError(
            "UNION_ARMS cannot preserve ORDER BY across arms (per-arm order != global order)"
        )
    if _LIMIT_OR_SKIP_RE.search(text):
        raise AgeDialectError(
            "UNION_ARMS would duplicate LIMIT/SKIP per arm — pop_trailing_limit must run "
            "first (fail-loud, never arms x limit over-return)"
        )
    lower, upper, _single = _vle_bounds(seg_match)
    types = seg_match.group("types") or ""
    passign = _PATH_ASSIGN_RE.search(text)
    path_bound = passign is not None and passign.end() <= seg_match.start()
    arms: list[str] = []
    for k in range(lower, upper + 1):
        if path_bound:
            assert passign is not None
            pv = passign.group(1)
            chain = "()".join(f"-[__vle_r{i}{types}]->" for i in range(1, k + 1))
            arm = (
                text[: passign.start()]
                + "MATCH "
                + text[passign.end() : seg_match.start()]
                + chain
                + text[seg_match.end() :]
            )
            rel_list = "[" + ", ".join(f"__vle_r{i}" for i in range(1, k + 1)) + "]"
            arm = re.sub(rf"relationships\(\s*{re.escape(pv)}\s*\)", rel_list, arm)
            if re.search(rf"\b{re.escape(pv)}\b", arm):
                raise AgeDialectError(
                    f"UNION_ARMS: residual path token {pv!r} after expansion (unsupported shape)"
                )
        else:
            chain = "()".join([f"-[{types}]->"] * k)
            arm = text[: seg_match.start()] + chain + text[seg_match.end() :]
        arms.append(arm)
    return "\nUNION\n".join(arms)


# --- fulltext shim (feasibility row 9) ---------------------------------------


def ft_ddl(graph: str) -> list[str]:
    """DDL for the ``{graph}_ft`` fulltext side table + its indexes."""
    g = validate_graph_name(graph)
    table = f'"{g}_ft"'
    return [
        f"CREATE TABLE IF NOT EXISTS {table} ("
        "uuid text NOT NULL, "
        "kind text NOT NULL CHECK (kind IN ('node','edge')), "
        "group_id text, "
        "content text, "
        "tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED, "
        "PRIMARY KEY (uuid, kind))",
        f'CREATE INDEX IF NOT EXISTS "{g}_ft_tsv" ON {table} USING gin (tsv)',
        f'CREATE INDEX IF NOT EXISTS "{g}_ft_group" ON {table} USING btree (group_id)',
    ]


def ft_upsert_sql(graph: str) -> str:
    """INSERT ... ON CONFLICT upsert for one fulltext row."""
    g = validate_graph_name(graph)
    table = f'"{g}_ft"'
    return (
        f"INSERT INTO {table} (uuid, kind, group_id, content) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (uuid, kind) DO UPDATE SET "
        "group_id = EXCLUDED.group_id, content = EXCLUDED.content"
    )


def ft_search_sql(graph: str) -> str:
    """Fulltext search SQL: ts_rank over ``websearch_to_tsquery('simple', …)``.

    ts_rank is NOT BM25; under Graphiti's RRF rank-order fusion only the *order*
    matters, and ts_rank yields a sensible relevance order (feasibility row 9).
    """
    g = validate_graph_name(graph)
    table = f'"{g}_ft"'
    return (
        f"SELECT uuid, kind, ts_rank(tsv, q) AS rank "
        f"FROM {table}, websearch_to_tsquery('simple', %s) q "
        "WHERE tsv @@ q AND (%s::text[] IS NULL OR group_id = ANY(%s)) "
        "AND kind = %s "
        "ORDER BY rank DESC LIMIT %s"
    )


# --- vector shim (feasibility row 10) ----------------------------------------


class VectorBackend(StrEnum):
    PGVECTOR = "pgvector"
    CLIENT = "client"


def vec_pgvector_ddl(graph: str) -> list[str]:
    """DDL for the pgvector-backed ``{graph}_vec`` table.

    The ``embedding`` column is an UNDIMENSIONED ``vector`` — pgvector accepts an
    untyped vector column that stores any dimension, and the only index emitted here is
    the ``group_id`` btree (an ANN index would need a fixed dim, but this driver does
    not create one). Emitting it undimensioned lets the table be created up front in
    :meth:`build_indices_and_constraints` (before any embedding is seen), so a
    similarity search against a fresh / episode-only graph returns an empty result —
    exactly like the Neo4j empty-graph path — instead of failing with ``UndefinedTable``.
    The ``source_uuid`` / ``target_uuid`` endpoint columns carry an edge's node pair so
    an edge-similarity search can filter on source/target IN the SQL (parity with the
    Neo4j filtered path); they are NULL for node rows and for legacy top-level upserts.
    """
    g = validate_graph_name(graph)
    table = f'"{g}_vec"'
    return [
        f"CREATE TABLE IF NOT EXISTS {table} ("
        "uuid text NOT NULL, kind text NOT NULL, field text NOT NULL, "
        "group_id text, source_uuid text, target_uuid text, "
        "embedding vector, "
        "PRIMARY KEY (uuid, kind, field))",
        f'CREATE INDEX IF NOT EXISTS "{g}_vec_group" ON {table} USING btree (group_id)',
    ]


def vec_client_ddl(graph: str) -> list[str]:
    """DDL for the client-cosine fallback ``{graph}_vec`` table (embedding jsonb).

    Carries the same ``source_uuid`` / ``target_uuid`` endpoint columns as the pgvector
    rung so the endpoint filter is expressed in SQL on BOTH rungs (parity with Neo4j).
    """
    g = validate_graph_name(graph)
    table = f'"{g}_vec"'
    return [
        f"CREATE TABLE IF NOT EXISTS {table} ("
        "uuid text NOT NULL, kind text NOT NULL, field text NOT NULL, "
        "group_id text, source_uuid text, target_uuid text, embedding jsonb, "
        "PRIMARY KEY (uuid, kind, field))",
        f'CREATE INDEX IF NOT EXISTS "{g}_vec_group" ON {table} USING btree (group_id)',
    ]


def vec_table_exists_sql() -> str:
    """``to_regclass`` existence probe for the ``{graph}_vec`` side table.

    Defense-in-depth for a driver instance used before
    :meth:`build_indices_and_constraints` created the side tables: the table is now
    created up front (both rungs) so a fresh-graph search returns empty rather than
    ``UndefinedTable``; this probe remains as a belt-and-braces guard for the
    build-skipped path (it is no longer the primary mechanism).
    """
    return "SELECT to_regclass(%s)"


def vec_pgvector_search_sql(graph: str) -> str:
    """Top-k similarity via the pgvector ``<=>`` cosine-distance operator.

    Applies the optional ``source_uuid`` / ``target_uuid`` endpoint filters IN SQL (a
    NULL filter param passes everything through), matching the Neo4j filtered
    edge-similarity path — it can never under-return the way a bounded post-filter
    fetch could.
    """
    g = validate_graph_name(graph)
    table = f'"{g}_vec"'
    return (
        f"SELECT uuid, kind, 1 - (embedding <=> %s::vector) AS score "
        f"FROM {table} "
        "WHERE field = %s AND kind = %s "
        "AND (%s::text[] IS NULL OR group_id = ANY(%s)) "
        "AND (%s::text IS NULL OR source_uuid = %s) "
        "AND (%s::text IS NULL OR target_uuid = %s) "
        "ORDER BY embedding <=> %s::vector LIMIT %s"
    )


def vec_client_search_sql(graph: str) -> str:
    """Fetch group-filtered candidate rows for the client-side cosine fallback.

    The ``source_uuid`` / ``target_uuid`` endpoint filters are applied IN SQL here too
    (a NULL filter param passes everything through), so the client rung enforces the
    same source/target parity as the pgvector rung before client-side cosine ranking.
    """
    g = validate_graph_name(graph)
    table = f'"{g}_vec"'
    return (
        f"SELECT uuid, kind, embedding FROM {table} "
        "WHERE field = %s AND kind = %s "
        "AND (%s::text[] IS NULL OR group_id = ANY(%s)) "
        "AND (%s::text IS NULL OR source_uuid = %s) "
        "AND (%s::text IS NULL OR target_uuid = %s)"
    )


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (client-side fallback rung)."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# --- edge similarity source/target defense-in-depth post-filter --------------


def filter_edge_candidates(
    edges: list[Any],
    source_node_uuid: str | None,
    target_node_uuid: str | None,
) -> list[Any]:
    """Defense-in-depth post-filter of reconstructed edges on source/target node uuid.

    The primary source/target filter is now applied IN the vec-search SQL (via the
    ``source_uuid`` / ``target_uuid`` endpoint columns), matching Graphiti's Neo4j
    filtered edge-similarity path. This post-filter is retained as a cheap fail-closed
    guard over the reconstructed edges — so even a legacy row that slipped in with NULL
    endpoints can never surface a fact for the wrong entity pair. ``None`` filters pass
    everything through.
    """
    out: list[Any] = []
    for edge in edges:
        if (
            source_node_uuid is not None
            and getattr(edge, "source_node_uuid", None) != source_node_uuid
        ):
            continue
        if (
            target_node_uuid is not None
            and getattr(edge, "target_node_uuid", None) != target_node_uuid
        ):
            continue
        out.append(edge)
    return out


# --- residual cypher rewrites ------------------------------------------------

# ``vecf32(...)`` (FalkorDB) / ``vector.similarity.cosine(...)`` wrappers never reach
# AGE — search is intercepted by the SearchInterface. Any db.* procedure that DOES
# reach translation is a construct we deliberately do not route around: fail loud.
_UNSUPPORTED_CALL_RE = re.compile(r"\bCALL\s+db\.", re.IGNORECASE)
_VECF32_RE = re.compile(r"\bvecf32\s*\(([^()]*)\)")

# AGE rejects ``SET <var> = $param`` when ``$param`` is a map parameter
# ("SET clause expects a map"), but ACCEPTS ``SET <var> = properties($param)``
# (live-verified on apache/age:release_PG17_1.6.0). Graphiti's Neo4j entity/edge save
# queries use ``SET n = $entity_data`` / ``SET e = $edge_data`` (a whole-map replace),
# so wrap the RHS in ``properties(...)``. Property-level sets (``SET n.x = $y``) and
# inline-map sets (``SET n = {…}``, episodic save) do not match and are left intact.
_SET_MAP_PARAM_RE = re.compile(r"\bSET\s+([A-Za-z_]\w*)\s*=\s*\$([A-Za-z_]\w*)\b")

# Graphiti's Neo4j entity/edge save adds dynamic labels with ``SET n:Entity`` (and
# multi-label ``SET n:Entity:Person``). AGE fixes a node's single label at MERGE time
# and does not support ``SET n:Label``; the ``:Entity`` label is already bound by the
# ``MERGE (n:Entity …)`` head, so the redundant label-set is stripped. (A ``:`` after
# the variable distinguishes this from a property set ``SET n.x`` and a map set
# ``SET n = …``.)
_SET_LABEL_RE = re.compile(r"\bSET\s+[A-Za-z_]\w*(?::[A-Za-z_]\w*)+\s*")

# Graphiti's Neo4j BULK entity-node save (``add_nodes_and_edges_bulk_tx``, the path the
# sidecar ``/episodes`` service actually drives via ``Graphiti.add_episode``) uses the
# Neo4j-5 DYNAMIC-label form ``SET n:$(node.labels)`` — the label set is read from the
# UNWIND row. AGE 1.6.0 rejects it with a hard ``syntax error at ":"`` (live-verified),
# and the ``:Entity`` label is already bound by the ``MERGE (n:Entity …)`` head, so the
# redundant dynamic label-set is stripped — exactly the posture of the static
# ``SET n:Label`` strip above. The parenthesised expression carries no nested parens, so
# ``[^()]*`` suffices.
_SET_DYNLABEL_RE = re.compile(r"\bSET\s+[A-Za-z_]\w*:\$\([^()]*\)\s*")

# Graphiti's Neo4j save writes the embedding via ``WITH n CALL
# db.create.set{Node,Relationship}VectorProperty(n, "…_embedding", $x.…_embedding)``.
# The AGE driver routes embeddings to the vec side table (feasibility row 10), so this
# WITH…CALL segment is stripped (it is an EXPECTED write-path construct, unlike the
# fulltext/vector *search* index procedures which must never reach here). The property
# args carry no nested parens, so ``[^()]*`` is sufficient.
#
# The single-save queries carry ONE WITH variable (``WITH n CALL …`` / ``WITH e CALL …``)
# but the BULK save queries (``add_nodes_and_edges_bulk_tx`` — the ``/episodes`` service
# path) carry the UNWIND row alongside the node/edge var: ``WITH n, node CALL …`` /
# ``WITH e, edge CALL …``. Both must be stripped, so the WITH clause matches a
# COMMA-SEPARATED variable list — otherwise the bulk form is left intact and the
# ``CALL db.*`` fail-loud guard blocks all ``/episodes`` ingestion (Codex P1).
_VECTOR_PROP_CALL_RE = re.compile(
    r"\bWITH\s+[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*\s+CALL\s+"
    r"db\.create\.set(?:Node|Relationship)VectorProperty\s*\([^()]*\)\s*",
    re.IGNORECASE,
)


def rewrite_cypher(text: str) -> str:
    """Apply residual Neo4j/Falkor→AGE text rewrites; raise on unsupported constructs.

    * Strips ``vecf32(...)`` wrappers (a FalkorDB-base residue).
    * Strips the ``WITH n CALL db.create.set*VectorProperty(...)`` write-path clause —
      both the single-save (``WITH n``) and bulk (``WITH n, node``) variants (embeddings
      route to the vec side table).
    * Strips redundant ``SET n:Label`` label-adds AND the bulk dynamic-label form
      ``SET n:$(node.labels)`` (AGE binds the label at MERGE; the dynamic form is a hard
      AGE syntax error).
    * Rewrites ``SET <var> = $param`` → ``SET <var> = properties($param)`` (the AGE
      whole-map-replace form; live-verified). The bulk ``SET n = node`` / ``SET e = edge``
      map-from-UNWIND-row form executes on AGE directly and is left untouched.
    * Any *remaining* ``CALL db.*`` is a fulltext/vector index procedure that should
      have been intercepted by the SearchInterface — its presence is a bug, so raise
      :class:`AgeDialectError` (fail-loud, never a silent no-op).
    """
    rewritten = _VECF32_RE.sub(r"\1", text)
    rewritten = _VECTOR_PROP_CALL_RE.sub(" ", rewritten)
    rewritten = _SET_DYNLABEL_RE.sub("", rewritten)
    rewritten = _SET_LABEL_RE.sub("", rewritten)
    rewritten = _SET_MAP_PARAM_RE.sub(r"SET \1 = properties($\2)", rewritten)
    if _UNSUPPORTED_CALL_RE.search(rewritten):
        raise AgeDialectError(
            f"unsupported CALL db.* construct reached AGE translation: {rewritten[:120]!r}"
        )
    return rewritten


# --- RETURN alias extraction (for the driver's record keys) ------------------

# Graphiti's cypher RETURNs are ``RETURN <expr> AS <alias>, …`` or a bare ``RETURN 1``.
# The driver maps each projection item to a record-dict key; the AGE ``AS (col0, …)``
# clause uses generated names (see _pg), so these aliases NEVER touch SQL — they are
# pure presentation. Kept here (pure, no graphiti_core/psycopg import) so the mapping
# is covered by the always-on suite.
_RETURN_RE = re.compile(
    r"\bRETURN\s+(?:DISTINCT\s+)?(.+?)(?:\s+ORDER\s+BY|\s+SKIP|\s+LIMIT|\s*$)",
    re.IGNORECASE | re.DOTALL,
)
_AS_RE = re.compile(r"\bAS\s+([A-Za-z_]\w*)\s*$", re.IGNORECASE)


def _split_top_level(projection: str) -> list[str]:
    items: list[str] = []
    depth = 0
    current = ""
    for ch in projection:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        items.append(current)
    return items


def return_aliases(cypher: str) -> list[str]:
    """Best-effort RETURN column aliases for the driver's record keys.

    Falls back to a single ``result`` column when the projection cannot be parsed, and
    to ``col{i}`` for a projection item without an explicit ``AS`` alias.
    """
    match = _RETURN_RE.search(cypher)
    if match is None:
        return ["result"]
    aliases: list[str] = []
    for i, item in enumerate(_split_top_level(match.group(1))):
        as_match = _AS_RE.search(item.strip())
        aliases.append(as_match.group(1) if as_match else f"col{i}")
    return aliases or ["result"]


# --- side-table population plan (fulltext + vector) --------------------------

#: Embedding property names Graphiti carries, mapped to the vec-table ``field`` value.
_EMBEDDING_FIELDS = ("name_embedding", "fact_embedding")

FtRow = tuple[str, str, "str | None", str]
#: (uuid, kind, field, group_id, source_uuid, target_uuid, vector). ``source_uuid`` /
#: ``target_uuid`` are the edge's node pair (both ``None`` for node rows) so the vec
#: side table can filter edge-similarity searches on source/target IN SQL.
VecRow = tuple[str, str, str, "str | None", "str | None", "str | None", "list[float]"]


def _has_map_element(value: list[Any]) -> bool:
    """True iff the list holds at least one map element — i.e. it is a bulk-save payload.

    The bulk save queries pass ``nodes`` / ``entity_edges`` / ``episodes`` as lists of
    MAPS. This distinguishes them from same-named scalar-list params on other queries —
    critically the single ``EpisodicNode.save``, which passes ``entity_edges`` as a list
    of edge-uuid STRINGS (never dicts). Without this guard that string list would shadow
    the episodic-flat content branch and drop the episode's fulltext row. (Callers apply
    ``isinstance(x, list)`` first so the type narrows before this is reached.)
    """
    return any(isinstance(x, dict) for x in value)


def _pop_embeddings(
    data: dict[str, Any],
    kind: str,
    uuid: str,
    group_id: str | None,
    source_uuid: str | None,
    target_uuid: str | None,
) -> list[VecRow]:
    """Remove ``*_embedding`` keys from ``data`` (in place) and return vec rows."""
    rows: list[VecRow] = []
    for field in _EMBEDDING_FIELDS:
        vec = data.pop(field, None)
        if isinstance(vec, list) and vec:
            rows.append(
                (uuid, kind, field, group_id, source_uuid, target_uuid, [float(x) for x in vec])
            )
    return rows


def plan_side_tables(clean: dict[str, Any]) -> tuple[dict[str, Any], list[FtRow], list[VecRow]]:
    """Detect the Graphiti save shape in ``clean`` and plan ft/vec side-table rows.

    Returns ``(clean2, ft_rows, vec_rows)`` where ``clean2`` is ``clean`` with nested
    embedding vectors stripped out of the ``entity_data`` / ``edge_data`` maps (and each
    element of the bulk ``nodes`` / ``entity_edges`` lists) so a 1536-float list never
    enters agtype — feasibility row 10. Recognised SINGLE-save shapes:

    * ``entity_data`` map → a ``node`` ft row (name + summary) + a ``name_embedding``
      vec row (Graphiti ``EntityNode.save``);
    * ``edge_data`` map → an ``edge`` ft row (fact) + a ``fact_embedding`` vec row
      (``EntityEdge.save``);
    * top-level ``content`` + ``uuid`` → a ``node`` ft row (``EpisodicNode.save``).

    Recognised BULK shapes (``add_nodes_and_edges_bulk_tx`` — the path the sidecar
    ``/episodes`` service drives via ``Graphiti.add_episode``; without these the
    fulltext/vector side tables stay EMPTY for episode-ingested data and ``/search``
    misses everything — Codex P1):

    * ``nodes`` list of entity-data maps → one ``node`` ft row + ``name_embedding`` vec
      row per element;
    * ``entity_edges`` list of edge maps → one ``edge`` ft row + ``fact_embedding`` vec
      row per element, with endpoints read from ``source_node_uuid`` / ``target_node_uuid``
      (the BULK edge shape's keys — distinct from single-save ``edge_data``'s
      ``source_uuid`` / ``target_uuid``);
    * ``episodes`` list of episode maps → one ``node`` ft row per element (content),
      matching the single ``EpisodicNode.save`` content branch.

    The bulk ``episodic_edges`` (MENTIONS episode→entity) carry no fact/embedding and
    are not recall targets, so they yield no rows — parity with the single episodic-edge
    save. Unrecognised param shapes (reads, index DDL, deletes) yield no rows and pass
    ``clean`` through unchanged.
    """
    ft_rows: list[FtRow] = []
    vec_rows: list[VecRow] = []
    out = dict(clean)

    ed = out.get("entity_data")
    edge = out.get("edge_data")
    nodes = out.get("nodes")
    entity_edges = out.get("entity_edges")
    episodes = out.get("episodes")
    if isinstance(ed, dict):
        ed = dict(ed)
        out["entity_data"] = ed
        uuid = str(ed.get("uuid") or "")
        gid = ed.get("group_id")
        content = " ".join(str(ed.get(k)) for k in ("name", "summary") if ed.get(k))
        if uuid:
            ft_rows.append((uuid, "node", gid, content))
            vec_rows.extend(_pop_embeddings(ed, "node", uuid, gid, None, None))
    elif isinstance(edge, dict):
        edge = dict(edge)
        out["edge_data"] = edge
        uuid = str(edge.get("uuid") or "")
        gid = edge.get("group_id")
        content = str(edge.get("fact") or "")
        # graphiti-core's EntityEdge save-data carries the node pair as source_uuid /
        # target_uuid (edges.py:343-344) — thread them onto the vec row so an
        # edge-similarity search can filter on the pair in SQL.
        src = edge.get("source_uuid")
        tgt = edge.get("target_uuid")
        src = str(src) if src is not None else None
        tgt = str(tgt) if tgt is not None else None
        if uuid:
            ft_rows.append((uuid, "edge", gid, content))
            vec_rows.extend(_pop_embeddings(edge, "edge", uuid, gid, src, tgt))
    elif isinstance(nodes, list) and _has_map_element(nodes):
        # BULK entity-node save: nodes=[entity_data, …]. Strip each element's embedding
        # (into a fresh list so the caller's dicts are never mutated) and plan its rows.
        cleaned_nodes: list[Any] = []
        for raw in nodes:
            if not isinstance(raw, dict):
                cleaned_nodes.append(raw)
                continue
            node = dict(raw)
            uuid = str(node.get("uuid") or "")
            gid = node.get("group_id")
            content = " ".join(str(node.get(k)) for k in ("name", "summary") if node.get(k))
            if uuid:
                ft_rows.append((uuid, "node", gid, content))
                vec_rows.extend(_pop_embeddings(node, "node", uuid, gid, None, None))
            cleaned_nodes.append(node)
        out["nodes"] = cleaned_nodes
    elif isinstance(entity_edges, list) and _has_map_element(entity_edges):
        # BULK entity-edge save: entity_edges=[edge, …]. Endpoints are
        # source_node_uuid / target_node_uuid in this shape (NOT the single-save
        # edge_data's source_uuid / target_uuid) — thread them so an edge-similarity
        # search filters on the pair in SQL.
        cleaned_edges: list[Any] = []
        for raw in entity_edges:
            if not isinstance(raw, dict):
                cleaned_edges.append(raw)
                continue
            e = dict(raw)
            uuid = str(e.get("uuid") or "")
            gid = e.get("group_id")
            content = str(e.get("fact") or "")
            src = e.get("source_node_uuid")
            tgt = e.get("target_node_uuid")
            src = str(src) if src is not None else None
            tgt = str(tgt) if tgt is not None else None
            if uuid:
                ft_rows.append((uuid, "edge", gid, content))
                vec_rows.extend(_pop_embeddings(e, "edge", uuid, gid, src, tgt))
            cleaned_edges.append(e)
        out["entity_edges"] = cleaned_edges
    elif isinstance(episodes, list) and _has_map_element(episodes):
        # BULK episodic-node save: episodes=[episode, …]. Episodes carry no embedding;
        # fulltext-index each content as a node row (parity with the single-save
        # EpisodicNode.save content branch below).
        for raw in episodes:
            if not isinstance(raw, dict):
                continue
            uuid = str(raw.get("uuid") or "")
            gid = raw.get("group_id")
            if uuid and raw.get("content") is not None:
                ft_rows.append((uuid, "node", gid, str(raw["content"])))
    elif out.get("content") is not None and out.get("uuid"):
        uuid = str(out["uuid"])
        gid = out.get("group_id")
        ft_rows.append((uuid, "node", gid, str(out["content"])))

    return out, ft_rows, vec_rows
