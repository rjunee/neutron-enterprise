# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Reflection-derived guard locking the exhaustiveness of ``_PRESENCE_MARKER_KEYS``.

Repo rule (``written-rules-disarm-review``): a normative claim in a docstring must
trace to a TEST, not an argument. ``bootstrap.py`` asserts that ``this`` and
``wildcard`` are EXACTLY and EXHAUSTIVELY the OpenFGA fields where a raw ``{}`` is
semantically meaningful (vs. an unset default that ``_without_unset_defaults``
safely strips). That claim was true when hand-verified against the pinned SDK, but
nothing kept it true.

The failure mode: if a future ``openfga-sdk`` version introduces a THIRD zero-field
marker object, ``_without_unset_defaults`` strips it symmetrically on both the
server and in-repo model, and ``verify_model_pin`` becomes VACUOUS for that
construct — an added or widened instance compares EQUAL to its absence. One of the
two known markers (``wildcard``) is the PUBLIC-ACCESS marker, so the class of drift
this would silently stop catching is privilege escalation. This module walks the
SDK's generated ``openapi_types`` field-type maps transitively from
``AuthorizationModel``, DISCOVERS every zero-field marker-object field by
reflection, and asserts the discovered set equals the production constant — so the
claim breaks loudly on the next SDK bump instead of rotting silently.
"""

from __future__ import annotations

import re
from typing import ClassVar, NamedTuple

import openfga_sdk
import pytest

from ne.permissions_rebac import bootstrap
from ne.permissions_rebac.bootstrap import _PRESENCE_MARKER_KEYS

_OPENAPI_PRIMITIVES = frozenset(
    {
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "date",
        "datetime",
        # NOTE: "object" is deliberately ABSENT. It is the marker shape itself and
        # is matched explicitly in ``_discover`` BEFORE this primitive check, so
        # listing it here would filter out container-wrapped occurrences
        # (``dict[str, object]``) and reopen the vacuity gap.
        "file",
        "list",
        "dict",
        "set",
        "tuple",
        "none_type",
    }
)


class _Discovery(NamedTuple):
    """Result of one traversal of the SDK's generated model-class field graph."""

    markers: frozenset[str]  # JSON keys where a raw {} is presence-semantic
    visited: frozenset[type]  # every model class the walk reached
    skipped: tuple[str, ...]  # type-string identifiers that failed to resolve


def _is_zero_field_marker_class(cls: type) -> bool:
    """True for a generated class with no declared fields that is NOT an enum.

    Zero-field classes come in two flavours in the SDK's generator output: an
    anonymous marker object (``Userset.this``, ``RelationReference.wildcard`` —
    real presence markers) and a generated enum wrapper (``TypeName`` — serializes
    as a scalar string, never a raw ``{}``, so it can never carry presence
    semantics). Enums are distinguished by a non-empty ``allowable_values``.
    """
    return getattr(cls, "openapi_types", None) == {} and not getattr(cls, "allowable_values", None)


def _discover(root: type = openfga_sdk.AuthorizationModel) -> _Discovery:
    """Walk ``openapi_types`` transitively from ``root``, discovering marker fields.

    Tracks visited classes BEFORE reading their fields (the SDK's type graph has
    cycles, e.g. ``Userset`` <-> ``Usersets``). Field-type strings are container
    syntax (``list[X]``, ``dict[str, X]``, or bare ``X``); every bare identifier in
    the string is extracted and, if not an OpenAPI primitive, resolved against the
    ``openfga_sdk`` package namespace where every model class is re-exported.

    A visited class missing ``openapi_types``/``attribute_map`` raises loudly
    rather than being skipped: silently treating it as field-less is exactly how
    this guard would go vacuous under a future SDK generator rewrite (e.g. a
    pydantic-model rewrite that renames the attribute). An unresolvable type
    identifier is instead collected into ``skipped`` — it must never crash the
    traversal — and the caller decides whether an empty skip list is required.
    """
    visited: set[type] = set()
    markers: set[str] = set()
    skipped: list[str] = []
    queue: list[type] = [root]
    while queue:
        cls = queue.pop()
        if cls in visited:
            continue
        visited.add(cls)
        types = getattr(cls, "openapi_types", None)
        amap = getattr(cls, "attribute_map", None)
        if types is None or amap is None:
            raise AssertionError(
                f"{cls.__name__} lacks openapi_types/attribute_map — the SDK "
                "generator shape changed; the presence-marker guard cannot see "
                "this SDK and must be rewritten, not skipped"
            )
        for field, tstr in types.items():
            json_key = amap.get(field, field)
            for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", tstr):
                if name == "object":
                    # The anonymous zero-subfield marker shape (``Userset.this``,
                    # ``RelationReference.wildcard`` in the pinned SDK).
                    #
                    # Matched wherever the identifier appears, INCLUDING wrapped in
                    # a container (``dict[str, object]``), not only as the bare type
                    # string. A container-wrapped literal ``object`` is NOT safe to
                    # ignore: ``_without_unset_defaults`` drops a raw ``{}`` sitting
                    # as a MAP VALUE under any non-marker key (verified:
                    # ``{"m": {"k": {}}}`` -> ``{"m": {}}``), so a future SDK field
                    # typed ``dict[str, object]`` whose empty-value presence is
                    # semantic would be stripped symmetrically and compare equal to
                    # its absence — precisely the vacuity this module exists to
                    # prevent. Flagging it forces a human to adjudicate rather than
                    # letting the guard stay green through a shape it cannot see.
                    #
                    # ``list[object]`` would be harmless (the list branch of
                    # ``_without_unset_defaults`` never drops elements), but it is
                    # flagged too: distinguishing the container kind here would
                    # re-encode a strip-side implementation detail in the discovery
                    # side, and a false positive costs one review while a false
                    # negative costs silent vacuity.
                    markers.add(json_key)
                    continue
                if name in _OPENAPI_PRIMITIVES:
                    continue
                resolved = getattr(openfga_sdk, name, None)
                if not isinstance(resolved, type):
                    skipped.append(f"{cls.__name__}.{field}: {name}")
                    continue
                if _is_zero_field_marker_class(resolved):
                    # Conservative: flag even container-wrapped zero-field
                    # classes so a human adjudicates a future generator shape.
                    markers.add(json_key)
                queue.append(resolved)
    return _Discovery(frozenset(markers), frozenset(visited), tuple(skipped))


