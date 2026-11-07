# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-skills-cc — the Claude-Code ``SkillSubstrate`` binding (NE-SKILL-5, C5).

The SECOND ``ne.plugins.skill_substrate`` implementation, alongside
``ne.skills.substrate.PydanticAiSkillSubstrate`` (which stays the production
orchestrator). This package exists so ``ne-skills`` core stays free of any
substrate/orchestrator import (D-9/R-19): it lives in its OWN package, not
inside ``ne-skills``, so the import-linter C3 boundary and
``ne.skills.tests.test_import_boundary``'s D-10 guardian keep working
unmodified — this is the concrete proof that a second orchestrator binding
requires zero core changes ("any stack we support" made real, not asserted).

HONESTY: this binding is PROCEDURAL-SKILLS-ONLY in v1. It writes and reads only
``SKILL.md`` documents — there is no executable-artifact support, no
``scripts/`` directory, no exec bits. This matches the two deliberate ADR-0011
deviations; do not read "any stack" as "any capability" — the C6 governed-tool
chokepoint stays the only path from a skill to an actual tool call on every
binding, including this one.
"""

from __future__ import annotations

from ne.skills_cc.substrate import ClaudeCodeSkillSubstrate

__all__ = ["ClaudeCodeSkillSubstrate"]
