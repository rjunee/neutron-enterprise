# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The grounded-agent runtime POST-step (NE-GA-G3) — the symmetric mirror of
NE-VAF-U3's ``VerifiedRouter`` PRE-step.

Fakes stand in for the INJECTED ``guardrail_engine``/``render_fallback``
seams :class:`~ne.runtime.grounding.GroundingPolicy` carries — G2's real
``ne.govern.guardrails.grounding.GroundingGuardrail`` is a PARALLEL, unmerged
lane and MUST NOT be imported here. The REAL G1 gate
(``ne.govern.grounding.check_grounding``/``GroundingCorpus``) runs end-to-end
via the ``ne.runtime.grounding`` src import — every PASS/FAIL assertion below
is faithful to production grounding semantics, not a stubbed verdict.
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelResponse, TextPart

from ne.contracts.runtime import ToolSpec, VerifiedProvenance
from ne.contracts.types import Principal
from ne.govern.errors import AuditEmissionError, GuardrailViolation
from ne.govern.grounding import GATE_VERSION
from ne.govern.grounding_policy import (
    PROFILE_NG1_FROZEN,
    PROFILE_NG2,
    GateProfile,
    StrictnessTier,
)
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import RuntimeAuditError
from ne.runtime.factory import create_department_session
from ne.runtime.grounding import (
    FALLBACK_MARKER,
    GroundingPolicy,
    GroundingRecorder,
    current_answer_recorder,
    run_grounding_gate,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.runtime.tools import ToolBinding
from ne.runtime.toolset import ToolsetScopedExecutor

from .conftest import (
    FailingAuditSink,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_executor,
    make_pdp,
    make_principal,
    standard_bindings,
    text_completion,
    tool_call_completion,
)

# --- fixtures: a verified-answer's JSON shape + hand-verified prose ---------

PROMPT_PASS = "how many orders does acme corp have"

#: Grounds cleanly: "acme"/"corp"/"orders" are corpus vocabulary, "has" is on
#: CONNECTIVE_ALLOWLIST, and the numeral 3 is a "cell" fact whose full
#: row-identity {acme, corp} is present in-sentence (F3 row-binding).
DRAFT_PASS = "Acme Corp has 3 orders"

#: Fails: "Initech"/"Global"/"open" are vocab-ungrounded, 73 is
#: numeral-ungrounded (no corpus number canonicalizes to "73").
DRAFT_FAIL = "Initech Global has 73 open orders."

#: A PASS-polish candidate (NE-GA-G4): the SAME grounded content as
#: DRAFT_PASS with a citation marker inserted (1 <= 1 == corpus.citation_count
#: from answer_json()'s single citation) — re-checks as PASS too.
POLISHED = "Acme Corp has 3 orders [1]"

#: A PASS-polish candidate that SMUGGLES a fabricated, numeral-ungrounded
#: fact (99 canonicalizes to no corpus number) — re-check must FAIL.
SMUGGLED = "Acme Corp has 99 orders"

#: A FAIL-re-draft candidate that STILL fabricates (both vocab- and
#: numeral-ungrounded against the accounts-directory corpus) — the second
#: FAIL that must fall through to the deterministic table.
SMUGGLED2 = "Initech has 12 orders"

#: The full FindingReason class vocabulary (ne.govern.grounding.FindingReason)
#: — the FAIL-re-draft assist call's `reasons` kwarg must be a subset of this.
_FINDING_REASON_CLASSES = frozenset(
    {
        "citation-out-of-range",
        "iri-ungrounded",
        "numeral-ungrounded",
        "numeral-unparseable",
        "phrase-recombination",
        "vocab-ungrounded",
    }
)


def answer_json(**overrides: Any) -> str:
    base: dict[str, Any] = {
        "template_id": "accounts-directory",
        "template_version": 3,
        "config_hash": "deadbeef",
        "args": {},
        "columns": ["account", "orders"],
        "rows": [["Acme Corp", "3"]],
        "citations": ["urn:ne:entity:acme"],
        "truncated": False,
        "row_limit": 50,
        "ontology_version": "v1",
        "executed_at": "2026-07-16T00:00:00Z",
    }
    base.update(overrides)
    return json.dumps(base)


def render_fallback(answers: Sequence[Mapping[str, Any]]) -> str:
    """The injected fallback renderer fake — deterministic, template-id keyed."""
    return "VERIFIED-FALLBACK:" + ",".join(str(a["template_id"]) for a in answers)


class GroundedGateEngine:
    """A ``GuardrailEnforcerLike`` fake mirroring G2's ungrounded-claim semantics
    WITHOUT importing G2 (unmerged parallel lane): raises ``GuardrailViolation``
    on a genuine ``grounded_gate=True, grounded=False`` payload, else records
    and returns. ``raise_exc`` overrides with an arbitrary exception (used to
    drive the ``AuditEmissionError``-propagates test)."""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.payloads: list[dict[str, Any]] = []
        self._raise_exc = raise_exc

    async def enforce(self, payload: Mapping[str, Any]) -> object:
        self.payloads.append(dict(payload))
        if self._raise_exc is not None:
            raise self._raise_exc
        if payload.get("grounded_gate") is True and payload.get("grounded") is False:
            raise GuardrailViolation(())
        return None


class FakeAssist:
    """A test double for the INJECTED ``GroundingPolicy.assist`` seam
    (NE-GA-G4): returns a scripted ``result`` string, or RAISES it verbatim
    when ``result`` is an ``Exception`` (drives the assist-degrades-to-
    baseline tests). Every call's keyword arguments are recorded in call
    order so tests can assert on call COUNT (at-most-once) and the EXACT
    kwarg shape (structural C12 — the FAIL call must never carry a `draft`
    kwarg)."""

    def __init__(self, result: str | Exception) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _policy(
    engine: GroundedGateEngine,
    sink: Any,
    *,
    assist: Any = None,
    strictness: StrictnessTier = StrictnessTier.STRICT,
    profile: GateProfile = PROFILE_NG1_FROZEN,
) -> GroundingPolicy:
    return GroundingPolicy(
        granted_tools=("read.docs.search",),
        render_fallback=render_fallback,
        guardrail_engine=engine,
        sink=sink,
        pdp=make_pdp(sink),
        assist=assist,
        strictness=strictness,
        profile=profile,
    )


def _queued_read_docs_bindings(log: ToolLog, results: Sequence[str]) -> list[ToolBinding]:
    """``standard_bindings`` with ``read.docs.search`` returning ``results`` in
    order (repeating the last entry once exhausted)."""
    queue = list(results)

    async def _run(args: Mapping[str, Any]) -> str:
        log.calls.append(("read.docs.search", dict(args)))
        raw = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else answer_json())
        # NE-CTXB-5: the verified-read handler records the STRUCTURED answer to the
        # active per-run recorder DIRECTLY (§4.2) — the recorder no longer parses the
        # return string. This fake stands in for that handler, so it records too.
        record = current_answer_recorder()
        if record is not None:
            record(json.loads(raw))
        return raw

    bindings = standard_bindings(log)
    return [
        ToolBinding(spec=b.spec, handler=_run) if b.spec.name == "read.docs.search" else b
        for b in bindings
    ]


