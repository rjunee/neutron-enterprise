# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne demo`` — the one-command governed demo (NE-DEMO).

Bare ``ne demo`` == ``up`` then ``run`` (then prints a teardown hint). Import-cheap
(click only, at module scope): every heavy operation lives in the lazily-imported
``ne.cli.demo.live`` seams, so ``ne --help`` never drags ne.api/ne.acme/docker.

The demo is a pure CONSUMER of the existing governed stack: deploy via ``ne up``,
seed via ``ne connect sync fixture``, query via ne-api /v1/brain + /v1/chat, govern
via the frozen C6 executor/HITL/audit. It reimplements NONE of them. See
docs/demo/README.md for the operator runbook.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

import click

_FRONTIER_HELP = (
    "Swap the agent turns onto a frontier model this run — config-only "
    "(APPENDS a frontier-default model to the gateway menu, reroutes the chat "
    "'agent' task with NO fallback and the canonical "
    "'agent.meridian-analyst.v2' policy with its declared private fallbacks, "
    "and enables the governed frontier warm call so the lane can earn the "
    "serving evidence model selection requires). Requires ANTHROPIC_API_KEY / "
    "OPENAI_API_KEY exported on the host: fails LOUD before any docker call if "
    "absent — never a silent degrade to the open-weight lane. Omit (or re-run "
    "without the flag / unset NE_DEMO_FRONTIER) to run/revert to the sealed, "
    "CPU-local open-weight lane. See docs/demo/README.md 'Frontier model "
    "swap (--frontier)'."
)


@click.group("demo", invoke_without_command=True)
@click.option(
    "--workdir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("ne-demo"),
    show_default=True,
    help="Working directory for the demo bundle/secrets/models/corpus + dossier.",
)
@click.option(
    "--frontier",
    type=click.Choice(["anthropic", "openai"]),
    default=None,
    envvar="NE_DEMO_FRONTIER",
    help=_FRONTIER_HELP,
)
@click.pass_context
def demo(ctx: click.Context, workdir: Path, frontier: str | None) -> None:
    """Run the governed demo (T1→T3→write-back→audit; T2 degrades until AGE)."""
    ctx.ensure_object(dict)
    ctx.obj["workdir"] = workdir
    ctx.obj["frontier"] = frontier
    if ctx.invoked_subcommand is None:
        ctx.invoke(up, frontier=frontier)
        ctx.invoke(run)
        click.echo(f"\nTear down with: ne demo down --workdir {workdir}")


