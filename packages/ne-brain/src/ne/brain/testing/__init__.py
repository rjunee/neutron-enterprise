# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Test-only helpers for ne-brain adapters.

This package (and :mod:`ne.brain.testing.embedding`) imports nothing from
``.kits`` at import time, so non-test consumers of ``ne.brain`` never need
``pytest`` installed (mirrors ``ne.contracts.testing``). Import ``.kits``
submodules directly when writing conformance tests.
"""
