# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO non-vacuity gate: the full scripted arc over a fixture-mode ne-api.

No docker, no network. Real governed fixture-sync seeding (C7 KnowledgeWriter),
then the full narrated ladder over a fixture-mode ne-api (real OIDC/PDP/C6
executor/DurableApprovalGate/audit + real oxigraph T3) — asserting governed
response shapes, the ACL permission trap, T2 degrade + auto-light-up, a REAL
verified answer, deny-means-deny, a REAL HITL release, and the real
``ne audit verify --json`` returning valid over the session's audit store.
"""

from __future__ import annotations

import functools
import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import sqlalchemy as sa
from click.testing import CliRunner

from ne.audit.store import ne_audit_log
from ne.cli import main
from ne.cli.demo.arc import (
    _DELEGATION_DEGRADE_LABEL,
    _GROUNDED_DEGRADE_LABEL,
    _HITL_DEGRADE_LABEL,
    _T3_DEGRADE_LABEL,
    ARC_STEP_COUNT,
    CRM_WRITEBACK_ACTION,
    ArcProgressCallback,
    SseFrame,
    StepStatus,
    _done_verified_template_id,
    _grounded_counts_verdict,
    _t3_verified_status,
    _writeback_prompt,
    run_demo_arc,
)
from ne.cli.demo.corpus import (
    G_QUESTION,
    G_REFUSE_PROMPT,
    build_scenario,
    demo_doc_subset,
    export_connector_fixture,
)
from ne.cli.demo.dossier import render_dossier
from ne.cli.demo.live import DEMO_VERIFIED_MAX_RESULT_TOKENS
from ne.govern.grounding import PROFILE_NG1_FROZEN, PROFILE_NG2
from ne.govern.grounding_policy import GateProfile, StrictnessTier, apply_strictness

from .demo_fixture_harness import (
    _ASSIST_JUNK,
    _HALLUCINATED_DRAFT,
    WRITEBACK_ACTION,
    FakeCrmClient,
    FakeDraftClient,
    FakeTicketClient,
    _grounded_draft,
    build_demo_app,
    get_state,
    run,
)


def test_t3_verified_status_never_over_claims() -> None:
    """The T3 step is OK only when BOTH the governed invoke AND the chat leg confirm;
    a passing invoke with an unconfirmed chat leg is honestly DEGRADED, never OK
    (Codex P2 — the dossier must not imply the chat leg succeeded)."""
    assert _t3_verified_status(invoke_ok=True, done_frame_template_id=True) is StepStatus.OK
    # governed invoke passed, conversational chat leg did not confirm → honest DEGRADED
    assert _t3_verified_status(invoke_ok=True, done_frame_template_id=False) is StepStatus.DEGRADED
    # the governed invoke is the authoritative proof — its failure is a hard FAIL
    assert _t3_verified_status(invoke_ok=False, done_frame_template_id=True) is StepStatus.FAILED
    assert _t3_verified_status(invoke_ok=False, done_frame_template_id=False) is StepStatus.FAILED


# --- NE-GA-G7: the grounded_pass leg's structural-vs-count-check split --------


def test_grounded_counts_verdict_live_phrasing_is_structural_ok() -> None:
    """Unparseable live phrasing (the fixture-strict regex finds nothing) must
    NEVER degrade or fail a passing structural verdict — it is honestly OK with
    an explicit 'not machine-checked' label, and zero counts were checked."""
    expected = {"Acme Corp": 3, "Globex": 2}
    status, label, evidence = _grounded_counts_verdict({}, expected)
    assert status is StepStatus.OK
    assert label == "counts not machine-checked (live phrasing)"
    assert evidence["counts_checked"] == 0
    assert evidence["counts_machine_checked"] is False


def test_grounded_counts_verdict_fixture_exact_match_ok() -> None:
    """A full fixture-shaped parse matching the arc's own recompute keeps the
    exact-count teeth: OK, no label, counts_matched True."""
    expected = {"Acme Corp": 3, "Globex": 2}
    parsed = {"Acme Corp": 3, "Globex": 2}
    status, label, evidence = _grounded_counts_verdict(parsed, expected)
    assert status is StepStatus.OK
    assert label == ""
    assert evidence["counts_checked"] == 2
    assert evidence["counts_machine_checked"] is True
    assert evidence["counts_matched"] is True


def test_grounded_counts_verdict_contradiction_fails() -> None:
    """A parsed line contradicting the arc's own recompute is a real FAIL — the
    additional check is not vacuous just because it forgives non-coverage."""
    expected = {"Acme Corp": 3, "Globex": 2}
    parsed = {"Acme Corp": 99, "Globex": 2}
    status, _label, evidence = _grounded_counts_verdict(parsed, expected)
    assert status is StepStatus.FAILED
    assert evidence["counts_checked"] == 2
    assert evidence["counts_machine_checked"] is True
    assert evidence["counts_matched"] is False


def test_grounded_counts_verdict_partial_parse_is_ok() -> None:
    """A matching SUBSET of accounts parsed (live phrasing may not surface
    every line) never FAILs solely from incomplete phrasing coverage — OK with
    a 'partially machine-checked' label, counts_matched False."""
    expected = {"Acme Corp": 3, "Globex": 2}
    parsed = {"Acme Corp": 3}
    status, label, evidence = _grounded_counts_verdict(parsed, expected)
    assert status is StepStatus.OK
    assert label == "counts partially machine-checked (live phrasing)"
    assert evidence["counts_checked"] == 1
    assert evidence["counts_machine_checked"] is True
    assert evidence["counts_matched"] is False


def _seed_corpus(h, tmp_path: Path, doc_ids) -> int:
    """Seed the demo corpus through the EXISTING governed fixture-sync path (C7)."""
    from ne.acme.cli import default_fixtures_dir
    from ne.connect.fixture_sync import FixtureSyncSettings, run_fixture_sync
    from ne.connect.runner import ConnectSyncSettings, build_ingest_stack

    fixture_json = tmp_path / "acme-fixture.json"
    written_records = export_connector_fixture(
        default_fixtures_dir(), fixture_json, doc_ids=doc_ids
    )
    assert written_records == len(doc_ids)

    cs = ConnectSyncSettings(
        state_path=str(tmp_path / "state.db"),
        quarantine_path=str(tmp_path / "quarantine.db"),
    )
    engine = sa.create_engine(h.runtime_url)
    try:
        stack = build_ingest_stack(
            chunk_store=h.chunk_store,
            runtime_engine=engine,
            sink=get_state(h.app).sink,
            settings=cs,
        )
        settings = FixtureSyncSettings(path=str(fixture_json), owner="demo-owner")
        outcome = run(
            run_fixture_sync(
                settings,
                writer=stack.writer,
                state_store=stack.state_store,
                audit_sink=get_state(h.app).sink,
                actor="cli:connect-sync-fixture",
                kpi_tag="ne-demo",
            )
        )
    finally:
        engine.dispose()
    _admit_fixture_chunks(h)
    return outcome.report.written


def _admit_fixture_chunks(h) -> None:
    """Bind fixture-sync chunks to real current-source receipts and ACL sidecars.

    Fixture sync deliberately predates connector authority admission.  The demo-arc
    harness must therefore perform the producer step explicitly; blessing the old
    ``Chunk.acl`` field or marking these source-bearing rows ``no_source`` would make
    the current-source enforcement test vacuous.
    """
    from ne.brain.current_source_authority import (
        SqlAlchemyCurrentSourceAuthorityWriter,
        ne_source_authority_heads,
        persist_current_acl_snapshot,
    )
    from ne.brain.current_source_filter import SOURCE_AUTHORITY_STAMP_METADATA_KEY
    from ne.contracts.source_authority import (
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

    state = get_state(h.app)
    assert state.source_authority_reader is not None
    reader = cast(Any, state.source_authority_reader)
    runtime_engine = reader._engine
    construction_id = reader._construction_id
    writer = SqlAlchemyCurrentSourceAuthorityWriter(
        runtime_engine,
        construction_id,
        freshness_sla_seconds=Decimal("86400"),
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint",
        descriptor_schema_version=1,
        descriptor_hash=hashlib.sha256(b"ne-demo-fixture-source-contract-v1").hexdigest(),
    )

    async def _admit(key: str, content: bytes, labels: tuple[str, ...]) -> SourceAuthorityStamp:
        acl_bytes = json.dumps(
            {"acl": list(labels)},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        acl_hash = hashlib.sha256(acl_bytes).hexdigest()
        content_hash = hashlib.sha256(content).hexdigest()
        identity = SourceIdentity(
            deployment_id=construction_id,
            source_kind="m365-sharepoint",
            connector_instance_id="ne-demo-fixture-sync",
            point=M365SourcePoint(
                graph_tenant_id="ne-demo-fixture",
                site_id="",
                drive_id="ne-demo-fixture",
                item_id=key,
            ),
            generation=1,
        )
        artifact = SourceContentArtifactIdentity(
            source_identity=identity,
            source_snapshot_hash=content_hash,
            content_snapshot_hash=content_hash,
            acl_snapshot_hash=acl_hash,
            source_contract=contract,
            artifact_schema_version=1,
        )
        with runtime_engine.connect() as conn:
            expected_head = conn.execute(
                sa.select(ne_source_authority_heads.c.head_revision).where(
                    ne_source_authority_heads.c.identity_hash == identity.identity_hash
                )
            ).scalar_one_or_none()
        claim = await writer.claim(
            SourceReconciliationScope(path=("ne-demo-fixture-sync", key)),
            expected_head_revision=expected_head,
            lease_seconds=60,
        )
        revision = f"fixture-{content_hash}"
        persist_current_acl_snapshot(
            runtime_engine,
            identity_hash=identity.identity_hash,
            artifact_id=artifact.artifact_id,
            acl_snapshot_hash=acl_hash,
            labels=labels,
        )
        receipt = await writer.finalize(
            claim,
            SourceAdmissionFence(
                deployment_id=identity.deployment_id,
                connector_instance_id=identity.connector_instance_id,
                source_generation=identity.generation,
                external_source_revision=revision,
                contract_epoch=1,
                reconciliation_scope=claim.scope,
                reconciliation_epoch=claim.reconciliation_epoch,
                fencing_token=claim.fencing_token,
            ),
            SourceAuthorityCandidate(
                source_identity=identity,
                artifact_identity=artifact,
                source_revision=revision,
                source_contract=contract,
                contract_epoch=1,
                acl_snapshot_hash=acl_hash,
                state=SourceAuthorityState.PRESENT,
                authority_as_of=datetime.now(UTC) - timedelta(seconds=1),
            ),
        )
        await writer.release(claim)
        return SourceAuthorityStamp(
            source_identity=identity,
            artifact_identity=artifact,
            selected_head_revision=receipt.source_revision,
            acl_snapshot_hash=receipt.acl_snapshot_hash,
            source_contract=receipt.source_contract,
            contract_epoch=receipt.contract_epoch,
            authority_receipt_id=receipt.receipt_id,
            authority_receipt_hash=receipt.receipt_hash,
        )

    async def _admit_all() -> None:
        admitted = []
        for chunk in tuple(h.chunk_store._chunks.values()):
            stamp = await _admit(
                f"chunk:{chunk.chunk_id}",
                chunk.text.encode(),
                tuple(sorted(set(chunk.acl))),
            )
            admitted.append(
                chunk.model_copy(
                    update={
                        "metadata": {
                            **dict(chunk.metadata),
                            SOURCE_AUTHORITY_STAMP_METADATA_KEY: stamp.model_dump(mode="json"),
                        }
                    }
                )
            )
        await h.chunk_store.upsert_chunks(admitted)

    run(_admit_all())


def _bind_demo_graph_acl_stamps(h) -> None:
    """Replace the legacy dataset-wide stamp with ACL-exact fixture receipts."""
    from ne.brain.current_source_authority import (
        SqlAlchemyCurrentSourceAuthorityWriter,
        persist_current_acl_snapshot,
    )
    from ne.connect.identity import source_acl_hash
    from ne.contracts.source_authority import (
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

    state = get_state(h.app)
    assert state.entity_graph is not None
    assert state.source_authority_reader is not None
    store = cast(Any, state.entity_graph)._store
    reader = cast(Any, state.source_authority_reader)
    engine = reader._engine
    construction_id = reader._construction_id
    writer = SqlAlchemyCurrentSourceAuthorityWriter(
        engine,
        construction_id,
        freshness_sla_seconds=Decimal("86400"),
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint",
        descriptor_schema_version=1,
        descriptor_hash=hashlib.sha256(b"ne-demo-fixture-source-contract-v1").hexdigest(),
    )

    async def _admit_acl(labels: tuple[str, ...]) -> SourceAuthorityStamp:
        acl_hash = source_acl_hash(labels)
        identity = SourceIdentity(
            deployment_id=construction_id,
            source_kind="m365-sharepoint",
            connector_instance_id="ne-demo-fixture-sync",
            point=M365SourcePoint(
                graph_tenant_id="ne-demo-fixture",
                site_id="",
                drive_id="ne-demo-fixture",
                item_id=f"graph-acl:{acl_hash}",
            ),
            generation=1,
        )
        artifact = SourceContentArtifactIdentity(
            source_identity=identity,
            source_snapshot_hash=acl_hash,
            content_snapshot_hash=acl_hash,
            acl_snapshot_hash=acl_hash,
            source_contract=contract,
            artifact_schema_version=1,
        )
        claim = await writer.claim(
            SourceReconciliationScope(path=("ne-demo-fixture-sync", f"graph-acl:{acl_hash}")),
            expected_head_revision=None,
            lease_seconds=60,
        )
        persist_current_acl_snapshot(
            engine,
            identity_hash=identity.identity_hash,
            artifact_id=artifact.artifact_id,
            acl_snapshot_hash=acl_hash,
            labels=labels,
        )
        receipt = await writer.finalize(
            claim,
            SourceAdmissionFence(
                deployment_id=identity.deployment_id,
                connector_instance_id=identity.connector_instance_id,
                source_generation=identity.generation,
                external_source_revision=f"fixture-{acl_hash}",
                contract_epoch=1,
                reconciliation_scope=claim.scope,
                reconciliation_epoch=claim.reconciliation_epoch,
                fencing_token=claim.fencing_token,
            ),
            SourceAuthorityCandidate(
                source_identity=identity,
                artifact_identity=artifact,
                source_revision=f"fixture-{acl_hash}",
                source_contract=contract,
                contract_epoch=1,
                acl_snapshot_hash=acl_hash,
                state=SourceAuthorityState.PRESENT,
                authority_as_of=datetime.now(UTC) - timedelta(seconds=1),
            ),
        )
        await writer.release(claim)
        return SourceAuthorityStamp(
            source_identity=identity,
            artifact_identity=artifact,
            selected_head_revision=receipt.source_revision,
            acl_snapshot_hash=receipt.acl_snapshot_hash,
            source_contract=receipt.source_contract,
            contract_epoch=receipt.contract_epoch,
            authority_receipt_id=receipt.receipt_id,
            authority_receipt_hash=receipt.receipt_hash,
        )

    async def _bind_all() -> None:
        rows = (*store._nodes.values(), *store._edges.values())
        labels_by_acl = {tuple(sorted(set(cast(list[str], props["acl"])))) for props in rows}
        stamps = {labels: await _admit_acl(labels) for labels in labels_by_acl}
        for props in rows:
            labels = tuple(sorted(set(cast(list[str], props["acl"]))))
            props["source_authority_stamps"] = [stamps[labels].model_dump(mode="json")]

    run(_bind_all())


def _bearers(h, scenario):
    return {
        "reader": h.bearer(
            scenario.reader_id,
            groups=scenario.reader_groups,
            department=scenario.reader_department,
        ),
        "outsider": h.bearer(
            scenario.outsider_id,
            groups=scenario.outsider_groups,
            department=scenario.outsider_department,
        ),
        "approver": h.bearer("approver-demo", groups=("approvers",), department="operations"),
        "ops": h.bearer("ops-demo", groups=("ops",)),
    }


def _audit_verify_callable(audit_url: str):
    async def _verify() -> dict:
        result = CliRunner().invoke(
            main, ["audit", "verify", "--database-url", audit_url, "--json"]
        )
        return json.loads(result.output)

    return _verify


class _InterceptTransport(httpx.AsyncBaseTransport):
    """The harness's REAL ASGI transport with a handler in front of it.

    The handler answers the requests it recognises (returning ``None`` for the rest,
    which the real governed app then serves), so an arc can be driven against a
    deployment that differs from the fixture in exactly ONE way — e.g. one that does
    not stage a verified template.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, handler) -> None:
        self._inner = inner
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        stub = self._handler(request)
        if stub is not None:
            return stub
        return await self._inner.handle_async_request(request)


