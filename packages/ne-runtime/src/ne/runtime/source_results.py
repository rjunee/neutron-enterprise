# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Protected source-result delivery guard — ne-runtime's last-mile disclosure boundary.

PRE-015A0-ENFORCEMENT (SPEC §13). A source-bearing Tool result, protected assistant
output or approval invocation never travels as plaintext through ne-runtime. The
producer mints an opaque, server-side reference (:func:`encode_protected_reference`);
durable Pydantic history, Conversation rows and approval payloads retain only that
reference, and the protected text is materialized EPHEMERALLY, per delivery, under a
freshly re-checked authorization.

Three properties this module exists to hold:

* **The authorization is the request's, not the row's.** Every materialization runs
  under the verifier-authored :class:`~ne.contracts.identity.AuthenticatedRequestContext`
  bound for this unit of work — its opaque principal id and canonical context hash are
  what reach the injected :class:`~ne.contracts.source_authority.SourceBoundResultResolver`.
  Nothing here reconstructs a principal, reads a token or consults an ACL: ne-runtime
  owns no ACL logic and imports no ne-brain.
* **Absence refuses.** No ambient context, no target binding, an expired snapshot, a
  malformed reference or a resolution bound to a different target all yield a refusal
  or one of the two fixed safe strings — never protected text, and never a permissive
  default identity, an empty allow-all ACL or a cached prior context. The contextvars
  below carry ``None`` as their default and are reset on scope exit, so a prior
  request's snapshot cannot survive into a later one.
* **Drift withholds.** :class:`ProtectedSourceResultGuard` maps the resolver's typed
  decision onto exactly one of: the authorized text, the fixed permanent-withheld text,
  or the fixed retryable-unavailable text. ``retryable_unknown`` is never upgraded to a
  permanent denial, and ``authorized`` is never inferred from a missing decision.

Exact behaviour is pinned by tests, not by this prose: the codec, the contextvar
scopes, the fail-closed refusals and the model-send seam by
``tests/test_model_adapter.py``; the composed session by ``tests/test_agent_loop.py``;
the approval seam by ``tests/test_approval_payload_read.py`` and
``tests/test_approval_decision_linkage.py``; the direct routed publication by
``tests/test_verified_router.py``.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from pydantic import ValidationError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
)

from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    ProtectedClaimSliceRef,
    ProtectedSourceInvocationRef,
    ProtectedSourceOutputRef,
    ProtectedWorkflowStateRef,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceBoundApprovalResolution,
    SourceBoundDirectResolution,
    SourceBoundDispatchResolution,
    SourceBoundEvidenceProjectionResolution,
    SourceBoundModelSendResolution,
    SourceBoundResultResolver,
    SourceBoundToolResultRef,
    SourceBoundToolResultSetRef,
    SourceBoundWorkflowProjectionResolution,
)
from ne.runtime.errors import NeRuntimeError
from ne.runtime.private_payloads import PrivatePayloadDomain
from ne.runtime.run_audit_receipts import current_run_observation_audit
from ne.runtime.run_store import AgentRunStore, StoredSourcePayload

__all__ = [
    "PROTECTED_REFERENCE_PREFIX",
    "SOURCE_UNAVAILABLE_TEXT",
    "SOURCE_WITHHELD_TEXT",
    "AuthorizationExpiredError",
    "ClaimRetainingSourceGuard",
    "DeliverableReference",
    "DirectPublicationBinding",
    "EvidenceProjectionPayloadReader",
    "HistoryDelivery",
    "MalformedProtectedReferenceError",
    "MaterializedSourceResult",
    "MissingAuthenticatedContextError",
    "MissingTargetBindingError",
    "ModelCallBinding",
    "ProtectedDeliverySite",
    "ProtectedExecutionError",
    "ProtectedReference",
    "ProtectedSourceResultGuard",
    "RunDeliveryObservation",
    "RunDeliveryObserver",
    "RunStoreProtectedSourceExecutionWriter",
    "RunStoreProtectedSourceOutputWriter",
    "RunStoreSourceBoundResultResolver",
    "ServedTextRetention",
    "SourceAuthorityRetryableError",
    "SourceAuthorityWithheldError",
    "SourceStampEvaluator",
    "UnavailableSourceBoundResultResolver",
    "authenticated_request_scope",
    "complete_run_delivery_observation",
    "complete_run_direct_publication_observation",
    "current_authenticated_context",
    "current_protected_tool_evidence_refs",
    "decode_invocation_reference",
    "decode_protected_reference",
    "direct_publication_scope",
    "encode_protected_reference",
    "model_call_scope",
    "protected_tool_evidence_scope",
    "require_authenticated_context",
    "require_direct_publication_binding",
    "require_model_call_binding",
    "run_delivery_observation_scope",
    "run_delivery_set_id",
]

# The server-minted namespace an opaque protected reference occupies. A string in
# this namespace is ALWAYS treated as a reference: one that does not decode is
# refused rather than forwarded, so a model- or browser-supplied lookalike cannot
# reach a model as plaintext.
PROTECTED_REFERENCE_PREFIX: Final = "ne-source-ref:v1:"

# The two fixed safe substitutions. Both are byte-stable, carry no source
# identifier, artifact id, principal or reason detail, and are the ONLY strings
# this module ever puts in place of protected content.
SOURCE_WITHHELD_TEXT: Final = (
    "[source-authority] This content is withheld: the current source authorization "
    "no longer permits disclosing it."
)
SOURCE_UNAVAILABLE_TEXT: Final = (
    "[source-authority] This content is unavailable: the current source authorization "
    "could not be established. Treat it as unknown, not as absent."
)

# What a model send / history assembly may deliver. An approval invocation is a
# DIFFERENT target kind and is deliberately not in this union: one appearing at a
# model-send seam is the wrong kind and is withheld rather than delivered.
type DeliverableReference = (
    SourceBoundToolResultRef | SourceBoundToolResultSetRef | ProtectedSourceOutputRef
)
type ProtectedReference = (
    SourceBoundToolResultRef
    | SourceBoundToolResultSetRef
    | ProtectedSourceOutputRef
    | ProtectedSourceInvocationRef
)

_REFERENCE_KINDS: Final[dict[str, type[ProtectedReference]]] = {
    "tool_result": SourceBoundToolResultRef,
    "tool_result_set": SourceBoundToolResultSetRef,
    "source_output": ProtectedSourceOutputRef,
    "invocation": ProtectedSourceInvocationRef,
}


class ProtectedExecutionError(NeRuntimeError):
    """A protected source result could not be delivered, so nothing was disclosed."""


class MissingAuthenticatedContextError(ProtectedExecutionError):
    """No verifier-authored authorization snapshot is bound to this unit of work.

    Fail-closed by construction: there is no permissive fallback identity and no
    reuse of a previously bound context. A caller that has not entered
    :func:`authenticated_request_scope` cannot materialize protected content at all.
    """


class MissingTargetBindingError(ProtectedExecutionError):
    """The exact Run/attempt (or workflow/publication) target is not bound.

    The resolver authorizes a reference FOR one target. Without that target there
    is nothing to bind a served receipt to, so the delivery is refused.
    """


class AuthorizationExpiredError(ProtectedExecutionError):
    """The bound snapshot is at or past its verified expiry; nothing is disclosed.

    Retry requires a newly authenticated request — the runtime never silently
    rebinds an expired snapshot to a fresh one.
    """


class MalformedProtectedReferenceError(ProtectedExecutionError):
    """A string in the protected-reference namespace did not decode to a reference."""


class SourceAuthorityWithheldError(ProtectedExecutionError):
    """Admitted current authority permanently denies this protected material.

    Distinct from :class:`SourceAuthorityRetryableError`: only a successfully
    admitted authority fact (ACL contraction, deletion, generation replacement)
    reaches this class, so a caller may treat it as terminal for this material.
    """


class SourceAuthorityRetryableError(ProtectedExecutionError):
    """Current authority could not be established, so nothing terminal is written.

    Missing, stale or ambiguous authority, a dependency outage and an evaluator
    error all land here. The caller must leave its durable state exactly as it was
    and retry later — this is never relabelled a permanent denial.
    """


class SourceStampEvaluator(Protocol):
    """Injected composition callback; ne-runtime never imports ACL/Brain logic."""

    def __call__(
        self, stamp: SourceAuthorityStamp, context: AuthenticatedRequestContext
    ) -> Awaitable[SourceAuthorityDecision]: ...


class EvidenceProjectionPayloadReader(Protocol):
    """Read one protected payload through the exact owner/Run projection target.

    This deliberately is not a generic protected-row reader.  The implementation
    owns the durable binding between ``reference.reference_id`` and ``run_id`` and
    returns no material for a foreign owner, another Run, or another target kind.
    The resolver below still authenticates the request and re-checks every retained
    authority stamp before any text can leave this boundary.
    """

    async def load_for_evidence_projection(
        self,
        reference: ProtectedSourceOutputRef | ProtectedClaimSliceRef,
        *,
        owner_principal_id: str,
        run_id: str,
    ) -> StoredSourcePayload: ...


