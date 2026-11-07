# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Composition seam for ne-flywheel (mirrors ne-events/ne-runtime factory style)."""

from __future__ import annotations

import sqlalchemy as sa

from ne.audit import ensure_transactional_sqlite
from ne.events import ensure_outbox_schema
from ne.flywheel.capture import CorrectionSignalCapture
from ne.flywheel.config import FlywheelSettings
from ne.flywheel.curation import CurationAgent, ensure_curation_schema
from ne.flywheel.errors import FlywheelError
from ne.flywheel.review import ProposalReviewService, ensure_review_schema
from ne.flywheel.store import ensure_flywheel_schema


def capture_from_settings(settings: FlywheelSettings | None = None) -> CorrectionSignalCapture:
    """Build a :class:`CorrectionSignalCapture` over ``settings.signal_database_url``.

    Raises :class:`FlywheelError` if ``signal_database_url`` is empty (a caller
    must configure ``NE_FLYWHEEL_SIGNAL_DATABASE_URL`` / ``ne.toml``, or
    construct ``CorrectionSignalCapture(engine)`` directly). Ensures BOTH the
    flywheel schema and the ne-events outbox schema on the resulting engine.
    """
    settings = settings or FlywheelSettings()
    if not settings.signal_database_url:
        raise FlywheelError(
            "FlywheelSettings.signal_database_url is empty; set "
            "NE_FLYWHEEL_SIGNAL_DATABASE_URL or construct "
            "CorrectionSignalCapture(engine) directly"
        )
    engine = sa.create_engine(settings.signal_database_url)
    ensure_transactional_sqlite(engine)
    ensure_flywheel_schema(engine)
    ensure_outbox_schema(engine)
    return CorrectionSignalCapture(engine, max_text_chars=settings.max_text_chars)


def curation_agent_from_settings(
    settings: FlywheelSettings | None = None,
) -> CurationAgent:
    """Build a :class:`CurationAgent` over ``settings.signal_database_url``.

    Raises :class:`FlywheelError` if ``signal_database_url`` is empty (same guard
    as :func:`capture_from_settings`). Ensures the FULL curation schema closure
    (proposals + cursor + signals + outbox + audit chain) on the resulting
    engine so a run's proposals and its dual-appended audit event commit
    atomically on the SAME engine.
    """
    settings = settings or FlywheelSettings()
    if not settings.signal_database_url:
        raise FlywheelError(
            "FlywheelSettings.signal_database_url is empty; set "
            "NE_FLYWHEEL_SIGNAL_DATABASE_URL or construct CurationAgent(engine) "
            "directly"
        )
    engine = sa.create_engine(settings.signal_database_url)
    ensure_transactional_sqlite(engine)
    ensure_curation_schema(engine)
    return CurationAgent(engine, batch_limit=settings.curation_batch_limit)


def review_service_from_settings(
    settings: FlywheelSettings | None = None,
) -> ProposalReviewService:
    """Build a :class:`ProposalReviewService` over ``settings.signal_database_url``.

    Raises :class:`FlywheelError` if ``signal_database_url`` is empty (same guard
    as :func:`capture_from_settings`). Ensures the FULL review schema closure
    (reviews + proposals + signals + outbox + audit chain) on the resulting
    engine so a transition's review row, any rejection feedback signal, and its
    dual-appended audit event commit atomically on the SAME engine.
    """
    settings = settings or FlywheelSettings()
    if not settings.signal_database_url:
        raise FlywheelError(
            "FlywheelSettings.signal_database_url is empty; set "
            "NE_FLYWHEEL_SIGNAL_DATABASE_URL or construct "
            "ProposalReviewService(engine) directly"
        )
    engine = sa.create_engine(settings.signal_database_url)
    ensure_transactional_sqlite(engine)
    ensure_review_schema(engine)
    return ProposalReviewService(engine, max_text_chars=settings.max_text_chars)
