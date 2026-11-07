# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ne.inference_operator.vast_bundle import VAST_OFFLINE_LOCK_SHA256

_ROOT = Path(__file__).resolve().parents[3]
_CPU = _ROOT / "deploy/inference-operator/cpu-vllm-target.sh"
_VAST = _ROOT / "deploy/inference-operator/vast-rig.sh"
_INSTALL = _ROOT / "deploy/inference-operator/install-policy.sh"
_UNIT = _ROOT / "deploy/inference-operator/ne-inference-operator.service"
_SOCKET_UNIT = _ROOT / "deploy/inference-operator/ne-inference-operator.socket"
_PROVIDER_UNIT = _ROOT / "deploy/inference-operator/ne-inference-provider-broker.service"
_PROVIDER_SOCKET = _ROOT / "deploy/inference-operator/ne-inference-provider-broker.socket"
_PROVIDER_NETWORK = _ROOT / "deploy/inference-operator/provider-network-policy.sh"
_PROVIDER_REFRESH = _ROOT / "deploy/inference-operator/provider-network-refresh.sh"
_PROVIDER_REFRESH_TIMER = _ROOT / "deploy/inference-operator/ne-inference-provider-refresh.timer"
_PROVIDER_GRANT = _ROOT / "deploy/inference-operator/provider-ssh-grant.py"
_REGISTRY = _ROOT / "deploy/inference-operator/registry.json"
_VAST_LOCK = _ROOT / "deploy/inference-operator/vastai-offline-lock.json"
_MANIFEST = _ROOT / "tools/ne-inference-operator/src/ne/inference_operator/manifest.py"
_SERVER = _ROOT / "tools/ne-inference-operator/src/ne/inference_operator/server.py"
_RUNNER = _ROOT / "tools/ne-inference-operator/src/ne/inference_operator/runner.py"


