# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ONE GenAI OpenTelemetry semconv wrap point (SPEC §3.3 obs row, §10 risk).

OpenTelemetry's GenAI semantic conventions are still in development — they were
moved out of the main ``semantic-conventions`` repo into the dedicated
``open-telemetry/semantic-conventions-genai`` repo, which has NO releases yet, so
the ``gen_ai.*`` attribute names still ship (as incubating constants) inside
``opentelemetry-semantic-conventions`` but are subject to churn. Per SPEC §10 the
package is EXACT-pinned (``==0.64b0``) AND wrapped: this module owns its OWN
stable literal attribute names, and NO other module may import
``opentelemetry.semconv`` directly. A rename in a future semconv release is caught
loudly by ``tests/test_semconv_pin.py`` rather than silently changing our spans.

We use ``gen_ai.provider.name`` (present since 0.57b0), NOT the deprecated
``gen_ai.system``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

from ne.contracts import get_config_hash

# --- OUR stable attribute literals (the wrap boundary) -----------------------
# Standard OTel GenAI semconv keys (cross-checked against the pinned package in
# tests/test_semconv_pin.py):
ATTR_OPERATION_NAME = "gen_ai.operation.name"
ATTR_PROVIDER_NAME = "gen_ai.provider.name"
ATTR_REQUEST_MODEL = "gen_ai.request.model"
ATTR_RESPONSE_MODEL = "gen_ai.response.model"
ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

# Platform-specific extras (NOT semconv — our own namespace):
ATTR_NE_CONFIG_HASH = "ne.config_hash"
ATTR_NE_KPI_TAG = "ne.kpi_tag"
ATTR_NE_LANE = "ne.lane"
ATTR_NE_COST_USD = "ne.cost_usd"

AttrValue = str | int | float


def genai_attributes(
    *,
    operation: str,
    provider: str | None = None,
    request_model: str | None = None,
    response_model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    lane: str | None = None,
    cost_usd: float | None = None,
    kpi_tag: str | None = None,
    config_hash: str | None = None,
) -> dict[str, AttrValue]:
    """Build the OTel attribute dict for a GenAI operation.

    ``None`` entries are omitted. ``config_hash`` defaults to the frozen
    process-start hash via :func:`ne.contracts.get_config_hash` — stamping is
    non-optional (SPEC §4), so ``ConfigHashNotFrozenError`` propagates rather than
    an unstamped span being produced.
    """
    resolved_hash = config_hash if config_hash is not None else get_config_hash()
    attrs: dict[str, AttrValue] = {
        ATTR_OPERATION_NAME: operation,
        ATTR_NE_CONFIG_HASH: resolved_hash,
    }
    optional: dict[str, AttrValue | None] = {
        ATTR_PROVIDER_NAME: provider,
        ATTR_REQUEST_MODEL: request_model,
        ATTR_RESPONSE_MODEL: response_model,
        ATTR_INPUT_TOKENS: input_tokens,
        ATTR_OUTPUT_TOKENS: output_tokens,
        ATTR_NE_LANE: lane,
        ATTR_NE_COST_USD: cost_usd,
        ATTR_NE_KPI_TAG: kpi_tag,
    }
    for key, value in optional.items():
        if value is not None:
            attrs[key] = value
    return attrs


@contextmanager
def genai_span(
    tracer: Tracer,
    name: str,
    *,
    operation: str,
    provider: str | None = None,
    request_model: str | None = None,
    response_model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    lane: str | None = None,
    cost_usd: float | None = None,
    kpi_tag: str | None = None,
    config_hash: str | None = None,
) -> Iterator[None]:
    """Open a CLIENT span for a GenAI operation with the standard attributes.

    On an exception inside the ``with`` block the exception is recorded, the span
    status is set to ERROR, and the exception re-raises.
    """
    attrs = genai_attributes(
        operation=operation,
        provider=provider,
        request_model=request_model,
        response_model=response_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        lane=lane,
        cost_usd=cost_usd,
        kpi_tag=kpi_tag,
        config_hash=config_hash,
    )
    with tracer.start_as_current_span(name, kind=SpanKind.CLIENT, attributes=attrs) as span:
        try:
            yield
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
