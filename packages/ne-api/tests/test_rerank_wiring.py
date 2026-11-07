# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""P2-19: the composition root wires the SPEC §3.1-mandated rerank stage.

``build_platform`` wraps the ONE acl-trimmed ``HybridRetriever`` in
``RetrievalPipeline(HybridRetriever, TEIReranker(tei_rerank_url),
candidate_multiplier=4, max_candidate_k=RERANK_CANDIDATE_CEILING)`` BY DEFAULT
(rerank-on) for BOTH retrieval bindings (the
chat tool + the /v1/brain routes, which share the ONE composed retriever). An
empty rerank URL degrades LOUDLY to the bare retriever (logged warning); an
external/exfil-shaped URL fails LOUD at boot (C2).
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import httpx
import pytest

from ne.api import AppSeams
from ne.api.platform import RERANK_CANDIDATE_CEILING, build_platform
from ne.api.settings import ApiSettings
from ne.api.tools import CurrentSourceAuthorizedKnowledgeRead
from ne.audit.config import AuditSettings
from ne.brain.pipeline import RetrievalPipeline
from ne.brain.reranker import TEIReranker
from ne.brain.retriever import HybridRetriever
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Lane
from ne.gateway.config import GatewaySettings, ModelDef
from ne.identity.config import IdentitySettings, OidcConfig
from ne.permissions.config import PermissionsSettings
from ne.runtime.config import RuntimeSettings

from .conftest import (
    AUDIENCE,
    ISSUER,
    AppBuilder,
    InMemoryFilteredChunkStore,
    RegoFaithfulPdpFactory,
    ScriptedGateway,
    StaticEmbedder,
    StaticSparseEncoder,
    chunk,
    parse_sse,
    run,
    text_completion,
    tool_call_completion,
)


def _sqlite_seams(tmp_path: Path) -> AppSeams:
    """Seams over tmp SQLite with stub edges but NO reranker seam — so the DEFAULT
    composition constructs the REAL ``TEIReranker`` from the settings URL."""
    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", RegoFaithfulPdpFactory)
    runtime_url = f"sqlite:///{tmp_path}/runtime-{uuid.uuid4().hex}.db"
    audit_url = f"sqlite:///{tmp_path}/audit-{uuid.uuid4().hex}.db"
    perms_url = f"sqlite:///{tmp_path}/perms-{uuid.uuid4().hex}.db"
    return AppSeams(
        identity_settings=IdentitySettings(
            verifier="fake",
            oidc=OidcConfig(issuer=ISSUER, audience=AUDIENCE),
            principal_claims=("email", "preferred_username", "name", "department"),
        ),
        runtime_settings=RuntimeSettings(session_database_url=runtime_url),
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
        gateway=ScriptedGateway(),
        embedder=StaticEmbedder(),
        sparse_encoder=StaticSparseEncoder(),
        chunk_store=InMemoryFilteredChunkStore(),
        plugin_registry=registry,
    )


def _api_settings(**overrides: object) -> ApiSettings:
    base: dict[str, object] = {
        "qdrant_url": "http://qdrant.test",
        "qdrant_collection": "t",
        "tei_url": "http://tei.test",
    }
    base.update(overrides)
    return ApiSettings(**base)  # type: ignore[arg-type]


def test_embed_timeout_env_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # NE-DEMO-FIX1: _default_embedder honors NE_TEI_EMBED_TIMEOUT_S, defaulting
    # to the shipped 30s prod value when unset. Uses a dotless in-VPC tei_url
    # (unlike the module default "http://tei.test") so construction's C2 SSRF
    # validation accepts it directly (no reranker allowed_hosts precedent here).
    from ne.api.platform import _default_embedder
    from ne.brain.embedder import TEIEmbedder

    settings = _api_settings(tei_url="http://tei:80")

    monkeypatch.delenv("NE_TEI_EMBED_TIMEOUT_S", raising=False)
    embedder = _default_embedder(settings)
    assert isinstance(embedder, TEIEmbedder)
    assert embedder._timeout == 30.0

    monkeypatch.setenv("NE_TEI_EMBED_TIMEOUT_S", "180")
    embedder = _default_embedder(settings)
    assert isinstance(embedder, TEIEmbedder)
    assert embedder._timeout == 180.0


