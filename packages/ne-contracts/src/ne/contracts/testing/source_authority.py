# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Conformance-kit bases for the source-authority contract seams.

Bindings subclass a kit with a ``Test``-prefixed class and provide fresh real adapters.
The bases deliberately use only stdlib assertions so importing ``ne.contracts.testing``
does not pull the optional pytest dependency into runtime plugin discovery.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import re
from collections.abc import Mapping
from datetime import UTC
from typing import ClassVar

from ne.contracts.source_authority import (
    AuthorizedArtifactPageStatus,
    AuthorizedContinuationState,
    AuthorizedContinuationTokenCodec,
    AuthorizedContinuationTokenLocator,
    CurrentAuthorizedArtifactSelector,
    CurrentSourceAuthorityReader,
    CurrentSourceAuthorityReceipt,
    CurrentSourceAuthorityWriter,
    ImmutableKnowledgeArtifactWrite,
    ProtectedClaimSliceRef,
    ProtectedSourceExecutionWriter,
    ProtectedSourceInvocationRef,
    ProtectedSourceOutputRef,
    ProtectedSourceOutputWriter,
    ProtectedWorkflowStateRef,
    SourceAdmissionFence,
    SourceAuthorityCandidate,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceBoundEvidenceProjectionResolver,
    SourceBoundModelSendResolution,
    SourceBoundResultResolver,
    SourceBoundToolResultRef,
    SourceContentArtifactIdentity,
    SourceReconciliationClaim,
    SourceReconciliationScope,
)

__all__ = [
    "AuthorizedContinuationTokenCodecContractKit",
    "CurrentAuthorizedArtifactSelectorContractKit",
    "CurrentSourceAuthorityReaderContractKit",
    "CurrentSourceAuthorityWriterContractKit",
    "ImmutableKnowledgeArtifactWriteContractKit",
    "ProtectedSourceExecutionWriterContractKit",
    "ProtectedSourceOutputWriterContractKit",
    "SourceBoundEvidenceProjectionResolverContractKit",
    "SourceBoundResultResolverContractKit",
]


def _assert_receipt_matches_stamp(
    receipt: CurrentSourceAuthorityReceipt,
    stamp: SourceAuthorityStamp,
) -> None:
    assert receipt.source_identity == stamp.source_identity
    assert receipt.artifact_identity == stamp.artifact_identity
    assert receipt.source_revision == stamp.selected_head_revision
    assert receipt.acl_snapshot_hash == stamp.acl_snapshot_hash
    assert receipt.source_contract == stamp.source_contract
    assert receipt.contract_epoch == stamp.contract_epoch
    assert receipt.receipt_id == stamp.authority_receipt_id
    assert receipt.receipt_hash == stamp.authority_receipt_hash


def _resolver_target_names(protocol: type[object]) -> set[str]:
    """Derive a resolver Protocol's target set from the Protocol itself.

    Read from the class hierarchy rather than a copied list, so a target added to
    ``SourceBoundResultResolver`` or to an extension of it is immediately visible to
    every kit that claims to cover it.
    """

    names: set[str] = set()
    for klass in protocol.__mro__:
        for name, member in vars(klass).items():
            if name.startswith("_"):
                continue
            if inspect.isfunction(member) or inspect.iscoroutinefunction(member):
                names.add(name)
    return names


def _assert_model_send_stamps(
    result: SourceBoundModelSendResolution,
    stamps: tuple[SourceAuthorityStamp, ...],
) -> None:
    assert result.decision is SourceAuthorityDecision.AUTHORIZED
    assert result.stamps == stamps


