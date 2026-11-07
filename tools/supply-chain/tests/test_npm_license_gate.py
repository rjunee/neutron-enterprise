# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Red-team the npm C4 license gate (NE-061), mirroring the Checkpoint-A drill.

These exercise the gate's pure functions against tmp_path node_modules /
package-lock fixtures: a planted AGPL dep must turn the gate red, missing/garbage
licenses fail closed, C11 integrity is enforced, the [npm_known] rescue works but
can never launder the banned class, and the whole gate is driven by the SAME
license-policy.toml classes as the Python gate.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from ne.cli.licenses import BANNED, FREE, UNKNOWN, Policy, PolicyError, classify_license

_GATE_PATH = Path(__file__).resolve().parents[1] / "npm_license_gate.py"
_spec = importlib.util.spec_from_file_location("npm_license_gate", _GATE_PATH)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass introspection (cls.__module__ lookup) works.
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)

REAL_POLICY_PATH = Path(__file__).resolve().parents[1] / "license-policy.toml"


def real_policy() -> Policy:
    return Policy.load(REAL_POLICY_PATH)


def write_project(
    tmp_path: Path,
    packages: dict[str, dict],
    *,
    node_modules: dict[str, dict] | None = None,
    lockfile_version: int = 3,
    with_node_modules: bool = True,
) -> Path:
    """Build a minimal npm project: package-lock.json (+ optional node_modules).

    ``packages`` maps lock ``packages`` keys → lock entries (integrity/license/…).
    ``node_modules`` maps the same keys → the package.json dict to install on disk;
    when omitted for a key, that package's package.json is not written (so the
    gate falls back to the lock entry's inline license).
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    lock = {"lockfileVersion": lockfile_version, "packages": {"": {"name": "root"}, **packages}}
    (proj / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")

    if with_node_modules:
        nm = proj / "node_modules"
        nm.mkdir()
        for rel, pkg_json in (node_modules or {}).items():
            pkg_dir = proj / rel
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    return proj


SHA = "sha512-" + "a" * 88


def entry(license_: str | None = None, integrity: str | None = SHA) -> dict:
    e: dict = {"version": "1.0.0"}
    if integrity is not None:
        e["integrity"] = integrity
    if license_ is not None:
        e["license"] = license_
    return e


# ---------------------------------------------------------------------------
# (h) The gate is driven by the SAME license-policy.toml classes as the Python
# gate (Policy.load over the real policy file).
# ---------------------------------------------------------------------------
def test_uses_same_policy_file_classes() -> None:
    policy = real_policy()
    assert classify_license("MIT", policy) == FREE
    assert classify_license("AGPL-3.0", policy) == BANNED


# ---------------------------------------------------------------------------
# (a) A clean MIT dep classifies FREE and the gate passes.
# ---------------------------------------------------------------------------
def test_mit_dep_passes(tmp_path: Path) -> None:
    proj = write_project(
        tmp_path,
        {"node_modules/left-pad": entry("MIT")},
        node_modules={"node_modules/left-pad": {"name": "left-pad", "license": "MIT"}},
    )
    report = gate.scan_lockfile(proj, real_policy(), {})
    assert report.ok
    assert report.packages[0].classification == FREE


# ---------------------------------------------------------------------------
# (b) A planted AGPL-3.0 dep is BANNED and drives exit 1.
# ---------------------------------------------------------------------------
def test_planted_agpl_is_banned(tmp_path: Path) -> None:
    proj = write_project(
        tmp_path,
        {"node_modules/evil": entry("AGPL-3.0")},
        node_modules={"node_modules/evil": {"name": "evil", "license": "AGPL-3.0"}},
    )
    report = gate.scan_lockfile(proj, real_policy(), {})
    assert not report.ok
    assert report.packages[0].classification == BANNED
    assert gate.main(["--project", str(proj), "--policy", str(REAL_POLICY_PATH)]) == 1


def test_banned_never_rescued_by_npm_known(tmp_path: Path) -> None:
    # Even with an [npm_known] entry, a package whose real license is AGPL stays
    # banned — the rescue only fires for UNKNOWN, never for BANNED.
    proj = write_project(
        tmp_path,
        {"node_modules/evil": entry("AGPL-3.0")},
        node_modules={"node_modules/evil": {"name": "evil", "license": "AGPL-3.0"}},
    )
    report = gate.scan_lockfile(proj, real_policy(), {"evil": "MIT"})
    assert not report.ok
    assert report.packages[0].classification == BANNED


# ---------------------------------------------------------------------------
# (c) Missing / garbage license fields fail closed to UNKNOWN.
# ---------------------------------------------------------------------------
def test_missing_license_is_unknown(tmp_path: Path) -> None:
    proj = write_project(
        tmp_path,
        {"node_modules/mystery": entry(None)},
        node_modules={"node_modules/mystery": {"name": "mystery"}},
    )
    report = gate.scan_lockfile(proj, real_policy(), {})
    assert not report.ok
    assert report.packages[0].classification == UNKNOWN


def test_garbage_license_is_unknown(tmp_path: Path) -> None:
    proj = write_project(
        tmp_path,
        {"node_modules/weird": entry("Made-Up-License-9000")},
        node_modules={"node_modules/weird": {"name": "weird", "license": "Made-Up-License-9000"}},
    )
    report = gate.scan_lockfile(proj, real_policy(), {})
    assert not report.ok
    assert report.packages[0].classification == UNKNOWN


# ---------------------------------------------------------------------------
# (d) Legacy {"type": ...} object and legacy licenses-array forms resolve.
# ---------------------------------------------------------------------------
def test_legacy_object_license_resolves(tmp_path: Path) -> None:
    proj = write_project(
        tmp_path,
        {"node_modules/oldstyle": entry("MIT")},
        node_modules={"node_modules/oldstyle": {"name": "oldstyle", "license": {"type": "MIT"}}},
    )
    report = gate.scan_lockfile(proj, real_policy(), {})
    assert report.ok
    assert report.packages[0].license == "MIT"


def test_legacy_licenses_array_resolves(tmp_path: Path) -> None:
    proj = write_project(
        tmp_path,
        {"node_modules/dual": entry("MIT")},
        node_modules={
            "node_modules/dual": {
                "name": "dual",
                "licenses": [{"type": "MIT"}, {"type": "Apache-2.0"}],
            }
        },
    )
    report = gate.scan_lockfile(proj, real_policy(), {})
    assert report.ok
    # OR-join of two free licenses is still free.
    assert report.packages[0].classification == FREE


def test_normalize_license_field_forms() -> None:
    assert gate.normalize_license_field({"license": "MIT"}) == "MIT"
    assert gate.normalize_license_field({"license": {"type": "ISC"}}) == "ISC"
    assert (
        gate.normalize_license_field({"licenses": [{"type": "MIT"}, {"type": "BSD-3-Clause"}]})
        == "MIT OR BSD-3-Clause"
    )
    assert gate.normalize_license_field({}) is None


# ---------------------------------------------------------------------------
# (e) A lock entry without sha512 integrity is a C11 violation.
# ---------------------------------------------------------------------------
def test_missing_integrity_is_violation(tmp_path: Path) -> None:
    proj = write_project(
        tmp_path,
        {"node_modules/unpinned": entry("MIT", integrity=None)},
        node_modules={"node_modules/unpinned": {"name": "unpinned", "license": "MIT"}},
    )
    report = gate.scan_lockfile(proj, real_policy(), {})
    assert not report.ok
    assert any("integrity" in v for v in report.violations)


def test_non_sha512_integrity_is_violation(tmp_path: Path) -> None:
    proj = write_project(
        tmp_path,
        {"node_modules/weakhash": entry("MIT", integrity="sha1-deadbeef")},
        node_modules={"node_modules/weakhash": {"name": "weakhash", "license": "MIT"}},
    )
    report = gate.scan_lockfile(proj, real_policy(), {})
    assert any("integrity" in v for v in report.violations)


# ---------------------------------------------------------------------------
# (f) [npm_known] rescues a verified UNKNOWN; a BANNED [npm_known] value is a
# PolicyError.
# ---------------------------------------------------------------------------
def test_npm_known_rescues_unknown(tmp_path: Path) -> None:
    proj = write_project(
        tmp_path,
        {"node_modules/oddly-named": entry("3-Clause BSD License")},
        node_modules={
            "node_modules/oddly-named": {
                "name": "oddly-named",
                "license": "3-Clause BSD License",
            }
        },
    )
    # Without the rescue: UNKNOWN.
    assert not gate.scan_lockfile(proj, real_policy(), {}).ok
    # With a verified [npm_known] SPDX: FREE.
    report = gate.scan_lockfile(proj, real_policy(), {"oddly-named": "BSD-3-Clause"})
    assert report.ok
    assert report.packages[0].classification == FREE


def test_banned_npm_known_value_is_policy_error() -> None:
    with pytest.raises(PolicyError):
        gate.validate_npm_known({"sneaky": "AGPL-3.0"}, real_policy())


def test_banned_npm_known_value_fails_scan(tmp_path: Path) -> None:
    proj = write_project(
        tmp_path,
        {"node_modules/ok": entry("MIT")},
        node_modules={"node_modules/ok": {"name": "ok", "license": "MIT"}},
    )
    report = gate.scan_lockfile(proj, real_policy(), {"sneaky": "AGPL-3.0"})
    assert report.policy_errors
    assert not report.ok


# ---------------------------------------------------------------------------
# (g) Absent node_modules is a hard "run npm ci first" error.
# ---------------------------------------------------------------------------
def test_absent_node_modules_hard_error(tmp_path: Path) -> None:
    proj = write_project(
        tmp_path,
        {"node_modules/left-pad": entry("MIT")},
        with_node_modules=False,
    )
    report = gate.scan_lockfile(proj, real_policy(), {})
    assert not report.ok
    assert any("npm ci" in v for v in report.violations)


def test_old_lockfile_version_rejected(tmp_path: Path) -> None:
    proj = write_project(tmp_path, {"node_modules/x": entry("MIT")}, lockfile_version=2)
    report = gate.scan_lockfile(proj, real_policy(), {})
    assert not report.ok
    assert any("lockfileVersion" in v for v in report.violations)


# ---------------------------------------------------------------------------
# Fallback: a platform-gated optional dep with no installed package.json uses
# the license npm recorded inline in the lockfile.
# ---------------------------------------------------------------------------
def test_optional_dep_falls_back_to_lock_license(tmp_path: Path) -> None:
    proj = write_project(
        tmp_path,
        {"node_modules/@scope/win32-binary": entry("MIT")},
        node_modules={},  # not installed on this host
    )
    report = gate.scan_lockfile(proj, real_policy(), {})
    assert report.ok
    assert report.packages[0].name == "@scope/win32-binary"
    assert report.packages[0].classification == FREE


def test_name_from_lock_path() -> None:
    assert gate.name_from_lock_path("node_modules/@scope/pkg") == "@scope/pkg"
    assert gate.name_from_lock_path("node_modules/a/node_modules/b") == "b"
    assert gate.name_from_lock_path("node_modules/plain") == "plain"
