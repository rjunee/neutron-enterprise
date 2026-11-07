# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO live orchestration (LIVE-PARKABLE; operator-runnable per docs/demo).

``demo_up`` brings up the pilot stack from an AUTO-GENERATED bundle (the
make_ci_bundle recipe, in-code), provisions throwaway demo Keycloak principals,
and seeds the Acme corpus through the EXISTING ``ne connect sync fixture`` governed
path. ``demo_run`` drives the narrated arc over the live https stack and writes the
dossier. ``demo_down`` tears the stack down.

Every heavy import is LAZY (the connect.py precedent) so ``ne --help`` stays fast,
and every docker / keycloak / subprocess call is isolated in a small module-level
function (the up.py / test_up_cli monkeypatch pattern) so the CLI wiring is unit-
testable via sanctioned seam mocks without a docker daemon.

NE-DEMO-LIVE-SEAMS: T3 verified answers and tier-2 HITL write-back light up via a
runtime-generated compose OVERLAY (the NE-084 pilot-override ``-f`` layering pattern).
T2 remains honestly unavailable until the demo has persistent AGE plus governed source
producers. :func:`_stage_demo_seed` builds the verified-answer template bundle and the
first-party ne-govern rego bundle; :func:`_write_demo_overlay` wires the remaining
default-off ne-api demo seams and mounts the real rego bundle into OPA.

NE-BUNDLE-GATEWAY-CONFIG: ``ne demo up --frontier anthropic|openai`` (or
``NE_DEMO_FRONTIER=<provider>``) makes the frontier model swap demoable, in both
directions — frontier<->frontier (OpenAI<->Anthropic) and frontier->open-weight (the
commercially load-bearing direction: the same governed workload moves from an
external frontier API back to the in-VPC open-weight model by CONFIG alone, no code
change). :func:`_gateway_lane_env` APPENDS a ``frontier-default`` model and reroutes
ONLY the ``agent`` task at it (no fallback — a silent private-lane fallback would
fake the swap); :func:`_stage_frontier` fails LOUD, before any docker call, if the
provider's API key is not exported on the host. Re-running ``ne demo up`` without
the flag reverts to the sealed, CPU-local open-weight lane. Staged frontier
credentials never outlive the demo: a plain re-``up`` OR ``ne demo down`` both
unlink the staged ``frontier.env``/marker (C12). :func:`_write_demo_overlay` also
switches the redaction backend to the stdlib-only ``regex`` plugin while
``--frontier`` is staged — the shipped ``ne-app`` image carries no Presidio sidecar
venv, and the frontier lane's pre-egress redaction is mandatory.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import struct
import sys
import time
from collections.abc import AsyncGenerator, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import click

from ne.cli.envfile import dotenv_line

_MODEL = "Qwen/Qwen3-0.6B"
# vllm-cpu advertises this served-model-name (deploy/compose/serve/compose.yaml:78);
# the gateway must request it so demo inference resolves to the CPU model.
_SERVED_NAME = "qwen3-0.6b"
_CUSTOMER = "acme-demo"

# Pre-seeded pilot HITL approver principal from the committed ne-pilot realm
# (deploy/compose/pilot/keycloak/ne-pilot-realm.json): username `approver`, group
# `/approver` → the `approver` role in NE_API_APPROVER_ROLES. Reused for the demo
# approval step (per the brief) rather than provisioning a duplicate user. These
# are WELL-KNOWN LOCAL THROWAWAY demo credentials, not a secret (they live in the
# committed realm), recorded into demo-credentials.json so demo_run acquires the
# approver token the same way it does reader/outsider.
_SEEDED_APPROVER_USERNAME = "approver"
_SEEDED_APPROVER_PASSWORD = "approver-demo"
_SEEDED_STEWARD_USERNAME = "steward"
_SEEDED_STEWARD_PASSWORD = "steward-demo"

# Pre-seeded pilot ops principal from the committed ne-pilot realm (same well-known
# local throwaway pattern as the approver above): username `ops`, group `/ops` ->
# the `ops` role in NE_API_OPS_ROLES. Reused for the NE-168 delegation demo step
# (POST /v1/demo/delegate + GET /v1/trace/{trace_id} are ops-gated).
_SEEDED_OPS_USERNAME = "ops"
_SEEDED_OPS_PASSWORD = "ops-demo"
_MERIDIAN_RESTRICTED_USERNAME = "meridian-frontline"
_MERIDIAN_RESTRICTED_GROUPS: tuple[str, ...] = (
    "all-employees",
    "meridian-frontline",
    "bank.customer.read",
    "bank.account-summary.read",
    "sharepoint.frontline.read",
)
# Raw Keycloak group labels consumed by ``PrincipalExpander`` and the Meridian
# templates' ``graph_classes``.  These are DATA entitlements, not the ``ops``
# route role; never weaken the template scopes by substituting that role.
_MERIDIAN_OPS_GRAPH_GROUPS: tuple[str, ...] = (
    "bank.customer.read",
    "bank.servicing.read",
    "bank.dispute.read",
    "sharepoint.operations.read",
)
_MERIDIAN_OPS_GROUPS: tuple[str, ...] = (
    "all-employees",
    "ops",
    "meridian-ops",
    *_MERIDIAN_OPS_GRAPH_GROUPS,
)
_MERIDIAN_APPROVER_GROUPS: tuple[str, ...] = (
    "approver",
    "meridian-approver",
    "bank.customer.read",
    "bank.servicing.read",
    "bank.dispute.read",
    "bank.approval.read",
    "sharepoint.restricted.read",
)
_STEWARD_GROUPS: tuple[str, ...] = ("steward",)
_PILOT_KEYCLOAK_REALM = "ne-pilot"
_PILOT_KEYCLOAK_REALM_PATH = Path("deploy/compose/pilot/keycloak/ne-pilot-realm.json")
_KEYCLOAK_REALM_PRESENTATION_FIELDS: tuple[str, str] = (
    "displayName",
    "displayNameHtml",
)


# ---------------------------------------------------------------------------
# workdir layout
# ---------------------------------------------------------------------------
def _prepare_workdir(workdir: Path) -> dict[str, Path]:
    """Create the 0700 workdir layout; return its named subpaths."""
    workdir.mkdir(parents=True, exist_ok=True)
    workdir.chmod(0o700)
    layout = {
        "root": workdir,
        "bundle": workdir / "bundle",
        "store": workdir / "secret-store",
        "models": workdir / "models",
        "corpus": workdir / "corpus",
    }
    for key in ("bundle", "store", "models", "corpus"):
        layout[key].mkdir(parents=True, exist_ok=True)
    layout["store"].chmod(0o700)
    return layout


# ---------------------------------------------------------------------------
# docker / image / model / stack seams (each monkeypatchable in tests)
# ---------------------------------------------------------------------------
def _preflight_docker() -> None:
    from ne.cli.up import _check_daemon, _check_docker

    _check_docker()
    _check_daemon()


def _image_present() -> bool:
    import subprocess

    from ne.cli.up import NE_APP_IMAGE

    result = subprocess.run(
        ["docker", "image", "inspect", NE_APP_IMAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


_SOURCE_REV_LABEL = "org.neutron.ne-app.source-rev"


# The ne-app image build inputs, per deploy/docker/ne-app/Dockerfile (COPY
# pyproject.toml uv.lock + the packages/ and tools/ trees). The source-rev tracks
# exactly these, so a doc/CI edit doesn't force a needless ne-app rebuild and a
# real source edit is never missed.
_BUILD_INPUT_PATHSPECS = ("packages", "tools", "pyproject.toml", "uv.lock")
# Directories that never enter the Docker build context (artifacts, caches, venvs)
# — excluded from the no-git filesystem fallback so they neither churn the rev nor
# slow the walk.
_FS_REV_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".venv",
        "node_modules",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".git",
    }
)


def _fs_content_rev(root: Path) -> str:
    """A content revision over the ne-app Docker build inputs computed WITHOUT git —
    the fail-closed fallback for a source export that has no ``.git`` directory.
    Stable when nothing changed, changes on any build-input drift, so a no-git tree
    can never silently run a stale image either (the alternative — a constant label —
    would defeat the whole drift guard)."""
    import hashlib

    files: list[Path] = [root / name for name in _BUILD_INPUT_PATHSPECS if (root / name).is_file()]
    for name in _BUILD_INPUT_PATHSPECS:
        base = root / name
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file() and not (_FS_REV_SKIP_DIRS & set(p.relative_to(base).parts)):
                files.append(p)

    h = hashlib.sha256()
    for p in sorted(files, key=lambda x: x.relative_to(root).as_posix()):
        h.update(b"\0" + p.relative_to(root).as_posix().encode())
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"\0<unreadable>")
    return "fs:" + h.hexdigest()[:16]


def _source_rev(root: Path) -> str:
    """A content revision for the ne-app build inputs: git HEAD, suffixed with a
    short hash of any drift in the build-input trees (tracked edits AND new
    untracked-but-not-ignored source). `_build_app_image` stamps this onto the
    image as a label; `ne demo up` rebuilds when it drifts — so a `git pull` (moves
    HEAD), a local edit to tracked source, or a brand-new source file can never
    silently leave a STALE image running old code.

    Scoped to ``_BUILD_INPUT_PATHSPECS`` (what the Dockerfile actually copies), so
    a doc/CI edit doesn't force a needless rebuild. Untracked files are gathered via
    ``git ls-files --others --exclude-standard`` — which honours ``.gitignore``, so
    the gitignored demo workdir stays excluded and does NOT churn the rev, while a
    genuinely new source file under packages/ or tools/ IS caught. When git is
    unavailable, falls back to a fail-closed filesystem content hash rather than a
    constant label."""
    import hashlib
    import subprocess

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
        )

    head = _git("rev-parse", "HEAD")
    if head.returncode != 0:
        return _fs_content_rev(root)
    rev = head.stdout.strip()
    dirty = _git("diff", "HEAD", "--", *_BUILD_INPUT_PATHSPECS).stdout
    others = _git(
        "ls-files", "--others", "--exclude-standard", "--", *_BUILD_INPUT_PATHSPECS
    ).stdout
    if dirty.strip() or others.strip():
        h = hashlib.sha256()
        h.update(dirty.encode())
        for rel in sorted(others.splitlines()):
            h.update(b"\0" + rel.encode())
            try:
                h.update((root / rel).read_bytes())
            except OSError:
                h.update(b"\0<unreadable>")
        rev += "+" + h.hexdigest()[:12]
    return rev


def _image_source_rev() -> str | None:
    """The source-rev label on the present ne-app image, or None when the image is
    absent OR carries no such label (a pre-NE-DEMO-STALE build)."""
    import subprocess

    from ne.cli.up import NE_APP_IMAGE

    fmt = '{{ index .Config.Labels "' + _SOURCE_REV_LABEL + '" }}'
    result = subprocess.run(
        ["docker", "image", "inspect", NE_APP_IMAGE, "--format", fmt],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    stamped = result.stdout.strip()
    # docker prints "<no value>" (or empty) when the label is unset.
    return stamped if stamped and stamped != "<no value>" else None


def _image_up_to_date(root: Path) -> bool:
    """True only when the present ne-app image was built from the CURRENT source
    revision — the single signal `ne demo up` uses to decide whether to rebuild.
    A missing image, a label-less (old) image, or any source drift → rebuild."""
    stamped = _image_source_rev()
    return stamped is not None and stamped == _source_rev(root)


def _build_app_image() -> None:
    import os
    import subprocess

    from ne.cli.checks import _find_repo_root

    root = _find_repo_root(Path.cwd())
    script = root / "deploy" / "airgap" / "scripts" / "build-ne-app.sh"
    click.echo("building first-party ne-app image (egress-staging step) …")
    # Stamp the build inputs' revision onto the image so a later `ne demo up` can
    # detect drift and rebuild instead of silently running a stale image.
    env = {**os.environ, "NE_APP_SOURCE_REV": _source_rev(root)}
    subprocess.run(["bash", str(script)], check=True, env=env)


def _download_model(models_dir: Path) -> None:
    import subprocess

    if any(models_dir.iterdir()):
        return
    import sys

    click.echo(f"downloading CPU demo model {_MODEL} (egress-staging step) …")
    # sys.executable, not bare "python": the latter isn't on PATH in a detached /
    # non-login shell (e.g. `nohup ne demo up`), and the interpreter running us is
    # exactly the venv that has ne.serve.
    subprocess.run(
        [sys.executable, "-m", "ne.serve", "download", _MODEL, "--dest", str(models_dir)],
        check=True,
    )


_TEI_EMBED_REPO = "Qwen/Qwen3-Embedding-0.6B"
_TEI_EXPORT_MANIFEST_RELPATH = (
    Path("tools") / "model-export" / "manifests" / ("qwen3-embedding-0.6b-onnx-fp32.json")
)


def _stage_tei_cache(workdir: Path, onnx_artifact: Path | None) -> Path:
    """Pre-stage the tei-embed hub-cache dir under ``workdir`` (NE-TEI-ONNX /
    NE-TEI-GEN) via ``ne.serve stage-tei``/``stage-tei-verify`` — an egress-
    staging step (skip-if-already-populated is ``stage_tei``'s own idempotency,
    not special-cased here).

    Default path (``onnx_artifact`` is None): ``stage-tei`` runs in its
    default-on auto mode — it GENERATES the fast-ort ONNX pair on demand (from
    the pinned source, parity-gated) and CACHES it under ``$NE_TEI_ONNX_CACHE``,
    so TEI boots its fast ort backend instead of the slow candle CPU fallback
    (17-64 s/embed). The FIRST run on a fresh egress-OK host downloads ~2.3 GB
    and exports (tens of minutes); cached thereafter. (Owner decision 2026-07-19
    — no hosted release asset; generate-on-demand + local cache.)

    Explicit override (``onnx_artifact`` given, ``ne demo up --tei-onnx-artifact``):
    inject that prebuilt pair instead, resolving its export manifest sibling-first
    (the artifact must travel WITH its export manifest — the air-gap bundle carries
    both). Returns the tei-cache dir.
    """
    import subprocess
    import sys

    from ne.cli.checks import _find_repo_root

    tei_cache_dir = workdir / "tei-cache"
    # sys.executable, not bare "python" — see _download_model (detached-shell PATH).
    cmd = [
        sys.executable,
        "-m",
        "ne.serve",
        "stage-tei",
        _TEI_EMBED_REPO,
        "--dest",
        str(tei_cache_dir),
    ]
    if onnx_artifact is not None:
        # Resolve the export manifest sibling-first (an artifact travels with its
        # export-manifest.json), then the committed manifest; neither -> refuse.
        sibling = onnx_artifact.parent / "export-manifest.json"
        committed = _find_repo_root(Path.cwd()) / _TEI_EXPORT_MANIFEST_RELPATH
        if sibling.is_file():
            manifest_path = sibling
        elif committed.is_file():
            manifest_path = committed
        else:
            raise click.ClickException(
                f"--tei-onnx-artifact {onnx_artifact} was given but no export manifest was "
                f"found: neither its sibling {sibling} nor the committed {committed}. The "
                "artifact must travel WITH its export manifest (the air-gap bundle carries "
                "both) — or drop the flag to auto-generate + cache the fast-ONNX pair."
            )
        cmd += ["--onnx-artifact", str(onnx_artifact), "--export-manifest", str(manifest_path)]
        click.echo(f"staging tei-embed ONNX cache from {onnx_artifact} (egress-staging step) …")
    else:
        click.echo(
            "ensuring tei-embed fast-ONNX pair (generate-on-demand cache — first run "
            "downloads ~2.3 GB and exports, tens of minutes; cached under $NE_TEI_ONNX_CACHE "
            "thereafter)"
        )
    subprocess.run(cmd, check=True)
    subprocess.run(
        [sys.executable, "-m", "ne.serve", "stage-tei-verify", "--dest", str(tei_cache_dir)],
        check=True,
    )
    return tei_cache_dir


#: `ne demo run`'s embed-latency preflight budget (rehearsal worklist #3): a
#: fast ort backend embeds well under a second; candle takes 17-64s. Override
#: via NE_DEMO_EMBED_PREFLIGHT_BUDGET_S for a slower box.
_EMBED_PREFLIGHT_BUDGET_S = 2.0
_TEI_EMBED_URL = "http://127.0.0.1:8085/embed"

# ``/health`` proves only that vLLM's API front process answers. The demo needs
# the engine behind it to finish a bounded generation. This direct, tenant-free
# completion never enters ne-api, a chat session, the conversation ledger, or
# the audit chain; it is readiness evidence, not a demo turn.
_VLLM_CPU_COMPLETION_URL = "http://127.0.0.1:8091/v1/completions"
_VLLM_GPU_SERVED_NAME = "qwen3.6-35b-a3b"
_VLLM_COMPLETION_TARGET = "vllm-completion-target.json"
_VLLM_COMPLETION_DEADLINE_S = 90.0


def _write_vllm_completion_target(workdir: Path, gpu_vllm: str | None) -> None:
    """Persist the private-default completion route selected by ``demo up``.

    ``demo run`` is intentionally option-free, so recomputing the route there
    would silently fall back to CPU after a ``--gpu-vllm`` bring-up.  The target
    is operator-only (0600) deployment state and contains no API key; the GPU
    demo lane itself is documented as an unauthenticated synthetic-data route.
    """
    if gpu_vllm is None:
        target = {"url": _VLLM_CPU_COMPLETION_URL, "model": _SERVED_NAME, "lane": "cpu"}
    else:
        base = gpu_vllm.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3].rstrip("/")
        target = {
            "url": f"{base}/v1/completions",
            "model": _VLLM_GPU_SERVED_NAME,
            "lane": "gpu",
        }
    path = workdir / _VLLM_COMPLETION_TARGET
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(target, fh, sort_keys=True)
    os.chmod(path, 0o600)


def _vllm_completion_target(workdir: Path | None) -> tuple[str, str, str]:
    if workdir is None:
        return _VLLM_CPU_COMPLETION_URL, _SERVED_NAME, "cpu"
    path = workdir / _VLLM_COMPLETION_TARGET
    try:
        target = json.loads(path.read_text(encoding="utf-8"))
        url, model, lane = target["url"], target["model"], target["lane"]
        if not all(isinstance(value, str) and value for value in (url, model, lane)):
            raise ValueError("invalid completion target")
        return url, model, lane
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(
            "vLLM completion target is missing or invalid; rerun `ne demo up` so "
            "readiness probes the same private inference lane as the demo"
        ) from exc


def _vllm_completion_preflight(
    workdir: Path | None = None, *, post: Any = None, clock: Any = None
) -> None:
    """Require the active private vLLM engine to generate exactly one token.

    The request is deterministic, bounded, and deliberately bypasses ne-api so
    it cannot create or mutate demo data. Any transport/HTTP/shape failure is a
    fail-loud readiness failure; exception details are not echoed because URLs
    can carry deployment metadata.
    """
    import time

    poster = post
    if poster is None:
        import httpx

        poster = httpx.post
    clock_fn = clock if clock is not None else time.monotonic
    url, model, lane = _vllm_completion_target(workdir)
    payload = {
        "model": model,
        "prompt": "Reply with one token.",
        "max_tokens": 1,
        "temperature": 0,
        "ignore_eos": True,
    }

    click.echo(
        "vLLM completion readiness: START "
        f"({lane} private lane; one token; deadline {_VLLM_COMPLETION_DEADLINE_S:.0f}s)"
    )
    started = clock_fn()
    try:
        response = poster(
            url,
            json=payload,
            timeout=_VLLM_COMPLETION_DEADLINE_S,
        )
        response.raise_for_status()
        body = response.json()
        choices = body.get("choices", []) if isinstance(body, dict) else []
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        if len(choices) != 1 or usage.get("completion_tokens") != 1:
            raise ValueError("completion response did not prove exactly one generated token")
    except Exception as exc:
        click.echo(
            "vLLM completion readiness: FAILED "
            f"({type(exc).__name__}; deadline {_VLLM_COMPLETION_DEADLINE_S:.0f}s). "
            "The API front process may be alive while the model engine cannot complete a turn.",
            err=True,
        )
        raise SystemExit(1) from exc

    elapsed = clock_fn() - started
    click.echo(f"vLLM completion readiness: PASS ({elapsed:.2f}s; one token generated)")


def _arc_progress(
    index: int,
    total: int,
    title: str,
    phase: str,
    request_deadline_s: float,
    status: Any,
    elapsed_s: float | None,
) -> None:
    """Human-readable live progress for the otherwise buffered demo arc."""
    if phase == "start":
        click.echo(
            f"[{index:02d}/{total:02d}] START {title} "
            f"(step wall-clock deadline {request_deadline_s:.0f}s)"
        )
        return
    rendered_status = getattr(status, "value", str(status))
    click.echo(f"[{index:02d}/{total:02d}] {rendered_status} {title} ({elapsed_s or 0.0:.2f}s)")


def _tei_embed_preflight(
    *,
    onnx_staged: bool,
    post: Any = None,
    clock: Any = None,
) -> None:
    """Warm POST then a TIMED single-embed POST against the live tei-embed
    server, before the narrated arc starts. When ``onnx_staged`` and the
    budget is breached, this is fail-LOUD (SystemExit(1)) — the ORT backend
    should have booted; a breach here smells like the candle CPU fallback
    (``Could not start ORT backend`` / ``Starting Qwen3 model on Cpu``) that
    should never have happened given a staged ONNX pair. Without a staged
    ONNX pair, a breach is EXPECTED (candle fallback) — warn only, so the
    pre-publish demo (no artifact yet) stays green.
    """
    import time

    poster = post
    if poster is None:
        import httpx

        def _default_post(url: str, json: dict[str, Any]) -> Any:
            resp = httpx.post(url, json=json, timeout=30.0)
            # §0 law 5: a 5xx that returns FAST must never read as a fast-embed pass.
            # A failed embed is a preflight failure, not a latency win.
            resp.raise_for_status()
            return resp

        poster = _default_post

    clock_fn = clock if clock is not None else time.monotonic

    budget = float(os.environ.get("NE_DEMO_EMBED_PREFLIGHT_BUDGET_S", _EMBED_PREFLIGHT_BUDGET_S))

    try:
        poster(_TEI_EMBED_URL, {"inputs": "tei embed preflight warm-up"})  # untimed warm-up
        start = clock_fn()
        poster(_TEI_EMBED_URL, {"inputs": "tei embed preflight probe"})
        elapsed = clock_fn() - start
    except Exception as exc:  # any transport/HTTP error is a failed embed, not a pass
        if onnx_staged:
            click.echo(
                f"tei-embed preflight FAILED: the embed request errored ({exc!r}) — the server "
                "did not return an embedding, so this is NOT a fast-embed pass. Check the "
                "tei-embed container log for the ORT backend start line (a 5xx or a candle "
                "fallback both fail here).",
                err=True,
            )
            raise SystemExit(1) from exc
        click.echo(
            f"warning: tei-embed preflight embed request errored ({exc!r}) — tolerated only "
            "because no ONNX cache is staged (pre-publish demo stays green).",
            err=True,
        )
        return

    if elapsed <= budget:
        return

    if onnx_staged:
        click.echo(
            f"tei-embed preflight FAILED: single-embed latency {elapsed:.2f}s exceeds the "
            f"{budget:.2f}s budget even though an ONNX cache was staged — this is the candle "
            "CPU-fallback signature ('Could not start ORT backend' / 'Starting Qwen3 model on "
            "Cpu' in the tei-embed container log). Re-verify the staged cache: "
            "python -m ne.serve stage-tei-verify --dest <workdir>/tei-cache, then check the "
            "tei-embed container log for the ORT backend start line.",
            err=True,
        )
        raise SystemExit(1)

    click.echo(
        f"warning: tei-embed single-embed latency {elapsed:.2f}s exceeds the {budget:.2f}s "
        "budget (expected: no ONNX cache staged, tei-embed is on the slow candle CPU "
        "fallback). This is normal only when staging ran with --no-generate or generation "
        "was skipped; a default `ne demo up` on an egress-OK host auto-generates + caches "
        "the fast-ONNX pair (NE-TEI-GEN)."
    )


def _onnx_is_staged(workdir: Path) -> bool:
    snapshots_dir = workdir / "tei-cache" / "models--Qwen--Qwen3-Embedding-0.6B" / "snapshots"
    if not snapshots_dir.is_dir():
        return False
    return any(snapshots_dir.glob("*/onnx/model.onnx"))


def _seed_secret_store(store_dir: Path) -> Path:
    """Write a plain-mode SecretStore + random throwaway REQUIRED_SECRETS values.

    In-code port of deploy/airgap/scripts/make_ci_bundle.py: values are random
    ``token_urlsafe`` tokens that live only for the demo — NEVER printed (C12).
    Returns the ne-secrets.toml path for `ne pilot promote --secrets-config`.
    """
    import asyncio

    from ne.cli.pilotcfg import REQUIRED_SECRETS
    from ne.secrets import SecretsSettings, create_secret_store

    secrets_json = store_dir / "secrets.ne.json"
    toml_path = store_dir / "ne-secrets.toml"
    toml_path.write_text(
        f'store = "sops-file"\n\n[file]\npath = "{secrets_json}"\nmode = "plain"\n',
        encoding="utf-8",
    )

    async def _seed() -> None:
        from ne.contracts.errors import SecretNotFoundError

        settings = SecretsSettings.from_config_file(toml_path)
        store = create_secret_store(settings)
        # Idempotent: REUSE any secret already provisioned by a prior `ne demo up`
        # on this workdir (the store dir persists — _prepare_workdir is exist_ok).
        # Regenerating would rotate NE_PILOT_POSTGRES_PASSWORD out of sync with the
        # PERSISTENT pg-data volume — Postgres honors its password only on first
        # init, so a fresh password on re-up breaks keycloak/ne-api auth (FATAL:
        # password authentication failed) and crash-loops the stack. Only mint a
        # value for a field that is genuinely absent.
        by_path: dict[str, dict[str, str]] = {}
        for env_name, spec in REQUIRED_SECRETS.items():
            path, field = spec["path"], spec["field"]
            existing = by_path.get(path)
            if existing is None:
                try:
                    existing = dict((await store.read(path)).data)
                except SecretNotFoundError:
                    existing = {}
                by_path[path] = existing
            if not existing.get(field):
                # The continuation loader requires at least 256 bits of HMAC
                # material. Other opaque demo secrets retain their established
                # size; only this cryptographic key needs the canonical 32-byte
                # token_urlsafe shape used by the air-gap CI bundle builder.
                byte_count = (
                    32 if env_name == "NE_SOURCE_AUTHORITY_CONTINUATION_HMAC_KEY_B64" else 24
                )
                existing[field] = secrets.token_urlsafe(byte_count)
        for path, data in by_path.items():
            await store.write(path, data)

    asyncio.run(_seed())
    return toml_path


