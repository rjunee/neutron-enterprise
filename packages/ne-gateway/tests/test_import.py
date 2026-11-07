# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Import smoke test for ne-gateway."""

from __future__ import annotations

import ne.gateway


def test_import_and_version() -> None:
    assert ne.gateway.__version__ == "0.1.0"


def test_public_exports_present() -> None:
    expected = {
        "CostSink",
        "GatewayConfigError",
        "GatewaySettings",
        "LaneMismatchError",
        "LiteLLMGateway",
        "ModelCallFailedError",
        "ModelDef",
        "NeGatewayError",
        "OutboxCostSink",
        "TaskRoute",
        "UnknownTaskError",
        "extract_cost",
        "extract_usage",
    }
    assert expected <= set(ne.gateway.__all__)
    for name in expected:
        assert hasattr(ne.gateway, name), name


def test_litellm_not_reexported_from_root() -> None:
    # C3: the package root must NOT re-export litellm / Router.
    assert not hasattr(ne.gateway, "litellm")
    assert not hasattr(ne.gateway, "Router")
