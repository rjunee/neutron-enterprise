# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ``VerifiedAnswer`` artifact + the grounded, cited, C7-trimmed execution (NE-085c).

``execute_template`` is the request-time core: it validates the caller args against
the template's strict generated model, builds the C7 :class:`DatasetSpec` from the
principal's labels (trusted code, never the caller), renders the parameterized query
(``VALUES``/n3 binding — never string interpolation), runs it through the gateway's
``sparql_scoped`` permission-trimmed read path, and returns a frozen
:class:`VerifiedAnswer` carrying the structured rows + per-row citation IRIs (the
deterministic ``urn:ne:entity:<key>`` T2 join the lift minted).

Provenance double-check (defense in depth, §11.4): after extracting the citations, a
SECOND scoped ``SELECT`` over the SAME dataset asserts every cited IRI is actually
inside the principal's graph set — a belt against graph-assignment drift.

Replay honesty (§11.5): the artifact + audit detail carry ``template@version`` / args
/ ``config_hash`` / ``ontology_version``, so a replay is PROCEDURALLY identical; the
STRUCTURED result is bit-identical ONLY when the touched graphs are unchanged (an
append to a read graph legitimately changes the rows). :func:`answers_equal` compares
everything EXCEPT ``executed_at`` — no temporal-query claim is made (that is NE-083's
lane), and the ``ne verify-answer`` CLI is explicitly OUT of this unit.

NE-164b read-path auditing: ``execute_template`` optionally emits ONE config_hash-
stamped ``knowledge.verified_read`` ``AuditEvent`` per COMPLETED execution via an
injected :class:`~ne.contracts.governance.AuditSink` (mirrors the T1/T2
``knowledge.retrieve``/``knowledge.graph_read`` pattern). Unlike those two, this
event carries NO ``acl_trim_delta`` — the C7 trim happens A-PRIORI via the
:class:`DatasetSpec` graph scoping above (the query can never see a forbidden
graph in the first place), so there is no countable trimmed-row delta to report;
see the docstring on ``execute_template`` for the exact detail format.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from ne.brain_ontology.ontology import OntologyVersion
from ne.brain_ontology.store.protocols import DatasetSpec
from ne.brain_ontology.verified.errors import (
    VerifiedAnswerArgsError,
    VerifiedAnswerCountError,
    VerifiedAnswerScopeError,
)
from ne.brain_ontology.verified.scope import (
    GroupExpander,
    release_scoped_dataset,
    scoped_dataset,
    source_authorized_dataset,
)
from ne.brain_ontology.verified.selected_release import SelectedOxigraphRelease
from ne.brain_ontology.verified.templates import (
    QueryTemplate,
    describe_invalid_args,
    effective_limit,
    list_members,
    render_count_query,
    render_query,
)
from ne.contracts.governance import AuditSink
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReader,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    VerifiedEmptyResultReceipt,
    VerifiedRowContributorReceipt,
    canonical_source_authority_stamps,
)
from ne.contracts.tokens import ByteTokenEstimator, TokenEstimator
from ne.contracts.types import Principal
from ne.events import new_audit_event
from ne.schema import AuditOutcome

__all__ = [
    "ZERO_ROWS_NOTE",
    "VerifiedAnswer",
    "VerifiedReadStore",
    "answers_equal",
    "check_citations_in_dataset",
    "empty_members_note",
    "execute_template",
    "ontology_version_string",
    "render_answer_text",
]

# An IRI has a scheme and no whitespace/angle delimiters — enough to reject a literal
# value smuggled into the citation column before the provenance double-check runs.
_IRI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:[^\s<>\"{}|^`\\]*$")

#: NE-CTXB-5: the shared, empirical-default byte estimator (ne.contracts.tokens,
#: merged in NE-CTXB-1). Constructed with NO ``bytes_per_token`` override so it
#: inherits ``DEFAULT_BYTES_PER_TOKEN`` — the oracle's selection (ADR-0012), never
#: a re-declared divisor. Token packing is a DEFAULT-OFF path (``max_result_tokens``
#: is ``None`` unless a caller threads a budget), so importing this estimator adds
#: no per-read cost on the currently-green paths.
_DEFAULT_ESTIMATOR = ByteTokenEstimator()


@runtime_checkable
class VerifiedReadStore(Protocol):
    """The narrow read surface a verified answer consumes — the gateway satisfies it.

    Typed as a package-local Protocol so ``answers`` need not import the (rdflib-heavy)
    gateway module; the injected object is always the frozen ``ValidatingTripleStore``.
    """

    async def sparql_scoped(
        self, query: str, *, dataset: DatasetSpec
    ) -> Sequence[Mapping[str, str]]: ...

    def ontology_version(self) -> OntologyVersion: ...