def _promote_bundle(bundle: Path, secrets_toml: Path, hostname: str) -> None:
    from ne.cli.pilotcfg import promote

    ctx = click.Context(promote)
    ctx.invoke(
        promote,
        customer=_CUSTOMER,
        hostname=hostname,
        out=bundle,
        secrets_config=str(secrets_toml),
    )


def _bring_up_stack(
    bundle: Path,
    models_dir: Path,
    overlay_path: Path,
    https_port: int = 443,
    *,
    external_url: str | None = None,
    gpu_vllm: str | None = None,
) -> None:
    from ne.cli.up import up

    # The models dir feeds the vllm-cpu bind-mount source; Docker Compose treats a
    # relative path without a leading ``./`` (e.g. ``ne-demo/models``) as an UNDEFINED
    # named volume ("invalid compose project"), so it MUST be absolute — mirror the
    # seed-mount ``seed_abs`` convention.
    models_abs = str(models_dir.resolve())
    os.environ["NE_VLLM_WEIGHTS_ROOT"] = models_abs
    # CPU_MODEL_DIR is the model SUBPATH under the mounted /models — compose builds
    # `--model /models/${NE_VLLM_CPU_MODEL_DIR}`, and the downloader lays the weights
    # out at <models_dir>/Qwen/Qwen3-0.6B — so it must be _MODEL, NOT the host root
    # (an absolute value here doubled the path to /models//root/.../models and
    # crash-looped vllm-cpu with HFValidationError).
    os.environ.setdefault("NE_VLLM_CPU_MODEL_DIR", _MODEL)
    # Align the gateway's served-model-name with what vllm-cpu actually advertises
    # (`qwen3-0.6b`); ne-api's litellm_model is hosted_vllm/${NE_VLLM_SERVED_NAME}, and
    # the pinned default (qwen3.6-35b-a3b, the GPU model) 404s against the CPU server.
    os.environ.setdefault("NE_VLLM_SERVED_NAME", _SERVED_NAME)
    # NE-DEMO-NO-IDLE-VLLM: under ``--gpu-vllm`` the private lane is served by the
    # EXTERNAL rig — `_gateway_lane_env` points every private-vllm entry at that
    # host, so NOTHING in the emitted NE_GATEWAY_MODELS dials http://vllm:8000 and
    # the on-box CPU server answers zero requests. It is not free: MEASURED on the
    # 2026-08-28 demo box it held 10 GiB RSS and, once the kernel decided an idle
    # process was the cheapest thing to evict, 7,033 MB of SWAP — 7 of the 8.5 GB
    # reclaimed when a human restarted it by hand. That swap-out is invisible in
    # `docker stats` and showed up only as a box-wide 8.4 s root-page TTFB at a
    # load average of 1.91. Do not start what nothing can call.
    serve_cpu = gpu_vllm is None
    ctx = click.Context(up)
    ctx.invoke(
        up,
        profiles=("pilot",),
        bundle=bundle,
        serve_cpu=serve_cpu,
        overlays=(overlay_path,),
        https_port=https_port,
        port_bind_host="127.0.0.1" if external_url is not None else "0.0.0.0",
        # ``ne demo up`` is intentionally idempotent. The hardened ``ne up``
        # callback may reuse occupied publishes only after Docker proves both
        # exact bindings belong to this running Pilot project's Caddy.
        reuse_existing_project_ports=True,
    )


# ---------------------------------------------------------------------------
# NE-DEMO-LIVE-SEAMS: demo seed staging + the runtime-generated compose overlay
# ---------------------------------------------------------------------------
#: TEI silently falls back to the slow candle CPU backend when the pinned model
#: ships no ONNX (rehearsal 2026-07-17 issue 1); ingest AND query must share the
#: same generous timeout or query-time embeds 502 while ingest succeeds. Read via
#: os.environ, never in the config_hash preimage (hash-safe, §6.2/Q5).
_DEMO_TEI_TIMEOUT_S = "180"

#: NE-CTXB-8 (design §5.4, §8): the MODEL-FACING verified-read token budget the
#: PRODUCTION/demo composition turns ON (default-OFF in code — R2-P1-4/R3-P1-3).
#: Sizing rationale (MEASURED demo-scale compact renders, est tokens):
#: accounts-directory 6016 / orders-directory 6156 / orders-per-account-count 1532.
#: The cap MUST exceed the two directory tables so the arc's leg-(b) FULL directory
#: reads stay untruncated (R6-P1e — the independent Python row-join recompute needs
#: complete reads at demo scale), while still composing packing ON so the aggregate
#: and every read pack to a real budget. Honest truncation is proven at a SMALL cap
#: (1024) by dedicated fixture tests + the parked live override run — one global cap
#: cannot simultaneously keep the directory reads untruncated AND truncate them.
DEMO_VERIFIED_MAX_RESULT_TOKENS = 8192

#: The identical demo-seam env map applied to BOTH ne-api and ne-worker (§6.2/Q5
#: cross-process hash parity: the two ne-app processes MUST freeze the same
#: config_hash). Correct env prefixes: NE_BRAIN_GRAPH_* / NE_ONTOLOGY_* / NE_API_*
#: (the merged NE-FIX-ENVPREFIX rule). No secret material (C12).
_DEMO_OVERLAY_ENV: tuple[tuple[str, str], ...] = (
    ("NE_ONTOLOGY_VERIFIED_ANSWERS_ENABLED", "true"),
    ("NE_ONTOLOGY_VERIFIED_TEMPLATES_DIR", "/seed/verified-templates"),
    # The protected Meridian evidence lane must read the boot-published, pinned
    # selected release.  Leaving this false routes verified templates through the
    # mutable serving store whose source-native M365 ACL snapshot cannot select the
    # fixture's bank.* graphs, producing a successful but empty canonical B1 Run.
    ("NE_ONTOLOGY_GOVERNED_LOCAL_T3_ENABLED", "true"),
    ("NE_API_DEMO_MERIDIAN_FIXTURE_PATH", "/seed/meridian-v1"),
    ("NE_API_WORKFLOWS_ENABLED", "true"),
    # The CI-proven harness configuration — skip the heavy BFO/CCO ont-graph load on
    # every write so the boot lift lands well inside the container start_periods.
    ("NE_ONTOLOGY_INCLUDE_ONTOLOGY_GRAPH", "false"),
    # NE-CTXB-8: turn token packing ON in the demo composition — verified reads pack
    # to this model-facing budget so the directory reads become honest-truncated at
    # scale (default-OFF in code; this overlay is the ONLY place it is set). Sized to
    # exceed the demo directory tables (see DEMO_VERIFIED_MAX_RESULT_TOKENS).
    ("NE_ONTOLOGY_VERIFIED_MAX_RESULT_TOKENS", str(DEMO_VERIFIED_MAX_RESULT_TOKENS)),
    ("NE_API_DEMO_WRITEBACK_TOOL", "true"),
    # NE-168: light up the governed multi-lane agent-to-model delegation exhibit.
    ("NE_API_DEMO_DELEGATION", "true"),
    # Compose environment maps MERGE by key. The old live demo overlay enabled
    # this marker-driven response seam, so merely omitting it here would retain
    # the inherited "true" in a six-layer re-up. An explicit YAML null replaces
    # that lower-layer value. YAML null is NOT safe here: Compose treats it as
    # host-env pass-through, so a hostile inherited shell value would reactivate
    # the seam. An explicit false survives both lower overlay and host values.
    # The real Compose merge is an executing test; do not turn this back into an
    # absence-only source assertion.
    ("NE_API_DEMO_GROUNDED_HALLUCINATION", "false"),
    # Light up the natural-language grounded Ask mode. Every value that changes
    # the gate is explicit here (and copied byte-for-byte to API + worker) so a
    # library default cannot silently change the deployed demo contract.
    ("NE_ONTOLOGY_GROUNDED_AGENT_ENABLED", "true"),
    ("NE_ONTOLOGY_GROUNDED_AGENT_NAME", "grounded-analyst"),
    # STANDARD posture — non-factual vocabulary
    # becomes advisory so the model's natural prose passes beat 7, while numerals
    # (digit or spelled), IRIs, citations, and fact-sentence entity identity still
    # withhold at EVERY tier. The rulebook is pinned to ng-2 and assist is OFF:
    # this demo proves the deterministic post-generation gate, not a model judge.
    ("NE_ONTOLOGY_GROUNDING_STRICTNESS", "standard"),
    # NE-GA-RUN4 (U11 fix F1): the Run-path gate has its OWN tier and it is
    # STRICT — stated explicitly (it is also the library default) so no future
    # default change can silently alter the deployed contract. STANDARD above
    # is a CHAT posture; on the Run path the tier governs VOCABULARY
    # enforcement only, and the Grounded Analyst's whole retrievable corpus is
    # gate-visible (two read.kg.* verified templates, no chunk search), so
    # strict's cost is honest there. Zero-fetch runs are withheld at EVERY
    # tier by the Run path's own non-empty-evidence invariant (NE-GA-RUN5) —
    # that guarantee never rides this knob.
    ("NE_ONTOLOGY_GROUNDING_RUN_STRICTNESS", "strict"),
    ("NE_ONTOLOGY_GROUNDING_GATE_PROFILE", "ng2"),
    ("NE_ONTOLOGY_GROUNDED_ASSIST_ENABLED", "false"),
    # NE-GA-RUN2: the SAME deterministic gate, composed onto the canonical Run
    # path for exactly ONE agent — the Meridian Grounded Analyst, whose only
    # source-bearing grants are `read.kg.*` verified templates, so its whole
    # retrievable corpus is the recorded VerifiedAnswers the gate can see. The
    # Analyst and Customer Support are deliberately NOT named here: their
    # `read.knowledge.search` chunk corpus never enters the gate's corpus, so
    # gating them would withhold legitimately grounded answers (a mis-scoped
    # gate, not a stricter one). Empty/absent keeps the Run path byte-identical.
    ("NE_ONTOLOGY_GROUNDED_RUN_AGENT_IDS", "agt_01k3groundedanalyst000000000"),
    # The exact configured grounded agent must also be admitted alongside the
    # ordinary console agent. No marker-driven hallucination seam is deployed.
    ("NE_API_CHAT_AGENTS", '["console-chat", "grounded-analyst"]'),
    # NE-DEMO-FIX1: query-time embed/rerank timeout parity with the host-side
    # ingest seeder (below) — the slow TEI candle-CPU fallback otherwise times
    # out query embeds at the 30s prod default while ingest (already 180s)
    # succeeds, 502-ing brain retrieval (routers/brain.py).
    ("NE_TEI_EMBED_TIMEOUT_S", _DEMO_TEI_TIMEOUT_S),
    ("NE_TEI_RERANK_TIMEOUT_S", _DEMO_TEI_TIMEOUT_S),
)


def _stage_connect_dir(workdir: Path) -> Path:
    """Stage the polled-connector dir (NE-CONNECT-CSV): the LIVE copy of the sheet.

    ``workdir/connect`` is bind-mounted rw at ``/connect`` in ne-api + ne-worker:
    it carries the connector state + quarantine sqlite (written by the worker's
    15-minute cron, read by ne-api's /connect surfaces) and the FIXTURE renewals
    sheet + its ODCS schema, copied fresh from the committed exhibit
    (demo/acme/renewals/ — labelled a fixture there). Copying on every ``up``
    makes each bring-up deterministic; the demo-able live beat is editing THIS
    staged copy between polls, not the committed one.
    """
    from ne.cli.checks import _find_repo_root

    connect_dir = workdir / "connect"
    connect_dir.mkdir(parents=True, exist_ok=True)
    # Docker resolves the private host path before mounting this directory, so the
    # outer 0700 workdir can remain host-private. Inside the mount, however, ne-app
    # runs as uid/gid 10001 and must create SQLite databases plus journal/WAL files;
    # an inherited 0755 host directory makes a fresh container fail readonly.
    connect_dir.chmod(0o777)
    src = _find_repo_root(Path.cwd()) / "demo" / "acme" / "renewals"
    for name in ("acme-renewals.csv", "renewals.schema.json"):
        shutil.copyfile(src / name, connect_dir / name)
    # Reset the connector state + quarantine HERE, BEFORE bring-up — the worker
    # opens these sqlite files at boot, and unlinking them after it has (the old
    # _seed_corpus reset location) would split the inode: the worker's cron
    # writing to a deleted file while ne-api reads a fresh one. Deleting before
    # any container exists keeps every process on one file, and every `ne demo
    # up` still fully re-seeds the volume-wiped qdrant (the skipped_unchanged
    # trap the reset exists for).
    for state_db in ("state.db", "quarantine.db"):
        (connect_dir / state_db).unlink(missing_ok=True)
    return connect_dir


#: The in-container connect env (NE-CONNECT-CSV), shared VERBATIM by ne-api and
#: ne-worker (§6.2/Q5 config-hash parity). The csv lane is ALWAYS present — the
#: quarantine exhibit is a first-class demo beat; salesforce/m365 join the
#: sources list only under --live-writeback, whose staging has ALREADY validated
#: their connector env (a listed-but-misconfigured source fails the worker boot
#: loud, by design). Paths are the /connect bind mount staged by
#: :func:`_stage_connect_dir`.
_CONNECT_LANE_ENV: tuple[tuple[str, str], ...] = (
    ("NE_CONNECT_SYNC_CRON", "*/15 * * * *"),
    ("NE_CONNECT_SYNC_STATE_PATH", "/connect/state.db"),
    ("NE_CONNECT_SYNC_QUARANTINE_PATH", "/connect/quarantine.db"),
    # The ne-app image vendors only Docling's hash-pinned payload + pinned uv,
    # never its C4-isolated torch/CUDA closure.  Keep the built sidecar in the
    # existing writable /connect mount so both app roles resolve one persistent
    # runtime without putting dependencies or mutable bytes in the image.
    ("NE_PARSER_VENV_DIR", "/connect/.parser-venv"),
    ("NE_CONNECT_SYNC_KPI_TAG", "ne-demo"),
    ("NE_CONNECT_CSV_PATH", "/connect/acme-renewals.csv"),
    ("NE_CONNECT_CSV_SCHEMA_PATH", "/connect/renewals.schema.json"),
    ("NE_CONNECT_CSV_OWNER", "acme-demo-fixture"),
    ("NE_CONNECT_CSV_ID_COLUMN", "renewal_id"),
    # The declared audience (public-within-tenant): every demo persona carries
    # all-employees, so the clean rows are genuinely findable in Ask/Search.
    ("NE_CONNECT_CSV_DEFAULT_ACL", '["all-employees"]'),
)


#: ⛔ THERE IS NO SALESFORCE CRON OVERRIDE, and one must not be re-added.
#:
#: The demo org is a free Developer Edition with a HARD DAILY API CAP, and a
#: `{"salesforce": "0 */6 * * *"}` override lived here for exactly that reason.
#: It never once took effect — `build_connector_sync_surface` derives an
#: AUTHORITY-bearing source's cron from `authority_sweep_cadence_seconds`, and
#: salesforce is authority-enabled in this same file — and it cannot be made to
#: take effect, because a six-hourly job is arithmetically incompatible with the
#: connector's PT1H contract SLA:
#:
#:   `SweepCadencePlan.require_sub_sla` admits the authority producer only when
#:   `ceil(max_points / batch_size) * (cadence + max_batch_duration) < SLA`.
#:   ANY firing gap at or above the 3600s SLA fails that for any batch size, so
#:   21600s can never pass. `ne.worker.ingest._seeded_cron` now REFUSES such an
#:   override at seed time rather than seeding a job the rotation proof does not
#:   describe.
#:
#: The knob that genuinely throttles this lane is the sweep cadence below:
#: it moves the seeded cron AND the sub-SLA proof together, and — unlike
#: `freshness_sla` — it is NOT hashed into `salesforce_contract_identity`, so
#: raising it does not advance the contract epoch. `authority_sweep_cron` caps
#: the derived expression at `*/30`, so ~48 syncs/day is the floor available
#: this way (from ~720 at the current 120s cadence).
#:
#: ⚠️ Still the OPEN-LOOP half of the quota problem: nothing reads the org's
#: remaining daily allowance and backs off as it nears the cap. A quota-AWARE
#: policy is the real fix and is not this.


def _connect_lane_env(*, live_writeback: bool, graph: bool = False) -> tuple[tuple[str, str], ...]:
    """The connect-sync env for the app services (sources vary by write-back mode)."""
    if live_writeback:
        sources = '["salesforce", "m365", "csv"]'
    elif graph:
        sources = '["m365", "csv"]'
    else:
        sources = '["csv"]'
    return (("NE_CONNECT_SYNC_SOURCES", sources), *_CONNECT_LANE_ENV)


#: Source-authority is a deployment capability, activated only at the already
#: explicit ``--live-writeback`` boundary. These values are non-secret sweep
#: arithmetic and belong in the shared API/worker overlay environment, never in
#: the credential-bearing ``writeback.env``. One shared tuple prevents the two
#: composition roots from admitting different authority epochs.
#:
#: Both connector contracts use PT1H (3600s), and both plans are strictly
#: sub-SLA under ``ceil(max_points / batch_size) * (cadence + max_batch) < SLA``.
#:
#: M365 — unchanged, and no reason to change: ``ceil(500 / 50) * (120 + 60) =
#: 1800 < 3600``. The Graph tenant has no comparable daily cap.
#:
#: ⭐ SALESFORCE — the cadence IS the quota throttle. The demo org is a free
#: Developer Edition with a hard ~15,000-call daily cap, and because
#: :func:`ne.worker.ingest.authority_sweep_cron` DERIVES the seeded cron from
#: this cadence, raising it throttles the lane and moves the feasibility proof
#: with it; the two cannot diverge. ``authority_sweep_cron(1800)`` seeds
#: ``*/30 * * * *`` — 48 syncs a day, down from the ``*/2`` (~720/day, 52%
#: failing ``REQUEST_LIMIT_EXCEEDED``) that was live through 2026-09-09.
#: ``_MAX_AUTHORITY_CRON_MINUTES = 30`` is the ceiling, so 48/day is the
#: slowest this lever reaches.
#:
#: The cadence CANNOT be raised alone. ``batch_size >= max_points_per_scope``
#: makes the rotation exactly ONE batch, and that is a hard requirement rather
#: than an optimisation: a multi-batch rotation at a 1800s cadence would leave a
#: point unswept for ``batches * 1800`` seconds, and
#: ``SqlAlchemyHierarchicalAdmission.require_points_swept_within_sla`` refuses
#: the whole scope — on the DB clock, against the same 3600s — the moment any
#: admitted point ages past it. One batch: ``ceil(500 / 500) * (1800 + 900) =
#: 2700 < 3600``, with 900s of slack against that absolute gate.
#:
#: ⚠️ ``max_batch_duration_seconds = 900`` is a DECLARED bound that NOTHING
#: measures or enforces — it exists only inside ``SweepCadencePlan``'s
#: arithmetic. It is set from live evidence, not guessed: over nine real batches
#: on 2026-09-09 the org's per-point re-read (one ``rest_query`` per point) ran
#: 0.394 to 0.538 s, so the 500-point ceiling projects to ~269s. 900 carries 3.3x
#: headroom over that projection — deliberately generous because it EXTRAPOLATES
#: 10x from measured 50-point batches — while still clearing the guard. Measure
#: it directly with ``SELECT scope_hash, last_swept_epoch, max(last_swept_at) -
#: min(last_swept_at) FROM ne_source_scope_points GROUP BY 1, 2``.
_LIVE_WRITEBACK_AUTHORITY_ENV: tuple[tuple[str, str], ...] = (
    ("NE_M365_AUTHORITY_ENABLED", "true"),
    ("NE_M365_AUTHORITY_SWEEP_CADENCE_SECONDS", "120"),
    ("NE_M365_AUTHORITY_SWEEP_BATCH_SIZE", "50"),
    ("NE_M365_AUTHORITY_SWEEP_MAX_BATCH_DURATION_SECONDS", "60"),
    ("NE_M365_AUTHORITY_SWEEP_MAX_POINTS_PER_SCOPE", "500"),
    ("NE_SALESFORCE_AUTHORITY_ENABLED", "true"),
    ("NE_SALESFORCE_AUTHORITY_SWEEP_CADENCE_SECONDS", "1800"),
    ("NE_SALESFORCE_AUTHORITY_SWEEP_BATCH_SIZE", "500"),
    ("NE_SALESFORCE_AUTHORITY_SWEEP_MAX_BATCH_DURATION_SECONDS", "900"),
    ("NE_SALESFORCE_AUTHORITY_SWEEP_MAX_POINTS_PER_SCOPE", "500"),
)

_M365_GRAPH_AUTHORITY_ENV: tuple[tuple[str, str], ...] = tuple(
    item for item in _LIVE_WRITEBACK_AUTHORITY_ENV if item[0].startswith("NE_M365_")
)

_M365_DRAFT_TOOL = "draft.email.create"
_M365_SEND_TOOL = "write.external.email.send"

#: NE-CS-SEND (owner-specified, 2026-08-27): the ONE real address the governed
#: Tier-2 send may ever deliver to — the owner's own personal address, used as
#: the demo customer stand-in. NEVER a fixture "customer" address and never a
#: domain: `ApiSettings.email_send_allowed_recipients` is exact-match,
#: deny-by-default, and enforced against the draft's Graph-read recipient set
#: after the human approval.
_DEMO_SEND_RECIPIENT = "owner@example.com"

#: Draft-recipient guardrail allowlist (argument-level, C12). ONE exact
#: address: since the meridian-v1 fixture now carries the owner's demo inbox as
#: mer-cus-001's own email (the S5 outreach recipient is looked up from source
#: data, never taken from the prompt), the draft guardrail and the SEND
#: allowlist agree exactly — no draft can be created that the send allowlist
#: would later refuse, and no recipient is ever rewritten between draft and
#: send. A domain allowlist would admit every same-domain address, and a
#: host-exported value must never select a recipient. The default/non-live
#: overlay omits the key and therefore retains the guardrail's deny-by-default
#: posture.
_LIVE_WRITEBACK_RECIPIENT_ENV: tuple[tuple[str, str], ...] = (
    (
        "NE_GOVERN_GUARDRAILS__RECIPIENTS__ALLOWED_RECIPIENTS",
        f'["{_DEMO_SEND_RECIPIENT}"]',
    ),
)

_WRITEBACK_AUTHORITY_PREFIXES = (
    "NE_M365_AUTHORITY_",
    "NE_SALESFORCE_AUTHORITY_",
)


#: The mode the deploy gate pins for every staged verified template
#: (its `template_modes`). Set explicitly at staging time so
#: the seed does not inherit the bring-up caller's umask.
_STAGED_TEMPLATE_MODE: Final = 0o664

#: The mode the deploy gate pins for the staged verified-template DIRECTORY
#: (its directory loop, which runs BEFORE the per-file check).
#: Set explicitly for the same reason as the files: the `rmtree` above is
#: unconditional, so this directory is recreated on EVERY run and `mkdir` would
#: otherwise take `0o777 & ~umask` -- 0o755 under root's 0o022, which the gate
#: refuses. Fixing only the files left the deploy just as blocked, one assertion
#: earlier.
_STAGED_TEMPLATE_DIR_MODE: Final = 0o775


def _stage_demo_seed(workdir: Path) -> Path:
    """Stage verified templates, Meridian fixtures, and the first-party rego bundle.

    Any graph snapshot left by an older ``ne demo up`` is explicitly removed before
    staging. T2 stays unavailable until persistent AGE and governed source producers
    are composed; stale fixture bytes must never silently resurrect it. The verified-
    answer template bundle and the FIRST-PARTY ne-govern rego bundle (EXCLUDING
    ``*_test.rego``) are copied in — the
    shipped pilot OPA serves only the ne.dev placeholder, so mounting the real bundle is
    what makes ``/v1/data/ne/govern/decision`` defined (else every live PDP call fails
    closed). Returns the seed dir.

    THE VERIFIED BUNDLE IS THE PACKAGED MERIDIAN SET AND NOTHING ELSE. This demo
    composition turns governed local T3 ON unconditionally
    (:data:`_DEMO_OVERLAY_ENV` sets ``NE_ONTOLOGY_GOVERNED_LOCAL_T3_ENABLED`` and
    ``NE_API_DEMO_MERIDIAN_FIXTURE_PATH`` together), which makes the pinned
    ``SelectedOxigraphRelease`` the reader for EVERY verified template. That release
    holds exactly the three ``bank.*`` Meridian graphs, so the acme corpus templates
    ``corpus.write_verified_templates`` writes — which address
    ``urn:ne:graph:acl:all-employees`` — could only ever be advertised and then
    refused by ``SelectedOxigraphRelease.confine`` (HTTP 502 ``ReleaseScopeError``).
    Staging them was the advertise-what-cannot-be-served bug; they are not staged
    here at all. ``corpus.write_verified_templates`` itself is untouched — the
    fixture harness and its own tests exercise the acme bundle in NON-governed
    compositions where those graphs do exist.
    """
    from ne.cli.checks import _find_repo_root
    from ne.cli.opa_bundle import stage_govern_policies

    seed_dir = workdir / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    (seed_dir / "graph-fixture.json").unlink(missing_ok=True)
    root = _find_repo_root(Path.cwd())
    from ne.brain_ontology import MERIDIAN_TEMPLATE_IDS, meridian_template_dir
    from ne.brain_ontology.verified.templates import load_template_registry

    # Same reason the graph snapshot above is unlinked: a re-up over a seed dir an
    # older `ne demo up` filled must not leave its acme templates behind, or the
    # bundle would still advertise them and the API's own bidirectional boot guard
    # would (correctly) refuse to start.
    templates_dir = seed_dir / "verified-templates"
    if templates_dir.exists():
        shutil.rmtree(templates_dir)
    templates_dir.mkdir(parents=True)
    templates_dir.chmod(_STAGED_TEMPLATE_DIR_MODE)
    for source in sorted(meridian_template_dir().glob("*.toml")):
        staged = templates_dir / source.name
        shutil.copyfile(source, staged)
        # EXPLICIT, not umask-derived. `shutil.copyfile` does not preserve mode, so
        # the staged file lands at 0o666 & ~umask -- 0o664 under this box's 0o002
        # but 0o644 under root's default 0o022, which is the umask the deploy
        # wrapper actually runs with. The deploy gate pins every staged template at
        # 0o664 (template_modes), so a bring-up run as root
        # produced a seed that every subsequent deploy REFUSED -- loudly, at the
        # gate, but refused. Reproduced both ways before this line existed. Setting
        # the mode makes the roster deterministic instead of inheriting the ambient
        # umask of whoever ran the bring-up.
        staged.chmod(_STAGED_TEMPLATE_MODE)
    registry = load_template_registry(templates_dir)
    if tuple(registry.fingerprint.template_ids) != MERIDIAN_TEMPLATE_IDS:
        raise RuntimeError(
            "demo seed staging did not produce the exact packaged Meridian template set"
        )

    fixture_source = root / "tools" / "meridian" / "fixtures" / "meridian-v1"
    fixture_destination = seed_dir / "meridian-v1"
    if fixture_destination.exists():
        shutil.rmtree(fixture_destination)
    shutil.copytree(fixture_source, fixture_destination)

    stage_govern_policies(seed_dir / "opa-policies", root=root)
    return seed_dir


