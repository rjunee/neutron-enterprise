# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for `ne up` / `ne down` (NE-070 installer) with docker mocked.

Mocking the compose invocation + preflights is sanctioned here because CI has no
docker daemon (orchestrator-granted exception) — the compose content and the
live bring-up are validated separately (test_pilot_stack.py + the builder's live
core smoke). Secret injection runs FOR REAL against a FileSecretStore fixture.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from ne.cli import main
from ne.secrets import FileSecretStore

up_module = sys.modules["ne.cli.up"]
REPO_ROOT = Path(__file__).resolve().parents[3]

_PG_SENTINEL = "PGsentinel-abc123"
_KC_SENTINEL = "KCsentinel-def456"
_OF_SENTINEL = "OFsentinel-ghi789"
_KEK_SENTINEL = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE"
_CONTINUATION_SENTINEL = "Q09OVElOVUFUSU9OQ09OVElOVUFUSU9OQ09OVElOVUE"
_ET_TENANT = "ETtenant-sentinel-111"
_ET_CLIENT = "ETclient-sentinel-222"
_ET_SECRET = "ETsecret-sentinel-333"

_PREFLIGHTS = (
    "_check_docker",
    "_check_daemon",
    "_check_compose_version",
    "_check_app_image",
    "_check_age_image",
    "_check_ports_free",
    "_check_disk",
    "_check_bundle",
)


class _Recorder:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []

    def __call__(
        self, cmd: list[str], check: bool = False, env: dict[str, str] | None = None
    ) -> types.SimpleNamespace:
        self.calls.append(list(cmd))
        self.envs.append(env)
        return types.SimpleNamespace(returncode=self.returncode)


@pytest.fixture
def in_repo_root():
    cwd = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(cwd)


def _skip_preflights(monkeypatch, *, gpu_ok: bool = True) -> dict[str, bool]:
    consulted: dict[str, bool] = {}
    for name in _PREFLIGHTS:
        monkeypatch.setattr(up_module, name, lambda *a, **k: None)

    def _gpu() -> None:
        consulted["gpu"] = True
        if not gpu_ok:
            raise up_module.click.ClickException("no NVIDIA runtime detected — use --serve-cpu")

    monkeypatch.setattr(up_module, "_check_gpu", _gpu)
    return consulted


def _make_bundle(
    tmp_path: Path,
    *,
    seed: bool = True,
    rebac_ids: bool = False,
    entra: bool = False,
    entra_fields: dict[str, str] | None = None,
) -> Path:
    """``entra=True`` seeds a fully-populated ``pilot/entra-broker`` path
    (NE-BEW-INJECT). ``entra_fields`` overrides with an arbitrary (e.g.
    partial) field set, for the fail-loud-names-only test."""
    secrets_json = tmp_path / "secrets.ne.json"
    store = FileSecretStore(secrets_json)
    if seed:

        async def _seed() -> None:
            await store.write("pilot/postgres", {"password": _PG_SENTINEL})
            await store.write("pilot/keycloak", {"admin_password": _KC_SENTINEL})
            # NE-087 (O-4): the OpenFGA preshared key is now always-required.
            await store.write("pilot/openfga", {"api_token": _OF_SENTINEL})
            # PRE-009A (A2): the private-payload KEK is a REQUIRED secret, so a
            # bundle whose store lacks it now fails `ne up` by design.
            await store.write(
                "pilot/agent-run-private-payload-kek",
                {"key_id": "k1", "kek": _KEK_SENTINEL},
            )
            await store.write(
                "pilot/source-authority-continuation",
                {"key_id": "sa-k1", "hmac_key_b64": _CONTINUATION_SENTINEL},
            )
            if entra_fields is not None:
                await store.write("pilot/entra-broker", entra_fields)
            elif entra:
                await store.write(
                    "pilot/entra-broker",
                    {
                        "tenant_id": _ET_TENANT,
                        "client_id": _ET_CLIENT,
                        "client_secret": _ET_SECRET,
                    },
                )

        asyncio.run(_seed())
    else:
        # Materialize an EMPTY store file so from_config_file can open it.
        asyncio.run(store.list_secrets(""))
    sec_toml = tmp_path / "ne-secrets.toml"
    sec_toml.write_text(f'[file]\npath = "{secrets_json}"\nmode = "plain"\n', encoding="utf-8")
    out = tmp_path / "bundle"
    argv = [
        "pilot",
        "promote",
        "--customer",
        "t",
        "--hostname",
        "localhost",
        "--out",
        str(out),
        "--secrets-config",
        str(sec_toml),
    ]
    if rebac_ids:
        # A bootstrapped ne-rebac.toml (store/model pinned) so `ne up --profile rebac`
        # injects the NE_REBAC_* app env (FIX-1).
        rebac_toml = tmp_path / "ne-rebac.toml"
        rebac_toml.write_text(
            'enabled = true\napi_url = "http://openfga:8080"\n'
            'store_id = "01STORE"\nauthorization_model_id = "01MODEL"\n',
            encoding="utf-8",
        )
        argv += ["--rebac-config", str(rebac_toml)]
    result = CliRunner().invoke(main, argv)
    assert result.exit_code == 0, result.output
    return out


def _profiles_in(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--profile"]


def _skip_preflights_except_ports(monkeypatch) -> dict[str, bool]:
    """No-op every preflight except _check_ports_free, which raises (443 busy)
    while recording that it was consulted."""
    consulted: dict[str, bool] = {}
    for name in _PREFLIGHTS:
        if name != "_check_ports_free":
            monkeypatch.setattr(up_module, name, lambda *a, **k: None)
    monkeypatch.setattr(up_module, "_check_gpu", lambda *a, **k: None)

    def _ports(*a, **k) -> None:
        consulted["ports"] = True
        raise up_module.click.ClickException("port 443 is already in use")

    monkeypatch.setattr(up_module, "_check_ports_free", _ports)
    return consulted


# --- profile expansion / serve mapping ---------------------------------------
def test_pilot_expands_to_core_brain_obs(monkeypatch, in_repo_root, tmp_path) -> None:
    _skip_preflights(monkeypatch)
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)

    result = CliRunner().invoke(main, ["up", "--profile", "pilot", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    assert _profiles_in(rec.calls[0]) == ["brain", "core", "obs"]
    assert rec.calls[0][-2:] == ["--detach", "--wait"]


def test_serve_gpu_maps_to_gpu_and_consults_gpu_preflight(
    monkeypatch, in_repo_root, tmp_path
) -> None:
    consulted = _skip_preflights(monkeypatch, gpu_ok=True)
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)

    result = CliRunner().invoke(
        main, ["up", "--profile", "core", "--serve-gpu", "--bundle", str(bundle)]
    )
    assert result.exit_code == 0, result.output
    assert consulted.get("gpu") is True
    assert "gpu" in _profiles_in(rec.calls[0])
    assert "cpu" not in _profiles_in(rec.calls[0])


def test_serve_cpu_maps_to_cpu_without_gpu_preflight(monkeypatch, in_repo_root, tmp_path) -> None:
    consulted = _skip_preflights(monkeypatch)
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)

    result = CliRunner().invoke(
        main, ["up", "--profile", "core", "--serve-cpu", "--bundle", str(bundle)]
    )
    assert result.exit_code == 0, result.output
    assert "cpu" in _profiles_in(rec.calls[0])
    assert consulted.get("gpu") is None  # GPU preflight not consulted for --serve-cpu


