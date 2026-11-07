# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-T2X-4 — the ne-api extraction composition proof.

Covers: the boot fail-loud belt for the unrouted ``graph-extract`` task (+ its
documented no-op-when-empty-tasks semantics); the settings-layer PIN tests
proving checks #2/#3 (YOLO-without-consent, extraction-without-graph) are
enforced UNCONDITIONALLY at ``BrainGraphSettings`` construction — NOT
duplicated at this layer; the ``TaskRoutingGateway`` runtime SUSPENDERS pair;
modes 2/3 end-to-end over ``InMemoryGraphStore`` + a scripted gateway + a
sqlite flywheel engine (extract → confirm → reject/revert, autonomous mode +
the ``graph.extraction_mode`` event); THE wire proof (assurance 'extracted'
vs 'source' through the REAL ``retrieval_result_view`` mapping, the pinned
``provisional_source_count`` definition incl. dedup + unknown-label cases);
and the default-off / fail-loud identity tests.

No pytest-asyncio (repo policy): async is driven via the conftest ``run``
helper.
"""

from __future__ import annotations

import base64
import dataclasses
import importlib
import inspect
import json
import pkgutil
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import (
    Annotated,
    Any,
    Literal,
    NewType,
    Optional,
    TypeAliasType,
    TypedDict,
    TypeVar,
    get_args,
    get_origin,
    is_typeddict,
)

import pytest
import sqlalchemy as sa
from fastapi import APIRouter, Body, Depends, Form, Query, Security, WebSocket
from fastapi import params as fastapi_params
from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
from fastapi.security import APIKeyQuery, HTTPAuthorizationCredentials, HTTPBearer
from fastapi.security.base import SecurityBase
from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

import ne.api
import ne.api.wire as api_wire
import ne.contracts
import ne.contracts.types as contracts_types
from ne.api import create_app
from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.settings import ApiSettings
from ne.api.wire import (
    GraphEntityRefView,
    GraphHopView,
    GraphPathView,
    _provisional_source_count,
    provisional_summary,
    retrieval_result_view,
)
from ne.audit.config import AuditSettings
from ne.brain_graph.config import BrainGraphSettings
from ne.brain_graph.extraction import EXTRACTION_TASK
from ne.brain_graph.extraction.apply import apply_extraction, revert_extraction
from ne.brain_graph.extraction.identity import llm_entity_key
from ne.brain_graph.extraction.job import ExtractionChunk
from ne.brain_graph.testing.memory import InMemoryGraphStore
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import (
    Citation,
    GraphEntityRef,
    GraphHop,
    GraphPath,
    Lane,
    Principal,
    RetrievalResult,
)
from ne.flywheel.config import FlywheelSettings
from ne.flywheel.review import ProposalStatus, RejectionCode
from ne.flywheel.signals import SignalProvenance, SignalSurface
from ne.gateway.config import GatewaySettings, ModelDef
from ne.gateway.errors import UnknownTaskError
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings
from ne.schema import SCHEMA_VERSION

from .conftest import (
    ISSUER,
    AppBuilder,
    FakeTokenVerifier,
    InMemoryFilteredChunkStore,
    PassthroughReranker,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
    TaskRoutingGateway,
    admit_current_source,
    chunk,
    get_state,
    run,
)

_DOCS = "docs"


class _StaticExtractionChunkSource:
    """A ``ChunkSource`` over a fixed ``{source_system: [chunk, ...]}`` map."""

    def __init__(self, by_source: dict[str, list[ExtractionChunk]]) -> None:
        self._by_source = by_source

    async def chunks(self, source_system: str) -> Sequence[ExtractionChunk]:
        return self._by_source.get(source_system, [])


def _new_entity_completion(entity_class: str, display: str, *, confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "assertions": [
                {
                    "kind": "new_entity",
                    "entity_class": entity_class,
                    "display": display,
                    "confidence": confidence,
                    "properties": {"name": display},
                }
            ]
        }
    )


def _api_settings(**overrides: object) -> ApiSettings:
    base: dict[str, object] = {
        "qdrant_url": "http://qdrant.test",
        "qdrant_collection": "t",
        "tei_url": "http://tei.test",
    }
    base.update(overrides)
    return ApiSettings(**base)  # type: ignore[arg-type]


def _seams(
    tmp_path: Path,
    *,
    key: str,
    brain_graph_settings: BrainGraphSettings | None = None,
    graph_store: Any | None = None,
    gateway: Any | None = None,
    gateway_tasks: dict[str, Any] | None = None,
    chunks: tuple[Any, ...] = (),
    extraction_chunk_source: Any | None = None,
    flywheel_signal_url: str | None = None,
) -> AppSeams:
    registry = PluginRegistry()
    verifier = FakeTokenVerifier()
    registry.register("token_verifier", "fake", lambda oidc: verifier)
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)

    runtime_url = f"sqlite:///{tmp_path}/runtime-{key}.db"
    audit_url = f"sqlite:///{tmp_path}/audit-{key}.db"
    perms_url = f"sqlite:///{tmp_path}/perms-{key}.db"

    gw = gateway if gateway is not None else ScriptedGateway()
    store = InMemoryFilteredChunkStore(chunks)
    return AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience="ne-api"),
            principal_claims=("email", "department"),
        ),
        runtime_settings=RuntimeSettings(session_database_url=runtime_url),
        audit_settings=AuditSettings(database_url=audit_url),
        permissions_settings=PermissionsSettings(database_url=perms_url),
        gateway_settings=GatewaySettings(
            models=[
                ModelDef(
                    name="private-default",
                    litellm_model="hosted_vllm/x",
                    provider="hosted_vllm",
                    lane=Lane.PRIVATE_VLLM,
                    context_window=32768,
                )
            ],
            tasks=gateway_tasks or {},
        ),
        gateway=gw,
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=store,
        reranker=PassthroughReranker(),
        plugin_registry=registry,
        brain_graph_settings=brain_graph_settings,
        graph_store=graph_store,
        flywheel_settings=(
            FlywheelSettings(signal_database_url=flywheel_signal_url)
            if flywheel_signal_url is not None
            else None
        ),
        extraction_chunk_source=extraction_chunk_source,
    )


@dataclass
class PlatformHarness:
    platform: Platform


MakePlatform = Any


@pytest.fixture
def make_platform(tmp_path: Path) -> Iterator[Any]:
    engines: list[sa.Engine] = []

    def _make(**kwargs: Any) -> PlatformHarness:
        reset_config_hash_for_testing()
        key = kwargs.pop("key", None) or uuid.uuid4().hex
        extraction_source = kwargs.get("extraction_chunk_source")
        seams = _seams(tmp_path, key=key, **kwargs)
        platform = build_platform(_api_settings(), seams)
        if isinstance(extraction_source, _StaticExtractionChunkSource):
            for source_system, source_chunks in extraction_source._by_source.items():
                current_chunks: list[ExtractionChunk] = []
                for source_chunk in source_chunks:
                    _receipt, stamp = admit_current_source(
                        platform.source_authority_reader,
                        key=(
                            f"extraction:{source_system}:{source_chunk.document_ref}:"
                            f"{source_chunk.chunk_ref}"
                        ),
                        labels=source_chunk.acl,
                        content=source_chunk.text.encode(),
                    )
                    current_chunks.append(
                        source_chunk.model_copy(update={"source_authority_stamps": (stamp,)})
                    )
                extraction_source._by_source[source_system] = current_chunks
        engines.extend(platform.engines)
        return PlatformHarness(platform=platform)

    yield _make
    for engine in engines:
        engine.dispose()


def _extraction_settings(**overrides: Any) -> BrainGraphSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "extraction_enabled": True,
        "extraction_sources": (_DOCS,),
    }
    base.update(overrides)
    return BrainGraphSettings(**base)


def _flywheel_url(tmp_path: Path, key: str) -> str:
    return f"sqlite:///{tmp_path}/flywheel-{key}.db"


def _chunk_fixture(chunk_ref: str, document_ref: str, *, acl: tuple[str, ...] = ("g1",)) -> Any:
    return chunk(chunk_ref, acl=list(acl), text="body", document_id=document_ref)


def _extraction_chunk(
    chunk_ref: str, document_ref: str, *, acl: tuple[str, ...] = ("g1",)
) -> ExtractionChunk:
    return ExtractionChunk(
        chunk_ref=chunk_ref, document_ref=document_ref, source_system=_DOCS, text="x", acl=acl
    )


# --- (a) boot fail-loud trio -------------------------------------------------


def test_boot_refuses_unrouted_graph_extract_task(make_platform: Any, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="graph-extract"):
        make_platform(
            brain_graph_settings=_extraction_settings(),
            graph_store=InMemoryGraphStore(),
            gateway_tasks={"other-task": {"model": "private-default"}},
            extraction_chunk_source=_StaticExtractionChunkSource({}),
            flywheel_signal_url=_flywheel_url(tmp_path, "unrouted"),
        )


def test_boot_succeeds_with_graph_extract_routed(make_platform: Any, tmp_path: Path) -> None:
    h = make_platform(
        brain_graph_settings=_extraction_settings(),
        graph_store=InMemoryGraphStore(),
        gateway_tasks={EXTRACTION_TASK: {"model": "private-default"}},
        extraction_chunk_source=_StaticExtractionChunkSource({}),
        flywheel_signal_url=_flywheel_url(tmp_path, "routed"),
    )
    assert h.platform.extraction is not None
    assert h.platform.extraction.job._source_authority is h.platform.source_authority_reader
    assert h.platform.extraction.job._authority_principal_id == "system:graph-extraction"
    assert len(h.platform.extraction.job._authority_access_context_hash) == 64


def test_boot_noop_when_gateway_tasks_empty(make_platform: Any, tmp_path: Path) -> None:
    """The belt is a documented no-op over ``{}`` (the injected-seam default) —
    extraction still composes; only the LOUD refusal is skipped."""
    h = make_platform(
        brain_graph_settings=_extraction_settings(),
        graph_store=InMemoryGraphStore(),
        gateway_tasks={},
        extraction_chunk_source=_StaticExtractionChunkSource({}),
        flywheel_signal_url=_flywheel_url(tmp_path, "empty-tasks"),
    )
    assert h.platform.extraction is not None


# --- (b) settings-layer pins: checks #2/#3 fire at CONSTRUCTION, not here ----


def test_yolo_without_consent_is_a_settings_construction_error() -> None:
    """Pins that ``BrainGraphSettings._validate_extraction`` (config.py) is the
    SOLE enforcement point for the YOLO-consent combo — a platform-layer
    duplicate was deliberately rejected (see the boot-check comment in
    ``platform.py``); this test would still pass even if the platform layer
    never looked at ``extraction_mode`` at all."""
    with pytest.raises(Exception, match="extraction_risk_accepted"):
        BrainGraphSettings(
            enabled=True, extraction_mode="autonomous", extraction_risk_accepted=False
        )


def test_extraction_without_graph_is_a_settings_construction_error() -> None:
    """Same enforcement-point pin for the extraction-without-graph combo."""
    with pytest.raises(Exception, match="enabled=True"):
        BrainGraphSettings(enabled=False, extraction_enabled=True)


# --- (c) TaskRoutingGateway runtime suspenders --------------------------------


def test_unrouted_graph_extract_task_raises_at_runtime(make_platform: Any, tmp_path: Path) -> None:
    """gateway_settings.tasks stays {} (the boot belt no-ops); the INJECTED
    gateway seam is a TaskRoutingGateway missing the graph-extract route —
    isolates the runtime SUSPENDERS from the boot BELT."""
    graph_store = InMemoryGraphStore()
    gateway = TaskRoutingGateway(ScriptedGateway(), tasks={})  # graph-extract absent
    h = make_platform(
        brain_graph_settings=_extraction_settings(),
        graph_store=graph_store,
        gateway=gateway,
        gateway_tasks={},
        extraction_chunk_source=_StaticExtractionChunkSource(
            {_DOCS: [_extraction_chunk("c1", "d1")]}
        ),
        flywheel_signal_url=_flywheel_url(tmp_path, "runtime-negative"),
    )
    assert h.platform.extraction is not None
    with pytest.raises(UnknownTaskError):
        run(h.platform.extraction.run())


def test_routed_graph_extract_task_produces_proposals(make_platform: Any, tmp_path: Path) -> None:
    graph_store = InMemoryGraphStore()
    inner = ScriptedGateway([_c(_new_entity_completion("Account", "Acme"))])
    gateway = TaskRoutingGateway(inner, tasks={EXTRACTION_TASK: object()})
    h = make_platform(
        brain_graph_settings=_extraction_settings(),
        graph_store=graph_store,
        gateway=gateway,
        gateway_tasks={},
        chunks=(_chunk_fixture("c1", "d1"),),
        extraction_chunk_source=_StaticExtractionChunkSource(
            {_DOCS: [_extraction_chunk("c1", "d1")]}
        ),
        flywheel_signal_url=_flywheel_url(tmp_path, "runtime-positive"),
    )
    result = run(h.platform.extraction.run())
    assert result.report.batches_built == 1
    assert result.report.batch_proposal_ids
    # Mode 1 (ingest_review, the default) — proposals only, nothing written.
    assert result.writes == {}
    assert graph_store.node_count() == 0


def _c(text: str) -> Any:
    from ne.contracts.models import Completion, Usage

    return Completion(text=text, model_id="test", usage=Usage(prompt_tokens=0, completion_tokens=0))


# --- (d) modes 2/3 end-to-end -------------------------------------------------


def _prov(actor: str = "steward") -> SignalProvenance:
    return SignalProvenance(
        principal=Principal(id=actor), surface=SignalSurface.CONSOLE, session_id="s1"
    )


def test_mode2_read_flagged_writes_extracted_then_confirm_and_reject(
    make_platform: Any, tmp_path: Path
) -> None:
    graph_store = InMemoryGraphStore()
    # Three extraction.run() passes over 2 chunks each (this run, the idempotent
    # re-run, and the post-approve/reject third run) = 6 completions in chunk
    # order — NOT 2: ScriptedGateway returns filler text_completion("done") once
    # exhausted, which would silently build zero batches on runs 2/3.
    inner = ScriptedGateway(
        [
            _c(_new_entity_completion("Account", "Acme")),
            _c(_new_entity_completion("Account", "Globex")),
        ]
        * 3
    )
    gateway = TaskRoutingGateway(inner, tasks={EXTRACTION_TASK: object()})
    h = make_platform(
        brain_graph_settings=_extraction_settings(extraction_mode="read_flagged"),
        graph_store=graph_store,
        gateway=gateway,
        gateway_tasks={EXTRACTION_TASK: {"model": "private-default"}},
        chunks=(_chunk_fixture("c1", "d1"), _chunk_fixture("c2", "d2")),
        extraction_chunk_source=_StaticExtractionChunkSource(
            {_DOCS: [_extraction_chunk("c1", "d1"), _extraction_chunk("c2", "d2")]}
        ),
        flywheel_signal_url=_flywheel_url(tmp_path, "mode2"),
    )
    extraction = h.platform.extraction
    assert extraction is not None
    result = run(extraction.run())
    assert len(result.report.batch_proposal_ids) == 2
    for pid in result.report.batch_proposal_ids:
        assert result.writes[pid] == {"nodes": 1, "edges": 0, "links_noop": 0}

    acme_key = llm_entity_key("Account", "Acme")
    globex_key = llm_entity_key("Account", "Globex")
    acme_node = graph_store.raw_node(acme_key)
    globex_node = graph_store.raw_node(globex_key)
    assert acme_node is not None and acme_node["assurance"] == "extracted"
    assert globex_node is not None and globex_node["assurance"] == "extracted"

    write_ctx = extraction.write_ctx
    assert write_ctx is not None
    # Proposals stay 'proposed' — the immediate write is NOT a gate.
    for pid in result.report.batch_proposal_ids:
        assert write_ctx.review_service.status_of(pid) is ProposalStatus.PROPOSED

    acme_pid, globex_pid = result.report.batch_proposal_ids

    # Idempotent re-run: PROPOSED-only skip semantics — no duplicate writes,
    # no error, batches re-build identically (content-addressed). A
    # still-PROPOSED proposal is RE-WRITTEN idempotently (id-stable upsert),
    # not skipped — the skip case is separately proven below by the third
    # run's {'skipped_status': 'applied'} assertion (only reachable once a
    # proposal has moved past 'proposed').
    again = run(extraction.run())
    assert again.report.batch_proposal_ids == result.report.batch_proposal_ids
    assert graph_store.node_count() == 2
    for pid in result.report.batch_proposal_ids:
        assert again.writes[pid] == {"nodes": 1, "edges": 0, "links_noop": 0}

    # Confirm: approve then apply_extraction flips the SAME element to confirmed.
    write_ctx.review_service.approve(acme_pid, _prov())
    applied_ref = run(
        apply_extraction(
            write_ctx.engine,
            write_ctx.review_service,
            write_ctx.store,
            write_ctx.acl_resolver,
            acme_pid,
            actor="steward",
            audit_sink=h.platform.sink,
            config_hash=h.platform.config_hash,
            schema_version=SCHEMA_VERSION,
        )
    )
    assert applied_ref == "extraction:1n:0e"
    confirmed_acme = graph_store.raw_node(acme_key)
    assert confirmed_acme is not None and confirmed_acme["assurance"] == "confirmed"

    # A confirmed proposal now skips (status != proposed) on a further run.
    third = run(extraction.run())
    assert third.writes[acme_pid] == {"skipped_status": "applied"}

    # Reject + revert: the OTHER (still-provisional) element is deleted.
    write_ctx.review_service.reject(
        globex_pid, _prov(), code=RejectionCode.INCORRECT, note="hallucinated"
    )
    revert_result = run(
        revert_extraction(
            write_ctx.engine,
            write_ctx.review_service,
            write_ctx.store,
            globex_pid,
            actor="steward",
            audit_sink=h.platform.sink,
            config_hash=h.platform.config_hash,
        )
    )
    assert revert_result["nodes_deleted"] == 1
    assert graph_store.raw_node(globex_key) is None
    # The confirmed Acme element is untouched by the unrelated revert.
    assert graph_store.raw_node(acme_key) is not None


def test_run_isolates_a_failing_batch_and_still_writes_others(
    make_platform: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Argus P2: one batch's write refusing (``ExtractionGateError``/
    ``GraphAclError`` — a stale-payload/schema-drift refusal, both documented
    as non-adversarially reachable) must NOT abort every OTHER batch this run
    built. Proposal ids are content-addressed and stable across re-runs
    (E-P1-1), so an unisolated raise would fail the SAME batch at the SAME
    position on EVERY future run — a permanent blockage, not a transient one.

    Drives the composed ``ExtractionRuntime.run`` for real (real platform,
    real sink), but swaps in a fake ``job`` (a frozen-dataclass field, so
    ``dataclasses.replace`` produces a fresh runtime with everything else —
    ``write_ctx``, ``sink``, ``config_hash`` — real) that hands back two fixed
    batch ids, and monkeypatches ``write_provisional_extraction`` itself
    (imported fresh inside ``run()`` on every call, so the patch is visible)
    to fail on ONE of them deterministically.
    """
    from dataclasses import replace

    from ne.brain_graph.extraction import apply as apply_mod
    from ne.brain_graph.extraction.job import ExtractionRunReport

    graph_store = InMemoryGraphStore()
    inner = ScriptedGateway([_c(_new_entity_completion("Account", "Acme"))])
    gateway = TaskRoutingGateway(inner, tasks={EXTRACTION_TASK: object()})
    h = make_platform(
        brain_graph_settings=_extraction_settings(extraction_mode="read_flagged"),
        graph_store=graph_store,
        gateway=gateway,
        gateway_tasks={EXTRACTION_TASK: {"model": "private-default"}},
        chunks=(_chunk_fixture("c1", "d1"),),
        extraction_chunk_source=_StaticExtractionChunkSource(
            {_DOCS: [_extraction_chunk("c1", "d1")]}
        ),
        flywheel_signal_url=_flywheel_url(tmp_path, "isolation"),
    )
    extraction = h.platform.extraction
    assert extraction is not None

    class _FakeJob:
        async def run(
            self, *, source: str | None = None, immediate_write_composed: bool = False
        ) -> ExtractionRunReport:
            return ExtractionRunReport(
                run_id="fake-run",
                mode="read_flagged",
                sources=(_DOCS,),
                chunks_scanned=1,
                chunks_skipped_no_acl=0,
                mentions=2,
                links_auto=0,
                batches_built=2,
                proposals_appended=2,
                discarded_below_threshold=0,
                quarantined_invalid=0,
                batch_proposal_ids=("bad-pid", "good-pid"),
                appended_proposal_ids=("bad-pid", "good-pid"),
            )

    async def _fake_write(*args: Any, **kwargs: Any) -> dict[str, Any]:
        proposal_id = args[4]
        if proposal_id == "bad-pid":
            raise apply_mod.ExtractionGateError("simulated schema-drift refusal")
        return {"nodes": 1, "edges": 0, "links_noop": 0}

    monkeypatch.setattr(apply_mod, "write_provisional_extraction", _fake_write)
    faked = replace(extraction, job=_FakeJob())  # type: ignore[arg-type]

    result = run(faked.run())
    # The bad batch's failure is RECORDED, not raised — run() completes.
    assert result.writes["bad-pid"] == {
        "error": "ExtractionGateError",
        "detail": "simulated schema-drift refusal",
    }
    # The good batch AFTER the bad one in iteration order still gets written —
    # the failure did not short-circuit the loop.
    assert result.writes["good-pid"] == {"nodes": 1, "edges": 0, "links_noop": 0}


def test_mode3_autonomous_writes_and_emits_mode_event(make_platform: Any, tmp_path: Path) -> None:
    graph_store = InMemoryGraphStore()
    inner = ScriptedGateway([_c(_new_entity_completion("Account", "Initech"))])
    gateway = TaskRoutingGateway(inner, tasks={EXTRACTION_TASK: object()})
    h = make_platform(
        brain_graph_settings=_extraction_settings(
            extraction_mode="autonomous", extraction_risk_accepted=True
        ),
        graph_store=graph_store,
        gateway=gateway,
        gateway_tasks={EXTRACTION_TASK: {"model": "private-default"}},
        chunks=(_chunk_fixture("c1", "d1"),),
        extraction_chunk_source=_StaticExtractionChunkSource(
            {_DOCS: [_extraction_chunk("c1", "d1")]}
        ),
        flywheel_signal_url=_flywheel_url(tmp_path, "mode3"),
    )
    extraction = h.platform.extraction
    assert extraction is not None
    result = run(extraction.run())
    key = llm_entity_key("Account", "Initech")
    initech_node = graph_store.raw_node(key)
    assert initech_node is not None and initech_node["assurance"] == "extracted"
    assert result.writes[result.report.batch_proposal_ids[0]] == {
        "nodes": 1,
        "edges": 0,
        "links_noop": 0,
    }

    events: list[Any] = []

    class _Recording:
        async def append(self, event: Any) -> None:
            events.append(event)

    # Wrap the real sink transparently to observe (the sink itself already
    # accepts appends fine; we drive emit_mode_event over a spy to assert the
    # SHAPE without depending on the audit store's read API here).
    from dataclasses import replace

    spying = replace(extraction, sink=_Recording())  # type: ignore[misc]
    run(spying.emit_mode_event())
    assert len(events) == 1
    assert events[0].action == "graph.extraction_mode"
    assert json.loads(events[0].detail) == {"mode": "autonomous"}
    assert events[0].config_hash == h.platform.config_hash


# --- (e) THE wire proof: assurance through the REAL mapping path -------------


def test_wire_distinguishes_extracted_from_source_through_real_mapping() -> None:
    backbone_ref = GraphEntityRef(entity_class="Account", entity_key="a1", display="Acme")
    extracted_ref = GraphEntityRef(
        entity_class="Person",
        entity_key="p1",
        display="Jane",
        assurance="extracted",
        # ADR-0015: the ENTITY's own evidencing ref — distinct from the hop's
        # own "llm:abc:c1" below — proving each rides its own field.
        source_ref="llm:00000000000000ff:c9",
    )
    backbone_hop = GraphHop(
        subject=backbone_ref,
        edge_kind="RELATED_TO",
        obj=backbone_ref,
        source_system="salesforce",
        source_ref="Account:a1",
    )
    extracted_hop = GraphHop(
        subject=extracted_ref,
        edge_kind="WORKS_AT",
        obj=backbone_ref,
        source_system="ne:llm",
        source_ref="llm:abc:c1",
        assurance="extracted",
    )
    result = RetrievalResult(
        chunks=(),
        citations=(Citation(chunk_id="c1", document_id="d1", source_system="w", source_ref="r"),),
        graph_paths=(GraphPath(hops=(backbone_hop, extracted_hop)),),
    )
    view = retrieval_result_view(result)
    hops = view.graph_paths[0].hops
    assert hops[0].subject.assurance == "source"
    assert hops[0].assurance == "source"
    assert hops[1].subject.assurance == "extracted"
    assert hops[1].obj.assurance == "source"
    assert hops[1].assurance == "extracted"
    # ADR-0015: the extracted ENTITY's own ref and the extracted HOP's
    # different ref each arrive on their OWN wire field through the real
    # retrieval_result_view path — never swapped/collapsed onto one another.
    assert hops[1].subject.source_ref == "llm:00000000000000ff:c9"
    assert hops[1].source_ref == "llm:abc:c1"
    # Provisional: the extracted entity (p1) + the extracted hop itself = 2.
    # The backbone entity (a1) and the backbone hop never count.
    assert view.provisional_source_count == 2