def _run_full_arc(
    tmp_path: Path,
    *,
    verified_max_result_tokens: int = DEMO_VERIFIED_MAX_RESULT_TOKENS,
    progress: ArcProgressCallback | None = None,
    intercept=None,
):
    subset = demo_doc_subset()
    scenario = build_scenario().model_copy(update={"doc_ids": subset})
    h = build_demo_app(tmp_path, verified_max_result_tokens=verified_max_result_tokens)
    _bind_demo_graph_acl_stamps(h)
    written = _seed_corpus(h, tmp_path, subset)
    assert written > 0

    def _client() -> httpx.AsyncClient:
        if intercept is None:
            return h.client()
        return httpx.AsyncClient(
            transport=_InterceptTransport(httpx.ASGITransport(app=h.app), intercept),
            base_url="http://ne.test",
        )

    async def _drive():
        async with _client() as client:
            return await run_demo_arc(
                client,
                _bearers(h, scenario),
                scenario,
                audit_verify=_audit_verify_callable(h.audit_url),
                writeback_action=WRITEBACK_ACTION,
                progress=progress,
            )

    transcript = run(_drive())
    for engine in get_state(h.app).engines:
        engine.dispose()
    return scenario, transcript, h


def _by_name(transcript):
    return {s.name: s for s in transcript.steps}