def test_rerank_timeout_env_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # NE-DEMO-FIX1: _default_reranker mirrors the embedder's escape hatch via
    # NE_TEI_RERANK_TIMEOUT_S; the unset default stays 30.0 (prod unchanged) and
    # construction still yields a real TEIReranker with C2 URL validation.
    from ne.api.platform import _default_reranker

    monkeypatch.delenv("NE_TEI_RERANK_TIMEOUT_S", raising=False)
    reranker = _default_reranker(_api_settings())
    assert isinstance(reranker, TEIReranker)
    assert reranker._timeout == 30.0

    monkeypatch.setenv("NE_TEI_RERANK_TIMEOUT_S", "180")
    reranker = _default_reranker(_api_settings())
    assert isinstance(reranker, TEIReranker)
    assert reranker._timeout == 180.0


def test_default_composition_wires_retrieval_pipeline_with_tei_reranker(tmp_path: Path) -> None:
    seams = _sqlite_seams(tmp_path)
    platform = build_platform(_api_settings(), seams)

    # The ONE composed retriever is the RetrievalPipeline (NOT a bare retriever),
    # wrapping the REAL TEIReranker at the default in-cluster endpoint.
    assert isinstance(platform.retriever, RetrievalPipeline)
    assert isinstance(platform.retriever._reranker, TEIReranker)
    assert platform.retriever._candidate_multiplier == 4
    assert platform.retriever._max_candidate_k == RERANK_CANDIDATE_CEILING
    assert platform.retriever._reranker._base_url == "http://tei-rerank:80"

    for engine in platform.engines:
        engine.dispose()


def test_the_composed_ask_path_fans_out_to_forty_candidates_not_one_hundred_sixty(
    tmp_path: Path,
) -> None:
    """NE-RERANK-FANOUT at the composition root — the two over-fetches reconciled.

    ``CurrentSourceAuthorizedKnowledgeRead`` over-fetches for authorization and
    ``RetrievalPipeline`` over-fetches for the reranker. Composed with the old
    constants they multiplied: ``retrieval_top_k=5`` reranked ``5 * 8 * 4 = 160``
    documents in 5 sequential TEI calls (batch size 32), measured at
    ``rerank_ms=227271`` — 98.9% of a 229 s search. This pins the product the
    composition root actually builds, which is the only place both halves are
    visible at once.
    """
    seams = _sqlite_seams(tmp_path)
    platform = build_platform(_api_settings(retrieval_top_k=5), seams)

    assert isinstance(platform.retriever, RetrievalPipeline)
    assert isinstance(platform.current_retriever, CurrentSourceAuthorizedKnowledgeRead)
    window = 5 * platform.current_retriever._scan_multiplier
    pool = min(window * platform.retriever._candidate_multiplier, RERANK_CANDIDATE_CEILING)
    assert (window, pool) == (10, 40)
    # Both over-fetches survive: the reranker still sees more than it emits, and
    # authorization still sees more rows than the page it fills.
    assert pool > window > 5

    for engine in platform.engines:
        engine.dispose()


def test_external_rerank_url_fails_loud_at_boot(tmp_path: Path) -> None:
    # C2: a set-but-external/exfil-shaped URL fails LOUD at construction
    # (UnsafeEndpointError is a ValueError subclass) — never a runtime POST.
    seams = _sqlite_seams(tmp_path)
    with pytest.raises(Exception, match=r"external|not permitted"):
        build_platform(_api_settings(tei_rerank_url="http://evil.example.com"), seams)


