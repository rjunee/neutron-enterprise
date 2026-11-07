# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the ne-gateway suite.

Nothing is mocked past the litellm network boundary (plan §1.4): the REAL
``Router`` runs end-to-end, driven by litellm's ``mock_response`` param (a
string for success, an ``Exception`` instance to force a provider error). The
outbox writes execute real SQLAlchemy against in-memory SQLite. Only where a
test must capture the exact kwargs forwarded to litellm is ``Router.acompletion``
monkeypatched.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts import AuditVerification, Lane, freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import RedactionResult
from ne.events import ensure_outbox_schema
from ne.gateway import GatewaySettings, ModelDef, TaskRoute
from ne.schema import CostEvent


class _DemoSettings(NeSettings):
    demo: int = 11


def make_model(
    name: str,
    *,
    mock_response: Any = "ok",
    lane: Lane = Lane.FRONTIER_API,
    litellm_model: str = "gpt-3.5-turbo",
    provider: str = "openai",
    api_base: str | None = None,
    context_window: int = 32768,
    max_output_tokens: int | None = None,
    **extra: Any,
) -> ModelDef:
    """Build a ModelDef whose extra_params carry the sanctioned litellm boundary
    mock (``mock_response``) plus a dummy api_key so the real Router runs.

    ``api_base`` is an optional top-level ModelDef field (needed for the
    private-vllm lane, which the NE-012 egress policy requires to be set).
    ``context_window`` defaults to a comfortable 32768 so existing routing tests
    (short prompts) always pass the NE-CTXB-2a budget pre-flight; a test proving
    the pre-flight passes a small window explicitly."""
    return ModelDef(
        name=name,
        litellm_model=litellm_model,
        provider=provider,
        lane=lane,
        api_base=api_base,
        extra_params={"api_key": "test", "mock_response": mock_response, **extra},
        context_window=context_window,
        **({"max_output_tokens": max_output_tokens} if max_output_tokens is not None else {}),
    )


def make_settings(
    models: list[ModelDef],
    tasks: dict[str, TaskRoute],
    **kwargs: Any,
) -> GatewaySettings:
    return GatewaySettings(models=models, tasks=tasks, **kwargs)


class RecordingCostSink:
    """A real CostSink implementation that appends emitted events to a list."""

    def __init__(self) -> None:
        self.events: list[CostEvent] = []

    def emit(self, event: CostEvent) -> None:
        self.events.append(event)


@pytest.fixture
def sink() -> RecordingCostSink:
    return RecordingCostSink()


class RecordingAuditSink:
    """A test double structurally satisfying the frozen ``ne.contracts.AuditSink``
    Protocol: it records appended events. ``.events`` is typed with the frozen,
    minimal ``AuditEventLike`` surface (which does NOT expose
    ``.outcome``/``.action``/``.resource``/``.detail``), so a test reading those
    fields must first narrow the event with ``isinstance(event, AuditEvent)``."""

    def __init__(self) -> None:
        self.events: list[AuditEventLike] = []

    async def append(self, event: AuditEventLike) -> None:
        self.events.append(event)

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        raise NotImplementedError(
            "RecordingAuditSink is a test double; verify() is not exercised by these tests"
        )


@pytest.fixture
def audit_sink() -> RecordingAuditSink:
    return RecordingAuditSink()


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_outbox_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def frozen_config() -> Iterator[str]:
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


# -- NE-162 redactor stubs (real Redactor Protocol implementations; no mocking
# past the redaction seam, no live Presidio venv) -----------------------------


class PassthroughRedactor:
    """A ``Redactor`` that never redacts anything — the mechanical stub used to
    satisfy the fail-closed ``redactor is not None`` requirement in tests that
    are not exercising redaction *content*, only that the seam is wired."""

    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        return RedactionResult(text=text)


class SubstringRedactor:
    """A ``Redactor`` that replaces every occurrence of ``needle`` with
    ``replacement``, tallying one ``category`` hit per occurrence — enough to
    make hit/category counts and redacted-vs-untouched bodies testable without
    a live Presidio venv."""

    def __init__(
        self,
        needle: str = "SECRET",
        replacement: str = "[REDACTED]",
        category: str = "TEST_SECRET",
    ) -> None:
        self._needle = needle
        self._replacement = replacement
        self._category = category

    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        n_occurrences = text.count(self._needle)
        if n_occurrences == 0:
            return RedactionResult(text=text)
        return RedactionResult(
            text=text.replace(self._needle, self._replacement),
            redactions=(self._category,) * n_occurrences,
        )


PASSTHROUGH_REDACTOR = PassthroughRedactor()


# -- litellm module-global test isolation (NE-162 continuation) --------------

_LITELLM_GLOBAL_LISTS: tuple[str, ...] = (
    "success_callback",
    "failure_callback",
    "_async_success_callback",
    "_async_failure_callback",
    "callbacks",
)


@pytest.fixture(autouse=True)
def _isolate_litellm_callback_globals() -> Iterator[None]:
    """Reset/restore litellm's module-global callback lists around every test.

    Every real ``litellm.Router.__init__`` appends
    ``Router.sync_deployment_callback_on_success`` to the MODULE-GLOBAL
    ``litellm.success_callback`` list (and similar globals). A real-Router test
    running anywhere earlier in the SAME pytest session (e.g.
    ``packages/ne-api/tests/test_gateway_redaction_wiring.py``, which is
    collected before ``ne-gateway``'s own tests in a combined
    ``uv run pytest`` invocation) therefore POLLUTES these globals for every
    test collected afterwards — including
    ``test_c3_chokepoint.py::test_telemetry_forced_off``, which asserts the
    chokepoint's empty-list baseline.

    This fixture resets each global to empty BEFORE every test (re-forcing the
    ``ne.gateway._litellm`` chokepoint baseline so ne-gateway's own suite is
    collection-order-independent) and restores the exact PRE-TEST snapshot
    AFTER, so a test collected later in the same session (outside this
    package) still observes whatever pollution it would have seen without this
    fixture — this fixture is non-invasive to the rest of the session, it only
    guarantees order-independence WITHIN ne-gateway's own tests.

    Imports litellm ONLY via ``ne.gateway._litellm`` (never ``import litellm``
    directly) to respect the C3 import chokepoint.
    """
    from ne.gateway._litellm import litellm

    lists = {
        name: getattr(litellm, name)
        for name in _LITELLM_GLOBAL_LISTS
        if isinstance(getattr(litellm, name, None), list)
    }
    snapshot = {name: list(values) for name, values in lists.items()}
    for values in lists.values():
        values[:] = []
    try:
        yield
    finally:
        for name, values in lists.items():
            values[:] = snapshot[name]
