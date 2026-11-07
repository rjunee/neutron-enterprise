# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO-RIG-MODEL: behavioral suite for `deploy/inference-operator/vast-rig.sh doctor`.

Tests the doctor's LOCAL decision logic only — the remote probe's own
field-gathering (what `nvidia-smi`/`nvcc`/`python3 -c 'import vllm'` actually
report on a real box) is NOT exercised here; that is live-parked to the
orchestrator's box (see the unit's commit body). What IS proven, with no
network involved: given a `NE_DOCTOR key=value` report, does `run_doctor`
correctly discriminate healthy-vs-broken, name the RIGHT check in its
failure message (never a bare nonzero exit — vacuous-guards V7: a guard the
environment could satisfy for the wrong reason is not a guard), and does the
gcc-missing case actually stop `pull` BEFORE the ~11-minute download launch
(the ordering fix this unit's brief exists to prove)?

Harness: a stub `bin/` directory prepended to PATH holds a fake `ssh` that
never touches the network — it logs every invocation to `$NE_TEST_SSH_LOG`
and, keyed on the distinct `ne-doctor-probe` argv token `run_doctor` ships
the remote probe with, hands back a canned report from
`$NE_TEST_DOCTOR_REPORT` instead of really executing anything remote. A fake
`vastai` at `$NE_VAST_VENV/bin/vastai` (the exact path `ensure_vastai` looks
for, so the real one-time venv-install branch is skipped) answers
`set api-key`/`show instance --raw` with canned success/JSON.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RIG = REPO_ROOT / "deploy" / "inference-operator" / "vast-rig.sh"
_RIG_TEXT = RIG.read_text(encoding="utf-8")


def _module_rig_constant(name: str) -> str:
    """Parse a single `NAME=value` shell constant out of vast-rig.sh.

    Mirrors `test_gpu_rig_model_guard.py`'s `_rig_constant` (kept as a small
    local duplicate rather than a cross-module import, matching this repo's
    existing per-test-module parsing style) — the point is that this
    harness's canned report and its port-derivation assertions can never
    diverge from the script's own constants; a renamed/deleted constant
    fails loud here too.
    """
    matches = re.findall(rf"^{re.escape(name)}=(\S+)", _RIG_TEXT, re.MULTILINE)
    assert len(matches) == 1, (
        f"expected exactly one `{name}=...` constant in {RIG}, found {matches!r}"
    )
    return matches[0]


INT_VLLM = _module_rig_constant("INT_VLLM")
INT_CHEAP = _module_rig_constant("INT_CHEAP")
MIN_DRIVER_CUDA = _module_rig_constant("MIN_DRIVER_CUDA")
VLLM_PIN = _module_rig_constant("VLLM_PIN")
SERVED_NAME = _module_rig_constant("SERVED_NAME")

HEALTHY_REPORT: dict[str, str] = {
    "gcc": "/usr/bin/gcc",
    "nvcc": "/usr/local/cuda/bin/nvcc",
    "cuda_home": "/usr/local/cuda",
    "vllm": "0.24.0",
    "torch": "2.11.0+cu128",
    "gpus": "1",
    "driver_cuda": "12.8",
    "disk_free_gb": "100",
    f"serving_{INT_VLLM}": SERVED_NAME,
    f"serving_{INT_CHEAP}": "NOT_SERVING",
}

_FAKE_SSH = """#!/usr/bin/env bash
# Fake ssh for test_gpu_rig_doctor.py — never touches the network.
args=("$@")
n=${#args[@]}
last="${args[$((n-1))]}"

{
  printf 'SSH_CALL:'
  printf ' %q' "$@"
  printf '\\n'
} >> "$NE_TEST_SSH_LOG"

case "$last" in
  *ne-doctor-probe*)
    cat > "${NE_TEST_PROBE_STDIN:-/dev/null}"
    cat "$NE_TEST_DOCTOR_REPORT"
    exit 0
    ;;
  "echo ok")
    echo ok
    exit 0
    ;;
  *"cat > /root/ne-dl.sh"*)
    cat > /dev/null
    echo "WROTE_NE_DL_SH" >> "$NE_TEST_SSH_LOG"
    exit 0
    ;;
  *tmux*)
    echo launched
    exit 0
    ;;
  "bash -s")
    cat > "${NE_TEST_ENSURE_VLLM_SCRIPT:-/dev/null}"
    echo remote-ok
    exit 0
    ;;
  *nvidia-smi*)
    if [ "${NE_TEST_SSH_FAIL_NVIDIA:-0}" = "1" ]; then
      exit 255
    elif [ "${NE_TEST_SSH_EMPTY_NVIDIA:-0}" = "1" ]; then
      printf ''
      exit 0
    else
      echo 0
      exit 0
    fi
    ;;
  *)
    cat > /dev/null 2>/dev/null || true
    exit 0
    ;;
esac
"""

_FAKE_VASTAI = """#!/usr/bin/env bash
# Fake vastai for test_gpu_rig_doctor.py — never touches the network.
if [ "$1" = "set" ] && [ "$2" = "api-key" ]; then
  exit 0
fi
if [ "$1" = "create" ] && [ "$2" = "ssh-key" ]; then
  exit 0
fi
if [ "$1" = "show" ] && [ "$2" = "instance" ]; then
  cat "$NE_TEST_VASTAI_SHOW_JSON"
  exit 0
fi
exit 0
"""

_VASTAI_SHOW_JSON = json.dumps(
    {
        "public_ipaddr": "127.0.0.1",
        "ports": {
            "22/tcp": [{"HostPort": "2222"}],
            f"{INT_VLLM}/tcp": [{"HostPort": "20200"}],
            f"{INT_CHEAP}/tcp": [{"HostPort": "20100"}],
        },
        "actual_status": "running",
    }
)


def _report_text(overrides: dict[str, str] | None = None, drop: Iterable[str] = ()) -> str:
    """Render a canned `NE_DOCTOR key=value` report, healthy by default.

    `drop` removes keys entirely (as opposed to `overrides`, which can only
    change a key's VALUE) — used to simulate a probe/parser port-list
    mismatch, where the report simply never emits a `serving_<port>` line
    the local parser expects.
    """
    fields = dict(HEALTHY_REPORT)
    if overrides:
        fields.update(overrides)
    for key in drop:
        fields.pop(key, None)
    return "".join(f"NE_DOCTOR {k}={v}\n" for k, v in fields.items())


def _write_exec(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _build_env(
    tmp_path: Path,
    report_overrides: dict[str, str] | None = None,
    report_drop: Iterable[str] = (),
) -> dict[str, str]:
    """Build the full doctored environment for a `vast-rig.sh` subprocess run.

    Typed `dict[str, str]` throughout (pyright-clean): `subprocess.run`'s
    `env=` accepts exactly that shape, and every value assembled here is a
    real `str` (paths stringified, not left as `Path`).
    """
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(parents=True, exist_ok=True)
    _write_exec(stub_bin / "ssh", _FAKE_SSH)

    vast_venv = tmp_path / "vastvenv"
    (vast_venv / "bin").mkdir(parents=True, exist_ok=True)
    _write_exec(vast_venv / "bin" / "vastai", _FAKE_VASTAI)

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "instance.json").write_text(json.dumps({"id": 123}), encoding="utf-8")

    secrets = tmp_path / "secrets"
    (secrets / "vast").mkdir(parents=True, exist_ok=True)
    (secrets / "vast" / "api-key").write_text("testkey123", encoding="utf-8")
    (secrets / "huggingface").mkdir(parents=True, exist_ok=True)
    (secrets / "huggingface" / "ids.txt").write_text("hf_testtoken", encoding="utf-8")

    vastai_json_path = tmp_path / "vastai-show.json"
    vastai_json_path.write_text(_VASTAI_SHOW_JSON, encoding="utf-8")

    doctor_report_path = tmp_path / "doctor-report.txt"
    doctor_report_path.write_text(_report_text(report_overrides, report_drop), encoding="utf-8")

    probe_stdin_path = tmp_path / "probe-stdin.txt"
    ensure_vllm_script_path = tmp_path / "ensure-vllm-script.sh"

    ssh_log_path = tmp_path / "ssh.log"
    ssh_log_path.write_text("", encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)

    env: dict[str, str] = {
        "HOME": str(home),
        "PATH": f"{stub_bin}:{os.environ.get('PATH', '')}",
        "NE_VAST_VENV": str(vast_venv),
        "NE_VAST_STATE_DIR": str(state_dir),
        "NE_OWNER_SECRETS": str(secrets),
        "NE_TEST_SSH_LOG": str(ssh_log_path),
        "NE_TEST_DOCTOR_REPORT": str(doctor_report_path),
        "NE_TEST_VASTAI_SHOW_JSON": str(vastai_json_path),
        "NE_TEST_PROBE_STDIN": str(probe_stdin_path),
        "NE_TEST_ENSURE_VLLM_SCRIPT": str(ensure_vllm_script_path),
    }
    return env


def _run_rig(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(RIG), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _ssh_log(env: dict[str, str]) -> str:
    return Path(env["NE_TEST_SSH_LOG"]).read_text(encoding="utf-8")


# `rssh`'s fake-ssh logger renders argv via `printf ' %q'`, so a bare `bash
# -s` argument (ensure_vllm's remote-install invocation, per `| rssh 'bash
# -s'`) is logged with its space `%q`-escaped: `bash\ -s`, one full log line
# ending in a newline. The doctor probe is a DIFFERENT invocation —
# `bash -s -- ne-doctor-probe` — which also starts with `bash\ -s` but is
# followed by `\ --\ ne-doctor-probe` before its own newline. Matching the
# newline-terminated marker below is what tells the two apart; a bare
# substring match on `"bash -s"` (unescaped, no anchor) would either miss
# the escaped form entirely or, if written as `"bash\\ -s"` with no `\n`,
# false-positive-match the doctor-probe line too (it starts the same way).
_ENSURE_VLLM_SSH_MARKER = "bash\\ -s\n"


def _ensure_vllm_was_invoked(env: dict[str, str]) -> bool:
    return _ENSURE_VLLM_SSH_MARKER in _ssh_log(env)


# ---- doctor (standalone subcommand) ----------------------------------------


def test_doctor_healthy_box_exits_zero_and_notes_served_name_demo(tmp_path: Path) -> None:
    """Presence-only assertions here (`"served-model-name" in combined`,
    `"demo" in combined`) were satisfiable by the drift Argus's round-4
    review found: `serve_one`'s call sites passed the LITERAL `demo` while
    this NOTE also said `demo`, so a bump to either alone would have left
    this test green. Tightened: assert the NOTE's advertised name equals
    SERVED_NAME AND cross-check it against the value `serve single` actually
    puts on the vLLM command line (parsed from the fake-ssh call log, not
    from advisory text) — the two must never diverge silently again.
    """
    env = _build_env(tmp_path)
    result = _run_rig(env, "doctor")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "served-model-name" in combined
    assert SERVED_NAME in combined

    serve_env = _build_env(tmp_path)
    serve_result = _run_rig(serve_env, "serve", "single")
    assert serve_result.returncode == 0, serve_result.stdout + serve_result.stderr
    log = _ssh_log(serve_env)
    # The ssh call log is `%q`-escaped by the fake-ssh harness's `printf '
    # %q'`, so a literal space inside the logged command becomes `\ ` — match
    # one-or-more real-or-escaped whitespace between the flag and its value.
    served_on_wire = re.findall(r"--served-model-name(?:\\?\s)+(\S+?)(?:\\?\s)", log)
    assert served_on_wire, f"no `--served-model-name` found on the wire: {log!r}"
    assert all(v == SERVED_NAME for v in served_on_wire), (
        f"serve command served-model-name {served_on_wire!r} != rig SERVED_NAME={SERVED_NAME!r}"
    )


def test_doctor_fails_loud_on_missing_gcc(tmp_path: Path) -> None:
    env = _build_env(tmp_path, {"gcc": "MISSING"})
    result = _run_rig(env, "doctor")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "gcc" in combined
    assert "build-essential" in combined


def test_doctor_fails_loud_on_old_driver(tmp_path: Path) -> None:
    env = _build_env(tmp_path, {"driver_cuda": "12.2"})
    result = _run_rig(env, "doctor")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "driver" in combined.lower()
    assert "MIN_DRIVER_CUDA" in combined


def test_doctor_full_mode_fails_on_vllm_pin_mismatch(tmp_path: Path) -> None:
    """ "Full mode" is `run_doctor`'s post-INSTALL verification — the call
    `cmd_serve` makes AFTER `ensure_vllm` has already tried to install the
    pin. It is exercised here via `serve single`, not the standalone
    `doctor` subcommand.

    Argus P2 (this round): the standalone `doctor` subcommand used to run
    this same hard-fail mode, so running it right after `provision` — before
    anything had attempted an install — produced the factually wrong message
    "the pinned install did not take". `doctor` now runs a WARN-only mode
    (see `test_doctor_check_mode_warns_not_fails_on_vllm_mismatch` below);
    only `serve`'s post-`ensure_vllm` check treats a mismatch as the real
    problem it is at that point in the lifecycle. `serve single` dies inside
    `run_doctor` before ever reaching `serve_one` (no GPU-serving command is
    attempted), so the fake-ssh harness needs no extra stubbing.
    """
    env = _build_env(tmp_path, {"vllm": "0.26.0"})
    result = _run_rig(env, "serve", "single")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "0.26.0" in combined
    assert "0.24.0" in combined


def test_doctor_check_mode_warns_not_fails_on_vllm_mismatch(tmp_path: Path) -> None:
    """The standalone `doctor` subcommand runs BEFORE any install has been
    attempted (typically right after `provision`/`use`) — a vllm-pin
    mismatch there is informational, not a failure; `serve`'s `ensure_vllm`
    is what actually installs the pin. Argus P2 fix, direct control for the
    "full mode" test above: same mismatched report, different subcommand,
    opposite (WARN, not FAIL) outcome.
    """
    env = _build_env(tmp_path, {"vllm": "0.26.0"})
    result = _run_rig(env, "doctor")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "0.26.0" in combined
    assert "0.24.0" in combined


def test_doctor_warns_not_fails_on_missing_nvcc(tmp_path: Path) -> None:
    """Argus P1: nvcc/CUDA_HOME missing must WARN, never hard-block —
    whether vLLM needs either at *serve* time (vs. only at the published
    image's own *build* time) is unconfirmed, and hard-gating an unverified
    predicate on the orchestrator's nightly-used provisioning tool risks
    blocking a box that can demonstrably serve real tokens with no override.
    """
    env = _build_env(tmp_path, {"nvcc": "MISSING", "cuda_home": "MISSING"})
    result = _run_rig(env, "doctor")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "nvcc" in combined


def test_doctor_fails_loud_on_too_few_gpus(tmp_path: Path) -> None:
    env = _build_env(tmp_path, {"gpus": "0"})
    result = _run_rig(env, "doctor")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "gpus=0" in combined


def test_doctor_fails_loud_on_insufficient_disk(tmp_path: Path) -> None:
    env = _build_env(tmp_path, {"disk_free_gb": "1"})
    result = _run_rig(env, "doctor")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "disk_free_gb=1" in combined


# ---- pull (doctor wired in front of the download launch) -------------------


def test_pull_doctor_mode_warns_not_fails_on_vllm_mismatch(tmp_path: Path) -> None:
    env = _build_env(tmp_path, {"vllm": "0.26.0"})
    result = _run_rig(env, "pull", "fp8")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "ensure_vllm" in combined
    assert "force-install" in combined


def test_pull_runs_doctor_before_writing_download_script(tmp_path: Path) -> None:
    env = _build_env(tmp_path, {"gcc": "MISSING"})
    result = _run_rig(env, "pull", "fp8")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "ne-dl.sh" not in _ssh_log(env)
    assert "WROTE_NE_DL_SH" not in _ssh_log(env)


def test_pull_healthy_box_reaches_download_launch(tmp_path: Path) -> None:
    """Control: a healthy report DOES reach the download launch.

    Proves the RED cases above aren't satisfied by a harness that can never
    reach `ne-dl.sh` in the first place — that would make every RED case a
    false positive with an unrelated cause.
    """
    env = _build_env(tmp_path)
    result = _run_rig(env, "pull", "fp8")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    log = _ssh_log(env)
    assert "WROTE_NE_DL_SH" in log
    assert "launched" in combined


# ---- serve (GPU-count gate must run BEFORE ensure_vllm — Codex C1) --------


def test_serve_dual_checks_gpu_count_before_installing_vllm(tmp_path: Path) -> None:
    """Codex C1: `ensure_vllm` can pip-install a multi-GB vllm/torch on an
    adopted box, mutating its environment. `serve dual`/`serve route` need 2
    physical GPUs; `require_gpus` must reject an under-provisioned box
    BEFORE `ensure_vllm` ever runs, not after paying for the install.

    The fake `nvidia-smi -L` here reports 0 GPUs (the default, real-nvidia-smi
    -shaped `echo 0` — see `_FAKE_SSH`'s `*nvidia-smi*` case), so
    `require_gpus 2` always dies. `ensure_vllm`'s remote install script is
    shipped as `... | rssh 'bash -s'` — a distinct ssh-log invocation
    (`_ENSURE_VLLM_SSH_MARKER`, see helper above) separate from
    `require_gpus`'s `nvidia-smi -L | wc -l` command. If the ordering
    regressed back to ensure_vllm-first, that marker would appear in the log
    even though the command ultimately still fails on GPU count.
    """
    env = _build_env(tmp_path)
    result = _run_rig(env, "serve", "dual")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "needs 2 GPUs" in combined
    assert not _ensure_vllm_was_invoked(env), _ssh_log(env)


def test_serve_route_checks_gpu_count_before_installing_vllm(tmp_path: Path) -> None:
    """Same as above for `serve route` — the other 2-GPU mode."""
    env = _build_env(tmp_path)
    result = _run_rig(env, "serve", "route")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "needs 2 GPUs" in combined
    assert not _ensure_vllm_was_invoked(env), _ssh_log(env)


def test_serve_single_does_not_require_two_gpus(tmp_path: Path) -> None:
    """Control: `serve single` has no 2-GPU requirement, so it must reach
    `ensure_vllm` (the marker invocation appears) even on the same
    0-GPU-reporting fake box that rejects `dual`/`route` above — proving the
    gate in the two tests above is mode-specific, not a harness accident
    that blocks all `serve` calls regardless of mode.
    """
    env = _build_env(tmp_path)
    result = _run_rig(env, "serve", "single")
    combined = result.stdout + result.stderr
    assert _ensure_vllm_was_invoked(env), _ssh_log(env)
    assert "needs 2 GPUs" not in combined


def test_pull_skip_doctor_hatch_is_loud(tmp_path: Path) -> None:
    env = _build_env(tmp_path, {"gcc": "MISSING"})
    env["NE_SKIP_DOCTOR"] = "1"
    result = _run_rig(env, "pull", "fp8")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "NE_SKIP_DOCTOR" in combined
    assert "SKIPPING" in combined
    assert "WROTE_NE_DL_SH" in _ssh_log(env)


# ---- NE-DEMO-RIG-MODEL (REVISE 3): doctor unskippable + probe/gate sweep --


def test_doctor_ignores_ne_skip_doctor(tmp_path: Path) -> None:
    """never-silently-downgrade-critical-stage: `doctor`'s entire purpose is
    verification, so `NE_SKIP_DOCTOR` must not silence it — the hatch
    survives only on `pull`/`serve`, which have a real workflow to push
    through; `doctor` itself has none. Previously this returned 0 having
    verified nothing.
    """
    env = _build_env(tmp_path, {"gcc": "MISSING"})
    env["NE_SKIP_DOCTOR"] = "1"
    result = _run_rig(env, "doctor")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "gcc" in combined
    assert "SKIPPING" not in combined


def test_doctor_ignores_ne_skip_doctor_healthy_control(tmp_path: Path) -> None:
    """Control for the test above: a HEALTHY report under NE_SKIP_DOCTOR=1
    still runs doctor for real and exits 0 — proving the RED case isn't
    doctor unconditionally failing regardless of the report."""
    env = _build_env(tmp_path)
    env["NE_SKIP_DOCTOR"] = "1"
    result = _run_rig(env, "doctor")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "doctor OK" in combined


def test_doctor_probe_port_list_derives_from_rig_constants(tmp_path: Path) -> None:
    """The probe script shipped to the remote must interpolate INT_VLLM/
    INT_CHEAP, not hand-copy `10200 10100` — captured via the fake ssh's
    stdin-capture on the `*ne-doctor-probe*` case.
    """
    env = _build_env(tmp_path)
    result = _run_rig(env, "doctor")
    assert result.returncode == 0, result.stdout + result.stderr
    probe_stdin = Path(env["NE_TEST_PROBE_STDIN"]).read_text(encoding="utf-8")
    assert f"for p in {INT_VLLM} {INT_CHEAP}" in probe_stdin, probe_stdin
    for tok in re.findall(r"\b10[0-9]{3}\b", probe_stdin):
        assert tok in (INT_VLLM, INT_CHEAP), (
            f"probe script sent to the remote contains port {tok!r}, not one of "
            f"INT_VLLM={INT_VLLM!r}/INT_CHEAP={INT_CHEAP!r}: {probe_stdin}"
        )


def test_doctor_fails_loud_when_probe_report_lacks_serving_keys(tmp_path: Path) -> None:
    """A report missing its `serving_<port>` keys entirely (as opposed to
    reporting them `NOT_SERVING`) means the probe's port list and the local
    parser's have drifted — that must die loudly, in every mode, not log an
    empty value and exit 0. The healthy-report control already covers the
    non-drifted case (`test_doctor_healthy_box_exits_zero_and_notes_served_name_demo`).
    """
    env = _build_env(tmp_path, report_drop=[f"serving_{INT_VLLM}", f"serving_{INT_CHEAP}"])
    result = _run_rig(env, "doctor")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "serving_" in combined
    assert "drift" in combined


def test_ensure_vllm_driver_gate_tracks_min_driver_cuda(tmp_path: Path) -> None:
    """Boundary lock on `ensure_vllm`'s remote driver gate — Argus's exact
    round-3 mutation (loosening the generated gate to `-lt 10` with
    MIN_DRIVER_CUDA untouched, previously 33-passed) must turn THIS test RED.

    Captures the EXACT script `ensure_vllm` ships to the remote
    (`... | rssh 'bash -s'`) via the fake-ssh harness's new `"bash -s")`
    case, then re-executes that captured script locally under `bash -s`
    with stubbed `nvidia-smi`/`python3` on `$PATH` — no network, no real
    remote box — at THREE points, all derived from the parsed
    MIN_DRIVER_CUDA: one minor version below it (must reject), a full major
    version below it (must reject), and exactly at it (must accept). The
    minor-boundary leg catches a full-gate loosen; the major-version leg
    additionally catches a PARTIAL loosen that touches only the major clause
    (e.g. `-lt $minmaj` -> `-lt 10` with the minor clause untouched) — Argus's
    round-4 finding: that mutation left the two-leg version of this test
    22/22 GREEN while the gate would accept an 11.x driver for cu128 wheels.
    If the remote gate is ever loosened (fully or partially), or
    MIN_DRIVER_CUDA is bumped without re-deriving the gate, a reject leg goes
    green -> RED.
    """
    env = _build_env(tmp_path)
    result = _run_rig(env, "serve", "single")
    assert result.returncode == 0, result.stdout + result.stderr
    captured = Path(env["NE_TEST_ENSURE_VLLM_SCRIPT"]).read_text(encoding="utf-8")
    assert captured.strip(), "ensure_vllm's remote script was never captured"

    maj_str, _, min_str = MIN_DRIVER_CUDA.partition(".")
    maj, minr = int(maj_str), int(min_str)
    below = f"{maj - 1}.9" if minr == 0 else f"{maj}.{minr - 1}"
    below_major = f"{maj - 1}.9"

    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir(parents=True, exist_ok=True)
    _write_exec(
        stub_bin / "nvidia-smi",
        '#!/usr/bin/env bash\necho "GPU 0: Fake (CUDA Version: ${NE_FAKE_CUDA})"\n',
    )
    _write_exec(
        stub_bin / "python3",
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *vllm*) echo "$NE_FAKE_VLLM_PIN" ;;\n'
        '  *) echo "" ;;\n'
        "esac\n",
    )

    def _run_captured(cuda: str) -> subprocess.CompletedProcess[str]:
        stub_env: dict[str, str] = {
            "PATH": f"{stub_bin}:{os.environ.get('PATH', '')}",
            "NE_FAKE_CUDA": cuda,
            "NE_FAKE_VLLM_PIN": VLLM_PIN,
        }
        return subprocess.run(
            ["bash", "-s"],
            input=captured,
            env=stub_env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    below_result = _run_captured(below)
    below_combined = below_result.stdout + below_result.stderr
    assert below_result.returncode != 0, below_combined
    assert MIN_DRIVER_CUDA in below_combined, below_combined

    below_major_result = _run_captured(below_major)
    below_major_combined = below_major_result.stdout + below_major_result.stderr
    assert below_major_result.returncode != 0, below_major_combined
    assert MIN_DRIVER_CUDA in below_major_combined, below_major_combined

    at_result = _run_captured(MIN_DRIVER_CUDA)
    at_combined = at_result.stdout + at_result.stderr
    assert at_result.returncode == 0, at_combined
    assert "already present" in at_combined, at_combined


def test_serve_reports_ssh_failure_not_gpu_shortfall(tmp_path: Path) -> None:
    """An `rssh` failure during `require_gpus`'s GPU-count probe must be
    reported as an SSH/remote error, never misreported as `re-provision with
    --gpus N` — `run_doctor` already gets this discrimination right one
    function away; `require_gpus` now mirrors it.
    """
    env = _build_env(tmp_path)
    env["NE_TEST_SSH_FAIL_NVIDIA"] = "1"
    result = _run_rig(env, "serve", "dual")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "SSH" in combined
    assert "re-provision with --gpus" not in combined


def test_serve_reports_ssh_empty_nvidia_not_gpu_shortfall(tmp_path: Path) -> None:
    """Sibling of the test above: SSH succeeds but `nvidia-smi -L` itself
    returns nothing (as opposed to a real box, which always emits a digit,
    0 included) — a different failure than an SSH error, and still not a
    GPU shortfall.
    """
    env = _build_env(tmp_path)
    env["NE_TEST_SSH_EMPTY_NVIDIA"] = "1"
    result = _run_rig(env, "serve", "dual")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "nvidia-smi silent" in combined
    assert "re-provision with --gpus" not in combined


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
