# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Filesystem observation: transcript-derived snapshots + hook-buffer invocations.

LAYOUT CONTRACT (a Claude-Code-style deployment root):

- ``<root>/transcripts/*.jsonl`` — one completed CC session per file. Files
  MUST sort in creation order by NAME (a real deployment names these with a
  sortable timestamp prefix, e.g. ``20260720T120000Z-<session>.jsonl``); the
  cursor is a filename.
- ``<root>/hooks/invocations.jsonl`` — a PostToolUse-hook append buffer, one
  JSON object per line; the cursor is the count of raw lines already consumed.

Both readers are PURE functions over a root :class:`~pathlib.Path` — no
substrate state, so they are independently testable and safe to run inside
``anyio.to_thread``.

Every function here is intentionally permissive about malformed input: an
in-progress or corrupt transcript file, or a malformed hook-buffer line, is
SKIPPED, never raised — a corrupt/partial record is not a completed workflow,
and a stuck cursor (from an exception aborting the page) is worse than one
skipped record. A skipped file/line still advances the cursor so the reader
never gets stuck re-reading the same bad record forever.

C12: the ``tool_use`` transcript line is read for exactly three keys —
``name``, ``outcome``, ``ref`` — mapped onto :class:`WorkflowStep`'s
``action``/``outcome``/``detail_ref``. Any other key on that line (an
``input``/``args`` payload, say) is structurally ignored: it is never read,
never copied, never reachable from the returned :class:`WorkflowSnapshot`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ne.contracts.skills import (
    InvocationPage,
    SkillInvocation,
    SnapshotPage,
    WorkflowSnapshot,
    WorkflowStep,
)

_SUBSTRATE_ID = "claude-code"
_TRANSCRIPTS_DIRNAME = "transcripts"
_INVOCATIONS_RELPATH = ("hooks", "invocations.jsonl")


def _parse_transcript_file(path: Path) -> WorkflowSnapshot | None:
    """Parse one transcript file into a snapshot, or ``None`` to skip it."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    steps: list[WorkflowStep] = []
    terminal: dict[str, Any] | None = None
    try:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if kind == "tool_use":
                # C12: read ONLY name/outcome/ref — "input"/"args" (if present
                # on the line) are never touched.
                steps.append(
                    WorkflowStep(
                        action=str(obj.get("name") or ""),
                        outcome=str(obj.get("outcome") or ""),
                        detail_ref=str(obj.get("ref") or ""),
                    )
                )
            elif kind == "result":
                terminal = obj
    except (json.JSONDecodeError, TypeError, ValueError):
        return None  # malformed JSON on any line: skip the whole file.

    if terminal is None:
        return None  # no terminal line: an in-progress/incomplete session.

    try:
        department = terminal.get("department")
        return WorkflowSnapshot(
            snapshot_id=path.stem,
            substrate_id=_SUBSTRATE_ID,
            intent=str(terminal.get("intent") or ""),
            steps=tuple(steps),
            succeeded=bool(terminal.get("succeeded")),
            principal_id=str(terminal.get("principal_id") or ""),
            session_id=str(terminal.get("session_id") or ""),
            department=str(department) if department else None,
            started_at=datetime.fromisoformat(str(terminal["started_at"])),
            finished_at=datetime.fromisoformat(str(terminal["finished_at"])),
        )
    except (KeyError, TypeError, ValueError):
        # Missing keys, unparseable timestamps, or a WorkflowSnapshot
        # validation failure (e.g. a naive datetime) — skip, don't raise.
        return None


def read_snapshot_page(root: Path, cursor: str | None, limit: int) -> SnapshotPage:
    """Page completed workflows folded from ``<root>/transcripts/*.jsonl``.

    Files are listed lexicographically and filtered to those whose name sorts
    strictly after ``cursor`` (``None``/``""`` = start from the beginning); up
    to ``limit`` files are consumed per page. The returned cursor is the last
    consumed file's name (the incoming cursor is echoed back when no file was
    consumed — including when the ``transcripts`` directory is absent), so an
    empty/exhausted page is cursor-stable.
    """
    incoming = cursor or ""
    transcripts_dir = root / _TRANSCRIPTS_DIRNAME
    if not transcripts_dir.is_dir():
        return SnapshotPage(items=(), cursor=incoming, exhausted=True)

    all_files = sorted(transcripts_dir.glob("*.jsonl"), key=lambda p: p.name)
    candidates = [f for f in all_files if f.name > incoming] if incoming else all_files
    selected = candidates[:limit]

    items = tuple(snap for f in selected if (snap := _parse_transcript_file(f)) is not None)
    # Exhaustion is an end-of-sequence fact, not a short-read heuristic:
    # len(selected) < limit misreports the exact-multiple boundary (when exactly
    # `limit` files remain) as "more available", forcing an extra empty request.
    exhausted = len(candidates) <= limit
    new_cursor = selected[-1].name if selected else incoming
    return SnapshotPage(items=items, cursor=new_cursor, exhausted=exhausted)


def _parse_invocation_line(raw: str) -> SkillInvocation | None:
    line = raw.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
        if not isinstance(obj, dict):
            return None
        return SkillInvocation(
            skill_name=str(obj["skill_name"]),
            version=int(obj["version"]),
            principal_id=str(obj["principal_id"]),
            session_id=str(obj["session_id"]),
            fired_at=datetime.fromisoformat(str(obj["fired_at"])),
            run_succeeded=obj.get("run_succeeded"),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def read_invocation_page(root: Path, cursor: str | None, limit: int) -> InvocationPage:
    """Page skill firings from the ``<root>/hooks/invocations.jsonl`` buffer.

    ``cursor`` is the count of raw lines already consumed (``int(cursor or
    "0")``). A malformed line is skipped but still counts toward the cursor
    advance — the reader never re-reads a bad line. A missing buffer file
    yields an empty, exhausted page with the incoming cursor echoed.
    """
    path = root.joinpath(*_INVOCATIONS_RELPATH)
    after = int(cursor) if cursor else 0
    if not path.is_file():
        return InvocationPage(items=(), cursor=cursor or "0", exhausted=True)

    lines = path.read_text(encoding="utf-8").splitlines()
    window = lines[after : after + limit]
    items = tuple(inv for raw in window if (inv := _parse_invocation_line(raw)) is not None)

    consumed = len(window)
    # End-of-sequence check, not a short-read heuristic: consumed < limit
    # misreports the exact-multiple boundary (when exactly `limit` lines remain)
    # as "more available", forcing an extra empty request.
    exhausted = after + consumed >= len(lines)
    new_cursor = str(after + consumed)
    return InvocationPage(items=items, cursor=new_cursor, exhausted=exhausted)
