# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The contract-conformance test-kit pattern.

A *kit* is a class named ``<Protocol>ContractKit`` — deliberately NOT prefixed
``Test`` so pytest never collects the abstract base itself. An adapter package
proves conformance by subclassing the kit in its own tests and overriding the
abstract ``create_*()`` factory::

    from ne.contracts.testing.kits.object_store import ObjectStoreContractKit

    class TestMyObjectStore(ObjectStoreContractKit):
        def create_store(self) -> ObjectStore:
            return MyObjectStore(...)

pytest collects ``TestMyObjectStore`` (name matches ``Test*``), inherits every
``test_*`` method from the kit, and runs the full contract against the real
adapter — no mocking past the seam under test.

Kits are **contract law** (plan section 2.4): changing a kit requires a PR-body
note plus synthesis sign-off, exactly like changing a Protocol signature.

These modules import ``pytest`` (ship the ``testkit`` extra to get it); the
reference adapters in :mod:`ne.contracts.testing.memory` do not.

Shipped kits: :mod:`~ne.contracts.testing.kits.object_store`,
:mod:`~ne.contracts.testing.kits.secret_store`, and
:mod:`~ne.contracts.testing.kits.skill_substrate` (the last is
capabilities-aware — a read-only binding runs an honest subset by overriding
``capabilities``). NE-RTSWAP phase (a) adds the five runtime-slot kits proving
the runtime Protocols are real, testable seams:
:mod:`~ne.contracts.testing.kits.agent_session`,
:mod:`~ne.contracts.testing.kits.tool_registry`,
:mod:`~ne.contracts.testing.kits.sub_agent_dispatcher`,
:mod:`~ne.contracts.testing.kits.scheduler`, and
:mod:`~ne.contracts.testing.kits.memory_provider` (all capabilities-aware
like ``skill_substrate``).

ADR-0021 adds :mod:`~ne.contracts.testing.kits.inference_operator`, whose
test-only mutation counter proves an idempotent retry does not execute a second
provider or Compose mutation merely because it returns the same-shaped value.
"""
