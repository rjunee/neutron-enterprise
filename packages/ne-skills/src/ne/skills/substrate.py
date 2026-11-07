# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``PydanticAiSkillSubstrate`` — the SkillSubstrate binding (read side 1a + forge 1b).

1a implemented the READ subset: ``materialize``/``withdraw``/``collect_invocations``.
1b implements ``snapshots`` (audit-log-derived) and ``author`` (a toolless
skill-author sub-agent, injected as a dispatcher wrapper).

SNAPSHOTS are folded from what C6 already records in ``ne_audit_log`` — no new
observation surface. Terminal events (``subagent.complete`` from a dispatched
sub-agent run, and the D-2 ``session.run`` interactive-session event) each carry
a ``trace_id``; the ORDERED, NAMES-ONLY step sequence is the ``tool.execute``
events sharing that trace (``resource`` = ``tool:<name>`` → the action name;
argument values are STRUCTURALLY ABSENT — there is no args column, C12).

CRON EXCLUSION (verified plan deviation): cron ``job.run`` events are NOT a v1
snapshot source. The scheduler emits ``job.run`` OUTSIDE ``run_trace_scope``
(``scheduler.py`` ``_finish_run``), so it carries no ``trace_id`` and its
``tool.execute`` steps cannot be trace-joined without editing scheduler.py
(out of scope). v1 sources = sub-agent runs + interactive sessions only.

D-10 BOUNDARY unchanged: this class imports NOTHING from ``ne.runtime`` /
``pydantic_ai`` / ``dbos``. The author DISPATCHER is injected as a package-local
:class:`AuthorDispatch` callable (the composition root wraps the
GovernedSubAgentDispatcher), and the AUDIT engine is injected too — the substrate
only speaks SQLAlchemy + the frozen contracts.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import anyio.to_thread
import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.contracts.skills import (
    AuthoringStandard,
    InvocationPage,
    SkillPack,
    SnapshotPage,
    WorkflowSnapshot,
    WorkflowStep,
)
from ne.skills.active_set import render_index
from ne.skills.authoring import parse_author_output, render_author_task
from ne.skills.errors import SkillAuthoringUnavailableError
from ne.skills.registry import SqlAlchemySkillRegistry

_SUBSTRATE_ID = "pydantic-ai"

#: The two terminal audit actions a v1 snapshot folds from (cron excluded).
_TERMINAL_ACTIONS = ("subagent.complete", "session.run")
_TOOL_ACTION = "tool.execute"