class CurrentSourceAuthorityReaderContractKit:
    def create_reader(self) -> CurrentSourceAuthorityReader:
        raise NotImplementedError

    def authorized_stamp(self) -> SourceAuthorityStamp:
        raise NotImplementedError

    def unknown_stamp(self) -> SourceAuthorityStamp:
        raise NotImplementedError

    def foreign_same_shape_stamp(self) -> SourceAuthorityStamp:
        """Return a valid-looking stamp for a different source row."""
        raise NotImplementedError

    def test_exact_current_stamp_authorizes(self) -> None:
        async def scenario() -> None:
            stamp = self.authorized_stamp()
            result = await self.create_reader().resolve(
                stamp,
                principal_id="kit-principal",
                access_context_hash="a" * 64,
            )
            assert result.decision is SourceAuthorityDecision.AUTHORIZED
            receipt = result.current_receipt
            assert receipt is not None
            _assert_receipt_matches_stamp(receipt, stamp)

        asyncio.run(scenario())

    def test_foreign_same_shape_stamp_fails_closed(self) -> None:
        async def scenario() -> None:
            result = await self.create_reader().resolve(
                self.foreign_same_shape_stamp(),
                principal_id="kit-principal",
                access_context_hash="a" * 64,
            )
            assert result.decision is not SourceAuthorityDecision.AUTHORIZED

        asyncio.run(scenario())

    def test_unknown_stamp_fails_closed(self) -> None:
        async def scenario() -> None:
            result = await self.create_reader().resolve(
                self.unknown_stamp(),
                principal_id="kit-principal",
                access_context_hash="a" * 64,
            )
            assert result.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN
            assert result.current_receipt is None

        asyncio.run(scenario())


class CurrentSourceAuthorityWriterContractKit:
    def create_writer(self) -> CurrentSourceAuthorityWriter:
        raise NotImplementedError

    def scope(self) -> SourceReconciliationScope:
        raise NotImplementedError

    def fence(self, claim: SourceReconciliationClaim) -> SourceAdmissionFence:
        raise NotImplementedError

    def candidate(self) -> SourceAuthorityCandidate:
        raise NotImplementedError

    def test_claim_is_scope_bound_and_epoch_positive(self) -> None:
        async def scenario() -> None:
            claim = await self.create_writer().claim(
                self.scope(), expected_head_revision=None, lease_seconds=30
            )
            assert claim.scope == self.scope()
            assert claim.reconciliation_epoch >= 1
            assert claim.fencing_token

        asyncio.run(scenario())

    def test_renew_keeps_scope_epoch_and_fence(self) -> None:
        async def scenario() -> None:
            writer = self.create_writer()
            claim = await writer.claim(self.scope(), expected_head_revision=None, lease_seconds=30)
            renewed = await writer.renew(claim, lease_seconds=60)
            assert renewed.scope == claim.scope
            assert renewed.reconciliation_epoch == claim.reconciliation_epoch
            assert renewed.fencing_token == claim.fencing_token
            assert renewed.lease_expires_at >= claim.lease_expires_at

        asyncio.run(scenario())

    def test_finalize_returns_exact_candidate_binding(self) -> None:
        async def scenario() -> None:
            writer = self.create_writer()
            claim = await writer.claim(self.scope(), expected_head_revision=None, lease_seconds=30)
            candidate = self.candidate()
            receipt = await writer.finalize(claim, self.fence(claim), candidate)
            assert receipt.source_identity == candidate.source_identity
            assert receipt.artifact_identity == candidate.artifact_identity
            assert receipt.source_revision == candidate.source_revision
            assert receipt.source_contract == candidate.source_contract
            assert receipt.contract_epoch == candidate.contract_epoch
            assert receipt.acl_snapshot_hash == candidate.acl_snapshot_hash
            assert receipt.state == candidate.state

        asyncio.run(scenario())


