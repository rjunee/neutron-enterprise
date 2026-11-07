# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CI-only seams for a subprocess-booted worker (NE-103c T3/T4).

``ci_seams()`` returns an :class:`ne.api.platform.AppSeams` wiring SQLite/stub edges
so ``python -m ne.worker`` boots inside the live-Postgres CI job WITHOUT loading
fastembed/qdrant/litellm or reaching a real OPA. It mirrors the
``packages/ne-api/tests/conftest.py`` fake pattern (a REAL governed spine over faked
network edges) and is DOCUMENTED NOT-FOR-PRODUCTION (the ``NE_WORKER_SEAMS`` seam).

Two SIGKILL/HITL choreographies, selected by ``NE_WORKER_TEST_MODE``:

* ``block`` (default) — the stub gateway writes a ``start`` marker row into
  ``NE_RUNTIME_SESSION_DATABASE_URL`` then POLLS for a ``release`` row before
  returning. The T3 test SIGKILLs the worker after the marker appears, restarts it
  (the workflow re-runs → a SECOND ``start`` row proves at-least-once step
  re-execution), then inserts ``release`` to let the recovered turn complete.
* ``hitl`` — the stub gateway emits a tier-2 tool call (→ ``require-approval`` →
  the worker's gate blocks in ``await_outcome``); a SECOND process approves the row.
* ``cron-block`` — the pod-kill-during-cron choreography (NE-LP-DBOS-REALPG).
  Every cron surface is ``scheduled_execution='sole-tool'`` (63279abe/51844fa9:
  the scheduler invokes the job's exact governed tool, NO model loop), so the
  gateway can no longer be the cron test's blocking point. Instead the scripted
  OPA transport below blocks the governed PDP decision for the ALWAYS-ON
  rotation tool — the first governed step INSIDE the claimed job body, after
  the claim CAS — writing ``start`` then polling for ``release`` exactly like
  ``block`` mode's gateway. Turn-path choreographies are untouched: only the
  rotation action blocks, and only in this mode.

The PDP is the REAL ``GovernedPdp`` over a scripted OPA transport (OPA is a wire
edge — the ne-runtime ``make_pdp`` precedent), so no OPA service is needed in CI.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import sqlalchemy as sa

from ne.api.platform import AppSeams
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.plugins import PluginRegistry
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Chunk, Lane, ScoredChunk, SparseVector
from ne.gateway.config import GatewaySettings, ModelDef
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.govern.opa import OpaClient
from ne.govern.pdp import GovernedPdp
from ne.identity.config import IdentitySettings, OidcConfig
from ne.runtime.tools import ToolBinding

ISSUER = "https://idp.ne.test/realms/ne-test"
AUDIENCE = "ne-worker"
_DECISION_PATH = "/v1/data/ne/govern/decision"

#: The tier-2 tool the HITL choreography triggers (write.external.* ⇒ tier 2).
HITL_TOOL_NAME = "write.external.crm.note"

#: The sole-tool cron action the ``cron-block`` choreography blocks on. Keep in
#: lockstep with ``ne.worker.credential_rotation._TOOL`` — if that tool renames,
#: the pod-kill proof times out at its first ``start``-marker wait.
CRON_BLOCK_TOOL_NAME = "write.internal.identity.credential_rotation"

_MARKER_TABLE = sa.Table(
    "ne_worker_test_markers",
    sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


def ensure_marker_table(engine: sa.Engine) -> None:
    """Create the SIGKILL-choreography marker table (idempotent)."""
    _MARKER_TABLE.metadata.create_all(engine)


def _write_marker(url: str, kind: str) -> None:
    engine = sa.create_engine(url)
    try:
        ensure_marker_table(engine)
        with engine.begin() as conn:
            conn.execute(_MARKER_TABLE.insert().values(kind=kind, created_at=datetime.now(UTC)))
    finally:
        engine.dispose()


def _has_marker(url: str, kind: str) -> bool:
    engine = sa.create_engine(url)
    try:
        ensure_marker_table(engine)
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(sa.func.count())
                .select_from(_MARKER_TABLE)
                .where(_MARKER_TABLE.c.kind == kind)
            ).scalar_one()
        return int(row) > 0
    finally:
        engine.dispose()


# --- Rego-faithful scripted OPA transport (below the REAL GovernedPdp) -----


def _classify(action: str) -> int | None:
    tiers: set[int] = set()
    if action.startswith("read.") or action.startswith("draft."):
        tiers.add(0)
    if action.startswith("write.internal."):
        tiers.add(1)
    if action.startswith("write.external."):
        tiers.add(2)
    if ".pii." in action:
        tiers.add(2)
    if action.startswith("finance.") or action.startswith("legal."):
        tiers.add(3)
    return max(tiers) if tiers else None


def _rego_doc(action: str) -> dict[str, Any]:
    tier = _classify(action)
    effect = "deny" if tier is None else ("allow" if tier <= 1 else "require-approval")
    return {
        "effect": effect,
        "tier": tier,
        "reason": f"rego-faithful: {effect} (tier {tier})",
        "obligations": {},
        "policy_version": "1.0.0",
    }


def _rego_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        action = payload["input"]["action"]
        if action == CRON_BLOCK_TOOL_NAME and os.environ.get("NE_WORKER_TEST_MODE") == "cron-block":
            # cron-block choreography: mark the claimed rotation job body's
            # arrival at the governed gate, then hold it there (the SIGKILL
            # window). Deliberately a SYNCHRONOUS wait on the tick loop — the
            # runner exists to host blocking sweep bodies, and the ``block``
            # gateway's release poll set this precedent.
            marker_url = os.environ["NE_RUNTIME_SESSION_DATABASE_URL"]
            _write_marker(marker_url, "start")
            while not _has_marker(marker_url, "release"):
                time.sleep(0.2)
        return httpx.Response(200, json={"result": _rego_doc(action)})

    return httpx.MockTransport(handler)


class RegoFaithfulPdpFactory:
    """A ``policy_decision_point`` plugin returning the REAL PDP over a mock OPA."""

    @staticmethod
    def from_settings(_settings: Any, *, sink: Any) -> GovernedPdp:
        client = httpx.AsyncClient(base_url="http://opa.test", transport=_rego_transport())
        return GovernedPdp(OpaClient("http://opa.test", client=client), sink)


# --- stub brain edges (never load fastembed/qdrant) ------------------------


class StubEmbedder:
    @property
    def model_id(self) -> str:
        return "stub"

    @property
    def dimension(self) -> int:
        return 4

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]


class StubSparseEncoder:
    @property
    def model_id(self) -> str:
        return "stub-sparse"

    async def encode(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [SparseVector(indices=(), values=()) for _ in texts]

    async def encode_queries(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [SparseVector(indices=(), values=()) for _ in texts]


class StubChunkStore:
    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        return None

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        return []

    async def query(
        self,
        *,
        dense: Sequence[float] | None = None,
        sparse: SparseVector | None = None,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[ScoredChunk]:
        return []

    async def get_filtered(
        self, chunk_ids: Sequence[str], *, filters: Mapping[str, Any] | None = None
    ) -> Sequence[Chunk]:
        return []

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        return None


# --- the blockable / HITL-triggering stub gateway --------------------------


def _completion(text: str) -> Completion:
    return Completion(text=text, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


def _tool_call(name: str, arguments: dict[str, Any]) -> Completion:
    line = json.dumps({"tool_call": {"name": name, "arguments": arguments}})
    return _completion(line)


class ChoreographyGateway:
    """A stub gateway that drives the T3 SIGKILL or T4 HITL choreography.

    Mode ``block``: write a ``start`` marker into the session DB, poll for a
    ``release`` marker, then return final text. Mode ``hitl``: emit ONE tier-2 tool
    call (blocks the turn in the gate), then final text on the next call.
    """

    def __init__(self, *, mode: str, marker_url: str) -> None:
        self._mode = mode
        self._marker_url = marker_url
        self._calls = 0

    async def complete(self, request: CompletionRequest) -> Completion:
        self._calls += 1
        if self._mode == "hitl":
            if self._calls == 1:
                return _tool_call(HITL_TOOL_NAME, {"note": "hello"})
            return _completion("done")
        # block mode: mark start, then poll for release (the SIGKILL window).
        _write_marker(self._marker_url, "start")
        while not _has_marker(self._marker_url, "release"):
            time.sleep(0.2)
        return _completion("done")

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError("ChoreographyGateway does not stream")


def _hitl_binding() -> ToolBinding:
    async def _handler(args: Mapping[str, object]) -> str:
        return "ok"

    return ToolBinding(
        spec=ToolSpec(
            name=HITL_TOOL_NAME,
            description="A tier-2 external write (the HITL trigger).",
            tier=ApprovalTier.TIER_2,
            input_schema={
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": ["note"],
            },
        ),
        handler=_handler,
    )


def ci_seams() -> AppSeams:
    """Build the CI subprocess-boot seams from the environment (NOT for production)."""
    mode = os.environ.get("NE_WORKER_TEST_MODE", "block")
    marker_url = os.environ["NE_RUNTIME_SESSION_DATABASE_URL"]

    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)

    identity_settings = IdentitySettings(
        verifier="fake",
        oidc=OidcConfig(issuer=ISSUER, audience=AUDIENCE),
        principal_claims=("email", "preferred_username", "name", "department"),
    )
    gateway_settings = GatewaySettings(
        models=[
            ModelDef(
                name="stub",
                litellm_model="openai/stub",
                provider="stub",
                lane=Lane.FRONTIER_API,
                context_window=32768,
            )
        ]
    )
    return AppSeams(
        identity_settings=identity_settings,
        gateway_settings=gateway_settings,
        gateway=ChoreographyGateway(mode=mode, marker_url=marker_url),
        embedder=StubEmbedder(),
        sparse_encoder=StubSparseEncoder(),
        chunk_store=StubChunkStore(),
        plugin_registry=registry,
        extra_tool_bindings=(_hitl_binding(),),
        extra_tool_policies={
            HITL_TOOL_NAME: ToolArgPolicy(
                args={"note": ArgSpec(kind=ToolArgKind.text, required=True)}
            )
        },
    )
