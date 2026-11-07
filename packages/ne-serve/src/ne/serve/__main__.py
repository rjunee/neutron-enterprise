# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``python -m ne.serve`` CLI: air-gap weight pre-staging + integrity verify.

    python -m ne.serve download    <repo_id> --dest ./models [--revision main]
    python -m ne.serve verify      <repo_id> --dest ./models
    python -m ne.serve stage-tei   <repo_id> --dest ./tei-cache [--onnx-artifact PATH \
        --export-manifest PATH] [--no-generate]
    python -m ne.serve stage-tei-verify --dest ./tei-cache

``download`` pre-stages weights to a local path (writing a v2 manifest with the
resolved immutable commit SHA + per-file sha256), then prints the exact
offline-env exports and a ready-to-copy compose bring-up hint. ``verify``
re-checks the staged bytes against that manifest — integrity at the air-gap
boundary. ``stage-tei``/``stage-tei-verify`` build/verify the TEI hub-cache
tree (NE-TEI-ONNX). ``stage-tei`` AUTO-generates + caches the fast-ort ONNX pair
for known repos (NE-TEI-GEN, via ``$NE_TEI_ONNX_CACHE`` + the isolated export
toolchain) unless ``--no-generate`` is given or an explicit ``--onnx-artifact``
override (the air-gap bundle path) is passed. Zero-add deps: stdlib argparse
only (no click).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ne.serve.download import download_weights, local_path, offline_env, verify_weights
from ne.serve.errors import WeightIntegrityError
from ne.serve.stage_tei import stage_tei, stage_tei_verify

_COMPOSE_HINT = "docker compose -f deploy/compose/serve/compose.yaml --profile gpu up"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ne.serve",
        description="Pre-stage vLLM model weights for air-gapped (offline) serving.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download", help="pre-stage weights to a local path")
    download.add_argument("repo_id", help="HuggingFace repo id, e.g. Qwen/Qwen3-0.6B")
    download.add_argument(
        "--dest",
        default="./models",
        help="local weights root (default ./models); weights land under <dest>/<org>/<name>",
    )
    download.add_argument("--revision", default="main", help="git revision (default main)")

    verify = sub.add_parser("verify", help="re-check staged weights against their manifest")
    verify.add_argument("repo_id", help="HuggingFace repo id, e.g. Qwen/Qwen3-0.6B")
    verify.add_argument(
        "--dest",
        default="./models",
        help="local weights root (default ./models); weights live under <dest>/<org>/<name>",
    )

    stage = sub.add_parser(
        "stage-tei", help="pre-stage a TEI hub-cache tree (optionally injecting a fast ONNX pair)"
    )
    stage.add_argument("repo_id", help="HuggingFace repo id, e.g. Qwen/Qwen3-Embedding-0.6B")
    stage.add_argument(
        "--dest",
        default="./tei-cache",
        help="local hub-cache root (default ./tei-cache); the served TEI /data mount",
    )
    stage.add_argument("--revision", default="main", help="git revision (default main)")
    stage.add_argument(
        "--onnx-artifact",
        default=None,
        help="path to the committed model-export .tar.gz artifact (requires --export-manifest)",
    )
    stage.add_argument(
        "--export-manifest",
        default=None,
        help="path to the committed model-export manifest (the C11 pin/hash authority)",
    )
    stage.add_argument(
        "--skip-safetensors",
        action="store_true",
        help="omit model.safetensors from staging (size-constrained; a broken ONNX then "
        "degrades to a dead service, not slow-candle — default keeps it)",
    )
    stage.add_argument(
        "--no-generate",
        action="store_true",
        help="stage the official snapshot ONLY — tei-embed then falls back to the slow "
        "candle CPU backend. Default (no flag) AUTO-generates + caches the fast-ONNX pair "
        "via the isolated export toolchain for known repos, using $NE_TEI_ONNX_CACHE "
        "(AIR_GAP fail-closes on a cache miss). Ignored when --onnx-artifact is given.",
    )

    stage_verify = sub.add_parser(
        "stage-tei-verify", help="re-check a staged TEI hub-cache tree at the air-gap boundary"
    )
    stage_verify.add_argument(
        "--dest",
        default="./tei-cache",
        help="local hub-cache root (default ./tei-cache)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "download":
        try:
            result = download_weights(
                args.repo_id,
                Path(args.dest),
                revision=args.revision,
            )
        except Exception as exc:
            print(f"error: download failed for {args.repo_id!r}: {exc}", file=sys.stderr)
            return 1

        print(f"pre-staged {args.repo_id} -> {result.path}")
        print(f"manifest: {result.manifest_path}")
        print(f"resolved revision: {result.resolved_revision}")
        print()
        print("# offline serving env (zero runtime egress):")
        for key, value in offline_env().items():
            print(f"export {key}={value}")
        print()
        print("# verify staged weights against the manifest (air-gap boundary):")
        print(f"python -m ne.serve verify {args.repo_id} --dest {args.dest}")
        print()
        print("# then bring up the GPU serving profile:")
        print(f"NE_VLLM_WEIGHTS_ROOT={args.dest} {_COMPOSE_HINT}")
        return 0

    if args.command == "verify":
        try:
            manifest = verify_weights(local_path(args.repo_id, Path(args.dest)))
        except WeightIntegrityError as exc:
            print(
                f"error: weight verification FAILED for {args.repo_id!r}:\n{exc}",
                file=sys.stderr,
            )
            return 1
        print(
            f"verified {len(manifest['files'])} files for {args.repo_id} "
            f"@ {manifest['resolved_revision']}"
        )
        return 0

    if args.command == "stage-tei":
        if args.onnx_artifact and not args.export_manifest:
            print(
                "error: --onnx-artifact requires --export-manifest (the C11 pin/hash authority)",
                file=sys.stderr,
            )
            return 1
        try:
            result = stage_tei(
                args.repo_id,
                Path(args.dest),
                revision=args.revision,
                onnx_artifact=Path(args.onnx_artifact) if args.onnx_artifact else None,
                export_manifest=Path(args.export_manifest) if args.export_manifest else None,
                skip_safetensors=args.skip_safetensors,
                # Default auto-generates; --no-generate opts out; an explicit
                # --onnx-artifact override always wins (generate is gated on it None).
                generate=not args.no_generate and not args.onnx_artifact,
            )
        except WeightIntegrityError as exc:
            print(f"error: stage-tei FAILED for {args.repo_id!r}:\n{exc}", file=sys.stderr)
            return 1
        print(f"staged {args.repo_id} -> {result.path}")
        print(f"manifest: {result.manifest_path}")
        print(f"resolved revision: {result.resolved_revision}")
        print()
        print("# verify staged tree against the manifest (air-gap boundary):")
        print(f"python -m ne.serve stage-tei-verify --dest {args.dest}")
        return 0

    if args.command == "stage-tei-verify":
        try:
            results = stage_tei_verify(Path(args.dest))
        except WeightIntegrityError as exc:
            print(f"error: stage-tei-verify FAILED:\n{exc}", file=sys.stderr)
            return 1
        for name, manifest in sorted(results.items()):
            print(
                f"verified {name}: {len(manifest['files'])} files @ {manifest['resolved_revision']}"
            )
        return 0

    parser.error(f"unknown command {args.command!r}")
    return 2  # pragma: no cover — argparse.error exits before this


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