# ---------------------------------------------------------------------------
# NE-DEMO-WRITEBACK-LIVE: the --live-writeback staging (real NE-050 CRM write)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _LiveWritebackStaging:
    """What :func:`_stage_live_writeback` staged for this ``ne demo up`` run."""

    #: The 0600 env file (workdir/overlay/writeback.env) carrying the connector
    #: legs' env — referenced from the overlay via ``env_file:``, never inlined.
    #: ALWAYS absolute (Compose resolves a relative ``env_file:`` path against
    #: the base compose project's directory, not the invocation directory).
    env_file: Path
    #: (host absolute path, container path) pairs for every ``*_PEM_PATH`` var —
    #: mounted read-only; the key material itself never rides the overlay YAML.
    pem_mounts: tuple[tuple[str, str], ...]
    #: Exact normalized mailbox admitted for the Graph-only draft action.
    #: ``None`` for the legacy full live-writeback surface.
    draft_mailbox: str | None = None
    #: NE-CS-SEND: the exact `NE_API_WRITE_ACTION_TOOLS` selection this staging
    #: validated (graph-only mode). Empty for the legacy full live-writeback
    #: surface, which selects via `NE_API_WRITE_ACTIONS_ENABLED` instead.
    selected_write_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class _GraphPopulationStaging:
    """Closed, credential-free Graph population configuration for one M365 drive."""

    #: Canonical compact JSON consumed by ``BrainGraphSettings.sources`` in both roots.
    sources_json: str
    #: Exact manifest-owned drive selected for both API and worker runtime scope.
    drive_id: str


#: The workdir-relative marker recording which write-back mode the LAST
#: ``ne demo up`` staged — no secrets (mode + manifest dir only). Read by
#: :func:`_resolve_writeback` at ``ne demo run`` time.
_WRITEBACK_MARKER = "demo-writeback.json"

#: Cross-reference: tools/meridian-seed/src/ne/meridian_seed/manifest.py::
#: default_manifest_dir(). ne-cli does not import tools/meridian-seed at src
#: level (a sibling first-party tool, not a dependency of this package), so the
#: default is duplicated here (the principal_sync string-duplication
#: precedent) — ``--seed-manifest-dir`` overrides it either way. Both --graph
#: and --live-writeback resolve their seed manifests from this ONE directory
#: (m365.json and salesforce.json respectively) — the demo corpus is Meridian.
_DEFAULT_MERIDIAN_SEED_MANIFEST_DIR = Path.home() / ".ne" / "meridian-seed"

_M365_TARGET_KEYS = frozenset(
    {
        "tenant",
        "corpus_version",
        "graph_base_url",
        "tenant_id",
        "site_id",
        "drive_id",
        "folder_item_id",
        "generation_id",
    }
)


def _stage_graph_population(manifest_dir: Path) -> _GraphPopulationStaging:
    """Admit one manifest-bound Meridian M365 drive for governed AGE population.

    This is deliberately credential-free but is run while the host's live M365
    settings are present.  It validates the exact frozen target against the real
    connector settings class and then asks the production ``M365GraphSyncHost``
    mapping resolver to accept the generated one-drive mapping.  It performs no
    provider or Docker I/O and writes no files, so every refusal happens before
    the first Docker call in :func:`demo_up`.
    """
    manifest_path = manifest_dir / "m365.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(
            f"--graph requires a valid Meridian M365 manifest at {manifest_path}; "
            "run `ne-meridian-seed push --tenant m365` first"
        ) from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{manifest_path} must contain one JSON object")
    target = payload.get("m365_target")
    target_keys = frozenset(target) if isinstance(target, dict) else frozenset()
    if not isinstance(target, dict) or target_keys != _M365_TARGET_KEYS:
        missing = sorted(_M365_TARGET_KEYS - target_keys)
        surplus = sorted(target_keys - _M365_TARGET_KEYS)
        raise click.ClickException(
            "--graph requires the exact credential-free m365_target binding in "
            f"{manifest_path}; missing={missing!r} surplus={surplus!r}"
        )
    if payload.get("tenant") != "m365" or target.get("tenant") != "m365":
        raise click.ClickException(f"{manifest_path} is not an M365 Meridian manifest")
    if payload.get("corpus_version") != target.get("corpus_version"):
        raise click.ClickException(
            f"{manifest_path} outer corpus_version does not match its m365_target binding"
        )
    if payload.get("corpus_version") != "meridian-v1":
        raise click.ClickException(
            f"{manifest_path} is not the exact meridian-v1 structured graph corpus"
        )
    for key in _M365_TARGET_KEYS - {"tenant"}:
        value = target.get(key)
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or any(not char.isprintable() for char in value)
        ):
            raise click.ClickException(
                f"{manifest_path} m365_target.{key} must be a non-empty, trimmed, "
                "control-free string"
            )
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", target["generation_id"]) is None:
        raise click.ClickException(
            f"{manifest_path} m365_target.generation_id is not a valid persistence generation"
        )

    from pydantic_settings import SettingsError

    from ne.brain_graph.construct.meridian import meridian_m365_graph_mapping
    from ne.brain_graph.construct.runner import M365GraphSyncHost
    from ne.connect.m365.config import M365ConnectorSettings
    from ne.connect.m365.contract import m365_data_contract
    from ne.connect.m365.operational_graph import (
        OperationalGraphItemBinding,
        manifest_record_key,
        validate_binding_acls,
        validate_binding_set,
    )

    collected = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("NE_M365_") and not key.startswith("NE_M365_AUTHORITY_")
    }
    try:
        with _environ_overlay({**collected, **dict(_LIVE_WRITEBACK_AUTHORITY_ENV)}):
            settings = M365ConnectorSettings()  # type: ignore[call-arg]
    except (SettingsError, ValueError) as exc:
        raise click.ClickException(
            f"ne demo up --graph: invalid live M365 connector settings: {exc}. "
            "See docs/demo/README.md for the env recipe."
        ) from exc

    expected_graph_base = settings.graph_base_url.rstrip("/")
    if target["graph_base_url"].rstrip("/") != expected_graph_base:
        raise click.ClickException(
            "--graph manifest Graph cloud base URL differs from the configured M365 target"
        )
    if target["tenant_id"] != settings.tenant_id:
        raise click.ClickException(
            "--graph manifest tenant_id differs from the configured M365 target"
        )
    if settings.source_name != "m365-sharepoint":
        raise click.ClickException(
            "--graph requires the canonical NE_M365_SOURCE_NAME=m365-sharepoint"
        )
    if tuple(settings.site_ids) != (target["site_id"],):
        raise click.ClickException(
            "--graph requires exactly the manifest-bound SharePoint site_id in NE_M365_SITE_IDS"
        )
    if settings.drive_ids and tuple(settings.drive_ids) != (target["drive_id"],):
        raise click.ClickException(
            "--graph requires NE_M365_DRIVE_IDS, when present, to contain only the "
            "manifest-bound drive_id"
        )

    # Bind the resolved drive into the settings used by the production mapping
    # validator. This is not an invented target: the credential-free seed receipt
    # was saved only after the seeder resolved that site's default drive.
    bound_settings = settings.model_copy(
        update={"site_ids": (target["site_id"],), "drive_ids": (target["drive_id"],)}
    )
    raw_bindings = payload.get("operational_graph")
    if not isinstance(raw_bindings, list):
        raise click.ClickException(
            f"{manifest_path} lacks the exact operational_graph item receipts"
        )
    try:
        bindings = tuple(OperationalGraphItemBinding.model_validate(item) for item in raw_bindings)
        validate_binding_set(bindings)
        groups = payload.get("m365_groups")
        if not isinstance(groups, dict) or set(groups) != {"ops", "approver", "frontline"}:
            raise ValueError("missing exact M365 group bindings")
        validate_binding_acls(
            bindings,
            ops_group=groups["ops"],
            approver_group=groups["approver"],
            frontline_group=groups["frontline"],
        )
    except (TypeError, ValueError) as exc:
        raise click.ClickException(
            f"{manifest_path} operational_graph receipt set is invalid: {exc}"
        ) from exc
    records = payload.get("records")
    paths = payload.get("document_paths")
    grants = payload.get("grants")
    if not isinstance(records, dict) or not isinstance(paths, dict) or not isinstance(grants, dict):
        raise click.ClickException(
            f"{manifest_path} lacks exact records/document_paths/grants maps"
        )
    for binding in bindings:
        key = manifest_record_key(binding.entity_class, binding.record_id)
        if (
            records.get(key) != binding.item_id
            or paths.get(key) != binding.path
            or grants.get(key) != list(binding.acl)
        ):
            raise click.ClickException(
                f"{manifest_path} operational item {key!r} lacks its exact live binding"
            )
    stages = payload.get("stage_history")
    if not isinstance(stages, list) or "operational_graph" not in {
        entry.get("stage") for entry in stages if isinstance(entry, dict)
    }:
        raise click.ClickException(f"{manifest_path} has no completed operational_graph seed stage")
    mapping = meridian_m365_graph_mapping(
        source_system=m365_data_contract(bound_settings).source,
        drive_id=target["drive_id"],
        bindings=bindings,
    )
    # Constructor dependencies are unused by mapping_for; constructing the real
    # host nevertheless proves this exact mapping through its production resolver.
    host = M365GraphSyncHost(
        store=object(),  # type: ignore[arg-type]
        ledger=object(),  # type: ignore[arg-type]
        mappings=(mapping,),
        evidence_recorder=object(),  # type: ignore[arg-type]
        source_authority_provider=object(),  # type: ignore[arg-type]
        audit_sink=object(),  # type: ignore[arg-type]
        config_hash="demo-graph-population-admission",
    )
    host.mapping_for(bound_settings, resolved_drive_ids=(target["drive_id"],))
    return _GraphPopulationStaging(
        sources_json=json.dumps(
            [mapping.model_dump(mode="json")], sort_keys=True, separators=(",", ":")
        ),
        drive_id=target["drive_id"],
    )


#: The demo-only sqlite fallback for the SQL connector leg, injected ONLY when
#: no ``NE_SQL_*`` var is present on the host. ``create_validated_engine``
#: validates dialect/host only (never table existence — the ticket tool is
#: registered but the demo arc never invokes it), so this quartet boots the
#: leg cleanly without a real ticket-tracker database.
_SQL_WRITEBACK_FALLBACK_ENV: dict[str, str] = {
    "NE_SQL_CONNECTION_URL": "sqlite:////tmp/ne-demo-tickets.db",
    "NE_SQL_ALLOW_SQLITE": "true",
    "NE_SQL_OWNER": "ne-demo",
    "NE_SQL_TABLES": (
        '[{"table": "tickets", "id_column": "id", "content_columns": ["status"], '
        '"acl": {"principals": ["demo@ne.local"]}}]'
    ),
}


@contextmanager
def _environ_overlay(overlay: Mapping[str, str]) -> Iterator[None]:
    """Temporarily apply ``overlay`` to ``os.environ``, restoring on exit."""
    previous = {key: os.environ.get(key) for key in overlay}
    os.environ.update(overlay)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _check_writeback_legs(collected: Mapping[str, str]) -> None:
    """Fail loud unless the boot validator itself accepts ``collected``.

    DERIVED BY CALLING IT, not by re-implementing it. The previous version
    constructed the same three ``NeSettings`` subclasses the boot validator
    constructs — which is a transliteration wearing the word "derived": it
    re-listed the leg set, the prefixes and the settings classes, so adding a
    fourth leg in ``ne.api.platform`` would silently under-check here and the
    documented recipe would pass host-side and die at container boot. That is
    the identical defect this unit was written to remove, one level up.

    ``ne-api`` is already a declared runtime dependency of ``ne-cli``, so the
    validator is importable and its message is byte-identical to what the
    container prints. There is now exactly ONE definition of "configured".

    Non-``ValidationError`` failures are caught too: malformed JSON in a complex
    field (``NE_SALESFORCE_OBJECTS``, ``NE_SQL_TABLES``, ``NE_M365_SITE_IDS``)
    raises ``SettingsError``, and hand-typed JSON in a shell export is the most
    likely operator mistake — the one case where a friendly host-side gate must
    not dump a stack trace.

    C12: the message carries leg names / env prefixes / pydantic field locations.
    A validator that embeds its own input in its message text (Salesforce object
    names do) will surface that value; no ``SecretStr`` field does, and
    ``test_writeback_precheck_never_echoes_a_credential`` pins it.
    """
    from pydantic_settings import SettingsError

    from ne.api.platform import _load_write_action_env_settings

    with _environ_overlay(collected):
        try:
            _load_write_action_env_settings()
        # SettingsError FIRST: pydantic-settings makes it a ValueError subclass,
        # so the broader clause would swallow it and print the validator's
        # "error parsing value for field" instead of naming the actual mistake.
        # My own test caught that ordering — the friendly branch was unreachable.
        except SettingsError as exc:
            raise click.ClickException(
                "ne demo up --live-writeback: a write-action connector variable "
                "could not be parsed. Complex fields (objects / tables / site_ids) "
                f"must be valid JSON — {exc}. "
                "See docs/demo/README.md for the env recipe."
            ) from exc
        except ValueError as exc:
            raise click.ClickException(
                f"ne demo up --live-writeback: {exc} See docs/demo/README.md for the env recipe."
            ) from exc


_APP_DOCKERFILE_RELPATH = Path("deploy") / "docker" / "ne-app" / "Dockerfile"
_USER_DIRECTIVE_RE = re.compile(r"^\s*USER\s+(\d+):(\d+)\s*$")


def _app_container_uid_gid(root: Path) -> tuple[int, int]:
    """The (uid, gid) the ``ne-app`` container actually runs as, DERIVED from
    ``deploy/docker/ne-app/Dockerfile``'s ``USER`` directive — the artifact the
    image is built from — never a hand-pinned constant.

    Docker semantics: when a Dockerfile carries more than one ``USER``
    directive, the LAST one governs the runtime stage (earlier ones apply to
    earlier build stages only). This walks every line matching
    ``^\\s*USER\\s+(\\d+):(\\d+)\\s*$`` and returns the LAST match — that line
    is the same numeric ``USER uid:gid`` directive
    tools/ne-cli/tests/test_ne_app_image.py:76 (the image guardian) already
    pins to a specific non-root uid, cross-checked here rather than
    re-pinned.

    A missing Dockerfile, a Dockerfile with no numeric ``USER uid:gid`` line
    (e.g. a symbolic ``USER ne``), or any other parse miss is a LOUD
    ``click.ClickException`` naming the Dockerfile path — this function never
    falls back to a pinned constant, because a silent fallback is exactly the
    #377-shaped defect this unit removes: a security-relevant number derived
    from the wrong (or no) source.

    This DERIVES the uid from the same source the image is built from —
    ``ne demo up`` rebuilds on source drift (see :func:`_image_up_to_date`),
    so the derived uid matches the RUNNING image except under
    ``--skip-image-build`` with a stale image already on disk (a known
    residual hazard, not closed by this unit). Rootful-docker identity uid
    mapping is assumed; detecting Docker userns-remap (which would remap this
    uid inside the container) is out of scope — it would put a daemon
    dependency inside a function whose design point is unit-testability
    without one.

    THE RESIDUAL'S DANGEROUS DIRECTION IS FALSE-PASS. Under userns-remap the
    in-container uid differs from the derived one, so this check can report
    readable when the container still cannot read — which is precisely the
    defect it exists to remove: a human approval spent on a write that then
    dies with PermissionError. (False-FAIL is merely a refused staging run and
    costs nothing.) Verified 2026-08-03 that the deployment this ships for runs
    rootful docker with identity mapping, so the residual is not live there;
    a deployment that enables userns-remap must re-open this.
    """
    dockerfile = root / _APP_DOCKERFILE_RELPATH
    try:
        text = dockerfile.read_text(encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(
            f"could not read {dockerfile} to derive the ne-app container's runtime "
            "USER — the PEM readability pre-check cannot model the container's "
            "security context without it. This is not a fallback situation: fix "
            "the path or the Dockerfile."
        ) from exc

    matches = [m for line in text.splitlines() if (m := _USER_DIRECTIVE_RE.match(line))]
    if not matches:
        raise click.ClickException(
            f"{dockerfile} has no numeric `USER uid:gid` directive — the runtime "
            "USER (e.g. `USER <uid>:<gid>`) could not be derived, so the PEM "
            "readability pre-check cannot model the container's security "
            "context. Refusing to guess a uid."
        )
    last = matches[-1]
    return int(last.group(1)), int(last.group(2))


#: ``system.posix_acl_access`` xattr wire format (Linux ``acl_extended_file``):
#: a little-endian u32 version, then a sequence of 8-byte entries
#: ``tag:u16, perm:u16, id:u32``. See acl(5) / <linux/posix_acl_xattr.h>.
_ACL_EA_VERSION = 0x0002
_ACL_TAG_USER = 0x02
_ACL_TAG_MASK = 0x10
_ACL_PERM_READ = 0x04


def _parse_posix_acl_named_user_read(blob: bytes, uid: int) -> bool | None:
    """Does a NAMED-USER entry in a ``system.posix_acl_access`` xattr ``blob``
    grant ``uid`` a read, after applying the ACL_MASK entry (POSIX.1e: when a
    mask entry is present, it — not the named entry's own perm bits alone —
    is the effective permission for every named user/group entry)?

    Returns ``True`` (a named-user entry for ``uid`` exists and grants read),
    ``False`` (a named-user entry for ``uid`` exists and denies — this is
    AUTHORITATIVE: a named-user entry overrides the base owner/group/other
    classes for that uid, it never falls back to them), or ``None`` (no
    named-user entry names ``uid`` at all — the ACL says nothing extra about
    this uid, so the caller should fall back to ordinary mode-bit
    classification). A malformed, truncated, or wrong-version blob also
    returns ``None`` — fails OPEN to the mode-bit fallback, not a crash or a
    spurious deny; a corrupt xattr must never itself become a denial.

    This exists to fix a false-fail: ``sudo setfacl -m u:<uid>:r <pem>`` (the
    exact remedy :func:`_stage_live_writeback` prints) does NOT change the
    file's owning uid/gid — it adds a named-user ACL entry and folds the
    effective group-class permission into a MASK entry that the kernel then
    reflects in ``st_mode``'s group nibble. A stat-only, mode-only check has
    no way to see the named-user grant: st_uid/st_gid stay unrelated to the
    granted uid, so the check falls through to the OTHER class, which the
    named-user entry deliberately left empty (least-privilege: everyone else
    still gets nothing). Without this parse, the printed remedy would leave
    the owner blocked on the least-privileged of the two remedies offered.
    """
    if len(blob) < 4:
        return None
    (version,) = struct.unpack_from("<I", blob, 0)
    if version != _ACL_EA_VERSION:
        return None
    entries: list[tuple[int, int, int]] = []
    offset = 4
    while offset + 8 <= len(blob):
        tag, perm, entry_id = struct.unpack_from("<HHI", blob, offset)
        entries.append((tag, perm, entry_id))
        offset += 8
    mask_perm: int | None = None
    for tag, perm, _entry_id in entries:
        if tag == _ACL_TAG_MASK:
            mask_perm = perm
    for tag, perm, entry_id in entries:
        if tag == _ACL_TAG_USER and entry_id == uid:
            effective = perm & mask_perm if mask_perm is not None else perm
            return bool(effective & _ACL_PERM_READ)
    return None


def _read_acl_blob(path: Path) -> bytes | None:
    """The raw ``system.posix_acl_access`` xattr of ``path``, or ``None`` when
    the platform has no ``os.getxattr`` (non-Linux), the file carries no such
    xattr (``ENODATA`` — the common case: plain POSIX permission bits only,
    no extended ACL), or any other ``OSError`` occurs reading it. Fails OPEN
    to the mode-bit fallback on every error path — an xattr read failure
    must never itself become a spurious deny."""
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        return None
    try:
        return getxattr(str(path), "system.posix_acl_access")
    except OSError:
        return None


def _pem_read_denial(
    st: os.stat_result, uid: int, gid: int, *, acl_blob: bytes | None = None
) -> str | None:
    """POSIX read-access check: is a file with stat result ``st`` (and,
    optionally, the parsed ``system.posix_acl_access`` xattr ``acl_blob``)
    readable by a process running as ``uid:gid``? Returns ``None`` when
    readable, else a short reason naming the file's own ``uid``/``gid``/mode
    and which permission class (or ACL entry) denied the read.

    ACL takes precedence when it speaks: if ``acl_blob`` carries a
    NAMED-USER entry for ``uid`` (see :func:`_parse_posix_acl_named_user_read`),
    that entry is AUTHORITATIVE — it grants or denies regardless of what the
    base owner/group/other classes below would say, because that is exactly
    what a POSIX ACL named-user entry means (``sudo setfacl -m u:<uid>:r
    <pem>``, one of the two remedies this check prints, works ONLY because of
    this branch). When the ACL says nothing about ``uid`` (no matching
    named-user entry, or no ACL at all), the ONE-CLASS base check applies:

    - ``st.st_uid == uid``  -> the OWNER class governs (``S_IRUSR``), even if
      group or other would have allowed the read. A 0644 file owned by a
      DIFFERENT uid than the caller falls through to group/other, but a file
      owned BY the caller is judged on the owner bit alone.
    - ``st.st_gid == gid`` (uid did not match) -> the GROUP class governs
      (``S_IRGRP``).
    - neither matches -> the OTHER class governs (``S_IROTH``).

    ``ne-app``'s numeric ``USER uid:gid`` directive runs with NO supplementary
    groups, so the container process's primary uid and gid alone determine
    the class — there is no group list to consult.

    Why not ``os.access()``: it answers "can the CURRENT process read this
    file", evaluated with the CALLING process's real/effective uid — as root
    it returns ``True`` for a 0600 root-owned file, which is exactly the live
    defect this unit closes (demo.example.host audit seq 10866: the host-side
    check, run as root, could read a PEM the container's runtime uid could
    not).
    ``os.access`` cannot be made to answer for an arbitrary OTHER uid/gid, so
    it is the wrong tool no matter how it is called here.

    Why parent-directory traverse is irrelevant: Docker bind-mounts the PEM's
    file INODE directly into the container; path resolution/traversal happens
    in the Docker daemon (running as root) before the bind-mount is set up,
    and inside the container the parent directory (``/writeback-secrets``) is
    container-owned. Only the leaf file's own permission bits (and ACL)
    govern whether the container process can read the mounted content.
    """
    mode = stat.S_IMODE(st.st_mode)
    if acl_blob is not None:
        named_user_read = _parse_posix_acl_named_user_read(acl_blob, uid)
        if named_user_read is True:
            return None
        if named_user_read is False:
            return (
                f"file uid {st.st_uid} gid {st.st_gid} mode {oct(mode)}: a POSIX ACL "
                f"named-user entry for uid {uid} explicitly denies read (a named-user "
                "entry overrides the base owner/group/other classes for that uid)"
            )
        # named_user_read is None: no named-user entry names this uid — the
        # ACL says nothing extra about it, fall through to the base classes.
    if st.st_uid == uid:
        if mode & stat.S_IRUSR:
            return None
        cls = "owner"
    elif st.st_gid == gid:
        if mode & stat.S_IRGRP:
            return None
        cls = "group"
    else:
        if mode & stat.S_IROTH:
            return None
        cls = "other"
    return (
        f"file uid {st.st_uid} gid {st.st_gid} mode {oct(mode)}: "
        f"{cls}-class denies read to uid {uid} gid {gid}"
    )


def _stage_live_writeback(
    workdir: Path,
    manifest_dir: Path,
    *,
    graph_only: bool = False,
    draft_mailbox: str | None = None,
) -> _LiveWritebackStaging:
    """Stage the ``--live-writeback`` connector env + PEM mounts + marker.

    Collects every credential/config ``NE_SQL_*``/``NE_SALESFORCE_*``/
    ``NE_M365_*`` var already exported in the host environment (the owner's
    shell, per docs/demo/README.md's env recipe), excluding both connectors'
    ``*_AUTHORITY_*`` namespaces. Authority selection and its exact bounded
    sweep are fixed non-secret overlay configuration at the explicit
    ``--live-writeback`` boundary; ambient shell values may neither select nor
    skew them, and they never enter the secret-bearing env file. Injects
    :data:`_SQL_WRITEBACK_FALLBACK_ENV` ONLY when no ``NE_SQL_*`` var is present.
    Then :func:`_check_writeback_legs` fails loud — naming every leg that does
    not validate — before touching docker at all, by CONSTRUCTING the same
    connector settings classes the in-container boot validator constructs
    (never a hand-maintained key list, which drifts), including the fixed live
    authority map used by the real roots.

    Every collected key ending ``_PEM_PATH`` is resolved on the HOST (fail loud
    if the file is absent), mounted read-only into the container at
    ``/writeback-secrets/<basename>``, and its env value REWRITTEN to that
    container path — the key material itself never rides the overlay YAML.
    The resulting env is written to a 0600 file under the gitignored workdir
    (never echoed — C12); the marker records ONLY the mode + manifest dir.

    NE-CLI-DOTENV-UNIFY note: this now serializes through the shared
    ``ne.cli.envfile.dotenv_line`` rather than a private copy, which is
    STRICTER than before on one axis — a control character (e.g. a stray tab
    in an operator-exported connector value) now fails loud naming the key
    only, where the old private ``_dotenv_line`` accepted it (a tab is
    faithfully preserved inside compose-go single quotes, so this was never a
    corruption bug, just a narrower accepted-input surface). If
    ``--live-writeback`` starts failing on a previously-working connector env
    var, check it for control characters first.

    NE-WRITEBACK-PEM-READABLE invariant: every ``*_PEM_PATH`` is additionally
    checked for readability BY THE CONTAINER UID derived from the ne-app
    Dockerfile's ``USER`` directive (:func:`_app_container_uid_gid`), on Linux
    hosts, at staging — before any env file, marker, or container exists. This
    is the same wrong-security-context shape PR #377 removed from the leg
    validation above, one level down: a pre-check must answer the question
    the container will ask, not the question the invoking host user's
    permissions happen to answer. Without this, a governed write can fail
    AFTER a human approval is spent (demo.example.host audit seq 10866).
    """
    # Graph-only mode stays M365-only DELIBERATELY (NE-CS-SEND kept this): a
    # hostile or half-exported ambient NE_SALESFORCE_*/NE_SQL_* value must never
    # become load-bearing for a `--graph` deploy (pinned by
    # test_graph_only_stages_only_m365_and_enables_only_governed_draft). The
    # Salesforce Tier-1 note rides the full `--live-writeback` surface, which
    # the owner's combined `--live-writeback --graph` bring-up uses.
    admitted_prefixes = (
        ("NE_M365_",)
        if graph_only
        else (
            "NE_SQL_",
            "NE_SALESFORCE_",
            "NE_M365_",
        )
    )
    collected: dict[str, str] = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(admitted_prefixes) and not key.startswith(_WRITEBACK_AUTHORITY_PREFIXES)
    }
    if not graph_only and not any(key.startswith("NE_SQL_") for key in collected):
        collected.update(_SQL_WRITEBACK_FALLBACK_ENV)

    normalized_draft_mailbox: str | None = None
    selected_write_tools: tuple[str, ...] = ()
    if not graph_only and draft_mailbox is not None and draft_mailbox.strip():
        # NE-CS-USABLE-ADDRESS: full live-writeback mode ALSO admits an exact
        # draft mailbox. It becomes NE_API_EMAIL_DRAFT_ALLOWED_MAILBOXES (the
        # draft action's destination authority AND the composition root's
        # default mailbox, since NE-162 keeps mailbox addresses out of
        # frontier-visible text). Optional: omitted, the connector credential
        # stays the mailbox authority exactly as before.
        from pydantic import ValidationError as _ValidationError

        from ne.runtime.reference_actions.email_draft import EmailDraftInput

        normalized_draft_mailbox = draft_mailbox.strip().casefold()
        try:
            EmailDraftInput(
                mailbox=normalized_draft_mailbox,
                subject="staging validation",
                body="staging validation",
                to=_DEMO_SEND_RECIPIENT,
            )
        except _ValidationError as exc:
            raise click.ClickException(
                "--m365-draft-mailbox is not a valid mailbox identity"
            ) from exc
    if graph_only:
        if draft_mailbox is None or not draft_mailbox.strip():
            raise click.ClickException(
                "ne demo up --graph requires --m365-draft-mailbox so the governed "
                "draft action cannot address an arbitrary tenant mailbox"
            )
        from pydantic import ValidationError as _ValidationError

        from ne.runtime.reference_actions.email_draft import EmailDraftInput

        normalized_draft_mailbox = draft_mailbox.strip().casefold()
        try:
            EmailDraftInput(
                mailbox=normalized_draft_mailbox,
                subject="staging validation",
                body="staging validation",
                to=_DEMO_SEND_RECIPIENT,
            )
        except _ValidationError as exc:
            raise click.ClickException(
                "--m365-draft-mailbox is not a valid mailbox identity"
            ) from exc

        from pydantic_settings import SettingsError

        from ne.api.platform import _load_selected_write_action_env_settings

        # NE-CS-SEND: the graph-mode selection is draft + REAL Tier-2 send (both
        # M365 — one connector leg). The selection is validated HERE — before
        # any Docker call — by constructing the same connector settings classes
        # the in-container boot validator constructs, and the EXACT validated
        # tuple is what the overlay emits, so staging and boot can never select
        # differently. (The Tier-1 Salesforce note is a --live-writeback
        # capability; graph-only mode deliberately never reads NE_SALESFORCE_*.)
        selected_write_tools = tuple(sorted({_M365_DRAFT_TOOL, _M365_SEND_TOOL}))
        with _environ_overlay({**collected, **dict(_M365_GRAPH_AUTHORITY_ENV)}):
            try:
                _load_selected_write_action_env_settings(selected_write_tools)
            except (SettingsError, ValueError) as exc:
                raise click.ClickException(
                    f"ne demo up --graph: {exc} See docs/demo/README.md for the env recipe."
                ) from exc
    else:
        _check_writeback_legs({**collected, **dict(_LIVE_WRITEBACK_AUTHORITY_ENV)})

    pem_keys = [key for key in sorted(collected) if key.endswith("_PEM_PATH")]

    pem_mounts: list[tuple[str, str]] = []
    for key in pem_keys:
        host_path = Path(collected[key]).expanduser()
        if not host_path.is_file():
            raise click.ClickException(
                f"{key}={host_path} does not exist, or is not visible to this shell"
            )
        host_abs = host_path.resolve()
        st = host_abs.stat()

        # Derive the container's runtime uid/gid from the Dockerfile on EVERY
        # host platform (not just Linux), so a Dockerfile that stops parsing
        # (e.g. the USER directive is edited to a symbolic name) fails loud
        # everywhere, not only on the one platform where the read-check below
        # is actually enforced. Derived fresh per key rather than hoisted into
        # a pre-loop Optional local — the Dockerfile read is cheap and this
        # removes a nullable-then-asserted local entirely.
        from ne.cli.checks import _find_repo_root

        uid, gid = _app_container_uid_gid(_find_repo_root(Path.cwd()))

        # Enforcement is gated to real Linux hosts: host permission bits (and
        # ACL xattrs) only answer the container's question when host and
        # container share a kernel with an identity uid mapping. On Docker
        # Desktop (darwin / win32) the VM's file-sharing layer mediates
        # container file access itself — host stat bits do not answer the
        # container's question there, and enforcing this check on
        # macOS/Windows would break correctly-working owner demo runs (the
        # control this unit must not regress).
        if sys.platform == "linux":
            acl_blob = _read_acl_blob(host_abs)
            denial = _pem_read_denial(st, uid, gid, acl_blob=acl_blob)
            if denial is not None:
                raise click.ClickException(
                    f"{key}={host_abs} is not readable by the ne-app container "
                    f"user uid {uid} gid {gid} (deploy/docker/ne-app/Dockerfile "
                    f"USER): {denial}. The container performs the write, not "
                    "this shell — staging must fail HERE, not after a human "
                    "approval is spent. Grant that uid read access yourself, "
                    f"e.g. `sudo setfacl -m u:{uid}:r {host_abs}` or "
                    f"`sudo chown {uid} {host_abs}` — ne demo up never alters "
                    "your key files. Nothing was staged."
                )
        container_path = f"/writeback-secrets/{host_abs.name}"
        pem_mounts.append((str(host_abs), container_path))
        collected[key] = container_path

    overlay_dir = workdir / "overlay"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    # NE-DEMO-WRITEBACK-LIVE Codex C1: Compose resolves a relative `env_file:`
    # path against the base compose PROJECT's directory, not the invocation
    # directory (the same reason `seed_abs`/`host_abs` above are resolved) —
    # a relative `--workdir` (the `ne demo up` default) would otherwise point
    # at a file that doesn't exist from Compose's perspective. Resolve BEFORE
    # writing so the returned staging always carries an absolute path.
    env_file = (overlay_dir / ("graph.env" if graph_only else "writeback.env")).resolve()
    env_file.write_text(
        "".join(dotenv_line(k, v) for k, v in sorted(collected.items())), encoding="utf-8"
    )
    env_file.chmod(0o600)

    if not graph_only:
        (workdir / _WRITEBACK_MARKER).write_text(
            json.dumps({"mode": "live-crm", "manifest_dir": str(manifest_dir)}, sort_keys=True),
            encoding="utf-8",
        )

    return _LiveWritebackStaging(
        env_file=env_file,
        pem_mounts=tuple(pem_mounts),
        draft_mailbox=normalized_draft_mailbox,
        selected_write_tools=selected_write_tools,
    )


