# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``pr-body.md`` emitter (design §5.2) — the ``diffs.py`` conventions applied
to a schema-bootstrap draft.

v1 review is a git PR (design §5.2): there is no deployed platform instance
at discovery time to host a NE-066 proposal row, so the reviewable artifact
is a PR body carrying the draft schema (dynamically fenced — the same
``ne.flywheel.diffs`` idiom this package's :mod:`ne.schema_bootstrap.sanitize`
locally reimplements) plus a tracked-manual-apply footer stating the tool
NEVER applies or auto-lands anything itself.

**The round-trip line states only what this function can prove
(2026-07-28 Argus P2).** ``render_pr_body`` never runs
:func:`ne.schema_bootstrap.roundtrip.validate_draft_generates` itself and
takes no evidence that it ran — U2 ships no CLI (that is U3's job) to wire
the two together. Earlier this module asserted the draft "generated through
the isolated ne-schema codegen venv" unconditionally, regardless of whether
that check had actually run — a caller that renders the PR body before
running the round-trip (or after catching a ``DraftGenerationError``) would
still ship a body claiming success. ``render_pr_body`` now takes an explicit
``round_trip_verified: bool | None`` — ``True`` only when the CALLER
actually observed :func:`validate_draft_generates` return successfully;
``False`` when it observed a failure; ``None`` (the default) when it simply
was not run before this body was rendered. The rendered line always
reflects exactly that state — never more than the caller can prove.
"""

from __future__ import annotations

from . import sanitize
from .config import DEFAULT_CONFIG, BootstrapConfig
from .emit import EmittedSchema
from .model import CandidateModel

_MANUAL_APPLY_FOOTER = (
    "Tracked manual apply: a human reviews this PR (the draft schema below plus "
    "the accompanying `inference-report.md`) and, on approval, lands it through "
    "the EXISTING §5.3 procedure — copy `schema-draft.yaml` into "
    "`schemas/<engagement>.yaml`, add one `SchemaSpec` entry, bump `version` + "
    "add a CHANGELOG entry, then run `ne schema generate`. The tool NEVER "
    "applies, commits, or auto-lands the draft itself."
)


def _round_trip_line(round_trip_verified: bool | None) -> str:
    if round_trip_verified is True:
        return (
            "- round-trip: VERIFIED — `validate_draft_generates` ran and passed against "
            "the isolated ne-schema codegen venv"
        )
    if round_trip_verified is False:
        return (
            "- round-trip: NOT VERIFIED — `validate_draft_generates` was run and FAILED; "
            "do not land this draft until it passes"
        )
    return (
        "- round-trip: NOT YET RUN — this document does not itself run or confirm "
        "`validate_draft_generates`; run it against the isolated ne-schema codegen venv "
        "before landing this draft"
    )


def _metadata_bullets(
    model: CandidateModel,
    emitted: EmittedSchema,
    engagement: str,
    config: BootstrapConfig,
    round_trip_verified: bool | None,
) -> list[str]:
    n_classes = len(model.classes)
    n_slots = sum(len(c.slots) for c in model.classes)
    n_edges = sum(len(c.edges) for c in model.classes)
    n_enums = len(emitted.emitted_enums)
    lines = [
        f"- engagement: {sanitize.safe_inline(engagement)}",
        "- artifacts: schema-draft.yaml, inference-report.md",
        f"- classes: {n_classes}",
        f"- slots: {n_slots}",
        f"- edges: {n_edges}",
        f"- enums emitted: {n_enums}",
        f"- enums declined: {len(emitted.declined_enums)}",
    ]
    for key, value in config.model_dump().items():
        lines.append(f"- threshold {key}: {value}")
    lines.append(_round_trip_line(round_trip_verified))
    lines.append("- model routing: none (deterministic floor; LLM labeling is U5, opt-in)")
    return lines


def _review_notes(emitted: EmittedSchema) -> list[str]:
    lines = [
        "- base class name: every draft class is `is_a: OperationalEntity` "
        "(NOT `<Engagement>Entity`, despite the design doc's prose) — the real "
        "codegen Tier-2 graph emitter hardwires this literal name "
        "(`packages/ne-schema/codegen/generate.py:211,215`); an engagement "
        "schema wanting graph contracts needs this name today.",
        "- seed anchors are reviewer proposals only — no class is emitted "
        "`is_a: <SeedClass>`; adopting one requires manually importing "
        "`operational.yaml` and editing `is_a` by hand (see the inference report "
        "for the evidence behind each proposal).",
    ]
    if emitted.declined_enums:
        for name, reason in sorted(emitted.declined_enums.items()):
            lines.append(f"- declined enum `{name}`: {sanitize.safe_inline(reason)}")
    else:
        lines.append("- no enums were declined.")
    lines.append(
        "- known residual: `draft_graph.py`'s enum import is hardwired to "
        "`ne.schema.generated.operational` (`generate.py:250-257`, outside this "
        "unit's scope) — an enum-bearing draft's generated graph module is not "
        "importable as-is. A future `ne-schema` unit needs a "
        "`--graph-enum-module` argument added to `generate.py` to lift this."
    )
    return lines


def render_pr_body(
    model: CandidateModel,
    emitted: EmittedSchema,
    *,
    engagement: str,
    config: BootstrapConfig = DEFAULT_CONFIG,
    round_trip_verified: bool | None = None,
) -> str:
    """Render ``pr-body.md`` for the ``engagement`` schema draft (design §5.2).

    ``round_trip_verified`` states only what the CALLER can prove: pass
    ``True`` only after observing :func:`ne.schema_bootstrap.roundtrip.validate_draft_generates`
    return successfully for this exact draft, ``False`` after observing it
    raise, and leave it ``None`` (the default) when the round-trip was not
    run before calling this function — this function never runs it itself
    and never claims more than the caller told it. See the module docstring.
    """
    lines: list[str] = [
        f"# [schema-bootstrap] {sanitize.safe_inline(engagement)} schema draft",
        "",
    ]
    lines += _metadata_bullets(model, emitted, engagement, config, round_trip_verified)
    lines.append("")
    lines.append("## schema-draft.yaml")
    lines.append("")
    lines.append(sanitize.fenced(emitted.yaml_text, lang="yaml"))
    lines.append("")
    lines.append("## Review notes")
    lines.append("")
    lines += _review_notes(emitted)
    lines.append("")
    lines.append("## Tracked manual apply")
    lines.append("")
    lines.append(_MANUAL_APPLY_FOOTER)
    return "\n".join(lines).rstrip("\n") + "\n"


__all__ = ["render_pr_body"]