class ImmutableKnowledgeArtifactWriteContractKit:
    def create_writer(self) -> ImmutableKnowledgeArtifactWrite:
        raise NotImplementedError

    def artifact(self) -> SourceContentArtifactIdentity:
        raise NotImplementedError

    def canonical_bytes(self) -> bytes:
        raise NotImplementedError

    async def create_failure(
        self,
        writer: ImmutableKnowledgeArtifactWrite,
        identity: SourceContentArtifactIdentity,
        canonical_bytes: bytes,
    ) -> bool:
        """Return true only when create/reuse fails with the binding's safe error."""
        raise NotImplementedError

    def test_create_then_exact_reuse_does_not_transfer_ownership(self) -> None:
        async def scenario() -> None:
            writer = self.create_writer()
            created = await writer.create_if_absent(self.artifact(), self.canonical_bytes())
            reused = await writer.create_if_absent(self.artifact(), self.canonical_bytes())
            assert created.created is True and created.creator_token
            assert reused.created is False and reused.creator_token is None
            assert reused.artifact_identity == created.artifact_identity
            assert reused.inflight_reference_id != created.inflight_reference_id

        asyncio.run(scenario())

    def test_reuser_cannot_release_creator_artifact(self) -> None:
        async def scenario() -> None:
            writer = self.create_writer()
            await writer.create_if_absent(self.artifact(), self.canonical_bytes())
            reused = await writer.create_if_absent(self.artifact(), self.canonical_bytes())
            assert await writer.release_creator(reused, expected_current_head=None) is False

        asyncio.run(scenario())

    def test_same_identity_with_different_bytes_fails_closed(self) -> None:
        async def scenario() -> None:
            writer = self.create_writer()
            await writer.create_if_absent(self.artifact(), self.canonical_bytes())
            assert await self.create_failure(
                writer,
                self.artifact(),
                self.canonical_bytes() + b"tampered",
            )

        asyncio.run(scenario())


class CurrentAuthorizedArtifactSelectorContractKit:
    def create_selector(self) -> CurrentAuthorizedArtifactSelector:
        raise NotImplementedError

    def valid_continuation_token(self) -> str:
        """Return a current token for the kit's fixed principal/request arguments."""
        raise NotImplementedError

    async def invalid_continuation_outcome(
        self,
        selector: CurrentAuthorizedArtifactSelector,
        token: str,
    ) -> str:
        """Return one public safe-outcome fingerprint only after proving no page/counts."""
        raise NotImplementedError

    def private_row_lookup_count(self, selector: CurrentAuthorizedArtifactSelector) -> int:
        raise NotImplementedError

    def artifact_read_count(self, selector: CurrentAuthorizedArtifactSelector) -> int:
        raise NotImplementedError

    def first_page_restart_count(self, selector: CurrentAuthorizedArtifactSelector) -> int:
        raise NotImplementedError

    def test_bounded_page_counts_only_disclosed_items(self) -> None:
        async def scenario() -> None:
            page = await self.create_selector().select(
                principal_id="kit-principal",
                access_context_hash="a" * 64,
                request_hash="b" * 64,
                top_k=2,
                scan_limit=3,
            )
            assert page.returned_count == len(page.items)
            assert page.returned_count <= page.scanned_count <= page.scan_limit == 3
            if page.status is not AuthorizedArtifactPageStatus.COMPLETE:
                assert page.continuation_token

        asyncio.run(scenario())

    def test_empty_incomplete_page_is_exhausted_not_complete(self) -> None:
        async def scenario() -> None:
            page = await self.create_selector().select(
                principal_id="kit-no-visible-items",
                access_context_hash="c" * 64,
                request_hash="d" * 64,
                top_k=2,
                scan_limit=1,
            )
            assert page.items == ()
            assert page.status is AuthorizedArtifactPageStatus.EXHAUSTED
            assert page.continuation_token

        asyncio.run(scenario())

    def test_identical_first_page_retry_is_byte_stable(self) -> None:
        async def scenario() -> None:
            selector = self.create_selector()
            arguments = {
                "principal_id": "kit-principal",
                "access_context_hash": "a" * 64,
                "request_hash": "b" * 64,
                "top_k": 2,
                "scan_limit": 3,
            }
            first = await selector.select(**arguments)
            second = await selector.select(**arguments)
            assert second == first

        asyncio.run(scenario())

    def test_valid_continuation_claim_loads_a_bounded_page(self) -> None:
        async def scenario() -> None:
            page = await self.create_selector().select(
                principal_id="kit-principal",
                access_context_hash="a" * 64,
                request_hash="b" * 64,
                top_k=2,
                scan_limit=3,
                continuation_token=self.valid_continuation_token(),
            )
            assert page.returned_count == len(page.items)
            assert page.returned_count <= page.scanned_count <= page.scan_limit == 3

        asyncio.run(scenario())

    def test_invalid_continuations_share_one_no_page_outcome(self) -> None:
        async def scenario() -> None:
            token = self.valid_continuation_token()
            if token.count(".") != 1:
                raise AssertionError("valid continuation must contain one key-id separator")
            key_id, mac = token.split(".")
            assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key_id)
            assert re.fullmatch(r"[A-Za-z0-9_-]{43}", mac)
            raw_mac = base64.urlsafe_b64decode(mac + "=")
            assert len(raw_mac) == 32
            assert base64.urlsafe_b64encode(raw_mac).rstrip(b"=").decode() == mac

            def encoded(raw: bytes) -> str:
                return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

            tampered = bytes((raw_mac[0] ^ 1, *raw_mac[1:]))
            guessed = bytes(range(32))
            if guessed == raw_mac:
                guessed = bytes(reversed(range(32)))
            mismatched_key_id = "selector_mismatch" if key_id != "selector_mismatch" else "other"
            probes = (
                (f"{key_id}.{encoded(tampered)}", 1),
                (f"{key_id}.{encoded(guessed)}", 1),
                (f"{mismatched_key_id}.{mac}", 0),
            )
            assert len({candidate for candidate, _ in probes}) == len(probes)
            outcomes: list[str] = []
            for candidate, expected_row_lookups in probes:
                selector = self.create_selector()
                outcome = await self.invalid_continuation_outcome(selector, candidate)
                assert outcome
                outcomes.append(outcome)
                assert self.private_row_lookup_count(selector) == expected_row_lookups
                assert self.artifact_read_count(selector) == 0
                assert self.first_page_restart_count(selector) == 0
            assert len(set(outcomes)) == 1

        asyncio.run(scenario())


