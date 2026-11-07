# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Model registry (SPEC section 5, NE-011): entries tagged license/origin/lane/
capabilities, plus a bridge that constructs NE-010's ``GatewaySettings`` from
registered entries.

The boot-fail-loud-with-actionable-errors pattern is lifted from the ABI
``validate_defaults`` ergonomic (teardown section 3.4): every registration is
validated eagerly and every rejection names the exact offending model_id and
field. The one piece of that pattern deliberately NOT lifted is the silent
any-provider fallback (ABI ``ModelRegistryService.py:107-111``) — an unknown
``model_id`` here always raises :class:`~ne.gateway.errors.UnknownModelIdError`
naming every registered alternative; there is no silent substitution.

This module is pure data + pydantic validation. It NEVER imports ``litellm`` or
any provider SDK (C3 model independence) — litellm model strings are handled as
opaque data, only ever assembled into a :class:`~ne.gateway.config.ModelDef`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ne.contracts import Lane, ModelEntry
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute
from ne.gateway.errors import RegistryValidationError, UnknownModelIdError
from ne.gateway.ssrf import validate_endpoint_url

# -- known/banned classes ---------------------------------------------------

KNOWN_MODEL_LICENSES = frozenset({"MIT", "Apache-2.0", "Modified-MIT", "proprietary-api"})
"""``Modified-MIT`` is the Kimi K2.x caveat class (research brief);
``proprietary-api`` covers frontier-API models whose weights are not
distributed."""

BANNED_MODEL_LICENSES = frozenset(
    {
        "cc-by-nc",
        "cc-by-nc-4.0",
        "cc-by-nc-sa",
        "mrl",
        "research-only",
        "llama-4-community",
    }
)
"""Checked case-insensitively. C4: non-commercial (NC) model weights are
banned; Llama 4 is excluded per SPEC section 3.3."""

KNOWN_ORIGINS = frozenset({"CN", "US", "FR", "EU", "GB", "CA", "DE"})
"""Uppercase ISO-3166 alpha-2 of the model developer's HQ."""

WESTERN_ORIGINS = KNOWN_ORIGINS - {"CN"}

KNOWN_CAPABILITIES = frozenset(
    {"streaming", "structured-output", "tool-use", "reasoning", "vision", "long-context"}
)


def validate_entry(entry: ModelEntry) -> None:
    """Validate a single registry entry, raising ``RegistryValidationError``
    with a message that names the offending model_id, the bad field, and how
    to fix it.
    """
    model_id = entry.model_id.strip()
    if not model_id:
        raise RegistryValidationError(
            f"model {entry.model_id!r}: model_id must be a non-empty, non-whitespace string"
        )

    if not entry.license:
        raise RegistryValidationError(
            f"model {entry.model_id!r}: license must be a non-empty string; "
            f"known licenses: {sorted(KNOWN_MODEL_LICENSES)!r}"
        )

    if entry.license.lower() in BANNED_MODEL_LICENSES:
        raise RegistryValidationError(
            f"model {entry.model_id!r}: license {entry.license!r} is in the "
            "banned class (C4: non-commercial/research-restricted model "
            f"weights are banned); banned: {sorted(BANNED_MODEL_LICENSES)!r}"
        )

    if entry.license not in KNOWN_MODEL_LICENSES:
        raise RegistryValidationError(
            f"model {entry.model_id!r}: license {entry.license!r} is not a "
            f"known license class; known: {sorted(KNOWN_MODEL_LICENSES)!r}"
        )

    if entry.origin not in KNOWN_ORIGINS:
        raise RegistryValidationError(
            f"model {entry.model_id!r}: origin {entry.origin!r} is not a "
            f"known origin; known origins: {sorted(KNOWN_ORIGINS)!r}"
        )

    if len(entry.lanes) == 0:
        raise RegistryValidationError(
            f"model {entry.model_id!r}: must declare at least one lane (lanes is empty)"
        )

    if entry.license == "proprietary-api" and Lane.PRIVATE_VLLM in entry.lanes:
        raise RegistryValidationError(
            f"model {entry.model_id!r}: weights-restricted model (license "
            "'proprietary-api') cannot serve on the private-vllm lane; "
            "remove Lane.PRIVATE_VLLM from lanes"
        )

    if len(entry.capabilities) == 0:
        raise RegistryValidationError(
            f"model {entry.model_id!r}: must declare at least one capability; "
            f"known capabilities: {sorted(KNOWN_CAPABILITIES)!r}"
        )

    for cap in entry.capabilities:
        if cap not in KNOWN_CAPABILITIES:
            raise RegistryValidationError(
                f"model {entry.model_id!r}: unknown capability {cap!r}; "
                f"known capabilities: {sorted(KNOWN_CAPABILITIES)!r}"
            )

    if entry.context_window is None or entry.context_window <= 0:
        raise RegistryValidationError(
            f"model {entry.model_id!r}: context_window is required and must "
            f"be positive (got {entry.context_window!r})"
        )


