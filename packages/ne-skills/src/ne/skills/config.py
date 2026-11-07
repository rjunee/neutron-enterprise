# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``SkillsSettings`` — the Skill Forge read-side configuration (ADR-0003).

Joins the composed platform config hash via ``build_platform`` when skills are
enabled (enabled ⇔ the flywheel signal database is configured — the same-engine
requirement, plan §2.2), exactly like every other module's settings. The
``env_prefix``/``extra='forbid'`` discipline mirrors ``ne.worker.settings`` so a
typo fails loudly.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import SettingsConfigDict

from ne.contracts.config import NeSettings


class SkillsSettings(NeSettings):
    """Read-side Skill Forge knobs (later forge/gardener knobs ride here too)."""

    model_config = SettingsConfigDict(
        env_prefix="NE_SKILLS_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    #: The bundle directory the boot reconcile scans. EMPTY (default) ⇒ reconcile
    #: is a no-op (no bundled/engagement packs shipped for this deployment).
    bundle_dir: str = ""
    #: The active-set index cap (design §6.2 ~150 packs ≈ 5-6k tokens).
    active_set_cap: int = 150
    #: The default top-k for ``read.skills.search``.
    search_top_k: int = 8

    # -- forge (NE-SKILL-1b) --------------------------------------------------
    # These join the composed platform config hash when skills are enabled (a
    # one-time hash change on upgrade — expected).
    #: DECIDED default-off (R-6): no forge GA until NE-SKILL-2. Also avoids a
    #: failing daily cron on stacks without the ``skill-authoring`` gateway route.
    forge_enabled: bool = False
    #: The forge cron schedule (03:15 UTC daily by default).
    forge_cron: str = "15 3 * * *"
    #: How many snapshots one forge run pulls per pass.
    forge_batch_limit: int = 50
    #: The gateway task the skill-author agent routes on (a dev-stack config
    #: prerequisite — the task must map to a model in the gateway registry).
    author_route_task: str = "skill-authoring"
    #: The lane the author routes on — private/open-weight by construction
    #: (snapshot intents are customer text; frontier is structurally refused).
    author_route_lane: str = "private-vllm"

    # -- gardener / rot (NE-SKILL-4) ------------------------------------------
    # These join the composed platform config hash when skills are enabled (a
    # one-time hash change on upgrade — the 1b precedent). Default-off byte-parity
    # is preserved (an all-defaults enabled stack still stamps them identically).
    #: DECIDED default-off (the forge/R-6 fail-closed house pattern): the monthly
    #: Gardener cron only runs where a deployment opts in. Off ⇒ an EMPTY worker
    #: surface ⇒ zero gardener jobs.
    gardener_enabled: bool = False
    #: The Gardener cron schedule — monthly, the 1st at 04:30 UTC.
    gardener_cron: str = "30 4 1 * *"
    #: Retirement candidacy: unfired for this many days AND < ``retirement_min_fires``.
    retirement_days: int = 90
    retirement_min_fires: int = 3
    #: Failure flag: ≥ this many KNOWN-outcome fires failing at ≥ ``failure_flag_ratio``.
    failure_flag_min_fires: int = 4
    failure_flag_ratio: float = 0.5
    #: A skill proposal open (no review row) longer than this many days is FLAGGED
    #: stale in the digest (D-7: flagged only — a human batch-taps the reject).
    stale_proposal_days: int = 14
