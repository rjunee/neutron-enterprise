# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The total-traceability inference report renderer (design §5.1).

``render_inference_report`` renders the deterministic candidate model plus
its emitted schema into a reviewable markdown document — every untrusted,
profile-derived string reaches the rendered text through EXACTLY ONE of
:mod:`ne.schema_bootstrap.sanitize`'s two boundaries (see that module's
docstring for the full untrusted/trusted inventory).

**§5.1's self-check, made non-vacuous.** ``expected_anchors`` independently
re-derives, FROM the candidate model and the emitted schema (never from the
rendered report itself), the exact set of anchor tokens every emitted
class/slot/edge/enum must produce. ``verify_mention_completeness`` counts
each anchor's occurrences in the SERIALIZED report text and raises
:class:`ne.schema_bootstrap.errors.MentionCompletenessError` naming every
element that is missing (mentioned zero times) or duplicated (mentioned more
than once) — including the degenerate empty-report-vs-non-empty-model case,
where every anchor is reported missing rather than the check vacuously
passing on "nothing expected, nothing found". ``render_inference_report``
calls this check on its OWN output before returning, so a report that fails
its own traceability guarantee can never leave this module.

**Soundness, not just completeness (2026-07-28 Argus P2).** Completeness
alone — every EXPECTED anchor present exactly once — says nothing about
whether the report advertises an element that was never actually emitted.
An inferred enum whose sole column is provenance-mapped (e.g. a
low-cardinality ``source_system`` column) is neither in
``emitted.emitted_enums`` nor ``emitted.declined_enums`` — ``_emit_class``
skips provenance slots before ``_slot_attribute`` ever records either
outcome — so it must never get a full ``### Enum`` section; rendering one
anyway would tell a reviewer the draft schema carries an enum it does not.
``verify_mention_completeness`` therefore also scans the SERIALIZED report
for every anchor-SHAPED heading line (``### Class `...``` / ``### Enum
`...``` ``) and raises when one exists that ``expected_anchors`` did not
predict — the ``unexpected`` direction on
:class:`ne.schema_bootstrap.errors.MentionCompletenessError`. See
``tests/test_report.py::test_provenance_mapped_enum_is_not_rendered_as_an_enum_section``
and ``::test_mention_check_goes_red_on_an_unexpected_heading``.

Anchor formats (exact — every one is built from a MECHANICAL name, never
untrusted text):

* class  -> heading line ``### Class `{name}` `` (:data:`re.MULTILINE`,
  count must be exactly 1)
* slot   -> backticked token `` `{ClassName}.{mechanical_name}` `` for every
  NON-provenance-mapped slot (delimiters make ``Foo.bar`` vs ``Foo.barx``
  collisions impossible)
* edge   -> backticked token `` `{source_class}.{name} -> {target_class}` ``
* enum   -> heading line ``### Enum `{name}` `` — only for
  ``emitted.emitted_enums``; declined enums get a plain bullet instead and
  are NOT anchors (there is nothing traceable to a schema element that was
  never emitted).

**Anchor-namespace disjointness (the anchors are NOT self-sanitizing).**
Every anchor is a backtick-DELIMITED token, and backtick is one of
:data:`ne.schema_bootstrap.sanitize._INLINE_ESCAPE`'s characters — so
``safe_inline`` always strips literal backticks OUT of untrusted text. That
is necessary but not sufficient: an anchor's backtick DELIMITERS come from
this module's own render templates, not from the sanitizer, so a raw column
header or table-name stem whose *surviving* (post-``safe_inline``)
characters happen to spell ``ClassName.mechanical_name`` verbatim could
still forge an anchor token IF this module ever wrapped that untrusted value
in literal backticks the same way an anchor is wrapped. The fix applied
here: **no untrusted, profile-derived value is ever backtick-wrapped in the
rendered report** — every backtick-delimited substring in the whole document
is therefore, by construction, one of this module's own anchors. Untrusted
values that need a delimiter (the raw column header, the source-table stem)
are wrapped in double quotes instead — a different, disjoint delimiter — so
no untrusted input, however crafted, can ever produce a substring identical
to a real anchor. See
``tests/test_report.py::test_hostile_header_cannot_forge_an_anchor_token``.