class SourceBoundResultResolverContractKit:
    #: The resolver Protocol this kit certifies.  An extension kit overrides it.
    resolver_protocol: ClassVar[type[object]] = SourceBoundResultResolver

    #: Resolver target -> the kit scenario that exercises it.  Checked against the
    #: Protocol itself by :meth:`test_every_resolver_target_is_exercised_by_this_kit`,
    #: so a later target cannot reach a binding without a scenario behind it.
    target_coverage: ClassVar[Mapping[str, str]] = {
        "for_model_send": "test_model_send_binds_exact_target",
        "for_direct_result": "test_target_specific_methods_bind_their_exact_consumption_attempt",
        "for_approval": "test_target_specific_methods_bind_their_exact_consumption_attempt",
        "for_dispatch": "test_target_specific_methods_bind_their_exact_consumption_attempt",
        "for_workflow_projection": (
            "test_target_specific_methods_bind_their_exact_consumption_attempt"
        ),
    }

    def create_resolver(self) -> SourceBoundResultResolver:
        raise NotImplementedError

    def reference(self) -> SourceBoundToolResultRef:
        raise NotImplementedError

    def output_reference(self) -> ProtectedSourceOutputRef:
        raise NotImplementedError

    def invocation_reference(self) -> ProtectedSourceInvocationRef:
        raise NotImplementedError

    def workflow_reference(self) -> ProtectedWorkflowStateRef:
        raise NotImplementedError

    def foreign_same_shape_reference(self) -> SourceBoundToolResultRef:
        """Return a valid-looking reference for another protected row."""
        raise NotImplementedError

    def test_every_resolver_target_is_exercised_by_this_kit(self) -> None:
        declared = _resolver_target_names(self.resolver_protocol)
        assert declared, "resolver Protocol declares no targets"
        assert declared == set(self.target_coverage), (
            "resolver targets and kit coverage disagree: "
            f"{sorted(declared ^ set(self.target_coverage))}"
        )
        for target, scenario in self.target_coverage.items():
            assert callable(getattr(self, scenario, None)), (
                f"{target} names a missing kit scenario {scenario!r}"
            )

    def test_model_send_binds_exact_target(self) -> None:
        async def scenario() -> None:
            result = await self.create_resolver().for_model_send(
                self.reference(),
                principal_id="kit-principal",
                access_context_hash="a" * 64,
                run_id="kit-run",
                model_call_attempt_id="kit-call",
            )
            _assert_model_send_stamps(result, self.reference().stamps)
            assert result.run_id == "kit-run"
            assert result.model_call_attempt_id == "kit-call"
            output_result = await self.create_resolver().for_model_send(
                self.output_reference(),
                principal_id="kit-principal",
                access_context_hash="a" * 64,
                run_id="kit-run-output",
                model_call_attempt_id="kit-call-output",
            )
            _assert_model_send_stamps(output_result, self.output_reference().stamps)

        asyncio.run(scenario())

    def test_foreign_principal_never_materializes(self) -> None:
        async def scenario() -> None:
            result = await self.create_resolver().for_model_send(
                self.reference(),
                principal_id="kit-foreign-principal",
                access_context_hash="f" * 64,
                run_id="kit-run",
                model_call_attempt_id="kit-call",
            )
            assert result.decision is not SourceAuthorityDecision.AUTHORIZED
            assert result.materialized_text is None
            assert result.stamps == ()

        asyncio.run(scenario())

    def test_foreign_same_shape_reference_never_materializes(self) -> None:
        async def scenario() -> None:
            result = await self.create_resolver().for_model_send(
                self.foreign_same_shape_reference(),
                principal_id="kit-principal",
                access_context_hash="a" * 64,
                run_id="kit-run",
                model_call_attempt_id="kit-call",
            )
            assert result.decision is not SourceAuthorityDecision.AUTHORIZED
            assert result.materialized_text is None
            assert result.stamps == ()

        asyncio.run(scenario())

    def test_target_specific_methods_bind_their_exact_consumption_attempt(self) -> None:
        async def scenario() -> None:
            resolver = self.create_resolver()
            direct = await resolver.for_direct_result(
                self.reference(),
                principal_id="kit-principal",
                access_context_hash="a" * 64,
                workflow_execution_id="kit-workflow",
                publication_attempt_id="kit-publication",
            )
            approval = await resolver.for_approval(
                self.invocation_reference(),
                principal_id="kit-principal",
                access_context_hash="a" * 64,
                approval_id="kit-approval",
                decision_attempt_id="kit-decision",
            )
            dispatch = await resolver.for_dispatch(
                self.invocation_reference(),
                principal_id="kit-principal",
                access_context_hash="a" * 64,
                workflow_execution_id="kit-workflow",
                dispatch_attempt_id="kit-dispatch",
            )
            projection = await resolver.for_workflow_projection(
                self.workflow_reference(),
                principal_id="kit-principal",
                access_context_hash="a" * 64,
                workflow_execution_id="kit-workflow",
                projection_attempt_id="kit-projection",
            )
            assert direct.decision is SourceAuthorityDecision.AUTHORIZED
            assert direct.stamps == self.reference().stamps
            assert approval.decision is SourceAuthorityDecision.AUTHORIZED
            assert approval.stamps == self.invocation_reference().stamps
            assert dispatch.decision is SourceAuthorityDecision.AUTHORIZED
            assert dispatch.stamps == self.invocation_reference().stamps
            assert projection.decision is SourceAuthorityDecision.AUTHORIZED
            assert projection.stamps == self.workflow_reference().stamps
            assert direct.publication_attempt_id == "kit-publication"
            assert approval.decision_attempt_id == "kit-decision"
            assert dispatch.dispatch_attempt_id == "kit-dispatch"
            assert projection.projection_attempt_id == "kit-projection"

        asyncio.run(scenario())


