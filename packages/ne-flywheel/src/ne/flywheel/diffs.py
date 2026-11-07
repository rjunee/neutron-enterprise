# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic, injection-safe proposal -> reviewable-diff renderer (NE-066).

The propose-then-approve gate turns each typed
:class:`~ne.flywheel.proposals.StoredFixProposal` (from NE-065's four fix lanes)
into a concrete, human-reviewable ARTIFACT:

* **content / retrieval / schema** lanes -> a git-style unified diff + a PR-body
  markdown document a human reviews and applies through the normal git-PR flow.
* **policy** lane -> an OPA policy-diff whose added content is EXCLUSIVELY
  ``#``-prefixed comment lines (a review stub for the steward to translate into
  a real ``.rego`` change and deploy through the OPA-bundle flow).

Three renderer invariants, all structurally guaranteed:

* **Deterministic.** :func:`render_proposal_diff` is a PURE function of its
  inputs — no clock, no uuid, no randomness anywhere. The same proposal renders
  a byte-identical :class:`DiffArtifact` every time.
* **Artifact, not mutation.** This module has NO write capability by
  construction: it imports no SQLAlchemy, no HTTP/gateway client, and no
  ne-events / ne-audit writer (a structural test asserts this). It produces
  text; it can never commit, push, apply, or persist anything.
