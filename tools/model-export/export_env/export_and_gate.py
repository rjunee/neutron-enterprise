# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Model-export + build-time parity-gate worker — runs INSIDE the isolated
hash-pinned ``export_env`` venv (``tools/model-export/.export-venv``).

This module imports ``torch``/``optimum``/``onnxruntime``/``transformers``/
``sentence_transformers``, which are deliberately NOT part of the platform
virtualenv (SPEC §11 heavy-ML-closure principle, C4) — confined here and
driven as a subprocess by ``ne.model_export.cli`` (the platform-side,
dep-less orchestrator), exactly mirroring the
``ne.govern.redaction.presidio_backend`` / ``presidio_env/redact_text.py``
isolation pattern.

Protocol: writes a single ``build-report.json`` into ``--out-dir`` plus the
exported ``onnx/model.onnx`` + ``onnx/model.onnx_data`` pair. The platform-side
``ne.model_export.cli`` reads the report and — ONLY when
``report["parity"]["passed"]`` is true — builds + commits the export-manifest,
golden-vectors file, and the ``.tar.gz`` artifact. This script NEVER writes
outside ``--out-dir``; it is the truth gate (§0-law-5), not the artifact
publisher.

Steps:
1. Resolve ``--revision`` to an immutable commit SHA via ``HfApi().repo_info``
   and ``snapshot_download`` the pinned rev (never the mutable ref).
2. Export to ONNX via ``optimum.exporters.onnx.main_export`` (task
   ``feature-extraction``, fp32, external data — the 2.3 GB fp32 weight size
   exceeds protobuf's 2 GiB inline limit, so external data is mandatory, not a
   style choice) into ``<out-dir>/onnx/``.
3. Compute the REFERENCE (golden) embeddings with the safetensors checkpoint
   via ``sentence_transformers.SentenceTransformer`` (L2-normalized) — these
   become the committed golden VECTORS.
4. Re-embed the same texts through the exported ONNX graph under
   ``onnxruntime``, replicating TEI's own semantics in numpy: right-padded
   last-token pooling on ``last_hidden_state``, then L2 normalize.
5. Enforce per-pair cosine >= ``--threshold`` across every golden text; write
   ``build-report.json`` with the outcome (``passed`` — never silently true).
   A failing gate still exits non-zero (the truth gate never emits a report
   claiming success).
"""

# pyright: reportMissingImports=false, reportMissingModuleSource=false
from __future__ import annotations

import argparse
import json
import math
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, TypeGuard

_TOOLCHAIN_PACKAGES = (
    "torch",
    "transformers",
    "optimum",
    "optimum-onnx",
    "onnxruntime",
    "onnx",
    "sentence-transformers",
    "huggingface-hub",
)


def _toolchain_versions() -> dict[str, str]:
    """Best-effort ``name -> installed version`` for the sidecar closure.

    A package that resolved transitively but isn't independently distributed
    under exactly this name (rare) is simply omitted rather than failing the
    whole build — the manifest's toolchain map is provenance context, not a
    load-bearing gate (the gate is the cosine threshold).
    """
    versions: dict[str, str] = {}
    for name in _TOOLCHAIN_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return versions


def _is_resolved_sha(value: Any) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(c in "0123456789abcdef" for c in value.lower())
    )


def _load_texts(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    texts = data["texts"] if isinstance(data, dict) else data
    if not isinstance(texts, list) or not texts:
        raise ValueError(f"{path} must contain a non-empty list of golden texts")
    return [str(t) for t in texts]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        raise ValueError("zero-norm vector — cannot compute cosine similarity")
    return dot / (norm_a * norm_b)


def _resolve_revision(repo_id: str, revision: str) -> str:
    from huggingface_hub import HfApi

    info = HfApi().repo_info(repo_id=repo_id, revision=revision)
    resolved = getattr(info, "sha", None)
    if not _is_resolved_sha(resolved):
        raise RuntimeError(f"could not resolve {repo_id}@{revision} to an immutable commit SHA")
    return resolved


def _download_checkpoint(repo_id: str, resolved_revision: str, dest: Path) -> Path:
    from huggingface_hub import snapshot_download

    local_dir = snapshot_download(repo_id=repo_id, revision=resolved_revision, local_dir=str(dest))
    return Path(local_dir)


def _export_onnx(checkpoint_dir: Path, onnx_dir: Path) -> None:
    """Export ``checkpoint_dir`` to fp32 ONNX with external data (task
    ``feature-extraction``) via optimum-onnx's exporter entrypoint."""
    from optimum.exporters.onnx import main_export

    onnx_dir.mkdir(parents=True, exist_ok=True)
    main_export(
        model_name_or_path=str(checkpoint_dir),
        output=str(onnx_dir),
        task="feature-extraction",
        no_post_process=True,
    )
    # optimum names the graph model.onnx by default; external data is written
    # alongside automatically once the graph would exceed the 2 GiB protobuf
    # inline limit (fp32 Qwen3-Embedding-0.6B always does). Nothing further to
    # rename here; the layout contract (model.onnx / model.onnx_data) is
    # asserted by the caller before the parity gate runs.
    onnx_path = onnx_dir / "model.onnx"
    if not onnx_path.is_file():
        raise RuntimeError(f"optimum export did not produce {onnx_path}")


