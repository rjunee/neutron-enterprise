# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C12-style injection-safe rendering: the local three-boundary sanitizer.

This is a DELIBERATE LOCAL COPY of the renderer sanitizer boundary in
``packages/ne-flywheel/src/ne/flywheel/diffs.py``, not an import
of ``ne.flywheel`` — this package stays dependency-free of sibling platform
packages (see :mod:`ne.schema_bootstrap.hygiene`'s docstring for the same
discipline applied to credential-shape scrubbing). If the two copies drift,
that is a defect to fix by re-syncing this copy, not a reason to import
across packages.

**The three-boundary rule** (``diffs.py``'s own docstring, restated for this
package): untrusted, profile-derived text reaches a rendered, reviewed
artifact (:mod:`ne.schema_bootstrap.report`, :mod:`ne.schema_bootstrap.prbody`)
through EXACTLY ONE of three deterministic sanitizers:

1. :func:`safe_inline` — for INLINE (single-line, mid-line) interpolation:
   bullet values, table cells, comma-joined refs. Drops non-printables,
   collapses all whitespace to single spaces (so line-start markdown is
   structurally impossible), and backslash-escapes the mid-line
   markdown/GitHub-significant characters.
2. :func:`fenced` — for MULTI-LINE untrusted text (there is none in this
   package's v1 report content, since every profile-derived string is
   truncated to ``exemplar_max_chars`` and rendered inline; :func:`fenced`
   exists here for :mod:`ne.schema_bootstrap.prbody`'s ``schema-draft.yaml``
   section, where the "untrusted" text is the draft schema's own scalars,
   already embedded inside well-formed YAML): a DYNAMIC fence longer than
   any backtick run inside, so a ``` ``` ``` embedded in the text can never
   break out.
3. The third boundary in ``diffs.py`` (``_comment_lines``, for the OPA/rego
   review-stub artifact) has **no artifact in this package** — this package
   never emits rego. Stated explicitly so a reviewer checking "does every
   boundary have a use" finds an intentional gap, not an oversight.

**Untrusted inputs** (profile-derived, reach a RENDERED, human-facing artifact
— :mod:`ne.schema_bootstrap.report`'s Markdown, :mod:`ne.schema_bootstrap.prbody`'s
``pr-body.md`` — through one of the two functions above): table names
(sample-file stems, i.e. ``CandidateClass.source_table``), raw column headers
(``ColumnProfile.name`` / ``CandidateSlot.name``), exemplars
(``ColumnProfile.exemplars``), min/max (``ColumnProfile.min_value`` /
``.max_value``), permissible values (``CandidateEnum.permissible_values``),
and declined-enum reasons (:attr:`ne.schema_bootstrap.emit.EmittedSchema.declined_enums`
values — these can echo a raw hostile permissible value back verbatim).

**A third path exists and is NOT routed through either function here**
(2026-07-28 P3-1, corrected — this paragraph previously claimed the two
functions above were the ONLY path, which was false): ``emit.py`` interpolates
``cls.source_table`` RAW (unsanitized) into the emitted YAML class
``description`` field (see ``_emit_class``), and the emitted
``schema-draft.yaml`` is itself a written, PR-reviewed artifact — not merely
an intermediate. This path is safe in practice, not because it is sanitized,
but because of three OTHER properties stacked together: (1) PyYAML's
``SafeDumper`` quotes/escapes the scalar at dump time, so a hostile stem
cannot break YAML structure; (2) ``linkml``'s generator itself escapes quotes
when rendering the value into the generated Pydantic class's docstring, so a
hostile stem cannot break the generated Python source either; and (3)
:mod:`ne.schema_bootstrap.prbody` embeds the whole YAML block only inside
:func:`fenced`'s DYNAMIC fence, which grows past any backtick run the stem
contains. See
``tests/test_roundtrip.py::test_hostile_table_stem_roundtrips_and_prbody_fence_adapts``
for the round-trip proof of all three properties together — a stem
containing a 3-backtick run, quotes, and a space survives the full pipeline,
generates through the real venv, and forces the PR-body fence to widen.

**Trusted-by-construction, never routed through a sanitizer**: class names,
mechanical slot names, and enum names — every one of these is produced by
:mod:`ne.schema_bootstrap.model`'s ``to_camel_case``/``to_snake_case``
transforms, which emit only ``[A-Za-z0-9_]`` characters. These are the
anchor tokens :mod:`ne.schema_bootstrap.report`'s mention-completeness
self-check matches against; if they were sanitized (and thus potentially
mutated), the anchors themselves could drift from the emitted schema's real
identifiers.

Also trusted-by-construction: :attr:`ne.schema_bootstrap.model.CandidateClass.seed_anchor_evidence`.
Despite being *computed from* untrusted column headers (normalized via
``to_snake_case`` inside ``infer._seed_anchor``), every value that actually
reaches ``seed_anchor_evidence`` is the output of
``normalized_names & seed_attrs`` — a **set intersection** with
:data:`ne.schema_bootstrap.seeds.SEED_CLASS_SIGNATURES`'s hardcoded,
``[A-Za-z0-9_]``-only attribute names. A set intersection can only ever
contain elements equal to members of *both* operands, so no untrusted string
can appear in the result unless it happens to collide byte-for-byte with one
of the ~35 hardcoded seed attribute names (e.g. ``"email"``, ``"status"``,
``"placed_at"``) — at which point it IS one of those trusted strings, not
attacker-controlled content. This safety rests entirely on ``_seed_anchor``
continuing to intersect rather than pass through; a future change that
returns raw column names (or anything not filtered through
``SEED_CLASS_SIGNATURES``) would silently break this invariant and must
route through :func:`safe_inline` at that point.
"""

from __future__ import annotations

import re

#: Markdown / GitHub-significant characters that are backslash-escaped inline
#: so they render literally: code spans / links / images / autolinks / raw
#: HTML / emphasis / tables / strikethrough / HTML entities, plus GitHub
#: ``#123`` issue-closing refs and ``@`` mentions. Byte-for-byte the same set
#: as ``ne.flywheel.diffs._INLINE_ESCAPE``.
_INLINE_ESCAPE = frozenset("\\`*_{}[]()<>#!|~@&")


