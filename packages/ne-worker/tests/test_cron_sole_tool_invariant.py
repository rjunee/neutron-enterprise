# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""2026-08-29 U13 review (F6): the sole-tool cron invariant is STRUCTURAL.

63279abe's claim — the shared litellm Router "is never touched from the cron
path again" — held only for the surfaces that opted in. Three cron surfaces
(skill-gardener, skill-forge, answer-ledger-reask) kept the ``"agent"`` default
and would have put a per-fire model-arbitration loop back on the worker's
persistent tick loop the moment their config flags flipped on, with nothing red
anywhere.

Two layers of proof, deliberately:

* UNIT — ``require_sole_tool_definitions`` itself accepts/rejects correctly,
  with a sensitivity flip inside every accepting case so a pass is a verdict,
  not a no-op (the vacuous-guard lesson: an assertion-free positive proves
  nothing).
* COMPOSITION — ``ne.worker.main._compose_cron_scheduler`` is the single
  production assembly point ``_run()`` boots the cron path through, and these
  tests drive THAT function with rogue surfaces over a real sqlite platform.
  Deleting the guardian invocation from the composition (the exact regression
  the cross-model review flagged: every guardian test called the helper
  directly, so removing the boot invocation left them all green) turns
  ``test_boot_composition_rejects_agent_mode_surfaces`` red.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ne.contracts import reset_config_hash_for_testing
from ne.runtime.dispatch import AgentDefinition
from ne.worker.cron_seed import require_sole_tool_definitions
from ne.worker.main import _compose_cron_scheduler
from ne.worker.settings import WorkerSettings

from .conftest import build_sqlite_platform

# -- UNIT: the checker itself -------------------------------------------------


def _definition(name: str, *, execution: str) -> AgentDefinition:
    return AgentDefinition(
        name=name,
        instructions="call the tool once",
        toolset=(f"write.internal.{name}",),
        scheduled_execution=execution,  # type: ignore[arg-type]
    )


def test_all_sole_tool_definitions_pass_and_the_pass_is_a_verdict() -> None:
    definitions = {
        "a": _definition("a", execution="sole-tool"),
        "b": _definition("b", execution="sole-tool"),
    }
    assert require_sole_tool_definitions(definitions) is None  # accepts
    # Sensitivity: the SAME mapping with one entry flipped is rejected — proof
    # the accepting call above actually inspected what it was handed.
    with pytest.raises(RuntimeError, match=r"\['b'\]"):
        require_sole_tool_definitions({**definitions, "b": _definition("b", execution="agent")})


def test_empty_definitions_pass_and_a_single_offender_is_rejected() -> None:
    assert require_sole_tool_definitions({}) is None  # accepts
    with pytest.raises(RuntimeError, match=r"\['solo'\]"):
        require_sole_tool_definitions({"solo": _definition("solo", execution="agent")})


def test_an_agent_mode_definition_fails_loud_naming_every_offender() -> None:
    with pytest.raises(RuntimeError, match=r"\['rogue-a', 'rogue-b'\]"):
        require_sole_tool_definitions(
            {
                "ok": _definition("ok", execution="sole-tool"),
                "rogue-b": _definition("rogue-b", execution="agent"),
                "rogue-a": _definition("rogue-a", execution="agent"),
            }
        )


def test_the_default_execution_mode_is_what_fails() -> None:
    """The dangerous case is the DEFAULT: a new surface that never mentions
    scheduled_execution at all must be rejected, not silently admitted."""
    definition = AgentDefinition(
        name="new-surface",
        instructions="call the tool once",
        toolset=("write.internal.new_surface",),
    )
    assert definition.scheduled_execution == "agent"
    with pytest.raises(RuntimeError, match="new-surface"):
        require_sole_tool_definitions({"new-surface": definition})


# -- COMPOSITION: the guardian is bound to the production boot path -----------


@pytest.fixture
def _clean_cron_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the surfaces' env-driven config to the fail-closed defaults."""
    for var in (
        "NE_CONNECT_SYNC_SOURCES",
        "NE_SKILLS_FORGE_ENABLED",
        "NE_SKILLS_GARDENER_ENABLED",
        "NE_WORKER_ANSWER_LEDGER_REASK_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_config_hash_for_testing()


def _rogue_surface_builder(surface_cls: Any, *definitions: AgentDefinition) -> Any:
    """A surface builder stub returning agent-mode definitions and nothing else."""

    def _build(_platform: Any) -> Any:
        return surface_cls(
            bindings=(),
            definitions={d.name: d for d in definitions},
            principals={},
            jobs=(),
        )

    return _build


def test_boot_composition_rejects_agent_mode_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_cron_env: None
) -> None:
    """THE binding proof: _compose_cron_scheduler — the function _run() boots the
    cron path through — refuses to assemble when any surface hands it an
    agent-mode definition. Covers multiple offenders across two surfaces AND the
    silent-default case (one rogue is explicit "agent", the other never sets the
    field). Goes red if the require_sole_tool_definitions invocation is removed
    from the composition: the rogue surfaces carry no jobs or bindings, so
    without the guardian the scheduler assembles cleanly and no error is raised.
    """
    from ne.worker.skill_forge import SkillForgeSurface
    from ne.worker.skill_gardener import SkillGardenerSurface

    explicit_agent = _definition("rogue-gardener", execution="agent")
    silent_default = AgentDefinition(
        name="rogue-forge",
        instructions="call the tool once",
        toolset=("write.internal.rogue_forge",),
    )
    monkeypatch.setattr(
        "ne.worker.skill_gardener.build_skill_gardener_surface",
        _rogue_surface_builder(SkillGardenerSurface, explicit_agent),
    )
    monkeypatch.setattr(
        "ne.worker.skill_forge.build_skill_forge_surface",
        _rogue_surface_builder(SkillForgeSurface, silent_default),
    )

    platform = build_sqlite_platform(tmp_path)
    with pytest.raises(RuntimeError, match=r"\['rogue-forge', 'rogue-gardener'\]"):
        _compose_cron_scheduler(platform, WorkerSettings())


def test_boot_composition_accepts_the_real_surfaces_and_seeds_sole_tool_jobs(
    tmp_path: Path, _clean_cron_env: None
) -> None:
    """The real surfaces pass the boot guardian and the scheduler assembles.

    Contentful positive: the ALWAYS-ON credential-rotation job is actually
    seeded (so the composition ran end to end, not vacuously), and every job on
    the assembled scheduler maps to a sole-tool definition — the property the
    guardian exists to keep true of the production cron path.
    """
    platform = build_sqlite_platform(tmp_path)
    scheduler = _compose_cron_scheduler(platform, WorkerSettings())

    jobs = scheduler.list_jobs()
    names = [job.name for job in jobs]
    assert "identity-credential-rotation" in names
    definitions = scheduler._definitions  # composition proof reaches into the assembly
    for job in jobs:
        definition = definitions.get(job.agent)
        if definition is not None:
            assert definition.scheduled_execution == "sole-tool", job.name
