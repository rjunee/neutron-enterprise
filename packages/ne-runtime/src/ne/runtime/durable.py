# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin ``@ne_workflow`` / ``@ne_step`` wrappers over DBOS Transact.

Honesty note (documented, not papered over): the v1 agent loop is NOT itself
DBOS-decorated — composing DBOS around a live pydantic-ai run needs
serializable-args turn functions and a launched DBOS instance. Instead, a
composition root wraps ITS turn functions (serializable in/out) with
``@ne_workflow`` / ``@ne_step``; the README shows the pattern. The real
DBOS-Postgres exercise is LIVE-PARKED (``test_dbos_live.py``, needs Postgres +
``NE_LIVE_DBOS=1``); CI covers passthrough semantics (``durability='off'``) and
fake-DBOS decorator application (``durability='dbos'`` with ``_load_dbos``
monkeypatched).

Durability is read from ``RuntimeSettings().durability`` at DECORATION time:

* ``'off'`` (default) — the decorators are transparent passthroughs that preserve
  the function's name, signature, and sync/async nature (the function is returned
  unchanged, only ``__name__`` honored for the optional ``name`` kwarg).
* ``'dbos'`` — the real ``DBOS.workflow(name=...)`` / ``DBOS.step(name=...)`` are
  applied. ``_load_dbos`` is the single import seam (monkeypatched in tests).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, overload

from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import DurabilityUnavailableError


def _load_dbos() -> Any:
    """Import and return the ``dbos`` module (the single monkeypatch seam for tests)."""
    import dbos

    return dbos


def _durability() -> str:
    return RuntimeSettings().durability


@overload
def ne_workflow[F: Callable[..., Any]](fn: F) -> F: ...
@overload
def ne_workflow[F: Callable[..., Any]](
    fn: None = None, *, name: str | None = None
) -> Callable[[F], F]: ...
def ne_workflow(fn: Callable[..., Any] | None = None, *, name: str | None = None) -> Any:
    """Mark a turn function as a durable DBOS workflow (passthrough when off).

    Usable bare (``@ne_workflow``) or with kwargs (``@ne_workflow(name="turn")``).
    """

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        if _durability() == "dbos":
            dbos = _load_dbos()
            return dbos.DBOS.workflow(name=name or func.__name__)(func)  # type: ignore[no-any-return]
        return func

    if fn is not None:
        return decorate(fn)
    return decorate


@overload
def ne_step[F: Callable[..., Any]](fn: F) -> F: ...
@overload
def ne_step[F: Callable[..., Any]](
    fn: None = None, *, name: str | None = None
) -> Callable[[F], F]: ...
def ne_step(fn: Callable[..., Any] | None = None, *, name: str | None = None) -> Any:
    """Mark a function as a durable DBOS step (passthrough when off).

    Usable bare (``@ne_step``) or with kwargs (``@ne_step(name="charge")``).
    """

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        if _durability() == "dbos":
            dbos = _load_dbos()
            return dbos.DBOS.step(name=name or func.__name__)(func)  # type: ignore[no-any-return]
        return func

    if fn is not None:
        return decorate(fn)
    return decorate


def launch_durable_runtime(*, name: str = "ne-runtime", database_url: str) -> Any:
    """Construct and launch the DBOS runtime (real DBOS-Postgres path, live-parked).

    Raises :class:`DurabilityUnavailableError` unless ``RuntimeSettings().durability``
    is ``'dbos'`` — passthrough mode has no DBOS instance to launch. Returns the
    constructed ``DBOS`` instance after ``launch()``.
    """
    if _durability() != "dbos":
        raise DurabilityUnavailableError(
            "launch_durable_runtime requires NE_RUNTIME_DURABILITY=dbos; durability is "
            f"{_durability()!r} (passthrough mode has no DBOS runtime to launch)"
        )
    dbos = _load_dbos()
    instance = dbos.DBOS(config=dbos.DBOSConfig(name=name, database_url=database_url))
    dbos.DBOS.launch()
    return instance