def _ssh_grant_module():
    spec = importlib.util.spec_from_file_location("ne_test_provider_ssh_grant", _PROVIDER_GRANT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cpu_adapter_is_target_scoped_and_resource_safe() -> None:
    source = _CPU.read_text(encoding="utf-8")
    assert "up --detach --no-deps --pull never --force-recreate --wait vllm-cpu" in source
    assert "stop --timeout 30 vllm-cpu" in source
    assert "NE_UPGRADE_LOCK:-/run/lock/neutron-live-upgrade.lock" in source
    assert "flock -s 9" in source and "flock -x 9" not in source
    runner = _RUNNER.read_text(encoding="utf-8")
    assert "fcntl.LOCK_SH | fcntl.LOCK_NB" in runner
    assert "fcntl.LOCK_EX | fcntl.LOCK_NB" not in runner
    assert "completion_url" in source and '"max_tokens":1' in source
    assert "build_opener(NoRedirect).open" in source
    assert "runtime_config_label" in source and "com.docker.compose.config-hash" in source
    for forbidden in (" compose down", " compose rm", " compose kill", "docker rm", "docker kill"):
        assert forbidden not in source


def test_cpu_start_uses_exact_receipt_lock_and_single_service(tmp_path: Path) -> None:
    class _CompletionHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            request = json.loads(self.rfile.read(length))
            assert request["max_tokens"] == 1 and request["model"] == "qwen3-0.6b"
            payload = json.dumps(
                {"choices": [{"text": "ok"}], "usage": {"completion_tokens": 1}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 8091), _CompletionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    compose_file = tmp_path / "compose.yaml"
    env_file = tmp_path / ".env"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env_file.write_text("SAFE=1\n", encoding="utf-8")
    compose_file.chmod(0o600)
    env_file.chmod(0o600)
    semantic_hash = "a" * 64
    runtime_config_label = "d" * 64
    image = "example.invalid/vllm@sha256:" + "b" * 64
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": "ne-vllm-cpu-compose-receipt-v1",
                "project_name": "ne-pilot",
                "working_dir": str(tmp_path),
                "env_file": str(env_file),
                "compose_files": [str(compose_file)],
                "service": "vllm-cpu",
                "semantic_hash": semantic_hash,
                "runtime_config_label": runtime_config_label,
                "image": image,
                "completion_url": "http://127.0.0.1:8091/v1/completions",
                "model": "qwen3-0.6b",
            }
        ),
        encoding="utf-8",
    )
    receipt.chmod(0o600)
    log = tmp_path / "docker.log"
    ps_counter = tmp_path / "ps-counter"
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        f"Path({str(log)!r}).open('a').write(' '.join(sys.argv[1:])+'\\n')\n"
        "a=sys.argv[1:]\n"
        f"image={image!r}\n"
        f"files={str(compose_file)!r}\n"
        f"workdir={str(tmp_path)!r}\n"
        f"semantic={semantic_hash!r}\n"
        f"runtime_config={runtime_config_label!r}\n"
        f"counter=Path({str(ps_counter)!r})\n"
        "if 'config' in a and '--hash' in a:\n"
        " counter.write_text('0')\n"
        " print('vllm-cpu',semantic)\n"
        "elif 'config' in a and '--format' in a: "
        "print(json.dumps({'services':{'vllm-cpu':{'image':image}}}))\n"
        "elif 'ps' in a:\n"
        " calls=int(counter.read_text())+1\n"
        " counter.write_text(str(calls))\n"
        " print(('d' if os.environ.get('FAKE_DRIFT') and calls >= 2 else 'c')*64)\n"
        "elif a[0]=='inspect':\n"
        " f=a[a.index('--format')+1]\n"
        " if '.State.Running' in f: print('true')\n"
        " elif '.Config.Image' in f: print(image)\n"
        " elif '.State.Health.Status' in f: print('healthy')\n"
        " elif 'config-hash' in f: print(os.environ.get('FAKE_RUNTIME_CONFIG',runtime_config))\n"
        " elif 'config_files' in f: print(files)\n"
        " elif 'working_dir' in f: print(workdir)\n",
        encoding="utf-8",
    )
    docker.chmod(0o500)
    flock = tmp_path / "flock"
    flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    flock.chmod(0o500)
    lock = tmp_path / "upgrade.lock"
    environment = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "NE_INFERENCE_OPERATOR": "1",
        "NE_CPU_COMPOSE_RECEIPT": str(receipt),
        "NE_UPGRADE_LOCK": str(lock),
        "NE_OPERATOR_PYTHON": sys.executable,
        "NE_OPERATOR_DOCKER": str(docker),
        "NE_OPERATOR_OWNER_UID": str(os.getuid()),
        "FAKE_RUNTIME_CONFIG": runtime_config_label,
    }
    try:
        result = subprocess.run(
            [str(_CPU), "start"], env=environment, text=True, capture_output=True, timeout=10
        )
        drift = subprocess.run(
            [str(_CPU), "status"],
            env=environment | {"FAKE_DRIFT": "1"},
            text=True,
            capture_output=True,
            timeout=10,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result.returncode == 0, result.stdout + result.stderr
    assert drift.returncode == 4
    trace = log.read_text(encoding="utf-8")
    assert "up --detach --no-deps --pull never --force-recreate --wait vllm-cpu" in trace
    assert str(compose_file) in trace and str(env_file) in trace
    assert not any(word in trace for word in (" down ", " rm ", " kill "))

    stale_environment = environment | {"FAKE_RUNTIME_CONFIG": "e" * 64}
    stale = subprocess.run(
        [str(_CPU), "status"],
        env=stale_environment,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert stale.returncode == 4

    redirected = threading.Event()

    class _ExternalHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            redirected.set()
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    external = ThreadingHTTPServer(("127.0.0.1", 0), _ExternalHandler)

    class _RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{external.server_address[1]}/stolen",
            )
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    redirect = ThreadingHTTPServer(("127.0.0.1", 8091), _RedirectHandler)
    external_thread = threading.Thread(target=external.serve_forever, daemon=True)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    external_thread.start()
    redirect_thread.start()
    try:
        refused_redirect = subprocess.run(
            [str(_CPU), "status"],
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
        )
    finally:
        redirect.shutdown()
        external.shutdown()
        redirect.server_close()
        external.server_close()
        redirect_thread.join(timeout=2)
        external_thread.join(timeout=2)
    assert refused_redirect.returncode == 1
    assert not redirected.is_set()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "attacker-model"),
        ("completion_url", "http://127.0.0.1:8091@evil.invalid/v1/completions"),
        ("completion_url", "http://127.0.0.1.evil.invalid:8091/v1/completions"),
        ("completion_url", "http://127.0.0.1:8091/v1/completions?redirect=evil"),
    ],
)
def test_cpu_receipt_refuses_wrong_model_or_loopback_lookalike_before_docker(
    tmp_path: Path, field: str, value: str
) -> None:
    compose_file = tmp_path / "compose.yaml"
    env_file = tmp_path / ".env"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env_file.write_text("SAFE=1\n", encoding="utf-8")
    compose_file.chmod(0o600)
    env_file.chmod(0o600)
    document = {
        "schema": "ne-vllm-cpu-compose-receipt-v1",
        "project_name": "ne-pilot",
        "working_dir": str(tmp_path),
        "env_file": str(env_file),
        "compose_files": [str(compose_file)],
        "service": "vllm-cpu",
        "semantic_hash": "a" * 64,
        "runtime_config_label": "d" * 64,
        "image": "example.invalid/vllm@sha256:" + "b" * 64,
        "completion_url": "http://127.0.0.1:8091/v1/completions",
        "model": "qwen3-0.6b",
    }
    document[field] = value
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(document), encoding="utf-8")
    receipt.chmod(0o600)
    docker_marker = tmp_path / "docker-called"
    docker = tmp_path / "docker"
    docker.write_text(f"#!/bin/sh\ntouch '{docker_marker}'\n", encoding="utf-8")
    docker.chmod(0o500)
    flock = tmp_path / "flock"
    flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    flock.chmod(0o500)
    result = subprocess.run(
        [str(_CPU), "status"],
        env={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "NE_INFERENCE_OPERATOR": "1",
            "NE_CPU_COMPOSE_RECEIPT": str(receipt),
            "NE_UPGRADE_LOCK": str(tmp_path / "upgrade.lock"),
            "NE_OPERATOR_PYTHON": sys.executable,
            "NE_OPERATOR_DOCKER": str(docker),
            "NE_OPERATOR_OWNER_UID": str(os.getuid()),
        },
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 72
    assert not docker_marker.exists()


def test_vast_operator_mode_has_closed_non_destructive_dispatch() -> None:
    source = _VAST.read_text(encoding="utf-8")
    operator = source.rsplit('if [ "$OPERATOR_MODE" = "1" ]; then', 1)[1].split("exit 0", 1)[0]
    assert "operator-status) cmd_operator_status" in operator
    assert "operator-start) cmd_operator_start" in operator
    assert "operator-stop) cmd_operator_stop" in operator
    for forbidden in ("cmd_destroy", "cmd_provision", "cmd_use", "cmd_ssh", "cmd_serve"):
        assert forbidden not in operator
    assert "env -u NE_VAST_KEY_FILE" in source
    assert "VASTAI_NO_UPDATE_CHECK=1" in source
    assert 'install -m 600 "$VAST_KEY_FILE"' in source


def test_vast_operator_start_never_reuses_provider_json_argv_wait_path() -> None:
    source = _VAST.read_text(encoding="utf-8")
    wait = source.split("operator_wait_ready() {", 1)[1].split("\n}\n", 1)[0]
    start = source.split("cmd_operator_start() {", 1)[1].split("\n}\n", 1)[0]
    assert '<<<"$raw"' in wait
    assert 'operator_refresh_state "$id" "$raw"' in wait
    assert "operator_rssh 'echo ok'" in wait
    assert 'python3 - "$id" "$raw"' not in wait
    assert 'operator_wait_ready "$(IID)"' in start
    assert '\n  wait_ready "$(IID)"' not in start


@pytest.mark.skipif(sys.platform != "linux", reason="production FD execution uses Linux /proc")
def test_vast_operator_stop_uses_preinstalled_cli_without_secret_argv(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    (state_dir / "instance.json").write_text('{"id":42}', encoding="utf-8")
    (state_dir / "instance.json").chmod(0o600)
    key = tmp_path / "vast-key"
    key.write_text("sentinel-secret-value\n", encoding="utf-8")
    key.chmod(0o400)
    log = tmp_path / "vast.log"
    cli = tmp_path / "vastai"
    cli.write_text(
        "#!/bin/sh\n"
        'test -f "$HOME/.config/vastai/vast_api_key" || exit 91\n'
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$1 $2" in "show instance") printf \'{"actual_status":"stopped"}\\n\';; esac\n',
        encoding="utf-8",
    )
    cli.chmod(0o500)
    temp_root = tmp_path / "runtime"
    temp_root.mkdir(mode=0o700)
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "NE_VAST_OPERATOR_MODE": "1",
        "NE_VAST_KEY_FILE": str(key),
        "NE_VAST_STATE_DIR": str(state_dir),
        "NE_VAST_OPERATOR_TMP_ROOT": str(temp_root),
        "NE_OPERATOR_PYTHON": sys.executable,
        "NE_OPERATOR_OWNER_UID": str(os.getuid()),
    }

    descriptor = os.open(cli, os.O_RDONLY)
    environment["NE_VAST_OPERATOR_CLI_FD"] = f"/dev/fd/{descriptor}"
    try:
        result = subprocess.run(
            [str(_VAST), "operator-stop"],
            env=environment,
            pass_fds=(descriptor,),
            text=True,
            capture_output=True,
            timeout=10,
        )
    finally:
        os.close(descriptor)

    assert result.returncode == 0, result.stdout + result.stderr
    trace = log.read_text(encoding="utf-8")
    assert "stop instance 42" in trace
    assert "show instance 42 --raw" in trace
    assert "sentinel-secret-value" not in trace + result.stdout + result.stderr
    assert "set api-key" not in trace
    assert not any(word in trace for word in ("destroy", "create", "provision"))


def _run_vast_operator_status(
    tmp_path: Path,
    *,
    key_state: str = "valid",
    unsafe_known_hosts: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    (state_dir / "instance.json").write_text('{"id":42}', encoding="utf-8")
    (state_dir / "instance.json").chmod(0o600)
    api_key = tmp_path / "vast-key"
    api_key.write_text("sentinel-api-secret\n", encoding="utf-8")
    api_key.chmod(0o400)
    ssh_key = tmp_path / "vast-ssh-private-key"
    if key_state == "symlink":
        target = tmp_path / "real-ssh-key"
        target.write_text("admitted-key\n", encoding="utf-8")
        target.chmod(0o600)
        ssh_key.symlink_to(target)
    elif key_state != "missing":
        ssh_key.write_text(
            "wrong-key\n" if key_state == "wrong" else "admitted-key\n",
            encoding="utf-8",
        )
        ssh_key.chmod(0o644 if key_state == "mode" else 0o400)
    known_hosts = tmp_path / "known-hosts"
    if unsafe_known_hosts:
        known_hosts.write_text("unsafe\n", encoding="utf-8")
        known_hosts.chmod(0o644)
    ssh_log = tmp_path / "ssh.log"
    ssh = tmp_path / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{ssh_log}"\n'
        f'test "$(cat "{ssh_key}" 2>/dev/null)" = admitted-key\n',
        encoding="utf-8",
    )
    ssh.chmod(0o500)
    grant = tmp_path / "provider-grant"
    grant.write_text(
        '#!/bin/sh\ncat >/dev/null\nprintf \'{"code":"granted"}\\n\'\n',
        encoding="utf-8",
    )
    grant.chmod(0o500)
    cli = tmp_path / "vastai"
    cli.write_text(
        "#!/bin/sh\n"
        'test -f "$HOME/.config/vastai/vast_api_key" || exit 91\n'
        'case "$1 $2" in\n'
        "  \"show instance\") printf '%s\\n' "
        '\'{"actual_status":"running","public_ipaddr":"203.0.113.7",'
        '"ports":{"22/tcp":[{"HostPort":2222}]}}\';;\n'
        "esac\n",
        encoding="utf-8",
    )
    cli.chmod(0o500)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    environment = {
        "PATH": os.environ["PATH"],
        "NE_VAST_OPERATOR_MODE": "1",
        "NE_VAST_KEY_FILE": str(api_key),
        "NE_VAST_SSH_KEY_FILE": str(ssh_key),
        "NE_VAST_KNOWN_HOSTS_FILE": str(known_hosts),
        "NE_VAST_STATE_DIR": str(state_dir),
        "NE_VAST_OPERATOR_TMP_ROOT": str(runtime),
        "NE_OPERATOR_PYTHON": sys.executable,
        "NE_OPERATOR_SSH": str(ssh),
        "NE_OPERATOR_OWNER_UID": str(os.getuid()),
        "NE_PROVIDER_GRANT_CLIENT": str(grant),
        "NE_PROVIDER_TRANSITION_ID": "status-test",
    }
    descriptor = os.open(cli, os.O_RDONLY)
    environment["NE_VAST_OPERATOR_CLI_FD"] = f"/dev/fd/{descriptor}"
    try:
        result = subprocess.run(
            [str(_VAST), "operator-status"],
            env=environment,
            pass_fds=(descriptor,),
            text=True,
            capture_output=True,
            timeout=10,
        )
    finally:
        os.close(descriptor)
    return result, ssh_log, ssh_key, known_hosts


@pytest.mark.skipif(sys.platform != "linux", reason="production FD execution uses Linux /proc")
def test_vast_operator_ssh_uses_only_closed_identity_and_persistent_host_keys(
    tmp_path: Path,
) -> None:
    result, ssh_log, ssh_key, known_hosts = _run_vast_operator_status(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    arguments = ssh_log.read_text(encoding="utf-8").splitlines()
    assert arguments[:2] == ["-F", "/dev/null"]
    assert ["-i", str(ssh_key)] == arguments[arguments.index("-i") : arguments.index("-i") + 2]
    assert "IdentitiesOnly=yes" in arguments
    assert "BatchMode=yes" in arguments
    assert f"UserKnownHostsFile={known_hosts}" in arguments
    assert "root@203.0.113.7" in arguments
    assert known_hosts.read_bytes() == b""
    assert known_hosts.stat().st_mode & 0o777 == 0o600
    assert "sentinel-api-secret" not in "\n".join(arguments)


@pytest.mark.skipif(sys.platform != "linux", reason="production FD execution uses Linux /proc")
@pytest.mark.parametrize("key_state", ["missing", "mode", "symlink"])
def test_vast_operator_refuses_missing_or_unsafe_ssh_identity_before_ssh(
    tmp_path: Path, key_state: str
) -> None:
    result, ssh_log, _ssh_key, _known_hosts = _run_vast_operator_status(
        tmp_path, key_state=key_state
    )
    assert result.returncode == 4
    assert not ssh_log.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="production FD execution uses Linux /proc")
def test_vast_operator_wrong_ssh_identity_has_no_fallback(tmp_path: Path) -> None:
    result, ssh_log, _ssh_key, _known_hosts = _run_vast_operator_status(tmp_path, key_state="wrong")
    assert result.returncode == 4
    arguments = ssh_log.read_text(encoding="utf-8")
    assert "IdentitiesOnly=yes" in arguments
    assert "IdentityAgent" not in arguments


@pytest.mark.skipif(sys.platform != "linux", reason="production FD execution uses Linux /proc")
def test_vast_operator_refuses_unsafe_known_hosts_before_ssh(tmp_path: Path) -> None:
    result, ssh_log, _ssh_key, _known_hosts = _run_vast_operator_status(
        tmp_path, unsafe_known_hosts=True
    )
    assert result.returncode == 4
    assert not ssh_log.exists()


def test_install_policy_never_downloads_and_separates_registry_from_adapter_policy() -> None:
    source = _INSTALL.read_text(encoding="utf-8")
    assert "preinstalled Vast CLI is required; this installer never downloads it" in source
    assert "pip install" not in source
    assert "validate_vast_bundle" in source and "vastai-offline-lock.json" in source
    lock = json.loads(_VAST_LOCK.read_text(encoding="utf-8"))
    assert lock["upstream"] == {
        "commit": "bc7356483dd0f922ee17975c5357741f0d5d81fc",
        "installed_sha256": "b4f51244585015e755b1d60141e6f4856e3af0e6408a2db9c4fdcad59f5a771a",
        "name": "vast-cli",
        "source_sha256": "efb81f79dfbb2a6a8a8230c73ffb0922c2c72925ad103eee5e197d575e1a8a91",
        "tag": "v1.5.2",
        "version": "1.5.2",
    }
    assert {row["name"] for row in lock["wheels"]} == {
        "certifi",
        "charset-normalizer",
        "idna",
        "requests",
        "urllib3",
    }
    assert "cryptography" not in str(lock) and "pillow" not in str(lock)
    registry = json.loads(_REGISTRY.read_text(encoding="utf-8"))
    forbidden = {"command", "path", "digest", "sha256", "argv", "env", "endpoint", "secret"}
    for target in registry["targets"]:
        assert not (set(target) & forbidden)
    cpu = next(target for target in registry["targets"] if target["target_id"] == "local-vllm-cpu")
    vast = next(
        target for target in registry["targets"] if target["target_id"] == "private-vast-gpu"
    )
    assert cpu["secret_refs"] == []
    assert vast["secret_refs"] == ["vast-api-key", "vast-ssh-private-key"]
    assert cpu["reconcile_s"] >= cpu["deadlines_s"]["status"]
    assert '"schema": "ne-inference-adapter-digests-v1"' in source
    for forbidden in ('"operations"', '"verified_files"', '"environment"', '"path"', '"argv"'):
        assert forbidden not in source


_URLLIB3_WHEEL_ROW = {
    "filename": "urllib3-2.8.0-py3-none-any.whl",
    "name": "urllib3",
    "sha256": "0cf3cae568d36aa9576b28dfb35f11328f1cb974ca7647d9475ebb86c75ac6e3",
    "version": "2.8.0",
}


def test_vast_offline_lock_urllib3_row_is_the_2_8_0_wheel() -> None:
    # GHSA-vxq7-64xx-v4gw, GHSA-8988-9cw3-xx77 and GHSA-gh4c-6fx4-qh6g are fixed only in 2.8.0;
    # exact-row equality pins filename, version AND the PyPI wheel digest (C11).
    lock = json.loads(_VAST_LOCK.read_text(encoding="utf-8"))
    rows = [row for row in lock["wheels"] if row["name"] == "urllib3"]
    assert rows == [_URLLIB3_WHEEL_ROW]


def test_vast_offline_lock_digest_is_code_owned() -> None:
    raw = _VAST_LOCK.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == VAST_OFFLINE_LOCK_SHA256
    # The lock is committed in canonical form so a re-serialisation cannot move the digest.
    assert raw == (json.dumps(json.loads(raw), indent=2, sort_keys=True) + "\n").encode()


def test_systemd_unit_keeps_host_privilege_out_of_api_and_hardens_service() -> None:
    unit = _UNIT.read_text(encoding="utf-8")
    for setting in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "PrivateDevices=true",
        "RestrictSUIDSGID=true",
        "MemoryDenyWriteExecute=true",
        "AIR_GAP=true",
        "--air-gap-file /etc/neutron/inference-operator/environment",
    ):
        assert setting in unit
    assert "operator.sock" in unit
    assert "Group=10001" in unit and "--allowed-gid 10001" in unit
    assert "docker.sock" not in unit
    vast = _VAST.read_text(encoding="utf-8")
    assert "/etc/neutron/secrets/vast-ssh-private-key" in _INSTALL.read_text(encoding="utf-8")
    assert "IdentitiesOnly=yes" in vast and "UserKnownHostsFile=" in vast
    assert "/var/lib/neutron/inference-operator" in unit
    assert "--lifetime-lock /var/lib/neutron/inference-operator/operator.lock" in unit
    manifest = _MANIFEST.read_text(encoding="utf-8")
    assert '"NE_VAST_OPERATOR_TMP_ROOT": "/var/lib/neutron/inference-provider"' in manifest
    assert '"NE_VAST_OPERATOR_TMP_ROOT": "/run/neutron/inference-operator"' not in manifest
    assert '"NE_PROVIDER_GRANT_CLIENT"' in manifest
    server = _SERVER.read_text(encoding="utf-8")
    assert "socket_path.with_name" not in server
    assert "def acquire_lifetime_lock" in server
    socket_unit = _SOCKET_UNIT.read_text(encoding="utf-8")
    assert "ListenStream=/run/neutron/inference-operator/operator.sock" in socket_unit
    assert "FileDescriptorName=inference-operator" in socket_unit
    assert "SocketGroup=10001" in socket_unit and "SocketMode=0660" in socket_unit
    assert "--systemd-socket" in unit and "RuntimeDirectoryPreserve=yes" in unit
    assert "RestrictAddressFamilies=AF_UNIX AF_INET\n" in unit
    assert "IPAddressDeny=any\n" in unit
    assert "IPAddressAllow=localhost\n" in unit
    install = _INSTALL.read_text(encoding="utf-8")
    assert "systemd_version < 247" in install
    assert "/var/lib/neutron/vast" not in unit and "/etc/neutron/secrets" not in unit
    provider = _PROVIDER_UNIT.read_text(encoding="utf-8")
    for setting in (
        "User=10002",
        "Group=10002",
        "NetworkNamespacePath=/run/netns/neutron-inference-provider",
        "LoadCredential=vast-api-key:/etc/neutron/secrets/vast-api-key",
        "LoadCredential=vast-ssh-private-key:/etc/neutron/secrets/vast-ssh-private-key",
        "BindReadOnlyPaths=/run/neutron/inference-provider/hosts:/etc/hosts",
        "InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
    ):
        assert setting in provider
    condition = "ExecCondition=/usr/local/bin/ne-inference-provider-air-gap-condition"
    assert condition in provider
    assert "WantedBy=multi-user.target" not in provider
    provider_socket = _PROVIDER_SOCKET.read_text(encoding="utf-8")
    assert "FileDescriptorName=inference-provider" in provider_socket
    assert "SocketUser=root" in provider_socket
    assert "SocketGroup=10001" in provider_socket and "SocketMode=0660" in provider_socket
    network = _PROVIDER_NETWORK.read_text(encoding="utf-8")
    network_unit = (
        _ROOT / "deploy/inference-operator/ne-inference-provider-network.service"
    ).read_text(encoding="utf-8")
    assert condition in network_unit
    assert "WantedBy=multi-user.target" not in network_unit
    assert network.index("ne-inference-provider-air-gap-condition") < network.index(
        "getent ahostsv4"
    )
    assert network.index('if [[ "${1:-}" == teardown ]]') < network.index(
        "ne-inference-provider-air-gap-condition"
    )
    assert "policy drop" in network
    assert "ip daddr @provider_v4 tcp dport 443 accept" in network
    assert "ip daddr . tcp dport @ssh_grants_v4 accept" in network
    assert "0.0.0.0/0" not in network
    assert "sysctl" not in network and "/proc/sys/net/ipv4/ip_forward" in network
    grant = _PROVIDER_GRANT.read_text(encoding="utf-8")
    assert "instance_id != authority_instance_id" in grant
    assert 'f"https://console.vast.ai/api/v0/instances/{instance_id}/"' in grant
    assert "urllib.request.ProxyHandler({})" in grant and "class NoRedirect" in grant
    assert "vastai" not in grant.lower()
    assert 'request["target_id"] != "private-vast-gpu"' in grant
    assert "update element inet ne_inference_provider ssh_grants_v4" in grant
    assert '["/usr/sbin/nft", "-f", "-"]' in grant
    assert '"/usr/sbin/ip", "netns", "exec"' not in grant
    assert 'Path("/proc/self/ns/net")' in grant
    assert "RestrictNamespaces=true" in provider
    subprocess_call = grant.split("result = run(", 1)[1].split("input=rules", 1)[0]
    assert "request[" not in subprocess_call
    install = _INSTALL.read_text(encoding="utf-8")
    assert "printf 'AIR_GAP=true\\n'" in install
    assert 'b"AIR_GAP=false\\n"' in install
    assert "EnvironmentFile=" not in unit
    assert "path.is_symlink()" in install and "stat.S_IMODE(info.st_mode) != 0o640" in install
    assert "/usr/sbin/visudo -cf" in install and "provider-ssh-grant-client" in install
    assert "provider-network-refresh.sh" in install
    refresh = _PROVIDER_REFRESH.read_text(encoding="utf-8")
    assert "flock -x 9" in refresh
    assert refresh.index("stop ne-inference-provider-broker.service") < refresh.index(
        "restart ne-inference-provider-network.service"
    )
    assert refresh.index("stop ne-inference-provider-broker.service") < refresh.index(
        "stop ne-inference-provider-network.service"
    )
    assert "provider-network-policy.sh teardown" in refresh
    assert refresh.index("ne-inference-provider-air-gap-condition") < refresh.index(
        "restart ne-inference-provider-network.service"
    )
    assert "start ne-inference-provider-broker.socket" in refresh
    timer = _PROVIDER_REFRESH_TIMER.read_text(encoding="utf-8")
    assert "OnUnitActiveSec=12h" in timer
    assert "RandomizedDelaySec=5min" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
    assert "WantedBy=multi-user.target" not in timer
    assert "cleanup_socket=False" in server
    broker_server = (
        _ROOT / "tools/ne-inference-operator/src/ne/inference_operator/provider_broker.py"
    ).read_text(encoding="utf-8")
    assert "cleanup_socket=False" in broker_server