class _UnusedGate:
    async def request_approval(self, request: Any) -> Any:
        raise AssertionError("approval gate was consulted on a grounded-agent path")


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_runtime_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _grounded_session(
    db_engine: sa.Engine,
    gw: ScriptedGateway,
    sink: Any,
    log: ToolLog,
    *,
    policy: GroundingPolicy,
    fetch_results: Sequence[str] = (),
) -> DepartmentAgentSession:
    """Mirrors ``test_agent_loop.py``'s ``_session`` helper, wired for grounding:
    the SESSION's executor is a ``ToolsetScopedExecutor`` over a REAL
    ``GovernedToolExecutor`` (the full C6/C12 governed pipeline runs for every
    verified fetch); the per-run ``GroundingRecorder`` is built INSIDE
    ``session.run`` (not here) — this helper never constructs one."""
    store = SqlAlchemySessionStore(db_engine)
    results = fetch_results or (answer_json(),)
    base_executor = make_executor(
        _queued_read_docs_bindings(log, results), sink, approval_gate=_UnusedGate()
    )
    scoped = ToolsetScopedExecutor(
        base_executor,
        granted=policy.granted_tools,
        sink=sink,
        scope="grounded:assistant",
        pdp=make_pdp(sink),
    )
    session_id = asyncio.run(
        store.create_conversation("assistant", "eng", "user:ada")
    ).conversation_id
    return DepartmentAgentSession(
        session_id=session_id,
        agent_name="assistant",
        department="eng",
        owner_principal_id="user:ada",
        store=store,
        executor=scoped,
        gateway=gw,
        settings=RuntimeSettings(),
        grounding=policy,
    )


# --- (a) ungrounded draft: exactly one audited block + fallback ------------


def test_ungrounded_draft_blocked_with_one_audited_block_and_fallback(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "accounts"}), text_completion(DRAFT_FAIL)]
    )
    gate_engine = GroundedGateEngine()
    session = _grounded_session(engine, gw, sink, log, policy=_policy(gate_engine, sink))

    result = asyncio.run(session.run(PROMPT_PASS, principal=make_principal(department="eng")))

    assert len(gate_engine.payloads) == 1
    payload = gate_engine.payloads[0]
    assert payload["grounded_gate"] is True
    assert payload["grounded"] is False
    assert payload["tool"] == "grounding.gate"
    assert "numeral-ungrounded" in payload["reasons"]
    assert "vocab-ungrounded" in payload["reasons"]
    assert payload["finding_count"] > 0

    assert result.output == "VERIFIED-FALLBACK:accounts-directory"
    assert "Initech" not in result.output
    assert "73" not in result.output
    assert result.grounded is not None
    assert result.grounded.fallback is True
    assert result.grounded.gate == GATE_VERSION
    assert result.grounded.assist_used is False


# --- (b) grounded draft passes unchanged, with fetch provenance -------------


def test_grounded_draft_passes_unchanged_with_fetch_provenance(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "accounts"}), text_completion(DRAFT_PASS)]
    )
    gate_engine = GroundedGateEngine()
    session = _grounded_session(engine, gw, sink, log, policy=_policy(gate_engine, sink))

    result = asyncio.run(session.run(PROMPT_PASS, principal=make_principal(department="eng")))

    assert result.output == DRAFT_PASS
    assert result.grounded is not None
    assert result.grounded.fallback is False
    assert result.grounded.fetches == (
        VerifiedProvenance(
            template_id="accounts-directory",
            template_version=3,
            config_hash="deadbeef",
            row_count=1,
            truncated=False,
        ),
    )
    assert gate_engine.payloads == []
    assert result.grounded.citation_count == 1


# --- envelope keys flow recorder -> corpus -> gate end-to-end (NE-CTXB-4) ---


def test_envelope_keys_flow_through_gate_end_to_end(engine: sa.Engine) -> None:
    """The truncation-honesty seam: a recorded tool return carrying the
    envelope keys (returned_rows/total_rows/truncated) flows through the
    recorder -> corpus -> G1 gate, and an honest envelope draft passes with
    grounded.gate == GATE_VERSION. Pins the additive-key tolerance at the
    runtime seam (the recorder still captures the answer despite the new
    keys) and proves envelope facts mint under truncation."""
    log = ToolLog()
    sink = RecordingAuditSink()
    # base has exactly 1 row, so returned_rows=1 matches len(rows) and mints.
    truncated_answer = answer_json(truncated=True, returned_rows=1, total_rows=500)
    gw = ScriptedGateway(
        [
            tool_call_completion("read.docs.search", {"q": "accounts"}),
            text_completion("Returned 1 of 500 accounts"),
        ]
    )
    gate_engine = GroundedGateEngine()
    session = _grounded_session(
        engine, gw, sink, log, policy=_policy(gate_engine, sink), fetch_results=(truncated_answer,)
    )

    result = asyncio.run(session.run(PROMPT_PASS, principal=make_principal(department="eng")))

    # The honest envelope draft passes: not routed to the deterministic table.
    assert result.output == "Returned 1 of 500 accounts"
    assert FALLBACK_MARKER not in (result.grounded.gate if result.grounded else "")
    assert result.grounded is not None
    assert result.grounded.fallback is False
    assert result.grounded.gate == GATE_VERSION
    assert gate_engine.payloads == []  # a PASS never audits a block
    # additive-key tolerance: the recorder still captured the truncated answer.
    assert result.grounded.fetches == (
        VerifiedProvenance(
            template_id="accounts-directory",
            template_version=3,
            config_hash="deadbeef",
            row_count=1,
            truncated=True,
        ),
    )


# --- (c) a prior turn's fetch never grounds this turn -----------------------


def test_prior_turn_fetch_never_grounds_this_turn(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [
            tool_call_completion("read.docs.search", {"q": "accounts"}),
            text_completion(DRAFT_PASS),
            text_completion(DRAFT_PASS),
        ]
    )
    gate_engine = GroundedGateEngine()
    session = _grounded_session(engine, gw, sink, log, policy=_policy(gate_engine, sink))

    turn1 = asyncio.run(session.run(PROMPT_PASS, principal=make_principal(department="eng")))
    assert turn1.grounded is not None
    assert turn1.grounded.fallback is False

    turn2 = asyncio.run(session.run("and now?", principal=make_principal(department="eng")))
    assert turn2.grounded is not None
    assert turn2.grounded.fallback is True
    assert turn2.grounded.fetches == ()


# --- empty-corpus fail-closed invariant (no fetch this turn) ----------------


def test_no_fetch_turn_fails_closed_to_fallback(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion(DRAFT_PASS)])
    gate_engine = GroundedGateEngine()
    session = _grounded_session(engine, gw, sink, log, policy=_policy(gate_engine, sink))

    result = asyncio.run(session.run("summary please", principal=make_principal(department="eng")))

    assert result.grounded is not None
    assert result.grounded.fallback is True
    assert result.grounded.fetches == ()
    assert len(gate_engine.payloads) == 1


