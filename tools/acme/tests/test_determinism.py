# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Determinism: same seed -> byte-identical corpus; different seed -> differs."""

from __future__ import annotations

from ne.acme.generator import generate, serialize


def test_same_seed_byte_identical() -> None:
    first = serialize(generate(74), 74)
    second = serialize(generate(74), 74)
    assert first.keys() == second.keys()
    for name in first:
        assert first[name] == second[name], f"{name} differs across identical-seed runs"


def test_different_seed_differs() -> None:
    seed_74 = serialize(generate(74), 74)
    seed_75 = serialize(generate(75), 75)
    assert seed_74["records.json"] != seed_75["records.json"]
