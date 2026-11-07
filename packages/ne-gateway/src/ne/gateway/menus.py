# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Named default model menus (SPEC section 3.3 model-menu row). Origin tags
let a customer filter by procurement constraint; the Western menu exists for
buyers who cannot procure CN-origin weights.

Entries verified 2026-07-04; see ``packages/ne-gateway/README.md`` for sources
and license/context-window snapshot notes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from ne.contracts import Lane, ModelEntry
from ne.gateway.errors import RegistryValidationError
from ne.gateway.registry import InMemoryModelRegistry

# All six menu entries are open-weight, served in-VPC (lanes=(Lane.PRIVATE_VLLM,)).
# The frontier-api lane is populated per-deployment, not by these menus.

GLM_5_2 = ModelEntry(
    model_id="glm-5.2",
    provider="zai-org",
    license="MIT",
    origin="CN",
    lanes=(Lane.PRIVATE_VLLM,),
    capabilities=("streaming", "structured-output", "tool-use", "reasoning", "long-context"),
    context_window=1_000_000,
)

QWEN3_5_397B = ModelEntry(
    model_id="qwen3.5-397b-a17b",
    provider="Qwen",
    license="Apache-2.0",
    origin="CN",
    lanes=(Lane.PRIVATE_VLLM,),
    capabilities=("streaming", "structured-output", "tool-use", "reasoning", "long-context"),
    context_window=262_144,
)

QWEN3_6_35B = ModelEntry(
    model_id="qwen3.6-35b-a3b",
    provider="Qwen",
    license="Apache-2.0",
    origin="CN",
    lanes=(Lane.PRIVATE_VLLM,),
    capabilities=("streaming", "structured-output", "tool-use"),
    context_window=262_144,
)

DEEPSEEK_V4_FLASH = ModelEntry(
    model_id="deepseek-v4-flash",
    provider="deepseek-ai",
    license="MIT",
    origin="CN",
    lanes=(Lane.PRIVATE_VLLM,),
    capabilities=("streaming", "structured-output", "tool-use", "reasoning", "long-context"),
    context_window=1_000_000,
)

MISTRAL_LARGE_3 = ModelEntry(
    model_id="mistral-large-3",
    provider="mistralai",
    license="Apache-2.0",
    origin="FR",
    lanes=(Lane.PRIVATE_VLLM,),
    capabilities=("streaming", "structured-output", "tool-use", "reasoning", "long-context"),
    context_window=256_000,
)

GPT_OSS_120B = ModelEntry(
    model_id="gpt-oss-120b",
    provider="openai",
    license="Apache-2.0",
    origin="US",
    lanes=(Lane.PRIVATE_VLLM,),
    capabilities=("streaming", "structured-output", "tool-use", "reasoning"),
    context_window=131_072,
)

MENU_CAPABILITY_FIRST: Final[str] = "capability-first"
MENU_WESTERN: Final[str] = "western"

DEFAULT_MENUS: Mapping[str, tuple[ModelEntry, ...]] = {
    MENU_CAPABILITY_FIRST: (GLM_5_2, QWEN3_5_397B, QWEN3_6_35B, DEEPSEEK_V4_FLASH),
    MENU_WESTERN: (MISTRAL_LARGE_3, GPT_OSS_120B),
}


def load_menu(name: str) -> InMemoryModelRegistry:
    """Load a named default menu into a fresh registry. Every entry is
    validated via ``register()`` (the boot-fail ergonomic): an unknown menu
    name names the available alternatives.
    """
    if name not in DEFAULT_MENUS:
        raise RegistryValidationError(
            f"unknown menu {name!r}; available menus: {sorted(DEFAULT_MENUS)!r}"
        )
    return InMemoryModelRegistry(DEFAULT_MENUS[name])
