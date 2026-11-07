# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Below-the-seam unit tests for SopsCodec's subprocess wiring.

These tests use a FAKE ``sops`` executable (a tiny shell script implementing
``--encrypt`` as a reversible marker-wrap and ``--decrypt`` as the inverse) so
they run unconditionally in CI. The real-``sops`` seam is covered, skip-gated,
by test_file_store_sops_conformance.py — these tests cover only OUR plumbing.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from ne.secrets.codecs import SopsCodec
from ne.secrets.errors import SecretsBackendError

_FAKE_SOPS = """#!/bin/sh
set -e
mode=""
config=""
input=""
while [ $# -gt 0 ]; do
  case "$1" in
    --encrypt) mode="encrypt" ;;
    --decrypt) mode="decrypt" ;;
    --input-type) shift ;;
    --output-type) shift ;;
    --config) shift; config="$1" ;;
    --filename-override) shift ;;
    *) input="$1" ;;
  esac
  shift
done
if [ "$mode" = "encrypt" ]; then
  printf '{"sops": "fake", "wrapped": %s}' "$(cat "$input")"
elif [ "$mode" = "decrypt" ]; then
  python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['wrapped']))" < "$input"
else
  echo "unknown mode" 1>&2
  exit 2
fi
"""

_FAILING_SOPS = """#!/bin/sh
echo "boom: something went wrong" 1>&2
exit 1
"""

_SLEEPY_SOPS = """#!/bin/sh
sleep 5
"""


def _install_fake_sops(tmp_path: Path, script: str) -> Path:
    binary = tmp_path / "fake-sops"
    binary.write_text(script)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return binary


def test_round_trip_through_fake_binary(tmp_path: Path) -> None:
    binary = _install_fake_sops(tmp_path, _FAKE_SOPS)
    config = tmp_path / ".sops.yaml"
    config.write_text("creation_rules: []\n")
    codec = SopsCodec(
        binary=str(binary), config=config, extra_env={"SOPS_AGE_KEY_FILE": "/fake/key"}
    )

    out_path = tmp_path / "secrets.json"
    doc = {"format": "ne-secrets/kv2", "format_version": 1, "secrets": {"app/db": []}}
    codec.dump(doc, out_path)

    raw = out_path.read_text(encoding="utf-8")
    assert json.loads(raw)["sops"] == "fake"

    loaded = codec.load(out_path)
    assert loaded == doc


def test_missing_file_returns_empty_document(tmp_path: Path) -> None:
    binary = _install_fake_sops(tmp_path, _FAKE_SOPS)
    codec = SopsCodec(binary=str(binary))
    loaded = codec.load(tmp_path / "does-not-exist.json")
    assert loaded == {"format": "ne-secrets/kv2", "format_version": 1, "secrets": {}}


def test_nonzero_exit_raises_backend_error_with_stderr(tmp_path: Path) -> None:
    binary = _install_fake_sops(tmp_path, _FAILING_SOPS)
    codec = SopsCodec(binary=str(binary))
    out_path = tmp_path / "secrets.json"
    with pytest.raises(SecretsBackendError) as excinfo:
        codec.dump({"format": "ne-secrets/kv2", "format_version": 1, "secrets": {}}, out_path)
    assert "boom: something went wrong" in str(excinfo.value)


def test_missing_binary_raises_backend_error(tmp_path: Path) -> None:
    codec = SopsCodec(binary=str(tmp_path / "no-such-binary"))
    doc = {"format": "ne-secrets/kv2", "format_version": 1, "secrets": {}}
    with pytest.raises(SecretsBackendError):
        codec.dump(doc, tmp_path / "out.json")


def test_env_passthrough_visible_to_subprocess(tmp_path: Path) -> None:
    # A probe script that writes the env var it sees to a side-channel file,
    # so the assertion doesn't depend on capturing stdout/stderr on success.
    probe = tmp_path / "env-probe-sops"
    probe.write_text(
        "#!/bin/sh\n"
        'echo "SOPS_AGE_KEY_FILE=$SOPS_AGE_KEY_FILE" > "$(dirname "$0")/env_seen.txt"\n'
        'echo \'{"format": "ne-secrets/kv2", "format_version": 1, "secrets": {}}\'\n'
    )
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    codec = SopsCodec(binary=str(probe), extra_env={"SOPS_AGE_KEY_FILE": "/fake/key/path"})
    out_path = tmp_path / "secrets.json"
    codec.dump({"format": "ne-secrets/kv2", "format_version": 1, "secrets": {}}, out_path)

    seen = (tmp_path / "env_seen.txt").read_text()
    assert "SOPS_AGE_KEY_FILE=/fake/key/path" in seen


