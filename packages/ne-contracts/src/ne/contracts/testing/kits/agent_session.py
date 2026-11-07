# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AgentSession conformance kit (NE-RTSWAP phase (a) — runtime-slot kits).

Precedent: :mod:`ne.contracts.testing.kits.skill_substrate` (capabilities-aware,
factory hooks, ``_require`` skip helper). Async contract methods are exercised
via ``asyncio.run`` (pytest-asyncio is NOT a dependency and must not be added).

The kit asserts the ``run``/``close`` OBSERVABLE contract: a run returns an
``AgentRunResult`` shaped correctly and bound to the session that produced it,
and ``close()`` completes cleanly. ``result.session_id`` echoing the session's
own ``session_id`` is this kit's deliberate reading of the field — the only
meaningful one, though the Protocol docstring does not spell it out. Output
NON-EMPTINESS is deliberately NOT asserted: the frozen ``AgentRunResult.output``
field carries no such invariant.

Every test that creates a session closes it before returning (in a
``finally`` block where a preceding assertion could raise), so a binding
whose sessions hold real resources (connections, workers) is never leaked by
a conformance run.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from ne.contracts.runtime import AgentRunResult, AgentSession
from ne.contracts.types import Principal


class AgentSessionContractKit:
    """Subclass and override the factory hooks to prove AgentSession conformance."""

    #: The capabilities this suite asserts. A partial binding overrides this to
    #: an honest subset so the gated checks skip rather than fail.
    capabilities: ClassVar[frozenset[str]] = frozenset({"run", "context"})

    def create_session(self) -> AgentSession:
        """Return a fresh session able to complete at least two run() calls to a
        final text answer."""
        raise NotImplementedError

    def session_principal(self) -> Principal:
        """Return a principal this session accepts."""
        raise NotImplementedError

    def _require(self, cap: str) -> None:
        if cap not in self.capabilities:
            pytest.skip(f"session does not declare the {cap!r} capability")

    # -- session identity ----------------------------------------------------

    def test_session_identity_nonempty(self) -> None:
        session = self.create_session()

        async def scenario() -> None:
            try:
                assert isinstance(session.session_id, str) and session.session_id
                assert isinstance(session.agent_name, str) and session.agent_name
            finally:
                await session.close()

        asyncio.run(scenario())

    # -- run/close observable contract ---------------------------------------

    def test_run_returns_result_bound_to_session(self) -> None:
        self._require("run")
        session = self.create_session()

        async def scenario() -> None:
            try:
                result = await session.run(
                    "kit conformance prompt", principal=self.session_principal()
                )
                assert isinstance(result, AgentRunResult)
                assert isinstance(result.output, str)
                assert result.session_id == session.session_id
            finally:
                await session.close()

        asyncio.run(scenario())

    def test_run_accepts_context_mapping(self) -> None:
        self._require("context")
        session = self.create_session()

        async def scenario() -> None:
            try:
                result = await session.run(
                    "kit conformance prompt",
                    principal=self.session_principal(),
                    context={"kit_key": "kit_value"},
                )
                # The Protocol only admits the Mapping — no context semantics asserted.
                assert isinstance(result, AgentRunResult)
            finally:
                await session.close()

        asyncio.run(scenario())

    def test_close_after_run_returns_none(self) -> None:
        self._require("run")
        session = self.create_session()

        async def scenario() -> None:
            await session.run("kit conformance prompt", principal=self.session_principal())
            assert (await session.close()) is None

        asyncio.run(scenario())
