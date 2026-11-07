# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-schema-bootstrap — bounded sample profiling + deterministic candidate-model
inference + LinkML emission/round-trip/reporting for engagement schema bootstrap.

U1 of the NE-SCHEMA-BOOTSTRAP epic: a directory of
CSV/TSV/JSONL/JSON-array sample files profiles into bounded, PII-scrubbed
column profiles, and those profiles infer a typed :class:`CandidateModel`
(types, requiredness, identifiers, enums, FK edges, junction flags, seed
anchoring) as a PURE FUNCTION — no LLM, no network, no ``linkml`` import.
Same input -> byte-identical output.

U2 adds: the house-dialect LinkML emitter (:mod:`ne.schema_bootstrap.emit`),
the real-codegen-venv round-trip validator
(:mod:`ne.schema_bootstrap.roundtrip`), the total-traceability inference
report renderer (:mod:`ne.schema_bootstrap.report`), and the ``pr-body.md``
emitter (:mod:`ne.schema_bootstrap.prbody`). This package still imports
``linkml`` NOWHERE in-process — generation runs only as a subprocess of the
isolated, hash-pinned ``ne-schema`` codegen venv (see
:mod:`ne.schema_bootstrap.roundtrip`'s module docstring).

U3 adds: the ``ne schema bootstrap`` CLI's library backbone —
:func:`ne.schema_bootstrap.workspace.run_bootstrap` (workspace creation,
validate-before-write ordering, the five run artifacts) and
:func:`ne.schema_bootstrap.refine.three_way_merge` (the deterministic
``--refine`` merge with tombstoned deletions) — plus
:class:`DuplicateRecordSetSlotNameError`, fixing a prior round's residual
where that error was raised by :func:`emit_linkml` but absent from this
module's ``__all__``/README (see ``tests/test_boundaries.py``'s
``__all__``-parity assertion, added specifically so that drift cannot
recur). Live-DB sampling and LLM labeling remain later units (U4-U7) and
are out of scope here.
"""

from __future__ import annotations

from .config import DEFAULT_CONFIG, BootstrapConfig
from .emit import EmittedSchema, emit_linkml
from .errors import (
    DraftGenerationError,
    DuplicateClassNameError,
    DuplicateRecordSetSlotNameError,
    DuplicateSlotNameError,
    EmptySampleError,
    InvalidClassNameError,
    InvalidEngagementNameError,
    MentionCompletenessError,
    RefineError,
    SampleFormatError,
    SchemaBootstrapError,
    WorkspaceExistsError,
    WorkspaceIOError,
)
from .hygiene import scrub_text
from .infer import infer_candidate_model
from .model import (
    CandidateClass,
    CandidateEdge,
    CandidateEnum,
    CandidateModel,
    CandidateSlot,
)
from .prbody import render_pr_body
from .profiles import ColumnProfile, ProfileBundle, SampleProfile, TableProfile, profile_samples
from .refine import MergeResult, orphaned_classes, three_way_merge
from .report import (
    RefineLanding,
    expected_anchors,
    render_inference_report,
    verify_mention_completeness,
)
from .roundtrip import RoundTripArtifacts, validate_draft_generates
from .workspace import BootstrapRunResult, run_bootstrap

__all__ = [
    "DEFAULT_CONFIG",
    "BootstrapConfig",
    "BootstrapRunResult",
    "CandidateClass",
    "CandidateEdge",
    "CandidateEnum",
    "CandidateModel",
    "CandidateSlot",
    "ColumnProfile",
    "DraftGenerationError",
    "DuplicateClassNameError",
    "DuplicateRecordSetSlotNameError",
    "DuplicateSlotNameError",
    "EmittedSchema",
    "EmptySampleError",
    "InvalidClassNameError",
    "InvalidEngagementNameError",
    "MentionCompletenessError",
    "MergeResult",
    "ProfileBundle",
    "RefineError",
    "RefineLanding",
    "RoundTripArtifacts",
    "SampleFormatError",
    "SampleProfile",
    "SchemaBootstrapError",
    "TableProfile",
    "WorkspaceExistsError",
    "WorkspaceIOError",
    "emit_linkml",
    "expected_anchors",
    "infer_candidate_model",
    "orphaned_classes",
    "profile_samples",
    "render_inference_report",
    "render_pr_body",
    "run_bootstrap",
    "scrub_text",
    "three_way_merge",
    "validate_draft_generates",
    "verify_mention_completeness",
]
