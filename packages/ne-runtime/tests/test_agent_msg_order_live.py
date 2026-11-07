# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED: NE-AGENT-MSG-ORDER's real-rig regression.

**LIVE-PARKED discipline (repo law — see ``packages/ne-brain-graph/tests/
test_extraction_live.py`` and ``packages/ne-permissions-rebac/tests/
test_openfga_live.py``).** Env-var PRESENCE alone never enables a run; only
REACHABILITY does — an unreachable/misconfigured endpoint SKIPS exactly as if
the env var were unset. Module import always succeeds (skipif-pytestmark
discipline, NEVER ``pytest.skip(..., allow_module_level=True)``), so a
renamed/gutted suite fails CI loudly instead of skipping into a fake green.

**Why this module exists.** The orchestrator's three-probe isolation (rig:
Qwen3.6-35B-A3B-FP8 via vLLM 0.24.0, ``--enable-auto-tool-choice
--tool-call-parser qwen3_coder --reasoning-parser qwen3``, 2026-07-31) proved
that a raw-endpoint probe sending ``(system, user, system)`` gets HTTP 400
"System message must be at the beginning." from a strict chat template — and
that this shape is exactly what the SHIPPED (pre-fix) agent path sent. A
prior investigation reached a FALSE conclusion ("reasoning + tool-calling is
broken on this model") because its probe omitted a system message entirely
and so never reproduced the real agent's request shape; this module's
governed-turn structure is the counter-measure — it drives the REAL platform
seam (``DepartmentAgentSession`` -> ``GatewayModel`` -> the C3 chokepoint),
never a hand-built raw request.

**Declared fixture boundary.** REAL: ``GatewayModel`` -> ``LiteLLMGateway`` ->
litellm -> the rig's vLLM server, plus the real ``GovernedToolExecutor`` (PDP
+ guardrails + approval gate + audit). TEST-DOUBLES: an in-memory sqlite
session store, the conftest's scripted-OPA PDP transport
(``rego_faithful_transport``/``make_pdp``), the guardrail engine's config
(``govern_settings``), and ``NamedApproverGate`` (approval always granted —
the bound tool here is TIER_0 and never consults it).

**NEVER weaken this test to make the real system pass** (extraction-live's
rule, verbatim): if the rig disagrees with the assertion below, the test is
the honest party — report the divergence, never soften the assertion, never
add a skip branch for an "unexpected" outcome, and never turn the task prompt
into an instruction sheet that coaxes a tool call out of the model rather
than asking a real question a documentation search actually answers.

Run against the rig (thinking ENABLED — no ``enable_thinking=False``, no
``/no_think`` anywhere in this module; the rig's ``--reasoning-parser qwen3``
default applies):

    # 1. Provision + serve vLLM (deploy/inference-operator/vast-rig.sh);
    #    verify GET /health and GET /v1/models on the rig BEFORE running.
    # 2. Run, ON the rig (loopback egress posture — PRIVATE_VLLM requires an
    #    in-VPC api_base):
    NE_RUNTIME_LIVE_VLLM_URL=http://127.0.0.1:8090 \\
      uv run pytest packages/ne-runtime/tests/test_agent_msg_order_live.py -q -rs

Optional ``NE_RUNTIME_LIVE_VLLM_KEY`` is forwarded as the model's litellm
``api_key`` extra param when the rig's OpenAI-compatible server requires one.
The served model id is auto-derived from ``GET {VLLM_URL}/v1/models`` — there
is no env var for it.

Expected outcomes per environment:

* **rig up:** 2 passed (inventory tripwire + the live governed-turn
  regression) — NOT YET independently observed by this branch; see the PR
  body's LIVE-PARKED statement. The parked test is the designated vehicle:
  per ``verify-before-the-merge-gate``, the orchestrator's own rig re-run —
  not this build report — is what may call the C3 fix live-proven.
* **bare CI (nothing up):** 1 passed (the inventory tripwire only), 1 skipped
  — the skip reason prints the exact bring-up command above. Observed
  locally by the builder, 2026-07-31.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from ne.contracts import Lane
from ne.gateway import GatewaySettings, LiteLLMGateway, ModelDef, TaskRoute
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ToolLog,
    make_executor,
    make_principal,
    standard_bindings,
)

# ---------------------------------------------------------------------------
# Module-scope probe (executed at collection time; module import ALWAYS
# succeeds — skipif-pytestmark discipline, never allow_module_level, so a
# renamed/gutted suite fails CI loudly instead of skipping into a fake green)
# ---------------------------------------------------------------------------

_VLLM_URL = os.environ.get("NE_RUNTIME_LIVE_VLLM_URL", "http://127.0.0.1:8090")
_VLLM_KEY = os.environ.get("NE_RUNTIME_LIVE_VLLM_KEY")

_MODEL_ID: str | None = None


def _vllm_probe_headers() -> dict[str, str]:
    """Bearer header for the readiness probes, mirroring the api_key forwarded
    to LiteLLM via ``_rig_gateway_settings`` -- without this, an authenticated
    rig deployment answers 401 on /health and /v1/models, ``_vllm_ok`` returns
    False, and the rig-gated test silently skips instead of running."""
    return {"Authorization": f"Bearer {_VLLM_KEY}"} if _VLLM_KEY else {}


