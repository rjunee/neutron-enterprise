# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# THE SOURCE-FREE OUTPUT PREVIEW.
#
# A Run whose terminal envelope declares no evidence-bound claims is
# *source-free*: 010F stores its exact served output in the owner/turn-targeted
# `ProtectedTurnContent` domain and SPEC §9 requires that "an explicitly
# source-free fixture decrypts only through its exact owner/turn target and
# REMAINS READABLE".  Its aggregate verdict is nonetheless `unavailable`,
# because SPEC §10F reserves Verified/Grounded for complete claim mapping.
#
# Those two facts are independent, and conflating them is what made Ask serve
# nothing: the projection gated the preview on the *verdict* instead of on
# whether the stored output is source-free.  Every deterministic Run and every
# agentic Run that cited nothing then completed green with its answer sealed
# away.  These tests pin both halves: source-free output is served to its owner,
# and source-bound output stays withheld.

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa

from ne.api.run_projection import AgentRunProjection
from ne.contracts.identity import AuthenticatedRequestContext
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_finalizer import (
    RunFinalizer,
    RunFinalizerLease,
    RunTerminalCandidate,
    RunTurnTarget,
    StructuredRunOutputEnvelope,
    claim_set_hash,
)
from ne.runtime.run_store import (
    AgentRunStore,
    RunAuditDecisionKind,
    ensure_run_schema,
    ne_agent_run_finalizations,
    ne_agent_run_turn_targets,
)
from ne.runtime.run_workflow import (
    RunWorkflowClaim,
    RunWorkflowStateStore,
    ensure_run_workflow_schema,
)
from ne.runtime.source_results import (
    complete_run_delivery_observation,
    model_call_scope,
    run_delivery_observation_scope,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

OWNER = "alice"
DEPARTMENT = "claims"
AGENT = "agt_01"


@dataclass(frozen=True)
class _ReadyRun:
    engine: sa.Engine
    runs: AgentRunStore
    state: RunWorkflowStateStore
    target: RunTurnTarget
    claim: RunWorkflowClaim
    config_hash: str


async def _ready_run(tmp_path: Path, tag: str) -> _ReadyRun:
    engine = sa.create_engine(f"sqlite:///{tmp_path}/{tag}.db")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    ensure_run_workflow_schema(engine)
    sessions = SqlAlchemySessionStore(engine)
    runs = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
        ),
    )
    config_hash = "1" * 64
    conversation = await sessions.create_conversation("assistant", DEPARTMENT, OWNER)
    accepted = await runs.admit_start(
        SimpleNamespace(
            idempotency_key=f"start-{tag}",
            request_hash=hashlib.sha256(tag.encode()).hexdigest(),
            conversation_id=conversation.conversation_id,
            expected_version=0,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            conversation_agent_name="assistant",
            agent_id=AGENT,
            agent_version=7,
            strategy="agentic",
            config_hash=config_hash,
            capability_snapshot_hash="2" * 64,
            capability_snapshot_receipt_id="cap-receipt-1",
            input_plaintext=b"private input sentinel",
            authenticated_context=AuthenticatedRequestContext(
                principal_id=OWNER,
                authorization_groups=(),
                department=DEPARTMENT,
                issuer="https://issuer.example.test",
                expires_at=4_102_444_800,
            ),
        )
    )
    with engine.connect() as conn:
        authored = conn.execute(
            sa.select(ne_agent_run_turn_targets).where(
                ne_agent_run_turn_targets.c.run_id == accepted.run_id
            )
        ).one()
    target = RunTurnTarget(
        conversation_id=str(authored.conversation_id),
        run_id=str(authored.run_id),
        turn_id=str(authored.turn_id),
        turn_sequence=int(authored.turn_sequence),
        attachment_id=str(authored.attachment_id),
        attempt_id=str(authored.attempt_id),
    )
    state = RunWorkflowStateStore(engine)
    claim = state.claim(
        dispatch_id=f"dispatch-{tag}",
        run_id=accepted.run_id,
        owner_principal_id=OWNER,
        config_hash=config_hash,
    )
    await runs.record_audit_receipt(
        run_id=target.run_id,
        attempt_id=target.attempt_id,
        run_event_id=claim.claim_event_id,
        decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
        audit_event_id=f"audit-1-{uuid.uuid4()}",
        audit_sequence=1,
        audit_record_hash=hashlib.sha256(b"audit-1").hexdigest(),
        config_hash=config_hash,
        owner_principal_id=OWNER,
        step_id="run-execution",
    )
    return _ReadyRun(engine, runs, state, target, claim, config_hash)


