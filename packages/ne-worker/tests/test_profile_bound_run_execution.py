# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-010D exact profile consumer over PRE-010B's Run boundary."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel

import ne.worker.workflows as worker_workflows
from ne.api.tools import TURN_PRINCIPAL
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.types import Lane, Principal
from ne.runtime.agent import AgentExecutionBinding
from ne.runtime.correlation import (
    current_action_scope,
    current_run_requested_model,
    current_run_requested_model_lane,
    current_run_trace_id,
)
from ne.runtime.errors import (
    AgentOutputContractError,
    RunActivationError,
    RunSkillActivationError,
)
from ne.runtime.run_claim_verifier import ClaimVerificationUnavailable
from ne.runtime.run_finalizer import (
    RunTurnTarget,
    derived_failure_reason,
    published_failure_reason,
)
from ne.runtime.run_runner import RunExecutionContext, RunExecutionResult
from ne.runtime.run_store import (
    AgentRun,
    ClaimVerifierRecord,
    RunClaimEvidenceObservation,
    RunState,
)
from ne.runtime.runner import (
    BoundedAgenticAdapter,
    ModelPlannedClaim,
    ModelPlannedRun,
    model_request_limit,
    output_retry_allowances,
)
from ne.runtime.source_results import (
    authenticated_request_scope,
    complete_run_delivery_observation,
    model_call_scope,
    require_direct_publication_binding,
    run_delivery_observation_scope,
)
from ne.runtime.workflow_execution_profile import (
    MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
    ExecutionStrategy,
)
from ne.worker.run_execution import ProfileBoundRunExecutionAdapter, _source_free_envelope


class _Resolver:
    def __init__(self, principal: Principal) -> None:
        self.principal = principal
        self.calls: list[str] = []

    async def resolve(self, subject: str) -> Principal:
        self.calls.append(subject)
        return self.principal


class _TypedResult(BaseModel):
    outcome: str = "complete"


class _ProfileReceipts:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def record_execution_profile(self, **values: Any) -> Any:
        self.calls.append(values)
        return SimpleNamespace(receipt_hash="f" * 64)


def _run(
    *,
    strategy: str = "deterministic",
    version: int = 1,
    agent_id: str = "agt_01k3hardshipreview0000000000",
    requested_model: str | None = None,
    requested_model_lane: str | None = None,
    agent_definition_hash: str | None = None,
    model_policy_id: str | None = None,
    authorized_model: str | None = None,
) -> AgentRun:
    now = datetime.now(UTC)
    return AgentRun(
        "run-1",
        "conversation-1",
        "owner-1",
        "ops",
        "Meridian Hardship Review",
        agent_id,
        version,
        strategy,
        "a" * 64,
        "b" * 64,
        "snapshot-receipt",
        1,
        "Run 1",
        RunState.RUNNING,
        now,
        now,
        None,
        requested_model=requested_model,
        requested_model_lane=requested_model_lane,
        agent_definition_hash=agent_definition_hash,
        model_policy_id=model_policy_id,
        authorized_model=authorized_model,
    )


def _binding() -> AgentExecutionBinding:
    return AgentExecutionBinding(
        agent_id="agt_01k3hardshipreview0000000000",
        agent_version=1,
        strategy=ExecutionStrategy.DETERMINISTIC,
        workflow_profile=MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
    )


def _context(
    *,
    strategy: str = "deterministic",
    version: int = 1,
    agent_id: str = "agt_01k3hardshipreview0000000000",
    input_bytes: bytes = b'{"max_subjects":3}',
    recovered: bool = False,
    requested_model: str | None = None,
    requested_model_lane: str | None = None,
    boundary: Any = None,
    agent_definition_hash: str | None = None,
    model_policy_id: str | None = None,
    authorized_model: str | None = None,
) -> RunExecutionContext:
    return RunExecutionContext(
        run=_run(
            strategy=strategy,
            version=version,
            agent_id=agent_id,
            requested_model=requested_model,
            requested_model_lane=requested_model_lane,
            agent_definition_hash=agent_definition_hash,
            model_policy_id=model_policy_id,
            authorized_model=authorized_model,
        ),
        attempt_id="attempt-1",
        target=RunTurnTarget(
            conversation_id="conversation-1",
            run_id="run-1",
            turn_id="turn-1",
            turn_sequence=1,
            attachment_id="attachment-1",
            attempt_id="attempt-1",
        ),
        input_bytes=input_bytes,
        gateway=cast(Any, object()),
        executor=cast(Any, object()),
        audit_sink=cast(Any, object()),
        recovered_uncertain_model_attempt=recovered,
        **({"boundary": boundary} if boundary is not None else {}),
    )


async def _execute(adapter: ProfileBoundRunExecutionAdapter, context: RunExecutionContext):  # type: ignore[no-untyped-def]
    with run_delivery_observation_scope(run_id="run-1", attempt_id="attempt-1"):
        if context.run.strategy == "deterministic":
            return await adapter.execute(context)
        with model_call_scope(run_id="run-1", model_call_attempt_id="attempt-1"):
            return await adapter.execute(context)


def test_exact_profile_consumer_validates_input_and_effective_principal() -> None:
    resolver = _Resolver(Principal(id="owner-1", groups=("ops",)))
    received: list[tuple[Any, Any, Principal, RunExecutionContext]] = []

    async def deterministic(profile: Any, value: Any, principal: Principal, context: Any) -> Any:
        assert TURN_PRINCIPAL.get() == principal
        direct = require_direct_publication_binding()
        assert direct.workflow_execution_id == context.run.run_id
        assert direct.publication_attempt_id == context.attempt_id
        received.append((profile, value, principal, context))
        return _TypedResult()

    receipts = _ProfileReceipts()
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={_binding().agent_id: _binding()},
        principal_resolver=resolver,
        deterministic_executor=deterministic,
        runtime_settings=object(),
        sink=object(),
        run_store=receipts,
    )
    context = _context()
    result = asyncio.run(_execute(adapter, context))

    assert result.outcome == "execution_complete"
    assert resolver.calls == ["owner-1"]
    profile, value, principal, consumed_context = received[0]
    assert profile is MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE
    assert value.max_subjects == 3
    assert principal == Principal(id="owner-1", groups=("ops",))
    assert consumed_context is context
    assert TURN_PRINCIPAL.get() is None
    assert receipts.calls == [
        {
            "run_id": "run-1",
            "attempt_id": "attempt-1",
            "workflow_id": MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE.workflow_id,
            "profile_version": MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE.version,
            "profile_hash": MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE.config_hash,
            "input_schema": dict(MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE.input_schema),
            "ordered_plan": tuple(
                step.model_dump(mode="json")
                for step in MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE.fixed_plan
            ),
        }
    ]


def test_execution_unions_verified_request_groups_with_store_groups() -> None:
    received: list[Principal] = []

    async def deterministic(_profile: Any, _value: Any, principal: Principal, _context: Any) -> Any:
        received.append(principal)
        return _TypedResult()

    adapter = ProfileBoundRunExecutionAdapter(
        bindings={_binding().agent_id: _binding()},
        principal_resolver=_Resolver(Principal(id="owner-1", groups=("store-group",))),
        deterministic_executor=deterministic,
        runtime_settings=object(),
        sink=object(),
        run_store=_ProfileReceipts(),
    )
    authenticated = AuthenticatedRequestContext(
        principal_id="owner-1",
        authorization_groups=("bank.customer.read", "ops"),
        department="engineering",
        issuer="https://issuer.example.test",
        expires_at=4_102_444_800,
    )

    with authenticated_request_scope(authenticated):
        result = asyncio.run(_execute(adapter, _context()))

    assert result.outcome == "execution_complete"
    assert received == [
        Principal(
            id="owner-1",
            groups=("bank.customer.read", "ops", "store-group"),
        )
    ]


def test_execution_refuses_authenticated_principal_that_differs_from_owner() -> None:
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={_binding().agent_id: _binding()},
        principal_resolver=_Resolver(Principal(id="owner-1", groups=("ops",))),
        deterministic_executor=cast(Any, object()),
        runtime_settings=object(),
        sink=object(),
        run_store=_ProfileReceipts(),
    )
    authenticated = AuthenticatedRequestContext(
        principal_id="other-owner",
        authorization_groups=("ops",),
        department="engineering",
        issuer="https://issuer.example.test",
        expires_at=4_102_444_800,
    )

    with (
        authenticated_request_scope(authenticated),
        pytest.raises(
            PermissionError,
            match="authenticated Run principal differs from the accepted owner",
        ),
    ):
        asyncio.run(_execute(adapter, _context()))


def test_execution_uses_persisted_requested_model_and_exact_run_trace_scopes() -> None:
    observed: list[tuple[str | None, Lane | None, str | None]] = []

    async def deterministic(
        _profile: Any, _value: Any, _principal: Principal, _context: Any
    ) -> Any:
        observed.append(
            (
                current_run_requested_model(),
                current_run_requested_model_lane(),
                current_run_trace_id(),
            )
        )
        return _TypedResult()

    adapter = ProfileBoundRunExecutionAdapter(
        bindings={_binding().agent_id: _binding()},
        principal_resolver=_Resolver(Principal(id="owner-1", groups=("ops",))),
        deterministic_executor=deterministic,
        runtime_settings=object(),
        sink=object(),
        run_store=_ProfileReceipts(),
    )
    asyncio.run(
        _execute(
            adapter,
            _context(
                requested_model="private-reasoner",
                requested_model_lane=Lane.PRIVATE_VLLM.value,
            ),
        )
    )
    assert observed == [("private-reasoner", Lane.PRIVATE_VLLM, "run-1")]


def test_agentic_consumer_preserves_a_accepted_plaintext_instruction() -> None:
    captured: dict[str, Any] = {}
    scopes: list[str | None] = []

    class _Agentic:
        async def run(self, **kwargs: Any) -> Any:
            assert TURN_PRINCIPAL.get() == kwargs["principal"]
            captured.update(kwargs)
            scopes.append(current_action_scope())
            complete_run_delivery_observation()
            return SimpleNamespace(outcome="complete", output="agentic output")

    binding = AgentExecutionBinding(
        agent_id="agt_agentic",
        agent_version=1,
        strategy=ExecutionStrategy.AGENTIC,
        agentic_adapter=cast(Any, _Agentic()),
    )
    resolver = _Resolver(Principal(id="owner-1", groups=("ops",)))
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={"agt_agentic": binding},
        principal_resolver=resolver,
        deterministic_executor=cast(Any, None),
        runtime_settings=object(),
        sink=object(),
    )
    context = _context(
        strategy="agentic",
        agent_id="agt_agentic",
        input_bytes=b"PRIVATE accepted Run input",
    )

    async def scenario() -> tuple[Any, Any]:
        first = await _execute(adapter, context)
        recovered = await _execute(
            adapter,
            _context(
                strategy="agentic",
                agent_id="agt_agentic",
                input_bytes=b"PRIVATE accepted Run input",
                recovered=True,
            ),
        )
        return first, recovered

    result, recovered = asyncio.run(scenario())

    assert result.outcome == "execution_complete"
    assert recovered.outcome == "execution_complete"
    assert captured["task"] == "PRIVATE accepted Run input"
    assert captured["principal"] == Principal(id="owner-1", groups=("ops",))
    assert captured["boundary"] is context.boundary
    assert scopes == ["agent-run:run-1:attempt-1", "agent-run:run-1:attempt-1"]
    assert TURN_PRINCIPAL.get() is None


@pytest.mark.parametrize(
    ("context", "message"),
    [
        (_context(version=2), "version differs"),
        (_context(strategy="agentic"), "strategy differs"),
    ],
)
def test_run_identity_must_match_the_exact_boot_binding(
    context: RunExecutionContext, message: str
) -> None:
    resolver = _Resolver(Principal(id="owner-1"))

    async def unused(*_args: Any) -> None:
        raise AssertionError("mismatched Run reached execution")

    adapter = ProfileBoundRunExecutionAdapter(
        bindings={_binding().agent_id: _binding()},
        principal_resolver=resolver,
        deterministic_executor=unused,
        runtime_settings=object(),
        sink=object(),
    )
    with pytest.raises(RuntimeError, match=message):
        asyncio.run(_execute(adapter, context))
    assert resolver.calls == []


