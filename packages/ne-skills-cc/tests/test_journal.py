# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Filesystem journal: paging/exhausted/cursor discipline, skip-on-malformed, C12."""

from __future__ import annotations

import json
from pathlib import Path

from ne.skills_cc import journal

from .conftest import seed_cc_root


def test_snapshot_paging_2_2_2_then_exhausted_stable(tmp_path: Path) -> None:
    seed_cc_root(tmp_path)

    first = journal.read_snapshot_page(tmp_path, None, 2)
    assert len(first.items) == 2
    assert first.exhausted is False

    second = journal.read_snapshot_page(tmp_path, first.cursor, 2)
    assert len(second.items) == 2
    assert second.exhausted is False

    third = journal.read_snapshot_page(tmp_path, second.cursor, 2)
    assert len(third.items) == 2
    # 6 files, limit 2: the third page consumes the last two, so no further items
    # remain. Per the SnapshotPage contract ("exhausted is True when no further
    # items are currently available") this page is exhausted — a caller must not
    # be forced into an extra empty request to discover the end of the stream.
    assert third.exhausted is True

    all_ids = {s.snapshot_id for page in (first, second, third) for s in page.items}
    assert len(all_ids) == 6  # all disjoint

    # An extra pull on the exhausted cursor is still valid and cursor-stable.
    fourth = journal.read_snapshot_page(tmp_path, third.cursor, 2)
    assert fourth.items == ()
    assert fourth.exhausted is True
    assert fourth.cursor == third.cursor

    # calling again on the stable cursor stays stable
    fifth = journal.read_snapshot_page(tmp_path, fourth.cursor, 2)
    assert fifth.items == ()
    assert fifth.exhausted is True
    assert fifth.cursor == fourth.cursor


