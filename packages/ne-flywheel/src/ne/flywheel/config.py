# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Layered settings for ne-flywheel — correction-signal capture (NE-064).

Mirrors ``ne.runtime.config.RuntimeSettings``'s style: ``extra='forbid'`` so a
typo in ``ne.toml`` fails loudly rather than silently being ignored.

config_hash (SPEC §4 / ADR-0003): every captured signal is stamped with the
FROZEN process-start config_hash via ``ne.events.new_correction_signal`` (which
resolves ``ne.contracts.get_config_hash()`` internally) — capture never emits
an unstamped event. This module's own settings are a subset like every other
module's: a composition root that also runs ne-runtime/ne-govern freezes the
COMPOSED platform hash via ``freeze_platform_config_hash`` (passing
``FlywheelSettings()`` in the ``extra_modules`` mapping so the platform hash
covers this module's config too); tests use the single-module
``freeze_config_hash`` fixture directly.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings


class FlywheelSettings(NeSettings):
    """Settings for correction-signal capture (store URL, free-text caps)."""

    model_config = SettingsConfigDict(
        env_prefix="NE_FLYWHEEL_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    #: Signal-store engine URL; empty => the caller supplies the engine
    #: directly (tests use ``sqlite://``). Set via ``NE_FLYWHEEL_SIGNAL_DATABASE_URL``.
    signal_database_url: str = ""
    #: Hard cap (characters) on any stored free-text field (comment/diff_summary/
    #: reason), enforced by :func:`ne.flywheel.hygiene.scrub_text` AFTER redaction.
    max_text_chars: int = 4000
    #: Max signals a single curation run reads from the cursor; the next
    #: scheduled run continues from where this one stopped.
    curation_batch_limit: int = 500
    #: Default cron the composition root passes to ``scheduled_curation_job``
    #: (02:00 UTC daily). Scheduling itself lives in ne-runtime's CronScheduler.
    curation_cron: str = "0 2 * * *"