**Refine landing view (2026-07-29 round 4, Argus P2).** This report is the
*traceability* artifact — its whole job is letting a reviewer trace what was
inferred to what shipped. On ``--refine`` the ``## Classes``/``## Enums``
sections below can only ever truthfully describe THIS RUN'S fresh machine
profile/model (that is genuinely all the evidence they have); the draft that
actually lands as ``schema-draft.yaml`` is the separate three-way merge
computed in the same call (:mod:`ne.schema_bootstrap.refine`). Before this
fix, ``render_inference_report`` was called with only the fresh evidence, so
the report silently described a draft that had not landed — a human-deleted
class stayed fully documented, with nothing telling the reader the deletion
had taken. The fix is an explicit split, never an inference left to the
reader: an optional :class:`RefineLanding` view drives a leading
``## What landed (--refine)`` section plus a per-class/per-enum "landing
status" line in every ``### Class``/``### Enum`` section, so the report
states outright which draft each part of it is evidence for. Landing names
may be HUMAN-TYPED (e.g. a hand-added class key) and are therefore untrusted
for delimiter purposes exactly like a raw column header — rendered through
:func:`sanitize.safe_inline` inside DOUBLE QUOTES, never backticks, so a
hostile human-typed name can never forge a second occurrence of a real
backtick-delimited anchor token (see the anchor-namespace-disjointness
section above). ``landing=None`` (the fresh-run default) renders
byte-identically to before this fix. See
``tests/test_report.py::test_landing_view_marks_statuses_and_survives_mention_check``,
``tests/test_report.py::test_hostile_landing_name_cannot_forge_an_anchor_token``,
and
``tests/test_workspace.py::test_refine_report_reflects_landing_draft_not_fresh_machine_draft``.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from . import sanitize
from .config import DEFAULT_CONFIG, BootstrapConfig
from .emit import EmittedSchema
from .errors import MentionCompletenessError
from .model import CandidateClass, CandidateModel, CandidateSlot
from .profiles import PATTERN_SOURCES, ColumnProfile, SampleProfile, TableProfile

_PATTERN_LABELS: dict[str, str] = {source: name for name, source in PATTERN_SOURCES.items()}

_MODEL_ROUTING_LINE = "- model routing: none (deterministic floor; LLM labeling is U5, opt-in)"