# --- (d) the ungrounded draft never persists into history -------------------


def test_ungrounded_draft_never_persisted_to_history(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "accounts"}), text_completion(DRAFT_FAIL)]
    )
    gate_engine = GroundedGateEngine()
    session = _grounded_session(engine, gw, sink, log, policy=_policy(gate_engine, sink))

    result = asyncio.run(session.run(PROMPT_PASS, principal=make_principal(department="eng")))

    history = asyncio.run(
        session._store.load_conversation_history(
            session.session_id, "user:ada", "eng", "assistant", limit=10
        )
    )
    last = history[-1]
    assert isinstance(last, ModelResponse)
    assert last.model_name == FALLBACK_MARKER
    assert isinstance(last.parts[0], TextPart)
    assert last.parts[0].content == result.output

    serialized = repr(history)
    assert "Initech" not in serialized
    # Assert the fabricated numeral IN CONTEXT is absent. A bare "73" substring
    # collides with digits in the serialized turn timestamps (TzInfo/datetime),
    # so check the distinctive draft phrase instead — it can never appear in a
    # timestamp and still proves the ungrounded number was withheld from history.
    assert "73 open orders" not in serialized
    assert DRAFT_FAIL not in serialized


# --- (e) an out-of-set tool call becomes a BLOCKED result, loop continues ---


def test_out_of_set_tool_call_becomes_blocked_result_and_loop_continues(
    engine: sa.Engine,
) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [
            tool_call_completion("read.docs.search", {"q": "accounts"}),
            tool_call_completion("write.internal.notes.append", {"note": "x"}),
            text_completion(DRAFT_PASS),
        ]
    )
    gate_engine = GroundedGateEngine()
    session = _grounded_session(engine, gw, sink, log, policy=_policy(gate_engine, sink))

    result = asyncio.run(session.run(PROMPT_PASS, principal=make_principal(department="eng")))

    assert "write.internal.notes.append" not in log.names()
    assert sink.with_action("tool.toolset_denied")
    assert result.output == DRAFT_PASS
    assert result.grounded is not None
    assert result.grounded.fallback is False
    assert len(result.grounded.fetches) == 1


# --- (f) AuditEmissionError from enforce propagates, kills the run ---------


def test_audit_emission_error_from_enforce_kills_run(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "accounts"}), text_completion(DRAFT_FAIL)]
    )
    gate_engine = GroundedGateEngine(raise_exc=AuditEmissionError("sink down"))
    session = _grounded_session(engine, gw, sink, log, policy=_policy(gate_engine, sink))

    with pytest.raises(AuditEmissionError):
        asyncio.run(session.run(PROMPT_PASS, principal=make_principal(department="eng")))

    history = asyncio.run(session._store.load_history(session.session_id, limit=10))
    assert history == []


# --- (g) grounding=None leaves the model path byte-identical ----------------


def test_grounding_none_leaves_model_path_unchanged(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("final answer")])
    store = SqlAlchemySessionStore(engine)
    executor = make_executor(standard_bindings(log), sink, approval_gate=_UnusedGate())
    session = DepartmentAgentSession(
        session_id=asyncio.run(
            store.create_conversation("assistant", "eng", "user:ada")
        ).conversation_id,
        agent_name="assistant",
        department="eng",
        owner_principal_id="user:ada",
        store=store,
        executor=executor,
        gateway=gw,
        settings=RuntimeSettings(),
    )

    result = asyncio.run(session.run("hello", principal=make_principal(department="eng")))

    assert result.output == "final answer"
    assert result.grounded is None
    assert result.verified is None
    assert gw.requests  # the model ran

    history = asyncio.run(
        store.load_conversation_history(
            session.session_id, "user:ada", "eng", "assistant", limit=10
        )
    )
    last = history[-1]
    assert isinstance(last, ModelResponse)
    assert isinstance(last.model_name, str)
    assert last.model_name.startswith("ne-gateway:")


# --- (h) recorder ignores non-VerifiedAnswer / BLOCKED returns --------------


class _ScriptedExecutor:
    """A local ``ToolExecutorLike`` fake returning a scripted sequence, verbatim."""

    def __init__(self, returns: Sequence[str]) -> None:
        self._queue = list(returns)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return ()

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        self.calls.append((tool, dict(args)))
        return self._queue.pop(0)


def test_record_answer_structured_api() -> None:
    # execute NO LONGER parses the return string: a VerifiedAnswer-JSON return that
    # is never recorded via the direct API contributes nothing.
    inner = _ScriptedExecutor([answer_json()])
    recorder = GroundingRecorder(inner)
    out = asyncio.run(recorder.execute("read.docs.search", {}, principal=make_principal()))
    assert out == answer_json()
    assert recorder.answers == ()  # the string return was NOT captured

    # record_answer appends a shape-gated mapping.
    recorder.record_answer(json.loads(answer_json()))
    assert len(recorder.answers) == 1
    assert recorder.answers[0]["template_id"] == "accounts-directory"

    # A malformed (non-VerifiedAnswer-shaped) mapping raises loudly.
    with pytest.raises(ValueError, match="VerifiedAnswer-shaped"):
        recorder.record_answer({"template_id": "x", "template_version": 1})
    # Envelope-bearing mappings are tolerated (no new required key).
    enveloped = dict(
        json.loads(answer_json()), returned_rows=1, total_rows=1, truncation_reason=None
    )
    recorder.record_answer(enveloped)
    assert len(recorder.answers) == 2


class _ContextVarInner:
    """A ``ToolExecutorLike`` fake that records via the ACTIVE recorder ContextVar
    (mirrors the verified-read handler's §4.2 behavior), then returns compact text."""

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return ()

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        record = current_answer_recorder()
        assert record is not None  # a recorder window is active during execute
        record(json.loads(answer_json()))
        return "[showing 1 of 1 rows]\naccount | orders"


def test_current_answer_recorder_contextvar_window() -> None:
    assert current_answer_recorder() is None  # None outside any recorder window
    recorder = GroundingRecorder(_ContextVarInner())
    out = asyncio.run(recorder.execute("read.docs.search", {}, principal=make_principal()))
    assert out.startswith("[showing 1 of 1 rows]")
    assert len(recorder.answers) == 1
    assert recorder.answers[0]["template_id"] == "accounts-directory"
    assert current_answer_recorder() is None  # reset after the window closes


