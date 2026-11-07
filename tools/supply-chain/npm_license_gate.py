# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed C4 license gate over the npm dependency closure (NE-061).

The frontend (packages/ne-console) introduces a whole npm transitive closure.
This gate extends SPEC §2 C4 to that closure by REUSING the exact same policy
object as the Python gate: it imports ``ne.cli.licenses`` (Policy / classify_
license) and reads the SAME ``tools/supply-chain/license-policy.toml`` classes.
There is no second, weaker license policy — npm and Python deps are judged by
one classifier and one class file.

Fail-closed throughout (mirrors ``ne check licenses``):
  * ``package-lock.json`` must be lockfileVersion >= 3.
  * Every non-root, non-link lock entry MUST carry a sha512 ``integrity`` (C11
    pin policy) — a missing hash is a violation.
  * ``node_modules`` must be installed (run ``npm ci`` first) — its absence is a
    hard error, not a silent pass.
  * Each package's license is read from its installed
    ``node_modules/<path>/package.json`` (string form; legacy ``{type}`` object;
    legacy ``licenses`` array → OR-join). Platform-gated optional deps that are
    not installed on this host fall back to the license npm recorded inline in
    the lockfile; if neither resolves, the license is UNKNOWN (fail closed).
  * The whole tree is scanned, devDependencies included.

The optional ``[npm_known]`` table in the policy file (name → human-verified
SPDX) rescues a package whose resolved license classifies UNKNOWN — identical in
spirit to the Python gate's ``[known]`` map. It is NEVER an escape hatch for the
banned class: an ``[npm_known]`` value that itself classifies BANNED is a
PolicyError, and any resolved BANNED license fails regardless of the table.
"""

from __future__ import annotations

import argparse
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ne.cli.licenses import (
    BANNED,
    FREE,
    UNKNOWN,
    Policy,
    PolicyError,
    canonical_name,
    classify_license,
)

_LOCKFILE_MIN_VERSION = 3


def load_npm_known(policy_path: Path) -> dict[str, str]:
    """Read the optional ``[npm_known]`` (name → verified SPDX) map.

    Keys are PEP 503-canonicalized so lookups are name-normalization tolerant,
    matching the Python gate's ``[known]`` handling. Absent table → empty map.
    """
    data = tomllib.loads(policy_path.read_text(encoding="utf-8"))
    table = data.get("npm_known", {})
    return {canonical_name(str(k)): str(v) for k, v in table.items()}


def validate_npm_known(npm_known: dict[str, str], policy: Policy) -> None:
    """Raise :class:`PolicyError` if any ``[npm_known]`` value classifies BANNED.

    The banned class is structurally non-overridable — an ``[npm_known]`` entry
    can never launder an AGPL/SSPL/etc. license into an accepted one.
    """
    for name, spdx in npm_known.items():
        if classify_license(spdx, policy) == BANNED:
            raise PolicyError(
                f"[npm_known] {name} = {spdx!r} classifies BANNED — "
                "the banned class is never overridable"
            )


def name_from_lock_path(lock_path: str) -> str:
    """Derive the package name from a package-lock ``packages`` key.

    e.g. ``node_modules/@scope/pkg`` → ``@scope/pkg`` and a nested
    ``node_modules/a/node_modules/b`` → ``b`` (the deepest install).
    """
    marker = "node_modules/"
    idx = lock_path.rfind(marker)
    if idx == -1:
        return lock_path
    return lock_path[idx + len(marker) :]


def normalize_license_field(pkg_json: dict) -> str | None:
    """Resolve a package.json ``license``/``licenses`` field to a string.

    Handles the modern string form, the legacy ``{"type": "..."}`` object, and
    the legacy ``licenses`` array (OR-joined types). Returns None when no license
    information is present or parseable (→ UNKNOWN downstream, fail closed).
    """
    lic = pkg_json.get("license")
    if isinstance(lic, str) and lic.strip():
        return lic.strip()
    if isinstance(lic, dict):
        t = lic.get("type")
        if isinstance(t, str) and t.strip():
            return t.strip()
    legacy = pkg_json.get("licenses")
    if isinstance(legacy, list):
        types = [
            str(e.get("type")).strip()
            for e in legacy
            if isinstance(e, dict) and str(e.get("type", "")).strip()
        ]
        if types:
            return " OR ".join(types)
    return None


def resolve_npm_license(
    node_modules: Path,
    lock_rel_path: str,
    lock_entry: dict,
) -> str | None:
    """Resolve a package's license string, or None if unresolvable.

    Primary source is the installed ``node_modules/<path>/package.json``. When
    that file is absent (a platform-gated optional dep not installed on this
    host), fall back to the license npm recorded inline in the lock entry. The
    caller has already asserted the top-level ``node_modules`` directory exists.
    """
    pkg_json_path = node_modules.parent / lock_rel_path / "package.json"
    if pkg_json_path.is_file():
        try:
            pkg_json = json.loads(pkg_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return normalize_license_field(pkg_json)
    # Not installed here (optional/os/cpu-gated) → use npm's recorded license.
    inline = lock_entry.get("license")
    if isinstance(inline, str) and inline.strip():
        return inline.strip()
    if isinstance(inline, dict):
        t = inline.get("type")
        if isinstance(t, str) and t.strip():
            return t.strip()
    return None


@dataclass
class NpmPackageResult:
    name: str
    version: str
    license: str
    classification: str
    allowed: bool
    reason: str


@dataclass
class NpmScanReport:
    packages: list[NpmPackageResult] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    policy_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations and not self.policy_errors


def resolve_with_known(
    resolved: str | None, name: str, npm_known: dict[str, str], policy: Policy
) -> str:
    """Apply the ``[npm_known]`` rescue, mirroring ne.cli.licenses.resolve_license.

    The rescue fires ONLY when the resolved string is missing or classifies
    UNKNOWN — never for BANNED / *_NEEDS_APPROVAL outcomes (those keep their
    stricter discipline). A BANNED resolved license is returned unchanged so it
    still fails downstream.
    """
    if resolved is None:
        return npm_known.get(canonical_name(name), "UNKNOWN")
    if classify_license(resolved, policy) == UNKNOWN:
        return npm_known.get(canonical_name(name), resolved)
    return resolved


def scan_lockfile(
    project_dir: Path,
    policy: Policy,
    npm_known: dict[str, str],
) -> NpmScanReport:
    """Scan a project's package-lock.json + node_modules against the C4 policy."""
    report = NpmScanReport()
    lock_path = project_dir / "package-lock.json"
    if not lock_path.is_file():
        report.violations.append(f"{lock_path}: package-lock.json not found")
        return report

    data = json.loads(lock_path.read_text(encoding="utf-8"))
    lockfile_version = data.get("lockfileVersion", 0)
    if not isinstance(lockfile_version, int) or lockfile_version < _LOCKFILE_MIN_VERSION:
        report.violations.append(
            f"package-lock.json lockfileVersion {lockfile_version!r} < "
            f"{_LOCKFILE_MIN_VERSION} — regenerate with a modern npm (fail closed)"
        )
        return report

    node_modules = project_dir / "node_modules"
    if not node_modules.is_dir():
        report.violations.append(
            f"{node_modules}: node_modules absent — run `npm ci` first (fail closed)"
        )
        return report

    # Pre-validate any [npm_known] value: a banned override is a structural fault.
    try:
        validate_npm_known(npm_known, policy)
    except PolicyError as exc:
        report.policy_errors.append(str(exc))

    packages: dict[str, dict] = data.get("packages", {})
    for lock_rel_path, entry in sorted(packages.items()):
        if lock_rel_path == "":
            continue  # the first-party project root; nothing to verify here
        if entry.get("link"):
            continue  # workspace symlink, resolved elsewhere

        name = name_from_lock_path(lock_rel_path)
        version = str(entry.get("version", ""))

        # C11: every registry entry must be integrity-pinned (sha512).
        integrity = entry.get("integrity")
        if not (isinstance(integrity, str) and integrity.startswith("sha512-")):
            report.violations.append(
                f"{name} {version}: missing sha512 integrity in package-lock.json (C11)"
            )

        resolved = resolve_npm_license(node_modules, lock_rel_path, entry)
        license_str = resolve_with_known(resolved, name, npm_known, policy)
        classification = classify_license(license_str, policy)

        allowed, reason = _classify_outcome(classification)
        report.packages.append(
            NpmPackageResult(name, version, license_str, classification, allowed, reason)
        )
        if not allowed:
            report.violations.append(f"{name} {version}: {license_str} — {reason}")

    return report