def test_snapshot_without_result_line_skipped_but_advances_cursor(tmp_path: Path) -> None:
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir(parents=True)
    (transcripts / "000-a.jsonl").write_text(
        json.dumps({"type": "tool_use", "name": "t.a", "outcome": "success", "ref": "r1"})
        + "\n"
        + json.dumps(
            {
                "type": "result",
                "succeeded": True,
                "intent": "subagent:x",
                "principal_id": "user:x",
                "session_id": "s-a",
                "started_at": "2026-07-20T12:00:00+00:00",
                "finished_at": "2026-07-20T12:01:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (transcripts / "001-incomplete.jsonl").write_text(
        json.dumps({"type": "tool_use", "name": "t.b", "outcome": "success", "ref": "r2"}) + "\n",
        encoding="utf-8",
    )
    (transcripts / "002-c.jsonl").write_text(
        json.dumps({"type": "tool_use", "name": "t.c", "outcome": "success", "ref": "r3"})
        + "\n"
        + json.dumps(
            {
                "type": "result",
                "succeeded": True,
                "intent": "subagent:x",
                "principal_id": "user:x",
                "session_id": "s-c",
                "started_at": "2026-07-20T12:00:00+00:00",
                "finished_at": "2026-07-20T12:01:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    page = journal.read_snapshot_page(tmp_path, None, 10)
    assert {s.snapshot_id for s in page.items} == {"000-a", "002-c"}
    assert page.exhausted is True
    # cursor advanced past the skipped file to the LAST file consumed.
    assert page.cursor == "002-c.jsonl"


def test_malformed_json_file_is_skipped(tmp_path: Path) -> None:
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir(parents=True)
    (transcripts / "000-bad.jsonl").write_text("{not valid json at all", encoding="utf-8")

    page = journal.read_snapshot_page(tmp_path, None, 10)
    assert page.items == ()
    assert page.exhausted is True
    assert page.cursor == "000-bad.jsonl"


def test_c12_no_leak_from_planted_secret(tmp_path: Path) -> None:
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir(parents=True)
    (transcripts / "000-hostile.jsonl").write_text(
        json.dumps(
            {
                "type": "tool_use",
                "name": "t.a",
                "outcome": "success",
                "ref": "r1",
                "input": {"password": "hunter2"},
                "args": {"secret": "hunter2"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "result",
                "succeeded": True,
                "intent": "subagent:x",
                "principal_id": "user:x",
                "session_id": "s-hostile",
                "started_at": "2026-07-20T12:00:00+00:00",
                "finished_at": "2026-07-20T12:01:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    page = journal.read_snapshot_page(tmp_path, None, 10)
    assert len(page.items) == 1
    dumped = json.dumps([s.model_dump(mode="json") for s in page.items])
    assert "hunter2" not in dumped


def test_missing_transcripts_dir_echoes_incoming_cursor(tmp_path: Path) -> None:
    page = journal.read_snapshot_page(tmp_path, None, 10)
    assert page.items == ()
    assert page.exhausted is True
    assert page.cursor == ""

    page2 = journal.read_snapshot_page(tmp_path, "some-cursor", 10)
    assert page2.cursor == "some-cursor"
    assert page2.exhausted is True


def _write_invocation_lines(root: Path, lines: list[str]) -> None:
    hooks = root / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "invocations.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_invocation_paging_2_2_2_then_exhausted_stable(tmp_path: Path) -> None:
    lines = [
        json.dumps(
            {
                "skill_name": "alpha",
                "version": 1,
                "principal_id": f"u-{i}",
                "session_id": f"s-{i}",
                "fired_at": f"2026-07-20T13:{i:02d}:00+00:00",
            }
        )
        for i in range(6)
    ]
    _write_invocation_lines(tmp_path, lines)

    first = journal.read_invocation_page(tmp_path, None, 2)
    assert len(first.items) == 2
    assert first.exhausted is False

    second = journal.read_invocation_page(tmp_path, first.cursor, 2)
    assert len(second.items) == 2
    assert second.exhausted is False

    third = journal.read_invocation_page(tmp_path, second.cursor, 2)
    assert len(third.items) == 2
    # 6 lines, limit 2: the third page consumes the last two, so no further items
    # remain. Per the InvocationPage contract ("exhausted is True when no further
    # items are available") this page is exhausted — no extra empty request needed.
    assert third.exhausted is True

    # An extra pull on the exhausted cursor is still valid and cursor-stable.
    fourth = journal.read_invocation_page(tmp_path, third.cursor, 2)
    assert fourth.items == ()
    assert fourth.exhausted is True
    assert fourth.cursor == third.cursor


def test_invocation_malformed_line_skipped_cursor_advances(tmp_path: Path) -> None:
    valid = [
        json.dumps(
            {
                "skill_name": "alpha",
                "version": 1,
                "principal_id": f"u-{i}",
                "session_id": f"s-{i}",
                "fired_at": f"2026-07-20T13:{i:02d}:00+00:00",
            }
        )
        for i in range(6)
    ]
    lines = [*valid[:2], "{not valid json", *valid[2:]]
    assert len(lines) == 7
    _write_invocation_lines(tmp_path, lines)

    collected = []
    cursor: str | None = None
    guard = 0
    while True:
        guard += 1
        assert guard < 20
        page = journal.read_invocation_page(tmp_path, cursor, 2)
        collected.extend(page.items)
        if page.exhausted:
            stable_cursor = page.cursor
            again = journal.read_invocation_page(tmp_path, stable_cursor, 2)
            assert again.items == ()
            assert again.cursor == stable_cursor
            assert again.exhausted is True
            break
        cursor = page.cursor

    assert len(collected) == 6
    assert {(i.principal_id, i.session_id) for i in collected} == {
        (f"u-{i}", f"s-{i}") for i in range(6)
    }


def test_missing_invocations_file_is_empty_exhausted(tmp_path: Path) -> None:
    page = journal.read_invocation_page(tmp_path, None, 10)
    assert page.items == ()
    assert page.exhausted is True
    assert page.cursor == "0"


def test_snapshot_exact_multiple_boundary_exhausts(tmp_path: Path) -> None:
    # Regression (Codex P2): a page consuming exactly the last `limit` files must
    # report exhausted per the SnapshotPage contract, not defer end-of-stream to an
    # extra empty request. seed_cc_root writes 6 transcripts.
    seed_cc_root(tmp_path)

    one_page = journal.read_snapshot_page(tmp_path, None, 6)
    assert len(one_page.items) == 6
    assert one_page.exhausted is True

    page1 = journal.read_snapshot_page(tmp_path, None, 3)
    assert page1.exhausted is False
    page2 = journal.read_snapshot_page(tmp_path, page1.cursor, 3)
    assert len(page2.items) == 3
    assert page2.exhausted is True


def test_invocation_exact_multiple_boundary_exhausts(tmp_path: Path) -> None:
    # Regression (Codex P2): exactly `limit` remaining lines => exhausted, no extra
    # empty request, per the InvocationPage contract.
    lines = [
        json.dumps(
            {
                "skill_name": "alpha",
                "version": 1,
                "principal_id": f"u-{i}",
                "session_id": f"s-{i}",
                "fired_at": f"2026-07-20T14:{i:02d}:00+00:00",
            }
        )
        for i in range(4)
    ]
    _write_invocation_lines(tmp_path, lines)

    page1 = journal.read_invocation_page(tmp_path, None, 2)
    assert page1.exhausted is False
    page2 = journal.read_invocation_page(tmp_path, page1.cursor, 2)
    assert len(page2.items) == 2
    assert page2.exhausted is True