def test_resolved_principal_cannot_change_the_accepted_owner() -> None:
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={_binding().agent_id: _binding()},
        principal_resolver=_Resolver(Principal(id="other-owner")),
        deterministic_executor=cast(Any, None),
        runtime_settings=object(),
        sink=object(),
    )
    with pytest.raises(PermissionError, match="differs from the accepted owner"):
        asyncio.run(_execute(adapter, _context()))


def test_worker_service_builds_the_profile_adapter_from_the_shared_platform() -> None:
    source = inspect.getsource(worker_workflows._run_workflow_service)
    assert "build_profile_bound_run_execution_adapter(platform)" in source
    assert "UnavailableRunExecutionAdapter" not in inspect.getsource(worker_workflows)


def test_agentic_source_claims_use_exact_reverse_ordered_server_observations() -> None:
    claim_text = "Meridian hardship support is available"
    output = f"{claim_text}. {claim_text}."
    first_start = 0
    second_start = len(claim_text) + 2
    rendering = (
        b"[showing 1 of 1 retrieved chunks]\nchunk-a: Meridian hardship support is available"
    )
    locator = json.dumps(
        {
            "kind": "knowledge_search",
            "contributors": [{"source": "m365", "revision": "revision-1"}],
            "chunk_ids": ["chunk-a"],
            "evidence_assurance": "direct",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    def observation(protected_ref: str, evidence_id: str) -> RunClaimEvidenceObservation:
        return RunClaimEvidenceObservation(
            protected_ref=protected_ref,
            protected_result_hash=hashlib.sha256(rendering).hexdigest(),
            evidence_id=evidence_id,
            locator_id=f"locator-{evidence_id}",
            locator_hash=hashlib.sha256(locator).hexdigest(),
            producing_event_id=f"event-{evidence_id}",
            delivery_receipt_id=f"delivery-{evidence_id}",
            delivery_receipt_hash=hashlib.sha256(f"delivery-{evidence_id}".encode()).hexdigest(),
            protected_rendering=rendering,
            locator=locator,
            read_kind="search",
            assurance="direct",
        )

    class _RunStore:
        def __init__(self) -> None:
            self.observations = {
                "ne-source-ref:v1:ref-a": observation("ne-source-ref:v1:ref-a", "evidence-a"),
                "ne-source-ref:v1:ref-b": observation("ne-source-ref:v1:ref-b", "evidence-b"),
            }
            self.loads: list[str] = []

        async def load_run_claim_evidence(self, **kwargs: Any) -> RunClaimEvidenceObservation:
            self.loads.append(kwargs["protected_ref"])
            return self.observations[kwargs["protected_ref"]]

        async def claim_run_claim_verifier(self, **kwargs: Any) -> ClaimVerifierRecord:
            key = kwargs["claim_key"]
            return ClaimVerifierRecord(
                verifier_receipt_id=f"verifier-{key}",
                event_id=f"verifier-event-{key}",
                receipt_hash=hashlib.sha256(f"verifier-{key}".encode()).hexdigest(),
            )

    store = _RunStore()
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={},
        principal_resolver=_Resolver(Principal(id="owner-1")),
        deterministic_executor=cast(Any, None),
        runtime_settings=object(),
        sink=object(),
        run_store=store,
    )
    result = asyncio.run(
        adapter._agentic_result(
            _context(strategy="agentic"),
            ModelPlannedRun(
                outcome="complete",
                output=output,
                served_row_ids=("ne-source-ref:v1:ref-a", "ne-source-ref:v1:ref-b"),
                claims=(
                    ModelPlannedClaim(
                        claim_key="claim-a",
                        span_start_byte=first_start,
                        span_end_byte=first_start + len(claim_text),
                        claim_content_hash=hashlib.sha256(claim_text.encode()).hexdigest(),
                        served_ref="ne-source-ref:v1:ref-b",
                    ),
                    ModelPlannedClaim(
                        claim_key="claim-b",
                        span_start_byte=second_start,
                        span_end_byte=second_start + len(claim_text),
                        claim_content_hash=hashlib.sha256(claim_text.encode()).hexdigest(),
                        served_ref="ne-source-ref:v1:ref-a",
                    ),
                ),
            ),
        )
    )

    assert result.outcome == "execution_complete"
    assert store.loads == ["ne-source-ref:v1:ref-b", "ne-source-ref:v1:ref-a"]
    assert [binding.evidence_id for binding in result.evidence_bindings] == [
        "evidence-b",
        "evidence-a",
    ]
    assert result.structured_envelope is not None
    assert [claim.claim_key for claim in result.structured_envelope.claims] == [
        "claim-a",
        "claim-b",
    ]


# --- NE-CLAIM-SHARED-EVIDENCE: one record, several claims ----------------------


_SHARED_SENTENCES = (
    "Loan mer-loan-001 has an unpaid scheduled payment due 2026-09-03.",
    "Loan mer-loan-002 has an unpaid scheduled payment due 2026-09-05.",
)
_FABRICATED_SENTENCE = "Loan mer-loan-777 has an unpaid scheduled payment due 2026-09-09."


def _shared_record_store() -> Any:
    """One served search record whose TWO chunks name two different loans.

    This is the flagship question's real shape: a single retrieval answers the
    question about several loans, so the model has several claims to make and
    exactly one record to cite for them.
    """
    rendering = (
        "[showing 2 of 2 retrieved chunks]\n"
        f"chunk-a: {_SHARED_SENTENCES[0]}\n"
        f"chunk-b: {_SHARED_SENTENCES[1]}"
    ).encode()
    locator = json.dumps(
        {
            "kind": "knowledge_search",
            "contributors": [{"source": "m365", "revision": "revision-1"}],
            "chunk_ids": ["chunk-a", "chunk-b"],
            "evidence_assurance": "direct",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    observation = RunClaimEvidenceObservation(
        protected_ref="ne-source-ref:v1:shared",
        protected_result_hash=hashlib.sha256(rendering).hexdigest(),
        evidence_id="evidence-shared",
        locator_id="locator-shared",
        locator_hash=hashlib.sha256(locator).hexdigest(),
        producing_event_id="event-shared",
        delivery_receipt_id="delivery-shared",
        delivery_receipt_hash=hashlib.sha256(b"delivery-shared").hexdigest(),
        protected_rendering=rendering,
        locator=locator,
        read_kind="search",
        assurance="direct",
    )

    class _RunStore:
        def __init__(self) -> None:
            self.verifier_keys: list[str] = []

        async def load_run_claim_evidence(self, **kwargs: Any) -> RunClaimEvidenceObservation:
            assert kwargs["protected_ref"] == observation.protected_ref
            return observation

        async def claim_run_claim_verifier(self, **kwargs: Any) -> ClaimVerifierRecord:
            key = kwargs["claim_key"]
            self.verifier_keys.append(key)
            return ClaimVerifierRecord(
                verifier_receipt_id=f"verifier-{key}",
                event_id=f"verifier-event-{key}",
                receipt_hash=hashlib.sha256(f"verifier-{key}".encode()).hexdigest(),
            )

    return _RunStore()


def _shared_record_run(sentences: tuple[str, ...], *, claim_keys: tuple[str, ...]) -> Any:
    """Claims over one answer, every one of them citing the SAME served record."""
    output = " ".join(sentences)
    claims = []
    for key, sentence in zip(claim_keys, sentences, strict=True):
        start = output.index(sentence)
        claims.append(
            ModelPlannedClaim(
                claim_key=key,
                span_start_byte=start,
                span_end_byte=start + len(sentence),
                claim_content_hash=hashlib.sha256(sentence.encode()).hexdigest(),
                served_ref="ne-source-ref:v1:shared",
            )
        )
    return ModelPlannedRun(
        outcome="complete",
        output=output,
        served_row_ids=("ne-source-ref:v1:shared",),
        claims=tuple(claims),
    )


def _shared_record_result(run: Any, store: Any) -> Any:
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={},
        principal_resolver=_Resolver(Principal(id="owner-1")),
        deterministic_executor=cast(Any, None),
        runtime_settings=object(),
        sink=object(),
        run_store=store,
    )
    return asyncio.run(adapter._agentic_result(_context(strategy="agentic"), run))


def test_two_claims_may_cite_one_served_record_and_each_gets_its_own_verifier() -> None:
    """The flagship question's shape reaches a served answer, not a RuntimeError.

    `one served evidence record cannot bind two local Run claims` used to be
    raised HERE, as a bare `RuntimeError` that escaped into DBOS. It protected
    nothing: `ne_agent_run_claim_evidence` makes `claim_id` unique and leaves
    `evidence_id` unconstrained, and both local binding keys are derived from
    the CLAIM key, so N claims over one record were always representable.
    """
    store = _shared_record_store()
    result = _shared_record_result(
        _shared_record_run(_SHARED_SENTENCES, claim_keys=("claim-a", "claim-b")), store
    )

    assert result.outcome == "execution_complete"
    assert result.structured_envelope is not None
    assert [claim.claim_key for claim in result.structured_envelope.claims] == [
        "claim-a",
        "claim-b",
    ]
    # One record...
    assert {binding.evidence_id for binding in result.evidence_bindings} == {"evidence-shared"}
    # ...and nothing else shared. Two bindings, two verifier receipts, two
    # spans, two local keys on each side.
    assert len(result.evidence_bindings) == len(result.verifier_bindings) == 2
    assert len({binding.evidence_key for binding in result.evidence_bindings}) == 2
    assert len({binding.served_citation_ref_hash for binding in result.evidence_bindings}) == 2
    assert len({binding.verifier_key for binding in result.verifier_bindings}) == 2
    assert len({binding.verifier_receipt_id for binding in result.verifier_bindings}) == 2
    assert len({binding.receipt_hash for binding in result.verifier_bindings}) == 2
    assert store.verifier_keys == ["claim-a", "claim-b"]
    spans = {
        (claim.span_start_byte, claim.span_end_byte) for claim in result.structured_envelope.claims
    }
    assert len(spans) == 2


def test_a_fabricated_claim_still_hard_fails_when_it_shares_a_record_with_a_real_one() -> None:
    """Sharing a record buys a claim nothing. Grounding is still per claim."""
    with pytest.raises(ClaimVerificationUnavailable) as raised:
        _shared_record_result(
            _shared_record_run(
                (_SHARED_SENTENCES[0], _FABRICATED_SENTENCE),
                claim_keys=("claim-a", "claim-fabricated"),
            ),
            _shared_record_store(),
        )

    reason = raised.value.safe_reason
    assert 0 < len(reason) <= 256
    assert all(character.isprintable() for character in reason)
    assert "mer-loan-777" not in reason


def test_two_claims_under_one_claim_key_fail_with_a_governed_reason() -> None:
    """The invariant that IS load-bearing, and it never reaches DBOS unnamed.

    Both local binding keys are derived from the claim key, so two claims
    sharing one would collapse onto a single verifier row — the integrity loss
    the old check was reaching for and did not catch. `runner` refuses this
    upstream where a corrective retry is still available; this backstop is a
    TYPED error, so the run publishes a sentence instead of a failure class.
    """
    store = _shared_record_store()
    with pytest.raises(AgentOutputContractError, match="one claim key") as raised:
        _shared_record_result(
            _shared_record_run(_SHARED_SENTENCES, claim_keys=("claim-a", "claim-a")), store
        )

    reason = raised.value.safe_reason
    assert 0 < len(reason) <= 256
    assert all(character.isprintable() for character in reason)
    assert not any(sentence in reason for sentence in _SHARED_SENTENCES)
    # It stopped ON the duplicate, before minting a second verifier receipt.
    assert store.verifier_keys == ["claim-a"]


def test_no_bare_runtime_error_can_escape_the_claim_binding_path() -> None:
    """Every raise in the claim-binding path carries a publishable reason.

    `run_workflow` turns an unmarked exception into `Recorded failure class:
    RuntimeError` — a floor, not an explanation. Any `raise` added to this
    function must therefore be a typed error carrying `safe_reason`.
    """
    source = inspect.getsource(ProfileBoundRunExecutionAdapter._source_bound_agentic_result)
    assert "RuntimeError(" not in source
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("raise "):
            assert "AgentOutputContractError" in stripped, stripped


def test_agentic_served_source_without_claims_never_downgrades_to_source_free() -> None:
    """The BACKSTOP still refuses — and now says why.

    `ne.runtime.runner` enforces this at generation time, where the model still
    has a bounded retry to correct it, so this path should be unreachable. It
    stays, and it carries a `safe_reason`: this is the exact line that published
    `failure_reason: null` and left the reader a bare status word.
    """
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={},
        principal_resolver=_Resolver(Principal(id="owner-1")),
        deterministic_executor=cast(Any, None),
        runtime_settings=object(),
        sink=object(),
    )
    with pytest.raises(AgentOutputContractError, match="declared no claims") as raised:
        asyncio.run(
            adapter._agentic_result(
                _context(strategy="agentic"),
                ModelPlannedRun(
                    outcome="complete",
                    output="source-bearing output",
                    served_row_ids=("ne-source-ref:v1:served",),
                ),
            )
        )

    reason = raised.value.safe_reason
    assert 0 < len(reason) <= 256
    assert all(character.isprintable() for character in reason)
    assert "source-bearing output" not in reason


# --- a budget terminal says WHICH budget ---------------------------------------


def _exhaustion_adapter() -> BoundedAgenticAdapter:
    """The Meridian Analyst's own composed caps, so the sentences quote real values."""
    return BoundedAgenticAdapter(
        instructions="index",
        max_tool_calls=10,
        total_tokens_limit=96650,
        wall_clock_seconds=120.0,
        max_parked_writes=1,
    )


def _exhausted(cap: str | None, *, adapter: BoundedAgenticAdapter | None) -> str | None:
    result = asyncio.run(
        _adapter()._agentic_result(
            _context(strategy="agentic"),
            ModelPlannedRun(outcome="budget-exhausted", exhausted_cap=cap),
            adapter,
        )
    )
    assert result.outcome == "budget_exhausted"
    return result.exhausted_reason


def _adapter() -> ProfileBoundRunExecutionAdapter:
    return ProfileBoundRunExecutionAdapter(
        bindings={},
        principal_resolver=_Resolver(Principal(id="owner-1")),
        deterministic_executor=cast(Any, None),
        runtime_settings=object(),
        sink=object(),
    )


def test_a_budget_exhausted_run_names_the_ceiling_it_actually_hit() -> None:
    """`budget_exhausted` is ONE word for FIVE ceilings, so the word alone is not an answer.

    A reader told only "budget_exhausted" cannot tell a run that spent its tokens
    from one that ran out of clock, and cannot tell which number to change. The
    runner already types the distinction; these are the sentences it becomes, each
    quoting the ceiling's configured value from the same adapter that enforced it.
    """
    adapter = _exhaustion_adapter()
    assert _exhausted("tokens", adapter=adapter) == (
        "stopped: the turn's 96,650-token budget was exhausted"
    )
    assert _exhausted("tool-calls", adapter=adapter) == (
        "stopped: the turn's 10-tool-call budget was exhausted"
    )
    # Derived from max_tool_calls exactly as run_model_planned_agent derives its
    # own request_limit, so the sentence cannot claim a ceiling nothing enforced.
    #
    # ⛔ THIS WAS A HARDCODED 16 AND THE COMMENT ABOVE ALREADY CLAIMED IT WAS
    # DERIVED. Raising `_OUTPUT_RETRY_ALLOWANCES["answer-shape"]` 3 -> 4 moved
    # `model_request_limit` by one and this literal did not follow, so the test
    # asserted a ceiling the runner no longer enforced -- the exact failure its
    # own sentence exists to prevent. It now calls the same function the runner
    # calls, so the next allowance change cannot leave it behind.
    # Recomputed from the CONSTANTS the runner's formula is built from, NOT by
    # calling `model_request_limit` itself: deriving both sides from one function
    # asserts only that the function equals itself, and a mutation making it
    # return 999 passed. This spelling bites on a change to the formula as well
    # as on a change to the allowances.
    #
    # NE-WRAPPED-CALL-FUNDED: the allowances are no longer a module-level table
    # of constants -- the `wrapped-call` class is one corrective round per rung
    # of the ladder -- so the sum is taken over the RESOLVED allowances for this
    # adapter's own `max_tool_calls`. The raw table holds a negative sentinel
    # there and summing it would silently UNDER-count, which is the direction
    # that makes a run die `budget_exhausted` before its correction is sent.
    expected_requests = (
        adapter.max_tool_calls
        + 1
        + sum(n for _, n in output_retry_allowances(adapter.max_tool_calls))
    )
    assert expected_requests == model_request_limit(adapter.max_tool_calls)
    assert _exhausted("requests", adapter=adapter) == (
        f"stopped: the turn's {expected_requests}-model-request budget was exhausted"
    )
    assert _exhausted("wall-clock", adapter=adapter) == (
        "stopped: the turn's 120-second wall-clock budget was exhausted"
    )
    assert _exhausted("parked-writes", adapter=adapter) == (
        "stopped: the turn's 1-parked-draft budget was exhausted"
    )


def test_no_ceiling_sentence_is_invented_where_the_runner_named_none() -> None:
    """The discrimination control: silence stays silence.

    Without this the test above would pass against a composer that emits a
    plausible sentence for anything, which is precisely the failure the reason
    mechanism exists to avoid — a reader acting on a cause the server never
    recorded.
    """
    adapter = _exhaustion_adapter()
    # The runner could not say which ceiling: the reader gets the status word.
    assert _exhausted(None, adapter=adapter) is None
    # A cap this composer does not recognise is not guessed at.
    assert _exhausted("some-future-ceiling", adapter=adapter) is None
    # No adapter means no configured value to quote, so no sentence is written.
    assert _exhausted("tokens", adapter=None) is None
    # Context exhaustion is its own terminal and its own word; it never borrows
    # a budget sentence.
    context_result = asyncio.run(
        _adapter()._agentic_result(
            _context(strategy="agentic"),
            ModelPlannedRun(outcome="context-exhausted", exhausted_cap="context"),
            adapter,
        )
    )
    assert context_result.outcome == "context_exhausted"
    assert context_result.exhausted_reason is None


def test_a_reason_the_reader_would_drop_fails_loud_instead() -> None:
    """`run_projection._failure_reason` drops any reason over 256 chars, silently.

    A sentence that is silently dropped is worse than the blank it replaced: the
    producer believes it explained itself and the operator still sees nothing. The
    result object therefore refuses the same shapes the reader refuses.
    """
    with pytest.raises(ValueError, match="at most 256 characters"):
        RunExecutionResult(outcome="budget_exhausted", exhausted_reason="x" * 257)
    with pytest.raises(ValueError, match="at most 256 characters"):
        RunExecutionResult(outcome="budget_exhausted", exhausted_reason="stopped:\nsecond line")
    with pytest.raises(ValueError, match="no exhausted-ceiling reason"):
        RunExecutionResult(
            outcome="execution_complete",
            output_bytes=b"answer",
            structured_envelope=_source_free_envelope(b"answer"),
            exhausted_reason="stopped: the turn's 96,650-token budget was exhausted",
        )
    # The boundary itself is admitted, not merely "something short".
    assert (
        RunExecutionResult(outcome="budget_exhausted", exhausted_reason="y" * 256).exhausted_reason
        == "y" * 256
    )


# --- NE-RUN-ACTIVATION-SAYS-WHY -------------------------------------------------
# Skill activation happens BEFORE the model is called, so a run that dies there
# dies in seconds having touched nothing. Every refusal in that block used to be a
# bare `RuntimeError` that escaped into DBOS ("Future exception was never
# retrieved") and published `Recorded failure class: RuntimeError` — a class name
# for a condition the server could describe exactly. Measured live: four demo
# scenarios failed this way in 14-30s each when the caller's token stopped carrying
# the group label the pinned Skill is restricted to, and the record said nothing.
#
# The exactness of the pin is NOT relaxed by any of this: all of the Agent's pins,
# in order, or the run refuses. Only the refusal's explanation changed.

_SKILL_PIN = SimpleNamespace(
    skill_id="meridian-hardship-draft",
    version=1,
    content_hash="a9" * 32,
)


def _definition(
    *,
    skills: tuple[Any, ...] = (_SKILL_PIN,),
    instructions: str | None = None,
    model_policy_id: str | None = None,
) -> Any:
    return SimpleNamespace(
        agent_id="agt_01k3hardshipreview0000000000",
        version=1,
        default_strategy="agentic",
        definition_hash="a" * 64,
        model_policy_id=model_policy_id,
        skills=skills,
        instructions=instructions,
        knowledge_sources=(),
        optional_knowledge_sources=(),
    )


class _Catalog:
    def __init__(self, definition: Any) -> None:
        self._definition = definition

    def get_visible(self, agent_id: str, principal: Principal) -> Any:
        return SimpleNamespace(definition=self._definition, present=True, available=True)


class _Skills:
    """Activation that serves exactly the pins it is told to serve."""

    def __init__(self, served: tuple[Any, ...]) -> None:
        self._served = served
        self.calls = 0

    async def activate(self, definition: Any, principal: Principal, *, surface_key: str) -> Any:
        self.calls += 1
        return SimpleNamespace(
            pins=tuple(
                SimpleNamespace(
                    name=pin.skill_id, version=pin.version, content_hash=pin.content_hash
                )
                for pin in self._served
            ),
            index_hash="c" * 64,
            surface_hash="d" * 64,
            index_text="Available skills (1):\n- meridian-hardship-draft (v1): draft",
            pin_map=lambda: {},
        )


def _agentic_binding(adapter: Any) -> AgentExecutionBinding:
    return AgentExecutionBinding(
        agent_id="agt_01k3hardshipreview0000000000",
        agent_version=1,
        strategy=ExecutionStrategy.AGENTIC,
        workflow_profile=None,
        agentic_adapter=adapter,
    )


def test_a_run_whose_pinned_skill_is_not_served_publishes_why_not_a_bare_class() -> None:
    """THE LIVE REGRESSION'S RECORD. Same refusal, now with a reason attached."""

    skills = _Skills(())  # the pin resolved to nothing — e.g. ACL-invisible
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={},
        principal_resolver=_Resolver(Principal(id="owner-1")),
        deterministic_executor=cast(Any, None),
        runtime_settings=object(),
        sink=object(),
        catalog=_Catalog(_definition()),
        agent_skills=cast(Any, skills),
        run_store=cast(Any, SimpleNamespace()),
    )

    with pytest.raises(RunSkillActivationError) as raised:
        asyncio.run(
            adapter._execute_as_principal(
                _context(strategy="agentic"),
                _agentic_binding(SimpleNamespace()),
                Principal(id="owner-1"),
            )
        )

    error = raised.value
    assert skills.calls == 1
    assert (error.pinned, error.activated) == (1, 0)
    # The reason REACHES the reader: published verbatim, not the derived floor.
    published = published_failure_reason(
        safe_reason=error.safe_reason, failure_class=type(error).__name__
    )
    assert published == error.safe_reason
    assert published != derived_failure_reason(type(error).__name__)
    assert "Skill" in published and "1" in published and "0" in published
    assert len(published) <= 256
    assert all(character.isprintable() for character in published)
    # C12: counts over the Agent's OWN manifest and nothing else. No Skill id, no
    # authorization label, no principal — a refusal is never an existence oracle.
    assert "meridian-hardship-draft" not in published
    assert "department" not in published
    assert "owner-1" not in published