def _classify_outcome(classification: str) -> tuple[bool, str]:
    if classification == FREE:
        return True, "free-to-embed"
    if classification == BANNED:
        return False, "BANNED class (non-overridable)"
    if classification == UNKNOWN:
        return False, "UNKNOWN/unclassifiable license (fail closed)"
    # LGPL/GPL/Elastic needs-approval: npm deps are bundled in-process; no
    # per-package override mechanism is offered for the npm tree — fail closed.
    return False, f"{classification} — needs C4 review (fail closed)"


def render(report: NpmScanReport) -> None:
    by_class: dict[str, int] = {}
    for p in report.packages:
        by_class[p.classification] = by_class.get(p.classification, 0) + 1
    print(f"npm C4 license gate — {len(report.packages)} packages scanned")
    for cls in sorted(by_class):
        print(f"  {cls:<12} {by_class[cls]}")
    if report.policy_errors:
        print("POLICY ERRORS:")
        for e in report.policy_errors:
            print(f"  x {e}")
    if report.violations:
        print("LICENSE VIOLATIONS:")
        for v in report.violations:
            print(f"  x {v}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="npm C4 license gate (fail-closed).")
    parser.add_argument(
        "--project",
        type=Path,
        required=True,
        help="Path to the npm project (containing package-lock.json + node_modules).",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path(__file__).resolve().parent / "license-policy.toml",
        help="Path to license-policy.toml (defaults to the supply-chain policy).",
    )
    args = parser.parse_args(argv)

    policy = Policy.load(args.policy)
    npm_known = load_npm_known(args.policy)
    report = scan_lockfile(args.project, policy, npm_known)
    render(report)

    if report.ok:
        print(f"PASS — {len(report.packages)} npm packages, all within C4 policy.")
        return 0
    print(
        f"FAIL — {len(report.violations)} violation(s), "
        f"{len(report.policy_errors)} policy error(s)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