def _empty_delivery(target: RunTurnTarget):  # type: ignore[no-untyped-def]
    with (
        run_delivery_observation_scope(
            run_id=target.run_id, attempt_id=target.attempt_id
        ) as observer,
        model_call_scope(run_id=target.run_id, model_call_attempt_id=target.attempt_id),
    ):
        complete_run_delivery_observation()
        return observer.snapshot()


def _source_free_envelope(output: bytes) -> StructuredRunOutputEnvelope:
    """The exact envelope ne-worker mints for a Run that cited nothing."""

    output_hash = hashlib.sha256(output).hexdigest()
    return StructuredRunOutputEnvelope(
        discriminator="structured_run_output",
        served_output_hash=output_hash,
        claim_count=0,
        claim_set_hash=claim_set_hash(output_hash, ()),
        claims=(),
        disposition="completed",
    )


def _lease(ready: _ReadyRun) -> RunFinalizerLease:
    return RunFinalizerLease(
        dispatch_id=ready.claim.dispatch_id,
        run_id=ready.claim.run_id,
        attempt_id=ready.claim.attempt_id,
        lease_token=ready.claim.lease_token,
        lease_epoch=ready.claim.lease_epoch,
    )


async def _finalize_source_free(ready: _ReadyRun, output: bytes) -> None:
    finalizer = RunFinalizer(ready.runs)
    observations = await finalizer.observe(
        target=ready.target, delivery=_empty_delivery(ready.target)
    )
    receipt = await finalizer.finalize(
        RunTerminalCandidate(
            observations=observations,
            envelope=_source_free_envelope(output),
            output_bytes=output,
        ),
        lease=_lease(ready),
    )
    # The preconditions this test is about, asserted rather than assumed.
    assert receipt.output_verdict == "unavailable"
    assert receipt.mapping_state == "unavailable"
    assert receipt.protected_output_kind == "protected_turn_content"


def test_source_free_terminal_output_is_served_to_its_owner(tmp_path: Path) -> None:
    """The Ask/deterministic defect: a completed Run must serve its stored answer."""

    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "source-free-preview")
        output = b"The Meridian portfolio holds 4 subjects."
        await _finalize_source_free(ready, output)

        projection = AgentRunProjection(ready.runs, ready.engine)
        run = await projection.get_run(
            run_id=ready.target.run_id,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
        )
        assert run is not None
        assert run.output_preview == output.decode()

    asyncio.run(scenario())


def test_source_free_output_is_withheld_from_a_foreign_principal(tmp_path: Path) -> None:
    """Serving the answer must not widen who may read it."""

    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "source-free-foreign")
        output = b"OWNER-ONLY-ANSWER-SENTINEL"
        await _finalize_source_free(ready, output)

        projection = AgentRunProjection(ready.runs, ready.engine)
        assert (
            await projection.get_run(
                run_id=ready.target.run_id,
                owner_principal_id="user:foreign",
                department=DEPARTMENT,
                agent_id=AGENT,
            )
            is None
        )

    asyncio.run(scenario())


def _restate_terminal(ready: _ReadyRun, **values: str) -> None:
    """Restate one declared terminal fact, past the receipt's append-only trigger.

    The reader must fail closed on the terminal facts AS DECLARED, and must not
    borrow confidence from the fact that its own writer would never have
    declared them.  Dropping the trigger for the length of the restatement is
    how this test authors a row the writer refuses to.
    """

    with ready.engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_run_finalizations_append_only")
        conn.execute(
            ne_agent_run_finalizations.update()
            .where(ne_agent_run_finalizations.c.run_id == ready.target.run_id)
            .values(**values)
        )


def _previewer(ready: _ReadyRun):  # type: ignore[no-untyped-def]
    projection = AgentRunProjection(ready.runs, ready.engine)

    async def preview() -> str | None:
        run = await projection.get_run(
            run_id=ready.target.run_id,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
        )
        assert run is not None
        return run.output_preview

    return preview