* **Injection-safe (C12).** Proposal-carried free text is UNTRUSTED and reaches
  the steward's decision artifact through EXACTLY ONE of three deterministic
  sanitizers — one uniform, auditable (greppable) boundary:

  * :func:`_safe` — for INLINE (single-line, mid-line) interpolation: bullet
    values, comma-joined refs, and every str metadata field. Drops
    non-printables, collapses all whitespace to single spaces (so line-start
    markdown is structurally impossible), and backslash-escapes the mid-line
    markdown/GitHub-significant characters.
  * :func:`_fenced` — for MULTI-LINE untrusted markdown (``summary``,
    golden-set ``notes``): a DYNAMIC fence longer than any backtick run inside,
    so a ``` ``` ``` embedded in the text can never break out.
  * :func:`_comment_lines` — for the rego artifact: every added line is a ``#``
    comment, so rego source smuggled into a summary can never escape.

  The PR title, the ``# {title}`` H1, and all filenames derive EXCLUSIVELY from
  TRUSTED fields (allowlisted lane + ``sha256(proposal_id)`` stem) — never from
  the summary. The diff is DATA rendered as a patch, never something evaluated.

Trust boundary: proposal-carried text (``summary``, golden-set ``notes``, refs,
metadata str fields) is UNTRUSTED and is ALWAYS routed through one of the three
sanitizers above before it reaches an artifact. In contrast a
:class:`ProposedFileChange` (the optional concrete before/after seam) carries
STEWARD-AUTHORED reviewed content — that text IS the artifact and is rendered
verbatim into the patch. ``ProposedFileChange.path`` is still validated against
path-traversal AND control/non-printable characters because it names where a
human would apply the change (and is interpolated into headings + diff headers).
"""

from __future__ import annotations

import difflib
import enum
import hashlib
import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, field_validator

from ne.flywheel.lanes import FixLane
from ne.flywheel.proposals import ProposalKind, StoredFixProposal

#: The four TRUSTED lane values. ``StoredFixProposal.lane`` is an unconstrained
#: ``str`` read back from the DB; validating it against this allowlist makes the
#: lane trusted for both the PR title/H1 AND the ``proposals/{lane}/{name}.md``
#: artifact path (closing the latent path-steering vector).
_KNOWN_LANES = frozenset(lane.value for lane in FixLane)

_MANUAL_APPLY_FOOTER = (
    "Tracked manual apply: a human reviews this artifact and applies it through "
    "the normal git-PR review (content/retrieval/schema) or OPA-bundle-deploy "
    "(policy) flow, then records the landed change with "
    "ProposalReviewService.record_apply (WHO/WHEN/applied_ref). The platform "
    "NEVER applies the change itself — auto-apply and auto-re-index are v1.1."
)


class DiffArtifactKind(enum.StrEnum):
    """The two reviewable-artifact flavors this renderer produces."""

    GIT_UNIFIED_DIFF = "git_unified_diff"
    OPA_POLICY_DIFF = "opa_policy_diff"


class SourceAuthorityUnavailableError(RuntimeError):
    """A source-bearing proposal may not be rendered before current authorization."""


class ProposedFileChange(BaseModel):
    """An optional, steward-authored concrete before/after file change.

    This is the TRUSTED seam: when a steward has authored the actual reviewed
    content, the renderer emits a real before/after unified diff of it (the text
    IS the artifact). ``path`` is still validated against path-traversal /
    absolute / backslash / NUL because it names where a human would apply the
    change.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    before_text: str = ""
    after_text: str

    @field_validator("path")
    @classmethod
    def _path_safe(cls, v: str) -> str:
        if not v:
            raise ValueError("path must be non-empty")
        if len(v) > 300:
            raise ValueError("path must be <= 300 chars")
        if v.startswith("/"):
            raise ValueError("path must be relative (must not start with '/')")
        if "\\" in v:
            raise ValueError("path must not contain a backslash")
        if "\x00" in v:
            raise ValueError("path must not contain a NUL byte")
        if any(not ch.isprintable() for ch in v):
            raise ValueError("path must not contain control or non-printable characters")
        for segment in v.split("/"):
            if not segment or segment in {".", ".."}:
                raise ValueError(
                    "path must not contain an empty, '.' or '..' segment (path-traversal guard)"
                )
        return v


class DiffArtifact(BaseModel):
    """A frozen, reviewable diff artifact rendered from one fix proposal."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    lane: str
    kind: DiffArtifactKind
    path: str
    title: str
    patch: str
    pr_body: str


def _artifact_name(proposal_id: str) -> str:
    """A deterministic, injection-safe filename stem: ``sha256(proposal_id)[:16]``.

    Derived ONLY from the proposal id — never from the (untrusted) summary — so
    a malicious summary can never steer the artifact path.
    """
    return hashlib.sha256(proposal_id.encode()).hexdigest()[:16]


def _comment_lines(text: str) -> list[str]:
    """Render ``text`` as ``#``-prefixed comment lines (rego-comment-safe).

    Normalizes CR away first, then prefixes every line with ``# `` so rego
    source smuggled into an untrusted summary can NEVER escape a comment.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return ["# " + line for line in normalized.splitlines()]


def _fence(text: str) -> str:
    """Return a backtick fence longer than any backtick run inside ``text``.

    ``max(3, longest_run + 1)`` backticks, so a ``` ``` ``` (or longer) embedded
    in untrusted text can never break out of the fenced block.
    """
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(3, longest + 1)


def _fenced(text: str, *, lang: str = "") -> str:
    """Wrap ``text`` in a dynamic fence (optionally language-tagged)."""
    fence = _fence(text)
    return f"{fence}{lang}\n{text}\n{fence}"


def _unified_new_file(after_text: str, path: str) -> str:
    return _unified_diff("", after_text, path)


def _unified_diff(before_text: str, after_text: str, path: str) -> str:
    """A git-style unified diff of ``before_text`` -> ``after_text`` at ``path``.

    A new file (empty ``before_text``) uses ``--- /dev/null``.
    """
    fromfile = "/dev/null" if before_text == "" else f"a/{path}"
    tofile = f"b/{path}"
    lines = difflib.unified_diff(
        before_text.splitlines(),
        after_text.splitlines(),
        fromfile=fromfile,
        tofile=tofile,
        lineterm="",
    )
    return "\n".join(lines) + "\n"


#: Markdown / GitHub-significant characters that are backslash-escaped inline so
#: they render literally: code spans / links / images / autolinks / raw HTML /
#: emphasis / tables / strikethrough / HTML entities, plus GitHub ``#123``
#: issue-closing refs and ``@`` mentions. ``-`` ``.`` ``,`` ``:`` ``+`` are
#: DELIBERATELY not here — they are only line-start-significant (already
#: neutralized by the whitespace collapse), so benign refs like ``cite-1`` and
#: ``q-shared`` render byte-identical and existing golden tests pass unchanged.
_INLINE_ESCAPE = frozenset("\\`*_{}[]()<>#!|~@&")


def _safe(text: str) -> str:
    r"""Sanitize UNTRUSTED proposal-carried text for INLINE interpolation ONLY.

    This is the single inline sanitizer boundary. Use it for any untrusted
    proposal-sourced ``str`` that is spliced into a single-line, mid-line
    position (a bullet value, a comma-joined ref, a metadata field). Multi-line
    untrusted text goes through :func:`_fenced` (markdown) or
    :func:`_comment_lines` (rego) instead — never this.

    Three deterministic passes:

    1. Drop non-printable, non-whitespace characters (NUL / ESC / ANSI / DEL).
    2. Collapse every whitespace run (newlines / CR / tabs / U+2028 …) to a
       single space and strip — so line-start markdown (headings, fences, list
       items, blockquotes) is structurally impossible.
    3. Backslash-escape the mid-line-significant characters in
       :data:`_INLINE_ESCAPE` — neutralizing code spans, links / images /
       autolinks / raw HTML, emphasis, tables, strikethrough, HTML entities, and
       GitHub ``#123`` issue-closing refs + ``@`` mentions.
    """
    stripped = "".join(ch for ch in text if ch.isprintable() or ch.isspace())
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    return "".join(f"\\{ch}" if ch in _INLINE_ESCAPE else ch for ch in collapsed)


def _is_golden(proposal: StoredFixProposal) -> bool:
    return proposal.kind == ProposalKind.GOLDEN_SET_CANDIDATE.value


def _is_entity_merge(proposal: StoredFixProposal) -> bool:
    return proposal.kind == ProposalKind.ENTITY_MERGE_CANDIDATE.value


def _is_verified_answer(proposal: StoredFixProposal) -> bool:
    return proposal.kind == ProposalKind.VERIFIED_ANSWER_CANDIDATE.value


def _is_graph_extraction(proposal: StoredFixProposal) -> bool:
    return proposal.kind == ProposalKind.GRAPH_EXTRACTION_CANDIDATE.value


def _verified_lines(proposal: StoredFixProposal) -> list[str]:
    """Render the verified-answer-template payload (NE-ONTOLOGY-FLYWHEEL).

    ``template_id``/``version``/``toml_sha256`` are inline metadata through
    :func:`_safe`; the steward-authored ``template_toml`` is UNTRUSTED multi-line
    text rendered through :func:`_fenced` (a dynamic backtick fence, ``lang=toml``)
    so a hostile TOML body (backtick runs, markdown headings) can never break out
    of the fenced block (C12, injection-safe).
    """
    payload = proposal.payload
    template_id = _safe(str(payload.get("template_id", "")))
    version = _safe(str(payload.get("version", "")))
    toml_sha256 = _safe(str(payload.get("toml_sha256", "")))
    template_toml = str(payload.get("template_toml", ""))
    return [
        "## Verified-answer template (steward-authored)",
        "",
        f"- template_id: {template_id}",
        f"- version: {version}",
        f"- toml_sha256: {toml_sha256}",
        "",
        "### Template TOML (proposal-carried, untrusted)",
        "",
        _fenced(template_toml, lang="toml"),
    ]


def _entity_side_lines(label: str, side: object) -> list[str]:
    """Render one entity side of a merge-candidate as escaped inline bullets."""
    fields = side if isinstance(side, Mapping) else {}
    return [
        f"- {label} entity_class: {_safe(str(fields.get('entity_class', '')))}",
        f"- {label} entity_key: {_safe(str(fields.get('entity_key', '')))}",
        f"- {label} display: {_safe(str(fields.get('display', '')))}",
    ]


def _entity_merge_lines(proposal: StoredFixProposal) -> list[str]:
    """Render the entity-merge-candidate payload (the ``_golden_lines`` precedent).

    Every proposal-carried ``str`` (entity_class/entity_key/display, blocking_rule)
    goes through :func:`_safe` (single-line, markdown-escaped) so a hostile display
    name cannot fabricate a heading, fence, or link in the steward card. The
    typed numbers (match_probability / match_weight / threshold) the renderer
    formats itself.
    """
    payload = proposal.payload
    left = payload.get("left")
    right = payload.get("right")
    return [
        "## Entity-merge candidate",
        "",
        *_entity_side_lines("left", left),
        *_entity_side_lines("right", right),
        f"- match_probability: {_number(payload.get('match_probability'))}",
        f"- match_weight: {_number(payload.get('match_weight'))}",
        f"- threshold: {_number(payload.get('threshold'))}",
        f"- blocking_rule: {_safe(str(payload.get('blocking_rule', '')))}",
        f"- model config_hash: {_safe(proposal.config_hash)}",
    ]


#: The per-row review legend (renderer-authored, trusted) shown once per batch.
_GRAPH_EXTRACTION_LEGEND = (
    "Per-row review: mark each row approve / edit (note the correction) / "
    "decline. The NE-066 proposal-level approve/reject then applies the marked "
    "set through the extraction apply seam (NE-T2X-2). This artifact RECORDS "
    "the decision; it never applies it."
)

#: THREE separate GFM task-list lines, not one -- a single line
#: "- [ ] approve  - [ ] edit  - [ ] decline" renders as ONE tickable checkbox
#: (only the leading "[ ]" is task-list syntax; the rest is inert text after
#: it), defeating the whole point of per-row approve/edit/decline. Three
#: distinct list items each become a real, independently tickable checkbox.
_GRAPH_EXTRACTION_DECISION_LINES = [
    "- [ ] approve",
    "- [ ] edit (note the correction)",
    "- [ ] decline",
]


def _graph_extraction_assertion_lines(index: int, row: object) -> list[str]:
    """Render one assertion row as a per-row block.

    Defensive over the raw payload dict (the ``_entity_side_lines`` precedent):
    ``.get`` with defaults, ``isinstance`` checks, never a pydantic re-parse —
    this stays a pure function that never raises on a malformed/hostile stored
    payload. ``### Row {i}`` is a TRUSTED heading (the index is
    renderer-controlled, never untrusted text). kind/row_id/confidence/
    evidence_ref render ALWAYS; the class/key/display triple and the edge
    triple render only when non-empty (compact link rows); properties render
    one bullet per key, sorted for determinism.
    """
    fields = row if isinstance(row, Mapping) else {}
    row_id = _safe(str(fields.get("row_id", "")))
    kind = _safe(str(fields.get("kind", "")))
    entity_class = str(fields.get("entity_class", ""))
    entity_key = str(fields.get("entity_key", ""))
    display = str(fields.get("display", ""))
    subject_key = str(fields.get("subject_key", ""))
    edge_kind = str(fields.get("edge_kind", ""))
    object_key = str(fields.get("object_key", ""))
    evidence_ref = _safe(str(fields.get("evidence_ref", "")))
    lines = [
        f"### Row {index}",
        "",
        *_GRAPH_EXTRACTION_DECISION_LINES,
        "",
        f"- row_id: {row_id}",
        f"- kind: {kind}",
    ]
    if entity_class:
        lines.append(f"- entity_class: {_safe(entity_class)}")
    if entity_key:
        lines.append(f"- entity_key: {_safe(entity_key)}")
    if display:
        lines.append(f"- display: {_safe(display)}")
    if subject_key:
        lines.append(f"- subject_key: {_safe(subject_key)}")
    if edge_kind:
        lines.append(f"- edge_kind: {_safe(edge_kind)}")
    if object_key:
        lines.append(f"- object_key: {_safe(object_key)}")
    lines.append(f"- evidence_ref: {evidence_ref}")
    lines.append(f"- confidence: {_number(fields.get('confidence'))}")
    properties = fields.get("properties")
    if isinstance(properties, Mapping):
        for k in sorted(properties, key=str):
            lines.append(f"- prop {_safe(str(k))}: {_safe(str(properties[k]))}")
    return lines


def _graph_extraction_lines(proposal: StoredFixProposal) -> list[str]:
    """Render the graph-extraction-candidate batch payload (NE-T2X design §2.3/§2.4).

    DEFENSIVE over the raw payload dict — the ``_entity_side_lines`` precedent:
    ``.get`` with defaults + ``isinstance`` Mapping/list checks, no pydantic
    re-parse. A malformed/hostile stored payload (missing ``assertions``, a
    non-list value, non-mapping rows) never raises here: non-Mapping rows are
    skipped from the rendered ``### Row`` blocks.

    This renderer is the STEWARD'S ONLY VIEW of the batch (the raw payload is
    deliberately kept off the API wire) — so what a steward sees must never
    UNDERSTATE what is actually stored. ``- assertions: N`` alone (N = the
    Mapping-shaped row count) would silently hide a dropped row if the stored
    ``assertions`` list contains anything non-Mapping-shaped, or if
    ``assertions`` itself is not a list at all. ``- unrenderable rows: N``
    makes that drop explicit and always sums with ``assertions`` to what was
    actually present in the stored payload (Argus round-2 P2).

    Rendered ``### Row N`` headings PRESERVE the STORED index (Argus round-3
    P3): heading ``N`` always corresponds to ``assertions[N-1]`` in the raw
    payload, even when a malformed row is skipped — a skipped row leaves a
    visible GAP in the numbering rather than shifting every subsequent row
    down. The prior renderer enumerated only the renderable (Mapping-shaped)
    rows starting at 1, so dropping row 2 of 4 made stored row 4 render as
    ``### Row 3`` — a steward cross-referencing the raw stored payload by
    heading number would land on the WRONG assertion
    (``test_graph_extraction_row_numbering_preserves_stored_indices``).

    When the ``assertions`` field itself is malformed (present, but neither a
    list nor absent), the whole value is treated as ONE unrenderable item
    regardless of ITS OWN shape — even if that malformed value happens to be
    a ``Mapping`` (e.g. payload ``{"assertions": {"row_id": ...}}``), it is
    never rendered as a ``### Row`` block: a malformed CONTAINER field is not
    a row within a correctly-shaped list, and presenting it as one would
    silently hand the steward a fabricated-looking single assertion that was
    never actually a member of a batch
    (``test_graph_extraction_malformed_payload_renders_without_error``'s
    ``"mapping"`` case, Argus round-3 P3 second finding).
    """
    payload = proposal.payload
    document_ref = _safe(str(payload.get("document_ref", "")))
    run_id = _safe(str(payload.get("run_id", "")))
    source_system = _safe(str(payload.get("source_system", "")))
    raw_assertions = payload.get("assertions")
    assertions_field_malformed = False
    if isinstance(raw_assertions, list):
        rows: list[object] = raw_assertions
    elif raw_assertions is None:
        rows = []
    else:
        # The 'assertions' field itself is malformed (not a list, not
        # absent) -- treat the WHOLE value as one unrenderable item rather
        # than silently discarding it with zero trace. This must NOT fall
        # through to the Mapping-shaped-row path below: if the malformed
        # value itself happens to be a Mapping (e.g. payload
        # {"assertions": {"row_id": ...}}), it is a malformed CONTAINER
        # field, not a row within a correctly-shaped list, and must never be
        # presented to the steward as a legitimate single assertion (Argus
        # round-3 P3, second finding --
        # test_graph_extraction_malformed_payload_renders_without_error's
        # "mapping" case).
        rows = [raw_assertions]
        assertions_field_malformed = True
    mapping_rows = [] if assertions_field_malformed else [r for r in rows if isinstance(r, Mapping)]
    unrenderable = len(rows) - len(mapping_rows)
    lines = [
        "## Graph-extraction batch (per-document)",
        "",
        f"- document_ref: {document_ref}",
        f"- run_id: {run_id}",
        f"- source_system: {source_system}",
        f"- assertions: {len(mapping_rows)}",
        f"- unrenderable rows: {unrenderable}",
        "",
        _GRAPH_EXTRACTION_LEGEND,
    ]
    for i, row in enumerate(rows, start=1):
        if assertions_field_malformed or not isinstance(row, Mapping):
            continue
        lines += ["", *_graph_extraction_assertion_lines(i, row)]
    return lines


def _number(value: object) -> str:
    """Format a numeric payload value the renderer controls (never untrusted str)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return repr(value)


