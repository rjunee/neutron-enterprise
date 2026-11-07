# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FULL SkillSubstrateContractKit conformance for ClaudeCodeSkillSubstrate."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import ClassVar

from ne.contracts.skills import SkillSubstrate
from ne.contracts.testing.kits.skill_substrate import SkillSubstrateContractKit
from ne.skills_cc import store
from ne.skills_cc.substrate import ClaudeCodeSkillSubstrate

from .conftest import _good_author_md, seed_cc_root


class TestClaudeCodeSkillSubstrateConformance(SkillSubstrateContractKit):
    """FULL-capability conformance — no subset: this IS the C5 second binding."""

    capabilities: ClassVar[frozenset[str]] = frozenset(
        {"snapshots", "invocations", "materialize", "author"}
    )

    def create_substrate(self) -> SkillSubstrate:
        tmp = Path(tempfile.mkdtemp())
        seed_cc_root(tmp)
        self._root = tmp

        async def _dispatch(task: str) -> str:
            return _good_author_md()

        return ClaudeCodeSkillSubstrate(tmp, author_dispatch=_dispatch)

    def read_surface(self, surface_key: str) -> set[str]:
        return store.list_surface(self._root, surface_key)