def test_corpus_equivalent_across_recorder_paths() -> None:
    """The §4.2 recorder flip is BEHAVIOR-PRESERVING for the grounding corpus.

    RECONCILIATION A (NE-CTXB-4). Envelope keys are CORPUS-VISIBLE by design
    post-NE-CTXB-4: the grounding gate's ``_parse_envelope`` mints a
    ``NumberFact(kind='envelope')`` from returned_rows/total_rows/row_limit so an
    honest 'showing 3 of 500' is speakable. The old
    ``from_answers([enveloped]) == from_answers([stripped])`` invisibility
    invariant is therefore FALSE and dead. What THIS unit actually guarantees is
    reframed here: the new structured-record recorder path (§4.2 — the recorder
    stops parsing the model-visible string and captures the structured mapping
    directly) yields the SAME ``GroundingCorpus`` as the old string-parse path
    for the SAME answer. This pins recorder-path equivalence, NOT invisibility.
    """
    from ne.govern.grounding import GroundingCorpus, _parse_verified_answer
    from ne.runtime.grounding import _parse_answer_mapping

    base = json.loads(answer_json())
    enveloped = dict(
        base, returned_rows=len(base["rows"]), total_rows=len(base["rows"]), truncation_reason=None
    )

    for mapping in (base, enveloped):
        # (a) OLD string-parse path: the deleted recorder json.loads'd the tool-
        # return STRING and handed the parsed mapping to the corpus. Round-trip
        # through JSON to faithfully simulate that now-removed path.
        old_parsed = _parse_answer_mapping(json.loads(json.dumps(mapping)))
        assert old_parsed is not None
        old_corpus = GroundingCorpus.from_answers([old_parsed])

        # (b) NEW structured path: the handler records the structured mapping
        # DIRECTLY via the recorder; the corpus is built from recorder.answers.
        recorder = GroundingRecorder(_ScriptedExecutor([answer_json()]))
        recorder.record_answer(mapping)
        new_corpus = GroundingCorpus.from_answers(recorder.answers)

        # The recorder flip is behavior-preserving: SAME corpus either way.
        assert old_corpus == new_corpus

    # Extra-key tolerance pinned on BOTH parsers (neither gains a required key).
    assert _parse_verified_answer(enveloped) is not None
    assert _parse_answer_mapping(enveloped) is not None

    # Positive envelope-VISIBILITY pin, so the dead invisibility invariant can
    # never be reintroduced: the enveloped corpus mints a kind='envelope'
    # NumberFact for the total_rows numeral. ``numbers`` is a mapping
    # numeral-string -> tuple[NumberFact, ...], each with a ``.kind`` attribute.
    enveloped_corpus = GroundingCorpus.from_answers([enveloped])
    total_numeral = str(enveloped["total_rows"])
    assert total_numeral in enveloped_corpus.numbers
    assert any(fact.kind == "envelope" for fact in enveloped_corpus.numbers[total_numeral])


# --- (i) GroundedProvenance.fetches == the recorder's captured answers ------


def test_provenance_fetches_equal_recorder_answers(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [
            tool_call_completion("read.docs.search", {"q": "accounts"}),
            tool_call_completion("read.docs.search", {"q": "orders"}),
            text_completion("Acme Corp has 3 orders and Widget has 5 total."),
        ]
    )
    second = answer_json(
        template_id="orders-directory",
        config_hash="beefdead",
        columns=["item", "total"],
        rows=[["Widget", "5"]],
        citations=["urn:ne:entity:widget"],
    )
    gate_engine = GroundedGateEngine()
    session = _grounded_session(
        engine,
        gw,
        sink,
        log,
        policy=_policy(gate_engine, sink),
        fetch_results=(answer_json(), second),
    )

    result = asyncio.run(
        session.run("give me accounts and orders info", principal=make_principal(department="eng"))
    )

    assert result.grounded is not None
    assert result.grounded.fallback is False
    assert len(result.grounded.fetches) == 2
    assert result.grounded.fetches[0] == VerifiedProvenance(
        template_id="accounts-directory",
        template_version=3,
        config_hash="deadbeef",
        row_count=1,
        truncated=False,
    )
    assert result.grounded.fetches[1] == VerifiedProvenance(
        template_id="orders-directory",
        template_version=3,
        config_hash="beefdead",
        row_count=1,
        truncated=False,
    )


# --- C3 guardian: no ne.brain_ontology import anywhere in grounding.py ------