def _metadata_lines(proposal: StoredFixProposal, name: str, kind: DiffArtifactKind) -> list[str]:
    """The deterministic, structured metadata bullet list.

    Every value here is EITHER a typed non-str the renderer formats itself
    (``name`` / ``kind.value`` hashes + enum, ``confidence`` float,
    ``created_at.isoformat()``, int signal ids — trusted) OR a proposal-carried
    ``str`` routed through :func:`_safe` (single-line, markdown-escaped) — so no
    id-carried newline can fabricate a heading or fence in the artifact.
    """
    return [
        f"- artifact: {name}",
        f"- proposal_id: {_safe(proposal.proposal_id)}",
        f"- kind: {kind.value}",
        f"- proposal_kind: {_safe(proposal.kind)}",
        f"- lane: {_safe(proposal.lane)}",
        f"- cluster_id: {_safe(proposal.cluster_id)}",
        f"- priority: {_safe(proposal.priority)}",
        f"- confidence: {proposal.confidence}",
        f"- config_hash: {_safe(proposal.config_hash)}",
        f"- created_at: {proposal.created_at.isoformat()}",
        f"- evidence_event_ids: {', '.join(_safe(e) for e in proposal.evidence_event_ids)}",
        f"- evidence_signal_ids: {', '.join(str(s) for s in proposal.evidence_signal_ids)}",
    ]


