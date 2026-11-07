# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Curated, parameterized SPARQL templates — governed at load, never at request (NE-085c).

A :class:`QueryTemplate` is a NAMED, parameter-typed SPARQL query SHAPE: the query
BODY is fixed/curated, only the typed args are caller-supplied, and they bind via
``VALUES`` + rdflib ``.n3()`` serialization (NEVER string interpolation) — so a
malicious arg can only ever be an RDF literal, never syntax (C12, brain-tier3-spec
§11.2). EVERY validation is done at registry LOAD (boot fails, never per-request):
the raw SPARQL parses (rdflib ``prepareQuery``), it is SELECT-only, it names no
banned keyword (LOAD/SERVICE/UPDATE/… plus FROM/GRAPH/LIMIT/OFFSET — trusted code
owns dataset scoping and the row cap), it carries exactly one ``#BINDINGS#`` marker
line, every declared arg appears as a ``?var``, and the citation var is a declared
column in the SELECT projection; every answer column is graph-joined (a real triple
pattern binds it — never ``VALUES``/``BIND``-only), and no declared arg is projected
as a column unless it is a graph-joined ``iri`` (NE-GA-TG, arg-echo poisoning,
grounded-agent design §2.7-F1).

Deliberate divergences from the brain-tier3-spec §11.2 sketch (documented, stricter):
templates are TOML (stdlib ``tomllib``) not YAML (C4 — no new dependency); ALL args
are required in v1 (optional filtering is a second template); the ``LIMIT`` is
injected by trusted code, never by the template author.

Two DISTINCT aggregate axes, do not conflate them (NE-CTXB-7):

* ``[scope] result_shape='aggregate'`` (the old ``acl_complete`` existence-leak
  axis) STAYS rejected at load — an aggregate over a permission-trimmed view that
  claims completeness leaks existence of invisible data (R3/§11.4, deferred).
* ``[answer] kind='count'`` (NE-CTXB-7, design §5.1-5.5) is the GOVERNED aggregate
  surface and IS supported: a server-side verified COUNT whose C7 story is that the
  count is computed over the a-priori-trimmed dataset — an outsider sees SMALLER
  counts (pinned by test), never a completeness claim. The body stays a plain
  single-path conjunctive SELECT (every ban below intact); trusted code owns the
  ``COUNT(DISTINCT …)`` projection, GROUP BY, ORDER BY, and LIMIT.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, create_model, model_validator

from ne.brain_ontology.verified.errors import TemplateRegistryError

if TYPE_CHECKING:
    from ne.brain_ontology.verified.store import StoredVerifiedTemplate

__all__ = [
    "LIST_MAX_CEILING",
    "AnswerSpec",
    "ArgDef",
    "DateWindowSpec",
    "QueryTemplate",
    "RoutingSpec",
    "ScopeSpec",
    "TemplateRegistry",
    "TemplateRegistryFingerprint",
    "describe_invalid_args",
    "list_members",
    "load_template_registry",
    "load_template_registry_with_overlay",
    "parse_template_toml",
    "render_count_query",
    "render_query",
]

#: The marker line (a SPARQL comment) the renderer replaces with the ``VALUES`` block.
BINDINGS_MARKER = "#BINDINGS#"

#: The tool-name prefix classifying every verified-answer tool as tier-0 (``read.``).
TOOL_NAME_PREFIX = "read.kg."

_ArgType = Literal["str", "int", "bool", "date", "datetime", "iri"]
_PATTERNABLE: frozenset[str] = frozenset({"str", "iri"})

#: IRI-arg charset gate: reject the SPARQL-syntax-significant delimiters and any
#: whitespace, so an ``iri`` arg can never smuggle query structure through ``<...>``.
_IRI_FORBIDDEN = re.compile(r"[<>\"{}|^`\\\s]")

_TEMPLATE_ID_RE = r"^[a-z][a-z0-9_-]{1,63}$"

#: NE-KG-MULTIKEY hard ceiling on a multi-key (``VALUES``-list) argument. NO template
#: may declare a ``list_max`` above this, whatever its TOML says — the point of the
#: cap is that a list binding can never degenerate into an unbounded scan, so the
#: bound lives in CODE (this constant) as well as in per-template config. Derived
#: from the meridian-v1 fixture's real populations (15 customers, 10 auto loans, 3
#: selected counties out of a 4-value enum): 32 is ~2x the largest population, which
#: is generous headroom for fixture growth while staying an order of magnitude below
#: any template's own ``max_rows`` fan-out budget.
LIST_MAX_CEILING = 32


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ArgDef(_Frozen):
    """One typed template argument. ``pattern`` is only valid for ``str``/``iri``.

    ``enum`` (SPEC §12 item 3, NE-TEMPLATE-ENUM) declares a CLOSED vocabulary for a
    ``str`` arg. Declaring it does two things, both contract-level, neither
    agent-guessing: (1) the generated args model advertises the values in the tool
    JSON Schema the agent sees (``"enum": [...]``), and (2) an argument outside the
    vocabulary is REFUSED fail-loud BEFORE any query executes — never run into a
    silent zero-row result indistinguishable from "no matching data".

    C12 disclosure line (decided, defended in the NE-TEMPLATE-ENUM PR): an ``enum``
    is BY CONSTRUCTION published schema — every declared value already ships to the
    model in the advertised tool schema on every turn — so a refusal MAY name the
    permitted values (repeating them adds zero information). The REJECTED value is
    caller data and is NEVER echoed. A vocabulary that is itself sensitive must NOT
    be declared as ``enum`` at all (use ``pattern``/``max_length``, which constrain
    without enumerating): declaring an enum IS publishing it.
    """

    type: _ArgType
    pattern: str | None = None
    max_length: int = Field(default=256, ge=1, le=8192)
    #: Closed permitted-value vocabulary (``str`` args only). ``None`` = open.
    enum: tuple[str, ...] | None = None
    #: NE-KG-MULTIKEY. ``None`` (the default, and every pre-existing arg) = SCALAR
    #: ONLY: the arg binds exactly one value, byte-identically to before this field
    #: existed. An int N opts the arg into MULTI-KEY binding: the caller may pass a
    #: JSON ARRAY of at most N values, which trusted code renders as an N-row
    #: ``VALUES`` block (one bound row per member). N is the CARDINALITY CAP — the
    #: whole point of the feature is that a list can never become an unbounded scan,
    #: so the bound is declared per template AND ceilinged in code
    #: (:data:`LIST_MAX_CEILING`). Every member is validated with the IDENTICAL
    #: strictness as the scalar form (type, enum membership, pattern, max_length) and
    #: an invalid member refuses the WHOLE call — a member is never silently dropped.
    list_max: int | None = Field(default=None, ge=2, le=LIST_MAX_CEILING)
    #: NE-KG-MULTIKEY, REQUIRED whenever ``list_max`` is set: the answer COLUMN whose
    #: cell equals this argument for a matched row (the template body must carry the
    #: literal ``FILTER(?<key_column> = ?<arg>)`` join, checked at load). It is what
    #: makes the EMPTY-MEMBER case establishable: a member key absent from this
    #: graph-joined column across the complete (untruncated) result set returned no
    #: rows — the multi-key equivalent of today's empty single-key read. The column
    #: is graph data (GUARD 2 proves it is triple-bound), never the echoed argument.
    key_column: str | None = None
    #: NE-CAPTAB: author-written argument SEMANTICS (what the value means, its
    #: format, and any cross-field rule the validator enforces — e.g. a date
    #: window's inclusivity and span cap). Emitted into the generated args
    #: model's JSON Schema as the property ``description``, so BOTH consumers of
    #: the one schema see it: the model (via the in-band tool catalog) and the
    #: console's Capabilities panel (via the capability preflight projection).
    #: ``None`` (every pre-existing template) emits no description key and keeps
    #: the advertised schema byte-identical. Never a place for values or data —
    #: it documents the CONTRACT, and it ships to every principal that can see
    #: the tool, exactly like ``enum`` (see the C12 note above).
    description: str | None = None

    @model_validator(mode="after")
    def _check_pattern(self) -> ArgDef:
        if self.pattern is not None and self.type not in _PATTERNABLE:
            raise TemplateRegistryError(
                f"arg type {self.type!r} may not declare a pattern (only str/iri may)"
            )
        if self.enum is not None:
            if self.type != "str":
                raise TemplateRegistryError(
                    f"arg type {self.type!r} may not declare an enum (only str may): a "
                    "closed vocabulary is a string contract; other types constrain by type"
                )
            if self.pattern is not None:
                raise TemplateRegistryError(
                    "arg declares both enum and pattern — two vocabulary sources can "
                    "disagree; declare exactly one (the enum IS the value contract)"
                )
            if not self.enum:
                raise TemplateRegistryError(
                    "arg declares an empty enum — it would refuse every value; either "
                    "declare the vocabulary or remove the key"
                )
            if len(set(self.enum)) != len(self.enum):
                raise TemplateRegistryError(f"arg enum {self.enum!r} has duplicate values")
            for value in self.enum:
                if not value:
                    raise TemplateRegistryError("arg enum contains an empty string value")
                if len(value) > self.max_length:
                    raise TemplateRegistryError(
                        f"arg enum value {value!r} exceeds max_length={self.max_length}"
                    )
        if self.key_column is not None and self.list_max is None:
            raise TemplateRegistryError(
                "arg declares key_column without list_max — key_column exists only to "
                "resolve the multi-key EMPTY-MEMBER case; a scalar arg must not declare it"
            )
        if self.list_max is not None:
            # Multi-key binding is deliberately NARROW. It is legal only for a
            # string-shaped arg carrying a CLOSED or PATTERN-BOUNDED vocabulary, so
            # every member of a list is provably a short, prose-free token. That is
            # what lets the empty-member note name the members that returned no rows
            # without ever putting caller prose in front of the model (§2.7-F1: the
            # arg-echo attack needs a prose payload, and these types cannot carry one).
            if self.type not in _PATTERNABLE:
                raise TemplateRegistryError(
                    f"arg type {self.type!r} may not declare list_max (only str/iri may): "
                    "multi-key binding needs a vocabulary-bounded string-shaped argument"
                )
            if self.enum is None and self.pattern is None:
                raise TemplateRegistryError(
                    "arg declares list_max but neither enum nor pattern — a multi-key "
                    "argument must be vocabulary-bounded (a closed enum) or shape-bounded "
                    "(a pattern); an open free-text list is refused"
                )
            if self.enum is not None and self.list_max > len(self.enum):
                raise TemplateRegistryError(
                    f"arg list_max={self.list_max} exceeds its own closed enum size "
                    f"{len(self.enum)} — a list can never hold more DISTINCT values than "
                    "the vocabulary declares (duplicates are refused), so the cap is a lie"
                )
            if self.key_column is None:
                raise TemplateRegistryError(
                    "arg declares list_max without key_column — the multi-key EMPTY-MEMBER "
                    "case is only establishable against a named graph-joined answer column"
                )
        return self


