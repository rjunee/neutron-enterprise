#!/usr/bin/env bash
# vast-rig.sh — provision + operate a Vast.ai GPU rig for the Neutron demo/eval.
#
# Wraps the `vastai` CLI so a GPU box can be brought up, served, and torn down
# from the terminal instead of the web console. Reads secrets from
# ~/ne-owner-secrets (never takes them on the command line), pins the SSH key,
# and serves the demo model configs (single / dual-TP / model-per-GPU).
#
# SPDX-License-Identifier: Apache-2.0
#
# Usage:
#   vast-rig.sh provision [--gpus N] [--gpu any|<exact gpu_name>] [--disk GB] [--price MAX] [--cuda MIN]
#     --gpu defaults to `any`. It used to default to L40S, which MIN_GPU_RAM_MB=48000
#     now excludes (real L40S offers report ~46068 MiB), so the default search could
#     never return an offer. `any` is the right default BECAUSE the floors do the
#     selecting: gpu_ram and compute_cap are applied even to an explicitly named
#     card, so cheapest-first cannot reach an ineligible one.
#   vast-rig.sh doctor                             # verify gcc/nvcc/CUDA_HOME/vllm/GPUs/disk — run this BEFORE pull/serve
#   vast-rig.sh status
#   vast-rig.sh ssh [-- <cmd...>]
#   vast-rig.sh pull <fp8|bf16|tiny|rerank|all>    # all = fp8+tiny; bf16 needs --disk 150
#   vast-rig.sh serve <single|dual|route|rerank>   # start vLLM for a config (dual/route need --gpus 2;
#                                                  #   rerank rides BESIDE an already-serving primary on GPU0)
#   vast-rig.sh endpoint [single|cheap|rerank]     # print the vLLM base URL (rerank: bare base, no /v1 —
#                                                  #   hand it to `ne demo up --gpu-rerank`)
#   vast-rig.sh stop                               # stop (keep disk, fast re-up)
#   vast-rig.sh start                              # re-start a stopped instance
#   vast-rig.sh destroy                            # destroy (no idle cost)
#   vast-rig.sh use <INSTANCE_ID>                  # adopt an already-running box
set -euo pipefail

# ---- config ---------------------------------------------------------------
OPERATOR_MODE="${NE_VAST_OPERATOR_MODE:-0}"
if [ "$OPERATOR_MODE" = "1" ]; then
  SECRETS=""
  VAST_KEY_FILE="${NE_VAST_KEY_FILE:-}"
  HF_TOKEN_FILE=""
  SSH_PUBKEY=""
  STATE_DIR="${NE_VAST_STATE_DIR:-}"
  VAST_VENV=""
else
  SECRETS="${NE_OWNER_SECRETS:-$HOME/ne-owner-secrets}"
  VAST_KEY_FILE="${NE_VAST_KEY_FILE:-$SECRETS/vast/api-key}"
  HF_TOKEN_FILE="${NE_HF_TOKEN_FILE:-$SECRETS/huggingface/ids.txt}"
  SSH_PUBKEY="${NE_SSH_PUBKEY:-$HOME/.ssh/id_ed25519.pub}"
  STATE_DIR="${NE_VAST_STATE_DIR:-$HOME/.config/ne-vast}"
  VAST_VENV="${NE_VAST_VENV:-$STATE_DIR/venv}"
fi
STATE="$STATE_DIR/instance.json"
OPERATOR_VAST_CLI="${NE_VAST_OPERATOR_CLI_FD:-}"
OPERATOR_SSH_KEY="${NE_VAST_SSH_KEY_FILE:-}"
OPERATOR_KNOWN_HOSTS="${NE_VAST_KNOWN_HOSTS_FILE:-}"

# Model + serve constants (these must stay in lockstep with
# packages/ne-serve MODEL_SOURCES).
#
# IMG is a VERBATIM copy of the product's own serve image
# (deploy/compose/serve/compose.yaml's digest-pinned `vllm/vllm-openai:v0.24.0`
# ref) — guard: test_rig_image_is_exactly_the_serve_compose_image asserts
# byte equality, digest included. This REPLACED a bare PyTorch base image plus
# a runtime `pip install vllm` after the 2026-07-29 live run hit, on that
# path, in order: (2) no gcc -> triton JIT-compiles CUDA utils at vLLM init ->
# "Failed to find C compiler"; (3) no nvcc / no CUDA_HOME (an nvcc DID exist,
# under the conda tree's site-packages/nvidia/cu13/bin, but nothing pointed
# /usr/local/cuda at it); (4) a FlashInfer header/toolkit version skew
# (`cuda_toolkit.h:41:8: error: "CUDA compiler and CUDA toolkit headers are
# incompatible"`); and (5) `pip install vllm` silently upgrading torch
# (2.4.0+cu124 -> 2.11.0+cu130) UNDER an already-made toolkit decision,
# invalidating it. The vllm-openai image is EXPECTED to ship gcc/nvcc/CUDA-
# toolkit/matched-torch/matched-FlashInfer pre-resolved (per the image's own
# published Dockerfile), which is WHY the pip-on-pytorch path was dropped
# rather than patched — those four gaps are expected not to recur on a box
# `provision` creates, but that expectation is DERIVED, not yet
# independently live-verified on Vast (dated hedge: NE-DEMO-RIG-MODEL,
# 2026-07-29). This is WHY
# `run_doctor`'s nvcc/CUDA_HOME check below is a WARN, not a hard fail: the
# predicate it would enforce is itself part of this unverified expectation.
# The gaps CAN still recur — confirmed or not — on a `use`-adopted box (a
# console-created template, or an operator override) that is NOT this image
# — that is what `doctor` (below) exists to catch, loudly, before the
# 11-minute weight download rather than after it.
#
# NE_RIG_IMAGE_TAG_ONLY=1 is a decided, loudly-logged escape hatch (never a
# silent default) for the unconfirmed case that `vastai create instance`
# rejects a digest-qualified `--image` ref: it strips the `@sha256:...` and
# provisions the bare tag instead, with a warning that this is an
# operator-visible downgrade from the verified-by-digest image. Default stays
# digest-pinned.
IMG="vllm/vllm-openai:v0.24.0@sha256:251eba5cc7c12fed0b75da22a9240e582b1c9e39f6fbc064f86781b963bd814f"
# shellcheck disable=SC2034  # parse anchor only, read by test_gpu_rig_model_guard.py's regex, not referenced in this script
NE_MENU_MODEL_ID=qwen3.6-35b-a3b            # ne.serve.models.MODEL_SOURCES key = pilot compose default
FP8=Qwen/Qwen3.6-35B-A3B-FP8                # single-GPU + route primary — official FP8 quant, ~37.5GB/41 shards
BF16=Qwen/Qwen3.6-35B-A3B                   # dual-GPU TP=2 full precision, ~70GB
SMALL=Qwen/Qwen3-0.6B                       # cheap routing lane = the sanctioned TINY_CI model, NOT a menu entry
VLLM_PIN=0.24.0                             # parity with deploy/compose/serve vllm/vllm-openai:v0.24.0 (guard: test_rig_image_tag_version_equals_vllm_pin); Qwen3.6 needs >=0.19.0
MIN_DRIVER_CUDA=12.8                        # DECLARED hand-derivation from IMG's own cu128 build (vllm 0.24.0 -> torch cu128 wheels) — not separately guarded; re-derive by hand if VLLM_PIN/IMG ever bump to a build with a different torch/CUDA requirement
INT_VLLM=10200                              # internal port for the primary model (maps to the offer's mapped port)
INT_CHEAP=10100                             # internal port for the cheap lane
# Where the shared selector rules live. The consumer-GPU deny list is DERIVED
# from rig_select.py rather than transliterated into the embedded search below:
# a second copy of that regex would drift from the one the other providers use,
# and the failure it prevents (a modded GeForce clearing both numeric floors and
# then failing CUDA init) costs real money each time it recurs.
RIG_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)"
# shellcheck source=lib/private-endpoint.sh
if [ "$OPERATOR_MODE" != "1" ]; then
  . "$RIG_LIB_DIR/private-endpoint.sh"
