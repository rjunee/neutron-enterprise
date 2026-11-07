# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""config_hash: SHA-256 over canonicalized resolved config, secret-invariant, frozen once."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
from pydantic import BaseModel, SecretStr

from ne.contracts import (
    ConfigHashMismatchError,
    ConfigHashNotFrozenError,
    compute_config_hash,
    freeze_config_hash,
    get_config_hash,
    reset_config_hash_for_testing,
)


class Sample(BaseModel):
    a: int = 1
    b: str = "x"
    data: dict[str, int] = {}
    secret: SecretStr = SecretStr("orig")


@pytest.fixture(autouse=True)
def _reset_frozen_hash() -> Iterator[None]:
    reset_config_hash_for_testing()
    yield
    reset_config_hash_for_testing()


def test_hash_is_64_char_hex() -> None:
    digest = compute_config_hash(Sample())
    assert len(digest) == 64
    int(digest, 16)  # raises if not hex


def test_hash_is_deterministic_across_instances() -> None:
    assert compute_config_hash(Sample()) == compute_config_hash(Sample())


def test_hash_is_key_order_independent() -> None:
    m1 = Sample(data={"a": 1, "b": 2, "c": 3})
    m2 = Sample(data={"c": 3, "b": 2, "a": 1})
    assert compute_config_hash(m1) == compute_config_hash(m2)


def test_hash_changes_when_any_field_changes() -> None:
    base = compute_config_hash(Sample())
    assert compute_config_hash(Sample(a=2)) != base
    assert compute_config_hash(Sample(b="y")) != base
    assert compute_config_hash(Sample(data={"k": 1})) != base


def test_hash_invariant_under_secret_rotation() -> None:
    original = compute_config_hash(Sample(secret=SecretStr("orig")))
    rotated = compute_config_hash(Sample(secret=SecretStr("rotated-value")))
    assert original == rotated


def test_secret_material_not_in_preimage() -> None:
    tree = Sample(secret=SecretStr("super-secret")).model_dump(mode="json")
    assert tree["secret"] == "**********"


def test_get_before_freeze_raises() -> None:
    with pytest.raises(ConfigHashNotFrozenError):
        get_config_hash()


def test_freeze_then_get_roundtrip() -> None:
    frozen = freeze_config_hash(Sample())
    assert get_config_hash() == frozen
    assert frozen == compute_config_hash(Sample())


def test_refreeze_identical_is_idempotent() -> None:
    first = freeze_config_hash(Sample())
    second = freeze_config_hash(Sample())
    assert first == second


def test_refreeze_different_raises_mismatch() -> None:
    freeze_config_hash(Sample())
    with pytest.raises(ConfigHashMismatchError):
        freeze_config_hash(Sample(a=999))


def test_concurrent_freeze_exactly_one_wins() -> None:
    """Two threads racing the first freeze with different configs: one winner.

    The loser must raise ConfigHashMismatchError (not silently overwrite), and
    ``get_config_hash()`` must equal the winner's hash. Repeated to shake out the
    check-then-set race deterministically.
    """
    cfg_a = Sample(a=1)
    cfg_b = Sample(a=2)
    hash_a = compute_config_hash(cfg_a)
    hash_b = compute_config_hash(cfg_b)
    assert hash_a != hash_b

    def worker(
        name: str, cfg: BaseModel, barrier: threading.Barrier, results: dict[str, object]
    ) -> None:
        barrier.wait()
        try:
            results[name] = freeze_config_hash(cfg)
        except ConfigHashMismatchError as exc:
            results[name] = exc

    for _ in range(50):
        reset_config_hash_for_testing()
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}
        t1 = threading.Thread(target=worker, args=("a", cfg_a, barrier, results))
        t2 = threading.Thread(target=worker, args=("b", cfg_b, barrier, results))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        winners = [k for k, v in results.items() if isinstance(v, str)]
        losers = [v for v in results.values() if isinstance(v, ConfigHashMismatchError)]
        assert len(winners) == 1, results
        assert len(losers) == 1, results
        winning_hash = results[winners[0]]
        assert get_config_hash() == winning_hash
        assert winning_hash in (hash_a, hash_b)

    reset_config_hash_for_testing()