class ScopeSpec(_Frozen):
    """C7 scope declaration: the ACL-label graph families a template may read."""

    graph_classes: tuple[str, ...] = Field(min_length=1)
    result_shape: str = "enumerable"
    #: Present ONLY so an aggregate template's acl_complete pairing still fails loud
    #: (aggregate is rejected regardless — see the validator). Unused for enumerable.
    acl_complete: bool = False

    @model_validator(mode="after")
    def _check_result_shape(self) -> ScopeSpec:
        # NOTE (NE-CTXB-7): this rejects the ``[scope] result_shape='aggregate'``
        # existence-leak axis — an aggregate that CLAIMS completeness over a trimmed
        # view. It is NOT the governed count surface: a verified COUNT is opted into
        # via ``[answer] kind='count'`` (design §5.1-5.5), whose C7 story is that an
        # outsider sees SMALLER counts over the a-priori-trimmed dataset (no
        # completeness claim). The two are orthogonal — this ban stays.
        if self.result_shape == "aggregate":
            raise TemplateRegistryError(
                "result_shape='aggregate' is REJECTED at load: an aggregate over a "
                "permission-trimmed view that claims completeness leaks existence of "
                "invisible data (brain-tier3-spec R3/§11.4, deferred). The governed "
                "aggregate surface is [answer] kind='count' (NE-CTXB-7, design §5.1-5.5), "
                "whose counts are computed over the trimmed dataset (outsiders see smaller "
                "counts) — result_shape stays 'enumerable' even for a count template."
            )
        if self.result_shape != "enumerable":
            raise TemplateRegistryError(
                f"result_shape={self.result_shape!r} unsupported (only 'enumerable' in v1)"
            )
        return self


class DateWindowSpec(_Frozen):
    """Cross-argument bound for an inclusive pair of ``date`` arguments.

    Typed dates alone reject malformed values but do not prevent an agent from
    asking an otherwise-subject-specific template to scan an unbounded time span.
    This optional declaration keeps that bound in the curated template contract;
    both the pre-gate validator and the execution core enforce it.
    """

    start_arg: str
    end_arg: str
    max_days: int = Field(ge=0, le=366)


