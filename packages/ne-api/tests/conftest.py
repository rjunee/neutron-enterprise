# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Test harness for ne-api — the governed spine is REAL; only edges are faked.

The REAL ``OidcAuthMiddleware`` (over a fake ``TokenVerifier`` + the REAL
``JitProvisioner``), the REAL ``DurableApprovalGate`` / ``GovernedToolExecutor`` /
``PrincipalAclFilter`` / ``PrincipalExpander`` / ``PostgresAuditSink`` (over
SQLite) all run for real. Faked ONLY: the model gateway, the embedder/sparse
edges, the rerank edge (an order-preserving :class:`PassthroughReranker` so the
REAL ``RetrievalPipeline`` composition runs with only the network edge faked), the
Qdrant store, and the OPA HTTP wire (a scripted ``httpx.MockTransport`` BELOW the
REAL ``GovernedPdp`` — the ne-runtime ``make_pdp`` precedent). No pytest-asyncio
(repo policy): async is driven via :func:`run`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from starlette.routing import Mount

from ne.api import AppSeams, create_app
from ne.api.deps import ApiState
from ne.api.inference_lifecycle import InferenceLifecycleConfig
from ne.audit.config import AuditSettings
from ne.brain.current_source_authority import (
    SqlAlchemyCurrentSourceAuthorityWriter,
    ne_source_authority_heads,
    persist_current_acl_snapshot,
)
from ne.brain.trimming import ACL_ANY_KEY
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.identity import TokenClaims
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.plugins import PluginRegistry
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    SourceAdmissionFence,
    SourceAuthorityCandidate,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
    SourceReconciliationScope,
)
from ne.contracts.types import Chunk, Lane, ScoredChunk, SparseVector
from ne.flywheel.config import FlywheelSettings
from ne.flywheel.lanes import FixLane, ProposalPriority
from ne.flywheel.proposals import FixProposal, ProposalKind
from ne.gateway.config import GatewaySettings, ModelDef
from ne.gateway.errors import UnknownTaskError
from ne.govern.config import ToolArgPolicy
from ne.govern.opa import OpaClient
from ne.govern.pdp import GovernedPdp
from ne.identity.config import IdentitySettings, OidcConfig
from ne.identity.errors import TokenVerificationError
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import ApprovalSettings, RuntimeSettings
from ne.runtime.tools import ToolBinding

ISSUER = "https://idp.ne.test/realms/ne-test"
AUDIENCE = "ne-api"
_DECISION_PATH = "/v1/data/ne/govern/decision"


# --- config_hash reset (autouse) ------------------------------------------


@pytest.fixture(autouse=True)
def _reset_config_hash() -> Iterator[None]:
    reset_config_hash_for_testing()
    try:
        yield
    finally:
        reset_config_hash_for_testing()


def run(coro: Any) -> Any:
    """Drive a coroutine to completion (no pytest-asyncio in this repo)."""
    return asyncio.run(coro)


# --- fake token verifier ---------------------------------------------------


class FakeTokenVerifier:
    """Maps registered opaque tokens → verified ``TokenClaims``; else raises."""

    def __init__(self, _oidc: OidcConfig | None = None) -> None:
        self._tokens: dict[str, TokenClaims] = {}

    def mint(
        self,
        subject: str,
        *,
        groups: Sequence[str] = (),
        department: str | None = None,
        email: str | None = None,
        sid: str | None = None,
    ) -> str:
        token = uuid.uuid4().hex
        claims: dict[str, Any] = {"sub": subject, "iss": ISSUER, "groups": list(groups)}
        if department is not None:
            claims["department"] = department
        if email is not None:
            claims["email"] = email
        if sid is not None:
            claims["sid"] = sid
        self._tokens[token] = TokenClaims(
            subject=subject,
            issuer=ISSUER,
            claims=claims,
            verified_expires_at=(datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0),
        )
        return token

    async def verify(self, token: str) -> TokenClaims:
        claims = self._tokens.get(token)
        if claims is None:
            raise TokenVerificationError("unknown token")
        return claims


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


