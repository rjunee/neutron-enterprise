# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The `classify` step's docs-only fast path must actually be REACHABLE.

This executes the REAL step body extracted from `.github/workflows/ci.yml`
against a REAL temporary git repository, under the same
``shell: /usr/bin/bash -e {0}`` contract the runner uses. A static assertion on
the script's text would not have caught the bug these tests exist for: the
script read `$?` correctly and its comments described the 0/1/>=2 handling
accurately — errexit simply never let the `case` run.

The bug: `NONDOC="$(... | grep -v ...)"` takes the exit status of the command
substitution, and `grep -v` exits 1 when nothing matches, which for a docs-only
diff is the EXPECTED outcome. Under errexit the shell died there, so
`finish false` was unreachable, every docs-only PR reported `classify` FAILURE,
and — because an absent output reads as '' and the guarded `if:` treats that as
"run" — the live lanes ran anyway. The job's entire purpose is that skip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _classify_step() -> dict:
    doc = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    steps = [
        step
        for job in doc["jobs"].values()
        if isinstance(job, dict)
        for step in (job.get("steps") or [])
        if isinstance(step, dict) and "NONDOC" in str(step.get("run", ""))
    ]
    assert len(steps) == 1, f"expected exactly one classify step, found {len(steps)}"
    return steps[0]


def _run(tmp_path: Path, changed: dict[str, str]) -> tuple[int, str]:
    """Build a two-commit repo whose diff is `changed`, then run the real step."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "config", "user.email", "t@t"], check=True)
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    for rel, body in changed.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "head"], check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    script = tmp_path / "step.sh"
    script.write_text(_classify_step()["run"], encoding="utf-8")
    out_file = tmp_path / "gh_output"
    out_file.touch()
    # `bash -e` is the runner's own contract for this step; without it the bug
    # does not reproduce, which is precisely why it is spelled out here.
    proc = subprocess.run(
        ["bash", "-e", str(script)],
        cwd=repo,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "EVENT": "pull_request",
            "BASE_SHA": base,
            "HEAD_SHA": head,
            "BEFORE_SHA": "",
            "GITHUB_OUTPUT": str(out_file),
        },
    )
    return proc.returncode, out_file.read_text(encoding="utf-8")


def test_the_step_runs_under_the_runners_errexit_shell_contract() -> None:
    """The workflow must keep declaring the shell these tests execute under.

    If this ever stops being `bash -e`, the reproduction above is testing a
    contract the runner no longer has.
    """
    assert _classify_step().get("shell", "/usr/bin/bash -e {0}") == "/usr/bin/bash -e {0}"


def test_a_docs_only_diff_reaches_the_skip_instead_of_dying_on_errexit(tmp_path: Path) -> None:
    """THE REGRESSION. Fails on the pre-fix script with rc=1 and no output."""
    rc, output = _run(tmp_path, {"docs/spec-items/an-item.md": "prose\n"})
    assert rc == 0, f"classify died instead of classifying; output={output!r}"
    assert "code=false" in output, output


def test_a_prose_only_diff_across_several_allowlisted_paths_still_skips(tmp_path: Path) -> None:
    rc, output = _run(
        tmp_path,
        {"docs/ops/upgrade-plans/a-plan.md": "plan\n", "AS-BUILT.md": "| row |\n"},
    )
    assert rc == 0, output
    assert "code=false" in output, output


@pytest.mark.parametrize(
    ("label", "changed"),
    [
        ("code path", {"packages/ne-api/src/ne/api/thing.py": "x = 1\n"}),
        ("executable under docs", {"docs/orchestration/runner.mjs": "export {}\n"}),
        ("config under docs", {"docs/ops/some-config.yaml": "a: 1\n"}),
    ],
)
def test_anything_not_prose_still_runs_everything(
    label: str, changed: dict[str, str], tmp_path: Path
) -> None:
    """CONTROLS — the fix must not widen the skip.

    Without these, "docs-only reaches the skip" would be satisfiable by a script
    that skips unconditionally, which is the dangerous direction: it would park
    the live lanes on real code changes.
    """
    rc, output = _run(tmp_path, changed)
    assert rc == 0, output
    assert "code=true" in output, f"{label} was classified as skippable: {output!r}"