#: A count column alias must be a bare SPARQL variable name (no punctuation) so the
#: trusted projection ``(COUNT(DISTINCT ?over) AS ?<alias>)`` is a syntactic literal.
_COUNT_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AnswerSpec(BaseModel):
    """Deterministic rendering: which projected vars are columns + the citation var.

    ``kind='rows'`` (default, byte-identical back-compat) projects the declared
    ``columns`` verbatim. ``kind='count'`` (NE-CTXB-7, Layer 3) is a server-side
    verified COUNT aggregate: the template BODY stays a plain single-path
    conjunctive SELECT, and TRUSTED code renders
    ``SELECT ?g1 ?g2 (COUNT(DISTINCT ?<count_over>) AS ?<as>) … GROUP BY ?g1 ?g2``.
    The derived ``columns`` are ``[*group_by, <as>]`` — an EXPLICIT ``columns`` key
    is refused (they are computed, never author-declared). ``count_total_rows`` (legal
    on BOTH kinds) opts a template into the trusted ``COUNT(*)`` → ``total_rows``
    rewrite under truncation (as-of-count-time; see ``answers.execute_template``).

    TWO NAMED ROLES, one per axis of identity (NE-LIBRARY-LEGIBILITY):
    ``citation_var`` names the MACHINE IDENTIFIER column — the citation anchor an
    auditor quotes and the chain records. ``label_var`` (optional) names the
    HUMAN-READABLE LABEL column. A renderer that has a ``label_var`` leads with
    it and demotes — never hides — the identifier; with no ``label_var`` it
    renders the declared column order verbatim (identifier-led, no fabricated
    label). Column ORDER in ``columns`` stays load-bearing for machine renderings
    (the ``" | "``-joined tool text) — ``label_var`` changes display emphasis
    only, never the projection.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    #: DERIVED for ``kind='count'`` (``[*group_by, <as>]``); author-declared for rows.
    columns: tuple[str, ...] = ()
    citation_var: str
    #: Complete source-bearing subject set for each returned row. The executor uses
    #: these bindings to construct the immutable contributor receipt; a citation
    #: anchor alone is insufficient for joined rows.
    contributor_vars: tuple[str, ...] = ()
    source_status: Literal["source_bearing", "no_source"] = "no_source"
    #: The human-readable label column, or ``None`` when the template projects no
    #: label (renderers then degrade to the identifier — they never invent one).
    label_var: str | None = None
    kind: Literal["rows", "count"] = "rows"
    #: ``kind='count'`` only: the triple-bound GROUP BY keys (declared order).
    group_by: tuple[str, ...] = ()
    #: ``kind='count'`` only: the triple-bound variable being ``COUNT(DISTINCT …)``-ed.
    count_over: str | None = None
    #: ``kind='count'`` only: the aggregate column name (TOML key ``as``).
    count_as: str | None = Field(default=None, alias="as")
    #: Opt-in (either kind): fill ``total_rows`` under truncation via a trusted
    #: ``COUNT(*)`` rewrite over the identical body + the SAME C7 dataset.
    count_total_rows: bool = False

    @model_validator(mode="before")
    @classmethod
    def _derive_count_columns(cls, data: object) -> object:
        # For a count template the projected columns are DERIVED — an explicit
        # ``columns`` key is refused, and ``columns = [*group_by, <as>]`` is set
        # so the downstream GUARD/citation machinery has a concrete column tuple.
        if not isinstance(data, dict):
            return data
        if data.get("kind") != "count":
            return data
        if "columns" in data:
            raise TemplateRegistryError(
                "answer.columns must NOT be declared on a count template — the columns "
                "are DERIVED as [*group_by, <as>] (NE-CTXB-7 §5.1); remove the key"
            )
        group_by = data.get("group_by") or []
        as_value = data.get("as")
        if as_value is None:
            as_value = data.get("count_as")
        derived = list(group_by)
        if as_value is not None:
            derived.append(as_value)
        return {**data, "columns": derived}

    @model_validator(mode="after")
    def _check(self) -> AnswerSpec:
        if self.source_status == "source_bearing" and not self.contributor_vars:
            raise TemplateRegistryError(
                "answer.contributor_vars must declare every source-bearing subject"
            )
        if len(set(self.contributor_vars)) != len(self.contributor_vars):
            raise TemplateRegistryError("answer.contributor_vars must be unique")
        if any(var not in self.columns for var in self.contributor_vars):
            raise TemplateRegistryError(
                "every answer.contributor_vars entry must be a projected answer column"
            )
        if self.kind == "rows":
            if not self.columns:
                raise TemplateRegistryError("answer.columns must declare at least one column")
            if self.group_by or self.count_over is not None or self.count_as is not None:
                raise TemplateRegistryError(
                    "answer aggregate keys (group_by/count_over/as) are only valid on a "
                    "kind='count' template — refused on kind='rows' (NE-CTXB-7 §5.1)"
                )
        else:  # kind == "count"
            if not self.group_by:
                raise TemplateRegistryError(
                    "kind='count' requires a non-empty group_by (the GROUP BY keys)"
                )
            if len(set(self.group_by)) != len(self.group_by):
                raise TemplateRegistryError(
                    f"kind='count' group_by {self.group_by!r} has duplicate keys"
                )
            if self.count_over is None:
                raise TemplateRegistryError("kind='count' requires count_over (the counted var)")
            if self.count_over in self.group_by:
                raise TemplateRegistryError(
                    f"kind='count' count_over {self.count_over!r} is also a group_by key — a "
                    "count grouped by the counted variable is always 1 (degenerate, refused)"
                )
            if self.count_as is None:
                raise TemplateRegistryError("kind='count' requires as (the count column name)")
            if not _COUNT_ALIAS_RE.match(self.count_as):
                raise TemplateRegistryError(
                    f"kind='count' as {self.count_as!r} must match ^[A-Za-z_][A-Za-z0-9_]*$ "
                    "(a bare SPARQL variable name)"
                )
            if self.count_as in self.group_by:
                raise TemplateRegistryError(
                    f"kind='count' as {self.count_as!r} collides with a group_by key"
                )
            if self.count_as == self.count_over:
                raise TemplateRegistryError(
                    f"kind='count' as {self.count_as!r} must differ from count_over"
                )
            if self.citation_var not in self.group_by:
                raise TemplateRegistryError(
                    f"answer.citation_var {self.citation_var!r} must be one of the group_by "
                    f"keys {self.group_by!r} for a count template — the per-group citation is "
                    "the group-key entity IRI (a real graph subject) (NE-CTXB-7 §5.2)"
                )
        # Belt (both kinds): the citation column is a declared/derived column.
        if self.citation_var not in self.columns:
            raise TemplateRegistryError(
                f"answer.citation_var {self.citation_var!r} must be one of the declared "
                f"columns {self.columns!r}"
            )
        # label_var (both kinds): a declared/derived column, and NEVER the citation
        # column — a label that IS the identifier would let a renderer lead with
        # the machine key while claiming it leads with the human label.
        if self.label_var is not None:
            if self.label_var not in self.columns:
                raise TemplateRegistryError(
                    f"answer.label_var {self.label_var!r} must be one of the declared "
                    f"columns {self.columns!r}"
                )
            if self.label_var == self.citation_var:
                raise TemplateRegistryError(
                    f"answer.label_var {self.label_var!r} must differ from citation_var — "
                    "the human label and the machine identifier are distinct roles "
                    "(a template whose only column is the identifier simply omits label_var)"
                )
        return self


class RoutingSpec(_Frozen):
    """Deterministic-routing metadata (verified-answer-first Layer 1, NE-VAF-U1).

    A template declaring ``[routing]`` opts into the PURE, model-free matcher
    in :mod:`ne.brain_ontology.verified.routing` (``match_template``): a
    prompt that hits a ``trigger`` (exactly, or as an in-order token
    subsequence) routes straight to this template with no model call; a
    ``guard_term`` hit with no trigger surfaces a CLARIFY instead of guessing.

    ``extra='forbid'`` (via ``_Frozen``) means an UNKNOWN key inside
    ``[routing]`` fails loud at load — the same fail-loud contract as every
    other TOML table here: an old binary fed a template with a ``[routing]``
    block it doesn't understand must never silently ignore it.
    """

    domain: str = Field(min_length=1)
    triggers: tuple[str, ...] = Field(min_length=1)
    guard_terms: tuple[str, ...] = Field(min_length=1)
    clarify_max: int = Field(default=2, ge=0, le=5)

    @model_validator(mode="after")
    def _check_terms(self) -> RoutingSpec:
        # Lazy same-package import (established precedent at line ~199) —
        # avoids a top-level import cycle between templates.py and routing.py.
        from ne.brain_ontology.verified.routing import normalize

        for trigger in self.triggers:
            if not normalize(trigger):
                raise TemplateRegistryError(
                    f"routing trigger {trigger!r} normalizes to zero tokens — an "
                    "empty-normalizing trigger would subsequence-match EVERY prompt "
                    "(universal-match bug), refused at load"
                )
        for guard_term in self.guard_terms:
            if len(normalize(guard_term)) < 2:
                raise TemplateRegistryError(
                    f"routing guard_term {guard_term!r} normalizes to fewer than 2 "
                    "tokens — single-token guard_terms are false-positive machines — "
                    "guard terms must be multi-word"
                )
        return self


# SPARQL keywords refused in a template BODY. The update/graph-management + C2 egress
# set mirrors the gateway's steward gate; FROM/GRAPH/LIMIT/OFFSET are ALSO banned
# because trusted code owns dataset scoping (the DatasetSpec) and the row cap.
# UNION/MINUS/EXISTS are ALSO banned (NE-GA-TG, Codex P1 follow-up, design §2.7-F1):
# ``_triple_pattern_vars`` proves a projected var is "graph-joined" by collecting
# variable names from ANY triple pattern ANYWHERE in the parsed algebra — it does
# NOT track which alternative/branch of the algebra a given result row actually
# comes from. Without this ban, a template could write
# ``{ ?a rdfs:label ?label } UNION { BIND("fabricated" AS ?label) }`` (or hide the
# BIND inside a ``MINUS``/``FILTER (NOT) EXISTS`` sub-pattern): the string
# ``"label"`` would appear as a triple term in ONE arm, so the global collector
# would (wrongly) treat the column as graph-joined for ALL rows, even the ones
# that actually came from the BIND-only arm. Refusing these constructs outright
# keeps every triple pattern in the body on a single, unconditional evaluation
# path, which is what makes the global collection in ``_triple_pattern_vars``
# sound. (A nested subquery is refused separately below — it opens a NEW
# projection scope with its own SELECT-expression aliases.)
#
# HARD-COUPLING (NE-CTXB-7): the ``kind='count'`` GUARD-2 relaxation — which lets the
# single derived count column bypass the "every column is triple-bound" proof — is
# HARD-COUPLED in ``_validate_and_build`` to the presence of ``UNION``/``MINUS``/
# ``EXISTS`` in THIS set (an in-code precondition + a guardian monkeypatch test).
# Weakening any of those three entries makes the count path REFUSE to load, because
# the relaxation's soundness rests on the same single-conjunctive-path invariant
# ``_triple_pattern_vars`` requires. Do not remove them without re-deriving both.
_FORBIDDEN_KEYWORDS = frozenset(
    {
        "LOAD",
        "SERVICE",
        "INSERT",
        "DELETE",
        "DROP",
        "CLEAR",
        "CREATE",
        "MOVE",
        "COPY",
        "ADD",
        "FROM",
        "GRAPH",
        "LIMIT",
        "OFFSET",
        "UNION",
        "MINUS",
        "EXISTS",
    }
)


class QueryTemplate(_Frozen):
    """A frozen, load-validated verified-answer template. Generates its strict args model."""

    template_id: str = Field(pattern=_TEMPLATE_ID_RE)
    version: int = Field(ge=1)
    description: str
    sparql: str
    args: dict[str, ArgDef] = {}
    scope: ScopeSpec
    answer: AnswerSpec
    #: Optional per-template row cap; the effective cap is ``min(this, settings cap)``.
    max_rows: int | None = Field(default=None, ge=1)
    #: Optional deterministic-routing metadata (NE-VAF-U1, Layer 1). ``None`` for
    #: every pre-existing template (back-compat: absence of ``[routing]`` is a
    #: no-op). A trailing optional field; ``QueryTemplate`` keeps ``extra='forbid'``
    #: so an OLD binary built before this field existed would still fail loud
    #: (unrecognized top-level key ``routing``) on a template that declares
    #: ``[routing]`` — it can never silently ignore the block.
    routing: RoutingSpec | None = None
    #: Optional inclusive date-window constraint, enforced after typed parsing.
    date_window: DateWindowSpec | None = None

    _args_model: type[BaseModel] = PrivateAttr()

    @property
    def tool_name(self) -> str:
        """The governed tool name — ``read.kg.<id>`` (tier-0 via the ``read.`` prefix)."""
        return f"{TOOL_NAME_PREFIX}{self.template_id.replace('-', '_')}"

    @property
    def graph_classes(self) -> tuple[str, ...]:
        return self.scope.graph_classes

    @property
    def list_arg(self) -> str | None:
        """The single multi-key argument name, or ``None`` for a scalar-only template.

        Load validation permits AT MOST ONE (a second would bind as a cross product),
        so this is well-defined; ``None`` for every template written before
        NE-KG-MULTIKEY, which is what keeps the scalar path byte-identical.
        """
        for name, arg in self.args.items():
            if arg.list_max is not None:
                return name
        return None

    @property
    def args_model(self) -> type[BaseModel]:
        """The generated strict (``extra='forbid'``) Pydantic model for the args."""
        return self._args_model

    def args_json_schema(self) -> dict[str, object]:
        """JSON Schema of the generated args model (the ToolSpec ``input_schema``)."""
        return self._args_model.model_json_schema()

    def validate_semantic_args(self, validated: BaseModel) -> None:
        """Enforce cross-field constraints after the generated typed model parses."""
        if self.date_window is None:
            return
        start = getattr(validated, self.date_window.start_arg)
        end = getattr(validated, self.date_window.end_arg)
        if start > end or (end - start).days > self.date_window.max_days:
            raise ValueError(
                "date window must have start <= end and span no more than "
                f"{self.date_window.max_days} days"
            )

    @model_validator(mode="after")
    def _validate_and_build(self) -> QueryTemplate:
        # ZERO-ARG GATE (NE-VAF-U1): v1 deterministic routing never extracts an
        # arg from prose — there is no principal-context arg source in ArgDef
        # today, so a routed template must declare zero args. Checked first,
        # before any SPARQL parsing, so the failure message is unambiguous.
        if self.routing is not None and self.args:
            raise TemplateRegistryError(
                f"template {self.template_id!r} declares [routing] but has args "
                f"{sorted(self.args)!r} — v1 deterministic routing never extracts args "
                "from prose (no principal-context arg mechanism exists in ArgDef; "
                "zero-arg templates only)"
            )
        if self.date_window is not None:
            start = self.args.get(self.date_window.start_arg)
            end = self.args.get(self.date_window.end_arg)
            if start is None or end is None:
                raise TemplateRegistryError(
                    "date_window start_arg/end_arg must name declared template arguments"
                )
            if start.type != "date" or end.type != "date":
                raise TemplateRegistryError(
                    "date_window start_arg/end_arg must both name args with type='date'"
                )
            if self.date_window.start_arg == self.date_window.end_arg:
                raise TemplateRegistryError("date_window start_arg and end_arg must differ")

        text = self.sparql
        # (c) exactly one #BINDINGS# marker (a SPARQL comment — the raw text parses).
        marker_lines = [ln for ln in text.splitlines() if ln.strip() == BINDINGS_MARKER]
        if len(marker_lines) != 1:
            raise TemplateRegistryError(
                f"template {self.template_id!r} must carry exactly one {BINDINGS_MARKER!r} "
                f"marker line (found {len(marker_lines)})"
            )

        # (b) token gate on the comment/literal-stripped text (lazy same-package import
        # keeps this module light for the default-off path).
        from ne.brain_ontology.gateway import _query_form, _strip_literals_and_comments

        stripped = _strip_literals_and_comments(text)
        tokens = set(re.findall(r"[A-Za-z]+", stripped.upper()))
        banned = tokens & _FORBIDDEN_KEYWORDS
        if banned:
            raise TemplateRegistryError(
                f"template {self.template_id!r} names forbidden keyword(s) {sorted(banned)}: "
                "SELECT-only, no update/graph-management, no LOAD/SERVICE, no "
                "FROM/GRAPH/LIMIT/OFFSET (trusted code owns dataset scoping + the row cap), "
                "and no UNION/MINUS/EXISTS (a multi-branch/negation pattern can graph-join a "
                "variable in one path while a BIND fabricates it in another, defeating the "
                "GUARD-2 projection-hygiene proof below — design §2.7-F1)"
            )

        # ORDER BY is BANNED in a template BODY (§4.4): trusted code owns ordering
        # (render_query appends the deterministic ORDER BY). A REGEX check, NOT a
        # bare-token ban: adding "ORDER" to _FORBIDDEN_KEYWORDS would also refuse a
        # legitimate ``?order`` variable (which NE-CTXB-7's count_over='order'
        # aggregate template needs), so only the two-token phrase "ORDER BY" is
        # refused. OFFSET stays banned via the keyword token set above.
        if re.search(r"\bORDER\s+BY\b", stripped, re.IGNORECASE):
            raise TemplateRegistryError(
                f"template {self.template_id!r} names ORDER BY in its body — trusted code "
                "owns ordering (a deterministic ORDER BY is appended at render time, "
                "design §4.4); remove it from the template"
            )

        # SELECT-only form.
        form = _query_form(text)
        if form != "SELECT":
            raise TemplateRegistryError(
                f"template {self.template_id!r} must be a SELECT query, got {form!r}"
            )

        # No nested subquery (NE-GA-TG, Codex P1 follow-up, design §2.7-F1): a
        # subquery opens its own projection scope where a variable can be produced
        # by a SELECT-expression alias (e.g. ``{ SELECT ?a (STR(?x) AS ?label) ... }``)
        # rather than a real triple pattern, which would let the same variable name
        # reach ``_triple_pattern_vars`` as "seen" via an unrelated outer triple and
        # slip past GUARD 2. A template body legitimately contains the ``SELECT``
        # keyword exactly once (the mandatory top-level form checked above); a
        # second occurrence in the comment/literal-stripped text can only be a
        # subquery, so it is refused outright rather than attempting to reason
        # about nested-scope soundness.
        if len(re.findall(r"\bSELECT\b", stripped, re.IGNORECASE)) > 1:
            raise TemplateRegistryError(
                f"template {self.template_id!r} contains a nested subquery (SELECT "
                "appears more than once in the query body) — refused: a subquery "
                "scope can bind a projected variable via a SELECT-expression alias "
                "with no graph-joining triple pattern (design §2.7-F1)"
            )

        # (a) the raw SPARQL parses AND (projection) — via rdflib (lazy import).
        from rdflib.plugins.sparql import prepareQuery

        try:
            prepared = prepareQuery(text)
        except Exception as exc:
            raise TemplateRegistryError(
                f"template {self.template_id!r} SPARQL does not parse: {exc}"
            ) from exc
        projected = _projected_vars(prepared)
        answer = self.answer
        if answer.kind == "rows":
            if projected is not None:
                for column in answer.columns:
                    if column not in projected:
                        raise TemplateRegistryError(
                            f"template {self.template_id!r} column {column!r} is not in the "
                            f"SELECT projection {sorted(projected)!r}"
                        )
        else:
            # kind == "count": the BODY must project EXACTLY the group_by keys +
            # count_over (the trusted renderer rewrites this plain var list into the
            # aggregate projection). count_over is Optional[str] on the model but the
            # AnswerSpec after-validator has already proven it is set for a count kind.
            assert answer.count_over is not None  # narrowing: AnswerSpec proved this for count
            expected = set(answer.group_by) | {answer.count_over}
            if projected is not None:
                missing = expected - projected
                surplus = projected - expected
                if missing:
                    raise TemplateRegistryError(
                        f"template {self.template_id!r} count body must project exactly the "
                        f"group_by keys + count_over {sorted(expected)!r}; missing "
                        f"{sorted(missing)!r} from the SELECT projection {sorted(projected)!r}"
                    )
                if surplus:
                    raise TemplateRegistryError(
                        f"template {self.template_id!r} count body projects extra var(s) "
                        f"{sorted(surplus)!r} beyond the group_by keys + count_over "
                        f"{sorted(expected)!r} — a count body projects ONLY what it groups/counts"
                    )

        # NE-GA-TG (grounding-gate F1 fix, design §2.7-F1): the grounding gate assumes
        # every answer CELL is C7-trimmed graph data, never caller input. At this point
        # in validation the query text still carries #BINDINGS# as a bare SPARQL
        # comment (the VALUES clause is injected only at RENDER time — see
        # render_query), so a declared arg projected as a column is, in THIS parse,
        # bound by nothing at all. triple_vars collects every variable that a real
        # triple pattern in the body actually joins to the graph.
        triple_vars = _triple_pattern_vars(prepared)
        subject_vars = _triple_subject_vars(prepared)
        if (
            answer.source_status == "source_bearing"
            and set(answer.contributor_vars) != subject_vars
        ):
            raise TemplateRegistryError(
                f"template {self.template_id!r} contributor_vars must equal the complete "
                f"triple-subject set {sorted(subject_vars)!r}; got "
                f"{sorted(answer.contributor_vars)!r}"
            )

        # GUARD 1 (arg-as-column, the direct F1 fix): a declared arg may be projected
        # as an answer column ONLY IF it is a graph-joined `iri`. Deliberate, narrow
        # exemption (documented deviation from a literal 'never' reading — see the
        # unit's risk note): this is exactly the shipped `entity-by-iri` fetch-by-id
        # parity-probe shape (arg `account`, type iri, joined by a real triple, and
        # projected as the citation column). It is safe to exempt because (a) the
        # `_IRI_FORBIDDEN` charset gate forbids whitespace/quotes, so an iri arg can
        # never carry fabricated prose (the F1 phrase-echo attack requires a prose
        # payload); (b) the triple join means any RETURNED binding was matched against
        # the C7-trimmed graph, not merely echoed from the caller; and (c) the G1
        # corpus arg-exclusion belt (grounded-agent design §2.7-F1, second layer, a
        # later unit) drops arg-named columns from the grounding corpus regardless,
        # covering the residual case of an iri arg joined only inside an OPTIONAL
        # group. Every non-iri arg type (str/int/bool/date/datetime) is NEVER
        # projectable as a column — the phrase-echo attack is dead for those types.
        for name, arg in self.args.items():
            if name not in self.answer.columns:
                continue
            if arg.type != "iri":
                raise TemplateRegistryError(
                    f"template {self.template_id!r} projects declared arg {name!r} "
                    f"(type {arg.type!r}) as an answer column — a caller-supplied "
                    "value must never be a verified cell (arg-echo corpus poisoning, "
                    "grounded-agent design §2.7-F1); only a graph-joined iri arg may "
                    "be projected"
                )
            if name not in triple_vars:
                raise TemplateRegistryError(
                    f"template {self.template_id!r} projects iri arg {name!r} as an "
                    "answer column but no triple pattern joins it to the graph — the "
                    "cell would echo caller input verbatim (design §2.7-F1)"
                )

        if answer.kind == "rows":
            # GUARD 2 (projection hygiene, defense-in-depth): EVERY answer column must
            # be bound by a real triple pattern — never reachable only via the injected
            # VALUES/#BINDINGS# block, a BIND alias, or a SELECT expression. This closes
            # the residual path where a non-arg-named column could still be VALUES/BIND-
            # only (e.g. a future template author writing `BIND("literal" AS ?label)`).
            for column in answer.columns:
                if column not in triple_vars:
                    raise TemplateRegistryError(
                        f"template {self.template_id!r} column {column!r} is not bound by "
                        "any triple pattern in the query body — every verified cell must "
                        "be graph-joined, never VALUES/BIND-only (design §2.7-F1)"
                    )
        else:
            # GUARD 2 (count relaxation, NE-CTXB-7 design §5.4). The single DERIVED
            # count column (``as``) is the ONLY column not required to be triple-bound
            # — it is a trusted scalar the renderer computes as
            # ``(COUNT(DISTINCT ?count_over) AS ?<as>)``. The relaxation is sound ONLY
            # because (i) count_over AND every group_by key ARE triple-bound (checked
            # here), so every group and every counted binding was matched against the
            # C7-trimmed graph — the count cannot fabricate a cell; and (ii) the body
            # is a single conjunctive path (UNION/MINUS/EXISTS/subquery banned above),
            # so ``_triple_pattern_vars`` is sound. That second precondition is
            # HARD-COUPLED here: if the bans were ever weakened, this path REFUSES to
            # load (guardian monkeypatch test test_count_relaxation_hard_coupled_to_bans).
            assert answer.count_over is not None  # narrowing: AnswerSpec proved this for count
            if not {"UNION", "MINUS", "EXISTS"} <= _FORBIDDEN_KEYWORDS:
                raise TemplateRegistryError(
                    f"template {self.template_id!r} is kind='count' but the GUARD-2 count "
                    "relaxation's precondition is violated: UNION/MINUS/EXISTS are no longer "
                    "all in _FORBIDDEN_KEYWORDS. The relaxation lets a trusted COUNT column "
                    "skip the triple-bound proof, which is sound ONLY on a single conjunctive "
                    "path (_triple_pattern_vars soundness precondition) — refusing to load "
                    "rather than trust a count over a multi-branch/negation body"
                )
            for var in answer.group_by:
                if var not in triple_vars:
                    raise TemplateRegistryError(
                        f"template {self.template_id!r} non-triple-bound group_by var "
                        f"{var!r} — every GROUP BY key must be bound by a real triple "
                        "pattern (never VALUES/BIND-only), else a fabricated group could "
                        "appear (design §5.4)"
                    )
            if answer.count_over not in triple_vars:
                raise TemplateRegistryError(
                    f"template {self.template_id!r} count over non-triple-bound var "
                    f"{answer.count_over!r} — the counted variable must be bound by a real "
                    "triple pattern (never VALUES/BIND-only), else the count is fabricated "
                    "(design §5.4)"
                )
            # Alias-collision: the trusted ``AS ?<as>`` must not shadow a body variable.
            if re.search(rf"[?$]{re.escape(answer.count_as or '')}\b", stripped):
                raise TemplateRegistryError(
                    f"template {self.template_id!r} count column alias ?{answer.count_as} "
                    "already appears as a variable in the query body — the trusted "
                    "aggregate alias must be a fresh name"
                )

        # NE-KG-MULTIKEY (multi-key VALUES binding). Everything below is a LOAD-time
        # gate, exactly like every other rule in this method: a template that opts an
        # argument into list binding must prove, at boot, that the collapse is sound.
        list_args = sorted(name for name, arg in self.args.items() if arg.list_max is not None)
        if len(list_args) > 1:
            raise TemplateRegistryError(
                f"template {self.template_id!r} declares list_max on more than one arg "
                f"{list_args!r} — two list arguments would bind as a CROSS PRODUCT of "
                "their members (a combinatorial scan the per-arg cap cannot bound); "
                "exactly one multi-key argument per template"
            )
        if list_args:
            list_name = list_args[0]
            list_arg = self.args[list_name]
            if answer.kind != "rows":
                raise TemplateRegistryError(
                    f"template {self.template_id!r} declares a multi-key arg {list_name!r} on "
                    "a kind='count' template — a grouped count over a member list would make "
                    "the per-member zero-group case indistinguishable from a zero count "
                    "(refused; multi-key binding is kind='rows' only)"
                )
            if list_name in answer.columns:
                raise TemplateRegistryError(
                    f"template {self.template_id!r} projects its multi-key arg {list_name!r} "
                    "as an answer column — a caller-supplied value must never be a verified "
                    "cell, and a multi-row VALUES binding would echo one per row "
                    "(arg-echo corpus poisoning, design §2.7-F1)"
                )
            key_column = list_arg.key_column
            assert key_column is not None  # narrowing: ArgDef proved this for list_max
            if key_column not in answer.columns:
                raise TemplateRegistryError(
                    f"template {self.template_id!r} multi-key arg {list_name!r} names "
                    f"key_column {key_column!r}, which is not one of the declared answer "
                    f"columns {answer.columns!r}"
                )
            if key_column not in triple_vars:
                raise TemplateRegistryError(
                    f"template {self.template_id!r} multi-key key_column {key_column!r} is "
                    "not bound by any triple pattern — the empty-member claim is a statement "
                    "about GRAPH data, so its column must be graph-joined (design §2.7-F1)"
                )
            if key_column in self.args:
                raise TemplateRegistryError(
                    f"template {self.template_id!r} multi-key key_column {key_column!r} is "
                    "itself a declared argument — the column resolving which member matched "
                    "must be graph data, never a second caller-supplied value"
                )
            # The empty-member computation is a set difference between the requested
            # members and the observed values of ``key_column``. That is only sound if
            # the body actually JOINS the two — i.e. a matched row's key_column cell IS
            # the member that matched it. Pin the literal equality FILTER at load rather
            # than trusting the template author's prose.
            join = re.compile(
                rf"FILTER\s*\(\s*(?:[?$]{re.escape(key_column)}\s*=\s*[?$]{re.escape(list_name)}"
                rf"|[?$]{re.escape(list_name)}\s*=\s*[?$]{re.escape(key_column)})\s*\)",
                re.IGNORECASE,
            )
            if join.search(stripped) is None:
                raise TemplateRegistryError(
                    f"template {self.template_id!r} declares multi-key arg {list_name!r} with "
                    f"key_column {key_column!r} but its body carries no "
                    f"FILTER(?{key_column} = ?{list_name}) equality join — without it a "
                    "matched row cannot be attributed to the member that matched it, and the "
                    "empty-member set difference would be unsound"
                )

        # (d) every declared arg appears as a ?var in the (stripped) body.
        for name in self.args:
            if not re.search(rf"[?$]{re.escape(name)}\b", stripped):
                raise TemplateRegistryError(
                    f"template {self.template_id!r} declares arg {name!r} but never "
                    f"references it as ?{name} in the query body"
                )

        # (e) PROJECTION-LOCATOR (what makes the trusted textual projection rewrite
        # sound): whenever the renderer must rewrite the projection — for a count
        # template OR any count_total_rows opt-in — the body MUST be
        # ``SELECT <plain var list> WHERE {`` with the LITERAL ``WHERE`` keyword, so
        # the single ``SELECT … WHERE`` head can be located and replaced deterministically.
        if answer.kind == "count" or answer.count_total_rows:
            if _PROJECTION_LOCATOR.search(text) is None:
                raise TemplateRegistryError(
                    f"template {self.template_id!r} needs a trusted projection rewrite "
                    "(kind='count' or count_total_rows) but its body is not "
                    "'SELECT <plain ?var list> WHERE {' with a literal WHERE keyword — the "
                    "rewrite can only be applied to that exact shape (design §5.3)"
                )
            # ``?ne_total`` is the reserved alias for the trusted COUNT(*) total rewrite.
            if answer.count_total_rows and re.search(r"[?$]ne_total\b", stripped):
                raise TemplateRegistryError(
                    f"template {self.template_id!r} uses the reserved variable ?ne_total "
                    "in its body — it is reserved for the trusted total_rows COUNT(*) rewrite"
                )

        # (f) LOAD-TIME REWRITE PROBE (count kind): apply the aggregate projection
        # rewrite to the RAW sparql (the #BINDINGS# marker is still a comment) and
        # assert the rewritten query parses AND projects EXACTLY the derived columns —
        # a mis-located rewrite fails at BOOT, never silently at request time.
        if answer.kind == "count":
            probe = _rewrite_projection(text, _count_projection_head(answer))
            probe += "\nGROUP BY " + " ".join(f"?{g}" for g in answer.group_by)
            try:
                prepared_probe = prepareQuery(probe)
            except Exception as exc:
                raise TemplateRegistryError(
                    f"template {self.template_id!r} count projection rewrite does not parse "
                    f"at load (refusing): {exc}"
                ) from exc
            if _projected_vars(prepared_probe) != set(answer.columns):
                raise TemplateRegistryError(
                    f"template {self.template_id!r} count projection rewrite projects "
                    f"{sorted(_projected_vars(prepared_probe) or set())!r}, expected the "
                    f"derived columns {sorted(answer.columns)!r} — rewrite mis-located"
                )

        # (g) the generated strict args model (extra='forbid').
        self._args_model = _build_args_model(self)
        return self


def _triple_pattern_vars(prepared: object) -> set[str]:
    """Variable names that a real triple (BGP) pattern in the body joins to the graph.

    Walks rdflib's parsed algebra looking strictly for ``CompValue`` nodes carrying a
    ``"triples"`` key (the basic-graph-pattern list rdflib's SPARQL parser produces).
    This is DELIBERATELY narrow: a variable reachable only via a hand-written
    ``VALUES`` block, a ``BIND`` alias, or a SELECT expression (e.g. ``(STR(?x) AS
    ?y)``) is NOT counted as graph-joined, even though it may appear elsewhere in the
    algebra. At validation time the injected ``#BINDINGS#`` marker is still a bare
    SPARQL comment (the actual ``VALUES`` clause is injected only at RENDER time —
    see ``render_query``), so a declared arg is bound by NOTHING in THIS parse unless
    a real triple pattern names it — that is exactly the invariant this function
    proves (NE-GA-TG, grounded-agent design §2.7-F1).

    SOUNDNESS PRECONDITION: this collector unions variable names across the WHOLE
    algebra tree with no notion of branch/scope — it does not (and cannot, from
    variable names alone) tell whether a given RESULT ROW actually took the
    triple-bound path. That is only sound because ``_validate_and_build`` refuses
    ``UNION``/``MINUS``/``EXISTS`` and any nested subquery at the keyword-ban and
    SELECT-count checks above: with those constructs gone, the body is a single
    conjunctive (``Join``/``LeftJoin``) evaluation path, so "a triple somewhere
    names this variable" and "this variable's binding, whenever present, came from
    a triple" coincide. Do not relax those bans without re-deriving this function's
    correctness (Codex P1 follow-up, NE-GA-TG).
    """
    from rdflib.plugins.sparql.parserutils import CompValue
    from rdflib.term import Variable

    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, CompValue):
            for key, value in node.items():
                if key == "triples" and isinstance(value, list):
                    for triple in value:
                        for term in triple:
                            if isinstance(term, Variable):
                                found.add(str(term))
                else:
                    walk(value)
        elif isinstance(node, list | tuple):
            for item in node:
                walk(item)

    walk(getattr(prepared, "algebra", None))
    return found


def _triple_subject_vars(prepared: object) -> set[str]:
    """Variable names used as subjects by any real BGP triple."""
    from rdflib.plugins.sparql.parserutils import CompValue
    from rdflib.term import Variable

    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, CompValue):
            for key, value in node.items():
                if key == "triples" and isinstance(value, list):
                    for triple in value:
                        if triple and isinstance(triple[0], Variable):
                            found.add(str(triple[0]))
                else:
                    walk(value)
        elif isinstance(node, list | tuple):
            for item in node:
                walk(item)

    walk(getattr(prepared, "algebra", None))
    return found


def _projected_vars(prepared: object) -> set[str] | None:
    """The explicitly-projected SELECT variable names, or ``None`` for ``SELECT *``."""
    algebra = getattr(prepared, "algebra", None)
    if algebra is None:
        return None
    projection = algebra.get("PV") if hasattr(algebra, "get") else None
    if not projection:
        return None
    return {str(var) for var in projection}


#: NE-KG-MULTIKEY: the sentinel a multi-key list's uniqueness check raises. It is
#: matched by ``describe_invalid_args`` to compose a SPECIFIC refusal reason without
#: reading pydantic's ``msg`` (which is why the text itself never carries caller data).
_DUPLICATE_MEMBER_MESSAGE = "duplicate member"


def _reject_duplicate_members(values: list[Any]) -> list[Any]:
    """Refuse a multi-key list carrying the same member twice (fail closed).

    A duplicate is never silently collapsed: a repeated member would bind a second
    identical ``VALUES`` row, and — because the C7 union has BAG semantics — could
    duplicate result rows, inflating a count the caller reads as a distinct
    population. Refusing is the honest option; de-duplicating silently would mean the
    executed query no longer matches the arguments recorded on the artifact.
    """
    if len(set(values)) != len(values):
        raise ValueError(_DUPLICATE_MEMBER_MESSAGE)
    return values


#: What a ``[date_window]`` arg MEANS, published in the generated JSON Schema the
#: agent reads on every turn (NE-WINDOW-ENDPOINTS).
#:
#: THE DEFECT. ``date_window`` is enforced but was never EXPLAINED. The bounds are
#: model-supplied, the SPARQL is ``>=`` / ``<=`` on both sides, and the only place
#: that said so was the template's prose description ("an inclusive validated date
#: window") — which states that the ENDPOINTS match without ever saying how to
#: COMPUTE the far endpoint. Live, the flagship multi-hop question answered with one
#: subject too few and gave its reason in the answer: a row due on day N of an
#: N-day window was called "outside window". The bound was off by one day, and
#: everything downstream of it — the population, the count, the recommendation —
#: was confidently wrong while quoting the correct date.
#:
#: WHY HERE. An off-by-one is a question about the CONTRACT of an argument, so it is
#: answered in that argument's schema rather than in prose the model may or may not
#: have retrieved, or in a system prompt every other run also pays for. It reaches
#: exactly the templates that declare a window, on the turn the model is choosing
#: the value, and it costs nothing anywhere else.
#:
#: WHAT IT DOES NOT DO. It states no span, no reference date and no subject: N and D
#: are the caller's, and a template that wants a different span still declares its
#: own ``max_days``. It is arithmetic, not an answer.
_WINDOW_START_DESCRIPTION = (
    "First date in the window. INCLUSIVE: a row dated exactly this day is returned."
)
_WINDOW_END_DESCRIPTION = (
    "Last date in the window. INCLUSIVE: a row dated exactly this day is returned. "
    "A window of N days beginning on date D therefore ENDS ON D+N, not D+N-1: count "
    "N days forward from the first date and do not subtract one. Span at most {max_days} days."
)


def _build_args_model(template: QueryTemplate) -> type[BaseModel]:
    """Generate the strict (``extra='forbid'``) Pydantic model validating the args.

    NE-KG-MULTIKEY: an arg declaring ``list_max`` generates a UNION field —
    ``<scalar> | list[<scalar>]`` — whose list branch carries the SAME per-element
    constraint object as the scalar branch. That is the mechanism guaranteeing "per-row
    validation identical in strictness to the single-value validation": there is one
    constraint, used twice, so the two can never drift. The list branch adds only the
    cardinality bounds (``min_length=1``, ``max_length=list_max``) and the
    uniqueness check. Every other arg generates EXACTLY the annotation it generated
    before this feature existed, so the advertised schema and the accepted values for
    a scalar-only template are unchanged.
    """
    from datetime import date, datetime
    from typing import Annotated

    from pydantic import AfterValidator, StringConstraints

    fields: dict[str, object] = {}
    for name, arg in template.args.items():
        annotation: object
        if arg.enum is not None:
            # A closed vocabulary generates a Literal field: pydantic enforces it
            # strictly AND ``model_json_schema`` advertises ``"enum": [...]`` in the
            # ToolSpec input_schema the agent sees — the one source of truth for
            # both the advertisement and the refusal (SPEC §12 item 3).
            annotation = Literal[arg.enum]  # type: ignore[valid-type]
        elif arg.type in _PATTERNABLE:
            annotation = Annotated[
                str, StringConstraints(pattern=arg.pattern, max_length=arg.max_length)
            ]
        elif arg.type == "int":
            annotation = int
        elif arg.type == "bool":
            annotation = bool
        elif arg.type == "date":
            annotation = date
        else:  # datetime
            annotation = datetime
        if arg.list_max is not None:
            # SAME element annotation, reused verbatim — one strictness source.
            annotation = (
                annotation
                | Annotated[  # type: ignore[operator]
                    list[annotation],  # type: ignore[valid-type]
                    Field(min_length=1, max_length=arg.list_max),
                    AfterValidator(_reject_duplicate_members),
                ]
            )
        # TWO description channels, one schema property.
        #
        # NE-CAPTAB: ``arg.description`` is the AUTHOR-written semantics declared in
        # the template TOML — what the value means, its format, and any cross-field
        # rule. It reaches both consumers of the one generated schema: the model (via
        # the in-band tool catalog) and the console's Capabilities panel (via the
        # capability preflight projection, which reads this schema, not the TOML).
        #
        # NE-WINDOW-ENDPOINTS: a declared window's two endpoint args publish the
        # GENERATED arithmetic INSTEAD, and it wins over any author text on those two
        # args. That precedence is the point: the generated clause is derived from the
        # declared ``max_days`` and states the D+N rule the live off-by-one turned on,
        # so it cannot drift from the cap the validator actually enforces the way
        # hand-written prose can. An author description on an endpoint arg stays in
        # the TOML as the declared fallback and is superseded while a window is
        # declared.
        #
        # Every other arg is byte-identical to before unless its author declared a
        # description: ``description=None`` emits no key at all.
        description = arg.description
        window = template.date_window
        if window is not None:
            if name == window.start_arg:
                description = _WINDOW_START_DESCRIPTION
            elif name == window.end_arg:
                description = _WINDOW_END_DESCRIPTION.format(max_days=window.max_days)
        # ``...`` ⇒ required (all args required in v1).
        fields[name] = (annotation, Field(..., description=description))
    return create_model(
        f"{template.template_id.replace('-', '_')}_Args",
        __config__=ConfigDict(extra="forbid"),
        **fields,  # type: ignore[call-overload]
    )


def describe_invalid_args(template: QueryTemplate, exc: Exception) -> str:
    """The C12-safe args-refusal message: fields + constraints, NEVER the rejected value.

    THE single composer for every surface that refuses invalid verified-answer args
    (the pre-gate ``validate_args`` seam and ``execute_template``'s defense-in-depth
    re-validation) — one message policy, not two that drift. It interprets the
    generated args model's :class:`pydantic.ValidationError` against the template's
    :class:`ArgDef`\\s:

    * every part names the offending FIELD — and only ever a DECLARED one. An
      UNDECLARED key is caller data (pydantic puts the supplied key in ``loc`` for
      ``extra_forbidden``, unbounded and injection-capable), so undeclared keys are
      COUNTED and the declared vocabulary is named instead, never the supplied key;
    * an enum violation lists the PERMITTED values — safe because a declared enum is
      already published in the advertised tool schema (see :class:`ArgDef`), so the
      refusal repeats schema, never data;
    * the REJECTED value is never echoed (pydantic's own rendering embeds
      ``input_value=...`` — this composer exists so that text never reaches a model,
      a user, or a log via this refusal). The composed MESSAGE is only half the
      discipline: every raise site that wraps the ``ValidationError`` also severs
      the exception chain (``raise ... from None``), because a chained cause
      resurfaces ``input_value=`` in ``traceback.format_exception`` output — the
      text any ``logger.exception`` / Sentry / OTel ``record_exception`` would
      ship. Both halves are chain-swept by test;
    * the closing sentence states that no query ran, so a refusal can never be read
      as an empty result (SPEC §12 item 3 — the two must be distinguishable).
    """
    from pydantic import ValidationError

    parts: list[str] = []
    undeclared = 0
    #: Multi-key union branches that failed only on their bare TYPE — surfaced as a
    #: shape message iff no more specific finding was recorded for that argument.
    branch_noise: dict[str, ArgDef] = {}
    if isinstance(exc, ValidationError):
        for error in exc.errors(include_url=False, include_context=False, include_input=False):
            loc = error.get("loc") or ()
            kind = str(error.get("type", "invalid"))
            name = str(loc[0]) if loc else ""
            arg = template.args.get(name) if loc else None
            # C12 — ``loc`` IS CALLER DATA when the key is undeclared. For
            # ``extra_forbidden`` pydantic reports the SUPPLIED key, which is
            # model/caller text of unbounded length and can carry an injection
            # payload; echoing it would reflect caller content into model context
            # (the agent lane renders this message as ``BLOCKED: ...``) and into
            # MCP client output, in the same sentence that claims values are not
            # echoed. So a name is echoed ONLY when it is a DECLARED argument —
            # published schema — and everything else is COUNTED, never quoted.
            # The test is membership in ``template.args``, not the error ``kind``,
            # so a future pydantic error type carrying a caller-controlled ``loc``
            # is covered the day it appears rather than the day someone notices.
            if loc and arg is None:
                undeclared += 1
                continue
            if arg is not None and arg.list_max is not None:
                # NE-KG-MULTIKEY. A multi-key arg is a UNION field, so pydantic reports
                # BOTH branches: the scalar branch's bare type mismatch AND the real
                # finding inside the list branch. ``loc`` also carries pydantic's
                # generated union tag (``constrained-str`` / ``function-after[...]``),
                # which is SCHEMA text, not caller data, but is noise in a refusal the
                # model has to act on. So list-arg errors are composed separately:
                # the branch-only mismatches are buffered and emitted ONLY if nothing
                # more specific was found, and a per-member finding names the member's
                # INDEX (a position — never the rejected value, C12).
                index = next((piece for piece in loc[1:] if isinstance(piece, int)), None)
                member = f"{name!r} member #{index}" if index is not None else repr(name)
                if kind in {"string_type", "list_type"}:
                    branch_noise.setdefault(name, arg)
                elif kind == "too_long":
                    parts.append(
                        f"{name!r} supplies more than the declared cardinality cap of "
                        f"{arg.list_max} values"
                    )
                elif kind == "too_short":
                    parts.append(f"{name!r} is an empty list (supply at least one value)")
                elif kind == "value_error":
                    # The ONLY AfterValidator on the list branch is the uniqueness
                    # check, so ``value_error`` here is unambiguous — composed from
                    # that fact rather than by reading pydantic's message (C12).
                    parts.append(f"{name!r} repeats a value (list members must be distinct)")
                elif kind == "literal_error" and arg.enum is not None:
                    parts.append(f"{member} must be one of: {', '.join(arg.enum)}")
                elif kind == "string_pattern_mismatch":
                    parts.append(f"{member} does not match the declared pattern")
                elif kind == "string_too_long":
                    parts.append(f"{member} exceeds max_length={arg.max_length}")
                elif kind == "missing":
                    parts.append(f"{name!r} is required")
                else:
                    parts.append(f"{member} failed {kind} validation")
                continue
            field = ".".join(str(piece) for piece in loc) if loc else "(arguments)"
            if kind == "literal_error" and arg is not None and arg.enum is not None:
                permitted = ", ".join(arg.enum)
                parts.append(f"{field!r} must be one of: {permitted}")
            elif kind == "missing":
                parts.append(f"{field!r} is required")
            elif kind == "string_pattern_mismatch" and arg is not None:
                parts.append(f"{field!r} does not match the declared pattern")
            elif kind == "string_too_long" and arg is not None:
                parts.append(f"{field!r} exceeds max_length={arg.max_length}")
            else:
                parts.append(f"{field!r} failed {kind} validation")
        for noisy_name, noisy_arg in branch_noise.items():
            if any(part.startswith(f"'{noisy_name}'") for part in parts):
                continue
            parts.append(
                f"{noisy_name!r} must be a single value or a list of at most "
                f"{noisy_arg.list_max} values"
            )
        if undeclared:
            # Names the COUNT plus the DECLARED vocabulary (already advertised in the
            # tool schema every turn), never the supplied keys.
            declared = ", ".join(sorted(template.args)) or "(none)"
            noun = "argument" if undeclared == 1 else "arguments"
            parts.append(
                f"{undeclared} undeclared {noun} supplied (names not echoed); "
                f"the declared arguments are: {declared}"
            )
    else:  # non-ValidationError crash: class name only (C12), never its message.
        # This branch belongs to ``isinstance(exc, ValidationError)``, NOT to the
        # undeclared-count above: validation that RAN and produced findings must never
        # be reported as validation that could not run.
        parts.append(f"argument validation could not run ({type(exc).__name__})")
    detail = "; ".join(parts) or "argument validation failed"
    return (
        f"invalid arguments for {template.tool_name!r}: {detail}. "
        "The supplied values are not echoed (C12). No query was executed — "
        "this is a refusal, not an empty result."
    )


def _n3_term(arg_type: str, value: object) -> str:
    """Serialize a validated arg value to an RDF term via rdflib ``.n3()`` (never text)."""
    from rdflib import Literal, URIRef
    from rdflib.namespace import XSD

    if arg_type == "iri":
        text = str(value)
        if _IRI_FORBIDDEN.search(text):
            raise TemplateRegistryError(
                f"iri arg value {text!r} contains a forbidden character "
                '(<>"{}|^`\\ or whitespace) — refused before binding'
            )
        return URIRef(text).n3()
    if arg_type == "int":
        return Literal(str(int(value)), datatype=XSD.integer).n3()  # type: ignore[arg-type]
    if arg_type == "bool":
        return Literal(bool(value), datatype=XSD.boolean).n3()
    if arg_type == "date":
        return Literal(value, datatype=XSD.date).n3()
    if arg_type == "datetime":
        return Literal(value, datatype=XSD.dateTime).n3()
    return Literal(str(value)).n3()  # str → plain (RDF 1.1 xsd:string) literal


def effective_limit(template: QueryTemplate, max_rows: int) -> int:
    """The trusted row cap: ``min(template cap if declared else settings cap, settings cap)``."""
    declared = template.max_rows if template.max_rows is not None else max_rows
    return min(declared, max_rows)


#: Locates the single ``SELECT <plain ?var list> WHERE`` head of a validated template
#: body so trusted code can rewrite ONLY the projection (the body BGP + FILTERs are
#: left byte-untouched). Sound because a count/count_total_rows body is load-validated
#: to exactly this shape (``_validate_and_build`` step (e)).
_PROJECTION_LOCATOR = re.compile(
    r"\bSELECT\b\s+((?:[?$][A-Za-z_]\w*\s+)*[?$][A-Za-z_]\w*)\s+WHERE\b",
    re.IGNORECASE,
)


def _rewrite_projection(text: str, new_head: str) -> str:
    """Replace the single ``SELECT <var list> WHERE`` head with ``<new_head> WHERE``.

    ``new_head`` already carries the ``SELECT`` keyword and the trusted projection
    (e.g. ``SELECT ?a ?b (COUNT(DISTINCT ?x) AS ?n)``). Only the projection is
    replaced; the ``WHERE`` keyword and everything after it (the BGP the body author
    wrote, now with its ``VALUES`` binding) are preserved verbatim.
    """
    return _PROJECTION_LOCATOR.sub(lambda _m: f"{new_head} WHERE", text, count=1)


def _count_projection_head(answer: AnswerSpec) -> str:
    """The trusted aggregate projection head for a count template (design §5.3).

    ``COUNT(DISTINCT …)`` is MANDATORY: the C7 multi-graph union has BAG semantics on
    oxigraph — a multi-label entity's triples are duplicated per ACL graph, so a plain
    ``COUNT(?count_over)`` over-counts (a 2-order account visible via 2 ACL graphs was
    spike-verified to count as 8). ``DISTINCT`` recovers the true per-group cardinality.
    """
    group_clause = " ".join(f"?{g}" for g in answer.group_by)
    prefix = f"{group_clause} " if group_clause else ""
    return f"SELECT {prefix}(COUNT(DISTINCT ?{answer.count_over}) AS ?{answer.count_as})"


def list_members(template: QueryTemplate, validated_args: Mapping[str, object]) -> list[str] | None:
    """The multi-key members of THIS call, or ``None`` when it is a scalar call.

    THE single discriminator for "was this a list call?", shared by the renderer and
    the executor so they can never disagree. It returns ``None`` — the scalar path,
    byte-identical to pre-NE-KG-MULTIKEY behaviour — whenever the template declares no
    multi-key argument OR the caller supplied that argument as a single value. A
    one-element ARRAY is a list call (``[...]`` is caller intent, not a scalar), which
    is what keeps the parity rule crisp: *no array in, nothing changes*.
    """
    name = template.list_arg
    if name is None:
        return None
    value = validated_args.get(name)
    if not isinstance(value, list):
        return None
    return [str(member) for member in value]


def _bound_body(template: QueryTemplate, validated_args: Mapping[str, object]) -> str:
    """The template body with its ``#BINDINGS#`` marker replaced by the ``VALUES`` block.

    Shared by ``render_query`` (rows + count) and ``render_count_query`` so the exact
    same bound body underlies the answer rows, the trusted ORDER BY, and the
    total_rows COUNT — the ``kind='rows'`` output stays BYTE-IDENTICAL (frozen goldens).
    """
    names = sorted(template.args)
    members = list_members(template, validated_args)
    if names and members is not None:
        # NE-KG-MULTIKEY: ONE ``VALUES`` block, one bound ROW PER MEMBER — the scalar
        # args are repeated verbatim on every row. Members are rendered in SORTED order
        # so the query TEXT is a pure function of the member SET (a caller that permutes
        # its list gets a byte-identical query); result ORDER is owned by the trusted
        # ORDER BY below either way, so this is determinism, not semantics.
        list_name = template.list_arg
        assert list_name is not None  # narrowing: list_members returned a member list
        vars_clause = " ".join(f"?{name}" for name in names)
        rendered_rows = []
        for member in sorted(members):
            row = " ".join(
                _n3_term(
                    template.args[name].type,
                    member if name == list_name else validated_args[name],
                )
                for name in names
            )
            rendered_rows.append(f"({row})")
        binding = f"VALUES ({vars_clause}) {{ {' '.join(rendered_rows)} }}"
    elif names:
        vars_clause = " ".join(f"?{name}" for name in names)
        row = " ".join(_n3_term(template.args[name].type, validated_args[name]) for name in names)
        binding = f"VALUES ({vars_clause}) {{ ({row}) }}"
    else:
        binding = ""
    lines = [
        binding if line.strip() == BINDINGS_MARKER else line
        for line in template.sparql.splitlines()
    ]
    return "\n".join(lines)


def render_query(
    template: QueryTemplate, validated_args: Mapping[str, object], max_rows: int
) -> str:
    """Render the final executable query: ``VALUES`` binding + trusted ORDER BY + ``LIMIT``.

    ``validated_args`` are the TYPED outputs of the generated args model (a ``date``
    is a ``date``, an ``int`` is an ``int``, …). Each is serialized via rdflib
    ``.n3()`` so a malicious value can only ever be an RDF literal, never syntax. The
    FINAL rendered query is re-parsed with ``prepareQuery`` before returning (fail
    closed — an escape that produced un-parseable text is refused, not executed).

    ``kind='count'`` (NE-CTXB-7): the plain-var projection is rewritten to the trusted
    ``SELECT ?g1 ?g2 (COUNT(DISTINCT ?<count_over>) AS ?<as>)`` + ``GROUP BY ?g1 ?g2``,
    then the SAME trusted ORDER BY (citation first) and ``LIMIT`` (which bounds GROUP
    rows for a count template). ``kind='rows'`` output is unchanged (byte-identical).
    """
    body = _bound_body(template, validated_args)
    limit = effective_limit(template, max_rows)
    answer = template.answer

    if answer.kind == "count":
        body = _rewrite_projection(body, _count_projection_head(answer))
        body += "\nGROUP BY " + " ".join(f"?{g}" for g in answer.group_by)

    # Trusted, deterministic ORDER BY (§4.4, R2-P0-2): citation var FIRST, then the
    # remaining declared columns (in declared order), all ascending. This makes the
    # kept prefix under truncation engine-plan-INDEPENDENT — the prerequisite for
    # honest truncation, replay determinism (answers_equal), and cross-seam parity.
    # ORDER BY / OFFSET stay BANNED in template BODIES (trusted code owns ordering);
    # this clause is appended AFTER the body, BEFORE the trusted LIMIT. For a count
    # template the count alias is a derived column, so it participates in the ordering.
    order_vars = [answer.citation_var] + [
        col for col in answer.columns if col != answer.citation_var
    ]
    order_by = "ORDER BY " + " ".join(f"?{var}" for var in order_vars)
    # Fetch limit+1 so the handler can detect (and flag) row-cap truncation.
    query = body + f"\n{order_by}" + f"\nLIMIT {limit + 1}"

    from rdflib.plugins.sparql import prepareQuery

    try:
        prepareQuery(query)
    except Exception as exc:
        raise TemplateRegistryError(
            f"rendered query for {template.template_id!r} failed to parse (refusing to "
            f"execute): {exc}"
        ) from exc
    return query


def render_count_query(template: QueryTemplate, validated_args: Mapping[str, object]) -> str:
    """Render the trusted ``total_rows`` COUNT(*) query — the degenerate aggregate case.

    A SECOND, unsnapshotted read (AS-OF-COUNT-TIME): it counts what a FULL enumeration
    of the SAME bound body over the SAME C7 dataset would return, projecting a single
    ``?ne_total``. No ORDER BY / LIMIT (a scalar). Re-parsed before returning (fail closed).

    * ``kind='rows'``  → ``SELECT (COUNT(*) AS ?ne_total) WHERE { <bound body> }`` — the
      bag-solution total (== what a full enumeration returns; on a multi-label view the
      union has bag semantics, which is exactly the enumeration cardinality being paged).
    * ``kind='count'`` → ``SELECT (COUNT(*) AS ?ne_total) WHERE { { SELECT DISTINCT ?g1 …
      WHERE { <bound body> } } }`` — the number of GROUPS (distinct group-key tuples).
      The inner subquery is TRUSTED-rendered: the load-time SELECT-count/subquery bans
      apply to template BODIES, never to this trusted rewrite.
    """
    answer = template.answer
    body = _bound_body(template, validated_args)
    if answer.kind == "count":
        inner_head = "SELECT DISTINCT " + " ".join(f"?{g}" for g in answer.group_by)
        inner = _rewrite_projection(body, inner_head)
        query = f"SELECT (COUNT(*) AS ?ne_total) WHERE {{ {{ {inner} }} }}"
    else:
        query = _rewrite_projection(body, "SELECT (COUNT(*) AS ?ne_total)")

    from rdflib.plugins.sparql import prepareQuery

    try:
        prepareQuery(query)
    except Exception as exc:
        raise TemplateRegistryError(
            f"total_rows COUNT query for {template.template_id!r} failed to parse "
            f"(refusing to execute): {exc}"
        ) from exc
    return query


class TemplateRegistryFingerprint(BaseModel):
    """The registry freeze input: a sha256 over every template file + its id@version."""

    model_config = ConfigDict(frozen=True)

    registry_hash: str
    template_ids: tuple[str, ...]


class TemplateRegistry(BaseModel):
    """The loaded, load-validated set of verified-answer templates + its fingerprint."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    templates: tuple[QueryTemplate, ...]
    fingerprint: TemplateRegistryFingerprint


def parse_template_toml(text: str) -> QueryTemplate:
    """Parse + FULLY validate one template TOML string into a :class:`QueryTemplate`.

    The SINGLE validation gate shared by boot loading (``load_template_registry``),
    the governed HITL overlay (``load_template_registry_with_overlay``), and the
    ne-api propose/apply seam — so a steward-authored template is validated at
    propose-time with the EXACT same SELECT-only / banned-keyword / typed-arg /
    citation-projection gate as a package-shipped one (fail-closed; an invalid
    template can never become a proposal or reach the store). A TOML decode error
    OR any validation failure is wrapped in :class:`TemplateRegistryError`.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TemplateRegistryError(f"template is not valid TOML: {exc}") from exc
    try:
        return QueryTemplate.model_validate(data)
    except TemplateRegistryError:
        raise
    except Exception as exc:
        raise TemplateRegistryError(f"template failed validation: {exc}") from exc


def load_template_registry(directory: Path) -> TemplateRegistry:
    """Load + FULLY validate every ``*.toml`` template under ``directory`` (fail loud).

    Duplicate ``template_id``\\s across files fail; every per-file validation lives in
    :class:`QueryTemplate` (SELECT-only, banned-keyword gate, marker, arg-var presence,
    citation projection, generated strict args model). The registry hash covers the raw
    file bytes AND the ``id@vN`` set, so ANY byte change re-fingerprints the config.
    """
    if not directory.is_dir():
        raise TemplateRegistryError(
            f"verified_templates_dir {directory} is not a directory (verified answers are "
            "enabled but no template bundle is present — fail loud, never an empty registry)"
        )
    files = sorted(directory.glob("*.toml"))
    if not files:
        raise TemplateRegistryError(
            f"verified_templates_dir {directory} contains no *.toml templates (verified "
            "answers are enabled but the bundle is empty — fail loud, never an empty registry)"
        )
    templates: list[QueryTemplate] = []
    seen: dict[str, str] = {}
    hasher = hashlib.sha256()
    for path in files:
        raw = path.read_bytes()
        hasher.update(path.name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(raw)
        hasher.update(b"\0")
        try:
            template = parse_template_toml(raw.decode("utf-8"))
        except TemplateRegistryError as exc:
            raise TemplateRegistryError(f"template file {path.name}: {exc}") from exc
        if template.template_id in seen:
            raise TemplateRegistryError(
                f"duplicate template_id {template.template_id!r} in {path.name} "
                f"(already defined in {seen[template.template_id]})"
            )
        seen[template.template_id] = path.name
        templates.append(template)

    ids = tuple(f"{t.template_id}@v{t.version}" for t in templates)
    for token in sorted(ids):
        hasher.update(token.encode("utf-8"))
        hasher.update(b"\0")
    fingerprint = TemplateRegistryFingerprint(
        registry_hash=hasher.hexdigest(),
        template_ids=tuple(t.template_id for t in templates),
    )
    return TemplateRegistry(templates=tuple(templates), fingerprint=fingerprint)


def load_template_registry_with_overlay(
    directory: Path, stored: Sequence[StoredVerifiedTemplate]
) -> TemplateRegistry:
    """Load the boot bundle, then OVERLAY the governed HITL template store.

    The effective registry = per ``template_id`` the HIGHEST version wins across
    the boot bundle unioned with the governed store (NE-ONTOLOGY-FLYWHEEL). Every stored TOML
    is re-validated through :func:`parse_template_toml` (fail-loud — a tampered
    store row can never boot a weaker registry). A stored ``(id, version)`` equal
    to a boot pair with an IDENTICAL sha256 is ignored (boot wins); with a
    DIFFERENT sha it raises :class:`TemplateRegistryError` (fail-loud divergence).

    Fingerprint recipe (a strict SUPERSET of ``load_template_registry``): the boot
    files are hashed EXACTLY as today; then each surviving stored row (sorted by
    ``(template_id, version)``) is hashed as ``store:<id>@v<version>`` + NUL + TOML
    bytes + NUL; then the sorted ``id@vN`` tokens of the EFFECTIVE set. INVARIANT:
    ``stored == ()`` ⇒ the fingerprint is BYTE-IDENTICAL to
    ``load_template_registry(directory)`` (the enabled-no-HITL-rows parity anchor).
    """
    # (1) Boot bundle — the identical file loop as load_template_registry, plus a
    # per-(id, version) sha256 for the boot-vs-store divergence gate.
    boot = load_template_registry(directory)
    hasher = hashlib.sha256()
    boot_pair_sha: dict[tuple[str, int], str] = {}
    for path in sorted(directory.glob("*.toml")):
        raw = path.read_bytes()
        hasher.update(path.name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(raw)
        hasher.update(b"\0")
        parsed = parse_template_toml(raw.decode("utf-8"))
        boot_pair_sha[(parsed.template_id, parsed.version)] = hashlib.sha256(raw).hexdigest()

    # (2) Overlay the governed store rows (sorted by (template_id, version)).
    effective: dict[str, QueryTemplate] = {t.template_id: t for t in boot.templates}
    order: list[str] = [t.template_id for t in boot.templates]
    ordered_store = sorted(stored, key=lambda r: (r.template_id, r.version))
    survived: list[StoredVerifiedTemplate] = []
    for row in ordered_store:
        template = parse_template_toml(row.template_toml)
        if template.template_id != row.template_id or template.version != row.version:
            raise TemplateRegistryError(
                f"stored template row {row.template_id!r}@v{row.version} disagrees with its "
                f"parsed TOML ({template.template_id!r}@v{template.version}) — refusing to load"
            )
        sha = hashlib.sha256(row.template_toml.encode("utf-8")).hexdigest()
        pair = (row.template_id, row.version)
        if pair in boot_pair_sha:
            if boot_pair_sha[pair] == sha:
                continue  # identical to a boot template — boot wins, ignore.
            raise TemplateRegistryError(
                f"stored template {row.template_id!r}@v{row.version} diverges from the boot "
                f"bundle at the same (id, version) — fail-loud (one body per slot)"
            )
        survived.append(row)
        tid = template.template_id
        if tid in effective:
            if template.version > effective[tid].version:
                effective[tid] = template
        else:
            effective[tid] = template
            order.append(tid)

    # (3) Hash the surviving store rows (already (id, version)-sorted).
    for row in survived:
        hasher.update(f"store:{row.template_id}@v{row.version}".encode())
        hasher.update(b"\0")
        hasher.update(row.template_toml.encode("utf-8"))
        hasher.update(b"\0")

    # (4) Effective set + the sorted id@vN tokens (boot-order preserved for parity).
    effective_templates = tuple(effective[tid] for tid in order)
    tokens = tuple(f"{t.template_id}@v{t.version}" for t in effective_templates)
    for token in sorted(tokens):
        hasher.update(token.encode("utf-8"))
        hasher.update(b"\0")
    fingerprint = TemplateRegistryFingerprint(
        registry_hash=hasher.hexdigest(),
        template_ids=tuple(t.template_id for t in effective_templates),
    )
    return TemplateRegistry(templates=effective_templates, fingerprint=fingerprint)
