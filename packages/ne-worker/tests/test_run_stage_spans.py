# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Where a Run's wall-clock goes (POST_DEMO item 12).

Measured 2026-08-30 over seven live runs: a reader waits a median 10.2 s before
anything can appear, and 7.6 s of it — worst 34.4 s — falls in one stretch
between admission and the model being chosen that emits no event at all. The
model call itself is 1.5 s, the fastest part of the run.

The boundary was already being handed a name at every one of those edges
(`profile:binding`, `profile:agentic-before`, …) and discarding it. These tests
pin that the names now reach a durable row, so the gap is decomposable from data
instead of being a number I can only assert.
"""

from __future__ import annotations

import ast
import asyncio
import re
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.runtime.run_workflow import (
    RunWorkflowClaim,
    RunWorkflowStateStore,
    ne_agent_run_stage_spans,
)

from .test_agent_run_workflow import _Harness, _harness, _queued_run, _workflow_request


def _spans(harness: _Harness) -> list[tuple[str, datetime]]:
    with harness.runtime.connect() as conn:
        return [
            (str(row.stage), row.observed_at)
            for row in conn.execute(
                sa.select(ne_agent_run_stage_spans).order_by(
                    ne_agent_run_stage_spans.c.observed_at,
                    ne_agent_run_stage_spans.c.span_id,
                )
            ).all()
        ]


def _claim(harness: _Harness, run: object) -> tuple[RunWorkflowStateStore, RunWorkflowClaim]:
    state = RunWorkflowStateStore(harness.runtime)
    request = _workflow_request(harness, run, dispatch_id="dispatch-stage-spans")
    claim = state.claim(
        dispatch_id=request.dispatch_id,
        run_id=request.run_id,
        owner_principal_id=request.owner_principal_id,
        config_hash=request.config_hash,
    )
    return state, claim


def test_a_named_stage_reaches_a_durable_row(tmp_path: Path) -> None:
    """The whole point: the name the boundary was already given is now recorded."""
    harness = _harness(tmp_path)

    async def scenario() -> None:
        run = await _queued_run(harness)
        state, claim = _claim(harness, run)
        state.renew(claim, stage="profile:binding")
        assert [stage for stage, _ in _spans(harness)] == ["profile:binding"]

    asyncio.run(scenario())


def test_an_UNNAMED_renewal_writes_NOTHING(tmp_path: Path) -> None:
    """The control, and it is what stops this from re-labelling every lease renewal.

    `renew()` is called on paths that are not stage boundaries at all. Without
    this, adding the column could have silently written a span for each of them
    and the first test would still pass.
    """
    harness = _harness(tmp_path)

    async def scenario() -> None:
        run = await _queued_run(harness)
        state, claim = _claim(harness, run)
        state.renew(claim)
        state.probe(claim)
        assert _spans(harness) == []

    asyncio.run(scenario())


def test_the_GAP_between_two_stages_is_derivable(tmp_path: Path) -> None:
    """Item 12 needs a DURATION, not a list.

    An adversarial pass correctly refused an earlier version of this test: it
    asserted an exact ordered sequence after sorting by `(observed_at, span_id)`,
    and `span_id` is a random uuid4. Two spans landing in the same clock tick
    would have made that assertion pass or fail on a coin flip — a flaky test
    dressed as an invariant.

    So this asserts what the feature actually needs and the schema actually
    guarantees: both stages are present, and the elapsed time between them is a
    real, non-negative number. HONEST LIMIT: `observed_at` is
    millisecond-resolution on SQLite and transaction-start `current_timestamp` on
    PostgreSQL, so two stages in one tick are not separable. That is irrelevant to
    a 7.6-SECOND gap and would matter only to a consumer measuring sub-tick
    ordering — which is why no ordering guarantee is claimed here.
    """
    harness = _harness(tmp_path)

    async def scenario() -> None:
        run = await _queued_run(harness)
        state, claim = _claim(harness, run)
        state.renew(claim, stage="profile:binding")
        state.renew(claim, stage="profile:agentic-before")
        by_stage = {stage: observed_at for stage, observed_at in _spans(harness)}
        assert set(by_stage) == {"profile:binding", "profile:agentic-before"}
        elapsed = (by_stage["profile:agentic-before"] - by_stage["profile:binding"]).total_seconds()
        assert elapsed >= 0.0, f"a stage gap must be a real duration; got {elapsed}"

    asyncio.run(scenario())


def test_a_TERMINAL_run_records_no_span(tmp_path: Path) -> None:
    """A stage the platform then REFUSES must not appear as time spent.

    The terminal-status check lives OUTSIDE the lease transaction, so without a
    guard a heartbeat against an already-finished run would commit its span and
    THEN raise — leaving the table asserting a boundary crossing the platform
    rejected. Found by an adversarial pass, not by me.

    The run is terminalized through the store's own `terminalize`, not by writing
    the row: a trigger refuses a terminal Run without its atomic lifecycle outbox,
    which is itself a guard worth not fighting.
    """
    from ne.runtime.run_store import RunState

    harness = _harness(tmp_path)

    async def scenario() -> None:
        run = await _queued_run(harness)
        state, claim = _claim(harness, run)
        state.renew(claim, stage="profile:binding")
        assert [stage for stage, _ in _spans(harness)] == ["profile:binding"]

        state.terminalize(claim, status=RunState.FAILED, outcome="failed")
        with pytest.raises(RuntimeError):
            state.renew(claim, stage="profile:agentic-before")
        assert [stage for stage, _ in _spans(harness)] == ["profile:binding"], (
            "a boundary the platform refused must leave no span behind"
        )

    asyncio.run(scenario())


def test_the_SAME_stage_twice_is_recorded_TWICE(tmp_path: Path) -> None:
    """`profile:agentic-before` is legitimately reached from two call sites, so a
    stage is NOT unique per attempt. This is why these are not execution
    checkpoints, which are UNIQUE on (run_id, attempt_id, stage) and would have
    raised on ordinary control flow."""
    harness = _harness(tmp_path)

    async def scenario() -> None:
        run = await _queued_run(harness)
        state, claim = _claim(harness, run)
        state.renew(claim, stage="profile:agentic-before")
        state.renew(claim, stage="profile:agentic-before")
        assert len(_spans(harness)) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "x" * 65,
        "stage with spaces",
        "stage\nwith-newline",
        'stage"quote',
        "stage/slash",
    ],
)
def test_a_malformed_stage_name_is_REFUSED_at_the_writer(tmp_path: Path, bad: str) -> None:
    """Server-authored is not the same as unvalidated.

    Every caller is a literal in this repo, but `heartbeat(stage: str)` is a plain
    string parameter, and "only our own code calls it" is exactly the assumption
    that put an unvalidated driver string into the audit ledger in `3944763c`.
    The shapes rejected here are the shapes of error messages and injected text.
    """
    harness = _harness(tmp_path)

    async def scenario() -> None:
        run = await _queued_run(harness)
        state, claim = _claim(harness, run)
        with pytest.raises(ValueError):
            state.renew(claim, stage=bad)
        assert _spans(harness) == [], "a refused stage must leave no row behind"

    asyncio.run(scenario())


def test_a_REAL_run_records_the_stages_it_actually_passed(tmp_path: Path) -> None:
    """End to end, through the real workflow service — not the store directly.

    This is the test that proves the feature rather than the plumbing: an ordinary
    run must leave behind the named edges it crossed, because those names are what
    turn item 12's 7.6 s into a decomposable span set. Asserted on PERSISTED rows.
    """
    from ne.runtime.run_workflow import RunWorkflowService

    from .test_agent_run_workflow import _Executor, _Gateway, _RecordingAdapter

    harness = _harness(tmp_path)
    service = RunWorkflowService(
        state=RunWorkflowStateStore(harness.runtime),
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=_Gateway(),
        executor=_Executor(),
        adapter=_RecordingAdapter(),
        config_hash=harness.config_hash,
    )

    async def scenario() -> None:
        run = await _queued_run(harness)
        result = await service.execute(
            _workflow_request(harness, run, dispatch_id="dispatch-real-spans")
        )
        assert result.outcome == "execution_complete"
        spans = _spans(harness)
        stages = [stage for stage, _ in spans]
        assert stages, "a completed run recorded no stage at all"
        # The edge that matters for item 12 is the one immediately BEFORE the model
        # is called: the measured 7.6 s ends there. Named `run-model:before:<n>` on
        # this path (the worker's own profile path names its edges `profile:*`; both
        # flow through the same boundary, which is the point).
        assert any(stage.startswith("run-model:before:") for stage in stages), (
            f"the edge before the model call must be recorded; got {stages}"
        )
        # And the span set must be a usable timeline, not a bag: ordered by one
        # clock, so a duration between any two edges is derivable.
        stamps = [observed_at for _, observed_at in spans]
        assert stamps == sorted(stamps), f"spans are not orderable in time: {stages}"

    asyncio.run(scenario())


def test_the_span_table_is_created_by_the_WORKFLOW_schema_bootstrap(tmp_path: Path) -> None:
    """The bootstrap must name this table, not rely on import order.

    This module's tables register on the shared metadata only when the module is
    IMPORTED. `ensure_run_schema` imports `run_checkpoint` for precisely that
    reason and does NOT import `run_workflow`, so a table left out of
    `ensure_run_workflow_schema`'s tuple is created or not depending on what else
    happened to be imported first — and the failure surfaces far away, at the
    first write.

    Every other test in this file passed while the table was missing from that
    tuple, because their harness constructs a `RunWorkflowStateStore` and so
    imports the module. This one asserts the bootstrap itself.
    """
    from ne.runtime.run_workflow import ensure_run_workflow_schema

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    try:
        ensure_run_workflow_schema(engine)
        assert sa.inspect(engine).has_table(ne_agent_run_stage_spans.name), (
            "the workflow schema bootstrap must create the stage-span table by name"
        )
    finally:
        engine.dispose()


def test_the_emitted_stage_names_are_writable_and_sit_where_they_claim(tmp_path: Path) -> None:
    """POST_DEMO item 12, increment 1b — the STATIC half, and only the static half.

    The spans shipped in #939 put the ENTIRE opening wait in one edge:
    `profile:agentic-before` → `run-model:before:0`, 7.67 s average over three live
    runs, while everything before the agent loop totals 0.11 s.

    ⚠️ THE NAME AND DOCSTRING THIS TEST USED TO CARRY WERE WRONG and are corrected
    here rather than left for a reader to trip over. It was called "the two
    suspects INSIDE the opening gap" and said skill activation and history
    materialization "sit inside that edge". They do not: the production
    `profile:agentic-before` is emitted BELOW both, so both complete before the
    measured edge opens. And the correction ALSO said `profile:adapter-before` was
    the marker that divides the edge; it is not, for the same reason one layer
    down — nothing between the production `profile:agentic-before` and it does any
    I/O, so it partitions 7.67 s into ~0 and ~all of it. The marker that divides
    unmeasured work is `profile:agent-built`, in the ADAPTER, after the governed
    tools and the Agent are built (pinned by
    ``test_the_agent_built_boundary_fires_before_the_first_model_request`` in
    ne-runtime). A stale description here is how performance triage gets pointed
    at the wrong operations, which is why this paragraph has now been corrected
    twice rather than trimmed.

    ⚠️ AND THIS TEST IS NOT THE REMEDY FOR THIS FEATURE'S DEFECT CLASS. Its earlier
    justification — "a test that drove them would be testing the harness, not the
    worker" — was false and is retracted: `test_profile_bound_run_execution.py`
    drives the real `_execute_as_principal` through the production composition, and
    both arms now assert what actually FIRED, under a recording boundary. What
    remains here is what only source can check: that each name is one the writer
    will accept, and that each marker sits on the correct side of the call it
    brackets. Both of those are worth pinning; neither would catch a heartbeat
    guarded behind an unrelated runtime condition, which is why the runtime pair
    exists.
    """
    from pathlib import Path as _Path

    source = (
        _Path(__file__).resolve().parents[1] / "src" / "ne" / "worker" / "run_execution.py"
    ).read_text()

    for stage in (
        "profile:skills-before",
        "profile:skills-after",
        "profile:history-before",
        "profile:history-after",
        "profile:adapter-before",
    ):
        assert f'heartbeat("{stage}")' in source, f"{stage} is not emitted by the worker"

    # ⚠️ THE ASSERTION MY FIRST VERSION LACKED, and Codex named the gap: straddling
    # a call proves nothing about whether the marker falls inside the EDGE it is
    # meant to split. There are TWO `profile:agentic-before` sites, and the
    # PRODUCTION one is below skills and history — so those four markers fire
    # before the 7.67 s edge begins and can never partition it.
    #
    # `profile:adapter-before` is the one that at least sits INSIDE the edge: it
    # must fall AFTER the production `profile:agentic-before` and BEFORE the
    # adapter is entered, which is where `run-model:before:0` is emitted from.
    # ⚠️ Sitting inside it is not splitting it -- this comment used to say
    # `adapter-before` "is the one that does", which the docstring below retracts:
    # the region it fences carries no I/O, so it bounds the worker's setup rather
    # than partitioning the 7.67 s.
    #
    # IDENTIFIED BY WINDOW, NOT BY POSITION. An earlier version took `rindex` —
    # "the last one" — which is the production site today only because the
    # test-only composition lane happens to sit above it. An adversarial pass was
    # right that this FAILS OPEN: reorder that lane below the production site but
    # still above `run_adapter.run(`, and `rindex` silently anchors on the
    # test-only marker while `p < a < r` still holds, so the assertion quietly
    # stops testing anything. Requiring EXACTLY ONE site in the window between
    # skill activation and the adapter call fails closed instead — an ambiguous
    # tree reds this test rather than making it vacuous.
    activate = source.index("self._agent_skills.activate(")
    adapter_run = source.index("await run_adapter.run(")
    agentic_before_sites = [
        match.start()
        for match in re.finditer(re.escape('heartbeat("profile:agentic-before")'), source)
    ]
    in_window = [at for at in agentic_before_sites if activate < at < adapter_run]
    assert len(in_window) == 1, (
        "exactly one profile:agentic-before must lie between skill activation and the "
        f"adapter call, or this test cannot say which site it measured; found {in_window}"
    )
    production_agentic_before = in_window[0]
    adapter_before = source.index('heartbeat("profile:adapter-before")')
    assert production_agentic_before < adapter_before < adapter_run, (
        "profile:adapter-before must fall INSIDE the profile:agentic-before → "
        "run-model:before:0 edge; outside it, it does not even bound the worker "
        "setup it is there to fence"
    )

    # BOTH history markers must be inside the continuity branch. Checking only
    # `history-before` leaves a hole Codex named: hoist `history-after` just below
    # the `else` and a no-continuity run emits a history marker although
    # materialization never ran — while this test stays green, because the
    # ordering assertion below merely places it somewhere after the call.
    # Branch ownership is asserted for each marker separately.
    #
    # ⚠️ AND THIS ANCHOR FAILED OPEN THE SAME WAY THE `rindex` ONE ABOVE DID —
    # caught in the round that fixed that one, in the very next assertion, which
    # is exactly how a discipline gets applied once and then forgotten. There are
    # THREE `if continuity_active:` sites; `source.index` takes the FIRST, and it
    # is the history branch only by position. Put another one above it and the
    # anchor moves while `else_arm` still resolves to the history `else:` — so
    # `branch < at < else_arm` keeps holding for markers hoisted OUT of the
    # branch, as long as they sit above that `else`.
    #
    # ⚠️ WHAT THE FIX BELOW ACTUALLY DOES, stated exactly, because the first
    # version of this comment said it was "required to be unique" and the code
    # says `assert owning` — non-emptiness — then takes `owning[-1]`. A reviewer
    # was right that the sentence claimed a guarantee the code does not make. It
    # anchors on the INNERMOST `if continuity_active:` enclosing the
    # materialization call, and the containment assertion is what carries the
    # weight: a decoy branch ABOVE cannot move the anchor, and a marker hoisted
    # OUT of the branch lands before it and reds. A nested decoy INSIDE would move
    # the anchor inward and red a marker that legitimately sits in the outer
    # branch — a false RED, which is the safe direction. So the property is
    # fail-closed; it just is not uniqueness, and the comment no longer says it
    # is. Adding a uniqueness assertion here would be ceremony over a check that
    # already fails closed.
    materialize_at = source.index("materialize_conversation_history(")
    branch_sites = [
        match.start() for match in re.finditer(re.escape("if continuity_active:"), source)
    ]
    owning = [at for at in branch_sites if at < materialize_at]
    assert owning, "no `if continuity_active:` precedes the materialization call"
    branch = owning[-1]
    else_arm = source.index("else:", branch)
    assert branch < materialize_at < else_arm, (
        "the anchored branch must be the one that CONTAINS the materialization "
        f"call, or this test cannot say which branch it measured; branch={branch} "
        f"materialize={materialize_at} else={else_arm}"
    )
    for marker in ("profile:history-before", "profile:history-after"):
        at = source.index(f'heartbeat("{marker}")')
        assert branch < at < else_arm, (
            f"{marker} must sit inside the continuity branch — above the else — or a "
            "no-continuity run emits a marker for work that never happened"
        )

    # Ordering is the whole point: a pair whose members straddle the wrong call
    # measures the wrong thing.
    before_skills = source.index('heartbeat("profile:skills-before")')
    after_skills = source.index('heartbeat("profile:skills-after")')
    assert before_skills < activate < after_skills, "the skills pair must straddle activate()"

    before_history = source.index('heartbeat("profile:history-before")')
    materialize = source.index("materialize_conversation_history(")
    # ⛔ `profile:skills-after` MUST SIT BELOW `record_skill_activation`, and this
    # is a provenance assertion, not a layout one. `heartbeat` renews the lease
    # and RAISES `RunCancellationRequested` on a CANCELLING run. Between
    # `activate()` returning and the receipt committing there was no boundary
    # before this increment, so a cancellation could not split that pair. Put the
    # marker between them and a run that HAS activated its Skills is cancelled
    # before the receipt lands -- and `AgentRunStore.record_skill_activation`
    # defines an ABSENT receipt as "the attempt died before activation", so the
    # durable record then states something untrue about the run.
    #
    # Codex found that placement in review. Nothing failed when the marker was
    # moved back and forth, which is why this assertion exists: the ordering test
    # above only pins that the pair straddles `activate(`, and that stays true on
    # both sides of the receipt write.
    record_activation = source.index("record_skill_activation(")
    assert record_activation < source.index('heartbeat("profile:skills-after")'), (
        "profile:skills-after must fall AFTER record_skill_activation, or a "
        "cancellation between them publishes an absent activation receipt for a "
        "run that did activate"
    )

    after_history = source.index('heartbeat("profile:history-after")')
    assert before_history < materialize < after_history, (
        "the history pair must straddle materialize_conversation_history()"
    )

    # And every one of them must be a name the writer will ACCEPT — a stage the
    # shape guard rejects would raise at the boundary instead of measuring.
    from ne.runtime.run_workflow import _validated_stage

    # ROUND 13, from an adversarial P2 that was right. This was a hand-typed
    # ENUMERATION of five names, and `profile:policy-recheck-before` -- added by
    # this same increment, emitted from this same module -- was not in it. So the
    # one new marker whose name nothing else checks was the one name no test ran
    # through the guard.
    #
    # That gap is worse than a missing assertion, because of WHERE the guard sits.
    # `_validated_stage` is called inside the lease-renewal transaction at the
    # span INSERT, and it raises `ValueError`, while `_LeaseExecutionBoundary.
    # heartbeat` translates only `RuntimeError`. A name the guard refuses does not
    # surface as a rejected marker: it aborts the renewal and reaches the caller as
    # an untranslated `ValueError` rather than `RunLeaseLost`, so nothing
    # downstream recognises it as a lease loss.
    #
    # The list is therefore DERIVED from the source instead of typed here. Any
    # marker a later change adds to this module is covered the moment it is
    # written, which is the only version of this test that stays true.
    #
    # ⚠️ FOUR ROUNDS OF THIS GUARD CHECKED LESS THAN ITS COMMENT CLAIMED, so the
    # history is worth the lines. R21 matched only f-strings whose text STARTS
    # with `profile:` -- run against `run_runner.py`, the file its own comment
    # cited as the precedent, it found ZERO of that file's three interpolated
    # heartbeats. R22 required a double-quoted literal but anchored on
    # `.heartbeat(`, so `hb = boundary.heartbeat` then `await hb(name)` evaded
    # it. R23 anchored on the identifier over comment-stripped text, which closed
    # that hole and opened three more: it read a DIFFERENT text than the
    # derivation (so a call quoted in a COMMENT inflated the name count and could
    # mask a real emission's deletion), it fails closed on a call the formatter
    # wraps across lines, and it forbade the module from ever defining or naming
    # `heartbeat` at all.
    #
    # ⭐ ROUND 24 STOPS PATTERN-MATCHING TEXT AND PARSES THE MODULE. `ast` sees
    # exactly what Python sees: comments and docstrings are not code, wrapping is
    # irrelevant, and an attribute BOUND rather than called is a different node.
    # Three properties, each the failure of one earlier round:
    #   (a) every `.heartbeat(...)` call takes ONE positional argument that is a
    #       string CONSTANT -- no f-string, concatenation, name or conditional;
    #   (b) the `heartbeat` attribute is never accessed except as a call target,
    #       and the bare name is never used, so nothing can route around (a);
    #   (c) EVERY literal so found is run through `_validated_stage`, not just the
    #       `profile:`-prefixed ones. An adversarial pass was right that this was
    #       the live hole: `heartbeat("skills activated")` is a plain literal, so
    #       every earlier guard PASSED it while the derivation never captured it
    #       and so never validated it. At runtime the space makes
    #       `_validated_stage` raise `ValueError` INSIDE the lease-renewal
    #       transaction, where `_LeaseExecutionBoundary.heartbeat` translates only
    #       `RuntimeError` -- the renewal aborts and nothing downstream reads it
    #       as a lost lease. That is verbatim the failure this test exists to
    #       find, and "only an interpolated name can reach it" was false.
    #
    # ⚠️ ONE MUTATION I EXPECTED TO RED DID NOT, AND THE GUARD WAS RIGHT:
    # `heartbeat("profile:" "binding")`. Python folds adjacent string literals at
    # PARSE time, so `ast` sees one `Constant` whose value is exactly
    # `"profile:binding"` -- indistinguishable from the single-literal form and
    # identical in value, hence correctly allowed and correctly validated.
    # `"profile:" + name` is a `BinOp` and reds, as do a conditional expression
    # and passing the bound attribute to another function. Recorded because a
    # future reader will see the implicit-concatenation form pass and wonder.
    tree = ast.parse(source)
    call_targets: set[int] = set()
    stage_literals: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "heartbeat"):
            continue
        call_targets.add(id(func))
        assert len(node.args) == 1 and not node.keywords, (
            "a heartbeat call here does not take exactly one positional stage name "
            f"(line {node.lineno})"
        )
        arg = node.args[0]
        assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
            f"the stage name at line {node.lineno} is not a string CONSTANT -- an "
            "f-string, concatenation, name or conditional is invisible to the "
            "derived list below, and a name the writer refuses aborts the lease "
            "renewal as an untranslated ValueError"
        )
        stage_literals.append(arg.value)
    bound = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "heartbeat"
        and id(node) not in call_targets
    ]
    assert not bound, (
        "the `heartbeat` attribute is bound or passed rather than called directly "
        f"(line {bound[0].lineno}); every route around the call form defeats the "
        "derived list"
    )
    assert not [
        node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "heartbeat"
    ], "a bare `heartbeat` name is in play in this module; only `.heartbeat(...)` is allowed"
    # ⚠️ AND NO STRING "heartbeat" EITHER. An adversarial pass on #943 got past the
    # round-24 guard with `getattr(context.boundary, "heartbeat")("skills activated")`:
    # the `Call.func` is a `Call`, not an `Attribute`, so the call check never sees
    # it; `"heartbeat"` is a `Constant`, not a `Name`, so the bare-name check passes;
    # and the stage literal never enters `stage_literals`, so `_validated_stage` never
    # runs on it. The space then raises `ValueError` inside the lease-renewal
    # transaction -- verbatim the failure this test exists to find, suite green.
    # Refusing the METHOD NAME AS DATA closes `getattr`, `operator.attrgetter`,
    # `functools.partial` and every other indirection at once, without the test having
    # to enumerate them.
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "heartbeat"
    ], (
        'the string "heartbeat" appears in this module; the method name used as DATA '
        "reaches the boundary through `getattr` and friends, which no AST call check "
        "can see"
    )
    assert stage_literals, "no heartbeat calls found -- the derivation is reading the wrong source"
    # (c): EVERY name, not only the profile-prefixed ones.
    for stage in stage_literals:
        assert _validated_stage(stage) == stage, stage
    emitted = sorted({name for name in stage_literals if name.startswith("profile:")})
    # ⚠️ The `_validated_stage` loop is ABOVE the membership assertions ON PURPOSE,
    # and round 23 had it the other way round in this file while `runner.py`'s twin
    # stated the discipline correctly. With membership first, renaming
    # `profile:policy-recheck-before` to `profile:policy-recheck BEFORE` reds on
    # "policy-recheck-before is missing" instead of on the writer's own refusal --
    # so the mutation proves the wrong thing. This ordering makes it honest.
    assert "profile:policy-recheck-before" in emitted, emitted
    assert len(emitted) >= 13, emitted


def test_only_the_known_modules_emit_stages_to_the_span_writer() -> None:
    """⚠️ SCOPE. The two derived-name tests read ONE module each, and an adversarial
    pass on #943 was right that FOUR modules reach `_validated_stage`: this one,
    `runner.py`, `run_runner.py` (three deliberately INTERPOLATED names) and
    `run_workflow.py`. A future emitter placed in a helper module -- or a one-line
    delegate `async def _mark(self, s): await self._context.boundary.heartbeat(s)`
    moved out of `run_execution.py` -- is invisible to both of them, and its name
    would never be run through the writer's own validator.

    A whole-tree per-module check is not possible here (`run_runner.py`'s names are
    interpolated on purpose, from a server-minted `step_id` and an int ordinal). What
    IS checkable, and what actually closes the hole, is that the SET of emitting
    modules does not grow silently: a new one reds this test and forces whoever adds
    it to decide how its names are guarded.
    """
    import ast as _ast
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[3]
    emitters = set()
    for path in sorted(root.glob("packages/*/src/ne/**/*.py")):
        tree = _ast.parse(path.read_text())
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.Call)
                and isinstance(node.func, _ast.Attribute)
                and node.func.attr == "heartbeat"
            ):
                emitters.add(str(path.relative_to(root)))
                break
    assert emitters == {
        "packages/ne-runtime/src/ne/runtime/run_runner.py",
        "packages/ne-runtime/src/ne/runtime/run_workflow.py",
        "packages/ne-runtime/src/ne/runtime/runner.py",
        "packages/ne-worker/src/ne/worker/run_execution.py",
    }, (
        "the set of modules emitting stages to the span writer has changed; each one "
        "needs its names guarded, and only two of them have a derived-name test -- "
        f"got {sorted(emitters)}"
    )
