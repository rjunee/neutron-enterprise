# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C4/C11 textual guards on the isolated parser env pin files (NE-020).

Mirrors packages/ne-schema/tests/test_codegen_env_policy.py. The parser env
must be fully ==-pinned and hashed (C11), must pin docling==2.148.0, and the
platform lock (uv.lock, read-only here) must contain NONE of docling's
torch/NVIDIA-CUDA transitive closure (C4 isolation regression guard).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from packaging.version import Version

_PARSER_ENV = Path(__file__).resolve().parents[1] / "parser_env"
_PARSE_DOC = _PARSER_ENV / "parse_doc.py"
_REQUIREMENTS = _PARSER_ENV / "requirements.txt"
_REQUIREMENTS_IN = _PARSER_ENV / "requirements.in"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_UV_LOCK = _REPO_ROOT / "uv.lock"

_ISOLATED_ONLY_NAMES = (
    "docling",
    "docling-core",
    "docling-slim",
    "docling-ibm-models",
    "torch",
)


def _requirement_lines(text: str) -> list[str]:
    """Top-level requirement lines (name==version), ignoring hash/comment/continuation."""
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("--hash"):
            continue
        if raw[:1].isspace():
            continue
        lines.append(stripped)
    return lines


def test_docling_pinned() -> None:
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    assert re.search(r"(?m)^docling==2\.148\.0", text), "docling==2.148.0 must be pinned"
    assert re.search(r"(?m)^docling==2\.148\.0", _REQUIREMENTS_IN.read_text(encoding="utf-8"))


def test_every_requirement_is_pinned_and_hashed() -> None:
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    reqs = _requirement_lines(text)
    assert reqs, "requirements.txt should list distributions"
    for req in reqs:
        spec = req.rstrip("\\").strip()
        assert "==" in spec, f"requirement not ==-pinned: {spec!r}"
    assert text.count("--hash=sha256:") >= len(reqs), "every requirement must be hashed"


def test_security_floor_pins() -> None:
    """Guardian: setuptools/torch must never regress below their CVE-fixed floors.

    setuptools >= 83.0.0 fixes CVE-2026-59890 / GHSA-h35f-9h28-mq5c (MANIFEST.in
    exclusion bypass via Unicode NFC/NFD collision on macOS APFS/HFS+ sdist
    builds). torch >= 2.13.0 fixes CVE-2025-3000 / GHSA-rrmf-rvhw-rf47
    (torch.jit.script local memory corruption); re-landed after the orphaned
    NE-SEC-0718-TORCH dispatch silently failed to update this file. These are
    FLOORS, not exact pins — future recompiles may move versions up freely.
    """
    text = _REQUIREMENTS.read_text(encoding="utf-8")

    setuptools_match = re.search(r"(?m)^setuptools==([0-9][^ \\]*)", text)
    assert setuptools_match is not None, "setuptools must be pinned in requirements.txt"
    assert Version(setuptools_match.group(1)) >= Version("83.0.0"), (
        "setuptools has regressed below the CVE-2026-59890 fixed floor (83.0.0)"
    )

    torch_match = re.search(r"(?m)^torch==([0-9][^ \\]*)", text)
    assert torch_match is not None, "torch must be pinned in requirements.txt"
    assert Version(torch_match.group(1)) >= Version("2.13.0"), (
        "torch has regressed below the CVE-2025-3000 fixed floor (2.13.0)"
    )


def test_repo_root_found() -> None:
    assert _UV_LOCK.is_file(), f"expected uv.lock at {_UV_LOCK}"


def test_platform_lock_has_no_docling_or_torch_or_nvidia_packages() -> None:
    """C4 isolation regression guard: the platform lock must stay clean."""
    lock_text = _UV_LOCK.read_text(encoding="utf-8")
    package_names = re.findall(r'(?m)^name = "([^"]+)"', lock_text)
    lowered = {name.lower() for name in package_names}

    for banned in _ISOLATED_ONLY_NAMES:
        assert banned not in lowered, f"{banned!r} must not appear in the platform uv.lock"

    nvidia_names = [name for name in lowered if name.startswith("nvidia-")]
    assert not nvidia_names, (
        f"nvidia-* packages must not appear in the platform uv.lock: {nvidia_names}"
    )


# -- Parser subprocess never inherits connector network authority --------------
#
# The isolated venv is never built in CI, so the docling weight-fetch fail-closed
# is proven STRUCTURALLY here (live parse stays LIVE-PARKED in
# test_docling_parse_live.py). parse_doc.py runs INSIDE the venv and cannot import
# ne.brain, so this test pins that it unconditionally forces HF_HUB_OFFLINE=1
# BEFORE the first docling import. A non-AIR_GAP connector may access its
# allowlisted source, but that authority never permits an implicit model fetch.


def _first_docling_import_lineno(tree: ast.Module) -> int | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("docling"):
            return node.lineno
    return None


def _hf_hub_offline_lineno(tree: ast.Module) -> int | None:
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "HF_HUB_OFFLINE"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and node.value.value == "1"
        ):
            return node.lineno
    return None


def _telemetry_setdefault_lineno(tree: ast.Module) -> int | None:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "HF_HUB_DISABLE_TELEMETRY"
        ):
            return node.lineno
    return None


def test_parse_doc_forces_hf_hub_offline_unconditionally_before_docling_import() -> None:
    tree = ast.parse(_PARSE_DOC.read_text(encoding="utf-8"))

    docling_lineno = _first_docling_import_lineno(tree)
    assert docling_lineno is not None, "parse_doc.py must import docling"

    offline_lineno = _hf_hub_offline_lineno(tree)
    assert offline_lineno is not None, "parse_doc.py must force HF_HUB_OFFLINE=1"
    assert offline_lineno < docling_lineno, (
        "the unconditional HF_HUB_OFFLINE guard must run BEFORE the first docling import"
    )

    telemetry_lineno = _telemetry_setdefault_lineno(tree)
    assert telemetry_lineno is not None, "the HF_HUB_DISABLE_TELEMETRY setdefault must remain"
    assert telemetry_lineno < docling_lineno, (
        "HF_HUB_DISABLE_TELEMETRY setdefault must remain before the docling import"
    )
