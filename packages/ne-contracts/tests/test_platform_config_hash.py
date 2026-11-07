# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Composed platform config_hash (ADR-0003, audit #7): covers ALL module configs."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from pydantic import BaseModel, SecretBytes, SecretStr

from ne.contracts import (
    ConfigHashMismatchError,
    ConfigHashNotFrozenError,
    canonical_platform_config_tree,
    compute_config_hash,
    compute_platform_config_hash,
    compute_platform_config_tree_hash,
    freeze_config_hash,
    freeze_platform_config_hash,
    get_config_hash,
    reset_config_hash_for_testing,
)


@pytest.fixture(autouse=True)
def _reset_frozen_hash() -> Iterator[None]:
    reset_config_hash_for_testing()
    yield
    reset_config_hash_for_testing()


class BrainSettings(BaseModel):
    top_k: int = 8


class GovernSettings(BaseModel):
    policy_bundle: str = "v1"
    deny_pii: bool = True


class GatewaySettings(BaseModel):
    default_model: str = "glm-5"
    api_key: SecretStr = SecretStr("k")
    signing_key: SecretBytes = SecretBytes(b"signing-key")


def modules(**overrides: BaseModel) -> dict[str, BaseModel]:
    """A fresh 3-module mapping; pass module_key=Settings(...) to override one."""
    base: dict[str, BaseModel] = {
        "brain": BrainSettings(),
        "govern": GovernSettings(),
        "gateway": GatewaySettings(),
    }
    base.update(overrides)
    return base


def test_platform_hash_is_64_char_hex() -> None:
    digest = compute_platform_config_hash(modules())
    assert len(digest) == 64
    int(digest, 16)  # raises if not hex


def test_resolved_tree_is_exact_json_safe_artifact_and_hash_source() -> None:
    tree = canonical_platform_config_tree(modules())
    assert tree == {
        "brain": {"top_k": 8},
        "gateway": {
            "api_key": "**********",
            "default_model": "glm-5",
            "signing_key": "**********",
        },
        "govern": {"deny_pii": True, "policy_bundle": "v1"},
    }
    assert json.loads(json.dumps(tree, allow_nan=False)) == tree
    assert compute_platform_config_tree_hash(tree) == compute_platform_config_hash(modules())


def test_resolved_tree_is_recursively_order_independent() -> None:
    class Nested(BaseModel):
        values: dict[str, dict[str, int]]

    left = {"mod": Nested(values={"b": {"y": 2, "x": 1}, "a": {"z": 3}})}
    right = {"mod": Nested(values={"a": {"z": 3}, "b": {"x": 1, "y": 2}})}
    assert canonical_platform_config_tree(left) == canonical_platform_config_tree(right)
    assert compute_platform_config_hash(left) == compute_platform_config_hash(right)


def test_resolved_tree_masks_valid_and_malformed_url_userinfo() -> None:
    class Dsns(BaseModel):
        nested: list[str]

    tree = canonical_platform_config_tree(
        {"db": Dsns(nested=["postgresql://u:secret@host/db", "redis://u:secret@[bad"])}
    )
    serialized = json.dumps(tree)
    assert "secret" not in serialized
    assert serialized.count("***:***@") == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"nested": {"access_token": "raw-token"}},
        {"nested": {"authorization": "Bearer raw-token"}},
        {"nested": {"raw_env": {"DATABASE_URL": "postgresql://u:p@host/db"}}},
    ],
)
def test_resolved_tree_fails_closed_on_plain_credentials_and_raw_env(
    payload: dict[str, object],
) -> None:
    class Unsafe(BaseModel):
        payload: dict[str, object]

    with pytest.raises(ValueError, match="forbidden"):
        canonical_platform_config_tree({"unsafe": Unsafe(payload=payload)})


def test_tree_recompute_canonicalizes_order_but_detects_value_change() -> None:
    tree = canonical_platform_config_tree(modules())
    reordered = dict(reversed(list(tree.items())))
    assert compute_platform_config_tree_hash(reordered) == compute_platform_config_tree_hash(tree)
    changed = json.loads(json.dumps(tree))
    changed["brain"]["top_k"] = 9
    assert compute_platform_config_tree_hash(changed) != compute_platform_config_tree_hash(tree)


