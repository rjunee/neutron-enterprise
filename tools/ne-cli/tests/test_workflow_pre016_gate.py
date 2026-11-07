# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exact-path guardian for PRE-016's two promoted integration controls.

The backend proof belongs to the required ``check`` context and stays serial
because its shell gate parses pytest's own summary.  The browser-composition
proof belongs to the existing required ``frontend`` context and consumes
Vitest's JSON result rather than accepting a zero-test exit.  Both controls are
additive: the broad Python and frontend suites remain in their original jobs.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BACKEND_NODE = "packages/ne-worker/tests/test_pre016_integrated_agent_vertical_postgres.py"
CONSOLE_NODE = "src/agents/AgentVerticalIntegration.test.tsx"
BACKEND_STEP = (
    "PRE-016 production-composed Agent vertical PostgreSQL proofs "
    "(REQUIRED gate — exact floor, must run)"
)
CONSOLE_STEP = (
    "PRE-016 Agent vertical console composition proofs (REQUIRED gate — exact floor, must run)"
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


def _collected_backend() -> int:
    result = subprocess.run(  # fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            str(REPO_ROOT / BACKEND_NODE),
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


def test_backend_gate_is_in_required_check_at_the_exact_node() -> None:
    run = str(_step("check", BACKEND_STEP)["run"])
    invocation = run.split("uv run pytest", 1)[1].split("-q -rs", 1)[0]
    assert BACKEND_NODE in invocation
    assert (REPO_ROOT / BACKEND_NODE).is_file()
    assert "-n " not in invocation and "--numprocesses" not in invocation


def test_backend_gate_is_ordered_after_authority_and_before_checkpoint_c() -> None:
    names = [step.get("name") for step in _job("check")["steps"]]
    authority = (
        "Source-authority store and immutable-artifact proofs "
        "(REQUIRED gate — exact floor, must run)"
    )
    assert (
        names.index(authority)
        < names.index(BACKEND_STEP)
        < names.index("Checkpoint-C executing gate (must run, not skip)")
    )
    authority_run = str(_step("check", authority)["run"])
    assert "SOURCE_AUTHORITY_MIN_PASSED=12" in authority_run


def test_backend_database_and_durability_are_step_scoped() -> None:
    env = _step("check", BACKEND_STEP)["env"]
    assert env == {
        "NE_TEST_POSTGRES_URL": ("postgresql+psycopg://postgres:postgres@localhost:5432/postgres"),
        "NE_RUNTIME_DURABILITY": "dbos",
    }
    job_env = _job("check").get("env", {})
    assert "NE_TEST_POSTGRES_URL" not in job_env
    assert "NE_RUNTIME_DURABILITY" not in job_env


def test_backend_age_readiness_waits_for_the_final_tcp_server() -> None:
    run = str(_step("check", BACKEND_STEP)["run"])
    probe = "docker exec pre016-postgres-age pg_isready --host 127.0.0.1 --username postgres"
    assert run.count(probe) == 2
    assert f"if {probe} >/dev/null 2>&1; then" in run
    assert f"\n{probe}\n" in run
    assert "pg_isready --username postgres" not in run
    assert "trap 'docker rm --force pre016-postgres-age >/dev/null 2>&1 || true' EXIT" in run


def test_backend_floor_equals_pytest_collection_and_fails_closed() -> None:
    run = str(_step("check", BACKEND_STEP)["run"])
    floor = re.search(r"PRE016_BACKEND_MIN_PASSED=(\d+)", run)
    assert floor is not None
    assert int(floor.group(1)) == _collected_backend()
    assert "set -euo pipefail" in run
    assert "skipped" in run and "no tests ran" in run


def test_console_gate_is_in_required_frontend_at_the_exact_node() -> None:
    run = str(_step("frontend", CONSOLE_STEP)["run"])
    assert f"npm run test -- {CONSOLE_NODE}" in run
    assert (REPO_ROOT / "packages" / "ne-console" / CONSOLE_NODE).is_file()
    assert "--passWithNoTests" not in run


def test_console_gate_preserves_the_broad_test_and_build_order() -> None:
    steps = _job("frontend")["steps"]
    names = [step.get("name") for step in steps]
    assert (
        names.index("Test (vitest, MSW fixtures)")
        < names.index(CONSOLE_STEP)
        < names.index("Build (tsc -b && vite build)")
    )
    assert _step("frontend", "Test (vitest, MSW fixtures)")["run"] == "npm run test"
    assert _step("frontend", "Build (tsc -b && vite build)")["run"] == "npm run build"
    assert _step("typecheck", "Type check")["env"]["NODE_OPTIONS"] == ("--max-old-space-size=4096")


def test_console_gate_uses_json_exact_floor_and_rejects_nonpassing_tests() -> None:
    run = str(_step("frontend", CONSOLE_STEP)["run"])
    floor = re.search(r"PRE016_CONSOLE_MIN_PASSED=(\d+)", run)
    assert floor is not None and int(floor.group(1)) == 3
    for witness in (
        "--reporter=json",
        "--outputFile=",
        "report.success !== true",
        "total < floor",
        "passed < floor",
        "pending !== 0",
        "todo !== 0",
    ):
        assert witness in run, witness