def test_serve_gpu_and_cpu_together_rejected(monkeypatch, in_repo_root, tmp_path) -> None:
    _skip_preflights(monkeypatch)
    bundle = _make_bundle(tmp_path)
    result = CliRunner().invoke(
        main, ["up", "--serve-gpu", "--serve-cpu", "--profile", "core", "--bundle", str(bundle)]
    )
    assert result.exit_code != 0
    assert "at most one" in result.output


def test_agents_profile_warns_and_adds_nothing(monkeypatch, in_repo_root, tmp_path) -> None:
    _skip_preflights(monkeypatch)
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)

    result = CliRunner().invoke(
        main, ["up", "--profile", "core", "--profile", "agents", "--bundle", str(bundle)]
    )
    assert result.exit_code == 0, result.output
    assert "agents" not in _profiles_in(rec.calls[0])
    assert _profiles_in(rec.calls[0]) == ["core"]
    assert "agents" in result.output  # the warning is surfaced


# --- NE-103a: first-party ne-app image preflight (core only) -----------------
def test_up_core_requires_app_image(monkeypatch, in_repo_root, tmp_path) -> None:
    """`ne up --profile core` fails closed with a build hint when the first-party
    ne-app image is absent (docker image inspect rc != 0). The image is
    pull_policy: never, so a missing image would otherwise fail deep in compose."""
    # No-op every preflight EXCEPT the real _check_app_image under test.
    for name in _PREFLIGHTS:
        if name != "_check_app_image":
            monkeypatch.setattr(up_module, name, lambda *a, **k: None)
    monkeypatch.setattr(up_module, "_check_gpu", lambda *a, **k: None)

    def _fake_run(cmd: list[str], *a, **k) -> types.SimpleNamespace:
        # The image-inspect probe fails; nothing else should be reached.
        return types.SimpleNamespace(returncode=1, stdout="", stderr="no such image")

    monkeypatch.setattr(up_module.subprocess, "run", _fake_run)
    bundle = _make_bundle(tmp_path)

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code != 0
    assert "ne-app" in result.output


def test_check_app_image_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    with pytest.raises(up_module.click.ClickException, match="ne-app"):
        up_module._check_app_image()


def test_check_app_image_present(monkeypatch) -> None:
    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )
    up_module._check_app_image()  # no raise


def test_age_image_production_and_ci_admissions_require_distinct_sources(monkeypatch) -> None:
    assert up_module.NE_POSTGRES_AGE_SOURCE_REVISION == ("7b7cede30d7af58ac3259b51cf59c76f1e95754c")
    assert up_module._CI_NE_POSTGRES_AGE_SOURCE_REVISION == (
        "8b60ae265b5d08a794a489492e7709f0da775734"
    )

    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=0,
            stdout=(
                f"{up_module.NE_POSTGRES_AGE_IMAGE}|"
                f"{up_module.NE_POSTGRES_AGE_SOURCE_REVISION}|linux|amd64\n"
            ),
            stderr="",
        ),
    )
    up_module._check_age_image()

    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=0,
            stdout=(
                f"{up_module.NE_POSTGRES_AGE_IMAGE}|"
                f"{up_module._CI_NE_POSTGRES_AGE_SOURCE_REVISION}|linux|amd64\n"
            ),
            stderr="",
        ),
    )
    with pytest.raises(up_module.click.ClickException, match="source revision is not admitted"):
        up_module._check_age_image()


# --- private no-egress AGE admission (CI harness only) ----------------------
_CI_AGE_ID = "sha256:" + "a" * 64


def _configure_ci_age_env(monkeypatch: pytest.MonkeyPatch, iidfile: Path) -> None:
    monkeypatch.setattr(up_module, "_CI_AGE_IIDFILE", iidfile)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("NE_AIRGAP_CI", "1")
    monkeypatch.setenv("NE_AIRGAP_DOCKER_HOST", "unix:///mnt/ne-airgap/run/docker.sock")
    monkeypatch.setenv("DOCKER_HOST", "unix:///mnt/ne-airgap/run/docker.sock")


def _ci_age_inspect(cmd: list[str], *args, **kwargs) -> types.SimpleNamespace:
    if up_module._NE_POSTGRES_AGE_LOCAL_TAG in cmd:
        return types.SimpleNamespace(returncode=0, stdout=f"{_CI_AGE_ID}\n", stderr="")
    expected = f"{_CI_AGE_ID}|{up_module._CI_NE_POSTGRES_AGE_SOURCE_REVISION}|linux|amd64\n"
    return types.SimpleNamespace(returncode=0, stdout=expected, stderr="")


@pytest.mark.parametrize("missing", ["CI", "GITHUB_ACTIONS", "NE_AIRGAP_CI"])
def test_ci_age_iidfile_requires_every_ci_marker(monkeypatch, tmp_path, missing) -> None:
    iidfile = tmp_path / "ne-postgres-age.iid"
    iidfile.write_text(f"{_CI_AGE_ID}\n", encoding="utf-8")
    _configure_ci_age_env(monkeypatch, iidfile)
    monkeypatch.delenv(missing)
    monkeypatch.setattr(up_module.subprocess, "run", _ci_age_inspect)

    with pytest.raises(up_module.click.ClickException, match="no-egress harness"):
        up_module._validate_ci_age_iidfile(iidfile)


def test_ci_age_iidfile_requires_exact_inner_docker_host(monkeypatch, tmp_path) -> None:
    iidfile = tmp_path / "ne-postgres-age.iid"
    iidfile.write_text(f"{_CI_AGE_ID}\n", encoding="utf-8")
    _configure_ci_age_env(monkeypatch, iidfile)
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")

    with pytest.raises(up_module.click.ClickException, match="inner air-gap Docker host"):
        up_module._validate_ci_age_iidfile(iidfile)


