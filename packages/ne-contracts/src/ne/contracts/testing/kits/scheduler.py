# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Scheduler conformance kit (NE-RTSWAP phase (a) — runtime-slot kits).

Precedent: :mod:`ne.contracts.testing.kits.skill_substrate` (capabilities-aware,
factory hooks, ``_require`` skip helper). ``Scheduler`` is a SYNC Protocol for
registration — no ``asyncio.run`` here.

``test_list_jobs_reflects_schedule_with_job_id_set`` asserts the Protocol
docstring's DOCUMENTED invariant: "``list_jobs()`` returns jobs with ``job_id``
set (the id returned by ``schedule()``)". Cancel-of-an-unknown-id behavior is
undocumented and deliberately unasserted.

``cancel`` is exercised via ``schedule()`` first, so the cancel-gated test
also requires the ``schedule`` capability — a binding that declares ``cancel``
without ``schedule`` cannot reach it and the test honestly skips.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from ne.contracts.runtime import ScheduledJob, Scheduler


class SchedulerContractKit:
    """Subclass and override the factory hooks to prove Scheduler conformance."""

    #: The capabilities this suite asserts. A partial binding overrides this to
    #: an honest subset so the gated checks skip rather than fail.
    capabilities: ClassVar[frozenset[str]] = frozenset({"schedule", "cancel"})

    def create_scheduler(self) -> Scheduler:
        """Return a fresh scheduler."""
        raise NotImplementedError

    def make_job(self, name: str) -> ScheduledJob:
        """Return a valid job named ``name`` this scheduler accepts — cron/agent/
        toolset are binding-owned."""
        raise NotImplementedError

    def _require(self, cap: str) -> None:
        if cap not in self.capabilities:
            pytest.skip(f"scheduler does not declare the {cap!r} capability")

    def test_schedule_returns_nonempty_job_id(self) -> None:
        self._require("schedule")
        scheduler = self.create_scheduler()
        job_id = scheduler.schedule(self.make_job("kit-job-a"))
        assert isinstance(job_id, str) and job_id

    def test_list_jobs_reflects_schedule_with_job_id_set(self) -> None:
        self._require("schedule")
        scheduler = self.create_scheduler()
        submitted = self.make_job("kit-job-b")
        job_id = scheduler.schedule(submitted)
        found = [j for j in scheduler.list_jobs() if j.name == "kit-job-b"]
        assert len(found) == 1
        assert found[0].job_id == job_id
        assert (found[0].cron, found[0].agent, found[0].toolset) == (
            submitted.cron,
            submitted.agent,
            submitted.toolset,
        )

    def test_cancel_removes_job_from_listing(self) -> None:
        self._require("schedule")
        self._require("cancel")
        scheduler = self.create_scheduler()
        job_id = scheduler.schedule(self.make_job("kit-job-c"))
        scheduler.cancel(job_id)
        assert not any(j.job_id == job_id or j.name == "kit-job-c" for j in scheduler.list_jobs())