def test_step_deadline_is_cumulative_wall_clock_not_per_await() -> None:
    """Two individually-fast awaits cannot each consume the whole step budget."""
    import asyncio
    import time

    from ne.cli.demo.arc import _StepDeadline

    async def _exercise() -> None:
        deadline = _StepDeadline(started=time.monotonic(), deadline_s=0.08)
        await deadline.wait(asyncio.sleep(0.05))
        with pytest.raises(TimeoutError):
            await deadline.wait(asyncio.sleep(0.05))

    run(_exercise())


def test_full_arc_fixture_mode(tmp_path: Path) -> None:
    events: list[tuple[object, ...]] = []
    scenario, transcript, h = _run_full_arc(tmp_path, progress=lambda *event: events.append(event))
    steps = _by_name(transcript)

    assert len(events) == ARC_STEP_COUNT * 2
    assert [event[0] for event in events[::2]] == list(range(1, ARC_STEP_COUNT + 1))
    assert all(event[3] == "start" for event in events[::2])
    assert all(event[3] == "finish" for event in events[1::2])
    assert all(event[4] == 60.0 for event in events)
    assert all(event[5] is None for event in events[::2])
    assert all(event[5] is not None for event in events[1::2])

    # T1: cited chunks + the ground-truth answer.
    t1 = steps["t1_search"]
    assert t1.status is StepStatus.OK
    assert t1.citations
    assert t1.evidence["answer_seen"] is True

    # Permission trap: outsider sees zero trap chunks (search AND retrieve-by-id).
    trap = steps["permission_trap"]
    assert trap.status is StepStatus.OK
    assert trap.evidence["trap_in_search"] is False
    assert trap.evidence["trap_in_retrieve"] is False

    # T2: a REAL governed /v1/graph/neighborhood read — the reader sees the anchor's
    # neighborhood, the outsider's trap anchor is trimmed to EMPTY at source (dual-key).
    t2 = steps["t2_graph"]
    assert t2.status is StepStatus.OK
    assert t2.evidence["graph_surface_present"] is True
    assert t2.evidence["reader_nodes"] > 0
    assert t2.evidence["outsider_trap_nodes"] == 0

    # T3: a REAL verified answer over oxigraph templates (deterministic invoke leg) —
    # rows, urn:ne:entity citations, config_hash, ontology_version, replay match, and
    # the chat-turn done-frame carries STRUCTURAL provenance (verified.template_id),
    # not a substring of model output text.
    t3 = steps["t3_verified"]
    assert t3.status is StepStatus.OK
    assert t3.evidence["rows"] >= 1
    assert t3.evidence["citations_urn_ne_entity"] is True
    assert t3.evidence["config_hash_set"] is True
    assert t3.evidence["ontology_version_set"] is True
    assert t3.evidence["replay_match"] is True
    assert t3.evidence["done_frame_template_id"] is True
    assert t3.evidence["done_frame_verified_template_id"] == "accounts-directory"
    assert t3.evidence["config_hash_match"] is True

    # T3 clarify: an in-domain ambiguous prompt gets a governed menu (no verified
    # key), then an ordinal reply in the SAME session routes with structural
    # provenance.
    t3_clarify = steps["t3_clarify"]
    assert t3_clarify.status is StepStatus.OK
    assert t3_clarify.evidence["menu_shown"] is True
    assert t3_clarify.evidence["clarify_no_verified"] is True
    assert t3_clarify.evidence["ordinal_routed"] is True

    # T3 refusal: the clarify bound (2) is exhausted, then the router refuses
    # honestly — never an unverified answer.
    t3_refusal = steps["t3_refusal"]
    assert t3_refusal.status is StepStatus.OK
    assert t3_refusal.evidence["refused"] is True
    assert t3_refusal.evidence["refusal_no_verified"] is True

    # Grounded analyst PASS (NE-CTXB-8): the model plans and fetches ONE
    # server-side verified COUNT aggregate — every reported count is INDEPENDENTLY
    # recomputed by the arc TWO ways (re-invoke the same aggregate + an independent
    # Python row-join over the full directory reads), never trusting the model's
    # own numbers.
    grounded_pass = steps["grounded_pass"]
    assert grounded_pass.status is StepStatus.OK
    assert grounded_pass.evidence["fetches"] == ["orders-per-account-count"]
    assert grounded_pass.evidence["fallback"] is False
    assert grounded_pass.evidence["assist_used"] is False
    assert grounded_pass.evidence["counts_matched"] is True
    assert grounded_pass.evidence["counts_checked"] >= 1
    assert grounded_pass.evidence["accounts_with_placed"] == 28
    assert grounded_pass.evidence["aggregate_groups"] == 28
    assert grounded_pass.evidence["recompute_legs_match"] is True
    assert grounded_pass.evidence["aggregate_truncated"] is False
    # NE-HGATE-4: the done-frame posture surfaces additively. The fixture harness
    # pins the upgrade-default STRICT tier on the ng2 config-default profile.
    assert grounded_pass.evidence["strictness"] == "strict"
    assert str(grounded_pass.evidence["gate"]).startswith("ng-2/")

    # Grounded analyst REFUSE: a scripted [grounded-hallucination] draft invents
    # an account + count; the REAL grounding gate refuses it (audited) and hands
    # back only the deterministic verified table — no fabricated fact survives.
    grounded_refuse = steps["grounded_refuse"]
    assert grounded_refuse.status is StepStatus.OK
    assert grounded_refuse.evidence["fallback"] is True
    assert grounded_refuse.evidence["assist_used"] is False
    assert grounded_refuse.evidence["initech_absent"] is True
    assert grounded_refuse.evidence["table_shown"] is True
    assert "accounts-directory" in grounded_refuse.evidence["fetches"]
    assert "initech" not in grounded_refuse.response_excerpt.lower()
    # NE-HGATE-4: same additive posture keys on the refuse leg (beat 8 stays green
    # at STRICT here; the fixture harness pins the STRICT tier on the ng2 profile).
    assert grounded_refuse.evidence["strictness"] == "strict"
    assert str(grounded_refuse.evidence["gate"]).startswith("ng-2/")

    # Deny-means-deny: the rejection was recorded, principal is the reader.
    deny = steps["deny_means_deny"]
    assert deny.status is StepStatus.OK
    assert deny.evidence.get("principal_id") == scenario.reader_id

    # HITL: a DISTINCT approver released the turn.
    hitl = steps["hitl_approval"]
    assert hitl.status is StepStatus.OK
    assert hitl.evidence["principal_ok"] is True
    assert hitl.evidence["released"] is True

    # NE-168: delegation — governed multi-lane agent-to-model dispatch, one trace.
    delegation = steps["delegation"]
    assert delegation.status is StepStatus.OK
    assert delegation.evidence["route_legs"] == 3
    assert delegation.evidence["private_success"] == 2
    assert delegation.evidence["frontier_denied"] is True

    # Audit: the real chain verify is valid over the session.
    audit = steps["audit_verify"]
    assert audit.status is StepStatus.OK
    assert transcript.audit_verify is not None
    assert transcript.audit_verify["valid"] is True
    assert transcript.audit_verify["checked"] > 0

    assert not transcript.has_failure()

    # The audited fail-closed bound: the T3 refusal leg emits a REAL
    # guardrail.block event whose payload carries the 'verified.unrouted' rule
    # (VerifiedDomainGuardrail) — queried directly over the session's audit DB,
    # not inferred from the arc's own evidence. The grounded-refuse leg emits
    # EXACTLY ONE guardrail.block carrying 'grounding.ungrounded-claim' — the
    # PASS leg's genuinely-grounded draft must emit none.
    engine = sa.create_engine(h.audit_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_audit_log.c.payload).where(ne_audit_log.c.action == "guardrail.block")
            ).all()
        payloads = [json.dumps(r.payload) for r in rows]
        assert any("verified.unrouted" in p for p in payloads)
        assert sum("grounding.ungrounded-claim" in p for p in payloads) == 1
    finally:
        engine.dispose()


# --- NE-CTXB-8: packing-ON honest truncation + rule-3 DEGRADE + anti-tautology --


def _invoke_verified(h, scenario, template_id: str) -> dict:
    """POST /v1/verified/invoke as the reader (a helper for the packing tests).

    No corpus chunk-seed is needed: the verified reads run over the oxigraph the
    boot lift populates from the FULL governed graph snapshot (all accounts/orders),
    so the totals are the fixture-complete 120/80/28 regardless of the doc subset.
    """
    reader = _bearers(h, scenario)["reader"]

    async def _drive() -> dict:
        async with h.client() as client:
            resp = await client.post(
                "/api/v1/verified/invoke",
                json={"template_id": template_id, "args": {}},
                headers=reader,
            )
            assert resp.status_code == 200, resp.text
            return resp.json()

    return run(_drive())