def test_preview_is_withheld_the_moment_the_terminal_declares_source_bound_output(
    tmp_path: Path,
) -> None:
    """The gate is the STORED DOMAIN, not the verdict -- so it must still bite.

    Same Run, same bytes, one declared column restated: 010F now says the
    terminal output is source-derived, so the owner projection must stop serving
    it and leave it to the evidence projection's separate current-source
    reauthorization.
    """

    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "source-bound-declared")
        output = b"SOURCE-BOUND-OUTPUT-SENTINEL"
        await _finalize_source_free(ready, output)
        preview = _previewer(ready)

        assert await preview() == output.decode()
        _restate_terminal(ready, protected_output_kind="protected_source_output")
        assert await preview() is None

    asyncio.run(scenario())


def test_a_partial_claim_mapping_is_neither_attested_nor_source_free(tmp_path: Path) -> None:
    """Partial mapping is the one state this widening must NOT let through.

    Complete mapping earns Verified/Grounded; an empty mapping is the honest
    ungrounded answer.  Partial is claims the server could only half-associate,
    and SPEC 10F forces it to aggregate Unavailable -- so the widened gate must
    read the mapping state, not just the verdict word beside it.
    """

    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "partial-mapping")
        output = b"PARTIALLY-MAPPED-OUTPUT-SENTINEL"
        await _finalize_source_free(ready, output)
        preview = _previewer(ready)

        assert await preview() == output.decode()
        _restate_terminal(ready, mapping_state="partial")
        assert await preview() is None

    asyncio.run(scenario())


def test_a_refusal_serves_its_refusal_text_and_keeps_its_own_verdict(
    tmp_path: Path,
) -> None:
    """A refusal is a served disposition, so its TEXT is served -- with its verdict.

    THIS ASSERTION IS A DELIBERATE REVERSAL of the one this test shipped with,
    and the reversal is the point.  The earlier rule said a refusal's bytes stay
    withheld because "the console already states a refusal through its own
    Refused pill".  That reasoning does not survive contact with what the
    console actually draws: with `output_preview` null, the Output pane renders
    "no output was served" -- so the ONE beat this product exists to demonstrate,
    the system correctly declining to fabricate an answer, rendered identically
    to the system being broken.  A refusal the reader cannot read is
    indistinguishable from an outage.

    So disclosure and verdict are separated, in the direction SPEC 10F already
    points ("Refused is a successfully served output disposition"): the refusal
    TEXT is disclosed to its owner, and `output_verdict` stays `refused` so
    every surface still draws the Refused treatment rather than a success.  The
    verdict is a RENDERED FACT, never the key to the disclosure.
    """

    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "refused-terminal")
        output = b"REFUSAL-TEXT-SENTINEL"
        finalizer = RunFinalizer(ready.runs)
        observations = await finalizer.observe(
            target=ready.target, delivery=_empty_delivery(ready.target)
        )
        output_hash = hashlib.sha256(output).hexdigest()
        receipt = await finalizer.finalize(
            RunTerminalCandidate(
                observations=observations,
                envelope=StructuredRunOutputEnvelope(
                    discriminator="structured_run_output",
                    served_output_hash=output_hash,
                    claim_count=0,
                    claim_set_hash=claim_set_hash(output_hash, ()),
                    claims=(),
                    disposition="refused",
                ),
                output_bytes=output,
            ),
            lease=_lease(ready),
        )
        assert receipt.output_verdict == "refused"
        assert receipt.mapping_state == "unavailable"
        assert receipt.protected_output_kind == "protected_turn_content"

        projection = AgentRunProjection(ready.runs, ready.engine)
        run = await projection.get_run(
            run_id=ready.target.run_id,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
        )
        assert run is not None
        # Both halves, together: the words the agent used to decline ARE served,
        # and the verdict beside them still says it declined.
        assert run.output_preview == output.decode()
        assert run.output_verdict == "refused"

    asyncio.run(scenario())


