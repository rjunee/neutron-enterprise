# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Test helpers for constructing gateway config objects.

``make_model_def`` bounds the fixture churn from the NE-CTXB-2a required-field
change (``context_window``): every ``ModelDef`` a test needs can be built with a
single call and per-field overrides, so a future required field is added in ONE
place, not across every test module. Precedent: ``ne.worker.testing``.

Import directly (``from ne.gateway.testing import make_model_def``) — this module
is deliberately NOT re-exported from ``ne.gateway.__init__``.
"""

from __future__ import annotations

from typing import Any

from ne.contracts import Lane
from ne.gateway.config import ModelDef


def make_model_def(**overrides: Any) -> ModelDef:
    """Build a ``ModelDef`` with sensible PRIVATE_VLLM defaults + overrides.

    ``context_window`` defaults to 32768; ``max_output_tokens`` is left unset so
    the ``ModelDef`` default (``min(4096, context_window // 8)``) fires — pass it
    explicitly to exercise the reserve validator.
    """
    fields: dict[str, Any] = {
        "name": "m1",
        "litellm_model": "hosted_vllm/m1",
        "provider": "hosted_vllm",
        "lane": Lane.PRIVATE_VLLM,
        "api_base": None,
        "extra_params": {},
        "context_window": 32768,
    }
    fields.update(overrides)
    return ModelDef(**fields)
