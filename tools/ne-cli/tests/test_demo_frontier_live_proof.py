# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED (NE-BUNDLE-GATEWAY-CONFIG): a REAL governed frontier completion
through the demo's exact ``--frontier`` config surface.

Skip-clean unless ``NE_DEMO_FRONTIER_LIVE_PROOF=1`` AND a real provider API key
is exported — the worm-live-proof green-by-skip discipline. This module never
FAILS on a normal (no-keys) CI/dev run; it SKIPS, naming the exact env needed.

Owner-run recipe (no frontier keys exist on the builder's CI/dev machine as of
this unit — see the PR body's honest-boundary note)::

    NE_DEMO_FRONTIER_LIVE_PROOF=1 \\
    ANTHROPIC_API_KEY=... \\          # or OPENAI_API_KEY=... with NE_DEMO_FRONTIER=openai
    NE_DEMO_FRONTIER=anthropic \\     # optional; default anthropic
    uv run pytest tools/ne-cli/tests/test_demo_frontier_live_proof.py -v

The owner-secrets precedent (mirrors salesforce/m365): export the key from
``~/ne-owner-secrets/<provider>/api-key``.

Builds ``GatewaySettings`` from ``live._gateway_lane_env(None, frontier=...)``
UNMODIFIED (no ``mock_response`` — this is real egress), the real
``build_egress_guard`` over the EXACT emitted allowlist, and a redactor wired
the SAME way ``ne.api.platform._default_gateway`` wires ``create_redactor``
(platform.py ~1870-1895) — so this proof exercises the identical governance
path production/the demo actually uses, not a relaxed proxy of it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

_FRONTIER_KEY_VARS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

pytestmark = pytest.mark.skipif(
    os.environ.get("NE_DEMO_FRONTIER_LIVE_PROOF") != "1",
    reason=(
        "LIVE-PARKED: set NE_DEMO_FRONTIER_LIVE_PROOF=1 plus a real "
        "ANTHROPIC_API_KEY or OPENAI_API_KEY (optionally NE_DEMO_FRONTIER=<provider>, "
        "default anthropic) to run a real governed frontier completion — see this "
        "module's docstring for the exact recipe."
    ),
)


def test_real_frontier_completion_is_governed_and_audited() -> None:
    provider = os.environ.get("NE_DEMO_FRONTIER", "anthropic")
    if provider not in _FRONTIER_KEY_VARS:
        pytest.fail(
            f"NE_DEMO_FRONTIER={provider!r} is not a supported --frontier provider "
            f"({sorted(_FRONTIER_KEY_VARS)!r})"
        )
    key_var = _FRONTIER_KEY_VARS[provider]
    if not os.environ.get(key_var):
        pytest.skip(
            f"{key_var} is not set — see test_demo_frontier_live_proof's module "
            "docstring for the live-proof recipe."
        )

    from ne.cli.demo import live
    from ne.contracts import ChatMessage, CompletionRequest, reset_config_hash_for_testing
    from ne.gateway import GatewaySettings, LiteLLMGateway, ModelDef, TaskRoute
    from ne.gateway.egress import EgressSettings, build_egress_guard
    from ne.govern import create_redactor
    from ne.runtime.factory import freeze_platform_config

    env = live._gateway_lane_env(None, frontier=provider)
    models_match = re.search(r"NE_GATEWAY_MODELS:\s*'(.+?)'\n", env)
    tasks_match = re.search(r"NE_GATEWAY_TASKS:\s*'(.+?)'\n", env)
    allow_match = re.search(r"NE_EGRESS_ALLOWED_FRONTIER_HOSTS:\s*'(.+?)'\n", env)
    assert models_match and tasks_match and allow_match, env

    models = [ModelDef.model_validate(m) for m in json.loads(models_match.group(1))]
    tasks = {k: TaskRoute(**v) for k, v in json.loads(tasks_match.group(1)).items()}
    host = json.loads(allow_match.group(1))[0]

    settings = GatewaySettings(models=models, tasks=tasks)
    egress_guard = build_egress_guard(EgressSettings(allowed_frontier_hosts=(host,)))
    # Mirrors ne.api.platform._default_gateway's create_redactor(...) wiring
    # (platform.py ~1870-1895) — the SAME fail-closed redaction path production
    # uses, not a passthrough test double.
    redactor = create_redactor()

    class _RecordingCostSink:
        def __init__(self) -> None:
            self.events: list[object] = []

        def emit(self, event: object) -> None:
            self.events.append(event)

    sink = _RecordingCostSink()
    gateway = LiteLLMGateway(settings, sink, egress_guard=egress_guard, redactor=redactor)
    request = CompletionRequest(
        messages=(ChatMessage(role="user", content="Reply with exactly one word: ok"),),
        task="agent.meridian-analyst.v2",
    )

    # Every gateway completion stamps the frozen process config_hash onto its
    # CostEvent/AuditEvent (§6.2/Q5) — production freezes it in create_app via
    # freeze_platform_config; this proof performs the SAME composed freeze over
    # the swapped GatewaySettings (without it, the completion dies with
    # ConfigHashNotFrozenError before egress — caught by the first real-key run
    # of this proof at the synthesis gate).
    reset_config_hash_for_testing()
    freeze_platform_config(extra_modules={"ne-gateway": settings})
    try:
        result = asyncio.run(gateway.complete(request))
    finally:
        reset_config_hash_for_testing()

    assert result.text
    assert result.model_id == "frontier-default"
    assert len(sink.events) == 1
