# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the ne-eval tests.

Everything runs offline: a REAL in-memory SQLite engine for the eval-run store,
real ne-eval code over the ``ScriptedJudge``/``ScriptedTarget`` seams (the seams
NE-025 fills live — mocked AT the seam, never past it, plan §1.4). Async is
exercised via ``asyncio.run``; pytest-asyncio is NOT a dependency.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NoReturn

import pytest
import sqlalchemy as sa

from ne.brain.testing.qdrant_live import (
    OwnedQdrantCollections,
    qdrant_server_url,
    retry_pending_drops,
    sweep_named_server,
)
from ne.eval.golden import GoldenSet, load_golden_set
from ne.eval.store import ensure_eval_schema

SAMPLE_GOLDEN_PATH = Path(__file__).resolve().parent.parent / "goldensets" / "sample.jsonl"

#: Env flag that arms the fail-closed Checkpoint-C gate (NE-CKC6). CI sets it to
#: ``"1"``; local dev leaves it unset.
_REQUIRE_BASELINE_ENV = "NE_REQUIRE_PUBLIC_BASELINE"


def skip_or_fail_baseline(reason: str) -> NoReturn:
    """NE-CKC6 anti-vacuous-pass guard for the Checkpoint-C fixtures.

    The Checkpoint-C threshold proof (the public retrieval baseline and the ragas
    cross-validation oracle) used to *skip* — never fail — whenever its BM25 asset
    or ragas dev venv was unavailable, so ``bare pytest`` on CI could not tell
    "gate green" apart from "gate never ran": a regression below the SPEC floors
    could merge green if the fixture happened to skip on a network flake.

    This guard makes the behaviour depend on the ``NE_REQUIRE_PUBLIC_BASELINE``
    env flag:

    * flag == ``"1"`` (CI, fail-closed) → :func:`pytest.fail` — a missing asset or
      venv is a HARD failure, never a silent skip, so the gate must actually run.
    * flag unset / any other value (local dev) → :func:`pytest.skip` with a
      ``LIVE-PARKED`` reason, preserving the skip-if-unavailable local ergonomics.

    Wired ONLY into the two CI-runnable Checkpoint-C fixtures (bm25 encoder, ragas
    oracle); the live-parked tests gated on a NAMED Qdrant/TEI keep their own skips.
    """
    if os.environ.get(_REQUIRE_BASELINE_ENV) == "1":
        pytest.fail(
            f"NE_REQUIRE_PUBLIC_BASELINE=1 (fail-closed Checkpoint-C gate, NE-CKC6) but: {reason}",
            pytrace=False,
        )
    pytest.skip(f"LIVE-PARKED: {reason}")


@pytest.fixture(scope="session")
def baseline_guard() -> Callable[[str], NoReturn]:
    """Expose :func:`skip_or_fail_baseline` to test modules (NE-CKC6).

    Test modules request this fixture rather than importing the conftest directly:
    several packages ship a ``tests/conftest.py`` and, under pytest's default
    ``prepend`` import mode, importing "conftest" is a name-collision hazard.
    """
    return skip_or_fail_baseline


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_eval_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def sample_golden_path() -> Path:
    return SAMPLE_GOLDEN_PATH


@pytest.fixture
def sample_golden() -> GoldenSet:
    return load_golden_set(SAMPLE_GOLDEN_PATH)


@pytest.fixture
def owned_qdrant_collections() -> Iterator[OwnedQdrantCollections]:
    """The ONLY handle onto a real Qdrant server, and the janitor for what it makes.

    Twin of ``packages/ne-brain/tests/conftest.py``'s fixture — see
    ``ne.brain.testing.qdrant_live`` for the 2026-09-10 outage this shape exists to
    prevent. Teardown is a ``finally``: the live baseline's collection is dropped
    whether the run passed, failed or errored, which the old inline delete inside
    the happy path did not do.


    A delete that FAILS (or that the server rejects with ``False``) leaves the name
    in ``owner.pending`` and the owner registered for
    :func:`~ne.brain.testing.qdrant_live.retry_pending_drops`, which
    ``pytest_sessionfinish`` below drains — otherwise "kept for retry" would mean
    "dropped on the floor the moment this fixture goes out of scope".
    """
    url = qdrant_server_url()
    # Guaranteed by the module-level skip in every module that requests this.
    assert url is not None, "NE_QDRANT_URL is not set — this fixture must be gated"
    owner = OwnedQdrantCollections(url)
    try:
        yield owner
    finally:
        owner.drop_all()


def pytest_sessionfinish(session: pytest.Session, exitstatus: object) -> None:
    """Retry the teardown that failed, then sweep what teardown never reached.

    ⛔ WITHOUT THIS HOOK BOTH HALVES OF THE JANITOR ARE DECORATIVE.

    ``OwnedQdrantCollections.drop_all`` keeps a name whose delete did not succeed
    — but the fixture that owned it is discarded one line later, so "kept for
    retry" meant "dropped on the floor". :func:`retry_pending_drops` is the retry:
    it re-attempts only names THIS process minted, so it is safe on a worker.

    ``sweep_named_server`` covers what a ``finally`` cannot reach at all — a
    SIGKILLed xdist worker, a cancelled CI job — and until now nothing outside its
    own unit test ever called it, so those leaks accumulated forever. That is how
    985 of them got into the live store. It visits EVERY server a test-consent
    variable names (both of them — ``NE_TEST_QDRANT_URL`` is where the promoted
    source-authority gate runs), and none that no variable names.

    ⚠️ THE SWEEP IS CONTROLLER-ONLY, AND THE GUARD IS LOAD-BEARING. It deletes
    every ``<sanctioned prefix>_<uuid4>`` on the named server, including ones a
    sibling worker is using RIGHT NOW. ``config.workerinput`` exists only in an
    xdist worker, and the controller's ``pytest_sessionfinish`` runs after every
    worker has exited — so this is the one moment at which no live collection can
    be swept out from under anybody.
    """
    leaked = retry_pending_drops()
    if hasattr(session.config, "workerinput"):
        return  # a worker must never bulk-sweep: its siblings are still running
    swept = sweep_named_server()
    _report_qdrant_teardown(session, leaked, swept)


def _report_qdrant_teardown(
    session: pytest.Session, leaked: tuple[str, ...], swept: list[str]
) -> None:
    """Put leaks on the TERMINAL. A WARNING log at session end is read by nobody."""
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    if swept:
        reporter.write_line(
            f"qdrant: swept {len(swept)} orphaned test collection(s) left by teardown "
            f"that never ran: {', '.join(sorted(swept))}"
        )
    if leaked:
        reporter.write_line(
            f"qdrant: {len(leaked)} collection(s) could NOT be dropped and are "
            f"LEAKED on the named server(s): {', '.join(sorted(leaked))}"
        )
