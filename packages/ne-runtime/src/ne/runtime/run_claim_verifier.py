# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pure deterministic verification for one server-authored Run claim.

The adapter deliberately accepts only already-protected, server-observed evidence.  It
does not mint claim, evidence, event, delivery, or receipt identities.  Persistence and
same-Run loading remain the responsibility of the Run store integration layer.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ne.govern.grounding import (
    PROFILE_NG2,
    GroundingCorpus,
    GroundingVerdict,
    check_grounding,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN)]
OpaqueId = Annotated[str, Field(min_length=1, max_length=512)]
_SEARCH_HEADER = re.compile(r"^\[showing ([0-9]+) of ([0-9]+) retrieved chunks\]$")
#: ``ne.brain_ontology.verified.answers.render_answer_text`` — the ONLY model-facing
#: rendering of a verified (T3) answer, and therefore the only content-bearing byte
#: string a verified-T3 claim can be grounded in.
_VERIFIED_HEADER = re.compile(
    r"^\[showing ([0-9]+) of (?:[0-9]+|unknown) rows(?:, truncated \([^)]*\))?\]$"
)
#: ``ne.api.knowledge_source_evidence._verified_row_anchor`` — the row anchor is a
#: pure function of the row receipt's hash, so a locator cannot claim a row it does
#: not carry the receipt for.
_ROW_ANCHOR_DOMAIN = b"ne.verified-evidence-row-anchor.v1\x00"
_VERIFIED_LOCATOR_KEYS = frozenset(
    {"kind", "row_anchor", "receipt", "contributor_index", "contributor", "evidence_assurance"}
)
#: ``ne.api.knowledge_source_evidence`` claims a multi-key read's empty-member
#: receipt into the SAME evidence set as its rows (NE-KG-MULTIKEY partial coverage).
#: Its locator is the exact ``verified_t3_empty`` shape — coverage evidence, not row
#: evidence: no rendered row cites it, so row verification must SKIP it (after
#: checking its shape) rather than refuse the whole set.
_VERIFIED_EMPTY_LOCATOR_KEYS = frozenset({"kind", "receipt", "evidence_assurance"})
_SHA256_RE = re.compile(_SHA256_PATTERN)

CLAIM_VERIFIER_METHOD = "ne.grounding.claim"
#: ``v2``: NE-CLAIM-LINEFACT — a verified (T3) rendering's corpus carries one
#: line-level fact per served line alongside the row-bound table, so a
#: rendering-contiguous span can ground. A receipt minted under ``v1`` was
#: verified WITHOUT line facts; the bump keeps the two regimes distinguishable.
CLAIM_VERIFIER_VERSION = f"v2/{PROFILE_NG2.version}"


#: The longest ``safe_reason`` that actually REACHES a reader.
#: ``ne.api.run_projection.RunProjection._failure_reason`` drops a checkpointed
#: ``error_reason`` longer than this, so a reason built past the cap is not a
#: longer explanation — it is ``failure_reason: null`` all over again. Pinned
#: against ``ne.runtime.runner._MAX_SAFE_REASON_CHARS`` by
#: ``test_the_verifier_reason_shares_the_projection_cap``.
_MAX_SAFE_REASON_CHARS = 256