class RunStoreProtectedSourceOutputWriter:
    """Least-privilege output writer over the encrypted AgentRunStore."""

    def __init__(self, store: AgentRunStore) -> None:
        self._store = store

    async def claim_output(
        self,
        *,
        target_id: str,
        attempt_id: str,
        output: bytes,
        output_hash: str,
        stamps: Sequence[SourceAuthorityStamp],
    ) -> ProtectedSourceOutputRef:
        _require_hash(output, output_hash, "output")
        context = require_authenticated_context()
        validated = ProtectedSourceOutputRef(
            reference_id="pending", output_hash=output_hash, stamps=tuple(stamps)
        )
        row = await self._store.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
            run_id=None,
            owner_principal_id=context.principal_id,
            target_kind="assistant_turn",
            target_binding=(target_id, attempt_id),
            plaintext=output,
            source_stamps=validated.stamps,
        )
        return validated.model_copy(update={"reference_id": row.row_id})


class RunStoreProtectedSourceExecutionWriter:
    """Invocation/workflow-state writer with no read or authority capability."""

    def __init__(self, store: AgentRunStore) -> None:
        self._store = store

    async def claim_invocation(
        self,
        *,
        target_kind: str,
        target_id: str,
        attempt_id: str,
        invocation: bytes,
        invocation_hash: str,
        stamps: Sequence[SourceAuthorityStamp],
    ) -> ProtectedSourceInvocationRef:
        if target_kind not in {"approval_action", "subagent_dispatch"}:
            raise ValueError("unsupported protected invocation target")
        _require_hash(invocation, invocation_hash, "invocation")
        context = require_authenticated_context()
        validated = ProtectedSourceInvocationRef(
            reference_id="pending", invocation_hash=invocation_hash, stamps=tuple(stamps)
        )
        row = await self._store.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_SOURCE_INVOCATION,
            run_id=None,
            owner_principal_id=context.principal_id,
            target_kind=target_kind,
            target_binding=(target_id, attempt_id),
            plaintext=invocation,
            source_stamps=validated.stamps,
        )
        return validated.model_copy(update={"reference_id": row.row_id})

    async def claim_workflow_state(
        self,
        *,
        workflow_execution_id: str,
        workflow_version: int,
        step_id: str,
        publication_id: str,
        state: bytes,
        state_hash: str,
        stamps: Sequence[SourceAuthorityStamp],
    ) -> ProtectedWorkflowStateRef:
        if workflow_version < 1:
            raise ValueError("workflow_version must be positive")
        _require_hash(state, state_hash, "workflow state")
        context = require_authenticated_context()
        validated = ProtectedWorkflowStateRef(
            reference_id="pending", state_hash=state_hash, stamps=tuple(stamps)
        )
        row = await self._store.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_WORKFLOW_STATE,
            run_id=None,
            owner_principal_id=context.principal_id,
            target_kind="standalone_workflow",
            target_binding=(
                workflow_execution_id,
                str(workflow_version),
                step_id,
                publication_id,
            ),
            plaintext=state,
            source_stamps=validated.stamps,
        )
        return validated.model_copy(update={"reference_id": row.row_id})


def _require_hash(payload: bytes, expected: str, label: str) -> None:
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError(f"{label} hash does not match its protected bytes")


def _may_name_an_evidence_set(
    reference: SourceBoundToolResultRef
    | SourceBoundToolResultSetRef
    | ProtectedSourceOutputRef
    | ProtectedSourceInvocationRef
    | ProtectedWorkflowStateRef
    | ProtectedClaimSliceRef,
) -> bool:
    """MAY THIS TARGET'S STORED PAYLOAD NAME THE EVIDENCE RECORDS BEHIND IT?

    Exactly two writers pack an evidence set into a protected payload, and this
    predicate names both. Everything else must carry none, and a payload that
    carries one anyway does not match its target and is refused -- that is
    PRE-011C's rule (#731) and it is unchanged.

      `SourceBoundToolResultSetRef`   run_store, the pending tool result for an
                                      atomic verified evidence set. The caller
                                      DECLARES the set, so it is bound above:
                                      `stored.evidence_ids` must equal the
                                      reference's, exactly and in order.

      `ProtectedSourceOutputRef`      run_finalizer `_protect_output`, which
                                      packs "the SET of records this output is
                                      bound to" into the answer's own payload.

    WHY THE OUTPUT'S SET IS NOT BOUND TO A CALLER-DECLARED VALUE, and why that
    is not a hole. The reader of an answer never asserts which evidence the
    answer rested on -- it is asking. There is nothing of the caller's to check
    the stored ids against, and checking them against themselves would be
    ceremony, not a control. What actually protects them is stronger and already
    in force: the ids live INSIDE the AEAD ciphertext whose AAD binds the exact
    owner, target kind and turn binding (run_finalizer `_protect_output` ->
    `protected_payload_aad_binding`), so they cannot be substituted, moved to
    another run, or edited without the key; `stored.plaintext_hash` must still
    equal the reference's `output_hash`; the stamp set must still equal the
    reference's; and every stamp is still re-evaluated against CURRENT source
    authority below. Not one of those checks is relaxed here.

    THE COST OF GETTING THIS WRONG WAS TOTAL. The finalizer landed after #731
    and was never taught about its door, so every source-derived answer -- which
    is every grounded answer the product exists to produce -- resolved
    RETRYABLE_UNKNOWN forever, and the evidence projection reported that as
    "Answer withheld — access changed": an access decision that was never made.
    """

    if isinstance(reference, SourceBoundToolResultSetRef):
        return True
    return isinstance(reference, ProtectedSourceOutputRef)


