# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Composition seam for ne-obs KPI reporting (mirrors ne-flywheel/factory.py style)."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from ne.audit import ensure_audit_schema
from ne.eval.store import EvalRunStore, ensure_eval_schema
from ne.flywheel import SqlAlchemyCorrectionSignalStore, ensure_flywheel_schema
from ne.obs.config import ObsReportSettings
from ne.obs.errors import ObsReportError


@dataclass(frozen=True)
class ReportDataSources:
    """The three read-only engines/stores the monthly report is collected from."""

    audit_engine: sa.Engine
    eval_store: EvalRunStore
    signal_store: SqlAlchemyCorrectionSignalStore


def report_sources_from_settings(settings: ObsReportSettings | None = None) -> ReportDataSources:
    """Build :class:`ReportDataSources` over the three configured store URLs.

    Raises :class:`~ne.obs.errors.ObsReportError` naming the offending env var
    if any of ``audit_database_url``/``eval_database_url``/``signal_database_url``
    is empty (a caller must configure ``NE_OBS_AUDIT_DATABASE_URL`` /
    ``NE_OBS_EVAL_DATABASE_URL`` / ``NE_OBS_SIGNAL_DATABASE_URL`` — or construct
    ``ReportDataSources(...)`` directly). Ensures each consumed store's schema
    exists (idempotent ``create_all`` bootstrap-if-absent — the established
    factory convention, NOT a mutation of another package's data). When two
    URLs are identical strings, separate engines to the same DB are fine — this
    package only ever reads through them.
    """
    settings = settings or ObsReportSettings()

    if not settings.audit_database_url:
        raise ObsReportError(
            "ObsReportSettings.audit_database_url is empty; set "
            "NE_OBS_AUDIT_DATABASE_URL or construct ReportDataSources(...) directly"
        )
    if not settings.eval_database_url:
        raise ObsReportError(
            "ObsReportSettings.eval_database_url is empty; set "
            "NE_OBS_EVAL_DATABASE_URL or construct ReportDataSources(...) directly"
        )
    if not settings.signal_database_url:
        raise ObsReportError(
            "ObsReportSettings.signal_database_url is empty; set "
            "NE_OBS_SIGNAL_DATABASE_URL or construct ReportDataSources(...) directly"
        )

    audit_engine = sa.create_engine(settings.audit_database_url)
    eval_engine = sa.create_engine(settings.eval_database_url)
    signal_engine = sa.create_engine(settings.signal_database_url)

    ensure_audit_schema(audit_engine)
    ensure_eval_schema(eval_engine)
    ensure_flywheel_schema(signal_engine)

    return ReportDataSources(
        audit_engine=audit_engine,
        eval_store=EvalRunStore(eval_engine),
        signal_store=SqlAlchemyCorrectionSignalStore(signal_engine),
    )