def test_provisional_source_count_dedups_across_paths_and_hop_positions() -> None:
    extracted_ref = GraphEntityRef(
        entity_class="Person", entity_key="p1", display="Jane", assurance="extracted"
    )
    other_ref = GraphEntityRef(entity_class="Account", entity_key="a1", display="Acme")
    # p1 appears as subject in one hop and object in another, across two paths —
    # counts ONCE. The extracted hop itself repeats identically across the two
    # paths (same subject/edge_kind/obj/source_ref) — also counts ONCE.
    hop_a = GraphHop(
        subject=extracted_ref,
        edge_kind="WORKS_AT",
        obj=other_ref,
        source_system="ne:llm",
        source_ref="llm:x:c1",
        assurance="extracted",
    )
    hop_b = GraphHop(
        subject=other_ref,
        edge_kind="OWNS",
        obj=extracted_ref,
        source_system="salesforce",
        source_ref="Account:a1",
    )
    paths = [
        GraphPathView(
            hops=[
                GraphHopView(
                    subject=GraphEntityRefView(
                        entity_class=h.subject.entity_class,
                        entity_key=h.subject.entity_key,
                        display=h.subject.display,
                        assurance=h.subject.assurance,
                        source_ref=h.subject.source_ref,
                    ),
                    edge_kind=h.edge_kind,
                    obj=GraphEntityRefView(
                        entity_class=h.obj.entity_class,
                        entity_key=h.obj.entity_key,
                        display=h.obj.display,
                        assurance=h.obj.assurance,
                        source_ref=h.obj.source_ref,
                    ),
                    source_system=h.source_system,
                    source_ref=h.source_ref,
                    assurance=h.assurance,
                )
                for h in hops
            ]
        )
        for hops in ((hop_a, hop_b), (hop_a, hop_b))
    ]
    # p1 (provisional entity) + hop_a (provisional hop) = 2, deduped across
    # BOTH repeated paths and both subject/object positions. hop_b's assurance
    # is "source" so it never contributes despite naming a provisional entity.
    assert _provisional_source_count(paths) == 2


def test_provisional_source_count_treats_unknown_label_as_provisional() -> None:
    """Fail toward flagging: an unrecognized/misspelled assurance value is NOT
    treated as vouched — pinning the design's 'fail-closed toward untrusted'
    stance rather than an allowlist-of-bad-values approach."""
    ref = GraphEntityRefView(
        entity_class="Account",
        entity_key="a1",
        display="Acme",
        assurance="tampered-value",
        source_ref="Account:a1",
    )
    hop = GraphHopView(
        subject=ref,
        edge_kind="RELATED_TO",
        obj=ref,
        source_system="w",
        source_ref="r",
        assurance="tampered-value",
    )
    paths = [GraphPathView(hops=[hop])]
    # ref (1, same entity_key both positions) + the hop itself (1) = 2.
    assert _provisional_source_count(paths) == 2


def test_provisional_source_count_confirmed_is_vouched_not_provisional() -> None:
    ref = GraphEntityRefView(
        entity_class="Account",
        entity_key="a1",
        display="Acme",
        assurance="confirmed",
        source_ref="Account:a1",
    )
    hop = GraphHopView(
        subject=ref,
        edge_kind="RELATED_TO",
        obj=ref,
        source_system="w",
        source_ref="r",
        assurance="confirmed",
    )
    assert _provisional_source_count([GraphPathView(hops=[hop])]) == 0


# --- (e-bis) provisional_summary refs (NE-T2X-5-RECORDER-WIRE) ----------------


def test_provisional_summary_refs_are_extracted_hop_source_refs_deduped() -> None:
    """Refs are ordered, first-occurrence-deduped provisional-HOP source_refs
    from the ONE shared traversal — the SAME identical extracted hop repeated
    across two paths contributes its ref ONCE."""
    extracted_ref = GraphEntityRefView(
        entity_class="Person",
        entity_key="p1",
        display="Jane",
        assurance="extracted",
        source_ref="llm:x:c1",
    )
    other_ref = GraphEntityRefView(
        entity_class="Account",
        entity_key="a1",
        display="Acme",
        assurance="source",
        source_ref="Account:a1",
    )
    hop = GraphHopView(
        subject=extracted_ref,
        edge_kind="WORKS_AT",
        obj=other_ref,
        source_system="ne:llm",
        source_ref="llm:x:c1",
        assurance="extracted",
    )
    paths = [GraphPathView(hops=[hop]), GraphPathView(hops=[hop])]
    summary = provisional_summary(paths)
    # p1 (provisional entity) + the hop itself (provisional) = 2, deduped
    # across BOTH repeated paths — matches the pinned count arithmetic.
    assert summary.provisional_source_count == 2
    assert summary.provisional_source_refs == ("llm:x:c1",)


def test_provisional_summary_entity_only_provisional_contributes_count_but_no_ref() -> None:
    """A provisional ENTITY on an all-vouched hop contributes to the count but
    has NO ref in ``provisional_source_refs`` — a DELIBERATE lane boundary
    (ADR-0015/NE-T2X-5-ENDPOINT-EVIDENCE-REF), not a datum-availability gap:
    the entity's own ref is reachable on ``hop.subject.source_ref`` below
    (asserted explicitly), it is simply not folded into THIS list because
    that would widen the §6.3 PDP/approval payload shape ``refs`` feeds —
    out of this unit's lanes."""
    extracted_ref = GraphEntityRefView(
        entity_class="Person",
        entity_key="p1",
        display="Jane",
        assurance="extracted",
        source_ref="llm:fedcba9876543210:doc-x#c1",
    )
    other_ref = GraphEntityRefView(
        entity_class="Account",
        entity_key="a1",
        display="Acme",
        assurance="source",
        source_ref="Account:a1",
    )
    hop = GraphHopView(
        subject=extracted_ref,
        edge_kind="WORKS_AT",
        obj=other_ref,
        source_system="salesforce",
        source_ref="Account:a1",
        assurance="source",
        # The hop itself is vouched ('source') — only the subject is provisional.
    )
    # The entity's own ref IS reachable on the wire view (the ADR-0015 fix) —
    # it just never lands in provisional_source_refs (see docstring above).
    assert hop.subject.source_ref == "llm:fedcba9876543210:doc-x#c1"
    summary = provisional_summary([GraphPathView(hops=[hop])])
    assert summary.provisional_source_count == 1
    assert summary.provisional_source_refs == ()


# --- (f) default-off identity -------------------------------------------------


def test_extraction_disabled_platform_extraction_is_none(make_platform: Any) -> None:
    h = make_platform()
    assert h.platform.extraction is None


# --- (g) fail-louds -----------------------------------------------------------


def test_extraction_enabled_without_flywheel_db_fails_boot(make_platform: Any) -> None:
    with pytest.raises(ValueError, match="flywheel signal database"):
        make_platform(
            brain_graph_settings=_extraction_settings(),
            graph_store=InMemoryGraphStore(),
            gateway_tasks={},
            extraction_chunk_source=_StaticExtractionChunkSource({}),
            # flywheel_signal_url intentionally omitted.
        )


def test_extraction_enabled_without_chunk_source_fails_boot(
    make_platform: Any, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="extraction_chunk_source"):
        make_platform(
            brain_graph_settings=_extraction_settings(),
            graph_store=InMemoryGraphStore(),
            gateway_tasks={},
            flywheel_signal_url=_flywheel_url(tmp_path, "no-chunk-source"),
            # extraction_chunk_source intentionally omitted.
        )


# --- lifespan wiring: the mode event fires ONCE at real app startup ---------


def _extraction_mode_events(tmp_path: Path, key: str) -> list[Any]:
    from ne.audit.store import ne_audit_log

    engine = sa.create_engine(f"sqlite:///{tmp_path}/audit-{key}.db")
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_audit_log.c.payload).where(
                    ne_audit_log.c.action == "graph.extraction_mode"
                )
            ).all()
    finally:
        engine.dispose()
    return list(rows)


def test_app_lifespan_emits_mode_event_when_extraction_composed(tmp_path: Path) -> None:
    graph_store = InMemoryGraphStore()
    inner = ScriptedGateway()
    gateway = TaskRoutingGateway(inner, tasks={EXTRACTION_TASK: object()})
    seams = _seams(
        tmp_path,
        key="lifespan-on",
        brain_graph_settings=_extraction_settings(),
        graph_store=graph_store,
        gateway=gateway,
        gateway_tasks={EXTRACTION_TASK: {"model": "private-default"}},
        extraction_chunk_source=_StaticExtractionChunkSource({}),
        flywheel_signal_url=_flywheel_url(tmp_path, "lifespan-on"),
    )
    reset_config_hash_for_testing()
    app = create_app(_api_settings(), seams=seams)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            pass

    run(scenario())

    rows = _extraction_mode_events(tmp_path, "lifespan-on")
    assert len(rows) == 1
    assert json.loads(rows[0].payload["detail"]) == {"mode": "ingest_review"}
    for engine in get_state(app).engines:
        engine.dispose()


def test_app_lifespan_no_mode_event_when_extraction_disabled(tmp_path: Path) -> None:
    seams = _seams(tmp_path, key="lifespan-off")
    reset_config_hash_for_testing()
    app = create_app(_api_settings(), seams=seams)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            pass

    run(scenario())

    assert _extraction_mode_events(tmp_path, "lifespan-off") == []
    for engine in get_state(app).engines:
        engine.dispose()


def test_provisional_summary_repeats_a_ref_across_distinct_hops() -> None:
    """Argus P3: the dedup key is the HOP, not the ref STRING.

    Two DISTINCT provisional hops that happen to share one ``source_ref``
    each contribute it, so ``refs`` repeats. That is the common real shape —
    the writer stamps ``llm:<sha16>:<chunk-ref>`` per source CHUNK and one
    chunk typically evidences several edges — and the previous coverage
    reused the SAME hop object, where the ref-string and hop-identity dedup
    keys coincide and the distinction is invisible.

    Pins the ACTUAL behaviour so the docstring's "first-occurrence-
    deduplicated" cannot be read as string-dedup. Harmless downstream only
    because ``ne.runtime.provisional``'s accumulator dedupes by ref string —
    a property of the CONSUMER, asserted here so a future change to either
    side has to confront it rather than silently rely on it.
    """
    shared_ref = "llm:sha16:c1"
    subj = GraphEntityRefView(
        entity_class="Person",
        entity_key="p1",
        display="Jane",
        assurance="extracted",
        source_ref="llm:sha16:jane-c1",
    )
    obj_a = GraphEntityRefView(
        entity_class="Account",
        entity_key="a1",
        display="Acme",
        assurance="source",
        source_ref="Account:a1",
    )
    obj_b = GraphEntityRefView(
        entity_class="Account",
        entity_key="a2",
        display="Beta",
        assurance="source",
        source_ref="Account:a2",
    )
    hop_a = GraphHopView(
        subject=subj,
        edge_kind="WORKS_AT",
        obj=obj_a,
        source_system="ne:llm",
        source_ref=shared_ref,
        assurance="extracted",
    )
    hop_b = GraphHopView(
        subject=subj,
        edge_kind="ADVISES",
        obj=obj_b,
        source_system="ne:llm",
        source_ref=shared_ref,
        assurance="extracted",
    )
    summary = provisional_summary([GraphPathView(hops=[hop_a, hop_b])])

    assert summary.provisional_source_refs == (shared_ref, shared_ref), (
        "distinct provisional hops sharing one source_ref must EACH contribute it — "
        "the dedup key is the hop identity, not the ref string"
    )
    # Control: the same hop repeated still collapses to one ref, so this test
    # is distinguishing hop-identity dedup from no-dedup-at-all, not merely
    # asserting that dedup is absent.
    assert provisional_summary(
        [GraphPathView(hops=[hop_a]), GraphPathView(hops=[hop_a])]
    ).provisional_source_refs == (shared_ref,)


# --- (h) B-P0-1 no-request-override guardian (NE-T2X-DOC-SYNC) ---------------
#
# The design's B-P0-1 disposition (design line 96) promises that no
# request/wire field influences extraction behavior. This section makes that
# claim MECHANICAL, per the vacuity-audit-ledger discipline: the forbidden field set is DERIVED
# by reflection over ``BrainGraphSettings.model_fields`` rather than
# enumerated, so a future 7th ``extraction_*`` setting joins the forbidden
# set with zero test-file changes; wire/contract models are discovered by a
# ``pkgutil.walk_packages`` sweep of the ENTIRE ``ne.api`` and
# ``ne.contracts`` packages (every module, not a hand-picked subset), not
# hand-listed, so a new module ANYWHERE in either package — a new router, a
# new top-level wire file like ``graph_wire.py``, a new contracts submodule —
# is swept in automatically; models are keyed by fully-qualified name
# (``module.ClassName``) so same-named classes in different modules never
# shadow one another; discovery is guarded by membership controls + numeric
# floors so a broken discoverer fails LOUD rather than silently passing on
# zero models; and an in-suite RED control
# (``test_bp01_guard_detects_a_smuggled_field``) proves the guard actually
# fires (non-vacuity), per the vacuous-guard-shapes discipline.
#
# Argus P1 (round 1): the original sweep was a five-module literal list
# (``api_wire``, ``turn_wire``, ``tool_transit``, ``scim_models``,
# ``contracts_types``) plus a routers-only pkgutil pass, and it MISSED
# ``ne.api.graph_wire`` — the module defining ``GraphQueryRequest`` and
# ``GraphPathsRequest``, the POST /v1/graph/query and /v1/graph/paths request
# models, i.e. the wire surface closest to the T2 graph subsystem this guard
# exists to protect. A demonstrated blind spot, not a hypothetical one: with
# an ``extraction_mode`` field injected into ``ne.api.graph_wire``, all four
# ``test_bp01_*`` tests still passed. Fixed by walking the full package tree
# instead of a curated module list.
#
# Precedent: ``test_grounding_wiring.py::test_no_request_level_strictness_
# override`` — NE-HGATE's B-P0-1 guardian for the grounding/strictness-tier
# subsystem. That is a DIFFERENT subsystem (it proves wire models cannot name
# a grounding tier); this section is extraction's own guardian, since no test
# previously pinned the extraction claim at all.
#
# Argus P1 (round 2): the model sweep above is blind to FastAPI
# route-signature parameters — a caller-controlled surface that does not
# require a pydantic model at all. ``ne.api.routers.graph.graph_neighborhood``
# already declares its request inputs as bare ``Annotated[..., Query(...)]``
# endpoint parameters (``entity``, ``hops``, ``limit_nodes``, ``limit_edges``
# — GET /v1/graph/neighborhood, not a body model), so a future
# ``extraction_mode: Annotated[str, Query()] = "autonomous"`` on any route
# would leave every model-only ``test_bp01_*`` test green. Fixed by sweeping
# every ``APIRoute`` endpoint's signature with the SAME derived nets, over
# routers discovered on the SAME walked package tree (no curated router
# list). Argus P3 (round 2): the request-model floor was set to the EXACT
# observed count (25), reddening on any unrelated model rename anywhere in
# ne.api/ne.contracts — loosened to "observed minus 2" (23), matching the
# other floors' discipline. Argus P3 (round 2): ``pkgutil.walk_packages`` now
# passes ``onerror`` so an unimportable subpackage fails the sweep LOUD
# instead of silently vanishing along with everything beneath it.
#
# Argus P1 (round 3): the round-2 route sweep collected only MODULE-LEVEL
# ``APIRouter`` objects — blind to factory-built routers (``build_scim_app``),
# proven by transient mutation on GET ``/scim/v2/Users`` (added
# ``extraction_mode: str = Query(default="autonomous")`` to ``_list_users``;
# the round-2 ``-k bp01`` selection stayed fully green — 6 passed, 22
# deselected). Fixed by a THIRD surface: walk the COMPOSED application's
# mounted route table (``_walk_composed_routes``,
# ``test_bp01_composed_app_routes_*``) — enumeration-vs-derivation one level
# up: ask the object FastAPI actually serves, not the module namespace.
# Argus P3 (round 3): both model nets intersected field KEYS only — a field
# named innocuously in Python but wired to ``extraction_mode`` via
# ``alias``/``validation_alias`` satisfied every guard while exposing
# ``extraction_mode`` on the wire; alias/validation_alias smuggling is now
# unioned into the swept name set (``_model_wire_names``), pinned by
# in-suite offenders (``test_bp01_guard_detects_an_alias_smuggled_field``).
#
# Argus P1 (round 4): the round-3 composed-app fix still read each route's
# wire names off ``inspect.signature(route.endpoint).parameters`` — blind to
# a parameter declared inside a ``Depends(...)`` callable rather than the
# endpoint's own signature. Live, not hypothetical: ``build_scim_app`` gates
# every ``/scim/v2`` route with ``APIRouter(dependencies=[Depends(
# require_bearer)])`` (``ne.api.scim.router``/``ne.api.scim.auth``), whose
# dependency declares an ``authorization`` header parameter FastAPI resolves
# from the caller's request — invisible to the round-3 sweep on all fifteen
# SCIM routes, and exactly the shape an ``extraction_mode`` dependency could
# hide behind next. Fixed by walking ``route.dependant`` — the PUBLIC
# ``fastapi.dependencies.models.Dependant`` FastAPI already resolved onto
# every ``APIRoute`` — recursively over ``.dependencies``, reading
# ``.query_params``/``.header_params``/``.cookie_params``/``.path_params``
# (``_dependant_wire_names``), which strictly dominates the signature-based
# approach it replaces: FastAPI has already resolved every alias onto
# ``field_info.alias`` by the time the ``Dependant`` exists, so no
# ``get_type_hints``/``Annotated``-parsing of our own is needed at all.
# Pinned by an in-suite RED/GREEN control
# (``test_bp01_composed_walker_detects_a_dependency_injected_smuggle``) and
# by the live transient mutation on ``ne.api.scim.auth.build_scim_bearer_
# dependency`` recorded in this unit's commit message. Argus P3 (round 4):
# a request BODY parameter (``.body_params``) still contributes only its
# Python parameter name (e.g. ``"payload"``), never the fields *inside* the
# body pydantic model — a documented boundary, not an oversight, because
# field-level body-model coverage already comes from the separate
# ``_all_models``/``_request_models`` sweep for any body model defined
# inside the walked ``ne.api``/``ne.contracts`` trees (true of every body
# parameter on the live composed app today; see
# ``_dependant_wire_names``'s docstring).
#
# Review after round 4 found the round-4 fix itself had the exact shape it
# exists to prevent — a stated boundary list ("THREE honest coverage
# boundaries") that OVERCLAIMED totality: (Argus P2) ``_dependant_wire_names``
# took ``field_info.alias`` only from ``fastapi.params.Param``, missing
# ``fastapi.params.Body`` entirely (``Form``/``File`` subclass ``Body``, not
# ``Param``) — a scalar ``Body(alias="extraction_mode", embed=True)`` or
# ``Form(alias="extraction_mode")`` parameter bound the caller's JSON/form
# key while every guard stayed green; fixed by widening the ``isinstance``
# check, pinned by
# ``test_bp01_composed_walker_detects_a_body_or_form_alias_smuggle``. (Argus
# P3) a ``Security(scheme)`` dependency (``APIKeyQuery``/``APIKeyHeader``/
# ``APIKeyCookie``) binds a wire name that lives ONLY on ``scheme.model.
# name`` — FastAPI builds an EMPTY sub-``Dependant`` for it, invisible to the
# param-list walk; fixed by reading ``sub_dependant.call.model.name`` when
# ``.call`` is a ``fastapi.security.base.SecurityBase``, pinned by
# ``test_bp01_composed_walker_detects_a_security_scheme_smuggle``. Two
# walker-labeling nits were also fixed in ``_walk_composed_routes`` (a
# ``Mount`` nested inside a prefixed sub-router now keys under the correct
# prefixed path; a ``StaticFiles`` mount is excluded from ``opaque`` instead
# of flagged as an unwalkable blind spot), pinned by
# ``test_bp01_composed_walker_resolves_a_router_prefixed_mount_path`` and
# ``test_bp01_composed_walker_does_not_flag_staticfiles_as_opaque``. A
# dependency reading the raw ``Request`` object directly remains a NAMED,
# genuinely non-closable boundary rather than an implied one — see
# ``_dependant_wire_names``'s and ``test_bp01_composed_app_routes_cannot_
# name_an_extraction_knob``'s docstrings. No live exposure existed for any
# of the closed gaps; this is coverage hardening, not an incident response.
#
# Review after round 5 (Argus P2, round 6) found the walker skipped FastAPI
# ``APIWebSocketRoute`` objects in a silent, unconditional ``else`` branch
# whose own comment affirmatively declared that surface safe — reasoning
# that is true for a plain Starlette ``Route``/``WebSocketRoute`` (raw
# endpoint, no declared binding) but FALSE for ``APIWebSocketRoute``, which
# binds declared ``Query``/``Header``/``Cookie``/``Path``/``Depends``
# parameters for the handshake exactly like an ``APIRoute`` does. Proven
# live by Argus's transient mutation on ``ne.api.routers.graph`` (a
# ``@router.websocket("/v1/graph/stream")`` route binding
# ``extraction_mode`` via ``Query`` left ``-k bp01`` fully green). Fixed by
# sweeping ``APIWebSocketRoute.dependant`` with the same
# ``_route_param_wire_names``/``_dependant_wire_names`` extraction used for
# ``APIRoute``, pinned by
# ``test_bp01_composed_walker_detects_a_websocket_smuggle``. The silent
# ``else``-skip is gone: remaining skips are a NAMED, exact-type Starlette
# ``Route``/``WebSocketRoute`` boundary, and anything else — including a
# subclass of either — REDS the guard via ``opaque`` instead of being
# silently declared safe, pinned by
# ``test_bp01_composed_walker_flags_an_unrecognized_route_class_as_opaque``.
#
# Review after round 6 (Argus P1, round 7) found a gap ONE LAYER BELOW the
# round-6 fix: even for a route class the walker recognized (``APIRoute``/
# ``APIWebSocketRoute``), it read ``_route_param_wire_names(ctx.
# original_route)`` — the route object's OWN construction-time
# ``.dependant`` — never the EFFECTIVE, composition-time dependant FastAPI
# actually resolves once the route is mounted. ``include_router(router,
# dependencies=[...])`` (dependencies given to the INCLUDE call, distinct
# from ``APIRouter(dependencies=[...])`` at router-construction time, which
# WAS already covered) and ``FastAPI(dependencies=[...])`` (app-level) are
# both merged in later by ``fastapi.routing._EffectiveRouteContext`` and
# never touch the original route object at all. Proven live by TWO
# transient mutations on the real app (both made, observed RED, reverted;
# transcripts in this unit's round-7 commit message): ``inner.
# include_router(graph.router, dependencies=[Depends(_mode_dep)])`` bound
# ``extraction_mode`` on every ``/api/v1/graph/*`` route; ``inner =
# FastAPI(dependencies=[Depends(_mode_dep)])`` bound it on every
# ``/api/v1/*`` route — BOTH left ``-k bp01`` fully green (16 passed, 22
# deselected), including this walker's own ``opaque == []`` assertion.
# Fixed by unioning in ``_effective_wire_names(ctx)`` — reached via
# ``RouteContext.__getattr__``'s proxy to the ``_EffectiveRouteContext``
# (``ctx.dependant`` for an ``APIRoute``; ``ctx.starlette_route.dependant``
# for an ``APIWebSocketRoute`` — a verified asymmetry, see that function's
# docstring) — in both the ``APIRoute`` and ``APIWebSocketRoute`` branches,
# pinned by ``test_bp01_composed_walker_detects_an_include_level_
# dependency_smuggle``, ``test_bp01_composed_walker_detects_a_websocket_
# include_level_dependency_smuggle``, and ``test_bp01_composed_walker_
# detects_an_app_level_dependency_smuggle``. The round-4 dependency control
# did not catch this because it calls ``_route_param_wire_names`` directly
# on a bare ``APIRouter``'s routes and never exercises ``_walk_composed_
# routes``/``iter_route_contexts`` at all — proving one dependency-
# attachment shape (router-constructor-level) safe was read as proving the
# whole class safe, the same escape shape as every prior round. No live
# exposure existed on the real composed app (verified: no
# ``include_router(..., dependencies=...)`` or ``FastAPI(dependencies=...)``
# call exists anywhere in ``ne.api`` today) — this is coverage hardening,
# not an incident response.
#
# Review after round 7 (Argus, round 8) found four more findings, none a
# live caller-facing exposure, all now closed or NAMED per this unit's
# "inspect, or say you didn't" discipline:
#
# (P2) The round-7 app-level control (``test_bp01_composed_walker_detects_
# an_app_level_dependency_smuggle``) did not actually control: it built its
# probe route with ``@smuggled_app.get(...)`` directly on the
# ``FastAPI(dependencies=[...])`` instance, which bakes the APP's own
# ``dependencies`` into that route's OWN construction-time ``.dependant``
# at add-time (the SAME round-4 mechanism ``_route_param_wire_names`` alone
# already reaches) — so the test passed even with ``_effective_wire_names``
# monkeypatched to always return ``set()``. Fixed by rebuilding the probe
# route inside an ``APIRouter`` factory and reaching it via
# ``include_router(...)`` on the app-level-dependency app — the shape
# where the round-7 fix is actually load-bearing, matching the shape
# Argus's own round-7 live mutation used.
#
# (P2) A pydantic MODEL used as a Query/Header/Cookie/Path parameter (e.g.
# ``q: Annotated[QModel, Query()]``, FastAPI's documented form since
# 0.115) contributed only its outer PYTHON PARAMETER NAME — never a field
# living inside the model — while the boundary list below implied only the
# BODY-model case was open. Closed directly in ``_dependant_wire_names``
# (see its docstring) rather than left to rely on the separate
# ``_all_models``/``_request_models`` sweep, which only ever reached a
# model DEFINED inside the walked ``ne.api``/``ne.contracts`` trees; pinned
# by ``test_bp01_composed_walker_detects_a_query_model_field_smuggle``
# (whose probe model is deliberately defined OUTSIDE both trees, to prove
# the closure, not the external net, is doing the work).
#
# (P2, documented rather than swept) FastAPI's ``APIRouter.frontend(...)``
# (added by the SAME 0.139 lazy-``include_router`` refactor that introduced
# ``iter_route_contexts``) registers routes onto ``_low_priority_routes`` —
# a dispatch path SEPARATE from ``.routes`` that ``iter_route_contexts``
# never visits, and that CAN carry a router-level ``Depends(...)`` (proven
# empirically). No live exposure exists (``.frontend(`` has zero call
# sites anywhere in ``ne.api``/``ne.contracts`` today; the console SPA uses
# ``Mount`` + ``StaticFiles`` instead, already handled above). Deliberately
# NOT swept: reaching it needs ``APIRouter._low_priority_routes`` and
# ``_IncludedRouter.effective_low_priority_routes()`` — both underscore-
# prefixed FastAPI internals, unlike ``iter_route_contexts`` itself (see
# the VERSION COUPLING paragraph in ``_walk_composed_routes``'s docstring
# for why this walker deliberately stays off private internals). Named as
# a FOURTH honest boundary in ``_walk_composed_routes``'s docstring and in
# ``test_bp01_composed_app_routes_cannot_name_an_extraction_knob``'s
# boundary list, and PROVEN (not just claimed) by
# ``test_bp01_composed_walker_does_not_see_frontend_low_priority_routes``,
# which confirms both that the surface is real (a router-level dependency
# DOES land on the frontend group's own dependant) and that the walker is
# silent about it.
#
# (P3) ``params`` is keyed by ``f"{module}:{methods} {path}"`` and was
# assigned with plain ``params[key] = ...`` — two distinct discovered
# routes sharing all three components would silently drop one's wire-name
# surface with no ``opaque`` entry to flag the loss. Fixed by unioning
# onto any existing entry instead of overwriting, pinned by
# ``test_bp01_composed_walker_unions_wire_names_on_key_collision``.
#
# (P3, documented) ``app.dependency_overrides[dep] = other_dep`` resolves
# the OVERRIDE callable's own parameters at request time — invisible to
# any static walk. Testing-only FastAPI hook; nothing in the repo sets it
# outside test harnesses. Named in ``_dependant_wire_names``'s docstring.
#
# Review after round 8 (round 9) is a CHANGE OF METHOD, not another guarded
# surface. Count the rounds above: module-level ``APIRouter`` -> factory-
# built routers -> ``Mount`` targets -> ``APIWebSocketRoute`` -> depth-0
# unwrapped models (round 8's own ``isinstance(f.field_info.annotation,
# type) and issubclass(..., BaseModel)`` check, and ``_model_wire_names``'s
# non-recursive walk). Argus proved round 8's model sweep was STILL only
# depth-0 and unwrapped-only by transient mutation on a nested, wrapped
# field on ``ne.api.routers.graph``'s module-level, unconditionally-
# included router (made, observed RED, reverted, ``git status --porcelain``
# empty, control re-run green — transcript in this unit's round-9 commit
# message) while three separate docstrings declared the model surface
# CLOSED. Every round guarded exactly the shape found and the next round
# found one level deeper — enumeration converges only by accident. Fixed by
# replacing the enumerated depth-0 check with ``_models_in_annotation``, a
# STRUCTURAL decomposer: it unwraps ``Annotated``, unions, and generic
# containers to their argument types and recurses, reducing every
# annotation to either "contains no ``BaseModel``" or "these ``BaseModel``s"
# — and anything it cannot reduce to one of those two answers is appended
# to ``opaque`` (the SAME fail-loud channel round 6 already uses for an
# unrecognized route class) rather than silently treated as "no models".
# ``_model_wire_names`` becomes recursive with a ``_seen`` cycle guard, so a
# nested or self-referential model's OWN fields are unioned in at any
# depth. See ``_models_in_annotation``'s docstring for the full decision
# table and ``test_bp01_annotation_decomposer_reduces_wrappers_and_reds_on_
# exotics`` for the bidirectional proof (every wrapper shape reduces
# correctly; every exotic/undecomposable shape reds with exactly one
# entry) — the coverage claim is now a structural property, not a list of
# shapes someone thought to test for.


