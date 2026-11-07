# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Boot reconcile: register, idempotent, edit-wins, locally-edited, quarantine."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa

from ne.skills.config import SkillsSettings
from ne.skills.reconcile import (
    _RECONCILE_ADVISORY_LOCK_KEY,
    _reconcile_advisory_lock,
    reconcile_bundle_dir,
)
from ne.skills.registry import SqlAlchemySkillRegistry

from .conftest import GOOD_BODY


def _break(root: Path, name: str) -> None:
    """Overwrite ``name``'s SKILL.md with a house-standard-invalid body."""
    (root / name / "SKILL.md").write_text(
        f"---\nname: {name}\nversion: 1\nsource: bundled\n---\nno sections",
        encoding="utf-8",
    )


def _write_pack(
    root: Path,
    name: str,
    *,
    version: int = 1,
    description: str = "a pack",
    source: str = "bundled",
    body: str = GOOD_BODY,
) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    md = (
        f"---\nname: {name}\nversion: {version}\ndescription: {description}\n"
        f"source: {source}\ntriggers:\n  - use {name}\n---\n{body}"
    )
    (d / "SKILL.md").write_text(md, encoding="utf-8")


def _settings(root: Path) -> SkillsSettings:
    return SkillsSettings(bundle_dir=str(root))


