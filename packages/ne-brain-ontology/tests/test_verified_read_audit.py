# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-164b: read-path auditing on ``execute_template`` (T3 verified-answer reads).

Mirrors the shipped T1/T2 read-path audit discipline (NE-164's
``ne.brain.retriever.HybridRetriever._emit_read_audit`` and NE-164b's
``ne.brain_graph.read.EntityGraph._emit_read_audit``): every COMPLETED
``execute_template`` call, when an ``AuditSink`` is injected, emits exactly ONE
config_hash-stamped ``knowledge.verified_read`` ``AuditEvent`` — but, unlike the
T1/T2 events, carrying NO ``acl_trim_delta``: the C7 trim happens A-PRIORI via the
``DatasetSpec`` graph scoping (the query can never see a forbidden graph), so
there is no countable trimmed-row delta to report.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.verified import (
    build_verified_answer_tools,
    execute_template,
    load_template_registry,
)
from ne.brain_ontology.verified.errors import VerifiedAnswerArgsError
from ne.contracts import ConfigHashNotFrozenError, freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.contracts.governance import AuditVerification

from .verified_helpers import (
    FIXTURES_DIR,
    RecordingStore,
    StubExpander,
    account,
    make_gateway,
    principal,
    seed_accounts,
)

_reg = load_template_registry(FIXTURES_DIR)
_TPL = {t.template_id: t for t in _reg.templates}


class _DemoSettings(NeSettings):
    demo: int = 7


@pytest.fixture
def frozen_config() -> Any:
    """Freeze a deterministic config_hash for the duration of a test."""
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