class SourceBoundEvidenceProjectionResolverContractKit(SourceBoundResultResolverContractKit):
    """Certifies the five delivery targets plus the evidence-projection target."""

    resolver_protocol: ClassVar[type[object]] = SourceBoundEvidenceProjectionResolver
    target_coverage: ClassVar[Mapping[str, str]] = {
        **SourceBoundResultResolverContractKit.target_coverage,
        "for_evidence_projection": "test_evidence_projection_binds_exact_owner_read_attempt",
    }

    def create_resolver(self) -> SourceBoundEvidenceProjectionResolver:
        raise NotImplementedError

    def claim_slice_reference(self) -> ProtectedClaimSliceRef:
        raise NotImplementedError

    def test_evidence_projection_binds_exact_owner_read_attempt(self) -> None:
        async def scenario() -> None:
            projection = await self.create_resolver().for_evidence_projection(
                self.output_reference(),
                principal_id="kit-principal",
                access_context_hash="a" * 64,
                run_id="kit-run",
                evidence_projection_attempt_id="kit-evidence-read",
            )
            assert projection.decision is SourceAuthorityDecision.AUTHORIZED
            assert projection.stamps == self.output_reference().stamps
            assert projection.run_id == "kit-run"
            assert projection.evidence_projection_attempt_id == "kit-evidence-read"

            sliced = await self.create_resolver().for_evidence_projection(
                self.claim_slice_reference(),
                principal_id="kit-principal",
                access_context_hash="a" * 64,
                run_id="kit-run",
                evidence_projection_attempt_id="kit-evidence-slice",
            )
            assert sliced.decision is SourceAuthorityDecision.AUTHORIZED
            assert sliced.stamps == self.claim_slice_reference().stamps
            assert sliced.evidence_projection_attempt_id == "kit-evidence-slice"

        asyncio.run(scenario())

    def test_evidence_projection_never_materializes_for_a_foreign_principal(self) -> None:
        async def scenario() -> None:
            result = await self.create_resolver().for_evidence_projection(
                self.output_reference(),
                principal_id="kit-foreign-principal",
                access_context_hash="f" * 64,
                run_id="kit-run",
                evidence_projection_attempt_id="kit-evidence-read",
            )
            assert result.decision is not SourceAuthorityDecision.AUTHORIZED
            assert result.materialized_text is None
            assert result.stamps == ()

        asyncio.run(scenario())


