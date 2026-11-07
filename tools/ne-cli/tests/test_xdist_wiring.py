# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-TEST-XDIST wiring guard.

Locks three things the unit's SPEC row promises and nothing else may drift:

1. The CI full-suite step (the one literally named ``Test`` in ci.yml) runs
   pytest-xdist with ``-n auto --dist loadgroup``.
2. **Every** step that ``tee``s its pytest output through a
   ``grep -Eq '[0-9]+ passed'`` / skip-detecting parser stays serial — xdist
   changes pytest's summary-line shape and would corrupt that fail-closed
   parsing (NE-CKC6-adjacent). That set is **derived from ci.yml, not
   enumerated**: the first version of this guard named five gates and the
   repo has eleven across five jobs, so the six live-Postgres ones were
   unprotected. A hand-maintained list is silent about precisely the case
   being guarded — a ``-n`` added to a gate that did not exist when the list
   was written.
3. Root ``pyproject.toml`` never gains ``-n`` in
   ``[tool.pytest.ini_options].addopts`` — the SPEC-row NON-goal. A global
   default would add worker-startup cost to every single-test invocation and
   change output shape for every tool that parses bare ``uv run pytest``.

Each assertion carries a positive control so a vacuously-true check (e.g. an
empty gate-step list, or a key that silently doesn't exist) can't pass by
matching nothing. For (2) the control is a **cardinality floor**, which is the
only job the enumeration still has: a derived detector that silently matches
nothing would make every downstream assertion trivially true.

Bidirectional mutation, run and observed rather than asserted:

===  ==============================================================  ======
m    mutation                                                        result
===  ==============================================================  ======
m1   add ``-n 4`` to the ``pgAudit live proofs`` gate — one of the    RED
     six the earlier enumerated list did not cover                    (:111)
m2   make ``_is_output_parsing_gate`` match nothing                   RED
                                                                      (:105)
m3   unmutated control                                                GREEN
===  ==============================================================  ======

m1 and m2 fail at **different lines**, which is the point: it shows which
assertion is the guard and which is the control, rather than one broken thing
producing one red and being mistaken for coverage.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"

# Every step that tees pytest output through a `grep -Eq '[0-9]+ passed'`-style
# fail-closed parser must stay serial: xdist's worker-prefixed, differently
# ordered output would corrupt that parsing.
#
# This set is DERIVED from ci.yml, not enumerated. An enumerated list only
# protects the gates that existed when it was written — and this repo already
# has eleven such steps across five jobs (`check`, `upgrade`, `live-postgres`,
# `live-postgres-pooled`), while the first version of this guard named five.
# The six it missed are the live-Postgres suites, which do not currently run
# through the xdist'd `Test` step but are one CI edit away from doing so.
# Deriving the set means a NEW gate is covered the day it is added, by the
# same person who added it, without anyone remembering this file exists.
#
# The floor below is the enumeration's only surviving job: it makes a detector
# that silently matches nothing go red instead of green.
_PYTEST_OUTPUT_PARSER_MARKERS = ("passed", "skipp")
MIN_EXPECTED_SERIAL_GATES = 11


def _is_output_parsing_gate(run: str) -> bool:
    """A step that runs pytest AND parses its summary line to decide pass/fail."""
    if "pytest" not in run:
        return False
    if "grep" not in run:
        return False
    return any(marker in run for marker in _PYTEST_OUTPUT_PARSER_MARKERS)


def _load_workflow() -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _all_steps(workflow: dict) -> list[dict]:
    steps: list[dict] = []
    for job in workflow["jobs"].values():
        steps.extend(job.get("steps", []))
    return steps


def _step_run(step: dict) -> str:
    return step.get("run") or ""


def test_full_suite_test_step_runs_xdist_auto_loadgroup() -> None:
    workflow = _load_workflow()
    test_steps = [s for s in _all_steps(workflow) if s.get("name") == "Test"]
    assert test_steps, "no step named 'Test' found in ci.yml — full-suite step was renamed"
    run = _step_run(test_steps[0])
    assert "-n auto" in run, f"full-suite Test step lost '-n auto': {run!r}"
    assert "--dist loadgroup" in run, f"full-suite Test step lost '--dist loadgroup': {run!r}"


def test_every_output_parsing_gate_stays_serial() -> None:
    """No step that parses pytest's summary line may run under xdist.

    Derived, not enumerated: this covers gates added after this test was
    written, which is the whole point — the failure mode being guarded is
    someone adding ``-n`` to a *new* gate, and a hand-maintained list is
    silent about exactly that case.
    """
    workflow = _load_workflow()
    gates = [s for s in _all_steps(workflow) if _is_output_parsing_gate(_step_run(s))]

    # Cardinality floor FIRST: a detector that matched nothing would make every
    # assertion below vacuously true. This is the control, and it is the reason
    # the constant survives at all.
    assert len(gates) >= MIN_EXPECTED_SERIAL_GATES, (
        f"only {len(gates)} output-parsing gate(s) detected in ci.yml, expected at least "
        f"{MIN_EXPECTED_SERIAL_GATES} — the detector is broken, or gates were removed. "
        f"Found: {[g.get('name') for g in gates]}"
    )

    for step in gates:
        run = _step_run(step)
        assert "-n " not in run and not run.rstrip().endswith("-n"), (
            f"gate step {step['name']!r} gained an xdist '-n' flag — this would corrupt its "
            f"fail-closed pytest-summary parsing: {run!r}"
        )

    # The full-suite Test step must NOT be caught by the detector: it is the one
    # step that is *supposed* to run under xdist. If it ever starts parsing its
    # own output, this guard and the xdist wiring are in direct conflict and a
    # human has to resolve it rather than one silently winning.
    assert not any(g.get("name") == "Test" for g in gates), (
        "the full-suite 'Test' step now parses pytest output, which conflicts with running it "
        "under xdist — resolve deliberately, do not let one side win by accident"
    )


def test_root_addopts_never_gains_xdist_flag() -> None:
    config = tomllib.loads(ROOT_PYPROJECT.read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    assert "-n" not in addopts.split(), f"root addopts gained an xdist '-n' flag: {addopts!r}"
    # Positive control: addopts is non-empty and still carries the flag we
    # know must be there, so a key that resolved to "" wouldn't slip past.
    assert "--import-mode=importlib" in addopts


def test_pytest_xdist_declared_in_dev_group() -> None:
    config = tomllib.loads(ROOT_PYPROJECT.read_text(encoding="utf-8"))
    dev_deps = config["dependency-groups"]["dev"]
    assert any(dep.startswith("pytest-xdist") for dep in dev_deps), (
        f"pytest-xdist missing from [dependency-groups].dev: {dev_deps!r}"
    )
    # Positive control: a sibling dependency we know is always declared.
    assert any(dep.startswith("pytest>=") for dep in dev_deps)


def test_xdist_is_importable() -> None:
    import xdist  # noqa: F401