def _assert_discovered_markers_match_constant() -> None:
    """The guard's comparison, factored out so a RED case can exercise it.

    Reads the constant through the MODULE (``bootstrap._PRESENCE_MARKER_KEYS``)
    rather than through the module-level ``from ... import`` binding, so that
    ``monkeypatch.setattr(bootstrap, ...)`` genuinely changes what this compares
    against. That is what lets ``test_guard_goes_red_on_removed_known_marker``
    assert a real failure instead of a tautology.
    """
    discovery = _discover()
    assert discovery.skipped == ()
    assert discovery.markers == bootstrap._PRESENCE_MARKER_KEYS


def test_discovered_markers_equal_presence_marker_keys() -> None:
    """THE guard: reflection-discovered markers must equal the production constant.

    Compares against the production constant — never a literal set — so this cannot
    degrade into a second hardcoded list. Goes red on any SDK bump that adds or
    removes a zero-field marker object reachable from ``AuthorizationModel``. Also
    pins the skip list empty: a resolution failure on the real, pinned SDK would
    silently narrow discovery, which is exactly the vacuity this guard exists to
    prevent.
    """
    _assert_discovered_markers_match_constant()


def test_traversal_is_nonvacuous() -> None:
    """Control: the traversal must actually visit a non-trivial class graph.

    A broken traversal (e.g. a typo that stops recursion immediately) would
    discover an empty marker set and could pass by comparing empty-to-empty
    against an accidentally-emptied constant. This pins that the walk reaches a
    known, sizeable set of classes and that it finds a non-empty marker set,
    ruling out the invalid-probe trap.
    """
    discovery = _discover()
    names = {cls.__name__ for cls in discovery.visited}
    expected_reached = {
        "AuthorizationModel",
        "TypeDefinition",
        "Userset",
        "Usersets",
        "RelationReference",
        "Metadata",
        "RelationMetadata",
        "Condition",
        "TypeName",
    }
    assert expected_reached <= names
    assert len(discovery.visited) >= 10
    assert discovery.markers


def test_enum_valued_fields_are_not_presence_markers() -> None:
    """Locks the enum-exclusion discriminator against regression.

    ``TypeName`` has ``openapi_types == {}`` just like a real marker object, but it
    is a generated enum (non-empty ``allowable_values``) that always serializes as
    a scalar string — a raw ``{}`` can never appear at an enum-typed key. Without
    the ``allowable_values`` discriminator in ``_is_zero_field_marker_class`` this
    would be a false-positive discovery.
    """
    from openfga_sdk.models.type_name import TypeName

    assert TypeName.openapi_types == {}
    assert TypeName.allowable_values
    discovery = _discover()
    assert TypeName in discovery.visited
    assert "type_name" not in discovery.markers


def test_guard_goes_red_on_synthetic_object_typed_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED case: a synthetic literal-``object`` field must be discovered.

    ``RelationReference`` sits deep in the graph
    (``AuthorizationModel -> TypeDefinition -> Metadata -> RelationMetadata ->
    RelationReference``), so this also proves genuine transitive discovery rather
    than a top-level-only scan.
    """
    from openfga_sdk.models.relation_reference import RelationReference

    monkeypatch.setattr(
        RelationReference,
        "openapi_types",
        {**RelationReference.openapi_types, "synthetic_marker": "object"},
    )
    discovery = _discover()
    assert "synthetic_marker" in discovery.markers
    assert discovery.markers != _PRESENCE_MARKER_KEYS


def test_guard_goes_red_on_synthetic_zero_field_class_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED case: a synthetic zero-field NAMED class must be discovered.

    Exercises the named-class resolution branch (as opposed to the literal
    ``"object"`` string branch covered above) — a future SDK generator could
    plausibly emit a named empty class instead of the bare ``object`` string.
    """
    from openfga_sdk.models.type_definition import TypeDefinition

    class _SyntheticMarker:
        openapi_types: ClassVar[dict[str, str]] = {}
        attribute_map: ClassVar[dict[str, str]] = {}

    monkeypatch.setattr(openfga_sdk, "SyntheticMarker", _SyntheticMarker, raising=False)
    monkeypatch.setattr(
        TypeDefinition,
        "openapi_types",
        {**TypeDefinition.openapi_types, "synthetic2": "SyntheticMarker"},
    )
    discovery = _discover()
    assert "synthetic2" in discovery.markers


