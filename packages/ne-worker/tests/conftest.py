# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-worker test harness — a REAL governed spine over SQLite + stub edges.

No pytest-asyncio (repo policy): async is driven via :func:`run`. The composition
root is the REAL :func:`ne.api.platform.build_platform`; only the network edges
(model gateway, embedder/sparse/qdrant, OPA wire) are faked — exactly the ne-api
conftest discipline, so the worker's no-second-executor guarantees are proven over
the SAME governed stack the HTTP surface runs.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.settings import ApiSettings
from ne.audit.config import AuditSettings
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Lane
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings
from ne.worker.runtime_state import reset_worker_state_for_testing
from ne.worker.testing import (
    ISSUER,
    RegoFaithfulPdpFactory,
    StubChunkStore,
    StubEmbedder,
    StubSparseEncoder,
)


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    reset_config_hash_for_testing()
    reset_worker_state_for_testing()
    try:
        yield
    finally:
        reset_config_hash_for_testing()
        reset_worker_state_for_testing()


@pytest.fixture
def protected_transport_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scope the activated transport to the tests that actually compose it.

    Setting these variables while pytest imports a module leaks activation into
    unrelated modules collected by the same xdist worker.  Consumers opt in via
    ``pytest.mark.usefixtures`` so ``monkeypatch`` restores the dormant process
    environment after every test.
    """
    monkeypatch.setenv("NE_AGENT_PRIVATE_PAYLOAD_KEY_ID", "worker-test-k1")
    monkeypatch.setenv(
        "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64",
        "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s",
    )
    monkeypatch.setenv("NE_AGENT_TRANSPORT_GENERATION", "agent-transport-v1")


def run(coro: Any) -> Any:
    """Drive a coroutine to completion (no pytest-asyncio in this repo)."""
    return asyncio.run(coro)


class ScriptedGateway:
    """A ``ModelGateway`` returning scripted completions; records every request."""

    def __init__(self, completions: Sequence[Completion] | None = None) -> None:
        self._queue = list(completions or [])
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if self._queue:
            return self._queue.pop(0)
        return text_completion("done")

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError("ScriptedGateway does not stream")


def text_completion(text: str) -> Completion:
    return Completion(text=text, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


def sqlite_seams(
    tmp_path: Path,
    *,
    gateway: Any | None = None,
    extra_tool_bindings: tuple[Any, ...] = (),
    extra_tool_policies: Mapping[str, Any] | None = None,
    approvals: Any | None = None,
) -> tuple[ApiSettings, AppSeams]:
    """Build ``(api_settings, seams)`` over tmp SQLite with stub edges.

    Returned so a test can compose the SAME mapping TWICE (the idempotent-freeze
    proof) — the composed ``config_hash`` covers every settings field, so
    re-freezing requires the identical settings objects.
    """
    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    runtime_url = f"sqlite:///{tmp_path}/runtime-{uuid.uuid4().hex}.db"
    audit_url = f"sqlite:///{tmp_path}/audit-{uuid.uuid4().hex}.db"
    perms_url = f"sqlite:///{tmp_path}/perms-{uuid.uuid4().hex}.db"
    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-worker"),
            principal_claims=("email", "preferred_username", "name", "department"),
        ),
        runtime_settings=(
            RuntimeSettings(session_database_url=runtime_url)
            if approvals is None
            else RuntimeSettings(session_database_url=runtime_url, approvals=approvals)
        ),
        audit_settings=AuditSettings(database_url=audit_url),
        permissions_settings=PermissionsSettings(database_url=perms_url),
        gateway_settings=GatewaySettings(
            models=[
                ModelDef(
                    name="stub",
                    litellm_model="openai/stub",
                    provider="stub",
                    lane=Lane.FRONTIER_API,
                    context_window=32768,
                )
            ]
        ),
        gateway=gateway if gateway is not None else ScriptedGateway(),
        embedder=StubEmbedder(),
        sparse_encoder=StubSparseEncoder(),
        chunk_store=StubChunkStore(),
        plugin_registry=registry,
        extra_tool_bindings=extra_tool_bindings,
        extra_tool_policies=extra_tool_policies,
    )
    settings = ApiSettings(
        qdrant_url="http://qdrant.test",
        qdrant_collection="t",
        tei_url="http://tei.test",
    )
    return settings, seams


def build_sqlite_platform(
    tmp_path: Path,
    *,
    gateway: Any | None = None,
    extra_tool_bindings: tuple[Any, ...] = (),
    extra_tool_policies: Mapping[str, Any] | None = None,
    approvals: Any | None = None,
) -> Platform:
    """Compose a Platform over tmp SQLite with stub edges (the REAL governed spine)."""
    settings, seams = sqlite_seams(
        tmp_path,
        gateway=gateway,
        extra_tool_bindings=extra_tool_bindings,
        extra_tool_policies=extra_tool_policies,
        approvals=approvals,
    )
    return build_platform(settings, seams)