def _golden_lines(proposal: StoredFixProposal) -> list[str]:
    """Render the golden-set-candidate payload (refs + fenced steward note)."""
    payload = proposal.payload
    question_ref = _safe(str(payload.get("question_ref", "")))
    wrong = payload.get("wrong_source_refs") or []
    expected = payload.get("expected_source_refs") or []
    notes = str(payload.get("notes", ""))
    wrong_csv = ", ".join(_safe(str(r)) for r in wrong)
    expected_csv = ", ".join(_safe(str(r)) for r in expected)
    return [
        "## Golden-set candidate",
        "",
        f"- question_ref: {question_ref}",
        f"- wrong_source_refs: {wrong_csv}",
        f"- expected_source_refs: {expected_csv}",
        "",
        "### Steward resolution note",
        "",
        _fenced(notes),
    ]


def _review_document(proposal: StoredFixProposal, name: str, kind: DiffArtifactKind) -> str:
    """The proposal-review markdown document (the .md file content for git lanes)."""
    lines = [
        f"# Proposal review {name}",
        "",
        *_metadata_lines(proposal, name, kind),
        "",
        "## Summary (proposal-carried, untrusted)",
        "",
        _fenced(proposal.summary),
    ]
    if _is_golden(proposal):
        lines += ["", *_golden_lines(proposal)]
    if _is_entity_merge(proposal):
        lines += ["", *_entity_merge_lines(proposal)]
    if _is_verified_answer(proposal):
        lines += ["", *_verified_lines(proposal)]
    if _is_graph_extraction(proposal):
        lines += ["", *_graph_extraction_lines(proposal)]
    return "\n".join(lines) + "\n"


