# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO CLI wiring — sanctioned seam mocks (the test_up_cli pattern).

The docker / keycloak / model-download / stack seams are monkeypatched (CI has no
docker daemon — orchestrator-granted exception); the bundle promote + secret
seeding run FOR REAL against a throwaway plain SecretStore.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from ne.cli import main
from ne.cli.demo import live
from ne.cli.pilotcfg import REQUIRED_SECRETS, resolve_required_secrets


def test_help_exits_zero() -> None:
    runner = CliRunner()
    assert runner.invoke(main, ["demo", "--help"]).exit_code == 0
    for sub in ("up", "run", "down"):
        assert runner.invoke(main, ["demo", sub, "--help"]).exit_code == 0


def test_bare_demo_runs_up_then_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(live, "demo_up", lambda *a, **k: calls.append("up"))
    monkeypatch.setattr(live, "demo_run", lambda *a, **k: calls.append("run"))
    result = CliRunner().invoke(main, ["demo", "--workdir", str(tmp_path / "wd")])
    assert result.exit_code == 0, result.output
    assert calls == ["up", "run"]


def test_invalid_frontier_env_value_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """NE-BUNDLE-GATEWAY-CONFIG: the fail-loud instinct is KEPT, its target
    CHANGED — an unroutable NE_DEMO_FRONTIER value (e.g. the old is_flag '1')
    now fails via click's Choice validation, still with no silent path."""
    monkeypatch.setenv("NE_DEMO_FRONTIER", "1")
    result = CliRunner().invoke(main, ["demo", "run"])
    assert result.exit_code != 0
    assert "anthropic" in result.output
    assert "openai" in result.output


def test_frontier_missing_key_fails_loud_before_docker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing provider key aborts BEFORE any docker call — `_preflight_docker`
    (which shells out to the real `docker` binary/daemon) must NEVER run, and the
    bring-up seam recorder stays empty, never a silent degrade to the open-weight
    lane.

    Argus round-3 P2 (C1): `_preflight_docker` used to run BEFORE `_stage_frontier`
    validated the key, so on a host without Docker the missing-key case surfaced a
    Docker error instead of the documented "fails loud before any docker call"
    message. Asserting the recorder here (not a no-op stub) is the anti-vacuous
    check: reverting the ordering in live.py turns this assertion red even though
    the exit-code/message assertions below would still pass.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    preflight_calls: list[None] = []
    bring_up_calls: list[dict] = []
    monkeypatch.setattr(live, "_preflight_docker", lambda: preflight_calls.append(None))
    monkeypatch.setattr(live, "_image_up_to_date", lambda root: True)
    monkeypatch.setattr(live, "_download_model", lambda models_dir: None)
    monkeypatch.setattr(
        live, "_stage_tei_cache", lambda workdir, onnx_artifact: workdir / "tei-cache"
    )
    monkeypatch.setattr(live, "_stage_demo_seed", lambda workdir: workdir / "seed")
    monkeypatch.setattr(
        live,
        "_bring_up_stack",
        lambda bundle, models_dir, overlay_path, https_port=443: bring_up_calls.append(
            {"bundle": bundle}
        ),
    )
    monkeypatch.setattr(live, "_provision_keycloak", lambda bundle, hostname, scenario, **k: bundle)
    monkeypatch.setattr(live, "_seed_corpus", lambda workdir, bundle, corpus_dir: None)

    result = CliRunner().invoke(
        main, ["demo", "--workdir", str(tmp_path / "wd"), "up", "--frontier", "anthropic"]
    )
    assert result.exit_code != 0, result.output
    assert "ANTHROPIC_API_KEY" in result.output
    assert bring_up_calls == []
    assert preflight_calls == [], (
        "_preflight_docker ran despite the missing frontier key — the Docker "
        "check must not run before the key is validated (Argus P2 C1)"
    )