@runtime_checkable
class _ReleaseVerifiedReadStore(VerifiedReadStore, Protocol):
    """The selected-release extension used by source-bearing T3 reads."""

    @property
    def release(self) -> SelectedOxigraphRelease: ...

    @property
    def release_id(self) -> str: ...


class VerifiedAnswer(BaseModel):
    """The frozen, citation-carrying result of one verified-answer execution (§11.5)."""

    model_config = ConfigDict(frozen=True)

    template_id: str
    template_version: int
    #: The VALIDATED, typed args (JSON-shaped) — never the raw model text.
    args: dict[str, Any]
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    #: Per-row provenance IRI (the ``urn:ne:entity:<key>`` T2 join) from citation_var.
    citations: tuple[str, ...]
    contributor_receipts: tuple[VerifiedRowContributorReceipt, ...] = ()
    empty_result_receipt: VerifiedEmptyResultReceipt | None = None
    #: NE-KG-MULTIKEY. ``None`` for EVERY scalar call (which is every call a template
    #: without a multi-key argument can make) — the three fields below are inert, and
    #: the answer is byte-identical to the pre-feature shape apart from their defaults.
    #: Set to the multi-key argument's NAME when the caller supplied it as a list.
    list_arg: str | None = None
    #: The requested members that matched ZERO rows, sorted. Meaningful ONLY when
    #: ``empty_members_verified`` is True: an empty tuple otherwise means "not
    #: computed", never "every member matched". That distinction is the whole point —
    #: an unqualified empty tuple would be a silent claim of completeness.
    empty_members: tuple[str, ...] = ()
    #: True IFF the empty-member set difference actually RAN — i.e. this was a list
    #: call AND the read was complete (untruncated). Under truncation a member with no
    #: rows in the KEPT prefix may still have rows beyond the cap, so the claim is not
    #: available and is not made (fail closed; §0-law-5 — never a fabricated fact).
    empty_members_verified: bool = False
    #: Source-authority evidence for the empty-member claim, minted on the SAME
    #: contributor snapshot as :attr:`empty_result_receipt` and carrying the same
    #: args/dataset hashes. Present only when rows were returned AND some member was
    #: empty; a wholly-empty list read mints ``empty_result_receipt`` instead.
    empty_member_receipt: VerifiedEmptyResultReceipt | None = None
    #: The VALIDATED caller-chosen date window ``(start, end)`` echoed from the
    #: template's declared ``date_window`` args, or ``None`` when the template
    #: declares none. This exists so the empty-member note can say WHOSE window
    #: an absence is relative to: "no rows for this key" under a caller-authored
    #: window is a fact about that window only, and rendering the window makes the
    #: claim self-describing instead of leaving "the relevant window" to prose.
    #: C12-safe by construction: both values passed pydantic ``date`` validation.
    window: tuple[str, str] | None = None
    truncated: bool
    row_limit: int
    #: NE-CTXB-5 envelope (§4.1): the number of rows the model/caller actually
    #: sees. A ``model_validator`` pins ``returned_rows == len(rows)`` so the
    #: header count can never drift from the payload (R3-P1-4).
    returned_rows: int
    #: The trusted total-row count AS OF count time, or ``None`` when unknown.
    #: DECIDED (§4.1): an UNtruncated read is a complete enumeration, so
    #: ``total_rows == returned_rows`` is an EXACT trusted count, never fabricated.
    #: Under truncation it is ``None`` UNLESS the template opts into NE-CTXB-7's
    #: trusted COUNT rewrite (``count_total_rows``), which supplies an as-of-count-time
    #: total (or ``None`` on probe timeout) — never a model- or prose-derived number (C12).
    total_rows: int | None = None
    #: Why the answer was truncated, when it was: ``'rows'`` (the trusted store→app
    #: LIMIT cap) or ``'tokens'`` (the app→model packing budget). ``None`` ⇔ not
    #: truncated. Packing is DEFAULT-OFF at this unit's merge, so ``'tokens'`` never
    #: appears until a deployment sets ``verified_max_result_tokens`` (NE-CTXB-8).
    truncation_reason: Literal["rows", "tokens"] | None = None
    config_hash: str
    ontology_version: str
    executed_at: datetime

    @model_validator(mode="after")
    def _envelope_consistent(self) -> VerifiedAnswer:
        # STRUCTURAL lockstep pins (never convention): the header count and the
        # citation column are packed 1:1 with the rows the model can see, so an
        # off-by-one packing slice fails loud at construction, not in review.
        if self.returned_rows != len(self.rows):
            raise ValueError(
                f"returned_rows={self.returned_rows} disagrees with len(rows)={len(self.rows)} "
                "(the envelope header count must equal the payload — R3-P1-4)"
            )
        if len(self.citations) != len(self.rows):
            raise ValueError(
                f"len(citations)={len(self.citations)} disagrees with len(rows)={len(self.rows)} "
                "(citations are packed in lockstep with rows — §4.1)"
            )
        if self.contributor_receipts and len(self.contributor_receipts) != len(self.rows):
            raise ValueError("every verified row requires exactly one contributor receipt")
        if self.empty_result_receipt is not None and self.rows:
            raise ValueError("an empty-result receipt cannot accompany returned rows")
        if self.empty_result_receipt is not None and self.contributor_receipts:
            raise ValueError("row and empty-result receipts are mutually exclusive")
        # NE-KG-MULTIKEY structural pins — the empty-member claim can never be
        # constructible in a shape that reads as a completeness statement it did not earn.
        if self.empty_members and not self.empty_members_verified:
            raise ValueError(
                "empty_members is populated but empty_members_verified is False — an "
                "unverified empty-member set is not a claim this artifact may carry"
            )
        if self.empty_members_verified and self.list_arg is None:
            raise ValueError("empty_members_verified requires the list_arg it was computed for")
        if self.empty_members_verified and self.truncated:
            raise ValueError(
                "empty_members_verified cannot accompany a truncated read — a member absent "
                "from a truncated prefix may still have rows beyond the cap"
            )
        if len(set(self.empty_members)) != len(self.empty_members):
            raise ValueError("empty_members must be distinct")
        if tuple(sorted(self.empty_members)) != self.empty_members:
            raise ValueError("empty_members must be sorted (deterministic rendering)")
        if self.empty_member_receipt is not None and not self.empty_members:
            raise ValueError("an empty-member receipt requires at least one empty member")
        if self.empty_member_receipt is not None and self.empty_result_receipt is not None:
            raise ValueError("empty-member and empty-result receipts are mutually exclusive")
        return self


