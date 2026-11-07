# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Layered settings for ne-obs KPI/U-P-B reporting (NE-068).

Mirrors ``ne.flywheel.config.FlywheelSettings``'s style: ``extra='forbid'`` so
a typo in ``ne.toml`` fails loudly rather than silently being ignored.

config_hash (SPEC §4 / ADR-0003): every generated report is stamped with the
FROZEN process-start config_hash via ``ne.contracts.get_config_hash()`` (see
:func:`ne.obs.report.generate_monthly_report`). This module's own settings are
a subset like every other module's: a composition root that also runs
ne-runtime/ne-govern freezes the COMPOSED platform hash via
``freeze_platform_config_hash`` (passing ``ObsReportSettings()`` in the
``extra_modules`` mapping so the platform hash covers this module's config
too); tests use the single-module ``freeze_config_hash`` fixture directly.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings
from ne.obs.collect import DEFAULT_TOOL_ACTIONS, DEFAULT_TURN_ACTIONS
from ne.obs.errors import ObsReportError
from ne.obs.kpi import KpiLead

_LEAD_RE = re.compile(r"\s*(?P<name>.+?)\s*<(?P<email>[^<>@\s]+@[^<>@\s]+)>\s*")


class ObsReportSettings(NeSettings):
    """Settings for the U-P-B KPI report generator (store URLs, leads, floor knobs)."""

    model_config = SettingsConfigDict(
        env_prefix="NE_OBS_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    #: Read-only audit-store engine URL (``ne.audit.replay`` seam).
    audit_database_url: str = ""
    #: Read-only eval-store engine URL (``EvalRunStore.list_runs`` seam).
    eval_database_url: str = ""
    #: Read-only correction-signal-store engine URL (``read_signals`` seam).
    signal_database_url: str = ""
    #: KPI name -> "Full Name <email@example.com>", parsed by
    #: :func:`leads_from_settings`.
    kpi_leads: dict[str, str] = Field(default_factory=dict)
    #: Per-turn minutes-saved assumption for the hours-saved FLOOR (conservative
    #: default; see ``ne.obs.report.METHODOLOGY``).
    minutes_saved_per_turn: float = Field(default=2.0, gt=0)
    #: Conservatism discount applied to the hours-saved FLOOR.
    floor_discount: float = Field(default=0.5, gt=0, le=1.0)
    turn_actions: tuple[str, ...] = DEFAULT_TURN_ACTIONS
    tool_actions: tuple[str, ...] = DEFAULT_TOOL_ACTIONS


def leads_from_settings(settings: ObsReportSettings) -> tuple[KpiLead, ...]:
    """Parse ``settings.kpi_leads`` into a sorted (by kpi key) tuple of :class:`KpiLead`.

    Each value must match ``"Name <email>"``; a malformed value raises
    :class:`~ne.obs.errors.ObsReportError` naming the offending kpi key (fail
    loud, in the spirit of ``extra='forbid'``).
    """
    leads: list[KpiLead] = []
    for kpi, raw in settings.kpi_leads.items():
        match = _LEAD_RE.fullmatch(raw)
        if match is None:
            raise ObsReportError(
                f"ObsReportSettings.kpi_leads[{kpi!r}] = {raw!r} is not a valid "
                "'Name <email>' string"
            )
        leads.append(KpiLead(kpi=kpi, name=match.group("name"), email=match.group("email")))
    return tuple(sorted(leads, key=lambda lead: lead.kpi))