def test_empty_rerank_url_degrades_to_bare_retriever_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seams = _sqlite_seams(tmp_path)
    with caplog.at_level(logging.WARNING, logger="ne.api.platform"):
        platform = build_platform(_api_settings(tei_rerank_url=""), seams)

    assert isinstance(platform.retriever, HybridRetriever)
    assert any(
        "rerank" in r.message.lower()
        for r in caplog.records
        if r.name == "ne.api.platform" and r.levelno == logging.WARNING
    )

    for engine in platform.engines:
        engine.dispose()


def test_disabled_rerank_keeps_the_full_authorization_fanout(tmp_path: Path) -> None:
    """The reduced scan multiplier bounds RERANK work — none is being performed here.

    NE-RERANK-FANOUT cut the authorization over-fetch from 8x to 2x so the two
    stacked over-fetches reconcile to the documented 40-candidate rerank pool.
    With ``tei_rerank_url`` empty (the live configuration once CPU reranking is
    removed) the pipeline is a bare ``HybridRetriever`` — no rerank stage, no
    rerank cost to bound — yet the wrapper inherited the smaller fan-out: a
    ``top_k=5`` read scanned 10 rows and could return a short page where the
    pre-fanout 40-row scan filled it (measured: a 75%-denial evaluator returns
    3 chunks vs 5). The bare path must keep the original headroom.
    """
    seams = _sqlite_seams(tmp_path)
    platform = build_platform(_api_settings(tei_rerank_url="", retrieval_top_k=5), seams)

    assert isinstance(platform.retriever, HybridRetriever)
    assert isinstance(platform.current_retriever, CurrentSourceAuthorizedKnowledgeRead)
    # The default Ask scans the pre-NE-RERANK-FANOUT 40 rows again, so a
    # heavily-denied page can still refill — there is no rerank pool for that
    # width to compound with.
    assert platform.current_retriever._scan_multiplier == 8
    assert 5 * platform.current_retriever._scan_multiplier == 40
    # ...and the value is the composition root's named constant, not a magic 8.
    from ne.api.platform import BARE_RETRIEVER_SCAN_MULTIPLIER

    assert platform.current_retriever._scan_multiplier == BARE_RETRIEVER_SCAN_MULTIPLIER

    for engine in platform.engines:
        engine.dispose()


# --- NE-SEARCH-LATENCY: the vllm rerank backend + allowed-hosts threading -----


def test_vllm_backend_composes_vllm_reranker(tmp_path: Path) -> None:
    """``rerank_backend="vllm"`` swaps the SERVER adapter inside the SAME
    RetrievalPipeline composition — multiplier, URL semantics untouched."""
    from ne.brain.reranker import VllmReranker

    seams = _sqlite_seams(tmp_path)
    platform = build_platform(
        _api_settings(rerank_backend="vllm", tei_rerank_url="http://rerank-gpu:80"), seams
    )

    assert isinstance(platform.retriever, RetrievalPipeline)
    assert isinstance(platform.retriever._reranker, VllmReranker)
    assert platform.retriever._candidate_multiplier == 4
    assert platform.retriever._reranker._base_url == "http://rerank-gpu:80"

    for engine in platform.engines:
        engine.dispose()


def test_rerank_allowed_hosts_threads_into_both_adapters() -> None:
    """The C2 allowlist must actually REACH adapter construction (both backends).

    Mutation-sensitive in the direction that matters: if ``_default_reranker``
    stops passing ``allowed_hosts=`` (the kwarg this unit adds), the allowlisted
    external host below raises ``UnsafeEndpointError`` and this test goes RED.
    The no-allowlist control right after proves the allowlist is what admits the
    host — not a loosened validator.
    """
    from ne.api.platform import _default_reranker
    from ne.brain.reranker import VllmReranker
    from ne.brain.sovereignty import UnsafeEndpointError

    external = "http://8.8.8.8:1837"

    tei = _default_reranker(
        _api_settings(tei_rerank_url=external, rerank_allowed_hosts=("8.8.8.8",))
    )
    assert isinstance(tei, TEIReranker)
    assert tei._base_url == external

    vllm = _default_reranker(
        _api_settings(
            rerank_backend="vllm",
            tei_rerank_url=external,
            rerank_allowed_hosts=("8.8.8.8",),
        )
    )
    assert isinstance(vllm, VllmReranker)
    assert vllm._base_url == external

    # Control (fail-closed direction): the SAME external URL without the
    # allowlist is refused at construction, for both backends.
    with pytest.raises(UnsafeEndpointError):
        _default_reranker(_api_settings(tei_rerank_url=external))
    with pytest.raises(UnsafeEndpointError):
        _default_reranker(_api_settings(rerank_backend="vllm", tei_rerank_url=external))


