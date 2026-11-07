#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# This script is never a user-configurable command surface.  The host operator
# invokes exactly one of these three words from its closed adapter code.
[[ "${NE_INFERENCE_OPERATOR:-}" == "1" ]] || exit 70
operation="${1:-}"
[[ "$#" == 1 && "$operation" =~ ^(status|start|stop)$ ]] || exit 70

receipt="${NE_CPU_COMPOSE_RECEIPT:-/var/lib/neutron/inference-operator/cpu-compose-receipt.json}"
lock="${NE_UPGRADE_LOCK:-/run/lock/neutron-live-upgrade.lock}"
python="${NE_OPERATOR_PYTHON:-/usr/bin/python3}"
docker="${NE_OPERATOR_DOCKER:-/usr/bin/docker}"
[[ -x "$python" && -x "$docker" ]] || exit 71
command -v flock >/dev/null 2>&1 || exit 71
exec 9>"$lock"
# Target operations are concurrent readers of the deployed Compose/policy set.
# The host deployer is the only exclusive writer and cannot overlap any of
# them; same-target single-flight remains enforced by the host state machine.
flock -s 9

validated="$("$python" - "$receipt" "${NE_OPERATOR_OWNER_UID:-0}" <<'PY'
import json, os, stat, sys
from pathlib import Path
from urllib.parse import urlsplit

path = Path(sys.argv[1])
owner = int(sys.argv[2])
try:
    info = path.lstat()
    value = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
if (
    not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path
    or not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1
    or stat.S_IMODE(info.st_mode) != 0o600
    or not isinstance(value, dict)
    or set(value) != {"schema", "project_name", "working_dir", "env_file", "compose_files", "service", "semantic_hash", "runtime_config_label", "image", "completion_url", "model"}
    or value["schema"] != "ne-vllm-cpu-compose-receipt-v1"
    or value["service"] != "vllm-cpu"
    or value["model"] != "qwen3-0.6b"
):
    raise SystemExit(1)
for key in ("project_name", "working_dir", "env_file", "semantic_hash", "runtime_config_label", "image", "completion_url", "model"):
    if not isinstance(value[key], str) or not value[key] or "\n" in value[key]:
        raise SystemExit(1)
if len(value["semantic_hash"]) != 64 or any(c not in "0123456789abcdef" for c in value["semantic_hash"]):
    raise SystemExit(1)
if not 32 <= len(value["runtime_config_label"]) <= 128 or any(c not in "0123456789abcdef" for c in value["runtime_config_label"]):
    raise SystemExit(1)
completion = urlsplit(value["completion_url"])
if (
    completion.scheme != "http" or completion.netloc != "127.0.0.1:8091"
    or completion.hostname != "127.0.0.1" or completion.port != 8091
    or completion.path != "/v1/completions" or completion.username is not None
    or completion.password is not None or completion.query or completion.fragment
):
    raise SystemExit(1)
files = value["compose_files"]
if not isinstance(files, list) or not files:
    raise SystemExit(1)
for item in files:
    candidate = Path(item)
    try:
        current = candidate.lstat()
    except OSError:
        raise SystemExit(1)
    if (
        not candidate.is_absolute() or candidate.is_symlink()
        or candidate.resolve(strict=True) != candidate
        or not stat.S_ISREG(current.st_mode) or current.st_uid != owner or current.st_nlink != 1
        or current.st_mode & 0o022
        or "," in item
    ):
        raise SystemExit(1)
env_file = Path(value["env_file"])
try:
    env_info = env_file.lstat()
except OSError:
    raise SystemExit(1)
if (
    not env_file.is_absolute() or env_file.is_symlink()
    or env_file.resolve(strict=True) != env_file
    or not stat.S_ISREG(env_info.st_mode) or env_info.st_uid != owner or env_info.st_nlink != 1
    or env_info.st_mode & 0o077
):
    raise SystemExit(1)
