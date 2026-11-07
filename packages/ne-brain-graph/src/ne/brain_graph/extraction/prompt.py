# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The DERIVED extraction system-prompt schema block (NE-T2X-PROMPT-SCHEMA).

**The defect this module fixes.** The shipped ``_build_request`` system
message ended with the sentence "Never invent an entity_class, edge_kind, or
property outside the schema you were given" — but no schema was ever given.
The prompt passed only the ``Candidates (class|key|display)`` lines and the
chunk text; the assertion envelope the validation wall (``job.py``'s
``_validate_wall``/``_handle_link``/``_handle_new_entity``/``_handle_new_edge``)
actually requires — ``kind``, ``confidence``, and the per-kind required
fields — was never stated. A real routed model (Qwen2.5-32B-Instruct-AWQ on
the first live rig run) therefore invented a reasonable-looking but wrong
shape, and every assertion was quarantined: zero valid extraction batches.

**The fix, and why it can be trusted.** :func:`render_extraction_system_prompt`
DERIVES the schema block from the wall's own authorities —
:class:`~ne.flywheel.proposals.GraphExtractionAssertionKind` for the three
``kind`` values, :data:`~ne.schema.ENTITY_CONTRACTS` (minus
:data:`~ne.brain_graph.extraction.writer._PLATFORM_OWNED_KEYS`, the writer's
own strip-list) for entity classes and their properties, and
:data:`~ne.schema.EDGE_DEFS` for the allowed edge triples — rather than
hand-writing a second copy of the wall's requirements that could drift out of
sync with it (that exact drift is what shipped the defect). The one thing
that is NOT mechanically derivable — the per-kind field lists themselves
(which fields go with ``link`` vs ``new_entity`` vs ``new_edge``) — is pinned
by a closed-loop test that proves the prompt and the wall agree:
``packages/ne-brain-graph/tests/test_extraction_prompt_wall_contract.py``'s
``test_prompt_example_block_round_trips_through_the_wall`` parses the
example JSON block straight out of this module's OWN rendered prompt and
feeds it through the real :class:`~ne.brain_graph.extraction.job.ExtractionJob`
end to end; a drift on either side of the contract turns that test red. THAT
is the test that makes this module's closing sentence — "Never emit an
entity_class, edge_kind, or property outside the schema above" — true.

**Confidence is never defaulted.** The wall requires every assertion to carry
a numeric ``confidence`` and quarantines silently when it is absent — this
module demands it explicitly rather than the job synthesizing one, because
``extraction_min_confidence``/``extraction_auto_link_threshold`` both route on
that value; a synthesized default would make both thresholds meaningless.
Pinned by ``test_example_missing_confidence_is_quarantined``.