class RecordingAuditSink:
    """Test-double ``AuditSink`` that records every appended event verbatim."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, *, start: Any = None, end: Any = None) -> AuditVerification:
        raise NotImplementedError


class _FailingAuditSink:
    """An ``AuditSink`` whose ``append`` always raises (fail-closed test double)."""

    async def append(self, event: Any) -> None:
        raise RuntimeError("audit store unreachable")

    async def verify(self, *, start: Any = None, end: Any = None) -> AuditVerification:
        raise NotImplementedError


def test_execute_template_emits_verified_read_with_rows_and_trace_id(frozen_config: str) -> None:
    sink = RecordingAuditSink()
    trace_id = "b" * 32

    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_alpha", ["alpha"]), account("acc_beta", ["beta"])])
        template = _TPL["accounts-directory"]
        answer = await execute_template(
            template,
            {},
            store=gw,
            expander=StubExpander(),
            principal=principal("u1", ("alpha",)),
            config_hash="a" * 64,
            max_rows=500,
            audit_sink=sink,
            trace_id=trace_id,
        )
        assert len(answer.rows) == 1  # only acc_alpha is visible to an alpha-only caller
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.action == "knowledge.verified_read"
        assert event.resource == "knowledge://verified"
        assert event.outcome == "success"
        assert event.actor == "u1"
        expected = (
            f"rows={len(answer.rows)} row_limit={answer.row_limit} "
            "truncated=false truncation_reason=none"
        )
        assert event.detail == expected
        assert event.trace_id == trace_id
        assert event.config_hash == frozen_config

    asyncio.run(scenario())


def test_detail_carries_truncation_reason_rows_and_tokens(frozen_config: str) -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(
            gw,
            [account("acc_a", ["alpha"]), account("acc_b", ["alpha"]), account("acc_c", ["alpha"])],
        )
        p = principal("u1", ("alpha",))
        template = _TPL["accounts-directory"]

        # Row-cap truncation -> reason=rows (counts/class tokens only, C12).
        rows_sink = RecordingAuditSink()
        await execute_template(
            template,
            {},
            store=gw,
            expander=StubExpander(),
            principal=p,
            config_hash="a" * 64,
            max_rows=1,
            audit_sink=rows_sink,
        )
        assert rows_sink.events[-1].detail.endswith("truncated=true truncation_reason=rows")

        # Token-budget truncation -> reason=tokens.
        tok_sink = RecordingAuditSink()
        await execute_template(
            template,
            {},
            store=gw,
            expander=StubExpander(),
            principal=p,
            config_hash="a" * 64,
            max_rows=500,
            max_result_tokens=45,
            audit_sink=tok_sink,
        )
        assert tok_sink.events[-1].detail.endswith("truncated=true truncation_reason=tokens")

        # No truncation -> reason=none.
        none_sink = RecordingAuditSink()
        await execute_template(
            template,
            {},
            store=gw,
            expander=StubExpander(),
            principal=p,
            config_hash="a" * 64,
            max_rows=500,
            audit_sink=none_sink,
        )
        assert none_sink.events[-1].detail.endswith("truncated=false truncation_reason=none")
        # C12: no row value / label / IRI leaks into the class-token detail.
        for detail in (rows_sink, tok_sink, none_sink):
            assert "Corp" not in detail.events[-1].detail
            assert "urn:ne:entity" not in detail.events[-1].detail

    asyncio.run(scenario())


def test_event_payload_leaks_no_labels_rows_or_iris(frozen_config: str) -> None:
    sink = RecordingAuditSink()

    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_alpha", ["alpha"]), account("acc_beta", ["beta"])])
        template = _TPL["accounts-directory"]
        await execute_template(
            template,
            {},
            store=gw,
            expander=StubExpander(),
            principal=principal("u1", ("alpha",)),
            config_hash="a" * 64,
            max_rows=500,
            audit_sink=sink,
        )
        payload = json.dumps(sink.events[-1].model_dump(mode="json"))
        for leaked in (
            "alpha",
            "beta",
            "Corp",
            "urn:ne:entity",
            "SELECT",
            template.sparql,
            "accounts-directory",
        ):
            assert leaked not in payload

    asyncio.run(scenario())


def test_failed_args_validation_emits_no_event(frozen_config: str) -> None:
    sink = RecordingAuditSink()

    async def scenario() -> None:
        gw = make_gateway()
        rec = RecordingStore(gw)
        await seed_accounts(gw, [account("acc_alpha", ["alpha"])])
        template = _TPL["accounts-by-name"]
        with pytest.raises(VerifiedAnswerArgsError):
            await execute_template(
                template,
                {"q_name": '"} UNION {'},  # violates the strict pattern
                store=rec,
                expander=StubExpander(),
                principal=principal("u1", ("alpha",)),
                config_hash="a" * 64,
                max_rows=500,
                audit_sink=sink,
            )
        assert sink.events == []
        assert rec.scoped_calls == 0  # rejected before any store touch

    asyncio.run(scenario())


def test_no_sink_default_behaves_identically() -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_alpha", ["alpha"])])
        template = _TPL["accounts-directory"]
        answer = await execute_template(
            template,
            {},
            store=gw,
            expander=StubExpander(),
            principal=principal("u1", ("alpha",)),
            config_hash="a" * 64,
            max_rows=500,
        )
        assert len(answer.rows) == 1

    asyncio.run(scenario())


def test_sink_append_failure_fails_closed(frozen_config: str) -> None:
    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_alpha", ["alpha"])])
        template = _TPL["accounts-directory"]
        with pytest.raises(RuntimeError):
            await execute_template(
                template,
                {},
                store=gw,
                expander=StubExpander(),
                principal=principal("u1", ("alpha",)),
                config_hash="a" * 64,
                max_rows=500,
                audit_sink=_FailingAuditSink(),
            )

    asyncio.run(scenario())


def test_unfrozen_config_hash_fails_loud() -> None:
    sink = RecordingAuditSink()

    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_alpha", ["alpha"])])
        template = _TPL["accounts-directory"]
        with pytest.raises(ConfigHashNotFrozenError):
            await execute_template(
                template,
                {},
                store=gw,
                expander=StubExpander(),
                principal=principal("u1", ("alpha",)),
                config_hash="a" * 64,
                max_rows=500,
                audit_sink=sink,
            )
        assert sink.events == []

    asyncio.run(scenario())


def test_factory_threads_sink_and_trace_provider(frozen_config: str) -> None:
    sink = RecordingAuditSink()

    async def scenario() -> None:
        gw = make_gateway()
        await seed_accounts(gw, [account("acc_alpha", ["alpha"])])
        defs = build_verified_answer_tools(
            registry=_reg,
            store=gw,
            expander=StubExpander(),
            principal_provider=lambda: principal("u1", ("alpha",)),
            settings=OntologySettings(verified_max_rows=500),
            config_hash="a" * 64,
            audit_sink=sink,
            trace_id_provider=lambda: "c" * 32,
        )
        directory = next(d for d in defs if d.spec.name == "read.kg.accounts_directory")
        await directory.handler({})
        assert len(sink.events) == 1
        assert sink.events[0].trace_id == "c" * 32

    asyncio.run(scenario())
