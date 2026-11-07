# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Immutable, hashable mapping type for frozen value-object fields.

A pydantic ``ConfigDict(frozen=True)`` model blocks *attribute reassignment* but
does **not** freeze the *contents* of a plain-``dict`` field: the dict stays
mutable in place, and — because a ``dict`` is unhashable — ``hash(model)`` raises
``TypeError`` the moment the model has any mapping field. :class:`FrozenDict`
closes both holes: it is a read-only :class:`collections.abc.Mapping` with a
stable hash, and the :data:`StrMap` / :data:`AnyMap` / :data:`FloatMap` annotated
aliases coerce any incoming ``dict`` to a ``FrozenDict`` at validation time while
keeping the *declared* field type a plain ``Mapping`` (so plain-``dict``
construction stays source-compatible at every call site and under pyright).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType
from typing import Annotated, Any, NoReturn, TypeVar

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

K = TypeVar("K")
V = TypeVar("V")


def _freeze_value(value: Any) -> Any:
    """Recursively convert a JSON-shaped value into an immutable, hashable form.

    ``Mapping`` -> :class:`FrozenDict`, ``list``/``tuple`` -> ``tuple``,
    ``set``/``frozenset`` -> ``frozenset``; scalars (``str``/``int``/``float``/
    ``bool``/``None``/``datetime``/...) and already-frozen values pass through.

    A leaf value that is not hashable after freezing (e.g. a ``bytearray`` or an
    exotic mutable object smuggled into an ``Any``-typed map) is rejected here at
    construction with a clear ``TypeError`` — there is no deferred ``hash()``
    failure later.
    """
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(v) for v in value)
    try:
        hash(value)
    except TypeError as exc:
        raise TypeError(
            f"unhashable value in frozen mapping: {value!r} of type {type(value).__name__}"
        ) from exc
    return value


def thaw(value: Any) -> Any:
    """Recursively convert a frozen value back into plain, literal builtins.

    Inverse of :func:`_freeze_value`. This function has TWO sanctioned callers:

    1. The pydantic serializer for :class:`FrozenDict`-typed fields (wired below
       in :class:`_FrozenDictAnnotation`), so ``model_dump`` / ``model_dump_json``
       emit plain nested ``dict``/``list`` structures.
    2. The sanctioned EGRESS-BOUNDARY converter for handing a frozen contract
       value to a third-party library that type-checks for literal builtins
       rather than accepting the ``Mapping``/``Sequence`` ABCs — e.g. litellm's
       ``type_to_response_format_param``, which requires
       ``isinstance(response_format, dict)`` and raises ``TypeError`` on any
       other ``Mapping`` (a bare ``FrozenDict`` included, since
       ``FrozenDict.__eq__`` intentionally compares equal to ``dict`` but
       ``isinstance`` checks are unaffected). ``ne.gateway.gateway._call_params``
       is the reference call site (NE-GATEWAY-FROZENDICT).

    Semantics, recursive to arbitrary depth (no depth limit — a fix that stops
    after one extra layer is exactly the defect this function exists to close):
    ``Mapping`` -> ``dict``; ``list``/``tuple``/``set``/``frozenset`` -> ``list``;
    every other leaf passes through UNCHANGED. Pass-through is safe by
    construction: :func:`_freeze_value` already rejects any leaf that is not
    hashable, loudly, with a ``TypeError``, at model-construction time — so by
    the time a value reaches ``thaw`` every non-container leaf is already an
    immutable, hashable, JSON-shaped scalar (or an already-frozen contract
    value that a caller has deliberately smuggled through an ``Any``-typed
    map); there is nothing further for ``thaw`` to normalize about it (see
    ``test_thaw_passes_exotic_hashable_leaf_through_by_identity``).
    """
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [thaw(v) for v in value]
    return value


# ``ne-api``'s ``wire.py`` imports the private name directly
# (``from ne.contracts.frozen import _thaw``) and ``ne-mcp``'s ``server.py``
# carries an independent, NEAR-duplicate local implementation of a similar
# walk (it additionally coerces keys with ``str(k)`` and only converts
# ``list``/``tuple``, not ``set``/``frozenset`` — not a byte-for-byte match,
# so repointing it is not a drop-in swap). Repointing both call sites at the
# new public ``thaw`` export is an explicit follow-up OUTSIDE this unit's
# ``mayTouch`` (named in the NE-GATEWAY-FROZENDICT PR body, ADR-0016) — this
# alias keeps the existing private import working unchanged in the meantime,
# so promoting the function to a public name is purely additive.
_thaw = thaw


