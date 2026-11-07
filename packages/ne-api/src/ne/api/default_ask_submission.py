# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Dormant PRE-010G boundary for default Ask -> canonical Run activation.

This module deliberately mounts no route and composes no production service.
The remaining default-Agent producers must first bind one exact readiness pin
in ``platform.py`` (outside this phase's ownership), and the runtime store must
implement the atomic transaction declared here.  Until both happen, default
Ask remains on PRE-009C's protected attachment transport and creates no Run.

The eventual activation has one write boundary: validate the live catalog and
011E snapshot without mutation, then atomically claim the Conversation turn,
create its Run/attempt/outbox, persist the server-authored ``RunTurnTarget``,
and return the content-free ``RunWorkflowRequest``.  There is intentionally no
sequential fallback and no legacy terminal, attachment-writer, or cross-turn
loader dependency in this service.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Literal, Protocol, runtime_checkable

from ne.api.agent_catalog import CatalogAgent
from ne.api.agent_manifest import ExecutionStrategy
from ne.api.run_submission import (
    AcceptedRunSubmission,
    RunSubmissionConflict,
    RunSubmissionNotFound,
    RunSubmissionService,
    StartRunCommand,
)
from ne.contracts.types import Lane, Principal
from ne.runtime.run_store import RunSubmissionConflictError, RunSubmissionNotFoundError
from ne.runtime.run_workflow import RunWorkflowRequest

__all__ = [
    "DEFAULT_ASK_AGENT_ID",
    "DEFAULT_ASK_WIRE_GENERATION",
    "DefaultAskAccepted",
    "DefaultAskActivationPin",
    "DefaultAskAdmissionCommand",
    "DefaultAskSubmissionService",
    "DefaultAskTransactions",
    "DefaultAskUnavailable",
    "compose_default_ask_submission_service",
    "select_default_ask_agent",
]

DEFAULT_ASK_AGENT_ID = "agt_01k3meridiananalyst000000000"
DEFAULT_ASK_WIRE_GENERATION = "default-ask/canonical-run/v1"
_RETIRED = "retired"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DefaultAskUnavailable(RuntimeError):
    """The complete canonical default-Ask path is not currently safe to use."""


def select_default_ask_agent(
    agents: Iterable[CatalogAgent],
    *,
    configured_agent_id: str = DEFAULT_ASK_AGENT_ID,
    allow_knowledge_bootstrap: bool = False,
) -> CatalogAgent:
    """Select the configured Ask Agent without inferring from catalog cardinality.

    Workflow-backed Agents may be available beside the Q&A Agent.  Their
    availability must not disable Ask or make one of them an accidental default;
    only the configured opaque identity is eligible.  Absence, duplication, or
    unavailability all fail boot readiness instead of selecting a substitute.
    The one explicit exception lets a fresh governed connector deployment start
    its API before its first authority-producing sync.  It accepts only the exact
    ``knowledge-unavailable`` reason; the caller must keep canonical Ask dormant
    and every legacy writer closed until a later boot sees the authority receipt.
    """

    if not configured_agent_id.strip():
        raise ValueError("configured default Ask Agent is unavailable")
    matches = tuple(row for row in agents if row.definition.agent_id == configured_agent_id)
    if len(matches) != 1:
        raise ValueError("configured default Ask Agent is unavailable")
    selected = matches[0]
    if selected.available:
        return selected
    if allow_knowledge_bootstrap and selected.availability_reasons == ("knowledge-unavailable",):
        return selected
    raise ValueError("configured default Ask Agent is unavailable")


@dataclass(frozen=True, slots=True)
class DefaultAskActivationPin:
    """Boot-frozen identities required before default Ask may be activated.

    The three producer booleans are intentionally independent.  A composition
    that has only submission, worker execution, or finalization must remain
    unavailable rather than falling back after a partial write.  The three
    retirement fields likewise name distinct legacy capabilities so reopening
    any one of them invalidates readiness.
    """

    wire_generation: str
    config_hash: str
    default_agent_id: str
    default_agent_version: int
    producer_snapshot_hash: str
    submission_ready: bool
    worker_execution_ready: bool
    finalizer_ready: bool
    legacy_attachment_writer: Literal["retired"]
    legacy_terminal_writer: Literal["retired"]
    legacy_cross_turn_loader: Literal["retired"]

    def is_ready(self) -> bool:
        hashes_are_real = all(
            _SHA256_RE.fullmatch(value) is not None and len(set(value)) > 1
            for value in (self.config_hash, self.producer_snapshot_hash)
        )
        return (
            self.wire_generation == DEFAULT_ASK_WIRE_GENERATION
            and hashes_are_real
            and bool(self.default_agent_id.strip())
            and self.default_agent_version >= 1
            and self.submission_ready is True
            and self.worker_execution_ready is True
            and self.finalizer_ready is True
            and self.legacy_attachment_writer == _RETIRED
            and self.legacy_terminal_writer == _RETIRED
            and self.legacy_cross_turn_loader == _RETIRED
        )


@dataclass(frozen=True, slots=True)
class DefaultAskAdmissionCommand:
    """One command for the future atomic Run + target + outbox transaction."""

    start: StartRunCommand
    activation_wire_generation: str
    producer_snapshot_hash: str
    turn_id: str
    attachment_id: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class DefaultAskAccepted:
    """Canonical admission beside its exact, persisted-target workflow input."""

    submission: AcceptedRunSubmission
    workflow_request: RunWorkflowRequest


@runtime_checkable
class DefaultAskTransactions(Protocol):
    """Runtime-owned atomic default-Ask admission.

    The implementation must claim-or-load the idempotency key and, in the same
    database transaction, create/load the canonical Run and attempt, claim the
    Conversation turn/attachment, persist its ``RunTurnTarget``, and insert the
    exact enqueue outbox.  It returns identities loaded from those rows; an API
    caller never supplies or fabricates a Run, attempt, target, or dispatch id.
    """

    async def admit_default_ask(
        self, command: DefaultAskAdmissionCommand
    ) -> DefaultAskAccepted: ...


class DefaultAskSubmissionService:
    """Prepare without mutation, then cross one canonical atomic boundary."""

    def __init__(
        self,
        *,
        run_submissions: RunSubmissionService,
        transactions: DefaultAskTransactions,
        activation: DefaultAskActivationPin,
    ) -> None:
        if not activation.is_ready():
            raise ValueError("default Ask activation pin is not ready")
        self._run_submissions = run_submissions
        self._transactions = transactions
        self._activation = activation

    @property
    def activation(self) -> DefaultAskActivationPin:
        return self._activation

    async def submit(
        self,
        *,
        principal: Principal,
        conversation_id: str,
        expected_version: int,
        agent_id: str,
        agent_version: int,
        strategy: ExecutionStrategy,
        config_hash: str,
        capability_snapshot_hash: str,
        input_text: str,
        idempotency_key: str,
        turn_id: str | None = None,
        attachment_id: str | None = None,
        trace_id: str | None = None,
        producer_snapshot_hash: str | None = None,
        requested_model: str | None = None,
        requested_model_lane: Lane | None = None,
    ) -> DefaultAskAccepted:
        pin = self._activation
        if (
            agent_id != pin.default_agent_id
            or agent_version != pin.default_agent_version
            or config_hash != pin.config_hash
            or (producer_snapshot_hash or capability_snapshot_hash) != pin.producer_snapshot_hash
        ):
            raise DefaultAskUnavailable("default Ask producer snapshot is not current")

        # This performs current principal/catalog/producer revalidation but no
        # Run, attachment, target, or enqueue write.
        start = await self._run_submissions.prepare_start(
            principal=principal,
            agent_id=agent_id,
            conversation_id=conversation_id,
            expected_version=expected_version,
            agent_version=agent_version,
            strategy=strategy,
            config_hash=config_hash,
            capability_hash=capability_snapshot_hash,
            input_text=input_text,
            idempotency_key=idempotency_key,
            requested_model=requested_model,
            requested_model_lane=requested_model_lane,
        )
        # The Conversation CAS version fences only the first write. It is not
        # part of canonical Ask's replay identity: after that write succeeds the
        # parent version necessarily advances, and a queue-failure retry must
        # still load the exact original Run with zero writes.
        replay_identity = {
            "action": "default-ask",
            "principal_id": start.owner_principal_id,
            "department": start.department,
            "conversation_id": start.conversation_id,
            "agent_id": start.agent_id,
            "agent_version": start.agent_version,
            "strategy": str(start.strategy),
            "config_hash": start.config_hash,
            "capability_snapshot_hash": start.capability_snapshot_hash,
            "capability_snapshot_receipt_id": start.capability_snapshot_receipt_id,
            "input_hash": hashlib.sha256(start.input_plaintext).hexdigest(),
            # The explicit model choice is part of what this turn IS: a frozen
            # idempotent resend replays only the run it named, and a resend that
            # names a DIFFERENT model conflicts loudly instead of silently
            # joining a run on another lane (this bug's whole signature).
            "requested_model": start.requested_model,
            "turn_id": turn_id or idempotency_key,
            "attachment_id": attachment_id or turn_id or idempotency_key,
        }
        start = replace(
            start,
            request_hash=hashlib.sha256(
                json.dumps(replay_identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )
        command = DefaultAskAdmissionCommand(
            start=start,
            activation_wire_generation=pin.wire_generation,
            producer_snapshot_hash=pin.producer_snapshot_hash,
            turn_id=turn_id or idempotency_key,
            attachment_id=attachment_id or turn_id or idempotency_key,
            trace_id=trace_id
            or hashlib.sha256(f"default-ask:{idempotency_key}".encode()).hexdigest(),
        )
        try:
            accepted = await self._transactions.admit_default_ask(command)
        except RunSubmissionNotFoundError as exc:
            raise RunSubmissionNotFound from exc
        except RunSubmissionConflictError as exc:
            raise RunSubmissionConflict from exc
        self._require_canonical_dispatch(accepted, start)
        return accepted

    @staticmethod
    def _require_canonical_dispatch(accepted: DefaultAskAccepted, start: StartRunCommand) -> None:
        submission = accepted.submission
        request = accepted.workflow_request
        target = request.target
        if (
            submission.action != "start"
            or submission.status not in {"submitted", "queued"}
            or submission.version < 1
            or request.dispatch_id != submission.enqueue_id
            or request.run_id != submission.run_id
            or request.owner_principal_id != start.owner_principal_id
            or request.config_hash != start.config_hash
            or target.conversation_id != start.conversation_id
            or target.run_id != submission.run_id
            or target.attempt_id != submission.attempt_id
        ):
            raise DefaultAskUnavailable(
                "default Ask admission did not return one canonical Run target"
            )


def compose_default_ask_submission_service(
    *,
    run_submissions: RunSubmissionService | None,
    transactions: object,
    activation: DefaultAskActivationPin | None,
) -> DefaultAskSubmissionService | None:
    """Remain dormant until every exact producer and retirement pin is present."""

    if (
        run_submissions is None
        or activation is None
        or not activation.is_ready()
        or not isinstance(transactions, DefaultAskTransactions)
    ):
        return None
    return DefaultAskSubmissionService(
        run_submissions=run_submissions,
        transactions=transactions,
        activation=activation,
    )
