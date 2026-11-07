# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the shared OPA governance-bundle staging (NE-PILOT-OPA-BUNDLE).

No docker/helm needed — these are pure filesystem + YAML-shape guardians for
``ne.cli.opa_bundle``, the module shared by ``ne demo up``, ``ne up``, and
``ne k8s render``/``ne k8s up``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

import ne.cli.opa_bundle as opa_bundle
from ne.cli.opa_bundle import (
    bundle_content_hash,
    govern_bundle_files,
    stage_govern_policies,
    write_compose_opa_overlay,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
GOVERN_POLICIES = REPO_ROOT / "packages" / "ne-govern" / "policies"

_KEY_CHARSET = re.compile(r"[-._a-zA-Z0-9]+")

#: The deployment's OPA data document — the served bundle's only JSON DATA
#: member (``.manifest`` is the other non-rego file). It carries
#: `data.ne.config.approval_windows`; a bundle without it falls back to
#: `tiers.rego`'s built-in floor and promises four hours this deployment
#: cannot keep (see `ne.cli.opa_bundle._policy_data_document`).
_DATA_DOCUMENT = Path("ne/config/data.json")

_EXPECTED_KEYS = {
    ".manifest",
    "ne__govern__decision.rego",
    "ne__govern__subagent.rego",
    "ne__govern__tiers.rego",
    "ne__govern__version.rego",
    "ne__config__data.json",
}


def _source_files() -> dict[Path, bytes]:
    """Every non-test file under packages/ne-govern/policies, keyed by relpath."""
    out: dict[Path, bytes] = {}
    for path in GOVERN_POLICIES.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith("_test.rego"):
            continue
        out[path.relative_to(GOVERN_POLICIES)] = path.read_bytes()
    return out


def test_stage_byte_matches_source(tmp_path: Path) -> None:
    dest = tmp_path / "staged"
    result = stage_govern_policies(dest, root=REPO_ROOT)
    assert result == dest

    source = _source_files()
    source[_DATA_DOCUMENT] = (REPO_ROOT / "deploy/opa/data" / _DATA_DOCUMENT).read_bytes()
    staged = {p.relative_to(dest): p.read_bytes() for p in dest.rglob("*") if p.is_file()}
    assert set(staged) == set(source)
    assert staged == source
    # The six members must all be present (.manifest + 4 rego + the DEPLOYMENT's
    # data document, which is what puts approval_windows in front of tiers.rego).
    assert set(source) == {
        Path(".manifest"),
        Path("ne/govern/decision.rego"),
        Path("ne/govern/subagent.rego"),
        Path("ne/govern/tiers.rego"),
        Path("ne/govern/version.rego"),
        _DATA_DOCUMENT,
    }
    # Non-vacuous: the staged document really carries the deployment's windows at
    # the path OPA's directory loader maps to `data.ne.config`.
    assert "approval_windows" in json.loads((dest / _DATA_DOCUMENT).read_text())


def test_stage_excludes_test_rego_and_keeps_package(tmp_path: Path) -> None:
    dest = tmp_path / "staged"
    stage_govern_policies(dest, root=REPO_ROOT)

    assert not list(dest.rglob("*_test.rego"))
    decision = (dest / "ne" / "govern" / "decision.rego").read_text(encoding="utf-8")
    assert "package ne.govern" in decision
    # Non-vacuous: the shipped placeholder carries package ne.dev, not ne.govern.
    assert "package ne.dev" not in decision


def test_stage_is_idempotent_and_replaces_stale_contents(tmp_path: Path) -> None:
    dest = tmp_path / "staged"
    dest.mkdir()
    stale = dest / "stale-leftover.rego"
    stale.write_text("package stale\n", encoding="utf-8")

    stage_govern_policies(dest, root=REPO_ROOT)

    assert not stale.exists()
    assert (dest / "ne" / "govern" / "decision.rego").is_file()


def test_compose_overlay_mounts_staged_dir(tmp_path: Path) -> None:
    staged = tmp_path / "opa-policies"
    stage_govern_policies(staged, root=REPO_ROOT)
    overlay_path = tmp_path / "opa-govern.overlay.yaml"

    result = write_compose_opa_overlay(overlay_path, staged)
    assert result == overlay_path

    doc = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    volumes = doc["services"]["opa"]["volumes"]
    assert volumes == [f"{staged.resolve()}:/policies:ro"]
    assert staged.resolve().is_absolute()


def test_govern_bundle_files_parity_and_valid_keys() -> None:
    files = govern_bundle_files(root=REPO_ROOT)

    assert set(files) == _EXPECTED_KEYS
    for key, spec in files.items():
        assert _KEY_CHARSET.fullmatch(key), key
        root_dir = (
            REPO_ROOT / "deploy/opa/data" if key.startswith("ne__config__") else GOVERN_POLICIES
        )
        source_path = root_dir / spec["path"]
        assert source_path.is_file(), spec["path"]
        assert spec["content"] == source_path.read_text(encoding="utf-8")
    # path values round-trip to the exact relative POSIX path.
    assert files["ne__govern__decision.rego"]["path"] == "ne/govern/decision.rego"
    assert files[".manifest"]["path"] == ".manifest"
    # The K8s tier must serve the SAME data document the Compose tier mounts —
    # without it K8s resolves tiers.rego's 4 h floor and promises four hours it
    # cannot keep, while Compose promises the three minutes it can.
    assert files["ne__config__data.json"]["path"] == "ne/config/data.json"
    assert "approval_windows" in json.loads(files["ne__config__data.json"]["content"])


def test_missing_data_document_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """A bundle staged without the deployment's data document is a LOUD error.

    A missing document means tiers.rego's 4 h built-in floor applies. That does
    not refuse anything — the park-time coherence check warns and parks on a
    floor — it does something quieter: every tier-2 row's `expires_at` becomes a
    four-hour promise this deployment strands as `authorization_expired` fifteen
    minutes in, which is the measured 12.7% failure the document's 180 s removes.
    "Stage nothing and hope" ships that silently, so it must fail at bring-up
    instead — the same discipline as a missing policies dir.

    MUTATION-CHECK: make `_policy_data_document` return `None`/skip instead of
    raising, or stop calling it from `stage_govern_policies` /
    `govern_bundle_files`, and this goes RED with DID NOT RAISE. ⛔ Dropping
    `POLICY_DATA_DOCUMENT` from `wanted` is NOT that mutation and leaves this
    GREEN: `stage_govern_policies` resolves the document BEFORE it builds
    `wanted`, and `govern_bundle_files` never builds `wanted` at all.
    """
    import click

    # A real policies dir (so the FIRST fail-closed check passes) with no
    # deploy/opa/data anywhere above it.
    isolated = tmp_path / "isolated"
    policies = isolated / "packages" / "ne-govern" / "policies"
    (policies / "ne" / "govern").mkdir(parents=True)
    (policies / ".manifest").write_text('{"revision": "test", "roots": ["ne/govern"]}\n')
    (policies / "ne" / "govern" / "decision.rego").write_text("package ne.govern\n")
    monkeypatch.setattr(opa_bundle, "_installed_govern_policies_path", lambda: None)

    with pytest.raises(click.ClickException) as staged:
        stage_govern_policies(tmp_path / "staged", root=isolated)
    assert "approval windows" in str(staged.value)

    with pytest.raises(click.ClickException) as helm:
        govern_bundle_files(root=isolated)
    assert "approval windows" in str(helm.value)


def test_a_data_document_above_the_checkout_is_not_adopted(tmp_path: Path, monkeypatch) -> None:
    """The ancestor search stops at the checkout root, so a stray document above
    it is NOT this deployment's approval-window document.

    `_data_roots_near` walked every ancestor to `/`. A
    `deploy/opa/data/ne/config/data.json` in any of them — a sibling clone's
    parent, a home directory, the tmpdir a test happened to run in — was adopted
    silently and staged into the served bundle. That is the worst available
    failure shape: it does not refuse, it succeeds with someone else's windows,
    and every downstream comparison agrees with the wrong answer.

    MUTATION-CHECK: restore the unbounded `policies.resolve().parents` in
    `_data_roots_near` and this goes RED with DID NOT RAISE — the stray document
    one level above the checkout root (the first ancestor past the
    `ancestors[:3]` bound) is picked up instead.
    """
    import click

    outside = tmp_path / "outside"
    checkout = outside / "checkout"
    policies = checkout / "packages" / "ne-govern" / "policies"
    (policies / "ne" / "govern").mkdir(parents=True)
    (policies / ".manifest").write_text('{"revision": "test", "roots": ["ne/govern"]}\n')
    (policies / "ne" / "govern" / "decision.rego").write_text("package ne.govern\n")
    monkeypatch.setattr(opa_bundle, "_installed_govern_policies_path", lambda: None)

    stray = outside / "deploy" / "opa" / "data" / "ne" / "config"
    stray.mkdir(parents=True)
    (stray / "data.json").write_text(
        json.dumps({"approval_windows": {"actions": {"write.external.email.send": 86400}}}),
        encoding="utf-8",
    )

    with pytest.raises(click.ClickException) as staged:
        stage_govern_policies(tmp_path / "staged", root=checkout)
    assert "approval windows" in str(staged.value)

    # Non-vacuous: the SAME document one level lower — inside the checkout, where
    # the repo actually keeps it — is found and staged.
    inside = checkout / "deploy" / "opa" / "data" / "ne" / "config"
    inside.mkdir(parents=True)
    (inside / "data.json").write_text(
        json.dumps({"approval_windows": {"actions": {"write.external.email.send": 180}}}),
        encoding="utf-8",
    )
    dest = stage_govern_policies(tmp_path / "staged-ok", root=checkout)
    document = json.loads((dest / _DATA_DOCUMENT).read_text(encoding="utf-8"))
    assert document["approval_windows"]["actions"]["write.external.email.send"] == 180


def test_govern_bundle_files_excludes_test_rego() -> None:
    files = govern_bundle_files(root=REPO_ROOT)
    for spec in files.values():
        assert not spec["path"].endswith("_test.rego")


@pytest.mark.parametrize("func", [stage_govern_policies, govern_bundle_files])
def test_missing_policies_dir_fails_closed(tmp_path: Path, func, monkeypatch) -> None:
    import click

    # Force the C1 installed-package fallback to miss too: this dev venv has
    # ne-govern editable-installed (pointing at the REAL repo checkout), so
    # without this the fallback would find the real policies and mask the
    # "truly nowhere found" case this test exists to prove.
    monkeypatch.setattr(opa_bundle, "_installed_govern_policies_path", lambda: None)

    fake_root = tmp_path / "not-a-repo-checkout"
    fake_root.mkdir()

    with pytest.raises(click.ClickException) as excinfo:
        if func is stage_govern_policies:
            func(tmp_path / "dest", root=fake_root)
        else:
            func(root=fake_root)
    assert "ne-govern policies not found" in str(excinfo.value)
    assert str(fake_root) in str(excinfo.value)


def test_missing_root_falls_back_to_installed_package(tmp_path: Path, monkeypatch) -> None:
    """C1: a CWD/root that doesn't contain packages/ne-govern/policies (e.g. an
    operator invoking `ne k8s render`/`ne up` from outside the checkout) must
    still succeed when ne-govern is installed EDITABLE/WORKSPACE — the only
    shape any documented deploy path uses — and its package-adjacent policies/
    dir can be located independent of `root`.

    ⛔ NOT "editable or otherwise", which an earlier revision of this docstring
    said. A non-editable wheel has no checkout ancestor holding
    `deploy/opa/data`, so `_data_roots_near` yields only candidates that do not
    exist and the data document fails closed — the case
    `_installed_govern_policies_path` already documents as unsupported. This
    test passes because the fallback here points at the CHECKOUT's own
    policies/, whose root ancestor does hold the document."""
    monkeypatch.setattr(opa_bundle, "_installed_govern_policies_path", lambda: GOVERN_POLICIES)

    fake_root = tmp_path / "not-a-repo-checkout"
    fake_root.mkdir()

    files = govern_bundle_files(root=fake_root)
    assert set(files) == _EXPECTED_KEYS

    dest = tmp_path / "staged"
    stage_govern_policies(dest, root=fake_root)
    assert (dest / "ne" / "govern" / "decision.rego").is_file()


def test_fallback_not_consulted_when_root_has_policies(tmp_path: Path, monkeypatch) -> None:
    """The CWD/root-relative path is tried FIRST; the installed-package
    fallback is a true fallback, never preferred over an explicit root."""

    def _boom() -> Path:
        raise AssertionError("fallback must not be consulted when root resolves")

    monkeypatch.setattr(opa_bundle, "_installed_govern_policies_path", _boom)
    stage_govern_policies(tmp_path / "staged", root=REPO_ROOT)  # must not raise


def test_stage_preserves_dest_inode_across_rerun(tmp_path: Path) -> None:
    """C2: a second `stage_govern_policies` re-run against an existing `dest`
    (the ALREADY-RUNNING-deployment case) must update contents IN PLACE, not
    delete-and-recreate `dest` — a running OPA container has that directory
    bind-mounted, and a Linux bind mount pins the dentry it was created
    against; swapping the inode at the same path orphans the live mount."""
    dest = tmp_path / "staged"
    stage_govern_policies(dest, root=REPO_ROOT)
    inode_before = dest.stat().st_ino

    stage_govern_policies(dest, root=REPO_ROOT)
    inode_after = dest.stat().st_ino

    assert inode_before == inode_after


def test_stage_removes_stale_subdir_in_place(tmp_path: Path) -> None:
    """Stale content nested in a now-obsolete subdirectory is removed and the
    emptied directory pruned, without ever deleting `dest` itself."""
    dest = tmp_path / "staged"
    dest.mkdir()
    stale_dir = dest / "ne" / "stale_pkg"
    stale_dir.mkdir(parents=True)
    (stale_dir / "old.rego").write_text("package ne.stale_pkg\n", encoding="utf-8")
    dest_inode = dest.stat().st_ino

    stage_govern_policies(dest, root=REPO_ROOT)

    assert not stale_dir.exists()
    assert (dest / "ne" / "govern" / "decision.rego").is_file()
    assert dest.stat().st_ino == dest_inode


def test_overlay_bundle_hash_label_present_and_stable(tmp_path: Path) -> None:
    """C2: the generated overlay carries a content-derived label so Compose's
    own recreate-diff can tell "content changed" from "content unchanged"
    across `ne up` re-runs, even though the staged directory's inode (and
    thus its mtime-based signal) never changes."""
    staged = tmp_path / "opa-policies"
    stage_govern_policies(staged, root=REPO_ROOT)
    overlay_path = tmp_path / "opa-govern.overlay.yaml"

    write_compose_opa_overlay(overlay_path, staged)
    doc = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    label = doc["services"]["opa"]["labels"]["ne.govern.bundle-hash"]
    assert label == bundle_content_hash(staged)

    # Re-running against unchanged content yields the SAME label (no spurious
    # recreate when nothing actually changed).
    stage_govern_policies(staged, root=REPO_ROOT)
    write_compose_opa_overlay(overlay_path, staged)
    doc2 = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    assert doc2["services"]["opa"]["labels"]["ne.govern.bundle-hash"] == label

    # Content actually changing (simulating an updated policy release) DOES
    # change the label, so Compose recreates the opa container and OPA
    # re-reads /policies at startup.
    (staged / "ne" / "govern" / "decision.rego").write_text(
        "package ne.govern\n# perturbed for the test\n", encoding="utf-8"
    )
    write_compose_opa_overlay(overlay_path, staged)
    doc3 = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    assert doc3["services"]["opa"]["labels"]["ne.govern.bundle-hash"] != label