class FrozenDict(Mapping[K, V]):
    """An immutable, hashable :class:`~collections.abc.Mapping`.

    Construction recursively freezes values (see :func:`_freeze_value`) so an
    ``Any``-valued map is hashable. There are **no** mutators of any kind:
    contents live in a read-only :class:`types.MappingProxyType`, so item
    assignment (``fd._data['x'] = 1``) raises, and attribute assignment/deletion
    on the instance itself raises ``TypeError`` via the guards below. The hash is
    computed **eagerly** at construction over the sorted items (keys are ``str``
    in every contract field) and stored once — there is no lazy first-call
    caching window, so the hash can never go stale relative to the contents.

    Pickle/copy round-trips go through :meth:`__reduce__` -> ``__init__``, so a
    reconstructed instance is re-frozen and re-hashed with every invariant intact.
    """

    __slots__ = ("_data", "_hash")

    _data: Mapping[K, V]
    _hash: int

    def __init__(
        self,
        data: Mapping[K, V] | Iterable[tuple[K, V]] | None = None,
    ) -> None:
        raw: dict[K, V] = dict(data) if data is not None else {}
        frozen = {key: _freeze_value(value) for key, value in raw.items()}
        object.__setattr__(self, "_data", MappingProxyType(frozen))
        object.__setattr__(self, "_hash", hash(tuple(sorted(frozen.items()))))

    def __getitem__(self, key: K) -> V:
        return self._data[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"FrozenDict({dict(self._data)!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self) == dict(other)
        return NotImplemented

    def __hash__(self) -> int:
        return self._hash

    def __reduce__(self) -> tuple[Any, ...]:
        # Contents live in a MappingProxyType, which pickle cannot serialize —
        # without this, every map-bearing contract model is unpicklable and
        # un-deepcopyable (fatal to the DBOS-pickled durable-runtime path,
        # SPEC §4 / NE-033). Reconstruct via __init__ from a plain dict: the
        # proxy is rebuilt, values re-frozen, and the hash recomputed eagerly,
        # so every immutability invariant survives the round-trip.
        return (type(self), (dict(self._data),))

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise TypeError("FrozenDict is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise TypeError("FrozenDict is immutable")


def _coerce(value: Any) -> FrozenDict[Any, Any]:
    """Validator hook: accept a plain dict (or FrozenDict) and freeze it."""
    if isinstance(value, FrozenDict):
        return value
    return FrozenDict(value)


class _FrozenDictAnnotation:
    """Pydantic annotation that coerces a ``Mapping`` field to :class:`FrozenDict`.

    Attached to a ``Mapping[K, V]`` via :data:`typing.Annotated`, it builds a
    ``dict`` core-schema over ``(K, V)`` (so keys/values are validated exactly as
    a plain mapping field would be), runs the validated ``dict`` through
    :func:`_coerce`, and serializes via :func:`thaw` back to plain JSON.
    """

    def __get_pydantic_core_schema__(
        self, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        from typing import get_args

        args = get_args(source_type)
        if len(args) == 2:
            dict_schema = core_schema.dict_schema(
                handler.generate_schema(args[0]),
                handler.generate_schema(args[1]),
            )
        else:  # pragma: no cover - aliases always supply both args
            dict_schema = core_schema.dict_schema()
        return core_schema.no_info_after_validator_function(
            _coerce,
            dict_schema,
            serialization=core_schema.plain_serializer_function_ser_schema(thaw),
        )


# Declared type stays ``Mapping[...]`` (DELIBERATE): pyright/typecheckers keep
# accepting plain-``dict`` construction at every call site, while pydantic coerces
# the runtime value to an immutable, hashable ``FrozenDict`` at validation time.
StrMap = Annotated[Mapping[str, str], _FrozenDictAnnotation()]
AnyMap = Annotated[Mapping[str, Any], _FrozenDictAnnotation()]
FloatMap = Annotated[Mapping[str, float], _FrozenDictAnnotation()]