def _models_defined_in(module: ModuleType) -> dict[str, type[BaseModel]]:
    """Every pydantic ``BaseModel`` subclass DEFINED IN (not merely imported
    into) ``module``, keyed by BARE class name. ``obj.__module__ ==
    module.__name__`` excludes re-exports, so a model imported into a router
    file for type hints is not double-counted under that router's namespace.

    Callers that aggregate across MULTIPLE modules must re-key by fully
    qualified name (``module.__name__ + "." + name``) themselves — see
    ``_all_models()`` — so that two modules defining a same-named class never
    silently shadow one another (Argus P3, round 1).
    """
    return {
        name: obj
        for name, obj in vars(module).items()
        if isinstance(obj, type)
        and issubclass(obj, BaseModel)
        and obj is not BaseModel
        and obj.__module__ == module.__name__
    }


def _extraction_knob_fields() -> set[str]:
    """The forbidden field set — DERIVED by reflection over
    ``BrainGraphSettings.model_fields``, never enumerated. A future
    ``extraction_*`` setting joins this set automatically.
    """
    return {n for n in BrainGraphSettings.model_fields if n.startswith("extraction_")}


def _validation_alias_names(va: object, *, opaque: list[str]) -> set[str]:
    """The caller-facing wire name(s) a pydantic ``validation_alias`` binds.

    ``None`` -> no alias, no names. ``str`` -> that literal name.
    ``AliasPath`` -> its first path segment, when that segment is a string (a
    leading int would be a list index, not a field name — never the case for
    a top-level knob name). ``AliasChoices`` -> the union over every choice
    (each recursed the same way, passing ``opaque`` through, so an
    ``AliasChoices`` mixing plain strings and ``AliasPath``s is fully swept).

    Round 9 (CHANGE OF METHOD): anything else is now UNDECOMPOSABLE, not
    silently "no names" — a ``pytest.fail``-triggering ``opaque`` entry is
    appended naming the shape's ``repr()``, the same fail-loud primitive
    ``_models_in_annotation`` and the route-class walk already use. An
    unrecognized ``validation_alias`` shape (e.g. a future pydantic version
    introducing a new alias type) REDS the guard instead of being assumed to
    contribute nothing.
    """
    if va is None:
        return set()
    if isinstance(va, str):
        return {va}
    if isinstance(va, AliasPath):
        first = va.path[0] if va.path else None
        return {first} if isinstance(first, str) else set()
    if isinstance(va, AliasChoices):
        names: set[str] = set()
        for choice in va.choices:
            names |= _validation_alias_names(choice, opaque=opaque)
        return names
    opaque.append(f"undecomposable validation_alias {va!r} ({type(va).__name__})")
    return set()


def _models_in_annotation(annotation: Any, *, opaque: list[str]) -> set[type[BaseModel]]:
    """Structurally reduce ``annotation`` to the set of ``BaseModel``
    subclasses reachable inside it, or fail loud into ``opaque``.

    Round 9 (CHANGE OF METHOD): this is the primitive the whole B-P0-1 model
    walk pivots on. Every previous round guarded exactly the shape Argus
    found (module-level routers -> factory-built routers -> ``Mount``
    targets -> ``APIWebSocketRoute`` -> depth-0 unwrapped models) and the
    next round found one level deeper. Enumerating shapes converges only by
    accident. This function instead DECOMPOSES an annotation to either
    "contains no ``BaseModel``" or "these ``BaseModel``s" — and anything it
    cannot reduce to one of those two answers is appended to ``opaque``
    (never silently treated as "no models"), the same fail-loud channel that
    already reds the guard for an unrecognized route class.

    Ordered decision table (every branch is intentional; order matters —
    e.g. a pydantic model must be checked before the generic "plain type"
    fallback):

    1. ``None`` / ``NoneType`` / ``Ellipsis`` -> no models. These are not
       type annotations at all (a bare ``None`` sentinel, ``type(None)`` from
       an ``Optional`` unwrap, or the ``...`` used inside
       ``tuple[int, ...]``) and carry no field surface.
    2. ``Any`` -> no NAMED model surface. DOCUMENTED DECISION: ``Any``
       declares an arbitrary, un-typed payload — the same category as the
       already-named raw-``Request`` boundary (``_dependant_wire_names``'s
       docstring): there is no declared field for FastAPI to bind a caller
       value to by name, so there is nothing here for a NAME-based guard to
       sweep. This is a decision, not an oversight — recorded here so a
       future reviewer does not mistake it for an unhandled case.
    3. A concrete ``type`` that is a ``BaseModel`` subclass -> ``{annotation}``.
       This also covers pydantic v2 parametrized generics (e.g.
       ``GenericModel[int]``), which pydantic resolves to a concrete
       ``BaseModel`` subclass satisfying ``isinstance(x, type)``.
    4. A ``TypedDict`` -> UNDECOMPOSABLE, appended to ``opaque``. A
       ``TypedDict`` declares a named-field surface exactly like a
       ``BaseModel`` does, but this walker has no machinery to sweep its
       fields (no ``model_fields``/alias machinery to read) — rather than
       silently claiming "no models" for a shape that plainly CAN carry
       named fields, it reds the guard so a human decides.
    5. A stdlib ``dataclasses.dataclass`` -> UNDECOMPOSABLE, appended to
       ``opaque``, same reasoning as (4) — FastAPI officially supports
       dataclass request bodies (verified empirically: a route with a
       ``@dataclass`` body parameter constructs and serves), so this is a
       REAL, reachable shape, not a hypothetical.
    6. Anything with a ``typing.get_origin(...)`` -> decomposed structurally:
       - ``Annotated[X, ...]`` -> recurse into ``X`` (``get_args(...)[0]``)
         ONLY; the remaining args are ``Param``/``Body``/``FieldInfo``
         metadata markers, never wire types.
       - ``Literal[...]`` -> no models; the args are VALUES, not types.
       - any other origin (``list``, ``dict``, ``tuple``, ``set``,
         ``frozenset``, ``collections.abc.Sequence``, ``typing.Optional``,
         ``typing.Union``, and PEP 604 ``X | Y`` / ``types.UnionType``) ->
         union of recursing every arg in ``get_args(...)`` (skipping a bare
         ``Ellipsis``, e.g. the second slot of ``tuple[int, ...]``) PLUS
         recursing the origin itself when ``isinstance(origin, type)`` — a
         guard that specifically excludes the ``typing.Union`` SPECIAL FORM
         (not a ``type``) from being treated as an annotation and landing in
         ``opaque`` for no reason; ``types.UnionType`` (PEP 604's own origin)
         and container origins like ``list``/``dict`` ARE real types, so the
         guard admits them (harmlessly — none of them are ``BaseModel``
         subclasses, so recursing the origin itself just contributes the
         empty set; the real work happens via the ``get_args`` recursion).
       This one branch uniformly covers every generic-container and
       union shape — there is no separate list/dict/union enumeration to
       keep in sync.
    7. A ``typing.TypeAliasType`` -> recurse into its ``__value__``. PEP 695
       aliases are transparent wrappers, not scalar types; their value may
       contain the same nested model/Literal/container surface guarded here.
    8. A ``typing.NewType`` (``hasattr(annotation, "__supertype__")``) ->
       recurse into ``annotation.__supertype__``.
    9. A plain ``type`` that reached this point (not a ``BaseModel``
       subclass, not a ``TypedDict``, not a dataclass, no ``get_origin``) ->
       no models. A scalar type (``str``, ``int``, ``UUID``, ``datetime``,
       ``Enum``, ``Decimal``, a custom scalar) binds as a single value under
       the parameter's own already-swept name/alias and declares no nested
       pydantic field surface of its own.
    10. Anything else -> UNDECOMPOSABLE, appended to ``opaque``. Catches a
       bare ``TypeVar``, an unresolved ``ForwardRef``/string annotation, a
       ``ParamSpec``, and every future shape this decomposer was not taught
       — by design: an unhandled shape belongs in ``opaque``, not in a
       silently-widening whitelist.
    """
    if annotation is None or annotation is type(None) or annotation is Ellipsis:
        return set()
    if annotation is Any:
        return set()
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {annotation}
    if is_typeddict(annotation):
        opaque.append(
            f"undecomposable annotation {annotation!r} "
            "(TypedDict — named-field surface the model walk does not decompose)"
        )
        return set()
    if dataclasses.is_dataclass(annotation):
        opaque.append(
            f"undecomposable annotation {annotation!r} "
            "(dataclass — named-field surface the model walk does not decompose)"
        )
        return set()
    origin = get_origin(annotation)
    if origin is not None:
        if origin is Annotated:
            return _models_in_annotation(get_args(annotation)[0], opaque=opaque)
        if origin is Literal:
            return set()
        models: set[type[BaseModel]] = set()
        if isinstance(origin, type):
            models |= _models_in_annotation(origin, opaque=opaque)
        for arg in get_args(annotation):
            if arg is Ellipsis:
                continue
            models |= _models_in_annotation(arg, opaque=opaque)
        return models
    if isinstance(annotation, TypeAliasType):
        return _models_in_annotation(annotation.__value__, opaque=opaque)
    if hasattr(annotation, "__supertype__"):
        return _models_in_annotation(annotation.__supertype__, opaque=opaque)
    if isinstance(annotation, type):
        return set()
    opaque.append(f"undecomposable annotation {annotation!r} ({type(annotation).__name__})")
    return set()


def _model_wire_names(
    model: type[BaseModel],
    *,
    _seen: set[type[BaseModel]] | None = None,
    opaque: list[str] | None = None,
) -> set[str]:
    """The full caller-controllable INPUT wire surface of ``model``: every
    field's Python name, its ``alias`` (if any), every name its
    ``validation_alias`` binds, and — round 9 (CHANGE OF METHOD) — the same
    swept for every ``BaseModel`` reachable through the field's annotation at
    ANY nesting depth, through ``Annotated`` wrappers, unions, and generic
    containers (``_models_in_annotation`` does the structural decomposition;
    see its docstring for the full decision table). A nested model's own
    field NAMES are caller-bindable JSON keys exactly like the outer model's
    own fields are — a caller submitting ``{"mid": {"inner": {"extraction_
    mode": "yolo"}}}`` binds ``extraction_mode`` just as directly as a
    top-level field would, so nested names are unioned in, not merely
    noted.

    ``_seen`` guards recursion against a self-referential / mutually-
    referential model graph (a model with a field typed as itself, directly
    or through a cycle of other models) — each model is visited at most
    once, so the walk always terminates.

    Fail-loud contract: this function defaults to ``opaque=None``, meaning
    "I am the top-level call" — in that case, any undecomposable annotation
    found anywhere in the walk (this model's own fields, or any nested
    model's) triggers ``pytest.fail`` before returning, so every EXISTING
    call site (the T2/T3 sweeps via ``_extraction_influencing_fields``, the
    round-3 tests) becomes fail-loud with zero call-site changes. A caller
    that wants to aggregate ``opaque`` across many models/routes itself
    (e.g. ``_dependant_wire_names``, ``_walk_composed_routes``) passes its
    own ``opaque`` list explicitly, which suppresses the local
    ``pytest.fail`` — the caller is responsible for asserting on it.

    The coverage claim this function makes — "every field's wire name, at
    any nesting depth, through any wrapper" — is no longer a list of shapes
    someone thought to test for; it is a structural property of
    ``_models_in_annotation``'s decomposition, proven by
    ``test_bp01_annotation_decomposer_reduces_wrappers_and_reds_on_exotics``
    (every wrapper shape reduces correctly, every exotic shape reds) and by
    ``test_bp01_model_wire_names_recurse_nested_and_wrapped_models`` (the
    recursive walk itself, plus the cycle guard).

    Deliberately excludes ``serialization_alias`` — that is an OUTBOUND
    display name pydantic uses when dumping a model, never a name pydantic
    binds an incoming request value to, so it cannot smuggle caller input
    into extraction config and is out of scope for this validation-side
    sweep.
    """
    fail_here = opaque is None
    opq: list[str] = [] if opaque is None else opaque
    seen = _seen if _seen is not None else set()
    seen.add(model)

    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        if isinstance(field.alias, str):
            names.add(field.alias)
        names |= _validation_alias_names(field.validation_alias, opaque=opq)
        for nested in _models_in_annotation(field.annotation, opaque=opq):
            if nested not in seen:
                names |= _model_wire_names(nested, _seen=seen, opaque=opq)

    if fail_here and opq:
        pytest.fail(
            f"model walk hit undecomposable annotation(s) under {model.__qualname__}: {opq}"
        )
    return names


def _extraction_influencing_fields(
    model: type[BaseModel], *, substring_net: bool = False
) -> set[str]:
    """Fields on ``model`` that could influence extraction behavior.

    Exact-name net (default): intersects the model's full wire-name surface
    (field name + ``alias`` + ``validation_alias``, see ``_model_wire_names``
    — Argus P3, round 3) with the derived knob set — catches a field that
    names a REAL ``BrainGraphSettings`` ``extraction_*`` knob exactly, by
    ANY name a caller could bind it under. Used for the repo-wide sweep (T3),
    deliberately narrow so a future legitimate read-side provenance field
    (e.g. a hypothetical ``extraction_run_id`` on a VIEW) does not
    false-positive.

    Substring net (``substring_net=True``): additionally flags any wire name
    that merely CONTAINS ``"extraction"``. Used only for REQUEST models (T2),
    where any such field is either the forbidden thing itself or warrants a
    conscious guard revision — never a legitimate field.
    """
    knobs = _extraction_knob_fields()
    names = _model_wire_names(model)
    hit = names & knobs
    if substring_net:
        hit |= {n for n in names if "extraction" in n}
    return hit


def _sweep_fail_loud(name: str) -> None:
    """``pkgutil.walk_packages`` ``onerror`` hook (Argus P3, round 2): without
    this, a subpackage that fails to import while ``walk_packages`` is
    resolving its ``__path__`` is SILENTLY dropped from the sweep, along with
    every submodule beneath it — the opposite of the fail-loud discipline
    this section's own docstring claims. Raising here makes a broken
    discoverer red instead of quietly shrinking the swept surface.
    """
    pytest.fail(f"B-P0-1 sweep could not import {name!r} — discovery is broken, not just narrow")


def _wire_surface_modules() -> list[ModuleType]:
    """EVERY module in the ``ne.api`` and ``ne.contracts`` packages,
    discovered via ``pkgutil.walk_packages`` over each package's ``__path__``
    — not a hand-picked subset (Argus P1, round 1: a five-module literal
    list plus a routers-only sweep missed ``ne.api.graph_wire``, the module
    defining the graph-query/graph-paths request models). Walking the full
    tree means a new top-level wire module, a new router, or a new contracts
    submodule is swept in with zero test-file changes. ``onerror`` fails loud
    (Argus P3, round 2) rather than letting ``walk_packages`` silently drop an
    unimportable subpackage and everything beneath it.
    """
    modules: list[ModuleType] = [api_wire, contracts_types]
    seen: set[str] = {api_wire.__name__, contracts_types.__name__}
    for package in (ne.api, ne.contracts):
        for info in pkgutil.walk_packages(
            package.__path__, prefix=package.__name__ + ".", onerror=_sweep_fail_loud
        ):
            if info.name in seen:
                continue
            seen.add(info.name)
            modules.append(importlib.import_module(info.name))
    return modules


def _all_models() -> dict[str, type[BaseModel]]:
    """Every pydantic ``BaseModel`` subclass defined anywhere on the swept
    surface, keyed by FULLY QUALIFIED name (``module.ClassName``) so two
    modules defining a same-named class never shadow one another (Argus P3,
    round 1: the original dict keyed on the bare class name).
    """
    models: dict[str, type[BaseModel]] = {}
    for module in _wire_surface_modules():
        for name, model in _models_defined_in(module).items():
            models[f"{module.__name__}.{name}"] = model
    return models


def _request_models() -> dict[str, type[BaseModel]]:
    """Every ``*Request``-suffixed model discovered across the wire surface,
    keyed by fully qualified name (see ``_all_models``).
    """
    return {
        qualname: model
        for qualname, model in _all_models().items()
        if qualname.rsplit(".", 1)[-1].endswith("Request")
    }


def _api_routers() -> list[APIRouter]:
    """Every ``APIRouter`` instance ASSIGNED AT MODULE SCOPE anywhere on the
    SAME walked ``ne.api``/``ne.contracts`` surface as
    ``_wire_surface_modules()`` (Argus P1, round 2) — not a curated router
    list, so a new module-level router is swept in with zero test-file
    changes, same as the model sweep above.

    Honest residual role (Argus P1, round 3): this is a MODULE-ATTRIBUTE
    sweep — it is BLIND to a router constructed INSIDE a factory function
    (``build_scim_app``'s router, ``ne.api.scim.router``), because such a
    router is never bound to a module-level name. Its value is catching an
    extraction-naming route on a router that exists at module scope but is
    NOT (yet) mounted into ``create_app`` at all — a route that would never
    appear in the composed-app sweep below because it is never served. The
    composed sweep (``_walk_composed_routes``,
    ``test_bp01_composed_app_routes_*``) covers everything ACTUALLY served,
    including factory-built and sub-mounted routers; this sweep and the
    route-signature sweep built on it are kept, not deleted, for that
    narrower not-yet-mounted case.
    """
    routers: list[APIRouter] = []
    for module in _wire_surface_modules():
        for obj in vars(module).values():
            if isinstance(obj, APIRouter):
                routers.append(obj)
    return routers


def _route_endpoint_params() -> dict[str, set[str]]:
    """Every ``APIRoute`` endpoint's parameter NAMES, keyed by a
    human-readable ``"<endpoint module>:<methods> <path>"`` id (Argus P1,
    round 2 — the model-only sweep is blind to bare
    ``Annotated[..., Query(...)]``/``Path(...)``/``Header(...)`` endpoint
    parameters, which carry no pydantic model at all).

    Dependency-injected parameter names (``principal``, ``state``, ``body``,
    ...) are included unfiltered rather than parsed out of ``Annotated``
    metadata — they never collide with the extraction nets (no dependency is
    named ``extraction_*`` or contains ``"extraction"``), so filtering them
    out would add complexity without changing what the guard can catch.
    """
    params: dict[str, set[str]] = {}
    for api_router in _api_routers():
        for route in api_router.routes:
            if not isinstance(route, APIRoute):
                continue
            methods = ",".join(sorted(route.methods or ()))
            key = f"{route.endpoint.__module__}:{methods} {route.path}"
            params[key] = set(inspect.signature(route.endpoint).parameters)
    return params