def test_packing_on_truncates_directory_reads(tmp_path: Path) -> None:
    """Packing-ON in the demo composition produces honest-truncated directory reads
    at a small cap (NE-CTXB-8): the directory tables pack to the budget
    (truncated=true, reason=tokens, returned_rows<total, citations in lockstep) and
    the count_total_rows COUNT rewrite supplies the exact totals (80/120 — exact on
    static fixtures). The aggregate stays UNtruncated at the shipped default cap
    (the COUNT-on replay pin at the demo seam)."""
    scenario = build_scenario()

    # Small cap → the directory tables (measured 6156/6016 est tokens) truncate.
    h = build_demo_app(tmp_path, verified_max_result_tokens=1024)
    try:
        orders = _invoke_verified(h, scenario, "orders-directory")
        o_answer = orders["answer"]
        assert o_answer["truncated"] is True
        assert o_answer["truncation_reason"] == "tokens"
        assert o_answer["returned_rows"] < 80
        assert o_answer["total_rows"] == 80
        assert len(o_answer["citations"]) == o_answer["returned_rows"]
        assert orders["replay_match"] is True

        accounts = _invoke_verified(h, scenario, "accounts-directory")
        a_answer = accounts["answer"]
        assert a_answer["truncated"] is True
        assert a_answer["truncation_reason"] == "tokens"
        assert a_answer["returned_rows"] < 120
        assert a_answer["total_rows"] == 120
        assert len(a_answer["citations"]) == a_answer["returned_rows"]
    finally:
        for engine in get_state(h.app).engines:
            engine.dispose()

    # The aggregate (measured 1532 est tokens) stays untruncated at the DEFAULT cap.
    agg_dir = tmp_path / "agg"
    agg_dir.mkdir()
    h2 = build_demo_app(agg_dir)
    try:
        agg = _invoke_verified(h2, scenario, "orders-per-account-count")
        answer = agg["answer"]
        assert answer["truncated"] is False
        assert answer["returned_rows"] == 28
        assert answer["total_rows"] == 28
        assert agg["replay_match"] is True
    finally:
        for engine in get_state(h2.app).engines:
            engine.dispose()


def test_grounded_beat_degrades_on_truncated_reads(tmp_path: Path) -> None:
    """Rule 3 (§5.4): ANY truncated answer the arc consumes → DEGRADED with the
    reason, never FAILED and never a silent partial-count comparison. At a small
    cap (1024) the grounded_pass leg's aggregate recompute read truncates (DEGRADE)
    AND the sibling t3_verified invoke leg truncates (DEGRADE) — both carry a
    'truncated' label."""
    _scenario, transcript, h = _run_full_arc(tmp_path, verified_max_result_tokens=1024)
    for engine in get_state(h.app).engines:
        engine.dispose()
    steps = _by_name(transcript)

    grounded_pass = steps["grounded_pass"]
    assert grounded_pass.status is StepStatus.DEGRADED
    assert "truncated" in grounded_pass.label

    t3 = steps["t3_verified"]
    assert t3.status is StepStatus.DEGRADED
    assert "truncated" in t3.label


def test_grounded_beat_degrades_on_directory_truncation(tmp_path: Path) -> None:
    """Rule 3, LEG-B coverage (Argus A1): at a cap ABOVE the aggregate (~1532 est
    tokens) but BELOW the directory tables (6016/6156 est), leg A stays untruncated
    so the recompute reaches leg B, whose INDEPENDENT directory read truncates →
    grounded_pass DEGRADED naming the truncated directory template, never FAILED and
    never a partial-count comparison. This exercises the leg-B truncation branch that
    the cap-1024 test (where leg A truncates first and returns early) cannot reach."""
    _scenario, transcript, h = _run_full_arc(tmp_path, verified_max_result_tokens=2048)
    for engine in get_state(h.app).engines:
        engine.dispose()
    grounded_pass = _by_name(transcript)["grounded_pass"]
    assert grounded_pass.status is StepStatus.DEGRADED
    # Leg-B label is distinct from leg-A's "aggregate read truncated" — it names
    # the directory template + the independent recompute read.
    assert "recompute read truncated" in grounded_pass.label
    assert grounded_pass.evidence["aggregate_groups"] == 28


def test_grounded_recount_divergence_fails() -> None:
    """The anti-tautology pin (R6-P1e): the pure comparison helper reports a
    failure reason when the aggregate cells diverge from the independent Python
    row-join recount, and OK ("") when they match — leg (b) is a real SECOND
    implementation, never a re-invoke tautology that trivially agrees."""
    from ne.cli.demo.arc import _compare_recount

    assert _compare_recount({"Acme Corp": 3, "Globex": 2}, {"Acme Corp": 3, "Globex": 2}) == ""
    # A wrong count → a non-empty failure reason.
    assert _compare_recount({"Acme Corp": 3, "Globex": 2}, {"Acme Corp": 3, "Globex": 99}) != ""
    # A missing group → a non-empty failure reason.
    assert _compare_recount({"Acme Corp": 3}, {"Acme Corp": 3, "Globex": 1}) != ""


def test_delegation_degrades_when_disabled(tmp_path: Path) -> None:
    """The delegation demo flag is off → the arc step honestly DEGRADEs (never
    FAILS — arc.py:38-49 discipline)."""
    subset = demo_doc_subset()
    scenario = build_scenario().model_copy(update={"doc_ids": subset})
    h = build_demo_app(tmp_path, delegation=False)
    _bind_demo_graph_acl_stamps(h)
    written = _seed_corpus(h, tmp_path, subset)
    assert written > 0

    async def _drive():
        async with h.client() as client:
            return await run_demo_arc(
                client,
                _bearers(h, scenario),
                scenario,
                audit_verify=_audit_verify_callable(h.audit_url),
                writeback_action=WRITEBACK_ACTION,
            )

    transcript = run(_drive())
    for engine in get_state(h.app).engines:
        engine.dispose()

    delegation = _by_name(transcript)["delegation"]
    assert delegation.status is StepStatus.DEGRADED
    assert delegation.label == _DELEGATION_DEGRADE_LABEL
    assert not transcript.has_failure()


def test_clarify_and_refusal_degrade_when_verified_disabled(tmp_path: Path) -> None:
    """Verified answers are OFF → t3_verified/t3_clarify/t3_refusal all honestly
    DEGRADE with the exact T3 lights-up label — never a fake OK, never a FAIL
    (arc degrade discipline)."""
    subset = demo_doc_subset()
    scenario = build_scenario().model_copy(update={"doc_ids": subset})
    h = build_demo_app(tmp_path, verified_answers=False)
    _bind_demo_graph_acl_stamps(h)
    written = _seed_corpus(h, tmp_path, subset)
    assert written > 0

    async def _drive():
        async with h.client() as client:
            return await run_demo_arc(
                client,
                _bearers(h, scenario),
                scenario,
                audit_verify=_audit_verify_callable(h.audit_url),
                writeback_action=WRITEBACK_ACTION,
            )

    transcript = run(_drive())
    for engine in get_state(h.app).engines:
        engine.dispose()

    steps = _by_name(transcript)
    for name in ("t3_verified", "t3_clarify", "t3_refusal"):
        assert steps[name].status is StepStatus.DEGRADED, name
        assert steps[name].label == _T3_DEGRADE_LABEL, name
    # The grounded-analyst legs require verified answers (they fetch through the
    # same registry) — with T3 off they honestly DEGRADE too, never FAIL/fake-OK.
    for name in ("grounded_pass", "grounded_refuse"):
        assert steps[name].status is StepStatus.DEGRADED, name
        assert steps[name].label == _GROUNDED_DEGRADE_LABEL, name
    assert not transcript.has_failure()


def test_template_not_staged_predicate_matches_only_the_audited_refusal() -> None:
    """The anti-blanket-swallow pin: ONLY a 404 whose body is the governed API's
    ``unknown-template`` refusal counts as "this deployment does not stage it".

    ``POST /v1/verified/invoke`` raises 404 from TWO causes and they mean opposite
    things (ontology.py): the fail-closed registry pre-check (``unknown-template`` —
    a configuration fact) and ``ToolNotRegisteredError`` from the executor
    (``template-tool-unregistered`` — the template IS staged and its binding is gone,
    a PLATFORM REGRESSION). Only the first may degrade. Any OTHER 404 — an
    unmounted/mistyped route, a sibling route's refusal, a proxy's HTML page — must
    keep FAILING too, or a dropped route would hide behind an honest-looking
    DEGRADE."""
    from ne.cli.demo.arc import _template_not_staged

    assert _template_not_staged(httpx.Response(404, json={"detail": "unknown-template"})) is True
    # NOT this refusal — the executor-binding regression, which must FAIL the step.
    assert (
        _template_not_staged(httpx.Response(404, json={"detail": "template-tool-unregistered"}))
        is False
    )
    assert _template_not_staged(httpx.Response(404, json={"detail": "Not Found"})) is False
    assert (
        _template_not_staged(httpx.Response(404, json={"detail": "validation-issue-not-found"}))
        is False
    )
    assert _template_not_staged(httpx.Response(404, text="<html>404</html>")) is False
    assert _template_not_staged(httpx.Response(404, json=["unknown-template"])) is False
    # The same body under a different status is not this refusal either.
    assert _template_not_staged(httpx.Response(503, json={"detail": "unknown-template"})) is False
    assert _template_not_staged(httpx.Response(200, json={"detail": "unknown-template"})) is False