def test_vllm_backend_threads_served_model_name_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ne.api.platform import _default_reranker
    from ne.brain.reranker import VllmReranker

    monkeypatch.setenv("NE_TEI_RERANK_TIMEOUT_S", "180")
    reranker = _default_reranker(
        _api_settings(
            rerank_backend="vllm",
            tei_rerank_url="http://rerank-gpu:80",
            rerank_served_model_name="lane-x",
        )
    )
    assert isinstance(reranker, VllmReranker)
    assert reranker._served_model_name == "lane-x"
    assert reranker._timeout == 180.0


# --- NE-SEARCH-LATENCY P0: the composed reranker is dominated by AIR_GAP -------


def test_composed_reranker_carries_the_live_air_gap_posture(tmp_path: Path) -> None:
    """The wiring the P0 fix turns on, asserted at the seam that matters.

    The rerank adapter's air-gap term must be the SAME live register the gateway
    reads — not a boot-time snapshot — because the platform's air gap is flipped
    at RUNTIME by a governed action against a durable posture row, in a process
    whose reranker was constructed at boot. Here the composed adapter is built
    with the gap open (so it constructs at all), the register is then flipped
    exactly as ``write.internal.network.egress.restrict`` flips it, and the
    ALREADY-CONSTRUCTED adapter must refuse to send.
    """
    from ne.brain.reranker import VllmReranker
    from ne.net.airgap import AirGapViolationError

    seams = _sqlite_seams(tmp_path)
    platform = build_platform(
        _api_settings(
            rerank_backend="vllm",
            tei_rerank_url="http://8.8.8.8:1837",
            rerank_allowed_hosts=("8.8.8.8",),
        ),
        seams,
    )
    try:
        assert isinstance(platform.retriever, RetrievalPipeline)
        reranker = platform.retriever._reranker
        assert isinstance(reranker, VllmReranker)
        assert reranker._base_url == "http://8.8.8.8:1837"
        # CONTROL: with the gap open the composed adapter's guard permits the host.
        assert platform.egress_store is not None
        assert reranker._air_gap is not None
        assert reranker._air_gap() is False

        platform.egress_store.apply(
            air_gap=True, actor="ops@example.com", via_action="test.restrict"
        )

        assert reranker._air_gap() is True
        with pytest.raises(AirGapViolationError, match="AIR_GAP is engaged"):
            run(reranker.rerank("q", [chunk("c0", acl=("all-employees",))], top_k=1))
    finally:
        for engine in platform.engines:
            engine.dispose()