# --- (h-bis) the COMPOSED-app route sweep (Argus P1, round 3) ----------------
#
# ``_api_routers()``/``_route_endpoint_params()`` above only ever see
# ``APIRouter`` objects assigned at MODULE scope (``for obj in
# vars(module).values(): if isinstance(obj, APIRouter)``). ``build_scim_app``
# (``ne.api.scim.router``) constructs its ``APIRouter`` INSIDE the factory —
# invisible to a module-attribute sweep even though ``create_app`` mounts it
# and FastAPI serves it. Argus proved this by transient mutation: adding
# ``extraction_mode: str = Query(default="autonomous")`` to ``_list_users``
# (GET ``/scim/v2/Users``) left the round-2 route-signature sweep's selection
# fully green (6 passed, 22 deselected) — a live, mounted, caller-facing
# route could name the forbidden knob with every guard passing.
#
# The fix asks a DIFFERENT question: not "what routers does the module
# namespace happen to expose", but "what routes does the ACTUALLY-COMPOSED
# application serve" — the same object FastAPI hands to an ASGI server. That
# reaches factory-built and sub-mounted routers BY CONSTRUCTION, because
# ``create_app`` must itself resolve them into ``Starlette(routes=[...])``
# for the app to work at all.


def _resolve_routed(app_obj: Any) -> Any | None:
    """Unwrap ASGI wrapper layers down to the nearest object carrying a
    ``routes`` table.

    ``create_app``'s ``/api`` ``Mount.app`` is an ``OidcAuthMiddleware``
    instance (``ne.identity.factory.build_identity``), not a router-bearing
    app — the middleware stores the wrapped inner FastAPI app on
    ``self._app`` (``ne.identity.middleware.py``). This walks BOTH the
    ``.app`` convention (Starlette/``BaseHTTPMiddleware``-style wrappers) and
    the ``._app`` convention (``OidcAuthMiddleware``'s own), so either shape
    of future middleware is still traversed. Bounded to 20 hops so a
    pathological wrapping cycle fails fast instead of looping forever;
    returns ``None`` if no routed object is ever reached (an opaque mount —
    the caller must treat that as a coverage gap, never silently drop it).
    """
    obj: Any = app_obj
    for _ in range(20):
        if hasattr(obj, "routes"):
            return obj
        nxt = getattr(obj, "app", None)
        if nxt is None:
            nxt = getattr(obj, "_app", None)
        if nxt is None:
            return None
        obj = nxt
    return None


def _dependant_wire_names(dependant: Any, *, opaque: list[str] | None = None) -> set[str]:
    """Every wire name FastAPI will bind a caller-supplied value to for
    ``dependant``, recursing into every ``Depends(...)`` it pulls in
    (Argus P1, round 4).

    The PREVIOUS implementation of ``_route_param_wire_names`` inspected only
    ``route.endpoint``'s own Python signature — blind to a parameter declared
    inside a ``Depends(...)`` callable, even though FastAPI still resolves it
    from the caller's request and binds it before the endpoint ever runs.
    Proven live and not hypothetical: ``build_scim_app`` gates its ENTIRE
    router with ``APIRouter(dependencies=[Depends(require_bearer)])``
    (``ne.api.scim.router``), whose ``_require_bearer`` (``ne.api.scim.auth``)
    declares an ``authorization`` header parameter — invisible to a
    signature-only sweep of any of the fifteen ``/scim/v2`` endpoints, and
    the exact shape an ``extraction_mode`` knob could hide inside next.

    Walks the PUBLIC ``fastapi.dependencies.models.Dependant`` shape that
    ``APIRoute.dependant`` already carries — ``.path_params``/
    ``.query_params``/``.header_params``/``.cookie_params``/``.body_params``
    plus a recursive descent over ``.dependencies`` — never the internal
    ``get_flat_dependant`` helper (the brief's deliberate boundary, so this
    stays robust across a FastAPI minor bump).

    For each ``ModelField`` both ``f.name`` (the Python parameter name) and
    ``f.field_info.alias`` are taken, when ``f.field_info`` is a
    ``fastapi.params.Param`` (``Query``/``Header``/``Cookie``/``Path``) OR a
    ``fastapi.params.Body`` (which also covers ``Form``/``File``, both
    ``Body`` subclasses — Argus P2, review after round 4: the PREVIOUS check
    took ``Param`` only, so a SCALAR body/form parameter's own ``alias=`` was
    invisible — ``mode: Annotated[str, Body(alias="extraction_mode",
    embed=True)]`` and ``mode: Annotated[str, Form(alias="extraction_mode")]``
    both wire-bound the caller's JSON/form key ``extraction_mode`` while every
    guard stayed green, the same smuggle shape round 3 closed for pydantic
    MODEL fields (``_model_wire_names``) but left open here, since a scalar
    body/form parameter has no model for that sweep to walk at all — pinned
    by ``test_bp01_composed_walker_detects_a_body_or_form_alias_smuggle``).
    FastAPI itself resolves ``field_info.alias`` to the real wire name by the
    time a ``Dependant`` is built — to the caller's explicit ``alias=`` when
    given, or to the parameter's own name (dash-converted for ``Header``)
    otherwise (``fastapi/dependencies/utils.py``: ``field_info.alias =
    field_info.alias or param_name``) — so this single read strictly
    dominates the previous ``get_type_hints``/``Annotated``-parsing approach:
    both the explicit-alias and the default-name cases are already resolved,
    with no annotation-parsing machinery of our own to keep in sync with
    FastAPI's.

    A ``Security(scheme)`` dependency — ``APIKeyQuery``/``APIKeyHeader``/
    ``APIKeyCookie`` — binds the caller's request to a wire name that lives
    ONLY on ``scheme.model.name``: FastAPI builds an EMPTY sub-``Dependant``
    for it (verified empirically — ``.query_params``/``.header_params`` are
    both ``[]`` for a ``Security(APIKeyQuery(name=...))`` dependency at any
    level), so the param-list walk above is blind to it (Argus P3, review
    after round 4). Closed by reading ``sub_dependant.call.model.name`` when
    ``sub_dependant.call`` is a ``fastapi.security.base.SecurityBase``
    instance — the same PUBLIC attribute FastAPI's own OpenAPI generator
    reads to describe the scheme. ``HTTPBearer``/``HTTPBasic`` schemes have
    no caller-NAMED wire key (they always bind FastAPI's own fixed
    ``Authorization`` header, already covered by the real ``/scim/v2``
    membership control's ``authorization`` assertion above) — a benign
    control (``test_bp01_composed_walker_detects_a_security_scheme_smuggle``)
    proves such a scheme contributes nothing spurious.

    Argus P2 (round 8) narrowed the FORMER boundary (1) below — a pydantic
    MODEL used as a parameter's own type contributing only the outer
    PARAMETER NAME, never the model's own fields — to the model's OWN
    (depth-0, unwrapped) field surface. Round 9 (CHANGE OF METHOD) REPLACES
    that depth-0 check with structural decomposition: every ModelField's
    ``field_info.annotation`` is run through ``_models_in_annotation``,
    which reduces ``Annotated`` wrappers, unions, and generic containers to
    their argument types and recurses to ANY nesting depth (cycle-safe via
    ``_model_wire_names``'s ``_seen`` set) — not merely the outer,
    unwrapped case. The property this now proves is structural, not a list
    of shapes someone thought to test: EVERY parameter/field annotation
    reduces to either "contains no ``BaseModel``" or "these ``BaseModel``s,
    recursively swept" — or it REDS via the ``opaque`` channel threaded
    through this function (see ``_models_in_annotation``'s docstring for the
    full decision table, and
    ``test_bp01_annotation_decomposer_reduces_wrappers_and_reds_on_exotics``
    for the proof, both directions). Proven live: a probe route with
    ``q: Annotated[_QModel, Query()]`` where ``_QModel`` declares a field
    named ``extraction_mode`` bound the caller's ``?extraction_mode=...``
    while the depth-0-only walk stayed silent — pinned by
    ``test_bp01_composed_walker_detects_a_query_model_field_smuggle``, whose
    probe model is defined in the TEST module itself (outside both
    ``ne.api``/``ne.contracts`` trees) to prove the closure lives HERE, not
    in the separate ``_all_models``/``_request_models`` sweep. A depth>=3,
    list+union+``Annotated``-wrapped nested smuggle is additionally pinned
    by ``test_bp01_composed_walker_detects_a_nested_model_smuggle``.

    Fail-loud contract (round 9): this function defaults to ``opaque=None``
    — "I am the top-level call" — in which case an undecomposable
    annotation found anywhere in the walk triggers ``pytest.fail`` before
    returning. A caller aggregating ``opaque`` across many routes
    (``_route_param_wire_names``, ``_effective_wire_names``,
    ``_walk_composed_routes``) passes its own list through instead, which
    suppresses the local ``pytest.fail`` and makes the caller responsible
    for asserting on it — the same pattern ``_model_wire_names`` uses.

    ONE honest, DOCUMENTED coverage boundary remains (Argus P3, review
    after round 4; re-confirmed round 8 — stated rather than silently
    assumed): a dependency that reads a value directly off the raw
    ``Request`` object (e.g. ``request.query_params.get("extraction_mode")``
    inside a ``Depends(...)`` callable taking ``request: Request``) is
    invisible to ANY static walk: FastAPI never resolves such an access into
    a ``ModelField`` on any ``Dependant`` — there is nothing here to read. This
    is NOT closable by widening this function; it is named as a genuine
    boundary rather than implied to be covered. No such dependency exists on
    the live composed app today (every caller input is bound through a
    declared ``Query``/``Header``/``Cookie``/``Path``/``Body``/``Security``
    parameter, which is why this guard is meaningful at all).

    A SECOND boundary, decided rather than swept (round 9): a ``Depends(...)``
    callable's own RETURN annotation is NOT inspected. FastAPI performs no
    caller-input binding on a dependency's return value — it is server-side
    outbound data (the callable's result, handed to the endpoint), the same
    category as ``serialization_alias`` on a model (see ``_model_wire_names``'s
    docstring). Only a dependency's PARAMETERS (already swept, recursively,
    above) can bind caller input. Pinned by
    ``test_bp01_composed_walker_detects_a_wrapped_depends_model_smuggle``,
    whose smuggle lives in the dependency's PARAMETER, not its return type.

    A THIRD, TESTING-ONLY surface is out of scope by construction (Argus
    P3, review round 8): ``app.dependency_overrides[dep] = other_dep``
    substitutes a dependency's CALLABLE at REQUEST time, and FastAPI solves
    the OVERRIDE's own declared parameters instead of the original
    dependency's — a binding surface no static ``.dependant``/
    ``RouteContext`` walk can see (proven empirically:
    ``app.dependency_overrides[_benign] = _mode_dep`` made a route silently
    bind ``extraction_mode`` while every static walk above reported
    nothing). ``dependency_overrides`` is a FastAPI TESTING hook, not a
    production composition mechanism — nothing in the repo sets it outside
    test harnesses — so it is named here rather than swept.
    """
    fail_here = opaque is None
    opq: list[str] = [] if opaque is None else opaque

    names: set[str] = set()
    for f in (
        list(dependant.path_params)
        + list(dependant.query_params)
        + list(dependant.header_params)
        + list(dependant.cookie_params)
        + list(dependant.body_params)
    ):
        names.add(f.name)
        if isinstance(f.field_info, (fastapi_params.Param, fastapi_params.Body)) and isinstance(
            f.field_info.alias, str
        ):
            names.add(f.field_info.alias)
        # Round 9 (CHANGE OF METHOD): the depth-0 ``isinstance(annotation,
        # type) and issubclass(annotation, BaseModel)`` check is replaced by
        # ``_models_in_annotation``'s structural decomposition — see this
        # function's docstring for the full property this now proves, and
        # ``_models_in_annotation``'s docstring for the decision table. Every
        # ``BaseModel`` the decomposer finds (at any depth, through any
        # wrapper) is swept via ``_model_wire_names``; anything the
        # decomposer cannot reduce lands in ``opq`` instead of being
        # silently skipped.
        annotation = getattr(f.field_info, "annotation", None)
        for nested in _models_in_annotation(annotation, opaque=opq):
            names |= _model_wire_names(nested, opaque=opq)
    for sub_dependant in dependant.dependencies:
        if isinstance(sub_dependant.call, SecurityBase):
            scheme_name = getattr(getattr(sub_dependant.call, "model", None), "name", None)
            if isinstance(scheme_name, str):
                names.add(scheme_name)
        names |= _dependant_wire_names(sub_dependant, opaque=opq)

    if fail_here and opq:
        pytest.fail(f"dependant walk hit undecomposable annotation(s): {opq}")
    return names


def _route_param_wire_names(
    route: APIRoute | APIWebSocketRoute, *, opaque: list[str] | None = None
) -> set[str]:
    """Every wire name ``route``'s OWN ``.dependant`` binds — thin wrapper
    over ``_dependant_wire_names(route.dependant, opaque=opaque)`` (Argus P1, round 4:
    walking the resolved ``Dependant`` reaches every ``Depends(...)``
    dependency's own declared parameters, not just the endpoint's own
    signature; see that function's docstring for the full rationale and the
    live ``authorization``/SCIM-router proof). ``route.dependant`` is always
    populated for a constructed ``APIRoute`` (a non-``Optional`` attribute on
    FastAPI's own ``_APIRouteLike`` protocol), so — unlike the previous
    ``get_type_hints``-based implementation — resolving ``route.dependant``
    itself cannot fail. Round 9: an ANNOTATION reached through it can still
    be undecomposable (see ``_models_in_annotation``'s docstring) — that
    fails loud via the threaded ``opaque`` list, not via a resolution
    exception here.

    Argus P2 (round 6): ``route`` also accepts ``APIWebSocketRoute`` —
    ``.dependant`` is likewise a non-``Optional`` attribute built in
    ``__init__`` on THAT class, and it binds declared ``Query``/``Header``/
    ``Cookie``/``Path``/``Depends`` parameters for the websocket handshake
    exactly like ``APIRoute`` does (a websocket route has no request body, so
    ``.body_params`` is simply always empty for it — the walk needs no
    per-class branching at all). The identical ``_dependant_wire_names`` call
    therefore applies unchanged to both classes.

    NAMED BOUNDARY (Argus P1, round 7): ``route.dependant`` here is always
    the route's OWN construction-time dependant — it reflects dependencies
    that were part of ``route.dependencies`` AT THE MOMENT the route object
    was built (e.g. everything passed to ``APIRouter(dependencies=[...])``
    at router-construction time, which FastAPI bakes into each added
    route's own ``.dependant`` immediately — this is what the round-4 fix
    above already reaches). It is NOT the EFFECTIVE dependant FastAPI
    actually resolves at request time once this route is composed into a
    larger app: ``include_router(router, dependencies=[...])`` (dependencies
    given to the INCLUDE call, not the router constructor) and
    ``FastAPI(dependencies=[...])`` (app-level) are both merged in LATER,
    by ``fastapi.routing._EffectiveRouteContext``, and never touch this
    route object's own ``.dependant`` at all. Reaching those two shapes
    requires walking the ``RouteContext`` produced by
    ``iter_route_contexts`` instead — see ``_effective_wire_names`` below,
    which ``_walk_composed_routes`` unions in on top of this function's
    result. A caller that calls THIS function directly on a bare route
    object (as several tests below do, deliberately, to isolate router- and
    dependency-level smuggles from the composed-app machinery) is
    correspondingly blind to include-level/app-level dependencies by
    construction — not a bug in those tests, just the boundary of what a
    bare route object can tell you before it is ever composed.
    """
    return _dependant_wire_names(route.dependant, opaque=opaque)


def _effective_wire_names(ctx: Any, *, opaque: list[str] | None = None) -> set[str]:
    """Every wire name bound ONLY at composition time — via
    ``include_router(router, dependencies=[...])`` or ``FastAPI(
    dependencies=[...])`` — for the route ``ctx`` (a
    ``fastapi.routing.RouteContext`` yielded by ``iter_route_contexts``)
    represents (Argus P1, round 7).

    THE GAP THIS CLOSES: ``_walk_composed_routes`` (below) fed ``ctx.
    original_route`` into ``_route_param_wire_names``, which reads only that
    route object's OWN ``.dependant`` (see that function's docstring for the
    now-named boundary). ``RouteContext.__getattr__`` (``fastapi/
    routing.py``) proxies attribute access to ``ctx._effective_route`` — an
    ``_EffectiveRouteContext`` when the route came through an
    ``_IncludedRouter`` (i.e. it went through ``include_router`` at least
    once), or the bare original route otherwise. For an ``APIRoute``,
    ``_EffectiveRouteContext.from_api_route`` builds ``context.dependant``
    itself from ``dependencies=[*include_context.dependencies, *route.
    dependencies]`` (``fastapi/routing.py``, ``_EffectiveRouteContext.
    from_api_route``) — the COMBINED include-level-plus-own dependency list
    — so ``ctx.dependant`` (reached only through the ``RouteContext``, never
    through the original route object) is the surface that was missing.

    PROVEN LIVE, TWO SHAPES, BOTH MADE-OBSERVED-REVERTED ON THE REAL APP
    (Argus round-7 review; re-run and recorded verbatim in this unit's
    round-7 commit message): (1) ``ne/api/app.py``'s
    ``inner.include_router(graph.router)`` mutated to ``inner.
    include_router(graph.router, dependencies=[Depends(_mode_dep)])`` binds
    ``extraction_mode`` on every ``/api/v1/graph/*`` route; (2) ``inner =
    FastAPI()`` mutated to ``inner = FastAPI(dependencies=[Depends(
    _mode_dep)])`` binds it on every ``/api/v1/*`` route. Both left ``-k
    bp01`` fully green before this fix — 16 passed, 22 deselected.

    THE ASYMMETRY BETWEEN THE TWO API ROUTE SHAPES (verified empirically,
    fastapi 0.139.0 — this is exactly the kind of version-coupled mechanics
    the ``iter_route_contexts`` version-coupling note below warns a future
    FastAPI-bump unit to re-verify):

    - ``APIRoute``: the effective ``_EffectiveRouteContext`` built by
      ``from_api_route`` populates ``context.dependant`` directly (via
      ``_populate_api_route_state``) with the COMBINED dependency list, so
      ``ctx.dependant`` alone carries the effective surface.
    - ``APIWebSocketRoute``: ``_build_effective_context`` does NOT populate
      ``_EffectiveRouteContext.dependant`` at all for this class (it stays
      the dataclass default, ``None``) — instead it constructs a BRAND NEW
      ``APIWebSocketRoute`` object with ``dependencies=[*include_context.
      dependencies, *route.dependencies]`` and stores IT as ``context.
      starlette_route``. That new object's own ``.dependant`` (built in its
      ``__init__``, same as any ``APIWebSocketRoute``) is where the
      effective surface actually lives — the identical object the
      ``APIWebSocketRoute`` branch in ``_walk_composed_routes`` already
      resolves (as ``resolved_ws``) for its PATH; this function re-derives
      it independently via ``getattr(ctx, "starlette_route", None)`` so it
      does not depend on that branch's local variable.

    For a route that never passed through an ``_IncludedRouter`` at all
    (registered directly on the top-level ``FastAPI()``/``APIRouter()``
    with no intervening ``include_router`` call), ``RouteContext.
    original_route`` IS ``RouteContext.route`` with no ``_EffectiveRouteContext``
    wrapper — ``ctx.dependant`` then resolves straight through to
    ``original_route.dependant``, identical to what ``_route_param_wire_
    names`` already computed; unioning it in is a no-op, not a
    double-count risk (this is a ``set`` union of NAMES, not a count).

    Union, never replace: this can only WIDEN the forbidden-name surface
    ``_walk_composed_routes`` reports, never narrow it below what
    ``_route_param_wire_names`` alone already found.
    """
    names: set[str] = set()
    eff_dependant = getattr(ctx, "dependant", None)
    if eff_dependant is not None:
        names |= _dependant_wire_names(eff_dependant, opaque=opaque)
    resolved = getattr(ctx, "starlette_route", None)
    resolved_dependant = getattr(resolved, "dependant", None)
    if resolved_dependant is not None:
        names |= _dependant_wire_names(resolved_dependant, opaque=opaque)
    return names


#: Single-sourced floor for the number of routes
#: ``test_bp01_composed_app_routes_cannot_name_an_extraction_knob`` discovers
#: on the real composed app (P3-1, round 9: this number previously appeared
#: three times — ``42`` here in a discovery-narrative aside, ``51`` in the
#: round-6 VERSION COUPLING paragraph, ``51`` in the actual assertion — with
#: the first stale relative to the other two. Observed 56 composed routes at
#: fix time (2026-07-29); floored to observed-5, matching the discipline the
#: OTHER floors in this file already use (see
#: ``test_bp01_request_models_cannot_name_an_extraction_knob``'s "observed
#: minus 2" floor for the same reasoning): wide enough that an unrelated
#: route addition/removal elsewhere in the app does not red this guard for a
#: reason that has nothing to do with B-P0-1, tight enough that the
#: ``iter_route_contexts``-goes-silent failure mode this floor exists to
#: catch (see the discovery-during-implementation paragraph below) still
#: reds loudly.
_COMPOSED_ROUTE_FLOOR = 51


