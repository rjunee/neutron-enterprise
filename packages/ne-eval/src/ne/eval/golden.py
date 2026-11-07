# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Golden-set format and management (JSONL, git-diffable, content-hashed).

A golden set is a list of :class:`GoldenItem` questions, each carrying a
reference answer and the set of relevant document ids. The on-disk format is
JSONL — one canonical JSON object per line — so it diffs cleanly in git.
:func:`golden_set_hash` mirrors ``ne.contracts.config.compute_config_hash``
(sorted-by-id, ``sort_keys``, tight separators, ``ensure_ascii``) so a run can be
stamped with the exact golden-set content it ran against.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from ne.eval.errors import GoldenSetError

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class GoldenItem(BaseModel):
    """One golden question with its reference answer and relevance labels."""

    model_config = ConfigDict(frozen=True)

    id: str
    question: str
    reference_answer: str
    relevant_document_ids: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    notes: str | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value or not _SLUG_RE.match(value):
            raise ValueError(f"golden item id must be a non-empty slug; got {value!r}")
        return value

    @field_validator("question")
    @classmethod
    def _validate_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("golden item question must be non-empty")
        return value


class GoldenSet(BaseModel):
    """An immutable collection of golden items."""

    model_config = ConfigDict(frozen=True)

    items: tuple[GoldenItem, ...] = ()


def _canonical_items(gs: GoldenSet) -> list[dict[str, object]]:
    """Return item dicts sorted by id for stable serialization/hashing."""
    return [item.model_dump(mode="json") for item in sorted(gs.items, key=lambda it: it.id)]


def load_golden_set(path: str | Path) -> GoldenSet:
    """Load a golden set from a JSONL file (one JSON object per line).

    Raises :class:`GoldenSetError` on an empty file, a blank/invalid line, or a
    duplicate item id.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise GoldenSetError(f"cannot read golden set {p}: {exc}") from exc

    items: list[GoldenItem] = []
    seen: set[str] = set()
    saw_any = False
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            raise GoldenSetError(f"{p}:{lineno}: blank line (JSONL forbids blank lines)")
        saw_any = True
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GoldenSetError(f"{p}:{lineno}: invalid JSON: {exc}") from exc
        try:
            item = GoldenItem.model_validate(obj)
        except ValueError as exc:
            raise GoldenSetError(f"{p}:{lineno}: invalid golden item: {exc}") from exc
        if item.id in seen:
            raise GoldenSetError(f"{p}:{lineno}: duplicate golden item id {item.id!r}")
        seen.add(item.id)
        items.append(item)

    if not saw_any:
        raise GoldenSetError(f"{p}: golden set is empty")
    return GoldenSet(items=tuple(items))


def save_golden_set(gs: GoldenSet, path: str | Path) -> None:
    """Write a golden set as stable, git-diffable JSONL (sorted by id, sorted keys)."""
    lines = [
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        for obj in _canonical_items(gs)
    ]
    Path(path).write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def golden_set_hash(gs: GoldenSet) -> str:
    """Return the SHA-256 hex digest over the canonical (sorted-by-id) golden set."""
    canonical = json.dumps(
        _canonical_items(gs), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def add_items(gs: GoldenSet, new: tuple[GoldenItem, ...]) -> GoldenSet:
    """Return a new golden set with ``new`` appended; rejects duplicate ids."""
    existing = {item.id for item in gs.items}
    for item in new:
        if item.id in existing:
            raise GoldenSetError(f"duplicate golden item id {item.id!r}")
        existing.add(item.id)
    return GoldenSet(items=gs.items + tuple(new))


def filter_by_tag(gs: GoldenSet, tag: str) -> GoldenSet:
    """Return a new golden set of items carrying ``tag``."""
    return GoldenSet(items=tuple(item for item in gs.items if tag in item.tags))
