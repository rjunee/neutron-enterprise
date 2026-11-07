# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Policy-bundle builder + version-consistency ratchet."""

from __future__ import annotations

import re
import tarfile
from pathlib import Path

import pytest

import ne.govern.cedar
from ne.govern import build_bundle, read_manifest_revision, read_policy_version
from ne.govern.errors import BundleError

_POLICIES = Path(__file__).parents[1] / "policies"


def test_build_bundle_includes_policies_and_excludes_tests(tmp_path: Path) -> None:
    out = build_bundle(_POLICIES, tmp_path / "bundle.tar.gz")
    with tarfile.open(out, "r:gz") as tar:
        names = set(tar.getnames())
    assert names == {
        ".manifest",
        "ne/govern/decision.rego",
        "ne/govern/subagent.rego",
        "ne/govern/tiers.rego",
        "ne/govern/version.rego",
    }
    assert "ne/govern/decision_test.rego" not in names
    assert "ne/govern/subagent_test.rego" not in names


def test_manifest_revision_matches_policy_version() -> None:
    assert read_manifest_revision(_POLICIES) == read_policy_version(_POLICIES)


def test_read_manifest_revision_missing_manifest_raises(tmp_path: Path) -> None:
    with pytest.raises(BundleError):
        read_manifest_revision(tmp_path)


def test_build_bundle_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(BundleError):
        build_bundle(tmp_path / "nope", tmp_path / "out.tar.gz")


# ---------------------------------------------------------------------------
# C12 — the decision `reason` is END-USER-VISIBLE COPY, so it must stay a constant
# ---------------------------------------------------------------------------
# `ne.api.tool_transit` serves `detail["reason"]` from a pdp.decide DENY onto the
# chat SSE stream, and `ne-console` renders it verbatim in the Ask screen's gate
# card, to the REQUESTING END USER — not to an operator. That is only safe while
# every reason this bundle can emit is a fixed sentence: an interpolated one could
# carry a rule id, a principal attribute, a resource path or an echo of the input,
# which is exactly the leak C12 forbids through a governance surface and C7's
# entitlement rule stops the requester seeing.
#
# This is a claim about the BUNDLE, not about a call site, so it is checked here
# rather than argued in a docstring. It pins the SHAPE (a literal), not the
# wording — rewording a sentence is a copy change and stays free.

_REASON_ASSIGNMENT = re.compile(r'^\s*"reason":\s*(?P<value>.+?),?\s*$', re.MULTILINE)
_CEDAR_REASON = re.compile(r"^\s*@ne_reason\((?P<value>.+)\)\s*$", re.MULTILINE)
#: A complete double-quoted Rego/Cedar string literal and nothing else.
_STRING_LITERAL = re.compile(r'^"(?:[^"\\]|\\.)*"$')


def _assert_all_literals(values: list[str], *, where: str, at_least: int) -> None:
    assert len(values) >= at_least, f"{where}: found {len(values)} reasons, expected >= {at_least}"
    for value in values:
        assert _STRING_LITERAL.fullmatch(value), (
            f"{where}: reason {value!r} is not a plain string literal. A computed reason "
            "reaches the requesting end user's gate card verbatim (ne-api tool_transit -> "
            "ne-console MessageList) and may carry rule ids, principal attributes, resource "
            "paths or an echo of the input — C12/C7. Serve a fixed sentence, or narrow the "
            "console to a reason CLASS before making this one dynamic."
        )


def test_every_rego_decision_reason_is_a_fixed_literal() -> None:
    source = (_POLICIES / "ne" / "govern" / "decision.rego").read_text(encoding="utf-8")
    _assert_all_literals(
        [m.group("value") for m in _REASON_ASSIGNMENT.finditer(source)],
        where="decision.rego",
        at_least=9,
    )


def test_every_cedar_decision_reason_is_a_fixed_literal() -> None:
    # The second engine of the same deployment (cedar/evaluator.py reads these
    # annotations into the SAME decision document), so it owes the same property.
    source = (Path(ne.govern.cedar.__file__).parent / "decision.cedar").read_text(encoding="utf-8")
    _assert_all_literals(
        [m.group("value") for m in _CEDAR_REASON.finditer(source)],
        where="decision.cedar",
        at_least=8,
    )


def test_the_literal_guard_rejects_an_interpolated_reason() -> None:
    """The guard's own control: it must FAIL on the shapes it exists to catch, and
    PASS on a legitimate rewording. Without this, a regex that matched nothing would
    certify the bundle by accident."""
    for computed in ('sprintf("denied: %v", [input.action])', 'concat(" ", parts)', "msg"):
        with pytest.raises(AssertionError):
            _assert_all_literals([computed], where="control", at_least=1)
    # A reworded literal — including one carrying the bundle's em dash and quotes —
    # is still a literal and still passes.
    _assert_all_literals(
        ['"tier 4: a brand new class — reworded freely"', '"a \\"quoted\\" clause"'],
        where="control",
        at_least=2,
    )
    # …and an EMPTY extraction is a failure, not a pass (the vacuity guard).
    with pytest.raises(AssertionError):
        _assert_all_literals([], where="control", at_least=1)
