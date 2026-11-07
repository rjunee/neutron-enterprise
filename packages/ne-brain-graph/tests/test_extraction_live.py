# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED: the NE-T2X extraction epic's REAL merge gate (NE-T2X-6).

**LIVE-PARKED discipline (repo law — see ``packages/ne-permissions-rebac/tests/
test_openfga_live.py`` and ``test_graph_store_conformance_server.py``).** This
module composes the REAL production stack in-process — a real
``LiteLLMGateway`` routed at a rig's vLLM (``AppSeams.gateway=None`` so
``build_platform`` builds the production gateway with the real audit sink,
cost outbox, and egress guard), a real ``AgeGraphStore`` over a real Apache
AGE container, a real Cedar PDP, a real ``DurableApprovalGate``, and a real
FastAPI app via ``httpx.ASGITransport`` — and proves the three extraction
modes, the extracted-vs-backbone escalation discrimination end-to-end, the
first-ever LIVE execution of the AGE purge Cypher, and the ``/v1/brain``
decided boundary (Argus P3-4, PR #343).

**NEVER weaken a test to make the real system pass.** If the real system
disagrees with a test here, the test is the honest party until proven
otherwise: report the divergence, do not soften the assertion, and never
tune the fixture chunk text into an instruction sheet to coax a passing
extraction out of the routed model.

Run against the rig (mode 1/2/3 + escalation + brain boundary — 8 tests,
``_live_rig``/``_live_age``-gated):

    # 1. Provision + serve vLLM (deploy/inference-operator/vast-rig.sh);
    #    verify GET /health and GET /v1/models on the rig BEFORE running.
    # 2. Start Apache AGE (byte-identical to the conformance lane's pin —
    #    deliberately NOT bumped to upstream's newer 1.7.0 release here):
    docker run --name ne-age -e POSTGRES_PASSWORD=agens -p 5455:5432 \\
        apache/age:release_PG17_1.6.0
    # 3. Run, ON the rig (loopback egress posture — PRIVATE_VLLM requires an
    #    in-VPC api_base):
    NE_T2X_LIVE_VLLM_URL=http://127.0.0.1:8090 \\
      NE_AGE_DSN=postgresql://postgres:agens@localhost:5455/postgres \\
      uv run pytest packages/ne-brain-graph/tests/test_extraction_live.py -q -rs

Optional ``NE_T2X_LIVE_VLLM_KEY`` is forwarded as the model's litellm
``api_key`` extra param when the rig's OpenAI-compatible server requires one.
The served model id is auto-derived from ``GET {VLLM_URL}/v1/models`` — there
is no env var for it.

Run the AGE-only purge leg alone (docker only, no GPU — ``_live_age``-gated;
this is the ONLY place the wholesale AGE purge Cypher executes against a real
Apache AGE instance; every other purge/revert proof in this repo runs against
``InMemoryGraphStore``):

    docker run --name ne-age -e POSTGRES_PASSWORD=agens -p 5455:5432 \\
        apache/age:release_PG17_1.6.0
    NE_AGE_DSN=postgresql://postgres:agens@localhost:5455/postgres \\
      uv run pytest packages/ne-brain-graph/tests/test_extraction_live.py -q -rs

Expected outcomes per environment:

* **rig (vLLM + AGE both up):** 9 passed, 0 skipped -- OBSERVED by
  NE-T2X-PROMPT-SCHEMA on 2026-07-29, against the same rig instance the
  orchestrator root-caused the defect on (``127.0.0.1:8090``, vLLM serving
  ``demo``->``Qwen/Qwen2.5-32B-Instruct-AWQ``) and a fresh
  ``apache/age:release_PG17_1.6.0`` container on ``:5455``: ``9 passed in
  ~38s``, reproduced twice. Before this unit's fix, 4 of these 9 legs failed
  with "the routed model produced zero valid extraction batches" (the
  prompt<->wall schema gap) and 3 more failed 422 at session creation (the
  department-claim harness drift) -- both root-caused and fixed; see the PR
  body. The orchestrator's own rig re-run remains the epic's binding merge
  gate.
* **AGE-only (docker up, no vLLM):** 2 passed (the purge leg +
  ``test_inventory_of_live_tests_pinned``), 7 skipped -- OBSERVED locally by
  NE-T2X-6 on 2026-07-27, against the builder's own
  ``apache/age:release_PG17_1.6.0`` container on a private port:
  ``2 passed, 7 skipped in 7.61s``.
* **bare CI (nothing up):** 1 passed (the inventory tripwire only), 8 skipped
  — every skip line prints the exact bring-up command. Also OBSERVED locally,
  2026-07-27: ``1 passed, 8 skipped in 2.75s``.

**RESOLVED-HISTORY (past tense; nothing below is a live blocker today).**
Two issues once blocked the AGE-only purge leg (the other 7 ``_live_rig``
legs go through ``_live_platform``/``_live_app`` -- themselves thin wrappers
over ``build_platform``/``create_app``, which freeze the config hash
internally -- so only issue (1) ever applied to them):

**(1) RESOLVED in NE-T2X-6 (this branch):** the purge leg was the only test
in this module driving ``ProposalReviewService``/``apply_extraction``
directly without building a ``Platform`` or app -- ``build_platform``/
``create_app`` freeze the process config hash as part of production boot;
this leg did not, so ``review.approve`` raised ``ConfigHashNotFrozenError``
before ever reaching AGE (a transliteration gap versus
``test_extraction_lifecycle.py``'s ``_frozen_config`` fixture, which this
leg is otherwise modeled on). Fixed by calling
``reset_config_hash_for_testing()``/``freeze_config_hash(BrainGraphSettings())``
at the top of the test. Live-verified load-bearing by mutation: commenting
out the ``freeze_config_hash`` call turns the AGE-only leg RED with exactly
``ConfigHashNotFrozenError`` at ``review.py:427``; restoring it turns the
leg GREEN again (mutation M1, recorded in this unit's commit history).

**(2) RESOLVED by NE-AGE-CYPHER-LITERALS (PR #344), out of this unit's
``mayTouch`` (``src/`` is not touched here):** every write to a real Apache
AGE server was broken -- ``ne.brain_graph._pg.execute_cypher`` bound the
graph name and query text as bind parameters, but AGE's ``cypher()`` SQL
function requires both to be literal SQL text at parse time (the query text
specifically ``$$...$$`` DOLLAR-QUOTED); neither cursor mode psycopg offers
can satisfy that via a bind parameter. PR #344 fixed this by validating the
graph name as a regex-checked identifier and splicing it plus the
dollar-quoted query text (with a collision-safe delimiter tag) directly into
the SQL text, keeping ONLY the ``::agtype`` params blob as the sole bind
parameter; the ``client_side``/``AsyncClientCursor`` fallback was REMOVED
because AGE rejects a client-interpolated params blob outright (``third
argument of cypher function must be a parameter``). Live conformance on the
existing ``test_graph_store_conformance_server.py`` went from 3/15 to 15/15
against ``apache/age:release_PG17_1.6.0``, including the residual 4-case
predicate-rung failure that was open when PR #344 started -- root-caused to
the conformance KIT hardcoding ``PredicateStyle.ANY`` instead of binding to
the store's own probed style, not a server or ``_pg.py`` defect, and fixed
in the same PR. NE-T2X-6 independently confirmed this live (see "Expected
outcomes" above and this module's ``test_live_mode3_age_purge_empties_ne_
llm_and_spares_backbone`` docstring).

**The declared fixture boundary.** T1 vector-search edges — the token
verifier, the embedder/sparse encoder, the chunk store, and the rerank stage
— are in-file FAKES mirroring ``packages/ne-api/tests/conftest.py`` by name
(a sibling package's ``conftest.py`` cannot be imported across packages under
this repo's ``--import-mode=importlib``). The parts actually LIVE-proven here
are: the routed model (via the real ``LiteLLMGateway``), Apache AGE, the real
Cedar PDP, the real ``DurableApprovalGate``, and the extraction pathway
itself. ``apply_extraction`` has NO production HTTP caller — mode 1's
"approve then readable as confirmed" leg necessarily includes an in-process,
host-side apply step (documented at its call site below), not a claim that a
console click alone performs the write.

**Known deployment gaps, stated honestly (not hidden by this harness):** the
stock `ne demo up` stack leaves Tier-2 disabled until persistent AGE and governed
source producers are composed; it does not substitute an in-memory fixture. Only
THIS module, run against a real Apache AGE container, proves the AGE leg.
Rebuild and verify the ``ne-app`` image before running it (a stale image has
silently run yesterday's code before in this repo).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import cedarpy  # noqa: F401 -- guardian import: must be real, never importorskip'd.
import pytest
import sqlalchemy as sa
from starlette.routing import Mount

from ne.api import AppSeams, create_app
from ne.api.extraction import ChunkStoreAclResolver
from ne.api.platform import Platform, build_platform
from ne.api.settings import ApiSettings
from ne.audit import ensure_transactional_sqlite
from ne.audit.config import AuditSettings
from ne.audit.store import ne_audit_log
from ne.audit.verify import verify_chain
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.extraction import EXTRACTION_TASK, LLM_SOURCE_SYSTEM
from ne.brain_graph.extraction.apply import apply_extraction, revert_extraction
from ne.brain_graph.extraction.identity import llm_entity_key
from ne.brain_graph.extraction.job import ExtractionChunk
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.identity import TokenClaims
from ne.contracts.plugins import PluginRegistry
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Chunk, Lane, Principal, ScoredChunk, SparseVector
from ne.flywheel.config import FlywheelSettings
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import (
    FixProposal,
    GraphExtractionAssertion,
    GraphExtractionAssertionKind,
    GraphExtractionBatch,
    ProposalKind,
    SqlAlchemyProposalStore,
    ensure_proposal_schema,
    graph_extraction_proposal_id,
    graph_extraction_row_id,
    ne_fix_proposals,
)
from ne.flywheel.review import (
    ProposalReviewService,
    ProposalStatus,
    RejectionCode,
    ensure_review_schema,
)
from ne.flywheel.signals import SignalProvenance, SignalSurface
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute
from ne.govern.cedar.evaluator import CedarEvaluator
from ne.govern.cedar.pdp import CedarGovernedPdp
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.identity.config import IdentitySettings, OidcConfig
from ne.identity.errors import TokenVerificationError
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import ApprovalSettings, RuntimeSettings
from ne.runtime.tools import ToolBinding
from ne.schema import SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Module-scope probes (executed at collection time; module import ALWAYS
# succeeds — skipif-pytestmark discipline, never allow_module_level, so a
# renamed/gutted suite fails CI loudly instead of skipping into a fake green)
# ---------------------------------------------------------------------------

_AGE_DSN = os.environ.get("NE_AGE_DSN", "postgresql://postgres:agens@localhost:5455/postgres")
_VLLM_URL = os.environ.get("NE_T2X_LIVE_VLLM_URL", "http://127.0.0.1:8090")
_VLLM_KEY = os.environ.get("NE_T2X_LIVE_VLLM_KEY")

_MODEL_ID: str | None = None


def _age_ok() -> bool:
    """Byte-mirror of ``test_graph_store_conformance_server.py``'s AGE probe."""
    try:
        from ne.brain_graph import _pg
    except Exception:  # pragma: no cover - psycopg import guard
        return False

    async def probe() -> bool:
        try:
            conn = await _pg.connect(_AGE_DSN)
        except Exception:
            return False
        await conn.close()
        return True

    try:
        return asyncio.run(probe())
    except Exception:
        return False


def _vllm_probe_headers() -> dict[str, str]:
    """Bearer header for the readiness probes, mirroring the api_key forwarded
    to LiteLLM via ``_rig_gateway_settings`` -- without this, an authenticated
    rig deployment answers 401 on /health and /v1/models, ``_vllm_ok`` returns
    False, and all seven rig-gated tests silently skip instead of running."""
    return {"Authorization": f"Bearer {_VLLM_KEY}"} if _VLLM_KEY else {}


def _vllm_ok() -> bool:
    """GET /health == 200 AND GET /v1/models resolves a served model id."""
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


_AGE_OK = _age_ok()
_VLLM_OK = _vllm_ok()

_RIG_BRING_UP = (
    "LIVE-PARKED (rig): needs BOTH a reachable Apache AGE and a routed vLLM. "
    "Start AGE: docker run --name ne-age -e POSTGRES_PASSWORD=agens -p 5455:5432 "
    "apache/age:release_PG17_1.6.0 -- then set NE_AGE_DSN=postgresql://postgres:"
    "agens@localhost:5455/postgres. Serve vLLM on the rig: deploy/inference-operator/vast-rig.sh "
    "-- then set NE_T2X_LIVE_VLLM_URL=http://127.0.0.1:8090 "
    "(run ON the rig -- loopback egress posture). Then: NE_T2X_LIVE_VLLM_URL=... "
    "NE_AGE_DSN=... uv run pytest packages/ne-brain-graph/tests/test_extraction_live.py -q -rs"
)
_AGE_BRING_UP = (
    "LIVE-PARKED (AGE-only): docker run --name ne-age -e POSTGRES_PASSWORD=agens "
    "-p 5455:5432 apache/age:release_PG17_1.6.0 -- then set NE_AGE_DSN=postgresql://"
    "postgres:agens@localhost:5455/postgres and run: "
    "NE_AGE_DSN=... uv run pytest packages/ne-brain-graph/tests/test_extraction_live.py -q -rs"
)

_live_rig = pytest.mark.skipif(not (_AGE_OK and _VLLM_OK), reason=_RIG_BRING_UP)
_live_age = pytest.mark.skipif(not _AGE_OK, reason=_AGE_BRING_UP)

# Only import the AGE store lazily-safe module-level symbol AFTER the probes —
# the module itself is cheap to import (psycopg only), so this is a plain
# top-level import, never importorskip'd (module imports always execute).
from ne.brain_graph.age.store import AgeGraphStore  # noqa: E402

ISSUER = "https://idp.ne.test/realms/ne-test"
_SOURCE = "docs"
_CFG = "e" * 64

_ExtractionMode = Literal["ingest_review", "read_flagged", "autonomous"]


def run(coro: Any) -> Any:
    """Drive a coroutine to completion (no pytest-asyncio in this repo)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# In-file harness (replicated by name from packages/ne-api/tests/conftest.py
# and test_provisional_recorder_wiring.py -- a sibling package's conftest
# cannot be imported here under --import-mode=importlib)
# ---------------------------------------------------------------------------


class FakeTokenVerifier:
    """Maps registered opaque tokens -> verified TokenClaims; else raises."""

    def __init__(self, _oidc: OidcConfig | None = None) -> None:
        self._tokens: dict[str, TokenClaims] = {}

    def mint(
        self, subject: str, *, groups: Sequence[str] = (), department: str | None = None
    ) -> str:
        token = uuid4().hex
        claims: dict[str, Any] = {"sub": subject, "iss": ISSUER, "groups": list(groups)}
        if department is not None:
            claims["department"] = department
        self._tokens[token] = TokenClaims(subject=subject, issuer=ISSUER, claims=claims)
        return token

    async def verify(self, token: str) -> TokenClaims:
        claims = self._tokens.get(token)
        if claims is None:
            raise TokenVerificationError("unknown token")
        return claims


def _bearer(
    verifier: FakeTokenVerifier,
    subject: str,
    *,
    groups: Sequence[str] = (),
    department: str | None = None,
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {verifier.mint(subject, groups=groups, department=department)}"
    }


class StaticEmbedder:
    @property
    def model_id(self) -> str:
        return "static"

    @property
    def dimension(self) -> int:
        return 4

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]


class StaticSparseEncoder:
    @property
    def model_id(self) -> str:
        return "static-sparse"

    async def encode(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [SparseVector(indices=(), values=()) for _ in texts]

    async def encode_queries(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [SparseVector(indices=(), values=()) for _ in texts]


class PassthroughReranker:
    @property
    def model_id(self) -> str:
        return "fake-reranker"

    async def rerank(
        self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
    ) -> Sequence[ScoredChunk]:
        return tuple(ScoredChunk(chunk=c, score=1.0) for c in chunks[:top_k])


def _match_any(chunk: Chunk, filters: Mapping[str, Any] | None) -> bool:
    if not filters:
        return True
    if "$any" in filters:
        return any(_match_any(chunk, sub) for sub in filters["$any"])
    for key, wanted in filters.items():
        if key == "acl":
            if not (set(chunk.acl) & set(wanted)):
                return False
        elif key == "document_id":
            if chunk.document_id not in set(wanted):
                return False
        else:
            return False
    return True


class InMemoryFilteredChunkStore:
    """A ChunkStore + FilteredChunkFetch honoring MatchAny on the 'acl' key."""

    def __init__(self, chunks: Sequence[Chunk] = ()) -> None:
        self._chunks: dict[str, Chunk] = {c.chunk_id: c for c in chunks}

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        for c in chunks:
            self._chunks[c.chunk_id] = c

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        return [self._chunks[c] for c in chunk_ids if c in self._chunks]

    async def query(
        self,
        *,
        dense: Sequence[float] | None = None,
        sparse: SparseVector | None = None,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[ScoredChunk]:
        visible = [c for c in self._chunks.values() if _match_any(c, filters)]
        return [ScoredChunk(chunk=c, score=1.0) for c in visible[:top_k]]

    async def get_filtered(
        self, chunk_ids: Sequence[str], *, filters: Mapping[str, Any] | None = None
    ) -> Sequence[Chunk]:
        out: list[Chunk] = []
        for cid in chunk_ids:
            c = self._chunks.get(cid)
            if c is not None and _match_any(c, filters):
                out.append(c)
        return out

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        for cid in chunk_ids:
            self._chunks.pop(cid, None)


def chunk(chunk_id: str, *, acl: Sequence[str], text: str, document_id: str | None = None) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id=document_id or f"doc-{chunk_id}",
        text=text,
        metadata={"source_system": "wiki", "source_ref": f"ref-{chunk_id}"},
        acl=tuple(acl),
    )


class CedarPdpFactory:
    """A ``policy_decision_point`` plugin factory over the REAL Cedar engine
    (the ne-runtime Cedar-guardian precedent, ``test_provisional_recorder_
    wiring.py:186-202``). Registered under the plugin name "opa" — a
    pre-existing property of the harness seam, not a claim about the backend.
    """

    @staticmethod
    def from_settings(_settings: Any, *, sink: Any) -> CedarGovernedPdp:
        return CedarGovernedPdp(CedarEvaluator(), sink)


class _StaticExtractionChunkSource:
    def __init__(self, by_source: dict[str, list[ExtractionChunk]]) -> None:
        self._by_source = by_source

    async def chunks(self, source_system: str) -> Sequence[ExtractionChunk]:
        return self._by_source.get(source_system, [])


_WRITE_ACTION = "write.internal.notes.append"


def _write_binding(ran: list[str]) -> ToolBinding:
    async def _handler(args: Mapping[str, object]) -> str:
        ran.append(str(args.get("note", "")))
        return "ok"

    return ToolBinding(
        spec=ToolSpec(
            name=_WRITE_ACTION,
            description="append a note",
            tier=ApprovalTier.TIER_1,
            input_schema={
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": ["note"],
            },
        ),
        handler=_handler,
    )


def get_state(app: Any) -> Any:
    for route in app.routes:
        if isinstance(route, Mount) and route.path == "/api":
            middleware: Any = route.app  # the OidcAuthMiddleware ASGI wrapper
            inner = middleware._app  # its inner FastAPI app carries state.ne_api
            return inner.state.ne_api
    raise AssertionError("no /api mount found")


def _client(app: Any) -> Any:
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ne.test")


def _api_settings(**overrides: object) -> ApiSettings:
    base: dict[str, object] = {
        "qdrant_url": "http://qdrant.test",
        "qdrant_collection": "t",
        "tei_url": "http://tei.test",
    }
    base.update(overrides)
    return ApiSettings(**base)  # type: ignore[arg-type]


def _rig_gateway_settings() -> GatewaySettings:
    """The ONE routed model, both tasks (graph-extract + agent) pointed at it.

    ``PRIVATE_VLLM`` + a loopback/in-VPC ``api_base`` (egress.py:176-198) —
    the rig posture is to run ON the rig against 127.0.0.1; a public-IP
    misconfiguration fails loudly (the documented, correct behavior).
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
    route = TaskRoute(model="rig-vllm")
    return GatewaySettings(models=[model], tasks={"graph-extract": route, "agent": route})


@dataclass(frozen=True)
class _Urls:
    runtime_url: str
    audit_url: str
    perms_url: str
    flywheel_url: str


def _urls(tmp_path: Path, key: str) -> _Urls:
    return _Urls(
        runtime_url=f"sqlite:///{tmp_path}/runtime-{key}.db",
        audit_url=f"sqlite:///{tmp_path}/audit-{key}.db",
        perms_url=f"sqlite:///{tmp_path}/perms-{key}.db",
        flywheel_url=f"sqlite:///{tmp_path}/flywheel-{key}.db",
    )


def _live_platform(
    urls: _Urls,
    mode: _ExtractionMode,
    *,
    graph_store: AgeGraphStore,
    chunk_store: InMemoryFilteredChunkStore,
    extraction_chunks: Sequence[ExtractionChunk],
) -> Platform:
    """(a) The REAL production platform composition (NE-T2X-6 brief (a)).

    ``seams.gateway=None`` makes ``build_platform`` construct the PRODUCTION
    ``LiteLLMGateway`` via ``_default_gateway`` (platform.py:1864-1893) with
    the real audit sink, ``OutboxCostSink``, and egress guard -- ZERO
    scripted gateways anywhere in this file.
    """
    registry = PluginRegistry()
    verifier = FakeTokenVerifier()
    registry.register("token_verifier", "fake", lambda oidc: verifier)
    registry.register("policy_decision_point", "opa", CedarPdpFactory)
    reset_config_hash_for_testing()
    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
            principal_claims=("email", "department"),
        ),
        runtime_settings=RuntimeSettings(session_database_url=urls.runtime_url),
        audit_settings=AuditSettings(database_url=urls.audit_url),
        permissions_settings=PermissionsSettings(database_url=urls.perms_url),
        flywheel_settings=FlywheelSettings(signal_database_url=urls.flywheel_url),
        gateway_settings=_rig_gateway_settings(),
        gateway=None,
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=chunk_store,
        reranker=PassthroughReranker(),
        plugin_registry=registry,
        brain_graph_settings=BrainGraphSettings(
            enabled=True,
            extraction_enabled=True,
            extraction_sources=(_SOURCE,),
            extraction_mode=mode,
            extraction_risk_accepted=(mode == "autonomous"),
        ),
        graph_store=graph_store,
        extraction_chunk_source=_StaticExtractionChunkSource({_SOURCE: list(extraction_chunks)}),
    )
    return build_platform(_api_settings(), seams)


def _live_app(
    urls: _Urls,
    *,
    chunk_store: InMemoryFilteredChunkStore,
    steward_roles: tuple[str, ...] = (),
    approver_roles: tuple[str, ...] = (),
    brain_graph_settings: BrainGraphSettings | None = None,
    graph_store: AgeGraphStore | None = None,
    extra_tool_bindings: tuple[ToolBinding, ...] = (),
    extra_tool_policies: Mapping[str, ToolArgPolicy] | None = None,
) -> tuple[Any, FakeTokenVerifier]:
    """(b) The REAL FastAPI app composition (NE-T2X-6 brief (b)), for HTTP legs."""
    registry = PluginRegistry()
    verifier = FakeTokenVerifier()
    registry.register("token_verifier", "fake", lambda oidc: verifier)
    registry.register("policy_decision_point", "opa", CedarPdpFactory)
    reset_config_hash_for_testing()
    seams = AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
            principal_claims=("email", "preferred_username", "name", "department"),
        ),
        runtime_settings=RuntimeSettings(
            # expiry_s: the default 86400.0 (packages/ne-runtime/src/ne/runtime/
            # config.py:53) would leave a chat turn blocked in
            # DurableApprovalGate.await_outcome for up to 24h when a Tier-2
            # write escalates and nothing approves it -- httpx.ASGITransport
            # does not honour httpx client timeouts, so nothing else stops
            # this. 120.0 is chosen to fail an unapproved write fast and
            # loudly while staying > _ESCALATION_DEADLINE_S (90.0, see below)
            # so a legitimately-pending approval in the approving arms
            # (tests 6/7) is never expired out from under it.
            session_database_url=urls.runtime_url,
            approvals=ApprovalSettings(poll_interval_s=0.05, expiry_s=120.0),
        ),
        audit_settings=AuditSettings(database_url=urls.audit_url),
        permissions_settings=PermissionsSettings(database_url=urls.perms_url),
        flywheel_settings=FlywheelSettings(signal_database_url=urls.flywheel_url),
        gateway_settings=_rig_gateway_settings(),
        gateway=None,
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=chunk_store,
        reranker=PassthroughReranker(),
        plugin_registry=registry,
        brain_graph_settings=brain_graph_settings,
        graph_store=graph_store,
        extra_tool_bindings=extra_tool_bindings,
        extra_tool_policies=extra_tool_policies,
    )
    app = create_app(
        _api_settings(steward_roles=steward_roles, approver_roles=approver_roles),
        seams=seams,
    )
    return app, verifier


def _dispose(*engines: sa.Engine) -> None:
    for engine in engines:
        engine.dispose()


# ---------------------------------------------------------------------------
# Fixture corpus (deterministic, first-party -- never ne-acme, the default
# sync omits it)
# ---------------------------------------------------------------------------

_CHUNK_REF = "c-live-1"
_DOCUMENT_REF = "d-live-1"
_CHUNK_TEXT = (
    "Initech Corporation is a newly signed customer account. "
    "Initech Corporation was referred to us by Acme Corporation."
)


async def _seed_backbone(store: AgeGraphStore) -> None:
    """Account 'Acme Corporation' + one Person -- teaches the real model the
    valid entity_class vocabulary via job.py's candidate lines (:495-516).

    ``ensure_graph()`` (label tables + predicate-rung probe, DDL) MUST run
    before any read/write against a freshly-named AGE graph -- mirrors every
    ``GraphStoreContractKit`` test method's own first call.
    """
    await store.ensure_graph()
    await store.upsert_nodes(
        [
            make_node(
                "seed-acme",
                "Account",
                ("eng",),
                display_name="Acme Corporation",
                source_ref="seed:acme",
            ),
            make_node(
                "seed-jane", "Person", ("eng",), display_name="Jane Doe", source_ref="seed:jane"
            ),
        ]
    )


def _extraction_chunks() -> list[ExtractionChunk]:
    return [
        ExtractionChunk(
            chunk_ref=_CHUNK_REF,
            document_ref=_DOCUMENT_REF,
            source_system=_SOURCE,
            text=_CHUNK_TEXT,
            acl=("eng",),
        )
    ]


def _chunk_store() -> InMemoryFilteredChunkStore:
    return InMemoryFilteredChunkStore(
        [chunk(_CHUNK_REF, acl=["eng"], text=_CHUNK_TEXT, document_id=_DOCUMENT_REF)]
    )


def _prov(actor: str = "steward") -> SignalProvenance:
    return SignalProvenance(
        principal=Principal(id=actor), surface=SignalSurface.CONSOLE, session_id="live-s1"
    )


async def _llm_nodes(store: AgeGraphStore) -> list[Mapping[str, Any]]:
    return [n for n in await store.scan_nodes() if n.get("source_system") == LLM_SOURCE_SYSTEM]


_SOURCE_REF_RE = re.compile(r"^llm:.+:c-live-1$")


# ---------------------------------------------------------------------------
# TEST 1 -- mode 1 (ingest_review): console queue + approve => confirmed
# ---------------------------------------------------------------------------


@_live_rig
def test_live_mode1_ingest_review_console_queue_approve_confirm(tmp_path: Path) -> None:
    urls = _urls(tmp_path, "mode1")
    graph_store = AgeGraphStore(_AGE_DSN, graph_name=f"ne_t2x_{uuid4().hex[:8]}")
    chunk_store = _chunk_store()
    platform = _live_platform(
        urls,
        "ingest_review",
        graph_store=graph_store,
        chunk_store=chunk_store,
        extraction_chunks=_extraction_chunks(),
    )
    app, verifier = _live_app(urls, chunk_store=chunk_store, steward_roles=("stewards",))

    async def _body() -> None:
        try:
            await _seed_backbone(graph_store)
            assert platform.extraction is not None
            result = await platform.extraction.run()
            assert result.report.batches_built >= 1, (
                "the routed model produced zero valid extraction batches -- a real "
                "finding about the shipped prompt vs the routed model, not a harness bug"
            )
            assert result.writes == {}  # mode 1: proposals only, nothing written
            assert await _llm_nodes(graph_store) == []
            pid = result.report.batch_proposal_ids[0]

            steward = _bearer(verifier, "user:steward", groups=["stewards"])
            async with _client(app) as client:
                resp = await client.get("/api/v1/steward/proposals", headers=steward)
                assert resp.status_code == 200
                proposals = resp.json()["proposals"]
                match = next((p for p in proposals if p["proposal_id"] == pid), None)
                assert match is not None, "the extraction proposal never reached the console queue"
                assert match["kind"] == "graph_extraction_candidate"
                assert "initech" in match["artifact"]["patch"].casefold(), (
                    "the rendered steward artifact must carry the extracted entity's "
                    "display name, not just a bare non-empty payload"
                )

                approve_resp = await client.post(
                    f"/api/v1/steward/proposals/{pid}/approve", json={}, headers=steward
                )
                assert approve_resp.status_code == 200
                # NE-T2X-PROMPT-SCHEMA: the wire response is a ReviewEventView
                # (ne.api.wire) -- it has never carried a "status" key, only
                # "to_status" (ne.api.wire.ReviewEventView; mirrors sibling
                # assertions at test_steward_api.py:113,
                # test_verified_curation_api.py:356/552,
                # test_steward_skills_rot.py:254). This leg never reached far
                # enough to exercise the old assertion before this unit's fix
                # (batches_built was always 0), so the stale key never turned
                # red until now -- harness fixture drift, not a system defect.
                assert approve_resp.json()["to_status"] == "approved"

            # Host-side apply: apply_extraction has NO production HTTP caller
            # (a decided property of this design) -- the console approve above
            # only flips the NE-066 gate; a host composition (here, this test,
            # standing in for that host) performs the actual graph write.
            apply_engine = sa.create_engine(urls.flywheel_url)
            try:
                review_service = ProposalReviewService(apply_engine)
                applied_ref = await apply_extraction(
                    apply_engine,
                    review_service,
                    graph_store,
                    ChunkStoreAclResolver(chunk_store),
                    pid,
                    actor="steward",
                    audit_sink=platform.sink,
                    config_hash=platform.config_hash,
                    schema_version=SCHEMA_VERSION,
                )
            finally:
                apply_engine.dispose()
            assert applied_ref.startswith("extraction:")

            confirmed_nodes = [
                n for n in await _llm_nodes(graph_store) if n.get("assurance") == "confirmed"
            ]
            assert confirmed_nodes, "apply_extraction did not land a confirmed ne:llm node in AGE"
            initech = next(
                (
                    n
                    for n in confirmed_nodes
                    if "initech" in str(n.get("display_name", "")).casefold()
                ),
                None,
            )
            assert initech is not None, (
                f"no confirmed node named like 'initech' -- got display names "
                f"{[n.get('display_name') for n in confirmed_nodes]}"
            )
            assert initech["source_system"] == LLM_SOURCE_SYSTEM
            assert tuple(initech["acl"]) == ("eng",)
        finally:
            await graph_store.close()

    try:
        run(_body())
    finally:
        _dispose(*platform.engines)
        _dispose(*get_state(app).engines)


# ---------------------------------------------------------------------------
# TEST 2 -- mode 2 (read_flagged): immediate flag, C7 trim, confirm clears it
# ---------------------------------------------------------------------------


@_live_rig
def test_live_mode2_read_flagged_immediate_flag_then_confirm_clears(tmp_path: Path) -> None:
    urls = _urls(tmp_path, "mode2")
    graph_store = AgeGraphStore(_AGE_DSN, graph_name=f"ne_t2x_{uuid4().hex[:8]}")
    chunk_store = _chunk_store()
    platform = _live_platform(
        urls,
        "read_flagged",
        graph_store=graph_store,
        chunk_store=chunk_store,
        extraction_chunks=_extraction_chunks(),
    )

    async def _body() -> None:
        try:
            await _seed_backbone(graph_store)
            assert platform.extraction is not None
            result = await platform.extraction.run()
            assert result.report.batches_built >= 1, (
                "the routed model produced zero valid extraction batches -- a real "
                "finding about the shipped prompt vs the routed model"
            )
            pid = result.report.batch_proposal_ids[0]
            assert result.writes[pid].get("nodes", 0) >= 1, result.writes[pid]

            llm_nodes = await _llm_nodes(graph_store)
            assert llm_nodes, "mode 2 wrote nothing to AGE"
            for node in llm_nodes:
                assert node.get("assurance") == "extracted"
                assert tuple(node.get("acl", ())) == ("eng",)
                assert _SOURCE_REF_RE.match(str(node.get("source_ref", ""))), node.get("source_ref")

            initech = next(
                (n for n in llm_nodes if "initech" in str(n.get("display_name", "")).casefold()),
                None,
            )
            assert initech is not None, [n.get("display_name") for n in llm_nodes]
            source_ref = str(initech["source_ref"])

            # READ-THROUGH over REAL AGE, principal-scoped.
            assert platform.entity_graph is not None
            eng_reader = Principal(id="live-eng-reader", groups=("eng",))
            visible = await platform.entity_graph.lookup(
                source_refs=[source_ref], principal=eng_reader
            )
            assert len(visible) == 1
            assert visible[0].assurance == "extracted"

            # C7 negative control: an out-of-ACL principal gets NOTHING.
            outsider = Principal(id="live-outsider", groups=())
            trimmed = await platform.entity_graph.lookup(
                source_refs=[source_ref], principal=outsider
            )
            assert trimmed == (), "an out-of-ACL principal must never see an extracted element"

            # CONFIRM -> assurance flips to 'confirmed' for every future reader.
            write_ctx = platform.extraction.write_ctx
            assert write_ctx is not None
            write_ctx.review_service.approve(pid, _prov())
            await apply_extraction(
                write_ctx.engine,
                write_ctx.review_service,
                write_ctx.store,
                write_ctx.acl_resolver,
                pid,
                actor="steward",
                audit_sink=platform.sink,
                config_hash=platform.config_hash,
                schema_version=SCHEMA_VERSION,
            )
            confirmed = await platform.entity_graph.lookup(
                source_refs=[source_ref], principal=eng_reader
            )
            assert len(confirmed) == 1
            assert confirmed[0].assurance == "confirmed"
        finally:
            await graph_store.close()

    try:
        run(_body())
    finally:
        _dispose(*platform.engines)


# ---------------------------------------------------------------------------
# TEST 3 -- mode 2 reject/revert + no-auto-promotion (design §6.2 absence pin)
# ---------------------------------------------------------------------------


@_live_rig
def test_live_mode2_reject_reverts_and_no_auto_promotion(tmp_path: Path) -> None:
    urls = _urls(tmp_path, "mode2-reject")
    graph_store = AgeGraphStore(_AGE_DSN, graph_name=f"ne_t2x_{uuid4().hex[:8]}")
    chunk_store = _chunk_store()
    platform = _live_platform(
        urls,
        "read_flagged",
        graph_store=graph_store,
        chunk_store=chunk_store,
        extraction_chunks=_extraction_chunks(),
    )

    async def _body() -> None:
        try:
            await _seed_backbone(graph_store)
            assert platform.extraction is not None
            result = await platform.extraction.run()
            assert result.report.batches_built >= 1, (
                "the routed model produced zero valid extraction batches -- a real finding"
            )
            pid = result.report.batch_proposal_ids[0]
            llm_nodes = await _llm_nodes(graph_store)
            initech = next(
                (n for n in llm_nodes if "initech" in str(n.get("display_name", "")).casefold()),
                None,
            )
            assert initech is not None, [n.get("display_name") for n in llm_nodes]
            key = str(initech["key"])
            assert platform.entity_graph is not None
            reader = Principal(id="live-eng-reader-2", groups=("eng",))

            # NO-AUTO-PROMOTION (design §6.2: "No third state, no expiry, no
            # auto-promotion on read-count or age (rejected as trust-
            # laundering: popularity is not verification).").
            # 10 reads (up from an earlier 3-read tripwire) pins the
            # prohibition at any read-count threshold reachable here, and the
            # full stored-mapping equality below pins it against ANY
            # read-triggered bookkeeping (not just the `assurance` field) --
            # the mechanism any count- or age-based promotion/expiry would
            # require. This is a live tripwire, not an exhaustiveness proof:
            # a build-time grep over packages/ne-brain-graph/src for
            # promot|timestamp|expir returns nothing, so no read-count- or
            # age-keyed promotion/expiry path exists in source today. That
            # grep does NOT clear the writer of stamping time at all --
            # ExtractionWriter DOES set `synced_at` on writes (writer.py:225,
            # 246, 301; also er/merge.py:142,196 and construct/runner.py:227,
            # observed live in this test's own row data during mutation
            # M2: `synced_at: '2026-07-09T00:00:00+00:00'`) -- which is
            # exactly why the guard below is a full stored-mapping equality
            # rather than an `assurance`-only check: a read that bumped
            # `synced_at`, or any other field, would be caught. What the grep
            # DOES establish is narrower: no read path compares a stored
            # timestamp against anything, and the store runs no background
            # task -- so nothing in source consults that stamp to justify a
            # promotion. This assertion is what forces the pin to be
            # revisited the day that changes.
            before = await graph_store.fetch_nodes([key])
            for i in range(10):
                hits = await platform.entity_graph.lookup(
                    source_refs=[str(initech["source_ref"])], principal=reader
                )
                assert hits, (
                    f"read {i} never actually retrieved the extracted node -- "
                    "the no-auto-promotion pin below would be vacuous if the "
                    "read path stopped reaching it (source_ref drift, an "
                    "ACL/label change, or _node_visible trimming every row "
                    "would all silently no-op the loop)"
                )
            after = await graph_store.fetch_nodes([key])
            assert after == before, (
                "stored node state changed after mere reads -- reads must be "
                "observationally side-effect-free"
            )
            assert after and after[0]["assurance"] == "extracted", (
                "assurance changed after mere reads -- popularity must never verify a fact"
            )

            write_ctx = platform.extraction.write_ctx
            assert write_ctx is not None
            write_ctx.review_service.reject(
                pid, _prov(), code=RejectionCode.INCORRECT, note="live reject"
            )
            revert_result = await revert_extraction(
                write_ctx.engine,
                write_ctx.review_service,
                write_ctx.store,
                pid,
                actor="steward",
                audit_sink=platform.sink,
                config_hash=platform.config_hash,
            )
            assert revert_result["nodes_deleted"] >= 1
            assert await graph_store.fetch_nodes([key]) == [], (
                "the rejected node must be gone from AGE"
            )
        finally:
            await graph_store.close()

    try:
        run(_body())
    finally:
        _dispose(*platform.engines)


# ---------------------------------------------------------------------------
# TEST 4 -- mode 3 (autonomous): unreviewed write + audit chain reconstruction
# ---------------------------------------------------------------------------


@_live_rig
def test_live_mode3_autonomous_write_audit_reconstruction(tmp_path: Path) -> None:
    urls = _urls(tmp_path, "mode3")
    graph_store = AgeGraphStore(_AGE_DSN, graph_name=f"ne_t2x_{uuid4().hex[:8]}")
    chunk_store = _chunk_store()
    platform = _live_platform(
        urls,
        "autonomous",
        graph_store=graph_store,
        chunk_store=chunk_store,
        extraction_chunks=_extraction_chunks(),
    )

    async def _body() -> None:
        try:
            await _seed_backbone(graph_store)
            assert platform.extraction is not None
            result = await platform.extraction.run()
            assert result.report.batches_built >= 1, (
                "the routed model produced zero valid extraction batches -- a real finding"
            )
            pid = result.report.batch_proposal_ids[0]
            llm_nodes = await _llm_nodes(graph_store)
            initech = next(
                (n for n in llm_nodes if "initech" in str(n.get("display_name", "")).casefold()),
                None,
            )
            assert initech is not None, [n.get("display_name") for n in llm_nodes]
            assert initech["assurance"] == "extracted"

            write_ctx = platform.extraction.write_ctx
            assert write_ctx is not None
            # The YOLO meaning: never approved, ever.
            assert write_ctx.review_service.status_of(pid) is ProposalStatus.PROPOSED

            await platform.extraction.emit_mode_event()

            # -- audit chain reconstruction: run + chunk + model -------------
            audit_engine = platform.audit_engine
            with audit_engine.connect() as conn:
                rows = conn.execute(sa.select(ne_audit_log.c.action, ne_audit_log.c.payload)).all()
            by_action: dict[str, list[Any]] = {}
            for row in rows:
                by_action.setdefault(row.action, []).append(dict(row.payload))

            extraction_events = by_action.get("graph.extraction", [])
            assert extraction_events, "no graph.extraction audit event was emitted"
            run_detail = json.loads(extraction_events[-1]["detail"])
            assert run_detail["run_id"] == result.report.run_id

            gateway_events = by_action.get("ne.gateway.complete", [])
            matching_model_calls = [
                e
                for e in gateway_events
                if e.get("provenance", {}).get("task") == EXTRACTION_TASK
                and e.get("provenance", {}).get("model_name") == "rig-vllm"
            ]
            assert matching_model_calls, (
                f"no ne.gateway.complete event for task={EXTRACTION_TASK!r} model_name='rig-vllm'; "
                f"got {[e.get('provenance') for e in gateway_events]}"
            )

            source_ref = str(initech["source_ref"])
            assert _SOURCE_REF_RE.match(source_ref), source_ref  # chunk link (c-live-1 half)
            evidence = [str(e) for e in initech.get("evidence", [])]
            assert evidence, "the autonomous node carries no evidence ref"
            batch_pid = evidence[0].split("|", 1)[0]

            proposal_engine = write_ctx.engine
            with proposal_engine.connect() as conn:
                proposal_row = conn.execute(
                    sa.select(ne_fix_proposals.c.payload).where(
                        ne_fix_proposals.c.proposal_id == batch_pid
                    )
                ).first()
            assert proposal_row is not None
            assert dict(proposal_row.payload)["run_id"] == result.report.run_id  # run link

            mode_events = by_action.get("graph.extraction_mode", [])
            assert mode_events
            assert json.loads(mode_events[-1]["detail"]) == {"mode": "autonomous"}

            verification = verify_chain(audit_engine)
            assert verification.valid
            assert verification.checked > 0
        finally:
            await graph_store.close()

    try:
        run(_body())
    finally:
        _dispose(*platform.engines)


# ---------------------------------------------------------------------------
# TEST 5 -- THE first live execution of the AGE purge Cypher (AGE-only gate)
# ---------------------------------------------------------------------------


def _mint(
    kind: GraphExtractionAssertionKind,
    *,
    entity_class: str = "",
    entity_key: str = "",
    display: str = "",
    properties: dict[str, Any] | None = None,
    subject_key: str = "",
    edge_kind: str = "",
    object_key: str = "",
    confidence: float = 0.9,
    evidence_ref: str,
) -> GraphExtractionAssertion:
    draft = GraphExtractionAssertion(
        row_id="pending",
        kind=kind,
        entity_class=entity_class,
        entity_key=entity_key,
        display=display,
        properties=properties or {},
        subject_key=subject_key,
        edge_kind=edge_kind,
        object_key=object_key,
        confidence=confidence,
        evidence_ref=evidence_ref,
    )
    return draft.model_copy(update={"row_id": graph_extraction_row_id(draft)})


def _seed_extraction_proposal(
    engine: sa.Engine, document_ref: str, assertions: Sequence[GraphExtractionAssertion]
) -> str:
    ensure_proposal_schema(engine)
    proposal_id, sha16 = graph_extraction_proposal_id(document_ref, assertions)
    batch = GraphExtractionBatch(
        document_ref=document_ref,
        run_id="live-purge-run",
        source_system=_SOURCE,
        assertions=tuple(assertions),
    )
    proposal = FixProposal(
        proposal_id=proposal_id,
        kind=ProposalKind.GRAPH_EXTRACTION_CANDIDATE,
        lane=FixLane.ENTITY_RESOLUTION,
        cluster_id=f"extract:{sha16}",
        evidence_event_ids=(),
        evidence_signal_ids=(),
        summary=f"live purge-leg batch over {document_ref}",
        confidence=max(a.confidence for a in assertions),
        priority=ProposalPriority.MEDIUM,
        config_hash=_CFG,
        created_at=datetime(2026, 7, 27, tzinfo=UTC),
        payload=batch.model_dump(mode="json"),
    )
    with engine.begin() as conn:
        SqlAlchemyProposalStore(engine).append(conn, proposal)
    return proposal_id


class _DictAclResolver:
    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self._mapping = mapping

    async def acl_for(self, chunk_ref: str) -> tuple[str, ...]:
        return self._mapping.get(chunk_ref, ())


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


@_live_age
def test_live_mode3_age_purge_empties_ne_llm_and_spares_backbone(tmp_path: Path) -> None:
    """THE primary objective: purge_llm_extracted has NEVER executed against
    real AGE before this test. No model needed -- ExtractionWriter (via
    apply_extraction) is the sole sanctioned ne:llm writer, mirrored here per
    test_extraction_lifecycle.py's writer/apply composition, swapping
    InMemoryGraphStore -> AgeGraphStore.

    Unlike every other test in this module, this leg builds NO ``Platform``
    and NO app -- ``build_platform``/``create_app`` freeze the process config
    hash internally as part of production boot, but this test drives
    ``ProposalReviewService``/``apply_extraction`` directly, so it must freeze
    the hash itself (mirrors ``test_extraction_lifecycle.py``'s
    ``_frozen_config`` fixture) or ``review.approve`` raises
    ``ConfigHashNotFrozenError`` before ever reaching AGE.

    HISTORY: the ``_pg.py`` defect that once blocked every write to a real
    Apache AGE server (module docstring, resolved-history item (2)) was
    fixed by NE-AGE-CYPHER-LITERALS (PR #344). OBSERVED, NE-T2X-6,
    2026-07-27, against the builder's own ``apache/age:release_PG17_1.6.0``
    container: this leg passes green end-to-end -- the count equality, the
    zero-remaining rescan, the backbone-survival control, and the
    purged-key-fetches-empty check all held live (``pytest -q``:
    ``2 passed, 7 skipped in 7.61s`` for the module; this test alone:
    ``1 passed``). This is the first-ever live execution of the AGE purge
    Cypher end-to-end."""
    reset_config_hash_for_testing()
    freeze_config_hash(BrainGraphSettings())
    store = AgeGraphStore(_AGE_DSN, graph_name=f"ne_t2x_purge_{uuid4().hex[:8]}")
    engine = sa.create_engine(f"sqlite:///{tmp_path}/purge-flywheel.db")
    ensure_transactional_sqlite(engine)
    ensure_review_schema(engine)
    ensure_proposal_schema(engine)

    async def _body() -> None:
        try:
            await store.ensure_graph()
            # Backbone controls (source_system='fixture') -- must SURVIVE.
            backbone_a = make_node(
                "purge-backbone-a", "Account", ("eng",), display_name="Backbone Co"
            )
            backbone_b = make_node(
                "purge-backbone-b", "Person", ("eng",), display_name="Backbone Person"
            )
            backbone_edge = make_edge("WORKS_AT", "purge-backbone-b", "purge-backbone-a", ("eng",))
            await store.upsert_nodes([backbone_a, backbone_b])
            await store.upsert_edges([backbone_edge])

            # entity_key MUST equal the writer's recomputed identity
            # (write_entity recomputes from (entity_class, display) -- see
            # apply.py's _validate_new_entities docstring) or the batch is
            # refused as a tampered/stale payload before anything is written.
            doc_key = llm_entity_key("Document", "Live Purge Doc")
            acct_key = llm_entity_key("Account", "Purge Account")
            doc_a = _mint(
                GraphExtractionAssertionKind.NEW_ENTITY,
                entity_class="Document",
                entity_key=doc_key,
                display="Live Purge Doc",
                properties={"title": "Live Purge Doc", "doc_type": "contract"},
                evidence_ref="c-purge",
            )
            acct_a = _mint(
                GraphExtractionAssertionKind.NEW_ENTITY,
                entity_class="Account",
                entity_key=acct_key,
                display="Purge Account",
                properties={"name": "Purge Account"},
                evidence_ref="c-purge",
            )
            edge_a = _mint(
                GraphExtractionAssertionKind.NEW_EDGE,
                subject_key=doc_key,
                edge_kind="ABOUT",
                object_key=acct_key,
                evidence_ref="c-purge",
            )
            pid = _seed_extraction_proposal(engine, "d-live-purge", [doc_a, acct_a, edge_a])
            review = ProposalReviewService(engine)
            review.approve(pid, _prov())
            applied_ref = await apply_extraction(
                engine,
                review,
                store,
                _DictAclResolver({"c-purge": ("eng",)}),
                pid,
                actor="op",
                audit_sink=_RecordingAudit(),
                config_hash=_CFG,
                schema_version=SCHEMA_VERSION,
            )
            assert applied_ref == "extraction:2n:1e"

            llm_nodes_before = await _llm_nodes(store)
            llm_edges_before = [
                e for e in await store.scan_edges() if e.get("source_system") == LLM_SOURCE_SYSTEM
            ]
            assert len(llm_nodes_before) == 2, llm_nodes_before
            assert len(llm_edges_before) == 1, llm_edges_before

            counts = await store.purge_llm_extracted()
            assert counts == {"nodes": 2, "edges": 1}, counts

            llm_nodes_after = await _llm_nodes(store)
            llm_edges_after = [
                e for e in await store.scan_edges() if e.get("source_system") == LLM_SOURCE_SYSTEM
            ]
            assert llm_nodes_after == []
            assert llm_edges_after == []

            # Bidirectional control: the backbone node + edge SURVIVE.
            surviving = await store.fetch_nodes(["purge-backbone-a", "purge-backbone-b"])
            assert len(surviving) == 2
            surviving_edges = [
                e for e in await store.scan_edges() if e.get("source_system") == "fixture"
            ]
            assert len(surviving_edges) == 1

            # A purged key fetches empty.
            assert await store.fetch_nodes([doc_key]) == []
            assert await store.fetch_nodes([acct_key]) == []
        finally:
            await store.close()

    try:
        run(_body())
    finally:
        engine.dispose()
        reset_config_hash_for_testing()


# ---------------------------------------------------------------------------
# TESTS 6+7 -- escalation discrimination: extracted-context write requires
# approval; the byte-identical backbone-only arc executes inline
# ---------------------------------------------------------------------------


async def _seed_escalation_graph(store: AgeGraphStore, *, extracted: bool) -> None:
    """Mirrors test_provisional_recorder_wiring.py's ``_seed_graph`` exactly,
    swapping InMemoryGraphStore -> AgeGraphStore. ``ensure_graph()`` first --
    see ``_seed_backbone``'s docstring."""
    await store.ensure_graph()
    node_b_props: dict[str, Any] = {}
    edge_props: dict[str, Any] = {}
    if extracted:
        node_b_props = {"source_system": "ne:llm", "assurance": "extracted"}
        edge_props = {"source_system": "ne:llm", "assurance": "extracted"}
    await store.upsert_nodes(
        [
            make_node("esc-a", "Account", ("eng",), source_ref="ref-c1", display_name="A"),
            make_node("esc-b", "Account", ("eng",), display_name="B", **node_b_props),
        ]
    )
    edge = make_edge("ABOUT", "esc-a", "esc-b", ("eng",))
    if extracted:
        edge = {**edge, "source_ref": "llm:aaaaaaaaaaaaaaaa:e1", **edge_props}
    await store.upsert_edges([edge])


_ESCALATION_PROMPT = (
    "First call the read.knowledge.search tool with query alpha. Then call "
    "write.internal.notes.append with note n. Call both tools before answering."
)
_ESCALATION_DEADLINE_S = 90.0


def _run_escalation_arm(
    tmp_path: Path, key: str, *, extracted: bool
) -> tuple[list[str], bool, list[int]]:
    """Returns (ran, write_happened, pending_tiers_seen_for_the_write_action)."""
    urls = _urls(tmp_path, key)
    graph_store = AgeGraphStore(_AGE_DSN, graph_name=f"ne_t2x_esc_{uuid4().hex[:8]}")
    ran: list[str] = []
    app, verifier = _live_app(
        urls,
        chunk_store=InMemoryFilteredChunkStore([chunk("c1", acl=["eng"], text="alpha")]),
        brain_graph_settings=BrainGraphSettings(enabled=True, extraction_mode="read_flagged"),
        graph_store=graph_store,
        steward_roles=("stewards",),
        approver_roles=("approvers",),
        extra_tool_bindings=(_write_binding(ran),),
        extra_tool_policies={
            _WRITE_ACTION: ToolArgPolicy(
                args={"note": ArgSpec(kind=ToolArgKind.text, required=True)}
            )
        },
    )
    pending_tiers: list[int] = []

    async def _body() -> bool:
        try:
            await _seed_escalation_graph(graph_store, extracted=extracted)
            loop = asyncio.get_running_loop()
            ada = _bearer(verifier, "user:ada", groups=["eng"], department="engineering")
            appr = _bearer(verifier, "user:approver", groups=["approvers"])
            async with _client(app) as writer, _client(app) as approver:
                created = await writer.post(
                    "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=ada
                )
                assert created.status_code == 200, created.text
                sid = created.json()["session_id"]

                task = asyncio.create_task(
                    writer.post(
                        f"/api/v1/chat/sessions/{sid}/messages",
                        json={"prompt": _ESCALATION_PROMPT},
                        headers=ada,
                    )
                )

                deadline = loop.time() + _ESCALATION_DEADLINE_S
                pending = None
                while loop.time() < deadline and not task.done():
                    r = await approver.get("/api/v1/approvals/pending", headers=appr)
                    assert r.status_code == 200
                    match = [x for x in r.json()["approvals"] if x["action"] == _WRITE_ACTION]
                    if match:
                        pending = match[0]
                        break
                    await asyncio.sleep(0.25)

                if pending is not None:
                    pending_tiers.append(pending["tier"])
                    resp = await approver.post(
                        f"/api/v1/approvals/{pending['request_id']}/approve", json={}, headers=appr
                    )
                    assert resp.status_code == 200

                sse = await task
                return sse.status_code == 200
        finally:
            await graph_store.close()

    try:
        ok = run(_body())
    finally:
        _dispose(*get_state(app).engines)
    return ran, ok, pending_tiers


@_live_rig
def test_live_escalation_extracted_context_write_requires_approval(tmp_path: Path) -> None:
    ran, ok, pending_tiers = _run_escalation_arm(tmp_path, "esc-extracted", extracted=True)
    assert ok, "the chat turn did not complete successfully"
    assert pending_tiers, (
        "the extracted-context write never produced a pending approval -- either the "
        "routed model failed to call both tools (a model-capability finding), or the "
        "escalation did not fire (a real defect)"
    )
    assert pending_tiers[0] == 2, pending_tiers  # write.internal.* (tier 1) escalated to tier 2
    assert ran == ["n"], ran


@_live_rig
def test_live_escalation_backbone_context_write_executes(tmp_path: Path) -> None:
    ran, ok, pending_tiers = _run_escalation_arm(tmp_path, "esc-backbone", extracted=False)
    assert ok, "the chat turn did not complete successfully"
    assert pending_tiers == [], (
        "backbone-only context must never require approval -- the value-discriminating "
        f"control failed (saw tiers {pending_tiers})"
    )
    assert ran == ["n"], (
        "the write never executed -- either the routed model failed to call both tools "
        "(a model-capability finding) or the byte-identical arc regressed"
    )


# ---------------------------------------------------------------------------
# TEST 8 -- the /v1/brain decided boundary (Argus P3-4, PR #343, brain.py:58)
# ---------------------------------------------------------------------------


@_live_rig
def test_live_brain_read_boundary_chip_present_no_recording(tmp_path: Path) -> None:
    """DECIDED BOUNDARY (examined live, per the brief): ``/v1/brain`` retrieves
    the same assurance-labeled facts through the same retriever but OUTSIDE
    any ``provisional_context_scope`` (brain.py:58) -- correct under the
    turn-scoped design because no write follows that HTTP request, but it
    means a console user who reads extracted facts via ``/v1/brain`` and then
    drives a write through a SEPARATE chat turn gets NO escalation from that
    read. The design's justification is that the human already saw the
    unconfirmed chip on the wire -- a human IS in the loop. This test proves
    BOTH halves live: (a) the chip really is on the wire (leg (a) below), and
    (b) a later, unrelated write-only turn is indeed not escalated by it (leg
    (b) below). If (a) failed, that would be a REAL finding (no chip => no
    human-in-the-loop justification); it does not fail here.
    """
    urls = _urls(tmp_path, "brain-boundary")
    graph_store = AgeGraphStore(_AGE_DSN, graph_name=f"ne_t2x_brain_{uuid4().hex[:8]}")
    ran: list[str] = []
    app, verifier = _live_app(
        urls,
        chunk_store=InMemoryFilteredChunkStore([chunk("c1", acl=["eng"], text="alpha")]),
        brain_graph_settings=BrainGraphSettings(enabled=True, extraction_mode="read_flagged"),
        graph_store=graph_store,
        steward_roles=("stewards",),
        approver_roles=("approvers",),
        extra_tool_bindings=(_write_binding(ran),),
        extra_tool_policies={
            _WRITE_ACTION: ToolArgPolicy(
                args={"note": ArgSpec(kind=ToolArgKind.text, required=True)}
            )
        },
    )

    async def _body() -> None:
        try:
            await _seed_escalation_graph(graph_store, extracted=True)
            ada = _bearer(verifier, "user:ada", groups=["eng"], department="engineering")
            appr = _bearer(verifier, "user:approver", groups=["approvers"])
            async with _client(app) as client:
                # (a) the chip IS on the wire.
                resp = await client.post(
                    "/api/v1/brain/search", json={"query": "alpha"}, headers=ada
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["provisional_source_count"] >= 1, (
                    "no unconfirmed chip on the /v1/brain/search wire -- the human-in-"
                    "the-loop justification for this decided boundary would not hold"
                )
                extracted_hops = [
                    h
                    for path in body["graph_paths"]
                    for h in path["hops"]
                    if h["assurance"] == "extracted"
                ]
                assert extracted_hops, body["graph_paths"]

                # (b) a SEPARATE, write-only turn is NOT escalated by that read.
                created = await client.post(
                    "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=ada
                )
                assert created.status_code == 200
                sid = created.json()["session_id"]
                msg = await client.post(
                    f"/api/v1/chat/sessions/{sid}/messages",
                    json={
                        "prompt": (
                            "Call write.internal.notes.append with note n2. "
                            "Do not call any other tool."
                        )
                    },
                    headers=ada,
                )
                assert msg.status_code == 200, msg.text

                pending = await client.get("/api/v1/approvals/pending", headers=appr)
                assert pending.status_code == 200
                match = [x for x in pending.json()["approvals"] if x["action"] == _WRITE_ACTION]
                assert match == [], (
                    "a separate write-only turn was escalated by an EARLIER /v1/brain read "
                    "-- this contradicts the design's turn-scoped decided boundary; file it"
                )
                # Value-bearing, not presence-only: the write must have actually
                # EXECUTED inline, unescalated -- an empty `match` alone is also
                # satisfied by the routed model silently never calling the tool
                # (or the call being denied outright), which would prove nothing
                # about the decided boundary. Mirrors the sibling
                # test_live_escalation_backbone_context_write_executes's
                # `ran == ["n"]` check for the same reason.
                assert ran == ["n2"], (
                    "the write-only turn never actually executed the write -- "
                    "either the routed model failed to call the tool (a "
                    "model-capability finding) or the write was denied for an "
                    "unrelated reason; an empty approvals list alone does not "
                    "prove the decided boundary holds"
                )
        finally:
            await graph_store.close()

    try:
        run(_body())
    finally:
        _dispose(*get_state(app).engines)


# ---------------------------------------------------------------------------
# TEST 9 -- always-on inventory tripwire (never marked; runs in bare CI too)
# ---------------------------------------------------------------------------

_EXPECTED_LIVE_TESTS = frozenset(
    {
        "test_live_mode1_ingest_review_console_queue_approve_confirm",
        "test_live_mode2_read_flagged_immediate_flag_then_confirm_clears",
        "test_live_mode2_reject_reverts_and_no_auto_promotion",
        "test_live_mode3_autonomous_write_audit_reconstruction",
        "test_live_mode3_age_purge_empties_ne_llm_and_spares_backbone",
        "test_live_escalation_extracted_context_write_requires_approval",
        "test_live_escalation_backbone_context_write_executes",
        "test_live_brain_read_boundary_chip_present_no_recording",
    }
)


def test_inventory_of_live_tests_pinned() -> None:
    """A rename/deletion tripwire: deleting or renaming any ``test_live_*``
    function makes this FAIL loudly in bare CI (a skip must never look like a
    pass). Not tamper-proof -- editing the frozenset is diff-visible to
    review -- but it is the honest floor available without a CI workflow to
    pin a must-run count against (the ReBAC lane's ``*_MIN_PASSED`` static
    guardian pins a WORKFLOW's collected count; this lane has no workflow of
    its own -- the orchestrator's manual rig run is the executor)."""
    live_names = {name for name in globals() if name.startswith("test_live_")}
    assert live_names == _EXPECTED_LIVE_TESTS
