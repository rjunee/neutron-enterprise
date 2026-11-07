# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C4 license-gate RECOGNITION proof for NE-021's qdrant-client + fastembed closure.

Per SPEC §11 decision (2026-07-03): the qdrant-client + fastembed dependency
closure fails `ne check licenses` today even though every license in it is
genuinely free — the gate did not RECOGNIZE non-SPDX-but-permissive license
strings. NE-CKB1 fixes recognition (the `[known]` map now also rescues
resolved-but-UNKNOWN license strings, and CC0-1.0/MIT-CMU were added to the
free list). This test proves — against the REAL committed
tools/supply-chain/license-policy.toml, with no monkeypatching of the policy
— that the fixture closure now scans clean, so NE-021 can add qdrant-client
and fastembed to uv.lock with confidence the gate will pass.

NE-021's continuation has now actually installed the real closure
(qdrant-client, fastembed, and their transitive deps) into this environment.
The version-binding control in ne.cli.licenses.scan fails closed when a
uv.lock pin disagrees with the version importlib.metadata reports as
installed ("license verified against wrong version"). To satisfy — not
bypass — that control, the synthetic fixture lock below reads each
package's REAL installed version via importlib.metadata at test runtime
and pins that, so honestly-resolved dists agree with the lock the way a
real `uv lock` output would. Packages that are not installed in this
environment (pywin32/win32-setctime, win32-gated) keep a fixed fallback
version; the binding check has nothing to compare against for them
(installed_ver is None), so they still exercise the absent-metadata
[known] path.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import metadata as _im
from pathlib import Path

from ne.cli.licenses import BANNED, FREE, Policy, default_policy_path, scan

REPO_ROOT = Path(__file__).resolve().parents[3]


def _installed_version(name: str, default: str = "1.0.0") -> str:
    """Return the REAL installed version of `name`, or `default` if absent.

    Never hardcode a specific version literal here (e.g. 0.8.0, 1.18.0) —
    the whole point is that this fixture tracks whatever is actually
    installed, so it stays correct as dependency versions move. The
    `default` fallback only matters for dists that are genuinely absent
    from this environment (e.g. win32-gated packages on non-Windows), where
    the version-binding check has no installed version to compare against
    and skips straight to the [known] resolution path.
    """
    try:
        return _im.version(name)
    except _im.PackageNotFoundError:
        return default


def real_policy() -> Policy:
    return Policy.load(default_policy_path(REPO_ROOT))


def resolver_from(mapping: dict[str, str]) -> Callable[[str], str | None]:
    return lambda name: mapping.get(name)


def _write_lock(tmp_path: Path, names: list[str]) -> Path:
    """A minimal synthetic uv.lock with one third-party registry entry per name."""
    lines = ["version = 1", 'requires-python = ">=3.12"', ""]
    for name in names:
        lines.append("[[package]]")
        lines.append(f'name = "{name}"')
        lines.append(f'version = "{_installed_version(name)}"')
        lines.append('source = { registry = "https://pypi.org/simple" }')
        lines.append(
            f'wheels = [{{ url = "https://example/{name}.whl", hash = "sha256:deadbeef" }}]'
        )
        lines.append("")
    path = tmp_path / "uv.lock"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# The transitive closure of qdrant-client + fastembed that failed the gate
# before NE-CKB1's recognition fix, with each dist's ACTUAL resolved license
# string as of 2026-07-03 (see license-policy.toml's [known] comments for
# citations). NE-021 has since installed this closure for real: qdrant-client,
# fastembed, protobuf, numpy, and pillow ARE now in uv.lock and installed
# (their fixture versions above come from importlib.metadata, so the
# version-binding check passes honestly rather than being bypassed).
# py-rust-stemmers is also installed but is deliberately kept OUT of
# _CLOSURE_RESOLVER_MAPPING below so its resolution still falls through to
# the [known] fallback path (exercising that path the same way colorama
# does). pywin32/win32-setctime remain win32-gated and genuinely absent from
# this (non-Windows) environment, so they exercise the absent-metadata
# [known] path via the `default` branch of `_installed_version`.
_CLOSURE_NAMES = [
    "qdrant-client",
    "fastembed",
    "protobuf",
    "py-rust-stemmers",
    "pywin32",
    "win32-setctime",
    "numpy",
    "pillow",
]

_CLOSURE_RESOLVER_MAPPING = {
    "qdrant-client": "Apache-2.0",
    "fastembed": "Apache License",
    "protobuf": "3-Clause BSD License",
    "numpy": "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
    "pillow": "MIT-CMU",
}


def test_qdrant_fastembed_closure_passes_gate(tmp_path: Path) -> None:
    lock = _write_lock(tmp_path, _CLOSURE_NAMES)
    resolver = resolver_from(_CLOSURE_RESOLVER_MAPPING)
    report = scan(lock, real_policy(), resolver=resolver)
    assert report.ok, report.violations + report.policy_errors
    assert not report.violations
    assert not report.policy_errors
    for pkg in report.packages:
        assert pkg.classification == FREE, f"{pkg.name}: {pkg.classification} ({pkg.license})"


def test_planted_agpl_in_closure_still_rejected(tmp_path: Path) -> None:
    """Negative control: the harness is not a rubber stamp — a planted BANNED
    dep in the SAME closure still fails the gate."""
    names = [*_CLOSURE_NAMES, "pymupdf"]
    lock = _write_lock(tmp_path, names)
    mapping = dict(_CLOSURE_RESOLVER_MAPPING)
    mapping["pymupdf"] = "AGPL-3.0"
    report = scan(lock, real_policy(), resolver=resolver_from(mapping))
    assert not report.ok
    pkg = next(p for p in report.packages if p.name == "pymupdf")
    assert pkg.classification == BANNED
    assert any("pymupdf" in v and "BANNED" in v for v in report.violations)


def test_policy_file_contains_recognition_entries() -> None:
    policy = real_policy()
    assert "CC0-1.0" in policy.free
    assert "MIT-CMU" in policy.free
    expected_known = {
        "protobuf": "BSD-3-Clause",
        "fastembed": "Apache-2.0",
        "py-rust-stemmers": "MIT",
        "pywin32": "PSF-2.0",
        "win32-setctime": "MIT",
    }
    for name, spdx in expected_known.items():
        assert policy.known.get(name) == spdx, (name, policy.known.get(name))