def _live_writeback_target(manifest_dir: Path) -> str:
    """The live Salesforce Account id Beat 4's CRM write targets.

    Loads ``manifest_dir/salesforce.json`` (the NE-102 meridian-seed manifest)
    and returns the FIRST (sorted-key order — deterministic: ``mer-hh-001``
    first) ``mer-hh-*`` household-Account record whose value shape-matches a
    Salesforce id (15-18 case-safe alphanumeric chars). Fails loud, naming the
    manifest path and the seed-push remedy, on a missing file or no matching
    record.
    """
    manifest_path = manifest_dir / "salesforce.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise click.ClickException(
            f"no meridian-seed Salesforce manifest at {manifest_path} — push the Meridian "
            "corpus into the live tenant first: `ne-meridian-seed push --tenant salesforce`"
        ) from exc
    records = manifest.get("records", {})
    for key in sorted(records):
        if not key.startswith("mer-hh-"):
            continue
        value = records[key]
        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9]{15,18}", value):
            return value
    raise click.ClickException(
        f"no mer-hh-* record with a valid Salesforce id (15-18 alnum chars) found in "
        f"{manifest_path} — push the Meridian corpus into the live tenant first: "
        "`ne-meridian-seed push --tenant salesforce`"
    )


def _resolve_writeback(workdir: Path) -> tuple[str, dict[str, Any] | None]:
    """The write-back action + args ``ne demo run`` drives Beat 4 with.

    No marker (the default — ``ne demo up`` without ``--live-writeback``, or
    never staged) → the inert demo action with ``None`` args, BYTE-IDENTICAL to
    the pre-unit default (``run_demo_arc``'s own default args, ``{"to": "ops"}``).
    A ``mode == "live-crm"`` marker → the real NE-050 CRM-note action targeting
    a live seeded Account id, with a fresh per-run timestamped title (never a
    static ``request_id`` — the per-turn action scope mints one; a static value
    would dedup-collapse re-runs into 'deduplicated').
    """
    marker_path = workdir / _WRITEBACK_MARKER
    if not marker_path.is_file():
        return "write.external.demo.send", None
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("mode") != "live-crm":
        return "write.external.demo.send", None

    from ne.cli.demo.arc import CRM_WRITEBACK_ACTION

    record_id = _live_writeback_target(Path(marker["manifest_dir"]))
    title = f"Neutron demo governed write-back {datetime.now(UTC):%Y-%m-%d %H:%MZ}"
    body = (
        "Created by the Neutron demo arc Beat 4 - governed Tier-2 write-back "
        "released by HITL approval."
    )
    return CRM_WRITEBACK_ACTION, {"record_id": record_id, "title": title, "body": body}


# ---------------------------------------------------------------------------
# NE-BUNDLE-GATEWAY-CONFIG: --frontier staging (real frontier API key -> a
# 0600 overlay env file — the --live-writeback owner-secrets precedent)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _FrontierStaging:
    """What :func:`_stage_frontier` staged for this ``ne demo up --frontier`` run."""

    provider: str
    #: The 0600 env file (workdir/overlay/frontier.env) carrying the provider's
    #: API key — referenced from the overlay via ``env_file:``, never inlined.
    #: ALWAYS absolute (mirrors :attr:`_LiveWritebackStaging.env_file` — the
    #: Compose env_file relative-path trap: Compose resolves a relative
    #: ``env_file:`` path against the base compose project's directory, not the
    #: invocation directory).
    env_file: Path


#: The workdir-relative marker recording which provider the LAST
#: ``ne demo up --frontier`` staged — no secret (provider name only). Read by
#: :func:`_frontier_posture_note` at ``ne demo run`` time.
_FRONTIER_MARKER = "demo-frontier.json"

#: The host env var each supported provider's API key is read from — the SAME
#: owner-secrets pattern the salesforce/m365 write-back legs use (see
#: docs/demo/README.md 'Frontier model swap (--frontier)'): the owner exports
#: it from ``~/ne-owner-secrets/<provider>/api-key`` before running `ne demo up`.
_FRONTIER_KEY_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _stage_frontier(workdir: Path, provider: str) -> _FrontierStaging:
    """Stage the ``--frontier`` provider's API key into a 0600 overlay env file.

    Fails LOUD (``click.ClickException``, naming the exact missing env var and
    the provider) before any docker call — the owner's fail-loud instinct,
    kept: a demo that silently fell back to the open-weight lane on a missing
    key would make the swap beat a lie. Mirrors :func:`_stage_live_writeback`'s
    message shape.

    litellm resolves ``ANTHROPIC_API_KEY``/``OPENAI_API_KEY`` from the
    container's own env automatically (its standard per-provider env-var
    convention), so the key NEVER rides ``NE_GATEWAY_MODELS`` JSON or the
    overlay YAML (C12) — it exists ONLY in this 0600 file, referenced from the
    overlay via compose ``env_file:`` (never echoed to stdout/logs).
    """
    var = _FRONTIER_KEY_VARS[provider]
    value = os.environ.get(var)
    if not value:
        raise click.ClickException(
            f"ne demo up --frontier {provider} requires {var} exported on the "
            "host (none found) — see docs/demo/README.md 'Frontier model swap "
            f"(--frontier)' for the env recipe "
            f"(~/ne-owner-secrets/{provider}/api-key is the owner-secrets "
            "precedent, same pattern as the Salesforce/M365 write-back legs)."
        )

    overlay_dir = workdir / "overlay"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    # Resolve BEFORE writing so the returned staging always carries an absolute
    # path (the same Compose env_file relative-path trap fixed for
    # writeback.env — see _LiveWritebackStaging.env_file).
    env_file = (overlay_dir / "frontier.env").resolve()
    env_file.write_text(dotenv_line(var, value), encoding="utf-8")
    env_file.chmod(0o600)

    (workdir / _FRONTIER_MARKER).write_text(
        json.dumps({"provider": provider}, sort_keys=True), encoding="utf-8"
    )
    return _FrontierStaging(provider=provider, env_file=env_file)


def _frontier_posture_note(workdir: Path) -> str | None:
    """A one-line operator note naming the ACTIVE frontier swap, or ``None``
    when no ``ne demo up --frontier`` marker is staged (the open-weight
    default). Read + echoed by :func:`demo_run` before the narrated arc starts
    — the ``--gpu-vllm`` framing precedent: Beat 0's "0 BYTES egress" framing
    does not hold while a frontier lane is active either.
    """
    marker_path = workdir / _FRONTIER_MARKER
    if not marker_path.is_file():
        return None
    from ne.gateway.egress import KNOWN_FRONTIER_PROVIDER_HOSTS

    provider = json.loads(marker_path.read_text(encoding="utf-8"))["provider"]
    host = KNOWN_FRONTIER_PROVIDER_HOSTS[provider]
    return (
        f"frontier lane ACTIVE ({provider}): 'agent' turns egress to {host} — "
        "allowlisted (C2), redacted pre-egress via the stdlib regex backend "
        "(the sidecar-free NE_GOVERN_REDACTOR override this lane requires), "
        "audited; Beat 0's zero-egress framing does not apply this run"
    )


def _console_base(hostname: str, https_port: int) -> str:
    """The console/API origin URL for ``hostname`` on the chosen publish port.

    NE-DEMO-HTTPS-PORT default byte-parity requirement: when ``https_port`` is the
    default 443 this returns ``https://{hostname}`` (the explicit ``:443`` is
    OMITTED, exactly as every URL/redirect is spelled today) so the default run is
    byte-identical; a non-default port appends ``:{https_port}``.
    """
    if https_port == 443:
        return f"https://{hostname}"
    return f"https://{hostname}:{https_port}"


def _onbox_dial(hostname: str, port: int, external_url: str | None) -> tuple[str, dict[str, str]]:
    """Dial target + extra headers for an ON-BOX HTTPS call to the demo stack.

    NE-DEMO-EXTERNAL-URL hairpin fix. Default (``external_url`` is None): dial the
    pilot ``hostname`` directly with NO extra headers — byte-identical to today
    (443 omits the explicit port, exactly as :func:`_console_base` spells it).

    When ``external_url`` is set, an external edge fronts the demo and the public
    ``hostname`` resolves to that edge's PUBLIC IP. Dialing ``https://{hostname}:{port}``
    FROM THE BOX hairpin-NATs out to the edge and back — which most NAT/edge setups
    drop (a box can't reach its own public IP through the edge), so the call 502s /
    times out and the bring-up aborts (e.g. Keycloak's admin-token grant → non-JSON
    502 body → JSONDecodeError in ``_provision_keycloak``). Instead dial loopback
    (``127.0.0.1``, ALWAYS reachable on-box) and carry the public ``Host`` header so
    Caddy still routes to the ``{hostname}`` vhost and Keycloak (``KC_HOSTNAME``
    hostname-strict) mints its issuer/frontend URLs correctly. The ``Host`` header
    alone is NOT enough: the caller must also mount :func:`_onbox_transport` so the
    TLS handshake carries ``hostname`` as SNI — dialing an IP literal sends no SNI,
    and the pilot Caddy serves ONLY named vhosts, so it rejects the handshake with
    ``tlsv1 alert internal error`` before a single HTTP byte moves (observed live on
    the demo host, 2026-08-04). ``verify=False`` (Caddy's local-CA cert) tolerates the
    certificate; it cannot fix the missing SNI. The public/external URL is used ONLY
    for browser-facing values (the ne-console redirect-URIs/web-origins), never as an
    on-box dial target.
    """
    if external_url is None:
        base = f"https://{hostname}" if port == 443 else f"https://{hostname}:{port}"
        return base, {}
    host = hostname if port == 443 else f"{hostname}:{port}"
    return f"https://127.0.0.1:{port}", {"Host": host}


def _onbox_transport(hostname: str, external_url: str | None, *, use_async: bool = False) -> Any:
    """httpx transport that carries ``hostname`` as TLS SNI for loopback dials.

    The :func:`_onbox_dial` ``Host`` header routes the HTTP request to the right
    Caddy vhost, but the TLS handshake happens FIRST — and a dial to ``127.0.0.1``
    sends no SNI at all (an IP literal is never a ``server_name``), so the pilot
    Caddy, which serves ONLY named vhosts, rejects the handshake with
    ``tlsv1 alert internal error`` before any HTTP exchange (observed live on
    the demo host, 2026-08-04 — the failure a Host header cannot see, let alone fix).
    This transport injects the public ``hostname`` as the httpcore ``sni_hostname``
    request extension so the handshake presents the vhost's name; certificate
    verification stays off (the pilot Caddy's local-CA cert — demo-only, same
    confinement as the callers' ``verify=False``).

    Returns ``None`` when ``external_url`` is unset: the default path dials the
    public hostname directly and the URL itself supplies SNI (byte-identical
    behaviour — ``httpx.Client(transport=None)`` builds the stock transport).
    """
    if external_url is None:
        return None
    import httpx

    if use_async:

        class _AsyncSNITransport(httpx.AsyncHTTPTransport):
            async def handle_async_request(self, request: Any) -> Any:
                request.extensions.setdefault("sni_hostname", hostname)
                return await super().handle_async_request(request)

        return _AsyncSNITransport(verify=False)

    class _SNITransport(httpx.HTTPTransport):
        def handle_request(self, request: Any) -> Any:
            request.extensions.setdefault("sni_hostname", hostname)
            return super().handle_request(request)

    return _SNITransport(verify=False)


#: The litellm provider/model string for each supported ``--frontier`` swap
#: target — the LIVE lane, the one that actually holds a key and egresses.
#:
#: COST-MAP STATUS, MEASURED not assumed (litellm 1.90.3,
#: ``LITELLM_LOCAL_MODEL_COST_MAP=True``, ``ne/gateway/_litellm.py``):
#: ``openai/gpt-5.5`` IS in the pinned local map; ``anthropic/claude-sonnet-5``
#: is NOT (``litellm.cost_per_token`` raises "This model isn't mapped yet").
#: An unmapped id does NOT break the call — ``get_llm_provider`` still resolves
#: it to the anthropic provider, and ``ne.gateway.cost.extract_cost`` catches
#: the lookup and degrades to ``None`` rather than failing a successful
#: completion. What it DOES break is spend accounting: while this id is
#: unmapped, the Usage rollup for ``frontier-default`` reads as no cost rather
#: than as unknown cost. That is a KPI-instrumentation gap, not an outage, and
#: closing it needs a litellm re-pin — a separate supply-chain unit. The
#: earlier claim here (that an unmapped id "would break litellm cost
#: computation", justifying the stale ``claude-sonnet-4-5`` pin) overstated
#: the consequence; the owner's model choice governs, so it is recorded
#: honestly instead of used as a reason to stay stale.
_FRONTIER_LITELLM_MODEL: dict[str, str] = {
    "anthropic": "anthropic/claude-sonnet-5",
    "openai": "openai/gpt-5.5",
}

#: Frontier model strings whose provider runs ADAPTIVE THINKING when the request
#: omits ``thinking`` — i.e. the models for which "not asking" is itself a
#: choice, and the wrong one for this deployment.
#:
#: Nothing in this repo sends ``thinking`` or ``reasoning_effort``. On a model in
#: this set that silence selects adaptive thinking AND the provider's default
#: thinking ``display`` of ``"omitted"``, which returns a thinking block whose
#: ``thinking`` field is the EMPTY STRING plus a signature and — intermittently —
#: no text block at all. litellm derives ``reasoning_content`` from that field, so
#: it arrives empty too. Live audit row, owner's demo question, 2026-08-26::
#:
#:     ne.gateway.complete  outcome=error  resource=anthropic/claude-sonnet-5
#:       finish_reason=stop   output_tokens=144   input_tokens=5792
#:       response_channels=thinking_blocks=1,provider_specific_fields=2
#:       empty_completion_tokens=144
#:
#: The model completed NORMALLY, billed 144 reasoning tokens, and served nothing.
#: This set exists so that adding a model to :data:`_FRONTIER_LITELLM_MODEL` (or
#: bumping one of its values) cannot silently re-acquire that failure mode — the
#: guardian test sweeps every emitted frontier model against it.
_ADAPTIVE_THINKING_DEFAULT_ON: frozenset[str] = frozenset(
    {
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-5",
        "anthropic/claude-fable-5",
        "anthropic/claude-mythos-5",
    }
)

#: Frontier model strings that REFUSE ``thinking: {"type": "disabled"}`` — the
#: provider rejects the request with a 400 because thinking is structurally
#: always-on for that model. They are therefore exempt from the guardian above:
#: the empty-answer failure mode is unreachable-by-config for them, not fixed.
#:
#: ``anthropic/claude-fable-5`` is the live case (``frontier-best`` on the demo
#: tip). It is a config-only row — the NE-161 runtime gate refuses frontier
#: sub-agent routing, so ``adjudicate`` never actually calls it — but writing the
#: disable onto it would turn an inert row into a guaranteed 400 the moment that
#: gate ever opened. This is the reason ``frontier-best`` and ``frontier-default``
#: DIVERGE rather than carrying the same parameter.
_THINKING_DISABLE_REFUSED: frozenset[str] = frozenset(
    {
        "anthropic/claude-fable-5",
        "anthropic/claude-mythos-5",
    }
)

#: Per-MODEL provider request params folded into a frontier model's
#: ``ModelDef.extra_params`` — i.e. into that deployment's litellm_params, which
#: litellm merges into the wire body. Model-independence is a hard architectural
#: rule here, so this is a CONFIG TABLE and not a provider branch in gateway
#: code: the gateway stays provider-opaque and merely forwards what the config
#: declares (``gateway.py`` Router construction, ``**d.extra_params``).
#:
#: Keyed by the litellm MODEL string, never by provider, and that is the whole
#: point — ``thinking`` is a model-generation parameter, not a vendor-wide one.
#: The same vendor both requires this key (``claude-sonnet-5``, above) and
#: rejects it (``claude-fable-5``, :data:`_THINKING_DISABLE_REFUSED`), so a
#: provider-keyed table would be factually wrong and would 400 the best lane.
#:
#: Note what this does NOT buy: gateway policy discards reasoning text anyway, so
#: the saving is not tokens — it is removing an intermittent EMPTY ANSWER from the
#: demo's primary answering lane. Nor does it weaken ``ReasoningWithoutAnswerError``:
#: that terminal still reports reasoning-without-answer accurately if the provider
#: ever returns it again. This reduces how often that happens; it does not replace
#: handling it.
#:
#: The documented risk of disabling thinking — the model writing a tool call into
#: visible text instead of a native ``tool_use`` block — is NOT a regression here.
#: Decision D3 does tool-calling entirely IN-BAND: the tool call *is* a line of
#: JSON inside a plain text message, ``tool_choice`` is never sent, and the parser
#: reads the model's text (``ne/runtime/model.py``). Visible-text tool calls are
#: this codebase's normal operating mode, not a failure of it.
#:
#: ``allowed_openai_params`` rides alongside ``thinking`` as an escape hatch.
#: litellm gates ``thinking`` on the target model's ``supports_reasoning``, read
#: from its cost map; the chokepoint forces ``LITELLM_LOCAL_MODEL_COST_MAP=True``
#: (``ne/gateway/_litellm.py`` — no network fetch at import). On a pin whose
#: packaged map lacks ``anthropic/claude-sonnet-5``, a bare ``thinking`` raises
#: ``UnsupportedParamsError`` before any request is built and every frontier call
#: fails. The pinned litellm 1.100.4 map contains the id, so the hatch is not
#: needed on this pin; it stays as a guard against a re-pin that drops the id
#: (``test_extra_params_reach_wire.py`` records which case the pin is in).
#:
#: ``allowed_openai_params`` is a litellm CONTROL key: litellm consumes it and it
#: never appears in the provider body (pinned by the wire-body test's key-set
#: assertion).
#:
#: LIVE on this branch: ``_FRONTIER_LITELLM_MODEL`` pins
#: ``anthropic/claude-sonnet-5``, so ``frontier-default`` DOES carry these params
#: and the emitted ``NE_GATEWAY_MODELS`` JSON changes — a config_hash-affecting,
#: release-level change. ``frontier-best`` names ``anthropic/claude-fable-5`` and
#: correctly gets NOTHING: the provider 400s a thinking disable on it.
_FRONTIER_MODEL_EXTRA_PARAMS: dict[str, dict[str, Any]] = {
    "anthropic/claude-sonnet-5": {
        "thinking": {"type": "disabled"},
        "allowed_openai_params": ["thinking"],
    },
}


def _frontier_extra_params(litellm_model: str) -> dict[str, Any]:
    """Splat-ready ``{"extra_params": {...}}`` for a frontier model row, or an
    EMPTY dict when this model declares none.

    Returning ``{}`` rather than ``{"extra_params": {}}`` is deliberate: a model
    with no entry emits the exact JSON it emitted before this table existed, so
    adding the seam does not itself move the config_hash.
    """
    params = _FRONTIER_MODEL_EXTRA_PARAMS.get(litellm_model)
    return {"extra_params": dict(params)} if params else {}