def test_a_refusal_is_still_withheld_from_a_foreign_principal(tmp_path: Path) -> None:
    """Serving refusal text must not widen WHO may read it -- the paired half."""

    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "refused-foreign")
        output = b"REFUSAL-OWNER-ONLY-SENTINEL"
        finalizer = RunFinalizer(ready.runs)
        observations = await finalizer.observe(
            target=ready.target, delivery=_empty_delivery(ready.target)
        )
        output_hash = hashlib.sha256(output).hexdigest()
        await finalizer.finalize(
            RunTerminalCandidate(
                observations=observations,
                envelope=StructuredRunOutputEnvelope(
                    discriminator="structured_run_output",
                    served_output_hash=output_hash,
                    claim_count=0,
                    claim_set_hash=claim_set_hash(output_hash, ()),
                    claims=(),
                    disposition="refused",
                ),
                output_bytes=output,
            ),
            lease=_lease(ready),
        )
        projection = AgentRunProjection(ready.runs, ready.engine)
        assert (
            await projection.get_run(
                run_id=ready.target.run_id,
                owner_principal_id="user:foreign",
                department=DEPARTMENT,
                agent_id=AGENT,
            )
            is None
        )

    asyncio.run(scenario())


def test_the_one_run_read_serves_the_whole_answer_and_list_rows_stay_capped(
    tmp_path: Path,
) -> None:
    """THE THIRD LIMITER: a 500-character stub where the answer belongs.

    The cap is a LIST-PAGE BUDGET -- a page enriches up to 25 rows and each
    enrichment decrypts one protected terminal, so an uncapped list read is 25
    whole answers on one response.  The single-Run read decrypts exactly one,
    and truncating it there served a stub past a gate that had just been opened.

    Both halves in one test, because either alone is the bug: the one-run read
    is WHOLE, and the list row is still capped.
    """

    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "full-output-cap")
        # Long enough that a 500-character cap is unmistakable, and structured
        # so the assertion names the exact boundary rather than a length.
        output = ("A" * 400 + "B" * 400).encode()
        assert len(output) > 500
        await _finalize_source_free(ready, output)

        projection = AgentRunProjection(ready.runs, ready.engine)
        run = await projection.get_run(
            run_id=ready.target.run_id,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
        )
        assert run is not None
        assert run.output_preview == output.decode()

        rows, _ = await projection.list_runs(
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
            limit=25,
            after=None,
        )
        assert len(rows) == 1
        assert rows[0].output_preview == output.decode()[:500]
        assert rows[0].output_preview != output.decode()

    asyncio.run(scenario())


def test_one_gate_un_blanks_all_three_surfaces_that_read_it(tmp_path: Path) -> None:
    """THE CLAIM UNDER TEST: there is exactly ONE output seam, gated in one place.

    `_preview_target` is consulted by `_enrich_run`, and `_enrich_run` is what
    the Run detail read, the Run LIST and the CONVERSATION TURNS read all go
    through.  So the disclosure fix is one fix and it lands on three screens at
    once: the Ask thread, the Run workspace Summary tab, and the Conversation
    turn cards.

    This pins the third surface, which had no coverage of its own -- and it is
    the one a reader hits by RELOADING an Ask thread, where a regression would
    look exactly like the original defect coming back.
    """

    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "three-surfaces")
        output = b"Provisional credit is due within 10 business days."
        await _finalize_source_free(ready, output)

        projection = AgentRunProjection(ready.runs, ready.engine)
        detail = await projection.get_run(
            run_id=ready.target.run_id,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
        )
        assert detail is not None
        rows, _ = await projection.list_runs(
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
            limit=25,
            after=None,
        )
        turns, _ = await projection.conversation_turns(
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            agent_id=AGENT,
            conversation_id=detail.conversation_id,
            limit=25,
            after_sequence=None,
        )
        assert detail.output_preview == output.decode()
        assert [row.output_preview for row in rows] == [output.decode()]
        assert [turn.run.output_preview for turn in turns] == [output.decode()]

        # AND THE OWNER RULE HOLDS ON ALL THREE. Widening what an owner may read
        # of their own answer must never widen WHO may read it.
        assert (
            await projection.get_run(
                run_id=ready.target.run_id,
                owner_principal_id="user:foreign",
                department=DEPARTMENT,
                agent_id=AGENT,
            )
            is None
        )
        foreign_rows, _ = await projection.list_runs(
            owner_principal_id="user:foreign",
            department=DEPARTMENT,
            agent_id=AGENT,
            limit=25,
            after=None,
        )
        assert foreign_rows == ()
        foreign_turns, _ = await projection.conversation_turns(
            owner_principal_id="user:foreign",
            department=DEPARTMENT,
            agent_id=AGENT,
            conversation_id=detail.conversation_id,
            limit=25,
            after_sequence=None,
        )
        assert foreign_turns == ()

    asyncio.run(scenario())
