# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tier-parity guardian — tiers.rego (authority) vs actions.py mirror MUST agree.

NE-CKD3 (Checkpoint-D finding #3): the Python mirror ``tier_for_action`` is a
registration-time defense-in-depth floor. If it silently drifts BELOW the Rego
policy, an escalation could be under-tiered. This guardian cross-evaluates the
REAL ``tiers.rego`` (``opa eval data.ne.govern.class_tier``) against the mirror
over a static probe corpus PLUS probes derived from the Rego source literals, and
proves — via an injected-divergence self-test — that it actually fires.

LIVE-PARKABLE: the build host has no ``opa`` binary, so this module skips there.
CI installs a SHA256-pinned opa 1.18.2 (see .github/workflows/ci.yml), so the
guardian executes on every PR — enforced must-run-not-skip by a dedicated CI step.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ne.runtime.actions import tier_for_action
from ne.runtime.errors import ToolRegistrationError

_OPA = shutil.which("opa")

if _OPA is None:
    pytest.skip(
        "LIVE-PARKABLE: opa binary not on PATH — install OPA to cross-check the "
        "tiers.rego↔actions.py mirror (`opa eval data.ne.govern.class_tier`).",
        allow_module_level=True,
    )

# Narrowed for the type checker; unreachable when the module skips above.
_OPA_BIN: str = _OPA or "opa"
_POLICIES = Path(__file__).parents[2] / "ne-govern" / "policies"
_TIERS_REGO = _POLICIES / "ne" / "govern" / "tiers.rego"

# A fixed corpus spanning every class, multi-match, and unclassified-on-both-sides.
STATIC_PROBES = [
    "read.docs.search",
    "draft.reply",
    "write.internal.notes.append",
    "write.external.email.send",
    "write.internal.pii.export",  # multi-match 1+2 -> 2
    "report.pii.summary",
    "finance.payment.initiate",
    "legal.contract.sign",
    "legal.pii.review",  # 3+2 -> 3
    "draft.finance.memo",  # draft. prefix -> 0
    "financex.other",  # NOT finance. -> unclassified
    "readx.other",  # NOT read. -> unclassified
    "mystery.action",  # unclassified
]


def _rego_class_tier(policies_dir: Path, action: str) -> int | None:
    """The Rego authority's class tier for ``action`` (None when unclassified)."""
    result = subprocess.run(
        [
            _OPA_BIN,
            "eval",
            "-d",
            str(policies_dir),
            "--stdin-input",
            "--format=json",
            "data.ne.govern.class_tier",
        ],
        input=json.dumps({"action": action}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    # class_tier is undefined when candidate_tiers is empty (max([]) is undefined
    # in Rego 1.x); opa eval then omits "result".
    entries = body.get("result")
    if not entries:
        return None
    return entries[0]["expressions"][0]["value"]


def _python_class_tier(action: str) -> int | None:
    """The Python mirror's class tier (None when unclassified — deny-by-default)."""
    try:
        return int(tier_for_action(action))
    except ToolRegistrationError:
        return None


def _derived_probes(rego_text: str) -> list[str]:
    """Probes derived from every classification literal in the Rego source.

    Guards regex rot: if ``tiers.rego`` syntax changes so extraction finds nothing,
    the coverage self-test fails instead of silently probing an empty corpus.
    """
    probes: list[str] = []
    for lit in re.findall(r'startswith\(input\.action,\s*"([^"]+)"\)', rego_text):
        probes.append(f"{lit}parity-probe")
    for lit in re.findall(r'contains\(input\.action,\s*"([^"]+)"\)', rego_text):
        probes.append(f"probe{lit}parity")
    return probes


def _collect_divergences(
    policies_dir: Path, actions: list[str]
) -> list[tuple[str, int | None, int | None]]:
    divergences: list[tuple[str, int | None, int | None]] = []
    for action in actions:
        rego = _rego_class_tier(policies_dir, action)
        py = _python_class_tier(action)
        if rego != py:
            divergences.append((action, rego, py))
    return divergences


def test_static_probe_corpus_parity() -> None:
    assert _collect_divergences(_POLICIES, STATIC_PROBES) == []


def test_derived_probes_cover_every_rego_literal() -> None:
    derived = _derived_probes(_TIERS_REGO.read_text(encoding="utf-8"))
    # 6 startswith prefixes + 1 contains substring in the shipped tiers.rego.
    assert len(derived) >= 7


def test_derived_probe_parity() -> None:
    derived = _derived_probes(_TIERS_REGO.read_text(encoding="utf-8"))
    assert _collect_divergences(_POLICIES, derived) == []


def test_guardian_detects_injected_divergence(tmp_path: Path) -> None:
    # Copy the real bundle and inject a policy escalation (read.* ⇒ tier 3) that
    # the Python mirror does NOT have. The guardian must DETECT it.
    dst = tmp_path / "policies"
    shutil.copytree(_POLICIES, dst)
    injected = dst / "ne" / "govern" / "tiers.rego"
    injected.write_text(
        injected.read_text(encoding="utf-8")
        + '\ncandidate_tiers contains 3 if startswith(input.action, "read.")\n',
        encoding="utf-8",
    )
    divergences = _collect_divergences(dst, STATIC_PROBES)
    assert divergences, "guardian failed to detect an injected rego escalation"
    read_hits = [d for d in divergences if d[0].startswith("read.")]
    assert read_hits, divergences
    # rego now says 3 for read.*, python mirror still says 0.
    assert any(rego == 3 and py == 0 for _, rego, py in read_hits)
