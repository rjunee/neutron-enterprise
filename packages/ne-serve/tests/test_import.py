# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-serve — imports every public export."""

from __future__ import annotations

import subprocess
import sys


def test_import_and_version() -> None:
    import ne.serve

    assert ne.serve.__version__ == "0.1.0"


def test_public_exports_present() -> None:
    import ne.serve

    expected = {
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
        "StructuredOutputViolationError",
        "UnsafeEndpointError",
        "WeightIntegrityError",
        "download_weights",
        "errors",
        "local_path",
        "offline_env",
        "validate_in_vpc_endpoint",
        "verify_weights",
        "vllm_serve_args",
    }
    assert expected <= set(ne.serve.__all__)
    for name in expected:
        assert hasattr(ne.serve, name), name


def test_import_pulls_no_provider_sdk() -> None:
    """C3: a fresh ``import ne.serve`` must not pull litellm/openai/ne.gateway.

    Checked in a clean subprocess so an unrelated test that imports ne.gateway
    can't pollute the assertion.
    """
    code = (
        "import ne.serve, sys; "
        "banned = [m for m in ('litellm', 'openai', 'ne.gateway') if m in sys.modules]; "
        "print(banned)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", result.stdout
