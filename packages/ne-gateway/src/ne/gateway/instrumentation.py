# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Post-hoc GenAI span enrichment for the gateway (NE-014).

The :func:`ne.obs.genai_span` wrap opens each gateway span with the attributes
known at call-open time (operation / provider / request_model / lane / kpi_tag /
config_hash). This module layers on the attributes only knowable *after* the
provider call returns — task, wall-clock latency, the response model, and token
usage — WITHOUT re-pinning the GenAI semconv or duplicating any ``ne.obs``
constant: the four post-hoc GenAI keys are consumed straight from :mod:`ne.obs`;
only the two gateway-local, NON-semconv extras (``ne.task`` and
``ne.gateway.latency_ms``) are defined here.

Imports ONLY ``opentelemetry.trace`` and ``ne.obs`` — no ``litellm`` (C3).
"""

from __future__ import annotations

from opentelemetry.trace import Span

from ne.obs import (
    ATTR_INPUT_TOKENS,
    ATTR_NE_COST_USD,
    ATTR_OUTPUT_TOKENS,
    ATTR_RESPONSE_MODEL,
)

# Gateway-local, NON-semconv extras (do NOT collide with any ne.obs constant):
ATTR_NE_TASK = "ne.task"
ATTR_NE_LATENCY_MS = "ne.gateway.latency_ms"


def annotate_span_after_call(
    span: Span,
    *,
    task: str,
    latency_ms: float,
    response_model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """Set the post-call span attributes on ``span``.

    ``task`` and ``latency_ms`` are set unconditionally; the four GenAI-usage
    attributes are set ONLY when their kwarg is not ``None`` (mirroring
    :func:`ne.obs.genai_attributes`' own None-omission convention).
    """
    span.set_attribute(ATTR_NE_TASK, task)
    span.set_attribute(ATTR_NE_LATENCY_MS, latency_ms)
    if response_model is not None:
        span.set_attribute(ATTR_RESPONSE_MODEL, response_model)
    if input_tokens is not None:
        span.set_attribute(ATTR_INPUT_TOKENS, input_tokens)
    if output_tokens is not None:
        span.set_attribute(ATTR_OUTPUT_TOKENS, output_tokens)
    if cost_usd is not None:
        span.set_attribute(ATTR_NE_COST_USD, cost_usd)