class ClaimVerificationUnavailable(ValueError):
    """The exact claim/evidence binding cannot be verified.

    Carries a ``safe_reason`` so a Run that dies here says WHY. Every ``detail``
    below is a static, server-authored literal — no claim text, no evidence
    text, no model output — so publishing it verbatim keeps the same C12
    guarantee the rest of this module holds. ``run_workflow`` reads it off the
    exception and checkpoints it as ``execution_failed``/``error_reason``, which
    ``run_projection`` serves and ``TurnFailureReadout`` renders.

    Before this, EVERY raise site here published ``failure_reason: null``: the
    reader saw a bare status word for a failure the server could name precisely.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        reason = f"A claim could not be verified against its cited evidence: {detail}."
        self.safe_reason = reason[:_MAX_SAFE_REASON_CHARS]
        super().__init__(detail)


def _grounding_detail(verdict: GroundingVerdict) -> str:
    """``claim is not grounded...`` plus the CLASSES that failed, within the cap.

    A :class:`~ne.govern.grounding.GroundingFinding` carries only a reason class
    from a closed ``Literal`` and a count — the ungrounded text never leaves the
    grounding module — so the classes are publishable exactly as they are, and
    they are the whole diagnostic value here. "not grounded" alone cannot
    distinguish a model that paraphrased its source (``vocab-ungrounded-factual``)
    from one that quoted it perfectly while the gate's numeral parser refused a
    date inside the quotation (``numeral-unparseable``), and those are a prompt
    problem and a gate problem respectively.

    Findings are appended one at a time and only while they fit: a partial list a
    reader receives beats a complete one dropped whole for length.
    """
    base = "claim is not grounded by exact evidence"
    findings = verdict.findings
    if not findings:
        return base
    detail = base
    named: list[str] = []
    for finding in findings:
        entry = f"{finding.reason} x{finding.count}"
        candidate = f"{base} [{', '.join([*named, entry])}]"
        # Measured against the FULL published sentence, not the fragment.
        if len(f"A claim could not be verified against its cited evidence: {candidate}.") <= (
            _MAX_SAFE_REASON_CHARS
        ):
            named.append(entry)
            detail = candidate
        else:
            break
    return detail


class _ClosedModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ClaimSlice(_ClosedModel):
    """One server-authored claim slice within an exact UTF-8 output."""

    claim_key: OpaqueId
    claim_content_hash: Sha256
    span_start_byte: Annotated[int, Field(ge=0)]
    span_end_byte: Annotated[int, Field(gt=0)]
    served_output_hash: Sha256
    served_output: bytes


class _EvidenceObservation(_ClosedModel):
    """Content plus immutable identities loaded by the owning Run store."""

    schema_version: Literal["run-claim-evidence-observation.v1"] = (
        "run-claim-evidence-observation.v1"
    )
    protected_ref: OpaqueId
    protected_result_hash: Sha256
    evidence_id: OpaqueId
    locator_id: OpaqueId
    locator_hash: Sha256
    producing_event_id: OpaqueId
    delivery_receipt_id: OpaqueId
    delivery_receipt_hash: Sha256
    protected_rendering: bytes
    locator: bytes


class DirectSearchEvidenceObservation(_EvidenceObservation):
    """Exact protected search rendering plus its canonical locator."""

    read_kind: Literal["search"] = "search"
    assurance: Literal["direct"] = "direct"


class InferredGraphPathEvidenceObservation(_EvidenceObservation):
    """Exact protected GraphPath JSON plus its canonical locator."""

    read_kind: Literal["graph_path"] = "graph_path"
    assurance: Literal["inferred"] = "inferred"


class VerifiedRowSetMemberObservation(_ClosedModel):
    """One (row, contributor) member of a verified-T3 evidence set."""

    evidence_id: OpaqueId
    locator_id: OpaqueId
    locator_hash: Sha256
    producing_event_id: OpaqueId
    locator: bytes


class VerifiedRowSetEvidenceObservation(_ClosedModel):
    """The COMPLETE ordered evidence set behind one served verified (T3) result.

    A verified read does not produce one evidence record: it produces one per
    (row, contributor) pair under a single protected result, and the rendering the
    model was shown is the WHOLE table. Binding a claim to one member would assert
    that a claim over the table was verified against a single contributor of a
    single row, which is exactly the collapse the loader used to refuse outright.

    So the set arrives here whole. ``members`` is the set's own server-minted order
    (``ne_agent_evidence_sets.evidence_ids``, re-checked against the sealed
    reference by the Run store), every member's locator is hash-checked here, and
    :func:`_verified_set_answers` binds each rendered row to its row receipt by the
    receipt's own ``canonical_row_hash`` before one byte reaches the corpus.
    """

    schema_version: Literal["run-claim-evidence-set-observation.v1"] = (
        "run-claim-evidence-set-observation.v1"
    )
    read_kind: Literal["verified_t3"] = "verified_t3"
    assurance: Literal["direct"] = "direct"
    protected_ref: OpaqueId
    protected_result_hash: Sha256
    evidence_set_id: OpaqueId
    set_key: Sha256
    delivery_receipt_id: OpaqueId
    delivery_receipt_hash: Sha256
    protected_rendering: bytes
    members: Annotated[
        tuple[VerifiedRowSetMemberObservation, ...], Field(min_length=1, max_length=1024)
    ]


EvidenceObservation = (
    DirectSearchEvidenceObservation
    | InferredGraphPathEvidenceObservation
    | VerifiedRowSetEvidenceObservation
)


class DeterministicClaimVerification(_ClosedModel):
    """Content-free deterministic result ready for a server-minted receipt."""

    verifier_method: Literal["ne.grounding.claim"] = CLAIM_VERIFIER_METHOD
    verifier_version: str
    result_hash: Sha256
    claim_key: OpaqueId
    claim_content_hash: Sha256
    span_start_byte: Annotated[int, Field(ge=0)]
    span_end_byte: Annotated[int, Field(gt=0)]
    served_output_hash: Sha256
    protected_ref: OpaqueId
    protected_result_hash: Sha256
    evidence_id: OpaqueId
    locator_id: OpaqueId
    locator_hash: Sha256
    producing_event_id: OpaqueId
    delivery_receipt_id: OpaqueId
    delivery_receipt_hash: Sha256
    #: Set-shaped (verified-T3) evidence only. ``evidence_id``/``locator_id``/
    #: ``locator_hash``/``producing_event_id`` above then name the set's ANCHOR
    #: member — ``members[0]`` in the set's own server-minted order, so the choice
    #: is deterministic rather than arbitrary — because
    #: ``ne_agent_run_claim_evidence`` keys one addressable evidence row per claim.
    #: Nothing is collapsed by that: the pair below names the whole set, both are
    #: hashed into ``result_hash``, and ``_verified_set_answers`` verified EVERY
    #: member before this value existed.
    evidence_set_id: OpaqueId | None = None
    member_evidence_ids: tuple[OpaqueId, ...] = ()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_object(raw: bytes, *, label: str) -> dict[str, object]:
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClaimVerificationUnavailable(f"{label} is unavailable") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ClaimVerificationUnavailable(f"{label} is unavailable")
    return value


def _exact_keys(value: dict[str, object], expected: frozenset[str], *, label: str) -> None:
    if frozenset(value) != expected:
        raise ClaimVerificationUnavailable(f"{label} is unavailable")


def _verified_answer(*, template_id: str, columns: list[str], rows: list[list[str]]) -> object:
    return {
        "template_id": template_id,
        "template_version": 1,
        "config_hash": _sha256(template_id.encode("utf-8")),
        "columns": columns,
        "rows": rows,
        "citations": [],
        "truncated": False,
        "args": {},
    }


def _search_answers(observation: DirectSearchEvidenceObservation) -> tuple[object, ...]:
    if _sha256(observation.protected_rendering) != observation.protected_result_hash:
        raise ClaimVerificationUnavailable("protected search rendering hash mismatch")
    if _sha256(observation.locator) != observation.locator_hash:
        raise ClaimVerificationUnavailable("search locator hash mismatch")

    locator = _json_object(observation.locator, label="search locator")
    _exact_keys(
        locator,
        frozenset({"kind", "contributors", "chunk_ids", "evidence_assurance"}),
        label="search locator",
    )
    if locator["kind"] != "knowledge_search" or locator["evidence_assurance"] != "direct":
        raise ClaimVerificationUnavailable("search evidence kind or assurance mismatch")
    contributors = locator["contributors"]
    chunk_ids = locator["chunk_ids"]
    if (
        not isinstance(contributors, list)
        or not contributors
        or not all(isinstance(item, dict) and item for item in contributors)
        or not isinstance(chunk_ids, list)
        or not chunk_ids
        or not all(isinstance(item, str) and item for item in chunk_ids)
        or len(set(chunk_ids)) != len(chunk_ids)
    ):
        raise ClaimVerificationUnavailable("search locator is unavailable")

    try:
        lines = observation.protected_rendering.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ClaimVerificationUnavailable("protected search rendering is unavailable") from exc
    if not lines:
        raise ClaimVerificationUnavailable("protected search rendering is empty")
    match = _SEARCH_HEADER.fullmatch(lines[0])
    if match is None:
        raise ClaimVerificationUnavailable("protected search rendering header is unavailable")
    shown, total = (int(value) for value in match.groups())
    if shown < 1 or total < shown or shown != len(chunk_ids) or len(lines) - 1 < shown:
        raise ClaimVerificationUnavailable("protected search rendering count mismatch")

    # The body grammar is one ``{chunk_id}: {text}`` BLOCK per served chunk, in
    # the locator's exact order — and ``{text}`` is free document prose that may
    # itself span physical lines (``ne.api.tools`` renders ``f"{chunk_id}:
    # {text}"`` verbatim, and ``ne.api.workflows.parse_retrieved_text`` pins the
    # same body tolerance). So chunks are counted by id-prefix block, never by
    # line: a body line either OPENS the next expected chunk's block or
    # CONTINUES the current one. Fail-closed on ambiguity: a continuation line
    # that itself starts with ANY served chunk-id prefix (out of order, a
    # duplicate, or prose that collides with an id) cannot be attributed with
    # certainty, and silent mis-attribution — binding one chunk's words to
    # another chunk's citation — is exactly what this guard exists to prevent,
    # so it refuses rather than guesses.
    prefixes = [f"{expected_id}: " for expected_id in chunk_ids]
    blocks: list[list[str]] = []
    for line in lines[1:]:
        opened = len(blocks)
        if opened < len(prefixes) and line.startswith(prefixes[opened]):
            blocks.append([line[len(prefixes[opened]) :]])
            continue
        if not blocks or any(line.startswith(prefix) for prefix in prefixes):
            raise ClaimVerificationUnavailable("protected search chunk order mismatch")
        blocks[-1].append(line)
    if len(blocks) != len(chunk_ids):
        raise ClaimVerificationUnavailable("protected search rendering count mismatch")

    answers: list[object] = []
    for expected_id, block in zip(chunk_ids, blocks, strict=True):
        text = "\n".join(block)
        if not text:
            raise ClaimVerificationUnavailable("protected search chunk order mismatch")
        # A complete protected chunk is one grounding fact.  Reusing its exact
        # text as the column anchor is intentional: GroundingCorpus's numeral
        # binding then requires the asserted surrounding words from that same
        # chunk, rather than accepting a numeral merely because it occurred in
        # some other chunk in the protected rendering.
        answers.append(
            _verified_answer(
                template_id=f"knowledge-search-{expected_id}",
                columns=[text],
                rows=[[text]],
            )
        )
    return tuple(answers)


_ENTITY_KEYS = frozenset({"entity_class", "entity_key", "display", "assurance", "source_ref"})
_HOP_KEYS = frozenset({"subject", "edge_kind", "obj", "source_system", "source_ref", "assurance"})


def _entity(value: object) -> tuple[str, str, str, str, str]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ClaimVerificationUnavailable("graph entity is unavailable")
    _exact_keys(value, _ENTITY_KEYS, label="graph entity")
    fields = tuple(
        value[key] for key in ("entity_class", "entity_key", "display", "assurance", "source_ref")
    )
    if not all(isinstance(item, str) for item in fields):
        raise ClaimVerificationUnavailable("graph entity is unavailable")
    entity_class, entity_key, display, assurance, source_ref = fields
    if (
        not entity_class
        or not entity_key
        or not source_ref
        or assurance
        not in {
            "source",
            "confirmed",
            "extracted",
        }
    ):
        raise ClaimVerificationUnavailable("graph entity is unavailable")
    return entity_class, entity_key, display, assurance, source_ref


def _graph_answers(observation: InferredGraphPathEvidenceObservation) -> tuple[object, ...]:
    if _sha256(observation.protected_rendering) != observation.protected_result_hash:
        raise ClaimVerificationUnavailable("protected graph path hash mismatch")
    if _sha256(observation.locator) != observation.locator_hash:
        raise ClaimVerificationUnavailable("graph locator hash mismatch")
    path = _json_object(observation.protected_rendering, label="protected graph path")
    _exact_keys(path, frozenset({"hops"}), label="protected graph path")
    locator = _json_object(observation.locator, label="graph locator")
    _exact_keys(
        locator,
        frozenset({"kind", "path", "hop_source_stamps", "evidence_assurance"}),
        label="graph locator",
    )
    if (
        locator["kind"] != "knowledge_graph_path"
        or locator["evidence_assurance"] != "inferred"
        or locator["path"] != path
    ):
        raise ClaimVerificationUnavailable("protected graph path and locator mismatch")
    canonical_path = json.dumps(
        path, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if canonical_path != observation.protected_rendering:
        raise ClaimVerificationUnavailable("protected graph path is not canonical")

    hops = path["hops"]
    stamp_groups = locator["hop_source_stamps"]
    if (
        not isinstance(hops, list)
        or not hops
        or not isinstance(stamp_groups, list)
        or len(stamp_groups) != len(hops)
        or not all(
            isinstance(group, list)
            and group
            and all(isinstance(stamp, dict) and stamp for stamp in group)
            for group in stamp_groups
        )
    ):
        raise ClaimVerificationUnavailable("graph path contributors are unavailable")

    projected: list[tuple[str, ...]] = []
    narrations: list[str] = []
    for hop in hops:
        if not isinstance(hop, dict) or not all(isinstance(key, str) for key in hop):
            raise ClaimVerificationUnavailable("graph hop is unavailable")
        _exact_keys(hop, _HOP_KEYS, label="graph hop")
        subject = _entity(hop["subject"])
        obj = _entity(hop["obj"])
        edge_kind, source_system, source_ref, edge_assurance = (
            hop["edge_kind"],
            hop["source_system"],
            hop["source_ref"],
            hop["assurance"],
        )
        if (
            not all(
                isinstance(item, str)
                for item in (edge_kind, source_system, source_ref, edge_assurance)
            )
            or not edge_kind
            or not source_system
            or not source_ref
            or edge_assurance not in {"source", "confirmed", "extracted"}
        ):
            raise ClaimVerificationUnavailable("graph hop is unavailable")
        subject_name = subject[2] or subject[1]
        object_name = obj[2] or obj[1]
        narration = f"{subject_name} {edge_kind} {object_name}"
        narrations.append(narration)
        projected.append(
            (
                *subject,
                edge_kind,
                *obj,
                source_system,
                source_ref,
                edge_assurance,
                narration,
            )
        )
    path_narration = " then ".join(narrations)
    rows = [list((*row, path_narration)) for row in projected]
    fields = [
        "subject_class",
        "subject_key",
        "subject_display",
        "subject_assurance",
        "subject_source_ref",
        "edge_kind",
        "object_class",
        "object_key",
        "object_display",
        "object_assurance",
        "object_source_ref",
        "source_system",
        "edge_source_ref",
        "edge_assurance",
        "hop_narration",
        "path_narration",
    ]
    answer = _verified_answer(
        template_id="knowledge-graph-path",
        columns=fields,
        rows=rows,
    )
    narration_answer = _verified_answer(
        template_id="knowledge-graph-path-narration",
        columns=[path_narration],
        rows=[[path_narration]],
    )
    return answer, narration_answer


class _VerifiedRow:
    """The receipt identity every member of one rendered row must agree on."""

    __slots__ = ("canonical_row_hash", "contributor_count", "receipt_hash", "seen")

    def __init__(self, *, receipt_hash: str, canonical_row_hash: str, contributor_count: int):
        self.receipt_hash = receipt_hash
        self.canonical_row_hash = canonical_row_hash
        self.contributor_count = contributor_count
        self.seen: set[int] = set()


def _verified_empty_member(locator: dict[str, object]) -> tuple[str, int]:
    """Shape-check one empty-member coverage locator -> its template identity.

    Everything checked here is structural: the exact ``verified_t3_empty`` key set
    (the same closed shape ``ne.api.evidence_projection`` refuses to widen), direct
    assurance, and a well-formed receipt naming one template. The receipt's source
    authority was resolved AUTHORIZED at mint time; this member backs no rendered
    row, so no row hash exists to re-derive — the check that matters downstream is
    that it can never be COUNTED as a row.
    """

    _exact_keys(locator, _VERIFIED_EMPTY_LOCATOR_KEYS, label="verified empty-member locator")
    if locator["kind"] != "verified_t3_empty" or locator["evidence_assurance"] != "direct":
        raise ClaimVerificationUnavailable("verified empty-member kind or assurance mismatch")
    receipt = locator["receipt"]
    if not isinstance(receipt, dict) or not all(isinstance(key, str) for key in receipt):
        raise ClaimVerificationUnavailable("verified empty-member receipt is unavailable")
    receipt_hash = receipt.get("receipt_hash")
    template_id = receipt.get("template_id")
    template_version = receipt.get("template_version")
    if (
        not isinstance(receipt_hash, str)
        or _SHA256_RE.fullmatch(receipt_hash) is None
        or not isinstance(template_id, str)
        or not template_id
        or isinstance(template_version, bool)
        or not isinstance(template_version, int)
        or template_version < 1
    ):
        raise ClaimVerificationUnavailable("verified empty-member receipt is unavailable")
    return (template_id, template_version)


def _verified_receipt(locator: dict[str, object]) -> tuple[str, _VerifiedRow, tuple[str, int]]:
    """One member locator -> its row anchor, row identity and template identity."""

    _exact_keys(locator, _VERIFIED_LOCATOR_KEYS, label="verified evidence locator")
    if locator["kind"] != "verified_t3" or locator["evidence_assurance"] != "direct":
        raise ClaimVerificationUnavailable("verified evidence kind or assurance mismatch")
    receipt = locator["receipt"]
    if not isinstance(receipt, dict) or not all(isinstance(key, str) for key in receipt):
        raise ClaimVerificationUnavailable("verified row receipt is unavailable")
    receipt_hash = receipt.get("receipt_hash")
    row_anchor = locator["row_anchor"]
    if (
        not isinstance(receipt_hash, str)
        or _SHA256_RE.fullmatch(receipt_hash) is None
        or not isinstance(row_anchor, str)
        # The anchor is a pure function of the receipt hash, so a member cannot be
        # filed under a row whose receipt it does not carry.
        or row_anchor != _sha256(_ROW_ANCHOR_DOMAIN + receipt_hash.encode("ascii"))
    ):
        raise ClaimVerificationUnavailable("verified row anchor mismatch")
    template_id = receipt.get("template_id")
    template_version = receipt.get("template_version")
    canonical_row_hash = receipt.get("canonical_row_hash")
    if (
        not isinstance(template_id, str)
        or not template_id
        or isinstance(template_version, bool)
        or not isinstance(template_version, int)
        or template_version < 1
        or not isinstance(canonical_row_hash, str)
        or _SHA256_RE.fullmatch(canonical_row_hash) is None
    ):
        raise ClaimVerificationUnavailable("verified row receipt is unavailable")
    contributors = receipt.get("contributors")
    index = locator["contributor_index"]
    if (
        not isinstance(contributors, list)
        or not contributors
        or isinstance(index, bool)
        or not isinstance(index, int)
        or not 0 <= index < len(contributors)
        # The member's own contributor must be the one the receipt lists at its
        # declared position: a member cannot substitute a different authority.
        or contributors[index] != locator["contributor"]
    ):
        raise ClaimVerificationUnavailable("verified row contributor mismatch")
    row = _VerifiedRow(
        receipt_hash=receipt_hash,
        canonical_row_hash=canonical_row_hash,
        contributor_count=len(contributors),
    )
    row.seen.add(index)
    return row_anchor, row, (template_id, template_version)


def _verified_set_rows(
    observation: VerifiedRowSetEvidenceObservation,
) -> tuple[list[_VerifiedRow], tuple[str, int]]:
    """Every member decoded and checked; the ordered rows the set actually covers."""

    order: list[str] = []
    rows: dict[str, _VerifiedRow] = {}
    templates: set[tuple[str, int]] = set()
    for member in observation.members:
        if _sha256(member.locator) != member.locator_hash:
            raise ClaimVerificationUnavailable("verified evidence locator hash mismatch")
        locator = _json_object(member.locator, label="verified evidence locator")
        if locator.get("kind") == "verified_t3_empty":
            # Empty-member COVERAGE evidence (NE-KG-MULTIKEY): shape-checked, its
            # template joins the one-template invariant, and it contributes NO row —
            # it attests which requested keys matched nothing, which the rendering
            # carries as an envelope note, never as a row line.
            templates.add(_verified_empty_member(locator))
            continue
        anchor, parsed, template = _verified_receipt(locator)
        templates.add(template)
        existing = rows.get(anchor)
        if existing is None:
            order.append(anchor)
            rows[anchor] = parsed
            continue
        if (
            existing.receipt_hash != parsed.receipt_hash
            or existing.canonical_row_hash != parsed.canonical_row_hash
            or existing.contributor_count != parsed.contributor_count
        ):
            raise ClaimVerificationUnavailable("verified row receipt is not consistent")
        index = next(iter(parsed.seen))
        if index in existing.seen:
            raise ClaimVerificationUnavailable("verified row contributor is duplicated")
        existing.seen.add(index)
    if len(templates) != 1:
        raise ClaimVerificationUnavailable("verified evidence set spans several templates")
    for parsed in rows.values():
        # COMPLETENESS. Every contributor the row receipt lists must be present as
        # its own member, so a set cannot be served with authorities missing.
        if parsed.seen != set(range(parsed.contributor_count)):
            raise ClaimVerificationUnavailable("verified row contributor set is incomplete")
    return [rows[anchor] for anchor in order], next(iter(templates))


def _verified_set_answers(observation: VerifiedRowSetEvidenceObservation) -> tuple[object, ...]:
    """The corpus for a verified (T3) read: the WHOLE served table, row-bound.

    The rendering is parsed back into its columns and rows and then each parsed row
    is re-hashed exactly as ``ne.brain_ontology.verified.answers`` hashed it when it
    minted the row receipt — ``sha256(json.dumps([columns, row], separators=(",",
    ":")))``. That makes the parse self-verifying: a cell that happened to contain
    the column separator, a reordered table, a substituted row or a rendering that
    is not the one the receipts cover all fail the hash rather than reaching the
    grounding corpus.

    NOTE the corpus is built with no ``args``, exactly as the search and GraphPath
    paths above are: the verified answer's argument echo is not recoverable from
    the rendering, so argument-named columns are not filtered out of the corpus
    here the way ``ne.runtime.grounding`` filters them at generation time. Those
    bytes were still served to the model under this same protected result, so this
    remains "grounded in exactly what was served" — it is a narrower corpus filter,
    never a wider disclosure.
    """

    if _sha256(observation.protected_rendering) != observation.protected_result_hash:
        raise ClaimVerificationUnavailable("protected verified rendering hash mismatch")
    receipt_rows, (template_id, _) = _verified_set_rows(observation)

    try:
        text = observation.protected_rendering.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClaimVerificationUnavailable("protected verified rendering is unavailable") from exc
    lines = text.split("\n")
    if len(lines) < 3:
        raise ClaimVerificationUnavailable("protected verified rendering is unavailable")
    match = _VERIFIED_HEADER.fullmatch(lines[0])
    if match is None:
        raise ClaimVerificationUnavailable("protected verified rendering header is unavailable")
    returned = int(match.group(1))
    columns = lines[1].split(" | ")
    if lines[2] != "-+-".join("-" * len(column) for column in columns):
        raise ClaimVerificationUnavailable("protected verified rendering columns mismatch")
    row_lines = lines[3:]
    # ``render_answer_text`` may append ENVELOPE lines after the row table — the
    # zero-row scoping note, and (NE-KG-MULTIKEY) the empty-member note naming the
    # requested keys of a multi-key read that matched nothing. They are commentary on
    # the result, never rows, and they carry no receipt. Drop them ONLY while the line
    # count still EXCEEDS the header's ``returned`` count, so a note can never displace
    # a real row: when the counts already agree nothing is stripped, which makes this a
    # no-op on every rendering that carries no note (byte-identical to before).
    while (
        len(row_lines) > returned
        and row_lines[-1].startswith("[note: ")
        and row_lines[-1].endswith("]")
    ):
        row_lines.pop()
    if returned != len(row_lines) or returned != len(receipt_rows):
        raise ClaimVerificationUnavailable("protected verified rendering row count mismatch")
    rows: list[list[str]] = []
    for line, receipt_row in zip(row_lines, receipt_rows, strict=True):
        row = line.split(" | ")
        material = json.dumps([columns, row], separators=(",", ":")).encode("utf-8")
        if _sha256(material) != receipt_row.canonical_row_hash:
            raise ClaimVerificationUnavailable("verified row is not the row its receipt covers")
        rows.append(row)
    # NE-CLAIM-LINEFACT. The parsed-table answer above is the row-bound corpus,
    # but its phrases are PER CELL — while the server's span selector
    # (``ne.runtime.runner._server_bound_claims``) binds the longest verbatim run
    # common to the answer and THIS RENDERING, whose every >=5-word run crosses
    # ``" | "`` cell boundaries (a cell is one or two words). Grading a
    # rendering-contiguous span against a per-cell phrase corpus therefore
    # refused EVERY table-backed claim as ``phrase-recombination`` by
    # construction, and its row-ID-anchored cell numerals as
    # ``numeral-ungrounded`` (a row whose only identity is a compound identifier
    # has no prose anchor to offer F3). Measured live 2026-08-27: 10/10 runs of
    # three demo scenarios, including a CORRECT refusal, died here.
    #
    # So each served LINE is additionally minted as one single-cell answer —
    # exactly the construction :func:`_search_answers` gives a served chunk ("a
    # complete protected chunk is one grounding fact"), applied to the only
    # other model-facing rendering. A verbatim slice of a served line is then
    # what it claims to be: contiguous in something the model was actually
    # shown. This widens nothing beyond the rendering's own contiguity: a
    # cross-ROW stitch is contiguous in no line and still refuses, a numeral
    # attributed to a row whose line the sentence does not quote still refuses
    # (the line-fact's anchors and ``row_tokens`` are that line's own tokens),
    # and a numeral or compound the rendering nowhere carries still refuses.
    # The dash separator line (``lines[2]``) folds to zero tokens and is
    # skipped; header, column and note lines are served text and included.
    table = _verified_answer(template_id=template_id, columns=columns, rows=rows)
    line_answers = tuple(
        _verified_answer(template_id=template_id, columns=[line], rows=[[line]])
        for index, line in enumerate(lines)
        if line and index != 2
    )
    return (table, *line_answers)


def rendering_preview_answers(text: str) -> tuple[object, ...] | None:
    """The UNVERIFIED corpus preview of a verified-T3 rendering — SPAN SELECTION ONLY.

    ``ne.runtime.runner._server_bound_claims`` needs to know, BEFORE binding,
    whether a candidate sentence of the answer would ground against a table
    source's served rendering. It holds only the retained delivered text — no
    receipts, no locators — so this helper parses that text into the same answer
    shapes :func:`_verified_set_answers` builds (the parsed table plus one
    single-cell answer per served line, NE-CLAIM-LINEFACT), with two deliberate
    differences that keep it STRICTLY NARROWER than the verifier's corpus:

    * ``template_id`` is the empty string, so the template-id vocabulary the
      verifier's corpus carries (phrase + count/envelope anchors) is absent here.
      Every token, phrase and fact this preview mints is therefore also minted by
      the verifier's corpus — a sentence that grounds against the preview grounds
      at verification, while the reverse may conservatively fail to bind.
    * nothing is hash-checked. That is safe ONLY because this preview never
      verifies anything: it selects which answer bytes are worth binding, and
      ``verify_claim`` re-parses the sealed observation with full receipt force
      before any claim is accepted.

    Returns ``None`` when ``text`` is not rendering-shaped (a search rendering, a
    graph JSON, arbitrary prose) — the caller then simply has no sentence path
    for that source. A malformed row line (wrong cell count) also returns
    ``None``: fail closed, never guess a table out of non-table bytes.
    """
    lines = text.split("\n")
    if len(lines) < 3 or _VERIFIED_HEADER.fullmatch(lines[0]) is None:
        return None
    columns = lines[1].split(" | ")
    if lines[2] != "-+-".join("-" * len(column) for column in columns):
        return None
    rows: list[list[str]] = []
    for line in lines[3:]:
        if not line or (line.startswith("[note: ") and line.endswith("]")):
            continue
        row = line.split(" | ")
        if len(row) != len(columns):
            return None
        rows.append(row)
    table = _verified_answer(template_id="", columns=columns, rows=rows)
    line_answers = tuple(
        _verified_answer(template_id="", columns=[line], rows=[[line]])
        for index, line in enumerate(lines)
        if line and index != 2
    )
    return (table, *line_answers)


def _claim_text(claim: ClaimSlice) -> str:
    if _sha256(claim.served_output) != claim.served_output_hash:
        raise ClaimVerificationUnavailable("served output hash mismatch")
    if claim.span_end_byte > len(claim.served_output):
        raise ClaimVerificationUnavailable("claim span is outside served output")
    selected = claim.served_output[claim.span_start_byte : claim.span_end_byte]
    if _sha256(selected) != claim.claim_content_hash:
        raise ClaimVerificationUnavailable("claim content hash mismatch")
    try:
        claim.served_output[: claim.span_start_byte].decode("utf-8")
        selected_text = selected.decode("utf-8")
        claim.served_output[claim.span_end_byte :].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClaimVerificationUnavailable("claim span is not an exact UTF-8 slice") from exc
    if not selected_text.strip():
        raise ClaimVerificationUnavailable("claim slice is empty")
    return selected_text


def verify_claim(
    claim: ClaimSlice, observation: EvidenceObservation
) -> DeterministicClaimVerification:
    """Verify one exact claim against one exact server-observed evidence observation."""

    if not isinstance(claim, ClaimSlice):
        raise TypeError("claim must be ClaimSlice")
    evidence_set_id: str | None = None
    member_evidence_ids: tuple[str, ...] = ()
    if isinstance(observation, DirectSearchEvidenceObservation):
        answers = _search_answers(observation)
        evidence_id = observation.evidence_id
        locator_id = observation.locator_id
        locator_hash = observation.locator_hash
        producing_event_id = observation.producing_event_id
    elif isinstance(observation, InferredGraphPathEvidenceObservation):
        answers = _graph_answers(observation)
        evidence_id = observation.evidence_id
        locator_id = observation.locator_id
        locator_hash = observation.locator_hash
        producing_event_id = observation.producing_event_id
    elif isinstance(observation, VerifiedRowSetEvidenceObservation):
        answers = _verified_set_answers(observation)
        evidence_set_id = observation.evidence_set_id
        member_evidence_ids = tuple(member.evidence_id for member in observation.members)
        anchor = observation.members[0]
        evidence_id = anchor.evidence_id
        locator_id = anchor.locator_id
        locator_hash = anchor.locator_hash
        producing_event_id = anchor.producing_event_id
    else:
        raise TypeError("observation must be a closed evidence observation")
    claim_text = _claim_text(claim)
    corpus = GroundingCorpus.from_answers(answers)
    if not corpus.tokens:
        raise ClaimVerificationUnavailable("evidence corpus is empty")
    verdict = check_grounding(claim_text, corpus, "", profile=PROFILE_NG2)
    if not verdict.passed or verdict.prompt_echo_count:
        raise ClaimVerificationUnavailable(_grounding_detail(verdict))

    material = {
        "domain": "ne.run-claim-verifier-result.v1",
        "verifier_method": CLAIM_VERIFIER_METHOD,
        "verifier_version": CLAIM_VERIFIER_VERSION,
        "claim_key": claim.claim_key,
        "claim_content_hash": claim.claim_content_hash,
        "span_start_byte": claim.span_start_byte,
        "span_end_byte": claim.span_end_byte,
        "served_output_hash": claim.served_output_hash,
        "read_kind": observation.read_kind,
        "assurance": observation.assurance,
        "protected_ref": observation.protected_ref,
        "protected_result_hash": observation.protected_result_hash,
        "evidence_id": evidence_id,
        "locator_id": locator_id,
        "locator_hash": locator_hash,
        "producing_event_id": producing_event_id,
        "delivery_receipt_id": observation.delivery_receipt_id,
        "delivery_receipt_hash": observation.delivery_receipt_hash,
        # Set-shaped evidence binds the WHOLE set into the receipt: the anchor
        # above is an address, these are the members that were actually verified.
        "evidence_set_id": evidence_set_id,
        "member_evidence_ids": list(member_evidence_ids),
    }
    result_hash = _sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return DeterministicClaimVerification(
        verifier_version=CLAIM_VERIFIER_VERSION,
        result_hash=result_hash,
        claim_key=claim.claim_key,
        claim_content_hash=claim.claim_content_hash,
        span_start_byte=claim.span_start_byte,
        span_end_byte=claim.span_end_byte,
        served_output_hash=claim.served_output_hash,
        protected_ref=observation.protected_ref,
        protected_result_hash=observation.protected_result_hash,
        evidence_id=evidence_id,
        locator_id=locator_id,
        locator_hash=locator_hash,
        producing_event_id=producing_event_id,
        delivery_receipt_id=observation.delivery_receipt_id,
        delivery_receipt_hash=observation.delivery_receipt_hash,
        evidence_set_id=evidence_set_id,
        member_evidence_ids=member_evidence_ids,
    )
