# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral suite for the Vast adapter (`deploy/inference-operator/vast-rig.sh`)
and its shared card-class filter (`deploy/inference-operator/lib/rig_select.py`).

Every test here runs with NO network, NO provider account and NO spend. A fake
`vastai` on `$NE_VAST_VENV/bin` answers `search offers` with canned offers and
records `create instance` by touching a marker file, so the adapter's real
selection and spend-gate code runs for real.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPERATOR_DIR = REPO_ROOT / "deploy" / "inference-operator"
LIB_DIR = OPERATOR_DIR / "lib"
VAST_RIG = OPERATOR_DIR / "vast-rig.sh"


def _load(name: str) -> Any:
    """Import a module out of `deploy/inference-operator/lib/` by path."""
    spec = importlib.util.spec_from_file_location(name, LIB_DIR / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rig_select = _load("rig_select")

# Cards that clear every numeric floor and then die at vLLM init with CUDA
# error 804. A modded 48GB RTX 4090 reports compute capability 8.9 and 49140
# MiB — no number distinguishes it from a card that works.
KNOWN_BAD_CARDS = [
    "RTX 4090",
    "RTX 4090 48GB",  # the exact modded part that burned two rigs
    "RTX4090",
    "GeForce RTX 4090",
    "NVIDIA GeForce RTX 4090",
    "RTX 4090D",
    "RTX 3090",
    "RTX 3090 Ti",
    "RTX 5090",
    "RTX 2080",
    "GTX 1080 Ti",
]

# Professional / datacenter parts. Every one of these MUST survive the filter —
# the RTX 6000 Ada in particular demonstrably served this model on 2026-08-04,
# and its name starts with the same three letters as the cards above.
KNOWN_GOOD_CARDS = [
    "RTX 6000 Ada 48GB",
    "RTX A6000 48GB",
    "RTX PRO 6000 96GB",
    "RTX PRO 6000 CC 96GB",
    "RTX 5880 Ada",
    "Quadro RTX 8000",
    "L40S 48GB",
    "A100 SXM4 80GB",
    "H200 SXM5 141GB",
    "Tesla V100 16GB",
]


@pytest.mark.parametrize("card", KNOWN_BAD_CARDS)
def test_known_bad_consumer_card_names_are_refused(card: str) -> None:
    assert rig_select._is_denied_card(card) is True, (
        f"{card!r} is a consumer GeForce part that fails vLLM init with CUDA error "
        f"804 (the forward-compatibility layer is datacenter-only), and the card "
        f"filter admitted it"
    )


@pytest.mark.parametrize("card", KNOWN_GOOD_CARDS)
def test_professional_card_names_are_not_refused(card: str) -> None:
    assert rig_select._is_denied_card(card) is False, (
        f"{card!r} is a datacenter/workstation card we rent on purpose and the "
        f"filter refused it — an over-broad name pattern leaves nothing rentable"
    )


# ===========================================================================
_FAKE_SSH = r"""#!/usr/bin/env bash
# Fake ssh — never touches the network.
args=("$@"); n=${#args[@]}; last="${args[$((n-1))]}"
{ printf 'SSH_CALL:'; printf ' %q' "$@"; printf '\n'; } >> "$NE_TEST_SSH_LOG"
case "$last" in
  *ne-rig-probe*) cat > /dev/null; cat "$NE_TEST_DOCTOR_REPORT"; exit 0 ;;
  *ne-ready-*) echo "${last#echo }"; exit 0 ;;
  "echo ok") echo ok; exit 0 ;;
  *'cat > $HOME/ne-dl.sh'*)
    cat > "$NE_TEST_DOWNLOAD_SCRIPT"
    echo "WROTE_NE_DL_SH" >> "$NE_TEST_SSH_LOG"
    exit 0
    ;;
  *tmux*) echo launched; exit 0 ;;
  "bash -s") cat > "$NE_TEST_REMOTE_STDIN"; echo remote-ok; exit 0 ;;
  *) echo ""; exit 0 ;;
esac
"""

HEALTHY_REPORT = {
    "container_runtime": "docker",
    "vllm": "0.24.0",
    "gpus": "1",
    "gpu_names": "NVIDIA RTX 6000 Ada Generation",
    "driver_cuda": "12.8",
    "disk_free_gb": "200",
    "tun": "OK",
    "downlink_bytes": "100000000",  # 100 MB in 2s = 400 Mbps, above the 250 floor
    "downlink_secs": "2.0",
}


@pytest.fixture
def rig_env(tmp_path: Path):
    """PATH-stubbed environment for invoking a real adapter script."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(_FAKE_SSH)
    ssh.chmod(0o755)

    report_file = tmp_path / "doctor-report.txt"
    ssh_log = tmp_path / "ssh.log"
    download_script = tmp_path / "ne-dl.sh"
    remote_stdin = tmp_path / "remote-stdin.sh"
    state_root = tmp_path / "state"

    def make_env(**over: str) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["NE_TEST_DOCTOR_REPORT"] = str(report_file)
        env["NE_TEST_SSH_LOG"] = str(ssh_log)
        env["NE_TEST_DOWNLOAD_SCRIPT"] = str(download_script)
        env["NE_TEST_REMOTE_STDIN"] = str(remote_stdin)
        env["NE_RIG_STATE_ROOT"] = str(state_root)
        env["NE_OWNER_SECRETS"] = str(tmp_path / "secrets")
        env.update(over)
        return env

    def write_report(fields: dict[str, str]) -> None:
        report_file.write_text(
            "".join(f"NE_DOCTOR {k}={v}\n" for k, v in fields.items())
            + "NE_DOCTOR serving_10200=demo\nNE_DOCTOR serving_10100=NOT_SERVING\n"
        )

    def write_state(provider: str, payload: dict) -> Path:
        d = state_root / provider
        d.mkdir(parents=True, exist_ok=True)
        p = d / "instance.json"
        p.write_text(json.dumps(payload))
        return p

    return type(
        "RigEnv",
        (),
        {
            "make_env": staticmethod(make_env),
            "write_report": staticmethod(write_report),
            "write_state": staticmethod(write_state),
            "state_root": state_root,
            "ssh_log": ssh_log,
            "download_script": download_script,
            "remote_stdin": remote_stdin,
        },
    )


def _run_rig(script: Path, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(script), *args], env=env, capture_output=True, text=True, timeout=120
    )


def _write_secret(root: Path, provider: str, name: str, value: str) -> None:
    """A mode-600 credential file under the NE_OWNER_SECRETS root."""
    d = root / provider
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(value)
    p.chmod(0o600)


def _fake_vastai_bindir(
    tmp_path: Path, marker: Path, gpu_ram: int = 49140, offers: list | None = None
) -> Path:
    """A PATH-independent fake `vastai` at $NE_VAST_VENV/bin/vastai. It answers
    `search offers` with one offer that clears every client-side filter, records
    a `create instance` invocation by touching `marker`, and returns no
    new_contract so the run dies immediately after (no slow wait_ready)."""
    offer = {
        "id": 999888,
        "gpu_name": "L40S",
        "gpu_ram": gpu_ram,
        "compute_cap": 890,  # >= MIN_COMPUTE_CAP (890)
        "num_gpus": 1,
        "rentable": True,
        "verification": "verified",
        "disk_space": 100,  # >= --disk (80)
        "cuda_max_good": 12.8,  # >= MIN_DRIVER_CUDA (12.8)
        "direct_port_count": 4,  # >= 2
        "dph_total": 0.5,  # <= --price (1.0)
        "reliability2": 0.99,
    }
    venv_bin = tmp_path / (marker.name + "-venv") / "bin"
    venv_bin.mkdir(parents=True)
    vastai = venv_bin / "vastai"
    vastai.write_text(
        "#!/usr/bin/env bash\n"
        'cmd="${1:-}"; sub="${2:-}"\n'
        'case "$cmd $sub" in\n'
        "  'search offers') printf '%s' "
        + repr(json.dumps(offers if offers is not None else [offer]))
        + " ;;\n"
        f'  "create instance") : > {shlex.quote(str(marker))}; echo "{{}}" ;;\n'
        '  "show instance") echo \'{"actual_status":"running"}\' ;;\n'
        "  *) : ;;\n"
        "esac\n"
    )
    vastai.chmod(0o755)
    return venv_bin.parent


def _vast_provision(
    rig_env, tmp_path: Path, marker: Path, *args, gpu_ram: int = 49140, offers: list | None = None
):
    venv = _fake_vastai_bindir(tmp_path, marker, gpu_ram=gpu_ram, offers=offers)
    pub = tmp_path / "id.pub"
    pub.write_text("ssh-ed25519 AAAA test")
    env = rig_env.make_env(
        NE_VAST_VENV=str(venv),
        NE_VAST_STATE_DIR=str(tmp_path / "vast-state"),
        NE_SSH_PUBKEY=str(pub),
    )
    _write_secret(Path(env["NE_OWNER_SECRETS"]), "vast", "api-key", "fake-key")
    env["NE_VAST_KEY_FILE"] = str(Path(env["NE_OWNER_SECRETS"]) / "vast" / "api-key")
    proc = _run_rig(VAST_RIG, ["provision", *args], env)
    return proc.stdout + proc.stderr


def test_vast_provision_refuses_to_spend_without_the_flag_and_never_bills(
    rig_env, tmp_path: Path
) -> None:
    """Vast `provision` with a `--price` ceiling but no `--yes-spend` must refuse
    AND never invoke `vastai create instance`. Vast is the pre-existing adapter
    the spend gate was retrofitted onto in this PR; its gate had no proof either.

    A fake `vastai` yields one offer that clears every client-side filter, so the
    run reaches the gate (not the "no offers" path). Billing prevention is
    observed by a marker file the fake touches ONLY inside `create instance`: it
    must be absent after a refused run.

    Control (same fake, `--yes-spend`): `create instance` IS invoked — the marker
    appears — proving the offer is rentable and the gate is the only thing that
    stood between the ceiling and the rental.

    Mutation-verified: neutering the gate to `if false` turns the refusal leg RED
    — "refusing to spend" vanishes and the create marker appears without consent.
    """
    marker = tmp_path / "vast-create-called"

    out = _vast_provision(rig_env, tmp_path, marker, "--price", "1.0")
    assert "cheapest offer" in out, (
        f"the fake offer must clear selection so the run reaches the gate, not the "
        f"'no offers' path — got:\n{out}"
    )
    assert "refusing to spend" in out, (
        f"a --price ceiling without --yes-spend must be refused — got:\n{out}"
    )
    assert not marker.exists(), (
        "the gate must fire BEFORE `vastai create instance` — but the create marker "
        "was written, so the box was billed without consent"
    )

    # Control: WITH consent, the same offer reaches `create instance`.
    marker2 = tmp_path / "vast-create-called-2"
    out_yes = _vast_provision(rig_env, tmp_path, marker2, "--price", "1.0", "--yes-spend")
    assert "refusing to spend" not in out_yes, f"--yes-spend must clear the gate — got:\n{out_yes}"
    assert marker2.exists(), (
        f"control: with consent the run must reach `vastai create instance` — proving "
        f"the offer is rentable and the refusal above was the gate — got:\n{out_yes}"
    )


def _vast_offer(**over):
    """One fully-eligible Vast offer, overridable field by field."""
    base = {
        "id": 999888,
        "gpu_name": "L40S",
        "gpu_ram": 49140,
        "compute_cap": 890,
        "num_gpus": 1,
        "rentable": True,
        "verification": "verified",
        "disk_space": 100,
        "cuda_max_good": 12.8,
        "direct_port_count": 4,
        "dph_total": 0.5,
        "reliability2": 0.99,
    }
    base.update(over)
    return base


def test_vast_default_any_search_refuses_the_cheapest_modded_geforce(
    rig_env, tmp_path: Path
) -> None:
    """`--gpu any` must not let cheapest-first reach a consumer GeForce card.

    This is the hazard the default change introduced and it is a MONEY hazard,
    not a quality one. A modded RTX 4090 reports 49,140 MiB and compute_cap 890,
    so it clears BOTH numeric floors, and then fails `cudaGetDeviceCount()` with
    error 804 because the pinned image's forward-compat layer is datacenter-only.
    It is also the cheapest 48GB-class part in existence, so a cheapest-first
    selector picks it every time the caller does not narrow the name domain --
    which is exactly what the new `--gpu any` default stopped doing.

    Differential, because a test that only offers the bad card cannot tell
    "correctly refused" from "search is broken": the 4090 is CHEAPER and a valid
    professional card is dearer, so a selector that ignores the deny rule picks
    the 4090 on price alone.
    Both the DEFAULT (`any`) and an EXPLICIT `--gpu "RTX 4090 48GB"` are driven.
    The deny rule is unconditional in the selector today, but the named path is
    money-sensitive and was unpinned: a change that kept denial for `any` and
    waived it for an explicit name would have left the `any`-only test green
    and re-permitted renting the card.

    The assertions are on the `selected <card>` observable, NOT on the card
    name appearing anywhere in the output. The no-offer fallback PRINTS the
    rentable table including raw GPU names, so "the professional card is
    mentioned" is satisfied by a search that selected nothing at all -- the
    control has to name the selection or it is not a control.
    """
    # The BILLING boundary, earned rather than assumed: consent is GIVEN
    # (--yes-spend) and the ONLY offer is the denied card. A correct selector
    # finds nothing eligible and never calls `create instance`; delete the deny
    # rule and this exact run rents the 4090. The legs above cannot make this
    # claim because they stop at the spend gate first.
    billing_marker = tmp_path / "vast-consented-bad-only"
    billing_out = _vast_provision(
        rig_env,
        tmp_path,
        billing_marker,
        "--price",
        "1.0",
        "--yes-spend",
        offers=[_vast_offer(id=4090, gpu_name="RTX 4090 48GB", dph_total=0.30)],
    )
    assert not billing_marker.exists(), (
        "a denied card reached `vastai create instance` WITH consent given — that "
        f"is a rental of hardware that cannot run the model. Got:\n{billing_out}"
    )

    for gpu_arg, label in (((), "any"), (("--gpu", "RTX 4090 48GB"), "named-4090")):
        marker = tmp_path / f"vast-{label}-geforce"
        out = _vast_provision(
            rig_env,
            tmp_path,
            marker,
            *gpu_arg,
            "--price",
            "1.0",
            offers=[
                _vast_offer(id=4090, gpu_name="RTX 4090 48GB", dph_total=0.30),
                _vast_offer(id=6000, gpu_name="RTX 6000 Ada", dph_total=0.90),
            ],
        )

        assert "selected RTX 4090" not in out, (
            f"[{label}] cheapest-first SELECTED a modded GeForce. It clears both "
            f"numeric floors and then fails CUDA init 804. Got:\n{out}"
        )
        # NOTE: no marker assertion here. These legs run WITHOUT --yes-spend, so
        # the run dies at the spend gate before `create instance` is ever
        # reached — the marker would be absent even with the deny rule removed.
        # Claiming it as a billing boundary would be an assertion the setup
        # cannot support. The consented leg below is where that claim is earned.

    # CONTROL, on the default path: the dearer professional card is genuinely
    # SELECTED. Without this, every assertion above is satisfied by a selector
    # that rejects everything.
    control_marker = tmp_path / "vast-any-control"
    control_out = _vast_provision(
        rig_env,
        tmp_path,
        control_marker,
        "--price",
        "1.0",
        offers=[
            _vast_offer(id=4090, gpu_name="RTX 4090 48GB", dph_total=0.30),
            _vast_offer(id=6000, gpu_name="RTX 6000 Ada", dph_total=0.90),
        ],
    )
    assert "selected RTX 6000 Ada" in control_out, (
        "control: the dearer professional card must be the SELECTED offer, else "
        f"the refusals above prove only that the search returns nothing. Got:\n{control_out}"
    )


def test_vast_gpu_ram_floor_applies_to_named_and_any_searches(rig_env, tmp_path: Path) -> None:
    """The floor rejects at the REAL boundary, not at a number nothing ships.

    The earlier fixture rejected a 24,564 MiB card — a value so far below
    `MIN_GPU_RAM_MB` that it would have been rejected by any floor at all,
    including the 46,000 one this PR replaced. It therefore could not
    distinguish the new floor from the old, and could not see the case that
    actually matters: **a real L40S reports ~46,068 MiB**, which the old floor
    admitted and the new one must reject, because full-native-context bf16 KV
    at 262,144 is not proven on a 46GB card.

    Driving the boundary is also what makes the default-`--gpu` change
    visible: while the default was `L40S`, this same floor made the plain
    `provision` command unsatisfiable against real inventory.
    """
    for gpu_arg, label in (((), "any"), (("--gpu", "L40S"), "L40S")):
        # The card the old floor let through. This is the discriminating case.
        real_l40s = tmp_path / f"vast-{label}-real-l40s"
        l40s_out = _vast_provision(
            rig_env, tmp_path, real_l40s, *gpu_arg, "--price", "1.0", gpu_ram=46068
        )
        assert f"no {label} offers" in l40s_out, (
            f"a real L40S (46,068 MiB) was NOT rejected under {label!r} — the floor is "
            f"not the configured 48,000, or it is waived for this search. Got:\n{l40s_out}"
        )
        assert not real_l40s.exists(), "a below-floor offer must never reach `create instance`"

        # CONTROL: the live-proven card clears the same search, so the rejection
        # above is the floor doing its job rather than the fixture never renting.
        ada = tmp_path / f"vast-{label}-ada"
        ada_out = _vast_provision(rig_env, tmp_path, ada, *gpu_arg, "--price", "1.0", gpu_ram=49140)
        assert f"no {label} offers" not in ada_out, (
            f"control: the live-proven 49,140 MiB card must clear the floor under "
            f"{label!r}, else this test would pass with the search broken. Got:\n{ada_out}"
        )