def test_ci_age_iidfile_requires_fixed_path(monkeypatch, tmp_path) -> None:
    iidfile = tmp_path / "ne-postgres-age.iid"
    wrong = tmp_path / "other.iid"
    iidfile.write_text(f"{_CI_AGE_ID}\n", encoding="utf-8")
    wrong.write_text(f"{_CI_AGE_ID}\n", encoding="utf-8")
    _configure_ci_age_env(monkeypatch, iidfile)

    with pytest.raises(up_module.click.ClickException, match="fixed harness iidfile"):
        up_module._validate_ci_age_iidfile(wrong)


@pytest.mark.parametrize(
    "content",
    [
        "not-a-digest\n",
        f"{_CI_AGE_ID}\n{_CI_AGE_ID}\n",
        f"{_CI_AGE_ID}\n\n",
        "sha256:" + "A" * 64 + "\n",
    ],
)
def test_ci_age_iidfile_requires_one_canonical_id(monkeypatch, tmp_path, content) -> None:
    iidfile = tmp_path / "ne-postgres-age.iid"
    iidfile.write_text(content, encoding="utf-8")
    _configure_ci_age_env(monkeypatch, iidfile)

    with pytest.raises(up_module.click.ClickException, match=r"exactly one|canonical"):
        up_module._validate_ci_age_iidfile(iidfile)


def test_ci_age_iidfile_requires_tag_and_exact_identity(monkeypatch, tmp_path) -> None:
    iidfile = tmp_path / "ne-postgres-age.iid"
    iidfile.write_text(f"{_CI_AGE_ID}\n", encoding="utf-8")
    _configure_ci_age_env(monkeypatch, iidfile)

    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout="sha256:" + "b" * 64 + "\n", stderr=""
        ),
    )
    with pytest.raises(up_module.click.ClickException, match="local tag"):
        up_module._validate_ci_age_iidfile(iidfile)

    def _wrong_identity(cmd: list[str], *args, **kwargs) -> types.SimpleNamespace:
        if up_module._NE_POSTGRES_AGE_LOCAL_TAG in cmd:
            return types.SimpleNamespace(returncode=0, stdout=f"{_CI_AGE_ID}\n", stderr="")
        return types.SimpleNamespace(
            returncode=0,
            stdout=(f"{_CI_AGE_ID}|{up_module.NE_POSTGRES_AGE_SOURCE_REVISION}|linux|amd64\n"),
            stderr="",
        )

    monkeypatch.setattr(up_module.subprocess, "run", _wrong_identity)
    with pytest.raises(up_module.click.ClickException, match=r"identity.*source.*platform"):
        up_module._validate_ci_age_iidfile(iidfile)


def test_ci_age_iidfile_writes_exact_private_two_service_overlay(monkeypatch, tmp_path) -> None:
    iidfile = tmp_path / "ne-postgres-age.iid"
    iidfile.write_text(f"{_CI_AGE_ID}\n", encoding="utf-8")
    _configure_ci_age_env(monkeypatch, iidfile)
    monkeypatch.setattr(up_module.subprocess, "run", _ci_age_inspect)

    assert up_module._validate_ci_age_iidfile(iidfile) == _CI_AGE_ID
    overlay = up_module._write_ci_age_overlay(tmp_path / "bundle", _CI_AGE_ID)
    assert stat.S_IMODE(overlay.stat().st_mode) == 0o600
    assert yaml.safe_load(overlay.read_text(encoding="utf-8")) == {
        "services": {
            "postgres": {"image": _CI_AGE_ID},
            "age-admission": {
                "image": _CI_AGE_ID,
                "environment": {"NE_POSTGRES_AGE_IMAGE_DIGEST": _CI_AGE_ID},
            },
        }
    }


def test_up_ci_age_overlay_is_hidden_and_always_last(monkeypatch, in_repo_root, tmp_path) -> None:
    help_result = CliRunner().invoke(main, ["up", "--help"])
    assert help_result.exit_code == 0
    assert "ci-age-iidfile" not in help_result.output

    _skip_preflights(monkeypatch)
    monkeypatch.setattr(up_module, "_validate_ci_age_iidfile", lambda _path: _CI_AGE_ID)
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)
    caller_overlay = tmp_path / "caller.yaml"
    caller_overlay.write_text("services: {}\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "up",
            "--profile",
            "core",
            "--overlay",
            str(caller_overlay),
            "--ci-age-iidfile",
            str(up_module._CI_AGE_IIDFILE),
            "--bundle",
            str(bundle),
        ],
    )
    assert result.exit_code == 0, result.output
    argv = rec.calls[0]
    compose_files = [argv[index + 1] for index, value in enumerate(argv) if value == "-f"]
    assert compose_files[-2] == str(caller_overlay)
    assert compose_files[-1].endswith("runtime/postgres-age-ci.overlay.yaml")


def test_up_default_still_calls_immutable_age_preflight(
    monkeypatch, in_repo_root, tmp_path
) -> None:
    consulted: list[str] = []
    _skip_preflights(monkeypatch)
    monkeypatch.setattr(up_module, "_check_age_image", lambda: consulted.append("immutable"))
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    assert consulted == ["immutable"]
    assert not any("postgres-age-ci.overlay.yaml" in value for value in rec.calls[0])


# --- down --------------------------------------------------------------------
def test_down_passes_all_profiles_and_volumes(monkeypatch, in_repo_root, tmp_path) -> None:
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)
    # A runtime env already present (down reuses it).
    (bundle / "runtime").mkdir(exist_ok=True)
    (bundle / "runtime" / "pilot.env").write_text("NE_PILOT_HOSTNAME=localhost\n")

    result = CliRunner().invoke(main, ["down", "--bundle", str(bundle), "--volumes"])
    assert result.exit_code == 0, result.output
    profiles = _profiles_in(rec.calls[0])
    assert set(profiles) == {"core", "brain", "obs", "gpu", "cpu", "rebac"}
    assert "down" in rec.calls[0]
    assert "--volumes" in rec.calls[0]