def test_arc_degrades_when_the_verified_template_is_not_staged(tmp_path: Path) -> None:
    """The governed demo composition stages the Meridian bundle ONLY, so the acme
    templates this arc invokes answer an audited 404 ``unknown-template``. That is an
    UNAVAILABLE tier, not a broken one: the T3 + grounded legs DEGRADE with a reason
    naming the template (visibly not a pass) and the run still exits ZERO — exactly
    what the 503 "verified answers off" path already does."""

    def _not_staged(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/v1/verified/invoke"):
            return httpx.Response(404, json={"detail": "unknown-template"})
        return None

    _scenario, transcript, h = _run_full_arc(tmp_path, intercept=_not_staged)
    for engine in get_state(h.app).engines:
        engine.dispose()
    steps = _by_name(transcript)

    t3 = steps["t3_verified"]
    assert t3.status is StepStatus.DEGRADED
    assert "accounts-directory" in t3.label
    assert "not staged" in t3.label
    assert t3.evidence["verified_template_not_staged"] == "accounts-directory"
    # The gated legs degrade too, naming the SAME real reason — never the "verified
    # answers are not enabled" text, which would blame a flag that IS set.
    for name in ("t3_clarify", "t3_refusal", "grounded_pass", "grounded_refuse"):
        assert steps[name].status is StepStatus.DEGRADED, name
        assert "accounts-directory" in steps[name].label, name
        assert steps[name].label != _T3_DEGRADE_LABEL, name
        assert steps[name].label != _GROUNDED_DEGRADE_LABEL, name
    # The whole point: an absent template does not fail the run.
    assert not transcript.has_failure()


def test_arc_fails_when_a_staged_template_has_no_executor_binding(tmp_path: Path) -> None:
    """The regression this distinction exists for: a STAGED template whose executor
    binding is missing must FAIL the arc, never degrade it.

    ``ne demo run`` is the acceptance command, so a DEGRADE exits zero. While both
    404 causes shared the body ``unknown-template``, a lost binding — a platform
    regression — was reported as "this deployment does not stage it" and the demo
    reported success. The router now answers ``template-tool-unregistered`` for that
    cause, ``_template_not_staged`` does not match it, and the run fails loudly."""

    def _unbound(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/v1/verified/invoke"):
            return httpx.Response(404, json={"detail": "template-tool-unregistered"})
        return None

    _scenario, transcript, h = _run_full_arc(tmp_path, intercept=_unbound)
    for engine in get_state(h.app).engines:
        engine.dispose()
    steps = _by_name(transcript)

    t3 = steps["t3_verified"]
    assert t3.status is StepStatus.FAILED
    # Never mislabelled as an absent template.
    assert "not staged" not in t3.label
    assert "verified_template_not_staged" not in t3.evidence
    # The acceptance command must exit NONZERO on this.
    assert transcript.has_failure()


def test_dossier_renders(tmp_path: Path) -> None:
    scenario, transcript, _h = _run_full_arc(tmp_path)
    md = render_dossier(transcript, scenario, deployment_note="fixture-mode CI arc")

    # Every step title appears.
    for step in transcript.steps:
        assert step.title in md
    # NE-DEMO-LIVE-SEAMS: T2/T3/tier-2 now light LIVE, so the dossier carries no
    # NE-BRAIN-T2-WIRING degrade label — the old caveat is gone.
    assert "NE-BRAIN-T2-WIRING" not in md
    # Citations + approval + audit block present.
    assert "Citations / provenance" in md
    # NE-DEMO-LIVE-SEAMS (Codex P2 fix): T3 verified-answer provenance renders the
    # real urn:ne:entity citations — never the chunk-shaped "chunk `?`" fallback.
    assert "urn:ne:entity" in md
    assert "chunk `?` in doc `?`" not in md
    assert "approval rejected" in md or "released" in md
    assert "Audit-chain verification" in md
    assert '"valid": true' in md
    # No secret material.
    lowered = md.lower()
    assert "password" not in lowered
    assert "postgresql://" not in lowered
    assert "bearer " not in lowered


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ne.test")


def test_t2_lights_up(tmp_path: Path) -> None:
    scenario = build_scenario()

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/graph/neighborhood"):
            entity = request.url.params.get("entity")
            if entity == scenario.t2_anchor_key:
                # Reader anchor → a non-empty C7-visible neighborhood.
                return httpx.Response(
                    200,
                    json={
                        "nodes": [{"key": scenario.t2_anchor_key}, {"key": "n2"}],
                        "edges": [],
                        "truncated": False,
                    },
                )
            # Outsider trap anchor → trimmed to EMPTY at source.
            return httpx.Response(200, json={"nodes": [], "edges": [], "truncated": False})
        return httpx.Response(200, json={"chunks": [], "citations": []})

    async def _drive():
        async with _mock_client(_handler) as client:
            return await run_demo_arc(
                client,
                {"reader": {}, "outsider": {}, "approver": {}},
                scenario,
                audit_verify=None,
                poll_deadline_s=0.3,
            )

    transcript = run(_drive())
    t2 = _by_name(transcript)["t2_graph"]
    assert t2.status is StepStatus.OK
    assert t2.evidence["graph_surface_present"] is True
    assert t2.evidence["reader_nodes"] == 2
    assert t2.evidence["outsider_trap_nodes"] == 0


def test_t2_degrades_when_graph_tier_absent(tmp_path: Path) -> None:
    scenario = build_scenario()

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/graph/neighborhood"):
            return httpx.Response(404, json={"detail": "graph tier not enabled"})
        return httpx.Response(200, json={"chunks": [], "citations": []})

    async def _drive():
        async with _mock_client(_handler) as client:
            return await run_demo_arc(
                client,
                {"reader": {}, "outsider": {}, "approver": {}},
                scenario,
                audit_verify=None,
                poll_deadline_s=0.3,
            )

    transcript = run(_drive())
    t2 = _by_name(transcript)["t2_graph"]
    assert t2.status is StepStatus.DEGRADED
    assert t2.evidence["graph_surface_present"] is False


def test_arc_step_isolation(tmp_path: Path) -> None:
    scenario = build_scenario()

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/brain/search"):
            return httpx.Response(500, json={"detail": "InternalError"})
        if request.url.path.endswith("/openapi.json"):
            return httpx.Response(200, json={"paths": {}})
        return httpx.Response(200, json={"chunks": [], "citations": []})

    async def _drive():
        async with _mock_client(_handler) as client:
            return await run_demo_arc(
                client,
                {"reader": {}, "outsider": {}, "approver": {}},
                scenario,
                audit_verify=None,
                poll_deadline_s=0.3,
            )

    transcript = run(_drive())
    steps = _by_name(transcript)
    # T1 recorded FAILED, but the arc still completed every step + renders.
    assert steps["t1_search"].status is StepStatus.FAILED
    # NE-GA-G6 golden-literal update: 10 -> 12 (the two grounded-analyst steps).
    assert len(transcript.steps) == 12
    md = render_dossier(transcript, scenario)
    assert "T1 — hybrid retrieval" in md


def _verified_invoke_handler(request: httpx.Request) -> httpx.Response | None:
    """Shared /v1/verified/invoke stub (NE-DEMO-FIX1): satisfies the T3 invoke leg
    AND the grounded analyst's own two-leg arc-recompute
    (``_grounded_expected_counts`` — NE-CTXB-8: the aggregate leg A + the directory
    leg B) with a minimal but real, mutually-consistent VerifiedAnswer shape (one
    account with one placed order → aggregate cell {Acme: 1} matches the row-join).
    Returns ``None`` when the path doesn't match, so callers can chain it."""
    if not request.url.path.endswith("/v1/verified/invoke"):
        return None
    body = json.loads(request.content)
    template_id = body.get("template_id")
    if template_id == "accounts-directory":
        answer = {
            "columns": ["name", "account"],
            "rows": [["Acme", "urn:ne:entity:a1"]],
            "truncated": False,
        }
    elif template_id == "orders-directory":
        answer = {
            "columns": ["order", "account", "status"],
            "rows": [["urn:ne:entity:o1", "urn:ne:entity:a1", "placed"]],
            "truncated": False,
        }
    elif template_id == "orders-per-account-count":
        answer = {
            "columns": ["name", "account", "n_orders"],
            "rows": [["Acme", "urn:ne:entity:a1", "1"]],
            "truncated": False,
        }
    else:
        answer = {"columns": [], "rows": []}
    return httpx.Response(200, json={"answer": answer, "replay_match": True})


def _sse_error_body(detail: str) -> str:
    return f'event: error\ndata: {{"type":"error","detail":"{detail}"}}\n\n'


def test_grounded_turn_error_reports_error_class(tmp_path: Path) -> None:
    scenario = build_scenario()

    def _handler(request: httpx.Request) -> httpx.Response:
        stub = _verified_invoke_handler(request)
        if stub is not None:
            return stub
        path = request.url.path
        if path.endswith("/v1/chat/sessions"):
            return httpx.Response(200, json={"session_id": "s1"})
        if path.endswith("/messages"):
            return httpx.Response(200, text=_sse_error_body("EgressBlockedError"))
        if path.endswith("/v1/approvals/pending"):
            return httpx.Response(200, json={"approvals": []})
        return httpx.Response(200, json={"chunks": [], "citations": []})

    async def _drive():
        async with _mock_client(_handler) as client:
            return await run_demo_arc(
                client,
                {"reader": {}, "outsider": {}, "approver": {}},
                scenario,
                audit_verify=None,
                poll_deadline_s=0.3,
            )

    transcript = run(_drive())
    steps = _by_name(transcript)

    for name in ("grounded_pass", "grounded_refuse"):
        step = steps[name]
        assert step.status is StepStatus.FAILED
        assert "EgressBlockedError" in step.evidence["error_details"]
        assert step.label != _GROUNDED_DEGRADE_LABEL
        assert "not enabled" not in step.label


def _arc_over_verified_invoke(status: int, body: dict, scenario):
    """Drive the arc with ONE stubbed ``/v1/verified/invoke`` status+body for every
    template; everything else is the usual inert mock surface."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/verified/invoke"):
            return httpx.Response(status, json=body)
        if request.url.path.endswith("/v1/approvals/pending"):
            return httpx.Response(200, json={"approvals": []})
        return httpx.Response(200, json={"chunks": [], "citations": []})

    async def _drive():
        async with _mock_client(_handler) as client:
            return await run_demo_arc(
                client,
                {"reader": {}, "outsider": {}, "approver": {}},
                scenario,
                audit_verify=None,
                poll_deadline_s=0.3,
            )

    return _by_name(run(_drive()))


def test_t3_still_fails_on_a_404_that_is_not_the_not_staged_refusal() -> None:
    """The guard is NOT a blanket 404 swallow: a route-level 404 (a dropped or
    mistyped verified route) still FAILS the T3 step, because degrading on it would
    hide a real regression behind an honest-looking 'unavailable'."""
    scenario = build_scenario()
    steps = _arc_over_verified_invoke(404, {"detail": "Not Found"}, scenario)

    t3 = steps["t3_verified"]
    assert t3.status is StepStatus.FAILED
    assert t3.label != _T3_DEGRADE_LABEL
    assert "not staged" not in t3.label


def test_t3_degrade_on_503_is_unchanged() -> None:
    """The pre-existing 503 path is untouched: verified answers OFF still DEGRADEs
    with the exact lights-up label, never the not-staged reason."""
    scenario = build_scenario()
    steps = _arc_over_verified_invoke(503, {"detail": "verified-answers-disabled"}, scenario)

    t3 = steps["t3_verified"]
    assert t3.status is StepStatus.DEGRADED
    assert t3.label == _T3_DEGRADE_LABEL
    assert t3.evidence["verified_surface_present"] is False


def test_grounded_recompute_degrades_when_the_aggregate_is_not_staged() -> None:
    """The arc's OWN recompute legs degrade on the same audited refusal: with
    accounts-directory served but the aggregate absent, grounded_pass DEGRADEs naming
    ``orders-per-account-count`` instead of FAILING with 'aggregate invoke failed'."""
    scenario = build_scenario()

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/verified/invoke"):
            body = json.loads(request.content)
            if body.get("template_id") == "orders-per-account-count":
                return httpx.Response(404, json={"detail": "unknown-template"})
        stub = _verified_invoke_handler(request)
        if stub is not None:
            return stub
        if request.url.path.endswith("/v1/chat/sessions"):
            return httpx.Response(200, json={"session_id": "s1"})
        if request.url.path.endswith("/v1/approvals/pending"):
            return httpx.Response(200, json={"approvals": []})
        return httpx.Response(200, json={"chunks": [], "citations": []})

    async def _drive():
        async with _mock_client(_handler) as client:
            return await run_demo_arc(
                client,
                {"reader": {}, "outsider": {}, "approver": {}},
                scenario,
                audit_verify=None,
                poll_deadline_s=0.3,
            )

    grounded_pass = _by_name(run(_drive()))["grounded_pass"]
    assert grounded_pass.status is StepStatus.DEGRADED
    assert "orders-per-account-count" in grounded_pass.label
    assert "not staged" in grounded_pass.label


def test_grounded_recompute_fails_when_a_directory_read_is_a_real_error() -> None:
    """Precision pin for leg B: one absent template ALONGSIDE a genuine backend error
    is still a FAILURE — the degrade fires only when every read that failed to arrive
    was the audited not-staged refusal."""
    scenario = build_scenario()

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/verified/invoke"):
            body = json.loads(request.content)
            if body.get("template_id") == "accounts-directory":
                return httpx.Response(404, json={"detail": "unknown-template"})
            if body.get("template_id") == "orders-directory":
                return httpx.Response(500, json={"detail": "InternalError"})
        stub = _verified_invoke_handler(request)
        if stub is not None:
            return stub
        if request.url.path.endswith("/v1/chat/sessions"):
            return httpx.Response(200, json={"session_id": "s1"})
        if request.url.path.endswith("/v1/approvals/pending"):
            return httpx.Response(200, json={"approvals": []})
        return httpx.Response(200, json={"chunks": [], "citations": []})

    async def _drive():
        async with _mock_client(_handler) as client:
            return await run_demo_arc(
                client,
                {"reader": {}, "outsider": {}, "approver": {}},
                scenario,
                audit_verify=None,
                poll_deadline_s=0.3,
            )

    steps = _by_name(run(_drive()))
    # t3_verified invokes accounts-directory → the not-staged DEGRADE gates the
    # grounded legs, so drive the recompute directly for the mixed-outcome case.
    assert steps["t3_verified"].status is StepStatus.DEGRADED

    from ne.cli.demo.arc import _grounded_expected_counts

    async def _recompute():
        async with _mock_client(_handler) as client:
            return await _grounded_expected_counts(client, "/api", {})

    recompute = run(_recompute())
    assert recompute.degrade_reason == ""
    assert recompute.failed_reason == "directory recompute read failed"


def test_writeback_deny_unknown_tool_class_is_governed_rejection(tmp_path: Path) -> None:
    scenario = build_scenario()

    def _handler(request: httpx.Request) -> httpx.Response:
        stub = _verified_invoke_handler(request)
        if stub is not None:
            return stub
        path = request.url.path
        if path.endswith("/v1/chat/sessions"):
            return httpx.Response(200, json={"session_id": "s1"})
        if path.endswith("/messages"):
            return httpx.Response(200, text=_sse_error_body("ToolNotRegisteredError"))
        if path.endswith("/v1/approvals/pending"):
            return httpx.Response(200, json={"approvals": []})
        return httpx.Response(200, json={"chunks": [], "citations": []})

    async def _drive():
        async with _mock_client(_handler) as client:
            return await run_demo_arc(
                client,
                {"reader": {}, "outsider": {}, "approver": {}},
                scenario,
                audit_verify=None,
                poll_deadline_s=0.3,
            )

    transcript = run(_drive())
    steps = _by_name(transcript)
    deny = steps["deny_means_deny"]
    hitl = steps["hitl_approval"]

    assert deny.status is StepStatus.OK
    assert deny.evidence["governed_rejection"] is True
    assert "ToolNotRegisteredError" in deny.evidence["error_details"]
    assert hitl.status is StepStatus.DEGRADED
    assert hitl.label == _HITL_DEGRADE_LABEL


def test_writeback_deny_foreign_error_class_fails_honestly(tmp_path: Path) -> None:
    scenario = build_scenario()

    def _handler(request: httpx.Request) -> httpx.Response:
        stub = _verified_invoke_handler(request)
        if stub is not None:
            return stub
        path = request.url.path
        if path.endswith("/v1/chat/sessions"):
            return httpx.Response(200, json={"session_id": "s1"})
        if path.endswith("/messages"):
            return httpx.Response(200, text=_sse_error_body("EgressBlockedError"))
        if path.endswith("/v1/approvals/pending"):
            return httpx.Response(200, json={"approvals": []})
        return httpx.Response(200, json={"chunks": [], "citations": []})

    async def _drive():
        async with _mock_client(_handler) as client:
            return await run_demo_arc(
                client,
                {"reader": {}, "outsider": {}, "approver": {}},
                scenario,
                audit_verify=None,
                poll_deadline_s=0.3,
            )

    transcript = run(_drive())
    steps = _by_name(transcript)
    deny = steps["deny_means_deny"]
    hitl = steps["hitl_approval"]

    assert deny.status is StepStatus.FAILED
    assert "EgressBlockedError" in deny.evidence["error_details"]
    assert "not registered" not in deny.label
    assert hitl.label != _HITL_DEGRADE_LABEL


def test_done_frame_substring_forgery_closed() -> None:
    """The H1 forgery-closure proof: a done frame whose OUTPUT TEXT merely
    CONTAINS ``template_id=...`` (the exact pre-U5 gateway echo) does NOT flip
    the structural bit — only a real ``verified`` dict does."""
    forged = [
        SseFrame(
            event="done",
            data={"output": "Verified answer delivered (template_id=accounts-directory)"},
        )
    ]
    assert _done_verified_template_id(forged) is None

    structural = [
        SseFrame(
            event="done",
            data={"output": "x", "verified": {"template_id": "accounts-directory"}},
        )
    ]
    assert _done_verified_template_id(structural) == "accounts-directory"

    non_dict_verified = [SseFrame(event="done", data={"output": "x", "verified": "nope"})]
    assert _done_verified_template_id(non_dict_verified) is None

    assert _done_verified_template_id([]) is None


# --- NE-GA-G6: the gate-drift tripwire (scripted draft vs the REAL G1 gate) --


@functools.cache
def _shipped_answer_shapes() -> dict[str, tuple[tuple[str, ...], str]]:
    """Each shipped demo template's OWN declared ``[answer]`` column order and
    ``citation_var``, read through the REAL registry loader over the REAL bundle
    ``write_verified_templates`` ships — never a second copy of the order.
    """
    from ne.brain_ontology.verified.templates import load_template_registry
    from ne.cli.demo.corpus import write_verified_templates

    with tempfile.TemporaryDirectory() as tmp:
        registry = load_template_registry(write_verified_templates(Path(tmp) / "vt"))
    return {
        t.template_id: (tuple(t.answer.columns), t.answer.citation_var) for t in registry.templates
    }


def _verified_answer(template_id: str, columns: list[str], rows: list[tuple[str, ...]]) -> dict:
    """A VerifiedAnswer-shaped mapping (the exact JSON shape ``VerifiedAnswer.
    model_dump_json()`` emits) built directly from the real acme fixtures —
    NEVER the live oxigraph lift. ``citations`` mirrors the citation_var column
    (synthetic ``urn:ne:entity`` IRIs; the gate rides name/status cells + pair-
    frequency anchors, never IRI tokens, so a synthetic IRI is safe).

    The citation column is located by the SHIPPED template's declared
    ``citation_var``, so the citations follow the identifier wherever the
    declared column order puts it instead of assuming it leads.
    """
    _declared, citation_var = _shipped_answer_shapes()[template_id]
    citation_ix = columns.index(citation_var)
    return {
        "template_id": template_id,
        "template_version": 1,
        "config_hash": "0" * 64,
        "columns": columns,
        "rows": rows,
        "citations": [row[citation_ix] for row in rows],
        "returned_rows": len(rows),
        "row_limit": 500,
        "ontology_version": "acme-fixture-v1",
        "executed_at": "2026-01-01T00:00:00+00:00",
        "truncated": False,
        "args": {},
    }


def _fixture_accounts_orders_answers() -> tuple[dict, dict]:
    """The real acme fixture accounts/orders, wrapped as VerifiedAnswer-shaped
    mappings — the exact plan-time drift-tripwire probe, pinned in CI."""
    from ne.acme.cli import default_fixtures_dir

    records = json.loads((default_fixtures_dir() / "records.json").read_text(encoding="utf-8"))

    def iri(record_id: str) -> str:
        return f"urn:ne:entity:acme/x:{record_id}"

    accounts_answer = _verified_answer(
        "accounts-directory",
        ["name", "account"],
        [(a["name"], iri(a["id"])) for a in records["accounts"]],
    )
    orders_answer = _verified_answer(
        "orders-directory",
        ["order", "account", "status"],
        [(iri(o["id"]), iri(o["account"]), o["status"]) for o in records["orders"]],
    )
    return accounts_answer, orders_answer


def _fixture_aggregate_answer() -> dict:
    """The real acme fixtures folded into the ``orders-per-account-count`` aggregate
    (columns ``[name, account, n_orders]``; one row per account WITH >=1 placed
    order — the inner-join shape the kind='count' rewrite produces) — the exact
    plan-time drift-tripwire probe for the NE-CTXB-8 grounded beat, pinned in CI."""
    from ne.acme.cli import default_fixtures_dir

    records = json.loads((default_fixtures_dir() / "records.json").read_text(encoding="utf-8"))

    def iri(record_id: str) -> str:
        return f"urn:ne:entity:acme/x:{record_id}"

    name_by_id = {a["id"]: a["name"] for a in records["accounts"]}
    placed: dict[str, int] = {}
    for o in records["orders"]:
        if o["status"] == "placed":
            placed[o["account"]] = placed.get(o["account"], 0) + 1
    rows = [
        (name_by_id[acct_id], iri(acct_id), str(count))
        for acct_id, count in placed.items()
        if acct_id in name_by_id
    ]
    return _verified_answer("orders-per-account-count", ["name", "account", "n_orders"], rows)


def test_arc_fixture_answers_carry_the_shipped_column_order() -> None:
    """THE DRIFT TRIPWIRE ITSELF — the fixture answers must project the SHIPPED
    template's own declared column ORDER, and their citations must come from the
    SHIPPED ``citation_var``'s column.

    Every consumer of these fixtures keys by ``columns.index(...)``
    (``arc.py``'s ``_grounded_expected_counts``), so a fixture that names the
    right columns in the WRONG order stays green while claiming to be "the exact
    plan-time drift-tripwire probe" — a probe that cannot trip. This test is
    what makes the claim true: it compares ORDERED tuples against the registry
    the deployment actually loads, so re-ordering a shipped ``[answer].columns``
    without re-ordering the fixture rows goes RED here.
    """
    shapes = _shipped_answer_shapes()
    accounts_answer, orders_answer = _fixture_accounts_orders_answers()
    fixtures = (accounts_answer, orders_answer, _fixture_aggregate_answer())
    assert {a["template_id"] for a in fixtures} <= set(shapes), (
        "a fixture names no shipped template"
    )

    for answer in fixtures:
        template_id = answer["template_id"]
        declared, citation_var = shapes[template_id]
        assert tuple(answer["columns"]) == declared, (
            f"fixture for {template_id} projects columns in an order the shipped "
            f"template does not declare (field: [answer].columns)"
        )
        # The citations column is the citation_var's column, POSITIONALLY — the
        # thing a bare re-order silently breaks while every index-keyed consumer
        # keeps passing.
        ix = declared.index(citation_var)
        assert answer["citations"] == [row[ix] for row in answer["rows"]], (
            f"fixture for {template_id} draws citations from a column that is not "
            f"its declared citation_var (field: [answer].citation_var)"
        )


def test_arc_invoke_stub_serves_the_shipped_column_order() -> None:
    """The same tripwire over the ``/v1/verified/invoke`` HTTP stub the arc tests
    run against: the stubbed answers must carry the shipped declared column
    order, or the arc is proven against a wire shape the deployment never serves.
    """
    shapes = _shipped_answer_shapes()
    for template_id, (declared, _citation_var) in shapes.items():
        if template_id == "tickets-by-status":
            continue  # arg-taking; the stub serves no zero-arg answer for it
        request = httpx.Request(
            "POST",
            "https://demo.invalid/v1/verified/invoke",
            json={"template_id": template_id, "args": {}},
        )
        response = _verified_invoke_handler(request)
        assert response is not None
        served = response.json()["answer"]
        assert tuple(served["columns"]) == declared, (
            f"the invoke stub serves {template_id} with columns in an order the "
            f"shipped template does not declare (field: [answer].columns)"
        )


@pytest.mark.parametrize(
    ("profile", "tier"),
    [
        (PROFILE_NG1_FROZEN, StrictnessTier.STRICT),
        (PROFILE_NG2, StrictnessTier.STRICT),
        (PROFILE_NG2, StrictnessTier.STANDARD),
    ],
    ids=["ng1-strict", "ng2-strict", "ng2-standard"],
)
def test_grounded_draft_passes_real_gate(profile: GateProfile, tier: StrictnessTier) -> None:
    """The drift-tripwire between the scripted fixture-mode draft and the REAL
    grounding gate, tier-by-profile parametrized (NE-HGATE-4, design §5.2).

    The fixture ``_grounded_draft`` is DELIBERATELY unchanged and yields ZERO
    findings at ng-1 AND ng-2 — so it passes at ng-2/STRICT AND ng-2/STANDARD
    (``apply_strictness`` therefore never withholds). The fabricated refuse draft
    withholds at every combo: enforced reasons always contain ``numeral-ungrounded``
    plus the vocab fact-class, whose NAME is profile-conditional (ng-1 emits
    ``vocab-ungrounded``; ng-2 emits ``vocab-ungrounded-factual`` for the same
    digit-bearing sentence). The assist junk (HGATE-3-hardened with an ungroundable
    numeral) withholds at STANDARD too. The word 'placed' must never re-enter the
    draft (no longer a corpus token — the aggregate carries no status cells)."""
    from ne.brain_ontology.verified.answers import VerifiedAnswer, render_answer_text
    from ne.govern.grounding import GroundingCorpus, check_grounding

    aggregate_answer = _fixture_aggregate_answer()

    # NE-CTXB-5 §4.2: the verified-read return the model (and the scripted
    # _grounded_draft) sees is the COMPACT rendering, not the JSON — feed the
    # harness exactly what a real model reads (via render_answer_text).
    def _compact(mapping: dict) -> str:
        return render_answer_text(VerifiedAnswer.model_validate(mapping))

    tool_contents = [
        f"read.kg.orders_per_account_count returned: {_compact(aggregate_answer)}",
    ]
    draft = _grounded_draft(tool_contents)
    assert "placed" not in draft

    aggregate_corpus = GroundingCorpus.from_answers([aggregate_answer])
    verdict = check_grounding(draft, aggregate_corpus, G_QUESTION, profile=profile)
    assert verdict.passed is True
    assert verdict.findings == ()
    decision = apply_strictness(verdict, tier)
    assert decision.withhold is False
    assert decision.enforced == ()
    assert decision.advisory == ()

    # The fabricated refuse draft withholds at BOTH tiers — the accounts-only corpus
    # is the seam the production DemoGroundedHallucinationGateway fabricates against.
    accounts_answer, _orders_answer = _fixture_accounts_orders_answers()
    accounts_only_corpus = GroundingCorpus.from_answers([accounts_answer])
    hallucinated_verdict = check_grounding(
        _HALLUCINATED_DRAFT, accounts_only_corpus, G_REFUSE_PROMPT, profile=profile
    )
    hd = apply_strictness(hallucinated_verdict, tier)
    assert hd.withhold is True
    enforced_reasons = {f.reason for f in hd.enforced}
    # ng-2 relabels the digit-bearing-sentence vocab class (vocab-ungrounded-factual);
    # ng-1 emits today's flat vocab-ungrounded. Both always withhold.
    expected_vocab_reason = (
        "vocab-ungrounded" if profile is PROFILE_NG1_FROZEN else "vocab-ungrounded-factual"
    )
    assert "numeral-ungrounded" in enforced_reasons
    assert expected_vocab_reason in enforced_reasons

    # The assist junk withholds at every combo (HGATE-3 hardened it with an
    # ungroundable numeral, so STANDARD refuses it too).
    assert (
        apply_strictness(
            check_grounding(_ASSIST_JUNK, aggregate_corpus, G_QUESTION, profile=profile), tier
        ).withhold
        is True
    )
    assert (
        apply_strictness(
            check_grounding(_ASSIST_JUNK, accounts_only_corpus, G_REFUSE_PROMPT, profile=profile),
            tier,
        ).withhold
        is True
    )


# --- NE-DEMO-WRITEBACK-LIVE: the Beat-4 tool swap (write.external.demo.send ->
# write.internal.crm.note) -----------------------------------------------------


def test_writeback_prompt_default_action_byte_identical() -> None:
    """The DEFAULT demo action's deny+approve prompts are BYTE-IDENTICAL to the
    pre-unit literals — the fixture/live back-compat pin."""
    action = "write.external.demo.send"
    args = {"to": "ops"}
    assert (
        _writeback_prompt(action, args, again=False)
        == f"[writeback] please send an external note ({action})"
    )
    assert (
        _writeback_prompt(action, args, again=True)
        == f"[writeback] please send an external note again ({action})"
    )


def test_writeback_prompt_crm_renders_sorted_quoted_args() -> None:
    """Any OTHER action renders its ``args`` sorted + single-quoted — the exact
    shape ``demo_fixture_harness.PromptRoutedGateway``'s regex parser recovers
    (the prompt<->parser drift tripwire)."""
    args = {
        "title": "demo note title",
        "record_id": "001000000000001AAA",
        "body": "demo note body",
    }
    prompt = _writeback_prompt(CRM_WRITEBACK_ACTION, args, again=False)
    assert prompt == (
        f"[writeback] please call the {CRM_WRITEBACK_ACTION} tool with arguments "
        "body='demo note body', record_id='001000000000001AAA', title='demo note title'"
    )
    again_prompt = _writeback_prompt(CRM_WRITEBACK_ACTION, args, again=True)
    assert again_prompt == (
        f"[writeback] please call the {CRM_WRITEBACK_ACTION} tool again with arguments "
        "body='demo note body', record_id='001000000000001AAA', title='demo note title'"
    )


def test_crm_action_name_matches_reference_action() -> None:
    """Cross-package string-duplication pin: ``arc.CRM_WRITEBACK_ACTION`` must
    stay in lockstep with the real ``crm_note_definition`` ToolSpec name."""
    from ne.runtime.reference_actions import crm_note_definition

    assert crm_note_definition(FakeCrmClient()).spec.name == CRM_WRITEBACK_ACTION


def test_full_arc_live_crm_writeback(tmp_path: Path) -> None:
    """The FULL fixture arc over the real governed spine with the Beat-4 tool
    SWAPPED onto the real NE-050 ``write.internal.crm.note`` and seam-injected
    fakes.

    NE-CS-SEND (owner decision 2026-08-27): the note is genuinely TIER-1 now —
    autonomous + digest — so the swapped Beat 4 is the AUTONOMOUS pair: the
    write executes immediately with exactly ONE Note create (ParentId ==
    record_id, closed field set, VERBATIM body — no approval, hence no
    provenance stamp), and the control step proves NO approval was ever queued.
    The deny→HITL-approve exhibit lives on ``write.external.email.send``.
    """
    from ne.api.platform import WriteActionClients

    subset = demo_doc_subset()
    scenario = build_scenario().model_copy(update={"doc_ids": subset})
    fake_crm = FakeCrmClient()
    clients = WriteActionClients(crm=fake_crm, ticket=FakeTicketClient(), draft=FakeDraftClient())
    h = build_demo_app(tmp_path, write_action_clients=clients)
    _bind_demo_graph_acl_stamps(h)
    written = _seed_corpus(h, tmp_path, subset)
    assert written > 0

    call_args = {
        "record_id": "001000000000001AAA",
        "title": "demo note title",
        "body": "demo note body",
    }

    async def _drive():
        async with h.client() as client:
            return await run_demo_arc(
                client,
                _bearers(h, scenario),
                scenario,
                audit_verify=_audit_verify_callable(h.audit_url),
                writeback_action=CRM_WRITEBACK_ACTION,
                writeback_args=call_args,
            )

    transcript = run(_drive())
    for engine in get_state(h.app).engines:
        engine.dispose()

    steps = _by_name(transcript)
    auto = steps["autonomous_write"]
    control = steps["no_gate_control"]
    assert auto.status is StepStatus.OK, auto.response_excerpt
    assert control.status is StepStatus.OK, control.response_excerpt
    assert control.evidence["pending_after"] is False
    assert not transcript.has_failure()

    audit_engine = sa.create_engine(h.audit_url)
    try:
        with audit_engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_audit_log.c.outcome, ne_audit_log.c.payload).where(
                    ne_audit_log.c.action == "tool.execute"
                )
            ).all()
            granted = conn.execute(
                sa.select(ne_audit_log.c.actor, ne_audit_log.c.payload).where(
                    ne_audit_log.c.action == "approval.granted"
                )
            ).all()
    finally:
        audit_engine.dispose()

    # The Tier-1 write consumed NO approval: the gate's own decision records are
    # absent, which is the independent source of truth that no human was in this
    # loop (the control step asserted the same from the pending queue).
    assert granted == []

    # Exactly ONE create, with ParentId/Title exact and the field set CLOSED.
    assert len(fake_crm.calls) == 1
    sobject, fields = fake_crm.calls[0]
    assert sobject == "Note"
    assert set(fields) == {"ParentId", "Title", "Body"}
    assert fields["ParentId"] == call_args["record_id"]
    assert fields["Title"] == call_args["title"]

    # NE-CRM-NOTE-PROVENANCE, the no-approval half: with no consumed approval
    # the written Body is BYTE-IDENTICAL to the caller's text — a stamp naming
    # nobody, or a placeholder, would be a misattribution.
    assert fields["Body"] == call_args["body"]
    targets = [
        json.loads(row.payload.get("detail") or "{}").get("target")
        for row in rows
        if row.outcome == "success"
    ]
    assert "003000000000001AAA" in targets