class ProtectedSourceOutputWriterContractKit:
    def create_writer(self) -> ProtectedSourceOutputWriter:
        raise NotImplementedError

    def stamps(self) -> tuple[SourceAuthorityStamp, ...]:
        raise NotImplementedError

    def test_output_ref_binds_exact_hash_and_complete_stamps(self) -> None:
        async def scenario() -> None:
            writer = self.create_writer()
            result = await writer.claim_output(
                target_id="kit-target",
                attempt_id="kit-attempt",
                output=b"kit output",
                output_hash="a" * 64,
                stamps=self.stamps(),
            )
            assert result.output_hash == "a" * 64
            assert result.stamps == self.stamps()
            replay = await writer.claim_output(
                target_id="kit-target",
                attempt_id="kit-attempt",
                output=b"kit output",
                output_hash="a" * 64,
                stamps=self.stamps(),
            )
            assert replay == result

        asyncio.run(scenario())


class ProtectedSourceExecutionWriterContractKit:
    def create_writer(self) -> ProtectedSourceExecutionWriter:
        raise NotImplementedError

    def stamps(self) -> tuple[SourceAuthorityStamp, ...]:
        raise NotImplementedError

    def test_invocation_ref_binds_exact_hash_and_complete_stamps(self) -> None:
        async def scenario() -> None:
            writer = self.create_writer()
            result = await writer.claim_invocation(
                target_kind="approval_action",
                target_id="kit-approval",
                attempt_id="kit-attempt",
                invocation=b"kit invocation",
                invocation_hash="b" * 64,
                stamps=self.stamps(),
            )
            assert result.invocation_hash == "b" * 64
            assert result.stamps == self.stamps()
            replay = await writer.claim_invocation(
                target_kind="approval_action",
                target_id="kit-approval",
                attempt_id="kit-attempt",
                invocation=b"kit invocation",
                invocation_hash="b" * 64,
                stamps=self.stamps(),
            )
            assert replay == result

        asyncio.run(scenario())

    def test_workflow_state_ref_binds_exact_publication_target(self) -> None:
        async def scenario() -> None:
            result = await self.create_writer().claim_workflow_state(
                workflow_execution_id="kit-workflow",
                workflow_version=2,
                step_id="kit-step",
                publication_id="kit-publication",
                state=b"kit state",
                state_hash="c" * 64,
                stamps=self.stamps(),
            )
            assert result.state_hash == "c" * 64
            assert result.stamps == self.stamps()

        asyncio.run(scenario())


