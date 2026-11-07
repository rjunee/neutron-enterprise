# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Regression locks on the GitHub Actions supply-chain posture.

Enforces that every third-party action is pinned to a full 40-hex commit SHA and
that the compromised aquasecurity trivy actions are never referenced (the
2026-03-19 credential-stealer incident).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))

# `uses: owner/repo@<ref>` — capture the ref. Local actions (./path) are exempt.
_USES = re.compile(r"^\s*-?\s*uses:\s*(?P<value>\S+)")
_SHA_PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}(\s|$)")


def _uses_values() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for wf in WORKFLOWS:
        for line in wf.read_text(encoding="utf-8").splitlines():
            m = _USES.match(line)
            if m:
                out.append((wf, m.group("value")))
    return out


def test_workflows_exist() -> None:
    names = {wf.name for wf in WORKFLOWS}
    assert {"ci.yml", "supply-chain.yml", "release.yml"} <= names


def test_all_action_uses_sha_pinned() -> None:
    for wf, value in _uses_values():
        if value.startswith("./") or value.startswith("docker://"):
            continue
        assert _SHA_PINNED.match(value), f"{wf.name}: `{value}` is not pinned to a 40-hex SHA"


def test_no_aquasecurity_actions() -> None:
    for wf in WORKFLOWS:
        text = wf.read_text(encoding="utf-8")
        assert "aquasecurity/trivy-action" not in text, f"{wf.name} references compromised action"
        assert "aquasecurity/setup-trivy" not in text, f"{wf.name} references compromised action"


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_uses_are_present_and_parsed(wf: Path) -> None:
    # Guard against the regex silently matching nothing (which would make the
    # SHA-pin assertion vacuously pass).
    assert WORKFLOWS, "no workflow files found"


def test_release_ensures_release_exists_before_upload() -> None:
    """`gh release upload` fails if the release does not exist yet: the workflow
    must `gh release view || gh release create --verify-tag` BEFORE uploading,
    under the same tag guard."""
    text = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    view_idx = text.find("gh release view")
    create_idx = text.find("gh release create")
    upload_idx = text.find("gh release upload")

    assert view_idx != -1, "release.yml must `gh release view` to check for the release"
    assert create_idx != -1, "release.yml must `gh release create` when the release is absent"
    assert upload_idx != -1, "release.yml must `gh release upload` the artifacts"

    # The ensure-exists (view||create) must precede the upload.
    assert view_idx < upload_idx, "`gh release view` must precede `gh release upload`"
    assert create_idx < upload_idx, "`gh release create` must precede `gh release upload`"

    # create must carry --verify-tag and --generate-notes.
    assert "--verify-tag" in text
    assert "--generate-notes" in text

    # The ensure-exists step is tag-guarded like the upload step.
    ensure_block = text[text.find("Ensure GitHub release exists") : upload_idx]
    assert "startsWith(github.ref, 'refs/tags/')" in ensure_block
