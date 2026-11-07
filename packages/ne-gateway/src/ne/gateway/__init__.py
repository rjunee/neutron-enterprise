# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Model gateway: all frontier calls route here.

A single litellm ``Router`` is embedded behind the frozen
:class:`ne.contracts.ModelGateway` seam. ``litellm`` itself is imported ONLY in
:mod:`ne.gateway._litellm` (C3 model independence) and is deliberately NOT
re-exported from this package root.
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute
from ne.gateway.cost import CostSink, OutboxCostSink, extract_cost, extract_usage
from ne.gateway.egress import (
    KNOWN_FRONTIER_PROVIDER_HOSTS,
    EgressPolicy,
    EgressSettings,
    build_egress_guard,
)
from ne.gateway.errors import (
    AirGapViolationError,
    EgressBlockedError,
    EmptyCompletionError,
    FrontierProviderError,
    GatewayConfigError,
    LaneMismatchError,
    ModelCallFailedError,
    NeGatewayError,
    ReasoningExhaustedError,
    ReasoningWithoutAnswerError,
    RedactionRequiredError,
    RegistryValidationError,
    UnknownModelIdError,
    UnknownRoutedModelError,
    UnknownTaskError,
    UnsafeEndpointError,
)
from ne.gateway.gateway import LiteLLMGateway
from ne.gateway.instrumentation import ATTR_NE_LATENCY_MS, ATTR_NE_TASK, annotate_span_after_call
from ne.gateway.menus import DEFAULT_MENUS, MENU_CAPABILITY_FIRST, MENU_WESTERN, load_menu
from ne.gateway.registry import (
    InMemoryModelRegistry,
    ModelBinding,
    build_gateway_settings,
    default_vllm_litellm_model,
    entry_to_model_def,
    validate_entry,
)
from ne.gateway.ssrf import redact_url_for_logs, validate_endpoint_url

__all__ = [
    "ATTR_NE_LATENCY_MS",
    "ATTR_NE_TASK",
    "DEFAULT_MENUS",
    "KNOWN_FRONTIER_PROVIDER_HOSTS",
    "MENU_CAPABILITY_FIRST",
    "MENU_WESTERN",
    "AirGapViolationError",
    "CostSink",
    "EgressBlockedError",
    "EgressPolicy",
    "EgressSettings",
    "EmptyCompletionError",
    "FrontierProviderError",
    "GatewayConfigError",
    "GatewaySettings",
    "InMemoryModelRegistry",
    "LaneMismatchError",
    "LiteLLMGateway",
    "ModelBinding",
    "ModelCallFailedError",
    "ModelDef",
    "NeGatewayError",
    "OutboxCostSink",
    "ReasoningExhaustedError",
    "ReasoningWithoutAnswerError",
    "RedactionRequiredError",
    "RegistryValidationError",
    "TaskRoute",
    "UnknownModelIdError",
    "UnknownRoutedModelError",
    "UnknownTaskError",
    "UnsafeEndpointError",
    "__version__",
    "annotate_span_after_call",
    "build_egress_guard",
    "build_gateway_settings",
    "default_vllm_litellm_model",
    "entry_to_model_def",
    "extract_cost",
    "extract_usage",
    "load_menu",
    "redact_url_for_logs",
    "validate_endpoint_url",
    "validate_entry",
]
