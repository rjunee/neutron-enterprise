# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared OPA governance-bundle staging (NE-PILOT-OPA-BUNDLE).

Both the pilot Compose tier (``ne up``, up.py) and the K8s tier (``ne k8s
render``/``ne k8s up``, k8s.py) need the REAL ne-govern rego bundle
(``packages/ne-govern/policies``) staged over the shipped ``ne.dev``
placeholder so a live ``/v1/data/ne/govern/decision`` query is DEFINED
instead of failing closed (``OpaClient.evaluate`` is fail-closed by design —
packages/ne-govern/src/ne/govern/opa.py). This module is the single
first-party staging mechanism, extracted from the proven ``ne demo up`` seed
staging (demo/live.py's former inline block, NE-DEMO-LIVE-SEAMS) so the demo,
compose, and Helm call sites all share one implementation rather than
inventing a third mechanism. The shipped placeholder
(deploy/compose/pilot/opa/policies/ne_system.rego and its Helm ConfigMap
mirror) is NEVER touched by this module — staging is purely additive, at a
separate runtime location.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import shutil
from pathlib import Path

import click

#: ``packages/ne-govern/policies``, relative to the repo root.
GOVERN_POLICIES_RELPATH = Path("packages") / "ne-govern" / "policies"

#: ``deploy/opa/data``, relative to the repo root — the DEPLOYMENT's OPA data
#: document tree, staged INTO the served bundle beside the rego.
#:
#: It is deliberately NOT under ``packages/ne-govern/policies``. That directory
#: is the rego TEST surface: ``opa test packages/ne-govern/policies`` and
#: ``opa eval -d packages/ne-govern/policies`` both load it whole, and
#: ``decision_test.rego``'s built-in-floor cases assert 14400/86400 by relying on
#: ``data.ne.config.approval_windows`` being ABSENT. A deployment document there
#: would silently redefine the floor those tests measure. Keeping deployment data
#: out of the policy package also keeps ``tiers.rego``'s own promise true —
#: "deployments override by supplying the data document; nothing here is baked
#: into the bundle".
POLICY_DATA_RELPATH = Path("deploy") / "opa" / "data"

#: The one relpath the deployment data tree contributes to the staged bundle.
#: OPA's directory loader keys data by DIRECTORY (the basename is dropped), so
#: ``ne/config/data.json`` lands at ``data.ne.config`` — which is where
#: ``tiers.rego`` reads ``approval_windows``.
POLICY_DATA_DOCUMENT = Path("ne") / "config" / "data.json"


def _installed_govern_policies_path() -> Path | None:
    """Best-effort fallback: locate ``policies/`` next to the INSTALLED
    ``ne.govern`` package, independent of the current working directory.

    ``_govern_policies_source`` primarily resolves ``packages/ne-govern/policies``
    relative to a repo root found by walking up from CWD (``_find_repo_root``).
    That is CWD-dependent even though every documented deploy path
    (``ne up``, ``ne k8s render``/``ne k8s up``) is used as a workspace-editable
    install today — an operator invoking the CLI from a directory other than
    the repo root would otherwise hit a spurious fail-closed error even though
    the checkout (and its policies) is right there on disk. An editable/
    workspace install of ``ne-govern`` points ``ne.govern.__file__`` at
    ``packages/ne-govern/src/ne/govern/__init__.py``; walking up its parents
    lands on ``packages/ne-govern``, whose ``policies/`` sibling is the same
    directory ``_govern_policies_source`` would have found via ``root``.
    Returns ``None`` (never raises) when ``ne.govern`` cannot be located or has
    no adjacent, populated ``policies/`` dir — callers still fail closed in
    that case. A genuinely non-editable wheel install with no source tree
    anywhere on disk (the policies are not currently packaged as wheel data)
    remains unsupported; that is a separate, larger packaging change.
    """
    try:
        spec = importlib.util.find_spec("ne.govern")
    except (ImportError, ValueError):
        return None
    if spec is None or spec.origin is None:
        return None
    for ancestor in Path(spec.origin).resolve().parents:
        candidate = ancestor / "policies"
        if candidate.is_dir() and (candidate / ".manifest").is_file():
            return candidate
    return None


def _govern_policies_source(root: Path) -> Path:
    primary = root / GOVERN_POLICIES_RELPATH
    if primary.is_dir():
        return primary
    fallback = _installed_govern_policies_path()
    if fallback is not None:
        return fallback
    raise click.ClickException(
        f"ne-govern policies not found at {primary} — run from a repo checkout"
    )


def _policy_data_document(root: Path, policies: Path) -> Path:
    """The deployment's OPA data document, or FAIL CLOSED.

    Resolved from ``root`` first and then relative to the located ``policies``
    directory, mirroring :func:`_govern_policies_source` so an operator invoking
    the CLI outside the repo root gets the same answer.

    THIS IS FAIL-CLOSED ON PURPOSE. ``tiers.rego``'s built-in floor promises a
    tier-2 approval window of 4 h; the run owner's frozen authorization on this
    deployment lasts 900 s. A bundle staged without this document does not
    refuse anything — ``_require_honourable_approval_window`` warns and parks on
    a floor — it does something quieter and worse: it writes a FOUR-HOUR promise
    into every tier-2 row's ``expires_at`` and then strands the run as an opaque
    ``authorization_expired`` a quarter of an hour later, which is the measured
    12.7% failure this document's 180 s removes. Staging nothing and hoping is
    exactly the advertise-what-cannot-be-served failure, so a missing document is
    a loud bring-up error instead of a silent four-hour lie.
    """

    for candidate in (root / POLICY_DATA_RELPATH, *_data_roots_near(policies)):
        document = candidate / POLICY_DATA_DOCUMENT
        if document.is_file():
            return document
    raise click.ClickException(
        f"deployment OPA data document not found at "
        f"{root / POLICY_DATA_RELPATH / POLICY_DATA_DOCUMENT} — it carries this "
        "deployment's approval windows, and without it every tier-2 approval "
        "silently promises the 4 h built-in floor this deployment cannot keep "
        "(docs/architecture/approval-horizon.md)"
    )


def _data_roots_near(policies: Path) -> tuple[Path, ...]:
    """Candidate ``deploy/opa/data`` dirs implied by a located policies dir.

    BOUNDED AT THE CHECKOUT ROOT, and that bound is the point. ``policies`` is
    ``<root>/packages/ne-govern/policies``, so the only ancestor that can
    legitimately hold ``deploy/opa/data`` is exactly
    ``len(GOVERN_POLICIES_RELPATH.parts)`` levels up. Walking every ancestor to
    ``/`` instead meant a stray ``deploy/opa/data/ne/config/data.json`` ANYWHERE
    above the checkout — a sibling clone's parent directory, a home directory, a
    tmpdir a test happened to run in — was silently adopted as this
    deployment's approval-window document and staged into the served bundle. The
    failure mode is the worst shape available: it does not refuse, it succeeds
    with someone else's windows.

    ⭐ AND THE FALLBACK IS WHAT THIS TUPLE EXISTS FOR, not an afterthought.
    Every documented deploy path runs a WORKSPACE-EDITABLE install today, so
    ``_installed_govern_policies_path`` returns the checkout's OWN
    ``packages/ne-govern/policies`` — whose ancestor exactly
    ``len(GOVERN_POLICIES_RELPATH.parts)`` levels up IS the checkout root, and
    the document is found there. That is the case the bound is sized for: the
    third candidate resolves, the two below it do not exist. Only a genuinely
    non-editable wheel install (``site-packages``, no source tree) has no such
    ancestor; there the truncated tuple yields candidates that do not exist and
    the caller fails closed, which is the correct answer there.
    """

    ancestors = policies.resolve().parents
    depth = len(GOVERN_POLICIES_RELPATH.parts)
    return tuple(ancestor / POLICY_DATA_RELPATH for ancestor in ancestors[:depth])


def _wanted_relpaths(source: Path) -> set[Path]:
    return {
        p.relative_to(source)
        for p in source.rglob("*")
        if p.is_file() and not p.name.endswith("_test.rego")
    }


def stage_govern_policies(dest: Path, *, root: Path) -> Path:
    """Copy the first-party ne-govern rego bundle to ``dest``, excluding tests.

    ALSO stages the DEPLOYMENT's OPA data document
    (``deploy/opa/data/ne/config/data.json`` -> ``dest/ne/config/data.json``),
    which is what puts ``data.ne.config.approval_windows`` in front of
    ``tiers.rego``. It is staged here rather than by each caller so every served
    bundle — ``ne up``, ``ne demo up`` — carries it by construction, so the
    prune below treats it as wanted instead of deleting it on the next re-run,
    and so :func:`bundle_content_hash` covers it (a changed approval window must
    recreate the OPA container exactly like a changed rule).

    OPA loads ``/policies`` recursively and ``ne/govern/*.rego`` carries
    ``package ne.govern``, so ``*_test.rego`` files are excluded (they are
    source-only fixtures, never part of the served bundle). Fail-closed:
    raises ``click.ClickException`` when ``packages/ne-govern/policies`` is
    not found under ``root`` nor via an installed ``ne.govern`` package.

    Updates ``dest``'s CONTENTS in place rather than deleting and recreating
    the directory. A re-run of ``ne up`` against an ALREADY-RUNNING core
    deployment must not swap ``dest``'s inode: a running OPA container has
    that directory bind-mounted, and a Linux bind mount pins the dentry it
    was created against, not the path string — deleting and recreating the
    same path leaves the live container looking at the orphaned OLD
    directory while fresh content lands, invisibly, at the new one. Staging
    in place (remove stale files, prune emptied dirs, copy new/changed
    files) keeps the mounted directory's identity stable across re-runs so
    the bind mount always reflects the latest staged content. Returns
    ``dest``.
    """
    source = _govern_policies_source(root)
    data_document = _policy_data_document(root, source)
    dest.mkdir(parents=True, exist_ok=True)
    wanted = _wanted_relpaths(source) | {POLICY_DATA_DOCUMENT}
    for existing in dest.rglob("*"):
        if existing.is_file() and existing.relative_to(dest) not in wanted:
            existing.unlink()
    # Prune directories left empty by the removals above, deepest first.
    for existing_dir in sorted(
        (p for p in dest.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        with contextlib.suppress(OSError):
            existing_dir.rmdir()  # no-op if not empty — still holds wanted content
    for rel in wanted:
        src_path = data_document if rel == POLICY_DATA_DOCUMENT else source / rel
        dst_path = dest / rel
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)
    return dest


def bundle_content_hash(staged_dir: Path) -> str:
    """Deterministic short digest of a staged bundle's relpath+content.

    Used to stamp the generated compose overlay so `docker compose up`
    recreates the OPA container exactly when staged policy content actually
    changes between runs — necessary because OPA (no `--watch` flag) reads
    ``/policies`` only at process startup, and because staging now updates
    ``dest`` in place (see :func:`stage_govern_policies`), so the mounted
    directory's mtime/inode no longer signal "content changed" to Compose.
    """
    digest = hashlib.sha256()
    for path in sorted(staged_dir.rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(staged_dir).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def write_compose_opa_overlay(overlay_path: Path, staged_dir: Path) -> Path:
    """Write the generated `ne up` compose overlay that re-mounts /policies.

    Hand-formatted YAML text (PyYAML is test-only — mirrors
    demo/live.py::_write_demo_overlay's string-building precedent). Compose
    merges same-target volume mounts by LATER-file-wins, so layering this
    overlay's ``-f`` before any operator/demo overlay lets either replace the
    ``/policies`` mount again (this is what keeps ``ne demo up``'s own
    opa-policies overlay byte-identically in control of the effective mount).

    Also stamps a ``ne.govern.bundle-hash`` label derived from the staged
    bundle's content (:func:`bundle_content_hash`). ``stage_govern_policies``
    updates the mounted directory IN PLACE (its inode never changes across
    re-runs — see that function's docstring), so Compose's own path/volume
    diffing can no longer tell "content changed" from "content unchanged"
    between two ``ne up`` runs against the same bundle. The label makes that
    change visible to Compose's container-recreation diff: `docker compose up`
    (no ``--force-recreate`` needed) recreates the ``opa`` service — and so
    restarts the OPA process, which only reads ``/policies`` at startup
    (no ``--watch``) — exactly when the staged policy content actually
    differs from the previous run, and leaves an already-current container
    alone otherwise. Returns ``overlay_path``.
    """
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    content_hash = bundle_content_hash(staged_dir)
    content = (
        "# Generated by `ne up` (NE-PILOT-OPA-BUNDLE) — mounts the FIRST-PARTY\n"
        "# ne-govern rego bundle over the shipped ne.dev placeholder so\n"
        "# /v1/data/ne/govern/decision is DEFINED; compose merges same-target\n"
        "# volume mounts, later file wins. The bundle-hash label forces a\n"
        "# recreate (OPA re-reads /policies at startup only) whenever staged\n"
        "# policy content actually changes between `ne up` re-runs.\n"
        "services:\n"
        "  opa:\n"
        "    labels:\n"
        f'      ne.govern.bundle-hash: "{content_hash}"\n'
        "    volumes:\n"
        f'      - "{staged_dir.resolve()}:/policies:ro"\n'
    )
    overlay_path.write_text(content, encoding="utf-8")
    return overlay_path


def govern_bundle_files(*, root: Path) -> dict[str, dict[str, str]]:
    """The served bundle's files as a Helm-values-safe dict, keyed for a ConfigMap.

    Walks ``packages/ne-govern/policies`` (the ``.manifest`` plus every
    non-test ``*.rego`` file) AND the deployment's OPA data document
    (``ne/config/data.json``, from ``deploy/opa/data``), so the K8s tier serves
    byte-identically to what :func:`stage_govern_policies` mounts on the Compose
    tier. Dropping the data document here would leave K8s resolving
    ``tiers.rego``'s 4 h built-in floor, which refuses nothing (see
    :func:`_policy_data_document`) — it writes a four-hour promise into every
    tier-2 row's ``expires_at`` against a 900 s session and strands the run as
    an opaque ``authorization_expired`` a quarter of an hour later. The two
    tiers would park DIFFERENT windows for the same approval, and only the
    K8s one would be a lie. Keyed by the source-relative POSIX
    path with
    ``/`` replaced by ``__`` — a ConfigMap-data-key-safe charset
    (``[-._a-zA-Z0-9]+``), e.g. ``ne/govern/decision.rego`` becomes
    ``ne__govern__decision.rego``. Each value carries the original
    source-relative ``path`` (for the Deployment's per-file ``subPath``
    mount) and the file's ``content`` verbatim. Fail-closed the same way as
    :func:`stage_govern_policies`.
    """
    source = _govern_policies_source(root)
    data_document = _policy_data_document(root, source)
    files: dict[str, dict[str, str]] = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        if path.name.endswith("_test.rego"):
            continue
        if path.name != ".manifest" and path.suffix != ".rego":
            continue
        rel = path.relative_to(source).as_posix()
        key = rel.replace("/", "__")
        files[key] = {"path": rel, "content": path.read_text(encoding="utf-8")}
    data_rel = POLICY_DATA_DOCUMENT.as_posix()
    files[data_rel.replace("/", "__")] = {
        "path": data_rel,
        "content": data_document.read_text(encoding="utf-8"),
    }
    return files
