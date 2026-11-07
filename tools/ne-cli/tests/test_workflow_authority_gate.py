# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-CORE's exact-path guardian for its promoted CI gate.

``test_xdist_wiring.py`` already derives every pytest-summary-parsing gate in
``ci.yml`` and fails any that runs under xdist, so seriality is NOT restated here.
What this file owns is everything that guardian cannot see:

* the gate lives in the REQUIRED ``check`` job — a node promoted only into
  ``live-postgres`` gates no merge;
* its Qdrant image is READ OUT of ``deploy/compose/pilot/compose.yaml`` and
  compared, never restated as a second literal. A control holding its own copy of
  the digest passes against both the correct workflow and one pinned to a
  floating tag, which is the entire failure mode;
* both database URLs are set at STEP level, not job-wide, so the broad ``Test``
  step keeps its existing behaviour;
* the pass-count floor equals what pytest ITSELF collects — zero slack, so a
  deleted proof reds PR CI instead of fitting underneath;
* the no-skip and zero-collection checks are both present in the step.

Self-contained by convention -- `tools/ne-cli/tests/` has no conftest.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PILOT_COMPOSE = REPO_ROOT / "deploy" / "compose" / "pilot" / "compose.yaml"

GATE_JOB = "check"
GATE_STEP = (
    "Source-authority store and immutable-artifact proofs (REQUIRED gate — exact floor, must run)"
)
PROMOTED_NODES = (
    "packages/ne-brain/tests/test_current_source_authority_postgres.py",
    "packages/ne-brain/tests/test_immutable_artifact_store_postgres_qdrant.py",
)


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _job(name: str) -> dict[str, Any]:
    return _workflow()["jobs"][name]


