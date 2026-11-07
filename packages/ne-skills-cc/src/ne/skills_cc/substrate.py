# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ClaudeCodeSkillSubstrate`` — the second ``SkillSubstrate`` binding (C5).

Proves the Protocol is genuinely substrate-agnostic: swapping the orchestrator
means writing ONE new binding, with ``ne-skills`` core untouched. This binding
does NOT replace :class:`~ne.skills.substrate.PydanticAiSkillSubstrate` as the
production orchestrator — it is the SECOND implementation the C5 dual-binding
swap test (``tests/test_swap_c5.py``) runs the same scenarios against.

``snapshots``/``collect_invocations`` fold from a filesystem journal
(:mod:`ne.skills_cc.journal`) instead of ``ne_audit_log``; ``materialize``/
``withdraw`` project onto a ``.claude/skills`` directory tree
(:mod:`ne.skills_cc.store`) instead of the skills registry. Both are pure
filesystem I/O, run via ``anyio.to_thread`` — mirroring the pydantic-ai
binding's to-thread discipline for its own (SQL) I/O.

``author`` reuses ``ne.skills.authoring`` — ``render_author_task`` /
``parse_author_output`` — DELIBERATELY. The R-23 injection-fenced prompt and the
forced ``source=FORGED``/``acl_labels=()``/empty-``provenance`` draft discipline
are core authoring POLICY, not a per-binding choice; only the dispatch
mechanism (an injected subprocess here vs. an injected in-process callable on
the pydantic-ai binding) differs, which is why the two bindings' ``author``
behavior is identical by construction — proven by the swap test, not merely
asserted here.

R-26 (telemetry-model honesty): on the pydantic-ai binding, the ``read.skills.view``
handler is the SOLE invocation writer, so ``collect_invocations`` there is
effectively a no-op drain of an already-empty stream in most conformance
scenarios. On THIS binding, ``collect_invocations`` is a REAL out-of-band drain
of a PostToolUse-hook JSONL buffer that a live deployment's hook script
appends to independently of this process. The two bindings' internal WRITE
paths are intentionally different; the C5 swap test asserts Protocol-OBSERVABLE
behavior (what a page of invocations looks like), never internal write-path
identity.

Procedural-skills-only (C6, ADR-0011 deviation 2): the store writes ONLY
``SKILL.md`` files, never an executable artifact — see ``ne.skills_cc.store``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import anyio.to_thread

from ne.contracts.skills import (
    AuthoringStandard,
    InvocationPage,
    SkillPack,
    SnapshotPage,
    WorkflowSnapshot,
)
from ne.skills.authoring import parse_author_output, render_author_task
from ne.skills.errors import SkillAuthoringUnavailableError
from ne.skills.substrate import AuthorDispatch
from ne.skills_cc import journal, store

_SUBSTRATE_ID = "claude-code"


class ClaudeCodeSkillSubstrate:
    """The SkillSubstrate over a Claude-Code-style filesystem deployment root."""

    def __init__(self, root: Path, *, author_dispatch: AuthorDispatch | None = None) -> None:
        self._root = root
        self._author_dispatch = author_dispatch

    @property
    def substrate_id(self) -> str:
        return _SUBSTRATE_ID

    async def snapshots(self, *, cursor: str | None = None, limit: int = 50) -> SnapshotPage:
        return await anyio.to_thread.run_sync(journal.read_snapshot_page, self._root, cursor, limit)

    async def author(
        self,
        snapshot: WorkflowSnapshot,
        *,
        standard: AuthoringStandard,
        existing: SkillPack | None = None,
    ) -> SkillPack:
        """Dispatch the toolless skill-author agent, then parse its output.

        No dispatcher ⇒ ``SkillAuthoringUnavailableError``. The draft parsing
        (forced ``source=FORGED``, ``acl_labels=()``, empty ``provenance``) is
        IDENTICAL to ``PydanticAiSkillSubstrate.author`` by construction: both
        bindings call the same core ``ne.skills.authoring`` functions.
        """
        if self._author_dispatch is None:
            raise SkillAuthoringUnavailableError()
        text = await self._author_dispatch(render_author_task(snapshot, existing))
        return parse_author_output(text, existing=existing)

    async def materialize(self, packs: Sequence[SkillPack], *, surface_key: str) -> None:
        await anyio.to_thread.run_sync(store.write_surface, self._root, tuple(packs), surface_key)

    async def withdraw(self, names: Sequence[str], *, surface_key: str) -> None:
        await anyio.to_thread.run_sync(store.remove_names, self._root, tuple(names), surface_key)

    async def collect_invocations(
        self, *, cursor: str | None = None, limit: int = 500
    ) -> InvocationPage:
        return await anyio.to_thread.run_sync(
            journal.read_invocation_page, self._root, cursor, limit
        )
