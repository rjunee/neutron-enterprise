# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused, no-Docker tests for the read-only demo-host preflight."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
import yaml
from click.testing import CliRunner

from ne.cli import main
from ne.cli.demo import live
from ne.cli.up import NE_APP_IMAGE

host = import_module("ne.cli.demo.preflight")

_CURRENT_ID = "sha256:" + "1" * 64
_ROLLBACK_ID = "sha256:" + "2" * 64
_ROLLBACK_TAG = "ne-app:demo-rollback-20260820"
_GPU_RERANK = "http://8.8.8.8:1837"


def _completed(
    args: tuple[str, ...], *, returncode: int = 0, stdout: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker", *args], returncode, stdout=stdout, stderr="")


def _write_meminfo(
    path: Path, *, available_gib: int, swap_total_gib: int, swap_free_gib: int
) -> None:
    path.write_text(
        "\n".join(
            (
                f"MemAvailable: {available_gib * 1024**2} kB",
                f"SwapTotal: {swap_total_gib * 1024**2} kB",
                f"SwapFree: {swap_free_gib * 1024**2} kB",
            )
        ),
        encoding="utf-8",
    )


def _stage_overlay(
    workdir: Path, *, live_writeback: bool = False, gpu_rerank: str | None = None
) -> Path:
    seed_dir = workdir / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    staging: live._LiveWritebackStaging | None = None
    if live_writeback:
        manifest_dir = workdir / "manifest"
        manifest_dir.mkdir()
        (manifest_dir / "salesforce.json").write_text('{"records": {}}', encoding="utf-8")
        env_path = (workdir / "overlay" / "writeback.env").resolve()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("NE_TEST_VALUE='redacted'\n", encoding="utf-8")
        env_path.chmod(0o600)
        (workdir / live._WRITEBACK_MARKER).write_text(
            json.dumps({"mode": "live-crm", "manifest_dir": str(manifest_dir)}),
            encoding="utf-8",
        )
        staging = live._LiveWritebackStaging(env_file=env_path, pem_mounts=())
    return live._write_demo_overlay(
        workdir,
        seed_dir,
        live_writeback=staging,
        gpu_rerank=gpu_rerank,
    )


def _patch_host_headroom(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(host, "_docker_data_root", lambda: tmp_path)
    monkeypatch.setattr(
        host.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=30 * 1024**3),
    )
    monkeypatch.setattr(
        host,
        "_read_meminfo",
        lambda: {
            "MemAvailable": 12 * 1024**3,
            "SwapTotal": 16 * 1024**3,
            "SwapFree": 12 * 1024**3,
        },
    )


def _write_dossier(
    workdir: Path,
    *,
    finished: datetime,
    modified: datetime,
    failed: bool = False,
    audit_valid: bool = True,
    audit_checked: int | bool = 7,
) -> Path:
    status = "❌ FAILED" if failed else "✅ OK"
    path = workdir / "DEMO-DOSSIER.md"
    path.write_text(
        "\n".join(
            (
                "# Neutron Enterprise — Full-Ladder Demo Dossier",
                "",
                f"- Generated: {(finished - timedelta(minutes=1)).isoformat()} → "
                f"{finished.isoformat()}",
                "",
                f"### 1. Governed turn — {status}",
                "",
                "## Audit-chain verification",
                "",
                "```json",
                json.dumps({"valid": audit_valid, "checked": audit_checked}),
                "```",
            )
        ),
        encoding="utf-8",
    )
    os.utime(path, (modified.timestamp(), modified.timestamp()))
    return path


def test_cli_help_exposes_read_only_preflight() -> None:
    result = CliRunner().invoke(main, ["demo", "preflight", "--help"])
    assert result.exit_code == 0, result.output
    assert "--min-memory-available-gib" in result.output
    assert "--max-swap-used-percent" in result.output
    assert "--expected-rollback-id" in result.output
    assert "fresh-host" in result.output
    assert "without mutations" in " ".join(result.output.split())


def test_fresh_host_passes_only_with_no_containers_or_public_services(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutation target: removing either isolation refusal must turn this RED."""
    _patch_host_headroom(monkeypatch, tmp_path)
    monkeypatch.setattr(host.os, "geteuid", lambda: 1000)
    docker_calls: list[tuple[str, ...]] = []
    host_calls: list[tuple[str, ...]] = []

    def _docker(*args: str) -> subprocess.CompletedProcess[str]:
        docker_calls.append(args)
        assert args == ("ps", "--all", "--format", "{{ .ID }}")
        return _completed(args)

    def _host(*args: str) -> subprocess.CompletedProcess[str]:
        host_calls.append(args)
        if args == ("sudo", "-n", "true"):
            return _completed(args)
        if args == ("ss", "-H", "-ltnp"):
            return _completed(
                args,
                stdout="LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n",
            )
        assert args == ("sudo", "-n", "ss", "-H", "-ltnp")
        return subprocess.CompletedProcess(
            list(args),
            0,
            'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))\n'
            'LISTEN 0 128 127.0.0.1:2019 0.0.0.0:* users:(("local",pid=2,fd=3))\n',
            "",
        )

    monkeypatch.setattr(host, "_docker", _docker)
    monkeypatch.setattr(host, "_host", _host)
    result = CliRunner().invoke(
        main,
        [
            "demo",
            "--workdir",
            str(tmp_path),
            "preflight",
            "--phase",
            "fresh-host",
            "--min-memory-available-gib",
            "8",
            "--max-swap-used-percent",
            "50",
            "--writeback-mode",
            "inert",
            "--rerank-mode",
            "cpu",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dedicated isolation" in result.output
    assert docker_calls == [("ps", "--all", "--format", "{{ .ID }}")]
    assert host_calls == [
        ("sudo", "-n", "true"),
        ("sudo", "-n", "ss", "-H", "-ltnp"),
    ]


@pytest.mark.parametrize(
    "listeners",
    (
        "LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* "
        'users:(("systemd",pid=1,fd=86),("sshd",pid=832,fd=3))\n',
        'LISTEN 0 128 [::]:22 [::]:* users:(("sshd",pid=832,fd=3),("systemd",pid=1,fd=86))\n',
    ),
)
def test_nonroot_fresh_host_accepts_exact_socket_activated_ssh_owners(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    listeners: str,
) -> None:
    _patch_host_headroom(monkeypatch, tmp_path)
    monkeypatch.setattr(host.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(host, "_docker", lambda *args: _completed(args))

    def _host(*args: str) -> subprocess.CompletedProcess[str]:
        if args == ("sudo", "-n", "true"):
            return _completed(args)
        assert args == ("sudo", "-n", "ss", "-H", "-ltnp")
        return _completed(args, stdout=listeners)

    monkeypatch.setattr(host, "_host", _host)

    host.run_host_preflight(
        tmp_path,
        phase="fresh-host",
        min_disk_free_gib=10,
        min_memory_available_gib=8,
        max_swap_used_percent=50,
        rollback_image=None,
        expected_rollback_id=None,
        writeback_mode="inert",
        rerank_mode="cpu",
        gpu_rerank=None,
    )


@pytest.mark.parametrize(
    "listeners",
    (
        "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n",
        "LISTEN 0 128 0.0.0.0:22 0.0.0.0:* "
        'users:(("sshd",pid=832,fd=3),("systemd",pid=1,fd=86),("nginx",pid=9,fd=4))\n',
        'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("systemd",pid=1,fd=86))\n',
        'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("systemd",pid=22,fd=86))\n',
        "LISTEN 0 128 0.0.0.0:443 0.0.0.0:* "
        'users:(("sshd",pid=832,fd=3),("systemd",pid=1,fd=86))\n',
    ),
)
def test_nonroot_fresh_host_rejects_hidden_or_foreign_listener_owners(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    listeners: str,
) -> None:
    _patch_host_headroom(monkeypatch, tmp_path)
    monkeypatch.setattr(host.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(host, "_docker", lambda *args: _completed(args))
    monkeypatch.setattr(
        host,
        "_host",
        lambda *args: (
            _completed(args)
            if args == ("sudo", "-n", "true")
            else _completed(args, stdout=listeners)
        ),
    )

    with pytest.raises(click.ClickException, match="unrelated public TCP listeners"):
        host._fresh_host_isolation()


def test_nonroot_fresh_host_refuses_unavailable_passwordless_sudo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(host, "_docker", lambda *args: _completed(args))
    calls: list[tuple[str, ...]] = []

    def _host(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(list(args), 1, "", "sudo unavailable")

    monkeypatch.setattr(host, "_host", _host)

    with pytest.raises(click.ClickException, match="passwordless sudo is required"):
        host._fresh_host_isolation()
    assert calls == [("sudo", "-n", "true")]


def test_root_fresh_host_reads_process_owners_without_sudo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host.os, "geteuid", lambda: 0)
    monkeypatch.setattr(host, "_docker", lambda *args: _completed(args))
    calls: list[tuple[str, ...]] = []

    def _host(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _completed(
            args,
            stdout='LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=832,fd=3))\n',
        )

    monkeypatch.setattr(host, "_host", _host)

    host._fresh_host_isolation()
    assert calls == [("ss", "-H", "-ltnp")]


@pytest.mark.parametrize(
    ("containers", "listeners", "message"),
    (
        ("abc123\n", "", "already contains Docker containers"),
        ("", 'LISTEN 0 128 0.0.0.0:443 0.0.0.0:* users:(("caddy"))\n', "unrelated"),
    ),
)
def test_fresh_host_refuses_shared_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    containers: str,
    listeners: str,
    message: str,
) -> None:
    _patch_host_headroom(monkeypatch, tmp_path)
    monkeypatch.setattr(
        host,
        "_docker",
        lambda *args: _completed(args, stdout=containers),
    )
    monkeypatch.setattr(
        host,
        "_host",
        lambda *args: subprocess.CompletedProcess(list(args), 0, listeners, ""),
    )
    with pytest.raises(click.ClickException, match=message):
        host.run_host_preflight(
            tmp_path,
            phase="fresh-host",
            min_disk_free_gib=10,
            min_memory_available_gib=8,
            max_swap_used_percent=50,
            rollback_image=None,
            expected_rollback_id=None,
            writeback_mode="inert",
            rerank_mode="cpu",
            gpu_rerank=None,
        )


def test_predeploy_passes_and_uses_only_read_only_docker_queries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage_overlay(tmp_path)
    _patch_host_headroom(monkeypatch, tmp_path)
    calls: list[tuple[str, ...]] = []

    def _docker(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert args[:2] == ("image", "inspect")
        return _completed(args, stdout=_CURRENT_ID + "\n")

    monkeypatch.setattr(host, "_docker", _docker)
    result = CliRunner().invoke(
        main,
        [
            "demo",
            "--workdir",
            str(tmp_path),
            "preflight",
            "--phase",
            "pre-deploy",
            "--min-memory-available-gib",
            "8",
            "--max-swap-used-percent",
            "50",
            "--rollback-image",
            _ROLLBACK_TAG,
            "--writeback-mode",
            "inert",
            "--rerank-mode",
            "cpu",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "PASS (read-only)" in result.output
    assert _CURRENT_ID in result.output
    assert "required re-up preservation args:" in result.output
    assert calls == [
        ("image", "inspect", NE_APP_IMAGE, "--format", "{{ .Id }}"),
        ("image", "inspect", _ROLLBACK_TAG, "--format", "{{ .Id }}"),
    ]


def test_data_root_query_is_read_only_and_fails_closed_on_unknown_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, ...]] = []

    def _docker(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _completed(args, stdout=str(tmp_path) + "\n")

    monkeypatch.setattr(host, "_docker", _docker)
    assert host._docker_data_root() == tmp_path
    assert calls == [("info", "--format", "{{ .DockerRootDir }}")]

    monkeypatch.setattr(host, "_docker", lambda *args: _completed(args, stdout="relative\n"))
    with pytest.raises(click.ClickException, match="no absolute"):
        host._docker_data_root()


def test_disk_and_memory_thresholds_fail_on_the_measured_boundaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(host, "_docker_data_root", lambda: tmp_path)
    monkeypatch.setattr(
        host.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=1 * 1024**3),
    )
    with pytest.raises(click.ClickException, match="insufficient disk"):
        host._disk_headroom(10 * 1024**3)

    monkeypatch.setattr(
        host,
        "_read_meminfo",
        lambda: {
            "MemAvailable": 3 * 1024**3,
            "SwapTotal": 16 * 1024**3,
            "SwapFree": 0,
        },
    )
    with pytest.raises(click.ClickException, match="insufficient available memory"):
        host._memory_headroom(4 * 1024**3, 100.0)
    with pytest.raises(click.ClickException, match="swap pressure too high"):
        host._memory_headroom(3 * 1024**3, 99.9)


def test_meminfo_fails_closed_on_missing_or_inconsistent_counters(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed"
    malformed.write_text("MemAvailable: 8 kB\nSwapTotal: 16 kB\n", encoding="utf-8")
    with pytest.raises(click.ClickException, match="SwapFree"):
        host._read_meminfo(malformed)

    inconsistent = tmp_path / "inconsistent"
    _write_meminfo(inconsistent, available_gib=8, swap_total_gib=2, swap_free_gib=3)
    with pytest.raises(click.ClickException, match="inconsistent"):
        host._read_meminfo(inconsistent)


@pytest.mark.parametrize(
    "image",
    (
        "ne-app",
        "ne-app:latest",
        NE_APP_IMAGE,
        "ne-app@sha256:" + "a" * 64,
    ),
)
def test_rollback_requires_a_dedicated_non_latest_tag(image: str) -> None:
    with pytest.raises(click.UsageError):
        host._validate_rollback_tag(image)


@pytest.mark.parametrize(
    ("option", "value"),
    (("--min-memory-available-gib", "0"), ("--max-swap-used-percent", "100")),
)
def test_cli_refuses_thresholds_that_disable_memory_or_full_swap_gate(
    option: str, value: str
) -> None:
    args = [
        "demo",
        "preflight",
        "--phase",
        "pre-deploy",
        "--min-memory-available-gib",
        "1",
        "--max-swap-used-percent",
        "99",
        "--rollback-image",
        _ROLLBACK_TAG,
        "--writeback-mode",
        "inert",
        "--rerank-mode",
        "cpu",
    ]
    index = args.index(option)
    args[index + 1] = value
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 2


def test_predeploy_rejects_rollback_tag_not_pointing_at_current_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_host_headroom(monkeypatch, tmp_path)
    monkeypatch.setattr(
        host,
        "_image_id",
        lambda image: _CURRENT_ID if image == NE_APP_IMAGE else _ROLLBACK_ID,
    )
    monkeypatch.setattr(host, "_overlay_preservation", lambda *a, **k: [])
    with pytest.raises(click.ClickException, match="does not point"):
        host.run_host_preflight(
            tmp_path,
            phase="pre-deploy",
            min_disk_free_gib=10,
            min_memory_available_gib=8,
            max_swap_used_percent=50,
            rollback_image=_ROLLBACK_TAG,
            expected_rollback_id=None,
            writeback_mode="inert",
            rerank_mode="cpu",
            gpu_rerank=None,
        )


def test_live_writeback_and_gpu_rerank_round_trip_through_production_overlay(
    tmp_path: Path,
) -> None:
    _stage_overlay(tmp_path, live_writeback=True, gpu_rerank=_GPU_RERANK + "/")
    args = host._overlay_preservation(
        tmp_path,
        writeback_mode="live-crm",
        rerank_mode="gpu",
        gpu_rerank=_GPU_RERANK,
    )
    assert args == [
        "--live-writeback",
        "--seed-manifest-dir",
        str(tmp_path / "manifest"),
        "--gpu-rerank",
        _GPU_RERANK,
    ]


def test_writeback_modes_fail_closed_on_artifact_or_overlay_disagreement(tmp_path: Path) -> None:
    _stage_overlay(tmp_path, live_writeback=True)
    (tmp_path / "overlay" / "writeback.env").chmod(0o644)
    with pytest.raises(click.ClickException, match="0600"):
        host._overlay_preservation(
            tmp_path, writeback_mode="live-crm", rerank_mode="cpu", gpu_rerank=None
        )
    with pytest.raises(click.ClickException, match="artifacts remain"):
        host._overlay_preservation(
            tmp_path, writeback_mode="inert", rerank_mode="cpu", gpu_rerank=None
        )


def test_gpu_rerank_requires_exact_both_service_parity(tmp_path: Path) -> None:
    overlay = _stage_overlay(tmp_path, gpu_rerank=_GPU_RERANK)
    text = overlay.read_text(encoding="utf-8")
    expected = f"      NE_API_TEI_RERANK_URL: {json.dumps(_GPU_RERANK)}"
    before, final = text.rsplit(expected, 1)
    overlay.write_text(
        before + final.replace(_GPU_RERANK, "http://wrong.invalid", 1), encoding="utf-8"
    )
    with pytest.raises(click.ClickException, match="both services"):
        host._overlay_preservation(
            tmp_path,
            writeback_mode="inert",
            rerank_mode="gpu",
            gpu_rerank=_GPU_RERANK,
        )


def test_cpu_rerank_refuses_a_stale_gpu_overlay(tmp_path: Path) -> None:
    _stage_overlay(tmp_path, gpu_rerank=_GPU_RERANK)
    with pytest.raises(click.ClickException, match="GPU rerank state"):
        host._overlay_preservation(
            tmp_path, writeback_mode="inert", rerank_mode="cpu", gpu_rerank=None
        )


@pytest.mark.parametrize("writeback_mode", ("inert", "live-crm"))
@pytest.mark.parametrize("rerank_mode", ("cpu", "gpu"))
def test_runtime_env_expectations_agree_with_the_deployed_overlay(
    tmp_path: Path, writeback_mode: str, rerank_mode: str
) -> None:
    """Preflight's RUNTIME half must mirror the overlay ``ne demo up`` actually writes.

    The two halves of the post-deploy gate drifted once already: NE-ITEM1-INERT-PARK
    made ``_write_demo_overlay`` KEEP ``NE_API_DEMO_WRITEBACK_TOOL`` in live mode and
    ``_check_writeback`` was updated to match, but ``_runtime_env_expectations`` still
    listed the key as forbidden — so every ``--live-writeback`` post-deploy preflight
    died at ``running ne-api unexpectedly carries ...``. No test caught it because
    every other caller passes ``writeback_mode="inert"``.

    Mutation target: restore ``absent.add("NE_API_DEMO_WRITEBACK_TOOL")`` in the
    live-crm branch and the ``live-crm`` parameterisations turn RED on the "declared
    absent but the overlay sets it" assertion.
    """
    gpu_rerank = _GPU_RERANK if rerank_mode == "gpu" else None
    overlay = _stage_overlay(
        tmp_path, live_writeback=writeback_mode == "live-crm", gpu_rerank=gpu_rerank
    )
    document = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    expected, absent = host._runtime_env_expectations(
        writeback_mode=writeback_mode, rerank_mode=rerank_mode, gpu_rerank=gpu_rerank
    )
    assert expected
    # Both app services freeze the same config hash, so both must agree (Q5).
    for service in ("ne-api", "ne-worker"):
        environment = document["services"][service]["environment"]
        for key, value in expected.items():
            assert environment.get(key) == value, (
                f"{service} must carry {key}={value!r} but the overlay has {environment.get(key)!r}"
            )
        for key in absent:
            assert key not in environment, (
                f"preflight declares {key} absent at runtime but the overlay sets it "
                f"on {service} — the post-deploy gate would refuse its own deploy"
            )


def test_running_container_is_unique_and_uses_current_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = datetime(2026, 8, 19, 3, 0, tzinfo=UTC)
    calls: list[tuple[str, ...]] = []

    def _docker(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[0] == "ps":
            return _completed(args, stdout="container-1\n")
        if ".State.Running" in args[-1]:
            return _completed(args, stdout=f"true\n{started.isoformat()}\n{_CURRENT_ID}\n")
        if "NE_API_DEMO_WRITEBACK_TOOL=true" in args[-1]:
            return _completed(args, stdout="match")
        return _completed(args)

    monkeypatch.setattr(host, "_docker", _docker)
    assert host._running_api_container(
        _CURRENT_ID,
        expected_env={"NE_API_DEMO_WRITEBACK_TOOL": "true"},
        absent_env={"NE_API_WRITE_ACTIONS_ENABLED"},
    ) == ("container-1", started)
    assert all("{{json .}}" not in arg for call in calls for arg in call)

    monkeypatch.setattr(host, "_docker", lambda *args: _completed(args, stdout="one\ntwo\n"))
    with pytest.raises(click.ClickException, match="exactly one"):
        host._running_api_container(_CURRENT_ID, expected_env={}, absent_env=set())


def test_running_container_rejects_an_old_image(monkeypatch: pytest.MonkeyPatch) -> None:
    def _docker(*args: str) -> subprocess.CompletedProcess[str]:
        if args[0] == "ps":
            return _completed(args, stdout="container-1\n")
        return _completed(args, stdout=f"true\n2026-08-19T03:00:00Z\n{_ROLLBACK_ID}\n")

    monkeypatch.setattr(host, "_docker", _docker)
    with pytest.raises(click.ClickException, match="does not use"):
        host._running_api_container(_CURRENT_ID, expected_env={}, absent_env=set())


def test_running_container_rejects_overlay_state_not_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _docker(*args: str) -> subprocess.CompletedProcess[str]:
        if args[0] == "ps":
            return _completed(args, stdout="container-1\n")
        if ".State.Running" in args[-1]:
            return _completed(args, stdout=f"true\n2026-08-19T03:00:00Z\n{_CURRENT_ID}\n")
        return _completed(args)

    monkeypatch.setattr(host, "_docker", _docker)
    with pytest.raises(click.ClickException, match="NE_API_RERANK_BACKEND"):
        host._running_api_container(
            _CURRENT_ID,
            expected_env={"NE_API_RERANK_BACKEND": "vllm"},
            absent_env=set(),
        )


def test_rehearsal_accepts_only_fresh_successful_valid_audit_evidence(tmp_path: Path) -> None:
    started = datetime(2026, 8, 19, 3, 0, tzinfo=UTC)
    finished = started + timedelta(minutes=4)
    _write_dossier(tmp_path, finished=finished, modified=finished)
    assert host._rehearsal(tmp_path, container_started=started) == finished


def test_rehearsal_refuses_dossier_older_than_running_container(tmp_path: Path) -> None:
    """Mutation target: bypassing either freshness comparison must turn this RED."""
    started = datetime(2026, 8, 19, 3, 0, tzinfo=UTC)
    old = started - timedelta(minutes=1)
    _write_dossier(tmp_path, finished=old, modified=old)
    with pytest.raises(click.ClickException, match="predates"):
        host._rehearsal(tmp_path, container_started=started)


@pytest.mark.parametrize(
    ("failed", "audit_valid", "audit_checked", "message"),
    (
        (True, True, 7, "contains a failure"),
        (False, False, 7, "valid audit chain"),
        (False, True, 0, "zero records"),
        (False, True, True, "readable valid audit chain"),
    ),
)
def test_rehearsal_refuses_failed_or_invalid_evidence(
    tmp_path: Path, failed: bool, audit_valid: bool, audit_checked: int | bool, message: str
) -> None:
    started = datetime(2026, 8, 19, 3, 0, tzinfo=UTC)
    finished = started + timedelta(minutes=1)
    _write_dossier(
        tmp_path,
        finished=finished,
        modified=finished,
        failed=failed,
        audit_valid=audit_valid,
        audit_checked=audit_checked,
    )
    with pytest.raises(click.ClickException, match=message):
        host._rehearsal(tmp_path, container_started=started)


def test_postdeploy_requires_pinned_rollback_id_and_detects_tag_movement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_host_headroom(monkeypatch, tmp_path)
    monkeypatch.setattr(
        host,
        "_image_id",
        lambda image: _CURRENT_ID if image == NE_APP_IMAGE else _ROLLBACK_ID,
    )
    monkeypatch.setattr(host, "_overlay_preservation", lambda *a, **k: [])
    with pytest.raises(click.UsageError, match="requires the exact"):
        host.run_host_preflight(
            tmp_path,
            phase="post-deploy",
            min_disk_free_gib=10,
            min_memory_available_gib=8,
            max_swap_used_percent=50,
            rollback_image=_ROLLBACK_TAG,
            expected_rollback_id=None,
            writeback_mode="inert",
            rerank_mode="cpu",
            gpu_rerank=None,
        )
    with pytest.raises(click.ClickException, match="tag moved"):
        host.run_host_preflight(
            tmp_path,
            phase="post-deploy",
            min_disk_free_gib=10,
            min_memory_available_gib=8,
            max_swap_used_percent=50,
            rollback_image=_ROLLBACK_TAG,
            expected_rollback_id=_CURRENT_ID,
            writeback_mode="inert",
            rerank_mode="cpu",
            gpu_rerank=None,
        )


def test_postdeploy_complete_gate_binds_source_container_and_rehearsal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_host_headroom(monkeypatch, tmp_path)
    monkeypatch.setattr(
        host,
        "_image_id",
        lambda image: _CURRENT_ID if image == NE_APP_IMAGE else _ROLLBACK_ID,
    )
    monkeypatch.setattr(host, "_overlay_preservation", lambda *a, **k: [])
    monkeypatch.setattr(host, "_current_source_matches_image", lambda: True)
    started = datetime(2026, 8, 19, 3, 0, tzinfo=UTC)
    finished = started + timedelta(minutes=1)
    monkeypatch.setattr(
        host,
        "_running_api_container",
        lambda image_id, expected_env, absent_env: ("api-1", started),
    )
    monkeypatch.setattr(host, "_rehearsal", lambda workdir, container_started: finished)
    host.run_host_preflight(
        tmp_path,
        phase="post-deploy",
        min_disk_free_gib=10,
        min_memory_available_gib=8,
        max_swap_used_percent=50,
        rollback_image=_ROLLBACK_TAG,
        expected_rollback_id=_ROLLBACK_ID,
        writeback_mode="inert",
        rerank_mode="cpu",
        gpu_rerank=None,
    )

    monkeypatch.setattr(host, "_current_source_matches_image", lambda: False)
    with pytest.raises(click.ClickException, match="freeze is unproven"):
        host.run_host_preflight(
            tmp_path,
            phase="post-deploy",
            min_disk_free_gib=10,
            min_memory_available_gib=8,
            max_swap_used_percent=50,
            rollback_image=_ROLLBACK_TAG,
            expected_rollback_id=_ROLLBACK_ID,
            writeback_mode="inert",
            rerank_mode="cpu",
            gpu_rerank=None,
        )