def test_every_activation_refusal_carries_a_reason_and_none_is_the_bare_floor() -> None:
    """The whole pre-flight block, not just the Skill pin."""

    reasons: set[str] = set()
    for row in (
        SimpleNamespace(definition=_definition(), present=False, available=True),
        SimpleNamespace(definition=_definition(), present=True, available=False),
        SimpleNamespace(
            definition=SimpleNamespace(**{**vars(_definition()), "version": 99}),
            present=True,
            available=True,
        ),
        SimpleNamespace(
            definition=SimpleNamespace(**{**vars(_definition()), "default_strategy": "workflow"}),
            present=True,
            available=True,
        ),
    ):

        class _Row:
            def __init__(self, value: Any) -> None:
                self.value = value

            def get_visible(self, agent_id: str, principal: Principal) -> Any:
                return self.value

        adapter = ProfileBoundRunExecutionAdapter(
            bindings={},
            principal_resolver=_Resolver(Principal(id="owner-1")),
            deterministic_executor=cast(Any, None),
            runtime_settings=object(),
            sink=object(),
            catalog=_Row(row),
            agent_skills=cast(Any, _Skills((_SKILL_PIN,))),
            run_store=cast(Any, SimpleNamespace()),
        )
        with pytest.raises(RunActivationError) as raised:
            asyncio.run(
                adapter._execute_as_principal(
                    _context(strategy="agentic"),
                    _agentic_binding(SimpleNamespace()),
                    Principal(id="owner-1"),
                )
            )
        reason = raised.value.safe_reason
        assert reason.startswith("The run could not start: ")
        assert (
            published_failure_reason(safe_reason=reason, failure_class=type(raised.value).__name__)
            == reason
        )
        reasons.add(reason)
    # Distinct preconditions get distinct sentences — not one shared blob. Three,
    # not four: `present=False` and `available=False` are ONE branch by design (an
    # Agent the caller cannot see and one that is unavailable are the same answer
    # to the caller, and splitting them would be an existence oracle).
    assert len(reasons) == 3