def test_composed_tei_reranker_sends_nothing_once_the_air_gap_is_engaged(
    tmp_path: Path,
) -> None:
    """The default backend is threaded identically — the allowlist is a property
    of the SETTING, not of the vLLM adapter, so both adapters must be covered.

    Asserted BEHAVIOURALLY, at the wire. An earlier version of this test only
    checked that ``reranker._air_gap()`` flipped to ``True``; that asserts the
    posture callable is live, which is a property of ``build_platform``'s
    threading alone. It passed with ``TEIReranker.rerank``'s per-request
    ``assert_air_gap_permits`` deleted outright — the composed adapter went on
    POSTing chunk text to the allowlisted public IP and nothing went red. The
    claim that matters is not "the adapter can see the gap" but "zero bytes
    leave", so this records outbound requests through an injected transport and
    asserts none is made after the flip.

    CONTROL first: the SAME composed adapter, over the SAME transport, with the
    gap still open, does send — so the silence after the flip is the air gap and
    not a broken client, a bad endpoint, or a reranker that never ran.
    """
    from ne.net.airgap import AirGapViolationError

    recorded: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json=[{"index": 0, "score": 0.9}])

    seams = _sqlite_seams(tmp_path)
    platform = build_platform(
        _api_settings(tei_rerank_url="http://8.8.8.8:1837", rerank_allowed_hosts=("8.8.8.8",)),
        seams,
    )
    try:
        assert isinstance(platform.retriever, RetrievalPipeline)
        reranker = platform.retriever._reranker
        assert isinstance(reranker, TEIReranker)
        assert reranker._air_gap is not None
        assert reranker._air_gap() is False
        # Inject the recording transport into the ALREADY-COMPOSED adapter, so what
        # is under test is the object build_platform actually wired in.
        reranker._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        corpus = "SENTINEL-COMPOSED-CHUNK-4b7e"
        # CONTROL: gap open ⇒ the composed adapter really does egress.
        run(reranker.rerank("q", [chunk("c0", acl=("all-employees",), text=corpus)], top_k=1))
        assert len(recorded) == 1, "control: the composed adapter must send with the gap open"

        assert platform.egress_store is not None
        platform.egress_store.apply(
            air_gap=True, actor="ops@example.com", via_action="test.restrict"
        )
        assert reranker._air_gap() is True

        with pytest.raises(AirGapViolationError, match="AIR_GAP is engaged"):
            run(reranker.rerank("q", [chunk("c1", acl=("all-employees",), text=corpus)], top_k=1))

        # THE assertion: not "it raised", but "nothing left the process".
        assert len(recorded) == 1, "a request left the composed adapter under an engaged air gap"
        assert corpus not in "".join(r.read().decode() for r in recorded[1:])
    finally:
        for engine in platform.engines:
            engine.dispose()


