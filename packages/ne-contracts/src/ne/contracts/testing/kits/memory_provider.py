# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""MemoryProvider conformance kit (NE-RTSWAP phase (a) — runtime-slot kits).

Precedent: :mod:`ne.contracts.testing.kits.skill_substrate` (capabilities-aware,
factory hooks, ``_require`` skip helper). Async contract methods are exercised
via ``asyncio.run`` (pytest-asyncio is NOT a dependency and must not be added).

The window check asserts CONTAINMENT (every returned event's ``occurred_at``
falls within ``[since, until]``) plus the presence of a strictly-interior
event; boundary INCLUSIVITY (whether an event occurring exactly at ``since``
or ``until`` is returned) is deliberately NOT pinned — the Protocol says
nothing about it.

The ``limit`` and ``window`` checks both append events before recalling, so
they also require the ``append_recall`` capability — a binding that declares
``limit``/``window`` without ``append_recall`` cannot reach them and the
tests honestly skip.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from ne.contracts.runtime import MemoryEvent, MemoryProvider

_T0 = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class MemoryProviderContractKit:
    """Subclass and override :meth:`create_provider` to prove MemoryProvider conformance."""

    #: The capabilities this suite asserts. A partial binding overrides this to
    #: an honest subset so the gated checks skip rather than fail.
    capabilities: ClassVar[frozenset[str]] = frozenset({"append_recall", "limit", "window"})

    def create_provider(self) -> MemoryProvider:
        """Return a fresh, empty provider."""
        raise NotImplementedError

    def make_event(
        self, content: str, occurred_at: datetime, kind: str = "kit-note"
    ) -> MemoryEvent:
        return MemoryEvent(kind=kind, content=content, occurred_at=occurred_at, provenance="kit")

    def _require(self, cap: str) -> None:
        if cap not in self.capabilities:
            pytest.skip(f"provider does not declare the {cap!r} capability")

    def test_appended_event_recalled_by_content_query(self) -> None:
        self._require("append_recall")
        provider = self.create_provider()

        async def scenario() -> None:
            await provider.append(self.make_event("kit-fact quartz-zebra-17", _T0))
            recalled = await provider.recall("quartz-zebra-17")
            assert all(isinstance(e, MemoryEvent) for e in recalled)
            matches = [e for e in recalled if e.content == "kit-fact quartz-zebra-17"]
            assert len(matches) == 1
            assert matches[0].kind == "kit-note"
            assert matches[0].provenance == "kit"
            assert matches[0].occurred_at == _T0

        asyncio.run(scenario())

    def test_limit_caps_results(self) -> None:
        self._require("append_recall")
        self._require("limit")
        provider = self.create_provider()

        async def scenario() -> None:
            for i in range(25):
                await provider.append(
                    self.make_event(f"kit-bulk item {i}", _T0 + timedelta(minutes=i))
                )
            recalled = await provider.recall("kit-bulk", limit=5)
            assert len(recalled) == 5

        asyncio.run(scenario())

    def test_window_filters_by_occurred_at(self) -> None:
        self._require("append_recall")
        self._require("window")
        provider = self.create_provider()

        async def scenario() -> None:
            for i in range(10):
                await provider.append(
                    self.make_event(f"kit-window w{i}", _T0 + timedelta(minutes=i))
                )
            recalled = await provider.recall(
                "kit-window",
                since=_T0 + timedelta(minutes=7),
                until=_T0 + timedelta(minutes=9),
            )
            since = _T0 + timedelta(minutes=7)
            until = _T0 + timedelta(minutes=9)
            assert all(since <= e.occurred_at <= until for e in recalled)
            contents = {e.content for e in recalled}
            assert "kit-window w8" in contents  # strictly interior
            assert contents <= {"kit-window w7", "kit-window w8", "kit-window w9"}

        asyncio.run(scenario())
