# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""deploy/backup ops artifacts parse correctly and carry no secrets; the drill
workflow uses only 40-hex-pinned actions and sets NE_BACKUP_DRILL=1."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKUP = _REPO_ROOT / "deploy" / "backup"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "backup-restore-drill.yml"

# A secret-shaped ASSIGNMENT: a name containing password/secret/token/key_b64
# receiving a concrete literal value (not a ${VAR} reference and not empty).
_SECRET_ASSIGN = re.compile(r"(?i)\b\w*(password|secret|token|key_b64)\w*\b\s*[:=]\s*(?P<val>\S+)")
_PRIVATE_KEY = re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")


def _parse_unit(text: str) -> dict[str, dict[str, str]]:
    section: str | None = None
    out: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out[section] = {}
        elif "=" in line and section is not None:
            key, _, value = line.partition("=")
            out[section][key.strip()] = value.strip()
    return out


def test_timer_is_daily_and_persistent() -> None:
    unit = _parse_unit((_BACKUP / "systemd" / "ne-backup.timer").read_text())
    assert unit["Timer"]["OnCalendar"] == "*-*-* 02:00:00"
    assert unit["Timer"]["Persistent"] == "true"
    assert unit["Install"]["WantedBy"] == "timers.target"


def test_service_is_oneshot_and_invokes_run_backup() -> None:
    unit = _parse_unit((_BACKUP / "systemd" / "ne-backup.service").read_text())
    assert unit["Service"]["Type"] == "oneshot"
    assert unit["Service"]["ExecStart"].endswith("run-backup.sh")


def test_run_backup_creates_then_prunes() -> None:
    script = (_BACKUP / "run-backup.sh").read_text()
    assert "set -euo pipefail" in script
    assert "ne-backup create --kind scheduled" in script
    assert "ne-backup prune --keep" in script
    create_idx = script.index("ne-backup create")
    prune_idx = script.index("ne-backup prune")
    assert create_idx < prune_idx


def test_no_secret_literals_in_deploy_backup() -> None:
    for path in _BACKUP.rglob("*"):
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if line.startswith("#"):  # prose / comments name concepts, not values
                continue
            assert not _PRIVATE_KEY.search(line), f"private key material in {path}: {line!r}"
            match = _SECRET_ASSIGN.search(line)
            if match:
                value = match.group("val")
                # A ${VAR...} reference or a placeholder is fine; a concrete literal
                # secret value is not.
                assert value.startswith("${") or value.startswith('"${'), (
                    f"possible secret literal in {path}: {line!r}"
                )


def test_workflow_pins_and_drill_env() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    # Every `uses:` is a 40-hex SHA pin.
    for match in re.finditer(r"uses:\s*(\S+)", text):
        value = match.group(1)
        assert re.match(r"^[^@\s]+@[0-9a-f]{40}$", value), f"unpinned action: {value}"
    drill = doc["jobs"]["drill"]
    assert drill["env"]["NE_BACKUP_DRILL"] == "1" or drill["env"]["NE_BACKUP_DRILL"] == 1
    # The executing-gate grep is present (anti-vacuous-skip).
    assert "skipped" in text and "no tests ran" in text