print(value["project_name"])
print(value["working_dir"])
print(value["env_file"])
print(value["semantic_hash"])
print(value["runtime_config_label"])
print(value["image"])
print(value["completion_url"])
print(value["model"])
print(len(files))
print(*files, sep="\n")
PY
)" || exit 72
admitted=()
while IFS= read -r line; do admitted+=("$line"); done <<<"$validated"

(( ${#admitted[@]} >= 9 )) || exit 72
project="${admitted[0]}"; working_dir="${admitted[1]}"; env_file="${admitted[2]}"
semantic_hash="${admitted[3]}"; runtime_config_label="${admitted[4]}"; image="${admitted[5]}"
completion_url="${admitted[6]}"; model="${admitted[7]}"; file_count="${admitted[8]}"
[[ "$file_count" =~ ^[1-9][0-9]*$ ]] || exit 72
(( ${#admitted[@]} == 9 + file_count )) || exit 72
compose=("$docker" compose --project-name "$project" --project-directory "$working_dir" --env-file "$env_file")
for (( index=0; index<file_count; index++ )); do
  compose+=(--file "${admitted[index+9]}")
done

resolved_hash="$("${compose[@]}" config --hash vllm-cpu | awk 'NF == 2 && $1 == "vllm-cpu" { print $2 }')"
[[ "$resolved_hash" == "$semantic_hash" ]] || exit 73
resolved_image="$("${compose[@]}" config --format json | "$python" -c \
  'import json,sys; d=json.load(sys.stdin); print(d["services"]["vllm-cpu"]["image"])')"
[[ "$resolved_image" == "$image" ]] || exit 73

container_id() { "${compose[@]}" ps --quiet vllm-cpu; }
is_ready() {
  local container health running container_image label_config label_files label_workdir
  container="$(container_id)"
  [[ "$container" =~ ^[0-9a-f]{12,64}$ ]] || return 3
  running="$($docker inspect --format '{{.State.Running}}' "$container")"
  [[ "$running" == true ]] || return 3
  container_image="$($docker inspect --format '{{.Config.Image}}' "$container")"
  [[ "$container_image" == "$image" ]] || return 4
  health="$($docker inspect --format '{{.State.Health.Status}}' "$container")"
  [[ "$health" == healthy ]] || return 4
  label_config="$($docker inspect --format '{{ index .Config.Labels "com.docker.compose.config-hash" }}' "$container")"
  [[ "$label_config" == "$runtime_config_label" ]] || return 4
  label_files="$($docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$container")"
  label_workdir="$($docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container")"
  local expected_files
  printf -v expected_files '%s,' "${admitted[@]:9}"
  expected_files="${expected_files%,}"
  [[ "$label_files" == "$expected_files" && "$label_workdir" == "$working_dir" ]] || return 4
  "$python" - "$completion_url" "$model" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

payload=json.dumps({"model":sys.argv[2],"prompt":"Reply with one token.","max_tokens":1,"temperature":0,"ignore_eos":True}).encode()
request=urllib.request.Request(sys.argv[1],data=payload,headers={"Content-Type":"application/json"},method="POST")
with urllib.request.build_opener(NoRedirect).open(request,timeout=90) as response:
    body=json.load(response)
if response.status != 200 or len(body.get("choices",())) != 1 or body.get("usage",{}).get("completion_tokens") != 1:
    raise SystemExit(1)
PY
  [[ "$(container_id)" == "$container" ]] || return 4
}

case "$operation" in
  status)
    is_ready
    ;;
  start)
    "${compose[@]}" up --detach --no-deps --pull never --force-recreate --wait vllm-cpu >/dev/null
    is_ready
    ;;
  stop)
    # Target-scoped stop only: never down/rm/kill and never any other service.
    "${compose[@]}" stop --timeout 30 vllm-cpu >/dev/null
    stopped_container="$(container_id)"
    [[ -z "$stopped_container" ]] || [[ "$($docker inspect --format '{{.State.Running}}' "$stopped_container")" == false ]]
    ;;
esac