@demo.command("up")
@click.option("--hostname", default="localhost", show_default=True, help="Pilot TLS hostname.")
@click.option("--seed", type=int, default=None, help="Acme corpus seed (default = ne-acme seed).")
@click.option(
    "--skip-image-build",
    is_flag=True,
    help="Do not build the first-party ne-app image (assume it is already present).",
)
@click.option(
    "--rebuild-image",
    is_flag=True,
    help="Force a rebuild of the first-party ne-app image even when it is already "
    "present AND up to date. `ne demo up` normally auto-rebuilds on source drift "
    "(git HEAD or local edits) via the image's stamped source-rev label, so this "
    "is only needed to force a rebuild with no source change. Mutually exclusive "
    "with --skip-image-build.",
)
@click.option(
    "--https-port",
    type=click.IntRange(1, 65535),
    default=443,
    show_default=True,
    help="Host port the demo publishes the API on (use when 443 is held, e.g. a "
    "Tailscale listener). Keycloak stays on 8443. A non-443 port requires docker "
    "compose >= 2.24.4.",
)
@click.option(
    "--external-url",
    default=None,
    help="External URL an edge/reverse-proxy serves the demo at (e.g. "
    "https://demo.example.host — real cert, no port). When set, the demo binds its "
    "published ports to 127.0.0.1 (the edge fronts them) and points the ne-console "
    "OIDC redirect/web-origin allowlist at this URL. Keycloak's OIDC issuer stays on "
    "https://<hostname>:8443 (the edge proxies :8443 with a real cert).",
)
@click.option(
    "--gpu-vllm",
    default=None,
    help="Point the private-vllm LLM lane at an EXTERNAL GPU vLLM server (e.g. a rented "
    "RTX 6000 Ada 48GB at https://<pod>-8000.proxy.runpod.net) for fast, reliably-tool-calling "
    "conversation — this lights the T3-chat + HITL beats green. The card must be Ada or "
    "newer (SM >= 8.9): the primary is an official FP8 quant, and vast-rig.sh's "
    "MIN_COMPUTE_CAP deliberately excludes Ampere (an A6000 serves it via Marlin "
    "weight-only, unverified for this checkpoint — a trap, not a wall, because it runs "
    "at full speed while quality degrades). The pod must serve "
    "`--served-model-name qwen3.6-35b-a3b --max-model-len 262144`. Its host is added to "
    "the C2 egress allowlist: a "
    "DELIBERATE demo-only relaxation of no-egress (synthetic corpus only) — Beat 0's "
    "'0 BYTES egress' is not literally true while a cloud GPU lane is active.",
)
@click.option(
    "--gpu-rerank",
    default=None,
    help="Point the /v1/brain/search rerank stage at an EXTERNAL GPU vLLM /rerank server "
    "serving the SAME BAAI/bge-reranker-v2-m3 under `--served-model-name rerank` (e.g. "
    "`deploy/inference-operator/vast-rig.sh serve rerank` → http://<ip>:<port>). NE-SEARCH-LATENCY "
    "measured the on-box CPU TEI rerank at ~40s per search (40 real candidates, ~1s/pair) "
    "vs ~0.12s on GPU — a server swap, never a model swap. The host is added to "
    "NE_API_RERANK_ALLOWED_HOSTS: a DELIBERATE demo-only egress relaxation, mirroring "
    "--gpu-vllm's framing, and it is CLEARTEXT http:// to an endpoint with no API key — "
    "synthetic corpus only. Three consequences: a dead rig HARD-FAILS every search (no "
    "fallback) after up to NE_TEI_RERANK_TIMEOUT_S; engaging the air gap makes search "
    "fail CLOSED on this lane however the host is spelled (an allowlist grant is fully "
    "revoked under the gap), so revert before the air-gap beat; re-measure the "
    "side-by-side before claiming identical results. Omit to keep the sealed on-box CPU "
    "tei-rerank (searches are slow but sovereign).",
)
@click.option(
    "--tei-onnx-artifact",
    "tei_onnx_artifact",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    envvar="NE_DEMO_TEI_ONNX_ARTIFACT",
    help="Path to the committed model-export .tar.gz artifact (NE-TEI-ONNX) — stages it "
    "into the tei-embed hub cache so TEI boots its fast ort backend instead of the slow "
    "candle CPU fallback. Requires the committed export manifest under "
    "tools/model-export/manifests/. Omit to stage the official snapshot only (candle).",
)
@click.option(
    "--live-writeback",
    is_flag=True,
    help="Swap Beat 4 (CONTROL RODS) off the inert demo write-back tool onto the real "
    "governed NE-050 write.internal.crm.note Salesforce write — a DELIBERATE demo-only "
    "egress to a Salesforce developer org you control (mirrors the --gpu-vllm framing). "
    "Requires NE_SALESFORCE_*/NE_M365_* connector env exported on the host "
    "(NE_SQL_* is optional — a demo-only sqlite fallback is injected when absent). "
    "See docs/demo/README.md 'Live write-back (--live-writeback)'.",
)
@click.option(
    "--graph",
    is_flag=True,
    help="Enable persistent AGE reads and governed M365→Graph population from an "
    "exact Meridian m365.json target binding. This does not enable Salesforce write-back.",
)
@click.option(
    "--m365-draft-mailbox",
    default=None,
    envvar="NE_DEMO_M365_DRAFT_MAILBOX",
    help="Exact M365 mailbox (UPN or Entra user object id) that the governed "
    "draft action may address. Required with --graph unless --live-writeback "
    "is selected; optional with --live-writeback (it then also becomes the "
    "deployment default mailbox); non-secret and config-hashed.",
)
@click.option(
    "--seed-manifest-dir",
    "seed_manifest_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the external seed-manifest directory. Live write-back resolves its "
    "target Account from salesforce.json; --graph resolves the exact Meridian M365 "
    "target from m365.json. Without an override the standard ~/.ne/meridian-seed "
    "location is used for both.",
)
@click.option(
    "--frontier",
    type=click.Choice(["anthropic", "openai"]),
    default=None,
    envvar="NE_DEMO_FRONTIER",
    help=_FRONTIER_HELP,
)
@click.pass_context
def up(
    ctx: click.Context,
    hostname: str,
    seed: int | None,
    skip_image_build: bool,
    rebuild_image: bool,
    https_port: int,
    external_url: str | None,
    gpu_vllm: str | None,
    gpu_rerank: str | None,
    tei_onnx_artifact: Path | None,
    live_writeback: bool,
    graph: bool,
    m365_draft_mailbox: str | None,
    seed_manifest_dir: Path | None,
    frontier: str | None,
) -> None:
    """Bring up the pilot stack, provision demo principals, and seed the Acme corpus."""
    from ne.cli.demo import live

    if https_port == 8443:
        raise click.ClickException("--https-port must differ from the Keycloak publish port 8443")
    if rebuild_image and skip_image_build:
        raise click.UsageError("--rebuild-image and --skip-image-build are mutually exclusive")
    workdir = ctx.obj["workdir"]
    # `demo up` invoked directly (not via bare `ne demo`) still honors a
    # group-level --frontier/NE_DEMO_FRONTIER — `up`'s own option (its own
    # envvar read) wins when explicitly given; else fall back to the group's.
    frontier = frontier or ctx.obj.get("frontier")
    live.demo_up(
        workdir,
        hostname=hostname,
        seed=seed,
        skip_image_build=skip_image_build,
        rebuild_image=rebuild_image,
        https_port=https_port,
        external_url=external_url,
        gpu_vllm=gpu_vllm,
        gpu_rerank=gpu_rerank,
        tei_onnx_artifact=tei_onnx_artifact,
        live_writeback=live_writeback,
        graph=graph,
        m365_draft_mailbox=m365_draft_mailbox,
        seed_manifest_dir=seed_manifest_dir,
        frontier=frontier,
    )