def _step(job: str, name: str) -> dict[str, Any]:
    for step in _job(job)["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"{job!r} has no step named {name!r}")


def _collected(*modules: str) -> int:
    """How many tests pytest itself collects from ``modules``.

    Derived from pytest's OWN collection rather than a hand count, so deleting or
    renaming a promoted proof moves the floor with it instead of leaving slack.
    """
    result = subprocess.run(  # fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            *[str(REPO_ROOT / module) for module in modules],
            "--collect-only",
            "-q",
            "-p",
            "no:randomly",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    assert match is not None, result.stdout + result.stderr
    return int(match.group(1))


# --- the Qdrant pin has exactly one source -----------------------------------


def test_the_required_job_runs_the_exact_qdrant_image_the_pilot_pins() -> None:
    pilot = yaml.safe_load(PILOT_COMPOSE.read_text(encoding="utf-8"))
    pinned = pilot["services"]["qdrant"]["image"]
    # Read, not restated: this test holds no digest of its own, so replacing the
    # workflow pin with a floating tag fails HERE.
    assert "@sha256:" in pinned
    services = _job(GATE_JOB).get("services", {})
    assert "qdrant" in services
    assert services["qdrant"]["image"] == pinned
    assert "6333:6333" in [str(port) for port in services["qdrant"]["ports"]]
    assert services["qdrant"]["env"]["QDRANT__TELEMETRY_DISABLED"] == "true"


def test_the_gate_waits_on_qdrant_readiness_before_running_the_proofs() -> None:
    """A service container is scheduled, not ready; without the wait the gate flakes."""
    run = str(_step(GATE_JOB, GATE_STEP)["run"])
    wait, _, invocation = run.partition("uv run pytest")
    assert "/readyz" in wait
    assert "curl" in wait
    assert invocation, "the readiness wait must precede the pytest invocation"


# --- the gate is in the REQUIRED job, at its exact path ----------------------


def test_the_required_check_job_runs_both_promoted_authority_nodes() -> None:
    """`live-postgres` is not a required context, so a gate there blocks nothing."""
    run = str(_step(GATE_JOB, GATE_STEP)["run"])
    invocation = run.split("uv run pytest", 1)[1].split("-q -rs", 1)[0]
    for node in PROMOTED_NODES:
        assert node in invocation, node
        assert (REPO_ROOT / node).is_file(), node


def test_both_database_urls_are_set_on_the_step_and_not_job_wide() -> None:
    """Job-wide would change the broad `Test` step's behaviour, silently."""
    step_env = _step(GATE_JOB, GATE_STEP).get("env", {})
    assert step_env["NE_TEST_POSTGRES_URL"].startswith("postgresql+psycopg://")
    assert step_env["NE_TEST_QDRANT_URL"] == "http://localhost:6333"
    job_env = _job(GATE_JOB).get("env", {})
    assert "NE_TEST_POSTGRES_URL" not in job_env
    assert "NE_TEST_QDRANT_URL" not in job_env


def test_the_gate_floor_equals_what_pytest_collects() -> None:
    """Zero slack: a floor above collection is a lie, one below hides a deletion."""
    run = str(_step(GATE_JOB, GATE_STEP)["run"])
    match = re.search(r"SOURCE_AUTHORITY_MIN_PASSED=(\d+)", run)
    assert match is not None
    assert int(match.group(1)) == _collected(*PROMOTED_NODES)


def test_the_gate_fails_closed_on_a_skip_or_an_empty_collection() -> None:
    """A skipped acceptance gate is itself a vacuous guard."""
    run = str(_step(GATE_JOB, GATE_STEP)["run"])
    assert "skipped" in run
    assert "no tests ran" in run
    assert "set -euo pipefail" in run


# --- the broad `Test` step's Qdrant proofs cannot park green ------------------
#
# NE-CI-QDRANT-FAIL-CLOSED. Separate concern from the promoted gate above, same
# file because it is the same question asked of the same job: does the REQUIRED
# `check` context actually EXECUTE the real-Qdrant proofs, or can it go green
# having skipped them?
#
# The hole predates this train — the server-mode modules probed the same port the
# same way when their address was defaulted rather than named — and it exists
# because the `qdrant` service carries no `--health-cmd` (the image ships no
# shell HTTP client), so GitHub waits only for the container to be SCHEDULED. A
# slow start means collection happens against a dead port and every server-mode
# module skips itself.
#
# The `Test` step cannot use the tee/grep floor the gates above use: it runs the
# whole suite under `-n auto`, and `test_xdist_wiring.py` fails any
# summary-parsing gate that carries `-n` (worker-prefixed output corrupts that
# parsing). It also cannot use a blanket no-skip rule, because most of the suite
# skips legitimately. So the floor is a JUnit report read per-module, aimed at
# the modules whose ONLY live dependency is the Qdrant this job provides.
#
# Bidirectional mutation, run and observed rather than asserted:
#
# ===  ==============================================================  ======
# m    mutation                                                        result
# ===  ==============================================================  ======
# m1   drop one module from the gate's list in ci.yml                   RED
#                                                                       (:325)
# m2   make ``_parks_on_qdrant_alone`` match nothing                    RED
#                                                                       (:320)
# m3   delete the unguarded ``curl`` after the readiness loop           RED
#                                                                       (:288)
# m4   drop ``--junitxml`` from the full-suite step                     RED
#                                                                       (:302)
# m5   point the readiness curl at a path Qdrant does not serve         RED
#                                                                       (:288)
# m6   unmutated control                                               GREEN
# ===  ==============================================================  ======
#
# m1 and m2 fail at DIFFERENT lines: one is the guard, the other is the control
# that keeps an empty derivation from making the guard vacuously true.
#
# The gate's own script was exercised directly against two JUnit reports: one
# produced by really running the four modules with no Qdrant named (four
# ``collection skipped`` entries -> exit 1, each module named), and one with those
# entries rewritten into executed testcases (-> exit 0). A truncated report and a
# missing report both exit 1 on the vacuity control.

QDRANT_WAIT_STEP = (
    "Wait for the job's Qdrant service (the full suite has no health check to lean on)"
)
FULL_SUITE_STEP = "Test"
NO_PARK_STEP = (
    "Qdrant real-server proofs must not park in the full suite (REQUIRED — no silent skip)"
)
LIVE_TEST_DIRS = (
    REPO_ROOT / "packages" / "ne-brain" / "tests",
    REPO_ROOT / "packages" / "ne-eval" / "tests",
)
# Cardinality floor for the derivation below: a matcher that silently matched
# nothing would make the set-equality assertion vacuously true against an empty
# list in ci.yml. This is the control, not the expectation.
MIN_QDRANT_ONLY_MODULES = 4


def _step_names(job: str) -> list[str]:
    return [str(step.get("name")) for step in _job(job)["steps"]]


def _module_level_skip_guards(tree: ast.Module) -> list[ast.expr]:
    """Conditions of module-level ``if ...: pytest.skip(allow_module_level=True)``."""
    guards: list[ast.expr] = []
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        for inner in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if not (isinstance(func, ast.Attribute) and func.attr == "skip"):
                continue
            if any(kw.arg == "allow_module_level" for kw in inner.keywords):
                guards.append(node.test)
                break
    return guards


def _parks_on_qdrant_alone(path: Path) -> bool:
    """Does this module skip itself for a missing Qdrant and NOTHING else?

    Derived, not enumerated. ``test_knowledge_write_live_e2e`` also wants a
    Postgres (its condition carries a comparison), and
    ``test_retrieval_pipeline_conformance_server`` also wants a TEI reranker (its
    condition carries a second call) — neither is provided by the ``check`` job,
    so neither belongs in a floor that says "you had everything you needed".
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for guard in _module_level_skip_guards(tree):
        calls = [node for node in ast.walk(guard) if isinstance(node, ast.Call)]
        named = {node.func.id for node in calls if isinstance(node.func, ast.Name)}
        if len(calls) != 1 or named != {"server_reachable"}:
            continue
        if any(isinstance(node, ast.Compare) for node in ast.walk(guard)):
            continue
        return True
    return False


def _qdrant_only_modules() -> set[str]:
    found: set[str] = set()
    for directory in LIVE_TEST_DIRS:
        for path in sorted(directory.glob("test_*.py")):
            if _parks_on_qdrant_alone(path):
                found.add(path.relative_to(REPO_ROOT).as_posix())
    return found


def test_the_full_suite_waits_for_qdrant_because_the_service_has_no_health_check() -> None:
    """No `--health-cmd` means GitHub waits for SCHEDULED, not READY."""
    services = _job(GATE_JOB).get("services", {})
    assert "options" not in services["qdrant"], (
        "the qdrant service gained container-level options — if a health check was added, "
        "say so here rather than leaving two mechanisms disagreeing"
    )
    names = _step_names(GATE_JOB)
    assert names.index(QDRANT_WAIT_STEP) < names.index(FULL_SUITE_STEP), (
        "the readiness wait must precede the full suite, or collection can race the service"
    )
    run = str(_step(GATE_JOB, QDRANT_WAIT_STEP)["run"])
    assert "/readyz" in run
    # Fail-closed half: a bounded loop that gives up silently is not a gate. The
    # bare `curl -sSf` after the loop is what turns "never came up" into a red job.
    loop, _, after = run.partition("done")
    assert "seq" in loop and "sleep" in loop, run
    assert "curl -sSf http://localhost:6333/readyz" in after, (
        "the wait loop must be followed by an unguarded curl, or a service that never "
        f"answers just falls out of the loop and the suite runs anyway: {run!r}"
    )
    assert "set -euo pipefail" in run


def test_the_full_suite_writes_the_report_the_no_park_gate_reads() -> None:
    """One path, named once per step and asserted equal — not two literals."""
    full_suite = _step(GATE_JOB, FULL_SUITE_STEP)
    no_park = _step(GATE_JOB, NO_PARK_STEP)
    report = full_suite["env"]["NE_FULL_SUITE_JUNIT"]
    assert report.endswith(".xml")
    assert no_park["env"]["NE_FULL_SUITE_JUNIT"] == report
    assert '--junitxml="$NE_FULL_SUITE_JUNIT"' in str(full_suite["run"])
    names = _step_names(GATE_JOB)
    assert names.index(FULL_SUITE_STEP) < names.index(NO_PARK_STEP)


def test_the_full_suite_still_names_the_qdrant_the_no_park_gate_assumes() -> None:
    """The floor is only honest if the job actually handed the modules an address."""
    step_env = _step(GATE_JOB, FULL_SUITE_STEP).get("env", {})
    assert step_env["NE_QDRANT_URL"] == "http://localhost:6333"
    assert "NE_QDRANT_URL" not in _job(GATE_JOB).get("env", {}), (
        "job-wide would hand the address to every step, including ones with no service"
    )


def test_the_no_park_gate_covers_every_module_that_parks_on_qdrant_alone() -> None:
    """Derived from ne-brain/ne-eval source, so a fifth such module is covered on day one."""
    derived = _qdrant_only_modules()
    # Control first: an empty derivation would make the equality below vacuous.
    assert len(derived) >= MIN_QDRANT_ONLY_MODULES, (
        f"only {len(derived)} Qdrant-only module(s) detected — the matcher is broken: {derived}"
    )
    run = str(_step(GATE_JOB, NO_PARK_STEP)["run"])
    listed = set(re.findall(r'"(packages/[^"]+/tests/test_[^"]+\.py)"', run))
    assert listed == derived, (
        "the no-park gate's module list has drifted from what actually parks on a missing "
        f"Qdrant alone — listed {sorted(listed)}, derived {sorted(derived)}"
    )


def test_the_no_park_gate_is_specific_rather_than_a_blanket_no_skip_rule() -> None:
    """A blanket rule over the whole suite would be wrong: most skips are legitimate."""
    run = str(_step(GATE_JOB, NO_PARK_STEP)["run"])
    # It must NOT catch the modules this job deliberately does not provide for.
    for parked_for_other_reasons in (
        "packages/ne-brain/tests/test_knowledge_write_live_e2e.py",
        "packages/ne-brain/tests/test_retrieval_pipeline_conformance_server.py",
        "packages/ne-eval/tests/test_retrieval_baseline_live.py",
    ):
        assert parked_for_other_reasons not in run, parked_for_other_reasons
    assert "collection skipped" in run or 'case.get("name") == dotted' in run, (
        "the gate must recognise a collection-level park, which is the shape the "
        "module-level pytest.skip produces in JUnit XML"
    )
    assert "set -euo pipefail" in run


def test_the_no_park_gate_never_parses_pytest_summary_output() -> None:
    """It runs after an xdist'd step; summary parsing there is what `test_xdist_wiring` bans."""
    run = str(_step(GATE_JOB, NO_PARK_STEP)["run"])
    assert "grep" not in run, (
        "a grep here would classify this step as an output-parsing gate in "
        "test_xdist_wiring.py, which is a guard about a mechanism this step does not use"
    )
    assert "uv run pytest" not in run, "the gate reads a report; it must not re-run the proofs"