def test_frontier_stages_env_and_reverts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    overlay_calls: list[dict] = []
    monkeypatch.setattr(live, "_preflight_docker", lambda: None)
    monkeypatch.setattr(live, "_image_up_to_date", lambda root: True)
    monkeypatch.setattr(live, "_download_model", lambda models_dir: None)
    monkeypatch.setattr(
        live, "_stage_tei_cache", lambda workdir, onnx_artifact: workdir / "tei-cache"
    )
    monkeypatch.setattr(live, "_stage_demo_seed", lambda workdir: workdir / "seed")

    def _overlay_stub(
        workdir: Path,
        seed_dir: Path,
        https_port: int = 443,
        external_url: str | None = None,
        gpu_vllm: str | None = None,
        tei_cache_dir: Path | None = None,
        live_writeback: live._LiveWritebackStaging | None = None,
        frontier: live._FrontierStaging | None = None,
    ) -> Path:
        overlay_calls.append({"frontier": frontier})
        return workdir / "overlay.yaml"

    monkeypatch.setattr(live, "_write_demo_overlay", _overlay_stub)
    monkeypatch.setattr(live, "_bring_up_stack", lambda *a, **k: None)
    monkeypatch.setattr(live, "_provision_keycloak", lambda bundle, hostname, scenario, **k: bundle)
    monkeypatch.setattr(live, "_seed_corpus", lambda workdir, bundle, corpus_dir: None)

    workdir = tmp_path / "wd"
    live.demo_up(
        workdir, hostname="localhost", seed=None, skip_image_build=False, frontier="anthropic"
    )

    env_file = workdir / "overlay" / "frontier.env"
    assert env_file.is_file()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "ANTHROPIC_API_KEY='test-anthropic-key'" in env_file.read_text(encoding="utf-8")
    marker = json.loads((workdir / live._FRONTIER_MARKER).read_text(encoding="utf-8"))
    assert marker == {"provider": "anthropic"}
    staged = overlay_calls[0]["frontier"]
    assert isinstance(staged, live._FrontierStaging)
    assert staged.provider == "anthropic"
    assert staged.env_file == env_file

    # Revert: a re-up WITHOUT --frontier clears the marker + env file and passes
    # frontier=None into the overlay call.
    overlay_calls.clear()
    live.demo_up(workdir, hostname="localhost", seed=None, skip_image_build=False)
    assert not env_file.is_file()
    assert not (workdir / live._FRONTIER_MARKER).is_file()
    assert overlay_calls[0]["frontier"] is None


def test_demo_up_promotes_and_seeds_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    up_calls: list[dict] = []
    monkeypatch.setattr(live, "_preflight_docker", lambda: None)
    monkeypatch.setattr(live, "_image_up_to_date", lambda root: True)
    monkeypatch.setattr(live, "_download_model", lambda models_dir: None)
    # NE-TEI-ONNX: stub the tei-embed cache staging (exercised for real in
    # test_demo_live_seams.py) so this orchestration test stays fast + network-free.
    monkeypatch.setattr(
        live, "_stage_tei_cache", lambda workdir, onnx_artifact: workdir / "tei-cache"
    )
    # NE-DEMO-LIVE-SEAMS: stub the seed staging + overlay generation (exercised for real
    # in test_demo_live_seams.py) so this orchestration test stays fast + docker-free.
    monkeypatch.setattr(live, "_stage_demo_seed", lambda workdir: workdir / "seed")

    def _overlay_stub(
        workdir: Path,
        seed_dir: Path,
        https_port: int = 443,
        external_url: str | None = None,
        gpu_vllm: str | None = None,
        tei_cache_dir: Path | None = None,
        live_writeback: live._LiveWritebackStaging | None = None,
    ) -> Path:
        return workdir / "overlay.yaml"

    monkeypatch.setattr(live, "_write_demo_overlay", _overlay_stub)
    monkeypatch.setattr(
        live,
        "_bring_up_stack",
        lambda bundle, models_dir, overlay_path, https_port=443: up_calls.append(
            {"bundle": bundle, "overlay": overlay_path}
        ),
    )
    monkeypatch.setattr(live, "_provision_keycloak", lambda bundle, hostname, scenario, **k: bundle)
    monkeypatch.setattr(live, "_seed_corpus", lambda workdir, bundle, corpus_dir: None)

    workdir = tmp_path / "wd"
    live.demo_up(workdir, hostname="localhost", seed=None, skip_image_build=False)

    bundle = workdir / "bundle"
    assert (bundle / "manifest.json").is_file()  # pilot promote ran
    assert up_calls and up_calls[0]["bundle"] == bundle  # stack bring-up driven with the bundle
    # Every REQUIRED_SECRETS name resolves through the seeded store (values unchecked).
    resolved = resolve_required_secrets(bundle)
    assert set(resolved) == set(REQUIRED_SECRETS)