def safe_inline(text: str) -> str:
    r"""Sanitize UNTRUSTED profile-derived text for INLINE interpolation ONLY.

    Mirrors ``ne.flywheel.diffs._safe`` exactly, three deterministic passes:

    1. Drop non-printable, non-whitespace characters (NUL / ESC / ANSI / DEL).
    2. Collapse every whitespace run (newlines / CR / tabs / U+2028 …) to a
       single space and strip — so line-start markdown (headings, fences,
       list items, blockquotes) is structurally impossible.
    3. Backslash-escape the mid-line-significant characters in
       :data:`_INLINE_ESCAPE`.
    """
    stripped = "".join(ch for ch in text if ch.isprintable() or ch.isspace())
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    return "".join(f"\\{ch}" if ch in _INLINE_ESCAPE else ch for ch in collapsed)


def fence(text: str) -> str:
    """Return a backtick fence longer than any backtick run inside ``text``.

    Mirrors ``ne.flywheel.diffs._fence`` exactly: ``max(3, longest_run + 1)``
    backticks, so a ``` ``` ``` (or longer) embedded in untrusted text can
    never break out of the fenced block.
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


def fenced(text: str, *, lang: str = "") -> str:
    """Wrap ``text`` in a dynamic fence (optionally language-tagged).

    Mirrors ``ne.flywheel.diffs._fenced`` exactly.
    """
    f = fence(text)
    return f"{f}{lang}\n{text}\n{f}"


__all__ = ["fence", "fenced", "safe_inline"]