def test_grounding_module_imports_no_brain_ontology() -> None:
    import ne.runtime.grounding as mod

    source = Path(mod.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("ne.brain_ontology")
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("ne.brain_ontology")


# --- factory: wraps a ToolsetScopedExecutor + threads the policy ------------


def test_factory_wraps_scoped_executor_and_threads_policy(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    store = SqlAlchemySessionStore(engine)
    executor = make_executor(standard_bindings(log), sink, approval_gate=_UnusedGate())
    gate_engine = GroundedGateEngine()
    policy = _policy(gate_engine, sink)

    session = asyncio.run(
        create_department_session(
            "assistant",
            "eng",
            owner_principal_id="user:ada",
            store=store,
            gateway=ScriptedGateway([]),
            executor=executor,
            grounding=policy,
        )
    )

    scoped = session._executor  # pyright: ignore[reportPrivateUsage]
    assert isinstance(scoped, ToolsetScopedExecutor)
    assert {s.name for s in scoped.tool_specs} == {"read.docs.search"}
    assert session._grounding is policy  # pyright: ignore[reportPrivateUsage]


def test_factory_grounding_none_passes_executor_through_unwrapped(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    store = SqlAlchemySessionStore(engine)
    executor = make_executor(standard_bindings(log), sink, approval_gate=_UnusedGate())

    session = asyncio.run(
        create_department_session(
            "assistant",
            "eng",
            owner_principal_id="user:ada",
            store=store,
            gateway=ScriptedGateway([]),
            executor=executor,
        )
    )

    assert session._executor is executor  # pyright: ignore[reportPrivateUsage]
    assert session._grounding is None  # pyright: ignore[reportPrivateUsage]


# =============================================================================
# NE-GA-G4: bounded model-assist (design §4) — single emit point.
# =============================================================================

_FETCHED = (json.loads(answer_json()),)


# --- (a) parity: assist=None never touches render_fallback on PASS ---------


def test_assist_none_pass_never_invokes_render_fallback() -> None:
    def _exploding_render_fallback(answers: Sequence[Mapping[str, Any]]) -> str:
        raise AssertionError("render_fallback must not run on a PASS with assist=None")

    sink = RecordingAuditSink()
    policy = GroundingPolicy(
        granted_tools=("read.docs.search",),
        render_fallback=_exploding_render_fallback,
        guardrail_engine=GroundedGateEngine(),
        sink=sink,
        pdp=make_pdp(sink),
        assist=None,
    )

    gate = asyncio.run(
        run_grounding_gate(
            draft=DRAFT_PASS,
            prompt=PROMPT_PASS,
            principal=make_principal(),
            fetched=_FETCHED,
            policy=policy,
        )
    )

    assert gate.output == DRAFT_PASS
    assert gate.provenance.assist_used is False
    assert gate.provenance.fallback is False


# --- (b) PASS-polish improvement is emitted, assist_used=True --------------


def test_pass_polish_improvement_emitted_with_assist_used() -> None:
    fake = FakeAssist(POLISHED)
    policy = _policy(GroundedGateEngine(), RecordingAuditSink(), assist=fake)

    gate = asyncio.run(
        run_grounding_gate(
            draft=DRAFT_PASS,
            prompt=PROMPT_PASS,
            principal=make_principal(),
            fetched=_FETCHED,
            policy=policy,
        )
    )

    assert gate.output == POLISHED
    assert gate.provenance.assist_used is True
    assert gate.provenance.fallback is False
    assert len(fake.calls) == 1
    assert set(fake.calls[0]) == {"question", "draft", "tables"}
    assert fake.calls[0]["question"] == PROMPT_PASS
    assert fake.calls[0]["draft"] == DRAFT_PASS
    assert fake.calls[0]["tables"] == "VERIFIED-FALLBACK:accounts-directory"


# --- (c) PASS-polish that smuggles a fabricated fact is rejected -----------


def test_pass_polish_smuggled_fact_rejected_original_draft_kept() -> None:
    fake = FakeAssist(SMUGGLED)
    gate_engine = GroundedGateEngine()
    policy = _policy(gate_engine, RecordingAuditSink(), assist=fake)

    gate = asyncio.run(
        run_grounding_gate(
            draft=DRAFT_PASS,
            prompt=PROMPT_PASS,
            principal=make_principal(),
            fetched=_FETCHED,
            policy=policy,
        )
    )

    assert gate.output == DRAFT_PASS
    assert gate.provenance.assist_used is False
    assert "99" not in gate.output
    # PASS branch never audits a block, whether or not an assist ran.
    assert gate_engine.payloads == []


# --- (d) FAIL-re-draft that grounds is emitted, exactly one audited block --


def test_fail_redraft_grounded_emitted_with_single_audited_block() -> None:
    fake = FakeAssist(DRAFT_PASS)
    gate_engine = GroundedGateEngine()
    policy = _policy(gate_engine, RecordingAuditSink(), assist=fake)

    gate = asyncio.run(
        run_grounding_gate(
            draft=DRAFT_FAIL,
            prompt=PROMPT_PASS,
            principal=make_principal(),
            fetched=_FETCHED,
            policy=policy,
        )
    )

    assert gate.output == DRAFT_PASS
    assert gate.provenance.fallback is True
    assert gate.provenance.assist_used is True
    assert len(gate_engine.payloads) == 1
    payload = gate_engine.payloads[0]
    assert payload["grounded_gate"] is True
    assert payload["grounded"] is False
    assert "Initech" not in gate.output
    assert "73" not in gate.output


# --- (d) structural C12: the FAIL call never receives the withheld draft ---


def test_fail_redraft_never_receives_withheld_draft() -> None:
    fake = FakeAssist(DRAFT_PASS)
    gate_engine = GroundedGateEngine()
    policy = _policy(gate_engine, RecordingAuditSink(), assist=fake)

    asyncio.run(
        run_grounding_gate(
            draft=DRAFT_FAIL,
            prompt=PROMPT_PASS,
            principal=make_principal(),
            fetched=_FETCHED,
            policy=policy,
        )
    )

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert "draft" not in call
    assert set(call) == {"question", "tables", "reasons"}
    assert "Initech" not in repr(fake.calls)
    assert "73 open orders" not in repr(fake.calls)
    assert set(call["reasons"]) <= _FINDING_REASON_CLASSES


# --- (e) FAIL-re-draft that fabricates falls to the deterministic table ----


def test_fail_redraft_fabricating_falls_to_table() -> None:
    fake = FakeAssist(SMUGGLED2)
    gate_engine = GroundedGateEngine()
    policy = _policy(gate_engine, RecordingAuditSink(), assist=fake)

    gate = asyncio.run(
        run_grounding_gate(
            draft=DRAFT_FAIL,
            prompt=PROMPT_PASS,
            principal=make_principal(),
            fetched=_FETCHED,
            policy=policy,
        )
    )

    assert gate.output == "VERIFIED-FALLBACK:accounts-directory"
    assert gate.provenance.fallback is True
    assert gate.provenance.assist_used is False
    assert len(gate_engine.payloads) == 1


# --- (g) the assist runs AT MOST ONCE per turn, on either branch -----------


@pytest.mark.parametrize(
    ("draft", "assist_result"),
    [
        pytest.param(DRAFT_PASS, POLISHED, id="pass-scenario"),
        pytest.param(DRAFT_FAIL, DRAFT_PASS, id="fail-scenario"),
    ],
)
def test_assist_called_at_most_once_per_turn(draft: str, assist_result: str) -> None:
    fake = FakeAssist(assist_result)
    policy = _policy(GroundedGateEngine(), RecordingAuditSink(), assist=fake)

    asyncio.run(
        run_grounding_gate(
            draft=draft,
            prompt=PROMPT_PASS,
            principal=make_principal(),
            fetched=_FETCHED,
            policy=policy,
        )
    )

    assert len(fake.calls) == 1


# --- assist can never degrade: an exception falls back to the baseline -----


def test_assist_exception_on_pass_keeps_original_draft() -> None:
    fake = FakeAssist(RuntimeError("assist blew up"))
    policy = _policy(GroundedGateEngine(), RecordingAuditSink(), assist=fake)

    gate = asyncio.run(
        run_grounding_gate(
            draft=DRAFT_PASS,
            prompt=PROMPT_PASS,
            principal=make_principal(),
            fetched=_FETCHED,
            policy=policy,
        )
    )

    assert gate.output == DRAFT_PASS
    assert gate.provenance.assist_used is False
    assert gate.provenance.fallback is False


def test_assist_exception_on_fail_falls_to_table() -> None:
    fake = FakeAssist(RuntimeError("assist blew up"))
    gate_engine = GroundedGateEngine()
    policy = _policy(gate_engine, RecordingAuditSink(), assist=fake)

    gate = asyncio.run(
        run_grounding_gate(
            draft=DRAFT_FAIL,
            prompt=PROMPT_PASS,
            principal=make_principal(),
            fetched=_FETCHED,
            policy=policy,
        )
    )

    assert gate.output == "VERIFIED-FALLBACK:accounts-directory"
    assert gate.provenance.assist_used is False
    assert gate.provenance.fallback is True
    assert len(gate_engine.payloads) == 1


# --- the original FAIL's audited block precedes any assist call ------------


def test_audit_block_precedes_assist_on_fail() -> None:
    fake = FakeAssist(DRAFT_PASS)
    gate_engine = GroundedGateEngine(raise_exc=AuditEmissionError("sink down"))
    policy = _policy(gate_engine, RecordingAuditSink(), assist=fake)

    with pytest.raises(AuditEmissionError):
        asyncio.run(
            run_grounding_gate(
                draft=DRAFT_FAIL,
                prompt=PROMPT_PASS,
                principal=make_principal(),
                fetched=_FETCHED,
                policy=policy,
            )
        )

    assert fake.calls == []


# --- (f) GUARDIAN, part 1: exactly one `return` in run_grounding_gate ------


def test_gate_single_emit_point_ast() -> None:
    import ne.runtime.grounding as mod

    source = Path(mod.__file__).read_text()
    tree = ast.parse(source)

    target: ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_grounding_gate":
            target = node
            break
    assert target is not None, "run_grounding_gate not found"

    returns = [n for n in ast.walk(target) if isinstance(n, ast.Return)]
    assert len(returns) == 1, "run_grounding_gate must have exactly one emit point"


# --- (f) GUARDIAN, part 2: every emitted assist text was gate-checked ------


def test_assist_output_gate_checked_before_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    import ne.runtime.grounding as mod
    from ne.govern.grounding import check_grounding as real_check_grounding

    recorded_drafts: list[str] = []

    def _spy(draft: str, corpus: Any, prompt: str, *, profile: Any = PROFILE_NG1_FROZEN) -> Any:
        recorded_drafts.append(draft)
        return real_check_grounding(draft, corpus, prompt, profile=profile)

    monkeypatch.setattr(mod, "check_grounding", _spy)

    # PASS-polish: the emitted (accepted) candidate must have been re-checked.
    pass_policy = _policy(GroundedGateEngine(), RecordingAuditSink(), assist=FakeAssist(POLISHED))
    pass_gate = asyncio.run(
        run_grounding_gate(
            draft=DRAFT_PASS,
            prompt=PROMPT_PASS,
            principal=make_principal(),
            fetched=_FETCHED,
            policy=pass_policy,
        )
    )
    assert pass_gate.output in recorded_drafts

    # FAIL-re-draft: the emitted (accepted) candidate must have been re-checked.
    recorded_drafts.clear()
    fail_policy = _policy(GroundedGateEngine(), RecordingAuditSink(), assist=FakeAssist(DRAFT_PASS))
    fail_gate = asyncio.run(
        run_grounding_gate(
            draft=DRAFT_FAIL,
            prompt=PROMPT_PASS,
            principal=make_principal(),
            fetched=_FETCHED,
            policy=fail_policy,
        )
    )
    assert fail_gate.output in recorded_drafts


# --- e2e: an accepted PASS-polish surfaces through agent.py ----------------


def test_session_pass_polish_emits_assist_output_end_to_end(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "accounts"}), text_completion(DRAFT_PASS)]
    )
    gate_engine = GroundedGateEngine()
    fake = FakeAssist(POLISHED)
    session = _grounded_session(
        engine, gw, sink, log, policy=_policy(gate_engine, sink, assist=fake)
    )

    result = asyncio.run(session.run(PROMPT_PASS, principal=make_principal(department="eng")))

    assert result.output == POLISHED
    assert result.grounded is not None
    assert result.grounded.assist_used is True
    assert result.grounded.fallback is False

    history = asyncio.run(
        session._store.load_conversation_history(
            session.session_id, "user:ada", "eng", "assistant", limit=10
        )
    )
    last = history[-1]
    assert isinstance(last, ModelResponse)
    assert isinstance(last.model_name, str)
    assert last.model_name.startswith("ne-gateway:")
    # History persists the model's own gate-CERTIFIED passing draft, even
    # though the recertified assist polish is what was emitted as `output`.
    assert DRAFT_PASS in repr(history)


