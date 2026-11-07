# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Test-only helpers for the connector framework.

This package imports nothing from ``.kits`` at import time, so non-test
consumers of ``ne.connect`` never need ``pytest`` installed (mirrors
``ne.brain.testing`` / ``ne.contracts.testing``). Import ``.kits`` submodules
directly when writing conformance tests. :mod:`ne.connect.testing.fixture_connector`
is import-cheap (stdlib only) and is the entry-point-registered ``fixture``
connector NE-044/045 recorded-fixture suites build on.
"""