def _policy_review_text(proposal: StoredFixProposal, name: str) -> str:
    """The plain-text policy-review body that becomes rego COMMENT lines.

    Two-boundary rule. The WHOLE body is emitted through :func:`_comment_lines`,
    so every line of the added rego file is a ``#`` comment — a malicious summary
    containing rego source can never become executable policy; ``proposal.summary``
    therefore stays RAW (multi-line by design, ``#``-prefixed per line). The
    inline str metadata fields are ADDITIONALLY routed through :func:`_safe` so an
    id-carried newline cannot fabricate a fake ``Summary:`` section inside the
    comment stub.
    """
    lines = [
        f"OPA policy-diff proposal {name}",
        "",
        f"proposal_id: {_safe(proposal.proposal_id)}",
        f"lane: {_safe(proposal.lane)}",
        f"cluster_id: {_safe(proposal.cluster_id)}",
        f"priority: {_safe(proposal.priority)}",
        f"confidence: {proposal.confidence}",
        f"config_hash: {_safe(proposal.config_hash)}",
        f"created_at: {proposal.created_at.isoformat()}",
        f"evidence_event_ids: {', '.join(_safe(e) for e in proposal.evidence_event_ids)}",
        f"evidence_signal_ids: {', '.join(str(s) for s in proposal.evidence_signal_ids)}",
        "",
        "Summary (proposal-carried, untrusted):",
        proposal.summary,
        "",
        "This is a REVIEW STUB, not a policy. A steward translates the above into "
        "a concrete .rego change and deploys it through the OPA-bundle flow.",
    ]
    return "\n".join(lines)