# --- the combination the unit suites missed -------------------------------------
# Skill activation and set-shaped (verified-T3) evidence are the two halves of the
# `read.kg.*` lane, and they had only ever been tested apart: the claim-binder
# tests called `_agentic_result` directly and never activated a Skill, and the
# activation tests never produced evidence. Live, the same four runs exercise both
# in one attempt. This drives ONE composed adapter through both.

_T3_SET_COLUMNS = ["customer", "provisional_credit_amount", "status"]
_T3_SET_ROWS = [["Meridian Bank", "412.55", "posted"], ["Northwind Trust", "88.20", "pending"]]


def _t3_observation() -> Any:
    from ne.runtime.run_store import RunClaimEvidenceSetMember, RunClaimEvidenceSetObservation

    lines = [
        f"[showing {len(_T3_SET_ROWS)} of {len(_T3_SET_ROWS)} rows]",
        " | ".join(_T3_SET_COLUMNS),
        "-+-".join("-" * len(column) for column in _T3_SET_COLUMNS),
        *(" | ".join(row) for row in _T3_SET_ROWS),
    ]
    rendering = "\n".join(lines).encode()
    members = []
    for index, row in enumerate(_T3_SET_ROWS):
        receipt_hash = hashlib.sha256(f"receipt-{index}".encode()).hexdigest()
        locator = json.dumps(
            {
                "kind": "verified_t3",
                "row_anchor": hashlib.sha256(
                    b"ne.verified-evidence-row-anchor.v1\x00" + receipt_hash.encode("ascii")
                ).hexdigest(),
                "receipt": {
                    "receipt_schema_version": 1,
                    "template_id": "reg_e_provisional_credits",
                    "template_version": 1,
                    "canonical_row_hash": hashlib.sha256(
                        json.dumps([_T3_SET_COLUMNS, row], separators=(",", ":")).encode("utf-8")
                    ).hexdigest(),
                    "contributor_schema_version": 1,
                    "contributors": [{"server_stamp": "stamp-a"}],
                    "contributor_count": 1,
                    "contributor_set_hash": "1" * 64,
                    "receipt_hash": receipt_hash,
                },
                "contributor_index": 0,
                "contributor": {"server_stamp": "stamp-a"},
                "evidence_assurance": "direct",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        members.append(
            RunClaimEvidenceSetMember(
                evidence_id=f"evidence-{index}",
                locator_id=f"locator-{index}",
                locator_hash=hashlib.sha256(locator).hexdigest(),
                producing_event_id=f"event-{index}",
                locator=locator,
            )
        )
    return RunClaimEvidenceSetObservation(
        protected_ref="pending-result-a",
        protected_result_hash=hashlib.sha256(rendering).hexdigest(),
        evidence_set_id="evidence-set-a",
        set_key="9" * 64,
        delivery_receipt_id="delivery-t3",
        delivery_receipt_hash="f" * 64,
        protected_rendering=rendering,
        members=tuple(members),
    )


def test_a_run_activates_its_skills_and_then_binds_set_shaped_evidence() -> None:

    observation = _t3_observation()
    answer = "Meridian Bank 412.55 posted and Northwind Trust 88.20 pending"
    recorded: list[dict[str, Any]] = []
    seen_instructions: list[str] = []

    class _Store:
        async def record_skill_activation(self, **values: Any) -> None:
            recorded.append(values)

        async def load_run_claim_evidence(self, **kwargs: Any) -> Any:
            assert kwargs["protected_ref"] == "ne-source-ref:v1:verified-set"
            return observation

        async def claim_run_claim_verifier(self, **kwargs: Any) -> ClaimVerifierRecord:
            key = kwargs["claim_key"]
            return ClaimVerifierRecord(
                verifier_receipt_id=f"verifier-{key}",
                event_id=f"verifier-event-{key}",
                receipt_hash=hashlib.sha256(key.encode()).hexdigest(),
            )

    @dataclass(frozen=True)
    class _Adapter:
        instructions: str = ""

        async def run(self, **kwargs: Any) -> Any:
            seen_instructions.append(self.instructions)
            raw = answer.encode()
            return ModelPlannedRun(
                outcome="complete",
                output=answer,
                served_row_ids=("ne-source-ref:v1:verified-set",),
                claims=(
                    ModelPlannedClaim(
                        claim_key="claim-total",
                        span_start_byte=0,
                        span_end_byte=len(raw),
                        claim_content_hash=hashlib.sha256(raw).hexdigest(),
                        served_ref="ne-source-ref:v1:verified-set",
                    ),
                ),
            )

    class _Settings(BaseModel):
        task: str | None = None

    adapter = ProfileBoundRunExecutionAdapter(
        bindings={},
        principal_resolver=_Resolver(Principal(id="owner-1")),
        deterministic_executor=cast(Any, None),
        runtime_settings=_Settings(),
        sink=object(),
        catalog=_Catalog(_definition(instructions="Follow the pinned procedure.")),
        agent_skills=cast(Any, _Skills((_SKILL_PIN,))),
        run_store=cast(Any, _Store()),
        effective_tool_grants={"agt_01k3hardshipreview0000000000": ("read.skills.view",)},
        scope_executor=lambda executor, **_: executor,
    )

    # POST_DEMO item 12 — the RUNTIME assertion a source test cannot make. An
    # adversarial pass was right that a positional-string test is the wrong remedy
    # for this feature: both defects it has already had were conditional or
    # positional AT RUNTIME (a marker outside its branch; markers on the wrong side
    # of the edge), and a third source test cannot catch the next one. Guard a
    # heartbeat as `if self._grounding is not None:` and every string assertion
    # still passes while ungated runs emit nothing.
    #
    # This test already drives the real `_execute_as_principal` through the
    # production composition, so it is the right place: `RunExecutionContext.
    # boundary` defaults to a no-op, and substituting a recorder is the whole trick.
    stages: list[str] = []

    class _RecordingBoundary:
        async def heartbeat(self, stage: str) -> None:
            stages.append(stage)

        async def checkpoint(self, *, stage: str, safe_detail: Any) -> None:
            return None

        async def await_inflight(self, operation: Any, *, stage: str) -> Any:
            return await operation

        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

    result = asyncio.run(
        adapter._execute_as_principal(
            _context(strategy="agentic", boundary=_RecordingBoundary()),
            _agentic_binding(_Adapter()),
            Principal(id="owner-1"),
        )
    )

    # The skills pair really FIRED, around real skill activation...
    assert "profile:skills-before" in stages, f"skills-before never fired; got {stages}"
    assert "profile:skills-after" in stages, f"skills-after never fired; got {stages}"
    assert stages.index("profile:skills-before") < stages.index("profile:skills-after")
    # ...and `profile:adapter-before` fired AFTER the production
    # `profile:agentic-before`, which is the ordering the whole increment depends
    # on — asserted against what RAN, not against where the text sits.
    #
    # It is NOT "the marker that splits the measured edge", which is what this
    # comment used to say and what this same file retracts below: between the
    # production `agentic-before` and the adapter call there is no I/O at all, so
    # `adapter-before` is a FENCE proving the cost is not in the worker's setup,
    # not a splitter of the 7.67 s.
    assert "profile:agentic-before" in stages
    assert "profile:adapter-before" in stages, f"adapter-before never fired; got {stages}"
    assert stages.index("profile:agentic-before") < stages.index("profile:adapter-before"), (
        f"adapter-before must fire INSIDE the agentic-before edge; observed: {stages}"
    )
    # THE OTHER ARM, asserted at runtime and not by reading source. This run has
    # no `model_policy_id`, so `continuity_active` is false and the history pair
    # must be ABSENT — that absence is the only thing that makes its presence
    # diagnostic, and it is the property a positional-string test cannot check at
    # all. Its partner below drives the branch itself.
    assert "profile:history-before" not in stages, (
        f"a run without continuity emitted a history marker; observed: {stages}"
    )
    assert "profile:history-after" not in stages, (
        f"a run without continuity emitted a history marker; observed: {stages}"
    )

    # The Skill surface activated exactly as pinned and was recorded...
    assert len(recorded) == 1
    assert recorded[0]["pins"] == (("meridian-hardship-draft", 1, "a9" * 32),)
    # NE-ANALYST-INSTRUCTIONS did not regress the SKILLED path: the run's
    # composed prompt is the Agent's own manifest instructions AND the rendered
    # Skill index, in that order — the index is appended, never displaced.
    assert seen_instructions == [
        "Follow the pinned procedure.\n\n"
        "Available skills (1):\n- meridian-hardship-draft (v1): draft"
    ]
    # ...and the SAME attempt bound a claim over the whole verified table.
    assert result.outcome == "execution_complete"
    assert len(result.evidence_bindings) == 1
    binding = result.evidence_bindings[0]
    # The join row names the set's FIRST member — a deterministic anchor.
    assert binding.evidence_id == "evidence-0"
    assert binding.producing_event_id == "event-0"
    assert len(result.verifier_bindings) == 1


def test_a_continuity_run_emits_its_history_pair_around_the_real_materialization() -> None:
    """THE ARM THE FIRST FIX MISSED, and the one that had the actual defect.

    Round 2 established that a source-text test cannot catch this feature's defect
    class, and I moved the runtime assertions onto the skills pair and the adapter
    marker. Both reviewers then landed on the same gap independently: I never
    applied the remedy to ``profile:history-*`` -- the pair whose defect WAS a
    runtime conditional (my first version emitted it on every run, including runs
    that materialize no history). ``_context(...)`` builds a run with no
    ``model_policy_id``, so ``continuity_active`` is false and that branch never
    executes under the recorder; ``materialize_conversation_history`` appears in
    exactly one place in ne-worker -- the production call -- and in no worker test
    at all. So the reviewer's own mutation still survived: guard either heartbeat
    behind ``if self._grounding is not None:`` and every assertion in this file
    stayed green while ungated production runs emitted no history span.

    This drives the branch. It is the strong form rather than a presence check:
    the fake store appends its OWN marker to the same recorder, so the assertion
    is that the pair BRACKETS the materialization that actually ran -- ordering a
    string test cannot see, on the arm a no-continuity context cannot reach.
    """

    stages: list[str] = []

    class _RecordingBoundary:
        async def heartbeat(self, stage: str) -> None:
            stages.append(stage)

        async def checkpoint(self, *, stage: str, safe_detail: Any) -> None:
            return None

        async def await_inflight(self, operation: Any, *, stage: str) -> Any:
            return await operation

        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _Store:
        async def record_skill_activation(self, **values: Any) -> None:
            return None

        async def materialize_conversation_history(self, **kwargs: Any) -> list[Any]:
            # The work the pair claims to bracket, recorded on the SAME timeline.
            assert kwargs["run_id"] == "run-1"
            assert kwargs["owner_principal_id"] == "owner-1"
            stages.append("materialize_conversation_history")
            return []

    class _Selection:
        def context_budget(self, model: str) -> int:
            assert model == "model-a"
            return 8192

    @dataclass(frozen=True)
    class _Adapter:
        instructions: str = ""

        async def run(self, **kwargs: Any) -> Any:
            return ModelPlannedRun(outcome="complete", output="grounded")

    class _Settings(BaseModel):
        task: str | None = None
        context_budget: int | None = None

    adapter = ProfileBoundRunExecutionAdapter(
        bindings={},
        principal_resolver=_Resolver(Principal(id="owner-1")),
        deterministic_executor=cast(Any, None),
        runtime_settings=_Settings(),
        sink=object(),
        catalog=_Catalog(
            _definition(
                instructions="Follow the pinned procedure.",
                model_policy_id="mp-hardship",
            )
        ),
        agent_skills=cast(Any, _Skills((_SKILL_PIN,))),
        run_store=cast(Any, _Store()),
        effective_tool_grants={"agt_01k3hardshipreview0000000000": ("read.skills.view",)},
        scope_executor=lambda executor, **_: executor,
        model_selection=cast(Any, _Selection()),
    )

    result = asyncio.run(
        adapter._execute_as_principal(
            _context(
                strategy="agentic",
                boundary=_RecordingBoundary(),
                agent_definition_hash="a" * 64,
                model_policy_id="mp-hardship",
                authorized_model="model-a",
            ),
            _agentic_binding(_Adapter()),
            Principal(id="owner-1"),
        )
    )

    assert result.outcome == "execution_complete"
    # The branch really ran...
    assert "materialize_conversation_history" in stages, (
        f"continuity never activated, so this test proves nothing; observed: {stages}"
    )
    # ...and the pair BRACKETS it. Either heartbeat moved out of the branch, or
    # guarded behind an unrelated condition, reds this.
    assert "profile:history-before" in stages, f"history-before never fired; got {stages}"
    assert "profile:history-after" in stages, f"history-after never fired; got {stages}"
    assert (
        stages.index("profile:history-before")
        < stages.index("materialize_conversation_history")
        < stages.index("profile:history-after")
    ), f"the history pair does not bracket the materialization; observed: {stages}"
    # ⛔ AND THE OTHER FOUR MARKERS, ON THIS COMPOSITION, because an adversarial
    # pass found that NOTHING in the tree asserted them here. `model_policy_id`
    # is set in exactly one test of this file -- this one -- and it asserted only
    # the history pair. So guarding `skills-before`, `skills-after` or
    # `adapter-before` behind `if not continuity_active:` left the WHOLE suite
    # green while the production path emitted none of them: the non-continuity
    # test above and the GROUNDED test below both run with `continuity_active`
    # FALSE, and the source test pins text offsets an added `if` does not move.
    #
    # That is the identical hole this PR escalated to P1 twice already. The
    # remedy was applied to the grounded composition and NOT to this one, which
    # is the more important of the two: the record's own production floor of 12
    # rows is the CONTINUITY floor.
    assert "profile:skills-before" in stages, f"continuity run lost skills-before; got {stages}"
    assert "profile:skills-after" in stages, f"continuity run lost skills-after; got {stages}"
    assert "profile:adapter-before" in stages, f"continuity run lost adapter-before; got {stages}"
    assert stages.index("profile:skills-before") < stages.index("profile:skills-after")
    assert stages.index("profile:history-after") < stages.index("profile:adapter-before"), (
        f"adapter-before must follow the history pair on a continuity run; got {stages}"
    )
    # And the pair sits BEFORE the measured edge opens, which is the retraction
    # this increment already had to publish once: these markers do not split the
    # 7.67 s gap.
    #
    # ⛔ THIS COMMENT USED TO FINISH "`profile:adapter-before` does" — a claim the
    # SAME COMMIT retracts in three other places, because that marker partitions
    # the edge into ~0 and ~all of it. Sixth instance of the class this PR has
    # now flagged five times, and caught here by an adversarial pass rather than
    # by me. The marker that divides unmeasured work is `profile:agent-built`, in
    # the adapter.
    assert stages.index("profile:history-after") < stages.index("profile:agentic-before")


def test_the_profile_markers_fire_on_a_GROUNDED_run_too() -> None:
    """THE OTHER COMPOSITION, and the inverse mutation that was still surviving.

    An adversarial pass made the sharper version of its own earlier point: the two
    recording-boundary tests both drive an UNGROUNDED adapter, so at the emission
    sites ``gate_policy`` and ``recorder`` are ``None`` on every path they reach.
    The mutation they advertise -- ``if self._grounding is not None:`` -- reds them.
    Its INVERSE, ``if self._grounding is None:``, passed every assertion in this
    file while a GROUNDED run emitted nothing. A remedy that asserts what fired is
    only as good as the compositions it fires under.

    ⚠️ THIS DOCSTRING USED TO SAY "the grounded configuration is the hardship
    agent's own, the run whose 7.67 s gap is being measured". THAT IS FALSE and an
    adversarial pass caught it: ``NE_ONTOLOGY_GROUNDED_RUN_AGENT_IDS`` names
    ``agt_01k3groundedanalyst000000000`` in every shipped registry, a DIFFERENT
    agent from ``agt_01k3hardshipreview0000000000``. The sentence conflated the
    two, and the conflation had a consequence rather than being cosmetic: it made
    "grounded" and "the measured run" sound like one composition, so nothing
    covered GROUNDED AND CONTINUITY TOGETHER -- the shape every production
    grounded run takes, since the grounded agent carries a ``model_policy_id``.
    This test now composes both, which is the whole point of the pair of fixes
    round 17 and round 18 make: assert under the composition PRODUCTION takes.

    So this drives the same path with a real ``GroundingPolicy`` composed and the
    Agent id gated. The markers are asserted BEFORE the run's outcome, because
    they must fire whatever the gate decides: a refused draft is still a run that
    activated skills and entered its adapter.
    """

    stages: list[str] = []

    class _RecordingBoundary:
        async def heartbeat(self, stage: str) -> None:
            stages.append(stage)

        async def checkpoint(self, *, stage: str, safe_detail: Any) -> None:
            return None

        async def await_inflight(self, operation: Any, *, stage: str) -> Any:
            return await operation

        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _Store:
        async def record_skill_activation(self, **values: Any) -> None:
            return None

        async def materialize_conversation_history(self, **kwargs: Any) -> list[Any]:
            # Present because this test now runs the CONTINUITY branch too. Without
            # it the branch raised, `contextlib.suppress` swallowed it, and the run
            # stopped at `profile:history-before` -- a test that would have looked
            # green on the assertions above while covering less than before.
            stages.append("materialize_conversation_history")
            return []

    @dataclass(frozen=True)
    class _Adapter:
        instructions: str = ""

        async def run(self, **kwargs: Any) -> Any:
            return ModelPlannedRun(outcome="complete", output="grounded")

    class _Settings(BaseModel):
        task: str | None = None

    class _Selection:
        # Needed because this test now composes CONTINUITY as well as grounding;
        # `continuity_active` requires a model selection, and production grounded
        # runs carry a `model_policy_id`.
        def context_budget(self, model: str) -> int:
            assert model == "model-a"
            return 8192

    agent_id = "agt_01k3hardshipreview0000000000"
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={},
        principal_resolver=_Resolver(Principal(id="owner-1")),
        deterministic_executor=cast(Any, None),
        runtime_settings=_Settings(),
        sink=object(),
        catalog=_Catalog(
            _definition(
                instructions="Follow the pinned procedure.",
                model_policy_id="mp-hardship",
            )
        ),
        agent_skills=cast(Any, _Skills((_SKILL_PIN,))),
        run_store=cast(Any, _Store()),
        effective_tool_grants={agent_id: ("read.skills.view",)},
        scope_executor=lambda executor, **_: executor,
        grounding=_gate_policy(_GateEngine(), _GateSink()),
        grounded_run_agent_ids=(agent_id,),
        model_selection=cast(Any, _Selection()),
    )

    # The gate may well refuse this draft -- that is not what is under test, and
    # asserting an outcome here would make this test about the gate instead of
    # about the markers. What matters is that the boundaries fired at all.
    with contextlib.suppress(Exception):  # the gate's verdict is deliberately not asserted
        asyncio.run(
            adapter._execute_as_principal(
                _context(
                    strategy="agentic",
                    boundary=_RecordingBoundary(),
                    agent_definition_hash="a" * 64,
                    model_policy_id="mp-hardship",
                    authorized_model="model-a",
                ),
                _agentic_binding(_Adapter()),
                Principal(id="owner-1"),
            )
        )

    assert "profile:skills-before" in stages, f"grounded run emitted no skills pair; got {stages}"
    assert "profile:skills-after" in stages, f"grounded run emitted no skills pair; got {stages}"
    assert "profile:agentic-before" in stages, f"got {stages}"
    assert "profile:adapter-before" in stages, (
        f"grounded run never emitted adapter-before; got {stages}"
    )
    assert stages.index("profile:agentic-before") < stages.index("profile:adapter-before")
    # ⛔ AND THE TWO MARKERS CONTINUITY ACTUALLY OWNS. Round 18 composed
    # grounded and continuity together and then asserted only the four markers
    # that fire REGARDLESS of continuity -- so guarding the history pair with
    # `if self._grounding is None:` inside `if continuity_active:` left the whole
    # suite green while the grounded production composition emitted no history
    # span at all: the continuity test composes `grounding=None` so the guard is
    # true there, this test never mentioned the pair, and both source tests are
    # positional and unmoved by a nested `if`.
    #
    # That is the round-17 P1 again, inside the composition round 18 added
    # SPECIFICALLY to close it. Composing a path and asserting nothing that path
    # controls is not coverage.
    assert "profile:history-before" in stages, f"grounded continuity run lost it; got {stages}"
    assert "profile:history-after" in stages, f"grounded continuity run lost it; got {stages}"
    assert (
        stages.index("profile:history-before")
        < stages.index("materialize_conversation_history")
        < stages.index("profile:history-after")
    ), f"the history pair must bracket the materialization here too; got {stages}"


def test_a_zero_pin_agent_runs_end_to_end_and_records_an_empty_activation() -> None:
    """THE LIVE REGRESSION. An agentic Agent with ``skills = []`` must RUN.

    Train-44's read-only Meridian Analyst declares no Skill pins (owner-approved:
    the draft pin was removed so it cannot email), and every run died in under a
    second with a bare ``ValueError`` — the worker records the Skill activation
    unconditionally and the store refused an empty pin tuple. The unit fakes on
    both sides hid the seam: the worker tests stubbed the store, the store tests
    never drove the worker. This test crosses it — the REAL ``AgentRunStore``
    behind the real ``_execute_as_principal`` — so the floor cannot silently
    come back.

    The empty receipt is recorded as a positive fact ("this attempt activated
    exactly no Skills"), not skipped: absence of the row keeps one meaning — the
    attempt died before activation. Exactness for Agents that DO pin Skills is
    untouched; ``test_a_run_whose_pinned_skill_is_not_served_publishes_why_not_a_
    bare_class`` above still proves the declared-but-unserved pin hard-fails.

    SECOND-ORDER REGRESSION, same seam (train-46): with the activation floor
    fixed, the run then died at ``BoundedAgenticAdapter.__post_init__`` —
    "bounded agentic instructions must not be empty" — because the Analyst's
    instructions had only ever been the rendered Skill index, and
    ``render_index`` renders zero pins as ``""``. The first version of THIS test
    missed it by faking the adapter with a guardless dataclass, so the
    ``replace(...)`` in the worker never ran the real guard. The adapter below is
    therefore the REAL ``BoundedAgenticAdapter`` (only ``run`` is overridden):
    ``dataclasses.replace`` re-runs the real ``__post_init__`` against the
    composed instructions, and the test asserts the composition — the Agent's
    own manifest instructions, with no index to append — reached the adapter
    non-empty.
    """
    import sqlalchemy as sa

    from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
    from ne.runtime.run_store import AgentRunStore, ensure_run_schema
    from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    cipher = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
    )
    sessions = SqlAlchemySessionStore(engine)
    run_store = AgentRunStore(engine, cipher)

    empty_index_text = ""
    empty_index_hash = hashlib.sha256(empty_index_text.encode("utf-8")).hexdigest()

    class _NoSkills:
        """The real surface's answer for a zero-pin Agent: nothing, honestly.

        ``ne.skills.active_set.render_index`` renders NO PACKS as the empty
        string by design (never a dangling header), so the activation carries
        zero pins and an empty index — mirrored here with a real hash.
        """

        async def activate(self, definition: Any, principal: Principal, *, surface_key: str) -> Any:
            return SimpleNamespace(
                pins=(),
                index_hash=empty_index_hash,
                surface_hash=hashlib.sha256(surface_key.encode("utf-8")).hexdigest(),
                index_text=empty_index_text,
                pin_map=lambda: {},
            )

    manifest_instructions = (
        "You are a read-only governed analyst. Answer strictly from the "
        "governed evidence served this run."
    )
    seen_instructions: list[str] = []

    class _RecordingBoundedAdapter(BoundedAgenticAdapter):
        """The REAL adapter — frozen caps, real non-empty-instructions guard.

        Only ``run`` is overridden, so no model or gateway is needed while the
        worker's ``replace(adapter, instructions=...)`` still constructs a new
        instance of this class and re-runs the production ``__post_init__``.
        """

        async def run(self, **kwargs: Any) -> Any:
            seen_instructions.append(self.instructions)
            return ModelPlannedRun(
                outcome="complete",
                output="Q2 delinquency is trending down in the Meridian book.",
                served_row_ids=(),
                claims=(),
            )

    class _Settings2(BaseModel):
        task: str | None = None

    async def scenario() -> None:
        conversation = await sessions.create_conversation("Meridian Analyst", "ops", "owner-1")
        run = await run_store.create_run(
            conversation_id=conversation.conversation_id,
            owner_principal_id="owner-1",
            department="ops",
            conversation_agent_name="Meridian Analyst",
            agent_id="agt_01k3hardshipreview0000000000",
            agent_version=1,
            strategy="agentic",
            config_hash="a" * 64,
            capability_snapshot_hash="b" * 64,
            capability_snapshot_receipt_id="cap-receipt-1",
            input_plaintext=b"How is Q2 delinquency trending?",
        )
        attempt = await run_store.link_attempt(run_id=run.run_id)

        adapter = ProfileBoundRunExecutionAdapter(
            bindings={},
            principal_resolver=_Resolver(Principal(id="owner-1")),
            deterministic_executor=cast(Any, None),
            runtime_settings=_Settings2(),
            sink=object(),
            catalog=_Catalog(_definition(skills=(), instructions=manifest_instructions)),
            agent_skills=cast(Any, _NoSkills()),
            run_store=cast(Any, run_store),
            effective_tool_grants={"agt_01k3hardshipreview0000000000": ()},
            scope_executor=lambda executor, **_: executor,
        )
        context = RunExecutionContext(
            run=run,
            attempt_id=attempt.attempt_id,
            target=RunTurnTarget(
                conversation_id=conversation.conversation_id,
                run_id=run.run_id,
                turn_id="turn-1",
                turn_sequence=1,
                attachment_id="attachment-1",
                attempt_id=attempt.attempt_id,
            ),
            input_bytes=b"How is Q2 delinquency trending?",
            gateway=cast(Any, object()),
            executor=cast(Any, object()),
            audit_sink=cast(Any, object()),
            recovered_uncertain_model_attempt=False,
        )

        result = await adapter._execute_as_principal(
            context,
            _agentic_binding(
                _RecordingBoundedAdapter(
                    # The API-side boot sentinel, exactly as `platform` composes
                    # it: unusable as guidance, replaced by the worker.
                    instructions="Run Skill activation is required before execution.",
                    max_tool_calls=1,
                    total_tokens_limit=1000,
                    wall_clock_seconds=10.0,
                    max_parked_writes=0,
                )
            ),
            Principal(id="owner-1"),
        )

        # The run COMPLETES — before the fixes it died here twice over, before
        # any model call, on every lane: first a bare ValueError from the
        # activation store, then "bounded agentic instructions must not be
        # empty" from the real adapter's own guard.
        assert result.outcome == "execution_complete"
        assert result.output_bytes is not None

        # The composed instructions the model would receive are the Agent's own
        # manifest instructions — non-empty with zero pins and an empty index.
        assert seen_instructions == [manifest_instructions]

        # The audit trail is positive, not absent: the real store holds one
        # activation receipt for the attempt, pinning exactly no Skills.
        receipt = await run_store.load_skill_activation(
            run_id=run.run_id,
            attempt_id=attempt.attempt_id,
            owner_principal_id="owner-1",
        )
        assert receipt is not None
        assert receipt.pins == ()
        assert receipt.index_hash == empty_index_hash
        assert receipt.surface_key == f"run:{run.run_id}:attempt:{attempt.attempt_id}:skills"

    asyncio.run(scenario())
    engine.dispose()