def ontology_version_string(version: OntologyVersion) -> str:
    """A stable digest of the :class:`OntologyVersion` (canonical sorted JSON → sha256)."""
    payload = json.dumps(version.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_iri(value: str) -> bool:
    return bool(_IRI_RE.match(value))


async def check_citations_in_dataset(
    store: VerifiedReadStore, dataset: DatasetSpec, citations: Sequence[str]
) -> None:
    """Raise :class:`VerifiedAnswerScopeError` if any cited IRI is NOT inside ``dataset``.

    The provenance double-check (§11.4): a SECOND scoped ``SELECT`` over the SAME
    dataset confirms each cited subject actually has a triple the principal may see.
    """
    unique = sorted(set(citations))
    if not unique:
        return
    values = " ".join(f"<{iri}>" for iri in unique)
    probe = f"SELECT DISTINCT ?s WHERE {{ VALUES ?s {{ {values} }} ?s ?p ?o }}"
    rows = await store.sparql_scoped(probe, dataset=dataset)
    present = {row.get("s") for row in rows}
    missing = [iri for iri in unique if iri not in present]
    if missing:
        raise VerifiedAnswerScopeError(
            f"provenance double-check failed: cited IRIs {missing!r} are not inside the "
            "principal's permitted graph set (graph-assignment drift — fail closed)"
        )


def _parse_ne_total(count_rows: Sequence[Mapping[str, str]]) -> int:
    """Parse the trusted COUNT(*) probe result → a non-negative int, or fail closed.

    A malformed result (not exactly one row, missing/blank ``ne_total``, non-integer,
    or negative) raises :class:`VerifiedAnswerCountError` — NEVER a guessed total
    (SPEC §0-law-5: never fake a computed value).
    """
    if len(count_rows) != 1:
        raise VerifiedAnswerCountError(
            f"total_rows COUNT probe returned {len(count_rows)} rows, expected exactly 1 "
            "(fail closed — never a fabricated total)"
        )
    raw = count_rows[0].get("ne_total")
    if raw is None or raw == "":
        raise VerifiedAnswerCountError(
            "total_rows COUNT probe row is missing the ne_total binding (fail closed)"
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise VerifiedAnswerCountError(
            f"total_rows COUNT probe ne_total {raw!r} is not an integer (fail closed)"
        ) from exc
    if value < 0:
        raise VerifiedAnswerCountError(
            f"total_rows COUNT probe ne_total {value} is negative (fail closed)"
        )
    return value


async def execute_template(
    template: QueryTemplate,
    args: Mapping[str, object],
    *,
    store: VerifiedReadStore,
    expander: GroupExpander,
    principal: Principal,
    config_hash: str,
    max_rows: int,
    audit_sink: AuditSink | None = None,
    trace_id: str | None = None,
    max_result_tokens: int | None = None,
    estimator: TokenEstimator | None = None,
    count_timeout_s: float = 2.0,
    contributor_stamp_provider: Callable[[str], Awaitable[SourceAuthorityStamp | None]]
    | None = None,
    contributor_snapshot_provider: Callable[
        [tuple[str, ...]], Awaitable[Sequence[SourceAuthorityStamp]]
    ]
    | None = None,
    source_authority: CurrentSourceAuthorityReader | None = None,
    authority_access_context_hash: str = "",
    authorized_contributor_graphs: Sequence[str] | None = None,
    require_contributor_receipts: bool = False,
) -> VerifiedAnswer:
    """Validate args → C7 scope → parameterized read → grounded, cited VerifiedAnswer.

    NE-164b: when an ``audit_sink`` is injected, a COMPLETED execution (i.e. one
    that reached the ``return`` below) emits exactly ONE ``knowledge.verified_read``
    ``AuditEvent`` carrying ``rows=<n> row_limit=<l> truncated=<bool>`` — COUNTS
    ONLY, never template args, row VALUES, citation IRIs, or ``template_id`` (the
    template identity already rides the executor's ``tool.execute`` event via
    ``extract_audit_detail`` at the ne-api seam; duplicating it here would be a
    redundant C7/C12 surface).

    There is deliberately NO ``acl_trim_delta`` field on this event (unlike the
    T1/T2 read-path events): the C7 trim happens A-PRIORI via the
    :func:`scoped_dataset` graph scoping above — the rendered query can never see
    a forbidden graph in the first place, so there is no countable trimmed-row
    delta to report; fabricating one would misrepresent the trimming mechanism.

    Fail-closed (NE-CKD6, mirrors NE-164/NE-164b's T1/T2 read paths): a sink
    ``append`` failure PROPAGATES — surfacing to the caller as a
    ``ToolExecutionError``/502 at the ne-api route, never a silently-unaudited
    read. A FAILED execution (args validation, scope, or the provenance
    double-check) emits NOTHING: the refusal/exception precedes the audit
    decision below, exactly like NE-164's fail-closed branches.

    NE-CTXB-7 total_rows (opt-in per template via ``count_total_rows``): under
    truncation ONLY, a THIRD scoped read (``render_count_query``) fills
    ``total_rows`` with a trusted ``COUNT(*)`` over the SAME C7 dataset —
    AS-OF-COUNT-TIME semantics: a second, UNsnapshotted read, so on a live store
    the rows and the total may reflect different snapshots (exact on fixtures;
    NO test or arc asserts rows-vs-total equality live). ``count_timeout_s`` (the
    DEDICATED read timeout, ≤2s §5.3 budget, distinct from Fuseki's
    write-contention retry) bounds it; a timeout leaves ``total_rows=None`` (an
    honest unknown, never fabricated). An untruncated read never runs the probe.
    """
    try:
        validated = template.args_model.model_validate(dict(args))
    except ValidationError as exc:
        # Defense-in-depth re-validation behind the pre-gate ``validate_args`` seam
        # (tools.py). C12: the message names fields + constraints (and, for a
        # declared enum, the permitted values — already-published schema) via the
        # ONE composer; the rejected value is never echoed, and no query has run.
        # ``from None`` severs the chain: pydantic's rendering embeds the rejected
        # value as ``input_value=...``, so a chained cause would resurface it in
        # ``traceback.format_exception`` output (any logger.exception / Sentry /
        # OTel record_exception). Chain-swept by test.
        raise VerifiedAnswerArgsError(describe_invalid_args(template, exc)) from None
    try:
        template.validate_semantic_args(validated)
    except ValueError as exc:
        raise VerifiedAnswerArgsError(
            f"invalid arguments for {template.tool_name!r}: {exc}. "
            "The supplied values are not echoed (C12). No query was executed — "
            "this is a refusal, not an empty result."
        ) from None
    typed_args = {name: getattr(validated, name) for name in template.args}
    json_args = validated.model_dump(mode="json")
    # NE-KG-MULTIKEY: ``None`` on every scalar call (the single discriminator shared
    # with the renderer, so "did this bind a list?" has exactly one answer).
    requested_members = list_members(template, typed_args)
    # The caller-chosen window, echoed from the ALREADY-VALIDATED args so the
    # renderer can bind "returned no rows" to the window it was measured under.
    window: tuple[str, str] | None = None
    if template.date_window is not None:
        window = (
            str(json_args[template.date_window.start_arg]),
            str(json_args[template.date_window.end_arg]),
        )

    selected_release_store = store if isinstance(store, _ReleaseVerifiedReadStore) else None
    source_evidence_bound = selected_release_store is not None or require_contributor_receipts
    if (
        template.answer.source_status == "source_bearing"
        and source_evidence_bound
        and (
            contributor_stamp_provider is None
            or source_authority is None
            or not authorized_contributor_graphs
        )
    ):
        raise VerifiedAnswerScopeError("source_authority_unavailable")

    if selected_release_store is not None:
        dataset = release_scoped_dataset(
            selected_release_store.release,
            principal,
            expander,
            template,
            authorized_contributor_graphs=authorized_contributor_graphs,
        )
    elif require_contributor_receipts:
        dataset = source_authorized_dataset(
            principal,
            expander,
            template,
            authorized_contributor_graphs=authorized_contributor_graphs or (),
        )
    else:
        dataset = scoped_dataset(principal, expander, template)

    async def _mint_no_rows_receipt() -> VerifiedEmptyResultReceipt:
        """The source-authority evidence backing a "these rows do not exist" claim.

        ONE minting path, used by BOTH no-rows claims this executor can make: the
        whole-read empty result, and (NE-KG-MULTIKEY) the per-member empty set of a
        multi-key read. Both rest on exactly the same evidence — the same C7 dataset,
        the same validated args, the same authorized contributor snapshot resolved
        AUTHORIZED at mint time — so they mint identically rather than diverging.
        """
        if contributor_snapshot_provider is None or source_authority is None:
            raise VerifiedAnswerScopeError("source_authority_unavailable")
        snapshot = canonical_source_authority_stamps(
            await contributor_snapshot_provider(tuple(dataset.default_graphs))
        )
        if not snapshot:
            raise VerifiedAnswerScopeError("source_authority_unavailable")
        for stamp in snapshot:
            resolution = await source_authority.resolve(
                stamp,
                principal_id=principal.id,
                access_context_hash=authority_access_context_hash,
            )
            if resolution.decision is not SourceAuthorityDecision.AUTHORIZED:
                raise VerifiedAnswerScopeError("source_authority_unavailable")
        return VerifiedEmptyResultReceipt(
            template_id=template.template_id,
            template_version=template.version,
            canonical_args_hash=hashlib.sha256(
                json.dumps(json_args, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            dataset_graphs_hash=hashlib.sha256(
                json.dumps(sorted(dataset.default_graphs), separators=(",", ":")).encode()
            ).hexdigest(),
            contributor_schema_version=1,
            contributors=snapshot,
        )

    query = render_query(template, typed_args, max_rows)
    raw_rows = await store.sparql_scoped(query, dataset=dataset)

    limit = effective_limit(template, max_rows)
    row_truncated = len(raw_rows) > limit
    kept = list(raw_rows[:limit])

    columns = template.answer.columns
    citation_var = template.answer.citation_var
    rows: list[tuple[str, ...]] = []
    citations: list[str] = []
    contributor_receipts: list[VerifiedRowContributorReceipt] = []
    for row in kept:
        output_row = tuple(row.get(col, "") for col in columns)
        rows.append(output_row)
        cite = row.get(citation_var, "")
        if not _is_iri(cite):
            raise VerifiedAnswerScopeError(
                f"template {template.template_id!r} citation_var {citation_var!r} produced a "
                f"non-IRI value {cite!r} — a citation must be a provenance IRI"
            )
        citations.append(cite)
        if template.answer.source_status == "source_bearing" and source_evidence_bound:
            if contributor_stamp_provider is None or source_authority is None:
                raise VerifiedAnswerScopeError("source_authority_unavailable")
            stamps: list[SourceAuthorityStamp] = []
            for contributor_var in template.answer.contributor_vars:
                contributor = row.get(contributor_var, "")
                if not _is_iri(contributor):
                    raise VerifiedAnswerScopeError("source_authority_unavailable")
                stamp = await contributor_stamp_provider(contributor)
                if stamp is None:
                    raise VerifiedAnswerScopeError("source_authority_unavailable")
                resolution = await source_authority.resolve(
                    stamp,
                    principal_id=principal.id,
                    access_context_hash=authority_access_context_hash,
                )
                if resolution.decision is not SourceAuthorityDecision.AUTHORIZED:
                    raise VerifiedAnswerScopeError("source_authority_unavailable")
                stamps.append(stamp)
            contributor_receipts.append(
                VerifiedRowContributorReceipt(
                    template_id=template.template_id,
                    template_version=template.version,
                    canonical_row_hash=hashlib.sha256(
                        json.dumps([columns, output_row], separators=(",", ":")).encode()
                    ).hexdigest(),
                    contributor_schema_version=1,
                    contributors=tuple(stamps),
                )
            )

    empty_result_receipt: VerifiedEmptyResultReceipt | None = None
    if not rows and template.answer.source_status == "source_bearing" and source_evidence_bound:
        empty_result_receipt = await _mint_no_rows_receipt()

    # Token packing (§4.2, DEFAULT-OFF): trim the ALREADY row-capped set to the
    # app→model token budget, in QUERY order (the trusted ORDER BY made that order
    # deterministic — §4.4), citations sliced in LOCKSTEP. Always keep >= 1 row if
    # any exist so a single oversized row is still an honest, non-empty answer.
    # ``max_result_tokens is None`` (the merge-time default on every seam) leaves
    # ``rows``/``citations`` untouched — byte-identical to the row-cap-only path,
    # which is what pins "merging changes no truncation behavior" (R2-P1-4).
    #
    # The budget bounds the FULL model-facing rendering (:func:`render_answer_text`),
    # not just the row VALUES (C1): before packing we pre-charge the fixed frame the
    # model actually sees — the envelope header line, the ``col | col`` header, and
    # the separator rule — and each candidate row is estimated WITH its joining
    # newline exactly as it will be rendered. Estimating the frame and each row as
    # separate ``estimate`` calls (each adding ``per_call_overhead``) is one-sided
    # conservative: the running total upper-bounds the single-string render, so the
    # kept set's actual render is provably <= the budget (+ the one always-kept row).
    token_cut = False
    if max_result_tokens is not None:
        est = estimator if estimator is not None else _DEFAULT_ESTIMATOR
        # Fixed rendering overhead. The header count is not known until packing
        # finishes; use the pre-pack candidate count (``len(rows)``) — an over-, never
        # under-, estimate of the final header width — and the truncated-(tokens)
        # variant, which is the longest header this path can emit.
        header_probe = f"[showing {len(rows)} of unknown rows, truncated (tokens)]"
        col_header = " | ".join(columns)
        separator = "-+-".join("-" * len(col) for col in columns)
        # NE-KG-MULTIKEY: the empty-member note is part of the FIXED frame the model
        # sees, so it is pre-charged like the rest of it. The charge is the WORST case
        # — every requested member empty — which is knowable here (the members are
        # caller args) and is an over-, never under-, estimate, keeping the packed
        # set's actual render provably <= the budget. Zero on every scalar call.
        if requested_members:
            window_probe = (
                f" in the caller-chosen window {window[0]}..{window[1]}"
                if window is not None
                else ""
            )
            members_probe = (
                f"\n[note: {len(requested_members)} of {len(requested_members)} requested "
                f"{template.list_arg} values returned no rows{window_probe}: "
                f"{', '.join(sorted(requested_members))} — a fact about this query's "
                "result only, not evidence that the data does not exist]"
            )
        else:
            members_probe = ""
        packed_rows: list[tuple[str, ...]] = []
        packed_citations: list[str] = []
        packed_receipts: list[VerifiedRowContributorReceipt] = []
        running = est.estimate("\n".join((header_probe, col_header, separator)))
        if members_probe:
            running += est.estimate(members_probe)
        for i, row in enumerate(rows):
            # Each row is rendered on its own line — charge the joining newline too.
            row_est = est.estimate("\n" + " | ".join(row))
            if i > 0 and running + row_est > max_result_tokens:
                break
            packed_rows.append(row)
            packed_citations.append(citations[i])
            if contributor_receipts:
                packed_receipts.append(contributor_receipts[i])
            running += row_est
        token_cut = len(packed_rows) < len(rows)
        rows = packed_rows
        citations = packed_citations
        contributor_receipts = packed_receipts

    if token_cut:
        truncated = True
        truncation_reason: Literal["rows", "tokens"] | None = "tokens"
    elif row_truncated:
        truncated = True
        truncation_reason = "rows"
    else:
        truncated = False
        truncation_reason = None

    # NE-KG-MULTIKEY empty-member resolution. A multi-key read collapses N single-key
    # reads into one, so it must still answer what N empty single-key reads answered:
    # WHICH requested members returned nothing. The claim is a set difference between
    # the requested members and the observed values of the template's declared
    # ``key_column`` — a GRAPH-JOINED column (proved triple-bound at load), never the
    # echoed argument — over the COMPLETE result set. Two fail-closed conditions:
    #
    # * TRUNCATION voids it. A member absent from a capped prefix may hold rows past
    #   the cap, so under either truncation reason the difference is not computed and
    #   ``empty_members_verified`` stays False (an honest "unknown", never a guess).
    # * The set difference NEVER fabricates a row. It reports absence, and the
    #   rectangular row payload is untouched — no synthetic member row is added.
    list_arg_name = template.list_arg if requested_members is not None else None
    empty_members: tuple[str, ...] = ()
    empty_members_verified = False
    empty_member_receipt: VerifiedEmptyResultReceipt | None = None
    if requested_members is not None and list_arg_name is not None and not truncated:
        key_column = template.args[list_arg_name].key_column
        assert key_column is not None  # narrowing: load validation proved this
        observed = {row[columns.index(key_column)] for row in rows}
        empty_members = tuple(sorted(set(requested_members) - observed))
        empty_members_verified = True
        if (
            empty_members
            and rows
            and template.answer.source_status == "source_bearing"
            and source_evidence_bound
        ):
            empty_member_receipt = await _mint_no_rows_receipt()

    # The provenance double-check runs over the PACKED citations only: every
    # citation the model can actually see is probe-verified against the C7-trimmed
    # graph set (C7 — a packed-away row's citation is not a model-visible claim).
    await check_citations_in_dataset(store, dataset, citations)

    # DECIDED (§4.1): an untruncated read is a complete enumeration, so its total is
    # an exact trusted count (== returned_rows). Under truncation the total is unknown
    # here (None) UNLESS the template opts into the trusted COUNT rewrite below —
    # never a fabricated number.
    total_rows = len(rows) if not truncated else None

    # NE-CTXB-7 total_rows COUNT rewrite (opt-in per template). A THIRD read that runs
    # ONLY under truncation: an untruncated enumeration already has the exact total, so
    # the probe never fires (no extra store touch). AS-OF-COUNT-TIME semantics: this is
    # a second, UNsnapshotted read over the SAME C7 dataset (never the full union), so
    # on a live store the rows and the total may reflect different snapshots — exact on
    # fixtures, and NO test/arc asserts rows-vs-total equality live. A dedicated read
    # timeout (distinct from Fuseki's write-contention retry; ≤2s p95 budget §5.3,
    # measured on the rig in NE-CTXB-8) bounds it: on timeout total_rows stays None (an
    # honest unknown, never fabricated — §0-law-5). A malformed result raises
    # VerifiedAnswerCountError; any OTHER store exception propagates (fail loud). This
    # opt-in is per-template ONLY — a deployment-setting override is deliberately
    # deferred to NE-CTXB-8 (keeps config.py/tools.py out of this diff).
    if truncated and template.answer.count_total_rows:
        count_query = render_count_query(template, typed_args)
        try:
            async with asyncio.timeout(count_timeout_s):
                count_rows = await store.sparql_scoped(count_query, dataset=dataset)
        except TimeoutError:
            total_rows = None
        else:
            total_rows = _parse_ne_total(count_rows)

    # Re-resolve after every primary/count/citation query and immediately before
    # publication. Mid-query release/head/ACL drift makes the whole answer Unknown.
    if contributor_receipts:
        assert source_authority is not None
        for receipt in contributor_receipts:
            for stamp in receipt.contributors:
                resolution = await source_authority.resolve(
                    stamp,
                    principal_id=principal.id,
                    access_context_hash=authority_access_context_hash,
                )
                if resolution.decision is not SourceAuthorityDecision.AUTHORIZED:
                    raise VerifiedAnswerScopeError("source_authority_unavailable")
    for publication_receipt in (empty_result_receipt, empty_member_receipt):
        if publication_receipt is None:
            continue
        assert source_authority is not None
        for stamp in publication_receipt.contributors:
            resolution = await source_authority.resolve(
                stamp,
                principal_id=principal.id,
                access_context_hash=authority_access_context_hash,
            )
            if resolution.decision is not SourceAuthorityDecision.AUTHORIZED:
                raise VerifiedAnswerScopeError("source_authority_unavailable")

    answer = VerifiedAnswer(
        template_id=template.template_id,
        template_version=template.version,
        args=json_args,
        columns=columns,
        rows=tuple(rows),
        citations=tuple(citations),
        contributor_receipts=tuple(contributor_receipts),
        empty_result_receipt=empty_result_receipt,
        list_arg=list_arg_name,
        empty_members=empty_members,
        empty_members_verified=empty_members_verified,
        empty_member_receipt=empty_member_receipt,
        window=window,
        truncated=truncated,
        row_limit=limit,
        returned_rows=len(rows),
        total_rows=total_rows,
        truncation_reason=truncation_reason,
        config_hash=config_hash,
        ontology_version=ontology_version_string(store.ontology_version()),
        executed_at=datetime.now(UTC),
    )
    if audit_sink is not None:
        await audit_sink.append(
            new_audit_event(
                action="knowledge.verified_read",
                resource="knowledge://verified",
                outcome=AuditOutcome.success,
                actor=principal.id,
                detail=(
                    f"rows={len(rows)} row_limit={limit} "
                    f"truncated={str(truncated).lower()} "
                    f"truncation_reason={truncation_reason or 'none'}"
                    + (
                        # NE-KG-MULTIKEY coverage accounting, COUNTS ONLY (C12): a
                        # multi-key read that matched fewer members than it asked for
                        # must be discoverable in the plaintext audit trail, not only
                        # inside the encrypted rendering. ``unknown`` is the honest
                        # truncated case — the set difference was never computed.
                        (
                            f" requested_members={len(requested_members)} "
                            + (
                                f"empty_members={len(empty_members)}"
                                if empty_members_verified
                                else "empty_members=unknown"
                            )
                        )
                        if requested_members is not None
                        else ""
                    )
                ),
                trace_id=trace_id,
            )
        )
    return answer


def render_answer_text(answer: VerifiedAnswer) -> str:
    """The COMPACT, deterministic model-facing rendering (§4.2, default-ON).

    An envelope HEADER line — ``[showing {returned_rows} of {total_rows or
    "unknown"} rows{, truncated (reason)}]`` — followed by the ``col | col``
    table. This is the ONLY thing the model sees per verified fetch: the
    duplicated citations array, the echoed args, and both hashes are gone from
    the model's context forever (they still ride the STRUCTURED artifact the
    ``GroundingRecorder`` captures + the HTTP wire view). Zero model tokens —
    a pure render of the frozen answer, stable for golden/regression tests.

    NOTE: the grounding corpus is NOT built from this string — it is built from
    the recorded structured answers (see ``ne.runtime.grounding``), so this
    compact rendering never weakens the grounding gate (§4.2, pinned by the
    corpus-byte-identical test).
    """
    total_text = str(answer.total_rows) if answer.total_rows is not None else "unknown"
    head = f"[showing {answer.returned_rows} of {total_text} rows"
    if answer.truncated and answer.truncation_reason is not None:
        head += f", truncated ({answer.truncation_reason})"
    head += "]"
    lines = [head, " | ".join(answer.columns)]
    lines.append("-+-".join("-" * len(col) for col in answer.columns))
    for row in answer.rows:
        lines.append(" | ".join(row))
    if answer.returned_rows == 0:
        lines.append(ZERO_ROWS_NOTE)
    members_note = empty_members_note(answer)
    if members_note is not None:
        lines.append(members_note)
    return "\n".join(lines)


def empty_members_note(answer: VerifiedAnswer) -> str | None:
    """The model-facing EMPTY-MEMBER line for a multi-key read, or ``None``.

    A multi-key read collapses N single-key reads into one call, and this line is what
    preserves the thing those N calls established that the row table alone cannot: that
    a specific requested key matched NOTHING. Without it the model would have to derive
    the absence by set difference — arithmetic it is not permitted to be trusted with,
    on exactly the question ("who has no loan?") the demo's ground truth turns on.

    ``None`` — nothing appended, rendering byte-identical to before NE-KG-MULTIKEY —
    for EVERY scalar call, for a truncated read (where the claim was never computed),
    and for a list read in which every member matched. That is the parity rule in one
    place: *no array in, nothing changes.*

    C12: the members ARE caller-supplied values, and they are named here. That is safe
    for exactly the reason ``list_max`` is restricted to enum- or pattern-bounded string
    args at load — a member is provably a short vocabulary token and cannot carry the
    prose payload an arg-echo/corpus-poisoning attack needs (design §2.7-F1). The line
    is also NOT a verified cell: it is envelope, never a row, so it does not enter the
    grounding corpus (which is built from ``columns``/``rows``), exactly like today's
    zero-row scoping note.
    """
    if not answer.empty_members_verified or not answer.empty_members:
        return None
    supplied = answer.args.get(answer.list_arg or "")
    total = len(supplied) if isinstance(supplied, list) else len(answer.empty_members)
    listed = ", ".join(answer.empty_members)
    # A template with a caller-chosen date window gets the window NAMED in the
    # note: "no rows for this key" was measured under those exact bounds, and the
    # bounds are the caller's, not the data's. Leaving them out is how "the
    # payment query for the relevant window returned no rows" gets read as "no
    # payment is due" when the window itself was the invented part. C12-safe:
    # both values passed ``date`` validation and cannot carry prose.
    scope = (
        f" in the caller-chosen window {answer.window[0]}..{answer.window[1]}"
        if answer.window is not None
        else ""
    )
    return (
        f"[note: {len(answer.empty_members)} of {total} requested {answer.list_arg} "
        f"values returned no rows{scope}: {listed} — a fact about this query's "
        "result only, not evidence that the data does not exist]"
    )


#: SPEC §12 items 3/4 (NE-TEMPLATE-ENUM): the model-facing scoping note appended to
#: every ZERO-row rendering. Zero rows means "this query matched nothing in the
#: caller-visible data" — it must never read as "the store holds no such data". The
#: note is EMITTED ON EVERY zero-row answer regardless of cause (no data-dependent
#: signal, so it leaks nothing — C7), and it claims only what was verified: the
#: query's own result. The bracketed ``[note: ...]`` shape is load-bearing:
#: ``ne.api.workflows.parse_verified_rows`` skips exactly this line.
ZERO_ROWS_NOTE = (
    "[note: this query returned no rows — a fact about this query's result only, "
    "not evidence that the data does not exist]"
)


def answers_equal(a: VerifiedAnswer, b: VerifiedAnswer) -> bool:
    """Replay diff: equal IFF every field EXCEPT ``executed_at`` matches (procedural id)."""
    return a.model_dump(exclude={"executed_at"}) == b.model_dump(exclude={"executed_at"})
