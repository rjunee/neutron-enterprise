# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Air-gap download tooling: offline env, local layout, pre-stage + manifest,
and the vLLM serve argv. No network — snapshot_download is injected.
"""

from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path
from typing import Any

from ne.serve.download import (
    OFFLINE_ENV,
    download_weights,
    local_path,
    offline_env,
    vllm_serve_args,
)

_RESOLVED_SHA = "a" * 40


def _fake_repo_info(**_kwargs: Any) -> Any:
    return types.SimpleNamespace(
        sha=_RESOLVED_SHA,
        siblings=[
            types.SimpleNamespace(rfilename="model-00001.safetensors"),
            types.SimpleNamespace(rfilename="config.json"),
        ],
    )


def test_offline_env_is_the_four_key_block_and_a_fresh_copy() -> None:
    env = offline_env()
    assert env == {
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "VLLM_NO_USAGE_STATS": "1",
        "DO_NOT_TRACK": "1",
    }
    # Mutating the returned dict must not affect the module constant.
    env["HF_HUB_OFFLINE"] = "0"
    assert OFFLINE_ENV["HF_HUB_OFFLINE"] == "1"
    assert offline_env()["HF_HUB_OFFLINE"] == "1"


def test_local_path_layout() -> None:
    assert local_path("Qwen/Qwen3-0.6B", Path("/w")) == Path("/w/Qwen/Qwen3-0.6B")


def test_download_writes_weights_and_manifest(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_snapshot_download(**kwargs: Any) -> None:
        calls.append(kwargs)
        dest = Path(kwargs["local_dir"])
        # Simulate two downloaded weight shards.
        (dest / "model-00001.safetensors").write_bytes(b"abc")
        (dest / "config.json").write_text("{}", encoding="utf-8")

    result = download_weights(
        "Qwen/Qwen3-0.6B",
        tmp_path,
        revision="main",
        snapshot_download=fake_snapshot_download,
        repo_info=_fake_repo_info,
    )

    # snapshot_download is PINNED to the resolved SHA (not the mutable 'main').
    assert len(calls) == 1
    assert calls[0]["repo_id"] == "Qwen/Qwen3-0.6B"
    assert calls[0]["revision"] == _RESOLVED_SHA
    assert calls[0]["local_dir"] == str(tmp_path / "Qwen" / "Qwen3-0.6B")

    assert result.path == tmp_path / "Qwen" / "Qwen3-0.6B"
    assert result.path.is_dir()
    assert result.manifest_path.is_file()
    assert result.resolved_revision == _RESOLVED_SHA

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 2
    assert manifest["repo_id"] == "Qwen/Qwen3-0.6B"
    assert manifest["revision"] == "main"  # requested revision kept for context
    assert manifest["resolved_revision"] == _RESOLVED_SHA
    assert "downloaded_at" in manifest
    names = {f["name"] for f in manifest["files"]}
    assert names == {"model-00001.safetensors", "config.json"}
    # The manifest excludes itself.
    assert "manifest.json" not in names
    # Sizes and per-file sha256 are recorded.
    by_name = {f["name"]: f for f in manifest["files"]}
    assert by_name["model-00001.safetensors"]["size"] == 3
    assert by_name["model-00001.safetensors"]["sha256"] == hashlib.sha256(b"abc").hexdigest()


def test_vllm_serve_args_points_at_local_weights() -> None:
    args = vllm_serve_args("/models/Qwen/Qwen3-0.6B", "qwen3-0.6b", port=8000)
    assert args[:2] == ["--model", "/models/Qwen/Qwen3-0.6B"]
    assert "--served-model-name" in args
    assert args[args.index("--served-model-name") + 1] == "qwen3-0.6b"
    assert "--structured-outputs-config.backend" in args
    assert args[args.index("--structured-outputs-config.backend") + 1] == "xgrammar"
    assert "--guided-decoding-backend" not in args
    # Safe-by-default loopback bind (NE-013 compose loopback default).
    assert "--host" in args
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert "0.0.0.0" not in args


def test_vllm_serve_args_honors_host_override() -> None:
    args = vllm_serve_args("/models/x", "x", host="10.0.0.7")
    assert args[args.index("--host") + 1] == "10.0.0.7"
