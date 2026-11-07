# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Guardian: a test must never mutate a PDP it does not own.

⛔ THIS CLOSES A DEFECT CLASS THAT HAS TAKEN GOVERNANCE DOWN TWICE.

  * 2026-08-30 — ``packages/ne-govern/tests/test_opa_pdp_live.py`` defaulted to
    ``http://localhost:8181`` and skipped only when that was UNREACHABLE. On a box
    running the stack, that port IS the deployed PDP and it is very much reachable,
    so a plain ``pytest packages/ne-govern`` uploaded a working tree into live
    governance: every module held twice, two ``tiers.rego`` versions answering one
    query, HTTP 500 ``eval_conflict_error`` instead of decisions. It was then fitted
    with a CONSENT GATE — an explicit ``NE_GOVERN_OPA_URL``, no default — and that
    gate failed too: the file's own documented recipe named ``localhost:8181``, so
    following the instructions armed it again. It brings its own PDP now.
  * 2026-09-09 — the three live drop-in proofs in THIS package did the same thing:
    ``PUT /v1/policies/<id>`` into ``NE_GOVERN_OPA_URL`` (same default). A run from
    a STALE worktree planted an older ``decision.rego``/``tiers.rego`` beside the
    deployed bundle; both declare ``package ne.govern``, so they merged into one
    namespace rather than being isolated, and 12 ``POST /v1/data/ne/govern/decision``
    calls returned HTTP 500 over ~90 seconds (16:08:30Z → 16:09:58Z). ``opa check``
    passes such a state — a merged-conflict bundle only fails when EVALUATED.

The lesson both incidents teach is the same, and it is not "be careful": REACHABILITY
IS NOT CONSENT, and a documented recipe that names the deployed port is a loaded gun
in every stale worktree on the box. So the rule is structural, and it now has NO
exceptions. A test that needs a real PDP starts its own (``conftest.disposable_opa_url``
here, ``packages/ne-govern/tests/conftest.py::disposable_opa_url`` there — from disk, on
a kernel-assigned loopback port, torn down after) and uploads nothing to anything.

This guardian must contain ZERO skip markers (self-checked below): it needs no OPA,
no docker and no network, so it runs on every CI job and cannot degrade to a green
skip the way the live proofs it guards deliberately do.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# tests/ -> ne-mcp/ -> packages/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]

_TEST_GLOBS = ("packages/*/tests/**/*.py", "tools/*/tests/**/*.py", "e2e/**/*.py")

#: OPA's write APIs. ``/v1/data/ne`` is the data document; ``/v1/policies`` is the
#: policy module store — the one the 2026-09-09 outage was planted through. A POST to
#: ``/v1/data/...`` is how a decision is EVALUATED, so only the mutating verbs count.
_OPA_WRITE_PATHS = ("/v1/policies", "/v1/data/ne")
_MUTATING_VERB_RE = re.compile(r"\.(put|patch|delete)\s*\(")

#: The PDP-address env var every governed component reads.
_PDP_URL_VAR = "NE_GOVERN_OPA_URL"

#: EMPTY, AND IT STAYS EMPTY. ⛔ NOTHING IN THIS REPO MAY UPLOAD POLICY TO A PDP.
#:
#: This held ONE entry for a few hours: ``packages/ne-govern/tests/test_opa_pdp_live.py``,
#: the 2026-08-30 outage's own culprit, kept because it had been fitted with a consent
#: gate (an EXPLICIT ``NE_GOVERN_OPA_URL``, no default, so reachability alone could not
#: arm it). That gate was not enough, and the reason is the whole lesson: the file's own
#: documented recipe was ``NE_GOVERN_OPA_URL=http://localhost:8181``, so FOLLOWING THE
#: INSTRUCTIONS SATISFIED THE CONSENT GATE and aimed the upload straight back at the
#: deployed PDP. A procedure cannot fix a shape. That module now starts its own
#: disposable OPA (``packages/ne-govern/tests/conftest.py::disposable_opa_url``) and
#: uploads nothing, so the allowlist is empty and the rule below is absolute.
#:
#: The assertion is an EXACT set comparison rather than a subset check, so a new
#: uploader FAILS this guardian instead of quietly joining a list. Re-populating this
#: set is not a fix for that failure — starting your own PDP is.
_SANCTIONED_UPLOADERS: set[Path] = set()


def _test_sources() -> list[Path]:
    paths: set[Path] = set()
    for glob in _TEST_GLOBS:
        paths.update(p for p in _REPO_ROOT.glob(glob) if p.is_file())
    paths.discard(Path(__file__).resolve())
    return sorted(paths)


def _mutates_a_pdp(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        source = ast.unparse(node)
        if not _MUTATING_VERB_RE.search(source):
            continue
        if any(path in source for path in _OPA_WRITE_PATHS):
            return True
    return False


def _defaults_the_pdp_address(tree: ast.AST) -> bool:
    """``os.environ.get(NE_GOVERN_OPA_URL, "http://...")`` — the arming default.

    A non-empty fallback is what turns "the operator named a PDP" into "whatever
    answers the deployment's port". Both incidents above were armed by exactly this
    expression, and removing it is what makes the skip honest.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func = node.func
        getter = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if getter not in ("get", "getenv"):
            continue
        name, fallback = node.args[0], node.args[1]
        if not (isinstance(name, ast.Constant) and name.value == _PDP_URL_VAR):
            continue
        if isinstance(fallback, ast.Constant) and fallback.value:
            return True
    return False


def test_no_test_mutates_a_pdp_it_does_not_own() -> None:
    offenders = {
        path.relative_to(_REPO_ROOT)
        for path in _test_sources()
        if _mutates_a_pdp(ast.parse(path.read_text(encoding="utf-8")))
    }
    assert offenders == _SANCTIONED_UPLOADERS, (
        "a test writes to an OPA policy/data API. Tests that need a real PDP must "
        "START ONE (see packages/ne-mcp/tests/conftest.py::disposable_opa_url) — "
        f"unexpected: {sorted(offenders - _SANCTIONED_UPLOADERS)}, "
        f"no longer present: {sorted(_SANCTIONED_UPLOADERS - offenders)}"
    )


def test_no_test_defaults_the_pdp_address() -> None:
    offenders = sorted(
        path.relative_to(_REPO_ROOT)
        for path in _test_sources()
        if _defaults_the_pdp_address(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert offenders == [], (
        f"{_PDP_URL_VAR} must never carry a non-empty default in a test: a default is "
        f"how a test ends up talking to the DEPLOYED PDP. Offenders: {offenders}"
    )


def test_guardian_has_no_skip_markers() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    prefix = "pytest.mark."
    for suffix in ("skip", "skipif", "xfail"):
        marker = prefix + suffix
        assert marker not in source, f"guardian file must not contain {marker}"