class RunStoreSourceBoundResultResolver:
    """Target-specific resolver over encrypted rows plus an injected evaluator."""

    def __init__(
        self,
        store: AgentRunStore,
        evaluator: SourceStampEvaluator,
        *,
        evidence_projection_reader: EvidenceProjectionPayloadReader | None = None,
    ) -> None:
        self._store = store
        self._evaluator = evaluator
        self._evidence_projection_reader = evidence_projection_reader

    def _context(self, principal_id: str, access_context_hash: str) -> AuthenticatedRequestContext:
        context = require_authenticated_context()
        if context.principal_id != principal_id or context.context_hash != access_context_hash:
            raise MissingAuthenticatedContextError(
                "resolver arguments do not match the bound authenticated context"
            )
        return context

    async def _decision(
        self,
        stored: StoredSourcePayload,
        reference: SourceBoundToolResultRef
        | SourceBoundToolResultSetRef
        | ProtectedSourceOutputRef
        | ProtectedSourceInvocationRef
        | ProtectedWorkflowStateRef
        | ProtectedClaimSliceRef,
        context: AuthenticatedRequestContext,
    ) -> tuple[SourceAuthorityDecision, str | None, tuple[SourceAuthorityStamp, ...]]:
        expected_hash = getattr(
            reference,
            "result_hash",
            getattr(
                reference,
                "output_hash",
                getattr(
                    reference,
                    "invocation_hash",
                    getattr(reference, "state_hash", getattr(reference, "slice_hash", "")),
                ),
            ),
        )
        if (
            stored.plaintext_hash != expected_hash
            or stored.stamps != reference.stamps
            or not stored.stamps
            or (
                isinstance(reference, SourceBoundToolResultSetRef)
                and stored.evidence_ids != reference.evidence_ids
            )
            or (not _may_name_an_evidence_set(reference) and bool(stored.evidence_ids))
        ):
            return SourceAuthorityDecision.RETRYABLE_UNKNOWN, None, ()
        # NE-DELIVERY-PASS-MEMO: within one delivery pass, a stamp already
        # evaluated for this exact principal/access context is not re-asked.
        # Outside a pass (memo is None) every stamp hits the evaluator exactly
        # as before. See _STAMP_DECISION_MEMO for the invariance argument.
        memo = _STAMP_DECISION_MEMO.get()
        decisions: list[SourceAuthorityDecision] = []
        for stamp in stored.stamps:
            if memo is None:
                decisions.append(await self._evaluator(stamp, context))
                continue
            key = (stamp, context.principal_id, context.context_hash)
            decision = memo.get(key)
            if decision is None:
                decision = await self._evaluator(stamp, context)
                memo[key] = decision
            decisions.append(decision)
        if any(decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN for decision in decisions):
            return SourceAuthorityDecision.RETRYABLE_UNKNOWN, None, ()
        if any(decision is SourceAuthorityDecision.PERMANENT_WITHHELD for decision in decisions):
            return SourceAuthorityDecision.PERMANENT_WITHHELD, None, ()
        try:
            text = stored.plaintext.decode("utf-8")
        except UnicodeDecodeError:
            return SourceAuthorityDecision.RETRYABLE_UNKNOWN, None, ()
        return SourceAuthorityDecision.AUTHORIZED, text, stored.stamps

    async def for_model_send(
        self,
        reference: SourceBoundToolResultRef
        | SourceBoundToolResultSetRef
        | ProtectedSourceOutputRef,
        *,
        principal_id: str,
        access_context_hash: str,
        run_id: str,
        model_call_attempt_id: str,
    ) -> SourceBoundModelSendResolution:
        context = self._context(principal_id, access_context_hash)
        stored: StoredSourcePayload | None = None
        try:
            stored = (
                await self._store.load_pending_source_tool_result(
                    reference_id=reference.reference_id, owner_principal_id=principal_id
                )
                if isinstance(reference, SourceBoundToolResultRef)
                else await self._store.load_protected_source_output(
                    reference_id=reference.reference_id,
                    owner_principal_id=principal_id,
                    target_id=run_id,
                    attempt_id=model_call_attempt_id,
                    delivery_site="model_send",
                )
            )
            decision, text, stamps = await self._decision(stored, reference, context)
        except (LookupError, ValueError):
            decision, text, stamps = SourceAuthorityDecision.RETRYABLE_UNKNOWN, None, ()
        observer = _current_delivery_observer()
        if stored is not None and observer is not None:
            protected_hash = (
                reference.result_hash
                if isinstance(reference, SourceBoundToolResultRef | SourceBoundToolResultSetRef)
                else reference.output_hash
            )
            delivery_kind = (
                "tool_result_served"
                if isinstance(reference, SourceBoundToolResultRef | SourceBoundToolResultSetRef)
                else "history_output_served"
            )
            if decision is not SourceAuthorityDecision.AUTHORIZED:
                delivery_kind = "withheld"
            receipt_id = await self._store.claim_delivery(
                run_id=run_id,
                owner_principal_id=principal_id,
                delivery_kind=delivery_kind,
                protected_ref=reference.reference_id,
                protected_hash=protected_hash,
                source_stamps=stored.stamps,
                receiving_attempt_id=model_call_attempt_id,
            )
            audit = current_run_observation_audit()
            if audit is not None:
                await audit.append_delivery_receipt(
                    delivery_receipt_id=receipt_id, actor=principal_id
                )
            observer.record_receipt(
                run_id=run_id,
                attempt_id=model_call_attempt_id,
                receipt_id=receipt_id,
            )
        return SourceBoundModelSendResolution(
            decision=decision,
            materialized_text=text,
            stamps=stamps,
            run_id=run_id,
            model_call_attempt_id=model_call_attempt_id,
        )

    async def for_direct_result(
        self,
        reference: SourceBoundToolResultRef | SourceBoundToolResultSetRef,
        *,
        principal_id: str,
        access_context_hash: str,
        workflow_execution_id: str,
        publication_attempt_id: str,
    ) -> SourceBoundDirectResolution:
        context = self._context(principal_id, access_context_hash)
        stored: StoredSourcePayload | None = None
        try:
            stored = await self._store.load_pending_source_tool_result(
                reference_id=reference.reference_id, owner_principal_id=principal_id
            )
            decision, text, stamps = await self._decision(stored, reference, context)
        except (LookupError, ValueError):
            decision, text, stamps = SourceAuthorityDecision.RETRYABLE_UNKNOWN, None, ()
        observer = _current_delivery_observer()
        if stored is not None and observer is not None:
            delivery_kind = (
                "direct_result_served"
                if decision is SourceAuthorityDecision.AUTHORIZED
                else "withheld"
            )
            receipt_id = await self._store.claim_delivery(
                run_id=workflow_execution_id,
                owner_principal_id=principal_id,
                delivery_kind=delivery_kind,
                protected_ref=reference.reference_id,
                protected_hash=reference.result_hash,
                source_stamps=stored.stamps,
                receiving_attempt_id=publication_attempt_id,
            )
            audit = current_run_observation_audit()
            if audit is not None:
                await audit.append_delivery_receipt(
                    delivery_receipt_id=receipt_id, actor=principal_id
                )
            observer.record_receipt(
                run_id=workflow_execution_id,
                attempt_id=publication_attempt_id,
                receipt_id=receipt_id,
            )
        return SourceBoundDirectResolution(
            decision=decision,
            materialized_text=text,
            stamps=stamps,
            workflow_execution_id=workflow_execution_id,
            publication_attempt_id=publication_attempt_id,
        )

    async def for_approval(
        self,
        reference: ProtectedSourceInvocationRef,
        *,
        principal_id: str,
        access_context_hash: str,
        approval_id: str,
        decision_attempt_id: str,
    ) -> SourceBoundApprovalResolution:
        context = self._context(principal_id, access_context_hash)
        try:
            stored = await self._store.load_protected_source_invocation(
                reference_id=reference.reference_id,
                owner_principal_id=principal_id,
                target_kind="approval_action",
                target_id=approval_id,
                attempt_id=decision_attempt_id,
            )
            decision, text, stamps = await self._decision(stored, reference, context)
        except (LookupError, ValueError):
            decision, text, stamps = SourceAuthorityDecision.RETRYABLE_UNKNOWN, None, ()
        return SourceBoundApprovalResolution(
            decision=decision,
            materialized_text=text,
            stamps=stamps,
            approval_id=approval_id,
            decision_attempt_id=decision_attempt_id,
        )

    async def for_approval_stored(
        self,
        stored: StoredSourcePayload,
        reference: ProtectedSourceInvocationRef,
        *,
        principal_id: str,
        access_context_hash: str,
        approval_id: str,
        decision_attempt_id: str,
    ) -> SourceBoundApprovalResolution:
        """Stamp-evaluate an ALREADY-LOADED park-time approval invocation.

        A Run-parked approval's protected invocation row is written at PARK time,
        owned by the RUN owner and bound to the ``(run_id, attempt_id, ...)``
        prefix. A decision-time reload keyed by ``(approval_id,
        decision_attempt_id)`` under the APPROVER's identity can therefore NEVER
        find it — that key was structurally impossible for the row that exists.
        The checkpoint coordinator loads and hash-verifies the row under its
        park-time key; this method performs the full stamp re-evaluation over
        every stamp in the stored payload for the CURRENT bound context (the
        approver at decision time, the run owner at resume time). Nothing about
        the evaluation itself is relaxed: hash echo, stamp-set equality and the
        per-stamp evaluator run exactly as in :meth:`for_approval`.
        """
        context = self._context(principal_id, access_context_hash)
        try:
            decision, text, stamps = await self._decision(stored, reference, context)
        except (LookupError, ValueError):
            decision, text, stamps = SourceAuthorityDecision.RETRYABLE_UNKNOWN, None, ()
        return SourceBoundApprovalResolution(
            decision=decision,
            materialized_text=text,
            stamps=stamps,
            approval_id=approval_id,
            decision_attempt_id=decision_attempt_id,
        )

    async def for_dispatch(
        self,
        reference: ProtectedSourceInvocationRef,
        *,
        principal_id: str,
        access_context_hash: str,
        workflow_execution_id: str,
        dispatch_attempt_id: str,
    ) -> SourceBoundDispatchResolution:
        context = self._context(principal_id, access_context_hash)
        try:
            stored = await self._store.load_protected_source_invocation(
                reference_id=reference.reference_id,
                owner_principal_id=principal_id,
                target_kind="subagent_dispatch",
                target_id=workflow_execution_id,
                attempt_id=dispatch_attempt_id,
            )
            decision, text, stamps = await self._decision(stored, reference, context)
        except (LookupError, ValueError):
            decision, text, stamps = SourceAuthorityDecision.RETRYABLE_UNKNOWN, None, ()
        return SourceBoundDispatchResolution(
            decision=decision,
            materialized_text=text,
            stamps=stamps,
            workflow_execution_id=workflow_execution_id,
            dispatch_attempt_id=dispatch_attempt_id,
        )

    async def for_workflow_projection(
        self,
        reference: ProtectedWorkflowStateRef | ProtectedSourceOutputRef,
        *,
        principal_id: str,
        access_context_hash: str,
        workflow_execution_id: str,
        projection_attempt_id: str,
    ) -> SourceBoundWorkflowProjectionResolution:
        context = self._context(principal_id, access_context_hash)
        try:
            stored = (
                await self._store.load_protected_workflow_state(
                    reference_id=reference.reference_id,
                    owner_principal_id=principal_id,
                    workflow_execution_id=workflow_execution_id,
                )
                if isinstance(reference, ProtectedWorkflowStateRef)
                else await self._store.load_protected_source_output(
                    reference_id=reference.reference_id,
                    owner_principal_id=principal_id,
                    target_id=workflow_execution_id,
                    attempt_id=projection_attempt_id,
                    delivery_site="workflow_projection",
                )
            )
            decision, text, stamps = await self._decision(stored, reference, context)
        except (LookupError, ValueError):
            decision, text, stamps = SourceAuthorityDecision.RETRYABLE_UNKNOWN, None, ()
        return SourceBoundWorkflowProjectionResolution(
            decision=decision,
            materialized_text=text,
            stamps=stamps,
            workflow_execution_id=workflow_execution_id,
            projection_attempt_id=projection_attempt_id,
        )

    async def for_evidence_projection(
        self,
        reference: ProtectedSourceOutputRef | ProtectedClaimSliceRef,
        *,
        principal_id: str,
        access_context_hash: str,
        run_id: str,
        evidence_projection_attempt_id: str,
    ) -> SourceBoundEvidenceProjectionResolution:
        """Resolve only the sixth, owner-scoped evidence-projection target.

        The read-attempt identity is carried only in the typed resolution envelope;
        it is never substituted for the durable output/claim target.  Missing or
        malformed storage, an unbound reader, and an unbound/foreign authenticated
        context all collapse to the same retryable-unknown result with no material,
        stamp, locator, or existence detail.
        """

        try:
            context = self._context(principal_id, access_context_hash)
            reader = self._evidence_projection_reader
            if reader is None:
                raise LookupError("evidence projection is unavailable")
            stored = await reader.load_for_evidence_projection(
                reference,
                owner_principal_id=principal_id,
                run_id=run_id,
            )
            decision, text, stamps = await self._decision(stored, reference, context)
        except (LookupError, ValueError, MissingAuthenticatedContextError):
            decision, text, stamps = SourceAuthorityDecision.RETRYABLE_UNKNOWN, None, ()
        return SourceBoundEvidenceProjectionResolution(
            decision=decision,
            materialized_text=text,
            stamps=stamps,
            run_id=run_id,
            evidence_projection_attempt_id=evidence_projection_attempt_id,
        )


