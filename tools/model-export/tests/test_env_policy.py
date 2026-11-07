# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C11 / C4 isolation guardian (MANDATORY, NE-TEI-ONNX orchestrator gate):

* the root ``uv.lock`` carries the new dep-less ``ne-model-export`` member and
  NONE of the heavy sidecar-only packages — the platform lock/SBOM must never
  see torch/optimum/onnxruntime/transformers/sentence-transformers/onnx;
* the sidecar's own hash-pinned lock (``export_env/requirements.txt``) is
  fully ``--hash``-pinned (no unpinned/unhashed transitive line);
* ``export_env/requirements.in`` carries EXACTLY the four orchestrator-
  approved ``==`` pins — no drift, no extra packages.

This test is the permanent trip-wire: it fails loud the moment the heavy
closure leaks into the platform lock, or a pin silently drifts.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
UV_LOCK = REPO_ROOT / "uv.lock"
REQUIREMENTS_IN = Path(__file__).resolve().parents[1] / "export_env" / "requirements.in"
REQUIREMENTS_TXT = Path(__file__).resolve().parents[1] / "export_env" / "requirements.txt"

# NOTE (factual correction vs. the dispatch brief's literal forbidden-set text):
# ``onnxruntime`` is ALREADY a pre-existing, previously-reviewed platform lock
# entry — ``ne-brain``'s ``fastembed`` extra (packages/ne-brain/pyproject.toml,
# NE-CKC9) pulls it in for its lightweight BM25 sparse encoder, unrelated to
# this unit's heavy fp32-export closure and unaffected by it (the sidecar's
# own ``uv pip compile`` is a fully independent resolution graph from the
# platform ``uv.lock`` — pinning both to 1.27.0 is coincidental, both being
# "current latest" at pin time, not a shared resolution). Blanket-forbidding
# it here would be asserting something already false about the committed
# tree pre-this-unit. The genuine C4/C11 isolation intent — MY new sidecar
# closure must not leak in, and ``ne-model-export`` must not itself depend on
# onnxruntime — is enforced precisely below
# (test_onnxruntime_locked_dependents_are_preexisting_and_exclude_ne_model_export)
# rather than via a blanket absence check.
_FORBIDDEN_HEAVY_PACKAGES = {
    "torch",
    "optimum",
    "optimum-onnx",
    "transformers",
    "sentence-transformers",
    "onnx",
}

# The one pre-existing, already-reviewed exception to the forbidden set above
# (see the NOTE): every package in the platform lock that depends on it must
# be drawn from this allowlist — a NEW dependent (e.g. ne-model-export itself)
# would mean the heavy closure leaked in via this unit.
_ONNXRUNTIME_ALLOWED_DEPENDENTS = {"fastembed"}

_APPROVED_PINS = {
    "optimum-onnx": "0.1.0",
    "onnxruntime": "1.27.0",
    "torch": "2.13.0",
    "sentence-transformers": "5.6.0",
}


def _load_uv_lock() -> dict:
    with UV_LOCK.open("rb") as fh:
        return tomllib.load(fh)


def test_uv_lock_registers_ne_model_export_member() -> None:
    lock = _load_uv_lock()
    members = lock["manifest"]["members"]
    assert "ne-model-export" in members, (
        "root uv.lock must register the new dep-less ne-model-export workspace "
        "member (regenerate via `uv lock`)"
    )
    package_names = {pkg["name"] for pkg in lock["package"]}
    assert "ne-model-export" in package_names


def test_ne_model_export_package_depends_only_on_ne_envbuild() -> None:
    lock = _load_uv_lock()
    pkg = next(p for p in lock["package"] if p["name"] == "ne-model-export")
    dep_names = {d["name"] for d in pkg.get("dependencies", [])}
    assert dep_names == {"ne-envbuild"}, (
        f"ne-model-export must depend on ONLY ne-envbuild in the platform lock; got {dep_names}"
    )


def test_uv_lock_never_carries_the_heavy_sidecar_closure() -> None:
    lock = _load_uv_lock()
    package_names = {pkg["name"] for pkg in lock["package"]}
    leaked = _FORBIDDEN_HEAVY_PACKAGES & package_names
    assert not leaked, (
        f"heavy sidecar-only package(s) leaked into the platform uv.lock: {sorted(leaked)} — "
        "these must live ONLY in tools/model-export/export_env/ (C4/C11 isolation)"
    )


def test_onnxruntime_locked_dependents_are_preexisting_and_exclude_ne_model_export() -> None:
    """See the module-level NOTE: onnxruntime itself is a pre-existing, already-
    reviewed platform lock entry (ne-brain's fastembed BM25 encoder). The
    isolation proof this unit owns is narrower and precise: no NEW dependent —
    especially not ne-model-export — pulls it in."""
    lock = _load_uv_lock()
    dependents = {
        pkg["name"]
        for pkg in lock["package"]
        if any(d["name"] == "onnxruntime" for d in pkg.get("dependencies", []))
    }
    assert dependents, "expected onnxruntime to still be locked (via fastembed) — did it drop?"
    unexpected = dependents - _ONNXRUNTIME_ALLOWED_DEPENDENTS
    assert not unexpected, (
        f"unexpected new dependent(s) of onnxruntime in the platform lock: {sorted(unexpected)} "
        "— the heavy fp32-export closure must stay sidecar-only (C4/C11)"
    )
    assert "ne-model-export" not in dependents


def test_requirements_in_carries_exactly_the_four_approved_pins() -> None:
    text = REQUIREMENTS_IN.read_text(encoding="utf-8")
    pin_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    pins: dict[str, str] = {}
    for line in pin_lines:
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.\-]+)$", line)
        assert match, f"requirements.in line is not an exact == pin: {line!r}"
        pins[match.group(1)] = match.group(2)
    assert pins == _APPROVED_PINS, (
        f"requirements.in pins {pins} do not match the orchestrator-approved set "
        f"{_APPROVED_PINS} — do not substitute without flagging"
    )


def test_requirements_txt_is_fully_hash_pinned() -> None:
    assert REQUIREMENTS_TXT.is_file(), (
        "export_env/requirements.txt must be generated via `uv pip compile "
        "... --generate-hashes` (never hand-written) before this guardian can run"
    )
    text = REQUIREMENTS_TXT.read_text(encoding="utf-8")
    blocks = re.split(r"\n(?=[A-Za-z0-9_.\-]+==)", text)
    unhashed: list[str] = []
    for block in blocks:
        first_line = block.splitlines()[0] if block.splitlines() else ""
        if "==" not in first_line:
            continue
        if "--hash=sha256:" not in block:
            unhashed.append(first_line)
    assert not unhashed, f"requirements.txt has unhashed package block(s): {unhashed}"


def test_requirements_txt_was_never_hand_edited_marker_present() -> None:
    text = REQUIREMENTS_TXT.read_text(encoding="utf-8")
    assert "autogenerated" in text.lower()
