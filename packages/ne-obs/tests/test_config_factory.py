# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ObsReportSettings / leads_from_settings / report_sources_from_settings (NE-068)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ne.obs.config import ObsReportSettings, leads_from_settings
from ne.obs.errors import ObsReportError
from ne.obs.factory import report_sources_from_settings
from ne.obs.kpi import KpiLead


def test_leads_from_settings_parses_and_sorts() -> None:
    settings = ObsReportSettings(
        kpi_leads={
            "utilization.agent_turns": "Grace Hopper <grace@acme.com>",
            "business_value.hours_saved_floor": "Ada Lovelace <ada@acme.com>",
        }
    )

    leads = leads_from_settings(settings)

    assert leads == (
        KpiLead(kpi="business_value.hours_saved_floor", name="Ada Lovelace", email="ada@acme.com"),
        KpiLead(kpi="utilization.agent_turns", name="Grace Hopper", email="grace@acme.com"),
    )


def test_kpi_leads_load_from_documented_toml_example(tmp_path: Path) -> None:
    """The README's ne.toml example (flat [kpi_leads] table) must actually load."""
    config = tmp_path / "ne.toml"
    config.write_text(
        "[kpi_leads]\n"
        '"business_value.hours_saved_floor" = "Ada Lovelace <ada@acme.com>"\n'
        '"utilization.agent_turns" = "Grace Hopper <grace@acme.com>"\n'
    )

    settings = ObsReportSettings.from_config_file(config)

    assert leads_from_settings(settings) == (
        KpiLead(kpi="business_value.hours_saved_floor", name="Ada Lovelace", email="ada@acme.com"),
        KpiLead(kpi="utilization.agent_turns", name="Grace Hopper", email="grace@acme.com"),
    )


def test_leads_from_settings_malformed_raises_naming_key() -> None:
    settings = ObsReportSettings(kpi_leads={"utilization.agent_turns": "not-a-valid-lead-string"})

    with pytest.raises(ObsReportError, match=r"utilization\.agent_turns"):
        leads_from_settings(settings)


def test_unknown_settings_key_rejected() -> None:
    with pytest.raises(ValidationError):
        ObsReportSettings(this_is_not_a_field="x")  # type: ignore[call-arg]


def test_report_sources_from_settings_raises_naming_env_var_when_audit_url_empty() -> None:
    settings = ObsReportSettings(
        audit_database_url="",
        eval_database_url="sqlite://",
        signal_database_url="sqlite://",
    )

    with pytest.raises(ObsReportError, match="NE_OBS_AUDIT_DATABASE_URL"):
        report_sources_from_settings(settings)


def test_report_sources_from_settings_works_over_sqlite_urls() -> None:
    settings = ObsReportSettings(
        audit_database_url="sqlite://",
        eval_database_url="sqlite://",
        signal_database_url="sqlite://",
    )

    sources = report_sources_from_settings(settings)

    assert sources.eval_store.list_runs() == []
    assert sources.signal_store.read_signals() == []