def test_boot_under_an_engaged_air_gap_refuses_an_allowlisted_public_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Construction-time direction, through the REAL composition root: a sealed
    deployment cannot boot an external rerank lane at all."""
    from ne.brain.sovereignty import UnsafeEndpointError

    monkeypatch.setenv("AIR_GAP", "1")
    seams = _sqlite_seams(tmp_path)
    with pytest.raises(UnsafeEndpointError, match="AIR_GAP is engaged"):
        build_platform(
            _api_settings(
                rerank_backend="vllm",
                tei_rerank_url="http://8.8.8.8:1837",
                rerank_allowed_hosts=("8.8.8.8",),
            ),
            seams,
        )


def test_sealed_boot_still_composes_the_on_box_rerank_lane(tmp_path: Path) -> None:
    """CONTROL for the boot refusal: an air-gapped deployment's in-VPC rerank
    lane composes normally. Without this the test above would pass if AIR_GAP
    simply broke rerank composition outright."""
    import os

    os.environ["AIR_GAP"] = "1"
    try:
        seams = _sqlite_seams(tmp_path)
        platform = build_platform(_api_settings(tei_rerank_url="http://tei-rerank:80"), seams)
        assert isinstance(platform.retriever, RetrievalPipeline)
        assert isinstance(platform.retriever._reranker, TEIReranker)
        for engine in platform.engines:
            engine.dispose()
    finally:
        del os.environ["AIR_GAP"]


def test_rerank_failures_map_to_a_field_only_502_not_an_opaque_500() -> None:
    """C12: both ways the rerank stage refuses must reach the brain router's
    backend-error map. ``RerankResponseError`` is a ``ValueError`` subclass and
    was NOT in ``_BACKEND_ERRORS``, so a malformed rerank response escaped as an
    uncaught 500; ``AirGapViolationError`` is a ``RuntimeError`` and did the same.
    The wire detail is the class NAME only — no URL, no field value, no repr."""
    from ne.api.errors import _BACKEND_ERRORS
    from ne.brain.reranker import RerankResponseError
    from ne.net.airgap import AirGapViolationError

    assert issubclass(RerankResponseError, _BACKEND_ERRORS)
    assert issubclass(AirGapViolationError, _BACKEND_ERRORS)
    # CONTROL: the map stays narrow — a bare ValueError/RuntimeError must NOT be
    # swept into a 502 (that would turn genuine bugs into "backend fault").
    assert not issubclass(ValueError, _BACKEND_ERRORS)
    assert not issubclass(RuntimeError, _BACKEND_ERRORS)


def test_chat_binding_rides_reranked_pipeline(make_app: AppBuilder) -> None:
    # The governed chat tool path (read.knowledge.search) executes the rerank stage.
    h = make_app(
        completions=[
            tool_call_completion("read.knowledge.search", {"query": "q"}),
            text_completion("answer"),
        ],
        chunks=[chunk("c1", acl=["eng"], text="alpha")],
    )

    async def _t() -> None:
        headers = h.bearer("user:ada", groups=["eng"], department="engineering")
        async with h.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions", json={"agent_name": "console-chat"}, headers=headers
            )
            assert created.status_code == 200
            sid = created.json()["session_id"]
            r = await client.post(
                f"/api/v1/chat/sessions/{sid}/messages", json={"prompt": "hi"}, headers=headers
            )
            assert r.status_code == 200
            done = parse_sse(r.text).of_type("done")[0]
            assert done.data is not None
            assert done.data["output"] == "answer"

    run(_t())
    assert h.reranker.calls >= 1


def test_brain_search_route_rides_reranked_pipeline(make_app: AppBuilder) -> None:
    # The /v1/brain search binding path reranks (the SAME composed retriever).
    h = make_app(chunks=[chunk("c1", acl=["eng"], text="alpha")])

    async def _t() -> None:
        headers = h.bearer("user:ada", groups=["eng"], department="engineering")
        async with h.client() as client:
            r = await client.post(
                "/api/v1/brain/search", json={"query": "alpha", "top_k": 5}, headers=headers
            )
            assert r.status_code == 200

    run(_t())
    assert h.reranker.calls >= 1


def test_rerank_scoring_char_limit_threads_into_both_adapters() -> None:
    """NE-RERANK-CPU: the scoring view is a STAGE property, not a server property.

    Mutation-sensitive both ways: drop the kwarg from either branch of
    ``_default_reranker`` and the corresponding assertion goes RED, and the
    default-off control below goes RED if someone hardcodes a limit.
    """
    from ne.api.platform import _default_reranker
    from ne.brain.reranker import VllmReranker

    tei = _default_reranker(_api_settings(rerank_scoring_char_limit=600))
    assert isinstance(tei, TEIReranker)
    assert tei._scoring_char_limit == 600

    vllm = _default_reranker(_api_settings(rerank_backend="vllm", rerank_scoring_char_limit=600))
    assert isinstance(vllm, VllmReranker)
    assert vllm._scoring_char_limit == 600

    # Default is OFF for both — full chunk text is what a cross-encoder scores
    # unless a deployment explicitly decides otherwise.
    default_tei = _default_reranker(_api_settings())
    assert isinstance(default_tei, TEIReranker)
    assert default_tei._scoring_char_limit is None
    default_vllm = _default_reranker(_api_settings(rerank_backend="vllm"))
    assert isinstance(default_vllm, VllmReranker)
    assert default_vllm._scoring_char_limit is None


def test_rerank_scoring_char_limit_rejects_a_non_positive_value_at_settings_boot() -> None:
    import pydantic

    for bad in (0, -1):
        with pytest.raises(pydantic.ValidationError):
            _api_settings(rerank_scoring_char_limit=bad)