fi
MAX_MODEL_LEN=262144                         # the model's native max (no YaRN). 2026-08-10 live on RTX 6000 Ada: vLLM reported 408,819 tokens of KV at DEFAULT bf16, so this fits with 1.56x concurrency and no --kv-cache-dtype fp8. The prior 32768 was NOT a maximum: SPEC §870 records it as a 2026-07-16 floor to stop a 16384 overflow on Qwen2.5-32B on RunPod. Guarded by test_max_model_len_transcriptions_match_rig
SMALL_MAX_MODEL_LEN=40960                    # Qwen3-0.6B native maximum; the route's cheap lane must not inherit MAX_MODEL_LEN
SERVED_NAME=qwen3.6-35b-a3b                            # --served-model-name for single/route/dual; probing /v1/models with the HF repo id 404s on a HEALTHY serve — guarded by test_served_name_transcriptions_match_rig_in_the_four_enumerated_ops_docs and, for every OTHER file documenting this rig, test_every_rig_documenting_file_that_names_the_served_model_is_guarded
MIN_GPU_RAM_MB=48000                        # admits the live-proven 49140 MiB RTX 6000 Ada and excludes 46GB L40-class cards; full-native-context bf16 KV is not proven on those smaller cards. Guarded by test_min_gpu_ram_transcriptions_match_rig
MIN_COMPUTE_CAP=890                         # SM 8.9 (Ada) floor — the FP8 primary is an OFFICIAL FP8 quant and needs native FP8 (SM>=8.9) in vllm==$VLLM_PIN. 2026-08-04 live lesson: the cheapest 48GB card on the market is the Quadro RTX 8000 (Turing, SM 7.5, no FP8 kernels at all) and cheapest-first selected it — the serve then cannot load the model. Ampere (SM 8.0-8.6) is deliberately excluded too: block-quant FP8 there rides Marlin weight-only, unverified for this MoE checkpoint on this pin. Lower by hand only with a verified reason.
RERANK_MODEL=BAAI/bge-reranker-v2-m3        # GPU rerank lane (NE-SEARCH-LATENCY) — the SAME model the pilot's CPU tei-rerank serves; NOT a menu entry (score/pooling model, not generative)
RERANK_SERVED_NAME=rerank                   # the served name `ne demo up --gpu-rerank` expects (= ApiSettings.rerank_served_model_name default)
RERANK_GPU_MEM_UTIL=0.05                    # ~2.4GB of a 48GB card: ~1.2GB fp16 weights + activations; a pooling model has no KV cache, so this fits BESIDE the primary's default 0.9 without freeing it
RERANK_MAX_LEN=8192                         # the reranker's own max positions — NEVER $MAX_MODEL_LEN (that generative-lane value exceeds this model's max and vllm refuses to serve)

# vLLM flags recommended by the Qwen3.6-35B-A3B model card (huggingface.co/Qwen/Qwen3.6-35B-A3B):
# qwen3_coder tool-call parser, qwen3 reasoning parser (routes <think> into
# reasoning_content), --language-model-only drops the vision tower (text-only demo use).
# `hermes` was the Qwen2.5-era parser and will NOT emit tool_calls for this model.
PRIMARY_FLAGS="--enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 --language-model-only"
# Hybrid thinking is ENABLED on the primary (the template's default — no
# enable_thinking override anywhere in this script). The 2026-08-04
# "empty-content chat death" that briefly motivated a nothink default was
# live-isolated to an OUTPUT-BUDGET truncation, not a thinking defect: the
# demo overlay's private-default carried no max_output_tokens, so the gateway
# sent max_tokens=4096 and a grounded synthesis turn that reasons for
# ~12-14k chars hit finish_reason=length inside/just after <think>, which the
# qwen3 reasoning parser surfaces as EMPTY content (reproduced 2/3 on this
# turn shape; 4/4 natural stop at max_tokens=12288). The fix lives in the
# consumers' output budget (ne demo up emits max_output_tokens=12288 on the
# GPU private-default — tools/ne-cli/src/ne/cli/demo/live.py) and in the
# gateway's typed refusal on a reasoning-exhausted completion
# (ne.gateway.gateway) — never in disabling thinking here.