class RefineLanding(BaseModel):
    """The three-way-merge landing view a ``--refine`` report is rendered
    against, so the report can state explicitly which draft each of its
    sections describes (see the module docstring's "Refine landing view"
    section).

    **Field provenance.** ``class_names``/``enum_names`` are the LANDING
    (merged) draft's own class/enum keys —
    :func:`ne.schema_bootstrap.refine.merged_expected_names`'s output.
    ``tombstoned_classes``/``tombstoned_enums``, ``human_kept``,
    ``machine_updated``, ``blocked_resurrections``, ``human_readded`` come
    straight from the same call's :class:`ne.schema_bootstrap.refine.MergeResult`
    — the last four still carry that type's ``"classes."``/``"enums."``
    prefixes unchanged. ``orphaned_classes`` is
    :func:`ne.schema_bootstrap.refine.orphaned_classes` run against the
    merged draft. Every OTHER field here holds plain (unprefixed) names.

    **Names here may be HUMAN-TYPED.** A class/enum key can be anything a
    human typed by hand into ``schema-draft.yaml`` (an added class, a
    rename) — for delimiter purposes that makes every name in this model
    untrusted text, exactly like a raw column header or table-name stem
    elsewhere in this module. Every name is therefore rendered through
    :func:`sanitize.safe_inline` inside DOUBLE QUOTES, never backticks: this
    module's anchors are backtick-delimited (see the module docstring's
    anchor-namespace-disjointness section), so a human-typed name that
    happens to spell a real anchor token could forge one if it were ever
    wrapped in backticks the same way a real anchor is. See
    ``tests/test_report.py::test_hostile_landing_name_cannot_forge_an_anchor_token``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_names: list[str]
    enum_names: list[str]
    tombstoned_classes: list[str]
    tombstoned_enums: list[str]
    human_kept: list[str]
    machine_updated: list[str]
    blocked_resurrections: list[str]
    human_readded: list[str]
    orphaned_classes: list[str]


def _class_anchor(name: str) -> str:
    return f"### Class `{name}`"


def _enum_anchor(name: str) -> str:
    return f"### Enum `{name}`"


def _slot_anchor(class_name: str, mechanical_name: str) -> str:
    return f"`{class_name}.{mechanical_name}`"


def _edge_anchor(source_class: str, name: str, target_class: str) -> str:
    return f"`{source_class}.{name} -> {target_class}`"


def expected_anchors(model: CandidateModel, emitted: EmittedSchema) -> dict[str, str]:
    """Independently re-derive the exact anchor-set the report must mention exactly once.

    Never reads the rendered report — every anchor here is computed straight
    from ``model``/``emitted``, which is what makes
    :func:`verify_mention_completeness` a genuine self-check rather than a
    tautology.
    """
    anchors: dict[str, str] = {}
    for cls in model.classes:
        anchors[_class_anchor(cls.name)] = f"class {cls.name}"
        provenance = set(emitted.provenance_mapped.get(cls.name, []))
        for slot in cls.slots:
            if slot.mechanical_name in provenance:
                continue
            anchors[_slot_anchor(cls.name, slot.mechanical_name)] = (
                f"slot {cls.name}.{slot.mechanical_name}"
            )
        for edge in cls.edges:
            anchors[_edge_anchor(edge.source_class, edge.name, edge.target_class)] = (
                f"edge {edge.source_class}.{edge.name}->{edge.target_class}"
            )
    for enum_name in emitted.emitted_enums:
        anchors[_enum_anchor(enum_name)] = f"enum {enum_name}"
    return anchors


def _count_occurrences(report: str, anchor: str) -> int:
    if anchor.startswith("### "):
        pattern = re.compile(r"(?m)^" + re.escape(anchor) + r"$")
        return len(pattern.findall(report))
    return report.count(anchor)


#: Heading-shaped anchor lines the SOUNDNESS scan looks for — the same two
#: heading forms `_class_anchor`/`_enum_anchor` produce. Captures the
#: bracketed name so an unexpected heading's identifier is reportable.
_CLASS_HEADING_RE = re.compile(r"(?m)^### Class `([^`]*)`$")
_ENUM_HEADING_RE = re.compile(r"(?m)^### Enum `([^`]*)`$")


def _unexpected_headings(report: str, anchors: dict[str, str]) -> list[str]:
    """Anchor-SHAPED heading lines in ``report`` that ``anchors`` never predicted.

    The completeness loop in :func:`verify_mention_completeness` can only
    ever say "every EXPECTED anchor is present" — it never looks at what
    else is IN the report. A heading line that mimics the anchor format
    (``### Class `X` `` / ``### Enum `X` ``) but names an element outside the
    independently-derived expected set is exactly the shape of the
    provenance-mapped-enum bug this check exists to catch (see the module
    docstring's "Soundness" section).
    """
    unexpected: list[str] = []
    for prefix, pattern in (("### Class `", _CLASS_HEADING_RE), ("### Enum `", _ENUM_HEADING_RE)):
        for name in sorted(set(pattern.findall(report))):
            heading = f"{prefix}{name}`"
            if heading not in anchors:
                unexpected.append(heading)
    return sorted(unexpected)


def verify_mention_completeness(report: str, model: CandidateModel, emitted: EmittedSchema) -> None:
    """Raise :class:`MentionCompletenessError` unless every anchor appears exactly once.

    Also enforces the SOUNDNESS direction: no anchor-shaped heading may
    appear in ``report`` beyond the independently-derived expected set (see
    the module docstring).
    """
    anchors = expected_anchors(model, emitted)
    missing: list[str] = []
    duplicated: list[str] = []
    for anchor, label in anchors.items():
        count = _count_occurrences(report, anchor)
        if count == 0:
            missing.append(label)
        elif count > 1:
            duplicated.append(label)
    unexpected = _unexpected_headings(report, anchors)
    if missing or duplicated or unexpected:
        raise MentionCompletenessError(sorted(missing), sorted(duplicated), unexpected)


def _metadata_lines(
    profile: SampleProfile, model: CandidateModel, config: BootstrapConfig
) -> list[str]:
    rows_sampled = sum(t.row_count_sampled for t in profile.tables)
    rows_seen = sum(t.row_count_seen for t in profile.tables)
    lines = [
        f"- tables sampled: {len(profile.tables)}",
        f"- rows sampled (total): {rows_sampled}",
        f"- rows seen (total): {rows_seen}",
        f"- classes inferred: {len(model.classes)}",
        f"- enums inferred: {len(model.enums)}",
    ]
    for key, value in config.model_dump().items():
        lines.append(f"- threshold {key}: {value}")
    lines.append(_MODEL_ROUTING_LINE)
    lines.append(
        "- note: requiredness is SAMPLE-DERIVED — a bounded sample proves nothing "
        "about the full population; treat `required: true` as a reviewer decision, "
        "not a guarantee."
    )
    return lines


def _pattern_label(pattern: str) -> str:
    return _PATTERN_LABELS.get(pattern, pattern)


def _render_slot_lines(class_name: str, slot: CandidateSlot, col: ColumnProfile) -> list[str]:
    anchor = _slot_anchor(class_name, slot.mechanical_name)
    # Double-quoted, NOT backtick-wrapped: untrusted text must never appear
    # inside backticks, the anchor-token delimiter (see module docstring).
    lines = [f'- {anchor} — header: "{sanitize.safe_inline(col.name)}"']
    if col.pii_flag:
        lines.append(
            f"  - PII ({sanitize.safe_inline(col.pii_reason or '')}): "
            "exemplars suppressed — confirm with data owner"
        )
        return lines

    lines.append(f"  - type/range: `{slot.range}`")
    required_note = " (sample-derived)" if slot.required and slot.required_is_sample_derived else ""
    lines.append(f"  - required: {slot.required}{required_note}")

    evidence_bits = [f"nulls={col.null_count}", f"distinct={col.distinct_count}"]
    if slot.pattern:
        evidence_bits.append(f"pattern={_pattern_label(slot.pattern)}")
    if col.min_value is not None:
        evidence_bits.append(f"min={sanitize.safe_inline(col.min_value)}")
    if col.max_value is not None:
        evidence_bits.append(f"max={sanitize.safe_inline(col.max_value)}")
    lines.append(f"  - evidence: {', '.join(evidence_bits)}")

    if col.exemplars:
        exemplars = ", ".join(sanitize.safe_inline(v) for v in col.exemplars)
        lines.append(f"  - exemplars: {exemplars}")
    return lines


def _landing_entry_status(kind: str, name: str, landing: RefineLanding) -> str:
    """Return the landing-status sentence for one ``classes``/``enums``
    (``kind``) entry ``name`` against ``landing`` — the FIRST matching branch
    below wins, mirroring the priority a reviewer would actually reason in
    (was it deleted? did the human's version win? did it land at all?):

    1. Tombstoned -> it did NOT land; a machine re-proposal in this same run
       that got blocked is called out explicitly rather than left silent.
    2. Not tombstoned but the human's edited/added version won the merge ->
       it landed, but as the HUMAN'S content — the machine evidence below is
       this run's fresh inference, not necessarily what is on disk.
    3. Not tombstoned, not human-kept, but present in the landing draft ->
       it landed as this run's machine version.
    4. Defensive fallback: absent from the landing draft with no tombstone
       recorded at all. The merge rules are believed to make this
       unreachable (every exclusion is either a tombstone or an
       untouched-by-human machine deletion, and the latter never has a
       rendered evidence section to begin with), but the fallback exists so
       an unanticipated shape renders an honest sentence instead of raising
       or asserting something untrue.
    """
    tombstoned = set(landing.tombstoned_classes if kind == "classes" else landing.tombstoned_enums)
    landed_names = set(landing.class_names if kind == "classes" else landing.enum_names)
    if name in tombstoned:
        status = (
            "did NOT land — tombstoned (human-deleted; excluded from "
            "schema-draft.yaml by the three-way merge)"
        )
        if f"{kind}.{name}" in landing.blocked_resurrections:
            status += "; this run re-proposed it and the merge blocked the resurrection"
        return status
    if f"{kind}.{name}" in landing.human_kept:
        return (
            "landed as the HUMAN'S version — this run's machine changes for "
            "this entry were discarded; the evidence below describes the "
            "machine's fresh inference, not necessarily the landed definition"
        )
    if name in landed_names:
        return "landed (machine version)"
    return "did NOT land (not present in the merged draft)"


def _quoted_names(names: list[str]) -> str:
    """Double-quoted, comma-joined, ``safe_inline``'d rendering of untrusted,
    possibly-human-typed ``names`` — see :class:`RefineLanding`'s docstring
    for why double quotes (never backticks).
    """
    return ", ".join(f'"{sanitize.safe_inline(n)}"' for n in names)


def _landing_section(model: CandidateModel, landing: RefineLanding) -> list[str]:
    """Render the ``## What landed (--refine)`` section — see the module
    docstring's "Refine landing view" paragraph. Contains no backtick
    characters anywhere (only double-quoted untrusted names), so it cannot
    interact with :func:`verify_mention_completeness`'s anchor scan.
    """
    lines = [
        "## What landed (--refine)",
        "",
        "The Classes and Enums sections below describe THIS RUN'S machine "
        "inference over the samples just profiled. The draft that actually "
        "landed as schema-draft.yaml is the three-way merge of that fresh "
        "output against the human-edited draft — human wins every conflict, "
        "and every human-deleted entry (a tombstone) stays deleted. Every "
        "class/enum section below carries an explicit landing-status line, "
        "so a reviewer never has to infer which draft it is documenting.",
        "",
        # `landing.class_names` is `merged_expected_names`'s NON-ABSTRACT
        # class list (2026-07-29 round 5, P3 #2) — a class like the
        # `OperationalEntity` provenance base can be present as a KEY in the
        # landed draft's `classes:` mapping while never appearing in this
        # bullet's count/list; without the clause below, a reviewer counting
        # `classes:` entries in schema-draft.yaml gets N+1 with nothing
        # telling them why. See
        # tests/test_refine.py::test_merged_expected_names_excludes_abstract
        # (proves the exclusion at the source) and
        # tests/test_workspace.py::test_refine_report_reflects_landing_draft_not_fresh_machine_draft
        # (extended, round 5: asserts OperationalEntity is a `classes:` key
        # yet absent from this bullet's quoted list).
        f"- landing classes ({len(landing.class_names)}; non-abstract only — "
        "abstract classes like the OperationalEntity provenance base remain "
        "in the landed draft's classes: mapping but are not counted or "
        "listed here): " + (_quoted_names(landing.class_names) if landing.class_names else "none"),
        f"- landing enums ({len(landing.enum_names)}): "
        + (_quoted_names(landing.enum_names) if landing.enum_names else "none"),
        "- tombstoned classes: "
        + (_quoted_names(landing.tombstoned_classes) if landing.tombstoned_classes else "none"),
        "- tombstoned enums: "
        + (_quoted_names(landing.tombstoned_enums) if landing.tombstoned_enums else "none"),
        "- blocked resurrections: "
        + (
            _quoted_names(landing.blocked_resurrections)
            if landing.blocked_resurrections
            else "none"
        ),
        "- human-kept: " + (_quoted_names(landing.human_kept) if landing.human_kept else "none"),
        "- human re-added: "
        + (_quoted_names(landing.human_readded) if landing.human_readded else "none"),
        "- machine-updated: "
        + (_quoted_names(landing.machine_updated) if landing.machine_updated else "none"),
    ]
    no_evidence = sorted(set(landing.class_names) - {c.name for c in model.classes})
    if no_evidence:
        lines.append(
            "- landing classes with no machine-evidence section below (the "
            "tree-root container, plus any human-added/kept class not "
            "inferred from this run's samples): " + _quoted_names(no_evidence)
        )
    if landing.orphaned_classes:
        lines.append(
            "- WARNING — orphaned classes (present in the landing draft but "
            "not reachable from the tree-root container): "
            + _quoted_names(landing.orphaned_classes)
        )
    lines.append("")
    return lines


def _render_class_section(
    cls: CandidateClass,
    table: TableProfile,
    emitted: EmittedSchema,
    enum_col_by_name: dict[str, ColumnProfile],
    *,
    landing_status: str | None = None,
) -> list[str]:
    lines = [_class_anchor(cls.name), ""]
    if landing_status is not None:
        lines.append(f"- landing status: {landing_status}")
    # Double-quoted, NOT backtick-wrapped — see module docstring: anchors are
    # backtick-delimited, so untrusted text is never rendered inside
    # backticks (that would let a crafted table-name stem forge an anchor).
    lines.append(f'- source table: "{sanitize.safe_inline(cls.source_table)}"')
    if cls.seed_anchor is not None:
        evidence = ", ".join(cls.seed_anchor_evidence)
        lines.append(
            f"- seed anchor proposal: `{cls.seed_anchor}` (evidence: {evidence}) — "
            "reviewer decision, not emitted as is_a"
        )
    if cls.is_junction:
        lines.append(f"- junction: {sanitize.safe_inline(cls.junction_note or '')}")
    provenance = sorted(emitted.provenance_mapped.get(cls.name, []))
    if provenance:
        lines.append(
            "- provenance-mapped columns (covered by the OperationalEntity base): "
            + ", ".join(provenance)
        )
    lines.append("")

    cols_by_name = {c.name: c for c in table.columns}
    provenance_set = set(provenance)
    for slot in cls.slots:
        if slot.mechanical_name in provenance_set:
            continue
        col = cols_by_name[slot.name]
        if slot.enum_name is not None:
            enum_col_by_name[slot.enum_name] = col
        lines += _render_slot_lines(cls.name, slot, col)

    if cls.edges:
        lines.append("")
        for edge in cls.edges:
            lines.append(
                f"- {_edge_anchor(edge.source_class, edge.name, edge.target_class)} "
                f"(containment: {edge.containment:.0%}; evidence: name heuristic + containment)"
            )
    return lines


def _render_enum_section(
    enum_name: str,
    permissible_values: list[str],
    col: ColumnProfile | None,
    *,
    landing_status: str | None = None,
) -> list[str]:
    lines = [_enum_anchor(enum_name), ""]
    if landing_status is not None:
        lines.append(f"- landing status: {landing_status}")
    for value in permissible_values:
        lines.append(f"- {sanitize.safe_inline(value)}")
    if col is not None:
        raw_n = col.distinct_count
        retained_m = len(permissible_values)
        lines.append(f"- distinct (raw): {raw_n}")
        lines.append(f"- permissible values (retained): {retained_m}")
        if retained_m < raw_n:
            lines.append(
                f"- {raw_n - retained_m} value(s) collapsed by scrub/truncate "
                "(residual: raw-distinct values can converge after credential-shape "
                "scrubbing/truncation — the retained list above is authoritative)"
            )
    return lines


def render_inference_report(
    profile: SampleProfile,
    model: CandidateModel,
    emitted: EmittedSchema,
    *,
    engagement: str,
    config: BootstrapConfig = DEFAULT_CONFIG,
    landing: RefineLanding | None = None,
) -> str:
    """Render the total-traceability inference report for ``model``/``emitted``.

    ``landing`` is ``None`` on a fresh run — the report describes exactly
    (and only) this run's machine evidence, byte-identically to before the
    2026-07-29 round-4 fix (see the module docstring). On ``--refine``,
    passing the same call's :class:`RefineLanding` adds a leading
    ``## What landed (--refine)`` section and a "landing status" line to
    every ``### Class``/``### Enum`` section, so the report states which
    draft it is evidence for instead of leaving that to be inferred.

    Calls :func:`verify_mention_completeness` on its OWN output before
    returning — the §5.1 self-check gate.
    """
    lines: list[str] = [f"# Inference report — {sanitize.safe_inline(engagement)}", ""]
    lines += _metadata_lines(profile, model, config)
    lines.append("")

    if landing is not None:
        lines += _landing_section(model, landing)

    tables_by_name = {t.name: t for t in profile.tables}
    enum_col_by_name: dict[str, ColumnProfile] = {}

    lines.append("## Classes")
    lines.append("")
    for cls in model.classes:
        class_status = (
            _landing_entry_status("classes", cls.name, landing) if landing is not None else None
        )
        lines += _render_class_section(
            cls,
            tables_by_name[cls.source_table],
            emitted,
            enum_col_by_name,
            landing_status=class_status,
        )
        lines.append("")

    lines.append("## Enums")
    lines.append("")
    emitted_enum_set = set(emitted.emitted_enums)
    for enum in model.enums:
        if enum.name in emitted_enum_set:
            # Only elements ACTUALLY emitted (i.e. present in
            # emitted.emitted_enums) get a full `### Enum` section — that is
            # the anchor `expected_anchors` predicts, and the soundness scan
            # in `verify_mention_completeness` rejects any `### Enum`
            # heading not in that set.
            enum_status = (
                _landing_entry_status("enums", enum.name, landing) if landing is not None else None
            )
            lines += _render_enum_section(
                enum.name,
                enum.permissible_values,
                enum_col_by_name.get(enum.name),
                landing_status=enum_status,
            )
        elif enum.name in emitted.declined_enums:
            reason = sanitize.safe_inline(emitted.declined_enums[enum.name])
            lines.append(f"- declined enum `{enum.name}`: {reason}")
        else:
            # Neither emitted nor declined: every slot that would have used
            # this enum was provenance-mapped (`_emit_class` skips
            # provenance slots before `_slot_attribute` ever records either
            # outcome — see the module docstring's "Soundness" section).
            # NOT rendered as a `### Enum` heading — that would advertise a
            # schema element the draft does not actually carry.
            lines.append(
                f"- `{enum.name}` not emitted (column is provenance-mapped; covered by "
                "the OperationalEntity base, not a schema-level enum)"
            )
        lines.append("")

    report = "\n".join(lines).rstrip("\n") + "\n"
    verify_mention_completeness(report, model, emitted)
    return report


__all__ = [
    "RefineLanding",
    "expected_anchors",
    "render_inference_report",
    "verify_mention_completeness",
]