def test_valid_pack_registers(engine: sa.Engine, tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    _write_pack(root, "alpha")
    report = reconcile_bundle_dir(engine, _settings(root))
    assert report.registered == ("alpha",)
    registry = SqlAlchemySkillRegistry(engine)
    assert registry.get_current("alpha") is not None


def test_idempotent_second_run(engine: sa.Engine, tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    _write_pack(root, "alpha")
    reconcile_bundle_dir(engine, _settings(root))
    report = reconcile_bundle_dir(engine, _settings(root))
    assert report.registered == ()
    assert report.unchanged == ("alpha",)


def test_edit_wins_appends_version(engine: sa.Engine, tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    _write_pack(root, "alpha", description="v1")
    reconcile_bundle_dir(engine, _settings(root))
    _write_pack(root, "alpha", version=2, description="v2-edited")
    report = reconcile_bundle_dir(engine, _settings(root))
    assert report.updated == ("alpha",)
    registry = SqlAlchemySkillRegistry(engine)
    current = registry.get_current("alpha")
    assert current is not None
    assert current.description == "v2-edited"


def test_in_place_edit_without_version_bump_is_idempotent(
    engine: sa.Engine, tmp_path: Path
) -> None:
    """In-place body edit that does NOT advance the frontmatter version must be
    idempotent after the first auto-bump (R-Argus-P1). Because ``version`` is
    part of ``content_hash``, a naive hash-equality check would append a fresh
    version on EVERY boot (unbounded growth + current_version drift)."""
    root = tmp_path / "bundle"
    _write_pack(root, "alpha", version=1, description="v1")
    reconcile_bundle_dir(engine, _settings(root))
    # Edit the body/description in place but KEEP version=1 (the broken branch).
    _write_pack(root, "alpha", version=1, description="v1-edited-in-place")
    registry = SqlAlchemySkillRegistry(engine)

    run2 = reconcile_bundle_dir(engine, _settings(root))
    assert run2.updated == ("alpha",)  # first edit-wins append (auto-bumped to v2)
    assert registry.current_version_of("alpha") == 2

    # Every subsequent boot over the same unchanged dir must be all-unchanged.
    run3 = reconcile_bundle_dir(engine, _settings(root))
    assert run3.updated == ()
    assert run3.unchanged == ("alpha",)
    assert registry.current_version_of("alpha") == 2

    run4 = reconcile_bundle_dir(engine, _settings(root))
    assert run4.updated == ()
    assert run4.unchanged == ("alpha",)
    assert registry.current_version_of("alpha") == 2

    current = registry.get_current("alpha")
    assert current is not None
    assert current.description == "v1-edited-in-place"


def test_locally_edited_not_clobbered_until_rearmed(engine: sa.Engine, tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    _write_pack(root, "alpha", description="orig")
    reconcile_bundle_dir(engine, _settings(root))
    registry = SqlAlchemySkillRegistry(engine)
    # simulate a local edit: append a version whose provenance origin != 'bundle'
    from .conftest import make_pack

    registry.append_version(
        make_pack("alpha", version=2, description="local-edit"), origin="steward-apply"
    )
    # bundle now ships a different content — must NOT clobber the local edit
    _write_pack(root, "alpha", version=3, description="bundle-v3")
    report = reconcile_bundle_dir(engine, _settings(root))
    assert report.skipped_locally_edited == ("alpha",)
    edited = registry.get_current("alpha")
    assert edited is not None and edited.description == "local-edit"
    # re-arm via a sync_rearmed lifecycle event ⇒ next reconcile updates
    registry.append_lifecycle("alpha", "sync_rearmed", actor="steward")
    report2 = reconcile_bundle_dir(engine, _settings(root))
    assert report2.updated == ("alpha",)
    rearmed = registry.get_current("alpha")
    assert rearmed is not None and rearmed.description == "bundle-v3"


def test_invalid_pack_quarantined(engine: sa.Engine, tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    d = root / "broken"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: BADNAME\nversion: 1\nsource: bundled\n---\nno sections", encoding="utf-8"
    )
    report = reconcile_bundle_dir(engine, _settings(root))
    assert report.quarantined == ("broken",)
    registry = SqlAlchemySkillRegistry(engine)
    row = {r.name: r for r in registry.library_rows()}["broken"]
    assert row.status == "quarantined"


def test_manual_pack_pending_forge(engine: sa.Engine, tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    _write_pack(root, "manual-pack", source="manual")
    report = reconcile_bundle_dir(engine, _settings(root))
    assert report.quarantined == ("manual-pack",)
    registry = SqlAlchemySkillRegistry(engine)
    # excluded from active read paths (quarantined, no version)
    assert registry.get_current("manual-pack") is None
    row = {r.name: r for r in registry.library_rows()}["manual-pack"]
    assert row.status == "quarantined"


def test_missing_bundle_dir_raises(engine: sa.Engine, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        reconcile_bundle_dir(engine, SkillsSettings(bundle_dir=str(tmp_path / "nope")))


def test_empty_bundle_dir_noop(engine: sa.Engine) -> None:
    report = reconcile_bundle_dir(engine, SkillsSettings(bundle_dir=""))
    assert report.registered == ()
    assert report.quarantined == ()


# -- Codex P1: a repaired quarantined pack must return to active --------------


def test_repaired_quarantined_pack_is_restored(engine: sa.Engine, tmp_path: Path) -> None:
    """A registered pack that went invalid (quarantined) is RESTORED to active
    when a valid file reappears — otherwise the last lifecycle event stays
    ``quarantined`` and the pack can never be searched/viewed again."""
    root = tmp_path / "bundle"
    _write_pack(root, "alpha")
    reconcile_bundle_dir(engine, _settings(root))
    registry = SqlAlchemySkillRegistry(engine)
    assert registry.current_status("alpha") == "active"

    _break(root, "alpha")
    quarantine = reconcile_bundle_dir(engine, _settings(root))
    assert "alpha" in quarantine.quarantined
    assert registry.current_status("alpha") == "quarantined"
    assert registry.get_current("alpha") is None  # invisible while quarantined

    _write_pack(root, "alpha", description="repaired")
    repaired = reconcile_bundle_dir(engine, _settings(root))
    assert "alpha" in repaired.updated
    assert registry.current_status("alpha") == "active"
    current = registry.get_current("alpha")
    assert current is not None
    assert current.description == "repaired"


def test_repaired_quarantined_identical_content_restored(engine: sa.Engine, tmp_path: Path) -> None:
    """Repair to the SAME content the registry already holds still restores the
    pack (the unchanged-content branch must also emit a ``restored`` event)."""
    root = tmp_path / "bundle"
    _write_pack(root, "beta")
    reconcile_bundle_dir(engine, _settings(root))
    _break(root, "beta")
    reconcile_bundle_dir(engine, _settings(root))
    registry = SqlAlchemySkillRegistry(engine)
    assert registry.current_status("beta") == "quarantined"

    _write_pack(root, "beta")  # byte-identical to the original registered version
    report = reconcile_bundle_dir(engine, _settings(root))
    assert "beta" in report.updated  # a restore is a state change, not "unchanged"
    assert registry.current_status("beta") == "active"
    assert registry.get_current("beta") is not None
    # idempotent afterwards: a further run leaves it active + unchanged.
    again = reconcile_bundle_dir(engine, _settings(root))
    assert again.unchanged == ("beta",)
    assert registry.current_status("beta") == "active"


# -- Codex P1: reconcile is serialized across replicas via an advisory lock ----


def test_advisory_lock_serializes_on_postgres() -> None:
    """On a Postgres engine, reconcile takes + releases a session advisory lock
    so concurrent worker replicas cannot race the check-then-insert / next-version
    paths into a unique-constraint boot crash."""
    calls: list[tuple[str, Any]] = []

    class _FakeConn:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, stmt: object, params: object = None) -> None:
            calls.append((str(stmt), params))

        def close(self) -> None:
            self.closed = True

    conn = _FakeConn()

    class _FakeDialect:
        name = "postgresql"

    class _FakeEngine:
        dialect = _FakeDialect()

        def connect(self) -> _FakeConn:
            return conn

    with _reconcile_advisory_lock(cast(sa.Engine, _FakeEngine())):
        pass

    assert conn.closed
    assert any("pg_advisory_lock" in sql for sql, _ in calls)
    assert any("pg_advisory_unlock" in sql for sql, _ in calls)
    assert calls[0][1] == {"k": _RECONCILE_ADVISORY_LOCK_KEY}


# -- NE-SKILL-4: the retirement-resurrection guard ----------------------------


def test_retired_bundled_pack_stays_retired_unchanged(engine: sa.Engine, tmp_path: Path) -> None:
    """A RETIRED bundled pack whose file is UNCHANGED survives reconcile retired
    (no lifecycle transition reactivates it)."""
    from ne.skills.retirement import retire_skill

    root = tmp_path / "bundle"
    _write_pack(root, "alpha")
    reconcile_bundle_dir(engine, _settings(root))
    retire_skill(engine, "alpha", actor="steward", reason="rot")
    registry = SqlAlchemySkillRegistry(engine)
    assert registry.current_status("alpha") == "retired"

    report = reconcile_bundle_dir(engine, _settings(root))
    assert registry.current_status("alpha") == "retired"
    assert "alpha" in report.unchanged
    assert "alpha" not in report.updated


def test_retired_bundled_pack_stays_retired_edit_wins(engine: sa.Engine, tmp_path: Path) -> None:
    """A RETIRED bundled pack whose file CHANGES may edit-wins-append a content
    version (harmless) but NO lifecycle transition reactivates it."""
    from ne.skills.retirement import retire_skill

    root = tmp_path / "bundle"
    _write_pack(root, "alpha", description="v1")
    reconcile_bundle_dir(engine, _settings(root))
    retire_skill(engine, "alpha", actor="steward", reason="rot")

    _write_pack(root, "alpha", version=2, description="v2-edited")
    report = reconcile_bundle_dir(engine, _settings(root))
    registry = SqlAlchemySkillRegistry(engine)
    assert registry.current_status("alpha") == "retired"  # STILL retired
    assert "alpha" in report.updated  # content version appended (restorable), no lifecycle


def test_retired_bundled_pack_invalid_file_not_relaundered(
    engine: sa.Engine, tmp_path: Path
) -> None:
    """A RETIRED bundled pack whose file goes INVALID is NOT re-quarantined — the
    quarantine-laundering path (retired → quarantined → restored-active) is
    closed. It is reported ``skipped_retired``."""
    from ne.skills.retirement import retire_skill

    root = tmp_path / "bundle"
    _write_pack(root, "alpha")
    reconcile_bundle_dir(engine, _settings(root))
    retire_skill(engine, "alpha", actor="steward", reason="rot")

    _break(root, "alpha")
    report = reconcile_bundle_dir(engine, _settings(root))
    registry = SqlAlchemySkillRegistry(engine)
    assert registry.current_status("alpha") == "retired"
    assert "alpha" in report.skipped_retired
    assert "alpha" not in report.quarantined


def test_advisory_lock_noop_on_sqlite() -> None:
    """SQLite has no advisory locks; reconcile must not even open a connection."""

    class _FakeDialect:
        name = "sqlite"

    class _FakeEngine:
        dialect = _FakeDialect()

        def connect(self) -> object:
            raise AssertionError("sqlite must not open a lock connection")

    with _reconcile_advisory_lock(cast(sa.Engine, _FakeEngine())):
        pass