def test_ssh_grant_consumes_exact_active_transition_and_root_endpoint(tmp_path: Path) -> None:
    grant_module = _ssh_grant_module()
    now = datetime(2026, 8, 20, 12, tzinfo=UTC)
    environment = tmp_path / "environment"
    environment.write_text("AIR_GAP=false\n", encoding="ascii")
    environment.chmod(0o640)
    namespace = tmp_path / "netns"
    namespace.write_text("namespace", encoding="ascii")
    receipt = tmp_path / "egress-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "addresses": ["1.1.1.1"],
                "authority": "console.vast.ai:443",
                "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                "netns_inode": namespace.stat().st_ino,
                "policy_sha256": grant_module.POLICY_SHA256,
                "schema": "ne-inference-provider-egress-v1",
            }
        ),
        encoding="utf-8",
    )
    receipt.chmod(0o640)
    authorities = tmp_path / "grants"
    authorities.mkdir(mode=0o700)

    def request_for(transition_id: str) -> dict[str, object]:
        expiry = (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        name = hashlib.sha256(transition_id.encode()).hexdigest()
        authority = authorities / f"{name}.json"
        authority.write_text(
            json.dumps(
                {
                    "expires_at": expiry,
                    "instance_id": 42,
                    "operation": "status",
                    "owner_pid": 123,
                    "owner_start_ticks": "456",
                    "policy_sha256": grant_module.POLICY_SHA256,
                    "schema": "ne-inference-provider-grant-authority-v1",
                    "target_id": "private-vast-gpu",
                    "transition_id": transition_id,
                }
            ),
            encoding="utf-8",
        )
        authority.chmod(0o600)
        return {
            "instance_id": 42,
            "policy_sha256": grant_module.POLICY_SHA256,
            "schema": "ne-inference-provider-ssh-grant-v1",
            "target_id": "private-vast-gpu",
            "transition_id": transition_id,
        }

    calls: list[bytes] = []
    clock = [now]

    def run(_argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(kwargs["input"])  # type: ignore[arg-type]
        return subprocess.CompletedProcess(_argv, 0)

    def resolve_endpoint(_instance_id: int) -> tuple[str, int]:
        # The untrusted request has no expiry to extend. A delayed provider
        # observation only consumes the root authority's remaining lifetime.
        clock[0] += timedelta(seconds=2)
        return "8.8.8.8", 2222

    arguments = {
        "environment_path": environment,
        "authority_dir": authorities,
        "receipt_path": receipt,
        "observation_path": tmp_path / "provider-observation.json",
        "network_namespace": namespace,
        "owner_uid": os.getuid(),
        "reader_gid": os.getgid(),
        "now": lambda: clock[0],
        "process_start": lambda _pid: "456",
        "endpoint_resolver": resolve_endpoint,
        "run": run,
    }
    request = request_for("status-one")
    grant_module.grant(request, **arguments)
    assert calls == [
        b"update element inet ne_inference_provider ssh_grants_v4 { 8.8.8.8 . 2222 timeout 298s }\n"
    ]
    observation = json.loads((tmp_path / "provider-observation.json").read_text())
    assert observation == {
        "address": "8.8.8.8",
        "instance_id": 42,
        "port": 2222,
        "schema": "ne-inference-provider-observation-v1",
    }

    # The active authority was atomically consumed: an otherwise identical
    # renewal cannot extend the nft grant or run nft a second time.
    with pytest.raises(SystemExit):
        grant_module.grant(request, **arguments)
    assert len(calls) == 1

    alternate = request_for("status-two")
    alternate |= {"address": "1.1.1.1", "port": 443}
    with pytest.raises(SystemExit):
        grant_module.grant(alternate, **arguments)
    assert len(calls) == 1

    invented = dict(request)
    invented["transition_id"] = "invented-transition"
    with pytest.raises(SystemExit):
        grant_module.grant(invented, **arguments)
    assert len(calls) == 1

    wrong_namespace = request_for("status-three")
    other_namespace = tmp_path / "wrong-netns"
    other_namespace.write_text("other", encoding="ascii")
    with pytest.raises(SystemExit):
        grant_module.grant(wrong_namespace, **(arguments | {"network_namespace": other_namespace}))
    assert len(calls) == 1


def test_privileged_endpoint_verifier_is_one_fixed_capped_https_read(tmp_path: Path) -> None:
    grant_module = _ssh_grant_module()
    credential = tmp_path / "vast-api-key"
    credential.write_text("fixed-test-token", encoding="ascii")
    credential.chmod(0o400)
    requests: list[urllib.request.Request] = []

    class Response:
        status = 200

        def __init__(self, url: str, port: object) -> None:
            self.url = url
            self.port = port

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return self.url

        def read(self, limit: int) -> bytes:
            assert limit == 65537
            return json.dumps(
                {
                    "instances": {
                        "actual_status": "running",
                        "id": 42,
                        "public_ipaddr": "8.8.4.4",
                        "ports": {"22/tcp": [{"HostPort": self.port}]},
                    }
                }
            ).encode()

    class Opener:
        def __init__(self, response_url: str, port: object = "22022") -> None:
            self.response_url = response_url
            self.port = port

        def open(self, request: urllib.request.Request, *, timeout: int):
            assert timeout == 15
            requests.append(request)
            return Response(self.response_url, self.port)

    url = "https://console.vast.ai/api/v0/instances/42/"
    assert grant_module._provider_endpoint(
        42,
        credential_path=credential,
        credential_owner_uid=os.getuid(),
        opener=Opener(url),
    ) == ("8.8.4.4", 22022)
    request = requests[0]
    assert request.full_url == url
    assert request.get_method() == "GET"
    assert request.headers["Authorization"] == "Bearer fixed-test-token"

    with pytest.raises(SystemExit):
        grant_module._provider_endpoint(
            42,
            credential_path=credential,
            credential_owner_uid=os.getuid(),
            opener=Opener("https://attacker.invalid/stolen"),
        )
    assert len(requests) == 2
    for malformed_port in (" 22022", "022022", "22022.0", 22022.0, True):
        with pytest.raises(SystemExit):
            grant_module._provider_endpoint(
                42,
                credential_path=credential,
                credential_owner_uid=os.getuid(),
                opener=Opener(url, malformed_port),
            )


@pytest.mark.skipif(sys.platform != "linux", reason="systemd IP filtering is Linux-only")
def test_root_operator_systemd_sandbox_allows_only_loopback() -> None:
    """Execute the production unit's network policy, not a test-local facsimile."""
    unit = _UNIT.read_text(encoding="utf-8")

    def directive(name: str) -> str:
        values = [
            line.split("=", 1)[1] for line in unit.splitlines() if line.startswith(f"{name}=")
        ]
        assert len(values) == 1, f"{name} must have one exact production value"
        return values[0]

    properties = {
        "RestrictAddressFamilies": directive("RestrictAddressFamilies"),
        "IPAddressDeny": directive("IPAddressDeny"),
        "IPAddressAllow": directive("IPAddressAllow"),
    }
    assert properties == {
        "RestrictAddressFamilies": "AF_UNIX AF_INET",
        "IPAddressDeny": "any",
        "IPAddressAllow": "localhost",
    }
    version = subprocess.run(
        ["/usr/bin/systemd", "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()[0]
    fields = version.split()
    assert fields[0] == "systemd" and fields[1].isdigit() and int(fields[1]) >= 247
    assert Path("/run/systemd/system").is_dir(), "Linux proof requires systemd as PID 1"
    assert Path("/usr/bin/systemd-run").is_file()
    assert Path("/usr/bin/sudo").is_file()

    route_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        route_probe.connect(("192.0.2.1", 9))
        non_loopback_ip = route_probe.getsockname()[0]
    finally:
        route_probe.close()
    assert not non_loopback_ip.startswith("127.")

    loopback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    non_loopback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        loopback.bind(("127.0.0.1", 0))
        loopback.listen(1)
        non_loopback.bind((non_loopback_ip, 0))
        non_loopback.listen(1)
        child = """
import errno
import socket
import sys

with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2):
    pass
try:
    socket.create_connection((sys.argv[2], int(sys.argv[3])), timeout=2)
except OSError as exc:
    if not isinstance(exc, TimeoutError) and exc.errno not in {errno.EACCES, errno.EPERM}:
        raise
else:
    raise SystemExit("non-loopback TCP escaped the production systemd policy")
"""
        command = [
            "/usr/bin/sudo",
            "-n",
            "/usr/bin/systemd-run",
            "--system",
            "--quiet",
            "--wait",
            "--collect",
            "--pipe",
            f"--unit=ne-pr661-loopback-{os.getpid()}",
            *(f"--property={name}={value}" for name, value in properties.items()),
            "/usr/bin/python3",
            "-c",
            child,
            str(loopback.getsockname()[1]),
            non_loopback_ip,
            str(non_loopback.getsockname()[1]),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, (result.stdout, result.stderr)
    finally:
        loopback.close()
        non_loopback.close()


@pytest.mark.skipif(sys.platform != "linux", reason="systemd AF_NETLINK filtering is Linux-only")
def test_provider_unit_admits_only_required_netlink_family_for_root_nft_helper() -> None:
    provider = _PROVIDER_UNIT.read_text(encoding="utf-8")
    directives = [
        line.split("=", 1)[1]
        for line in provider.splitlines()
        if line.startswith("RestrictAddressFamilies=")
    ]
    assert directives == ["AF_UNIX AF_INET AF_INET6 AF_NETLINK"]
    child = """
import socket

handle = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 12)
handle.close()
"""
    result = subprocess.run(
        [
            "/usr/bin/sudo",
            "-n",
            "/usr/bin/systemd-run",
            "--system",
            "--quiet",
            "--wait",
            "--collect",
            "--pipe",
            f"--unit=ne-pr661-netlink-{os.getpid()}",
            f"--property=RestrictAddressFamilies={directives[0]}",
            "/usr/bin/python3",
            "-c",
            child,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