**Field constraints are advertised too, not just types.** A field's pydantic
``Field(pattern=..., ge=..., ...)`` metadata (e.g. ``Order.order_number``'s
``^ORD-[0-9]{4,}$`` pattern, ``Account.employee_count``'s ``ge=0``) is exactly
what :func:`~ne.brain_graph.extraction.writer.validate_extracted_properties`
enforces via ``ENTITY_CONTRACTS[entity_class].model_validate`` -- a model told
only "string" for ``order_number`` has no way to know a non-conforming value
will be quarantined, which is the same shape of defect this module fixes for
``kind``/``confidence``. :func:`_constraint_label` DERIVES the note from each
field's own ``FieldInfo.metadata`` (Argus C1 on the prior build) rather than a
hand-written per-field list.

**The worked example now honours those same constraints, not just states
them.** :func:`_sample_value` walks the identical ``FieldInfo.metadata``
:func:`_constraint_label` advertises and derives a value that actually
SATISFIES it: pattern fields get a deterministic shortest-match value found
by :func:`_sample_for_pattern` (a breadth-first search over a per-pattern
alphabet using the ``regex`` package's partial-match API), and numeric/length
bounds are clamped. An unsatisfiable-in-bounds constraint fails CLOSED
(``ValueError``) rather than silently shipping a self-contradicting example.
Before this fix, ``_sample_value`` ignored constraint metadata entirely, and
the worked example was correct only by the incidental accident that
``example_triple()``'s ``EDGE_DEFS`` sort order happens to pick ``Document``
(no constrained fields) as the subject class; ``Order``'s and ``Person``'s
example properties failed their own validators (Argus P3 on the prior
build). ``test_every_entity_class_example_properties_pass_the_wall`` pins
EVERY entity class's example properties (required and required+optional)
against the real ``validate_extracted_properties`` wall, so this can never
again be correct by luck.

**Two fields are deliberately left unadvertised**: ``created_at``/
``updated_at`` are wall-ACCEPTED (they are not in ``_PLATFORM_OWNED_KEYS``)
but this module does not tell the model it may set them — they are
record-provenance fields a model reading one document chunk has no grounds to
assert, and advertising them would invite fabrication (A-P0-2 minimization).

**The per-kind field vocabulary has exactly ONE copy.** ``_LINK_REQUIRED``/
``_NEW_ENTITY_REQUIRED``/``_NEW_EDGE_REQUIRED``/``_NEW_EDGE_ENDPOINT_HINTS``
are the SOLE source of the per-kind field names; both the hand-written prose
sentences in :func:`render_extraction_system_prompt` and the worked example
entries in :func:`schema_example_entries` derive their field names from
these same constants. Before this fix the prose sentences hand-typed the
same names the example independently derived, and only the example copy was
wall-pinned -- a rename in the prose alone (e.g. ``entity_key`` ->
``entity_id``) left every existing test green (Argus P2 on the prior build).
``test_prompt_states_every_wall_vocabulary_item`` now runs its containment
pins against the prompt with the example JSON block STRIPPED OUT, so a
prose-only rename turns it red.
"""

from __future__ import annotations

import json
import types
import typing
from collections import deque
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import regex
from pydantic.fields import FieldInfo

from ne.brain_graph.extraction.writer import _PLATFORM_OWNED_KEYS
from ne.flywheel.proposals import GraphExtractionAssertionKind
from ne.schema import EDGE_DEFS, ENTITY_CONTRACTS
from ne.schema.generated.operational_graph import EdgeDef

__all__ = [
    "EXAMPLE_EXISTING_KEY",
    "example_properties",
    "example_triple",
    "render_extraction_system_prompt",
    "schema_example_entries",
]

#: Wall-accepted (not in ``_PLATFORM_OWNED_KEYS``) but deliberately NOT
#: advertised in the prompt: record-provenance timestamps a model reading one
#: chunk has no grounds to assert (A-P0-2 minimization) — see module
#: docstring.
_UNADVERTISED_OPTIONAL: frozenset[str] = frozenset({"created_at", "updated_at"})

#: The candidate key used in every schema example that demonstrates
#: candidate-key resolution (a ``link``, and a ``new_edge`` endpoint resolved
#: WITHOUT a same-response class hint).
EXAMPLE_EXISTING_KEY = "existing-key-1"

#: THE ONE COPY of the per-kind field vocabulary (Argus P2 on the prior
#: build). Both the hand-written prose sentences in
#: :func:`render_extraction_system_prompt` AND the worked example entries in
#: :func:`schema_example_entries` DERIVE their field names from these
#: constants — there is no second, independently-typed copy for either to
#: drift out of sync with (that drift, at the prose/example scale, is
#: exactly what shipped Argus's ``entity_key``->``entity_id`` /
#: ``display``->``label`` prose-only mutation undetected). Wall-pinned by
#: ``test_extraction_prompt_wall_contract.py``'s round-trip test (the
#: example) and its re-scoped ``test_prompt_states_every_wall_vocabulary_item``
#: (the prose, with the example block stripped out first).
_UNIVERSAL_REQUIRED: tuple[str, str] = ("kind", "confidence")
_KIND_FIELD, _CONFIDENCE_FIELD = _UNIVERSAL_REQUIRED
_LINK_REQUIRED: tuple[str, ...] = ("entity_key",)
_NEW_ENTITY_REQUIRED: tuple[str, ...] = ("entity_class", "display", "properties")
_NEW_EDGE_REQUIRED: tuple[str, ...] = ("edge_kind", "subject_key", "object_key")
_NEW_EDGE_ENDPOINT_HINTS: tuple[str, str] = ("subject_class", "object_class")
_SUBJECT_CLASS_HINT, _OBJECT_CLASS_HINT = _NEW_EDGE_ENDPOINT_HINTS


def _quoted_list(values: Sequence[str], conjunction: str) -> str:
    """``'"a"'`` / ``'"a" and "b"'`` / ``'"a", "b", and "c"'`` (Oxford comma) --
    the single joiner used by every DERIVED prose/example field-name list so
    the two can never diverge in punctuation either."""
    quoted = [f'"{v}"' for v in values]
    if len(quoted) == 1:
        return quoted[0]
    if len(quoted) == 2:
        return f"{quoted[0]} {conjunction} {quoted[1]}"
    return ", ".join(quoted[:-1]) + f", {conjunction} " + quoted[-1]


def _unwrap_optional(ann: Any) -> Any:
    """Unwrap ``X | None`` (or ``typing.Optional[X]``) to ``X``; pass through
    any other annotation unchanged."""
    origin = typing.get_origin(ann)
    if origin is types.UnionType or origin is typing.Union:
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        if args:
            return args[0]
    return ann


def _type_label(ann: Any) -> str:
    """A short, human-readable label for a (already-unwrapped) field type."""
    if isinstance(ann, type) and issubclass(ann, Enum):
        return "one of " + "|".join(f'"{m.value}"' for m in ann)
    if ann is bool:
        return "boolean"
    if ann is str:
        return "string"
    if ann is int:
        return "integer"
    if ann is float or ann is Decimal:
        return "number"
    if ann is datetime:
        return "ISO-8601 datetime string"
    return "string"


#: Fixed-order candidate alphabet for the BFS pattern search below — the
#: basis covers what most schema patterns need (letters, digits, common
#: punctuation); pattern-specific characters (e.g. class members inside
#: ``[A-Z]``) are appended in sorted order, so the search is fully
#: deterministic across processes and runs (E-P1-1).
_PATTERN_SAMPLE_BASIS: tuple[str, ...] = ("a", "A", "0", ".", "-", "@", "_")
_MAX_SAMPLE_LEN = 32
_MAX_SAMPLE_NODES = 100_000


def _pattern_alphabet(pattern: str) -> tuple[str, ...]:
    """The BFS search alphabet for ``pattern``: :data:`_PATTERN_SAMPLE_BASIS`
    plus every other alphanumeric character literally present in the pattern
    text, in sorted order (determinism)."""
    extra = sorted({ch for ch in pattern if ch.isalnum() and ch not in _PATTERN_SAMPLE_BASIS})
    return _PATTERN_SAMPLE_BASIS + tuple(extra)


def _sample_for_pattern(
    pattern: str, *, min_length: int | None = None, max_length: int | None = None
) -> str:
    """A deterministic, shortest-match value satisfying ``pattern`` (and
    ``min_length``/``max_length`` when given), found by breadth-first search
    over :func:`_pattern_alphabet` using the ``regex`` package's
    PARTIAL-MATCH API (``fullmatch(..., partial=True)`` -- stdlib ``re`` has
    no equivalent): a candidate that could still extend into a full match is
    enqueued; one that already fully matches (and meets ``min_length``) is
    returned immediately; one that cannot possibly extend into a match
    (``fullmatch(..., partial=True) is None``) is pruned. BFS plus a fixed
    alphabet order makes the result identical every process, every run.

    FAILS CLOSED: raises :class:`ValueError` when no satisfying value is
    found within :data:`_MAX_SAMPLE_LEN`/:data:`_MAX_SAMPLE_NODES` -- a
    future schema pattern this search cannot satisfy must turn the pinning
    test (``test_every_entity_class_example_properties_pass_the_wall``) red,
    never ship a prompt example that violates the pattern printed one
    section above it (Argus P3 on the prior build)."""
    compiled = regex.compile(pattern)
    alphabet = _pattern_alphabet(pattern)
    cap = _MAX_SAMPLE_LEN if max_length is None else min(_MAX_SAMPLE_LEN, max_length)
    bound = f"cannot derive a sample value satisfying pattern {pattern!r} within search bounds"

    empty_match = compiled.fullmatch("", partial=True)
    if empty_match is not None and not empty_match.partial and (min_length or 0) == 0:
        return ""

    queue: deque[str] = deque([""])
    nodes = 0
    while queue:
        candidate = queue.popleft()
        for ch in alphabet:
            nxt = candidate + ch
            if len(nxt) > cap:
                continue
            nodes += 1
            if nodes > _MAX_SAMPLE_NODES:
                raise ValueError(bound)
            match = compiled.fullmatch(nxt, partial=True)
            if match is None:
                continue
            if not match.partial and len(nxt) >= (min_length or 0):
                return nxt
            queue.append(nxt)
    raise ValueError(bound)


def _sample_value(ann: Any, field: FieldInfo) -> Any:
    """A JSON-serializable illustrative value for an (already-unwrapped) field
    type — used only in the example response block. Honours the SAME
    constraint metadata :func:`_constraint_label` advertises (pattern/bounds)
    so the emitted example cannot violate the constraint printed one section
    above it in the rendered prompt (Argus P3 on the prior build) -- pinned
    by ``test_every_entity_class_example_properties_pass_the_wall``, which
    runs EVERY entity class's example properties through the real
    ``validate_extracted_properties`` wall, not just the one class today's
    ``EDGE_DEFS`` sort order happens to pick for the worked example."""
    pattern: str | None = None
    ge: Any = None
    gt: Any = None
    le: Any = None
    lt: Any = None
    min_length: int | None = None
    max_length: int | None = None
    for constraint in field.metadata:
        c_pattern = getattr(constraint, "pattern", None)
        if isinstance(c_pattern, str):
            pattern = c_pattern
        c_ge = getattr(constraint, "ge", None)
        if c_ge is not None:
            ge = c_ge
        c_gt = getattr(constraint, "gt", None)
        if c_gt is not None:
            gt = c_gt
        c_le = getattr(constraint, "le", None)
        if c_le is not None:
            le = c_le
        c_lt = getattr(constraint, "lt", None)
        if c_lt is not None:
            lt = c_lt
        c_min_length = getattr(constraint, "min_length", None)
        if c_min_length is not None:
            min_length = c_min_length
        c_max_length = getattr(constraint, "max_length", None)
        if c_max_length is not None:
            max_length = c_max_length

    if isinstance(ann, type) and issubclass(ann, Enum):
        return next(iter(ann)).value
    if ann is bool:
        return True
    if ann is str:
        if pattern is not None:
            return _sample_for_pattern(pattern, min_length=min_length, max_length=max_length)
        if min_length is not None and max_length is not None and min_length > max_length:
            raise ValueError(
                f"conflicting length bounds: min_length={min_length} > max_length={max_length}"
            )
        base = "example"
        if min_length is not None and len(base) < min_length:
            base = base + ("e" * (min_length - len(base)))
        if max_length is not None:
            base = base[:max_length]
        return base
    if ann is int:
        lo = ge if ge is not None else (gt + 1 if gt is not None else None)
        hi = le if le is not None else (lt - 1 if lt is not None else None)
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"conflicting numeric bounds: lo={lo} > hi={hi}")
        value: int = 1
        if lo is not None:
            value = max(value, int(lo))
        if hi is not None:
            value = min(value, int(hi))
        return value
    if ann is float or ann is Decimal:
        lo_f = float(ge) if ge is not None else (float(gt) + 1.0 if gt is not None else None)
        hi_f = float(le) if le is not None else (float(lt) - 1.0 if lt is not None else None)
        if lo_f is not None and hi_f is not None and lo_f > hi_f:
            raise ValueError(f"conflicting numeric bounds: lo={lo_f} > hi={hi_f}")
        value_f = 1.0
        if lo_f is not None:
            value_f = max(value_f, lo_f)
        if hi_f is not None:
            value_f = min(value_f, hi_f)
        return value_f
    if ann is datetime:
        return "2026-01-01T00:00:00Z"
    return "example"


#: A ``(name, unwrapped_annotation, source_field)`` triple -- the ``FieldInfo``
#: is carried alongside the unwrapped type so callers can DERIVE constraint
#: text (pattern/bounds) from the same pydantic metadata the wall itself
#: validates against, rather than a second hand-written copy of which fields
#: are constrained (Argus C1: that hand-written gap is exactly the kind of
#: prompt/wall drift this module exists to close).
_FieldSpec = tuple[str, Any, FieldInfo]


def _contract_fields(entity_class: str) -> tuple[list[_FieldSpec], list[_FieldSpec]]:
    """``(required, optional)`` :data:`_FieldSpec` triples for ``entity_class``'s
    contract, in declaration order, EXCLUDING every :data:`_PLATFORM_OWNED_KEYS`
    entry (required list) and additionally excluding :data:`_UNADVERTISED_OPTIONAL`
    (optional list)."""
    contract = ENTITY_CONTRACTS[entity_class]
    required: list[_FieldSpec] = []
    optional: list[_FieldSpec] = []
    for name, field in contract.model_fields.items():
        if name in _PLATFORM_OWNED_KEYS:
            continue
        ann = _unwrap_optional(field.annotation)
        if field.is_required():
            required.append((name, ann, field))
        elif name not in _UNADVERTISED_OPTIONAL:
            optional.append((name, ann, field))
    return required, optional


def _constraint_label(field: FieldInfo) -> str | None:
    """A short constraint note DERIVED from ``field``'s pydantic metadata
    (``pattern``/``ge``/``gt``/``le``/``lt``/``min_length``/``max_length``) --
    generic introspection of whatever constraint objects pydantic attached,
    not a hand-copied list of which fields carry which constraint (Argus C1).
    Returns ``None`` when the field carries no such metadata."""
    parts: list[str] = []
    for constraint in field.metadata:
        pattern = getattr(constraint, "pattern", None)
        if isinstance(pattern, str):
            parts.append(f'matching regex "{pattern}"')
        for attr, symbol in (("ge", ">="), ("gt", ">"), ("le", "<="), ("lt", "<")):
            bound = getattr(constraint, attr, None)
            if bound is not None:
                parts.append(f"{symbol} {bound}")
        min_length = getattr(constraint, "min_length", None)
        if min_length is not None:
            parts.append(f"min length {min_length}")
        max_length = getattr(constraint, "max_length", None)
        if max_length is not None:
            parts.append(f"max length {max_length}")
    if not parts:
        return None
    return ", ".join(parts)


def example_triple() -> EdgeDef:
    """The lexicographically-first ``EDGE_DEFS`` triple, DERIVED (never
    hardcoded) — used as the ``new_entity``/``new_edge`` example pairing."""
    return sorted(EDGE_DEFS, key=lambda e: (e.subject_class, e.edge_kind, e.object_class))[0]


def _example_display(entity_class: str) -> str:
    return f"Example {entity_class}"


def _kind_values_label() -> str:
    """``"link", "new_entity", or "new_edge"`` -- DERIVED from
    :class:`GraphExtractionAssertionKind` member order rather than
    hand-written, so a coordinated rename of the enum's values (and the
    wall's matching dispatch literals) cannot silently leave this sentence
    teaching the model the OLD vocabulary. See Argus P2 finding on the prior
    build: a hand-written copy here was the one place the wall's kind
    vocabulary was NOT derived."""
    return _quoted_list([member.value for member in GraphExtractionAssertionKind], "or")


def example_properties(entity_class: str, *, include_optional: bool = False) -> dict[str, Any]:
    """DERIVED illustrative property values for ``entity_class``'s wall
    contract. Every value comes from :func:`_sample_value`, which honours
    the field's OWN pydantic constraint metadata (pattern/bounds) -- the
    result is guaranteed, and pinned by
    ``test_every_entity_class_example_properties_pass_the_wall``, to pass
    ``ne.brain_graph.extraction.writer.validate_extracted_properties`` for
    EVERY entity class, not just the one class :func:`example_triple`
    happens to pick via ``EDGE_DEFS`` sort order."""
    required, optional = _contract_fields(entity_class)
    specs = required + optional if include_optional else required
    return {name: _sample_value(ann, field) for name, ann, field in specs}


def schema_example_entries() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The three illustrative assertion entries (one per
    :class:`GraphExtractionAssertionKind`) rendered into the prompt's example
    response block. Every field NAME comes from the single per-kind field
    vocabulary (``_LINK_REQUIRED``/``_NEW_ENTITY_REQUIRED``/
    ``_NEW_EDGE_REQUIRED``/``_NEW_EDGE_ENDPOINT_HINTS``) that the prose
    sentences in :func:`render_extraction_system_prompt` ALSO derive from --
    there is no second, independently-typed copy (Argus P2). Every field
    VALUE is DERIVED from the wall's own schema authorities — see the module
    docstring."""
    triple = example_triple()
    properties = example_properties(triple.subject_class)

    link_entry: dict[str, Any] = {
        _KIND_FIELD: GraphExtractionAssertionKind.LINK.value,
        **dict(zip(_LINK_REQUIRED, (EXAMPLE_EXISTING_KEY,), strict=True)),
        _CONFIDENCE_FIELD: 0.85,
    }
    new_entity_entry: dict[str, Any] = {
        _KIND_FIELD: GraphExtractionAssertionKind.NEW_ENTITY.value,
        **dict(
            zip(
                _NEW_ENTITY_REQUIRED,
                (triple.subject_class, _example_display(triple.subject_class), properties),
                strict=True,
            )
        ),
        _CONFIDENCE_FIELD: 0.9,
    }
    # NOTE: field NAMES still come solely from the constants
    # (`_NEW_EDGE_REQUIRED`/`_SUBJECT_CLASS_HINT`), but this entry is built
    # key-by-key (not a single `dict(zip(...))` merge) so the rendered JSON
    # key ORDER matches the pre-existing prompt bytes exactly (subject_class
    # hint between subject_key and object_key) -- see PR body for the
    # byte-identical verification this preserves.
    edge_kind_field, subject_key_field, object_key_field = _NEW_EDGE_REQUIRED
    new_edge_entry: dict[str, Any] = {
        _KIND_FIELD: GraphExtractionAssertionKind.NEW_EDGE.value,
        edge_kind_field: triple.edge_kind,
        subject_key_field: _example_display(triple.subject_class),
        _SUBJECT_CLASS_HINT: triple.subject_class,
        object_key_field: EXAMPLE_EXISTING_KEY,
        _CONFIDENCE_FIELD: 0.8,
    }
    return link_entry, new_entity_entry, new_edge_entry


def _format_fields(fields: list[_FieldSpec]) -> str:
    def _one(name: str, ann: Any, field: FieldInfo) -> str:
        label = _type_label(ann)
        constraint = _constraint_label(field)
        if constraint is not None:
            label = f"{label}, {constraint}"
        return f"{name} ({label})"

    return ", ".join(_one(name, ann, field) for name, ann, field in fields)


def _entity_classes_block() -> str:
    lines = ["Entity classes and their properties:"]
    for entity_class in sorted(ENTITY_CONTRACTS):
        required, optional = _contract_fields(entity_class)
        line = f"- {entity_class}: required {_format_fields(required)}"
        if optional:
            line += f"; optional {_format_fields(optional)}"
        lines.append(line)
    return "\n".join(lines)


def _edge_kinds_block() -> str:
    lines = ["Edge kinds (subject_class -edge_kind-> object_class):"]
    for triple in sorted(EDGE_DEFS, key=lambda e: (e.subject_class, e.edge_kind, e.object_class)):
        lines.append(f"- {triple.subject_class} -{triple.edge_kind}-> {triple.object_class}")
    return "\n".join(lines)


def _example_response_block() -> str:
    envelope = {"assertions": list(schema_example_entries())}
    return (
        "Example response (keys and values are illustrative -- use real keys from the "
        "Candidates list and real values from the chunk text):\n"
        + json.dumps(envelope, ensure_ascii=False)
    )


def render_extraction_system_prompt() -> str:
    """The full extraction system-prompt text — a pure function of import-time
    schema constants (``ENTITY_CONTRACTS``, ``EDGE_DEFS``,
    ``GraphExtractionAssertionKind``, ``_PLATFORM_OWNED_KEYS``): same schema,
    same prompt bytes, every process, every run (E-P1-1).

    See the module docstring for what makes every claim below TRUE:
    ``test_extraction_prompt_wall_contract.py``.
    """
    sections = [
        (
            "You extract graph mentions from one document chunk. Link mentions to the "
            "provided candidates FIRST; only emit new_entity or new_edge for mentions that "
            "cannot be resolved to a candidate. Respond with strict JSON only: a single "
            'object of the form {"assertions": [...]} -- no prose, no markdown code fences, '
            "no text outside the JSON object."
        ),
        (
            f'Every assertion in "assertions" MUST carry two universal fields: "{_KIND_FIELD}" '
            "-- exactly one of " + _kind_values_label() + f'; and "{_CONFIDENCE_FIELD}" -- a '
            "number from 0.0 to 1.0, your calibrated estimate. Assertions missing either "
            "field are discarded unread."
        ),
        (
            f'A "link" assertion also requires {_quoted_list(_LINK_REQUIRED, "and")} -- must '
            "be a key from the Candidates list."
        ),
        (
            f'A "new_entity" assertion also requires {_quoted_list(_NEW_ENTITY_REQUIRED, "and")} '
            "(see the per-class property lists below). Emit a new_entity ONLY "
            "when every required property is grounded in the chunk text; never guess or "
            "fabricate values."
        ),
        (
            f'A "new_edge" assertion also requires {_quoted_list(_NEW_EDGE_REQUIRED, "and")}; '
            "include " + "/".join(f'"{h}"' for h in _NEW_EDGE_ENDPOINT_HINTS) + " when the "
            "endpoint is a new_entity display from THIS SAME response (required in that "
            "case), and the edge must match one of the triples listed below exactly."
        ),
        _entity_classes_block(),
        _edge_kinds_block(),
        _example_response_block(),
        ("Never emit an entity_class, edge_kind, or property outside the schema above."),
    ]
    return "\n\n".join(sections)