def test_plaintext_tempfile_is_unlinked_after_dump(tmp_path: Path) -> None:
    binary = _install_fake_sops(tmp_path, _FAKE_SOPS)
    codec = SopsCodec(binary=str(binary))
    out_path = tmp_path / "secrets.json"
    codec.dump({"format": "ne-secrets/kv2", "format_version": 1, "secrets": {}}, out_path)

    leftovers = [p for p in tmp_path.iterdir() if p not in (out_path, binary)]
    assert leftovers == [], f"unexpected leftover files: {leftovers}"


def test_plaintext_tempfile_unlinked_even_on_failure(tmp_path: Path) -> None:
    binary = _install_fake_sops(tmp_path, _FAILING_SOPS)
    codec = SopsCodec(binary=str(binary))
    out_path = tmp_path / "secrets.json"
    with pytest.raises(SecretsBackendError):
        codec.dump({"format": "ne-secrets/kv2", "format_version": 1, "secrets": {}}, out_path)

    leftovers = [p for p in tmp_path.iterdir() if p not in (binary,)]
    assert leftovers == [], f"unexpected leftover files: {leftovers}"


def test_config_flag_passed_when_config_given(tmp_path: Path) -> None:
    # A probe script that just dumps argv so we can assert --config was passed.
    probe = tmp_path / "probe-sops"
    probe.write_text(
        "#!/bin/sh\n"
        'echo "$@" > "$(dirname "$0")/argv.txt"\n'
        'echo \'{"format": "ne-secrets/kv2", "format_version": 1, "secrets": {}}\'\n'
    )
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    config = tmp_path / ".sops.yaml"
    config.write_text("creation_rules: []\n")
    codec = SopsCodec(binary=str(probe), config=config)
    doc = {"format": "ne-secrets/kv2", "format_version": 1, "secrets": {}}
    out_path = tmp_path / "out.json"
    codec.dump(doc, out_path)

    argv = (tmp_path / "argv.txt").read_text()
    assert "--encrypt" in argv
    assert "--input-type json" in argv
    assert "--output-type json" in argv
    assert f"--config {config}" in argv
    # Creation-rule matching must run against the destination filename, not the
    # random plaintext tempfile name (path-specific path_regex rules).
    assert f"--filename-override {out_path}" in argv


def test_load_timeout_raises_backend_error(tmp_path: Path) -> None:
    # A sops that sleeps 5s vs a 0.5s cap must raise (not hang forever). On revert
    # (no timeout=) the fake finishes after 5s and this raises-assert fails.
    binary = _install_fake_sops(tmp_path, _SLEEPY_SOPS)
    encrypted = tmp_path / "secrets.json"
    encrypted.write_text("{}", encoding="utf-8")  # must exist so load() runs sops
    codec = SopsCodec(binary=str(binary), timeout=0.5)
    with pytest.raises(SecretsBackendError, match="timed out"):
        codec.load(encrypted)


def test_dump_timeout_raises_backend_error(tmp_path: Path) -> None:
    binary = _install_fake_sops(tmp_path, _SLEEPY_SOPS)
    codec = SopsCodec(binary=str(binary), timeout=0.5)
    with pytest.raises(SecretsBackendError, match="timed out"):
        codec.dump(
            {"format": "ne-secrets/kv2", "format_version": 1, "secrets": {}}, tmp_path / "o.json"
        )


def test_subprocess_called_with_devnull_stdin_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _install_fake_sops(tmp_path, _FAKE_SOPS)
    codec = SopsCodec(binary=str(binary), timeout=12.5)
    out_path = tmp_path / "secrets.json"

    real_run = subprocess.run
    recorded: list[dict[str, object]] = []

    def _recording_run(cmd, *args, **kwargs):
        recorded.append({"stdin": kwargs.get("stdin"), "timeout": kwargs.get("timeout")})
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _recording_run)

    doc = {"format": "ne-secrets/kv2", "format_version": 1, "secrets": {"app/db": []}}
    codec.dump(doc, out_path)
    assert codec.load(out_path) == doc

    assert recorded, "sops subprocess.run was never invoked"
    for call in recorded:
        assert call["stdin"] is subprocess.DEVNULL
        assert call["timeout"] == 12.5