def _reference_embeddings(checkpoint_dir: Path, texts: list[str]) -> list[list[float]]:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(str(checkpoint_dir))
    vectors = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
    return [v.tolist() for v in vectors]


def _onnx_embeddings(checkpoint_dir: Path, onnx_path: Path, texts: list[str]) -> list[list[float]]:
    """Replicate TEI's ort-backend semantics: right-padded last-token pooling
    over ``last_hidden_state``, then L2 normalize — in numpy, over the
    exported graph."""
    import numpy as np
    import onnxruntime as ort
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(checkpoint_dir))
    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    input_names = {i.name for i in session.get_inputs()}

    vectors: list[list[float]] = []
    for text in texts:
        encoded = tokenizer(text, return_tensors="np", padding=True)
        feed: dict[str, Any] = {
            "input_ids": encoded["input_ids"].astype(np.int64),
            "attention_mask": encoded["attention_mask"].astype(np.int64),
        }
        if "token_type_ids" in input_names and "token_type_ids" in encoded:
            feed["token_type_ids"] = encoded["token_type_ids"].astype(np.int64)
        outputs = session.run(None, {k: v for k, v in feed.items() if k in input_names})
        output_names = [o.name for o in session.get_outputs()]
        # The per-token hidden states: a raw `transformers` ONNX export names this
        # "last_hidden_state", but the sentence-transformers export path (used here,
        # via the Pooling module) names the same [batch, seq, hidden] tensor
        # "token_embeddings". Accept either; fail LOUD with the actual names if the
        # export ever changes shape, rather than crashing on a hardcoded key.
        hidden_name = next(
            (n for n in ("last_hidden_state", "token_embeddings") if n in output_names),
            None,
        )
        if hidden_name is None:
            raise RuntimeError(
                "ONNX export exposes no per-token hidden-state output "
                f"(expected 'last_hidden_state' or 'token_embeddings'); got {output_names}"
            )
        # session.run's declared return type includes SparseTensor (which doesn't
        # support fancy indexing); the ort backend always returns a dense ndarray
        # for the hidden state, so treat it as one here.
        last_hidden_state: Any = np.asarray(outputs[output_names.index(hidden_name)])

        attention_mask = feed["attention_mask"][0]
        # Right-padded last-token pool: the last non-pad position.
        last_idx = int(attention_mask.sum() - 1)
        pooled = last_hidden_state[0, last_idx, :]
        norm = np.linalg.norm(pooled)
        normalized = pooled / norm if norm > 0 else pooled
        vectors.append(normalized.astype(np.float64).tolist())
    return vectors


def _run(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    onnx_dir = out_dir / "onnx"
    checkpoint_dir = out_dir / "checkpoint"

    texts = _load_texts(Path(args.texts_file))
    resolved_revision = _resolve_revision(args.repo_id, args.revision)
    checkpoint = _download_checkpoint(args.repo_id, resolved_revision, checkpoint_dir)
    _export_onnx(checkpoint, onnx_dir)

    reference = _reference_embeddings(checkpoint, texts)
    exported = _onnx_embeddings(checkpoint, onnx_dir / "model.onnx", texts)

    for vec in (*reference, *exported):
        if len(vec) != args.dim:
            raise RuntimeError(f"embedding dim {len(vec)} != expected {args.dim}")

    cosines = [_cosine(r, e) for r, e in zip(reference, exported, strict=True)]
    min_cosine = min(cosines)
    passed = min_cosine >= args.threshold

    return {
        "model_key": args.model_key,
        "resolved_revision": resolved_revision,
        "toolchain": _toolchain_versions(),
        "parity": {
            "golden_count": len(texts),
            "min_cosine": min_cosine,
            "threshold": args.threshold,
            "passed": passed,
        },
        "goldens": {
            "texts": texts,
            "vectors": reference,
            "dim": args.dim,
            "source_revision": resolved_revision,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export + build-time parity-gate an embedding model to ONNX (in-venv worker)."
    )
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--model-key", default="")
    parser.add_argument("--texts-file", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--dim", type=int, required=True)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument(
        "--cross-check-community",
        action="store_true",
        help="informational-only cross-check against onnx-community's export; gates nothing.",
    )
    args = parser.parse_args(argv)

    report = _run(args)
    report_path = Path(args.out_dir) / "build-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not report["parity"]["passed"]:
        print(
            f"PARITY GATE FAILED: min_cosine={report['parity']['min_cosine']!r} < "
            f"threshold={args.threshold!r} — refusing artifact emission (see {report_path})",
            file=sys.stderr,
        )
        return 1
    print(
        f"parity gate PASSED (min_cosine={report['parity']['min_cosine']!r}); report: {report_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