def _pr_body(
    proposal: StoredFixProposal,
    *,
    name: str,
    kind: DiffArtifactKind,
    title: str,
    path: str,
    patch: str,
) -> str:
    """The deterministic PR-body markdown wrapping the metadata + summary + patch."""
    lines = [
        f"# {title}",
        "",
        *_metadata_lines(proposal, name, kind),
        "",
        "## Summary (proposal-carried, untrusted)",
        "",
        _fenced(proposal.summary),
    ]
    if _is_golden(proposal):
        lines += ["", *_golden_lines(proposal)]
    if _is_entity_merge(proposal):
        lines += ["", *_entity_merge_lines(proposal)]
    if _is_verified_answer(proposal):
        lines += ["", *_verified_lines(proposal)]
    if _is_graph_extraction(proposal):
        lines += ["", *_graph_extraction_lines(proposal)]
    lines += [
        "",
        f"## Proposed change ({path})",
        "",
        _fenced(patch, lang="diff"),
        "",
        "## Tracked manual apply",
        "",
        _MANUAL_APPLY_FOOTER,
    ]
    return "\n".join(lines) + "\n"


def render_proposal_diff(
    proposal: StoredFixProposal,
    *,
    change: ProposedFileChange | None = None,
    source_authority_authorized: bool = False,
) -> DiffArtifact:
    """Render ``proposal`` into a deterministic, injection-safe reviewable artifact.

    ``kind`` is :attr:`DiffArtifactKind.OPA_POLICY_DIFF` for the policy lane and
    :attr:`DiffArtifactKind.GIT_UNIFIED_DIFF` for every other lane. The filename
    stem is ALWAYS ``sha256(proposal_id)[:16]`` — never derived from the summary.

    With ``change=None`` (the NE-065 v1 reality: proposals carry no concrete file
    content) the renderer emits a NEW-FILE review stub: comment-only rego for the
    policy lane, a markdown review document for the git lanes. With ``change``
    provided (the steward-authored trusted seam) it emits a real before/after
    diff of ``change`` at ``change.path``.
    """
    if proposal.lane not in _KNOWN_LANES:
        raise ValueError(f"unknown proposal lane: {proposal.lane!r}")
    if (
        _is_graph_extraction(proposal)
        and bool(proposal.payload.get("source_authority_stamps"))
        and not source_authority_authorized
    ):
        raise SourceAuthorityUnavailableError(
            "source-bearing proposal is unavailable until every contributor is current"
        )

    name = _artifact_name(proposal.proposal_id)
    is_policy = proposal.lane == "policy"
    kind = DiffArtifactKind.OPA_POLICY_DIFF if is_policy else DiffArtifactKind.GIT_UNIFIED_DIFF

    if change is not None:
        path = change.path
        patch = _unified_diff(change.before_text, change.after_text, path)
    elif is_policy:
        path = f"policies/proposals/{name}_review.rego"
        after_text = "\n".join(_comment_lines(_policy_review_text(proposal, name))) + "\n"
        patch = _unified_new_file(after_text, path)
    else:
        path = f"proposals/{proposal.lane}/{name}.md"
        after_text = _review_document(proposal, name, kind)
        patch = _unified_new_file(after_text, path)

    # TRUSTED-ONLY title: allowlisted lane + sha256(proposal_id) stem, never the
    # untrusted summary. The summary appears ONLY inside its dynamic-fenced
    # '## Summary (proposal-carried, untrusted)' section. _pr_body's `# {title}`
    # H1 is thereby trusted with no further change.
    title = f"[flywheel:{proposal.lane}] proposal {name}"

    pr_body = _pr_body(
        proposal,
        name=name,
        kind=kind,
        title=title,
        path=path,
        patch=patch,
    )

    return DiffArtifact(
        proposal_id=proposal.proposal_id,
        lane=proposal.lane,
        kind=kind,
        path=path,
        title=title,
        patch=patch,
        pr_body=pr_body,
    )


__all__ = [
    "DiffArtifact",
    "DiffArtifactKind",
    "ProposedFileChange",
    "SourceAuthorityUnavailableError",
    "render_proposal_diff",
]
