#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

[[ "$(id -u)" == 0 ]] || { echo "root is required" >&2; exit 1; }
IFS=' ' read -r systemd_name systemd_version _ < <(/usr/bin/systemd --version)
if [[ "$systemd_name" != systemd || ! "$systemd_version" =~ ^[0-9]+$ ]] \
  || (( systemd_version < 247 )); then
  echo "systemd >= 247 with LoadCredential and IPAddressAllow/Deny is required" >&2
  exit 1
fi
source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
destination=/opt/neutron/inference-operator
policy=/etc/neutron/inference-operator
vast_cli="$destination/vendor/vastai/bin/vastai"
vast_lock_source="$source_root/deploy/inference-operator/vastai-offline-lock.json"
vast_ssh_key=/etc/neutron/secrets/vast-ssh-private-key
provider_gid=10002
/usr/bin/python3 - "$destination/vendor/vastai" "$vast_cli" "$vast_ssh_key" <<'PY'
import stat, sys
from pathlib import Path

root, cli, key = map(Path, sys.argv[1:])
for path in (root, cli, key):
    try:
        info = path.lstat()
    except OSError:
        raise SystemExit("preinstalled provider artifact is required")
    if path.is_symlink() or path.resolve(strict=True) != path or info.st_uid != 0:
        raise SystemExit("preinstalled provider artifact is unsafe")
if not stat.S_ISDIR(root.lstat().st_mode) or not stat.S_ISREG(cli.lstat().st_mode):
    raise SystemExit("preinstalled provider bundle is unsafe")
for path in root.rglob("*"):
    info = path.lstat()
    if path.is_symlink() or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise SystemExit("preinstalled provider bundle is unsafe")
if not stat.S_ISREG(key.lstat().st_mode) or stat.S_IMODE(key.lstat().st_mode) != 0o600:
    raise SystemExit("preinstalled GPU SSH key is unsafe")
PY
[[ -x "$vast_cli" ]] || {
  echo "preinstalled Vast CLI is required; this installer never downloads it" >&2
  exit 1
}
install -o root -g root -m 644 \
  "$source_root/deploy/inference-operator/ne-inference-provider.sysusers" \
  /usr/lib/sysusers.d/ne-inference-provider.conf
/usr/bin/systemd-sysusers /usr/lib/sysusers.d/ne-inference-provider.conf
install -o root -g "$provider_gid" -m 440 "$vast_lock_source" \
  "$destination/vendor/vastai/offline-lock.json"
chown -R root:"$provider_gid" "$destination/vendor/vastai"
find "$destination/vendor/vastai" -type d -exec chmod 550 {} +
find "$destination/vendor/vastai" -type f -exec chmod 440 {} +
chmod 550 "$vast_cli"
PYTHONPATH="$source_root/tools/ne-inference-operator/src" /usr/bin/python3 - \
  "$destination/vendor/vastai" "$provider_gid" <<'PY'
import sys
from pathlib import Path

from ne.inference_operator.vast_bundle import validate_vast_bundle

validate_vast_bundle(Path(sys.argv[1]), reader_gid=int(sys.argv[2]))
PY
/usr/bin/python3 - "$vast_ssh_key" <<'PY'
import stat, sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    info = path.lstat()
except OSError:
    raise SystemExit("preinstalled GPU SSH key is required")
if (
    not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path
    or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
    or stat.S_IMODE(info.st_mode) != 0o600
):
    raise SystemExit("preinstalled GPU SSH key is unsafe")
PY

install -d -o root -g "$provider_gid" -m 750 "$destination" "$destination/bin" "$policy"
install -o root -g root -m 500 \
  "$source_root/deploy/inference-operator/cpu-vllm-target.sh" \
  "$destination/bin/cpu-vllm-target.sh"
install -o root -g "$provider_gid" -m 550 \
  "$source_root/deploy/inference-operator/vast-rig.sh" \
  "$destination/bin/vast-rig.sh"
install -o root -g root -m 500 \
  "$source_root/deploy/inference-operator/provider-network-policy.sh" \
  "$destination/bin/provider-network-policy.sh"
install -o root -g root -m 500 \
  "$source_root/deploy/inference-operator/provider-network-refresh.sh" \
  "$destination/bin/provider-network-refresh.sh"
install -o root -g root -m 500 \
  "$source_root/deploy/inference-operator/provider-ssh-grant.py" \
  "$destination/bin/provider-ssh-grant"
install -o root -g "$provider_gid" -m 550 \
  "$source_root/deploy/inference-operator/provider-ssh-grant-client.sh" \
  "$destination/bin/provider-ssh-grant-client"
install -o root -g root -m 400 \
  "$source_root/deploy/inference-operator/provider-egress-policy.json" \
  "$destination/provider-egress-policy.json"
install -o root -g root -m 440 \
  "$source_root/deploy/inference-operator/ne-inference-provider-sudoers" \
  /etc/sudoers.d/ne-inference-provider
/usr/sbin/visudo -cf /etc/sudoers.d/ne-inference-provider >/dev/null
install -o root -g "$provider_gid" -m 640 \
  "$source_root/deploy/inference-operator/registry.json" \
  "$policy/registry.json"
if [[ ! -e "$policy/environment" ]]; then
  umask 077
  printf 'AIR_GAP=true\n' > "$policy/environment"
fi
chown root:"$provider_gid" "$policy/environment"
chmod 640 "$policy/environment"
/usr/bin/python3 - "$policy/environment" "$provider_gid" <<'PY'
import stat, sys
from pathlib import Path
path = Path(sys.argv[1]); gid = int(sys.argv[2]); info = path.lstat()
if (
    path.is_symlink() or path.resolve(strict=True) != path
    or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != gid
    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o640
):
    raise SystemExit("operator environment must be canonical root:provider 0640 regular file")
if path.read_bytes() not in {b"AIR_GAP=true\n", b"AIR_GAP=false\n"}:
    raise SystemExit("operator environment must contain exactly one AIR_GAP boolean")
PY

/usr/bin/python3 - "$destination" "$policy/adapter-manifest.json" <<'PY'
import hashlib, json, os, sys, tempfile
from pathlib import Path

root, destination = map(Path, sys.argv[1:])
cpu = root / "bin/cpu-vllm-target.sh"
vast = root / "bin/vast-rig.sh"
vast_cli = root / "vendor/vastai/bin/vastai"
document = {
    "schema": "ne-inference-adapter-digests-v1",
    "digests": {
        "cpu-script": hashlib.sha256(cpu.read_bytes()).hexdigest(),
        "vast-script": hashlib.sha256(vast.read_bytes()).hexdigest(),
        "vast-cli": hashlib.sha256(vast_cli.read_bytes()).hexdigest(),
    },
}
payload=(json.dumps(document,sort_keys=True,separators=(",", ":"))+"\n").encode()
fd, temporary=tempfile.mkstemp(prefix=destination.name+".",dir=destination.parent)
try:
    with os.fdopen(fd,"wb") as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.fchown(handle.fileno(), 0, 10002)
    os.chmod(temporary,0o640)
    os.replace(temporary,destination)
    directory=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)
finally:
    try: os.unlink(temporary)
    except FileNotFoundError: pass
PY
