# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Configuration for the ne-serve vLLM client (``NE_SERVE_`` env / ``ne.toml``).

Layered like every other module's settings via :class:`ne.contracts.NeSettings`
(init kwargs > env > TOML > defaults). ``base_url`` points at the vLLM
OpenAI-compatible server's ROOT (no ``/v1`` suffix — ``/health`` lives at the
root, and the client appends ``/v1`` only for the OpenAI endpoints).
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import SettingsConfigDict

from ne.contracts import NeSettings


class ServeSettings(NeSettings):
    """Per-deployment ne-serve client config."""

    model_config = SettingsConfigDict(
        env_prefix="NE_SERVE_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    base_url: str = "http://localhost:8090"
    """Root URL of the vLLM OpenAI-compatible server. No trailing ``/v1``."""

    allowed_endpoint_hosts: tuple[str, ...] = ()
    """Private-DNS hostnames additionally accepted by the construction-time C2
    endpoint validator (mirror of ne-gateway's ``allowed_private_vllm_hosts``
    escape hatch). Env ``NE_SERVE_ALLOWED_ENDPOINT_HOSTS`` (JSON list)."""

    model: str = "qwen3.6-35b-a3b"
    """Default ``--served-model-name`` to target on completion requests."""

    timeout_s: float = 120.0
    """Per-request HTTP timeout in seconds."""

    weights_root: Path = Path("models")
    """Local directory under which pre-staged weights live (air-gap)."""