def _rego_faithful_doc(action: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    if action == "subagent.route":
        # Mirrors packages/ne-govern/policies/ne/govern/subagent.rego (NE-169,
        # bundle 1.1.0): private/unpinned resolved lanes are tier-0 allow;
        # frontier is a total deny; anything else is unclassified.
        route_tier = (context or {}).get("route_tier")
        if route_tier in ("private", "unpinned"):
            return {
                "effect": "allow",
                "tier": 0,
                "reason": "tier 0: read/internal draft — autonomous, logged",
                "obligations": {},
                "policy_version": "1.1.0",
            }
        if route_tier == "frontier":
            return {
                "effect": "deny",
                "tier": None,
                "reason": (
                    "subagent.route: frontier-lane routing requires an explicit "
                    "grant — default deny"
                ),
                "obligations": {},
                "policy_version": "1.1.0",
            }
        return {
            "effect": "deny",
            "tier": None,
            "reason": "unclassified action",
            "obligations": {},
            "policy_version": "1.1.0",
        }

    tier = _classify(action)
    effect = "deny" if tier is None else ("allow" if tier <= 1 else "require-approval")
    return {
        "effect": effect,
        "tier": tier,
        "reason": f"rego-faithful: {effect} (tier {tier})",
        "obligations": {},
        "policy_version": "1.0.0",
    }


def _rego_faithful_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == _DECISION_PATH
        payload = json.loads(request.content)
        action = payload["input"]["action"]
        context = payload["input"].get("context") or {}
        return httpx.Response(200, json={"result": _rego_faithful_doc(action, context)})

    return httpx.MockTransport(handler)


class RegoFaithfulPdpFactory:
    """A ``policy_decision_point`` plugin whose ``from_settings`` returns the REAL
    ``GovernedPdp`` over a scripted OPA transport (OPA is a network wire edge)."""

    @staticmethod
    def from_settings(_settings: Any, *, sink: Any) -> GovernedPdp:
        client = httpx.AsyncClient(base_url="http://opa.test", transport=_rego_faithful_transport())
        return GovernedPdp(OpaClient("http://opa.test", client=client), sink)


# --- scripted model gateway ------------------------------------------------


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


class TaskRoutingGateway:
    """The task-routing-FAITHFUL gateway double (NE-HGATE-3, design §6.3).

    Every prior test gateway (``ScriptedGateway.complete`` and friends) IGNORED
    ``request.task`` — which is WHY the dead production ``grounding-assist`` assist
    was never caught in CI (it was always validated against a routing-blind stub).
    This double resolves ``request.task`` against a provided tasks map and raises
    the REAL :class:`~ne.gateway.errors.UnknownTaskError` for an unrouted task —
    byte-mirroring ``LiteLlmGateway._candidates`` (packages/ne-gateway/src/ne/
    gateway/gateway.py:241-244) — then delegates to the wrapped scripted double.
    It is the reusable faithful primitive the two routing regression tests compose
    over (belt = the platform boot check; suspenders = this runtime double).
    """

    def __init__(self, inner: Any, tasks: Mapping[str, Any]) -> None:
        self._inner = inner
        self._tasks = dict(tasks)

    async def complete(self, request: CompletionRequest) -> Completion:
        route = self._tasks.get(request.task)
        if route is None:
            raise UnknownTaskError(request.task, list(self._tasks))
        return await self._inner.complete(request)

    def stream(self, request: CompletionRequest) -> Any:
        route = self._tasks.get(request.task)
        if route is None:
            raise UnknownTaskError(request.task, list(self._tasks))
        raise NotImplementedError("TaskRoutingGateway does not stream")

    @property
    def requests(self) -> list[CompletionRequest]:
        return self._inner.requests


class SleepyGateway:
    """A gateway whose ``complete`` sleeps then returns text (keep-alive/timeout)."""

    def __init__(self, delay_s: float, text: str = "answer") -> None:
        self._delay_s = delay_s
        self._text = text
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        await asyncio.sleep(self._delay_s)
        return text_completion(self._text)

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError


class RaisingGateway:
    """A gateway whose ``complete`` raises (post-stream error frame)."""

    async def complete(self, request: CompletionRequest) -> Completion:
        raise RuntimeError("gateway exploded mid-turn")

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError


def tool_call_completion(name: str, arguments: dict[str, Any]) -> Completion:
    line = json.dumps({"tool_call": {"name": name, "arguments": arguments}})
    return Completion(text=line, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


def text_completion(text: str) -> Completion:
    return Completion(text=text, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


# --- brain edges: static embedder/sparse + in-memory filtered chunk store --


class StaticEmbedder:
    """A trivial dense embedder (the fake store ignores the vector)."""

    @property
    def model_id(self) -> str:
        return "static"

    @property
    def dimension(self) -> int:
        return 4

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]


class StaticSparseEncoder:
    """A trivial sparse encoder (the fake store ignores the vector)."""

    @property
    def model_id(self) -> str:
        return "static-sparse"

    async def encode(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [SparseVector(indices=(), values=()) for _ in texts]

    async def encode_queries(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [SparseVector(indices=(), values=()) for _ in texts]


class PassthroughReranker:
    """An order-preserving ``Reranker`` fake (score 1.0, no reordering).

    Keeps every existing test's result bytes IDENTICAL to the pre-pipeline output
    (the RetrievalPipeline wrap is transparent) while recording that the rerank
    stage actually ran (``calls``). Only the rerank NETWORK edge is faked — the REAL
    ``RetrievalPipeline`` composition runs above the REAL acl-trimmed retriever.
    """

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model_id(self) -> str:
        return "fake-reranker"

    async def rerank(
        self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
    ) -> Sequence[ScoredChunk]:
        self.calls += 1
        return tuple(ScoredChunk(chunk=c, score=1.0) for c in chunks[:top_k])


def _acl_of(chunk: Chunk) -> set[str]:
    return set(chunk.acl)


def _match_any(chunk: Chunk, filters: Mapping[str, Any] | None) -> bool:
    """Qdrant filter fidelity for the merged NE-087 grammar (mirrors
    ``ne.brain.trimming`` + ``test_shared_docs_seam``):

    * ``$any`` → list of sub-mappings; visible iff ANY sub-mapping matches (the
      source-ACL leg OR the ReBAC shared-document leg);
    * ``acl`` → visible iff ``chunk.acl ∩ wanted != ∅`` (MatchAny);
    * ``document_id`` → visible iff ``chunk.document_id ∈ wanted``;
    * any other key AND-combines; an unknown key fails closed (no match).
    """
    if not filters:
        return True
    if ACL_ANY_KEY in filters:
        return any(_match_any(chunk, sub) for sub in filters[ACL_ANY_KEY])
    for key, wanted in filters.items():
        if key == "acl":
            if not (_acl_of(chunk) & set(wanted)):
                return False
        elif key == "document_id":
            if chunk.document_id not in set(wanted):
                return False
        else:
            return False
    return True


def _plain(value: Any) -> Any:
    """Recursively convert a frozen/JSON-shaped value into plain dict/list."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return value


class InMemoryFilteredChunkStore:
    """A ChunkStore + FilteredChunkFetch honoring MatchAny on the 'acl' key."""

    def __init__(self, chunks: Sequence[Chunk] = ()) -> None:
        self._chunks: dict[str, Chunk] = {c.chunk_id: c for c in chunks}
        self.seen_filters: list[Any] = []

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        for c in chunks:
            # Mirror the real Qdrant store's JSON round-trip: a persisted chunk's
            # metadata comes back as PLAIN dicts/lists, never pydantic FrozenDict.
            # Without this, a governed-written chunk (whose metadata nests the
            # provenance stamp) breaks the acl-stripped wire view's serializer —
            # an in-memory-seam artifact the real store never exhibits.
            self._chunks[c.chunk_id] = c.model_copy(update={"metadata": _plain(c.metadata)})

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
        self.seen_filters.append(dict(filters) if filters else None)
        visible = [c for c in self._chunks.values() if _match_any(c, filters)]
        return [ScoredChunk(chunk=c, score=1.0) for c in visible[:top_k]]

    async def get_filtered(
        self, chunk_ids: Sequence[str], *, filters: Mapping[str, Any] | None = None
    ) -> Sequence[Chunk]:
        self.seen_filters.append(dict(filters) if filters else None)
        out: list[Chunk] = []
        for cid in chunk_ids:
            c = self._chunks.get(cid)
            if c is not None and _match_any(c, filters):
                out.append(c)
        return out

    async def scan_filtered(self, *, filters: Mapping[str, Any], limit: int) -> Sequence[Chunk]:
        """Filter-only scan (the ``QdrantChunkStore.scan_filtered`` mirror).

        A NARROW double: it supports only the dotted ``metadata.<key>`` /
        ``document_id`` / ``chunk_id`` paths the internal CRM lookup
        (NE-S5-INTERNAL-CRM) actually scans by, with scalar equality / list
        any-of and whole-value comparison, and fails closed on anything else.
        It is NOT the frozen grammar — ``$any``, arbitrary payload paths and
        array-contains matching all differ from the shipped stores. The REAL
        implementations are pinned by
        ``packages/ne-brain/tests/test_scan_filtered_real_stores.py``; never
        read a grammar guarantee out of this double.
        """
        self.seen_filters.append(dict(filters) if filters else None)
        if not filters:
            raise ValueError("scan_filtered requires a non-empty filter")

        def _matches(c: Chunk) -> bool:
            for key, wanted in filters.items():
                if key.startswith("metadata."):
                    actual = c.metadata.get(key.removeprefix("metadata."))
                elif key == "document_id":
                    actual = c.document_id
                elif key == "chunk_id":
                    actual = c.chunk_id
                else:
                    return False  # unknown key fails closed, like _match_any
                allowed = wanted if isinstance(wanted, (list, tuple)) else (wanted,)
                if actual not in allowed:
                    return False
            return True

        return [c for c in self._chunks.values() if _matches(c)][:limit]

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        for cid in chunk_ids:
            self._chunks.pop(cid, None)


def chunk(
    chunk_id: str, *, acl: Sequence[str], text: str = "body", document_id: str | None = None
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id=document_id or f"doc-{chunk_id}",
        text=text,
        metadata={
            "source_system": "wiki",
            "source_ref": f"ref-{chunk_id}",
            "source_status": "no_source",
        },
        acl=tuple(acl),
    )


# --- SSE parser (a vendored copy of the sse.ts grammar) --------------------


@dataclass
class SseFrame:
    event: str | None
    data: dict[str, Any] | None


@dataclass
class ParsedSse:
    frames: list[SseFrame]
    comment_lines: int

    def events(self) -> list[str | None]:
        return [f.event for f in self.frames]

    def of_type(self, event: str) -> list[SseFrame]:
        return [f for f in self.frames if f.event == event]


def parse_sse(body: str) -> ParsedSse:
    """Parse an SSE stream per sse.ts: CRLF-normalize, blank-line frames, ':'
    comments skipped, multi-line ``data:`` joined with '\\n', ``event:`` captured."""
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    frames: list[SseFrame] = []
    comment_lines = 0
    for raw_block in normalized.split("\n\n"):
        block = raw_block.strip("\n")
        if not block:
            continue
        event: str | None = None
        data_parts: list[str] = []
        is_frame = False
        for line in block.split("\n"):
            if line.startswith(":"):
                comment_lines += 1
                continue
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
                is_frame = True
            elif line.startswith("data:"):
                data_parts.append(line[len("data:") :].lstrip())
                is_frame = True
        if not is_frame:
            continue
        data_str = "\n".join(data_parts)
        data = json.loads(data_str) if data_str else None
        frames.append(SseFrame(event=event, data=data))
    return ParsedSse(frames=frames, comment_lines=comment_lines)


# --- the app harness -------------------------------------------------------


@dataclass
class Harness:
    app: Any
    verifier: FakeTokenVerifier
    chunk_store: InMemoryFilteredChunkStore
    gateway: Any
    audit_url: str
    runtime_url: str
    eval_url: str
    signal_url: str
    #: The injected order-preserving rerank edge — tests assert ``reranker.calls``
    #: to prove the composed pipeline reranked. Defaulted so existing keyword
    #: Harness constructions stay non-breaking.
    reranker: Any = None

    def bearer(
        self,
        subject: str = "user:ada",
        *,
        groups: Sequence[str] = (),
        department: str | None = None,
        email: str | None = None,
        sid: str | None = None,
    ) -> dict[str, str]:
        token = self.verifier.mint(
            subject, groups=groups, department=department, email=email, sid=sid
        )
        return {"Authorization": f"Bearer {token}"}

    def client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app)
        return httpx.AsyncClient(transport=transport, base_url="http://ne.test")


def get_state(app: Any) -> ApiState:
    for route in app.routes:
        if isinstance(route, Mount) and route.path == "/api":
            middleware: Any = route.app  # the OidcAuthMiddleware ASGI wrapper
            inner = middleware._app  # its inner FastAPI app carries state.ne_api
            state = inner.state.ne_api
            assert isinstance(state, ApiState)
            return state
    raise AssertionError("no /api mount found")


def admit_current_source(
    reader: Any,
    *,
    key: str,
    labels: Sequence[str],
    content: bytes,
) -> tuple[CurrentSourceAuthorityReceipt, SourceAuthorityStamp]:
    """Admit one exact-current test artifact and its canonical ACL sidecar.

    This is intentionally a real Writer/Reader fixture over the composed runtime
    database. It does not bless legacy ACL fields or fabricate a Reader result.
    Reusing ``key`` advances the same logical source head to a new revision.
    """

    engine = reader._engine
    construction_id = reader._construction_id
    writer = SqlAlchemyCurrentSourceAuthorityWriter(
        engine, construction_id, freshness_sla_seconds=Decimal("3600")
    )
    canonical_labels = tuple(sorted(set(labels)))
    acl_payload = json.dumps(
        {"acl": list(canonical_labels)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    acl_hash = hashlib.sha256(acl_payload.encode()).hexdigest()
    identity = SourceIdentity(
        deployment_id="test-deployment",
        source_kind="m365-sharepoint",
        connector_instance_id="ne-api-focused-fixtures",
        point=M365SourcePoint(
            graph_tenant_id="test-tenant",
            site_id="test-site",
            drive_id="test-drive",
            item_id=key,
        ),
        generation=1,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint",
        descriptor_schema_version=1,
        descriptor_hash=hashlib.sha256(b"ne-api-focused-fixture-contract").hexdigest(),
    )
    snapshot_hash = hashlib.sha256(content).hexdigest()
    artifact = SourceContentArtifactIdentity(
        source_identity=identity,
        source_snapshot_hash=snapshot_hash,
        content_snapshot_hash=snapshot_hash,
        acl_snapshot_hash=acl_hash,
        source_contract=contract,
        artifact_schema_version=1,
    )
    with engine.connect() as conn:
        expected_head = conn.execute(
            sa.select(ne_source_authority_heads.c.head_revision).where(
                ne_source_authority_heads.c.identity_hash == identity.identity_hash
            )
        ).scalar_one_or_none()
    revision = f"fixture-rev-{uuid.uuid4().hex}"

    async def _admit() -> CurrentSourceAuthorityReceipt:
        claim = await writer.claim(
            SourceReconciliationScope(path=("ne-api-focused-fixture", key)),
            expected_head_revision=expected_head,
            lease_seconds=60,
        )
        fence = SourceAdmissionFence(
            deployment_id=identity.deployment_id,
            connector_instance_id=identity.connector_instance_id,
            source_generation=identity.generation,
            external_source_revision=revision,
            contract_epoch=1,
            reconciliation_scope=claim.scope,
            reconciliation_epoch=claim.reconciliation_epoch,
            fencing_token=claim.fencing_token,
        )
        candidate = SourceAuthorityCandidate(
            source_identity=identity,
            artifact_identity=artifact,
            source_revision=revision,
            source_contract=contract,
            contract_epoch=1,
            acl_snapshot_hash=acl_hash,
            state=SourceAuthorityState.PRESENT,
            authority_as_of=datetime.now(UTC) - timedelta(seconds=1),
        )
        persist_current_acl_snapshot(
            engine,
            identity_hash=identity.identity_hash,
            artifact_id=artifact.artifact_id,
            acl_snapshot_hash=acl_hash,
            labels=canonical_labels,
        )
        receipt = await writer.finalize(claim, fence, candidate)
        await writer.release(claim)
        return receipt

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        receipt = run(_admit())
    else:
        # Several HTTP fixtures seed exact authority from inside their async
        # scenario. Drive the Writer on a dedicated loop rather than nesting
        # asyncio.run() into the active test loop.
        with ThreadPoolExecutor(max_workers=1) as pool:
            receipt = pool.submit(asyncio.run, _admit()).result()
    stamp = SourceAuthorityStamp(
        source_identity=receipt.source_identity,
        artifact_identity=artifact,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=receipt.source_contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )
    return receipt, stamp


def bind_current_document_source(
    app: Any,
    *,
    document_id: str,
    labels: Sequence[str],
    content: bytes,
) -> SourceAuthorityStamp:
    """Bind one real exact-current authority receipt to the latest document version."""

    state = get_state(app)
    receipt, stamp = admit_current_source(
        state.source_authority_reader,
        key=f"document:{document_id}",
        labels=labels,
        content=content,
    )
    assert state.document_lineage.bind_source_authority(document_id, receipt=receipt)
    return stamp


def seed_proposal(
    app: Any,
    *,
    lane: str = "content",
    kind: ProposalKind = ProposalKind.VERIFIED_ANSWER_CANDIDATE,
    summary: str = "SMUGGLE-SUMMARY fix the retriever",
    payload: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> str:
    """Append a real ``FixProposal`` via the proposal store's governed append path.

    The sentinel strings ``SMUGGLE-SUMMARY``/``SMUGGLE-PAYLOAD`` let tests
    recursively assert raw text never crosses the wire. Returns the proposal_id.
    """
    state = get_state(app)
    assert state.proposal_store is not None
    proposal_id = uuid.uuid4().hex
    template_toml = 'template_id = "fixture"\nversion = 1\n'
    fp = FixProposal(
        proposal_id=proposal_id,
        kind=kind,
        lane=FixLane(lane),
        cluster_id="cl-1",
        evidence_event_ids=("ev-1",),
        evidence_signal_ids=(1,),
        summary=summary,
        confidence=0.5,
        priority=ProposalPriority.MEDIUM,
        config_hash=state.config_hash,
        created_at=created_at or datetime.now(UTC),
        payload=payload
        or {
            "template_id": "fixture",
            "version": 1,
            "toml_sha256": hashlib.sha256(template_toml.encode()).hexdigest(),
            "template_toml": template_toml,
        },
    )
    with state.proposal_store.engine.begin() as conn:
        state.proposal_store.append(conn, fp)
    return proposal_id


AppBuilder = Callable[..., Harness]


@pytest.fixture
def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AppBuilder]:
    built: list[Harness] = []

    def _make(
        *,
        completions: Sequence[Completion] | None = None,
        gateway: Any | None = None,
        chunks: Sequence[Chunk] = (),
        chunk_store: Any | None = None,
        sse_keepalive_s: float = 15.0,
        turn_timeout_s: float = 120.0,
        chat_agents: tuple[str, ...] = ("console-chat",),
        agent_manifest_path: str = "",
        identity_department_in_claims: bool = True,
        approver_roles: tuple[str, ...] = (),
        ops_roles: tuple[str, ...] = (),
        steward_roles: tuple[str, ...] = (),
        audit_export_max_rows: int = 100_000,
        audit_export_batch_rows: int = 500,
        approvals: ApprovalSettings | None = None,
        eval_store_configured: bool = True,
        kpi_configured: bool = True,
        extra_tool_bindings: tuple[ToolBinding, ...] = (),
        extra_tool_policies: Mapping[str, ToolArgPolicy] | None = None,
        serve_console_dist: str = "",
        turn_enqueuer: Any | None = None,
        turn_reader: Any | None = None,
        scim_bearer_token: str = "",
        console_client_id: str = "ne-console",
        cdc_webhook_secret: str = "",
        cdc_tables: str = "",
        cdc_quarantine_path: str | None = None,
        pdp_factory: Any | None = None,
        rebac_settings: Any | None = None,
        rebac_client: Any | None = None,
        ontology_settings: Any | None = None,
        triple_store: Any | None = None,
        retrieval_top_k: int = 5,
        brain_graph_settings: Any | None = None,
        graph_store: Any | None = None,
        # NE-SERVING-STATUS: override the composed lanes and/or inject a prober
        # with a fake transport/clock (only the network edge faked).
        gateway_models: Sequence[ModelDef] | None = None,
        # NE-MODEL-ROUTING: the boot task->model table the route register
        # overlays. Defaulted empty so every existing harness construction is
        # byte-identical.
        gateway_tasks: Mapping[str, Any] | None = None,
        serving_probe: Any | None = None,
        inference_lifecycle: InferenceLifecycleConfig | None = None,
        inference_operator: Any | None = None,
        protected_transport: bool = False,
        # NE-S5-INTERNAL-CRM: select real reference write actions and (optionally)
        # inject their connector seam — `None` clients + a selection containing
        # only write.internal.crm.note exercises the production INTERNAL wiring.
        write_action_tools: tuple[str, ...] = (),
        email_draft_allowed_mailboxes: tuple[str, ...] = (),
        email_send_allowed_recipients: tuple[str, ...] = (),
        write_action_clients: Any | None = None,
        govern_settings: Any | None = None,
        # NE-DEMO-LIVE-SEAMS: register the INERT tier-2 demo write-back
        # (`write.external.demo.send`, no I/O of any kind) in the composed
        # executor, exactly as `NE_API_DEMO_WRITEBACK_TOOL=true` does in a demo
        # deployment. Default False keeps every existing harness byte-identical.
        demo_writeback_tool: bool = False,
    ) -> Harness:
        from ne.api.settings import ApiSettings

        registry = PluginRegistry()
        verifier = FakeTokenVerifier()
        registry.register("token_verifier", "fake", lambda oidc: verifier)
        registry.register("policy_decision_point", "opa", pdp_factory or RegoFaithfulPdpFactory)
        if triple_store is not None:
            # NE-135: hand a pre-seeded gateway to the 085a triple_store entry-point
            # slot (resolve_triple_store returns it verbatim — no oxigraph double-open).
            registry.register("triple_store", "oxigraph", lambda **_kw: triple_store)

        principal_claims = (
            ("email", "preferred_username", "name", "department")
            if identity_department_in_claims
            else ("email", "preferred_username", "name")
        )
        identity_settings = IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience=AUDIENCE),
            principal_claims=principal_claims,
        )

        store = chunk_store if chunk_store is not None else InMemoryFilteredChunkStore(chunks)
        gw = gateway if gateway is not None else ScriptedGateway(completions)
        reranker = PassthroughReranker()

        runtime_url = f"sqlite:///{tmp_path}/runtime-{uuid.uuid4().hex}.db"
        audit_url = f"sqlite:///{tmp_path}/audit-{uuid.uuid4().hex}.db"
        perms_url = f"sqlite:///{tmp_path}/perms-{uuid.uuid4().hex}.db"
        eval_url = (
            f"sqlite:///{tmp_path}/eval-{uuid.uuid4().hex}.db" if eval_store_configured else ""
        )
        signal_url = f"sqlite:///{tmp_path}/signals-{uuid.uuid4().hex}.db" if kpi_configured else ""

        for name in (
            "NE_AGENT_PRIVATE_PAYLOAD_KEY_ID",
            "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64",
            "NE_AGENT_TRANSPORT_GENERATION",
        ):
            monkeypatch.delenv(name, raising=False)
        if protected_transport:
            monkeypatch.setenv("NE_AGENT_PRIVATE_PAYLOAD_KEY_ID", "api-test-key")
            monkeypatch.setenv(
                "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64",
                base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="),
            )
            monkeypatch.setenv("NE_AGENT_TRANSPORT_GENERATION", "agent-transport-v1")

        seams = AppSeams(
            identity_settings=identity_settings,
            runtime_settings=RuntimeSettings(
                session_database_url=runtime_url,
                approvals=approvals or ApprovalSettings(),
            ),
            audit_settings=AuditSettings(database_url=audit_url),
            permissions_settings=PermissionsSettings(database_url=perms_url),
            flywheel_settings=FlywheelSettings(signal_database_url=signal_url),
            gateway_settings=GatewaySettings(
                models=(
                    list(gateway_models)
                    if gateway_models is not None
                    else [
                        ModelDef(
                            name="stub",
                            litellm_model="openai/stub",
                            provider="stub",
                            lane=Lane.FRONTIER_API,
                            context_window=32768,
                        )
                    ]
                ),
                tasks=dict(gateway_tasks) if gateway_tasks is not None else {},
            ),
            gateway=gw,
            embedder=StaticEmbedder(),
            sparse_encoder=StaticSparseEncoder(),
            chunk_store=store,
            reranker=reranker,
            plugin_registry=registry,
            extra_tool_bindings=extra_tool_bindings,
            extra_tool_policies=extra_tool_policies,
            turn_enqueuer=turn_enqueuer,
            turn_reader=turn_reader,
            rebac_settings=rebac_settings,
            rebac_client=rebac_client,
            ontology_settings=ontology_settings,
            brain_graph_settings=brain_graph_settings,
            graph_store=graph_store,
            serving_probe=serving_probe,
            inference_operator=inference_operator,
            agent_private_payload_configured=protected_transport,
            write_action_clients=write_action_clients,
            govern_settings=govern_settings,
        )
        app = create_app(
            ApiSettings(
                qdrant_url="http://qdrant.test",
                qdrant_collection="t",
                tei_url="http://tei.test",
                chat_agents=chat_agents,
                agent_manifest_path=agent_manifest_path,
                sse_keepalive_s=sse_keepalive_s,
                turn_timeout_s=turn_timeout_s,
                approver_roles=approver_roles,
                ops_roles=ops_roles,
                steward_roles=steward_roles,
                eval_database_url=eval_url,
                serve_console_dist=serve_console_dist,
                scim_bearer_token=SecretStr(scim_bearer_token),
                console_client_id=console_client_id,
                cdc_webhook_secret=SecretStr(cdc_webhook_secret),
                cdc_tables=cdc_tables,
                cdc_quarantine_path=(
                    cdc_quarantine_path
                    if cdc_quarantine_path is not None
                    else (
                        f"{tmp_path}/cdc-quarantine-{uuid.uuid4().hex}.sqlite"
                        if cdc_webhook_secret
                        else ""
                    )
                ),
                retrieval_top_k=retrieval_top_k,
                audit_export_max_rows=audit_export_max_rows,
                audit_export_batch_rows=audit_export_batch_rows,
                inference_lifecycle=inference_lifecycle or InferenceLifecycleConfig(),
                write_action_tools=write_action_tools,  # type: ignore[arg-type]
                email_draft_allowed_mailboxes=email_draft_allowed_mailboxes,
                email_send_allowed_recipients=email_send_allowed_recipients,
                demo_writeback_tool=demo_writeback_tool,
            ),
            seams=seams,
        )
        harness = Harness(
            app=app,
            verifier=verifier,
            chunk_store=store,
            gateway=gw,
            audit_url=audit_url,
            runtime_url=runtime_url,
            eval_url=eval_url,
            signal_url=signal_url,
            reranker=reranker,
        )
        built.append(harness)
        return harness

    yield _make

    for h in built:
        for engine in get_state(h.app).engines:
            engine.dispose()