class InMemoryModelRegistry:
    """Structurally implements the frozen ``ne.contracts.ModelRegistry``
    Protocol. Every ``register()`` call validates the entry eagerly (fail-loud
    at registration, never at lookup).
    """

    def __init__(self, entries: Iterable[ModelEntry] = ()) -> None:
        self._entries: dict[str, ModelEntry] = {}
        for entry in entries:
            self.register(entry)

    def register(self, entry: ModelEntry) -> None:
        validate_entry(entry)
        if entry.model_id in self._entries:
            raise RegistryValidationError(
                f"model {entry.model_id!r} is already registered; model_id must be unique"
            )
        self._entries[entry.model_id] = entry

    def get(self, model_id: str) -> ModelEntry:
        try:
            return self._entries[model_id]
        except KeyError:
            raise UnknownModelIdError(model_id, list(self._entries.keys())) from None

    def list_models(
        self, *, lane: Lane | None = None, capability: str | None = None
    ) -> tuple[ModelEntry, ...]:
        result = []
        for entry in self._entries.values():
            if lane is not None and lane not in entry.lanes:
                continue
            if capability is not None and capability not in entry.capabilities:
                continue
            result.append(entry)
        return tuple(result)

    def filter_models(
        self,
        *,
        lane: Lane | None = None,
        capability: str | None = None,
        origin: str | None = None,
        license: str | None = None,
    ) -> tuple[ModelEntry, ...]:
        """Procurement-oriented filtering (origin/license), kept OFF the
        frozen ``list_models`` signature so Protocol conformance is never at
        risk of accidental widening.
        """
        result = []
        for entry in self._entries.values():
            if lane is not None and lane not in entry.lanes:
                continue
            if capability is not None and capability not in entry.capabilities:
                continue
            if origin is not None and entry.origin != origin:
                continue
            if license is not None and entry.license != license:
                continue
            result.append(entry)
        return tuple(result)


# -- registry -> gateway bridge ----------------------------------------------


class ModelBinding(BaseModel):
    """Deployment-specific wiring for a registered model: the litellm target
    string, optional api_base, and extra litellm params."""

    model_config = ConfigDict(frozen=True)

    litellm_model: str
    api_base: str | None = None
    extra_params: dict[str, Any] = Field(default_factory=dict)
    context_window: int | None = None
    """Deployment override for the serving window. May only NARROW the registry
    entry's ``context_window`` — the served deployment can be smaller than the
    registry entry (e.g. a vLLM ``--max-model-len`` below the model card), never
    larger. ``None`` inherits the entry's value."""
    max_output_tokens: int | None = None
    """Deployment override for the output reserve. ``None`` lets the
    ``ModelDef`` default (``min(4096, context_window // 8)``) fire."""


def default_vllm_litellm_model(entry: ModelEntry) -> str:
    """The conventional litellm target string for a self-hosted vLLM entry."""
    return f"hosted_vllm/{entry.model_id}"


