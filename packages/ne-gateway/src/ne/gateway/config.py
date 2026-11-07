# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Gateway settings — the boot-time model/task routing configuration.

Validation is fail-loud at construction: every reference from a task route to a
model name is checked, so a typo in ``tasks.<task>.model`` is rejected at boot
with the exact offending key path, never discovered as a silent mis-route at
call time.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import SettingsConfigDict

from ne.contracts import Lane
from ne.contracts.config import NeSettings
from ne.contracts.tokens import (
    DEFAULT_BYTES_PER_TOKEN,
    DEFAULT_PER_CALL_OVERHEAD,
    ByteTokenEstimator,
    ContextBudget,
)
from ne.gateway.errors import GatewayConfigError


def overhead_margin_for(context_window: int) -> int:
    """The per-call overhead margin for a serving window (design §2.2).

    ``max(1024, ceil(0.05 * context_window))`` — a floor of 1024 tokens plus a
    5% headroom that grows with the window. Owned here as the SINGLE margin
    policy so runtime trim (NE-CTXB-3) and gateway pre-flight derive the same
    number from the same window."""
    return max(1024, math.ceil(0.05 * context_window))


class ModelDef(BaseModel):
    """A single routable model: its gateway alias, litellm target, and lane.

    ``extra="forbid"``: an unknown key in ``NE_GATEWAY_MODELS`` env JSON (e.g. a
    typo'd ``context_windw``) now raises a LOUD boot error instead of being
    silently dropped — the fix for the observed silent-drop bug class (design
    §3.1). ``context_window`` is REQUIRED so a producer that forgets it cannot
    boot at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    """The routing alias — what ``tasks.<task>.model`` and requests reference."""
    litellm_model: str
    """The litellm provider/model string (e.g. ``anthropic/claude-...``)."""
    provider: str
    lane: Lane
    api_base: str | None = None
    extra_params: dict[str, Any] = Field(default_factory=dict)
    """Extra ``litellm_params`` merged into the Router entry (e.g. ``api_key``)."""
    context_window: int = Field(gt=0)
    """The SERVING window (e.g. vLLM ``--max-model-len``), NOT the model-card
    max — the RunPod incident proved the card value is wrong at runtime. Budget
    pre-flight and runtime trim both derive from this."""
    max_output_tokens: int = Field(default=4096, gt=0)
    """Output reserve for this model. When OMITTED it is computed by the
    ``mode="before"`` validator as ``min(4096, context_window // 8)``; a supplied
    value must be ``< context_window // 2``. The static ``4096`` default is a
    type-level placeholder only — it is reached solely when ``context_window`` is
    absent, in which case the required-``context_window`` error fires first, so
    it never silently applies to a validly-constructed model."""

    @model_validator(mode="before")
    @classmethod
    def _default_max_output_tokens(cls, data: Any) -> Any:
        # Only fill the default when max_output_tokens is absent/None AND a
        # positive context_window is present. A missing context_window is left
        # alone so the required-field error fires LOUDLY (design §3.1).
        if not isinstance(data, Mapping):
            return data
        if data.get("max_output_tokens") is not None:
            return data
        raw_cw = data.get("context_window")
        try:
            cw = int(raw_cw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return data
        if cw <= 0:
            return data
        return {**data, "max_output_tokens": min(4096, cw // 8)}

    @model_validator(mode="after")
    def _max_output_tokens_leaves_room(self) -> ModelDef:
        if self.max_output_tokens >= self.context_window // 2:
            # Counts only (C12): no free text beyond the two numbers.
            raise ValueError(
                f"max_output_tokens={self.max_output_tokens} must be < "
                f"context_window // 2 = {self.context_window // 2}"
            )
        return self


class TaskRoute(BaseModel):
    """A per-task route: a primary model plus an ordered fallback chain."""

    model_config = ConfigDict(frozen=True)

    model: str
    fallbacks: tuple[str, ...] = ()
    params: dict[str, Any] = Field(default_factory=dict)


class GatewaySettings(NeSettings):
    """Layered gateway configuration.

    ``models`` can be authored directly in ``ne.toml`` / ``NE_GATEWAY_`` env, or
    constructed from the model registry via
    :func:`ne.gateway.registry.build_gateway_settings` (the registry is the
    source of truth for model identity/tags; NE-011).
    """

    model_config = SettingsConfigDict(
        env_prefix="NE_GATEWAY_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    models: list[ModelDef] = Field(default_factory=list)
    tasks: dict[str, TaskRoute] = Field(default_factory=dict)
    num_retries: int = 2
    timeout_seconds: float = 120.0
    bytes_per_token: float = DEFAULT_BYTES_PER_TOKEN
    """Estimator divisor (env ``NE_GATEWAY_BYTES_PER_TOKEN``). SINGLE OWNERSHIP
    per design §2.3: both the gateway pre-flight and the runtime trim
    (NE-CTXB-3, threaded via the platform) derive their estimator from this one
    value. Defaults to the empirically-corrected
    :data:`ne.contracts.tokens.DEFAULT_BYTES_PER_TOKEN` — never the design
    prose's original 3.0."""
    per_call_overhead: int = DEFAULT_PER_CALL_OVERHEAD
    """Estimator per-call overhead in tokens (env
    ``NE_GATEWAY_PER_CALL_OVERHEAD``). Single ownership, see
    :attr:`bytes_per_token`."""

    def estimator(self) -> ByteTokenEstimator:
        """The configured token estimator (single source, design §2.3)."""
        return ByteTokenEstimator(
            bytes_per_token=self.bytes_per_token,
            per_call_overhead=self.per_call_overhead,
        )

    def context_budget_for(self, model: ModelDef) -> ContextBudget:
        """The :class:`ContextBudget` for a model's serving window (design §2.2).

        Raises ``ValueError`` (from ``ContextBudget``) if the output reserve +
        overhead margin swallow the whole window — surfaced as a
        ``GatewayConfigError`` by :meth:`_validate_routing` at boot."""
        return ContextBudget(
            context_window=model.context_window,
            max_output_tokens=model.max_output_tokens,
            overhead_margin=overhead_margin_for(model.context_window),
        )

    @model_validator(mode="after")
    def _validate_routing(self) -> GatewaySettings:
        # Raise GatewayConfigError directly: pydantic v2 propagates non-ValueError
        # exceptions from after-validators unwrapped (do NOT raise ValueError).
        if len(self.models) < 1:
            raise GatewayConfigError("models: at least one model must be configured")

        seen: dict[str, int] = {}
        for j, m in enumerate(self.models):
            if m.name in seen:
                raise GatewayConfigError(
                    f"models[{j}].name {m.name!r} duplicates models[{seen[m.name]}].name"
                )
            seen[m.name] = j

        names = sorted(seen)
        for k, route in self.tasks.items():
            if route.model not in seen:
                raise GatewayConfigError(
                    f"tasks.{k}.model {route.model!r} does not name a configured "
                    f"model; configured models: {names!r}"
                )
            for i, fb in enumerate(route.fallbacks):
                if fb not in seen:
                    raise GatewayConfigError(
                        f"tasks.{k}.fallbacks[{i}] {fb!r} does not name a configured "
                        f"model; configured models: {names!r}"
                    )

        # Fail-loud boot: a serving window too small to hold its own output
        # reserve + overhead margin has a non-positive input budget and can
        # never serve a single token of prompt (design §2.2). Surface the
        # ContextBudget ValueError as a GatewayConfigError with the model index.
        for j, m in enumerate(self.models):
            try:
                self.context_budget_for(m)
            except ValueError as e:
                raise GatewayConfigError(f"models[{j}] ({m.name!r}): {e}") from e
        return self


TOOL_CATALOG_FLOOR_TOKENS = 512
"""Minimum prompt headroom (beyond the tool catalog) every task's primary
model must leave for the user turn + system prompt (design §2.2 boot check)."""


def validate_tool_catalog_budgets(
    settings: GatewaySettings,
    tool_catalog_by_task: Mapping[str, str],
) -> None:
    """Fail-loud boot check: every task's tool catalog + a floor of prompt
    headroom must fit the PRIMARY model's input budget (design §2.2 / R1-P1c).

    This is what protects a small-window CPU rig from a "budget smaller than the
    catalog" silent failure. It is called by the composition root (NE-CTXB-3
    wires it into ``ne.api.platform``); this unit ships and tests the function
    but does NOT itself call it — so it is intentionally not-yet-referenced in
    production, not dead code.

    C12: the error carries COUNTS ONLY (task/model name + three token numbers),
    never the catalog text.
    """
    by_name = {m.name: m for m in settings.models}
    estimator = settings.estimator()
    for task, route in settings.tasks.items():
        primary = by_name[route.model]
        catalog = tool_catalog_by_task.get(task, "")
        catalog_tokens = estimator.estimate(catalog)
        input_budget = settings.context_budget_for(primary).input_budget
        if catalog_tokens + TOOL_CATALOG_FLOOR_TOKENS > input_budget:
            raise GatewayConfigError(
                f"task {task!r}: tool catalog does not fit the primary model "
                f"{primary.name!r} input budget: "
                f"catalog_tokens={catalog_tokens} "
                f"floor={TOOL_CATALOG_FLOOR_TOKENS} "
                f"input_budget={input_budget}"
            )
