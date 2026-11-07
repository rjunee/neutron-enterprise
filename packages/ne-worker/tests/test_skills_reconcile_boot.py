# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SKILL-1a: the worker boot reconcile registers a bundle dir's packs.

Drives ``reconcile_bundle_dir`` directly against a platform-built skills engine
(the boot-composition level); the full worker-boot on the compose stack is
LIVE-PARKABLE. Empty bundle_dir no-ops; a missing configured dir raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.skills.config import SkillsSettings
from ne.skills.reconcile import reconcile_bundle_dir
from ne.skills.registry import SqlAlchemySkillRegistry, ensure_skills_schema

GOOD_BODY = "## When to Use\nx\n## Procedure\nx\n## Pitfalls\nx\n## Verification\nx\n"


@pytest.fixture
def engine(tmp_path: Path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'skills.db'}")
    ensure_skills_schema(eng)
    yield eng
    eng.dispose()


def _write_pack(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\nversion: 1\ndescription: {name} skill\nsource: bundled\n"
        f"triggers:\n  - use {name}\n---\n{GOOD_BODY}",
        encoding="utf-8",
    )


def test_boot_reconcile_registers_bundle(engine: sa.Engine, tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    _write_pack(bundle, "alpha")
    _write_pack(bundle, "bravo")
    report = reconcile_bundle_dir(engine, SkillsSettings(bundle_dir=str(bundle)))
    assert set(report.registered) == {"alpha", "bravo"}
    registry = SqlAlchemySkillRegistry(engine)
    assert registry.get_current("alpha") is not None
    assert registry.get_current("bravo") is not None


def test_empty_bundle_dir_noop(engine: sa.Engine) -> None:
    report = reconcile_bundle_dir(engine, SkillsSettings(bundle_dir=""))
    assert report.registered == ()


def test_missing_configured_dir_raises(engine: sa.Engine, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        reconcile_bundle_dir(engine, SkillsSettings(bundle_dir=str(tmp_path / "does-not-exist")))