def test_tei_onnx_artifact_flag_threads_to_demo_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(live, "demo_up", lambda *a, **k: calls.append(k))
    artifact = tmp_path / "artifact.tar.gz"
    artifact.write_bytes(b"fake")

    result = CliRunner().invoke(
        main,
        ["demo", "--workdir", str(tmp_path / "wd"), "up", "--tei-onnx-artifact", str(artifact)],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["tei_onnx_artifact"] == artifact


def test_live_writeback_flag_threads_to_demo_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(live, "demo_up", lambda *a, **k: calls.append(k))

    result = CliRunner().invoke(
        main, ["demo", "--workdir", str(tmp_path / "wd"), "up", "--live-writeback"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["live_writeback"] is True

    # Absent by default -> False (byte-identical default demo mode).
    calls.clear()
    result = CliRunner().invoke(main, ["demo", "--workdir", str(tmp_path / "wd2"), "up"])
    assert result.exit_code == 0, result.output
    assert calls[0]["live_writeback"] is False


def test_m365_draft_mailbox_option_and_env_thread_to_demo_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(live, "demo_up", lambda *a, **k: calls.append(k))

    result = CliRunner().invoke(
        main,
        [
            "demo",
            "--workdir",
            str(tmp_path / "wd"),
            "up",
            "--graph",
            "--m365-draft-mailbox",
            "owner@contoso.test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["m365_draft_mailbox"] == "owner@contoso.test"

    calls.clear()
    result = CliRunner().invoke(
        main,
        ["demo", "--workdir", str(tmp_path / "wd2"), "up", "--graph"],
        env={"NE_DEMO_M365_DRAFT_MAILBOX": "env-owner@contoso.test"},
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["m365_draft_mailbox"] == "env-owner@contoso.test"


def test_seed_manifest_dir_flag_threads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(live, "demo_up", lambda *a, **k: calls.append(k))
    manifest_dir = tmp_path / "manifest"

    result = CliRunner().invoke(
        main,
        [
            "demo",
            "--workdir",
            str(tmp_path / "wd"),
            "up",
            "--seed-manifest-dir",
            str(manifest_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["seed_manifest_dir"] == manifest_dir

    # Absent by default -> None (live.demo_up resolves ~/.ne/acme-seed itself).
    calls.clear()
    result = CliRunner().invoke(main, ["demo", "--workdir", str(tmp_path / "wd2"), "up"])
    assert result.exit_code == 0, result.output
    assert calls[0]["seed_manifest_dir"] is None


def test_rebuild_image_flag_threads_to_demo_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(live, "demo_up", lambda *a, **k: calls.append(k))

    result = CliRunner().invoke(
        main, ["demo", "--workdir", str(tmp_path / "wd"), "up", "--rebuild-image"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["rebuild_image"] is True

    # Absent by default -> False (today's build-only-when-absent behavior preserved).
    calls.clear()
    result = CliRunner().invoke(main, ["demo", "--workdir", str(tmp_path / "wd2"), "up"])
    assert result.exit_code == 0, result.output
    assert calls[0]["rebuild_image"] is False


def test_rebuild_image_mutually_exclusive_with_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(live, "demo_up", lambda *a, **k: None)
    result = CliRunner().invoke(
        main,
        ["demo", "--workdir", str(tmp_path / "wd"), "up", "--rebuild-image", "--skip-image-build"],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_rebuild_image_forces_build_even_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gate builds when --rebuild-image is set even though the image is present,
    and stays a no-op without it — proven by stopping demo_up right after the gate."""

    class _StopAfterGate(Exception):
        pass

    builds: list[bool] = []
    monkeypatch.setattr(live, "_preflight_docker", lambda: None)
    monkeypatch.setattr(live, "_image_up_to_date", lambda root: True)  # image already present
    monkeypatch.setattr(live, "_build_app_image", lambda: builds.append(True))
    monkeypatch.setattr(
        live,
        "_prepare_workdir",
        lambda wd: {"store": wd / "store", "bundle": wd / "bundle", "models": wd / "models"},
    )

    def _stop(_store: Path) -> str:
        raise _StopAfterGate

    monkeypatch.setattr(live, "_seed_secret_store", _stop)  # first seam after the gate

    with pytest.raises(_StopAfterGate):
        live.demo_up(
            tmp_path / "a",
            hostname="localhost",
            seed=None,
            skip_image_build=False,
            rebuild_image=True,
        )
    assert builds == [True]  # forced despite the image being present

    builds.clear()
    with pytest.raises(_StopAfterGate):
        live.demo_up(
            tmp_path / "b",
            hostname="localhost",
            seed=None,
            skip_image_build=False,
            rebuild_image=False,
        )
    assert builds == []  # present + no rebuild flag -> no build


def test_tei_onnx_artifact_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(live, "demo_up", lambda *a, **k: calls.append(k))

    result = CliRunner().invoke(main, ["demo", "--workdir", str(tmp_path / "wd"), "up"])
    assert result.exit_code == 0, result.output
    assert calls[0]["tei_onnx_artifact"] is None


def test_tei_onnx_artifact_envvar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(live, "demo_up", lambda *a, **k: calls.append(k))
    artifact = tmp_path / "artifact.tar.gz"
    artifact.write_bytes(b"fake")
    monkeypatch.setenv("NE_DEMO_TEI_ONNX_ARTIFACT", str(artifact))

    result = CliRunner().invoke(main, ["demo", "--workdir", str(tmp_path / "wd"), "up"])
    assert result.exit_code == 0, result.output
    assert calls[0]["tei_onnx_artifact"] == artifact


def test_tei_onnx_artifact_must_exist(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main,
        [
            "demo",
            "--workdir",
            str(tmp_path / "wd"),
            "up",
            "--tei-onnx-artifact",
            str(tmp_path / "nonexistent.tar.gz"),
        ],
    )
    assert result.exit_code != 0


def test_bring_up_stack_uses_pilot_serve_cpu(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict = {}
    fake_up = click.Command("up", callback=lambda **kw: captured.update(kw))
    up_module = sys.modules["ne.cli.up"]
    monkeypatch.setattr(up_module, "up", fake_up)
    overlay = tmp_path / "demo-overlay.yaml"
    live._bring_up_stack(tmp_path / "bundle", tmp_path / "models", overlay)
    assert captured["profiles"] == ("pilot",)
    assert captured["serve_cpu"] is True
    assert captured["bundle"] == tmp_path / "bundle"
    assert captured["overlays"] == (overlay,)
    assert captured["https_port"] == 443
    assert captured["port_bind_host"] == "0.0.0.0"
    assert captured["reuse_existing_project_ports"] is True

    live._bring_up_stack(
        tmp_path / "bundle",
        tmp_path / "models",
        overlay,
        9443,
        external_url="https://demo.example.host",
    )
    assert captured["https_port"] == 9443
    assert captured["port_bind_host"] == "127.0.0.1"
    assert captured["reuse_existing_project_ports"] is True


def test_demo_down_passthrough(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict = {}
    fake_down = click.Command("down", callback=lambda **kw: captured.update(kw))
    up_module = sys.modules["ne.cli.up"]
    monkeypatch.setattr(up_module, "down", fake_down)
    live.demo_down(tmp_path / "wd", volumes=True)
    assert captured["bundle"] == tmp_path / "wd" / "bundle"
    assert captured["volumes"] is True


def test_demo_down_clears_staged_frontier_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Argus round-2 P2: a live frontier API key must not outlive `ne demo down`
    — a subsequent plain `ne demo up` clearing it (already covered by
    test_frontier_stages_env_and_reverts) is not enough, since a presenter may
    just tear the stack down and walk away."""
    fake_down = click.Command("down", callback=lambda **kw: None)
    up_module = sys.modules["ne.cli.up"]
    monkeypatch.setattr(up_module, "down", fake_down)

    workdir = tmp_path / "wd"
    (workdir / "overlay").mkdir(parents=True)
    env_file = workdir / "overlay" / "frontier.env"
    env_file.write_text("ANTHROPIC_API_KEY='super-secret'\n", encoding="utf-8")
    marker = workdir / live._FRONTIER_MARKER
    marker.write_text(json.dumps({"provider": "anthropic"}), encoding="utf-8")

    live.demo_down(workdir, volumes=False)

    assert not env_file.is_file()
    assert not marker.is_file()

    # Idempotent: calling demo_down again with nothing staged is a clean no-op.
    live.demo_down(workdir, volumes=False)


def test_import_stays_lazy() -> None:
    # `import ne.cli` must not drag ne.api (the lazy-import discipline). Run in a
    # fresh interpreter so a co-resident test's earlier ne.api import can't mask it.
    code = "import ne.cli, sys; assert 'ne.api' not in sys.modules, 'ne.api leaked'"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