def test_a_zero_pin_agent_without_instructions_is_refused_with_a_governed_reason() -> None:
    """Defense in depth below the manifest guard: a reason, never a crash.

    The manifest already refuses to LOAD an agentic Agent with zero Skill pins
    and no instructions (``AgentManifestDefinition._strategy_contract``), so in
    production this state should be unreachable. If it is reached anyway — an
    older catalog row, a hand-built definition — the worker must refuse the run
    as a governed pre-flight decision that publishes WHY, not let the adapter's
    ``__post_init__`` kill the attempt with a bare ``ValueError`` after
    admission (the train-46 live failure mode).
    """

    class _NoSkills:
        async def activate(self, definition: Any, principal: Principal, *, surface_key: str) -> Any:
            return SimpleNamespace(
                pins=(),
                index_hash=hashlib.sha256(b"").hexdigest(),
                surface_hash="d" * 64,
                index_text="",
                pin_map=lambda: {},
            )

    class _RecordingStore:
        async def record_skill_activation(self, **kwargs: Any) -> None:
            return None

    adapter = ProfileBoundRunExecutionAdapter(
        bindings={},
        principal_resolver=_Resolver(Principal(id="owner-1")),
        deterministic_executor=cast(Any, None),
        runtime_settings=object(),
        sink=object(),
        catalog=_Catalog(_definition(skills=(), instructions=None)),
        agent_skills=cast(Any, _NoSkills()),
        run_store=cast(Any, _RecordingStore()),
        effective_tool_grants={"agt_01k3hardshipreview0000000000": ()},
        scope_executor=lambda executor, **_: executor,
    )

    with pytest.raises(RunActivationError) as raised:
        asyncio.run(
            adapter._execute_as_principal(
                _context(strategy="agentic"),
                _agentic_binding(SimpleNamespace()),
                Principal(id="owner-1"),
            )
        )

    error = raised.value
    assert not isinstance(error, RunSkillActivationError)
    assert "declares no instructions" in error.safe_reason
    assert "empty system prompt" in error.safe_reason
    assert error.safe_reason.startswith("The run could not start:")