def _walk_composed_routes(app_obj: Any) -> tuple[dict[str, set[str]], list[str]]:
    """Walk the ACTUALLY-COMPOSED application's mounted route table.

    Unlike ``_api_routers()``/``_route_endpoint_params()`` (a module-
    attribute sweep, blind to factory-built routers by construction), this
    asks the SAME object FastAPI serves, so ``Mount``-nested and
    middleware-wrapped routers — ``/scim/v2`` and ``/cdc/v1`` (each built
    INSIDE a factory function) and the OIDC-wrapped ``/api`` tree — are all
    reached BY CONSTRUCTION, not by enumerating a curated list.

    DISCOVERY DURING IMPLEMENTATION (not anticipated by the original plan):
    fastapi 0.139.0 (the version locked at write time) changed
    ``include_router`` to build a LAZY ``_IncludedRouter`` placeholder in the
    including app's ``.routes`` instead of eagerly flattening the included
    router's ``APIRoute`` objects into it — so a naive ``isinstance(route,
    APIRoute)`` walk of ``app.routes`` finds almost NOTHING for any app built
    the ``FastAPI(); app.include_router(...)`` way (every mounted sub-app
    here: ``inner``, ``scim_app``, ``cdc_app``). Confirmed empirically before
    writing this fix (an unguarded first draft of this walker passed
    ``opaque == []`` trivially yet found zero ``/scim/v2/Users`` routes — the
    ``_COMPOSED_ROUTE_FLOOR`` floor below is what would have caught it in CI).
    Each ``routes`` list is therefore run through
    ``fastapi.routing.iter_route_contexts`` first — a public (no leading
    underscore), framework-maintained member of ``fastapi.routing`` (the SAME
    stable module ``APIRoute`` itself comes from; it is what FastAPI's own
    OpenAPI-schema generator uses to flatten this exact structure) — which
    yields one ``RouteContext`` per EFFECTIVE route, correctly resolving any
    ``_IncludedRouter`` nesting (arbitrarily deep) and any router-level
    ``prefix=`` into ``RouteContext.path``. ``Mount`` objects pass through
    ``iter_route_contexts`` unchanged (it does not touch ASGI-level mounting,
    only FastAPI's own router-inclusion), so the explicit ``Mount`` descent
    below is still required and unaffected.

    NAMED, DOCUMENTED ENUMERATION-SOURCE BOUNDARY (Argus P2, round 8):
    ``iter_route_contexts`` walks each router's ``.routes`` list ONLY.
    FastAPI 0.139 ALSO introduced ``APIRouter.frontend(...)`` (serving a
    static frontend build as low-priority routes), which registers onto a
    SEPARATE ``_low_priority_routes`` list, dispatched only via
    ``Router.app()``'s own ``_iter_low_priority_routes()``/
    ``_match_low_priority()`` machinery after every normal route fails to
    match — ``iter_route_contexts`` never touches it, so this walker never
    sees it, and a router-level ``Depends(...)`` DOES get baked into a
    frontend route group's own ``.dependant`` (proven empirically), making
    this a genuine — if currently unexercised — blind spot. No live
    exposure exists: ``.frontend(`` has zero call sites anywhere in
    ``ne.api``/``ne.contracts`` today (the console SPA uses ``Mount`` +
    ``StaticFiles`` instead, handled below). Deliberately NOT swept:
    reaching it needs ``APIRouter._low_priority_routes`` and
    ``_IncludedRouter.effective_low_priority_routes()`` — both underscore-
    prefixed, undocumented FastAPI internals, unlike ``iter_route_
    contexts`` itself (see the VERSION COUPLING paragraph below for why
    this walker deliberately stays off private internals) — so it is named
    as the FOURTH honest boundary here and in
    ``test_bp01_composed_app_routes_cannot_name_an_extraction_knob``'s
    boundary list, and PROVEN (not just claimed) by
    ``test_bp01_composed_walker_does_not_see_frontend_low_priority_routes``.
    If a future unit ever calls ``.frontend(...)`` anywhere in the composed
    app, that PROOF test does not fail (it targets a synthetic app) — the
    grep-verified "no live exposure" claim above must be re-checked by hand
    at that time.

    Returns ``(params, opaque)``. ``params`` maps every discovered route to
    its wire-name surface (see ``_route_param_wire_names`` and, for the
    composition-time surface, ``_effective_wire_names``). The walk
    recognizes exactly four route shapes and does NOT overclaim coverage
    beyond what each branch actually inspects (Argus P2, round 6 — see
    below):

    - ``APIRoute`` — swept via ``_route_param_wire_names``/
      ``_dependant_wire_names`` (query/header/cookie/path/body params plus
      recursive ``Depends``/``Security`` on the route's OWN
      construction-time dependant, with any model-typed parameter/field
      decomposed STRUCTURALLY at any nesting depth through any wrapper —
      round 9, ``_models_in_annotation``) UNIONED with
      ``_effective_wire_names`` (query/header/cookie/path/``Depends``/
      ``Security`` params contributed ONLY by ``include_router(...,
      dependencies=[...])`` or ``FastAPI(dependencies=[...])`` — Argus P1,
      round 7, see that function's docstring — likewise decomposed
      structurally).
    - ``APIWebSocketRoute`` — swept IDENTICALLY (same
      ``_route_param_wire_names`` call plus the same ``_effective_wire_
      names`` union, including the round-9 structural model decomposition;
      see both functions' docstrings for why the identical walk is correct
      for a websocket route and for the verified asymmetry in where the two
      classes' effective dependant actually lives).
    - A plain, EXACT-TYPE Starlette ``Route``/``WebSocketRoute`` (e.g.
      ``/healthz``, ``/config.json``) — NAMED, deliberate skip: the raw
      endpoint receives only the connection object and Starlette performs no
      declared-parameter binding for these exact classes, so there is
      nothing to sweep. This reasoning is scoped to the exact classes only —
      it does NOT extend to a subclass, and it never extended to
      ``APIWebSocketRoute`` (a FastAPI class, not a plain Starlette one).
    - Anything else — including a ``Mount`` whose target could not be
      resolved to a routed object (see ``_resolve_routed``) and is not a
      recognized non-routed ``StaticFiles`` mount, any route class not in
      the three bullets above (e.g. a ``Route``/``WebSocketRoute`` SUBCLASS
      that might add its own binding), AND (round 9) any parameter/field
      annotation the decomposer cannot reduce to either "contains no
      ``BaseModel``" or "these ``BaseModel``s" (see
      ``_models_in_annotation``'s docstring) — lands in ``opaque``, naming
      the route and/or the class/shape. The caller must assert ``opaque ==
      []``, so a future unwalkable route, unrecognized route class, or
      undecomposable annotation REDS the guard instead of being silently
      declared safe.

    Argus P2 (round 6): the PREVIOUS version of this walker silently skipped
    ``APIWebSocketRoute`` in an unconditional ``else`` branch, and that
    branch's comment affirmatively declared the skip safe using reasoning
    that is true for a plain Starlette ``Route``/``WebSocketRoute`` (raw
    endpoint, no declared binding) but FALSE for FastAPI's
    ``APIWebSocketRoute``, which binds declared ``Query``/``Header``/
    ``Cookie``/``Path``/``Depends`` parameters for the handshake exactly
    like an ``APIRoute`` does. Proven live by transient mutation: a
    ``@router.websocket("/v1/graph/stream")`` route appended to
    ``ne.api.routers.graph`` (module-level ``router``, unconditionally
    included by ``create_app``) binding ``extraction_mode`` via ``Query``
    left ``-k bp01`` fully green — 14 passed, 22 deselected — with every
    guard, including this walker's own ``opaque == []`` assertion, silent.
    Fixed by sweeping ``APIWebSocketRoute`` with the identical extraction
    used for ``APIRoute`` and by converting the silent ``else`` into a NAMED
    exact-type skip plus a fail-loud ``opaque`` branch for anything
    unrecognized (both above), pinned by
    ``test_bp01_composed_walker_detects_a_websocket_smuggle`` and
    ``test_bp01_composed_walker_flags_an_unrecognized_route_class_as_opaque``.
    Re-run of the exact mutation after the fix, and of the reverted-tree
    control, is recorded verbatim in this unit's commit message.

    Argus P1 (round 7): the round-6 fix above still fed ``ctx.
    original_route`` into ``_route_param_wire_names``, which reads only
    that route object's OWN construction-time ``.dependant`` — blind to a
    dependency attached at COMPOSITION time via ``include_router(router,
    dependencies=[...])`` (the include call's own ``dependencies=``, not
    the router constructor's) or ``FastAPI(dependencies=[...])`` (app-
    level). Both are merged in later by ``fastapi.routing.
    _EffectiveRouteContext`` and never touch the original route object at
    all. Proven live on the real app by TWO transient mutations (both
    made, observed RED, reverted — transcripts in this unit's round-7
    commit message): (1) ``inner.include_router(graph.router)`` mutated to
    add ``dependencies=[Depends(_mode_dep)]`` bound ``extraction_mode`` on
    every ``/api/v1/graph/*`` route; (2) ``inner = FastAPI()`` mutated to
    ``FastAPI(dependencies=[Depends(_mode_dep)])`` bound it on every
    ``/api/v1/*`` route — both left ``-k bp01`` fully green (16 passed, 22
    deselected) with this walker's own ``opaque == []`` assertion silent.
    Fixed by unioning in ``_effective_wire_names(ctx)`` (see its docstring
    for the full mechanism and the verified ``APIRoute``-vs-
    ``APIWebSocketRoute`` asymmetry) in both the ``APIRoute`` and
    ``APIWebSocketRoute`` branches below, pinned by
    ``test_bp01_composed_walker_detects_an_include_level_dependency_
    smuggle``, ``test_bp01_composed_walker_detects_a_websocket_include_
    level_dependency_smuggle``, and ``test_bp01_composed_walker_detects_
    an_app_level_dependency_smuggle``. The round-4 dependency-smuggle
    control (``test_bp01_composed_walker_detects_a_dependency_injected_
    smuggle``) did NOT catch this: it calls ``_route_param_wire_names``
    directly on a bare ``APIRouter``'s own routes (``APIRouter(
    dependencies=[...])`` — dependencies baked into each route's OWN
    ``.dependant`` at router-CONSTRUCTION time, which the round-4 fix
    already reaches) and never calls ``_walk_composed_routes`` at all, so
    it never touches the ``iter_route_contexts``/``RouteContext.dependant``
    path where this gap lived — same shape as every prior round's escape:
    a control that proves ONE dependency-attachment shape safe, read as
    proving the class of shapes safe.

    VERSION COUPLING (Argus P3, round 6): this walker depends on
    ``fastapi.routing.iter_route_contexts``, which is underscore-free and
    framework-maintained but NOT documented public API, and exists only as
    of fastapi 0.139's lazy-``include_router`` refactor described below
    (this repo locks ``fastapi==0.139.0``; PyPI is already at ``0.141.1``
    as of this round). A future bump that REMOVES the name fails LOUD at
    collection (an ``ImportError``, acceptable). A future bump that KEEPS
    the name but changes its semantics — e.g. what ``RouteContext.path``/
    ``.methods``/``.starlette_route``/``.dependant`` are populated with for
    an ``APIRoute`` or a websocket route (round 6's fix depends on the
    first three; round 7's ``_effective_wire_names`` depends on
    ``.dependant`` ALSO carrying the composition-time-merged surface, per
    that function's docstring) — would be caught only by the
    ``_COMPOSED_ROUTE_FLOOR`` floor below, which is a much blunter instrument
    than a targeted control. A future FastAPI-bump unit MUST re-run this
    unit's transient-mutation proofs (the round-6 AND round-7 commit
    messages) rather than trusting a green CI run alone.

    Two walker-correctness fixes (Argus review after round 4, walker nits):

    (a) A ``Mount``'s prefix is now read off ``ctx.starlette_route.path``
    when available, falling back to ``original.path`` (via ``getattr``'s
    default) when it is not. ``fastapi.routing.RouteContext.path`` is only
    populated for an ``APIRoute``-shaped effective route — for a
    ``Mount``-shaped one it is the empty string — even though a ``Mount``
    NESTED inside an ``_IncludedRouter`` under a router-level ``prefix=`` DOES
    get a correctly-reprefixed copy on ``RouteContext.starlette_route``
    (verified empirically). The un-prefixed key/opaque-message this produced
    was a labeling bug, not a coverage gap — the walker still recursed into
    the mounted app's own routes either way — but it is fixed to be correct
    BY CONSTRUCTION rather than by today's topology (every mount in
    ``create_app`` happens to be top-level today), pinned by
    ``test_bp01_composed_walker_resolves_a_router_prefixed_mount_path``. A
    top-level ``Mount`` (no enclosing ``_IncludedRouter``) has no
    ``RouteContext.starlette_route`` at all — accessing it raises
    ``AttributeError``, caught by ``getattr``'s default, falling back to
    ``original.path``, which is already correct for that case.

    (b) A ``Mount`` whose target is an (undiscoverable-by-``_resolve_routed``)
    ``starlette.staticfiles.StaticFiles`` instance (or a subclass, e.g.
    ``ne.api.app._SpaStaticFiles`` — the console SPA mount ``create_app``
    appends when ``ApiSettings.serve_console_dist`` is set) is now
    RECOGNIZED and excluded from ``opaque`` rather than flagged: it
    genuinely serves files off disk, not parameters, so it is not a blind
    spot — conflating it with a real unwalkable mount would make a future
    unit's decision to exercise ``serve_console_dist`` in
    ``test_bp01_composed_app_routes_cannot_name_an_extraction_knob`` (left
    OFF there today) red for the wrong reason. Pinned by
    ``test_bp01_composed_walker_does_not_flag_staticfiles_as_opaque``.
    """
    root = _resolve_routed(app_obj)
    assert root is not None, "could not resolve a routes table on the composed app itself"

    params: dict[str, set[str]] = {}
    opaque: list[str] = []

    def _visit(routes: Sequence[Any], prefix: str) -> None:
        for ctx in iter_route_contexts(routes):
            original = ctx.original_route
            if isinstance(original, APIRoute):
                methods = ",".join(sorted((ctx.methods or original.methods) or ()))
                path = ctx.path if ctx.path is not None else original.path
                key = f"{original.endpoint.__module__}:{methods} {prefix}{path}"
                # Argus P1 (round 7): union in whatever was bound ONLY at
                # composition time (``include_router(..., dependencies=
                # [...])`` / ``FastAPI(dependencies=[...])``) — see
                # ``_effective_wire_names``'s docstring. ``_route_param_
                # wire_names(original)`` alone reads only ``original``'s OWN
                # construction-time ``.dependant`` and is blind to it.
                #
                # Argus P3 (round 8): UNION onto any existing entry rather
                # than overwrite — two distinct discovered routes sharing
                # module+methods+path (a real, if unusual, possibility)
                # must never silently drop one's wire-name surface under
                # the other's key.
                #
                # Round 9 (CHANGE OF METHOD): an undecomposable parameter/
                # field annotation anywhere under THIS route's dependant —
                # own or effective — lands in ``ann_opaque``, keyed to the
                # route below, so it REDS the same asserted-empty ``opaque``
                # channel an unrecognized route class already reds, instead
                # of being silently dropped as "no models found".
                ann_opaque: list[str] = []
                wire = _route_param_wire_names(original, opaque=ann_opaque) | _effective_wire_names(
                    ctx, opaque=ann_opaque
                )
                params[key] = params.get(key, set()) | wire
                opaque.extend(f"{key}: {entry}" for entry in ann_opaque)
            elif isinstance(original, APIWebSocketRoute):
                # Argus P2 (round 6): a websocket route binds declared
                # parameters exactly like an APIRoute — see
                # ``_route_param_wire_names``'s docstring. Precision note
                # (Argus P3, round 6 review): the two empirical claims below
                # are scoped to the INCLUDED case only (verified
                # empirically) — a TOP-LEVEL websocket route (no enclosing
                # ``_IncludedRouter``) has ``ctx.methods is None`` (not an
                # empty set) and ``ctx.path`` already POPULATED with the
                # correct path (not the empty string); the code below
                # handles both via the literal ``WS`` token and the
                # ``getattr``-with-fallback pattern, so this is a prose
                # precision fix only, not a behavior change.
                #
                # For an INCLUDED websocket route: ``ctx.methods`` is an
                # empty set (there is no HTTP verb), so the literal ``WS``
                # token stands in for it in the key, matching the
                # ``{methods} {path}`` shape used above. ``RouteContext.
                # path`` is the EMPTY STRING for an ``APIWebSocketRoute``-
                # shaped effective route (only populated for ``APIRoute``-
                # shaped ones), while the correctly-reprefixed path lives on
                # ``ctx.starlette_route`` — the same resolution pattern as
                # the round-5 ``Mount`` fix above. A top-level ws route has
                # no ``RouteContext.starlette_route`` at all — ``getattr``'s
                # default falls back to ``original.path``, which is already
                # correct for that (unprefixed, top-level) case.
                resolved_ws = getattr(ctx, "starlette_route", None)
                path = resolved_ws.path if resolved_ws is not None else original.path
                key = f"{original.endpoint.__module__}:WS {prefix}{path}"
                # Argus P1 (round 7): same union as the APIRoute branch
                # above — for an INCLUDED websocket route the effective
                # dependant lives on ``resolved_ws.dependant`` (NOT on
                # ``ctx.dependant``, which ``_build_effective_context``
                # leaves ``None`` for this class — see
                # ``_effective_wire_names``'s docstring for the verified
                # asymmetry), which is exactly what ``_effective_wire_names``
                # resolves via ``ctx.starlette_route`` independently of this
                # branch's local ``resolved_ws``.
                #
                # Argus P3 (round 8): union onto any existing entry — see
                # the identical comment in the ``APIRoute`` branch above.
                #
                # Round 9 (CHANGE OF METHOD): same undecomposable-annotation
                # fail-loud threading as the ``APIRoute`` branch above.
                ann_opaque: list[str] = []
                wire = _route_param_wire_names(original, opaque=ann_opaque) | _effective_wire_names(
                    ctx, opaque=ann_opaque
                )
                params[key] = params.get(key, set()) | wire
                opaque.extend(f"{key}: {entry}" for entry in ann_opaque)
            elif isinstance(original, Mount):
                resolved_mount = getattr(ctx, "starlette_route", None)
                path = resolved_mount.path if resolved_mount is not None else original.path
                target = _resolve_routed(original.app)
                if target is not None:
                    _visit(target.routes, prefix + path)
                elif isinstance(original.app, StaticFiles):
                    # Legitimate-but-opaque: serves files off disk, not
                    # parameters — NOT a blind spot, so NOT added to
                    # ``opaque`` (Argus P3, review after round 4).
                    continue
                else:
                    opaque.append(f"{prefix}{path} ({type(original.app).__name__})")
            elif type(original) in (Route, WebSocketRoute):
                # NAMED, exact-type skip — plain Starlette routes (e.g.
                # ``/healthz``, ``/config.json``): the raw endpoint receives
                # only the connection object (``Request``/``WebSocket``) and
                # Starlette performs NO declared-parameter binding for these
                # EXACT classes, so there is no wire-name surface to sweep.
                # This reasoning does NOT extend to ``APIWebSocketRoute``
                # (handled in its own branch above, which DOES bind declared
                # parameters) — Argus P2 (round 6): the previous version of
                # this comment asserted the plain-Route reasoning covered
                # "a plain Starlette Route/WebSocketRoute" without excluding
                # FastAPI's ``APIWebSocketRoute``, i.e. it affirmatively
                # declared a surface safe that the walker never inspected.
                # ``type(original) in (...)`` (not ``isinstance``) is
                # deliberate: a SUBCLASS of ``Route``/``WebSocketRoute`` is
                # NOT assumed to share this boundary — it falls through to
                # the ``opaque`` branch below instead of being silently
                # waved through.
                continue
            else:
                # Fail-loud, not silently-safe: any route class the walker
                # does not specifically recognize — including a subclass of
                # ``Route``/``WebSocketRoute`` that might add its own
                # parameter binding — REDS the guard via ``opaque`` instead
                # of being assumed safe. Naming the class in the message
                # makes triage immediate (Argus P2, round 6: this branch is
                # the direct fix for a docstring asserting safety over a
                # surface the code never inspected).
                opaque.append(
                    f"{getattr(original, 'path', repr(original))} "
                    f"(unrecognized route class {type(original).__name__})"
                )

    _visit(root.routes, "")
    return params, opaque


#: A syntactically-valid dummy CDC webhook secret (Standard-Webhooks
#: ``whsec_``-prefixed base64) — mirrors ``test_cdc_sink_api.py``'s own
#: fixture shape. Not a real credential (C12).
_CDC_SECRET = "whsec_" + base64.b64encode(b"cdc-webhook-key-32-bytes-long!!!!").decode()

#: A minimal-but-valid CDC table map (mirrors ``test_cdc_sink_api.py:18-30``)
#: — enough for ``create_app`` to compose the ``/cdc/v1`` mount.
_CDC_TABLES = json.dumps(
    {
        "source_name": "debezium",
        "tables": {
            "public.customers": {
                "key_columns": ["id"],
                "content_columns": ["name", "email"],
                "acl": {"principal_column": "acl_group"},
            }
        },
    }
)


def test_bp01_forbidden_set_is_derived_and_nonvacuous() -> None:
    """The reflection-derived forbidden set is not accidentally empty or
    missing a known knob — guards the guard against a silent derivation
    break (e.g. a future rename of ``BrainGraphSettings`` fields away from
    the ``extraction_`` prefix).
    """
    expected = {
        "extraction_enabled",
        "extraction_mode",
        "extraction_risk_accepted",
        "extraction_sources",
        "extraction_min_confidence",
        "extraction_auto_link_threshold",
    }
    assert expected <= _extraction_knob_fields()


def test_bp01_request_models_cannot_name_an_extraction_knob() -> None:
    """No ``*Request`` model in ``ne.api`` or ``ne.contracts`` (the FULL
    package tree, per the Argus P1 fix — not a curated module list) names any
    extraction field (substring net — wider than T3's exact net, since a
    request model has no legitimate reason to carry anything resembling an
    extraction knob), and every ``ne.api.wire`` request model is
    ``extra="forbid"`` (wire.py's stated §4-4 policy — a smuggled key is a
    LOUD 422, the ``test_no_request_level_strictness_override`` precedent).

    The ``extra="forbid"`` assertion is scoped to ``ne.api.wire`` request
    models only: ``routers.flowise`` and ``graph_wire`` are not under
    wire.py's stated policy (asserting it there would red the suite on code
    this unit may not touch) — the substring-net field-name check still
    covers them regardless. ``scim.models.PatchRequest`` independently
    declares ``extra="forbid"`` for its own RFC 7644 reasons but is likewise
    not asserted here as a wire.py-policy matter.
    """
    models = _request_models()

    # Discovery controls: known request models from each swept surface must
    # actually be found, or the discoverer itself is broken — a silently
    # empty discovery is the classic vacuous-guard shape. Includes
    # ``ne.api.graph_wire`` (Argus P1: the module the round-1 curated list
    # missed) and one ``ne.contracts`` request model, proving BOTH walked
    # packages actually feed the discoverer.
    for expected_qualname in (
        "ne.api.wire.ChatMessageRequest",
        "ne.api.wire.CreateSessionRequest",
        "ne.api.wire.BrainRetrieveRequest",
        "ne.api.wire.VerifiedInvokeRequest",
        "ne.api.routers.flowise.FlowiseExecuteRequest",
        "ne.api.scim.models.PatchRequest",
        "ne.api.graph_wire.GraphQueryRequest",
        "ne.api.graph_wire.GraphPathsRequest",
        "ne.contracts.models.CompletionRequest",
    ):
        assert expected_qualname in models, f"discovery missed {expected_qualname}"

    # Floor is "observed minus 2" (Argus P3, round 2), not the exact observed
    # count (25 at fix time) — an exact-count floor reds on ANY unrelated
    # model rename/deletion anywhere in ne.api/ne.contracts, for a reason that
    # has nothing to do with B-P0-1. The nine fully-qualified membership
    # controls above carry the real anti-vacuity weight; this floor only
    # catches a sweep that's gone badly wrong (near-zero discovery).
    assert len(models) >= 23, (
        f"only {len(models)} *Request models discovered — floor is 23; the sweep may be broken"
    )

    for qualname, model in models.items():
        hit = _extraction_influencing_fields(model, substring_net=True)
        assert not hit, f"{qualname} names extraction field(s) {hit}"
        if model.__module__ == api_wire.__name__:
            assert model.model_config.get("extra") == "forbid", (
                f"{qualname} (ne.api.wire) must be extra='forbid' (wire.py §4-4 policy)"
            )


def test_bp01_no_wire_or_contract_model_carries_an_extraction_settings_field() -> None:
    """No model anywhere in the FULL ``ne.api`` or ``ne.contracts`` package
    trees (Argus P1 fix — a walked package, not a curated module list)
    carries any ``BrainGraphSettings`` ``extraction_*`` field by exact name
    (the exact net — deliberately narrower than T2's substring net). This is
    the design line-96 claim made mechanical: an auditor reading the doc can
    now run this test instead of taking the claim on trust.
    """
    models = _all_models()

    # Discovery controls: known non-Request models must be found, keyed by
    # fully qualified name (Argus P3: bare-name keys let two modules'
    # same-named classes shadow one another).
    for expected_qualname in (
        "ne.api.wire.GraphEntityRefView",
        "ne.api.wire.GraphHopView",
        "ne.api.graph_wire.GraphQueryRequest",
        "ne.contracts.types.GraphEntityRef",
        "ne.contracts.types.GraphHop",
        "ne.contracts.types.Principal",
    ):
        assert expected_qualname in models, f"discovery missed {expected_qualname}"

    # Per-package/module floors computed directly from qualname prefixes
    # (no accumulator dict — each module is walked exactly once, so a
    # ``.get(..., 0) +`` accumulator can never actually accumulate; Argus P3).
    wire_count = sum(1 for q in models if q.startswith(f"{api_wire.__name__}."))
    contracts_types_count = sum(1 for q in models if q.startswith(f"{contracts_types.__name__}."))
    api_total = sum(1 for q in models if q.startswith("ne.api."))
    contracts_total = sum(1 for q in models if q.startswith("ne.contracts."))
    assert wire_count >= 40, f"only {wire_count} models found in ne.api.wire — floor is 40"
    assert contracts_types_count >= 8, (
        f"only {contracts_types_count} models found in ne.contracts.types — floor is 8"
    )
    # Whole-package floors: proof the P1 fix is actually walking the full
    # tree and not silently back-sliding to a curated subset (observed 113 /
    # 58 respectively at fix time; floored well below to tolerate refactors).
    assert api_total >= 90, f"only {api_total} models found across ne.api — floor is 90"
    assert contracts_total >= 30, (
        f"only {contracts_total} models found across ne.contracts — floor is 30"
    )

    for qualname, model in models.items():
        hit = _extraction_influencing_fields(model)
        assert not hit, f"{qualname} carries extraction settings field(s) {hit}"


def test_bp01_guard_detects_a_smuggled_field() -> None:
    """In-suite RED/GREEN controls (vacuous-guard-shapes discipline): a model
    that actually smuggles ``extraction_mode`` is flagged by BOTH nets
    (even though it is ``extra="forbid"`` — forbid only blocks UNKNOWN keys,
    not a knowingly-named one, so the field-name net is what actually catches
    it); a substring-only smuggle is caught by the substring net but
    deliberately NOT by the exact net (documenting why request models get
    the wider net); a clean model passes both.
    """

    class _SmuggledKnob(BaseModel):
        model_config = ConfigDict(extra="forbid")

        extraction_mode: str = "autonomous"

    class _SmuggledBySubstring(BaseModel):
        extraction_yolo_override: bool = False

    class _Clean(BaseModel):
        prompt: str = ""

    assert _extraction_influencing_fields(_SmuggledKnob) == {"extraction_mode"}
    assert _extraction_influencing_fields(_SmuggledKnob, substring_net=True) == {"extraction_mode"}

    assert _extraction_influencing_fields(_SmuggledBySubstring) == set()
    assert _extraction_influencing_fields(_SmuggledBySubstring, substring_net=True) == {
        "extraction_yolo_override"
    }

    assert _extraction_influencing_fields(_Clean) == set()
    assert _extraction_influencing_fields(_Clean, substring_net=True) == set()