# --- fix 2: 443/8443 port preflight is conditional on the `core` profile ------
@pytest.mark.parametrize(
    "argv",
    [["--profile", "brain"], ["--profile", "obs", "--serve-cpu"]],
)
def test_up_noncore_profiles_skip_port_preflight(monkeypatch, in_repo_root, tmp_path, argv) -> None:
    consulted = _skip_preflights_except_ports(monkeypatch)
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)

    result = CliRunner().invoke(main, ["up", *argv, "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    assert consulted.get("ports") is None  # never consulted for a non-core bring-up
    assert rec.calls != []  # compose still ran


@pytest.mark.parametrize(
    "argv",
    [["--profile", "core"], ["--profile", "pilot"]],
)
def test_up_core_profiles_enforce_port_preflight(monkeypatch, in_repo_root, tmp_path, argv) -> None:
    consulted = _skip_preflights_except_ports(monkeypatch)
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)

    result = CliRunner().invoke(main, ["up", *argv, "--bundle", str(bundle)])
    assert result.exit_code != 0
    assert "already in use" in result.output
    assert consulted.get("ports") is True
    assert rec.calls == []


# --- fix 3: `ne down` tears down with placeholders, never reads secrets -------
def test_down_without_secrets_uses_placeholders_and_sweeps(
    monkeypatch, in_repo_root, tmp_path
) -> None:
    bundle = _make_bundle(tmp_path, seed=False)  # SecretStore is EMPTY
    assert not (bundle / "runtime" / "pilot.env").exists()

    captured: dict[str, object] = {}

    def _capture_run(cmd: list[str], check: bool = False, env=None) -> types.SimpleNamespace:
        env_file = Path(cmd[cmd.index("--env-file") + 1])
        captured["existed"] = env_file.is_file()
        captured["mode"] = stat.S_IMODE(env_file.stat().st_mode)
        captured["body"] = env_file.read_text()
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(up_module.subprocess, "run", _capture_run)

    result = CliRunner().invoke(main, ["down", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output

    placeholder = up_module._TEARDOWN_PLACEHOLDER
    body = captured["body"]
    assert isinstance(body, str)
    assert "NE_PILOT_HOSTNAME='localhost'\n" in body
    assert f"NE_PILOT_POSTGRES_PASSWORD='{placeholder}'\n" in body
    assert f"NE_PILOT_KEYCLOAK_ADMIN_PASSWORD='{placeholder}'\n" in body
    assert captured["mode"] == 0o600
    # NE-BEW-INJECT: teardown never carries or needs the optional Entra names
    # (they use compose's null-value pass-through form, never :?, so they
    # need no placeholder).
    assert "NE_PILOT_ENTRA_" not in body
    # Swept: the generated placeholder env is gone after `ne down` returns.
    assert not (bundle / "runtime" / "pilot.env").exists()


def test_down_never_touches_secret_store(monkeypatch, in_repo_root, tmp_path) -> None:
    bundle = _make_bundle(tmp_path, seed=False)  # EMPTY store

    def _bomb(_bundle: Path) -> Path:
        raise AssertionError("down must not resolve secrets")

    monkeypatch.setattr(up_module, "_write_runtime_env", _bomb)
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)

    result = CliRunner().invoke(main, ["down", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    assert rec.calls != []


def test_down_survives_corrupt_required_json(monkeypatch, in_repo_root, tmp_path) -> None:
    """A present-but-CORRUPT secrets/required.json must not block teardown:
    _write_teardown_env falls back to REQUIRED_SECRETS placeholder names
    (Codex P2 / Argus P3: json.loads raises ValueError, not OSError)."""
    bundle = _make_bundle(tmp_path, seed=False)  # EMPTY store
    (bundle / "secrets" / "required.json").write_text("{not json", encoding="utf-8")

    captured: dict[str, object] = {}

    def _capture_run(cmd: list[str], check: bool = False, env=None) -> types.SimpleNamespace:
        env_file = Path(cmd[cmd.index("--env-file") + 1])
        captured["body"] = env_file.read_text()
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(up_module.subprocess, "run", _capture_run)

    result = CliRunner().invoke(main, ["down", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    placeholder = up_module._TEARDOWN_PLACEHOLDER
    body = captured["body"]
    assert isinstance(body, str)
    # Fallback names come from REQUIRED_SECRETS; hostname still real (manifest intact).
    assert "NE_PILOT_HOSTNAME='localhost'\n" in body
    assert f"NE_PILOT_POSTGRES_PASSWORD='{placeholder}'\n" in body
    assert f"NE_PILOT_KEYCLOAK_ADMIN_PASSWORD='{placeholder}'\n" in body


def test_down_preserves_operator_env(monkeypatch, in_repo_root, tmp_path) -> None:
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path, seed=False)
    # An operator-placed runtime env (down reuses it and must NOT delete it).
    (bundle / "runtime").mkdir(exist_ok=True)
    env_path = bundle / "runtime" / "pilot.env"
    original = "NE_PILOT_HOSTNAME=localhost\nNE_PILOT_POSTGRES_PASSWORD=operator\n"
    env_path.write_text(original, encoding="utf-8")

    result = CliRunner().invoke(main, ["down", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    assert env_path.exists()
    assert env_path.read_text() == original
    assert rec.envs[0] is not None
    assert (
        rec.envs[0][up_module._AGENT_REGISTRY_HASH_ENV]
        == hashlib.sha256((REPO_ROOT / "deploy/agents/registry.toml").read_bytes()).hexdigest()
    )


def test_agent_registry_hash_is_exact_stable_and_ambient_override_is_powerless(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "repo"
    registry = root / "deploy" / "agents" / "registry.toml"
    registry.parent.mkdir(parents=True)
    registry.write_bytes(b"schema_version = 1\ngeneration = 1\n")
    skill = root / "deploy" / "skills" / "meridian-hardship-draft" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes((REPO_ROOT / "deploy/skills/meridian-hardship-draft/SKILL.md").read_bytes())
    expected = hashlib.sha256(registry.read_bytes()).hexdigest()
    monkeypatch.setenv(up_module._AGENT_REGISTRY_HASH_ENV, "0" * 64)

    first = up_module._scrubbed_child_env(root)
    second = up_module._scrubbed_child_env(root)
    assert first[up_module._AGENT_REGISTRY_HASH_ENV] == expected
    assert second[up_module._AGENT_REGISTRY_HASH_ENV] == expected

    registry.write_bytes(registry.read_bytes() + b"x")
    moved = up_module._scrubbed_child_env(root)[up_module._AGENT_REGISTRY_HASH_ENV]
    assert moved == hashlib.sha256(registry.read_bytes()).hexdigest()
    assert moved != expected


# --- secret injection: 0600, values never surfaced, missing names-only -------
def test_up_writes_0600_env_and_never_logs_values(monkeypatch, in_repo_root, tmp_path) -> None:
    _skip_preflights(monkeypatch)

    # Snapshot the env file AT compose-invocation time: `ne up` writes it 0600,
    # runs compose, then deletes it (ephemeral — no plaintext secret at rest).
    captured: dict[str, object] = {}

    def _capture_run(cmd: list[str], check: bool = False, env=None) -> types.SimpleNamespace:
        env_file = Path(cmd[cmd.index("--env-file") + 1])
        captured["existed"] = env_file.is_file()
        captured["mode"] = stat.S_IMODE(env_file.stat().st_mode)
        captured["body"] = env_file.read_text()
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(up_module.subprocess, "run", _capture_run)
    bundle = _make_bundle(tmp_path)  # real secret injection runs

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output

    # During the compose call the env existed, 0600, with the resolved values.
    assert captured["existed"] is True
    assert captured["mode"] == 0o600
    body = captured["body"]
    assert isinstance(body, str)
    assert _PG_SENTINEL in body and _KC_SENTINEL in body
    # ...but NEVER into stdout/stderr.
    assert _PG_SENTINEL not in result.output
    assert _KC_SENTINEL not in result.output
    # Ephemeral: the plaintext env is gone after `ne up` returns (P3 fix).
    assert not (bundle / "runtime" / "pilot.env").exists()


def test_up_missing_secret_lists_names_only(monkeypatch, in_repo_root, tmp_path) -> None:
    _skip_preflights(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path, seed=False)  # SecretStore is EMPTY

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code != 0
    assert "NE_PILOT_POSTGRES_PASSWORD" in result.output
    assert "NE_PILOT_KEYCLOAK_ADMIN_PASSWORD" in result.output
    # names only — no compose invocation happened
    assert rec.calls == []


# --- NE-BEW-INJECT: Entra broker optional-secret injection --------------------
def test_up_entra_secrets_resolve_into_env(monkeypatch, in_repo_root, tmp_path) -> None:
    _skip_preflights(monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(up_module.subprocess, "run", _read_env_file_run(captured))
    bundle = _make_bundle(tmp_path, entra=True)  # real optional-secret resolution

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output

    body = captured["body"]
    assert isinstance(body, str)
    assert f"NE_PILOT_ENTRA_TENANT_ID='{_ET_TENANT}'\n" in body
    assert f"NE_PILOT_ENTRA_BROKER_CLIENT_ID='{_ET_CLIENT}'\n" in body
    assert f"NE_PILOT_ENTRA_BROKER_CLIENT_SECRET='{_ET_SECRET}'\n" in body
    # ...but NEVER into stdout/stderr (C12).
    assert _ET_TENANT not in result.output
    assert _ET_CLIENT not in result.output
    assert _ET_SECRET not in result.output
    # Ephemeral: the plaintext env is gone after `ne up` returns.
    assert not (bundle / "runtime" / "pilot.env").exists()


def test_up_without_entra_secrets_is_clean_noop(monkeypatch, in_repo_root, tmp_path) -> None:
    """THE COMMON-PATH TEST: no pilot/entra-broker path configured at all —
    absence must degrade to exactly today's behavior with zero new keys."""
    _skip_preflights(monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(up_module.subprocess, "run", _read_env_file_run(captured))
    bundle = _make_bundle(tmp_path)  # no entra= at all — the overwhelming common case

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output

    body = captured["body"]
    assert isinstance(body, str)
    assert "NE_PILOT_ENTRA_" not in body
    assert "core" in _profiles_in(captured["argv"])  # type: ignore[arg-type]


def test_up_partial_entra_secret_fields_fail_loud_names_only(
    monkeypatch, in_repo_root, tmp_path
) -> None:
    _skip_preflights(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    # Only client_id populated — tenant_id and client_secret are missing.
    bundle = _make_bundle(tmp_path, entra_fields={"client_id": _ET_CLIENT})

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code != 0
    assert "NE_PILOT_ENTRA_TENANT_ID" in result.output
    assert "NE_PILOT_ENTRA_BROKER_CLIENT_SECRET" in result.output
    assert _ET_TENANT not in result.output
    assert _ET_CLIENT not in result.output
    assert _ET_SECRET not in result.output
    assert rec.calls == []  # no compose invocation happened


# --- individual preflight failure messages -----------------------------------
def test_check_docker_missing(monkeypatch) -> None:
    monkeypatch.setattr(up_module.shutil, "which", lambda _: None)
    with pytest.raises(up_module.click.ClickException, match="docker CLI not found"):
        up_module._check_docker()


def test_check_daemon_down(monkeypatch) -> None:
    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    with pytest.raises(up_module.click.ClickException, match="daemon not reachable"):
        up_module._check_daemon()


def test_check_compose_version_too_old(monkeypatch) -> None:
    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="2.10.1\n", stderr=""),
    )
    with pytest.raises(up_module.click.ClickException, match="too old"):
        up_module._check_compose_version()


def test_check_ports_free_in_use(monkeypatch) -> None:
    def _busy(port: int, *a) -> None:
        raise up_module.click.ClickException(f"port {port} is already in use")

    monkeypatch.setattr(up_module, "_bind_probe", _busy)
    with pytest.raises(up_module.click.ClickException, match="already in use"):
        up_module._check_ports_free()


def test_check_disk_insufficient(monkeypatch) -> None:
    # Hermeticity: don't shell out to a real docker daemon in this unit test.
    monkeypatch.setattr(up_module, "_docker_data_root", lambda: Path("/"))
    monkeypatch.setattr(
        up_module.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=1 * 1024**3)
    )
    with pytest.raises(up_module.click.ClickException, match="insufficient disk"):
        up_module._check_disk()


# --- fix 4: disk preflight stats the docker data-root filesystem, not `/` -----
def test_check_disk_stats_docker_data_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=f"{tmp_path}\n", stderr=""),
    )
    seen: dict[str, Path] = {}

    def _usage(path) -> types.SimpleNamespace:
        seen["path"] = Path(path)
        return types.SimpleNamespace(free=100 * 1024**3)

    monkeypatch.setattr(up_module.shutil, "disk_usage", _usage)
    up_module._check_disk()
    assert seen["path"] == tmp_path


def test_check_disk_ignores_root_pressure_when_data_root_elsewhere(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=f"{tmp_path}\n", stderr=""),
    )

    def _usage_root_tight(path) -> types.SimpleNamespace:
        free = 1 * 1024**3 if Path(path) == Path("/") else 100 * 1024**3
        return types.SimpleNamespace(free=free)

    monkeypatch.setattr(up_module.shutil, "disk_usage", _usage_root_tight)
    # `/` is at 1 GiB but the data-root (tmp_path) is roomy → must NOT raise.
    up_module._check_disk()

    # Companion: pressure ON the data-root itself DOES fail.
    def _usage_data_tight(path) -> types.SimpleNamespace:
        free = 100 * 1024**3 if Path(path) == Path("/") else 1 * 1024**3
        return types.SimpleNamespace(free=free)

    monkeypatch.setattr(up_module.shutil, "disk_usage", _usage_data_tight)
    with pytest.raises(up_module.click.ClickException, match="insufficient disk"):
        up_module._check_disk()


def test_docker_data_root_falls_back_to_root(monkeypatch) -> None:
    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="/var/lib/docker\n", stderr=""),
    )
    assert up_module._docker_data_root() == Path("/")

    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert up_module._docker_data_root() == Path("/")

    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="foo\n", stderr=""),
    )
    assert up_module._docker_data_root() == Path("/")


def test_docker_data_root_walks_to_existing_ancestor(monkeypatch, tmp_path) -> None:
    target = tmp_path / "missing" / "docker-data"
    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=f"{target}\n", stderr=""),
    )
    assert up_module._docker_data_root() == tmp_path