# --- NE-GA-RUN1: the whole-answer grounding gate on the canonical Run path ---
#
# The REAL gate runs end-to-end (`ne.runtime.grounding.run_grounding_gate` over
# the real `ne.govern.grounding.check_grounding`); only the INJECTED seams the
# chat composition also injects (render_fallback / guardrail engine / sink /
# pdp) are faked, mirroring `ne-runtime/tests/test_grounding_runtime.py`.


class _GateEngine:
    """GuardrailEnforcerLike fake: records payloads, raises GuardrailViolation
    on a genuine grounded_gate/grounded=False payload (the G2 semantics the
    gate suppresses after the block is audited)."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def enforce(self, payload: Any) -> object:
        self.payloads.append(dict(payload))
        if payload.get("grounded_gate") is True and payload.get("grounded") is False:
            from ne.govern.errors import GuardrailViolation

            raise GuardrailViolation(())
        return None


class _GateSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)


def _gate_policy(
    engine: _GateEngine,
    sink: _GateSink,
    *,
    assist: Any = None,
    deployed_posture: bool = False,
) -> Any:
    """The chat-composed policy. ``deployed_posture=True`` builds it exactly as
    the live demo composition does (NE_ONTOLOGY_GROUNDING_STRICTNESS=standard +
    NE_ONTOLOGY_GROUNDING_GATE_PROFILE=ng2, live.py) instead of the STRICT+ng1
    code defaults — the posture the U11 review found untested."""
    from ne.govern.grounding_policy import PROFILE_NG2, StrictnessTier
    from ne.runtime.grounding import GroundingPolicy

    extra: dict[str, Any] = (
        {"strictness": StrictnessTier.STANDARD, "profile": PROFILE_NG2} if deployed_posture else {}
    )
    return GroundingPolicy(
        granted_tools=("read.kg.reg_e_provisional_credits",),
        render_fallback=lambda answers: "VERIFIED-FALLBACK-TABLE",
        guardrail_engine=cast(Any, engine),
        sink=cast(Any, sink),
        pdp=cast(Any, object()),
        assist=assist,
        **extra,
    )


def _t3_verified_answer() -> dict[str, Any]:
    """A VerifiedAnswer-shaped mapping over the SAME table `_t3_observation`
    renders, so the whole-answer corpus and the per-claim evidence agree."""
    return {
        "template_id": "reg_e_provisional_credits",
        "template_version": 1,
        "config_hash": "deadbeef",
        "args": {},
        "columns": list(_T3_SET_COLUMNS),
        "rows": [list(row) for row in _T3_SET_ROWS],
        "citations": ["urn:ne:row:credit-1"],
        "truncated": False,
    }


_GATED_AGENT = "agt_01k3hardshipreview0000000000"


@pytest.fixture
def frozen_gate_config_hash() -> Any:
    """Freeze the process config hash for gated-Run tests that emit REAL audit
    events (``grounding.pass``/``grounding.advisory`` construct
    ``new_audit_event``, which resolves the frozen hash). The conftest autouse
    ``_reset_state`` fixture resets it after every test."""
    from ne.contracts import freeze_config_hash
    from ne.runtime.config import RuntimeSettings as _RuntimeSettings

    return freeze_config_hash(_RuntimeSettings())


def _gated_adapter(
    run_store: Any,
    model_run: Any,
    *,
    engine: _GateEngine,
    sink: _GateSink,
    grounded_ids: tuple[str, ...] = (_GATED_AGENT,),
    record_answer: bool = True,
    seen_executors: list[Any] | None = None,
    deployed_posture: bool = False,
    adapter_kwargs: dict[str, Any] | None = None,
) -> tuple[ProfileBoundRunExecutionAdapter, Any]:

    @dataclass(frozen=True)
    class _Agentic:
        instructions: str = ""

        async def run(self, **kwargs: Any) -> Any:
            if seen_executors is not None:
                seen_executors.append(kwargs["executor"])
            if record_answer:
                # Stands in for the verified-read handler's §4.2 direct record
                # (`current_answer_recorder()` resolves to this recorder inside
                # a governed execute window; the fake records directly).
                kwargs["executor"].record_answer(_t3_verified_answer())
            return model_run

    class _Settings(BaseModel):
        task: str | None = None

    return ProfileBoundRunExecutionAdapter(
        bindings={},
        principal_resolver=_Resolver(Principal(id="owner-1")),
        deterministic_executor=cast(Any, None),
        runtime_settings=_Settings(),
        sink=object(),
        catalog=_Catalog(_definition(instructions="Answer from the served records only.")),
        agent_skills=cast(Any, _Skills((_SKILL_PIN,))),
        run_store=cast(Any, run_store),
        effective_tool_grants={_GATED_AGENT: ("read.kg.reg_e_provisional_credits",)},
        scope_executor=lambda executor, **_: executor,
        grounding=_gate_policy(engine, sink, deployed_posture=deployed_posture),
        grounded_run_agent_ids=grounded_ids,
        **(adapter_kwargs or {}),
    ), _Agentic


def test_gated_run_ids_without_a_composed_policy_fail_loud_at_construction() -> None:
    with pytest.raises(ValueError, match="never execute ungated"):
        ProfileBoundRunExecutionAdapter(
            bindings={},
            principal_resolver=_Resolver(Principal(id="owner-1")),
            deterministic_executor=cast(Any, None),
            runtime_settings=object(),
            sink=object(),
            grounded_run_agent_ids=(_GATED_AGENT,),
        )


def test_the_run_path_strips_the_g4_assist_structurally() -> None:
    """A post-verdict re-draft would invalidate the server-bound claim spans of
    the exact bytes it rewrites, so the Run path's policy never carries one."""

    async def _assist(**kwargs: Any) -> str:
        raise AssertionError("assist must never be reachable on the Run path")

    engine, sink = _GateEngine(), _GateSink()
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={},
        principal_resolver=_Resolver(Principal(id="owner-1")),
        deterministic_executor=cast(Any, None),
        runtime_settings=object(),
        sink=object(),
        grounding=_gate_policy(engine, sink, assist=_assist),
        grounded_run_agent_ids=(_GATED_AGENT,),
    )
    assert adapter._grounding is not None
    assert adapter._grounding.assist is None


