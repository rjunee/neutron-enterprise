# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-flywheel — correction-signal capture backend (NE-064).

Typed ingestion for the six kinds of correction/feedback signal (thumbs up/
down, draft edit-distance, wrong-source, re-query, approval-rejection),
durably stored with full provenance (principal, surface, session/turn/message
ids) and retrievable in append order for NE-065's curation agent to cluster
into fix proposals.

Backend capture ONLY: the console/Slack/API UI emission hooks are follow-on
work (see the package README for the emit contract those surfaces implement
against :class:`~ne.flywheel.capture.CorrectionSignalCapture`). The scheduled
curation agent and steward-queue backend (NE-065) build on top of this
package's :meth:`~ne.flywheel.store.SqlAlchemyCorrectionSignalStore.read_signals`.

NE-066 adds the propose-then-approve GATE on top of NE-065: a deterministic,
injection-safe :func:`~ne.flywheel.diffs.render_proposal_diff` that turns each
typed proposal into a reviewable git/OPA diff artifact, and an append-only,
audited :class:`~ne.flywheel.review.ProposalReviewService` state machine
(proposed -> approved -> applied, proposed/approved -> rejected) with tracked
MANUAL apply — the platform records WHO/WHEN a human applied a change, it never
applies one (auto-apply/auto-re-index are v1.1). Every rejection atomically
feeds a ``thumbs_down`` correction signal back into the NE-064 store, closing
the flywheel loop.
"""

from __future__ import annotations

__version__ = "0.5.0"

from ne.flywheel.capture import CorrectionSignalCapture
from ne.flywheel.clustering import (
    SignalCluster,
    cluster_signals,
    correction_candidates,
)
from ne.flywheel.config import FlywheelSettings
from ne.flywheel.curation import (
    CurationAgent,
    CurationOutcome,
    SummaryComposer,
    TemplateSummaryComposer,
    ensure_curation_schema,
)
from ne.flywheel.diffs import (
    DiffArtifact,
    DiffArtifactKind,
    ProposedFileChange,
    render_proposal_diff,
)
from ne.flywheel.errors import (
    CurationAuditError,
    CurationError,
    FlywheelError,
    InvalidTransitionError,
    ReviewAuditError,
    ReviewError,
    SignalRejectedError,
    UnknownProposalError,
)
from ne.flywheel.factory import (
    capture_from_settings,
    curation_agent_from_settings,
    review_service_from_settings,
)
from ne.flywheel.governed import (
    CURATION_TOOL_NAME,
    curation_tool_handler,
    curation_tool_spec,
    scheduled_curation_job,
)
from ne.flywheel.hygiene import scrub_text
from ne.flywheel.lanes import FixLane, ProposalPriority, route_cluster
from ne.flywheel.proposals import (
    FixProposal,
    GoldenSetCandidate,
    GraphExtractionAssertion,
    GraphExtractionAssertionKind,
    GraphExtractionBatch,
    ProposalKind,
    SqlAlchemyProposalStore,
    StoredFixProposal,
    ensure_proposal_schema,
    graph_extraction_proposal_id,
    graph_extraction_row_id,
)
from ne.flywheel.review import (
    ProposalReviewService,
    ProposalStatus,
    RejectionCode,
    ReviewAction,
    StoredReviewEvent,
    ensure_review_schema,
    ne_proposal_reviews,
)
from ne.flywheel.signals import (
    ApprovalRejectionSignal,
    DraftEditSignal,
    ReQuerySignal,
    SignalProvenance,
    SignalSurface,
    ThumbsSignal,
    WrongSourceSignal,
    build_correction_signal,
    draft_edit_ratio,
)
from ne.flywheel.store import (
    SqlAlchemyCorrectionSignalStore,
    StoredCorrectionSignal,
    ensure_flywheel_schema,
    ne_correction_signals,
)

__all__ = [
    "CURATION_TOOL_NAME",
    "ApprovalRejectionSignal",
    "CorrectionSignalCapture",
    "CurationAgent",
    "CurationAuditError",
    "CurationError",
    "CurationOutcome",
    "DiffArtifact",
    "DiffArtifactKind",
    "DraftEditSignal",
    "FixLane",
    "FixProposal",
    "FlywheelError",
    "FlywheelSettings",
    "GoldenSetCandidate",
    "GraphExtractionAssertion",
    "GraphExtractionAssertionKind",
    "GraphExtractionBatch",
    "InvalidTransitionError",
    "ProposalKind",
    "ProposalPriority",
    "ProposalReviewService",
    "ProposalStatus",
    "ProposedFileChange",
    "ReQuerySignal",
    "RejectionCode",
    "ReviewAction",
    "ReviewAuditError",
    "ReviewError",
    "SignalCluster",
    "SignalProvenance",
    "SignalRejectedError",
    "SignalSurface",
    "SqlAlchemyCorrectionSignalStore",
    "SqlAlchemyProposalStore",
    "StoredCorrectionSignal",
    "StoredFixProposal",
    "StoredReviewEvent",
    "SummaryComposer",
    "TemplateSummaryComposer",
    "ThumbsSignal",
    "UnknownProposalError",
    "WrongSourceSignal",
    "__version__",
    "build_correction_signal",
    "capture_from_settings",
    "cluster_signals",
    "correction_candidates",
    "curation_agent_from_settings",
    "curation_tool_handler",
    "curation_tool_spec",
    "draft_edit_ratio",
    "ensure_curation_schema",
    "ensure_flywheel_schema",
    "ensure_proposal_schema",
    "ensure_review_schema",
    "graph_extraction_proposal_id",
    "graph_extraction_row_id",
    "ne_correction_signals",
    "ne_proposal_reviews",
    "render_proposal_diff",
    "review_service_from_settings",
    "route_cluster",
    "scheduled_curation_job",
    "scrub_text",
]