log()  { [ "$OPERATOR_MODE" = "1" ] || printf '\033[36m[vast-rig]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[vast-rig] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }

# vastai runs from a dedicated venv — a bare `pip install --user vastai` fails on
# an externally-managed (PEP 668) Python, which is the default on modern macOS.
OPERATOR_SECRET_HOME=""
operator_cleanup() {
  if [ -n "$OPERATOR_SECRET_HOME" ] && [ -d "$OPERATOR_SECRET_HOME" ]; then
    rm -rf -- "$OPERATOR_SECRET_HOME"
  fi
}
vastai() {
  if [ "$OPERATOR_MODE" = "1" ]; then
    HOME="$OPERATOR_SECRET_HOME" VASTAI_NO_UPDATE_CHECK=1 \
      env -u NE_VAST_KEY_FILE "$OPERATOR_VAST_CLI" "$@"
  else
    "$VAST_VENV/bin/vastai" "$@"
  fi
}
ensure_vastai() {
  if [ "$OPERATOR_MODE" = "1" ]; then
    case "$OPERATOR_VAST_CLI" in
      /proc/self/fd/*|/dev/fd/*) ;;
      *) die "operator Vast CLI must be an FD-bound executable" ;;
    esac
    [ -r "$OPERATOR_VAST_CLI" ] || die "operator Vast CLI is unavailable"
    "$NE_OPERATOR_PYTHON" - "$VAST_KEY_FILE" "$STATE" "${NE_OPERATOR_OWNER_UID:-0}" <<'PY' >/dev/null
import os, stat, sys
from pathlib import Path

key, state = map(Path, sys.argv[1:3]); owner = int(sys.argv[3])
for path, mode in ((key, 0o400), (state, 0o600)):
    try:
        info = path.lstat()
    except OSError:
        if required:
            raise SystemExit(1)
        continue
    if (
        not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path
        or not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise SystemExit(1)
PY
    if [ -z "$OPERATOR_SECRET_HOME" ]; then
      umask 077
      OPERATOR_SECRET_HOME="$(mktemp -d "${NE_VAST_OPERATOR_TMP_ROOT}/vast-secret.XXXXXX")"
      mkdir -m 700 "$OPERATOR_SECRET_HOME/.config" "$OPERATOR_SECRET_HOME/.config/vastai"
      install -m 600 "$VAST_KEY_FILE" "$OPERATOR_SECRET_HOME/.config/vastai/vast_api_key"
      trap operator_cleanup EXIT HUP INT TERM
    fi
    return 0
  fi
  if [ ! -x "$VAST_VENV/bin/vastai" ]; then
    log "installing vastai CLI into $VAST_VENV (one-time)"
    python3 -m venv "$VAST_VENV" || die "failed to create venv at $VAST_VENV"
    "$VAST_VENV/bin/pip" install --quiet --upgrade pip vastai || die "failed to install vastai"
  fi
  [ -f "$VAST_KEY_FILE" ] || die "Vast API key not found at $VAST_KEY_FILE — create it: mkdir -p $(dirname "$VAST_KEY_FILE") && pbpaste > $VAST_KEY_FILE  (needs Instances R/W + User R/W)"
  vastai set api-key "$(tr -d '[:space:]' < "$VAST_KEY_FILE")" >/dev/null
}

# ---- state helpers --------------------------------------------------------
save_state() { mkdir -p "$STATE_DIR"; printf '%s' "$1" > "$STATE"; }
_state() { [ -f "$STATE" ] || die "no current instance (run 'provision' or 'use <ID>')"; python3 -c "import json,sys;print(json.load(open('$STATE')).get(sys.argv[1],''))" "$1"; }
IID()   { _state id; }
HOST()  { _state host; }
PORT()  { _state ssh_port; }

rssh() { ssh -p "$(PORT)" -o ConnectTimeout=12 -o StrictHostKeyChecking=accept-new "root@$(HOST)" "$@"; }

# Refresh host/ssh_port/mapped-ports from Vast for the current (or given) id.
refresh_state() {
  local id="${1:-$(IID)}"
  local raw; raw="$(vastai show instance "$id" --raw)"
  python3 - "$id" "$raw" "$INT_VLLM" "$INT_CHEAP" <<'PY' > "$STATE"
import json,sys
iid=sys.argv[1]; d=json.loads(sys.argv[2])
host=d.get("public_ipaddr") or d.get("ssh_host")
ports=d.get("ports") or {}
def ext(internal):
    m=ports.get(f"{internal}/tcp")
    return int(m[0]["HostPort"]) if m else None
ssh_port=ext(22) or d.get("ssh_port")
keys=[22, int(sys.argv[3]), int(sys.argv[4])]
print(json.dumps({"id":int(iid),"host":host,"ssh_port":ssh_port,
                  "map":{k:ext(k) for k in keys},
                  "status":d.get("actual_status")},indent=0))
PY
  log "state: id=$(IID) host=$(HOST) ssh=$(PORT)"
}

# The confined operator never places provider JSON in argv and never exposes a
# partially-written state file. The JSON arrives on stdin and the replacement
# is fsynced before an atomic rename.
operator_refresh_state() {
  local id="$1" raw="$2"
  "$NE_OPERATOR_PYTHON" - "$STATE" "$id" "$INT_VLLM" "$INT_CHEAP" "${NE_OPERATOR_OWNER_UID:-0}" 3<<<"$raw" <<'PY'
import json, os, stat, sys, tempfile
from pathlib import Path

state = Path(sys.argv[1]); iid = sys.argv[2]; owner = int(sys.argv[5])
with os.fdopen(3) as provider_input:
    data = json.load(provider_input)
ports = data.get("ports") or {}
def ext(internal):
    mapping = ports.get(f"{internal}/tcp")
    return int(mapping[0]["HostPort"]) if mapping else None
payload = json.dumps({
    "id": int(iid),
    "host": data.get("public_ipaddr") or data.get("ssh_host"),
    "ssh_port": ext(22) or data.get("ssh_port"),
    "map": {str(port): ext(port) for port in (22, int(sys.argv[3]), int(sys.argv[4]))},
    "status": data.get("actual_status"),
}, sort_keys=True, separators=(",", ":")).encode()
parent = state.parent; info = parent.lstat()
if parent.is_symlink() or parent.resolve(strict=True) != parent or info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o700:
    raise SystemExit(1)
fd, temporary = tempfile.mkstemp(prefix=state.name + ".", dir=parent)
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    os.chmod(temporary, 0o600); os.chown(temporary, owner, -1)
    os.replace(temporary, state)
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)
finally:
    try: os.unlink(temporary)
    except FileNotFoundError: pass
PY
}

# Operator-mode provider observations stay off argv.  This path deliberately
# does not reuse refresh_state/wait_ready: those owner-terminal helpers predate
# the confined boundary and pass provider JSON as a Python argument.
operator_state() {
  "$NE_OPERATOR_PYTHON" - "$STATE" "$1" <<'PY'
import json, sys
from pathlib import Path

with Path(sys.argv[1]).open(encoding="utf-8") as handle:
    value = json.load(handle).get(sys.argv[2], "")
if not isinstance(value, (str, int)):
    raise SystemExit(1)
print(value)
PY
}

operator_prepare_ssh_identity() {
  "$NE_OPERATOR_PYTHON" - "$OPERATOR_SSH_KEY" "$OPERATOR_KNOWN_HOSTS" \
    "${NE_OPERATOR_OWNER_UID:-0}" <<'PY' >/dev/null
import os, stat, sys
from pathlib import Path

key, known_hosts = map(Path, sys.argv[1:3]); owner = int(sys.argv[3])

def validate_regular(path, mode):
    try:
        info = path.lstat()
    except OSError:
        raise SystemExit(1)
    if (
        not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path
        or not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise SystemExit(1)

validate_regular(key, 0o400)
parent = known_hosts.parent
try:
    parent_info = parent.lstat()
except OSError:
    raise SystemExit(1)
if (
    not known_hosts.is_absolute() or parent.is_symlink()
    or parent.resolve(strict=True) != parent or not stat.S_ISDIR(parent_info.st_mode)
    or parent_info.st_uid != owner or stat.S_IMODE(parent_info.st_mode) != 0o700
):
    raise SystemExit(1)
try:
    descriptor = os.open(
        known_hosts,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
except FileExistsError:
    pass
else:
    os.close(descriptor)
validate_regular(known_hosts, 0o600)
PY
}

operator_rssh() {
  local port host
  operator_prepare_ssh_identity || return 1
  port="$(operator_state ssh_port)" || return 1
  host="$(operator_state host)" || return 1
  [[ "$port" =~ ^[1-9][0-9]*$ && -n "$host" ]] || return 1
  "$NE_OPERATOR_SSH" -F /dev/null -p "$port" -o BatchMode=yes \
    -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
    -o ConnectTimeout=12 -o StrictHostKeyChecking=accept-new \
    -o IdentitiesOnly=yes -i "$OPERATOR_SSH_KEY" \
    -o GlobalKnownHostsFile=/dev/null \
    -o UserKnownHostsFile="$OPERATOR_KNOWN_HOSTS" "root@$host" "$@"
}

operator_grant_ssh() {
  "$NE_OPERATOR_PYTHON" - "$STATE" <<'PY' \
    | "$NE_PROVIDER_GRANT_CLIENT" \
    | "$NE_OPERATOR_PYTHON" -c \
      'import json,sys; assert json.load(sys.stdin) == {"code":"granted"}'
import json, os, sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
transition = os.environ.get("NE_PROVIDER_TRANSITION_ID", "")
document = {
    "instance_id": state.get("id"),
    "policy_sha256": "8f06a75d0c58f058d6b12293c4233bf2d05a10f009377278bbc6af4bcb7ca06d",
    "schema": "ne-inference-provider-ssh-grant-v1",
    "target_id": "private-vast-gpu",
    "transition_id": transition,
}
print(json.dumps(document, sort_keys=True, separators=(",", ":")))
PY
}

operator_wait_ready() {
  local id="$1" raw status i=0 granted=0
  while [ "$i" -lt 60 ]; do
    raw="$(vastai show instance "$id" --raw 2>/dev/null)" || raw=""
    status="$("$NE_OPERATOR_PYTHON" -c \
      'import json,sys; print(json.load(sys.stdin).get("actual_status", ""))' \
      <<<"$raw" 2>/dev/null)" || status=""
    if [ "$status" = "running" ]; then
      operator_refresh_state "$id" "$raw" >/dev/null 2>&1 || return 1
      if [ "$granted" -eq 0 ]; then
        operator_grant_ssh >/dev/null 2>&1 || return 1
        granted=1
      fi
      if operator_rssh 'echo ok' >/dev/null 2>&1; then
        return 0
      fi
    fi
    sleep 10
    i=$((i+1))
  done
  return 1
}

operator_prove_completion() {
  operator_rssh 'python3 -c '\''import json,urllib.request; p=json.dumps({"model":"qwen3.6-35b-a3b","prompt":"Reply with one token.","max_tokens":1,"temperature":0,"ignore_eos":True}).encode(); r=urllib.request.Request("http://127.0.0.1:'"$INT_VLLM"'/v1/completions",data=p,headers={"Content-Type":"application/json"},method="POST"); x=urllib.request.urlopen(r,timeout=90); d=json.load(x); assert x.status==200 and len(d.get("choices",()))==1 and d.get("usage",{}).get("completion_tokens")==1'\''' \
    >/dev/null 2>&1
}

# ---- commands -------------------------------------------------------------
cmd_provision() {
  local gpus=1 gpu="any" disk=80 price="1.0" cuda="$MIN_DRIVER_CUDA" allow_unverified=0 yes=0
  while [ $# -gt 0 ]; do case "$1" in
    --gpus) gpus="$2"; shift 2;; --gpu) gpu="$2"; shift 2;;
    --disk) disk="$2"; shift 2;; --price) price="$2"; shift 2;;
    --cuda) cuda="$2"; shift 2;;
    --allow-unverified) allow_unverified=1; shift;;
    --yes-spend) yes=1; shift;;
    *) die "unknown provision arg: $1";; esac; done
  ensure_vastai
  [ -f "$SSH_PUBKEY" ] || die "ssh pubkey not found at $SSH_PUBKEY"
  # `create ssh-key` takes the key CONTENT, not a path — passing the path
  # registers the literal path string and every SSH auth then fails.
  vastai create ssh-key "$(cat "$SSH_PUBKEY")" >/dev/null 2>&1 || true

  # `--gpu any` drops the exact-name filter in favor of "any rentable
  # 48GB-class card" (gpu_ram floor is MIN_GPU_RAM_MB, defined above) — the
  # 2026-07-29 live run found ZERO A40 offers at any price; the default name
  # below is a preference, not a guarantee of availability.
  local gpu_clause
  if [ "$gpu" = "any" ]; then
    gpu_clause="gpu_ram>=$MIN_GPU_RAM_MB"
    log "searching offers: ${gpus}× any 48GB-class GPU, disk ${disk}GB, <= \$$price/hr, cuda>=${cuda}"
  else
    gpu_clause="gpu_name=${gpu}"
    log "searching offers: ${gpus}× $gpu, disk ${disk}GB, <= \$$price/hr, cuda>=${cuda}"
  fi
  local q="${gpu_clause} num_gpus=${gpus} disk_space>=${disk} rentable=true verified=true direct_port_count>=2 dph_total<=${price} cuda_max_good>=${cuda}"
  # `|| offer=""` matters under `set -euo pipefail`: this is a plain
  # assignment (not masked by a preceding `local` on the same line, per the
  # documented bash `local x=$(cmd)` gotcha), so a `vastai` network error or
  # non-JSON payload WOULD otherwise abort cmd_provision right here — before
  # the friendly "here's what IS rentable" fallback below ever runs, which is
  # exactly the gap this preflight exists to close. The python side also
  # degrades to empty output on a JSON parse failure rather than raising, so
  # a truncated/garbled payload is treated the same as "no offer", not a
  # crash.
  # SELECTION IS CLIENT-SIDE. `vastai search offers <filter-string>` returns an
  # empty list on CLI 1.4.3 for every filter form tried — it fails silently
  # rather than erroring, so the script could not tell "nothing matches" from
  # "the filter did not parse". Fetching unfiltered and filtering here removes
  # that ambiguity entirely, and lets the failure path below report WHICH
  # constraint eliminated everything instead of a bare "no offers" — a message
  # that cost four wrong diagnoses in one session.
  #
  # `--limit 3000`: the default page is 64 rows, which is NOT the market. A
  # constraint tally computed over the default page is a sample presented as an
  # inventory, and that was the specific error.
  local offer; offer="$(vastai search offers --raw --limit 3000 2>/dev/null \
    | ALLOW_UNVERIFIED="$allow_unverified" MIN_RAM="$MIN_GPU_RAM_MB" GPUS="$gpus" \
      DISK="$disk" CUDA="$cuda" PRICE="$price" GPU_NAME="$gpu" MIN_CC="$MIN_COMPUTE_CAP" RIG_LIB="$RIG_LIB_DIR" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ['RIG_LIB'])
from rig_select import _is_denied_card
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)                       # parse failure -> empty, handled below
allow_unverified = os.environ['ALLOW_UNVERIFIED'] == '1'
name = os.environ['GPU_NAME']
steps = [
    # Apply the full-context RAM floor regardless of whether the GPU name was
    # explicit. A name filter selects a card family; it must not waive the
    # memory required to load the configured model and context.
    # Consumer GeForce parts are refused BY NAME, using the SAME rule the other
    # providers use (rig_select._is_denied_card) rather than a second copy of it.
    # A modded RTX 4090 reports 49140 MiB and compute_cap 890 -- it clears BOTH
    # numeric floors below -- and then fails cudaGetDeviceCount() with error 804,
    # because the pinned image's forward-compat layer is datacenter-only. These
    # are also the cheapest 48GB-class parts on the market, so cheapest-first
    # selects them repeatedly. This step matters most under the default
    # --gpu any, where the name domain is not narrowed by the caller.
    ('gpu_denylist',   lambda o: not _is_denied_card(str(o.get('gpu_name') or ''))),
    ('gpu_ram',        lambda o: (o.get('gpu_ram') or 0) >= int(os.environ['MIN_RAM'])),
    # compute_cap floor (see MIN_COMPUTE_CAP above): Turing 48GB cards are the
    # cheapest on the market and CANNOT run the FP8 primary — without this
    # step, cheapest-first reliably selects one (observed live 2026-08-04).
    ('compute_cap',    lambda o: (o.get('compute_cap') or 0) >= int(os.environ['MIN_CC'])),
    ('num_gpus',       lambda o: (o.get('num_gpus') or 0) == int(os.environ['GPUS'])),
    ('rentable',       lambda o: bool(o.get('rentable'))),
    # 2026-08-04: the offers API dropped the boolean 'verified' key in favor of
    # 'verification' ('verified'/'unverified'/...). Read the new field first,
    # fall back to the legacy boolean — with only the legacy read, EVERY offer
    # failed this step (observed live: rentable -> 112, verified -> 0 across
    # the whole market) and provision could never succeed.
    ('verified',       lambda o: allow_unverified
                                 or o.get('verification') == 'verified'
                                 or bool(o.get('verified'))),
    ('disk_space',     lambda o: (o.get('disk_space') or 0) >= float(os.environ['DISK'])),
    ('cuda_max_good',  lambda o: (o.get('cuda_max_good') or 0) >= float(os.environ['CUDA'])),
    ('direct_ports',   lambda o: (o.get('direct_port_count') or 0) >= 2),
    ('dph_total',      lambda o: (o.get('dph_total') or 1e9) <= float(os.environ['PRICE'])),
]
if name != 'any':
    steps.insert(0, ('gpu_name', lambda o: o.get('gpu_name') == name))
cur, tally = rows, []
for label, fn in steps:
    cur = [o for o in cur if fn(o)]
    tally.append('%s -> %d' % (label, len(cur)))
if not cur:
    # The whole point: name the constraint that emptied it, on stderr so the
    # caller's command substitution still yields an empty offer id.
    sys.stderr.write('  %d offers fetched; ' % len(rows) + ', '.join(tally) + chr(10))
    sys.exit(0)
cur.sort(key=lambda o: o.get('dph_total') or 1e9)
best = cur[0]
sys.stderr.write('  selected %s %sGB $%.3f/hr reliability %.3f (verified=%s)%s' % (
    best.get('gpu_name'), (best.get('gpu_ram') or 0)//1024, best.get('dph_total') or 0,
    best.get('reliability2') or 0, bool(best.get('verified')), chr(10)))
print(best['id'])
")" || offer=""

  if [ -z "$offer" ]; then
    # Availability preflight (2026-07-29 live-run gap 1): don't just tell the
    # operator to "loosen filters" with no data — show what IS actually
    # rentable right now, cheapest-first, any 48GB-class card.
    log "no offers for '$gpu' — querying what IS rentable right now (any 48GB-class card, cheapest-first)"
    local avail_q="num_gpus=${gpus} disk_space>=${disk} rentable=true verified=true direct_port_count>=2 cuda_max_good>=${cuda} gpu_ram>=$MIN_GPU_RAM_MB"
    # `|| true` for the same reason as above: this pipeline's own failure
    # (e.g. the availability query itself erroring) must not abort the
    # script before `die` below prints its guidance — a failed diagnostic
    # query silently losing the operator-facing message would be the same
    # "loosen your filters, no data" failure mode this preflight replaces.
    vastai search offers "$avail_q" -o 'dph_total' --raw | python3 -c "
import json, sys
try:
    rows = json.load(sys.stdin)[:12]
except Exception:
    rows = None
if rows is None:
    print('  (availability query itself failed — re-run manually: vastai search offers \'$avail_q\')')
elif not rows:
    print('  (no 48GB-class offers at all right now — try again later or widen --disk/--cuda)')
else:
    print(f\"  {'gpu_name':<20}{'num_gpus':>9}{'dph_total':>11}{'cuda_max_good':>15}{'disk_space':>12}\")
    for r in rows:
        print(f\"  {str(r.get('gpu_name','')):<20}{r.get('num_gpus',''):>9}{r.get('dph_total',0):>11.3f}{r.get('cuda_max_good',''):>15}{r.get('disk_space',0):>12.0f}\")
" >&2 || true
    die "no $gpu offers — the table above is what IS rentable; re-run with --gpu <exact gpu_name> or --gpu any"
  fi
  log "cheapest offer: $offer — creating instance"

  local img="$IMG"
  if [ "${NE_RIG_IMAGE_TAG_ONLY:-0}" = "1" ]; then
    img="${IMG%%@*}"
    log "NE_RIG_IMAGE_TAG_ONLY=1 — DROPPING the digest pin (provisioning '$img', not the verified '$IMG'). This is an operator-visible downgrade: only use it if 'vastai create instance --image <ref>@sha256:...' rejects the digest-qualified ref."
  fi

  # -p maps the vLLM ports (primary + cheap lane) so the endpoint is reachable
  # off-box; --ssh/--direct give a directly-connectable SSH endpoint.
  # SPEND GATE. Everything below bills.
  # A --price ceiling is a CONSTRAINT, not consent: on 2026-08-07 a high ceiling
  # passed to inspect the market was correctly read as authorization and rented a
  # box nobody wanted. The gate sits AFTER selection so the dry run still names
  # the offer it would take — a refusal that reports nothing forces a real run.
  if [ "$yes" != "1" ]; then
    log "DRY RUN — would rent offer $offer (image $img, disk ${disk}GB)."
    die "refusing to spend without --yes-spend"
  fi
  local out; out="$(vastai create instance "$offer" --image "$img" --disk "$disk" \
    --ssh --direct --env "-p ${INT_VLLM}:${INT_VLLM} -p ${INT_CHEAP}:${INT_CHEAP} -p 8000:8000" --raw)"
  local id; id="$(python3 -c "import json,sys;print(json.load(sys.stdin).get('new_contract',''))" <<<"$out")"
  [ -n "$id" ] || die "instance create failed: $out"
  save_state "{\"id\":$id}"
  log "instance $id created — waiting for it to come online (SSH ready)…"
  wait_ready "$id"
  cmd_status
  log "READY. Next: vast-rig.sh doctor && vast-rig.sh pull all && vast-rig.sh serve single"
}

wait_ready() {
  local id="$1" i=0
  while [ $i -lt 60 ]; do
    local st; st="$(vastai show instance "$id" --raw | python3 -c "import json,sys;print(json.load(sys.stdin).get('actual_status',''))" 2>/dev/null || true)"
    if [ "$st" = "running" ]; then refresh_state "$id"
      if rssh 'echo ok' >/dev/null 2>&1; then log "SSH is up"; return 0; fi
    fi
    sleep 10; i=$((i+1))
  done
  die "instance $id did not become SSH-ready in time"
}

cmd_use() { ensure_vastai; save_state "{\"id\":$1}"; refresh_state "$1"; cmd_status; }

cmd_status() {
  ensure_vastai; refresh_state >/dev/null 2>&1 || true
  echo "instance : $(IID)  ($(_state status))"
  echo "ssh      : ssh -p $(PORT) root@$(HOST)"
  echo "vllm map : $INT_VLLM→$(python3 -c "import json;print(json.load(open('$STATE'))['map'].get('$INT_VLLM'))")  $INT_CHEAP→$(python3 -c "import json;print(json.load(open('$STATE'))['map'].get('$INT_CHEAP'))")"
  rssh 'nvidia-smi --query-gpu=index,name,memory.used,utilization.gpu --format=csv,noheader 2>/dev/null; echo "--- vllm ---"; for p in '"$INT_VLLM $INT_CHEAP"'; do curl -s http://localhost:$p/v1/models 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"  :$p serving\",[m[\"id\"] for m in d[\"data\"]])" 2>/dev/null || echo "  :$p (not serving)"; done' 2>/dev/null || true
}

cmd_ssh() { ensure_vastai; refresh_state >/dev/null; if [ "${1:-}" = "--" ]; then shift; rssh "$@"; else rssh; fi; }

# Push the HF token to the box (authenticated downloads) — value never printed.
push_hf_token() {
  [ -f "$HF_TOKEN_FILE" ] || { log "no HF token at $HF_TOKEN_FILE — downloads will be throttled"; return 0; }
  tr -d '[:space:]' < "$HF_TOKEN_FILE" | rssh 'cat > /root/hf_token && chmod 600 /root/hf_token'
}

cmd_pull() {
  ensure_vastai; refresh_state >/dev/null
  local models=() min_disk
  case "${1:-all}" in
    fp8)  models=("$FP8"); min_disk=$DOCTOR_MIN_DISK_FP8;;
    bf16) models=("$BF16"); min_disk=$DOCTOR_MIN_DISK_BF16;;
    tiny) models=("$SMALL"); min_disk=$DOCTOR_MIN_DISK_TINY;;
    # rerank is tiny (~2.3GB safetensors) — the TINY disk floor covers it.
    rerank) models=("$RERANK_MODEL"); min_disk=$DOCTOR_MIN_DISK_TINY;;
    # 'all' deliberately EXCLUDES bf16: the ~70GB bf16 repo does not fit the
    # 80GB default disk; bf16 is explicit and documented to need --disk 150.
    all)  models=("$FP8" "$SMALL"); min_disk=$DOCTOR_MIN_DISK_ALL;;
    *) die "pull: one of fp8|bf16|tiny|rerank|all";;
  esac
  # 2026-07-29 live run: every real prerequisite failure surfaced AFTER an
  # 11-minute weight download. Run doctor FIRST — before writing/launching
  # the download script — so a missing gcc/nvcc/disk shortfall costs 2
  # minutes of diagnosis, not 90.
  if [ "${NE_SKIP_DOCTOR:-0}" = "1" ]; then
    log "NE_SKIP_DOCTOR=1 — SKIPPING the pre-pull doctor check (gcc/nvcc/CUDA_HOME/disk/vllm-pin NOT verified before an ~11-minute weight download; you were warned)"
  else
    run_doctor pull "$min_disk"
  fi
  # The digest-pinned vllm-openai image already ships huggingface_hub (it's a
  # vllm dependency) — only hf_transfer (the fast downloader accelerator)
  # is typically missing. Check + install EACH package independently and
  # `--no-deps`: installing both unconditionally (or without --no-deps) would
  # let pip upgrade huggingface_hub inside the pinned, matched-dependency
  # image whose whole point is that vllm/torch/etc. arrive pre-resolved
  # together (Argus P2 — the guard this image switch exists to provide).
  rssh 'python3 -c "import huggingface_hub" >/dev/null 2>&1 || { echo "installing huggingface_hub"; pip install -q --no-deps huggingface_hub 2>&1 | tail -1; }
python3 -c "import hf_transfer" >/dev/null 2>&1 || { echo "installing hf_transfer"; pip install -q --no-deps hf_transfer 2>&1 | tail -1; }
echo "hf deps ready"'
  push_hf_token
  local list; list="$(printf '%s ' "${models[@]}")"
  log "downloading (tmux 'dl', survives disconnect; resumable, retrying): $list"
  # Ship a script FILE and run it in tmux — inlining a retry loop through
  # ssh->tmux->bash->python quoting is brittle and silently truncates. $list
  # expands locally; \$m/\$a/\$(...) stay literal for the remote shell.
  printf '%s' "#!/bin/bash
export HF_HOME=/workspace/.hf_home
export HF_TOKEN=\$(cat /root/hf_token 2>/dev/null)
for m in $list; do
  for a in \$(seq 1 20); do
    echo \"=== START \$m attempt \$a \$(date +%T)\"
    find /workspace/.hf_home/hub/models--\$(echo \$m|tr / -) -name '*.incomplete' -mmin +30 -delete 2>/dev/null || true
    python3 -c \"from huggingface_hub import snapshot_download; snapshot_download('\$m', max_workers=6)\" && { echo \"=== DONE \$m\"; break; }
    echo \"=== retry \$m in 20s\"; sleep 20
  done
done
echo ALL_DONE" | rssh "cat > /root/ne-dl.sh && chmod +x /root/ne-dl.sh"
  rssh "tmux kill-session -t dl 2>/dev/null; tmux new-session -d -s dl '/root/ne-dl.sh > /root/dl.log 2>&1'; sleep 2; tmux has-session -t dl && echo launched"
  log "watch: vast-rig.sh ssh -- 'tail -f /root/dl.log'  (done marker: ALL_DONE)"
}

serve_one() {  # model, served-name, internal-port, gpu-index, max-model-len, [extra vllm args]
  # NO eager-mode flag (2026-08-04, live-measured on an RTX 6000 Ada): eager
  # mode served the FP8 primary at ~12 tok/s (600 tokens in 49.8s) — keep-alive
  # territory, not conversation; the same box, same flags, with CUDA graphs
  # (vLLM's default) did 600 tokens in 6.6s (~91 tok/s), tool_calls verified on
  # both. Cost: startup grows ~30-60s for graph capture. If a card ever fails
  # AT graph capture, that is a loud startup error in /root/vllm-<name>.log,
  # not silent garbage — fix forward there, don't re-pin eager globally.
  # (Guard: test_serve_line_literals_match_docs keys on the flag literal, so
  # this comment deliberately never spells it.)
  local model="$1" name="$2" port="$3" gpu="$4" max_model_len="$5"; shift 5
  rssh "export HF_HOME=/workspace/.hf_home; M=/workspace/.hf_home/hub/models--\$(echo '$model' | tr / -); \
    HF_HUB_OFFLINE=1 CUDA_VISIBLE_DEVICES=$gpu nohup vllm serve '$model' --host 0.0.0.0 --port $port \
    --served-model-name $name \
    --max-model-len $max_model_len $* > /root/vllm-$name.log 2>&1 < /dev/null & disown; echo 'serving $name on GPU $gpu:$port'"
}

ensure_vllm() {  # DRIVER-GATE-FIRST, THEN PIN-CHECKED + FAIL-LOUD: we pin exactly
  # $VLLM_PIN for image-parity with the product's own serve image
  # (deploy/compose/serve vllm/vllm-openai:v0.24.0) rather than floating to
  # whatever `pip install vllm` resolves, and rather than trusting the base
  # image's own preinstalled vllm sight-unseen. `use <INSTANCE_ID>` lets a
  # console-created / template box (e.g. a Vast
  # `vastai/vllm:*` template) enter the rig with an ARBITRARY pre-baked vllm
  # version AND an arbitrary driver — a bare "is it importable" probe would
  # silently trust that combination even if the driver predates cu128, exactly
  # the never-silently-downgrade-critical-stage failure this function exists
  # to prevent. So the CUDA driver check runs UNCONDITIONALLY, before the
  # already-installed short-circuit: an adopted box that happens to already
  # have vllm==$VLLM_PIN importable but a pre-12.8 driver still DIES here
  # instead of being waved through to fail later, confusingly, at model-load
  # time. Only once the driver clears the gate do we check `vllm.__version__`
  # and skip install when it EQUALS $VLLM_PIN; any other version (missing,
  # older, or newer) falls through to a pinned (re)install.
  #
  # NE-DEMO-RIG-MODEL removed the prior driver-ADAPTIVE fallback (cu121 torch +
  # vllm==0.6.6.post1 on <12.4 drivers): 0.6.6 predates Qwen3 support entirely,
  # so that fallback would have installed a vllm that CANNOT load Qwen3.6 and
  # failed later at model-load time with a confusing error — a silent downgrade
  # on a reasoning-critical stage, which repo policy forbids (never-silently-
  # downgrade-critical-stage). The provision-time offer query now filters
  # `cuda_max_good>=$MIN_DRIVER_CUDA` so this gate should rarely fire; if it
  # does, re-provision rather than trying to coax an old driver into working.
  # (The original driver-detection lesson is from the NE-HGATE-4 live gate,
  # but its cu121/0.6.6 remedy no longer applies.)
  #
  # `set -o pipefail` on the remote script matters: without it, the final
  # `pip install | tail -1` line's exit status is tail's (always 0), so a
  # failed install would be swallowed and never trip the local `|| die` below.
  #
  # minmaj/minmin: the remote gate below is GENERATED from MIN_DRIVER_CUDA
  # (never bare literals) — Argus P2: a prior version hardcoded `-lt 12` /
  # `-eq 12 && -lt 8` while the failure message interpolated
  # `$MIN_DRIVER_CUDA`, so loosening the gate (or bumping MIN_DRIVER_CUDA
  # without touching the gate) left the message asserting one bar while the
  # remote check enforced another. test_ensure_vllm_driver_gate_tracks_min_driver_cuda
  # boundary-locks this.
  local minmaj="${MIN_DRIVER_CUDA%%.*}" minmin="${MIN_DRIVER_CUDA#*.}"
  printf '%s\n' \
    'set -o pipefail' \
    'CUDA=$(nvidia-smi 2>/dev/null | grep -oE "CUDA Version: [0-9.]+" | grep -oE "[0-9]+\.[0-9]+" | head -1)' \
    'MAJ=${CUDA%%.*}; MIN=${CUDA#*.}' \
    "if [ \"\${MAJ:-0}\" -lt $minmaj ] || { [ \"\${MAJ:-0}\" -eq $minmaj ] && [ \"\${MIN:-0}\" -lt $minmin ]; }; then" \
    "  echo \"NVIDIA driver CUDA \${CUDA:-unknown} is too old for vllm==$VLLM_PIN (torch cu128 wheels need >=$MIN_DRIVER_CUDA). Re-provision — the provision query filters cuda_max_good>=$MIN_DRIVER_CUDA.\" >&2" \
    '  exit 1' \
    'fi' \
    "V=\$(python3 -c 'import vllm; print(vllm.__version__)' 2>/dev/null || true)" \
    "if [ \"\$V\" = \"$VLLM_PIN\" ]; then echo \"vllm==$VLLM_PIN already present (driver CUDA \${CUDA:-?})\"; exit 0; fi" \
    "echo \"installing vllm==$VLLM_PIN (found \${V:-none}, driver CUDA \${CUDA:-unknown})\"" \
    "pip install -q \"vllm==$VLLM_PIN\" 2>&1 | tail -1" \
    "TORCH=\$(python3 -c 'import torch; print(torch.__version__)' 2>/dev/null || echo unknown)" \
    "echo \"post-install torch==\$TORCH (gap-5 tripwire — verify this against MIN_DRIVER_CUDA/IMG's declared build, never trust a pre-install reading)\"" | rssh 'bash -s' \
    || die "vllm install/driver-gate failed on the rig — see the remote output above"
}

# dual/route need 2 physical GPUs; a box provisioned with the --gpus 1
# default would otherwise "succeed" while CUDA_VISIBLE_DEVICES=1 silently
# has no device to select — the failure buried in /root/vllm-cheap.log
# instead of failing loud here, the same silent-wrong shape ensure_vllm's
# fail-loud driver gate exists to avoid one layer down.
require_gpus() {
  local need="$1" n
  # Discriminate an SSH/remote failure from a genuine GPU shortfall (mirrors
  # run_doctor's probe, which already gets this right one function away): a
  # nonzero `rssh` propagates through `tr` because `set -o pipefail` is
  # global, so this first `|| die` fires ONLY on an SSH/remote error, never
  # on a low-but-real GPU count. A real `nvidia-smi -L | wc -l` always emits
  # a digit (0 included), never empty output, on a reachable box — empty
  # output with a successful SSH round-trip means nvidia-smi itself stayed
  # silent, a different failure than "SSH is down" and worth its own message.
  n="$(rssh 'nvidia-smi -L 2>/dev/null | wc -l' | tr -d '[:space:]')" \
    || die "serve: GPU-count probe failed to run on the rig (SSH/remote error — NOT a GPU shortfall); check 'vast-rig.sh status' and SSH access"
  [ -n "$n" ] || die "serve: GPU-count probe returned empty output (SSH up, nvidia-smi silent) — run 'vast-rig.sh doctor'"
  [ "$n" -ge "$need" ] 2>/dev/null || die "serve needs $need GPUs, box has ${n:-0} — re-provision with --gpus $need"
}

# Per-target minimum free disk (GB) `pull` requires BEFORE launching the
# download — the 2026-07-29 live run found every real failure surfaced AFTER
# an 11-minute weight pull; `doctor` moves the check ahead of it.
DOCTOR_MIN_DISK_FP8=45   # ~37.5GB weights + headroom
DOCTOR_MIN_DISK_ALL=48   # fp8 + the small tiny-CI model
DOCTOR_MIN_DISK_BF16=75  # ~70GB full-precision weights + headroom
DOCTOR_MIN_DISK_TINY=3   # Qwen3-0.6B is tiny

# The remote probe: a single self-contained script shipped over SSH stdin and
# run with the distinct argv token `ne-doctor-probe` (load-bearing: the test
# harness's fake `ssh` keys on this token appearing in the remote command to
# decide when to hand back a canned report instead of really executing
# anything). Emits `NE_DOCTOR key=value` lines only — parsed locally by
# _doctor_get, never evaluated remotely, so the pass/fail DECISION is made in
# THIS script, testable without a real box (test_gpu_rig_doctor.py).
read -r -d '' DOCTOR_PROBE_SCRIPT <<'PROBE' || true
set -uo pipefail
gcc_path=$(command -v gcc || command -v cc || true)
echo "NE_DOCTOR gcc=${gcc_path:-MISSING}"
nvcc_path=""
if command -v nvcc >/dev/null 2>&1; then nvcc_path=$(command -v nvcc)
elif [ -n "${CUDA_HOME:-}" ] && [ -x "$CUDA_HOME/bin/nvcc" ]; then nvcc_path="$CUDA_HOME/bin/nvcc"
elif [ -x /usr/local/cuda/bin/nvcc ]; then nvcc_path=/usr/local/cuda/bin/nvcc
fi
echo "NE_DOCTOR nvcc=${nvcc_path:-MISSING}"
cuda_home=""
if [ -n "${CUDA_HOME:-}" ] && [ -d "$CUDA_HOME" ]; then cuda_home="$CUDA_HOME"
elif [ -d /usr/local/cuda ]; then cuda_home=/usr/local/cuda
fi
echo "NE_DOCTOR cuda_home=${cuda_home:-MISSING}"
vllm_ver=$(python3 -c 'import vllm; print(vllm.__version__)' 2>/dev/null || true)
echo "NE_DOCTOR vllm=${vllm_ver:-MISSING}"
torch_ver=$(python3 -c 'import torch; print(torch.__version__)' 2>/dev/null || true)
echo "NE_DOCTOR torch=${torch_ver:-MISSING}"
gpus=$(nvidia-smi -L 2>/dev/null | wc -l | tr -d '[:space:]')
echo "NE_DOCTOR gpus=${gpus:-0}"
driver_cuda=$(nvidia-smi 2>/dev/null | grep -oE "CUDA Version: [0-9.]+" | grep -oE "[0-9]+\.[0-9]+" | head -1)
echo "NE_DOCTOR driver_cuda=${driver_cuda:-MISSING}"
disk_free=$( (df -BG /workspace 2>/dev/null || df -BG /) | awk 'NR==2{gsub("G","",$4); print $4}')
echo "NE_DOCTOR disk_free_gb=${disk_free:-0}"
for p in __NE_PROBE_PORTS__; do
  ids=$(curl -s "http://localhost:$p/v1/models" 2>/dev/null | python3 -c "import sys,json
try:
    d=json.load(sys.stdin); print(','.join(m['id'] for m in d['data']))
except Exception:
    pass" 2>/dev/null || true)
  echo "NE_DOCTOR serving_${p}=${ids:-NOT_SERVING}"
done
PROBE
# __NE_PROBE_PORTS__ is a placeholder, not a shell variable — the heredoc
# above is single-quoted (<<'PROBE') so the remote probe script cannot see
# INT_VLLM/INT_CHEAP directly; substitute it here so the probe's port list
# is DERIVED from the same two constants run_doctor's local parser keys off
# of (`_doctor_get "serving_${INT_VLLM}"`), never a second hand-copy.
DOCTOR_PROBE_SCRIPT="${DOCTOR_PROBE_SCRIPT//__NE_PROBE_PORTS__/$INT_VLLM $INT_CHEAP}"

# Extract one `NE_DOCTOR key=value` line's value out of a doctor report blob.
# Empty string if the key is absent (never crashes the caller — callers treat
# absence the same as an explicit MISSING via their own comparisons).
_doctor_get() {  # key, report
  printf '%s\n' "$2" | sed -n "s/^NE_DOCTOR ${1}=//p" | tail -1
}

# actual, min (e.g. "12.2" "12.8") -> 0 (true) if actual >= min. Mirrors the
# MAJ/MIN split ensure_vllm already uses for the same comparison remotely;
# this is the LOCAL twin, run against the value doctor already fetched.
_cuda_ge() {
  local a="${1:-0}" m="${2:-0}"
  local amaj=${a%%.*} amin=${a#*.} mmaj=${m%%.*} mmin=${m#*.}
  [ "${amaj:-0}" -gt "${mmaj:-0}" ] 2>/dev/null && return 0
  [ "${amaj:-0}" -lt "${mmaj:-0}" ] 2>/dev/null && return 1
  [ "${amin:-0}" -ge "${mmin:-0}" ] 2>/dev/null
}

# run_doctor(mode, min_disk_gb) — three modes, distinguished by WHEN in the
# lifecycle they run and what a vllm-pin mismatch means at that point:
#   "pull"  — pre-download, pre-install. A vllm mismatch WARNs only: `serve`'s
#             ensure_vllm force-installs the pin later anyway, so failing
#             pull over it would block a step that doesn't need vllm at all.
#   "check" — the standalone `doctor` subcommand, typically run right after
#             `provision`/`use`, BEFORE anything has tried to install vllm.
#             A mismatch here WARNs (same reasoning as "pull") — Argus P2:
#             hard-failing here with "the pinned install did not take" was
#             FACTUALLY WRONG at this point in the lifecycle, since no
#             install has been attempted yet.
#   "postinstall" — cmd_serve's call, AFTER ensure_vllm has already run. A
#             mismatch here IS a real problem (the install did not take) and
#             HARD FAILS.
# Every mode hard-fails on: gcc missing, too few GPUs, driver below
# MIN_DRIVER_CUDA, or insufficient disk — these are load-bearing regardless
# of lifecycle stage. nvcc/CUDA_HOME missing is a WARN, not a hard fail, in
# every mode (Argus P1): whether vLLM's own published image needs a
# user-visible nvcc/CUDA_HOME at SERVE time (as opposed to at the image's own
# build time) is unconfirmed — hard-gating the one tool the orchestrator runs
# nightly on an unverified predicate risks blocking a box that demonstrably
# serves real tokens, with no escape hatch. `doctor`'s job is to surface the
# finding loudly with named remediation either way, not to silently guess
# which predicates are safe to enforce.
# Named remediation per check — never a bare nonzero exit (vacuous-guards V7:
# the harness's own probe must discriminate reasons, see
# test_gpu_rig_doctor.py).
run_doctor() {
  local mode="$1" min_disk="$2"
  log "doctor: probing the rig (gcc/nvcc/CUDA_HOME/vllm/GPUs/driver/disk) before proceeding — mode=$mode"
  local report
  # A herestring (not a `printf | rssh` pipeline) — with the probe shipped
  # through a pipe under `set -o pipefail`, a remote exit before stdin fully
  # drains could SIGPIPE the local `printf`, turning a real probe failure
  # into a misleading "probe failed to run" from the diagnostic tool itself.
  # bash implements `<<<` via a temp file, not a pipe, so there is no writer
  # process to race regardless of how large DOCTOR_PROBE_SCRIPT grows.
  report="$(rssh 'bash -s -- ne-doctor-probe' <<< "$DOCTOR_PROBE_SCRIPT")" \
    || die "doctor: probe failed to run on the rig (SSH/remote error) — see output above"

  local gcc nvcc cuda_home vllm torch gpus driver_cuda disk_free serving_primary serving_cheap
  gcc="$(_doctor_get gcc "$report")"
  nvcc="$(_doctor_get nvcc "$report")"
  cuda_home="$(_doctor_get cuda_home "$report")"
  vllm="$(_doctor_get vllm "$report")"
  torch="$(_doctor_get torch "$report")"
  gpus="$(_doctor_get gpus "$report")"
  driver_cuda="$(_doctor_get driver_cuda "$report")"
  disk_free="$(_doctor_get disk_free_gb "$report")"
  serving_primary="$(_doctor_get "serving_${INT_VLLM}" "$report")"
  serving_cheap="$(_doctor_get "serving_${INT_CHEAP}" "$report")"

  log "doctor: gcc=$gcc nvcc=$nvcc cuda_home=$cuda_home vllm=$vllm torch=$torch(informational) gpus=$gpus driver_cuda=$driver_cuda disk_free=${disk_free}GB"
  log "doctor: serving :$INT_VLLM=$serving_primary  :$INT_CHEAP=$serving_cheap"
  log "doctor NOTE: vLLM serves under --served-model-name $SERVED_NAME — probing /v1/models with the HF repo id returns 404 'model does not exist' on a HEALTHY serve; probe for id '$SERVED_NAME' instead (cost an hour on the 2026-07-29 live run)."

  # Integrity gate, EVERY mode: the probe's port list is derived from
  # INT_VLLM/INT_CHEAP (see DOCTOR_PROBE_SCRIPT above's __NE_PROBE_PORTS__
  # substitution) and the local parser reads the same two constants via
  # `_doctor_get "serving_${INT_VLLM}"` / `"serving_${INT_CHEAP}"` — there is
  # exactly one report producer (`rssh 'bash -s -- ne-doctor-probe' <<<
  # "$DOCTOR_PROBE_SCRIPT"` above), so the real trigger for a missing key is
  # either the substitution breaking (e.g. the `__NE_PROBE_PORTS__` sentinel
  # surviving unreplaced) or a future rename of the `serving_` key on one
  # side only. Absent that gate, `serving_*` is otherwise only ever LOGGED,
  # never asserted, so either drift would silently print empty and doctor
  # would still exit 0. A missing key (as opposed to an empty-but-present
  # "NOT_SERVING" value) means the report and the local parser disagree on
  # which ports matter — fail loud, not silent.
  [ -n "$serving_primary" ] && [ -n "$serving_cheap" ] || die "doctor: probe report is missing serving_${INT_VLLM}/serving_${INT_CHEAP} keys — probe/parser port drift; the probe's port list no longer matches INT_VLLM/INT_CHEAP"

  local fail=0
  if [ "$gcc" = "MISSING" ]; then
    log "DOCTOR FAIL: gcc missing — remediation: apt-get update && apt-get install -y build-essential"
    fail=$((fail + 1))
  fi
  if [ "$nvcc" = "MISSING" ] || [ "$cuda_home" = "MISSING" ]; then
    log "DOCTOR WARN: nvcc/CUDA_HOME missing (nvcc=$nvcc cuda_home=$cuda_home) — NOT a blocking failure (unconfirmed whether vLLM needs either at serve time vs. only at the image's own build time). If serve genuinely fails on a JIT/compile error, the provisioned image ($IMG) ships the toolkit; an adopted ('use') box needs CUDA_HOME exported to its real toolkit, or re-provisioning with the digest-pinned image."
  fi
  if ! [ "${gpus:-0}" -ge 1 ] 2>/dev/null; then
    log "DOCTOR FAIL: gpus=$gpus < required 1 — nvidia-smi sees no GPU on this box"
    fail=$((fail + 1))
  fi
  if [ "$driver_cuda" = "MISSING" ] || [ -z "$driver_cuda" ]; then
    log "DOCTOR FAIL: nvidia driver CUDA version undetected (nvidia-smi did not report one) — required: MIN_DRIVER_CUDA=$MIN_DRIVER_CUDA"
    fail=$((fail + 1))
  elif ! _cuda_ge "$driver_cuda" "$MIN_DRIVER_CUDA"; then
    log "DOCTOR FAIL: driver_cuda=$driver_cuda < MIN_DRIVER_CUDA=$MIN_DRIVER_CUDA — re-provision (the offer query already filters cuda_max_good>=$MIN_DRIVER_CUDA; an adopted box may predate that filter)"
    fail=$((fail + 1))
  fi
  if ! [ "${disk_free:-0}" -ge "$min_disk" ] 2>/dev/null; then
    log "DOCTOR FAIL: disk_free_gb=$disk_free < required ${min_disk}GB"
    fail=$((fail + 1))
  fi
  if [ "$vllm" != "$VLLM_PIN" ]; then
    if [ "$mode" = "postinstall" ]; then
      log "DOCTOR FAIL: vllm=$vllm != VLLM_PIN=$VLLM_PIN — this check runs AFTER ensure_vllm's pinned install, so a mismatch here means the install did not take"
      fail=$((fail + 1))
    else
      log "DOCTOR WARN: vllm=$vllm != VLLM_PIN=$VLLM_PIN — not a failure in mode=$mode (no install has been attempted yet at this point in the lifecycle): serve's ensure_vllm force-installs $VLLM_PIN before serving (a multi-GB torch/vllm download — budget minutes)"
    fi
  fi

  [ "$fail" -eq 0 ] || die "doctor found $fail blocking issue(s) above — fix before proceeding (NE_SKIP_DOCTOR=1 overrides on pull/serve, loudly; the standalone doctor subcommand is never skippable)"
}

# `doctor`'s entire purpose is verification — never-silently-downgrade-
# critical-stage: the NE_SKIP_DOCTOR hatch belongs on `pull`/`serve` (which
# have a real workflow to push through) and NEVER on `doctor` itself, or an
# operator who exported the flag once to get a `pull` through would then get
# a silent, unconditional pass from the one command whose entire job is to
# verify nothing.
cmd_doctor() {
  ensure_vastai; refresh_state >/dev/null
  run_doctor check 5
  log "doctor OK — gcc/GPUs/driver/disk clear, vllm/nvcc findings (if any) are informational at this point in the lifecycle — see WARN lines above"
}

cmd_serve() {
  ensure_vastai; refresh_state >/dev/null
  # Validate the mode AND enforce its GPU-count requirement BEFORE ensure_vllm
  # (Codex C1): ensure_vllm can pip-install a multi-GB vllm/torch on an
  # adopted box, mutating the environment. On a 1-GPU box, `serve dual`/
  # `serve route` were previously rejected only AFTER that install — wasted
  # minutes/bandwidth for a configuration that was always going to fail.
  # Checking GPU count first means an under-provisioned box dies in seconds,
  # before touching pip at all.
  local mode="${1:-}"
  case "$mode" in
    single|dual|route|rerank) ;;
    *) die "serve: one of single|dual|route|rerank";;
  esac
  case "$mode" in
    dual|route) require_gpus 2;;
  esac
  ensure_vllm
  if [ "${NE_SKIP_DOCTOR:-0}" = "1" ]; then
    log "NE_SKIP_DOCTOR=1 — SKIPPING the post-install doctor check (GPUs/disk/driver/vllm-pin NOT verified before serving; you were warned)"
  else
    run_doctor postinstall 5
  fi
  case "$mode" in
    single) log "config: single-GPU 35B-A3B-FP8 (GPU0)"; serve_one "$FP8" "$SERVED_NAME" "$INT_VLLM" 0 "$MAX_MODEL_LEN" $PRIMARY_FLAGS;;
    dual)   log "config: dual-GPU bf16 tensor-parallel (2x48GB, GPU0+1)"
            rssh 'fuser -k /dev/nvidia* 2>/dev/null; sleep 3' || true   # free both cards
            rssh "export HF_HOME=/workspace/.hf_home; HF_HUB_OFFLINE=1 nohup vllm serve '$BF16' --host 0.0.0.0 --port $INT_VLLM --served-model-name $SERVED_NAME --tensor-parallel-size 2 $PRIMARY_FLAGS --max-model-len $MAX_MODEL_LEN > /root/vllm-dual.log 2>&1 </dev/null & disown; echo 'serving bf16 TP=2'";;
    route)  log "config: model-per-GPU — 35B-A3B-FP8 (GPU0) + Qwen3-0.6B cheap (GPU1)"
            serve_one "$FP8" "$SERVED_NAME" "$INT_VLLM" 0 "$MAX_MODEL_LEN" $PRIMARY_FLAGS
            # cheap lane gets NO tool-call/reasoning parser: PRIMARY_FLAGS's
            # qwen3_coder parser is the Qwen3.6-35B-A3B model card's specific
            # recommendation, unverified for a 0.6B non-coder checkpoint — so
            # this lane is triage-only (cannot emit tool_calls).
            serve_one "$SMALL" cheap "$INT_CHEAP" 1 "$SMALL_MAX_MODEL_LEN" --gpu-memory-utilization 0.30;;
    # NE-SEARCH-LATENCY: the GPU rerank lane — the SAME BAAI/bge-reranker-v2-m3
    # the pilot's CPU tei-rerank serves, on vLLM's Jina-compatible /rerank.
    # Runs BESIDE an already-serving primary on GPU0 (no fuser kill: a pooling
    # model has no KV cache and $RERANK_GPU_MEM_UTIL is sized to the primary's
    # leftover VRAM). Shares $INT_CHEAP with `serve route`'s cheap lane — the
    # two modes are mutually exclusive on a 1-GPU box. Uses its OWN
    # $RERANK_MAX_LEN, never $MAX_MODEL_LEN (the generative-lane value exceeds
    # this model's max and vllm refuses to serve). Hand `ne demo up --gpu-rerank "$(vast-rig.sh
    # endpoint rerank)"`.
    rerank) log "config: GPU rerank lane — $RERANK_MODEL as '$RERANK_SERVED_NAME' beside the primary (GPU0:$INT_CHEAP)"
            rssh "export HF_HOME=/workspace/.hf_home; HF_HUB_OFFLINE=1 CUDA_VISIBLE_DEVICES=0 nohup vllm serve '$RERANK_MODEL' --host 0.0.0.0 --port $INT_CHEAP \
    --served-model-name $RERANK_SERVED_NAME --gpu-memory-utilization $RERANK_GPU_MEM_UTIL --max-model-len $RERANK_MAX_LEN \
    > /root/vllm-$RERANK_SERVED_NAME.log 2>&1 < /dev/null & disown; echo 'serving $RERANK_SERVED_NAME on GPU 0:$INT_CHEAP'";;
  esac
  log "poll readiness: vast-rig.sh status"
}

cmd_endpoint() {
  # `endpoint`/`endpoint cheap` print an OpenAI-style /v1 base for the gateway
  # lane; `endpoint rerank` prints the BARE base (the ne-brain VllmReranker
  # POSTs /rerank on it) — hand it to `ne demo up --gpu-rerank`.  Unlike the
  # provider control/SSH path, model traffic never uses Vast's public mapped
  # port: it crosses an independently established private overlay to the
  # internal service port.
  local internal="$INT_VLLM" suffix="/v1"
  case "${1:-}" in
    cheap)  internal="$INT_CHEAP";;
    rerank) internal="$INT_CHEAP"; suffix="";;
  esac
  private_rig_endpoint_url "$internal" "$suffix"
}

cmd_start()   { ensure_vastai; vastai start instance "$(IID)"; log "starting $(IID) — waiting for SSH"; wait_ready "$(IID)"; cmd_status; }
cmd_stop()    { ensure_vastai; vastai stop instance "$(IID)"; log "stopped $(IID) (disk kept — re-up with: vast-rig.sh start)"; }
cmd_destroy() { ensure_vastai; local id; id="$(IID)"; vastai destroy instance -y "$id"; rm -f "$STATE"; log "destroyed $id (no idle cost; re-provision when needed)"; }

# Closed machine lifecycle surface for the privileged host operator.  It has no
# provision/destroy/use/ssh/serve/endpoint path and emits no provider or endpoint
# payload.  Exit 0 means ready/success; status exit 3 means proven stopped.
cmd_operator_status() {
  ensure_vastai
  operator_prepare_ssh_identity || exit 4
  local id raw status
  id="$(IID)"
  raw="$(vastai show instance "$id" --raw)" || exit 4
  status="$("$NE_OPERATOR_PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("actual_status", ""))' <<<"$raw")" || exit 4
  case "$status" in
    stopped|exited) exit 3 ;;
    running) ;;
    *) exit 4 ;;
  esac
  operator_refresh_state "$id" "$raw" >/dev/null 2>&1 || exit 4
  operator_grant_ssh >/dev/null 2>&1 || exit 4
  operator_prove_completion || exit 4
}

cmd_operator_start() {
  ensure_vastai
  operator_prepare_ssh_identity || exit 4
  vastai start instance "$(IID)" >/dev/null
  operator_wait_ready "$(IID)" >/dev/null 2>&1 || exit 4
  operator_prove_completion || exit 4
}

cmd_operator_stop() {
  ensure_vastai
  local id status
  id="$(IID)"
  vastai stop instance "$id" >/dev/null
  for _ in $(seq 1 60); do
    status="$(vastai show instance "$id" --raw | "$NE_OPERATOR_PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("actual_status", ""))')" || exit 4
    case "$status" in
      stopped|exited) return 0 ;;
    esac
    sleep 5
  done
  exit 4
}

# ---- dispatch -------------------------------------------------------------
need python3
cmd="${1:-}"; shift || true
if [ "$OPERATOR_MODE" = "1" ]; then
  [ "$#" -eq 0 ] || die "operator lifecycle commands take no arguments"
  case "$cmd" in
    operator-status) cmd_operator_status ;;
    operator-start) cmd_operator_start ;;
    operator-stop) cmd_operator_stop ;;
    *) die "operator mode exposes only status/start/stop" ;;
  esac
  exit 0
fi
case "$cmd" in
  provision) cmd_provision "$@";;
  doctor)    cmd_doctor "$@";;
  status)    cmd_status "$@";;
  ssh)       cmd_ssh "$@";;
  pull)      cmd_pull "$@";;
  serve)     cmd_serve "$@";;
  endpoint)  cmd_endpoint "$@";;
  start)     cmd_start "$@";;
  stop)      cmd_stop "$@";;
  destroy)   cmd_destroy "$@";;
  use)       cmd_use "$@";;
  *) awk 'NR==1 && /^#!/{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 1;;
esac