def test_guard_goes_red_on_container_wrapped_object_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A literal ``object`` wrapped in a container must ALSO be discovered.

    Regression pin for a real residual vacuity gap found in review of this unit:
    discovery originally matched only the BARE type string ``"object"``, while the
    identifier ``object`` inside ``dict[str, object]`` was filtered out as an
    OpenAPI primitive — so such a field stayed invisible and the guard stayed
    GREEN. That is not benign: ``_without_unset_defaults`` DROPS a raw ``{}``
    sitting as a map value under any non-marker key (verified directly:
    ``{"m": {"k": {}}}`` -> ``{"m": {}}``), so a future SDK field typed
    ``dict[str, object]`` whose empty-value presence carried meaning would be
    stripped symmetrically and compare equal to its absence.

    Uses ``TypeDefinition`` — a class no other test in this module patches — so the
    discovery walk must genuinely reach it rather than the mutation being visible
    only at the traversal root.
    """
    monkeypatch.setattr(
        openfga_sdk.TypeDefinition,
        "openapi_types",
        {**openfga_sdk.TypeDefinition.openapi_types, "mapmarker": "dict[str, object]"},
    )
    monkeypatch.setattr(
        openfga_sdk.TypeDefinition,
        "attribute_map",
        {**openfga_sdk.TypeDefinition.attribute_map, "mapmarker": "mapmarker"},
    )
    discovery = _discover()
    assert "mapmarker" in discovery.markers
    with pytest.raises(AssertionError):
        _assert_discovered_markers_match_constant()


def test_guard_goes_red_on_removed_known_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bidirectionality: dropping any one known marker from the constant must fail.

    This exercises the REAL comparison against a REAL mutated constant. An earlier
    version asserted ``discovery.markers != _PRESENCE_MARKER_KEYS - {key}``, which
    is a TAUTOLOGY: with a 2-element constant it compares a 2-element set against a
    1-element set, so it is unequal on cardinality alone and can never fail. It was
    observed PASSING while the main guard FAILED under a genuine mutation of the
    production constant — i.e. it asserted nothing about the property its own
    docstring claimed to prove. That is the exact defect class this module exists
    to prevent (a claim resting on an argument rather than on something that can
    fail), so it is fixed here rather than recorded as a nit.

    Now: patch ``bootstrap._PRESENCE_MARKER_KEYS`` to the reduced set and assert the
    guard's own comparison raises. This can genuinely fail — if the guard were ever
    rewritten to compare against a hardcoded literal instead of the production
    constant, the patch would stop having any effect and ``pytest.raises`` would
    not be satisfied.
    """
    for key in _PRESENCE_MARKER_KEYS:
        monkeypatch.setattr(bootstrap, "_PRESENCE_MARKER_KEYS", _PRESENCE_MARKER_KEYS - {key})
        with pytest.raises(AssertionError):
            _assert_discovered_markers_match_constant()
        monkeypatch.undo()

    # Control: with the constant unpatched the same call must NOT raise. Without
    # this, a version of _assert_... that raised unconditionally (or a mutation
    # breaking discovery entirely) would satisfy every pytest.raises above and look
    # like a working bidirectional guard — the invalid-probe trap.
    _assert_discovered_markers_match_constant()


def test_missing_openapi_types_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A visited class missing ``openapi_types`` must raise, not discover nothing.

    Locks the pydantic-rewrite failure mode: if a future SDK generator moves field
    metadata to a different attribute, the traversal must fail loudly rather than
    silently treating the class as field-less (which would shrink discovery).
    """
    from openfga_sdk.models.usersets import Usersets

    monkeypatch.delattr(Usersets, "openapi_types")
    with pytest.raises(AssertionError, match="openapi_types"):
        _discover()


def test_unresolvable_type_name_lands_in_skip_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable type identifier is collected, not crashed on.

    The main guard (``test_discovered_markers_equal_presence_marker_keys``)
    asserts the skip list is empty on the pinned SDK, so a resolution regression
    there turns the guard red rather than silently narrowing discovery.
    """
    from openfga_sdk.models.relation_reference import RelationReference

    monkeypatch.setattr(
        RelationReference,
        "openapi_types",
        {**RelationReference.openapi_types, "mystery": "NoSuchModelClass"},
    )
    discovery = _discover()
    assert any("NoSuchModelClass" in s for s in discovery.skipped)