class UnavailableSourceBoundResultResolver:
    """Fail-closed resolver used when protected storage is not activated."""

    async def for_model_send(
        self,
        reference: SourceBoundToolResultRef
        | SourceBoundToolResultSetRef
        | ProtectedSourceOutputRef,
        *,
        principal_id: str,
        access_context_hash: str,
        run_id: str,
        model_call_attempt_id: str,
    ) -> SourceBoundModelSendResolution:
        return SourceBoundModelSendResolution(
            decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN,
            materialized_text=None,
            stamps=(),
            run_id=run_id,
            model_call_attempt_id=model_call_attempt_id,
        )

    async def for_direct_result(
        self,
        reference: SourceBoundToolResultRef | SourceBoundToolResultSetRef,
        *,
        principal_id: str,
        access_context_hash: str,
        workflow_execution_id: str,
        publication_attempt_id: str,
    ) -> SourceBoundDirectResolution:
        return SourceBoundDirectResolution(
            decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN,
            materialized_text=None,
            stamps=(),
            workflow_execution_id=workflow_execution_id,
            publication_attempt_id=publication_attempt_id,
        )

    async def for_approval(
        self,
        reference: ProtectedSourceInvocationRef,
        *,
        principal_id: str,
        access_context_hash: str,
        approval_id: str,
        decision_attempt_id: str,
    ) -> SourceBoundApprovalResolution:
        return SourceBoundApprovalResolution(
            decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN,
            materialized_text=None,
            stamps=(),
            approval_id=approval_id,
            decision_attempt_id=decision_attempt_id,
        )

    async def for_approval_stored(
        self,
        stored: StoredSourcePayload,
        reference: ProtectedSourceInvocationRef,
        *,
        principal_id: str,
        access_context_hash: str,
        approval_id: str,
        decision_attempt_id: str,
    ) -> SourceBoundApprovalResolution:
        return SourceBoundApprovalResolution(
            decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN,
            materialized_text=None,
            stamps=(),
            approval_id=approval_id,
            decision_attempt_id=decision_attempt_id,
        )

    async def for_dispatch(
        self,
        reference: ProtectedSourceInvocationRef,
        *,
        principal_id: str,
        access_context_hash: str,
        workflow_execution_id: str,
        dispatch_attempt_id: str,
    ) -> SourceBoundDispatchResolution:
        return SourceBoundDispatchResolution(
            decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN,
            materialized_text=None,
            stamps=(),
            workflow_execution_id=workflow_execution_id,
            dispatch_attempt_id=dispatch_attempt_id,
        )

    async def for_workflow_projection(
        self,
        reference: ProtectedWorkflowStateRef | ProtectedSourceOutputRef,
        *,
        principal_id: str,
        access_context_hash: str,
        workflow_execution_id: str,
        projection_attempt_id: str,
    ) -> SourceBoundWorkflowProjectionResolution:
        return SourceBoundWorkflowProjectionResolution(
            decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN,
            materialized_text=None,
            stamps=(),
            workflow_execution_id=workflow_execution_id,
            projection_attempt_id=projection_attempt_id,
        )

    async def for_evidence_projection(
        self,
        reference: ProtectedSourceOutputRef | ProtectedClaimSliceRef,
        *,
        principal_id: str,
        access_context_hash: str,
        run_id: str,
        evidence_projection_attempt_id: str,
    ) -> SourceBoundEvidenceProjectionResolution:
        del reference, principal_id, access_context_hash
        return SourceBoundEvidenceProjectionResolution(
            decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN,
            materialized_text=None,
            stamps=(),
            run_id=run_id,
            evidence_projection_attempt_id=evidence_projection_attempt_id,
        )


_AUTHENTICATED_CONTEXT: ContextVar[AuthenticatedRequestContext | None] = ContextVar(
    "ne_runtime_authenticated_request_context", default=None
)


@dataclass(frozen=True, slots=True)
class ModelCallBinding:
    """The exact Run and model-call attempt a model-send delivery is bound to."""

    run_id: str
    model_call_attempt_id: str


@dataclass(frozen=True, slots=True)
class RunDeliveryObservation:
    """Content-free complete receipt set observed at real model-send boundaries."""

    run_id: str
    attempt_id: str
    boundary_count: int
    delivery_receipt_ids: tuple[str, ...]
    delivery_set_id: str

    @property
    def complete(self) -> bool:
        return self.boundary_count > 0