def test_check_gpu_no_nvidia(monkeypatch) -> None:
    monkeypatch.setattr(
        up_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout='{"runc":{}}', stderr=""),
    )
    with pytest.raises(up_module.click.ClickException, match="no NVIDIA runtime"):
        up_module._check_gpu()


def test_check_bundle_missing_manifest(monkeypatch, tmp_path) -> None:
    with pytest.raises(up_module.click.ClickException, match=r"no manifest\.json"):
        up_module._check_bundle(tmp_path)


# --- fix 1: `ne up` refuses a hostname-tampered bundle (manifest integrity) ---
def test_up_rejects_bundle_with_tampered_hostname(monkeypatch, in_repo_root, tmp_path) -> None:
    # Keep the REAL _check_bundle (it rides verify); no-op every other preflight.
    for name in _PREFLIGHTS:
        if name != "_check_bundle":
            monkeypatch.setattr(up_module, name, lambda *a, **k: None)
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)

    # Tamper the manifest hostname AFTER promote — passes file-hash + config_hash,
    # but must fail the manifest_hash integrity check before any compose runs.
    manifest_path = bundle / "manifest.json"
    m = json.loads(manifest_path.read_text())
    m["hostname"] = "evil.example.com"
    manifest_path.write_text(json.dumps(m), encoding="utf-8")

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code != 0
    assert "manifest integrity" in result.output
    assert rec.calls == []  # no compose invocation happened


