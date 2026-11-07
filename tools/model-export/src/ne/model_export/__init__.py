# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne-model-export`` — isolated, hash-pinned build-time ONNX export +
parity-gate tool (NE-TEI-ONNX).

The heavy torch/optimum-onnx/onnxruntime/transformers/sentence-transformers
closure lives ONLY in ``export_env/`` (a hash-pinned sidecar venv built by
``ne.envbuild.ensure_sidecar_venv``) — this package's own declared dependency
is exactly ``ne-envbuild`` (see ``tests/test_env_policy.py``, the C11
isolation guardian). See ``tools/model-export/README.md`` for the two-phase
split (this build vs. the orchestrator's post-merge heavy export run).
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