def test_tree_recompute_rejects_non_json_and_non_finite_values() -> None:
    with pytest.raises(TypeError):
        compute_platform_config_tree_hash({"mod": {"bad": object()}})
    with pytest.raises(ValueError):
        compute_platform_config_tree_hash({"mod": {"bad": float("nan")}})


def test_platform_hash_deterministic_across_calls_and_instances() -> None:
    # Fresh but equal instances on each call -> identical digest twice.
    assert compute_platform_config_hash(modules()) == compute_platform_config_hash(modules())


def test_platform_hash_changes_when_any_single_module_changes() -> None:
    base = compute_platform_config_hash(modules())
    # Perturb exactly one field of exactly one module, per module key. Proves no
    # module is omitted from the preimage (the audit-#7 regression test).
    assert compute_platform_config_hash(modules(brain=BrainSettings(top_k=16))) != base
    assert compute_platform_config_hash(modules(govern=GovernSettings(policy_bundle="v2"))) != base
    assert compute_platform_config_hash(modules(govern=GovernSettings(deny_pii=False))) != base
    assert (
        compute_platform_config_hash(modules(gateway=GatewaySettings(default_model="glm-6")))
        != base
    )


def test_platform_hash_independent_of_mapping_insertion_order() -> None:
    forward = {
        "brain": BrainSettings(),
        "govern": GovernSettings(),
        "gateway": GatewaySettings(),
    }
    reversed_order = {
        "gateway": GatewaySettings(),
        "govern": GovernSettings(),
        "brain": BrainSettings(),
    }
    assert compute_platform_config_hash(forward) == compute_platform_config_hash(reversed_order)


def test_platform_hash_differs_from_each_single_module_hash() -> None:
    mapping = modules()
    composed = compute_platform_config_hash(mapping)
    for settings in mapping.values():
        assert composed != compute_config_hash(settings)


def test_platform_hash_empty_mapping_raises_value_error() -> None:
    with pytest.raises(ValueError):
        compute_platform_config_hash({})
    with pytest.raises(ValueError):
        freeze_platform_config_hash({})
    # Nothing was frozen by the failed freeze.
    with pytest.raises(ConfigHashNotFrozenError):
        get_config_hash()


def test_platform_hash_invariant_under_secret_rotation() -> None:
    original = compute_platform_config_hash(
        modules(gateway=GatewaySettings(api_key=SecretStr("k")))
    )
    rotated = compute_platform_config_hash(
        modules(gateway=GatewaySettings(api_key=SecretStr("rotated-value")))
    )
    assert original == rotated
    # Secret material absent from every per-module tree.
    for settings in modules(gateway=GatewaySettings(api_key=SecretStr("super-secret"))).values():
        tree = settings.model_dump(mode="json")
        assert "super-secret" not in repr(tree)


def test_freeze_platform_then_get_roundtrip() -> None:
    frozen = freeze_platform_config_hash(modules())
    assert frozen == compute_platform_config_hash(modules())
    assert get_config_hash() == frozen


def test_freeze_platform_idempotent_on_identical() -> None:
    first = freeze_platform_config_hash(modules())
    second = freeze_platform_config_hash(modules())
    assert first == second


def test_freeze_platform_divergent_raises_mismatch() -> None:
    frozen = freeze_platform_config_hash(modules())
    with pytest.raises(ConfigHashMismatchError):
        freeze_platform_config_hash(modules(brain=BrainSettings(top_k=99)))
    # Original hash still frozen.
    assert get_config_hash() == frozen


def test_single_and_platform_freeze_share_one_slot() -> None:
    # A single-module freeze and a platform freeze with a different digest
    # contend for the ONE process-global slot -> mismatch.
    freeze_config_hash(BrainSettings())
    with pytest.raises(ConfigHashMismatchError):
        freeze_platform_config_hash(modules())