# --- NE-087 (O-4) FIX-1: rebac app-env injection ------------------------------
def _read_env_file_run(captured: dict[str, object]):
    """A fake subprocess.run that snapshots the --env-file body at compose time."""

    def _run(cmd: list[str], check: bool = False, env=None) -> types.SimpleNamespace:
        env_file = Path(cmd[cmd.index("--env-file") + 1])
        captured["body"] = env_file.read_text()
        captured["argv"] = list(cmd)
        captured["child_env"] = env
        return types.SimpleNamespace(returncode=0)

    return _run


def test_up_rebac_injects_app_env(monkeypatch, in_repo_root, tmp_path) -> None:
    # REAL _write_runtime_env + REAL secret resolution; only preflights skipped.
    _skip_preflights(monkeypatch)
    bundle = _make_bundle(tmp_path, rebac_ids=True)
    captured: dict[str, object] = {}
    monkeypatch.setattr(up_module.subprocess, "run", _read_env_file_run(captured))

    result = CliRunner().invoke(
        main, ["up", "--profile", "pilot", "--profile", "rebac", "--bundle", str(bundle)]
    )
    assert result.exit_code == 0, result.output
    body = captured["body"]
    assert isinstance(body, str)
    assert "NE_REBAC_ENABLED='true'\n" in body
    assert "NE_REBAC_API_URL='http://openfga:8080'\n" in body
    assert "NE_REBAC_STORE_ID='01STORE'\n" in body
    assert "NE_REBAC_AUTHORIZATION_MODEL_ID='01MODEL'\n" in body
    assert f"NE_REBAC_API_TOKEN='{_OF_SENTINEL}'\n" in body
    # ...and the token never leaks to stdout/stderr (C12).
    assert _OF_SENTINEL not in result.output
    assert "rebac" in _profiles_in(captured["argv"])  # type: ignore[arg-type]


