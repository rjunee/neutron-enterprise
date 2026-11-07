# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SubAgentDispatcher conformance kit (NE-RTSWAP phase (a) — runtime-slot kits).

Precedent: :mod:`ne.contracts.testing.kits.skill_substrate` (capabilities-aware,
factory hooks, ``_require`` skip helper). Async contract methods are exercised
via ``asyncio.run`` (pytest-asyncio is NOT a dependency and must not be added).

``result.agent`` echoing ``spec.agent`` is this kit's deliberate reading of the
field — the minimal meaningful one, though the Protocol docstring does not
spell it out.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from ne.contracts.runtime import SubAgentDispatcher, SubAgentResult, SubAgentSpec


class SubAgentDispatcherContractKit:
    """Subclass and override the factory hooks to prove SubAgentDispatcher conformance."""

    #: The capabilities this suite asserts. A partial binding overrides this to
    #: an honest subset so the gated checks skip rather than fail.
    capabilities: ClassVar[frozenset[str]] = frozenset({"dispatch"})

    def create_dispatcher(self) -> SubAgentDispatcher:
        """Return a fresh dispatcher."""
        raise NotImplementedError

    def make_spec(self) -> SubAgentSpec:
        """Return a spec this dispatcher can run to successful completion."""
        raise NotImplementedError

    def _require(self, cap: str) -> None:
        if cap not in self.capabilities:
            pytest.skip(f"dispatcher does not declare the {cap!r} capability")

    def test_dispatch_returns_result_for_spec_agent(self) -> None:
        self._require("dispatch")
        dispatcher = self.create_dispatcher()
        spec = self.make_spec()

        async def scenario() -> None:
            result = await dispatcher.dispatch(spec)
            assert isinstance(result, SubAgentResult)
            assert result.agent == spec.agent
            assert isinstance(result.output, str)
            assert result.succeeded is True

        asyncio.run(scenario())

    def test_sequential_dispatches_both_succeed(self) -> None:
        self._require("dispatch")
        dispatcher = self.create_dispatcher()

        async def scenario() -> None:
            first = await dispatcher.dispatch(self.make_spec())
            second = await dispatcher.dispatch(self.make_spec())
            assert first.succeeded is True
            assert second.succeeded is True

        asyncio.run(scenario())