@demo.command("reconcile-personas")
@click.pass_context
def reconcile_personas(ctx: click.Context) -> None:
    """Reconcile existing demo personas without deploying, seeding, or syncing."""
    from ne.cli.demo.personas import (
        PersonaReconciliationSafelyRefused,
        PersonaRecoveryUnproven,
        reconcile_demo_personas,
    )

    try:
        credentials_path = reconcile_demo_personas(ctx.obj["workdir"])
    except (PersonaReconciliationSafelyRefused, PersonaRecoveryUnproven):
        raise
    except click.ClickException as exc:
        raise PersonaReconciliationSafelyRefused() from exc
    click.echo(f"Demo personas reconciled. Credentials: {credentials_path}")


@demo.command("run")
@click.option(
    "--docs-limit",
    type=int,
    default=0,
    show_default=True,
    help="Cap the seeded doc subset in the narration (0 = all seeded docs).",
)
@click.pass_context
def run(ctx: click.Context, docs_limit: int) -> None:
    """Run the narrated governed arc against the live stack; write the dossier."""
    from ne.cli.demo import live

    live.demo_run(ctx.obj["workdir"], docs_limit=docs_limit)


@demo.command("preflight")
@click.option(
    "--phase",
    type=click.Choice(["fresh-host", "pre-deploy", "post-deploy"]),
    required=True,
    help=(
        "Prove an empty dedicated host, check staged rollback/overlays before deploy, "
        "or also bind the rehearsal to the running stack."
    ),
)
@click.option(
    "--min-disk-free-gib",
    type=click.FloatRange(min=0.0),
    default=10.0,
    show_default=True,
    help="Required free space on the Docker data-root filesystem.",
)
@click.option(
    "--min-memory-available-gib",
    type=click.FloatRange(min=0.0, min_open=True),
    required=True,
    help="Owner-selected MemAvailable floor; required so the unmeasured vLLM cap is never guessed.",
)
@click.option(
    "--max-swap-used-percent",
    type=click.FloatRange(min=0.0, max=100.0, max_open=True),
    required=True,
    help="Owner-selected swap-pressure ceiling.",
)
@click.option(
    "--rollback-image",
    default=None,
    help="Dedicated, non-latest local rollback tag to verify (the command never tags or pulls it).",
)
@click.option(
    "--expected-rollback-id",
    default=None,
    help="Exact sha256 image ID printed pre-deploy; required post-deploy to catch tag movement.",
)
@click.option(
    "--writeback-mode",
    type=click.Choice(["inert", "live-crm"]),
    required=True,
    help="The write-back state the overlay must preserve on both app services.",
)
@click.option(
    "--rerank-mode",
    type=click.Choice(["cpu", "gpu"]),
    required=True,
    help="The rerank state the overlay must preserve on both app services.",
)
@click.option(
    "--gpu-rerank",
    default=None,
    help="Exact credential-free rerank endpoint; required only with --rerank-mode gpu.",
)
@click.pass_context
def host_preflight(
    ctx: click.Context,
    phase: str,
    min_disk_free_gib: float,
    min_memory_available_gib: float,
    max_swap_used_percent: float,
    rollback_image: str | None,
    expected_rollback_id: str | None,
    writeback_mode: str,
    rerank_mode: str,
    gpu_rerank: str | None,
) -> None:
    """Measure demo-host headroom and verify staged deploy/rehearsal state without mutations."""
    from ne.cli.demo.preflight import run_host_preflight

    run_host_preflight(
        ctx.obj["workdir"],
        phase=cast(Literal["fresh-host", "pre-deploy", "post-deploy"], phase),
        min_disk_free_gib=min_disk_free_gib,
        min_memory_available_gib=min_memory_available_gib,
        max_swap_used_percent=max_swap_used_percent,
        rollback_image=rollback_image,
        expected_rollback_id=expected_rollback_id,
        writeback_mode=cast(Literal["inert", "live-crm"], writeback_mode),
        rerank_mode=cast(Literal["cpu", "gpu"], rerank_mode),
        gpu_rerank=gpu_rerank,
    )