# =============================================================================
# NE-HGATE-2: tiered enforcement + assist observability + the rescue proof.
#
# Every fixture below was verified against the REAL merged ng-2 gate in this
# worktree (probe reproducibility, acceptance §5): a default GroundingPolicy()
# reproduces today's gate byte-for-byte; STANDARD/ng-2 relaxes ONLY non-factual
# vocabulary; the §6.3 rescue pair proves the assist works once the ng-2 wall
# relaxes. LEGAL tier x profile combos only: ng1 x STRICT (default), ng2 x
# STRICT, ng2 x STANDARD — ng1 x STANDARD is HGATE-3's boot ValueError and is
# NEVER constructed here.
# =============================================================================

STANDARD = StrictnessTier.STANDARD
STRICT = StrictnessTier.STRICT

#: The rescue corpus: a two-column ``["account", "open orders"]`` answer whose
#: single row ``["Acme Corp", "3"]`` mints the corpus phrases (so "open"/"orders"
#: ground) and the cell numeral 3. Verified against ng-2 this session.
_RESCUE_FETCHED = (json.loads(answer_json(columns=["account", "open orders"])),)

#: §6.3 owner-required RESCUE draft: at STANDARD/ng-2 "maintains" is a
#: ``vocab-ungrounded-factual`` finding (count=1) in the fact-bearing sentence →
#: the gate WITHHOLDS. Verified.
RESCUE_DRAFT = "Acme Corp maintains 3 open orders."

#: The scripted assist re-draft: passes ng-2 at BOTH tiers (zero findings) — the
#: assist rescues once the ng-2 wall is relaxed. Verified.
RESCUE_CANDIDATE = "Acme Corp: 3 open orders."

#: Advisory-only under STANDARD/ng-2: "Wonderful" is ``vocab-ungrounded`` in a
#: NO-fact sentence → advisory (does not withhold) at STANDARD, but WITHHELD at
#: STRICT. `verdict.passed` is False either way. Verified.
ADVISORY_DRAFT = "Acme Corp has 3 orders. Wonderful."

#: The enforced/advisory split fixture: at STANDARD/ng-2 it carries ENFORCED
#: ``numeral-ungrounded`` (73) + ``vocab-ungrounded-factual`` (Initech/Global)
#: AND advisory ``vocab-ungrounded`` (Wonderful, a no-fact sentence). Verified.
SPLIT_DRAFT = "Initech Global has 73 open orders. Wonderful."


def _run_gate(
    draft: str,
    *,
    policy: GroundingPolicy,
    fetched: Sequence[Mapping[str, Any]] = _FETCHED,
    prompt: str = PROMPT_PASS,
) -> Any:
    return asyncio.run(
        run_grounding_gate(
            draft=draft,
            prompt=prompt,
            principal=make_principal(),
            fetched=fetched,
            policy=policy,
        )
    )


# --- G3-BYTE-PARITY: the regression proof (default == explicit ng1/STRICT) ---


def test_default_policy_byte_parity_with_explicit_ng1_strict() -> None:
    for draft in (DRAFT_PASS, DRAFT_FAIL):
        # A DEFAULT-constructed policy: strictness/profile kwargs OMITTED entirely
        # (proving the trailing defaults keep every constructor byte-compatible).
        default_sink = RecordingAuditSink()
        default_policy = GroundingPolicy(
            granted_tools=("read.docs.search",),
            render_fallback=render_fallback,
            guardrail_engine=GroundedGateEngine(),
            sink=default_sink,
            pdp=make_pdp(RecordingAuditSink()),
        )
        explicit_policy = GroundingPolicy(
            granted_tools=("read.docs.search",),
            render_fallback=render_fallback,
            guardrail_engine=GroundedGateEngine(),
            sink=RecordingAuditSink(),
            pdp=make_pdp(RecordingAuditSink()),
            strictness=StrictnessTier.STRICT,
            profile=PROFILE_NG1_FROZEN,
        )
        default_gate = _run_gate(draft, policy=default_policy)
        explicit_gate = _run_gate(draft, policy=explicit_policy)

        # Same GateResult (output + provenance) AND same provenance dump.
        assert default_gate == explicit_gate
        assert default_gate.provenance.model_dump() == explicit_gate.provenance.model_dump()
        assert default_gate.provenance.gate == GATE_VERSION
        assert default_gate.provenance.strictness == "strict"
        # assist=None never emits the new event on either branch; and the DEFAULT
        # STRICT policy never produces advisory findings (ENFORCEMENT[STRICT] ==
        # all classes), so the grounding.advisory channel stays silent too (C1
        # fix is byte-parity-safe).
        assert default_sink.with_action("grounding.assist") == []
        assert default_sink.with_action("grounding.advisory") == []