class AuthorizedContinuationTokenCodecContractKit:
    def create_codec(self) -> AuthorizedContinuationTokenCodec:
        raise NotImplementedError

    def state(self) -> AuthorizedContinuationState:
        raise NotImplementedError

    def mutate_token(self, token: str) -> str:
        raise NotImplementedError

    def decode_failure(self, codec: AuthorizedContinuationTokenCodec, token: str) -> bool:
        """Return true only when decoding ``token`` fails with the common safe outcome."""
        raise NotImplementedError

    def test_token_is_deterministic_and_decodes_only_opaque_locator(self) -> None:
        codec = self.create_codec()
        state = self.state()
        first = codec.encode(state)
        second = codec.encode(state)
        assert first == second
        locator = codec.decode(first)
        assert isinstance(locator, AuthorizedContinuationTokenLocator)
        assert state.expires_at.tzinfo == UTC
        key_id, separator, mac = first.partition(".")
        assert separator == "."
        assert key_id == codec.key_id
        assert re.fullmatch(r"[A-Za-z0-9_-]+", mac)
        assert "=" not in mac
        padding = "=" * (-len(mac) % 4)
        decoded_mac = base64.urlsafe_b64decode(mac + padding)
        assert len(decoded_mac) == 32
        assert locator.key_id == key_id
        assert locator.token_mac == decoded_mac.hex()
        assert set(locator.model_dump()) == {"key_id", "token_mac"}

    def test_canonical_mac_change_remains_only_an_opaque_locator(self) -> None:
        codec = self.create_codec()
        token = codec.encode(self.state())
        mutated = self.mutate_token(token)
        assert mutated != token
        assert codec.decode(mutated) != codec.decode(token)

    def test_malformed_or_noncanonical_token_syntax_fails(self) -> None:
        codec = self.create_codec()
        token = codec.encode(self.state())
        key_id, separator, mac = token.partition(".")
        assert separator == "."
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        last_index = alphabet.index(mac[-1])
        noncanonical_last = alphabet[(last_index // 16) * 16 + 1]
        noncanonical_alias = f"{key_id}.{mac[:-1]}{noncanonical_last}"
        malformed = (
            token.replace(".", "", 1),
            f"{key_id}.{mac}=",
            f"{key_id}.{mac}.extra",
            f"bad.key.{mac}",
            f"{key_id}.{mac[:-1]}",
            noncanonical_alias,
        )
        assert len(set(malformed)) == len(malformed)
        assert all(self.decode_failure(codec, candidate) for candidate in malformed)

    def test_bound_state_change_changes_token(self) -> None:
        codec = self.create_codec()
        state = self.state()
        changed = state.model_copy(update={"principal_access_hash": "f" * 64})
        assert codec.encode(changed) != codec.encode(state)
