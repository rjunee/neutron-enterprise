# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Current-source authorization for T1 artifact selection and disclosure.

The immutable artifact named by a search hit is historical evidence, not a
disclosure decision.  This module is the package-local implementation of the
015A0-ENFORCEMENT seam: every candidate is re-resolved against the current
authority head and the current ACL snapshot before its identity or material may
leave the Brain package.

The selector deliberately consumes only read capabilities.  It cannot advance a
source head, update an artifact, or reconstruct a missing authority row.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol, runtime_checkable

import sqlalchemy as sa

from ne.brain.current_source_authority import (
    ContinuationUnavailableError,
    SqlAlchemyAuthorizedContinuationStore,
    _receipt_matches_stamp,
    ne_source_authority_acl_snapshots,
    ne_source_authority_heads,
)
from ne.brain.trimming import GroupExpander, SharedDocumentResolver, acl_match_labels
from ne.contracts.source_authority import (
    NO_SOURCE_STATUS,
    SOURCE_AUTHORITY_STAMP_METADATA_KEY,
    SOURCE_STATUS_METADATA_KEY,
    AuthorizedArtifact,
    AuthorizedArtifactPage,
    AuthorizedArtifactPageStatus,
    AuthorizedContinuationReplay,
    AuthorizedContinuationState,
    CurrentSourceAuthorityReader,
    CurrentSourceAuthorityReceipt,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
)
from ne.contracts.types import Chunk, Principal, ScoredChunk

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SourceAuthorityEvaluationError(RuntimeError):
    """The current-source evaluator itself failed; authorization is UNKNOWN.

    Distinct from a denial on purpose. A denial is an answer — this chunk is not
    disclosable to this principal right now — and dropping it is correct. An
    evaluator fault is the ABSENCE of an answer, and silently dropping the chunk
    renders an outage byte-identical to a policy decision: the reader sees a
    smaller corpus with no way to tell that anything went wrong. Raising is what
    makes the difference observable at the call site and in the audit record.
    """


def chunk_is_source_free(chunk: Chunk) -> bool:
    """True iff the governed write path marked this chunk source-free.

    The mark is server-authored at ingest from the code-authored connector
    registry (``ne.connect.runner.SYNC_SOURCES``). Its absence — and any other
    value — means source-bearing, so a chunk that reaches here unmarked and
    unstamped is refused. Reading it is a strict equality against the ONE
    recognised value; no truthiness, no prefix, no fallback.
    """
    return chunk.metadata.get(SOURCE_STATUS_METADATA_KEY) == NO_SOURCE_STATUS


@dataclass(frozen=True)
class CurrentAclSnapshot:
    """Canonical current ACL truth for one exact current artifact.

    ``snapshot_hash`` is authored by the source-specific authority producer;
    this consumer compares it to the admitted receipt rather than inventing a
    second source-specific ACL canonicalizer.  ``labels`` are the canonical ACL
    principals/groups at that same observation.  ``document_id`` is the safe
    ReBAC object key, when that source supports document sharing.
    """

    artifact_id: str
    snapshot_hash: str
    labels: tuple[str, ...]
    document_id: str | None = None

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.artifact_id):
            raise ValueError("artifact_id must be a sha256 digest")
        if not _SHA256.fullmatch(self.snapshot_hash):
            raise ValueError("snapshot_hash must be a sha256 digest")
        if any(not label or len(label) > 2048 for label in self.labels):
            raise ValueError("ACL labels must be non-empty and bounded")
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("ACL labels must be unique")


@runtime_checkable
class CurrentAclSnapshotResolver(Protocol):
    """Resolve ACL truth from the same source-authority observation as a stamp."""

    def resolve_current_acl(
        self,
        stamp: SourceAuthorityStamp,
        *,
        current_receipt: CurrentSourceAuthorityReceipt,
    ) -> CurrentAclSnapshot | None | Awaitable[CurrentAclSnapshot | None]: ...