# --- tier threading: withhold decided by apply_strictness, not verdict.passed -


def test_standard_tier_advisory_vocab_passes_and_stamps() -> None:
    gate_engine = GroundedGateEngine()
    sink = RecordingAuditSink()
    policy = _policy(gate_engine, sink, strictness=STANDARD, profile=PROFILE_NG2)

    gate = _run_gate(ADVISORY_DRAFT, policy=policy, fetched=_RESCUE_FETCHED)

    # verdict.passed is False (a vocab-ungrounded finding exists) but the tier
    # makes it advisory → NOT withheld → the draft is emitted unchanged.
    assert gate.output == ADVISORY_DRAFT
    assert gate.provenance.fallback is False
    assert gate_engine.payloads == []
    assert gate.provenance.gate == PROFILE_NG2.version
    assert gate.provenance.gate.startswith("ng-2/")
    assert gate.provenance.strictness == "standard"
    assert sink.with_action("grounding.assist") == []
    # Codex C1: the advisory finding ("Wonderful") is not withheld, but it is NOT
    # dropped either — a single counts-only grounding.advisory event records it.
    advisory_events = sink.with_action("grounding.advisory")
    assert len(advisory_events) == 1
    detail = json.loads(advisory_events[0].detail)
    assert detail == {
        "strictness": "standard",
        "advisory_reasons": ["vocab-ungrounded"],
        "advisory_count": 1,
    }


def test_strict_tier_withholds_same_draft_same_profile() -> None:
    gate_engine = GroundedGateEngine()
    policy = _policy(gate_engine, RecordingAuditSink(), strictness=STRICT, profile=PROFILE_NG2)

    gate = _run_gate(ADVISORY_DRAFT, policy=policy, fetched=_RESCUE_FETCHED)

    # SAME draft, SAME profile — only the TIER flipped the outcome.
    assert gate.provenance.fallback is True
    assert gate.provenance.strictness == "strict"
    assert len(gate_engine.payloads) == 1


def test_fail_payload_strictness_and_enforced_advisory_split() -> None:
    gate_engine = GroundedGateEngine()
    policy = _policy(gate_engine, RecordingAuditSink(), strictness=STANDARD, profile=PROFILE_NG2)

    gate = _run_gate(SPLIT_DRAFT, policy=policy, fetched=_RESCUE_FETCHED)

    assert gate.provenance.fallback is True
    assert len(gate_engine.payloads) == 1
    payload = gate_engine.payloads[0]
    assert payload["strictness"] == "standard"
    assert "numeral-ungrounded" in payload["enforced_reasons"]
    assert "vocab-ungrounded-factual" in payload["enforced_reasons"]
    assert payload["advisory_reasons"] == ["vocab-ungrounded"]
    # Legacy keys stay byte-identical and cover ALL findings (advisory recorded).
    assert "vocab-ungrounded" in payload["reasons"]
    assert "numeral-ungrounded" in payload["reasons"]
    assert payload["finding_count"] > 0


# --- THE owner-required §6.3 rescue proof + its pass-polish twin -------------


def test_assist_rescue_fail_redraft_adopted_at_standard_ng2() -> None:
    fake = FakeAssist(RESCUE_CANDIDATE)
    gate_engine = GroundedGateEngine()
    policy = _policy(
        gate_engine, RecordingAuditSink(), assist=fake, strictness=STANDARD, profile=PROFILE_NG2
    )

    gate = _run_gate(RESCUE_DRAFT, policy=policy, fetched=_RESCUE_FETCHED)

    # The assist RESCUES: the ng-2 wall relaxed, "maintains" withheld the draft,
    # the scripted re-draft re-checks clean and is adopted.
    assert gate.output == RESCUE_CANDIDATE
    assert gate.provenance.fallback is True
    assert gate.provenance.assist_used is True
    assert len(gate_engine.payloads) == 1
    # Structural C12: the FAIL call never receives the withheld draft.
    assert len(fake.calls) == 1
    assert set(fake.calls[0]) == {"question", "tables", "reasons"}
    assert "draft" not in fake.calls[0]
    assert "maintains" not in repr(fake.calls)


def test_assist_pass_polish_twin_at_standard_ng2() -> None:
    fake = FakeAssist(POLISHED)
    policy = _policy(
        GroundedGateEngine(),
        RecordingAuditSink(),
        assist=fake,
        strictness=STANDARD,
        profile=PROFILE_NG2,
    )

    gate = _run_gate(DRAFT_PASS, policy=policy, fetched=_FETCHED)

    assert gate.output == POLISHED
    assert gate.provenance.fallback is False
    assert gate.provenance.assist_used is True


# --- sharpest symmetry pin: the re-check applies the SAME tier + profile -----


@pytest.mark.parametrize(
    ("tier", "adopted"),
    [pytest.param(STANDARD, True, id="standard"), pytest.param(STRICT, False, id="strict")],
)
def test_recheck_applies_same_tier_symmetrically(tier: StrictnessTier, adopted: bool) -> None:
    # RESCUE_DRAFT fails at BOTH tiers (vocab-ungrounded-factual). The candidate
    # ADVISORY_DRAFT has an advisory-only finding: NOT withheld at STANDARD →
    # adopted; WITHHELD at STRICT → rejected. Proves the re-check runs
    # apply_strictness with the SAME tier, not `recheck.passed`.
    fake = FakeAssist(ADVISORY_DRAFT)
    gate_engine = GroundedGateEngine()
    policy = _policy(
        gate_engine, RecordingAuditSink(), assist=fake, strictness=tier, profile=PROFILE_NG2
    )

    gate = _run_gate(RESCUE_DRAFT, policy=policy, fetched=_RESCUE_FETCHED)

    assert gate.provenance.assist_used is adopted
    if adopted:
        assert gate.output == ADVISORY_DRAFT
    else:
        assert gate.output == "VERIFIED-FALLBACK:accounts-directory"
    assert len(gate_engine.payloads) == 1


# --- C12 scan of the new counts-only grounding.assist event -----------------


@pytest.mark.parametrize(
    ("draft", "candidate", "phase", "fetched"),
    [
        pytest.param(DRAFT_PASS, POLISHED, "pass-polish", _FETCHED, id="pass-polish"),
        pytest.param(RESCUE_DRAFT, RESCUE_CANDIDATE, "fail-redraft", _RESCUE_FETCHED, id="redraft"),
    ],
)
def test_assist_event_emitted_once_counts_only(
    draft: str, candidate: str, phase: str, fetched: Sequence[Mapping[str, Any]]
) -> None:
    fake = FakeAssist(candidate)
    sink = RecordingAuditSink()
    policy = _policy(
        GroundedGateEngine(), sink, assist=fake, strictness=STANDARD, profile=PROFILE_NG2
    )

    gate = _run_gate(draft, policy=policy, fetched=fetched)

    events = sink.with_action("grounding.assist")
    assert len(events) == 1
    event = events[0]
    detail = json.loads(event.detail)
    assert set(detail) == {"phase", "adopted", "error_class"}
    assert detail["phase"] == phase
    assert detail["adopted"] is True
    assert detail["error_class"] is None
    assert event.outcome == "success"
    assert gate.provenance.assist_used is True
    # No text, no kwargs anywhere in the event.
    dump = repr(event)
    for banned in ("maintains", RESCUE_CANDIDATE, "VERIFIED-FALLBACK", PROMPT_PASS, DRAFT_PASS):
        assert banned not in dump


