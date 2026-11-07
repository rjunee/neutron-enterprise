# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Durability wrappers — passthrough (off) + fake-DBOS decorator application (dbos)."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import ClassVar

import pytest
from pydantic import ValidationError

import ne.runtime.durable as durable
from ne.runtime import ne_step, ne_workflow
from ne.runtime.config import RuntimeSettings
from ne.runtime.durable import launch_durable_runtime
from ne.runtime.errors import DurabilityUnavailableError

# --- durability = 'off' (default): transparent passthrough ----------------


def test_passthrough_preserves_async_and_result_bare() -> None:
    @ne_workflow
    async def turn(x: int) -> int:
        return x + 1

    assert inspect.iscoroutinefunction(turn)
    assert turn.__name__ == "turn"
    assert asyncio.run(turn(1)) == 2


def test_passthrough_preserves_sync_and_result_kwargs_form() -> None:
    @ne_step(name="charge")
    def charge(amount: int) -> int:
        return amount * 2

    assert not inspect.iscoroutinefunction(charge)
    assert charge.__name__ == "charge"
    assert charge(5) == 10
    # Signature preserved (passthrough returns the function unchanged).
    assert list(inspect.signature(charge).parameters) == ["amount"]


def test_workflow_kwargs_form_passthrough() -> None:
    @ne_workflow(name="named")
    async def wf() -> str:
        return "done"

    assert asyncio.run(wf()) == "done"


# --- durability = 'dbos': real decorators applied (fake module) -----------


class _FakeDBOS:
    workflow_names: ClassVar[list[str | None]] = []
    step_names: ClassVar[list[str | None]] = []

    @classmethod
    def workflow(cls, *, name: str | None = None):
        def deco(fn):
            cls.workflow_names.append(name)
            return fn

        return deco

    @classmethod
    def step(cls, *, name: str | None = None):
        def deco(fn):
            cls.step_names.append(name)
            return fn

        return deco


def test_dbos_mode_applies_decorators(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeDBOS.workflow_names.clear()
    _FakeDBOS.step_names.clear()
    monkeypatch.setenv("NE_RUNTIME_DURABILITY", "dbos")
    monkeypatch.setattr(durable, "_load_dbos", lambda: SimpleNamespace(DBOS=_FakeDBOS))

    @ne_workflow(name="turn-wf")
    async def turn() -> str:
        return "ok"

    @ne_step
    def charge() -> str:
        return "charged"

    assert _FakeDBOS.workflow_names == ["turn-wf"]
    # Bare form defaults the name to the function's __name__.
    assert _FakeDBOS.step_names == ["charge"]


def test_launch_requires_dbos_mode() -> None:
    # Default durability is 'off' — no DBOS runtime to launch.
    with pytest.raises(DurabilityUnavailableError):
        launch_durable_runtime(database_url="postgresql://ignored")


def test_launch_constructs_and_launches_in_dbos_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_RUNTIME_DURABILITY", "dbos")
    launched: list[str] = []

    class FakeDBOSInstance:
        def __init__(self, *, config) -> None:
            self.config = config

        @classmethod
        def launch(cls) -> None:
            launched.append("launched")

    def fake_config(**kwargs):
        return kwargs

    monkeypatch.setattr(
        durable,
        "_load_dbos",
        lambda: SimpleNamespace(DBOS=FakeDBOSInstance, DBOSConfig=fake_config),
    )
    launch_durable_runtime(name="ne-runtime", database_url="postgresql://x/db")
    assert launched == ["launched"]


# --- config validation ----------------------------------------------------


def test_unknown_durability_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings(durability="maybe")  # type: ignore[arg-type]