def entry_to_model_def(
    entry: ModelEntry,
    *,
    litellm_model: str,
    lane: Lane | None = None,
    api_base: str | None = None,
    extra_params: dict[str, Any] | None = None,
    context_window: int | None = None,
    max_output_tokens: int | None = None,
) -> ModelDef:
    """Construct a gateway ``ModelDef`` from a registry entry plus deployment
    binding details. ``lane`` defaults to the entry's first registered lane;
    an explicit lane must be one of the entry's registered lanes.

    The entry's ``context_window`` is threaded onto the ``ModelDef`` (NE-CTXB-2a;
    previously dropped). A ``context_window`` binding override may only NARROW —
    ``0 < override <= entry.context_window`` — a larger override is rejected
    (the served deployment can never exceed the registered window).
    """
    resolved_lane = entry.lanes[0] if lane is None else lane
    if resolved_lane not in entry.lanes:
        raise RegistryValidationError(
            f"model {entry.model_id!r}: requested lane {resolved_lane!r} is "
            f"not in its registered lanes {entry.lanes!r}"
        )
    # entry.context_window is typed int | None on the contracts ModelEntry;
    # mirror validate_entry's fail-loud check so the required ModelDef field is
    # never fed None/<=0 (satisfies pyright's narrowing too).
    if entry.context_window is None or entry.context_window <= 0:
        raise RegistryValidationError(
            f"model {entry.model_id!r}: context_window is required and must be "
            f"positive (got {entry.context_window!r})"
        )
    resolved_context_window = entry.context_window
    if context_window is not None:
        if not (0 < context_window <= entry.context_window):
            raise RegistryValidationError(
                f"model {entry.model_id!r}: context_window override "
                f"{context_window} may only NARROW the registry entry's "
                f"{entry.context_window} (must be 0 < override <= entry value)"
            )
        resolved_context_window = context_window
    if api_base is not None:
        # Registration-time SSRF validation (NE-012): reject metadata IPs,
        # credentials-in-URL, query/fragment, etc. lane-aware (private-vllm may
        # legitimately point at an in-VPC/private endpoint; frontier must be
        # https and non-private). UnsafeEndpointError propagates to the caller.
        validate_endpoint_url(
            api_base,
            require_https=(resolved_lane is Lane.FRONTIER_API),
            allow_private=(resolved_lane is Lane.PRIVATE_VLLM),
        )
    return ModelDef(
        name=entry.model_id,
        provider=entry.provider,
        lane=resolved_lane,
        litellm_model=litellm_model,
        api_base=api_base,
        extra_params=extra_params or {},
        context_window=resolved_context_window,
        # Only pass max_output_tokens through when overridden; otherwise let the
        # ModelDef default (min(4096, cw // 8)) fire.
        **({"max_output_tokens": max_output_tokens} if max_output_tokens is not None else {}),
    )


def build_gateway_settings(
    registry: InMemoryModelRegistry,
    *,
    bindings: Mapping[str, ModelBinding],
    tasks: Mapping[str, TaskRoute],
    **settings_kwargs: Any,
) -> GatewaySettings:
    """Construct a ``GatewaySettings`` from registered entries + deployment
    bindings. Each binding key is looked up in the registry (an unknown key
    raises ``UnknownModelIdError``, actionable by construction). NE-010's own
    boot validator then enforces task -> model references with its own
    exact-key-path errors; this function does not duplicate that validation.
    """
    models = []
    for model_id, binding in bindings.items():
        entry = registry.get(model_id)
        models.append(
            entry_to_model_def(
                entry,
                litellm_model=binding.litellm_model,
                api_base=binding.api_base,
                extra_params=dict(binding.extra_params),
                context_window=binding.context_window,
                max_output_tokens=binding.max_output_tokens,
            )
        )
    return GatewaySettings(models=models, tasks=dict(tasks), **settings_kwargs)