def test_up_without_rebac_writes_no_rebac_env(monkeypatch, in_repo_root, tmp_path) -> None:
    _skip_preflights(monkeypatch)
    bundle = _make_bundle(tmp_path)  # no rebac profile selected below
    captured: dict[str, object] = {}
    monkeypatch.setattr(up_module.subprocess, "run", _read_env_file_run(captured))

    result = CliRunner().invoke(main, ["up", "--profile", "pilot", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    body = captured["body"]
    assert isinstance(body, str)
    # Paired with the static null-pass-through guardian: zero NE_REBAC_* keys here
    # ⇒ the container app env carries none (default-off byte-identical).
    assert "NE_REBAC_" not in body
    assert "rebac" not in _profiles_in(captured["argv"])  # type: ignore[arg-type]


def test_up_rebac_core_requires_bootstrap_ids(monkeypatch, in_repo_root, tmp_path) -> None:
    _skip_preflights(monkeypatch)
    bundle = _make_bundle(tmp_path)  # ne-rebac.toml is the comment-only default
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)

    result = CliRunner().invoke(
        main, ["up", "--profile", "core", "--profile", "rebac", "--bundle", str(bundle)]
    )
    assert result.exit_code != 0
    assert "ne-rebac.toml" in result.output
    assert rec.calls == []  # no compose invocation


def test_up_rebac_only_allows_bootstrap_phase(monkeypatch, in_repo_root, tmp_path) -> None:
    _skip_preflights(monkeypatch)
    bundle = _make_bundle(tmp_path)  # unbootstrapped ids — the bootstrap phase
    captured: dict[str, object] = {}
    monkeypatch.setattr(up_module.subprocess, "run", _read_env_file_run(captured))

    result = CliRunner().invoke(main, ["up", "--profile", "rebac", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    body = captured["body"]
    assert isinstance(body, str)
    assert "NE_REBAC_" not in body  # server starts; no app env until ids exist
    assert "rebac" in _profiles_in(captured["argv"])  # type: ignore[arg-type]


# --- PRE-GRAPH-ON: Tier-2 activation is a deployment decision ----------------
def test_up_graph_activates_exactly_the_one_tier_2_key(monkeypatch, in_repo_root, tmp_path) -> None:
    """GRAPH-ON. REAL _write_runtime_env; only preflights skipped."""
    _skip_preflights(monkeypatch)
    bundle = _make_bundle(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(up_module.subprocess, "run", _read_env_file_run(captured))

    result = CliRunner().invoke(
        main, ["up", "--profile", "pilot", "--graph", "--bundle", str(bundle)]
    )
    assert result.exit_code == 0, result.output
    body = captured["body"]
    assert isinstance(body, str)
    assert "NE_BRAIN_GRAPH_ENABLED='true'\n" in body
    assert "NE_API_DEFAULT_ASK_GENERATION" not in body
    # EXACTLY one key. The DSN and graph name stay compose literals; writing either
    # here would let a bring-up address a database the age-admission gate never
    # admitted, and would make the two roots' env files the source of that answer.
    graph_keys = [
        line.split("=", 1)[0] for line in body.splitlines() if line.startswith("NE_BRAIN_GRAPH_")
    ]
    assert graph_keys == ["NE_BRAIN_GRAPH_ENABLED"]


def test_up_without_graph_writes_no_tier_2_activation(monkeypatch, in_repo_root, tmp_path) -> None:
    """GRAPH-OFF, and the ROLLBACK path: `ne up` without --graph is how an operator
    turns the tier back off, so it must write no activation at all."""
    _skip_preflights(monkeypatch)
    bundle = _make_bundle(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(up_module.subprocess, "run", _read_env_file_run(captured))

    result = CliRunner().invoke(main, ["up", "--profile", "pilot", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    body = captured["body"]
    assert isinstance(body, str)
    assert "NE_BRAIN_GRAPH_" not in body
    assert "NE_API_DEFAULT_ASK_GENERATION" not in body


@pytest.mark.parametrize(
    "profiles",
    [
        ("pilot",),
        ("core", "obs"),
        ("core", "brain", "obs"),
    ],
)
def test_base_up_never_writes_the_live_default_ask_pin(
    monkeypatch, in_repo_root, tmp_path, profiles
) -> None:
    """Only the generated live-writeback overlay owns canonical Ask activation."""
    _skip_preflights(monkeypatch)
    bundle = _make_bundle(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(up_module.subprocess, "run", _read_env_file_run(captured))
    args = ["up"]
    for profile in profiles:
        args.extend(("--profile", profile))
    args.extend(("--bundle", str(bundle)))

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0, result.output
    body = captured["body"]
    assert isinstance(body, str)
    assert "NE_API_DEFAULT_ASK_GENERATION" not in body


def test_up_rollback_scrubs_an_ambient_activation(monkeypatch, in_repo_root, tmp_path) -> None:
    """Rolling Tier-2 back must FAIL CLOSED against the operator's own shell.

    `ne up --graph` is a documented instruction, so the exported variable is the
    expected leftover in the shell that ran it. Compose gives a shell-exported name
    precedence over the same name in --env-file, so without the scrub the very next
    plain `ne up` would silently keep the graph tier composed.
    """
    _skip_preflights(monkeypatch)
    monkeypatch.setenv("NE_BRAIN_GRAPH_ENABLED", "true")  # the leftover from --graph
    monkeypatch.setenv("NE_API_DEFAULT_ASK_GENERATION", "foreign-generation")
    monkeypatch.setenv("NE_PILOT_UNRELATED_SENTINEL", "kept")
    bundle = _make_bundle(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    child_env = rec.envs[0]
    assert child_env is not None
    assert not any(k.startswith("NE_BRAIN_GRAPH_") for k in child_env)
    assert "NE_API_DEFAULT_ASK_GENERATION" not in child_env
    # Positive control: the scrub is targeted, not a wholesale clear — an unrelated
    # ambient variable still reaches compose, so the absence above is a real
    # exclusion rather than an empty environment.
    assert child_env["NE_PILOT_UNRELATED_SENTINEL"] == "kept"


def test_up_graph_requires_the_admitted_age_substrate(monkeypatch, in_repo_root, tmp_path) -> None:
    """--graph without the core profile starts no postgres and runs no admission,
    so it fails closed BEFORE the env file or any overlay is written."""
    _skip_preflights(monkeypatch)
    bundle = _make_bundle(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)

    def _bomb(*_a: object, **_k: object) -> Path:
        raise AssertionError("the refusal must precede every mutation, env file included")

    monkeypatch.setattr(up_module, "_write_runtime_env", _bomb)

    result = CliRunner().invoke(
        main, ["up", "--profile", "brain", "--graph", "--bundle", str(bundle)]
    )
    assert result.exit_code != 0
    assert "core profile" in result.output
    assert rec.calls == []  # no compose invocation


# --- NE-DEMO-LIVE-SEAMS / NE-PILOT-OPA-BUNDLE: `ne up` compose -f layering ---
def test_up_overlay_appends_compose_f(monkeypatch, in_repo_root, tmp_path) -> None:
    _skip_preflights(monkeypatch)
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)
    overlay = tmp_path / "demo-overlay.yaml"
    overlay.write_text("services: {}\n", encoding="utf-8")

    result = CliRunner().invoke(
        main, ["up", "--profile", "core", "--overlay", str(overlay), "--bundle", str(bundle)]
    )
    assert result.exit_code == 0, result.output
    argv = rec.calls[0]
    # NE-PILOT-OPA-BUNDLE: a `core` bring-up now carries THREE -f: the base
    # compose, the GENERATED opa-govern overlay (second — layered before any
    # operator overlay so the operator overlay still wins on /policies), then
    # the user-supplied --overlay LAST (merge order: later wins).
    f_indices = [i for i, tok in enumerate(argv) if tok == "-f"]
    assert len(f_indices) == 3
    base_f, generated_f, user_f = (argv[i + 1] for i in f_indices)
    assert base_f.endswith("deploy/compose/pilot/compose.yaml")
    assert generated_f.endswith("runtime/opa-govern.overlay.yaml")
    assert user_f == str(overlay)
    assert f_indices == sorted(f_indices)


def test_up_core_two_f_generated_overlay(monkeypatch, in_repo_root, tmp_path) -> None:
    _skip_preflights(monkeypatch)
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    argv = rec.calls[0]
    # A core bring-up ALWAYS carries the generated opa-govern overlay as its
    # second -f, even with no --overlay passed.
    assert argv.count("-f") == 2
    f_indices = [i for i, tok in enumerate(argv) if tok == "-f"]
    assert argv[f_indices[0] + 1].endswith("deploy/compose/pilot/compose.yaml")
    assert argv[f_indices[1] + 1].endswith("runtime/opa-govern.overlay.yaml")


def test_up_noncore_single_f(monkeypatch, in_repo_root, tmp_path) -> None:
    consulted = _skip_preflights_except_ports(monkeypatch)
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)

    result = CliRunner().invoke(main, ["up", "--profile", "brain", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    assert consulted.get("ports") is None  # non-core: no port preflight either
    argv = rec.calls[0]
    # A non-core bring-up (no `core` profile) stages nothing: argv stays
    # byte-identical to the pre-unit shape — exactly one compose file.
    assert argv.count("-f") == 1


def test_up_core_stages_govern_policies(monkeypatch, in_repo_root, tmp_path) -> None:
    _skip_preflights(monkeypatch)
    monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(tmp_path)

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output

    staged_decision = bundle / "runtime" / "opa-policies" / "ne" / "govern" / "decision.rego"
    assert staged_decision.is_file()
    assert "package ne.govern" in staged_decision.read_text(encoding="utf-8")
    assert not list((bundle / "runtime" / "opa-policies").rglob("*_test.rego"))

    overlay_path = bundle / "runtime" / "opa-govern.overlay.yaml"
    assert overlay_path.is_file()
    overlay_text = overlay_path.read_text(encoding="utf-8")
    assert ":/policies:ro" in overlay_text
    assert str((bundle / "runtime" / "opa-policies").resolve()) in overlay_text


def test_up_scrubs_ambient_rebac_env(monkeypatch, in_repo_root, tmp_path) -> None:
    _skip_preflights(monkeypatch)
    monkeypatch.setenv("NE_REBAC_ENABLED", "true")  # a hostile/leftover ambient var
    bundle = _make_bundle(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    child_env = rec.envs[0]
    assert child_env is not None
    assert not any(k.startswith("NE_REBAC_") for k in child_env)


def test_up_scrubs_ambient_entra_env(monkeypatch, in_repo_root, tmp_path) -> None:
    """C1: docker compose gives a shell-exported var PRECEDENCE over the same
    name in --env-file when resolving a null-value pass-through key. A bundle
    with NO pilot/entra-broker path is the common absent case — an ambient
    NE_PILOT_ENTRA_* left over in the operator's shell must not leak into (or
    fabricate) Entra brokering for a deployment that never configured it. The
    ephemeral --env-file must be the ONLY source, exactly like NE_REBAC_*."""
    _skip_preflights(monkeypatch)
    # A hostile/leftover ambient var — bundle has no pilot/entra-broker at all.
    monkeypatch.setenv("NE_PILOT_ENTRA_BROKER_CLIENT_SECRET", "ambient-shell-leak")
    bundle = _make_bundle(tmp_path)  # entra=False (default): no entra-broker path
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code == 0, result.output
    child_env = rec.envs[0]
    assert child_env is not None
    assert not any(k.startswith("NE_PILOT_ENTRA_") for k in child_env)


# --- P3-2: _write_env_file control-char guard + $ escape ----------------------
def test_up_entra_secret_with_newline_fails_loud_names_only(
    monkeypatch, in_repo_root, tmp_path
) -> None:
    """The Entra client secret is the FIRST externally-supplied, operator-
    pasted value ever to flow through _write_env_file — every prior value was
    tool-generated or a manifest hostname. A newline in a pasted secret must
    never inject an extra line into the --env-file (e.g. overriding another
    NE_PILOT_* key); it must abort `ne up` before any compose invocation,
    naming the offending KEY only, value never echoed."""
    _skip_preflights(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(up_module.subprocess, "run", rec)
    bundle = _make_bundle(
        tmp_path,
        entra_fields={
            "tenant_id": _ET_TENANT,
            "client_id": _ET_CLIENT,
            "client_secret": "evil\nNE_PILOT_POSTGRES_PASSWORD=owned",
        },
    )

    result = CliRunner().invoke(main, ["up", "--profile", "core", "--bundle", str(bundle)])
    assert result.exit_code != 0
    assert "NE_PILOT_ENTRA_BROKER_CLIENT_SECRET" in result.output
    assert "evil" not in result.output
    assert "owned" not in result.output
    assert rec.calls == []  # no compose invocation happened


def test_write_env_file_rejects_control_characters_names_only(tmp_path) -> None:
    with pytest.raises(up_module.click.ClickException) as exc_info:
        up_module._write_env_file(tmp_path, {"NE_PILOT_SOME_KEY": "line1\rline2"})
    message = str(exc_info.value)
    assert "NE_PILOT_SOME_KEY" in message
    assert "line1" not in message
    assert "line2" not in message

    with pytest.raises(up_module.click.ClickException) as exc_info2:
        up_module._write_env_file(tmp_path, {"NE_PILOT_OTHER_KEY": "sentinelXYZ\x01payload"})
    message2 = str(exc_info2.value)
    assert "NE_PILOT_OTHER_KEY" in message2
    assert "sentinelXYZ" not in message2
    assert "payload" not in message2


def test_write_env_file_single_quotes_values_for_compose(tmp_path) -> None:
    # NE-CLI-DOTENV-UNIFY: _write_env_file now delegates to the shared
    # ne.cli.envfile.dotenv_line single-quote serializer, which passes `$`
    # through literally (single-quoted dotenv values are taken fully literal
    # by compose-go's parser — no escaping needed at all) instead of the old,
    # weaker `$` -> `$$` doubling.
    env_path = up_module._write_env_file(
        tmp_path, {"NE_PILOT_DOLLAR_KEY": "pa$s", "NE_PILOT_PLAIN_KEY": "hex-abc123"}
    )
    body = env_path.read_text(encoding="utf-8")
    assert "NE_PILOT_DOLLAR_KEY='pa$s'\n" in body
    assert "NE_PILOT_PLAIN_KEY='hex-abc123'\n" in body
    # Bidirectional control: the old weak $$-doubled form must be GONE.
    assert "pa$$s" not in body


def test_write_env_file_rejects_embedded_single_quote_names_only(tmp_path) -> None:
    # Validate-all-before-any-byte: a rejected value must leave NO partial
    # pilot.env behind, even when other values in the same dict are fine.
    with pytest.raises(up_module.click.ClickException) as exc_info:
        up_module._write_env_file(
            tmp_path, {"NE_PILOT_OK_KEY": "fine", "NE_PILOT_QUOTE_KEY": "has'quote"}
        )
    message = str(exc_info.value)
    assert "NE_PILOT_QUOTE_KEY" in message
    assert "has'quote" not in message
    assert not (tmp_path / "runtime" / "pilot.env").exists()
