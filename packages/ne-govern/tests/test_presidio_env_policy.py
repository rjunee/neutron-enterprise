# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C4/C11 textual guards on the isolated presidio env pins (NE-032, SPEC §11).

Mirrors packages/ne-brain/tests/test_parser_env_policy.py. The presidio env must
be fully pinned (== or an exact ``@ <url>``) and hashed (C11), must pin both
presidio distributions, and the platform lock (uv.lock, read-only) must contain
NONE of presidio's spaCy/en_core_web_lg closure (C4 isolation regression guard).
"""

from __future__ import annotations

import re
from pathlib import Path

_PRESIDIO_ENV = Path(__file__).resolve().parents[1] / "presidio_env"
_REQUIREMENTS = _PRESIDIO_ENV / "requirements.txt"
_REQUIREMENTS_IN = _PRESIDIO_ENV / "requirements.in"
_OVERRIDES = _PRESIDIO_ENV / "overrides.txt"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_UV_LOCK = _REPO_ROOT / "uv.lock"

# The presidio/spaCy closure that must NEVER leak into the platform lock.
_ISOLATED_ONLY_NAMES = (
    "presidio-analyzer",
    "presidio-anonymizer",
    "spacy",
    "thinc",
    "en-core-web-lg",
)


def _requirement_lines(text: str) -> list[str]:
    """Top-level requirement lines, ignoring hash/comment/continuation lines."""
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("--hash"):
            continue
        if raw[:1].isspace():
            continue
        lines.append(stripped)
    return lines


def test_repo_root_found() -> None:
    assert _UV_LOCK.is_file(), f"expected uv.lock at {_UV_LOCK}"


def test_presidio_pinned_in_both_files() -> None:
    for path in (_REQUIREMENTS, _REQUIREMENTS_IN):
        text = path.read_text(encoding="utf-8")
        assert re.search(r"(?m)^presidio-analyzer==2\.2\.363", text), path
        assert re.search(r"(?m)^presidio-anonymizer==2\.2\.363", text), path


def test_every_requirement_is_pinned_and_hashed() -> None:
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    reqs = _requirement_lines(text)
    assert reqs, "requirements.txt should list distributions"
    for req in reqs:
        spec = req.rstrip("\\").strip()
        # Either a ==-pin or an exact ``name @ https://.../<pinned-wheel>`` line.
        assert "==" in spec or " @ " in spec, f"requirement not exactly pinned: {spec!r}"
    assert text.count("--hash=sha256:") >= len(reqs), "every requirement must be hashed"


def test_overrides_file_pins_cryptography_ghsa_floor() -> None:
    # The compile-time override lifts presidio-anonymizer 2.2.363's stale
    # cryptography<47 ceiling so the recompile can take the security fixes that
    # land only above it: GHSA-537c-gmf6-5ccf (>=48.0.1) and CVE-2026-69247
    # (PKCS#7 EnvelopedData Bleichenbacher oracle, >=50.0.0). The override
    # artifact must exist and carry exactly the higher floor so it can't be
    # silently deleted or lowered (which would let a naive recompile re-resolve
    # a vulnerable line).
    assert _OVERRIDES.is_file(), f"expected overrides.txt at {_OVERRIDES}"
    text = _OVERRIDES.read_text(encoding="utf-8")
    assert re.search(r"(?m)^cryptography>=50\.0\.0\s*$", text), _OVERRIDES


def test_compiled_cryptography_pin_remediates_ghsa() -> None:
    # Regression guard: a naive recompile without --override would silently
    # resolve cryptography 46.x again (presidio-anonymizer's declared ceiling),
    # reintroducing GHSA-537c-gmf6-5ccf — and anything below 50.0.0
    # reintroduces the CVE-2026-69247 padding oracle. Assert the compiled pin
    # took the fix.
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    m = re.search(r"(?m)^cryptography==(\d+)\.(\d+)\.(\d+)", text)
    assert m, "cryptography must be ==-pinned in requirements.txt"
    assert tuple(map(int, m.groups())) >= (50, 0, 0), (
        "cryptography pin must be >=50.0.0 to remediate CVE-2026-69247 "
        "(and GHSA-537c-gmf6-5ccf, fixed >=48.0.1)"
    )


def test_platform_lock_excludes_presidio_and_spacy_closure() -> None:
    lock_text = _UV_LOCK.read_text(encoding="utf-8")
    package_names = re.findall(r'(?m)^name = "([^"]+)"', lock_text)
    lowered = {name.lower() for name in package_names}
    for banned in _ISOLATED_ONLY_NAMES:
        assert banned not in lowered, f"{banned!r} must not appear in the platform uv.lock"
