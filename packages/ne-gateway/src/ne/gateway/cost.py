# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Cost sinks and litellm response extraction helpers.

A :class:`CostSink` is where the gateway hands a fully-built ``CostEvent``. The
production sink, :class:`OutboxCostSink`, writes it through the ne-events
Postgres outbox in the same transaction as the caller's connection open — the
outbox pattern (C8 event spine). The ``extract_*`` helpers pull token/cost
accounting off a litellm ``ModelResponse`` defensively: they NEVER raise, so a
provider that omits usage or an unknown model that yields ``0.0`` cost degrades
to ``None``/``0.0`` rather than failing an otherwise-successful call.
"""

from __future__ import annotations

from typing import Any, Protocol

import sqlalchemy as sa

from ne.events import append
from ne.gateway._litellm import litellm
from ne.schema import CostEvent


class CostSink(Protocol):
    """Where the gateway emits a :class:`CostEvent` per successful call."""

    def emit(self, event: CostEvent) -> None: ...


class OutboxCostSink:
    """A :class:`CostSink` that writes each event through the ne-events outbox."""

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    def emit(self, event: CostEvent) -> None:
        with self._engine.begin() as conn:
            append(conn, event)


def extract_usage(response: Any) -> tuple[int | None, int | None]:
    """Return ``(prompt_tokens, completion_tokens)`` from a response, or ``None``
    for either field that is absent. Never raises."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return (None, None)
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    return (prompt, completion)


def extract_cost(response: Any) -> float | None:
    """Return the response cost in USD, or ``None`` if it cannot be determined.

    ``0.0`` is a legitimate value (mock / unknown-in-cost-map models) and is
    returned as-is. Never raises.
    """
    hidden = getattr(response, "_hidden_params", None)
    if isinstance(hidden, dict):
        cost = hidden.get("response_cost")
        if cost is not None:
            return cost
    try:
        return litellm.completion_cost(completion_response=response)
    except Exception:
        return None