class AuthorDispatch(Protocol):
    """The injected author-dispatch seam (keeps the D-10 boundary).

    The composition root wraps a ``GovernedSubAgentDispatcher`` call so this
    package never imports ne-runtime. ``task`` is the fenced author prompt; the
    return is the model's raw SKILL.md text.
    """

    async def __call__(self, task: str) -> str: ...


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _detail_of(payload: Any) -> dict[str, Any]:
    """Return the terminal event's ``detail`` as a dict (names/ids only, C12)."""
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("detail")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class PydanticAiSkillSubstrate:
    """The SkillSubstrate over the skills registry engine + the audit engine."""

    def __init__(
        self,
        engine: sa.Engine,
        *,
        audit_engine: sa.Engine | None = None,
        author_dispatch: AuthorDispatch | None = None,
    ) -> None:
        self._registry = SqlAlchemySkillRegistry(engine)
        self._audit_engine = audit_engine
        self._author_dispatch = author_dispatch

    @property
    def substrate_id(self) -> str:
        return _SUBSTRATE_ID

    async def snapshots(self, *, cursor: str | None = None, limit: int = 50) -> SnapshotPage:
        """Fold completed workflows from ``ne_audit_log`` since ``cursor``.

        No audit engine ⇒ the 1a byte-preserved empty, exhausted page. Otherwise
        the cursor is an ``int(cursor or "0")`` over ``ne_audit_log.id``.
        """
        if self._audit_engine is None:
            return SnapshotPage(items=(), cursor=cursor or "0", exhausted=True)

        def _work() -> SnapshotPage:
            return self._fold_snapshots(int(cursor) if cursor else 0, limit)

        return await anyio.to_thread.run_sync(_work)

    def _fold_snapshots(self, after_id: int, limit: int) -> SnapshotPage:
        assert self._audit_engine is not None
        with self._audit_engine.connect() as conn:
            # Lookahead-by-one (mirrors invocations_page): fetch one extra row so
            # exhaustion is an end-of-sequence fact, not a short-read heuristic. A
            # bare `.limit(limit)` + `len < limit` misreports the exact-multiple
            # boundary (exactly `limit` rows remain) as "more available", forcing an
            # unnecessary empty page — a SnapshotPage-contract violation. The C5 swap
            # test pins this to match ClaudeCodeSkillSubstrate's journal reader.
            terminals = conn.execute(
                sa.select(ne_audit_log)
                .where(
                    ne_audit_log.c.action.in_(_TERMINAL_ACTIONS),
                    ne_audit_log.c.trace_id.is_not(None),
                    ne_audit_log.c.id > after_id,
                )
                .order_by(ne_audit_log.c.id.asc())
                .limit(limit + 1)
            ).all()

            has_more = len(terminals) > limit
            terminals = terminals[:limit]

            items: list[WorkflowSnapshot] = []
            max_id = after_id
            for term in terminals:
                max_id = max(max_id, int(term.id))
                steps = conn.execute(
                    sa.select(ne_audit_log)
                    .where(
                        ne_audit_log.c.action == _TOOL_ACTION,
                        ne_audit_log.c.trace_id == term.trace_id,
                        ne_audit_log.c.id < term.id,
                    )
                    .order_by(ne_audit_log.c.seq.asc())
                ).all()
                items.append(self._build_snapshot(term, steps))

        return SnapshotPage(items=tuple(items), cursor=str(max_id), exhausted=not has_more)

    def _build_snapshot(
        self, term: sa.Row[Any], step_rows: Sequence[sa.Row[Any]]
    ) -> WorkflowSnapshot:
        detail = _detail_of(term.payload)
        steps = tuple(
            WorkflowStep(
                action=str(row.resource or "").removeprefix("tool:"),
                outcome=str(row.outcome or ""),
                detail_ref=str(row.event_id),
            )
            for row in step_rows
        )
        is_session = term.action == "session.run"
        succeeded = str(term.outcome) == "success"
        if is_session and "succeeded" in detail:
            succeeded = bool(detail["succeeded"])

        agent = detail.get("agent")
        if is_session:
            intent = f"session:{agent}" if agent else "session"
        else:
            intent = f"subagent:{agent}" if agent else "subagent"

        session_id = str(detail.get("session_id") or term.trace_id)
        department = str(detail["department"]) if is_session and detail.get("department") else None

        finished_at = _as_utc(term.occurred_at)
        started_at = _as_utc(step_rows[0].occurred_at) if step_rows else finished_at

        return WorkflowSnapshot(
            snapshot_id=str(term.event_id),
            substrate_id=_SUBSTRATE_ID,
            intent=intent,
            steps=steps,
            succeeded=succeeded,
            principal_id=str(term.actor),
            session_id=session_id,
            department=department,
            started_at=started_at,
            finished_at=finished_at,
        )

    async def author(
        self,
        snapshot: WorkflowSnapshot,
        *,
        standard: AuthoringStandard,
        existing: SkillPack | None = None,
    ) -> SkillPack:
        """Dispatch the toolless skill-author agent, then parse its output.

        No dispatcher ⇒ ``SkillAuthoringUnavailableError`` (the 1a read-side
        posture). The returned draft is forced to ``source=FORGED`` with empty
        provenance and ``acl_labels=()`` (see ``authoring.parse_author_output``);
        the FORGE alone validates it against the house standard.
        """
        if self._author_dispatch is None:
            raise SkillAuthoringUnavailableError()
        text = await self._author_dispatch(render_author_task(snapshot, existing))
        return parse_author_output(text, existing=existing)

    async def materialize(self, packs: Sequence[SkillPack], *, surface_key: str) -> None:
        pack_tuple = tuple(packs)
        index_text = render_index(pack_tuple)
        names = tuple(p.name for p in pack_tuple)

        def _work() -> None:
            self._registry.upsert_surface(surface_key, index_text, names)

        await anyio.to_thread.run_sync(_work)

    async def withdraw(self, names: Sequence[str], *, surface_key: str) -> None:
        drop = set(names)

        def _work() -> None:
            current = self._registry.read_surface(surface_key)
            if current is None:
                return
            index_text, pack_names = current
            remaining = tuple(n for n in pack_names if n not in drop)
            prefixes = tuple(f"- {n} (" for n in drop)
            kept_lines = [
                line
                for line in index_text.splitlines()
                if not any(line.startswith(prefix) for prefix in prefixes)
            ]
            self._registry.upsert_surface(surface_key, "\n".join(kept_lines), remaining)

        await anyio.to_thread.run_sync(_work)

    async def collect_invocations(
        self, *, cursor: str | None = None, limit: int = 500
    ) -> InvocationPage:
        def _work() -> InvocationPage:
            return self._registry.invocations_page(cursor, limit)

        return await anyio.to_thread.run_sync(_work)