#: Output reserve for the LIVE frontier lane (``frontier-default``), in tokens.
#:
#: DERIVED, not chosen. Until NE-FRONTIER-OUTPUT this key was absent, so
#: ``ModelDef`` filled ``min(4096, context_window // 8)`` = **4096** — a sixth of
#: what the private lane a few lines below declares, for the SAME ``agent`` task,
#: over the SAME corpus, answering the SAME multi-hop questions. That private
#: key's own comment records why 4096 is not enough: at 4096 a grounded synthesis
#: turn reproducibly came back ``finish_reason=length``, which the runtime could
#: only retry into ``Exceeded maximum output retries``. The frontier lane had
#: inherited exactly the configuration that failure is documented against.
#:
#: The size comes from the one figure this repo actually MEASURED for this task
#: shape: the 2026-08-21 rig runs of a prospect-shaped multi-hop question, whose
#: worst completion was **12204 tokens** (see ``private_default`` below).
#: 12288 is the smallest value on the 4096 grid this file already uses that
#: covers it. The private lane declares twice that (24576) because it serves a
#: HYBRID-THINKING model whose reasoning shares the one completion budget; the
#: frontier lane runs with no reasoning channel enabled, so its completion is
#: content only and it needs no reasoning headroom on top of the measured
#: content requirement.
#:
#: Three bounds were checked, and this value is the largest that satisfies all
#: of them on BOTH emitted shapes (a bigger round number does not):
#:
#: 1. ``ModelDef``'s ``max_output_tokens < context_window // 2``. Since
#:    NE-CTX-IN-FORCE the frontier window is the model's OWN (1_000_000 for
#:    anthropic, 200_000 for openai), so this bound is slack — but it was the
#:    binding one when the window was capped at the sealed box's 32768 (bound
#:    16383), and it is why this is its own constant rather than an inheritance
#:    of the private lane's 24576, which that shape would have REFUSED at boot.
#: 2. ``ContextBudget``'s positive-input-budget rule. Trivially satisfied at the
#:    uncapped windows; it was 32768 - 12288 - 1639 = 18841 under the old cap,
#:    and that 18841 is precisely the defect NE-CTX-IN-FORCE removed — a 1M model
#:    with a narrower input budget than the 32k private lane's 27033.
#: 3. The turn deadline. ``NE_API_TURN_TIMEOUT_S`` is 300s
#:    (``deploy/compose/pilot/compose.yaml``); a frontier generation observed at
#:    ~4096 tokens/minute puts 12288 tokens at ~180s, inside the deadline.
#:    Declaring more than can finish converts a truncated answer into a timeout
#:    — the same ceiling argument the private key makes for itself. THIS is the
#:    live bound now, and it is why uncapping the WINDOW did not raise this key.
#:
#: Raising it still SHRINKS the frontier lane's input budget, but that is now
#: noise against a 1M window rather than a selection-floor question: the
#: input-budget floor in ``ne/api/model_routing.py`` is gone (NE-CTX-IN-FORCE).
_FRONTIER_MAX_OUTPUT_TOKENS = 12288


#: The SERVING context window each ``--frontier`` target offers, keyed by the
#: SAME provider key as :data:`_FRONTIER_LITELLM_MODEL` so a model id and the
#: window declared for it cannot drift apart — the defect the previous
#: ``200_000`` literal carried, where the number went on describing
#: ``claude-sonnet-4-5`` after the id beside it had moved on.
#:
#: These ARE the emitted values (NE-CTX-IN-FORCE, owner-directed 2026-09-01:
#: "every model should always use its maximum available context"). They used to
#: be ceilings that ``_gateway_lane_env`` then capped at the private lane's
#: window, which on the sealed box made a 1M model declare 32768.
_FRONTIER_CONTEXT_WINDOW: dict[str, int] = {
    # anthropic/claude-sonnet-5 — 1M serving window (Anthropic's model card;
    # the pinned litellm 1.90.3 map has no entry for this id at all, so it is
    # NOT a second source here — the cost-map note above says why).
    "anthropic": 1_000_000,
    # openai/gpt-5.5 — UNCHANGED from the pre-unit value and still a
    # DELIBERATE CONSERVATIVE FLOOR, not a per-provider fact. This unit changed
    # which model the anthropic lane names; it establishes nothing new about
    # OpenAI's, and under-declaring only shrinks the demo's context budget
    # (the safe direction). Bump only alongside a verified figure.
    "openai": 200_000,
}