def _vllm_ok() -> bool:
    """GET /health == 200 AND GET /v1/models resolves a served model id.

    Byte-mirrors ``packages/ne-brain-graph/tests/test_extraction_live.py``'s
    ``_vllm_ok`` probe: env-var PRESENCE alone never enables a run, only
    REACHABILITY does. ANY failure (connection refused, non-200, malformed
    JSON, no models listed) returns False -> SKIP, exactly as if the env var
    were unset."""
    global _MODEL_ID
    headers = _vllm_probe_headers()
    try:
        req = urllib.request.Request(f"{_VLLM_URL}/health", headers=headers)
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status != 200:
                return False
    except (urllib.error.URLError, OSError, ValueError):
        return False
    try:
        req = urllib.request.Request(f"{_VLLM_URL}/v1/models", headers=headers)
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return False
    model_id = data[0].get("id")
    if not model_id:
        return False
    _MODEL_ID = str(model_id)
    return True


_VLLM_OK = _vllm_ok()

_RIG_BRING_UP = (
    "LIVE-PARKED: needs a routed vLLM serving a reasoning model (e.g. "
    "Qwen3.6-35B-A3B-FP8, --reasoning-parser qwen3). Provision + serve: "
    "deploy/inference-operator/vast-rig.sh. Then, ON the rig "
    "(loopback egress posture -- PRIVATE_VLLM requires an in-VPC api_base): "
    "NE_RUNTIME_LIVE_VLLM_URL=http://127.0.0.1:8090 uv run pytest "
    "packages/ne-runtime/tests/test_agent_msg_order_live.py -q -rs"
)

_live_rig = pytest.mark.skipif(not _VLLM_OK, reason=_RIG_BRING_UP)


def _rig_gateway_settings() -> GatewaySettings:
    """The ONE routed model, task ``agent`` (``RuntimeSettings().task`` default).

    ``PRIVATE_VLLM`` + a loopback/in-VPC ``api_base`` -- byte-mirrors
    ``test_extraction_live.py``'s ``_rig_gateway_settings``: the rig posture
    is to run ON the rig against 127.0.0.1; a public-IP misconfiguration
    fails loudly (the documented, correct behavior).
    """
    extra_params = {"api_key": _VLLM_KEY} if _VLLM_KEY else {}
    model = ModelDef(
        name="rig-vllm",
        litellm_model=f"hosted_vllm/{_MODEL_ID}",
        provider="hosted_vllm",
        lane=Lane.PRIVATE_VLLM,
        api_base=f"{_VLLM_URL}/v1",
        context_window=32768,
        extra_params=extra_params,
    )
    return GatewaySettings(models=[model], tasks={"agent": TaskRoute(model="rig-vllm")})


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_runtime_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


class RecordingCostSink:
    """A real CostSink implementation that appends emitted events to a list.

    ne-runtime tests must not import test doubles from another package's
    tests/ (``--import-mode=importlib`` forbids cross-package test imports);
    this mirrors (rather than imports) ne-gateway's own conftest stub, and
    ``test_trace_correlation_run.py`` in THIS package already carries the
    identical duplication for the same reason.
    """

    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)


@_live_rig
def test_live_reasoning_model_governed_turn_emits_tool_call(engine: sa.Engine) -> None:
    """A governed turn THROUGH the platform, against a thinking-enabled
    reasoning model, must emit >= 1 governed ``read.docs.search`` tool call.

    Thinking is ENABLED throughout: no ``enable_thinking=False``, no
    ``/no_think`` anywhere in this module or the wiring it drives -- the
    rig's ``--reasoning-parser qwen3`` default applies unconditionally. This
    asserts HARD (fails, never skips) on no-tool-call or an empty final
    answer: a skip-on-anything-unexpected would turn this regression into
    decoration, exactly the vacuity trap the brief calls out.
    """
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=NamedApproverGate("alice"))
    gateway = LiteLLMGateway(_rig_gateway_settings(), RecordingCostSink(), audit_sink=sink)
    store = SqlAlchemySessionStore(engine)
    session = DepartmentAgentSession(
        session_id=asyncio.run(
            store.create_conversation("assistant", "eng", "user:ada")
        ).conversation_id,
        agent_name="assistant",
        department="eng",
        owner_principal_id="user:ada",
        store=store,
        executor=executor,
        gateway=gateway,
        settings=RuntimeSettings(),
        instructions=(
            "You are an internal engineering assistant. You have a "
            "read.docs.search tool over the internal documentation corpus. "
            "Use it whenever a question requires looking something up."
        ),
        sink=sink,
    )

    # A REAL task requiring a documentation lookup -- never an instruction
    # sheet coaxing a tool call (extraction-live's never-weaken rule).
    result = asyncio.run(
        session.run(
            "What does our internal documentation say about the "
            "escalation policy for the engineering department? Search the "
            "docs and summarize what you find.",
            principal=make_principal(department="eng"),
        )
    )

    tool_calls = [name for name, _args in log.calls if name == "read.docs.search"]
    assert len(tool_calls) >= 1, (
        "expected >= 1 governed read.docs.search tool call through the "
        f"REAL platform against the rig's routed model; got calls={log.calls!r} "
        f"output={result.output!r}. If this fails with a raw HTTP 400 "
        '"System message must be at the beginning." (ModelCallFailedError), '
        "the C3 fix has NOT reached the rig -- rebuild/redeploy before "
        "re-running."
    )
    assert isinstance(result.output, str)
    assert result.output.strip() != ""


# ---------------------------------------------------------------------------
# Inventory tripwire (always-on): a rename/deletion of any test_live_*
# function in this module must fail CI loudly, never skip into a fake green.
# ---------------------------------------------------------------------------

_EXPECTED_LIVE_TESTS = frozenset({"test_live_reasoning_model_governed_turn_emits_tool_call"})


def test_inventory_of_live_tests_pinned() -> None:
    live_names = {name for name in globals() if name.startswith("test_live_")}
    assert live_names == _EXPECTED_LIVE_TESTS
