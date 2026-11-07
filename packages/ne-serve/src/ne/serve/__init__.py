# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""vLLM serving profile plus model menu (the private-vllm lane, in-VPC).

ADOPT-VIA-ISOLATION (SPEC §11): vLLM runs ONLY as an external OCI container
(see ``deploy/compose/serve/``); this package adds ZERO vllm/torch/nvidia to
the platform lock. It ships a thin OpenAI-compatible client, air-gap weight
pre-staging tooling, and the servable model menu (kept consistent with the
NE-011 registry by a test).
"""

from __future__ import annotations

from ne.serve import errors
from ne.serve.client import ServeClient
from ne.serve.config import ServeSettings
from ne.serve.download import (
    DownloadResult,
    download_weights,
    local_path,
    offline_env,
    verify_weights,
    vllm_serve_args,
)
from ne.serve.errors import (
    ServeConnectionError,
    ServeError,
    ServeHealthError,
    StructuredOutputViolationError,
    UnsafeEndpointError,
    WeightIntegrityError,
)
from ne.serve.models import (
    MODEL_SOURCES,
    TINY_CI_SERVED_NAME,
    TINY_CI_SOURCE,
    HFSource,
)
from ne.serve.sovereignty import validate_in_vpc_endpoint
from ne.serve.stage_tei import StageResult, stage_tei, stage_tei_verify

__version__ = "0.1.0"

__all__ = [
    "MODEL_SOURCES",
    "TINY_CI_SERVED_NAME",
    "TINY_CI_SOURCE",
    "DownloadResult",
    "HFSource",
    "ServeClient",
    "ServeConnectionError",
    "ServeError",
    "ServeHealthError",
    "ServeSettings",
    "StageResult",
    "StructuredOutputViolationError",
    "UnsafeEndpointError",
    "WeightIntegrityError",
    "download_weights",
    "errors",
    "local_path",
    "offline_env",
    "stage_tei",
    "stage_tei_verify",
    "validate_in_vpc_endpoint",
    "verify_weights",
    "vllm_serve_args",
]