@demo.group("appendix-e")
def appendix_e_group() -> None:
    """Capture fresh Meridian T1/T2 comparison evidence without changing deployment state."""


@appendix_e_group.command("capture")
@click.option("--phase", type=click.Choice(["t1-only", "t1-t2"]), required=True)
@click.option("--rehearsal-id", required=True, help="One fresh identifier shared by both phases.")
@click.option(
    "--expected-model",
    required=True,
    help="Pinned requested model alias; every served model call is verified from its trace.",
)
@click.option("--api-base", required=True, help="HTTPS API base including /api when applicable.")
@click.option(
    "--reader-token-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--ops-token-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--ca-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None
)
@click.option(
    "--insecure",
    is_flag=True,
    help="Accept the pilot's untrusted TLS certificate (explicit rehearsal-only relaxation).",
)
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path), required=True)
def appendix_e_capture(
    phase: str,
    rehearsal_id: str,
    expected_model: str,
    api_base: str,
    reader_token_file: Path,
    ops_token_file: Path,
    ca_file: Path | None,
    insecure: bool,
    out: Path,
) -> None:
    """Capture one already-running profile; prints no comparison metrics."""
    import asyncio

    import httpx

    from ne.cli.demo.appendix_e import (
        EvidenceError,
        capture_appendix_e,
        normalize_api_base,
        read_token_file,
        tls_verifier,
        write_capture,
    )

    try:
        if out.exists():
            raise EvidenceError(f"refusing to overwrite existing capture: {out}")
        base = normalize_api_base(api_base)
        reader_token = read_token_file(reader_token_file)
        ops_token = read_token_file(ops_token_file)
        verify = tls_verifier(ca_file=ca_file, insecure=insecure)

        async def _capture() -> dict[str, object]:
            async with httpx.AsyncClient(
                base_url=base,
                verify=verify,
                timeout=httpx.Timeout(60.0, connect=10.0),
            ) as client:
                return await capture_appendix_e(
                    client,
                    phase=phase,
                    rehearsal_id=rehearsal_id,
                    expected_model=expected_model,
                    reader_token=reader_token,
                    ops_token=ops_token,
                )

        write_capture(out, asyncio.run(_capture()))
    except EvidenceError as exc:
        raise click.ClickException(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise click.ClickException(f"HTTP capture failed: {type(exc).__name__}") from exc
    click.echo(f"Captured fresh server evidence to {out}. No comparison metrics were printed.")


@appendix_e_group.command("compare")
@click.option("--t1", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option(
    "--t1-t2", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True
)
def appendix_e_compare(t1: Path, t1_t2: Path) -> None:
    """Print a comparison only from two fresh, controlled capture artifacts."""
    from ne.cli.demo.appendix_e import EvidenceError, compare_captures, load_capture

    try:
        rendered = compare_captures(load_capture(t1), load_capture(t1_t2))
    except EvidenceError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered, nl=False)


@demo.command("down")
@click.option("--volumes", is_flag=True, help="Also remove named volumes (destroys data).")
@click.pass_context
def down(ctx: click.Context, volumes: bool) -> None:
    """Tear down the pilot stack."""
    from ne.cli.demo import live

    live.demo_down(ctx.obj["workdir"], volumes=volumes)


__all__ = ["demo"]