class SqlAlchemyCurrentAclSnapshotResolver:
    """Read exact producer-authored ACL labels for the authorized current receipt."""

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    def resolve_current_acl(
        self,
        stamp: SourceAuthorityStamp,
        *,
        current_receipt: CurrentSourceAuthorityReceipt,
    ) -> CurrentAclSnapshot | None:
        artifact = current_receipt.artifact_identity
        if (
            artifact is None
            or current_receipt.state is not SourceAuthorityState.PRESENT
            or current_receipt.source_identity != stamp.source_identity
            or artifact != stamp.artifact_identity
            or current_receipt.source_revision != stamp.selected_head_revision
            or current_receipt.source_contract != stamp.source_contract
            or current_receipt.contract_epoch != stamp.contract_epoch
            or current_receipt.acl_snapshot_hash != stamp.acl_snapshot_hash
        ):
            return None
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_source_authority_acl_snapshots).where(
                    ne_source_authority_acl_snapshots.c.artifact_id == artifact.artifact_id
                )
            ).one_or_none()
        if row is None or not isinstance(row.labels, list):
            return None
        labels = tuple(row.labels)
        try:
            snapshot = CurrentAclSnapshot(
                artifact_id=str(row.artifact_id),
                snapshot_hash=str(row.acl_snapshot_hash),
                labels=labels,
            )
        except (TypeError, ValueError):
            return None
        if (
            str(row.identity_hash) != stamp.source_identity.identity_hash
            or snapshot.artifact_id != artifact.artifact_id
            or snapshot.snapshot_hash != current_receipt.acl_snapshot_hash
        ):
            return None
        canonical = json.dumps(
            {"acl": list(sorted(set(snapshot.labels)))},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != snapshot.snapshot_hash:
            return None
        return snapshot


@dataclass(frozen=True)
class CurrentDisclosureDecision:
    decision: SourceAuthorityDecision
    fingerprint: str

    @property
    def authorized(self) -> bool:
        return self.decision is SourceAuthorityDecision.AUTHORIZED


class CurrentSourceDisclosureEvaluator:
    """One canonical current-head/health/ACL membership decision.

    Authority is resolved before ACL material.  An Unknown/withheld head never
    triggers the ACL provider, which avoids both wasted source reads and an
    existence oracle.  Provider and ReBAC failures propagate and therefore deny
    disclosure by erroring; no historical ``Chunk.acl`` fallback exists.
    """

    def __init__(
        self,
        authority: CurrentSourceAuthorityReader,
        acl_snapshots: CurrentAclSnapshotResolver,
        *,
        group_expander: GroupExpander | None = None,
        shared_docs: SharedDocumentResolver | None = None,
    ) -> None:
        self._authority = authority
        self._acl_snapshots = acl_snapshots
        self._group_expander = group_expander
        self._shared_docs = shared_docs

    async def evaluate(
        self,
        stamp: SourceAuthorityStamp,
        *,
        principal: Principal,
        access_context_hash: str,
    ) -> CurrentDisclosureDecision:
        if not _SHA256.fullmatch(access_context_hash):
            raise ValueError("access_context_hash must be a sha256 digest")
        authority = await self._authority.resolve(
            stamp,
            principal_id=principal.id,
            access_context_hash=access_context_hash,
        )
        if authority.decision is not SourceAuthorityDecision.AUTHORIZED:
            return CurrentDisclosureDecision(
                decision=authority.decision,
                fingerprint=_decision_fingerprint(
                    stamp, access_context_hash, authority.decision, ""
                ),
            )

        assert authority.current_receipt is not None
        raw = self._acl_snapshots.resolve_current_acl(
            stamp, current_receipt=authority.current_receipt
        )
        snapshot = await raw if inspect.isawaitable(raw) else raw
        if (
            snapshot is None
            or snapshot.artifact_id != stamp.artifact_identity.artifact_id
            or snapshot.snapshot_hash != stamp.acl_snapshot_hash
            or snapshot.snapshot_hash != stamp.artifact_identity.acl_snapshot_hash
        ):
            return CurrentDisclosureDecision(
                decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN,
                fingerprint=_decision_fingerprint(
                    stamp, access_context_hash, SourceAuthorityDecision.RETRYABLE_UNKNOWN, ""
                ),
            )

        labels = set(acl_match_labels(principal, self._group_expander))
        allowed = bool(labels.intersection(snapshot.labels))
        if not allowed and self._shared_docs is not None and snapshot.document_id is not None:
            shared = await self._shared_docs.shared_document_ids(principal)
            allowed = snapshot.document_id in shared
        decision = (
            SourceAuthorityDecision.AUTHORIZED
            if allowed
            else SourceAuthorityDecision.PERMANENT_WITHHELD
        )
        return CurrentDisclosureDecision(
            decision=decision,
            fingerprint=_decision_fingerprint(
                stamp, access_context_hash, decision, snapshot.snapshot_hash
            ),
        )


class HistoricalSourceDisclosureEvaluator:
    """Disclosure decision for VIEWING retained historical run evidence.

    :class:`CurrentSourceDisclosureEvaluator` answers "is this stamp the current
    admitted truth" — the right question for SERVING content as current, where a
    superseded revision must never be presented as today's document.  Viewing a
    retained historical answer asks a different question: "may this principal
    currently see this document at all".  A document being superseded is
    history's NORMAL state — every answer becomes historical the moment its
    source is edited — so revision/artifact/ACL-snapshot rotation must not
    withhold the retained record.  What still withholds it:

    - a stamp that never came from this deployment's immutable receipt ledger
      (anti-fabrication — identical to the current evaluator);
    - a source whose current head is no longer PRESENT (deleted/withdrawn);
    - a principal outside the CURRENT receipt's ACL snapshot (revocation — the
      newest admitted ACL governs, not the historical one).

    During a contract-epoch transition the newest admitted receipt's ACL is the
    best governed truth available and is what this evaluator uses; the CURRENT
    evaluator's fail-closed unknown remains the behavior for serving-as-current.
    """

    def __init__(
        self,
        authority: CurrentSourceAuthorityReader,
        engine: sa.Engine,
        *,
        group_expander: GroupExpander | None = None,
        shared_docs: SharedDocumentResolver | None = None,
    ) -> None:
        self._authority = authority
        self._engine = engine
        self._group_expander = group_expander
        self._shared_docs = shared_docs

    def _load_receipt(self, receipt_id: str) -> CurrentSourceAuthorityReceipt | None:
        load_receipt = getattr(self._authority, "load_receipt", None)
        if not callable(load_receipt):
            return None
        receipt = load_receipt(receipt_id)
        return receipt if isinstance(receipt, CurrentSourceAuthorityReceipt) else None

    def _current_head_receipt(self, identity_hash: str) -> CurrentSourceAuthorityReceipt | None:
        with self._engine.connect() as conn:
            head = conn.execute(
                sa.select(ne_source_authority_heads.c.current_receipt_id).where(
                    ne_source_authority_heads.c.identity_hash == identity_hash
                )
            ).one_or_none()
        if head is None:
            return None
        return self._load_receipt(str(head.current_receipt_id))

    def _acl_snapshot(
        self, *, artifact_id: str, expected_hash: str, identity_hash: str
    ) -> CurrentAclSnapshot | None:
        """One exact producer-authored ACL snapshot, hash- and identity-bound."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_source_authority_acl_snapshots).where(
                    ne_source_authority_acl_snapshots.c.artifact_id == artifact_id
                )
            ).one_or_none()
        if row is None or not isinstance(row.labels, list):
            return None
        try:
            snapshot = CurrentAclSnapshot(
                artifact_id=str(row.artifact_id),
                snapshot_hash=str(row.acl_snapshot_hash),
                labels=tuple(row.labels),
            )
        except (TypeError, ValueError):
            return None
        # Self-consistency: the producer-authored row must bind the expected
        # artifact/identity, and its labels must re-derive its own hash.
        if (
            str(row.identity_hash) != identity_hash
            or snapshot.artifact_id != artifact_id
            or snapshot.snapshot_hash != expected_hash
        ):
            return None
        canonical = json.dumps(
            {"acl": list(sorted(set(snapshot.labels)))},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != snapshot.snapshot_hash:
            return None
        return snapshot

    async def evaluate(
        self,
        stamp: SourceAuthorityStamp,
        *,
        principal: Principal,
        access_context_hash: str,
    ) -> CurrentDisclosureDecision:
        if not _SHA256.fullmatch(access_context_hash):
            raise ValueError("access_context_hash must be a sha256 digest")

        def _decision(
            decision: SourceAuthorityDecision, acl_hash: str = ""
        ) -> CurrentDisclosureDecision:
            return CurrentDisclosureDecision(
                decision=decision,
                fingerprint=_decision_fingerprint(stamp, access_context_hash, decision, acl_hash),
            )

        # Anti-fabrication: the stamp must name a receipt this deployment's
        # immutable ledger actually retains, byte-consistent with the stamp.
        presented = self._load_receipt(stamp.authority_receipt_id)
        if presented is None or not _receipt_matches_stamp(presented, stamp):
            return _decision(SourceAuthorityDecision.PERMANENT_WITHHELD)

        current = self._current_head_receipt(stamp.source_identity.identity_hash)
        if current is None:
            return _decision(SourceAuthorityDecision.RETRYABLE_UNKNOWN)
        if current.state is not SourceAuthorityState.PRESENT:
            return _decision(SourceAuthorityDecision.PERMANENT_WITHHELD)
        current_artifact = current.artifact_identity
        if current_artifact is None:
            return _decision(SourceAuthorityDecision.RETRYABLE_UNKNOWN)

        # The HISTORICAL version's own retained ACL governs its content — the
        # same rule the document journey applies (no historical ACL leakage): a
        # principal who has since lost the labels that admitted the old version
        # may not re-read material derived from it, even where the current
        # version admits them.
        historical = self._acl_snapshot(
            artifact_id=stamp.artifact_identity.artifact_id,
            expected_hash=stamp.acl_snapshot_hash,
            identity_hash=stamp.source_identity.identity_hash,
        )
        if historical is None:
            return _decision(SourceAuthorityDecision.RETRYABLE_UNKNOWN)
        # And the CURRENT version's ACL must also admit them — losing access to
        # the document today revokes its history too, mirroring the journey
        # route's current-version authorization.
        snapshot = self._acl_snapshot(
            artifact_id=current_artifact.artifact_id,
            expected_hash=current.acl_snapshot_hash,
            identity_hash=current.source_identity.identity_hash,
        )
        if snapshot is None:
            return _decision(SourceAuthorityDecision.RETRYABLE_UNKNOWN)

        labels = set(acl_match_labels(principal, self._group_expander))
        allowed = bool(labels.intersection(historical.labels)) and bool(
            labels.intersection(snapshot.labels)
        )
        if not allowed and self._shared_docs is not None and snapshot.document_id is not None:
            shared = await self._shared_docs.shared_document_ids(principal)
            allowed = snapshot.document_id in shared
        return _decision(
            SourceAuthorityDecision.AUTHORIZED
            if allowed
            else SourceAuthorityDecision.PERMANENT_WITHHELD,
            snapshot.snapshot_hash,
        )


def _decision_fingerprint(
    stamp: SourceAuthorityStamp,
    access_context_hash: str,
    decision: SourceAuthorityDecision,
    acl_snapshot_hash: str,
) -> str:
    material = {
        "access": access_context_hash,
        "acl": acl_snapshot_hash,
        "artifact": stamp.artifact_identity.artifact_id,
        "decision": decision.value,
        "receipt": stamp.authority_receipt_hash,
        "source": stamp.source_identity.identity_hash,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class RankedArtifactCandidate:
    """One stable backend candidate; malformed/missing stamps remain scannable."""

    cursor_id: str
    score: Decimal
    artifact_identity: SourceContentArtifactIdentity | None
    authority_stamp: SourceAuthorityStamp | None

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.cursor_id):
            raise ValueError("candidate cursor_id must be a sha256 digest")
        if not self.score.is_finite():
            raise ValueError("candidate score must be finite")
        if (self.artifact_identity is None) != (self.authority_stamp is None):
            raise ValueError("candidate identity and authority stamp must appear together")
        if (
            self.authority_stamp is not None
            and self.authority_stamp.artifact_identity != self.artifact_identity
        ):
            raise ValueError("candidate stamp refers to another artifact")


@dataclass(frozen=True)
class RankedArtifactBatch:
    items: tuple[RankedArtifactCandidate, ...]
    end_reached: bool
    scope_health_epoch: int = 1

    def __post_init__(self) -> None:
        if self.scope_health_epoch < 1:
            raise ValueError("scope_health_epoch must be positive")
        expected = tuple(sorted(self.items, key=_candidate_sort_key))
        if expected != self.items:
            raise ValueError("candidate batch must use stable score/id order")
        if len({item.cursor_id for item in self.items}) != len(self.items):
            raise ValueError("candidate cursor ids must be unique")


@runtime_checkable
class RankedArtifactCandidateReader(Protocol):
    async def scan(
        self,
        *,
        after_score: Decimal | None,
        after_cursor_id: str | None,
        limit: int,
    ) -> RankedArtifactBatch: ...


def _candidate_sort_key(candidate: RankedArtifactCandidate) -> tuple[Decimal, str]:
    return (-candidate.score, candidate.cursor_id)


class CurrentAuthorizedArtifactSelectorImpl:
    """Contract-backed progressive current-authorized artifact selector."""

    def __init__(
        self,
        candidates: RankedArtifactCandidateReader,
        evaluator: CurrentSourceDisclosureEvaluator,
        continuation_store: SqlAlchemyAuthorizedContinuationStore,
        *,
        principal: Principal,
        config_hash: str,
        contract_head_hash: str,
        clock: Callable[[], datetime] | None = None,
        continuation_ttl: timedelta = timedelta(minutes=15),
        normalizer_version: int = 1,
        encoding_version: int = 1,
    ) -> None:
        if not _SHA256.fullmatch(config_hash) or not _SHA256.fullmatch(contract_head_hash):
            raise ValueError("selector config/head hashes must be sha256 digests")
        if continuation_ttl <= timedelta(0):
            raise ValueError("continuation_ttl must be positive")
        self._candidates = candidates
        self._evaluator = evaluator
        self._continuations = continuation_store
        self._principal = principal
        self._config_hash = config_hash
        self._contract_head_hash = contract_head_hash
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ttl = continuation_ttl
        self._normalizer_version = normalizer_version
        self._encoding_version = encoding_version
        self._first_pages: dict[tuple[str, str, int, int], AuthorizedArtifactPage] = {}

    async def select(
        self,
        *,
        principal_id: str,
        access_context_hash: str,
        request_hash: str,
        top_k: int,
        scan_limit: int,
        continuation_token: str | None = None,
    ) -> AuthorizedArtifactPage:
        self._validate_request(
            principal_id=principal_id,
            access_context_hash=access_context_hash,
            request_hash=request_hash,
            top_k=top_k,
            scan_limit=scan_limit,
        )
        cache_key = (access_context_hash, request_hash, top_k, scan_limit)
        if continuation_token is None:
            cached_page = self._first_pages.get(cache_key)
            if cached_page is not None:
                still_authorized = True
                for item in cached_page.items:
                    decision = await self._evaluator.evaluate(
                        item.authority_stamp,
                        principal=self._principal,
                        access_context_hash=access_context_hash,
                    )
                    if decision.decision is not SourceAuthorityDecision.AUTHORIZED:
                        still_authorized = False
                if still_authorized:
                    return cached_page
                self._first_pages.pop(cache_key, None)

        previous_state: AuthorizedContinuationState | None = None
        after_score: Decimal | None = None
        after_cursor: str | None = None
        if continuation_token is not None:
            previous_state = self._continuations.claim_load(continuation_token)
            self._validate_state(
                previous_state,
                access_context_hash=access_context_hash,
                request_hash=request_hash,
                top_k=top_k,
                scan_limit=scan_limit,
            )
            after_score = previous_state.cursor_score
            after_cursor = previous_state.cursor_artifact_id

        batch = await self._candidates.scan(
            after_score=after_score,
            after_cursor_id=after_cursor,
            limit=scan_limit,
        )
        authorized: list[AuthorizedArtifact] = []
        fingerprints: list[str] = []
        scanned = 0
        cursor: RankedArtifactCandidate | None = None
        for candidate in batch.items:
            if scanned == scan_limit or len(authorized) == top_k:
                break
            scanned += 1
            cursor = candidate
            if candidate.authority_stamp is None or candidate.artifact_identity is None:
                continue
            decision = await self._evaluator.evaluate(
                candidate.authority_stamp,
                principal=self._principal,
                access_context_hash=access_context_hash,
            )
            if not decision.authorized:
                continue
            authorized.append(
                AuthorizedArtifact(
                    artifact_identity=candidate.artifact_identity,
                    authority_stamp=candidate.authority_stamp,
                    score=candidate.score,
                )
            )
            fingerprints.append(decision.fingerprint)

        enough = len(authorized) == top_k
        proven_end = batch.end_reached and scanned == len(batch.items)
        if enough or proven_end:
            status = AuthorizedArtifactPageStatus.COMPLETE
            next_token = None
            next_state = None
        else:
            status = (
                AuthorizedArtifactPageStatus.PARTIAL
                if authorized
                else AuthorizedArtifactPageStatus.EXHAUSTED
            )
            if cursor is None:
                raise ValueError("an incomplete candidate batch must expose a cursor")
            previous_set_hash = (
                previous_state.prior_page_set_hash if previous_state is not None else "0" * 64
            )
            next_state = AuthorizedContinuationState(
                state_version=1,
                principal_access_hash=access_context_hash,
                request_hash=request_hash,
                normalizer_version=self._normalizer_version,
                encoding_version=self._encoding_version,
                requested_top_k=top_k,
                scan_limit=scan_limit,
                config_hash=self._config_hash,
                contract_head_hash=self._contract_head_hash,
                cursor_score=cursor.score,
                cursor_artifact_id=cursor.cursor_id,
                prior_page_set_hash=_page_set_hash(
                    previous_set_hash,
                    [item.artifact_identity.artifact_id for item in authorized],
                ),
                expires_at=(
                    previous_state.expires_at if previous_state else self._now() + self._ttl
                ),
            )
            next_token = self._continuations.persist(next_state)

        page = AuthorizedArtifactPage(
            items=tuple(authorized),
            status=status,
            scanned_count=scanned,
            returned_count=len(authorized),
            scan_limit=scan_limit,
            continuation_token=next_token,
        )
        if continuation_token is not None:
            assert previous_state is not None
            locator = self._continuations.codec.decode(continuation_token)
            replay = AuthorizedContinuationReplay(
                token_mac=locator.token_mac,
                state=previous_state,
                item_ids=tuple(item.artifact_identity.artifact_id for item in authorized),
                status=status,
                scanned_count=scanned,
                returned_count=len(authorized),
                scan_limit=scan_limit,
                contributor_stamps=tuple(item.authority_stamp for item in authorized),
                acl_decision_fingerprints=tuple(fingerprints),
                scope_health_epoch=batch.scope_health_epoch,
                next_state_hash=(None if next_state is None else _state_hash(next_state)),
            )
            stored = self._continuations.persist_replay(replay)
            if stored != replay.replay_hash:
                raise ContinuationUnavailableError("continuation is unavailable")
        else:
            self._first_pages[cache_key] = page
        return page

    def _validate_request(
        self,
        *,
        principal_id: str,
        access_context_hash: str,
        request_hash: str,
        top_k: int,
        scan_limit: int,
    ) -> None:
        if principal_id != self._principal.id:
            raise ValueError("selector is bound to another principal")
        if not _SHA256.fullmatch(access_context_hash) or not _SHA256.fullmatch(request_hash):
            raise ValueError("selector request hashes must be sha256 digests")
        if not 1 <= top_k <= 1000:
            raise ValueError("top_k must be between 1 and 1000")
        if scan_limit < 1:
            raise ValueError("scan_limit must be positive")

    def _validate_state(
        self,
        state: AuthorizedContinuationState,
        *,
        access_context_hash: str,
        request_hash: str,
        top_k: int,
        scan_limit: int,
    ) -> None:
        if (
            state.principal_access_hash != access_context_hash
            or state.request_hash != request_hash
            or state.requested_top_k != top_k
            or state.scan_limit != scan_limit
            or state.config_hash != self._config_hash
            or state.contract_head_hash != self._contract_head_hash
            or state.normalizer_version != self._normalizer_version
            or state.encoding_version != self._encoding_version
        ):
            raise ContinuationUnavailableError("continuation is unavailable")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("selector clock must return timezone-aware UTC")
        return value


def _page_set_hash(previous: str, item_ids: Sequence[str]) -> str:
    return hashlib.sha256(
        json.dumps([previous, *item_ids], separators=(",", ":")).encode()
    ).hexdigest()


def _state_hash(state: AuthorizedContinuationState) -> str:
    return hashlib.sha256(state.model_dump_json().encode()).hexdigest()


def authority_stamp_from_chunk(chunk: Chunk) -> SourceAuthorityStamp | None:
    """Strictly parse the one server-authored metadata key; malformed means unavailable."""

    raw = chunk.metadata.get(SOURCE_AUTHORITY_STAMP_METADATA_KEY)
    if raw is None:
        return None
    try:
        return SourceAuthorityStamp.model_validate(raw)
    except (TypeError, ValueError):
        return None


class ScoredChunkCandidateReader:
    """Stable in-request adapter from scored chunks to authority candidates.

    The backend may return equal scores in any order; this adapter canonicalizes
    by descending exact decimal score and then a collision-safe cursor id.  It
    never reads or applies ``Chunk.acl``.
    """

    def __init__(self, chunks: Sequence[ScoredChunk], *, end_reached: bool) -> None:
        self._material: dict[str, ScoredChunk] = {}
        candidates: list[RankedArtifactCandidate] = []
        for scored in chunks:
            stamp = authority_stamp_from_chunk(scored.chunk)
            artifact = None if stamp is None else stamp.artifact_identity
            cursor_id = (
                hashlib.sha256(scored.chunk.chunk_id.encode()).hexdigest()
                if artifact is None
                else artifact.artifact_id
            )
            candidate = RankedArtifactCandidate(
                cursor_id=cursor_id,
                score=Decimal(str(scored.score)),
                artifact_identity=artifact,
                authority_stamp=stamp,
            )
            if cursor_id in self._material:
                # One immutable artifact may never occupy two disclosure rows in
                # the same resolved page.  Keep the higher-scored stable row.
                continue
            self._material[cursor_id] = scored
            candidates.append(candidate)
        self._items = tuple(sorted(candidates, key=_candidate_sort_key))
        self._end_reached = end_reached

    async def scan(
        self,
        *,
        after_score: Decimal | None,
        after_cursor_id: str | None,
        limit: int,
    ) -> RankedArtifactBatch:
        if (after_score is None) != (after_cursor_id is None):
            raise ValueError("score and cursor id must appear together")
        items = self._items
        if after_score is not None and after_cursor_id is not None:
            cursor_key = (-after_score, after_cursor_id)
            items = tuple(item for item in items if _candidate_sort_key(item) > cursor_key)
        page = items[:limit]
        end = self._end_reached and len(items) <= limit
        return RankedArtifactBatch(items=page, end_reached=end)

    def materialize(self, artifacts: Sequence[AuthorizedArtifact]) -> tuple[ScoredChunk, ...]:
        return tuple(self._material[item.artifact_identity.artifact_id] for item in artifacts)


__all__ = [
    "SOURCE_AUTHORITY_STAMP_METADATA_KEY",
    "CurrentAclSnapshot",
    "CurrentAclSnapshotResolver",
    "CurrentAuthorizedArtifactSelectorImpl",
    "CurrentDisclosureDecision",
    "CurrentSourceDisclosureEvaluator",
    "RankedArtifactBatch",
    "RankedArtifactCandidate",
    "RankedArtifactCandidateReader",
    "ScoredChunkCandidateReader",
    "SourceAuthorityEvaluationError",
    "SqlAlchemyCurrentAclSnapshotResolver",
    "authority_stamp_from_chunk",
    "chunk_is_source_free",
]