def test_bp01_guard_detects_an_alias_smuggled_field() -> None:
    """Argus P3 (round 3): both nets previously intersected
    ``model.model_fields`` KEYS only, so a field named innocuously in Python
    but WIRED to ``extraction_mode`` via ``alias``/``validation_alias``
    satisfied every guard while exposing ``extraction_mode`` on the wire.
    No live exposure existed — a full-surface sweep of ``ne.api``/
    ``ne.contracts`` found exactly two aliased fields in real code, both
    benign: ``ne.api.graph_wire.GraphPathsRequest.from_key`` -> ``"from"``
    and ``.to_key`` -> ``"to"`` (mirrored by ``_BenignAlias`` below as the
    control proving the alias union does not false-positive real aliases).
    """

    class _SmuggledByAlias(BaseModel):
        mode: str = Field(default="autonomous", alias="extraction_mode")

    class _SmuggledByValidationAlias(BaseModel):
        mode: str = Field(
            default="autonomous", validation_alias=AliasChoices("m", "extraction_mode")
        )

    class _BenignAlias(BaseModel):
        from_key: str = Field(default="", alias="from")
        to_key: str = Field(default="", alias="to")

    assert _extraction_influencing_fields(_SmuggledByAlias) == {"extraction_mode"}
    assert _extraction_influencing_fields(_SmuggledByAlias, substring_net=True) == {
        "extraction_mode"
    }

    assert _extraction_influencing_fields(_SmuggledByValidationAlias) == {"extraction_mode"}
    assert _extraction_influencing_fields(_SmuggledByValidationAlias, substring_net=True) == {
        "extraction_mode"
    }

    assert _extraction_influencing_fields(_BenignAlias) == set()
    assert _extraction_influencing_fields(_BenignAlias, substring_net=True) == set()


def test_bp01_route_signatures_cannot_name_an_extraction_knob() -> None:
    """Argus P1 (round 2): the model sweep above only sees pydantic
    ``BaseModel`` fields — it is BLIND to FastAPI route parameters declared
    as bare ``Annotated[..., Query(...)]``/``Path(...)``/``Header(...)``
    endpoint arguments, which carry no model at all.
    ``ne.api.routers.graph.graph_neighborhood`` (GET /v1/graph/neighborhood)
    already uses exactly this shape for ``entity``, ``hops``, ``limit_nodes``,
    ``limit_edges``, proving the surface is real and in active use, not
    hypothetical. This test sweeps every ``APIRoute`` endpoint's SIGNATURE
    and applies the same derived extraction nets to the parameter NAMES.
    """
    routes = _route_endpoint_params()

    # Discovery control: the graph_neighborhood bare-Query-param route must
    # actually be found, with its real (non-extraction) parameter names — or
    # the discoverer itself is broken (the classic vacuous-guard shape).
    neighborhood_keys = [
        key
        for key in routes
        if key.startswith("ne.api.routers.graph:") and "/v1/graph/neighborhood" in key
    ]
    assert neighborhood_keys, "discovery missed GET /v1/graph/neighborhood"
    assert {"entity", "hops", "limit_nodes", "limit_edges"} <= routes[neighborhood_keys[0]]

    # Floor is "observed minus several" (40 APIRoutes at fix time), matching
    # the other floors' discipline — the membership control above carries
    # the real anti-vacuity weight; this only catches a sweep gone badly
    # wrong (near-zero route discovery).
    assert len(routes) >= 30, f"only {len(routes)} APIRoutes discovered — floor is 30"

    knobs = _extraction_knob_fields()
    for key, param_names in routes.items():
        hit = (param_names & knobs) | {p for p in param_names if "extraction" in p}
        assert not hit, f"route {key} names extraction parameter(s) {hit}"


def test_bp01_guard_detects_a_smuggled_route_parameter() -> None:
    """In-suite RED control for the route-signature sweep (Argus P1, round 2
    remedy) — vacuous-guard-shapes discipline applied to the NEW surface, not
    just the model surface ``test_bp01_guard_detects_a_smuggled_field``
    already covers: a local route whose endpoint bares an
    ``extraction_mode`` query parameter (the exact shape
    ``graph_neighborhood`` uses for its real params) is caught by the SAME
    derived knob set; a clean route's parameter is not.
    """

    smuggled_router = APIRouter()

    @smuggled_router.get("/v1/_bp01_smuggled_probe")
    async def _smuggled_endpoint(
        extraction_mode: Annotated[str, Query()] = "autonomous",
    ) -> dict[str, str]:
        return {"mode": extraction_mode}

    clean_router = APIRouter()

    @clean_router.get("/v1/_bp01_clean_probe")
    async def _clean_endpoint(prompt: Annotated[str, Query()] = "") -> dict[str, str]:
        return {"prompt": prompt}

    def _params_for(api_router: APIRouter) -> set[str]:
        (route,) = [r for r in api_router.routes if isinstance(r, APIRoute)]
        return set(inspect.signature(route.endpoint).parameters)

    knobs = _extraction_knob_fields()
    assert (_params_for(smuggled_router) & knobs) == {"extraction_mode"}
    assert (_params_for(clean_router) & knobs) == set()


def test_bp01_composed_app_routes_cannot_name_an_extraction_knob(make_app: AppBuilder) -> None:
    """Argus P1 (round 3) — THE fix for the guard's own blind spot.

    ``_api_routers()``/``_route_endpoint_params()`` above collect only
    MODULE-LEVEL ``APIRouter`` objects (``for obj in vars(module).values():
    if isinstance(obj, APIRouter)``). ``build_scim_app``
    (``ne.api.scim.router``) constructs its ``APIRouter`` INSIDE the factory
    function — invisible to a module-attribute sweep even though
    ``create_app`` mounts it at ``/scim/v2`` and FastAPI actually serves it.
    Argus proved this is not hypothetical by a transient mutation: adding
    ``extraction_mode: str = Query(default="autonomous")`` to ``_list_users``
    (GET ``/scim/v2/Users``) left the round-2 route-signature sweep's ``-k
    bp01`` selection fully green — 6 passed, 22 deselected. A live, mounted,
    caller-facing HTTP route could name the forbidden knob with every guard
    passing.

    Fixed by asking a different question: not "what routers does the module
    namespace expose" but "what routes does the ACTUALLY-COMPOSED
    application serve" — ``_walk_composed_routes`` walks the real route
    table of an app built by ``create_app`` (via the ``make_app`` harness),
    the SAME object FastAPI hands to an ASGI server, so it reaches
    factory-built routers (``/scim/v2``, ``/cdc/v1``) and the
    middleware-wrapped inner ``/api`` tree BY CONSTRUCTION, not by
    enumerating a curated list — the enumeration-vs-derivation distinction
    this whole campaign is about, one level up from the model/route-name
    sweeps above.

    Argus P1 (round 4): even the composed sweep above still read each
    route's wire names off ``inspect.signature(route.endpoint).parameters``
    — blind to a parameter declared inside a ``Depends(...)`` dependency
    rather than the endpoint's own signature. Live, not hypothetical: every
    ``/scim/v2`` route is gated by ``APIRouter(dependencies=[Depends(
    require_bearer)])``, whose dependency declares an ``authorization``
    header FastAPI resolves from the caller's request — a signature-only
    sweep never sees it on any of the fifteen SCIM routes. Fixed by having
    ``_route_param_wire_names`` walk ``route.dependant`` recursively over
    ``.dependencies`` instead (``_dependant_wire_names`` — see its docstring
    for the full mechanism); the membership control below now asserts
    ``authorization`` is actually found, proving the dependant walk (not
    just the ``Mount``/middleware descent) is exercised for real.

    Review after round 4 found the boundary list below was itself
    overclaiming totality — a security-scheme name and a scalar body/form
    parameter's own alias were BOTH invisible to the walker at the time, yet
    neither was listed as a boundary. Both are now CLOSED (see
    ``_dependant_wire_names``'s docstring): the security-scheme fix is pinned
    by ``test_bp01_composed_walker_detects_a_security_scheme_smuggle``, the
    body/form-alias fix by
    ``test_bp01_composed_walker_detects_a_body_or_form_alias_smuggle``. Two
    walker-labeling nits were also fixed (a ``Mount`` nested inside a
    router-level ``prefix=`` now keys under the correctly-prefixed path; a
    ``StaticFiles`` mount is excluded from ``opaque`` instead of flagged) —
    see ``_walk_composed_routes``'s docstring.

    Review after round 5 (Argus P2, round 6) found boundary (1) below
    OVERCLAIMED: it named "a plain Starlette Route/WebSocketRoute" as safe,
    but the walker's own else-branch skipped ``APIWebSocketRoute`` too — a
    FastAPI class that DOES bind declared parameters exactly like an
    ``APIRoute``, not a plain Starlette one. Argus proved the gap live on
    the real app by transient mutation: a
    ``@router.websocket("/v1/graph/stream")`` route on
    ``ne.api.routers.graph`` binding ``extraction_mode`` via ``Query`` left
    ``-k bp01`` fully green — 14 passed, 22 deselected. ``APIWebSocketRoute``
    is now INSPECTED (see ``_walk_composed_routes``'s docstring); boundary
    (1) below is corrected to name ONLY what remains genuinely unswept.

    Review after round 6 (Argus P1, round 7) found a gap ONE LAYER DOWN
    from the round-6 fix: even for a route class the walker DID recognize
    (``APIRoute``/``APIWebSocketRoute``), it read only that route object's
    OWN construction-time ``.dependant`` — blind to a dependency attached at
    COMPOSITION time via ``include_router(router, dependencies=[...])`` or
    ``FastAPI(dependencies=[...])``. Argus proved this live on the real app
    by two transient mutations (both made, observed RED, reverted;
    transcripts in this unit's round-7 commit message): adding
    ``dependencies=[Depends(_mode_dep)]`` to ``inner.include_router(graph.
    router)`` and, separately, to ``inner = FastAPI(...)`` itself each bound
    a caller-facing ``extraction_mode`` on every route in their respective
    trees with ``-k bp01`` fully green (16 passed, 22 deselected). Fixed by
    unioning in ``_effective_wire_names(ctx)`` (see ``_walk_composed_
    routes``'s and that function's docstrings) — this closes a gap, it does
    NOT add a new boundary to the three below (no live exposure existed on
    the real composed app either, per the walker docstring's round-7
    paragraph).

    Review after round 7 (Argus P2, round 8) found two more genuine gaps and
    hardened two more findings, none a live caller-facing exposure:
    (a) this test's own sibling app-level control
    (``test_bp01_composed_walker_detects_an_app_level_dependency_smuggle``)
    did not actually exercise the round-7 fix — it built its probe route
    with ``@app.get(...)`` directly on the ``FastAPI(dependencies=[...])``
    instance, which the round-4 fix alone already covers; fixed by
    rebuilding it through ``include_router(...)``, the shape where the
    round-7 fix is load-bearing. (b) a pydantic MODEL used as a Query/
    Header/Cookie/Path parameter contributed only its own PARAMETER name,
    never a field living inside it — the analogous gap to the body-model
    case in boundary (2) below, but for a form the boundary list never
    named; CLOSED directly in ``_dependant_wire_names`` (not merely
    documented), pinned by
    ``test_bp01_composed_walker_detects_a_query_model_field_smuggle``. Also
    named a FOURTH boundary (FastAPI's ``.frontend()``/
    ``_low_priority_routes`` — see below) and hardened key-collision
    hygiene in ``params`` (union instead of overwrite) — neither a live
    exposure; see ``_walk_composed_routes``'s round-8 paragraph and this
    unit's round-8 commit message for the full detail on all four.

    Round 9 is a CHANGE OF METHOD, not another guarded surface: every round
    above guarded exactly the shape Argus found (module-level routers ->
    factory-built routers -> ``Mount`` targets -> ``APIWebSocketRoute`` ->
    depth-0 unwrapped models) and the next round found one level deeper —
    round 8's own model-field sweep tested
    ``isinstance(f.field_info.annotation, type) and issubclass(...,
    BaseModel)``, so a model reached through ANY wrapper (``Annotated``,
    a union, a generic container) or at ANY nesting depth beyond the
    outermost contributed nothing. Rather than teach the walker another
    enumerated shape, ``_models_in_annotation`` now DECOMPOSES an
    annotation STRUCTURALLY — unwrapping ``Annotated``, unions, and generic
    containers to their argument types and recursing — so every
    parameter/field annotation reduces to either "contains no
    ``BaseModel``" (verified, not assumed) or "these ``BaseModel``s, swept
    recursively via ``_model_wire_names``'s ``_seen``-guarded walk", or it
    REDS via the same asserted-empty ``opaque`` channel that already reds
    an unrecognized route class — see ``_models_in_annotation``'s docstring
    for the full decision table. This is a structural property, proven by
    ``test_bp01_annotation_decomposer_reduces_wrappers_and_reds_on_exotics``
    (every wrapper shape reduces correctly with empty ``opaque``; every
    exotic/undecomposable shape reds with exactly one entry), not a list of
    shapes someone thought to test for.

    FOUR honest coverage boundaries remain, stated rather than silently
    assumed: (1) a plain, EXACT-TYPE Starlette ``Route``/``WebSocketRoute``
    — ``/healthz`` and ``/config.json`` — takes only the raw ``Request``; no
    declared FastAPI parameter surface exists there to bind a caller field
    to, so there is nothing for this walker to check. This boundary is
    scoped to the exact classes only — it does NOT cover
    ``APIWebSocketRoute`` (swept above) or any subclass of ``Route``/
    ``WebSocketRoute`` (which reds via ``opaque`` instead of being assumed
    safe). A ``StaticFiles`` mount — the optional console SPA, deliberately
    left OFF here — is the same shape as the exact-type skip: files off
    disk, not parameters, and is excluded from ``opaque`` for exactly that
    reason, per the walker fix above. (2) a request BODY parameter
    contributes its Python parameter name AND its own ``alias=`` (per the
    round-3/round-4 fixes above) — field-level coverage of the model
    itself, for a body OR a Query/Header/Cookie/Path parameter typed as a
    pydantic model (at ANY nesting depth, through ANY wrapper — round 9),
    is swept by ``_dependant_wire_names`` via ``_models_in_annotation`` +
    ``_model_wire_names`` (see both docstrings), with the separate
    ``_all_models``/``_request_models`` module sweep as an INDEPENDENT,
    depth-0 net over the subset of models DEFINED inside ``ne.api``/
    ``ne.contracts`` — not the mechanism this guard's coverage claim relies
    on. (3) a dependency reading a value directly off the raw ``Request``
    object, and a ``Depends(...)`` callable's own RETURN annotation (round
    9 — a deliberate, documented non-sweep; see ``_dependant_wire_names``'s
    docstring), are both genuinely NON-closable by any static walk — no
    such dependency, and no live caller-facing use of a return-annotation
    shape, exists on the composed app today. (4) FastAPI's ``APIRouter.
    frontend(...)`` registers routes onto a SEPARATE ``_low_priority_
    routes`` list that ``iter_route_contexts`` (and therefore this walker)
    never visits at all — deliberately NOT swept, because doing so would
    require underscore-prefixed FastAPI internals this walker otherwise
    avoids; see ``_walk_composed_routes``'s docstring for the full
    rationale and ``test_bp01_composed_walker_does_not_see_frontend_low_
    priority_routes`` for the proof that the gap is both real (a router-
    level dependency IS resolved onto the frontend group) and currently
    unexercised (zero ``.frontend(`` call sites in ``ne.api``/
    ``ne.contracts`` today).
    """
    harness = make_app(
        scim_bearer_token="scim-guard-token-0123456789abcdef",
        cdc_webhook_secret=_CDC_SECRET,
        cdc_tables=_CDC_TABLES,
    )
    params, opaque = _walk_composed_routes(harness.app)

    # A future mount the walker cannot see is EXACTLY the blind-spot shape
    # this test closes — it must RED the guard, never silently shrink
    # coverage.
    assert opaque == [], (
        "unwalkable mounted apps — a mount the walker cannot see is exactly "
        f"the blind-spot shape this test closes: {opaque}"
    )

    # Membership control: the factory-built SCIM router — the exact Argus
    # round-3 blind spot — must actually be discovered, with its real params.
    scim_list = [k for k in params if k.endswith(" /scim/v2/Users") and "GET" in k]
    assert scim_list, (
        "discovery missed GET /scim/v2/Users — the factory-built SCIM "
        "router, the exact Argus round-3 blind spot"
    )
    assert {"filter", "startIndex", "count"} <= params[scim_list[0]]
    # Argus P1 (round 4): "authorization" is declared by the router-level
    # ``Depends(require_bearer)`` dependency, not by ``_list_users``'s own
    # signature — its presence here proves the dependant walk actually
    # descends ``.dependencies``, not just ``Mount``s and middleware.
    assert "authorization" in params[scim_list[0]], (
        "discovery missed the router-level Depends(require_bearer) dependency's "
        "'authorization' header — the round-4 dependant-walk fix is not exercised"
    )

    # Membership control: the OTHER factory-built mount (/cdc/v1) is reached too.
    assert any(k.startswith("ne.api.cdc.app:") and k.endswith(" /cdc/v1/events") for k in params), (
        "discovery missed POST /cdc/v1/events — the factory-built CDC router"
    )

    # Membership control: the OIDC-wrapped inner /api tree is reached through
    # the OidcAuthMiddleware unwrap (._app) — proves the middleware unwrap
    # actually works, not just the plain Mount case.
    nbhd = [k for k in params if k.endswith(" /api/v1/graph/neighborhood")]
    assert nbhd, (
        "discovery missed GET /api/v1/graph/neighborhood — the OIDC-wrapped inner /api tree"
    )
    assert {"entity", "hops", "limit_nodes", "limit_edges"} <= params[nbhd[0]]

    # Numeric floor — see ``_COMPOSED_ROUTE_FLOOR``'s docstring for the
    # derivation; the membership controls above carry the real anti-vacuity
    # weight, this only catches a sweep gone badly (near-zero) wrong.
    assert len(params) >= _COMPOSED_ROUTE_FLOOR, (
        f"only {len(params)} composed routes discovered — floor is {_COMPOSED_ROUTE_FLOOR}"
    )

    knobs = _extraction_knob_fields()
    for key, names in params.items():
        hit = (names & knobs) | {n for n in names if "extraction" in n}
        assert not hit, f"composed route {key} names extraction parameter(s) {hit}"


def test_bp01_composed_walker_detects_factory_built_smuggles() -> None:
    """RED/GREEN control for ``_walk_composed_routes`` ITSELF (vacuous-guard-
    shapes discipline applied to the walker, not just the nets): builds the
    exact shape that made the real guard blind in round 2 — a router
    constructed INSIDE a factory function, mounted behind a ``Mount``, behind
    an opaque callable middleware wrapper storing the inner app on ``._app``
    (the precise ``OidcAuthMiddleware`` shape) — and proves the walker
    descends the ``Mount``, unwraps the middleware, reaches the factory-built
    router, and resolves a ``Query`` alias, each independently.
    """

    class _Wrap:
        """The ``OidcAuthMiddleware`` shape: an opaque ASGI callable storing
        the wrapped app on ``self._app``."""

        def __init__(self, app: Any) -> None:
            self._app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await self._app(scope, receive, send)

    def _factory(*, smuggle: bool) -> Any:
        from fastapi import FastAPI

        app = FastAPI()
        router = APIRouter()  # built INSIDE the factory — the build_scim_app shape.

        if smuggle:

            @router.get("/probe-name")
            async def _probe_name(
                extraction_mode: Annotated[str, Query()] = "autonomous",
            ) -> dict[str, str]:
                return {"mode": extraction_mode}

            @router.get("/probe-alias")
            async def _probe_alias(
                mode: Annotated[str, Query(alias="extraction_mode")] = "autonomous",
            ) -> dict[str, str]:
                return {"mode": mode}
        else:

            @router.get("/probe-name")
            async def _probe_name_clean(prompt: Annotated[str, Query()] = "") -> dict[str, str]:
                return {"prompt": prompt}

        app.include_router(router)
        return app

    knobs = _extraction_knob_fields()

    smuggled = Starlette(routes=[Mount("/x", app=_Wrap(_factory(smuggle=True)))])
    params, opaque = _walk_composed_routes(smuggled)
    assert opaque == []
    name_keys = [k for k in params if k.endswith(" /x/probe-name")]
    assert name_keys, (
        "walker failed to descend the Mount + middleware wrapper to the factory router"
    )
    assert (params[name_keys[0]] & knobs) == {"extraction_mode"}
    alias_keys = [k for k in params if k.endswith(" /x/probe-alias")]
    assert alias_keys, "walker failed to reach the second factory-built route"
    assert (params[alias_keys[0]] & knobs) == {"extraction_mode"}, (
        "walker failed to resolve the Query(alias=...) smuggle"
    )

    clean = Starlette(routes=[Mount("/x", app=_Wrap(_factory(smuggle=False)))])
    clean_params, clean_opaque = _walk_composed_routes(clean)
    assert clean_opaque == []
    clean_name_keys = [k for k in clean_params if k.endswith(" /x/probe-name")]
    assert clean_name_keys
    clean_names = clean_params[clean_name_keys[0]]
    assert (clean_names & knobs) == set()
    assert {n for n in clean_names if "extraction" in n} == set()


def test_bp01_composed_walker_detects_a_dependency_injected_smuggle() -> None:
    """Argus P1, round 4 — RED/GREEN control for ``_dependant_wire_names``.

    The round-3 walker read ``inspect.signature(route.endpoint).parameters``
    — blind to a parameter declared inside a ``Depends(...)`` callable
    (FastAPI still resolves it from the caller's request and binds it before
    the endpoint ever runs). This is not hypothetical: ``build_scim_app``
    gates its ENTIRE router with ``APIRouter(dependencies=[Depends(
    require_bearer)])`` (``ne.api.scim.router``/``ne.api.scim.auth``), whose
    dependency declares an ``authorization`` header — invisible to a
    signature-only sweep on every one of the fifteen ``/scim/v2`` routes.

    Builds the same shape locally, independent of any real router: a
    ``Depends(...)`` dependency declaring an ``extraction_mode`` query
    parameter, attached at ROUTER level (mirroring
    ``APIRouter(dependencies=[...])``) to an endpoint whose OWN signature
    names nothing extraction-related — proving the smuggle is caught only
    because the walker descends ``.dependencies``, not because the endpoint
    itself names the knob. A second dependency proves the alias-on-a-
    dependency-parameter case too (``Query(alias="extraction_mode")`` on a
    plainly-named dependency parameter). The clean twin has a dependency
    that names nothing extraction-related, proving no false positive.
    """

    async def _smuggling_dependency(
        extraction_mode: Annotated[str, Query()] = "autonomous",
    ) -> None:
        return None

    async def _alias_smuggling_dependency(
        mode: Annotated[str, Query(alias="extraction_mode")] = "autonomous",
    ) -> None:
        return None

    async def _benign_dependency(actor: Annotated[str | None, Query()] = None) -> None:
        return None

    smuggled_router = APIRouter(dependencies=[Depends(_smuggling_dependency)])

    @smuggled_router.get("/v1/_bp01_dependency_smuggled_probe")
    async def _smuggled_endpoint(prompt: Annotated[str, Query()] = "") -> dict[str, str]:
        return {"prompt": prompt}

    alias_smuggled_router = APIRouter(dependencies=[Depends(_alias_smuggling_dependency)])

    @alias_smuggled_router.get("/v1/_bp01_dependency_alias_smuggled_probe")
    async def _alias_smuggled_endpoint(prompt: Annotated[str, Query()] = "") -> dict[str, str]:
        return {"prompt": prompt}

    clean_router = APIRouter(dependencies=[Depends(_benign_dependency)])

    @clean_router.get("/v1/_bp01_dependency_clean_probe")
    async def _clean_endpoint(prompt: Annotated[str, Query()] = "") -> dict[str, str]:
        return {"prompt": prompt}

    def _wire_names_for(api_router: APIRouter) -> set[str]:
        (route,) = [r for r in api_router.routes if isinstance(r, APIRoute)]
        return _route_param_wire_names(route)

    knobs = _extraction_knob_fields()

    smuggled_names = _wire_names_for(smuggled_router)
    assert "prompt" in smuggled_names, "the endpoint's own parameter must still be found"
    assert (smuggled_names & knobs) == {"extraction_mode"}, (
        "walker failed to descend a router-level Depends(...) dependency's own parameters"
    )

    alias_smuggled_names = _wire_names_for(alias_smuggled_router)
    assert (alias_smuggled_names & knobs) == {"extraction_mode"}, (
        "walker failed to resolve a Query(alias=...) declared inside a dependency"
    )

    clean_names = _wire_names_for(clean_router)
    assert (clean_names & knobs) == set()
    assert {n for n in clean_names if "extraction" in n} == set()


