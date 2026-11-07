# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic Meridian Bank demo corpus (BD-1)."""

from ne.meridian.constants import MERIDIAN_DECLARATION, DeclarationSpec
from ne.meridian.generator import DEFAULT_SEED, generate, serialize

__all__ = [
    "DEFAULT_SEED",
    "MERIDIAN_DECLARATION",
    "DeclarationSpec",
    "generate",
    "serialize",
]
