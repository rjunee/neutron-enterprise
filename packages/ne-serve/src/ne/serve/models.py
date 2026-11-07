# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""HuggingFace weight sources for the servable open-weight model menu.

Keyed EXACTLY by the ``model_id`` values in ``ne.gateway.menus.DEFAULT_MENUS``
(the merged NE-011 registry). We deliberately do NOT import ``ne.gateway`` here:
that would pull the litellm closure into the ne-serve runtime. The three-way
consistency (menu <-> this table <-> ``docs/model-menu.md``) is enforced by a
test (``test_menu_docs_consistency.py``), not by a runtime import.

Repo ids re-verified 2026-07-05 against the HuggingFace model API (each returns
the canonical repo). ``Mistral-Large-3`` resolves (HTTP redirect) to the dated
canonical id recorded below. License/origin fields mirror the ``ModelEntry``
provenance and are re-checked per SPEC §3.3 before a menu refresh.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict


class HFSource(BaseModel):
    """Provenance for a servable model's HuggingFace weights (frozen)."""

    model_config = ConfigDict(frozen=True)

    repo_id: str
    license: str
    origin: str
    note: str = ""


# Keyed by the six DEFAULT_MENUS model_ids (capability-first + western).
MODEL_SOURCES: Final[dict[str, HFSource]] = {
    "glm-5.2": HFSource(
        repo_id="zai-org/GLM-5.2",
        license="MIT",
        origin="CN",
    ),
    "qwen3.5-397b-a17b": HFSource(
        repo_id="Qwen/Qwen3.5-397B-A17B",
        license="Apache-2.0",
        origin="CN",
    ),
    "qwen3.6-35b-a3b": HFSource(
        repo_id="Qwen/Qwen3.6-35B-A3B",
        license="Apache-2.0",
        origin="CN",
    ),
    "deepseek-v4-flash": HFSource(
        repo_id="deepseek-ai/DeepSeek-V4-Flash",
        license="MIT",
        origin="CN",
    ),
    "mistral-large-3": HFSource(
        repo_id="mistralai/Mistral-Large-3-675B-Instruct-2512-BF16",
        license="Apache-2.0",
        origin="FR",
        note="canonical resolve of mistralai/Mistral-Large-3 (HF redirect target)",
    ),
    "gpt-oss-120b": HFSource(
        repo_id="openai/gpt-oss-120b",
        license="Apache-2.0",
        origin="US",
    ),
}

# CPU-mode CI/dev tiny model — NOT a menu entry. Small enough to serve on the
# official vLLM CPU image for the no-egress live harness (NE-073).
TINY_CI_SOURCE: Final[HFSource] = HFSource(
    repo_id="Qwen/Qwen3-0.6B",
    license="Apache-2.0",
    origin="CN",
    note="CPU-mode CI/dev tiny model — NOT a menu entry",
)
TINY_CI_SERVED_NAME: Final[str] = "qwen3-0.6b"