def test_assist_event_error_class_and_rejected_candidate() -> None:
    # (a) the assist RAISED → error_class is the exception type name, outcome error.
    fake = FakeAssist(RuntimeError("assist blew up"))
    sink = RecordingAuditSink()
    policy = _policy(
        GroundedGateEngine(), sink, assist=fake, strictness=STANDARD, profile=PROFILE_NG2
    )
    gate = _run_gate(RESCUE_DRAFT, policy=policy, fetched=_RESCUE_FETCHED)
    detail = json.loads(sink.with_action("grounding.assist")[0].detail)
    assert detail["adopted"] is False
    assert detail["error_class"] == "RuntimeError"
    assert sink.with_action("grounding.assist")[0].outcome == "error"
    assert gate.output == "VERIFIED-FALLBACK:accounts-directory"
    assert gate.provenance.assist_used is False

    # (b) a REJECTED (re-check-withheld) candidate is NOT an error: adopted False,
    # error_class None, outcome success.
    fake2 = FakeAssist(SMUGGLED2)
    sink2 = RecordingAuditSink()
    policy2 = _policy(
        GroundedGateEngine(), sink2, assist=fake2, strictness=STANDARD, profile=PROFILE_NG2
    )
    _run_gate(RESCUE_DRAFT, policy=policy2, fetched=_RESCUE_FETCHED)
    event2 = sink2.with_action("grounding.assist")[0]
    detail2 = json.loads(event2.detail)
    assert detail2["adopted"] is False
    assert detail2["error_class"] is None
    assert event2.outcome == "success"


def test_assist_event_append_failure_fails_closed() -> None:
    # C10: an un-auditable assist invocation kills the run (mirrors toolset._emit).
    fake = FakeAssist(POLISHED)
    policy = _policy(
        GroundedGateEngine(),
        FailingAuditSink(),
        assist=fake,
        strictness=STANDARD,
        profile=PROFILE_NG2,
    )
    with pytest.raises(RuntimeAuditError):
        _run_gate(DRAFT_PASS, policy=policy, fetched=_FETCHED)


def test_no_assist_no_grounding_assist_event() -> None:
    for draft, fetched in ((DRAFT_PASS, _FETCHED), (DRAFT_FAIL, _FETCHED)):
        sink = RecordingAuditSink()
        policy = _policy(GroundedGateEngine(), sink, strictness=STANDARD, profile=PROFILE_NG2)
        _run_gate(draft, policy=policy, fetched=fetched)
        assert sink.with_action("grounding.assist") == []


# --- Codex C1: advisory findings on a PASS-through are recorded, never dropped -


def test_advisory_event_emitted_counts_only() -> None:
    # ADVISORY_DRAFT passes (not withheld) at STANDARD/ng-2 but carries an
    # advisory vocab-ungrounded finding. Exactly ONE counts-only grounding.advisory
    # event records it — the ONLY audit channel on this PASS path (no block, no
    # assist). C12: class strings + counts only, no draft text.
    sink = RecordingAuditSink()
    policy = _policy(GroundedGateEngine(), sink, strictness=STANDARD, profile=PROFILE_NG2)

    gate = _run_gate(ADVISORY_DRAFT, policy=policy, fetched=_RESCUE_FETCHED)

    assert gate.output == ADVISORY_DRAFT  # emitted, never withheld
    events = sink.with_action("grounding.advisory")
    assert len(events) == 1
    event = events[0]
    assert event.outcome == "success"  # advisory is not an error
    detail = json.loads(event.detail)
    assert set(detail) == {"strictness", "advisory_reasons", "advisory_count"}
    assert detail["strictness"] == "standard"
    assert detail["advisory_reasons"] == ["vocab-ungrounded"]
    assert detail["advisory_count"] == 1
    # No draft/candidate text anywhere in the event (structural C12).
    dump = repr(event)
    for banned in ("Acme", "Wonderful", "orders", ADVISORY_DRAFT):
        assert banned not in dump

    # A CLEAN pass (zero findings) emits NO advisory event.
    clean_sink = RecordingAuditSink()
    clean_policy = _policy(
        GroundedGateEngine(), clean_sink, strictness=STANDARD, profile=PROFILE_NG2
    )
    _run_gate(DRAFT_PASS, policy=clean_policy, fetched=_FETCHED)
    assert clean_sink.with_action("grounding.advisory") == []

    # On the WITHHOLD path the advisory split rides the guardrail.block payload —
    # NOT a separate grounding.advisory event (no double-recording).
    withhold_gate_engine = GroundedGateEngine()
    withhold_sink = RecordingAuditSink()
    withhold_policy = _policy(
        withhold_gate_engine, withhold_sink, strictness=STANDARD, profile=PROFILE_NG2
    )
    _run_gate(SPLIT_DRAFT, policy=withhold_policy, fetched=_RESCUE_FETCHED)
    assert withhold_sink.with_action("grounding.advisory") == []
    assert withhold_gate_engine.payloads[0]["advisory_reasons"] == ["vocab-ungrounded"]


def test_advisory_event_append_failure_fails_closed() -> None:
    # C10: an un-auditable advisory record kills the run (mirrors the assist event
    # and the withhold-branch AuditEmissionError posture).
    policy = _policy(
        GroundedGateEngine(), FailingAuditSink(), strictness=STANDARD, profile=PROFILE_NG2
    )
    with pytest.raises(RuntimeAuditError):
        _run_gate(ADVISORY_DRAFT, policy=policy, fetched=_RESCUE_FETCHED)


# --- e2e: the tier threads through DepartmentAgentSession into provenance ----


def test_session_standard_tier_threads_provenance(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [
            tool_call_completion("read.docs.search", {"q": "accounts"}),
            text_completion(ADVISORY_DRAFT),
        ]
    )
    gate_engine = GroundedGateEngine()
    policy = _policy(gate_engine, sink, strictness=STANDARD, profile=PROFILE_NG2)
    session = _grounded_session(engine, gw, sink, log, policy=policy)

    result = asyncio.run(session.run(PROMPT_PASS, principal=make_principal(department="eng")))

    # agent.py passes gate.provenance through untouched — no agent.py change.
    assert result.output == ADVISORY_DRAFT
    assert result.grounded is not None
    assert result.grounded.strictness == "standard"
    assert result.grounded.gate == PROFILE_NG2.version
    assert result.grounded.fallback is False
