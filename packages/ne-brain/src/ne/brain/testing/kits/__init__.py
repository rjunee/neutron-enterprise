# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ChunkStore/KnowledgeRead contract-conformance test-kit pattern.

Mirrors :mod:`ne.contracts.testing.kits` exactly: a *kit* is a class named
``<Protocol>ContractKit`` — deliberately NOT prefixed ``Test`` so pytest never
collects the abstract base itself. An adapter proves conformance by
subclassing the kit in its own tests and overriding the abstract
``create_*()`` factories.

**Location deviation (flagged, not silent):** ``ne.contracts`` is FROZEN as of
v1.0.0, so these kits cannot live under ``ne.contracts.testing.kits`` even
though SPEC section 4 says contract kits ship alongside their Protocol. They
ship here, under ``ne.brain.testing.kits``, using the identical kit-law
pattern, with a PR-body proposal to relocate them into
``ne.contracts.testing.kits`` via a small additive-exempt contracts rider —
so that NE-091 (pgvector) can reuse them without re-deriving the same tests.

Kits are **contract law** (orchestration plan section 2.4): changing a kit
requires a PR-body note plus synthesis sign-off, exactly like changing a
Protocol signature.

These modules import ``pytest`` (ship the ``testkit`` extra to get it); the
reference adapters in :mod:`ne.brain.testing.embedding` do not.
"""