def test_an_ungated_agent_run_never_sees_a_grounding_recorder() -> None:
    """Byte-parity for every agent NOT in grounded_run_agent_ids: the executor
    reaches the runner unwrapped and the terminal is untouched."""
    from ne.runtime.grounding import GroundingRecorder

    engine, sink = _GateEngine(), _GateSink()
    seen: list[Any] = []
    model_run = ModelPlannedRun(outcome="complete", output="A plain governed answer.")
    adapter, agentic = _gated_adapter(
        SimpleNamespace(record_skill_activation=_async_none),
        model_run,
        engine=engine,
        sink=sink,
        grounded_ids=("agt_someoneelse000000000000000",),
        record_answer=False,
        seen_executors=seen,
    )
    result = asyncio.run(
        adapter._execute_as_principal(
            _context(strategy="agentic"),
            _agentic_binding(agentic()),
            Principal(id="owner-1"),
        )
    )
    assert len(seen) == 1 and not isinstance(seen[0], GroundingRecorder)
    assert result.outcome == "execution_complete"
    assert result.output_bytes == b"A plain governed answer."
    assert result.structured_envelope is not None
    assert result.structured_envelope.disposition == "completed"
    assert engine.payloads == [] and sink.events == []


async def _async_none(**kwargs: Any) -> None:
    return None


@pytest.mark.usefixtures("frozen_gate_config_hash")
def test_a_gated_run_that_grounds_its_answer_is_served_unchanged_with_its_claims() -> None:
    """PASS is ADDITIVE: the gate certifies the whole answer, then the existing
    per-claim verifier still re-slices and receipts every server-bound claim."""
    from ne.runtime.grounding import GroundingRecorder

    observation = _t3_observation()
    answer = "Meridian Bank 412.55 posted and Northwind Trust 88.20 pending"
    raw = answer.encode()

    class _Store:
        def __init__(self) -> None:
            self.loads: list[str] = []

        async def record_skill_activation(self, **values: Any) -> None:
            return None

        async def load_run_claim_evidence(self, **kwargs: Any) -> Any:
            self.loads.append(kwargs["protected_ref"])
            return observation

        async def claim_run_claim_verifier(self, **kwargs: Any) -> ClaimVerifierRecord:
            key = kwargs["claim_key"]
            return ClaimVerifierRecord(
                verifier_receipt_id=f"verifier-{key}",
                event_id=f"verifier-event-{key}",
                receipt_hash=hashlib.sha256(key.encode()).hexdigest(),
            )

    store = _Store()
    engine, sink = _GateEngine(), _GateSink()
    seen: list[Any] = []
    model_run = ModelPlannedRun(
        outcome="complete",
        output=answer,
        served_row_ids=("ne-source-ref:v1:verified-set",),
        claims=(
            ModelPlannedClaim(
                claim_key="claim-total",
                span_start_byte=0,
                span_end_byte=len(raw),
                claim_content_hash=hashlib.sha256(raw).hexdigest(),
                served_ref="ne-source-ref:v1:verified-set",
            ),
        ),
    )
    adapter, agentic = _gated_adapter(
        store, model_run, engine=engine, sink=sink, seen_executors=seen
    )
    result = asyncio.run(
        adapter._execute_as_principal(
            _context(strategy="agentic", input_bytes=b"Which provisional credits are open?"),
            _agentic_binding(agentic()),
            Principal(id="owner-1"),
        )
    )
    # The runner received the RECORDER (wrapping the scoped executor)...
    assert len(seen) == 1 and isinstance(seen[0], GroundingRecorder)
    # ...the gate PASSED and the terminal is byte-identical to the draft...
    assert result.outcome == "execution_complete"
    assert result.output_bytes == raw
    assert result.structured_envelope is not None
    assert result.structured_envelope.disposition == "completed"
    # ...and the per-claim verifier lane is UNTOUCHED (additive, never replaced).
    assert store.loads == ["ne-source-ref:v1:verified-set"]
    assert len(result.evidence_bindings) == 1
    assert len(result.verifier_bindings) == 1
    # No block was emitted on a PASS.
    assert engine.payloads == []


def test_a_gated_run_whose_draft_cannot_be_proven_is_withheld_as_a_governed_refusal() -> None:
    """THE DEMO BEAT. A fabricated draft over genuinely served evidence is
    withheld: one audited guardrail.block, a refused terminal the console can
    read, the draft's claims discarded with the draft, and not one draft byte
    (nor the fallback table) in what is served."""

    class _Store:
        def __init__(self) -> None:
            self.loads: list[str] = []

        async def record_skill_activation(self, **values: Any) -> None:
            return None

        async def load_run_claim_evidence(self, **kwargs: Any) -> Any:
            self.loads.append(kwargs["protected_ref"])
            raise AssertionError("a withheld draft's claims must never be verified")

    draft = "Initech Global has 73 open provisional credits."
    raw = draft.encode()
    store = _Store()
    engine, sink = _GateEngine(), _GateSink()
    model_run = ModelPlannedRun(
        outcome="complete",
        output=draft,
        served_row_ids=("ne-source-ref:v1:verified-set",),
        claims=(
            ModelPlannedClaim(
                claim_key="claim-fabricated",
                span_start_byte=0,
                span_end_byte=len(raw),
                claim_content_hash=hashlib.sha256(raw).hexdigest(),
                served_ref="ne-source-ref:v1:verified-set",
            ),
        ),
    )
    adapter, agentic = _gated_adapter(store, model_run, engine=engine, sink=sink)
    result = asyncio.run(
        adapter._execute_as_principal(
            _context(strategy="agentic", input_bytes=b"Which provisional credits are open?"),
            _agentic_binding(agentic()),
            Principal(id="owner-1"),
        )
    )
    # A withheld run COMPLETED — it is a decision, not a failure...
    assert result.outcome == "execution_complete"
    assert result.structured_envelope is not None
    # ...on the finalizer's refused lane, with the draft's claims discarded.
    assert result.structured_envelope.disposition == "refused"
    assert result.structured_envelope.claims == ()
    assert result.evidence_bindings == () and result.verifier_bindings == ()
    assert store.loads == []
    # Exactly ONE audited block, carrying classes + counts only.
    assert len(engine.payloads) == 1
    payload = engine.payloads[0]
    assert payload["grounded_gate"] is True and payload["grounded"] is False
    assert "numeral-ungrounded" in payload["reasons"]
    # The served notice names the decision and its classes — and carries no
    # draft fragment and no fallback table.
    assert result.output_bytes is not None
    notice = result.output_bytes.decode()
    assert "withheld by the grounding gate" in notice
    assert "numeral-ungrounded" in notice
    assert "Initech" not in notice and "73" not in notice
    assert "VERIFIED-FALLBACK-TABLE" not in notice


def test_a_gated_run_that_fetched_nothing_is_withheld_even_for_a_plausible_answer() -> None:
    """An empty corpus grounds nothing: a turn that never fetched always falls
    back — the model cannot answer a gated Run from its own weights."""
    engine, sink = _GateEngine(), _GateSink()
    model_run = ModelPlannedRun(outcome="complete", output="Meridian Bank has 412.55 posted.")
    adapter, agentic = _gated_adapter(
        SimpleNamespace(record_skill_activation=_async_none),
        model_run,
        engine=engine,
        sink=sink,
        record_answer=False,
    )
    result = asyncio.run(
        adapter._execute_as_principal(
            _context(strategy="agentic", input_bytes=b"Which provisional credits are open?"),
            _agentic_binding(agentic()),
            Principal(id="owner-1"),
        )
    )
    assert result.structured_envelope is not None
    assert result.structured_envelope.disposition == "refused"
    assert len(engine.payloads) == 1


# --- NE-GA-RUN4 (U11 fix F1): the Run gate's tier is its OWN deployment knob --
#
# The gate previously inherited the ONE shared chat policy's tier, so the
# deployed posture (NE_ONTOLOGY_GROUNDING_STRICTNESS=standard + ng2, live.py)
# silently ran the gated Run agent at STANDARD — where a digit-free draft over
# an EMPTY corpus completes as a served "grounded" answer with only an advisory
# vocab-ungrounded finding, falsifying the "empty-corpus turns always withhold"
# record. These tests pin the DEPLOYED posture, not the code defaults.


def test_the_run_gate_defaults_to_strict_and_never_inherits_a_standard_chat_tier() -> None:
    from ne.govern.grounding_policy import PROFILE_NG2, StrictnessTier

    engine, sink = _GateEngine(), _GateSink()
    adapter, _agentic = _gated_adapter(
        SimpleNamespace(record_skill_activation=_async_none),
        ModelPlannedRun(outcome="complete", output="unused"),
        engine=engine,
        sink=sink,
        deployed_posture=True,
    )
    assert adapter._grounding is not None
    # The chat policy arrived STANDARD; the Run path re-tiers it to STRICT.
    assert adapter._grounding.strictness is StrictnessTier.STRICT
    # The PROFILE is untouched: the knob moves the tier, never the rulebook.
    assert adapter._grounding.profile is PROFILE_NG2


def test_a_digit_free_empty_corpus_draft_is_withheld_at_the_deployed_posture() -> None:
    """THE U11-F1 REPRODUCTION. Zero fetches, a digit-free qualitative draft,
    the deployed chat posture (STANDARD + ng2): before the Run-path knob this
    completed as a served grounded answer (vocab-ungrounded was advisory);
    the Run gate at STRICT withholds it."""
    engine, sink = _GateEngine(), _GateSink()
    model_run = ModelPlannedRun(
        outcome="complete",
        output=(
            "The Initech Global dispute remains open and unresolved, "
            "and the customer is still waiting."
        ),
    )
    adapter, agentic = _gated_adapter(
        SimpleNamespace(record_skill_activation=_async_none),
        model_run,
        engine=engine,
        sink=sink,
        record_answer=False,
        deployed_posture=True,
    )
    result = asyncio.run(
        adapter._execute_as_principal(
            _context(strategy="agentic", input_bytes=b"What is the Initech dispute status?"),
            _agentic_binding(agentic()),
            Principal(id="owner-1"),
        )
    )
    assert result.structured_envelope is not None
    assert result.structured_envelope.disposition == "refused"
    assert len(engine.payloads) == 1
    assert "vocab-ungrounded" in engine.payloads[0]["reasons"]
    assert engine.payloads[0]["strictness"] == "strict"
    notice = (result.output_bytes or b"").decode()
    assert "withheld by the grounding gate" in notice
    assert "Initech" not in notice


@pytest.mark.usefixtures("frozen_gate_config_hash")
def test_the_run_knob_set_to_standard_serves_the_digit_free_draft_advisory_only() -> None:
    """The knob is a real deployment choice: at STANDARD a digit-free draft
    whose extra vocabulary is only advisory is SERVED — the exact trade the
    registry description must not overclaim. The corpus is NON-empty: with
    zero fetches the empty-evidence invariant would withhold at EVERY tier,
    so the knob is only ever a vocabulary-enforcement choice, never an
    evidence waiver."""
    from ne.govern.grounding_policy import StrictnessTier

    engine, sink = _GateEngine(), _GateSink()
    draft = (
        "The Initech Global dispute remains open and unresolved, and the customer is still waiting."
    )
    adapter, agentic = _gated_adapter(
        SimpleNamespace(record_skill_activation=_async_none),
        ModelPlannedRun(outcome="complete", output=draft),
        engine=engine,
        sink=sink,
        record_answer=True,
        deployed_posture=True,
        adapter_kwargs={"grounding_run_strictness": StrictnessTier.STANDARD},
    )
    result = asyncio.run(
        adapter._execute_as_principal(
            _context(strategy="agentic", input_bytes=b"What is the Initech dispute status?"),
            _agentic_binding(agentic()),
            Principal(id="owner-1"),
        )
    )
    assert result.structured_envelope is not None
    assert result.structured_envelope.disposition == "completed"
    assert result.output_bytes == draft.encode()
    assert engine.payloads == []
    # Advisory is RECORDED, never dropped (grounding.advisory), and the
    # pass-side certification event still fires (U11 fix F4).
    actions = [event.action for event in sink.events]
    assert "grounding.advisory" in actions


# --- NE-GA-RUN5 (U11 review round 2, P1): the non-empty-evidence invariant ---
#
# STRICT does not make "empty-corpus turns always withhold" true: the tier only
# moves which finding CLASSES enforce, and a draft composed entirely of
# core-allowlisted words produces ZERO findings — nothing to enforce at any
# tier. Reviewer's reproduction: "The record is verified." over an empty
# corpus -> findings NONE, withhold False, at STRICT. The guarantee is
# therefore enforced as its own invariant on the Run path: a gated Run that
# fetched nothing withholds regardless of the gate's token analysis.


