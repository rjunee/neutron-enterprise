# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FrozenDict + the systemic immutable/hashable sweep over every mapping field.

Proves the now-TRUE guarantee: every frozen value object with a mapping field is
hashable, its mapping fields are immutable :class:`FrozenDict` instances (recursive
freezing), and in-place mutation raises ``TypeError``.
"""

from __future__ import annotations

import copy
import pickle
from datetime import UTC, datetime
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from ne.contracts import (
    ApprovalRequest,
    ApprovalTier,
    AuthenticatedRequestContext,
    Chunk,
    CompletionRequest,
    EvalRunResult,
    FrozenDict,
    KnowledgeItem,
    ObjectStat,
    PolicyDecision,
    PolicyRequest,
    Principal,
    Record,
    SecretVersion,
    SubAgentSpec,
    TokenClaims,
    ToolSpec,
    thaw,
)
from ne.contracts.frozen import _thaw
from ne.contracts.governance import DecisionEffect

# --------------------------------------------------------------------------- #
# FrozenDict unit behaviour
# --------------------------------------------------------------------------- #


def test_getitem_len_iter() -> None:
    fd: FrozenDict[str, int] = FrozenDict({"a": 1, "b": 2})
    assert fd["a"] == 1
    assert len(fd) == 2
    assert set(fd) == {"a", "b"}
    assert dict(fd) == {"a": 1, "b": 2}


def test_empty_construction() -> None:
    fd: FrozenDict[str, int] = FrozenDict()
    assert len(fd) == 0
    assert dict(fd) == {}


def test_from_item_iterable() -> None:
    fd: FrozenDict[str, int] = FrozenDict([("a", 1), ("b", 2)])
    assert dict(fd) == {"a": 1, "b": 2}


def test_eq_with_plain_dict() -> None:
    assert FrozenDict({"a": 1}) == {"a": 1}
    assert FrozenDict({"a": 1}) == FrozenDict({"a": 1})
    assert FrozenDict({"a": 1}) != {"a": 2}
    assert FrozenDict({"a": 1}) != "not-a-mapping"


def test_repr() -> None:
    assert repr(FrozenDict({"a": 1})) == "FrozenDict({'a': 1})"


def test_hash_equal_for_equal_content() -> None:
    assert hash(FrozenDict({"a": 1, "b": 2})) == hash(FrozenDict({"b": 2, "a": 1}))


def test_hash_stable_and_cached() -> None:
    fd = FrozenDict({"a": 1})
    assert hash(fd) == hash(fd)


def test_no_mutator_attributes() -> None:
    fd: FrozenDict[str, int] = FrozenDict({"a": 1})
    for attr in ("__setitem__", "__delitem__", "pop", "popitem", "update", "clear", "setdefault"):
        assert not hasattr(fd, attr), f"FrozenDict must not expose {attr}"


def test_data_is_readonly_mappingproxy() -> None:
    fd: FrozenDict[str, int] = FrozenDict({"a": 1})
    assert isinstance(fd._data, MappingProxyType)
    with pytest.raises(TypeError):
        fd._data["x"] = 1  # type: ignore[index]
    # Contents are unchanged: the P2 #1 mutation vector is closed.
    assert dict(fd) == {"a": 1}


def test_setattr_raises() -> None:
    fd: FrozenDict[str, int] = FrozenDict({"a": 1})
    with pytest.raises(TypeError, match="immutable"):
        fd._data = {}
    with pytest.raises(TypeError, match="immutable"):
        fd._hash = 0


def test_delattr_raises() -> None:
    fd: FrozenDict[str, int] = FrozenDict({"a": 1})
    with pytest.raises(TypeError, match="immutable"):
        delattr(fd, "_data")


def test_hash_computed_eagerly_at_construction() -> None:
    fd: FrozenDict[str, int] = FrozenDict({"a": 1})
    # _hash is populated at __init__ (no lazy first-call window): the stored
    # value equals hash(fd) without any prior hash() call having occurred.
    assert object.__getattribute__(fd, "_hash") == hash(fd)
    # Equal content still hashes equal regardless of insertion order.
    assert hash(FrozenDict({"a": 1, "b": 2})) == hash(FrozenDict({"b": 2, "a": 1}))


def test_shared_default_no_cross_instance_leakage() -> None:
    p1 = Principal(id="u")
    with pytest.raises(TypeError):
        p1.claims._data["evil"] = "x"  # type: ignore[index]
    p2 = Principal(id="v")
    assert dict(p2.claims) == {}
    assert dict(p1.claims) == {}


def test_unhashable_value_rejected_at_construction() -> None:
    with pytest.raises(TypeError, match="unhashable value in frozen mapping"):
        FrozenDict({"x": bytearray(b"z")})
    with pytest.raises(TypeError, match="unhashable value in frozen mapping"):
        FrozenDict({"rows": [bytearray(b"z")]})


def test_unhashable_value_rejected_via_model_field() -> None:
    with pytest.raises((TypeError, ValidationError)):
        Chunk(chunk_id="c", document_id="d", text="t", metadata={"k": bytearray(b"z")})


def test_recursive_freezing() -> None:
    fd: FrozenDict[str, object] = FrozenDict({"m": {"n": 1}, "lst": [1, 2], "s": {3, 4}})
    assert isinstance(fd["m"], FrozenDict)
    assert isinstance(fd["lst"], tuple)
    assert isinstance(fd["s"], frozenset)
    # A nested Any-valued map is hashable end-to-end.
    assert isinstance(hash(fd), int)


def test_nested_list_of_dicts_frozen() -> None:
    fd: FrozenDict[str, object] = FrozenDict({"rows": [{"a": 1}, {"b": 2}]})
    rows = fd["rows"]
    assert isinstance(rows, tuple)
    assert all(isinstance(r, FrozenDict) for r in rows)
    assert isinstance(hash(fd), int)


# --------------------------------------------------------------------------- #
# Pickle / copy round-trip — NE-CKA3 PERMANENT regression tests (do not remove). #
# --------------------------------------------------------------------------- #


def test_pickle_roundtrip_nested_frozendict() -> None:
    fd: FrozenDict[str, object] = FrozenDict({"a": {"b": 1}, "lst": [1, 2], "s": {3}})
    fd2 = pickle.loads(pickle.dumps(fd))
    assert fd2 == fd
    assert hash(fd2) == hash(fd)
    assert isinstance(fd2["a"], FrozenDict)
    assert isinstance(fd2["lst"], tuple)
    assert isinstance(fd2["s"], frozenset)
    assert isinstance(fd2._data, MappingProxyType)


def test_pickle_roundtrip_model_with_populated_map() -> None:
    p = Principal(id="u", claims={"x": "y"})
    p2 = pickle.loads(pickle.dumps(p))
    assert p2 == p
    assert hash(p2) == hash(p)
    assert isinstance(p2.claims, FrozenDict)

    c = Chunk(chunk_id="c", document_id="d", text="t", metadata={"a": {"b": [1, 2]}})
    c2 = pickle.loads(pickle.dumps(c))
    assert c2 == c
    assert hash(c2) == hash(c)
    assert isinstance(c2.metadata, FrozenDict)
    assert isinstance(c2.metadata["a"], FrozenDict)


def test_pickle_roundtrip_model_with_empty_default_map() -> None:
    e = Principal(id="u")
    e2 = pickle.loads(pickle.dumps(e))
    assert e2 == e
    assert hash(e2) == hash(e)


def test_deepcopy_and_copy_of_frozendict_and_model() -> None:
    fd: FrozenDict[str, object] = FrozenDict({"a": {"b": 1}})
    assert copy.copy(fd) == fd
    assert copy.deepcopy(fd) == fd
    p = Principal(id="u", claims={"x": "y"})
    pc = copy.deepcopy(p)
    assert pc == p
    assert hash(pc) == hash(p)


def test_model_copy_deep_roundtrip() -> None:
    p = Principal(id="u", claims={"x": "y"})
    pc = p.model_copy(deep=True)
    assert pc == p
    assert hash(pc) == hash(p)
    assert isinstance(pc.claims, FrozenDict)
    with pytest.raises(TypeError):
        pc.claims._data["evil"] = "x"  # type: ignore[index]


def test_immutability_guards_hold_after_unpickle() -> None:
    fd: FrozenDict[str, int] = FrozenDict({"a": 1})
    fd2 = pickle.loads(pickle.dumps(fd))
    with pytest.raises(TypeError):
        fd2._data["x"] = 1  # type: ignore[index]
    with pytest.raises(TypeError, match="immutable"):
        fd2._data = {}
    with pytest.raises(TypeError):
        delattr(fd2, "_data")
    assert hash(fd2) == hash(fd2)
    assert dict(fd2) == {"a": 1}


# --------------------------------------------------------------------------- #
# Systemic sweep: all 14 mapping-field models
# --------------------------------------------------------------------------- #

_NOW = datetime(2026, 7, 3, tzinfo=UTC)


def _principal() -> Principal:
    return Principal(id="u", claims={"x": "y"})


# (model instance, name of the mapping field) for every affected value object.
_MODELS: list[tuple[object, str]] = [
    (Principal(id="u", claims={"x": "y"}), "claims"),
    (Chunk(chunk_id="c", document_id="d", text="t", metadata={"k": 1}), "metadata"),
    (CompletionRequest(messages=(), task="t", params={"temp": 0.5}), "params"),
    (
        ToolSpec(name="n", description="d", tier=ApprovalTier.TIER_0, input_schema={"a": 1}),
        "input_schema",
    ),
    (SubAgentSpec(agent="a", task="t", context={"k": "v"}), "context"),
    (ObjectStat(key="k", size=1, content_type="text/plain", metadata={"a": "b"}), "metadata"),
    (SecretVersion(path="p", version=1, data={"u": "v"}, created_at=_NOW), "data"),
    (KnowledgeItem(item_id="i", content="c", metadata={"k": 1}, provenance="p"), "metadata"),
    (
        EvalRunResult(
            suite="s", config_hash="h", metrics={"acc": 0.9}, thresholds={"acc": 0.8}, passed=True
        ),
        "metrics",
    ),
    (TokenClaims(subject="s", issuer="i", claims={"scope": "read"}), "claims"),
    (PolicyRequest(principal=_principal(), action="a", resource="r", context={"k": 1}), "context"),
    (
        PolicyDecision(effect=DecisionEffect.ALLOW, policy_version="v1", obligations={"log": True}),
        "obligations",
    ),
    (
        ApprovalRequest(
            tier=ApprovalTier.TIER_2, action="a", principal=_principal(), payload={"k": 1}
        ),
        "payload",
    ),
    (Record(record_id="r", payload={"k": 1}, source_id="s"), "payload"),
]


def test_authenticated_request_context_is_frozen_hashable_and_copy_safe() -> None:
    context = AuthenticatedRequestContext(
        principal_id="user:u",
        authorization_groups=("engineering",),
        issuer="https://issuer.example",
        expires_at=1_800_000_000,
    )
    assert isinstance(hash(context), int)
    assert copy.copy(context) == context
    assert copy.deepcopy(context) == context
    assert pickle.loads(pickle.dumps(context)) == context
    with pytest.raises(ValidationError):
        context.expires_at = 1_900_000_000  # type: ignore[misc]


def _param_id(v: object) -> str:
    return v if isinstance(v, str) else type(v).__name__


@pytest.mark.parametrize("model,field", _MODELS, ids=_param_id)
def test_model_mapping_field_is_frozen_and_hashable(model: object, field: str) -> None:
    value = getattr(model, field)
    assert isinstance(value, FrozenDict), f"{type(model).__name__}.{field} is not a FrozenDict"
    # The whole value object is hashable.
    assert isinstance(hash(model), int)
    # In-place mutation of the mapping field raises.
    with pytest.raises(TypeError):
        value["injected"] = "boom"  # type: ignore[index]


def test_hash_of_minimal_principal_and_chunk() -> None:
    # Regression: these raised TypeError before FrozenDict (default {} was a dict).
    assert isinstance(hash(Principal(id="u")), int)
    assert isinstance(hash(Chunk(chunk_id="c", document_id="d", text="t")), int)


def test_default_empty_mapping_is_frozendict() -> None:
    assert isinstance(Principal(id="u").claims, FrozenDict)
    assert isinstance(Chunk(chunk_id="c", document_id="d", text="t").metadata, FrozenDict)


def test_attribute_reassignment_raises_validation_error() -> None:
    p = Principal(id="u")
    with pytest.raises(ValidationError):
        p.claims = {"x": "y"}  # type: ignore[misc]


def test_inplace_mutation_raises_typeerror() -> None:
    p = Principal(id="u", claims={"a": "b"})
    with pytest.raises(TypeError):
        p.claims["x"] = "y"  # type: ignore[index]
    c = Chunk(chunk_id="c", document_id="d", text="t", metadata={"k": 1})
    with pytest.raises(TypeError):
        c.metadata["k"] = 2  # type: ignore[index]


def test_model_dump_json_yields_plain_nested_structures() -> None:
    c = Chunk(chunk_id="c", document_id="d", text="t", metadata={"a": {"b": [1, 2]}})
    dumped = c.model_dump(mode="json")
    assert dumped["metadata"] == {"a": {"b": [1, 2]}}
    assert isinstance(dumped["metadata"], dict)
    assert isinstance(dumped["metadata"]["a"], dict)
    assert isinstance(dumped["metadata"]["a"]["b"], list)


def test_model_validate_roundtrip() -> None:
    c = Chunk(chunk_id="c", document_id="d", text="t", metadata={"a": {"b": [1, 2]}})
    restored = Chunk.model_validate(c.model_dump())
    assert restored == c
    assert hash(restored) == hash(c)
    assert isinstance(restored.metadata, FrozenDict)
    assert isinstance(restored.metadata["a"], FrozenDict)


# --------------------------------------------------------------------------- #
# thaw() — the public egress-boundary serializer (NE-GATEWAY-FROZENDICT).     #
#                                                                             #
# ``FrozenDict.__eq__`` intentionally compares equal to a plain ``dict``, so #
# a bare ``==`` assertion against an expected plain-builtin structure is     #
# VACUOUS here: it would pass whether or not ``thaw`` actually ran, because  #
# the untouched ``FrozenDict`` already compares equal to its thawed shape.   #
# Every assertion below therefore checks the CONCRETE runtime type via      #
# ``type(x) is dict`` / ``type(x) is list``, never ``==`` alone, and does so #
# at EVERY nesting level, not just the top one (a fix that thaws only one   #
# layer deep would otherwise slip through a shallow assertion undetected).  #
# --------------------------------------------------------------------------- #


def test_thaw_deep_nested_mappings_are_plain_dicts_at_every_level() -> None:
    fd = FrozenDict(
        {
            "level1": {
                "level2": {
                    "level3": {"leaf": 1},
                    "siblings": ({"a": 1}, {"b": 2}),
                },
            },
        }
    )
    assert isinstance(fd["level1"], FrozenDict)  # sanity: construction froze it

    result = thaw(fd)

    assert type(result) is dict
    assert type(result["level1"]) is dict
    level2 = result["level1"]["level2"]
    assert type(level2) is dict
    level3 = level2["level3"]
    assert type(level3) is dict
    assert level3 == {"leaf": 1}

    siblings = level2["siblings"]
    assert type(siblings) is list
    assert len(siblings) == 2
    for sibling in siblings:
        assert type(sibling) is dict
    assert siblings == [{"a": 1}, {"b": 2}]


def test_thaw_sequences_and_sets_become_lists() -> None:
    # Container-shape assertions ONLY (never leaf-value equality): this test
    # is the designated CONTROL for the "thaw's leaf branch is corrupted"
    # mutation probe (mutationPlan M4) — it must stay GREEN when only the
    # scalar leaf-passthrough branch is broken, proving the probe isolates
    # the container-vs-leaf branches from each other. Leaf-value fidelity is
    # covered separately by test_thaw_passes_exotic_hashable_leaf_through_by_identity.
    fd = FrozenDict({"tup": (1, 2, {"n": 3}), "fs": frozenset({"a", "b", "c"})})

    result = thaw(fd)

    assert type(result["tup"]) is list
    assert len(result["tup"]) == 3
    assert type(result["tup"][2]) is dict
    assert set(result["tup"][2].keys()) == {"n"}

    assert type(result["fs"]) is list
    assert len(result["fs"]) == 3


def test_thaw_passes_exotic_hashable_leaf_through_by_identity() -> None:
    leaf_dt = datetime(2026, 7, 3, tzinfo=UTC)
    leaf_enum = ApprovalTier.TIER_0
    fd = FrozenDict({"when": leaf_dt, "tier": leaf_enum})

    thawed = thaw(fd)

    # Pass-through is safe by construction (see thaw's docstring): every leaf
    # reaching thaw already survived _freeze_value's hashability check, so
    # there is nothing further to normalize about a non-container leaf —
    # thaw must hand back the EXACT SAME OBJECT, not a copy or a coercion.
    assert thawed["when"] is leaf_dt
    assert thawed["tier"] is leaf_enum


def test_thaw_public_name_and_private_alias_are_same_object() -> None:
    # Pins the ne-api compat alias (``from ne.contracts.frozen import _thaw``)
    # until the follow-up repoints it at the public export.
    assert _thaw is thaw
