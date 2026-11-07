# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the ne-skills-cc suite, incl. the C5 dual-binding seeds.

``seed_cc_root`` and ``seed_pydantic_engine`` seed LOGICALLY IDENTICAL fixture
data (same trace/session/principal ids, same fixed timestamps) for the two
sides of the C5 swap test (``tests/test_swap_c5.py``). ``seed_pydantic_engine``
duplicates the shape of ``packages/ne-skills/tests/test_substrate_conformance.py
::_seed_full_engine`` (test directories are not importable packages, so this is
accepted, documented duplication — see NE-SKILL-5's risk notes).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa

from ne.contracts.skills import SkillInvocation, SkillPack, SkillSource
from ne.skills.registry import SqlAlchemySkillRegistry, ensure_skills_schema

GOOD_BODY = (
    "## When to Use\nWhen the trigger fires.\n\n"
    "## Procedure\n1. Do the thing.\n\n"
    "## Pitfalls\nNone known.\n\n"
    "## Verification\nConfirm the outcome.\n"
)

#: Fixed, deterministic timestamps shared by both sides of the swap seed.
FIXED_STARTED_AT = "2026-07-20T12:00:00+00:00"
FIXED_FINISHED_AT = "2026-07-20T12:05:00+00:00"
FIXED_FIRED_AT = tuple(f"2026-07-20T13:{i:02d}:00+00:00" for i in range(6))


def make_pack(
    name: str,
    *,
    version: int = 1,
    description: str | None = None,
    triggers: tuple[str, ...] | None = None,
    body: str = GOOD_BODY,
    source: SkillSource = SkillSource.FORGED,
    category: str = "",
    tags: tuple[str, ...] = (),
    requires_tools: tuple[str, ...] = (),
    platforms: tuple[str, ...] = (),
    acl_labels: tuple[str, ...] = (),
    provenance: dict[str, object] | None = None,
) -> SkillPack:
    return SkillPack(
        name=name,
        version=version,
        description=description or f"skill {name}",
        triggers=triggers or (f"use {name}",),
        body=body,
        source=source,
        category=category,
        tags=tags,
        requires_tools=requires_tools,
        platforms=platforms,
        acl_labels=acl_labels,
        provenance=provenance or {},
    )


def _good_author_md() -> str:
    """Canned author output — verbatim copy of the ne-skills conformance kit's."""
    return (
        "---\n"
        "name: kit-forged\n"
        "description: a forged kit skill\n"
        "version: 1\n"
        "triggers:\n"
        "  - do the kit thing\n"
        "source: forged\n"
        "---\n"
        "## When to Use\nWhen the trigger fires.\n\n"
        "## Procedure\n1. Do the thing.\n\n"
        "## Pitfalls\nNone known.\n\n"
        "## Verification\nConfirm the outcome.\n"
    )


def seed_cc_root(tmp: Path) -> Path:
    """Seed a Claude-Code-style deployment root: 6 transcripts + 6 invocations."""
    transcripts_dir = tmp / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    for i in range(6):
        lines = [
            json.dumps(
                {
                    "type": "tool_use",
                    "name": f"tool.a{i}",
                    "outcome": "success",
                    "ref": f"kit-ref-{i}-0",
                }
            ),
            json.dumps(
                {
                    "type": "tool_use",
                    "name": f"tool.b{i}",
                    "outcome": "success",
                    "ref": f"kit-ref-{i}-1",
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "succeeded": True,
                    "intent": "subagent:kit",
                    "principal_id": "user:kit",
                    "session_id": f"s-{i}",
                    "started_at": FIXED_STARTED_AT,
                    "finished_at": FIXED_FINISHED_AT,
                }
            ),
        ]
        (transcripts_dir / f"{i:03d}-kit-{i}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    hooks_dir = tmp / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    inv_lines = [
        json.dumps(
            {
                "skill_name": "alpha",
                "version": 1,
                "principal_id": f"u-{i}",
                "session_id": f"s-{i}",
                "fired_at": FIXED_FIRED_AT[i],
            }
        )
        for i in range(6)
    ]
    (hooks_dir / "invocations.jsonl").write_text("\n".join(inv_lines) + "\n", encoding="utf-8")
    return tmp


def seed_pydantic_engine(tmp: Path) -> sa.Engine:
    """Seed the skills+audit closure for the pydantic-ai side of the swap test.

    LOGICALLY IDENTICAL to ``seed_cc_root``: same trace/session/principal ids
    ('kit-<i>' trace, 'user:kit' actor, 's-<i>' session), same 6 pageable
    snapshots (2 tool.execute steps + 1 subagent.complete terminal per trace),
    same 6 pageable invocations with the SAME fixed ``fired_at`` stamps.
    """
    from ne.audit import append_record, ensure_audit_schema, ensure_transactional_sqlite
    from ne.events import new_audit_event
    from ne.schema import AuditOutcome

    engine = sa.create_engine(f"sqlite:///{tmp / 'swap.db'}")
    ensure_transactional_sqlite(engine)
    ensure_skills_schema(engine)
    ensure_audit_schema(engine)
    registry = SqlAlchemySkillRegistry(engine)
    registry.register_pack(make_pack("alpha", source=SkillSource.BUNDLED), origin="bundle")
    for i in range(6):
        registry.append_invocation(
            SkillInvocation(
                skill_name="alpha",
                version=1,
                principal_id=f"u-{i}",
                session_id=f"s-{i}",
                fired_at=datetime.fromisoformat(FIXED_FIRED_AT[i]),
            )
        )

    for i in range(6):
        with engine.begin() as conn:
            for action in (f"tool.a{i}", f"tool.b{i}"):
                append_record(
                    conn,
                    new_audit_event(
                        action="tool.execute",
                        resource=f"tool:{action}",
                        outcome=AuditOutcome.success,
                        actor="user:kit",
                        detail='{"status": "ok"}',
                        trace_id=f"kit-{i}",
                        config_hash="a" * 64,
                    ),
                )
            append_record(
                conn,
                new_audit_event(
                    action="subagent.complete",
                    resource="subagent:kit",
                    outcome=AuditOutcome.success,
                    actor="user:kit",
                    # 'session_id' rides inside detail so the folded WorkflowSnapshot's
                    # session_id is 's-<i>' — matching seed_cc_root's transcript
                    # session_id, not the trace id (this is a deliberate deviation
                    # from ne-skills' own conformance fixture, made so the swap
                    # test's cross-binding session_id comparison is meaningful).
                    detail=json.dumps({"agent": "kit", "session_id": f"s-{i}"}),
                    trace_id=f"kit-{i}",
                    config_hash="a" * 64,
                ),
            )
    return engine