@pytest.mark.usefixtures("frozen_gate_config_hash")
def test_an_allowlist_only_zero_fetch_draft_is_withheld_by_the_empty_evidence_invariant() -> None:
    """THE ROUND-2 REPRODUCTION. Every token of the draft is core-allowlisted,
    so the gate itself finds nothing to enforce at ANY tier — before the
    invariant this served a zero-evidence answer as gated-and-certified."""
    engine, sink = _GateEngine(), _GateSink()
    adapter, agentic = _gated_adapter(
        SimpleNamespace(record_skill_activation=_async_none),
        ModelPlannedRun(outcome="complete", output="The record is verified."),
        engine=engine,
        sink=sink,
        record_answer=False,
    )
    result = asyncio.run(
        adapter._execute_as_principal(
            _context(strategy="agentic", input_bytes=b"Is the record verified?"),
            _agentic_binding(agentic()),
            Principal(id="owner-1"),
        )
    )
    assert result.structured_envelope is not None
    assert result.structured_envelope.disposition == "refused"
    # The GATE passed (zero findings, so no guardrail.block) — the withhold is
    # the invariant's, and it leaves its own audited trace instead of a pass.
    assert engine.payloads == []
    actions = [event.action for event in sink.events]
    assert "grounding.empty_evidence" in actions
    assert "grounding.pass" not in actions
    notice = (result.output_bytes or b"").decode()
    assert "withheld by the grounding gate" in notice
    assert "zero verified record reads" in notice
    assert "The record is verified." not in notice


@pytest.mark.usefixtures("frozen_gate_config_hash")
def test_the_empty_evidence_invariant_is_tier_independent() -> None:
    """The invariant binds at STANDARD exactly as at STRICT — it is an
    evidence rule, not a vocabulary class the knob could ever move."""
    from ne.govern.grounding_policy import StrictnessTier

    engine, sink = _GateEngine(), _GateSink()
    adapter, agentic = _gated_adapter(
        SimpleNamespace(record_skill_activation=_async_none),
        ModelPlannedRun(outcome="complete", output="The record is verified."),
        engine=engine,
        sink=sink,
        record_answer=False,
        deployed_posture=True,
        adapter_kwargs={"grounding_run_strictness": StrictnessTier.STANDARD},
    )
    result = asyncio.run(
        adapter._execute_as_principal(
            _context(strategy="agentic", input_bytes=b"Is the record verified?"),
            _agentic_binding(agentic()),
            Principal(id="owner-1"),
        )
    )
    assert result.structured_envelope is not None
    assert result.structured_envelope.disposition == "refused"
    assert [event.action for event in sink.events if event.action == "grounding.pass"] == []


@pytest.mark.usefixtures("frozen_gate_config_hash")
def test_an_empty_evidence_event_append_failure_fails_closed() -> None:
    """C10: an un-auditable invariant withhold still withholds — by killing the
    attempt, never by serving."""
    from ne.runtime.errors import RuntimeAuditError

    class _FailingSink(_GateSink):
        async def append(self, event: Any) -> None:
            if getattr(event, "action", "") == "grounding.empty_evidence":
                raise OSError("sink down")
            await super().append(event)

    engine, sink = _GateEngine(), _FailingSink()
    adapter, agentic = _gated_adapter(
        SimpleNamespace(record_skill_activation=_async_none),
        ModelPlannedRun(outcome="complete", output="The record is verified."),
        engine=engine,
        sink=sink,
        record_answer=False,
    )
    with pytest.raises(RuntimeAuditError, match=r"grounding\.empty_evidence"):
        asyncio.run(
            adapter._execute_as_principal(
                _context(strategy="agentic", input_bytes=b"Is the record verified?"),
                _agentic_binding(agentic()),
                Principal(id="owner-1"),
            )
        )


# --- U11 fix F4: a PASSING gated Run leaves an audit trace -------------------


@pytest.mark.usefixtures("frozen_gate_config_hash")
def test_a_passing_gated_run_emits_one_grounding_pass_event_with_provenance() -> None:
    """Before this fix a clean PASS emitted NOTHING: the audit trail could not
    distinguish "gated and certified" from "never gated" for a served answer."""
    engine, sink = _GateEngine(), _GateSink()
    model_run = ModelPlannedRun(
        outcome="complete",
        output="Meridian Bank 412.55 posted and Northwind Trust 88.20 pending",
    )
    adapter, agentic = _gated_adapter(
        SimpleNamespace(record_skill_activation=_async_none),
        model_run,
        engine=engine,
        sink=sink,
    )
    result = asyncio.run(
        adapter._execute_as_principal(
            _context(strategy="agentic", input_bytes=b"Which provisional credits are open?"),
            _agentic_binding(agentic()),
            Principal(id="owner-1"),
        )
    )
    assert result.structured_envelope is not None
    assert result.structured_envelope.disposition == "completed"
    assert engine.payloads == []
    passes = [event for event in sink.events if event.action == "grounding.pass"]
    assert len(passes) == 1
    detail = json.loads(passes[0].detail)
    # The FULL gate provenance, so the trail proves the answer was certified.
    assert detail["fallback"] is False
    assert detail["strictness"] == "strict"
    assert detail["assist_used"] is False
    assert [fetch["template_id"] for fetch in detail["fetches"]] == ["reg_e_provisional_credits"]


def test_a_withheld_gated_run_emits_no_grounding_pass_event() -> None:
    engine, sink = _GateEngine(), _GateSink()
    adapter, agentic = _gated_adapter(
        SimpleNamespace(record_skill_activation=_async_none),
        ModelPlannedRun(outcome="complete", output="Meridian Bank has 412.55 posted."),
        engine=engine,
        sink=sink,
        record_answer=False,
    )
    result = asyncio.run(
        adapter._execute_as_principal(
            _context(strategy="agentic", input_bytes=b"Which provisional credits are open?"),
            _agentic_binding(agentic()),
            Principal(id="owner-1"),
        )
    )
    assert result.structured_envelope is not None
    assert result.structured_envelope.disposition == "refused"
    assert [event.action for event in sink.events if event.action == "grounding.pass"] == []


@pytest.mark.usefixtures("frozen_gate_config_hash")
def test_a_grounding_pass_event_append_failure_fails_closed() -> None:
    """C10: an un-auditable certification kills the attempt — the answer is
    never served with its pass-side trace silently missing."""
    from ne.runtime.errors import RuntimeAuditError

    class _FailingSink(_GateSink):
        async def append(self, event: Any) -> None:
            if getattr(event, "action", "") == "grounding.pass":
                raise OSError("sink down")
            await super().append(event)

    engine, sink = _GateEngine(), _FailingSink()
    adapter, agentic = _gated_adapter(
        SimpleNamespace(record_skill_activation=_async_none),
        ModelPlannedRun(
            outcome="complete",
            output="Meridian Bank 412.55 posted and Northwind Trust 88.20 pending",
        ),
        engine=engine,
        sink=sink,
    )
    with pytest.raises(RuntimeAuditError, match=r"grounding\.pass"):
        asyncio.run(
            adapter._execute_as_principal(
                _context(strategy="agentic", input_bytes=b"Which provisional credits are open?"),
                _agentic_binding(agentic()),
                Principal(id="owner-1"),
            )
        )


# --- U11 fix F5: the test-only composition lane refuses a gated agent id -----


def test_the_test_only_composition_lane_refuses_a_gated_agent_id() -> None:
    """The all-integrations-None lane runs an adapter with NO recorder and NO
    gate. For an agent id the deployment gated, that would silently contradict
    the constructor's own "never execute ungated" rule — refuse instead."""

    class _NeverRuns:
        instructions = "unused"

        async def run(self, **kwargs: Any) -> Any:
            raise AssertionError("a gated agent must never execute on the ungated lane")

    engine, sink = _GateEngine(), _GateSink()
    adapter = ProfileBoundRunExecutionAdapter(
        bindings={},
        principal_resolver=_Resolver(Principal(id="owner-1")),
        deterministic_executor=cast(Any, None),
        runtime_settings=object(),
        sink=object(),
        grounding=_gate_policy(engine, sink),
        grounded_run_agent_ids=(_GATED_AGENT,),
    )
    with pytest.raises(RuntimeError, match="ungated"):
        asyncio.run(
            adapter._execute_as_principal(
                _context(strategy="agentic"),
                _agentic_binding(_NeverRuns()),
                Principal(id="owner-1"),
            )
        )


def test_approval_resume_continuation_executes_pinned_effect_through_the_real_adapter() -> None:
    """NE-S5-INTERNAL-CRM closure of the mirror gap: the REAL worker adapter.

    ``test_run_approval_resume_continuation`` (ne-api) proves the resume chain
    against an adapter that MIRRORS
    ``ProfileBoundRunExecutionAdapter._execute_effect_continuation``. This test
    drives the REAL adapter's public ``execute`` with a pinned
    ``RunEffectContinuation`` and asserts the production branch itself:

    * exactly ONE governed executor call — the approval's pinned action with
      the approved arguments, byte-for-byte;
    * the agent loop / model side is NEVER entered (the agentic adapter raises
      if touched);
    * the ambient ``action_scope`` the worker wraps every attempt in is LIVE
      around the continuation call — the derived idempotency identity an
      ActionDefinition effect (the live ``write.external.email.send``) needs to
      claim its dedup key on resume;
    * the terminal is a content-free ``execution_complete`` notice naming the
      action, never the tool's own result.
    """
    from dataclasses import replace as dc_replace

    from ne.runtime.run_runner import RunEffectContinuation

    executed: list[tuple[str, dict[str, Any], str | None]] = []

    class _RecordingExecutor:
        async def execute(self, tool: str, args: Any, *, principal: Principal) -> str:
            executed.append((tool, dict(args), current_action_scope()))
            return '{"succeeded": true, "detail": "sent"}'

    class _MustNotRunLoop(BoundedAgenticAdapter):
        async def run(self, **kwargs: Any) -> Any:
            raise AssertionError("the resume must never re-enter the agent loop or the model")

    class _Settings(BaseModel):
        task: str | None = None

    adapter = ProfileBoundRunExecutionAdapter(
        bindings={
            "agt_01k3hardshipreview0000000000": _agentic_binding(
                _MustNotRunLoop(
                    instructions="Run Skill activation is required before execution.",
                    max_tool_calls=1,
                    total_tokens_limit=1000,
                    wall_clock_seconds=10.0,
                    max_parked_writes=0,
                )
            )
        },
        principal_resolver=_Resolver(Principal(id="owner-1")),
        deterministic_executor=cast(Any, None),
        runtime_settings=_Settings(),
        sink=object(),
        catalog=_Catalog(_definition()),
        agent_skills=cast(Any, None),
        effective_tool_grants={"agt_01k3hardshipreview0000000000": ()},
        scope_executor=lambda executor, **_: executor,
    )

    arguments: dict[str, object] = {"draft_ref": "AAMkAG-s5-draft-message-id"}
    context = dc_replace(
        _context(strategy="agentic"),
        executor=cast(Any, _RecordingExecutor()),
        continuation=RunEffectContinuation(
            approval_id="apr-1",
            step_id="run-tool",
            tool_call_id="run-tool:tool:2",
            action="write.external.email.send",
            arguments=arguments,
        ),
    )

    async def _drive() -> RunExecutionResult:
        # The workflow service binds the delivery observer around the adapter
        # (RunWorkflowService.execute); mirrored here so the adapter's own
        # direct-publication completion lands on a bound observer.
        from ne.runtime.source_results import run_delivery_observation_scope

        with run_delivery_observation_scope(run_id="run-1", attempt_id="attempt-1"):
            return await adapter.execute(context)

    result = asyncio.run(_drive())

    assert result.outcome == "execution_complete"
    assert len(executed) == 1
    tool, args, scope = executed[0]
    assert tool == "write.external.email.send"
    assert args == arguments
    # The worker's ambient action scope was live around the continuation call.
    assert scope == "agent-run:run-1:attempt-1"
    assert result.output_bytes is not None
    assert b"write.external.email.send" in result.output_bytes
    assert b"after human approval" in result.output_bytes
