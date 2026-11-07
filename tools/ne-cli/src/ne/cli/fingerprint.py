# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic platform release fingerprint (NE-069).

A platform release is the tuple **{git tag vX.Y.Z, per-member CycloneDX SBOMs
(NE-CKB1), composed default platform config-hash (NE-CKC12)}** — see
``RELEASES.md`` at the repository root for the policy this implements. This
module computes that tuple as a single deterministic "release manifest" and a
"fingerprint" (a SHA-256 digest over the manifest's canonical JSON), reusing
the existing SBOM generator and config-hash machinery rather than reinventing
either:

* SBOMs come from ``ne.cli.sbom.build_sbom`` (NE-CKB1), with
  ``deterministic_serial=True`` so re-running the fingerprint over an
  unchanged tree is byte-reproducible.
* The config-hash comes from the FROZEN ``ne-contracts`` 1.2.0
  ``compute_platform_config_hash`` (NE-CKC12), composed over exactly the
  default module set ``ne.runtime.factory.freeze_platform_config`` assembles
  at boot ({"ne-runtime": RuntimeSettings(), "ne-govern": GovernSettings()}).
  A release's config-hash is therefore defined as the tree's DEFAULT composed
  configuration — not any one deployment's frozen, environment-specific hash
  (which is what ``freeze_platform_config`` itself produces at process start).
  ``test_release_fingerprint.py::test_fingerprint_config_hash_matches_composition_root_freeze``
  is the parity guard on this coupling: if a future composition root changes
  the default module set, that test goes red instead of the fingerprint
  silently drifting in meaning.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click

from ne.cli.checks import _find_repo_root
from ne.cli.licenses import Policy, default_policy_path
from ne.cli.sbom import _is_member, build_sbom


@contextmanager
def _scrubbed_determinism_guard() -> Iterator[None]:
    """Scrub ``NE_*`` env and ``cwd`` so neither leaks into the release config-hash.

    ``NeSettings`` layers environment (``NE_`` prefix) and a cwd ``ne.toml`` on
    top of field defaults (see ``ne.contracts.config.NeSettings``). A release's
    config-hash is defined as the tree's DEFAULT composed configuration — a
    deployment-specific hash is frozen separately, at boot, by
    ``freeze_platform_config`` (NE-CKC12). Without this guard, whatever
    environment/`ne.toml` happens to be lying around the process that computes
    the fingerprint would silently change the release identity. Restores both
    on exit, even on error.
    """
    saved_env = {k: v for k, v in os.environ.items() if k.startswith("NE_")}
    for k in saved_env:
        del os.environ[k]
    saved_cwd = Path.cwd()
    tmp_dir = tempfile.mkdtemp(prefix="ne-release-fingerprint-")
    try:
        os.chdir(tmp_dir)
        yield
    finally:
        os.chdir(saved_cwd)
        for k, v in saved_env.items():
            os.environ[k] = v


def compute_default_platform_config_hash() -> str:
    """The release config-hash: the frozen NE-CKC12 hash over the DEFAULT composition.

    Lazy imports: ``ne-contracts``/``ne-runtime``/``ne-govern`` are RUNTIME
    dependencies of ``ne-cli`` (moved from a dev-only group in the NE-069
    continuation so ``ne release fingerprint``/``ne release bundle`` work on a
    normal, non-dev install — only this function needs them). Importing
    ``ne.runtime`` pulls a heavy dependency graph, so the import stays lazy
    inside this function purely to keep every other ``ne`` command
    (``--help``, ``check``, ...) fast to start.
    """
    from ne.contracts.config import compute_platform_config_hash
    from ne.govern.config import GovernSettings
    from ne.runtime.config import RuntimeSettings

    with _scrubbed_determinism_guard():
        # Mirrors EXACTLY the default module composition
        # ne.runtime.factory.freeze_platform_config assembles when called with no
        # settings/govern_settings/extra_modules — see the module docstring and
        # the parity test guarding this coupling.
        modules = {"ne-runtime": RuntimeSettings(), "ne-govern": GovernSettings()}
        return compute_platform_config_hash(modules)


def _canonical_json(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(args: list[str], root: Path) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    return result.stdout.strip()


def _worktree_dirty(root: Path) -> bool:
    """True iff `git status --porcelain` reports any change. Module-level so tests
    can monkeypatch it directly rather than shelling out to git."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


def _tree_hash(root: Path) -> str:
    """`git rev-parse HEAD^{tree}` — content identity of the checked-out tree,
    not the commit (a release fingerprint identifies WHAT was built, and two
    different commits with identical trees, e.g. an empty merge, build the
    same artifact)."""
    return _git(["rev-parse", "HEAD^{tree}"], root)


def _resolve_tag(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def build_manifest(root: Path, tag: str) -> dict:
    """Build the deterministic release manifest for ``root`` at platform version ``tag``.

    Reuses ``ne.cli.sbom.build_sbom`` (NE-CKB1) with ``deterministic_serial=True``
    for every workspace member found in ``uv.lock``, and
    ``compute_default_platform_config_hash`` (NE-CKC12) for the composed
    config-hash. The manifest's ``members`` list is sorted by name so the
    manifest — and therefore the fingerprint — is independent of filesystem or
    dict iteration order.
    """
    lock_path = root / "uv.lock"
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages: list[dict] = data.get("package", [])
    policy = Policy.load(default_policy_path(root))

    members: list[dict] = []
    for name in sorted(p["name"] for p in packages if _is_member(p)):
        doc = build_sbom(lock_path, policy, name, deterministic_serial=True)
        version = doc["metadata"]["component"]["version"]
        content = json.dumps(doc, indent=2) + "\n"
        content_bytes = content.encode("utf-8")
        members.append(
            {
                "name": name,
                "version": version,
                "sbom": f"sbom/{name}-{version}.cdx.json",
                "sha256": _sha256_bytes(content_bytes),
            }
        )
    members.sort(key=lambda m: m["name"])

    return {
        "schema": "ne-release-manifest/1",
        "platform_version": tag,
        "tree": _tree_hash(root),
        "config_hash": compute_default_platform_config_hash(),
        "members": members,
    }


def compute_fingerprint(manifest: dict) -> str:
    """``sha256:<hex>`` over the canonical JSON of ``manifest``."""
    canonical = _canonical_json(manifest)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_tag(root: Path, tag: str | None) -> str:
    if tag:
        return tag
    resolved = _resolve_tag(root)
    if resolved is None:
        raise click.ClickException("HEAD is not tagged; pass --tag")
    return resolved


def _require_clean(root: Path, allow_dirty: bool) -> None:
    if allow_dirty:
        return
    if _worktree_dirty(root):
        raise click.ClickException(
            "worktree has uncommitted changes; refusing to compute a release "
            "fingerprint over a dirty tree (pass --allow-dirty to override)"
        )


@click.command("fingerprint")
@click.option(
    "--tag",
    "tag",
    default=None,
    help="Platform version tag (default: `git describe --tags --exact-match HEAD`).",
)
@click.option(
    "--allow-dirty",
    "allow_dirty",
    is_flag=True,
    default=False,
    help="Allow a dirty worktree (default: refuse, fail-closed).",
)
def fingerprint(tag: str | None, allow_dirty: bool) -> None:
    """Print the deterministic release manifest + fingerprint for this tree."""
    root = _find_repo_root(Path.cwd())
    _require_clean(root, allow_dirty)
    resolved_tag = _require_tag(root, tag)
    manifest = build_manifest(root, resolved_tag)
    fp = compute_fingerprint(manifest)
    click.echo(json.dumps({**manifest, "fingerprint": fp}, indent=2, sort_keys=True))