def test_bp01_composed_walker_detects_a_body_or_form_alias_smuggle() -> None:
    """Argus P2 (review after round 4) — ``_dependant_wire_names`` took a
    parameter's ``field_info.alias`` only when ``isinstance(f.field_info,
    fastapi_params.Param)`` — ``Query``/``Header``/``Cookie``/``Path``.
    ``fastapi.params.Body`` is NOT a ``Param`` subclass (``Form``/``File``
    subclass ``Body``, not ``Param``), so a SCALAR body/form parameter's own
    ``alias=`` was invisible: ``mode: Annotated[str, Body(alias=
    "extraction_mode", embed=True)]`` bound the caller's JSON key
    ``extraction_mode``, and ``mode: Annotated[str, Form(alias=
    "extraction_mode")]`` bound the caller's form field ``extraction_mode``,
    with every ``test_bp01_*`` guard staying green — the same smuggle round 3
    closed for pydantic MODEL fields (``_model_wire_names``), left open here
    because a scalar body/form parameter has no model for that sweep to
    walk at all. No live exposure exists today: every body parameter
    reachable from the real composed app is a pydantic MODEL (covered by the
    separate ``_all_models``/``_request_models`` sweep), never a bare scalar
    ``Body``/``Form`` parameter — verified by inspection at write time and by
    the real-app sweep in ``test_bp01_composed_app_routes_cannot_name_an_
    extraction_knob`` staying green with the widened check. Fixed by
    widening the ``isinstance`` check to ``(fastapi_params.Param,
    fastapi_params.Body)``.
    """

    body_router = APIRouter()

    @body_router.post("/v1/_bp01_body_alias_smuggled_probe")
    async def _body_smuggled(
        mode: Annotated[str, Body(alias="extraction_mode", embed=True)] = "autonomous",
    ) -> dict[str, str]:
        return {"mode": mode}

    form_router = APIRouter()

    @form_router.post("/v1/_bp01_form_alias_smuggled_probe")
    async def _form_smuggled(
        mode: Annotated[str, Form(alias="extraction_mode")] = "autonomous",
    ) -> dict[str, str]:
        return {"mode": mode}

    clean_router = APIRouter()

    @clean_router.post("/v1/_bp01_body_clean_probe")
    async def _clean_body(
        payload: Annotated[str, Body(embed=True)] = "",
    ) -> dict[str, str]:
        return {"payload": payload}

    def _wire_names_for(api_router: APIRouter) -> set[str]:
        (route,) = [r for r in api_router.routes if isinstance(r, APIRoute)]
        return _route_param_wire_names(route)

    knobs = _extraction_knob_fields()

    body_names = _wire_names_for(body_router)
    assert (body_names & knobs) == {"extraction_mode"}, (
        "walker failed to resolve a scalar Body(alias=...) smuggle"
    )

    form_names = _wire_names_for(form_router)
    assert (form_names & knobs) == {"extraction_mode"}, (
        "walker failed to resolve a scalar Form(alias=...) smuggle"
    )

    clean_names = _wire_names_for(clean_router)
    assert (clean_names & knobs) == set()
    assert "payload" in clean_names, "the endpoint's own body parameter must still be found"


#: MODULE-level, not local to the test function below, DELIBERATELY: this
#: file has ``from __future__ import annotations`` (postponed evaluation),
#: so ``Annotated[str, Security(scheme)]`` is stored as a STRING and later
#: resolved by FastAPI via ``eval(..., endpoint.__globals__, endpoint.
#: __globals__)`` — the endpoint's DEFINING MODULE's globals, never an
#: enclosing test function's locals. A first draft of this test referenced a
#: scheme built as a local variable inside the test function; pydantic's
#: lenient forward-ref eval silently failed to resolve it and FastAPI
#: dropped the ``Security(...)`` wrapper entirely, degrading the parameter
#: to a bare ``Query`` (empirically caught: the "smuggled" route's wire
#: names came back as ``{"key"}`` with no ``extraction_mode`` at all — a
#: false negative in the TEST, not a guard blind spot). Module-level scheme
#: objects are resolvable from ``endpoint.__globals__`` and avoid the trap.
_BP01_SECURITY_SMUGGLE_SCHEME = APIKeyQuery(name="extraction_mode")
_BP01_SECURITY_CLEAN_SCHEME = HTTPBearer()


def test_bp01_composed_walker_detects_a_security_scheme_smuggle() -> None:
    """Argus P3 (review after round 4) — a ``Security(scheme)`` dependency
    where ``scheme`` is an ``APIKeyQuery``/``APIKeyHeader``/``APIKeyCookie``
    binds the caller's request to a wire name that lives ONLY on
    ``scheme.model.name``: FastAPI builds an EMPTY sub-``Dependant`` for such
    a dependency (verified empirically — ``.query_params``/``.header_params``
    are both ``[]`` for a ``Security(APIKeyQuery(name=...))`` dependency at
    ANY level), so ``_dependant_wire_names``'s param-list walk alone never
    sees it — ``Security(APIKeyQuery(name="extraction_mode"))`` bound the
    query key ``extraction_mode`` while contributing nothing to any guard.
    No live exposure exists today: the only security-style dependency on the
    real composed app is SCIM's manual ``Header(default=None)`` bearer check
    (``ne.api.scim.auth``), which is a plain ``Param``, already covered — not
    a ``fastapi.security`` ``SecurityBase`` scheme at all. Fixed by reading
    ``sub_dependant.call.model.name`` when the sub-dependency's ``.call`` is
    a ``SecurityBase`` instance. ``HTTPBearer`` (no caller-NAMED wire key —
    it always binds the fixed ``Authorization`` header via FastAPI's own
    machinery, already covered by ``f.name``) is the benign control, proving
    the fix adds nothing spurious.
    """

    smuggled_router = APIRouter()

    @smuggled_router.get("/v1/_bp01_security_smuggled_probe")
    async def _smuggled_endpoint(
        key: Annotated[str, Security(_BP01_SECURITY_SMUGGLE_SCHEME)],
    ) -> dict[str, str]:
        return {"key": key}

    clean_router = APIRouter()

    @clean_router.get("/v1/_bp01_security_clean_probe")
    async def _clean_endpoint(
        creds: Annotated[HTTPAuthorizationCredentials, Security(_BP01_SECURITY_CLEAN_SCHEME)],
    ) -> dict[str, str]:
        return {"scheme": creds.scheme}

    def _wire_names_for(api_router: APIRouter) -> set[str]:
        (route,) = [r for r in api_router.routes if isinstance(r, APIRoute)]
        return _route_param_wire_names(route)

    knobs = _extraction_knob_fields()

    smuggled_names = _wire_names_for(smuggled_router)
    assert (smuggled_names & knobs) == {"extraction_mode"}, (
        "walker failed to resolve an APIKeyQuery Security-scheme name"
    )

    clean_names = _wire_names_for(clean_router)
    assert (clean_names & knobs) == set()
    assert {n for n in clean_names if "extraction" in n} == set()


def test_bp01_composed_walker_resolves_a_router_prefixed_mount_path() -> None:
    """Argus P3 (review after round 4, walker nit a) — a ``Mount`` nested
    inside an ``_IncludedRouter`` under a router-level ``prefix=`` was keyed
    (and its ``opaque`` message, if unresolvable) under the UNPREFIXED
    ``original.path``: ``fastapi.routing.RouteContext.path`` is empty for a
    ``Mount``-shaped effective route (it is only populated for
    ``APIRoute``-shaped ones) — the CORRECT prefixed path lives on
    ``RouteContext.starlette_route`` instead (verified empirically). No route
    was ever actually DROPPED by the old code (the walker still recursed
    into the mounted app's own routes either way), and no live coverage gap
    exists today (every mount in ``create_app`` — ``/scim/v2``, ``/cdc/v1``,
    the OIDC-wrapped ``/api``, the optional console ``StaticFiles`` — is a
    top-level ``app.mount(...)``/``app.include_router(..., prefix=...)``
    call, never a ``Mount`` nested inside a PREFIXED sub-router) — but the
    reported KEY was wrong, worth being correct by construction rather than
    by today's topology.
    """
    from fastapi import FastAPI

    inner = FastAPI()

    @inner.get("/probe")
    async def _probe(x: Annotated[str, Query()] = "") -> dict[str, str]:
        return {"x": x}

    sub = APIRouter()
    sub.mount("/mnt", inner)

    app = FastAPI()
    app.include_router(sub, prefix="/pfx")

    params, opaque = _walk_composed_routes(app)
    assert opaque == []
    matches = [k for k in params if k.endswith(" /pfx/mnt/probe")]
    assert matches, (
        f"Mount path resolution lost the router-level prefix; keys were {sorted(params)}"
    )


def test_bp01_composed_walker_does_not_flag_staticfiles_as_opaque() -> None:
    """Argus P3 (review after round 4, walker nit b) — before this fix, ANY
    unresolvable ``Mount`` target — including a ``StaticFiles`` mount, which
    serves files off disk and genuinely has no parameter surface to sweep —
    was appended to ``opaque``, conflating a real blind spot with an
    expected, harmless case. ``make_app``'s ``serve_console_dist`` knob is
    left OFF in ``test_bp01_composed_app_routes_cannot_name_an_extraction_
    knob`` specifically to dodge this today; this test proves the walker
    ITSELF now handles a ``StaticFiles`` mount correctly, so a future unit
    turning ``serve_console_dist`` on there does not read a spurious
    ``opaque`` regression.
    """
    with tempfile.TemporaryDirectory() as tmp:
        app = Starlette(routes=[Mount("/static", app=StaticFiles(directory=tmp))])
        params, opaque = _walk_composed_routes(app)
        assert opaque == [], f"a StaticFiles mount must not be flagged opaque: {opaque}"
        assert params == {}


def test_bp01_composed_walker_detects_a_websocket_smuggle() -> None:
    """Argus P2 (round 6) — the walker's ``else`` branch silently skipped
    FastAPI ``APIWebSocketRoute`` objects while affirmatively declaring
    (in its own comment) that skip safe using reasoning that only holds for
    a PLAIN Starlette ``Route``/``WebSocketRoute`` — not for
    ``APIWebSocketRoute``, which binds declared ``Query``/``Header``/
    ``Cookie``/``Path``/``Depends`` parameters for the handshake exactly
    like an ``APIRoute`` does. Proven live on the real composed app by
    Argus's transient mutation on ``ne.api.routers.graph`` (recorded
    verbatim in this unit's commit message): a
    ``@router.websocket("/v1/graph/stream")`` route binding
    ``extraction_mode`` via ``Query`` left ``-k bp01`` fully green — 14
    passed, 22 deselected.

    This is the RED/GREEN control for the walker ITSELF (vacuous-guard-
    shapes discipline, same as ``test_bp01_composed_walker_detects_factory_
    built_smuggles`` for HTTP routes): builds the exact factory-built,
    router-prefixed shape (an ``APIRouter`` constructed INSIDE a factory
    function, ``include_router(..., prefix=...)``'d onto a ``FastAPI()``
    app — the same shape as ``build_scim_app``) with a direct-name smuggle,
    an alias smuggle, and proves a CLEAN websocket route still contributes
    to ``params`` (so the guard cannot be satisfied by the walker simply
    finding nothing for websocket routes).
    """

    def _smuggled_factory() -> APIRouter:
        router = APIRouter()

        @router.websocket("/stream-name")
        async def _ws_name_smuggled(
            websocket: WebSocket,
            extraction_mode: Annotated[str, Query()] = "autonomous",
        ) -> None: ...

        @router.websocket("/stream-alias")
        async def _ws_alias_smuggled(
            websocket: WebSocket,
            mode: Annotated[str, Query(alias="extraction_mode")] = "autonomous",
        ) -> None: ...

        return router

    def _clean_factory() -> APIRouter:
        router = APIRouter()

        @router.websocket("/stream-clean")
        async def _ws_clean(
            websocket: WebSocket,
            topic: Annotated[str, Query()] = "",
        ) -> None: ...

        return router

    from fastapi import FastAPI

    smuggled_app = FastAPI()
    smuggled_app.include_router(_smuggled_factory(), prefix="/pfx")

    params, opaque = _walk_composed_routes(smuggled_app)
    assert opaque == [], f"unexpected opaque entries: {opaque}"

    name_keys = [k for k in params if k.endswith(" /pfx/stream-name")]
    assert name_keys, (
        f"discovery missed the router-prefixed websocket route; keys were {sorted(params)}"
    )
    assert ":WS " in name_keys[0], (
        f"websocket route key must carry the WS token, got {name_keys[0]!r}"
    )

    alias_keys = [k for k in params if k.endswith(" /pfx/stream-alias")]
    assert alias_keys, f"discovery missed the aliased websocket route; keys were {sorted(params)}"

    knobs = _extraction_knob_fields()
    assert (params[name_keys[0]] & knobs) == {"extraction_mode"}, (
        "walker failed to detect a direct-name extraction_mode smuggle on a "
        f"websocket route: {params[name_keys[0]]}"
    )
    assert (params[alias_keys[0]] & knobs) == {"extraction_mode"}, (
        "walker failed to detect a Query(alias='extraction_mode') smuggle on "
        f"a websocket route: {params[alias_keys[0]]}"
    )

    clean_app = FastAPI()
    clean_app.include_router(_clean_factory(), prefix="/pfx")
    clean_params, clean_opaque = _walk_composed_routes(clean_app)
    assert clean_opaque == []
    clean_keys = [k for k in clean_params if k.endswith(" /pfx/stream-clean")]
    assert clean_keys, (
        "a CLEAN websocket route must still be discovered and contribute to "
        f"params (not merely absent from a smuggle) — keys were {sorted(clean_params)}"
    )
    assert (clean_params[clean_keys[0]] & knobs) == set()
    assert not any("extraction" in n for n in clean_params[clean_keys[0]]), (
        f"clean websocket route unexpectedly named an extraction-ish field: "
        f"{clean_params[clean_keys[0]]}"
    )


def test_bp01_composed_walker_flags_an_unrecognized_route_class_as_opaque() -> None:
    """Argus P2 (round 6) — the replacement for the walker's old silent
    ``else``-skip must be FAIL-LOUD, not merely "less wrong": an
    unrecognized ``Route`` subclass (which might add its own parameter
    binding a future FastAPI/Starlette version could introduce) now lands in
    ``opaque`` — redding the real-app test's ``opaque == []`` gate — instead
    of silently falling through a class check that only recognizes the
    classes this walker knows about today.

    Bidirectional control (vacuous-guard-shapes discipline): the exact-type
    plain ``Route``/``WebSocketRoute`` skip must NOT false-positive into
    ``opaque`` — it is the documented, deliberate boundary, not the failure
    mode this test is pinning.
    """

    class _UnrecognizedRoute(Route):
        """A trivial Route subclass — deliberately unrecognized by the
        walker's ``type(original) in (Route, WebSocketRoute)`` exact-type
        check (``isinstance`` would have missed this bug; ``type() in``
        does not)."""

    async def _raw_http(request: Any) -> Any: ...

    async def _raw_ws(websocket: Any) -> None: ...

    weird_app = Starlette(routes=[_UnrecognizedRoute("/weird", _raw_http)])
    params, opaque = _walk_composed_routes(weird_app)
    assert params == {}
    assert len(opaque) == 1, f"expected exactly one opaque entry, got {opaque}"
    assert "/weird" in opaque[0], f"opaque message must name the path: {opaque[0]}"
    assert "_UnrecognizedRoute" in opaque[0], (
        f"opaque message must name the unrecognized class: {opaque[0]}"
    )

    benign_app = Starlette(
        routes=[
            Route("/plain", _raw_http),
            WebSocketRoute("/plain-ws", _raw_ws),
        ]
    )
    benign_params, benign_opaque = _walk_composed_routes(benign_app)
    assert benign_params == {}
    assert benign_opaque == [], (
        f"exact-type plain Route/WebSocketRoute must NOT be flagged opaque: {benign_opaque}"
    )


async def _bp01_include_level_mode_dep(
    extraction_mode: Annotated[str, Query()] = "autonomous",
) -> str:
    return extraction_mode


async def _bp01_include_level_benign_dep(
    actor: Annotated[str | None, Query()] = None,
) -> str | None:
    return actor


def test_bp01_composed_walker_detects_an_include_level_dependency_smuggle() -> None:
    """Argus P1, round 7 — RED/GREEN control for ``_effective_wire_names``
    (HTTP shape).

    ``_route_param_wire_names`` reads only a route's OWN construction-time
    ``.dependant`` — populated for dependencies given to ``APIRouter(
    dependencies=[...])`` at router-CONSTRUCTION time (already covered
    since round 4), but NOT for dependencies given to the
    ``include_router(router, dependencies=[...])`` CALL itself, which
    ``fastapi.routing._EffectiveRouteContext`` merges in only when the
    route is walked through ``iter_route_contexts`` — i.e. only reachable
    via ``_walk_composed_routes``, never via ``_route_param_wire_names``
    called directly on a bare route object (proven live on the real app:
    ``inner.include_router(graph.router, dependencies=[Depends(
    _mode_dep)])`` bound ``extraction_mode`` on every ``/api/v1/graph/*``
    route with ``-k bp01`` fully green — 16 passed, 22 deselected;
    transcript in this unit's round-7 commit message).

    Builds the same factory-built, router-prefixed shape as the round-6
    websocket control (an ``APIRouter`` constructed INSIDE a factory
    function, ``include_router(..., prefix=..., dependencies=[...])``'d
    onto a ``FastAPI()`` app) with the dependency attached at the INCLUDE
    call, not the router constructor — the exact shape
    ``test_bp01_composed_walker_detects_a_dependency_injected_smuggle``
    does NOT exercise. A clean twin (a benign include-level dependency)
    proves no false positive, and the endpoint's own unrelated parameter
    still surfaces (proving the union, not a replace).
    """
    from fastapi import FastAPI

    def _smuggled_factory() -> APIRouter:
        router = APIRouter()

        @router.get("/probe")
        async def _http_probe(prompt: Annotated[str, Query()] = "") -> dict[str, str]:
            return {"prompt": prompt}

        return router

    smuggled_app = FastAPI()
    smuggled_app.include_router(
        _smuggled_factory(),
        prefix="/pfx",
        dependencies=[Depends(_bp01_include_level_mode_dep)],
    )

    params, opaque = _walk_composed_routes(smuggled_app)
    assert opaque == [], f"unexpected opaque entries: {opaque}"

    probe_keys = [k for k in params if k.endswith(" /pfx/probe")]
    assert probe_keys, (
        f"discovery missed the router-prefixed include-level-dependency route; "
        f"keys were {sorted(params)}"
    )
    assert "prompt" in params[probe_keys[0]], (
        "the endpoint's own parameter must still be found (union, not replace)"
    )
    knobs = _extraction_knob_fields()
    assert (params[probe_keys[0]] & knobs) == {"extraction_mode"}, (
        "walker failed to detect an include_router(..., dependencies=[...]) "
        f"smuggle: {params[probe_keys[0]]}"
    )

    clean_app = FastAPI()
    clean_app.include_router(
        _smuggled_factory(),
        prefix="/pfx",
        dependencies=[Depends(_bp01_include_level_benign_dep)],
    )
    clean_params, clean_opaque = _walk_composed_routes(clean_app)
    assert clean_opaque == []
    clean_keys = [k for k in clean_params if k.endswith(" /pfx/probe")]
    assert clean_keys
    assert (clean_params[clean_keys[0]] & knobs) == set()
    assert not any("extraction" in n for n in clean_params[clean_keys[0]]), (
        f"clean include-level dependency unexpectedly named an extraction-ish "
        f"field: {clean_params[clean_keys[0]]}"
    )


def test_bp01_composed_walker_detects_a_websocket_include_level_dependency_smuggle() -> None:
    """Argus P1, round 7 — RED/GREEN control for ``_effective_wire_names``
    (websocket shape), pinning the VERIFIED ASYMMETRY documented in that
    function's docstring: for an included ``APIWebSocketRoute``,
    ``fastapi.routing._build_effective_context`` does NOT populate
    ``_EffectiveRouteContext.dependant`` at all (it stays the dataclass
    default, ``None``) — the merged, effective dependant instead lives on
    a BRAND NEW ``APIWebSocketRoute`` object stored as ``ctx.
    starlette_route``, the same object the round-6 websocket branch
    already resolves for its PATH. This test would NOT go red under a fix
    that only reads ``ctx.dependant`` (the ``APIRoute`` shape) — only one
    that also reads ``ctx.starlette_route.dependant`` for this class.

    Same factory-built, router-prefixed shape as ``test_bp01_composed_
    walker_detects_a_websocket_smuggle``, but the smuggling dependency is
    attached at the ``include_router(..., dependencies=[...])`` call, not
    inside the websocket endpoint's own signature and not via
    ``APIRouter(dependencies=[...])`` at router-construction time.
    """
    from fastapi import FastAPI

    def _smuggled_factory() -> APIRouter:
        router = APIRouter()

        @router.websocket("/stream")
        async def _ws_probe(websocket: WebSocket, topic: Annotated[str, Query()] = "") -> None: ...

        return router

    smuggled_app = FastAPI()
    smuggled_app.include_router(
        _smuggled_factory(),
        prefix="/pfx",
        dependencies=[Depends(_bp01_include_level_mode_dep)],
    )

    params, opaque = _walk_composed_routes(smuggled_app)
    assert opaque == [], f"unexpected opaque entries: {opaque}"

    stream_keys = [k for k in params if k.endswith(" /pfx/stream")]
    assert stream_keys, (
        f"discovery missed the router-prefixed websocket include-level-"
        f"dependency route; keys were {sorted(params)}"
    )
    assert ":WS " in stream_keys[0]
    assert "topic" in params[stream_keys[0]], (
        "the endpoint's own parameter must still be found (union, not replace)"
    )
    knobs = _extraction_knob_fields()
    assert (params[stream_keys[0]] & knobs) == {"extraction_mode"}, (
        "walker failed to detect an include_router(..., dependencies=[...]) "
        f"smuggle on a websocket route: {params[stream_keys[0]]}"
    )

    clean_app = FastAPI()
    clean_app.include_router(
        _smuggled_factory(),
        prefix="/pfx",
        dependencies=[Depends(_bp01_include_level_benign_dep)],
    )
    clean_params, clean_opaque = _walk_composed_routes(clean_app)
    assert clean_opaque == []
    clean_keys = [k for k in clean_params if k.endswith(" /pfx/stream")]
    assert clean_keys
    assert (clean_params[clean_keys[0]] & knobs) == set()
    assert not any("extraction" in n for n in clean_params[clean_keys[0]]), (
        f"clean include-level dependency unexpectedly named an extraction-ish "
        f"field on a websocket route: {clean_params[clean_keys[0]]}"
    )


def test_bp01_composed_walker_detects_an_app_level_dependency_smuggle() -> None:
    """Argus P1, round 7 — RED/GREEN control for ``_effective_wire_names``
    (app-level shape): ``FastAPI(dependencies=[...])`` merges the given
    dependencies into EVERY route's effective dependant via the same
    ``_EffectiveRouteContext`` mechanism as ``include_router(...,
    dependencies=[...])`` (proven live on the real app: ``inner =
    FastAPI(dependencies=[Depends(_mode_dep)])`` bound ``extraction_mode``
    on every ``/api/v1/*`` route with ``-k bp01`` fully green — 16 passed,
    22 deselected; transcript in this unit's round-7 commit message).

    Argus P2 (round 8): the PREVIOUS version of this test built its probe
    route with ``@smuggled_app.get(...)`` directly on the app-level
    ``FastAPI(dependencies=[...])`` instance. ``FastAPI.__init__`` hands
    its ``dependencies=`` straight to ``self.router = APIRouter(
    dependencies=dependencies, ...)``, and ``APIRouter.add_api_route``
    bakes the ROUTER'S OWN ``dependencies`` into each added route's OWN
    construction-time ``.dependant`` IMMEDIATELY — the exact round-4
    mechanism ``_route_param_wire_names`` alone (no ``_effective_wire_
    names`` needed at all) already reaches. Proven by mutation: with
    ``_effective_wire_names`` monkeypatched to always return ``set()``,
    the PREVIOUS version of this test still passed — it was not exercising
    the round-7 fix it claimed to control. Fixed by building the probe
    route inside an ``APIRouter`` factory (the shape ``build_scim_app``/
    ``graph.router`` actually use) and reaching it via ``include_router(
    ...)`` with NO include-level ``dependencies=`` of its own — so the app-
    level dependency can ONLY reach the route's effective dependant via
    ``_effective_wire_names``, the same shape Argus's own round-7 live
    mutation on ``inner = FastAPI(dependencies=[Depends(_mode_dep)])``
    used. Re-verified: monkeypatching ``_effective_wire_names`` to ``set()``
    now turns THIS version of the test red.
    """
    from fastapi import FastAPI

    def _probe_factory() -> APIRouter:
        router = APIRouter()

        @router.get("/probe")
        async def _http_probe(prompt: Annotated[str, Query()] = "") -> dict[str, str]:
            return {"prompt": prompt}

        return router

    smuggled_app = FastAPI(dependencies=[Depends(_bp01_include_level_mode_dep)])
    smuggled_app.include_router(_probe_factory(), prefix="/pfx")

    params, opaque = _walk_composed_routes(smuggled_app)
    assert opaque == [], f"unexpected opaque entries: {opaque}"

    probe_keys = [k for k in params if k.endswith(" /pfx/probe")]
    assert probe_keys, (
        f"discovery missed the app-level-dependency route; keys were {sorted(params)}"
    )
    assert "prompt" in params[probe_keys[0]], (
        "the endpoint's own parameter must still be found (union, not replace)"
    )
    knobs = _extraction_knob_fields()
    assert (params[probe_keys[0]] & knobs) == {"extraction_mode"}, (
        f"walker failed to detect a FastAPI(dependencies=[...]) smuggle: {params[probe_keys[0]]}"
    )

    clean_app = FastAPI(dependencies=[Depends(_bp01_include_level_benign_dep)])
    clean_app.include_router(_probe_factory(), prefix="/pfx")

    clean_params, clean_opaque = _walk_composed_routes(clean_app)
    assert clean_opaque == []
    clean_keys = [k for k in clean_params if k.endswith(" /pfx/probe")]
    assert clean_keys
    assert (clean_params[clean_keys[0]] & knobs) == set()
    assert not any("extraction" in n for n in clean_params[clean_keys[0]]), (
        f"clean app-level dependency unexpectedly named an extraction-ish "
        f"field: {clean_params[clean_keys[0]]}"
    )