def _gateway_lane_env(gpu_vllm: str | None, frontier: str | None = None) -> str:
    """Overlay env for BOTH ``NE_GATEWAY_MODELS`` and ``NE_GATEWAY_TASKS`` (NE-168).

    ALWAYS emits two models: ``private-default`` (the conversational agent's
    model — the on-box CPU vllm by default, or an EXTERNAL GPU vLLM server, e.g. a
    rented RunPod A6000, when ``gpu_vllm`` is set — for a fast, reliably-tool-
    calling conversational demo; the pod must serve
    ``--served-model-name qwen3.6-35b-a3b``,
    see the playbook §3.4) and
    ``frontier-best`` (a config-only litellm model string, NO api key, NEVER
    called — the NE-161 RUNTIME GATE refuses frontier sub-agent routing by default,
    so this is a governed-DENY exhibit, not a live egress relaxation).

    ``NE_GATEWAY_TASKS`` routes EVERY customer-text task — ``agent`` (the chat
    turn), ``triage``, ``reason``, ``skill-authoring``, ``grounding-assist`` (the
    G4 one-pass grounded re-draft, NE-HGATE-4 — the route the HGATE-3 boot
    fail-loud check requires whenever grounded+assist is enabled),
    ``graph-extract`` and both pinned Agent policies — at ``private-default``,
    with NO fallbacks; ``adjudicate`` stays at ``frontier-best``.

    **The dedicated 0.6B CPU lane is GONE ENTIRELY** (owner-directed
    2026-08-27/28): not in the menu, not any task's primary or fallback, not
    explicitly selectable. A 0.6B model answers a reasoning prompt fluently and
    wrongly rather than failing, so ANY route onto it is a silent quality
    collapse. See the rationale block on ``tasks_dict`` below. The trade-off is
    deliberate and load-bearing: with no second lane, an unreachable
    ``private-default`` makes these tasks FAIL (a named lane-exhaustion reason),
    which is the honest outcome and NOT a regression. (On the sealed default
    branch ``private-default`` itself still serves the on-box CPU vllm — the
    box's ONLY lane there; what is gone is the separate always-CPU lane that
    shadowed it under ``--gpu-vllm``.)

    When ``gpu_vllm`` is set, its host is added to the C2 egress allowlist
    (``NE_EGRESS_ALLOWED_PRIVATE_VLLM_HOSTS``) — a DELIBERATE, demo-only relaxation
    of the no-egress posture (synthetic corpus only; the sovereign product still
    serves the model IN-VPC on the customer's own GPUs). Beat 0's "0 BYTES egress"
    is not literally true while a cloud GPU lane is active — frame accordingly.

    NE-BUNDLE-GATEWAY-CONFIG: when ``frontier`` is ``'anthropic'`` or ``'openai'``,
    a 4th model ``frontier-default`` is APPENDED (NE-168 lesson — never replace) and
    ONLY the ``agent`` task is rerouted to it, with NO fallback — a private fallback
    here would let a missing/blocked frontier egress silently degrade back to the
    open-weight lane, which would fake the swap beat (the ``_guard_frontier``
    fail-loud instinct, kept, now guarding the ROUTE instead of refusing it
    outright). Every other task route (``triage``/``reason``/``adjudicate``/
    ``skill-authoring``/``grounding-assist``/``graph-extract``) is untouched BY THE
    SWAP: customer-text tasks stay structurally private, and ``adjudicate`` stays
    the ``frontier-best`` governed-DENY exhibit. ``frontier-default`` carries NO
    ``api_base`` — ``EgressPolicy._resolve_frontier_host`` derives the egress host
    from ``KNOWN_FRONTIER_PROVIDER_HOSTS`` (the SAME mapping this function derives
    the emitted ``NE_EGRESS_ALLOWED_FRONTIER_HOSTS`` allowlist from — one entry,
    never a blanket opening) and litellm resolves the API key from the container's
    own ``ANTHROPIC_API_KEY``/``OPENAI_API_KEY`` env (staged by
    :func:`_stage_frontier`), so no key literal ever rides this JSON (C12). When
    ``frontier`` is ``None`` the emitted string is BYTE-IDENTICAL to the pre-unit
    default (that pre-unit default itself moved with NE-DEMO-NO-CPU-FALLBACK; the
    claim is about the swap being additive, not about the literal never changing).
    """
    from urllib.parse import urlparse

    if gpu_vllm is not None:
        base = gpu_vllm.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3].rstrip("/")
        private_default = {
            "name": "private-default",
            "litellm_model": "hosted_vllm/qwen3.6-35b-a3b",
            "provider": "hosted_vllm",
            "lane": "private-vllm",
            "api_base": f"{base}/v1",
            # NE-CTXB-2a: the GPU rig serves --max-model-len 262144 (playbook
            # §3.4). context_window LAST to match the emitted JSON key order the
            # producer-parity/overlay literal tests assert.
            "context_window": 262144,
            # The GPU lane serves a HYBRID-THINKING reasoning model
            # (Qwen3.6-35B-A3B, --reasoning-parser qwen3) and reasoning shares
            # the one completion budget with content. Without this key the
            # ModelDef default (min(4096, cw//8) = 4096) reproducibly
            # truncated grounded synthesis turns MID-THINK (live-measured
            # 2026-08-04 on the rig: reasoning alone ran 12-14k chars,
            # finish_reason=length at exactly 4096, content empty/cut — the
            # Ask/grounded-analyst empty-content deaths). 12288 was sized on
            # 2026-08-04 at ~2.4x the then-max observed completion (5115
            # tokens) -- correct for the turns observed, but a bound derived
            # from observed traffic, not from what the model can want.
            #
            # RAISED to 24576 on 2026-08-21. A harder question (a 3-machine
            # defect-rate optimisation, the shape a prospect actually asks)
            # consumed 8303-12288+ tokens, and at temperature 0 the SAME prompt
            # varied 48% run to run. Live on the rig, 5 runs at 12288: TWO
            # returned finish_reason=length with empty content after ~103s --
            # the identical empty-content death this key exists to prevent,
            # recurring because the question fell outside the 08-04 sample.
            # At 24576 the same five all answered; the worst used 12204, i.e.
            # it would have failed by 84 tokens under the old cap.
            #
            # The ceiling is NOT this key. NE_API_TURN_TIMEOUT_S is 300s and the
            # rig serves ~120 tok/s, so ~36000 tokens is the most that can ever
            # complete; setting this higher just converts a blank answer into a
            # timeout. 24576 is ~205s of generation, leaving ~95s of margin, and
            # still sits under the ModelDef bound (< cw//2 = 131072) with ample
            # input headroom for an agentic trace. The sealed CPU branch keeps
            # the 4096 default DELIBERATELY:
            # qwen3-0.6b serves without a reasoning parser and a CPU-speed
            # 12k-token tail would be minutes, not seconds.
            "max_output_tokens": 24576,
        }
    else:
        base = None
        private_default = {
            "name": "private-default",
            "litellm_model": f"hosted_vllm/{_SERVED_NAME}",
            "provider": "hosted_vllm",
            "lane": "private-vllm",
            "api_base": "http://vllm:8000/v1",
            "context_window": 32768,
        }
    frontier_best = {
        "name": "frontier-best",
        # The BEST lane must name a DIFFERENT model from the live
        # `frontier-default` lane below, or the exhibit is a tautology: two
        # rows, one model, and "best" claiming nothing. Fable 5 is the
        # separation, and it is real in the pinned litellm 1.90.3 local cost
        # map (`claude-fable-5`, provider anthropic, max_input_tokens
        # 1000000) — unlike `claude-sonnet-5`, this id costs correctly today.
        "litellm_model": "anthropic/claude-fable-5",
        "provider": "anthropic",
        "lane": "frontier-api",
        # claude-fable-5's serving window: 1M (Anthropic's model card, and the
        # pinned litellm map's max_input_tokens for this id agrees). Config-only
        # — the NE-161 RUNTIME GATE refuses frontier sub-agent routing, so this
        # is never actually called; the number is here because the runtime gate
        # and the context-budget pre-flight READ it, and a window describing a
        # model this row no longer names is a stale claim whether or not the
        # call happens.
        #
        # UNCAPPED (NE-CTX-IN-FORCE, owner-directed 2026-09-01: "use the
        # model's context window"). This row declares what claude-fable-5
        # actually serves. It was previously capped at the private lane's
        # window because the declared window set `adjudicate`'s input-budget
        # FLOOR in `model_routing.refusal_for_target`, and at the raw 1M figure
        # that floor (~945k) made every in-VPC model an illegal repoint target
        # — the air-gap beat could never clear its last frontier primary. That
        # floor is GONE: the runtime trim guard re-derives its budget from the
        # model in force on every request, so a repoint to a narrower model is
        # trimmed to the narrower model and needs no config-time floor. The
        # NE-161 runtime gate still refuses this lane at call time — the
        # governed-DENY exhibit is unchanged.
        "context_window": 1_000_000,
        # Per-model provider params from the ONE table
        # (_FRONTIER_MODEL_EXTRA_PARAMS), so this row and `frontier-default`
        # below stay consistent BY CONSTRUCTION — they agree whenever they name
        # the same model, and diverge only where the provider itself demands it.
        # Emits NOTHING here: claude-fable-5 is in _THINKING_DISABLE_REFUSED
        # (Anthropic 400s a thinking disable on it — thinking is always on).
        **_frontier_extra_params("anthropic/claude-fable-5"),
    }
    # The OUTPUT reserve is unchanged by the window uncapping, and deliberately
    # so: NE-CTX-IN-FORCE retired the budget-ORDERING reason for this max (there
    # is no input-budget floor left to satisfy), but the TRUNCATION reason stands
    # untouched — see `_FRONTIER_MAX_OUTPUT_TOKENS`, whose 12288 is the measured
    # worst completion, and the 300s turn deadline that caps it there. Keeping the
    # max-with-the-private-reserve leaves this row byte-identical on both shapes
    # rather than silently shrinking a reserve nobody asked to shrink; it costs
    # 24576 of a 1_000_000-token window. Clamped under ModelDef's
    # `max_output_tokens < context_window // 2` bound.
    _best_window = int(frontier_best["context_window"])
    frontier_best["max_output_tokens"] = min(
        max(_FRONTIER_MAX_OUTPUT_TOKENS, int(private_default.get("max_output_tokens") or 0)),
        _best_window // 2 - 1,
    )
    models_list: list[dict[str, Any]] = [private_default, frontier_best]
    # NE-DEMO-NO-CPU-FALLBACK (owner-directed 2026-08-27, completed 2026-08-28):
    # the dedicated 0.6B CPU lane is REMOVED OUTRIGHT — no menu entry, no task
    # primary, no fallback, no explicit selection. Where a second lane is wanted,
    # the degrade goes UPWARD to ``frontier-default`` (appended, and therefore
    # wired, only when ``--frontier`` is passed — see the block below).
    #
    # WHY, and it is a demo-honesty requirement rather than a tidy-up: that lane
    # served qwen3-0.6b on the box's own CPU, while every route that named it as
    # a fallback is a CUSTOMER-FACING reasoning task (the Meridian analyst and
    # hardship-review agents, ``reason``, the G4 grounded re-draft, graph
    # extraction). A 0.6B model does not ERROR on those prompts — it answers,
    # fluently and wrongly. So the fallback converted "the frontier API hiccuped
    # and the GPU rig is unreachable" from a visible failure into a confident
    # wrong answer delivered in front of a customer, with the provenance chain
    # faithfully recording a downgrade nobody in the room would notice.
    # An honest failure is strictly better than a silent quality collapse, which
    # is the same rule the project already applies to the dead grounding assist
    # (docs/architecture/anti-hallucination-grounding.md) and to ``frontier_warm``'s refusal
    # to degrade a swap beat onto a private lane.
    #
    # CONSEQUENCE, STATED TWICE BECAUSE IT CUTS BOTH WAYS:
    #
    # (a) On the SEALED box (no ``--frontier``) there is no second lane at all,
    #     because ``frontier-default`` is not configured and naming it in a
    #     fallback would be a boot-time ``GatewayConfigError``. These tasks then
    #     FAIL when ``private-default`` cannot serve, rather than downgrading —
    #     see ``_final_failure``/``ModelCallFailedError`` in ne-gateway, whose
    #     published ``safe_reason`` names the task, the lanes tried and why.
    #     That is the intended trade, not an oversight.
    #
    # (b) With ``--frontier``, a private-primary task that loses the GPU rig
    #     degrades UPWARD to Sonnet 5 — better answers, but the run IS NO LONGER
    #     AIR-GAPPED. That is safe for quality and dangerous for the NARRATIVE,
    #     so it is only acceptable because the fallback is VISIBLE: the gateway
    #     stamps ``route_position``/``fallback_from`` provenance on the served
    #     event and the console renders the served lane, so a run that quietly
    #     used the API cannot be presented as an air-gapped one. Never claim
    #     air-gap from the CONFIG alone — read the run's served lane.
    tasks_dict: dict[str, Any] = {
        "agent": {"model": "private-default"},
        # PRE-014A: canonical Agent Runs never borrow chat's generic route.
        # Each immutable Agent version owns an independently selectable policy.
        "agent.meridian-analyst.v2": {"model": "private-default"},
        "agent.meridian-hardship-review.v3": {"model": "private-default"},
        # ``triage`` was PRIMARIED on the dedicated 0.6B CPU lane since NE-168;
        # moved to ``private-default`` by owner decision 2026-08-27, so that
        # NOTHING routes to the 0.6B lane (since removed from the menu outright).
        #
        # HONEST SCOPE OF THAT MOVE, since the obvious rationale would be wrong:
        # ``triage`` is NOT a behaviour-gating classifier today. It exists only
        # for the NE-168 routing-trace exhibit — ``demo_seams.demo_agent_
        # definitions()`` gives it fixed instructions ("Classify the priority of
        # the incident in one short sentence") and ``routers/demo.py`` a fixed
        # task string; the three delegation legs are INDEPENDENT, not chained,
        # and the leg's completion TEXT is discarded at every layer (C12: the
        # ``DelegateLegView`` response is identifiers/enums/bools only, the audit
        # events carry no text, and the arc step grades on ``succeeded``, i.e.
        # whether the call raised). So a wrong label changes nothing downstream.
        #
        # The move is therefore justified by CONSISTENCY and by what the exhibit
        # CLAIMS, not by output quality: this is the demo that shows a governed
        # multi-lane routing decision to a customer, and a lane whose real job is
        # to be seen should be a lane we would actually route work to. It also
        # removes the last reason for the CPU container to be on the critical
        # path at all. Both are real, and neither is "the 0.6B model would give
        # a wrong answer someone acts on" — nothing acts on it.
        "triage": {"model": "private-default"},
        "reason": {"model": "private-default"},
        "adjudicate": {"model": "frontier-best"},
        # NE-SKILL-1b: the Skill Forge author agent routes on this task
        # (private/open-weight lane — snapshot intents are customer text;
        # frontier is structurally refused). Required when forge_enabled=true.
        "skill-authoring": {"model": "private-default"},
        # NE-HGATE-4 (design §6.2-1): the G4 grounding-assist re-draft rides
        # the SAME private lane as the agent turn (GPU when --gpu-vllm, else
        # CPU) — closes the HGATE-3 boot fail-loud coupling (ne-api
        # platform.py:1181-1194) and makes assist_used=True reachable in
        # production. The 0.6B CPU fallback is gone: a re-draft that exists to
        # REPAIR an ungrounded answer is the last place to accept a WEAKER
        # model, since its output is what the grounding gate then judges.
        "grounding-assist": {"model": "private-default"},
        # NE-T2X design B-P1-4/§3.1-4: extraction runs over CUSTOMER TEXT, so
        # it rides the private/open-weight lane — frontier is structurally
        # refused (same reasoning as skill-authoring above). The overlay does
        # NOT set NE_BRAIN_GRAPH_EXTRACTION_ENABLED — only the ROUTE ships;
        # the feature itself stays default-off.
        "graph-extract": {"model": "private-default"},
    }
    if frontier is not None:
        # NE-BUNDLE-GATEWAY-CONFIG: APPEND, never replace (NE-168 lesson). NO
        # api_base — the egress guard derives the frontier host from `provider`
        # via KNOWN_FRONTIER_PROVIDER_HOSTS (below); NO api_key — litellm reads
        # ANTHROPIC_API_KEY/OPENAI_API_KEY from the container's own env (C12).
        # The declared window is what the NAMED model actually serves
        # (_FRONTIER_CONTEXT_WINDOW, keyed by the same provider key as the id).
        #
        # NE-CTX-IN-FORCE (owner-directed 2026-09-01) removed the `min(...,
        # private_default)` cap that used to sit here. That cap existed because
        # `refusal_for_selection` refused any model whose input budget was
        # SMALLER than the task primary's — the runtime trim guard having been
        # derived from the primary once at boot — so a 1M frontier primary made
        # the 32k `private-default` UNSELECTABLE for exactly the tasks that
        # declare it as their own fallback, and AIR_GAP then killed the frontier
        # lane AND its private fallback together.
        #
        # The trim guard is now re-derived per request from the model in force
        # (`GatewayModel._turn_budget`), the input-budget floor in
        # `model_routing` is gone with it, and the cap has nothing left to hold.
        # What it DID hold, measured on the sealed box before this change: this
        # lane declared 32768, and after the 12288 output reserve sized for a
        # 262144 GPU window it had 18841 input tokens — NARROWER than the private
        # lane's 27033. A real agentic run died `context_exhausted` at 18928
        # estimated tokens, 87 over, on a model serving 1M.
        frontier_window = _FRONTIER_CONTEXT_WINDOW[frontier]
        frontier_model: dict[str, Any] = {
            "name": "frontier-default",
            "litellm_model": _FRONTIER_LITELLM_MODEL[frontier],
            "provider": frontier,
            "lane": "frontier-api",
            "context_window": frontier_window,
            # Same ONE table as `frontier-best` above, keyed by the litellm model
            # string, so the swap target's own generation decides whether a
            # `thinking` disable rides along. LIVE for anthropic/claude-sonnet-5.
            **_frontier_extra_params(_FRONTIER_LITELLM_MODEL[frontier]),
        }
        # The frontier lane's output reserve. It used to be the MAXIMUM of two
        # independent requirements; NE-CTX-IN-FORCE retired the first, and the
        # value is UNCHANGED on both shapes because the second still binds.
        #
        # (1) RETIRED — NE-FRONTIER-LIVE's ORDERING requirement. It existed
        #     because `refusal_for_selection` compared input_budget = window -
        #     max_output_tokens - overhead_margin against the task primary's, and
        #     the (now removed) window cap made the GPU-branch windows EQUAL, so
        #     the smaller reserve won the larger budget and could make
        #     `private-default` unselectable for the very tasks declaring it as
        #     fallback. There is no input-budget floor in `model_routing` any
        #     more, and the windows are no longer equal, so nothing here rides on
        #     the reserve ordering.
        #
        # (2) STANDS — NE-FRONTIER-OUTPUT, TRUNCATION. On the sealed CPU branch
        #     `private-default` declares no reserve, so inheritance alone leaves
        #     ModelDef's min(4096, cw//8) = 4096 in force. The private key's own
        #     comment records that 4096 reproducibly returned finish_reason=length
        #     on grounded synthesis, which the runtime could only retry into
        #     `Exceeded maximum output retries`. _FRONTIER_MAX_OUTPUT_TOKENS is the
        #     measured floor (12204-token worst completion, 2026-08-21 rig runs),
        #     and the 300s turn deadline is why it is not larger.
        #
        # The max-with-the-private-reserve is KEPT rather than simplified to
        # `_FRONTIER_MAX_OUTPUT_TOKENS`: it leaves this row byte-identical on both
        # shapes (24576 on GPU, 12288 sealed) instead of silently shrinking a
        # reserve nobody asked to shrink, and on a 1_000_000-token window the
        # difference is noise. The clamp keeps ModelDef's `max_output_tokens <
        # context_window // 2` bound, which the uncapped window makes slack.
        private_reserve = int(private_default.get("max_output_tokens") or 0)
        frontier_reserve = max(_FRONTIER_MAX_OUTPUT_TOKENS, private_reserve)
        frontier_reserve = min(frontier_reserve, frontier_window // 2 - 1)
        frontier_model["max_output_tokens"] = frontier_reserve
        models_list.append(frontier_model)
        # Reroute ONLY `agent`, with NO fallback — see the docstring: a private
        # fallback would silently degrade the swap beat. Every other route
        # (including `adjudicate`, still the frontier-best deny exhibit)
        # is untouched.
        tasks_dict["agent"] = {"model": "frontier-default"}
        # The private lane stays as the analyst's ONE authorized fallback — it is
        # the air-gap beat ("now watch the same agent run on the private lane").
        # Keeping it SELECTABLE no longer depends on any window/reserve ordering:
        # `refusal_for_selection` has no budget floor left, and a roll onto this
        # narrower lane is trimmed to THIS lane's own window by the runtime guard.
        # The 0.6B CPU lane is no longer a third rung: a frontier outage
        # plus an unreachable GPU rig must now surface as a failure, not as a
        # 0.6B answer wearing the analyst's name.
        tasks_dict["agent.meridian-analyst.v2"] = {
            "model": "frontier-default",
            "fallbacks": ["private-default"],
        }
        # NE-DEMO-NO-CPU-FALLBACK, the UPWARD half (owner-directed 2026-08-27):
        # every PRIVATE-PRIMARY task takes ``frontier-default`` as its one
        # fallback, replacing the 0.6B CPU tier that was removed above. The
        # degrade now goes to a STRONGER model, so losing the GPU rig costs
        # egress and money instead of costing correctness.
        #
        # ONLY INSIDE THIS BRANCH, and that is a hard constraint rather than a
        # style choice: ``frontier-default`` is APPENDED to ``models_list`` only
        # when ``--frontier`` is passed, and ``GatewaySettings`` fail-loud
        # validation rejects any ``tasks.*.fallbacks[i]`` naming a model that is
        # not configured (ne-gateway/config.py). Declaring these fallbacks
        # unconditionally would make every SEALED (no ``--frontier``) bring-up
        # die at boot with a GatewayConfigError. On the sealed box these tasks
        # therefore keep NO fallback — which is correct there anyway: the whole
        # point of that posture is that no frontier lane exists to fall back to.
        #
        # ``agent`` and ``adjudicate`` are DELIBERATELY EXCLUDED — see below.
        for _task in (
            "agent.meridian-hardship-review.v3",
            "triage",
            "reason",
            "skill-authoring",
            "grounding-assist",
            "graph-extract",
        ):
            tasks_dict[_task] = {
                "model": "private-default",
                "fallbacks": ["frontier-default"],
            }
        # ``agent`` keeps NO fallback. This is the pre-existing, load-bearing
        # decision restated (see the docstring): ``agent`` is the SWAP BEAT's
        # task — the operator says "I am now running this on Sonnet 5" and the
        # console must be showing exactly that. A fallback here would let a
        # missing key or a blocked egress silently serve the private lane while
        # the narration claimed the frontier one, faking the beat. It is the same
        # anti-silent-downgrade rule as the rest of this unit, pointed the other
        # way, so it stays.
        #
        # ``adjudicate`` keeps NO fallback either, and adding one would be worse
        # than cosmetic: it is the GOVERNED-DENY EXHIBIT. It routes at
        # ``frontier-best``, which the NE-161 runtime gate REFUSES by design, and
        # the demo's claim is "governance stopped this call." Give it a fallback
        # and the refusal becomes a fall-through to a model that answers — the
        # exhibit would then demonstrate governance being ROUTED AROUND, which is
        # the opposite of the point.
    models = json.dumps(models_list)
    tasks = json.dumps(tasks_dict)
    # Single-quoted YAML scalars carry the JSON (which uses double quotes) verbatim.
    env_lines = f"      NE_GATEWAY_MODELS: '{models}'\n      NE_GATEWAY_TASKS: '{tasks}'\n"
    if gpu_vllm is not None:
        host = urlparse(base).hostname or ""
        env_lines += f"      NE_EGRESS_ALLOWED_PRIVATE_VLLM_HOSTS: '{json.dumps([host])}'\n"
    if frontier is not None:
        # Derived from the SAME mapping EgressPolicy._resolve_frontier_host uses
        # (ne.gateway.egress) — never a transliteration, and never a blanket
        # opening: exactly one host, per NE-BUNDLE-GATEWAY-CONFIG's C2 framing.
        from ne.gateway.egress import KNOWN_FRONTIER_PROVIDER_HOSTS

        frontier_host = KNOWN_FRONTIER_PROVIDER_HOSTS[frontier]
        env_lines += f"      NE_EGRESS_ALLOWED_FRONTIER_HOSTS: '{json.dumps([frontier_host])}'\n"
    return env_lines


def _tei_cache_bind_mount_lines(tei_cache_dir: Path | None) -> str:
    """The tei-embed service's ``volumes:`` block (empty string when no ONNX
    cache was staged this run — byte-identical to the pre-NE-TEI-ONNX overlay)."""
    if tei_cache_dir is None:
        return ""
    tei_cache_abs = tei_cache_dir.resolve()
    return f'    volumes:\n      - "{tei_cache_abs}:/data"\n'


def _vllm_cpu_overlay_lines(gpu_vllm: str | None) -> str:
    """The ``vllm-cpu`` overlay block — empty when an external GPU rig serves the
    private lane (NE-DEMO-NO-IDLE-VLLM).

    On the on-box CPU path this does two things the pinned compose cannot: it
    aliases the service as ``vllm`` on the shared default network (ne-api's
    gateway menu dials ``http://vllm:8000``, so the lane resolves without
    editing the pinned menu), and it raises the CPU KV-cache budget to 6 GiB
    (Qwen3-0.6B's 40960 max-seq-len needs ~4.38 GiB and the shipped 4 GiB budget
    aborts engine init; the pilot memory caps do not apply to the included serve
    compose).

    Under ``--gpu-vllm`` neither applies, because the service is not started:
    every private-vllm entry `_gateway_lane_env` emits points at the external
    rig, so nothing can call it. Emitting the block anyway would patch a service
    absent from the composed model, which Compose reads as a NEW service with no
    image — an invalid project, not a no-op.
    """
    if gpu_vllm is not None:
        return ""
    return (
        "  vllm-cpu:\n"
        "    networks:\n"
        "      default:\n"
        '        aliases: ["vllm"]\n'
        "    environment:\n"
        '      VLLM_CPU_KVCACHE_SPACE: "6"\n'
    )


def _write_demo_overlay(
    workdir: Path,
    seed_dir: Path,
    https_port: int = 443,
    external_url: str | None = None,
    gpu_vllm: str | None = None,
    gpu_rerank: str | None = None,
    tei_cache_dir: Path | None = None,
    live_writeback: _LiveWritebackStaging | None = None,
    graph_authority: _LiveWritebackStaging | None = None,
    graph_population: _GraphPopulationStaging | None = None,
    frontier: _FrontierStaging | None = None,
) -> Path:
    """Generate the runtime compose OVERLAY (the NE-084 pilot-override precedent).

    ne-api AND ne-worker receive the IDENTICAL demo-seam env map (§6.2/Q5 parity) plus a
    read-only ``/seed`` mount; opa's ``/policies`` mount is REPLACED (same target) with
    the staged first-party rego bundle — the shipped compose file is byte-untouched. NO
    secret material rides the overlay (C12). Written by hand (pyyaml is test-only) as a
    deterministic YAML string. Returns the overlay path.

    NE-DEMO-EXTERNAL-URL: when ``external_url`` is set, an external edge/reverse-proxy
    fronts the demo, so the caddy `ports: !override` block binds the published ports to
    ``127.0.0.1`` (``"127.0.0.1:{https_port}:443"`` / ``"127.0.0.1:8443:8443"``) — the
    edge terminates a real cert and forwards, and the demo never claims a public-interface
    (0.0.0.0) port. When ``external_url`` is None the ports stay bound to all interfaces,
    byte-identical to today.

    NE-TEI-ONNX: when ``tei_cache_dir`` is given, the tei-embed service gains a
    ``volumes: ["<abs tei_cache_dir>:/data"]`` bind — compose merges ``volumes:``
    entries by container TARGET, so this REPLACES the pinned compose's named
    ``tei-embed-cache:/data`` mount for ``/data`` only in the demo (the pinned
    compose file itself stays byte-untouched). ``tei_cache_dir`` is staged by
    :func:`_stage_tei_cache` BEFORE this overlay is written.

    NE-DEMO-WRITEBACK-LIVE: when ``live_writeback`` is given, Beat 4's tool swaps
    onto the real governed CRM write — ``NE_API_WRITE_ACTIONS_ENABLED: "true"`` is
    APPENDED (a local extended tuple; :data:`_DEMO_OVERLAY_ENV` itself is
    untouched). NE-ITEM1-INERT-PARK: the ``NE_API_DEMO_WRITEBACK_TOOL`` pair is
    KEPT rather than dropped — the swap is enforced by the agent's Tool grants,
    not by the inert binding's absence, and dropping it made the platform's own
    approval-lifecycle self-test unreachable on this deployment shape.
    The app services gain an ``env_file:`` reference to the staged
    0600 connector env (never inlined — C12) plus one read-only volume line per
    staged PEM mount. When ``live_writeback`` is ``None`` the emitted bytes are
    BYTE-IDENTICAL to the pre-unit overlay (the default-mode parity requirement).
    This same already-admitted boundary also emits the canonical default-Ask
    generation into the shared API/worker environment; without live writeback the
    key remains absent and both processes plus the database fence stay dormant.
    The same boundary pins the deterministic recipient guardrail to exactly
    ``owner@example.com`` (the owner's demo inbox — also mer-cus-001's fixture
    email and the entire send allowlist) in both roots; no recipient domain is
    admitted and the default/non-live overlay remains deny-by-default.

    NE-BUNDLE-GATEWAY-CONFIG: when ``frontier`` is given, ``_gateway_lane_env`` is
    called with ``frontier=frontier.provider`` (APPENDS a ``frontier-default``
    model + reroutes ONLY ``agent`` — see that function's docstring) and the app
    services gain an ``env_file:`` reference to the staged 0600 ``frontier.env`` —
    GENERALIZED alongside ``live_writeback``'s: ``env_file:`` stays a SINGLE key
    carrying a list, with one entry per staged file (so both can be active at
    once). The app services ALSO gain ``NE_GOVERN_REDACTOR: "regex"`` — the
    shipped ``ne-app`` image has no Presidio sidecar venv, so the frontier lane's
    mandatory pre-egress redaction would otherwise fail every in-container
    ``agent`` turn closed (see the inline comment above the emission for the full
    chain). When ``frontier`` is ``None`` this contributes nothing (byte-identical
    default).
    """
    if graph_population is not None and live_writeback is None and graph_authority is None:
        raise click.ClickException("Graph population requires admitted M365 authority")
    overlay_dir = workdir / "overlay"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    seed_abs = seed_dir.resolve()
    demo_env = _DEMO_OVERLAY_ENV
    if live_writeback is not None or graph_authority is not None:
        # NE-ITEM1-INERT-PARK. The inert ``write.external.demo.send`` binding is
        # KEPT in live-writeback / graph-authority mode. It used to be filtered
        # out here so Beat 4 could not possibly show a stand-in where the real
        # governed write belongs — but ABSENCE FROM THE EXECUTOR WAS NEVER WHAT
        # ENFORCED THAT. Which tool a beat runs is decided by the AGENT's
        # ``tool_grants`` in ``deploy/agents/registry.toml``, and Meridian
        # Customer Support grants ``write.internal.crm.note`` and
        # ``write.external.email.send`` and has never granted the inert one. The
        # filter bought no integrity; it only made the inert action unreachable
        # on exactly the deployment shape this box runs.
        #
        # What that cost: POST_DEMO item 1 ("parked runs can never terminalize")
        # is fixed but UNPROVEN on the deployment, because the only other way to
        # park a canonical Run here is a REAL Microsoft Graph send. Keeping this
        # pair registered lets the Governance Self-Test agent
        # (``agt_01k3inertparkselftest0000000``, granted the inert action and
        # nothing else that writes) park, be decided, and terminalize with
        # structurally zero outward effect — the handler
        # (``ne.api.demo_seams._demo_writeback_handler``) returns a constant
        # string and performs no I/O at all.
        #
        # ⚠ WHAT IT DOES COST, STATED: chat is NOT toolset-scoped. A chat turn
        # runs over the process-wide governed executor and is offered the WHOLE
        # registered set (``ne.runtime.agent.DepartmentAgentSession``:
        # ``[self._build_tool(spec, principal) for spec in
        # self._executor.tool_specs]``), so in live mode the inert action now
        # sits on the chat tool menu beside the real ones. The Run path is
        # unaffected — ``project_agent_capabilities`` intersects the registered
        # specs with the Agent's ``tool_grants``. Accepted as a bounded risk
        # (Tier-2 park under the action's own name, triple self-labelling, and
        # nothing in the live arc drives it — ``_writeback_action`` below
        # returns the real ``write.internal.crm.note`` off the live marker);
        # the full reasoning is recorded on the Governance Self-Test agent in
        # ``deploy/agents/registry.toml``. This is a real change to the
        # deployed environment, so "zero production runtime code changed" is
        # NOT an accurate description of this unit.
        demo_env = (
            *_DEMO_OVERLAY_ENV,
            *(
                (("NE_API_WRITE_ACTIONS_ENABLED", "true"),)
                if live_writeback is not None
                else (
                    (
                        "NE_API_WRITE_ACTION_TOOLS",
                        json.dumps(
                            list(
                                graph_authority.selected_write_tools
                                if graph_authority is not None
                                and graph_authority.selected_write_tools
                                else [_M365_DRAFT_TOOL]
                            )
                        ),
                    ),
                )
            ),
        )
    # NE-GA-G6: a value carrying a double quote (e.g. NE_API_CHAT_AGENTS's JSON
    # array) breaks a double-quoted YAML scalar — single-quote it instead (the
    # ``_gateway_lane_env`` precedent below). Every OTHER value (true/false/a
    # plain path) carries no ``"`` and stays double-quoted, byte-identical.
    env_block = "".join(
        (f"      {key}: '{value}'\n" if '"' in value else f'      {key}: "{value}"\n')
        for key, value in demo_env
    )
    # PRE-010G: canonical Ask becomes active only at the existing full producer
    # boundary. Live-writeback staging has already validated and mounted the
    # Meridian M365 authority inputs, while this SAME shared app block supplies
    # the model/task routes to both ne-api and ne-worker. Base/demo-fixture-only
    # overlays omit the key, so neither root installs the database-visible fence.
    if live_writeback is not None or graph_authority is not None:
        env_block += '      NE_API_DEFAULT_ASK_GENERATION: "default-ask/canonical-run/v1"\n'
        # Authority activation is non-secret deployment configuration. Emit it
        # once into the shared block so API and worker receive byte-identical
        # selection and bounds; writeback.env is deliberately unable to override
        # any of these names.
        authority_env = (
            _LIVE_WRITEBACK_AUTHORITY_ENV
            if live_writeback is not None
            else _M365_GRAPH_AUTHORITY_ENV
        )
        env_block += "".join(f'      {key}: "{value}"\n' for key, value in authority_env)
    if live_writeback is not None or graph_authority is not None:
        env_block += "".join(
            f"      {key}: '{value}'\n" for key, value in _LIVE_WRITEBACK_RECIPIENT_ENV
        )
        # NE-CS-SEND: the send-action recipient opt-in, emitted in BOTH live
        # modes (graph-only and full live-writeback — the latter selects the
        # send via NE_API_WRITE_ACTIONS_ENABLED). Without this key the platform
        # refuses to register the send tool at all (structural deny-by-default).
        send_allowlist = json.dumps([_DEMO_SEND_RECIPIENT], separators=(",", ":"))
        env_block += f"      NE_API_EMAIL_SEND_ALLOWED_RECIPIENTS: '{send_allowlist}'\n"
    if graph_authority is not None:
        if graph_authority.draft_mailbox is None:
            raise click.ClickException("Graph authority is missing its admitted draft mailbox")
        mailbox_allowlist = json.dumps(
            [graph_authority.draft_mailbox],
            separators=(",", ":"),
        )
        env_block += f"      NE_API_EMAIL_DRAFT_ALLOWED_MAILBOXES: '{mailbox_allowlist}'\n"
    elif live_writeback is not None and live_writeback.draft_mailbox is not None:
        # NE-CS-USABLE-ADDRESS: the full live-writeback surface's admitted
        # draft mailbox (validated in _stage_live_writeback). Same key, same
        # exact-one-mailbox shape as graph-only mode.
        mailbox_allowlist = json.dumps(
            [live_writeback.draft_mailbox],
            separators=(",", ":"),
        )
        env_block += f"      NE_API_EMAIL_DRAFT_ALLOWED_MAILBOXES: '{mailbox_allowlist}'\n"
    if graph_population is not None:
        # Both roots receive the same closed mapping. Explicit values dominate
        # hostile ambient NE_BRAIN_GRAPH_* exports; the base Pilot compose owns
        # the persistent AGE DSN/name and this overlay does not replace them.
        sources_yaml = graph_population.sources_json.replace("'", "''")
        env_block += '      NE_BRAIN_GRAPH_ENABLED: "true"\n'
        env_block += f"      NE_BRAIN_GRAPH_SOURCES: '{sources_yaml}'\n"
        # The Graph-only/live-writeback lane is the one deployment mode with a
        # governed, persistent T2 producer. Lift that admitted AGE projection into
        # the mutable T3 serving gateway so protected Run reads can bind every row
        # (and authoritative empty result) to the lift's current-authority index.
        # The stock demo still omits the Graph-to-T3 lift entirely.
        env_block += '      NE_API_DEMO_ONTOLOGY_LIFT: "true"\n'
        drive_ids = json.dumps([graph_population.drive_id], separators=(",", ":"))
        env_block += f"      NE_M365_DRIVE_IDS: '{drive_ids}'\n"
        env_block += "      NE_M365_SITE_IDS: '[]'\n"
    # NE-DEMO-CHAT-ROUTE / NE-168: the chat agent routes every completion with
    # task="agent" (ne/runtime/config.py), but the pilot compose configures gateway
    # MODELS without TASKS — so the gateway raises UnknownTaskError and EVERY chat
    # turn (T3-chat leg, HITL) fails at ROUTING, on CPU and GPU alike.
    # ``_gateway_lane_env`` ALWAYS emits BOTH NE_GATEWAY_MODELS (private-default +
    # frontier-best — the private lane + the delegation
    # demo's governed frontier-DENY exhibit) and NE_GATEWAY_TASKS (agent/triage/
    # reason/adjudicate), so env parity between ne-api/ne-worker is automatic via
    # the shared app_block below (§6.2/Q5) whether or not ``--gpu-vllm`` is set.
    frontier_provider = frontier.provider if frontier is not None else None
    env_block += _gateway_lane_env(gpu_vllm, frontier=frontier_provider)
    # The NE_DEMO_UNAPPROVED_ROUTE_REPOINT passthrough (NE-AIRGAP-NO-APPROVAL,
    # owner-authorized for the 2026-08-28 demo ONLY) used to ride here. RETIRED
    # after the demo (POST_DEMO.md item 20): ne-api no longer reads the variable,
    # so the overlay never emits it — exporting it in a deploy shell is inert.
    # NE-SEARCH-LATENCY: when ``gpu_rerank`` is set, the /v1/brain/search rerank
    # stage rides an EXTERNAL GPU vLLM /rerank server (the SAME
    # BAAI/bge-reranker-v2-m3 — a server swap, never a model swap; attribution
    # measured the on-box CPU TEI stage at ~40s/search vs ~0.12s on GPU). The host
    # is emitted into NE_API_RERANK_ALLOWED_HOSTS — the C2 allowlist
    # ``_default_reranker`` threads into the adapter (mirrors
    # NE_EGRESS_ALLOWED_PRIVATE_VLLM_HOSTS' framing: a DELIBERATE demo-only
    # relaxation, cleartext + unauthenticated, synthetic corpus only). AIR_GAP
    # dominates that grant COMPLETELY, at construction AND per request: under the
    # gap the allowlist contributes nothing and only provably in-VPC shapes pass,
    # so an external rig fails CLOSED whether the operator's URL spells the host
    # as an IP literal or a DNS name (``vast-rig.sh`` derives its host as
    # ``public_ipaddr or ssh_host``, so both shapes occur in practice — they now
    # behave identically). Revert to the CPU lane before the air-gap beat. When
    # ``gpu_rerank`` is None this contributes nothing (byte-identical default:
    # sealed on-box CPU tei-rerank).
    if gpu_rerank is not None:
        from urllib.parse import urlparse as _urlparse

        # LAZY (the ne.brain_graph / ne.api precedent in this module): ne-net is the
        # stdlib-only leaf, so `ne --help` startup is unaffected.
        from ne.net.ssrf import UnsafeEndpointError, normalize_host, validate_in_vpc_endpoint

        rerank_base = gpu_rerank.rstrip("/")
        rerank_host = normalize_host(_urlparse(rerank_base).hostname or "")
        # VALIDATE BEFORE WRITING. ne-api runs this same validator at boot, but by
        # then the value is already on disk in the operator's workdir. Two things
        # go wrong if it is only checked there:
        #   * C12 — this function promises "NO secret material rides the overlay",
        #     and a URL carrying userinfo (`http://user:pw@rig/`) would be written
        #     verbatim and persist even though boot rejects it;
        #   * the raw value is interpolated into a YAML scalar, so a `"` plus a
        #     newline injects ARBITRARY env keys into the ne-api service block
        #     (verified: a crafted --gpu-rerank added a key of its own).
        # `validate_in_vpc_endpoint` rejects embedded credentials, query/fragment,
        # a malformed port, and the metadata range; the host is allowlisted here
        # exactly as the emitted env will allowlist it, so this accepts precisely
        # what ne-api will accept. `air_gap` is deliberately NOT passed: this is a
        # host-side authoring check, and the boot-time check is where an engaged
        # gap must speak.
        try:
            validate_in_vpc_endpoint(rerank_base, allowed_hosts=(rerank_host,))
        except UnsafeEndpointError as exc:  # names the HOST only, never the URL
            raise click.ClickException(
                f"--gpu-rerank endpoint rejected before writing the overlay: {exc}"
            ) from None
        # The URL scalar is emitted with json.dumps, NOT an f-string in quotes.
        # Validation is not an output encoder: the check above rejects a hostile
        # AUTHORITY (the port fails to parse) but a URL may legitimately carry a
        # PATH, and a path containing `"` + newline closes the double-quoted YAML
        # scalar and injects arbitrary keys into this service block (verified).
        # YAML 1.2 is a superset of JSON, so a json.dumps string is a valid YAML
        # flow scalar with `"`, `\` and newlines escaped — and for an ordinary URL
        # it is byte-identical to the previous output.
        # The allowlist value is a JSON array, so it CONTAINS `"` and must ride a
        # single-quoted YAML scalar. A YAML single-quoted scalar escapes `'` by
        # DOUBLING it, and json.dumps does not touch apostrophes — so a hostname
        # carrying one (urlparse returns it in .hostname, and the validator does
        # not check DNS-label syntax) produced a syntactically invalid overlay
        # that failed at `ne demo up` AFTER being persisted. Same defect class as
        # the URL scalar above: an accepted input must not be able to break the
        # document it is written into.
        allowed_hosts_yaml = json.dumps([rerank_host]).replace("'", "''")
        env_block += (
            '      NE_API_RERANK_BACKEND: "vllm"\n'
            f"      NE_API_TEI_RERANK_URL: {json.dumps(rerank_base)}\n"
            f"      NE_API_RERANK_ALLOWED_HOSTS: '{allowed_hosts_yaml}'\n"
        )
    # NE-CONNECT-CSV: the polled-connector lane. The csv source (the fixture
    # renewals sheet under the /connect bind mount, staged by _stage_connect_dir)
    # is ALWAYS configured; salesforce/m365 join the sources list only when
    # --live-writeback staged (and therefore VALIDATED) their connector env.
    # JSON-array values carry double quotes -> single-quoted (the NE-GA-G6 rule).
    env_block += "".join(
        (f"      {key}: '{value}'\n" if '"' in value else f'      {key}: "{value}"\n')
        for key, value in _connect_lane_env(
            live_writeback=live_writeback is not None, graph=graph_authority is not None
        )
    )
    # NE-BUNDLE-GATEWAY-CONFIG (Argus round-2 P1): the frontier lane's pre-egress
    # redaction (NE-162, gateway._prepare_egress_body) is MANDATORY — a missing or
    # broken redactor fails the turn CLOSED. The default backend (Presidio) runs in
    # an isolated sidecar venv (packages/ne-govern/presidio_env) that the shipped
    # ne-app runtime image does NOT carry (its final stage is a slim `uv sync
    # --frozen --no-dev --no-editable` venv copy with no repo checkout, no `uv`
    # binary, and no `presidio_env/` — it lives outside `src/`, so it is never part
    # of the synced dist; see deploy/docker/ne-app/Dockerfile), so under Presidio the
    # first `agent` turn after `--frontier` would fail closed inside the container.
    # RegexRedactor (`ne.govern.redaction.regex_backend`, the "regex"
    # `ne.plugins.redactor` entry point) is stdlib-only — no sidecar venv, no extra
    # egress — and IS the repo's own documented air-gap SUPPLEMENT, so selecting it
    # ONLY while `--frontier` is staged turns the in-container frontier chat turn
    # from "fails closed" into "completes, with a WEAKER (non-NER, regex-only) PII
    # backend" — a deliberate trade disclosed in docs/demo/README.md's frontier
    # section, and itself stamped into `config_hash` via the ne-govern module (the
    # swap is still demo-able evidence, now covering the redactor choice too). The
    # PRIVATE lane never calls the redactor at all (`_prepare_egress_body` fires
    # ONLY for a FRONTIER_API candidate), so the default (no `--frontier`) demo path
    # is completely unaffected and keeps Presidio. When `frontier` is None this
    # contributes nothing (byte-identical default).
    if frontier is not None:
        env_block += '      NE_GOVERN_REDACTOR: "regex"\n'
        # NE-FRONTIER-LIVE: a staged frontier lane is USELESS without this.
        # ModelSelectionService only calls a frontier model available on a
        # successful `ne.gateway.model.observed` record less than 60s old, and
        # nothing produces the first one — so before this flag `--frontier`
        # appended a model that could never be authorized: absent from the Ask
        # picker, and on `agent.meridian-analyst.v2` (which declares private
        # fallbacks) SILENTLY demoted to the open-weight lane, i.e. the swap
        # beat's exact failure mode. On, an AUTHORIZATION path issues ONE
        # governed completion through the SAME gateway (allowlisted host,
        # pre-egress redaction, audited) to earn that evidence. It is emitted
        # ONLY alongside a staged `--frontier`, so the default demo path never
        # gains a warm call, and under AIR_GAP the warm is unreachable (frontier
        # candidates are dropped before it) — the flag cannot loosen the seal.
        env_block += '      NE_API_FRONTIER_WARM_ENABLED: "true"\n'
    # NE-DEMO-WRITEBACK-LIVE: one extra :ro volume line per staged PEM mount, and
    # an env_file reference to the 0600 connector env — both empty strings (byte-
    # identical default) when live_writeback is None.
    connector_staging = live_writeback or graph_authority
    pem_volume_lines = (
        "".join(
            f'      - "{host}:{container}:ro"\n' for host, container in connector_staging.pem_mounts
        )
        if connector_staging is not None
        else ""
    )
    # NE-BUNDLE-GATEWAY-CONFIG: `env_file:` is a SINGLE key carrying a list — one
    # entry per staged 0600 env file (writeback.env / frontier.env), so both can
    # be referenced at once. Empty when neither is staged (byte-identical default).
    env_files = [f.env_file for f in (connector_staging, frontier) if f is not None]
    env_file_lines = (
        "    env_file:\n" + "".join(f'      - "{f}"\n' for f in env_files) if env_files else ""
    )
    # The demo's startup ontology-lift (660-node fixture → embedded oxigraph) is
    # CPU-bound and pushes ne-api/ne-worker health past the shipped 360s budget on a
    # CPU-only box, so compose --wait aborts the bring-up even though they go healthy
    # ~1-2 min later. Extend start_period (test/interval/retries inherited from base).
    # NE-CONNECT-CSV: /connect is rw — the WORKER writes the connector state +
    # quarantine sqlite there (the read-only rootfs has nowhere else), and the
    # demo operator edits the staged sheet on the host between polls.
    connect_abs = (workdir / "connect").resolve()
    app_block = (
        f"    environment:\n{env_block}"
        f'    volumes:\n      - "{seed_abs}:/seed:ro"\n'
        f'      - "{connect_abs}:/connect"\n'
        f"{pem_volume_lines}"
        f"{env_file_lines}"
        "    healthcheck:\n"
        "      start_period: 600s\n"
    )
    content = (
        "# NE-DEMO-LIVE-SEAMS runtime overlay (the NE-084 pilot-override precedent).\n"
        "# Layered with `-f` over deploy/compose/pilot/compose.yaml — that file is\n"
        "# NEVER edited (the byte-match guardian). ne-api and ne-worker get the\n"
        "# IDENTICAL demo-seam env map (design §6.2/Q5 cross-process hash parity); opa's\n"
        "# /policies mount is replaced with the staged first-party ne.govern rego bundle.\n"
        "# NO secrets ride this overlay (C12).\n"
        "services:\n"
        "  ne-api:\n"
        f"{app_block}"
        "  ne-worker:\n"
        f"{app_block}"
        "  opa:\n"
        "    volumes:\n"
        f'      - "{seed_abs / "opa-policies"}:/policies:ro"\n'
        # TEI defaults to --max-batch-tokens 16384; warming that batch budget peaks
        # at ~8 GiB and cgroup-OOM-kills (137) against the pinned 4 GiB limit, so the
        # embed/rerank servers restart-loop and the stack never comes up. The demo
        # corpus is tiny — cap the batch budget so warm-up fits the limit with room.
        # (TEI reads MAX_BATCH_TOKENS from the env; the pinned compose is untouched.)
        "  tei-embed:\n"
        "    environment:\n"
        '      MAX_BATCH_TOKENS: "2048"\n'
        f"{_tei_cache_bind_mount_lines(tei_cache_dir)}"
        "  tei-rerank:\n"
        "    environment:\n"
        '      MAX_BATCH_TOKENS: "2048"\n'
        # NE-DEMO-NO-IDLE-VLLM: under --gpu-vllm, `_bring_up_stack` does not start
        # the `cpu` serve profile at all, so `vllm-cpu` is not a service in the
        # composed model — patching it here would DEFINE an imageless service and
        # fail the bring-up. Emit the block only on the on-box CPU path.
        f"{_vllm_cpu_overlay_lines(gpu_vllm)}"
    )
    # NE-DEMO-HTTPS-PORT / NE-DEMO-EXTERNAL-URL: append a caddy `ports: !override`
    # block (REPLACING the pinned compose's published list — a plain merge would
    # APPEND, leaving host 443 bound) whenever a non-default publish port is requested
    # OR an external edge fronts the demo (--external-url binds the ports to 127.0.0.1).
    # When https_port is the default 443 AND no external URL is set, NOTHING is
    # appended, so the overlay bytes stay byte-identical to the pre-change golden
    # (SPEC-row item 6 parity by construction).
    if https_port != 443 or external_url is not None:
        # NE-DEMO-EXTERNAL-URL: bind to loopback so an external reverse proxy fronts
        # the demo without a public-interface (0.0.0.0) port conflict. Empty prefix
        # keeps the non-external path byte-identical (host-all-interfaces publish).
        bind = "127.0.0.1:" if external_url is not None else ""
        loopback_note = (
            "    # NE-DEMO-EXTERNAL-URL: bind the published ports to 127.0.0.1 so an\n"
            "    # external edge/reverse-proxy (serving a real cert at the external URL)\n"
            "    # fronts the demo without a public-interface (0.0.0.0) port conflict.\n"
            if external_url is not None
            else ""
        )
        content += (
            "  caddy:\n"
            f"    # NE-DEMO-HTTPS-PORT: republish the API host port "
            f"({https_port} -> in-container 443).\n"
            "    # `!override` REPLACES the pinned compose's published list (a plain merge would\n"
            "    # APPEND and leave host 443 bound). Requires docker compose >= 2.24.4.\n"
            f"{loopback_note}"
            "    ports: !override\n"
            f'      - "{bind}{https_port}:443"\n'
            f'      - "{bind}8443:8443"\n'
        )
    overlay_path = overlay_dir / "demo-overlay.yaml"
    overlay_path.write_text(content, encoding="utf-8")
    return overlay_path


def _load_pilot_realm_presentation(repo_root: Path) -> dict[str, str] | None:
    """Load the optional login presentation from the exact Pilot realm artifact.

    Keycloak startup import intentionally skips an existing realm.  These two
    presentation fields are the narrow exception reconciled by ``ne demo up``;
    every other realm object keeps the established import/manual-migration
    lifecycle.  Both fields must be present together so a partial branding edit
    cannot silently produce an incoherent login page.
    """
    realm_path = repo_root / _PILOT_KEYCLOAK_REALM_PATH
    try:
        payload = json.loads(realm_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(
            "pilot Keycloak realm presentation could not be loaded from the checked-in artifact"
        ) from exc
    if not isinstance(payload, dict) or payload.get("realm") != _PILOT_KEYCLOAK_REALM:
        raise click.ClickException(
            "pilot Keycloak realm artifact does not identify realm 'ne-pilot' exactly"
        )

    present = {name for name in _KEYCLOAK_REALM_PRESENTATION_FIELDS if name in payload}
    if not present:
        return None
    if present != set(_KEYCLOAK_REALM_PRESENTATION_FIELDS):
        raise click.ClickException(
            "pilot Keycloak realm presentation must define displayName and displayNameHtml together"
        )

    presentation: dict[str, str] = {"realm": _PILOT_KEYCLOAK_REALM}
    for name in _KEYCLOAK_REALM_PRESENTATION_FIELDS:
        value = payload[name]
        if not isinstance(value, str) or not value.strip():
            raise click.ClickException(
                f"pilot Keycloak realm presentation field {name} must be a non-empty string"
            )
        presentation[name] = value
    return presentation


def _keycloak_realm_representation(response: Any, *, realm: str, label: str) -> dict[str, Any]:
    """Validate one Keycloak RealmRepresentation without exposing its contents."""
    response.raise_for_status()
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise click.ClickException(f"Keycloak {label} response is invalid") from exc
    if not isinstance(payload, dict) or payload.get("realm") != realm:
        raise click.ClickException(f"Keycloak {label} response does not identify realm {realm!r}")
    return payload


def _reconcile_keycloak_realm_presentation(
    client: Any,
    *,
    base: str,
    headers: Mapping[str, str],
    desired: Mapping[str, str] | None,
) -> None:
    """Idempotently reconcile only the existing realm's login presentation.

    This is deliberately an Admin REST GET/full-representation PUT/verify flow,
    never a startup import, database reset, or volume replacement.  A realm JSON
    without presentation fields is a zero-request no-op for backwards-compatible
    standalone deployment of this runtime change.
    """
    if desired is None:
        return
    realm = desired["realm"]
    realm_url = f"{base}/admin/realms/{realm}"
    current = _keycloak_realm_representation(
        client.get(realm_url, headers=headers), realm=realm, label="realm read"
    )
    if all(current.get(name) == desired[name] for name in _KEYCLOAK_REALM_PRESENTATION_FIELDS):
        return

    updated = dict(current)
    updated.update({name: desired[name] for name in _KEYCLOAK_REALM_PRESENTATION_FIELDS})
    client.put(realm_url, headers=headers, json=updated).raise_for_status()

    verified = _keycloak_realm_representation(
        client.get(realm_url, headers=headers), realm=realm, label="realm verification"
    )
    if any(verified.get(name) != desired[name] for name in _KEYCLOAK_REALM_PRESENTATION_FIELDS):
        raise click.ClickException(
            "Keycloak realm presentation reconciliation did not converge exactly"
        )


def _provision_keycloak(
    bundle: Path,
    hostname: str,
    scenario: Any,
    https_port: int = 443,
    external_url: str | None = None,
) -> Path:
    """Provision throwaway demo principals in Keycloak; write demo-credentials.json.

    Demo-only surface (verify=False against Caddy's local CA) — kept confined to
    this function with explicit comments so it is not read as a platform pattern.
    Returns the credentials file path (0600). LIVE-PARKABLE: idempotent group +
    user ensure over the admin REST API using the seeded admin password.

    NE-DEMO-HTTPS-PORT: the Keycloak admin ``base`` ALWAYS stays on :8443 (the OIDC
    issuer port never changes). When a non-443 console publish port is requested,
    the ne-console realm client's redirect-URI + web-origin allowlist is extended
    over the admin API to include ``https://{hostname}:{https_port}`` — the
    committed realm JSON only allowlists the :443 origin, so a non-443 console would
    break login (hazard c). The default 443 path issues NO client GET/PUT.

    NE-DEMO-EXTERNAL-URL: when ``external_url`` is set (an external edge fronts the
    demo at a clean URL, e.g. ``https://demo.example.host``), the ne-console client's
    redirect-URI + web-origin use ``external_url`` (no port) INSTEAD of the on-box
    ``_console_base(hostname, https_port)`` origin — the browser hits the external
    URL, so that is what OIDC must allowlist. The OIDC ISSUER identity stays on
    ``https://{hostname}:8443`` (the edge proxies :8443 with a real cert), but every
    on-box ADMIN call now dials ``https://127.0.0.1:8443`` with a ``Host: {hostname}:8443``
    header (:func:`_onbox_dial`) — dialing the public hostname from the box hairpin-NATs
    through the edge and fails (a box can't reach its own public IP). verify=False
    tolerates the loopback SNI/cert mismatch.
    """
    import httpx

    from ne.cli.checks import _find_repo_root
    from ne.cli.pilotcfg import resolve_required_secrets

    desired_realm_presentation = _load_pilot_realm_presentation(_find_repo_root(Path.cwd()))
    resolved = resolve_required_secrets(bundle)
    admin_password = resolved["NE_PILOT_KEYCLOAK_ADMIN_PASSWORD"]
    # NE-DEMO-EXTERNAL-URL: dial loopback (not the hairpinning public hostname) and
    # carry the public Host when an edge fronts the demo; default path is unchanged
    # (base == https://{hostname}:8443, kc_headers == {} → byte-identical requests).
    base, kc_headers = _onbox_dial(hostname, 8443, external_url)
    # verify=False: the pilot Caddy issues a LOCAL-CA cert; demo-only, never a
    # platform pattern (production clients pin the CA). The transport carries the
    # public hostname as TLS SNI on the loopback dial (see _onbox_transport — the
    # named-vhost Caddy rejects a no-SNI handshake outright); None on the default
    # path keeps the stock transport, byte-identical behaviour.
    creds: dict[str, dict[str, str]] = {}
    with httpx.Client(
        verify=False, timeout=30.0, transport=_onbox_transport(hostname, external_url)
    ) as client:
        token = client.post(
            f"{base}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "admin",
                "password": admin_password,
            },
            headers=kc_headers,
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}", **kc_headers}
        _reconcile_keycloak_realm_presentation(
            client,
            base=base,
            headers=headers,
            desired=desired_realm_presentation,
        )
        for group in (
            "all-employees",
            "dept-engineering",
            "executive",
            "department:servicing",
            *_MERIDIAN_OPS_GROUPS,
            *_MERIDIAN_APPROVER_GROUPS,
            *_MERIDIAN_RESTRICTED_GROUPS,
        ):
            response = client.post(
                f"{base}/admin/realms/ne-pilot/groups",
                headers=headers,
                json={"name": group},
            )
            if response.status_code not in (201, 409):
                response.raise_for_status()
        for person, groups in (
            (scenario.reader_id, list(scenario.reader_groups)),
            (scenario.outsider_id, list(scenario.outsider_groups)),
        ):
            password = f"{person}-demo"
            # Provision FULLY set up. The realm enables the VERIFY_PROFILE required
            # action, re-evaluated at auth time against profile completeness — so a
            # username-only user (missing firstName/lastName/email) is flagged
            # incomplete and a direct-access password grant fails 400 invalid_grant
            # "Account is not fully set up" (the demo arc's login), no matter that
            # requiredActions is cleared. Give each throwaway principal a complete
            # demo profile + verified email.
            user_repr = {
                "username": person,
                "enabled": True,
                "firstName": person,
                "lastName": "Demo",
                "email": f"{person}@acme-demo.local",
                "emailVerified": True,
                "requiredActions": [],
                "groups": groups,
                "credentials": [{"type": "password", "value": password, "temporary": False}],
            }
            resp = client.post(
                f"{base}/admin/realms/ne-pilot/users", headers=headers, json=user_repr
            )
            if resp.status_code == 409:
                # The user already exists — the keycloak realm lives in the pg-data
                # volume that `ne demo down` KEEPS, so a re-up re-provisions over a
                # persisted realm. POST-create is a no-op on 409 and does NOT update
                # the password, so the freshly-minted password would never reach
                # keycloak while demo-credentials.json records it → demo_run's grant
                # 400s. Reconcile: reset the existing user's profile + password so
                # keycloak matches demo-credentials.json. Idempotent re-up.
                existing = client.get(
                    f"{base}/admin/realms/ne-pilot/users",
                    params={"username": person, "exact": "true"},
                    headers=headers,
                ).json()
                if not existing:
                    resp.raise_for_status()
                uid = existing[0]["id"]
                # Fail LOUD if either reconciliation update fails: an unchecked PUT
                # (httpx does NOT raise on 4xx/5xx) would record the freshly-minted
                # password in demo-credentials.json while keycloak kept the OLD one,
                # so demo_run's password grant later 401s with a mystifying error far
                # from the cause. A failed reset here surfaces the real problem now.
                client.put(
                    f"{base}/admin/realms/ne-pilot/users/{uid}",
                    headers=headers,
                    json={k: v for k, v in user_repr.items() if k != "credentials"},
                ).raise_for_status()
                client.put(
                    f"{base}/admin/realms/ne-pilot/users/{uid}/reset-password",
                    headers=headers,
                    json={"type": "password", "value": password, "temporary": False},
                ).raise_for_status()
            creds[person] = {"username": person, "password": password}

        def exact_group_ids(names: tuple[str, ...]) -> dict[str, str]:
            ids: dict[str, str] = {}
            for name in names:
                group_response = client.get(
                    f"{base}/admin/realms/ne-pilot/groups",
                    params={"search": name, "exact": "true"},
                    headers=headers,
                )
                group_response.raise_for_status()
                exact_groups = [row for row in group_response.json() if row.get("name") == name]
                if len(exact_groups) != 1:
                    raise click.ClickException(
                        f"Meridian group {name!r} was not provisioned uniquely"
                    )
                ids[name] = exact_groups[0]["id"]
            return ids

        def exact_user_id(username: str) -> str:
            response = client.get(
                f"{base}/admin/realms/ne-pilot/users",
                params={"username": username, "exact": "true"},
                headers=headers,
            )
            response.raise_for_status()
            users = [row for row in response.json() if row.get("username") == username]
            if len(users) != 1:
                raise click.ClickException(
                    f"Meridian principal {username!r} not found uniquely in realm ne-pilot"
                )
            return users[0]["id"]

        def reconcile_exact_groups(user_id: str, names: tuple[str, ...]) -> None:
            group_ids = exact_group_ids(names)
            response = client.get(
                f"{base}/admin/realms/ne-pilot/users/{user_id}/groups", headers=headers
            )
            response.raise_for_status()
            current = {row["id"]: row.get("name") for row in response.json()}
            wanted_ids = set(group_ids.values())
            for group_id in current.keys() - wanted_ids:
                client.delete(
                    f"{base}/admin/realms/ne-pilot/users/{user_id}/groups/{group_id}",
                    headers=headers,
                ).raise_for_status()
            for group_id in wanted_ids - current.keys():
                client.put(
                    f"{base}/admin/realms/ne-pilot/users/{user_id}/groups/{group_id}",
                    headers=headers,
                ).raise_for_status()
            verified = client.get(
                f"{base}/admin/realms/ne-pilot/users/{user_id}/groups", headers=headers
            )
            verified.raise_for_status()
            verified_names = {row.get("name") for row in verified.json()}
            if verified_names != set(names):
                raise click.ClickException(
                    f"Meridian principal group reconciliation failed: expected {sorted(names)!r}, "
                    f"got {sorted(str(name) for name in verified_names)!r}"
                )

        restricted_password = f"{_MERIDIAN_RESTRICTED_USERNAME}-demo"
        restricted_repr = {
            "username": _MERIDIAN_RESTRICTED_USERNAME,
            "enabled": True,
            "firstName": "Meridian",
            "lastName": "Frontline",
            "email": "meridian-frontline@ne-pilot.local",
            "emailVerified": True,
            "requiredActions": [],
            "groups": list(_MERIDIAN_RESTRICTED_GROUPS),
            "credentials": [{"type": "password", "value": restricted_password, "temporary": False}],
        }
        create_restricted = client.post(
            f"{base}/admin/realms/ne-pilot/users", headers=headers, json=restricted_repr
        )
        if create_restricted.status_code not in (201, 409):
            create_restricted.raise_for_status()
        restricted_id = exact_user_id(_MERIDIAN_RESTRICTED_USERNAME)
        client.put(
            f"{base}/admin/realms/ne-pilot/users/{restricted_id}",
            headers=headers,
            json={k: v for k, v in restricted_repr.items() if k != "credentials"},
        ).raise_for_status()
        reconcile_exact_groups(restricted_id, _MERIDIAN_RESTRICTED_GROUPS)
        client.put(
            f"{base}/admin/realms/ne-pilot/users/{restricted_id}/reset-password",
            headers=headers,
            json={"type": "password", "value": restricted_password, "temporary": False},
        ).raise_for_status()

        # Keep the seeded owner/approver platform roles, but reconcile their exact
        # Meridian persona/entitlement groups. Never lend these groups to frontline
        # or persist credentials until every membership/password update succeeds.
        ops_id = exact_user_id(_SEEDED_OPS_USERNAME)
        reconcile_exact_groups(ops_id, _MERIDIAN_OPS_GROUPS)
        approver_id = exact_user_id(_SEEDED_APPROVER_USERNAME)
        reconcile_exact_groups(approver_id, _MERIDIAN_APPROVER_GROUPS)
        # The Skill ACL label is derived authority, never an independent persona
        # grant. Project it only after the source persona has reconciled, using the
        # same authenticated live admin client. The projector fully reads both
        # direct-membership sets before its first write and is a zero-write no-op
        # when already converged.
        from ne.cli.demo.skill_authority import project_servicing_skill_authority

        project_servicing_skill_authority(client, base=base, headers=headers)
        client.put(
            f"{base}/admin/realms/ne-pilot/users/{ops_id}/reset-password",
            headers=headers,
            json={"type": "password", "value": _SEEDED_OPS_PASSWORD, "temporary": False},
        ).raise_for_status()
        client.put(
            f"{base}/admin/realms/ne-pilot/users/{approver_id}/reset-password",
            headers=headers,
            json={
                "type": "password",
                "value": _SEEDED_APPROVER_PASSWORD,
                "temporary": False,
            },
        ).raise_for_status()
        steward_id = exact_user_id(_SEEDED_STEWARD_USERNAME)
        reconcile_exact_groups(steward_id, _STEWARD_GROUPS)
        client.put(
            f"{base}/admin/realms/ne-pilot/users/{steward_id}/reset-password",
            headers=headers,
            json={
                "type": "password",
                "value": _SEEDED_STEWARD_PASSWORD,
                "temporary": False,
            },
        ).raise_for_status()
        creds[_MERIDIAN_RESTRICTED_USERNAME] = {
            "username": _MERIDIAN_RESTRICTED_USERNAME,
            "password": restricted_password,
        }
        creds["ops"] = {
            "username": _SEEDED_OPS_USERNAME,
            "password": _SEEDED_OPS_PASSWORD,
        }
        creds[_SEEDED_STEWARD_USERNAME] = {
            "username": _SEEDED_STEWARD_USERNAME,
            "password": _SEEDED_STEWARD_PASSWORD,
        }

        # NE-DEMO-HTTPS-PORT (hazard c) / NE-DEMO-EXTERNAL-URL: a console origin that
        # differs from the committed realm's :443 allowlist must be added to the
        # ne-console client's redirect-URI + web-origin allowlist or OIDC login breaks.
        # The committed realm JSON allowlists only https://host[:443]/auth/callback, so
        # we PUT the full client representation with the chosen origin appended
        # (idempotent — re-running `ne demo up` is safe). The pinned realm JSON is never
        # edited; the default 443 path (no external URL) skips this entirely.
        if https_port != 443 or external_url is not None:
            resp = client.get(
                f"{base}/admin/realms/ne-pilot/clients",
                params={"clientId": "ne-console"},
                headers=headers,
            )
            found = resp.json()
            if not found:
                raise click.ClickException(
                    "ne-console client not found in realm ne-pilot — did the realm import complete?"
                )
            console = found[0]
            # NE-DEMO-EXTERNAL-URL: the browser hits the external edge URL (no port),
            # so allowlist THAT origin rather than the on-box console publish origin.
            if external_url is not None:
                origin = external_url
            else:
                origin = _console_base(hostname, https_port)
            redirect = f"{origin}/auth/callback"
            redirect_uris = console.setdefault("redirectUris", [])
            if redirect not in redirect_uris:
                redirect_uris.append(redirect)
            web_origins = console.setdefault("webOrigins", [])
            if origin not in web_origins:
                web_origins.append(origin)
            client.put(
                f"{base}/admin/realms/ne-pilot/clients/{console['id']}",
                headers=headers,
                json=console,
            ).raise_for_status()

    # Reuse the pre-seeded pilot `approver` principal for the HITL approval step
    # (throwaway local demo credential from the committed realm — see the module
    # constants). Recorded under scenario.approver_id so demo_run's token lookup
    # is uniform across reader/outsider/approver.
    creds[scenario.approver_id] = {
        "username": _SEEDED_APPROVER_USERNAME,
        "password": _SEEDED_APPROVER_PASSWORD,
    }
    creds_path = bundle.parent / "demo-credentials.json"
    fd = os.open(creds_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # os.open's mode applies only when creating a file. Tighten a pre-existing
    # credentials file before writing any newly generated password bytes.
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        # NE-DEMO-HTTPS-PORT: record the publish port so `ne demo run` drives the arc
        # against the same console origin (always written, incl. the default 443).
        payload: dict[str, Any] = {
            "hostname": hostname,
            "https_port": https_port,
            "principals": creds,
        }
        # NE-DEMO-EXTERNAL-URL: record the external URL ONLY when set so `ne demo run`
        # knows to dial loopback for its on-box calls (the default file stays
        # byte-identical — no `external_url: null` key when no edge fronts the demo).
        if external_url is not None:
            payload["external_url"] = external_url
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.chmod(creds_path, 0o600)
    return creds_path


def _host_sync_gateway_models() -> list[dict[str, object]]:
    """The single-model NE_GATEWAY_MODELS menu used by the host-loopback seed
    process (module-level so the producer-parity guardian can import + validate
    it). Mirrors the container's private-default entry but dials the
    host-published vllm-cpu port; LiteLLM is lazy so it is never called during
    sync. ``context_window`` LAST to match the emitted JSON key order."""
    return [
        {
            "name": "private-default",
            "litellm_model": f"hosted_vllm/{_SERVED_NAME}",
            "provider": "hosted_vllm",
            "lane": "private-vllm",
            "api_base": "http://127.0.0.1:8091/v1",
            "context_window": 32768,
        }
    ]


def _seed_corpus(workdir: Path, bundle: Path, corpus_dir: Path) -> tuple[str, ...]:
    """Export the acme fixture + drive ``ne connect sync`` for the fixture corpus
    AND the polled renewals sheet (the EXISTING governed C7 path — never a
    bespoke writer).

    Returns the sources that FAILED to seed (empty on success). The caller reports
    them; this function does not decide the bring-up's fate.
    """
    from ne.acme.cli import default_fixtures_dir
    from ne.cli.checks import _find_repo_root
    from ne.cli.connect import ConnectorSyncFailed, sync
    from ne.cli.demo.corpus import export_connector_fixture
    from ne.cli.pilotcfg import resolve_required_secrets

    fixture_json = corpus_dir / "acme-fixture.json"
    export_connector_fixture(default_fixtures_dir(), fixture_json)

    # The connector-state reset that makes every `ne demo up` fully RE-SEED the
    # (freshly `down --volumes`'d) qdrant happens in _stage_connect_dir, BEFORE
    # bring-up — deleting the sqlite files after the worker has opened them
    # would split the inode between its cron and ne-api's reads. Only the
    # LEGACY workdir-root paths (pre-/connect layouts) are cleared here so an
    # old workdir cannot leave a stale ledger behind.
    for state_db in ("connect-state.db", "connect-quarantine.db"):
        (workdir / state_db).unlink(missing_ok=True)

    resolved = resolve_required_secrets(bundle)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    agent_manifest_path = _find_repo_root(Path.cwd()) / "deploy" / "agents" / "registry.toml"
    pg = resolved["NE_PILOT_POSTGRES_PASSWORD"]
    # Host-loopback platform env mirroring the compose x-ne-app-env anchor
    # (DEPLOYMENT.md §6). The DSN carries the resolved password ONLY in-process env
    # — never logged or written to the dossier (C12).
    env = {
        # build_platform validates the FULL platform (gateway included) even though
        # fixture sync only exercises the TEI/embedding path — so the local process
        # needs a valid model menu (≥1 model) or GatewayConfigError aborts the seed.
        # Mirror the container's NE_GATEWAY_MODELS but dial the host-published vllm-cpu
        # port (LiteLLM is lazy, so it is never actually called during sync).
        "NE_GATEWAY_MODELS": json.dumps(_host_sync_gateway_models()),
        # Freeze the same checked-in Agent registry bytes used by API and worker.
        # Never inherit an ambient path from a prior shell/demo generation.
        "NE_API_AGENT_MANIFEST_PATH": str(agent_manifest_path),
        # The host-side governed fixture sync composes the same complete platform
        # as API/worker. Once private Agent Run payloads are active, that root
        # correctly refuses to open without the deployment's existing key
        # identity and KEK. Resolve both through the promoted SecretStore and
        # retain them only in this process environment, just like the database
        # password below; never generate a second key or write either value into
        # the worktree/overlay.
        "NE_AGENT_PRIVATE_PAYLOAD_KEY_ID": resolved["NE_AGENT_PRIVATE_PAYLOAD_KEY_ID"],
        "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64": resolved["NE_AGENT_PRIVATE_PAYLOAD_KEK_B64"],
        # Corpus chunks are embedded on the CPU-only demo TEI server; a batch can
        # exceed the embedder's 30s default read-timeout, so give it generous headroom
        # (read by _default_embedder; not part of config_hash). Shares
        # _DEMO_TEI_TIMEOUT_S with the query-time overlay above (NE-DEMO-FIX1) —
        # ingest and query must use the same timeout or query embeds 502.
        "NE_TEI_EMBED_TIMEOUT_S": _DEMO_TEI_TIMEOUT_S,
        # NE-CONNECT-CSV: the first seed runs BOTH the fixture corpus and the
        # renewals sheet through the SAME governed sync the in-container cron
        # drives, over the SAME /connect-mounted state + quarantine dbs — so the
        # Sources screen shows a populated csv lane (cursor, last-sync seq, a
        # non-zero quarantine) immediately after `ne demo up`, not at cron+15m.
        "NE_CONNECT_SYNC_SOURCES": '["fixture", "csv"]',
        "NE_CONNECT_SYNC_STATE_PATH": str(workdir / "connect" / "state.db"),
        "NE_CONNECT_SYNC_QUARANTINE_PATH": str(workdir / "connect" / "quarantine.db"),
        "NE_CONNECT_SYNC_KPI_TAG": "ne-demo",
        "NE_CONNECT_FIXTURE_PATH": str(fixture_json),
        "NE_CONNECT_FIXTURE_OWNER": "acme-demo",
        "NE_CONNECT_CSV_PATH": str(workdir / "connect" / "acme-renewals.csv"),
        "NE_CONNECT_CSV_SCHEMA_PATH": str(workdir / "connect" / "renewals.schema.json"),
        "NE_CONNECT_CSV_OWNER": "acme-demo-fixture",
        "NE_CONNECT_CSV_ID_COLUMN": "renewal_id",
        "NE_CONNECT_CSV_DEFAULT_ACL": '["all-employees"]',
        "NE_RUNTIME_SESSION_DATABASE_URL": f"postgresql+psycopg://ne:{pg}@127.0.0.1:5432/neutron",
        "NE_AUDIT_DATABASE_URL": f"postgresql+psycopg://ne:{pg}@127.0.0.1:5432/neutron",
        "NE_PERMISSIONS_DATABASE_URL": f"postgresql+psycopg://ne:{pg}@127.0.0.1:5432/neutron",
        "NE_FLYWHEEL_SIGNAL_DATABASE_URL": f"postgresql+psycopg://ne:{pg}@127.0.0.1:5432/neutron",
        "NE_API_EVAL_DATABASE_URL": f"postgresql+psycopg://ne:{pg}@127.0.0.1:5432/neutron",
        "NE_API_QDRANT_URL": "http://127.0.0.1:6333",
        "NE_API_QDRANT_COLLECTION": "ne_chunks",
        "NE_API_TEI_URL": "http://127.0.0.1:8085",
        "NE_GOVERN_OPA_URL": "http://127.0.0.1:8181",
        "NE_IDENTITY_OIDC__ISSUER": f"https://{manifest['hostname']}:8443/realms/ne-pilot",
        "NE_IDENTITY_OIDC__AUDIENCE": "ne-api",
    }
    os.environ.update(env)
    ctx = click.Context(sync)
    # No source argument ⇒ every configured source: fixture (the Acme corpus)
    # then csv (the renewals sheet — clean rows written, defect rows parked).
    # A failure is CAUGHT here, not swallowed: the containers are already up and a
    # demo box that refuses to come up is worse than one that comes up loudly
    # incomplete, so the sources are returned for :func:`_report_seed_failure` to
    # scream about. See NE-CONNECT-SEEDLOCK / INBOX 26.
    try:
        ctx.invoke(sync, source=None)
    except ConnectorSyncFailed as exc:
        return exc.sources
    return ()


#: The two corpora the host-side seed owns, and what the demo loses without each.
#: Neither is decorative: ``fixture`` IS the Acme corpus every Ask/Search/permission
#: -trap/grounded-analyst beat queries, and ``csv`` is the renewals sheet whose
#: parked defect rows ARE the quarantine exhibit.
_SEED_SOURCE_STAKES: dict[str, str] = {
    "fixture": (
        "the Acme corpus — Ask, Search, the permission trap and the grounded-analyst "
        "beats all query it; without it the stack has no documents"
    ),
    "csv": (
        "the Acme renewals sheet — the Sources screen csv lane and the write-quarantine "
        "exhibit (parked defect rows) come from it"
    ),
}


def _report_seed_failure(failed: Sequence[str], workdir: Path) -> None:
    """Make an unseeded corpus IMPOSSIBLE to miss, then fail the bring-up.

    The stack is already up by the time this runs, so the box stays usable and a
    human can retry the seed alone. What is NOT tolerated is the previous
    behaviour: one grey line, exit 0, and a half-seeded store discovered on demo
    morning. The banner names each corpus AND what the demo loses without it, and
    ``ne demo up`` exits non-zero so no wrapper can mistake this for a good run.
    """
    rule = "!" * 78
    click.echo("", err=True)
    click.echo(rule, err=True)
    click.echo("!! CORPUS SEEDING FAILED — THE DEMO STORE IS INCOMPLETE", err=True)
    click.echo(rule, err=True)
    for source in failed:
        stakes = _SEED_SOURCE_STAKES.get(source, "a configured connector source")
        click.echo(f"!!   {source}: NOT SEEDED — {stakes}", err=True)
    click.echo("!!", err=True)
    click.echo(
        "!! The stack itself is UP (containers are running) — only the seed failed, so",
        err=True,
    )
    click.echo("!! you can retry just the seed without a full bring-up:", err=True)
    click.echo(
        f"!!   ne connect sync <source>   # state: {workdir / 'connect' / 'state.db'}", err=True
    )
    click.echo(
        "!! The ERROR line above each source states the cause. `--traceback` adds more.",
        err=True,
    )
    click.echo(rule, err=True)
    click.echo("", err=True)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def demo_up(
    workdir: Path,
    *,
    hostname: str,
    seed: int | None,
    skip_image_build: bool,
    rebuild_image: bool = False,
    https_port: int = 443,
    external_url: str | None = None,
    gpu_vllm: str | None = None,
    gpu_rerank: str | None = None,
    tei_onnx_artifact: Path | None = None,
    live_writeback: bool = False,
    graph: bool = False,
    m365_draft_mailbox: str | None = None,
    seed_manifest_dir: Path | None = None,
    frontier: str | None = None,
) -> None:
    """Bring up the pilot stack, provision demo principals, seed the Acme corpus."""
    from ne.cli.demo.corpus import build_scenario

    graph_population: _GraphPopulationStaging | None = None
    graph_authority: _LiveWritebackStaging | None = None
    if graph:
        graph_manifest_dir = (
            seed_manifest_dir
            if seed_manifest_dir is not None
            else _DEFAULT_MERIDIAN_SEED_MANIFEST_DIR
        )
        graph_population = _stage_graph_population(graph_manifest_dir)

        # Stage only the M365 connector surface. SQL and Salesforce ambient
        # values are deliberately outside the admitted prefix set; Graph-only
        # selects exactly the governed draft action.
        if not live_writeback:
            graph_authority = _stage_live_writeback(
                workdir,
                graph_manifest_dir,
                graph_only=True,
                draft_mailbox=m365_draft_mailbox,
            )

    layout = _prepare_workdir(workdir)
    # NE-BUNDLE-GATEWAY-CONFIG (Argus P2 C1): stage the frontier API key BEFORE
    # `_preflight_docker()` — `_preflight_docker` shells out to the real `docker`
    # binary/daemon, so if it ran first a missing provider key would surface as a
    # Docker error (or, worse, a full image rebuild + model download) on a host
    # that lacks Docker, instead of the documented "fails loud before any docker
    # call" promise. Validating/staging the key first means the ONLY thing that
    # can run before the key check is workdir preparation (pure filesystem, no
    # docker/network). A missing key must abort HERE, before any docker call
    # (fail loud, never a silent degrade to the open-weight lane — the owner's
    # fail-loud instinct, kept from `_guard_frontier`). A re-up WITHOUT
    # --frontier clears any stale marker + env file from a prior swapped run —
    # the revert direction — so `ne demo up` always returns to the open-weight
    # lane by default.
    frontier_staging: _FrontierStaging | None = None
    if frontier is not None:
        frontier_staging = _stage_frontier(workdir, frontier)
    else:
        (workdir / _FRONTIER_MARKER).unlink(missing_ok=True)
        (workdir / "overlay" / "frontier.env").unlink(missing_ok=True)
    _preflight_docker()
    # Build the first-party ne-app image when it is absent, when its stamped
    # source-rev has DRIFTED from the current checkout (a `git pull` or local edit
    # — otherwise `ne demo up` silently runs a stale image, the NE-DEMO-STALE bug),
    # or when --rebuild-image forces it. Docker's layer cache makes a no-op rebuild
    # cheap; --skip-image-build opts out entirely (sealed re-runs).
    from ne.cli.checks import _find_repo_root

    repo_root = _find_repo_root(Path.cwd())
    if not skip_image_build and (rebuild_image or not _image_up_to_date(repo_root)):
        _build_app_image()
    secrets_toml = _seed_secret_store(layout["store"])
    _promote_bundle(layout["bundle"], secrets_toml, hostname)
    _download_model(layout["models"])
    # NE-TEI-ONNX: pre-stage the tei-embed hub cache (egress-staging block,
    # alongside _download_model) so the fast ort backend can boot instead of
    # the slow candle CPU fallback.
    tei_cache_dir = _stage_tei_cache(workdir, tei_onnx_artifact)
    # NE-DEMO-LIVE-SEAMS: stage the seed + overlay BEFORE bring-up (the graph fixture
    # reads the acme fixtures directly — no running stack needed). NE-DEMO-EXTERNAL-URL:
    # external_url binds the overlay's caddy ports to loopback (edge fronts the demo)
    # and points the ne-console OIDC allowlist at the external URL.
    seed_dir = _stage_demo_seed(workdir)
    # NE-CONNECT-CSV: stage the /connect bind-mount dir (the sheet + its schema)
    # BEFORE the overlay/bring-up — docker would otherwise create the missing
    # bind source as a root-owned empty dir and the csv lane would fail loud.
    _stage_connect_dir(workdir)
    # NE-DEMO-WRITEBACK-LIVE: staged AFTER the seed, BEFORE the overlay is written
    # (the overlay needs the staging's env_file/pem_mounts). A re-up WITHOUT the
    # flag clears any stale marker + env file from a prior --live-writeback run —
    # the revert direction — so `ne demo up` always returns to demo mode by default.
    staging: _LiveWritebackStaging | None = None
    if live_writeback:
        manifest_dir = (
            seed_manifest_dir
            if seed_manifest_dir is not None
            else _DEFAULT_MERIDIAN_SEED_MANIFEST_DIR
        )
        # The kwarg rides only when the flag is set, so seams that double the
        # two-argument staging signature keep working unchanged.
        staging = (
            _stage_live_writeback(workdir, manifest_dir, draft_mailbox=m365_draft_mailbox)
            if m365_draft_mailbox
            else _stage_live_writeback(workdir, manifest_dir)
        )
    else:
        (workdir / _WRITEBACK_MARKER).unlink(missing_ok=True)
        (workdir / "overlay" / "writeback.env").unlink(missing_ok=True)
    if not graph:
        (workdir / "overlay" / "graph.env").unlink(missing_ok=True)
    # NE-BUNDLE-GATEWAY-CONFIG: the frontier staging itself already happened
    # above, before `_preflight_docker()` (Argus P2 C1) — `frontier_staging` is
    # threaded into `overlay_kwargs` below.
    # NE-DEMO-WRITEBACK-LIVE: thread live_writeback ONLY when staged. Passing the
    # kwarg unconditionally (even as None) breaks test_demo_https_port.py's frozen
    # `_overlay_fn` stub, which predates this kwarg and does not accept it — that
    # file is the byte-parity tripwire and is explicitly out of mayTouch, so the
    # fix belongs here: the default-mode call site keeps the exact pre-unit arity.
    # NE-BUNDLE-GATEWAY-CONFIG: `frontier` is threaded the SAME way, for the SAME
    # reason.
    overlay_kwargs: dict[str, Any] = {
        "external_url": external_url,
        "gpu_vllm": gpu_vllm,
        "tei_cache_dir": tei_cache_dir,
    }
    # NE-SEARCH-LATENCY: threaded ONLY when set (the live_writeback/frontier
    # convention above — the default-mode call site keeps the exact pre-unit arity).
    if gpu_rerank is not None:
        overlay_kwargs["gpu_rerank"] = gpu_rerank
    if staging is not None:
        overlay_kwargs["live_writeback"] = staging
    if graph_population is not None:
        overlay_kwargs["graph_population"] = graph_population
    if graph_authority is not None and staging is None:
        overlay_kwargs["graph_authority"] = graph_authority
    if frontier_staging is not None:
        overlay_kwargs["frontier"] = frontier_staging
    overlay_path = _write_demo_overlay(
        workdir,
        seed_dir,
        https_port,
        **overlay_kwargs,
    )
    # Same "thread it ONLY when set" convention as overlay_kwargs above, and for
    # the same reason: the default-mode call site must keep the exact pre-unit
    # arity that test_demo_https_port.py's frozen `_bring` stub accepts.
    bring_up_kwargs: dict[str, Any] = {}
    if external_url is not None:
        bring_up_kwargs["external_url"] = external_url
    if gpu_vllm is not None:
        bring_up_kwargs["gpu_vllm"] = gpu_vllm
    _bring_up_stack(layout["bundle"], layout["models"], overlay_path, https_port, **bring_up_kwargs)
    scenario = build_scenario()
    _provision_keycloak(
        layout["bundle"], hostname, scenario, https_port=https_port, external_url=external_url
    )
    _write_vllm_completion_target(workdir, gpu_vllm)
    failed_sources = _seed_corpus(workdir, layout["bundle"], layout["corpus"])
    if failed_sources:
        _report_seed_failure(failed_sources, workdir)
    click.echo(f"demo stack up; corpus seeded. Bundle: {layout['bundle']}")


# --- NE-CRED-MARGIN: the arc's bearers must outlast the arc -------------------
#
# ⭐ WHY THE DRIVER RENEWS AT ALL. Run admission now REFUSES a credential that
# cannot outlast the Run it would start (``ne.api.run_credential_floor``): the
# canonical Agent leg at its own agent's floor, the pre-010G chat leg at the
# deployment bound (420 s). The pilot realm mints 900 s access tokens, so a
# bearer minted ONCE at arc start is ADMISSIBLE for only ~480 s of those 900 --
# while the arc drives eight sequential governed sends plus retrieval, approval
# polling and audit verification, and one grounded-analyst turn on the CPU lane
# has been measured at over five minutes. Minting once and holding therefore
# 401s every send past T+480 s and FAILs the arc.
#
# ⛔ THE FLOOR IS NOT THE BUG AND THE DRIVER IS NOT EXEMPT FROM IT. The fix is
# the one the console already implements (``ensureRunCredential`` in
# ``packages/ne-console/src/api/client.ts``): renew before a submit that is
# close to the floor. It lives at the HTTP layer, not in the arc, for the same
# reason it lives in the console's API client rather than in each screen --
# every arc request passes through here exactly once, so no send can be
# forgotten, and ``arc.py`` stays a narration of the governed ladder.

#: Renew this many seconds ABOVE the server's floor rather than exactly at it.
#: ⚠ The console's trigger and the server's floor are the same 420, so a submit
#: decided at exactly the trigger reaches the server with nothing in hand and a
#: sub-second disagreement between the two clocks turns a working submit into a
#: refusal. One arc step (``request_deadline_s``, 60 s) of separation costs one
#: extra grant per bearer per ~7 minutes of arc and removes that band entirely.
_BEARER_RENEW_MARGIN_SECONDS: Final = 60


def _admission_floor_seconds() -> int:
    """The server's deployment-wide run-credential floor, read from the server.

    Imported lazily (the module-wide convention) and NOT restated here: the
    driver must renew against the number ``ne-api`` actually refuses at, so a
    change to the floor moves this trigger with it instead of silently leaving
    the demo driver one release behind.
    """

    from ne.api.run_credential_floor import RUN_CREDENTIAL_FLOOR_BOUND_SECONDS

    return RUN_CREDENTIAL_FLOOR_BOUND_SECONDS


@dataclass
class _LiveBearer:
    """One demo principal's access token, and what it takes to mint another.

    ⭐ RE-GRANT RATHER THAN REFRESH-TOKEN, deliberately. Keycloak's password
    grant does return a refresh token, but using it would make the demo depend
    on the realm client's refresh-token settings and on a second lifespan --
    two more ways for the arc to fail at minute six. The driver already holds
    these principals' passwords (it read them out of ``demo-credentials.json``
    to make the first grant), so re-granting is the same operation it has
    already proven works, with no new failure mode.

    ⛔ ``password`` is a live credential: it is held for the duration of one
    arc and is never logged, echoed or written to the dossier.
    """

    username: str
    password: str
    header: str
    expires_at: float

    def headers(self) -> dict[str, str]:
        """The Authorization header dict the arc carries per request."""

        return {"Authorization": self.header}

    def remaining(self, now: float) -> float:
        return self.expires_at - now


def _token_endpoint(base: str) -> str:
    return f"{base}/realms/ne-pilot/protocol/openid-connect/token"


def _grant_form(username: str, password: str) -> dict[str, str]:
    return {
        "grant_type": "password",
        "client_id": "ne-console",
        "scope": "openid",
        "username": username,
        "password": password,
    }


def _token_lifetime(payload: Mapping[str, Any]) -> tuple[str, int]:
    """``(access token, seconds it is valid for)`` from an OIDC token response.

    ⛔ FAILS LOUD ON A MISSING ``expires_in`` rather than guessing. That number
    IS the driver's renewal decision; a guessed one either renews on every
    request or -- far worse -- never renews and hands the arc the exact dead
    bearer this closes. Every Keycloak password grant returns it.

    ⛔ FAILS LOUD WHEN A FRESHLY MINTED CREDENTIAL STILL CANNOT CLEAR THE
    SERVER'S FLOOR. The realm's ``accessTokenLifespan`` is then below what any
    run needs and no amount of renewing can fix it; saying so once, by name,
    beats letting the arc report eight identical 401s.

    ⛔ Never returns, logs or echoes anything but the token itself to its one
    caller, and never the password.
    """

    token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not isinstance(token, str) or not token:
        raise click.ClickException("OIDC token response carried no access_token")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
        raise click.ClickException(
            "OIDC token response carried no usable expires_in; the demo driver cannot "
            "keep its bearers above the run-admission floor without it"
        )
    floor = _admission_floor_seconds()
    if expires_in < floor:
        raise click.ClickException(
            f"the realm mints {expires_in}s access tokens but run admission requires "
            f"{floor}s of remaining credential — raise the realm's accessTokenLifespan "
            "(ne-pilot ships 900s). Renewing cannot fix a lifespan below the floor."
        )
    return token, expires_in


async def _password_grant(
    client: Any,
    base: str,
    username: str,
    password: str,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> _LiveBearer:
    # ``client`` is the httpx.AsyncClient the arc drives — the POST is a coroutine
    # and MUST be awaited (a sync .post() here would return a coroutine and
    # .json() would raise AttributeError before the arc starts).
    resp = await client.post(_token_endpoint(base), data=_grant_form(username, password))
    resp.raise_for_status()  # fail loud on a bad grant (else .json() masks it as a decode error)
    token, expires_in = _token_lifetime(resp.json())
    return _LiveBearer(
        username=username,
        password=password,
        header=f"Bearer {token}",
        # MONOTONIC, not wall clock: the driver measures how much of the
        # lifespan it has spent, and a stepped system clock must not be able to
        # make a live bearer look dead (or a dead one look live).
        expires_at=clock() + expires_in,
    )


def _bearer_renewal(
    token_base: str,
    extra_headers: Mapping[str, str] | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> Any:
    """An ``httpx.Auth`` that keeps every registered bearer above the floor.

    Registered bearers are matched by the exact Authorization header the arc
    sends. The arc copies its header dicts once at start-up and never updates
    them, so it keeps presenting the ORIGINAL token for the whole run; this
    rewrites the header on the way out, which is why no change to ``arc.py`` is
    needed and why no request can be missed. Every token the renewal has ever
    issued for a bearer stays in the map, so a stale copy held anywhere in the
    arc still resolves.

    Anything with no Authorization header — the token grant itself — passes
    through untouched, which is what keeps the renewal from recursing.

    ⚠ Two concurrent requests on the SAME bearer can both decide to renew; the
    arc runs its ladder sequentially but polls approvals alongside a streaming
    turn. That race is harmless rather than guarded: both grants succeed, both
    tokens are valid, both are registered, and the last one wins the bearer.
    """

    import httpx

    trigger = _admission_floor_seconds() + _BEARER_RENEW_MARGIN_SECONDS
    headers = dict(extra_headers or {})
    url = _token_endpoint(token_base)

    class _RenewingBearers(httpx.Auth):
        def __init__(self) -> None:
            self._by_header: dict[str, _LiveBearer] = {}

        def register(self, bearer: _LiveBearer) -> _LiveBearer:
            self._by_header[bearer.header] = bearer
            return bearer

        def _grant_request(self, request: Any, bearer: _LiveBearer) -> Any:
            # The client's own headers and timeout are NOT applied to a request
            # built here (only to requests built by `client.request`), so the
            # `Host` header the on-box dial needs is carried explicitly and the
            # timeout is inherited from the request that triggered the renewal
            # — without it httpcore would apply none at all and a wedged
            # Keycloak would hang the arc instead of failing its step.
            extensions = {}
            timeout = request.extensions.get("timeout")
            if timeout is not None:
                extensions["timeout"] = timeout
            return httpx.Request(
                "POST",
                url,
                data=_grant_form(bearer.username, bearer.password),
                headers=headers,
                extensions=extensions,
            )

        async def async_auth_flow(self, request: Any) -> AsyncGenerator[Any, Any]:
            held = request.headers.get("Authorization")
            bearer = self._by_header.get(held) if held is not None else None
            if bearer is None:
                yield request
                return
            if bearer.remaining(clock()) < trigger:
                response = yield self._grant_request(request, bearer)
                await response.aread()
                response.raise_for_status()
                token, expires_in = _token_lifetime(response.json())
                bearer.header = f"Bearer {token}"
                bearer.expires_at = clock() + expires_in
                self.register(bearer)
            request.headers["Authorization"] = bearer.header
            yield request

    return _RenewingBearers()


def demo_run(workdir: Path, *, docs_limit: int) -> None:
    """Run the narrated governed arc against the live stack; write the dossier."""
    import asyncio

    import httpx

    from ne.cli.demo.arc import run_demo_arc
    from ne.cli.demo.corpus import build_scenario
    from ne.cli.demo.dossier import render_dossier
    from ne.cli.pilotcfg import resolve_required_secrets

    # NE-VLLM-LIVENESS-PROBE: compose health is engine-completion-aware, but
    # recheck immediately before the run so the operator sees current, bounded
    # evidence rather than inferring readiness from ``docker ps``.
    _vllm_completion_preflight(workdir)

    # NE-TEI-ONNX: fail loud, before the narrated arc starts, if tei-embed is
    # unexpectedly slow given a staged ONNX cache (the candle-fallback smell);
    # warn-only (never fatal) when no ONNX was staged this run.
    _tei_embed_preflight(onnx_staged=_onnx_is_staged(workdir))

    # NE-BUNDLE-GATEWAY-CONFIG: name the active frontier swap before the arc
    # starts (the --gpu-vllm framing precedent) — None (no marker) prints nothing.
    posture_note = _frontier_posture_note(workdir)
    if posture_note is not None:
        click.echo(posture_note)

    bundle = workdir / "bundle"
    creds_path = workdir / "demo-credentials.json"
    creds = json.loads(creds_path.read_text(encoding="utf-8"))
    hostname = creds["hostname"]
    # NE-DEMO-HTTPS-PORT: old creds files (pre-flag) carry no https_port → default 443.
    https_port = int(creds.get("https_port", 443))
    # NE-DEMO-EXTERNAL-URL: when an edge fronts the demo, every on-box call (the arc's
    # ne-api requests AND the Keycloak password grants) must dial loopback, not the
    # hairpinning public hostname. Absent key → default (dial the hostname directly).
    external_url = creds.get("external_url")
    principals = creds["principals"]
    scenario = build_scenario()

    resolved = resolve_required_secrets(bundle)
    pg = resolved["NE_PILOT_POSTGRES_PASSWORD"]
    audit_dsn = f"postgresql+psycopg://ne:{pg}@127.0.0.1:5432/neutron"

    async def _audit_verify() -> dict[str, Any]:
        import sqlalchemy as sa

        from ne.audit import verify_chain

        engine = sa.create_engine(audit_dsn)
        try:
            cv = verify_chain(engine)
        finally:
            engine.dispose()
        return {"valid": cv.valid, "checked": cv.checked, "chain_detail": cv.detail}

    # NE-DEMO-EXTERNAL-URL: the arc's ne-api base and (below) the Keycloak grant base
    # dial loopback with a public Host header when an edge fronts the demo; the default
    # path is byte-identical (base == _console_base(hostname, https_port), no headers).
    base, onbox_headers = _onbox_dial(hostname, https_port, external_url)
    # OIDC token grants go to Keycloak (published on :8443), NOT the API/console port
    # — the API's Caddy serves the SPA (text/html) for /realms/*, so a grant against
    # `base` gets a non-JSON body. The issuer stays :8443 regardless of --https-port.
    kc_base, _ = _onbox_dial(hostname, 8443, external_url)
    # NE-DEMO-WRITEBACK-LIVE: which write-back tool Beat 4 calls — the inert demo
    # default (marker absent) or the real NE-050 CRM-note action (a live-writeback
    # marker staged by the last `ne demo up --live-writeback`).
    wb_action, wb_args = _resolve_writeback(workdir)

    async def _drive() -> Any:
        # verify=False: the pilot Caddy uses a LOCAL-CA cert; demo-only. The client-wide
        # Host header (set only under --external-url) routes both the ne-api and the
        # loopback-dialed Keycloak requests to the right Caddy vhost, and the transport
        # carries the public hostname as TLS SNI on those loopback dials (see
        # _onbox_transport — the named-vhost Caddy rejects a no-SNI handshake).
        #
        # NE-CRED-MARGIN: `auth=` is what keeps the arc's four bearers ADMISSIBLE
        # for the whole ladder. Without it the arc holds one 900 s token per role
        # and every send past T+480 s is refused by the run-credential floor.
        renewal = _bearer_renewal(kc_base, onbox_headers)
        async with httpx.AsyncClient(
            base_url=base,
            headers=onbox_headers or None,
            verify=False,
            timeout=60.0,
            transport=_onbox_transport(hostname, external_url, use_async=True),
            auth=renewal,
        ) as client:
            bearers = {}
            for role, key in (
                ("reader", scenario.reader_id),
                ("outsider", scenario.outsider_id),
                ("approver", scenario.approver_id),
            ):
                principal = principals[key]
                bearers[role] = renewal.register(
                    await _password_grant(
                        client, kc_base, principal["username"], principal["password"]
                    )
                ).headers()
            # NE-168: the delegation demo step is ops-gated. Back-compat with
            # pre-NE-168 creds files (no "ops" key) — fall back to the well-known
            # seeded ops principal (the pre-seeded pilot realm ships it either way).
            ops_cred = principals.get(
                "ops", {"username": _SEEDED_OPS_USERNAME, "password": _SEEDED_OPS_PASSWORD}
            )
            bearers["ops"] = renewal.register(
                await _password_grant(client, kc_base, ops_cred["username"], ops_cred["password"])
            ).headers()
            return await run_demo_arc(
                client,
                bearers,
                scenario,
                audit_verify=_audit_verify,
                writeback_action=wb_action,
                writeback_args=wb_args,
                progress=_arc_progress,
                request_deadline_s=60.0,
            )

    transcript = asyncio.run(_drive())
    note = (
        "live host-side connector-sync recomputes its own config_hash (differs from "
        "the containers'); audit-chain verification is unaffected (FLAG-4)."
    )
    dossier = render_dossier(transcript, scenario, deployment_note=note)
    dossier_path = workdir / "DEMO-DOSSIER.md"
    dossier_path.write_text(dossier, encoding="utf-8")

    click.echo("\nDemo arc step status:")
    for step in transcript.steps:
        click.echo(f"  {step.status.value:9} {step.title}")
    click.echo(f"\nDossier written: {dossier_path}")
    if transcript.has_failure():
        raise SystemExit(1)


def demo_down(workdir: Path, *, volumes: bool) -> None:
    """Tear down the pilot stack.

    NE-BUNDLE-GATEWAY-CONFIG (Argus round-2 P2): a staged frontier API key
    (``workdir/overlay/frontier.env``, 0600) and its marker
    (``workdir/demo-frontier.json``) are cleared HERE too, not only by a
    subsequent plain ``ne demo up`` — a live vendor credential must not be left
    on disk indefinitely after the demo stack is torn down (C12). Missing files
    are a no-op (``missing_ok=True``).
    """
    from ne.cli.up import down

    ctx = click.Context(down)
    ctx.invoke(down, bundle=workdir / "bundle", volumes=volumes)
    (workdir / _FRONTIER_MARKER).unlink(missing_ok=True)
    (workdir / "overlay" / "frontier.env").unlink(missing_ok=True)