def run_delivery_set_id(run_id: str, attempt_id: str, delivery_receipt_ids: Sequence[str]) -> str:
    """One derivation for the exact ordered server-observed delivery set."""

    return hashlib.sha256(
        json.dumps(
            [run_id, attempt_id, list(delivery_receipt_ids)],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class RunDeliveryObserver:
    """Mutable scope-local collector; callers receive only an immutable snapshot."""

    def __init__(self, *, run_id: str, attempt_id: str) -> None:
        self._run_id = _require_id(run_id, "run_id")
        self._attempt_id = _require_id(attempt_id, "attempt_id")
        self._receipt_ids: list[str] = []
        self._boundary_count = 0

    def record_receipt(self, *, run_id: str, attempt_id: str, receipt_id: str) -> None:
        if (run_id, attempt_id) != (self._run_id, self._attempt_id):
            raise MissingTargetBindingError(
                "delivery observer target differs from the resolved Run attempt"
            )
        receipt_id = _require_id(receipt_id, "delivery_receipt_id")
        if receipt_id not in self._receipt_ids:
            self._receipt_ids.append(receipt_id)

    def complete_model_send(self, binding: ModelCallBinding) -> None:
        self._complete(binding.run_id, binding.model_call_attempt_id)

    def complete_direct_publication(self, binding: DirectPublicationBinding) -> None:
        """Complete one deterministic direct-publication boundary."""

        self._complete(binding.workflow_execution_id, binding.publication_attempt_id)

    def _complete(self, run_id: str, attempt_id: str) -> None:
        if (run_id, attempt_id) != (
            self._run_id,
            self._attempt_id,
        ):
            raise MissingTargetBindingError(
                "delivery observer completion differs from the resolved Run attempt"
            )
        self._boundary_count += 1

    def snapshot(self) -> RunDeliveryObservation:
        receipt_ids = tuple(self._receipt_ids)
        return RunDeliveryObservation(
            run_id=self._run_id,
            attempt_id=self._attempt_id,
            boundary_count=self._boundary_count,
            delivery_receipt_ids=receipt_ids,
            delivery_set_id=run_delivery_set_id(self._run_id, self._attempt_id, receipt_ids),
        )


@dataclass(frozen=True, slots=True)
class DirectPublicationBinding:
    """The exact workflow execution and publication attempt a direct result is bound to."""

    workflow_execution_id: str
    publication_attempt_id: str


_MODEL_CALL_BINDING: ContextVar[ModelCallBinding | None] = ContextVar(
    "ne_runtime_protected_model_call_binding", default=None
)
_RUN_DELIVERY_OBSERVER: ContextVar[RunDeliveryObserver | None] = ContextVar(
    "ne_runtime_run_delivery_observer", default=None
)
_DIRECT_PUBLICATION_BINDING: ContextVar[DirectPublicationBinding | None] = ContextVar(
    "ne_runtime_protected_direct_publication_binding", default=None
)
_PROTECTED_TOOL_EVIDENCE_REFS: ContextVar[tuple[str, ...]] = ContextVar(
    "ne_runtime_protected_tool_evidence_refs", default=()
)

#: NE-DELIVERY-PASS-MEMO. One history-delivery pass re-evaluates the SAME
#: authority stamp once per reference that carries it: a run whose history
#: holds N source-bound Tool results over one corpus asks the evaluator the
#: same question N times within one pass, and the cost is O(references x
#: stamps) DB reads per model call — measured as the dominant inter-turn
#: machinery cost of the flagship run (240 stamp evaluations over a
#: 7-request/6-read run, growing quadratically with the turn count because
#: EVERY prior reference is re-resolved on EVERY model call).
#:
#: The memo is scoped to EXACTLY ONE delivery pass — one
#: :meth:`ProtectedSourceResultGuard.materialize_history`, one
#: :meth:`ProtectedSourceResultGuard.reauthorize_history`, or one
#: :meth:`ProtectedSourceResultGuard.reauthorize_for_effect` call — entered by
#: :func:`_stamp_decision_memo_scope` and dropped when the pass returns. It
#: NEVER spans the materialize→reauthorize boundary, so the documented drift
#: re-check between the trim estimate and the actual send still hits the real
#: evaluator, and authority revoked between the two passes still withholds.
#: Outside a pass the ContextVar is ``None`` and behaviour is byte-identical
#: to the unmemoized code (projection, approval and direct-result paths are
#: untouched).
#:
#: The key is the full frozen :class:`SourceAuthorityStamp` (identity,
#: artifact, selected head revision, ACL snapshot hash, contract epoch,
#: receipt id/hash) plus the exact principal id and access-context hash, so
#: two references share an entry only when they carry byte-identical
#: authority facts asked about by the same authenticated identity within the
#: same pass. The value is the :class:`SourceAuthorityDecision` alone —
#: never text, never stamps.
_STAMP_DECISION_MEMO: ContextVar[
    dict[tuple[SourceAuthorityStamp, str, str], SourceAuthorityDecision] | None
] = ContextVar("ne_runtime_stamp_decision_memo", default=None)


@contextmanager
def _stamp_decision_memo_scope() -> Iterator[None]:
    """Open one delivery-pass-scoped stamp-decision memo (see above)."""
    token = _STAMP_DECISION_MEMO.set({})
    try:
        yield
    finally:
        _STAMP_DECISION_MEMO.reset(token)


def _require_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise MissingTargetBindingError(f"{name} must be a non-empty string")
    return value


@contextmanager
def authenticated_request_scope(
    context: AuthenticatedRequestContext,
) -> Iterator[AuthenticatedRequestContext]:
    """Bind the verifier-authored snapshot for the enclosed delayed work.

    Always sets and always resets (the house contextvar pattern in
    :mod:`ne.runtime.correlation`), so the snapshot cannot outlive its request.
    """

    if not isinstance(context, AuthenticatedRequestContext):
        raise MissingAuthenticatedContextError(
            "only the verifier-authored AuthenticatedRequestContext may be bound; "
            "a reconstructed or duck-typed value is refused"
        )
    token = _AUTHENTICATED_CONTEXT.set(context)
    try:
        yield context
    finally:
        _AUTHENTICATED_CONTEXT.reset(token)


def current_authenticated_context() -> AuthenticatedRequestContext | None:
    """Return the bound snapshot, or ``None`` — never a synthesized default."""

    context = _AUTHENTICATED_CONTEXT.get()
    return context if isinstance(context, AuthenticatedRequestContext) else None


def require_authenticated_context() -> AuthenticatedRequestContext:
    """Return the bound snapshot or refuse. The single fail-closed read."""

    context = current_authenticated_context()
    if context is None:
        raise MissingAuthenticatedContextError(
            "no AuthenticatedRequestContext is bound to this unit of work; protected "
            "source material is refused (there is no default authorization)"
        )
    return context


@contextmanager
def model_call_scope(*, run_id: str, model_call_attempt_id: str) -> Iterator[ModelCallBinding]:
    """Bind the Run/model-call-attempt target for the enclosed model send."""

    binding = ModelCallBinding(
        run_id=_require_id(run_id, "run_id"),
        model_call_attempt_id=_require_id(model_call_attempt_id, "model_call_attempt_id"),
    )
    token = _MODEL_CALL_BINDING.set(binding)
    try:
        yield binding
    finally:
        _MODEL_CALL_BINDING.reset(token)


@contextmanager
def run_delivery_observation_scope(
    *, run_id: str, attempt_id: str
) -> Iterator[RunDeliveryObserver]:
    """Bind the only collector accepted by the protected delivery seam."""

    observer = RunDeliveryObserver(run_id=run_id, attempt_id=attempt_id)
    token = _RUN_DELIVERY_OBSERVER.set(observer)
    try:
        yield observer
    finally:
        _RUN_DELIVERY_OBSERVER.reset(token)


def complete_run_delivery_observation() -> None:
    """Mark a typed source-free Run output boundary as completely observed."""

    observer = _current_delivery_observer()
    if observer is None:
        raise MissingTargetBindingError("no Run delivery observer is bound")
    observer.complete_model_send(require_model_call_binding())


def complete_run_direct_publication_observation() -> None:
    """Mark a typed deterministic direct-publication boundary as complete."""

    observer = _current_delivery_observer()
    if observer is None:
        raise MissingTargetBindingError("no Run delivery observer is bound")
    observer.complete_direct_publication(require_direct_publication_binding())


def _current_delivery_observer() -> RunDeliveryObserver | None:
    observer = _RUN_DELIVERY_OBSERVER.get()
    return observer if isinstance(observer, RunDeliveryObserver) else None


def require_model_call_binding() -> ModelCallBinding:
    """Return the bound model-call target or refuse."""

    binding = _MODEL_CALL_BINDING.get()
    if not isinstance(binding, ModelCallBinding):
        raise MissingTargetBindingError(
            "no Run/model-call-attempt target is bound; a protected result cannot be "
            "delivered to an unidentified model call"
        )
    return binding


@contextmanager
def protected_tool_evidence_scope(references: Sequence[str]) -> Iterator[tuple[str, ...]]:
    """Bind exact server-observed Tool-result refs around one governed effect.

    This is an out-of-band carrier, not a Tool argument.  Only canonical encoded
    :class:`SourceBoundToolResultRef` values with retained stamps are admitted, so
    model-authored lookalikes, protected-output refs and stamp-free identities can
    never become approval evidence.  Nested scopes restore their predecessor.
    """

    validated: list[str] = []
    for candidate in references:
        if not isinstance(candidate, str):
            raise MalformedProtectedReferenceError(
                "protected Tool evidence must contain encoded string references"
            )
        reference = decode_protected_reference(candidate)
        if (
            not isinstance(reference, SourceBoundToolResultRef)
            or not reference.stamps
            or encode_protected_reference(reference) != candidate
        ):
            raise MalformedProtectedReferenceError(
                "protected Tool evidence accepts only canonical stamped Tool-result references"
            )
        if candidate in validated:
            raise MalformedProtectedReferenceError(
                "protected Tool evidence references must be unique"
            )
        validated.append(candidate)
    if not validated:
        raise MalformedProtectedReferenceError(
            "protected Tool evidence scope requires at least one exact reference"
        )
    bound = tuple(validated)
    token = _PROTECTED_TOOL_EVIDENCE_REFS.set(bound)
    try:
        yield bound
    finally:
        _PROTECTED_TOOL_EVIDENCE_REFS.reset(token)


def current_protected_tool_evidence_refs() -> tuple[str, ...]:
    """Return the exact ambient protected Tool evidence, or an empty tuple."""

    references = _PROTECTED_TOOL_EVIDENCE_REFS.get()
    return references if isinstance(references, tuple) else ()


@contextmanager
def direct_publication_scope(
    *, workflow_execution_id: str, publication_attempt_id: str
) -> Iterator[DirectPublicationBinding]:
    """Bind the workflow/publication target for an enclosed deterministic delivery."""

    binding = DirectPublicationBinding(
        workflow_execution_id=_require_id(workflow_execution_id, "workflow_execution_id"),
        publication_attempt_id=_require_id(publication_attempt_id, "publication_attempt_id"),
    )
    token = _DIRECT_PUBLICATION_BINDING.set(binding)
    try:
        yield binding
    finally:
        _DIRECT_PUBLICATION_BINDING.reset(token)


def require_direct_publication_binding() -> DirectPublicationBinding:
    """Return the bound direct-publication target or refuse."""

    binding = _DIRECT_PUBLICATION_BINDING.get()
    if not isinstance(binding, DirectPublicationBinding):
        raise MissingTargetBindingError(
            "no workflow/publication target is bound; a protected result cannot be "
            "delivered to an unidentified deterministic consumer"
        )
    return binding


def encode_protected_reference(reference: ProtectedReference) -> str:
    """Render one reference as the opaque string durable history retains.

    The encoding carries identities, revisions and hashes only — the protected
    bytes live in the producer's encrypted row and are never part of this string.
    """

    for kind, model in _REFERENCE_KINDS.items():
        if type(reference) is model:
            body = json.dumps(
                {"kind": kind, "reference": reference.model_dump(mode="json")},
                sort_keys=True,
                separators=(",", ":"),
            )
            token = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
            return f"{PROTECTED_REFERENCE_PREFIX}{token}"
    raise TypeError(f"{type(reference).__name__} is not a deliverable protected reference")


def decode_protected_reference(text: object) -> ProtectedReference | None:
    """Return the reference ``text`` carries, or ``None`` when it carries none.

    ``None`` means "ordinary source-free text"; a string inside the reference
    namespace that does not decode raises rather than being forwarded.
    """

    if not isinstance(text, str) or not text.startswith(PROTECTED_REFERENCE_PREFIX):
        return None
    token = text[len(PROTECTED_REFERENCE_PREFIX) :]
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise MalformedProtectedReferenceError(
            "a protected-reference string did not decode; it is refused rather than "
            "forwarded (server-minted references only)"
        ) from exc
    if not isinstance(payload, dict):
        raise MalformedProtectedReferenceError("a protected reference must decode to an object")
    model = _REFERENCE_KINDS.get(payload.get("kind", ""))
    if model is None:
        raise MalformedProtectedReferenceError("unknown protected-reference kind")
    try:
        return model.model_validate(payload.get("reference"))
    except ValidationError as exc:
        raise MalformedProtectedReferenceError(
            "a protected reference failed contract validation"
        ) from exc


@dataclass(frozen=True, slots=True)
class MaterializedSourceResult:
    """One delivery decision and the exact text this attempt may carry.

    ``stamps`` is non-empty only for an authorized delivery: a withheld or unknown
    decision carries neither protected material nor a stamp set.
    """

    decision: SourceAuthorityDecision
    text: str
    stamps: tuple[SourceAuthorityStamp, ...] = ()

    @property
    def authorized(self) -> bool:
        return self.decision is SourceAuthorityDecision.AUTHORIZED

    @property
    def retryable(self) -> bool:
        return self.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN


_WITHHELD = MaterializedSourceResult(
    decision=SourceAuthorityDecision.PERMANENT_WITHHELD, text=SOURCE_WITHHELD_TEXT
)
_UNAVAILABLE = MaterializedSourceResult(
    decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN, text=SOURCE_UNAVAILABLE_TEXT
)


@dataclass(frozen=True, slots=True)
class ProtectedDeliverySite:
    """One history message that carries protected references, and its decisions.

    ``original`` keeps the opaque references (it is what stays durable);
    ``delivered`` is the ephemeral substituted copy for this one outbound request.
    """

    original: ModelMessage
    delivered: ModelMessage
    references: tuple[tuple[int, DeliverableReference | None], ...]
    results: tuple[MaterializedSourceResult, ...]


@dataclass(frozen=True, slots=True)
class HistoryDelivery:
    """The ephemeral outbound history plus every decision that produced it."""

    messages: tuple[ModelMessage, ...]
    sites: tuple[ProtectedDeliverySite, ...]

    @property
    def results(self) -> tuple[MaterializedSourceResult, ...]:
        return tuple(result for site in self.sites for result in site.results)

    @property
    def stamps(self) -> tuple[SourceAuthorityStamp, ...]:
        """The exact ordered stamp set this delivery authorized, in delivery order."""

        return tuple(stamp for result in self.results for stamp in result.stamps)

    @property
    def source_bearing(self) -> bool:
        """True iff at least one message carried a protected reference."""

        return bool(self.sites)

    @property
    def fully_authorized(self) -> bool:
        """True iff every protected reference in this delivery authorized.

        Vacuously true for a source-free history — read it together with
        :attr:`source_bearing`, never alone.
        """

        return all(result.authorized for result in self.results)


class ProtectedSourceResultGuard:
    """The ne-runtime delivery guard over an injected ``SourceBoundResultResolver``.

    Holds no keys, no ACL logic and no source client: every decision comes from the
    injected resolver, and the guard's own job is to (a) supply the request's
    authorization, (b) prove the resolution is bound to the target it was asked
    about, and (c) substitute one of the two fixed safe strings whenever the answer
    is anything but ``authorized``.
    """

    def __init__(
        self,
        resolver: SourceBoundResultResolver,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if resolver is None:
            raise TypeError(
                "ProtectedSourceResultGuard requires a non-None SourceBoundResultResolver: "
                "a guard that cannot re-authorize would deliver protected text unchecked"
            )
        self._resolver = resolver
        self._clock: Callable[[], datetime] = clock if clock is not None else _utc_now

    # --- authorization -----------------------------------------------------

    def authorization(self) -> AuthenticatedRequestContext:
        """Return the bound, unexpired snapshot, or raise.

        Absent context and expired context are DIFFERENT typed refusals so a caller
        can tell "never authenticated" from "authorization ran out"; neither one
        yields a usable identity.
        """

        context = require_authenticated_context()
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ProtectedExecutionError(
                "the protected-execution clock must return timezone-aware UTC; an "
                "unanchored clock cannot prove the snapshot is unexpired"
            )
        if now.timestamp() >= context.expires_at:
            raise AuthorizationExpiredError(
                "the bound authorization snapshot is at or past its verified expiry; "
                "a newly authenticated request is required"
            )
        return context

    # --- target-bound materialization --------------------------------------

    async def resolve_for_model_send(
        self, reference: DeliverableReference
    ) -> MaterializedSourceResult:
        """Materialize one reference for the currently bound model-call attempt."""

        context = self.authorization()
        binding = require_model_call_binding()
        resolution = await self._resolver.for_model_send(
            reference,
            principal_id=context.principal_id,
            access_context_hash=context.context_hash,
            run_id=binding.run_id,
            model_call_attempt_id=binding.model_call_attempt_id,
        )
        bound = (
            getattr(resolution, "run_id", None) == binding.run_id
            and getattr(resolution, "model_call_attempt_id", None) == binding.model_call_attempt_id
        )
        return self._materialize(resolution, target_bound=bound)

    async def resolve_for_direct_result(
        self, reference: SourceBoundToolResultRef | SourceBoundToolResultSetRef
    ) -> MaterializedSourceResult:
        """Materialize one Tool result for the bound deterministic publication attempt."""

        context = self.authorization()
        binding = require_direct_publication_binding()
        resolution = await self._resolver.for_direct_result(
            reference,
            principal_id=context.principal_id,
            access_context_hash=context.context_hash,
            workflow_execution_id=binding.workflow_execution_id,
            publication_attempt_id=binding.publication_attempt_id,
        )
        bound = (
            getattr(resolution, "workflow_execution_id", None) == binding.workflow_execution_id
            and getattr(resolution, "publication_attempt_id", None)
            == binding.publication_attempt_id
        )
        return self._materialize(resolution, target_bound=bound)

    async def resolve_for_approval(
        self,
        reference: ProtectedSourceInvocationRef,
        *,
        approval_id: str,
        decision_attempt_id: str,
    ) -> MaterializedSourceResult:
        """Materialize one approval invocation for an exact approval/decision attempt."""

        context = self.authorization()
        approval_id = _require_id(approval_id, "approval_id")
        decision_attempt_id = _require_id(decision_attempt_id, "decision_attempt_id")
        resolution = await self._resolver.for_approval(
            reference,
            principal_id=context.principal_id,
            access_context_hash=context.context_hash,
            approval_id=approval_id,
            decision_attempt_id=decision_attempt_id,
        )
        bound = (
            getattr(resolution, "approval_id", None) == approval_id
            and getattr(resolution, "decision_attempt_id", None) == decision_attempt_id
        )
        return self._materialize(resolution, target_bound=bound)

    async def resolve_stored_for_approval(
        self,
        stored: StoredSourcePayload,
        reference: ProtectedSourceInvocationRef,
        *,
        approval_id: str,
        decision_attempt_id: str,
    ) -> MaterializedSourceResult:
        """Stamp-evaluate an already-loaded, integrity-verified approval invocation.

        The Run-approval checkpoint coordinator is the ONLY caller. It has
        already loaded the protected invocation under its park-time key — owner
        = the RUN owner, binding prefix ``(run_id, attempt_id)``, target hash,
        AAD-bound decryption and stamp-set hash all enforced by the store — and
        hash-compared the plaintext against the durable approval join. What this
        seam adds is exactly the guard's three jobs: the CURRENT bound
        authorization (the approver at decision time, the run owner at resume
        time — expiry enforced), the target-bound echo proof, and the safe-string
        substitution for every non-authorized outcome. Every stamp in the stored
        payload is re-evaluated against that live context; revoked authority
        between park and decision still withholds. This method never re-loads by
        a decision-time key: such a key is computed at decision time and can
        never match a park-time row, which is the structural 409 this seam
        exists to remove.
        """

        context = self.authorization()
        approval_id = _require_id(approval_id, "approval_id")
        decision_attempt_id = _require_id(decision_attempt_id, "decision_attempt_id")
        evaluate = getattr(self._resolver, "for_approval_stored", None)
        if evaluate is None:
            # Fail closed: a resolver without the stored-payload evaluation seam
            # yields the non-committal unavailable substitution, never text.
            return _UNAVAILABLE
        resolution = await evaluate(
            stored,
            reference,
            principal_id=context.principal_id,
            access_context_hash=context.context_hash,
            approval_id=approval_id,
            decision_attempt_id=decision_attempt_id,
        )
        bound = (
            getattr(resolution, "approval_id", None) == approval_id
            and getattr(resolution, "decision_attempt_id", None) == decision_attempt_id
        )
        return self._materialize(resolution, target_bound=bound)

    async def reauthorize_for_effect(
        self, references: Sequence[SourceBoundToolResultRef]
    ) -> SourceAuthorityDecision:
        """Recheck exact model-visible evidence immediately before a Tool effect.

        The existing model-send resolver is the frozen target-bound authority seam,
        so the effect check deliberately reuses it under the same Run/model-attempt
        binding.  It suppresses only the delivery observer while rechecking: the
        historical successful model-delivery receipt remains immutable and this
        non-disclosure check must not mint a second ``tool_result_served`` event.

        The aggregate preserves the platform's three-valued ordering.  Any Unknown
        makes the set Unknown; otherwise any permanent withholding withholds the
        set; only exact authorized stamp echoes authorize the effect.
        """

        if not references:
            raise ValueError("effect reauthorization requires protected Tool evidence")
        results: list[MaterializedSourceResult] = []
        token = _RUN_DELIVERY_OBSERVER.set(None)
        try:
            with _stamp_decision_memo_scope():
                for reference in references:
                    if not isinstance(reference, SourceBoundToolResultRef) or not reference.stamps:
                        raise TypeError(
                            "effect reauthorization accepts only stamped Tool-result references"
                        )
                    result = await self.resolve_for_model_send(reference)
                    if result.authorized and result.stamps != reference.stamps:
                        result = _UNAVAILABLE
                    results.append(result)
        finally:
            _RUN_DELIVERY_OBSERVER.reset(token)
        if any(result.retryable for result in results):
            return SourceAuthorityDecision.RETRYABLE_UNKNOWN
        if any(result.decision is SourceAuthorityDecision.PERMANENT_WITHHELD for result in results):
            return SourceAuthorityDecision.PERMANENT_WITHHELD
        return SourceAuthorityDecision.AUTHORIZED

    def _materialize(self, resolution: object, *, target_bound: bool) -> MaterializedSourceResult:
        decision = getattr(resolution, "decision", None)
        if not target_bound:
            # The resolver answered about a different target than it was asked
            # about. That is an integrity failure, not an authority fact, so it is
            # never permanent and never discloses.
            return _UNAVAILABLE
        if decision is SourceAuthorityDecision.AUTHORIZED:
            text = getattr(resolution, "materialized_text", None)
            stamps = tuple(getattr(resolution, "stamps", ()))
            if not isinstance(text, str) or not stamps:
                return _UNAVAILABLE
            return MaterializedSourceResult(
                decision=SourceAuthorityDecision.AUTHORIZED, text=text, stamps=stamps
            )
        if decision is SourceAuthorityDecision.PERMANENT_WITHHELD:
            return _WITHHELD
        # RETRYABLE_UNKNOWN and anything unrecognized share the one non-committal
        # outcome: an unreadable decision is never upgraded to a permanent denial.
        return _UNAVAILABLE

    # --- history delivery ---------------------------------------------------

    async def materialize_model_send_text(self, text: object) -> MaterializedSourceResult | None:
        """Resolve the reference ``text`` carries for the bound model call.

        ``None`` means the text is source-free and passes through untouched; a
        malformed reference resolves to the unavailable substitution, never to
        ``None`` and never to the original string.
        """

        occupies, reference = _decode_entry(text)
        if not occupies:
            return None
        return await self._resolve_reference(reference)

    async def materialize_direct_text(self, text: object) -> MaterializedSourceResult | None:
        """Resolve the reference ``text`` carries for the bound publication attempt.

        Only a Tool-result reference is directly consumable; a protected-output
        reference reaching this seam is the wrong target kind and is withheld.
        """

        occupies, reference = _decode_entry(text)
        if not occupies:
            return None
        if not isinstance(reference, SourceBoundToolResultRef | SourceBoundToolResultSetRef):
            return _UNAVAILABLE
        return await self.resolve_for_direct_result(reference)

    async def materialize_history(self, messages: Sequence[ModelMessage]) -> HistoryDelivery:
        """Build the ephemeral outbound history for the bound model call.

        Runs BEFORE trimming and request assembly so the trim estimate is taken over
        the bytes actually sent; ``messages`` itself is never mutated.

        This pass is PROVISIONAL: the run delivery observer is suppressed for its
        duration, so no delivery receipt is claimed and nothing reaches the run
        audit here. A message the trim later drops was never model-visible, and a
        decision that drifts before the send is not this pass's to record — the
        ONE durable claim per reference happens in :meth:`reauthorize_history`,
        over the exact final outbound set, immediately before the send.
        """

        delivered: list[ModelMessage] = []
        sites: list[ProtectedDeliverySite] = []
        token = _RUN_DELIVERY_OBSERVER.set(None)
        try:
            with _stamp_decision_memo_scope():
                for message in messages:
                    references = _references_in(message)
                    if not references:
                        delivered.append(message)
                        continue
                    site = await self._site(message, references)
                    sites.append(site)
                    delivered.append(site.delivered)
        finally:
            _RUN_DELIVERY_OBSERVER.reset(token)
        return HistoryDelivery(messages=tuple(delivered), sites=tuple(sites))

    async def reauthorize_history(
        self, messages: Sequence[ModelMessage], delivery: HistoryDelivery
    ) -> HistoryDelivery:
        """Re-resolve, immediately before the send, every reference still in ``messages``.

        ``messages`` is the possibly-trimmed output of :meth:`materialize_history`;
        a message the trim dropped is not re-resolved, and a reference that drifted
        between the two passes is substituted with its safe string here.

        This is the pass that CLAIMS: :meth:`materialize_history` suppresses the
        delivery observer, so the receipts and audit rows minted through the
        resolver here describe exactly what this send delivered (or withheld) —
        never a provisional pre-trim decision.
        """

        sites_by_delivered = {id(site.delivered): site for site in delivery.sites}
        delivered: list[ModelMessage] = []
        sites: list[ProtectedDeliverySite] = []
        with _stamp_decision_memo_scope():
            for message in messages:
                previous = sites_by_delivered.get(id(message))
                if previous is None:
                    delivered.append(message)
                    continue
                site = await self._site(previous.original, previous.references)
                sites.append(site)
                delivered.append(site.delivered)
        observed = HistoryDelivery(messages=tuple(delivered), sites=tuple(sites))
        observer = _current_delivery_observer()
        if observer is not None:
            observer.complete_model_send(require_model_call_binding())
        return observed

    async def _site(
        self,
        message: ModelMessage,
        references: tuple[tuple[int, DeliverableReference | None], ...],
    ) -> ProtectedDeliverySite:
        results = tuple([await self._resolve_reference(entry) for _, entry in references])
        return ProtectedDeliverySite(
            original=message,
            delivered=_substitute(message, references, results),
            references=references,
            results=results,
        )

    async def _resolve_reference(
        self, reference: DeliverableReference | None
    ) -> MaterializedSourceResult:
        # ``None`` here means the namespace was occupied by something that did not
        # decode — there is nothing to authorize, so it can only be withheld.
        if reference is None:
            return _UNAVAILABLE
        return await self.resolve_for_model_send(reference)


#: Retention caps (NE-SPAN-BIND). A single delivered rendering larger than the
#: per-entry cap is not retained (its source simply cannot server-bind a span),
#: and once the total cap is reached no NEW source is retained. Both are honest
#: refusals to grow, never truncations: a truncated buffer could miss a span
#: that IS verbatim in the served text, which would manufacture a false
#: "no common span" over bytes the run really served.
_MAX_RETAINED_TEXT_CHARS: Final = 200_000
_MAX_RETAINED_TOTAL_CHARS: Final = 2_000_000


class ServedTextRetention:
    """Run-scoped, memory-only retention of AUTHORIZED Tool-result text (NE-SPAN-BIND).

    Why this exists. The claim contract asks for one string that is a span of the
    final answer AND verbatim in a served chunk. Both halves are server-checkable,
    but the server used to hold the two texts at different times: the answer at
    validation time, the authorized chunk text only EPHEMERALLY inside one
    delivery (:meth:`ProtectedSourceResultGuard.materialize_history`) — released
    the moment the request was assembled. So the model was made to carry the
    chunk's words across the gap, and measurement showed it essentially never
    could (21 real claims: zero satisfied both halves). This buffer closes the
    gap: it keeps, for the life of ONE run, the text the guard actually delivered
    to the model, keyed by the same ``reference_id`` the delivery names on its
    ``[source-id: ...]`` line, so the runner can select the span itself.

    SECURITY BOUNDARY — this is authorized customer source text held in memory:

    * It lives ONLY inside one ``run_model_planned_agent`` call: created there,
      referenced only by that call's own wrapper guard and validator closure, and
      explicitly ``clear()``-ed in that call's ``finally``. Nothing module-level,
      nothing contextvar, nothing durable holds it.
    * It is never serialized. ``__repr__`` is content-free by construction, so
      even an accidental interpolation into an exception message, log line or
      audit ``detail`` cannot carry a byte of retained text.
    * Its reader (`ne.runtime.runner._server_bound_claims`) publishes only what
      the claim pipeline already publishes: byte offsets into the ANSWER, a hash
      of ANSWER bytes, and the served reference identity. Retained text decides
      WHICH answer bytes bind; it never itself crosses into an event, row,
      failure reason or correction.
    * Only text the model itself ALREADY received is retained: the recording
      site is the authorized model-send delivery, after every authority check
      this platform runs for that delivery. Retention therefore widens no
      disclosure — it remembers a disclosure that lawfully happened.

    A ``reference_id`` that arrives twice with DIFFERENT text is poisoned and
    withdrawn: binding "verbatim in the served chunk" against whichever text won
    a race would be the silent-wrong-answer the ambiguous-id rule already
    refuses.
    """

    __slots__ = ("_poisoned", "_texts", "_total_chars")

    def __init__(self) -> None:
        self._texts: dict[str, str] = {}
        self._poisoned: set[str] = set()
        self._total_chars = 0

    def record(self, reference_id: str, text: str) -> None:
        """Retain one authorized delivery, within the caps."""
        if reference_id in self._poisoned:
            return
        existing = self._texts.get(reference_id)
        if existing is not None:
            if existing != text:
                self._poisoned.add(reference_id)
                self._total_chars -= len(existing)
                del self._texts[reference_id]
            return
        if len(text) > _MAX_RETAINED_TEXT_CHARS:
            return
        if self._total_chars + len(text) > _MAX_RETAINED_TOTAL_CHARS:
            return
        self._texts[reference_id] = text
        self._total_chars += len(text)

    def text_for(self, reference_id: str) -> str | None:
        """The retained authorized text, or ``None`` if absent or poisoned."""
        return self._texts.get(reference_id)

    def clear(self) -> None:
        self._texts.clear()
        self._poisoned.clear()
        self._total_chars = 0

    def __len__(self) -> int:
        return len(self._texts)

    def __repr__(self) -> str:  # content-free ON PURPOSE — see the class docstring
        return f"<ServedTextRetention entries={len(self._texts)} chars={self._total_chars}>"


class ClaimRetainingSourceGuard(ProtectedSourceResultGuard):
    """A per-run view over the platform guard that RETAINS what it delivers.

    The platform constructs ONE :class:`ProtectedSourceResultGuard` for every run
    (``ne.api.platform``), so retention cannot live on the guard itself — text
    from one run would survive into the next and never be released. This subclass
    is the run-scoped seam: same injected resolver, same clock, byte-identical
    delivery behaviour; the single addition is that an AUTHORIZED Tool-result
    materialization is also recorded into this run's :class:`ServedTextRetention`
    under its ``reference_id`` — exactly the id and exactly the text that
    :func:`_delivered_text` puts in front of the model, so the retention
    vocabulary can never diverge from what a claim may cite.

    ``reauthorize_for_effect`` routes through :meth:`resolve_for_model_send` too;
    re-recording there is an idempotent no-op (same id, same text) or a poisoning
    (drifted text), both correct.
    """

    def __init__(self, inner: ProtectedSourceResultGuard, retention: ServedTextRetention) -> None:
        super().__init__(inner._resolver, clock=inner._clock)
        self._retention = retention

    async def resolve_for_model_send(
        self, reference: DeliverableReference
    ) -> MaterializedSourceResult:
        result = await super().resolve_for_model_send(reference)
        if result.authorized and isinstance(reference, SourceBoundToolResultRef):
            self._retention.record(reference.reference_id, result.text)
        return result


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _decode_entry(text: object) -> tuple[bool, DeliverableReference | None]:
    """Return ``(occupies the reference namespace, decoded deliverable or None)``.

    ``(False, None)`` is ordinary source-free text. ``(True, None)`` is a delivery
    site with nothing to resolve — a malformed reference, or a well-formed one whose
    kind is not deliverable at this seam (an approval invocation reaching a model
    send). Both can only be substituted, never forwarded.
    """

    try:
        reference = decode_protected_reference(text)
    except MalformedProtectedReferenceError:
        return True, None
    if reference is None:
        return False, None
    if isinstance(reference, ProtectedSourceInvocationRef):
        return True, None
    return True, reference


def decode_invocation_reference(text: object) -> ProtectedSourceInvocationRef | None:
    """Return the approval invocation reference ``text`` carries, or ``None``.

    A well-formed reference of any OTHER kind is refused here for the same reason a
    model send refuses an invocation: a target-bound reference is only ever
    materialized through its own target's resolver method.
    """

    reference = decode_protected_reference(text)
    if reference is None:
        return None
    if not isinstance(reference, ProtectedSourceInvocationRef):
        raise MalformedProtectedReferenceError(
            "an approval seam accepts only a protected invocation reference"
        )
    return reference


def _references_in(
    message: ModelMessage,
) -> tuple[tuple[int, DeliverableReference | None], ...]:
    """Index every part of ``message`` that occupies the protected-reference namespace."""

    if isinstance(message, ModelRequest):
        carriers: tuple[type, ...] = (ToolReturnPart,)
    elif isinstance(message, ModelResponse):
        carriers = (TextPart,)
    else:  # pragma: no cover - ModelMessage is exactly these two today
        return ()
    found: list[tuple[int, DeliverableReference | None]] = []
    for index, part in enumerate(message.parts):
        if not isinstance(part, carriers):
            continue
        occupies, reference = _decode_entry(getattr(part, "content", None))
        if occupies:
            found.append((index, reference))
    return tuple(found)


#: The line that names a delivered Tool result, so a claim can cite it.
#:
#: NE-REF-VOCAB. The opaque reference is a SERVER identity and never crosses this
#: seam — substituting it away is the whole mechanism. But the claim contract
#: asks the model to name the source an answer rests on, and before this the only
#: vocabulary it had for that was a string it could not see. It invented one, was
#: refused, invented another, and the run died naming neither problem.
#:
#: What crosses is the reference's own ``reference_id`` and nothing else. It is
#: ``uuid.uuid4()`` (``ne.runtime.run_store._id``, via ``pending_result_id``): a
#: random surrogate key that encodes no content, no source identity, no ACL and
#: no withheld material. It is delivered ONLY beside text the model is already
#: authorized to read, so it discloses nothing the delivery had not already
#: disclosed — it makes that same material nameable.
_SOURCE_ID_LINE = "[source-id: {source_id}]"


def _delivered_text(
    reference: DeliverableReference | None, result: MaterializedSourceResult
) -> str:
    """The authorized text, preceded by the id a claim may cite it under.

    The id is emitted ONLY for an authorized Tool result. A withheld or unknown
    decision carries no material, so there is nothing to cite and naming it would
    invite a claim over content that was never delivered. A non-Tool-result
    reference is not claimable either, and is left exactly as it was.
    """
    if not result.authorized or not isinstance(reference, SourceBoundToolResultRef):
        return result.text
    return f"{_SOURCE_ID_LINE.format(source_id=reference.reference_id)}\n{result.text}"


def _substitute(
    message: ModelMessage,
    references: Sequence[tuple[int, DeliverableReference | None]],
    results: Sequence[MaterializedSourceResult],
) -> ModelMessage:
    parts = list(message.parts)
    for (index, reference), result in zip(references, results, strict=True):
        parts[index] = dataclasses.replace(parts[index], content=_delivered_text(reference, result))
    return dataclasses.replace(message, parts=parts)