class _Bp01QueryModelSmuggle(BaseModel):
    """Probe model for ``test_bp01_composed_walker_detects_a_query_model_
    field_smuggle`` — deliberately module-level (NOT nested inside the test
    function): this repo's ``from __future__ import annotations`` makes
    ``field_info.annotation`` resolution go through FastAPI's own
    ``get_type_hints``, which cannot resolve a forward reference to a class
    defined LOCAL to a function (no module-global binding to resolve
    against) — a class-scoping detail, not a walker gap; module-level
    keeps the class OUTSIDE both ``ne.api``/``ne.contracts`` trees (this is
    a test module) while remaining resolvable.
    """

    extraction_mode: str = "autonomous"


class _Bp01QueryModelClean(BaseModel):
    """Clean twin of ``_Bp01QueryModelSmuggle`` — see its docstring."""

    topic: str = ""


def test_bp01_composed_walker_detects_a_query_model_field_smuggle() -> None:
    """Argus P2, round 8 — a pydantic MODEL used as a Query (or Header/
    Cookie/Path) parameter — FastAPI's documented query-parameter-model
    form (``q: Annotated[QModel, Query()]``, supported since fastapi
    0.115) — was UNINSPECTED: ``_dependant_wire_names`` took only ``f.name``
    (the PYTHON PARAMETER NAME, e.g. ``"q"``) and ``f.field_info.alias``
    per ``ModelField``, never the fields *inside* the model. Defense in
    depth from the SEPARATE ``_all_models``/``_request_models`` sweep
    (``test_bp01_no_wire_or_contract_model_carries_an_extraction_settings_
    field``) only ever reaches a model DEFINED somewhere inside the walked
    ``ne.api``/``ne.contracts`` package trees — the probe models above are
    defined in THIS test module, outside both trees, to prove the WALKER
    itself now closes this, not the external net.

    Fixed (round 8) by unioning in ``_model_wire_names(annotation)`` in
    ``_dependant_wire_names`` whenever a path/query/header/cookie/body
    ``ModelField``'s own ``field_info.annotation`` was DIRECTLY a
    ``BaseModel`` subclass. Round 9 (CHANGE OF METHOD) replaced that
    depth-0, unwrapped-only check with ``_models_in_annotation``'s
    structural decomposition — this test's probe (a bare, unwrapped model)
    is now the SHALLOWEST case the decomposer handles, not the boundary of
    what it can reach; ``test_bp01_composed_walker_detects_a_nested_model_
    smuggle`` and ``test_bp01_composed_walker_detects_a_wrapped_depends_
    model_smuggle`` pin the depth>=1/wrapped cases this test does not
    exercise.
    """
    from fastapi import FastAPI

    router = APIRouter()

    @router.get("/q")
    async def _q(q: Annotated[_Bp01QueryModelSmuggle, Query()]) -> dict[str, str]:
        return q.model_dump()

    @router.get("/q-clean")
    async def _q_clean(q: Annotated[_Bp01QueryModelClean, Query()]) -> dict[str, str]:
        return q.model_dump()

    app = FastAPI()
    app.include_router(router, prefix="/pfx")

    params, opaque = _walk_composed_routes(app)
    assert opaque == [], f"unexpected opaque entries: {opaque}"

    smuggle_keys = [k for k in params if k.endswith(" /pfx/q")]
    assert smuggle_keys, f"discovery missed the query-model route; keys were {sorted(params)}"
    knobs = _extraction_knob_fields()
    assert (params[smuggle_keys[0]] & knobs) == {"extraction_mode"}, (
        f"walker failed to detect a query-model-field smuggle: {params[smuggle_keys[0]]}"
    )

    clean_keys = [k for k in params if k.endswith(" /pfx/q-clean")]
    assert clean_keys, f"discovery missed the clean query-model route; keys were {sorted(params)}"
    assert (params[clean_keys[0]] & knobs) == set()
    assert not any("extraction" in n for n in params[clean_keys[0]]), (
        f"clean query-model route unexpectedly named an extraction-ish field: "
        f"{params[clean_keys[0]]}"
    )


def test_bp01_composed_walker_does_not_see_frontend_low_priority_routes(tmp_path: Path) -> None:
    """DOCUMENTED, PROVEN residual boundary (Argus P2, round 8): FastAPI's
    ``APIRouter.frontend(...)`` (added by the SAME 0.139 lazy-
    ``include_router`` refactor that introduced ``iter_route_contexts``
    itself) registers a ``_FrontendRouteGroup`` onto the router's OWN
    ``_low_priority_routes`` list — a dispatch path SEPARATE from
    ``.routes``, matched only AFTER every normal FastAPI route fails to
    match (``Router.app()``/``_iter_low_priority_routes()``/
    ``_match_low_priority()``). ``iter_route_contexts`` walks only
    ``.routes``; it never sees ``_low_priority_routes`` at all, so this
    walker cannot discover it either.

    Deliberately NOT swept: reaching it needs ``APIRouter.
    _low_priority_routes`` and ``_IncludedRouter.effective_low_priority_
    routes()`` — BOTH underscore-prefixed, undocumented FastAPI internals,
    unlike ``iter_route_contexts`` (public, no leading underscore, the
    framework's own OpenAPI-generator dependency — the documented boundary
    this walker's whole design deliberately stays on the right side of,
    per its VERSION COUPLING paragraph). Widening the walker to cover this
    would trade one already-flagged version-coupling risk for a strictly
    deeper one, for a surface with zero live call sites today.

    PROVEN, not merely claimed (written-rules-disarm-review discipline: a
    safety/boundary claim must trace to a test, not prose): (a) a router-
    level ``Depends(...)`` DOES get resolved onto the frontend route
    group's own ``.dependant`` — the surface is real, not hypothetical;
    (b) ``_walk_composed_routes`` reports NOTHING for it at all — not even
    a key with an empty parameter set — confirming total silence, matching
    the boundary claim exactly rather than a partial miss.
    """
    from fastapi import FastAPI

    def _mode_dep(extraction_mode: Annotated[str, Query()] = "autonomous") -> str:
        return extraction_mode

    router = APIRouter(dependencies=[Depends(_mode_dep)])
    router.frontend("/ui", directory=str(tmp_path), check_dir=False)

    app = FastAPI()
    app.include_router(router, prefix="/pfx")

    # (a) the surface is real: FastAPI DOES resolve the router-level
    # dependency onto the frontend group's own dependant.
    frontend_group = router._low_priority_routes[0]
    frontend_dependant = getattr(frontend_group, "dependant")  # noqa: B009
    assert any(d.call is _mode_dep for d in frontend_dependant.dependencies), (
        "the frontend route group unexpectedly did not inherit the "
        "router-level dependency -- this boundary's own premise is stale"
    )

    # (b) the walker is silent about it: no opaque entry, no params entry,
    # no leaked extraction-ish name anywhere.
    params, opaque = _walk_composed_routes(app)
    assert opaque == [], f"unexpected opaque entries: {opaque}"
    assert not any("/pfx/ui" in k for k in params), (
        f"walker unexpectedly discovered the frontend route -- this "
        f"boundary claim is stale, params={sorted(params)}"
    )
    assert not any("extraction" in n for names in params.values() for n in names), (
        "a frontend-mount smuggle became visible to the walker -- if so, "
        "SWEEP _low_priority_routes instead of relying on this documented boundary"
    )


def test_bp01_composed_walker_unions_wire_names_on_key_collision() -> None:
    """Argus P3, round 8 — hardening: ``params`` is keyed by
    ``f"{module}:{methods} {path}"``; two distinct discovered ``APIRoute``
    objects sharing all three components (unusual, but not forbidden by
    FastAPI — both get registered, route-matching order alone decides
    which actually serves a given request) previously OVERWROTE one
    another via plain ``params[key] = ...`` assignment, silently dropping
    the earlier one's wire-name surface with no ``opaque`` entry to flag
    the loss. Fixed by unioning onto any existing entry instead. Builds
    exactly two same-module, same-method, same-path routes with DISJOINT
    parameter names and proves BOTH survive under the single collided key.
    """
    from fastapi import FastAPI

    router = APIRouter()

    @router.get("/dup")
    async def _dup_alpha(alpha: Annotated[str, Query()] = "") -> dict[str, str]:
        return {"alpha": alpha}

    @router.get("/dup")
    async def _dup_beta(beta: Annotated[str, Query()] = "") -> dict[str, str]:
        return {"beta": beta}

    app = FastAPI()
    app.include_router(router)

    params, opaque = _walk_composed_routes(app)
    assert opaque == [], f"unexpected opaque entries: {opaque}"

    dup_keys = [k for k in params if k.endswith(" /dup")]
    assert len(dup_keys) == 1, (
        f"expected both same-module/method/path routes to collide onto exactly "
        f"one key, got {dup_keys}"
    )
    assert {"alpha", "beta"} <= params[dup_keys[0]], (
        f"key collision dropped one route's wire-name surface instead of "
        f"unioning both: {params[dup_keys[0]]}"
    )


# --- round 9 (CHANGE OF METHOD): fail-loud structural decomposition -------
#
# Every round above guarded exactly the surface Argus found (module-level
# routers -> factory-built routers -> ``Mount`` targets ->
# ``APIWebSocketRoute`` -> depth-0 unwrapped models) and the next round
# found one level deeper. The tests below pin the primitive that ends the
# treadmill: ``_models_in_annotation`` reduces EVERY annotation to either
# "contains no BaseModel" or "these BaseModels" and REDS via ``opaque`` on
# anything it cannot reduce — the same fail-loud channel that already reds
# an unrecognized route class. Model-level probe classes are deliberately
# MODULE-level (not local to a test function): this file's
# ``from __future__ import annotations`` makes annotation resolution go
# through a forward-ref eval against the DEFINING module's globals, which
# cannot see a class local to a function (see
# ``_Bp01QueryModelSmuggle``'s docstring above for the same trap, hit and
# fixed in round 8).


class _Bp01NestedInner(BaseModel):
    """Innermost probe model — the actual knob."""

    extraction_mode: str = "autonomous"


class _Bp01NestedMid(BaseModel):
    """Wraps ``_Bp01NestedInner`` behind a union (``| None``)."""

    inner: _Bp01NestedInner | None = None


class _Bp01NestedOuter(BaseModel):
    """Wraps ``_Bp01NestedMid`` behind a ``dict``-of-``list`` generic
    container, and separately wraps ``_Bp01NestedInner`` directly behind an
    ``Annotated`` wrapper unioned with ``None`` — two independent wrapped
    paths to the same knob, both must be found.
    """

    mids: dict[str, list[_Bp01NestedMid]] = Field(default_factory=dict)
    tagged: Annotated[_Bp01NestedInner, Field(description="x")] | None = None


class _Bp01Cyc(BaseModel):
    """Self-referential model — proves the ``_seen`` cycle guard actually
    terminates the walk rather than recursing forever, while still finding
    a knob reachable through a NON-cyclical field (``inner``).
    """

    me: _Bp01Cyc | None = None
    inner: _Bp01NestedInner | None = None


_Bp01Cyc.model_rebuild()

type _Bp01NestedAlias = dict[str, _Bp01NestedInner] | Literal["no-model"]


def test_bp01_model_wire_names_recurse_nested_and_wrapped_models() -> None:
    """Round 9 — the model walk finds a knob at depth>=3 through
    dict/list/union/``Annotated`` wrappers, and terminates (rather than
    recursing forever) on a self-referential model while still finding a
    knob reachable through the cycle's OTHER field.
    """
    outer_names = _model_wire_names(_Bp01NestedOuter)
    assert "extraction_mode" in outer_names, (
        f"model walk failed to recurse dict[str, list[Mid]] and "
        f"Annotated[Inner, ...] | None wrappers: {outer_names}"
    )
    assert {"mids", "tagged", "inner"} <= outer_names

    # Cycle guard: this call must simply RETURN (not hang), and must still
    # find the knob reachable through the non-cyclical ``inner`` field.
    cyc_names = _model_wire_names(_Bp01Cyc)
    assert "extraction_mode" in cyc_names, (
        f"self-referential model walk failed to find a knob reachable "
        f"through a non-cyclical field: {cyc_names}"
    )
    assert {"me", "inner"} <= cyc_names


class _Bp01RedTypedDict(TypedDict):
    """RED control: a ``TypedDict`` carrying the knob's own name — a real
    named-field surface this walker deliberately refuses to decompose."""

    extraction_mode: str


@dataclasses.dataclass
class _Bp01RedDataclass:
    """RED control: a stdlib dataclass carrying the knob's own name — see
    ``_bp01_undecomposable_body`` below for the LIVE (constructible,
    caller-facing) version of this same shape."""

    extraction_mode: str = "autonomous"


_Bp01RedTypeVar = TypeVar("_Bp01RedTypeVar")


def test_bp01_annotation_decomposer_reduces_wrappers_and_reds_on_exotics() -> None:
    """THE decomposer proof, bidirectional (vacuous-guard-shapes discipline):
    every wrapper shape reduces to the exact model set with EMPTY opaque (no
    false reds), and every exotic/undecomposable shape lands EXACTLY one
    repr-bearing opaque entry (no false greens — an unhandled shape must not
    silently pass as "no models").
    """
    _Bp01NestedInnerNT = NewType("_Bp01NestedInnerNT", _Bp01NestedInner)

    # GREEN half: every wrapper shape reduces to {_Bp01NestedInner} with a
    # clean (empty) opaque.
    green_shapes: tuple[Any, ...] = (
        Annotated[_Bp01NestedInner, "m"],
        _Bp01NestedInner | None,
        Optional[_Bp01NestedInner],  # noqa: UP045 — deliberately the typing.Optional spelling
        list[_Bp01NestedInner],
        dict[str, _Bp01NestedInner],
        tuple[_Bp01NestedInner, ...],
        _Bp01NestedInnerNT,
        _Bp01NestedAlias,
    )
    for shape in green_shapes:
        opq: list[str] = []
        found = _models_in_annotation(shape, opaque=opq)
        assert found == {_Bp01NestedInner}, (
            f"{shape!r} did not reduce to {{_Bp01NestedInner}}: {found}"
        )
        assert opq == [], f"{shape!r} unexpectedly produced opaque entries: {opq}"

    # GREEN half, no-model shapes: a clean scalar/union/literal/Any/UUID
    # correctly reduces to "no models", not to an opaque entry.
    no_model_shapes: tuple[Any, ...] = (
        str,
        int | None,
        Literal["a", "b"],
        Any,
        uuid.UUID,
    )
    for shape in no_model_shapes:
        opq = []
        found = _models_in_annotation(shape, opaque=opq)
        assert found == set(), f"{shape!r} unexpectedly found model(s): {found}"
        assert opq == [], f"{shape!r} unexpectedly produced opaque entries: {opq}"

    # RED half: each exotic/undecomposable shape must produce EXACTLY one
    # opaque entry whose repr names the shape — not silently "no models".
    red_shapes: tuple[Any, ...] = (
        _Bp01RedTypeVar,
        _Bp01RedTypedDict,
        _Bp01RedDataclass,
        "NotAResolvedType",
    )
    for shape in red_shapes:
        opq = []
        found = _models_in_annotation(shape, opaque=opq)
        assert found == set(), f"undecomposable shape {shape!r} must never yield a model: {found}"
        assert len(opq) == 1, f"expected exactly one opaque entry for {shape!r}, got {opq}"
        assert repr(shape) in opq[0], (
            f"opaque entry must name the undecomposable shape's repr: {opq[0]!r} vs {shape!r}"
        )


class _Bp01NestedBodyInner(BaseModel):
    extraction_mode: str = "autonomous"


class _Bp01NestedBodyMid(BaseModel):
    inner: _Bp01NestedBodyInner | None = None


class _Bp01NestedBodyWrap(BaseModel):
    """Depth-3 smuggle: ``payload`` (depth 0) -> ``items`` (a ``list``,
    depth 1) -> ``_Bp01NestedBodyMid.inner`` (a union, depth 2) ->
    ``_Bp01NestedBodyInner.extraction_mode`` (depth 3)."""

    items: list[_Bp01NestedBodyMid] = Field(default_factory=list)


class _Bp01NestedBodyInnerClean(BaseModel):
    topic: str = ""


class _Bp01NestedBodyMidClean(BaseModel):
    inner: _Bp01NestedBodyInnerClean | None = None


class _Bp01NestedBodyWrapClean(BaseModel):
    """Clean twin of ``_Bp01NestedBodyWrap`` — same nesting shape, innocuous
    field names."""

    items: list[_Bp01NestedBodyMidClean] = Field(default_factory=list)


def test_bp01_composed_walker_detects_a_nested_model_smuggle() -> None:
    """Round 9 — walker-level RED/GREEN control: a knob nested at depth 3
    behind a ``list`` AND a union, on a composed app reached through
    ``include_router(prefix=...)``, is detected; a same-shape clean twin
    proves no false positive.
    """
    from fastapi import FastAPI

    router = APIRouter()

    @router.post("/wrap")
    async def _wrap(payload: _Bp01NestedBodyWrap) -> dict[str, Any]:
        return payload.model_dump()

    @router.post("/wrap-clean")
    async def _wrap_clean(payload: _Bp01NestedBodyWrapClean) -> dict[str, Any]:
        return payload.model_dump()

    app = FastAPI()
    app.include_router(router, prefix="/pfx")

    params, opaque = _walk_composed_routes(app)
    assert opaque == [], f"unexpected opaque entries: {opaque}"

    smuggle_keys = [k for k in params if k.endswith(" /pfx/wrap")]
    assert smuggle_keys, f"discovery missed the nested-model route; keys were {sorted(params)}"
    knobs = _extraction_knob_fields()
    assert (params[smuggle_keys[0]] & knobs) == {"extraction_mode"}, (
        f"walker failed to detect a depth-3 list+union nested model smuggle: "
        f"{params[smuggle_keys[0]]}"
    )

    clean_keys = [k for k in params if k.endswith(" /pfx/wrap-clean")]
    assert clean_keys, f"discovery missed the clean nested-model route; keys were {sorted(params)}"
    assert (params[clean_keys[0]] & knobs) == set()
    assert not any("extraction" in n for n in params[clean_keys[0]]), (
        f"clean nested-model route unexpectedly named an extraction-ish field: "
        f"{params[clean_keys[0]]}"
    )


class _Bp01DepInner(BaseModel):
    extraction_mode: str = "autonomous"


class _Bp01DepWrap(BaseModel):
    inner: _Bp01DepInner | None = None


class _Bp01DepInnerClean(BaseModel):
    topic: str = ""


class _Bp01DepWrapClean(BaseModel):
    inner: _Bp01DepInnerClean | None = None


async def _bp01_wrapped_depends_smuggle(
    payload: Annotated[_Bp01DepWrap | None, Body()] = None,
) -> _Bp01DepWrap | None:
    return payload


async def _bp01_wrapped_depends_clean(
    payload: Annotated[_Bp01DepWrapClean | None, Body()] = None,
) -> _Bp01DepWrapClean | None:
    return payload


def test_bp01_composed_walker_detects_a_wrapped_depends_model_smuggle() -> None:
    """Round 9 — a ``Depends(...)`` PARAMETER (not its return annotation —
    see ``_dependant_wire_names``'s docstring for why the return side is a
    deliberate, documented non-sweep) typed ``Annotated[Wrap | None,
    Body()]``, where ``Wrap`` nests a model carrying the knob, composes
    Depends + Annotated + union + nesting in one shape. Clean twin proves no
    false positive.
    """
    from fastapi import FastAPI

    smuggled_router = APIRouter(dependencies=[Depends(_bp01_wrapped_depends_smuggle)])

    @smuggled_router.post("/probe")
    async def _probe(prompt: Annotated[str, Query()] = "") -> dict[str, str]:
        return {"prompt": prompt}

    clean_router = APIRouter(dependencies=[Depends(_bp01_wrapped_depends_clean)])

    @clean_router.post("/probe")
    async def _probe_clean(prompt: Annotated[str, Query()] = "") -> dict[str, str]:
        return {"prompt": prompt}

    smuggled_app = FastAPI()
    smuggled_app.include_router(smuggled_router, prefix="/pfx")

    params, opaque = _walk_composed_routes(smuggled_app)
    assert opaque == [], f"unexpected opaque entries: {opaque}"

    probe_keys = [k for k in params if k.endswith(" /pfx/probe")]
    assert probe_keys, (
        f"discovery missed the wrapped-Depends-model route; keys were {sorted(params)}"
    )
    knobs = _extraction_knob_fields()
    assert (params[probe_keys[0]] & knobs) == {"extraction_mode"}, (
        f"walker failed to detect a Depends-parameter Annotated+union+nested "
        f"model smuggle: {params[probe_keys[0]]}"
    )
    assert "prompt" in params[probe_keys[0]], (
        "the endpoint's own parameter must still be found (union, not replace)"
    )

    clean_app = FastAPI()
    clean_app.include_router(clean_router, prefix="/pfx")
    clean_params, clean_opaque = _walk_composed_routes(clean_app)
    assert clean_opaque == []
    clean_keys = [k for k in clean_params if k.endswith(" /pfx/probe")]
    assert clean_keys
    assert (clean_params[clean_keys[0]] & knobs) == set()
    assert not any("extraction" in n for n in clean_params[clean_keys[0]]), (
        f"clean wrapped-Depends-model route unexpectedly named an "
        f"extraction-ish field: {clean_params[clean_keys[0]]}"
    )


@dataclasses.dataclass
class _Bp01UndecomposableBody:
    """A plain stdlib dataclass, NOT a pydantic ``BaseModel`` — FastAPI
    officially supports dataclass request bodies (verified empirically: the
    route built from this class below CONSTRUCTS and would SERVE), so this
    is a real, reachable shape, not a hypothetical. Carries the knob's own
    name so a walker that silently treated "not a BaseModel" as "no models"
    would stay blind to it."""

    extraction_mode: str = "autonomous"


def test_bp01_composed_walker_reds_on_an_undecomposable_annotation() -> None:
    """THE BAR (round 9): an annotation shape the decomposer cannot reduce
    must RED the guard, not pass silently. A dataclass body parameter is
    exactly that shape — see ``_Bp01UndecomposableBody``'s docstring and
    ``_models_in_annotation``'s branch (5).

    The CONTROL proving the real tree is not merely "this test's shape
    happens to red" but "the real composed app fully decomposes" is
    ``test_bp01_composed_app_routes_cannot_name_an_extraction_knob``'s own
    ``assert opaque == []`` on the actual ``create_app()`` output — that
    test is the passing control for this one's red.
    """
    from fastapi import FastAPI

    router = APIRouter()

    @router.post("/undecomposable")
    async def _undecomposable(payload: _Bp01UndecomposableBody) -> dict[str, str]:
        return {"extraction_mode": payload.extraction_mode}

    app = FastAPI()
    app.include_router(router, prefix="/pfx")

    params, opaque = _walk_composed_routes(app)
    assert opaque, (
        "an undecomposable annotation (a dataclass body param carrying "
        "extraction_mode) must RED the guard, not pass silently -- an "
        "unknown shape must never be treated as safe"
    )
    matches = [
        entry
        for entry in opaque
        if "/pfx/undecomposable" in entry and "_Bp01UndecomposableBody" in entry
    ]
    assert matches, (
        f"opaque entry must name both the route and the undecomposable "
        f"shape's repr, so triage is immediate: {opaque}"
    )

    # The wire-name surface itself must NOT silently contain the knob: the
    # decomposer's refusal must show up as a RED, not as a (lucky) find —
    # proving the shape is genuinely blind to the walker, not merely
    # also-flagged.
    undecomposable_key = next(k for k in params if k.endswith(" /pfx/undecomposable"))
    assert "extraction_mode" not in params[undecomposable_key], (
        "the walker must never 'find' a knob inside a shape it could not "
        f"decompose: {params[undecomposable_key]}"
    )
