#!/usr/bin/env python3
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Publish the exact admitted vllm-cpu Compose set for the host operator."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--working-dir", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--compose-file", required=True, action="append", type=Path)
    return parser


def _run(command: list[str]) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def main() -> None:
    args = _parser().parse_args()
    docker = "/usr/bin/docker"
    if not Path(docker).is_file():
        raise SystemExit("fixed docker executable is unavailable")
    working_dir = args.working_dir.resolve(strict=True)
    env_file = args.env_file.resolve(strict=True)
    compose_files = tuple(path.resolve(strict=True) for path in args.compose_file)
    command = [
        docker,
        "compose",
        "--project-name",
        args.project_name,
        "--project-directory",
        str(working_dir),
        "--env-file",
        str(env_file),
    ]
    for compose_file in compose_files:
        command.extend(("--file", str(compose_file)))
    config = json.loads(_run([*command, "config", "--format", "json"]))
    service = config.get("services", {}).get("vllm-cpu")
    if not isinstance(service, dict) or not isinstance(service.get("image"), str):
        raise SystemExit("final admitted Compose set has no exact vllm-cpu service")
    hashes = _run([*command, "config", "--hash", "vllm-cpu"]).split()
    if len(hashes) != 2 or hashes[0] != "vllm-cpu" or len(hashes[1]) != 64:
        raise SystemExit("vllm-cpu semantic hash is unavailable")
    environment = service.get("environment")
    if not isinstance(environment, dict) or environment.get("NE_VLLM_HEALTH_MODEL") != "qwen3-0.6b":
        raise SystemExit("vllm-cpu completion model is not the admitted CPU model")
    container_id = _run([*command, "ps", "--quiet", "vllm-cpu"]).strip()
    if not 12 <= len(container_id) <= 64 or any(
        character not in "0123456789abcdef" for character in container_id
    ):
        raise SystemExit("one exact running vllm-cpu container is required")
    runtime_config_label = _run(
        [
            docker,
            "inspect",
            "--format",
            '{{ index .Config.Labels "com.docker.compose.config-hash" }}',
            container_id,
        ]
    ).strip()
    if not 32 <= len(runtime_config_label) <= 128 or any(
        character not in "0123456789abcdef" for character in runtime_config_label
    ):
        raise SystemExit("running vllm-cpu runtime config label is unavailable")
    if _run([*command, "ps", "--quiet", "vllm-cpu"]).strip() != container_id:
        raise SystemExit("vllm-cpu container changed during receipt capture")
    document = {
        "schema": "ne-vllm-cpu-compose-receipt-v1",
        "project_name": args.project_name,
        "working_dir": str(working_dir),
        "env_file": str(env_file),
        "compose_files": [str(path) for path in compose_files],
        "service": "vllm-cpu",
        "semantic_hash": hashes[1],
        # This opaque runtime attestation is intentionally separate from the
        # Compose semantic hash; neither value is interpreted as the other.
        "runtime_config_label": runtime_config_label,
        "image": service["image"],
        "completion_url": "http://127.0.0.1:8091/v1/completions",
        "model": "qwen3-0.6b",
    }
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=args.output.name + ".", dir=args.output.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if Path(temporary).read_bytes() != payload:
            raise OSError("receipt readback mismatch")
        os.replace(temporary, args.output)
        directory = os.open(args.output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


if __name__ == "__main__":
    main()
