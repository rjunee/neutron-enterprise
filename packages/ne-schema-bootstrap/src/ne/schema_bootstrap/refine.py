# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The deterministic three-way merge behind ``ne schema bootstrap --refine``
(design §4.4, NE-ESB-CLI U3).

``ne.schema_bootstrap.workspace`` keeps two YAML drafts in a workspace:
``schema-draft.machine.yaml`` (the last pure machine output — ``base``) and
``schema-draft.yaml`` (the human-reviewed/edited artifact — ``ours``).
``--refine`` re-runs the profiler/inferencer/emitter to produce a NEW machine
draft (``theirs``) and calls :func:`three_way_merge` to fold it into the
human draft: **the human side wins every conflict, and every human-deleted
entry stays deleted** (tracked in a workspace tombstone list so a later
refine cannot silently resurrect a rejected class/enum).

**Entry-level granularity, decided (not per-field).** A class or enum the
human touched at all is taken WHOLESALE from ``ours`` — this module never
merges individual attributes/slots within one class. Per-field merging would
require a second, independent diff algorithm over LinkML's own nested
attribute shape, and would silently discard the very signal --refine exists
to preserve: once a human has decided a class needs editing, the whole class
is their call, not a field-by-field negotiation. This is a documented
LIMITATION, not an oversight — see ``tests/test_refine.py::
test_human_edit_wins_over_machine`` for the positive case (one class fully
human-edited survives untouched) and the module's own scope.

The top-level ``slots:`` block (merged by :func:`_merge_scalar_keys` as a
single opaque value, the same way ``id``/``title``/``description`` are) has
the identical freeze property at BLOCK granularity, not just entry
granularity: a human edit anywhere inside ``slots:`` locks the WHOLE
shared-slot section to ``ours`` on every future refine, silently discarding
a later machine update to a DIFFERENT sibling slot in that same block. This
is a declared residual with NO diagnostic (unlike the tree-root-container
freeze, which :func:`orphaned_classes` detects) — see
:func:`three_way_merge`'s docstring and
``tests/test_refine.py::test_top_level_slots_block_merges_as_one_opaque_key``.

**Comments and formatting do not survive a refine.** ``yaml.safe_load`` /
``yaml.safe_dump`` round-trip through parsed Python objects; a human's
inline comments or blank-line grouping in ``schema-draft.yaml`` are gone
after the first ``--refine``. Declared here as a decided limitation of the
file-based iterate-to-approval loop (design §4.4), not fixed — LinkML YAML
has no widely-adopted comment-preserving round-trip library in this
package's dependency-free posture (``packages/ne-schema-bootstrap/README.md``
documents this as a Scope decision too).

The six-rule merge order below is LOCKED — see :func:`three_way_merge`'s
docstring for the exact per-name decision table, and
``tests/test_refine.py`` for one positive AND one negative/control assertion
per rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from .errors import RefineError

#: Sentinel distinguishing "key absent" from "key present with value None" —
#: a LinkML permissible-value entry is legitimately ``{value: None}`` (the
#: house-dialect bare-key rendering, see ``emit.py``'s ``_represent_none``),
#: so ``dict.get(name)`` alone cannot tell "absent" from "present but null".
_MISSING = object()

#: The two top-level sections merged at per-name (not per-field) granularity.
_NAMED_SECTIONS = ("classes", "enums")


class MergeResult(BaseModel):
    """The full, auditable outcome of one :func:`three_way_merge` call.

    Every list is SORTED for byte-determinism (see
    ``tests/test_refine.py::test_merge_is_deterministic``).
    ``human_kept``/``machine_updated``/``blocked_resurrections``/
    ``human_readded`` entries are prefixed ``"classes."``/``"enums."`` so a
    class and an enum that happen to share a name never collide in these
    combined-section lists; ``tombstoned_classes``/``tombstoned_enums`` are
    already section-scoped and carry plain (unprefixed) names.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    merged_yaml_text: str
    tombstoned_classes: list[str]
    tombstoned_enums: list[str]
    human_kept: list[str]
    machine_updated: list[str]
    blocked_resurrections: list[str]
    human_readded: list[str]


def _merge_named_section(
    section: str,
    base_sec: dict[str, Any],
    ours_sec: dict[str, Any],
    theirs_sec: dict[str, Any],
    tombstones: Sequence[str],
) -> tuple[dict[str, Any], set[str], list[str], list[str], list[str], list[str]]:
    """Apply the six LOCKED rules (see :func:`three_way_merge`'s docstring)
    to one named section (``"classes"`` or ``"enums"``), per entry name.

    Returns ``(merged_entries, new_tombstones, human_kept, machine_updated,
    blocked_resurrections, human_readded)`` — the last four already carry
    this section's ``"{section}."`` prefix.
    """
    tombstone_set = set(tombstones)
    names = set(base_sec) | set(ours_sec) | set(theirs_sec) | tombstone_set

    merged: dict[str, Any] = {}
    new_tombstones = set(tombstone_set)
    human_kept: list[str] = []
    machine_updated: list[str] = []
    blocked_resurrections: list[str] = []
    human_readded: list[str] = []

    for name in sorted(names):
        b = base_sec.get(name, _MISSING)
        o = ours_sec.get(name, _MISSING)
        t = theirs_sec.get(name, _MISSING)
        tombstoned = name in tombstone_set

        # Rule (1): tombstoned but the human re-added it -> keep ours, clear
        # the tombstone, record the re-add.
        if tombstoned and o is not _MISSING:
            merged[name] = o
            new_tombstones.discard(name)
            human_readded.append(f"{section}.{name}")
            continue

        # Rule (2): tombstoned, not re-added -> excluded; note a blocked
        # resurrection if the machine independently proposed it again.
        if tombstoned:
            if t is not _MISSING:
                blocked_resurrections.append(f"{section}.{name}")
            continue

        # Rule (3): the human deleted an entry the base machine output had
        # -> excluded, and the deletion is now tombstoned so a future
        # refine can never silently resurrect it.
        if b is not _MISSING and o is _MISSING:
            new_tombstones.add(name)
            continue

        # Rule (4): the human added or edited this entry -> ours wins,
        # whatever `theirs` says.
        if o is not _MISSING and (b is _MISSING or o != b):
            merged[name] = o
            human_kept.append(f"{section}.{name}")
            continue

        # Rule (5): untouched-by-human machine update (or a brand-new
        # machine entry) -> take theirs.
        if t is not _MISSING:
            merged[name] = t
            if b is _MISSING or t != b:
                machine_updated.append(f"{section}.{name}")
            continue

        # Rule (6): present in ours, ours == base (untouched by the human),
        # and the machine dropped it -> the machine deletion flows through;
        # excluded, no tombstone (nothing for a human to have rejected).

    # Key order: theirs-order first, then ours-only keys in ours-order.
    ordered_names = [n for n in theirs_sec if n in merged]
    for n in ours_sec:
        if n in merged and n not in ordered_names:
            ordered_names.append(n)
    for n in merged:
        if n not in ordered_names:  # pragma: no cover - defensive, unreachable
            ordered_names.append(n)
    ordered_merged = {n: merged[n] for n in ordered_names}

    return (
        ordered_merged,
        new_tombstones,
        human_kept,
        machine_updated,
        blocked_resurrections,
        human_readded,
    )


def _merge_scalar_keys(
    base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any]
) -> dict[str, Any]:
    """Merge every top-level key EXCEPT ``classes``/``enums``: take ``ours``
    if it differs from ``base`` at that key (including keys ``ours`` added
    outright), else ``theirs``; a key existing on only one side is kept.

    **A key present in ``base`` and absent from ``ours`` is a human
    deletion, and is excluded from the merge even when ``theirs`` still
    emits it** — the scalar-key mirror of :func:`_merge_named_section`'s
    Rule (3) for ``classes``/``enums``. Without this check, a fresh machine
    draft that (re)emits an auto-generated field the human had deliberately
    removed (``description``, ``prefixes``, ...) would silently resurrect
    it every refine; there is no scalar-key tombstone list, but none is
    needed — the merged draft becomes the next ``ours``, so the key stays
    absent from ``ours`` on every subsequent refine and the exclusion
    reapplies indefinitely. See ``tests/test_refine.py::
    test_scalar_key_deletion_by_human_is_not_resurrected_by_theirs``.
    """
    ordered_keys = [k for k in theirs if k not in _NAMED_SECTIONS]
    for k in ours:
        if k not in _NAMED_SECTIONS and k not in ordered_keys:
            ordered_keys.append(k)
    for k in base:
        if k not in _NAMED_SECTIONS and k not in ordered_keys:
            ordered_keys.append(k)

    merged: dict[str, Any] = {}
    for key in ordered_keys:
        b = base.get(key, _MISSING)
        o = ours.get(key, _MISSING)
        t = theirs.get(key, _MISSING)
        if b is not _MISSING and o is _MISSING:
            # Human deletion of a base-present key -> honor it; do not let
            # a fresh machine draft resurrect it.
            continue
        if o is not _MISSING and (b is _MISSING or o != b):
            merged[key] = o
        elif t is not _MISSING:
            merged[key] = t
        elif o is not _MISSING:
            merged[key] = o
    return merged


def _position_only_message(exc: yaml.YAMLError) -> str:
    """Render a YAML parser error's own diagnostic text plus a line/column
    position — and NOTHING ELSE — from a :class:`yaml.YAMLError`.

    PyYAML's own ``Mark.__str__``/``MarkedYAMLError.__str__`` call
    ``Mark.get_snippet()``, which echoes back the surrounding SOURCE LINES
    from the buffer being parsed — so ``str(exc)`` on a
    ``yaml.MarkedYAMLError`` is NOT position-only, despite this module's
    prior claim that it was (2026-07-29 Argus P1 live repro: a malformed
    ``schema-draft.yaml`` with a planted value on the broken line put that
    value's text on stderr via ``str(exc)``). This function instead reads
    only ``exc.problem``/``exc.context`` (PyYAML's own generic diagnostic
    strings, e.g. "expected ',' or ']', but got ':'" — never derived from
    the input buffer) and ``exc.problem_mark``/``exc.context_mark``'s
    ``.line``/``.column`` INTEGERS, never calling ``Mark.get_snippet()`` or
    ``str(mark)`` (which does). Falls back to ``str(exc)`` only for a
    ``yaml.YAMLError`` that isn't a ``MarkedYAMLError`` (no marks/problem
    attributes at all, so there is no snippet to have leaked). See
    ``tests/test_refine.py::test_refine_error_excludes_source_snippet_from_malformed_yaml``.
    """
    problem = getattr(exc, "problem", None)
    context = getattr(exc, "context", None)
    if problem is None and context is None:
        return str(exc)
    parts: list[str] = []
    if context:
        context_mark = getattr(exc, "context_mark", None)
        if context_mark is not None:
            parts.append(
                f"{context} (line {context_mark.line + 1}, column {context_mark.column + 1})"
            )
        else:
            parts.append(str(context))
    if problem:
        problem_mark = getattr(exc, "problem_mark", None)
        if problem_mark is not None:
            parts.append(
                f"{problem} (line {problem_mark.line + 1}, column {problem_mark.column + 1})"
            )
        else:
            parts.append(str(problem))
    return "; ".join(parts) if parts else str(exc)


def _safe_load_draft(text: str, *, label: str) -> dict[str, Any]:
    """Parse one draft's YAML ``text`` into a plain ``dict``, raising
    :class:`RefineError` — naming ``label`` and the parser's own
    position-only diagnostic (see :func:`_position_only_message`: a YAML
    syntax error names a line/column and PyYAML's own generic problem text,
    never surrounding content) — instead of letting a raw
    ``yaml.YAMLError`` escape (2026-07-29 Argus P2: a hand-edited
    ``schema-draft.yaml`` with a YAML syntax error previously produced an
    unhandled traceback from inside ``--refine``, bypassing the CLI's own
    comment calling library error messages "the reviewed disclosure
    surface"). Also rejects a document that parses cleanly but is not a
    mapping at the top level (e.g. a human accidentally turning the
    document into a YAML list) — the un-guarded code previously raised
    ``AttributeError: 'list' object has no attribute 'get'`` from inside
    :func:`three_way_merge` for that shape. See
    ``tests/test_refine.py::test_three_way_merge_raises_refine_error_on_malformed_yaml``
    and ``tests/test_refine.py::test_refine_error_excludes_source_snippet_from_malformed_yaml``.

    Also rejects a document that IS a mapping at the top level but gives
    ``classes:`` or ``enums:`` a non-mapping value (e.g. a YAML list or a
    bare scalar) — a shape ``yaml.safe_load`` accepts without complaint.
    Unguarded, that value flowed straight into :func:`_merge_named_section`,
    whose very first use of it is ``base_sec.get(name, _MISSING)`` — an
    ``AttributeError: 'list' object has no attribute 'get'`` raised from
    deep inside the merge, past the CLI's ``SchemaBootstrapError``-only
    catch, instead of the reviewed ``RefineError`` surface (2026-07-29
    Codex C1 live repro: a hand-edited draft with ``classes:`` rewritten to
    a YAML list crashed ``--refine`` with a raw traceback). See
    ``tests/test_refine.py::test_three_way_merge_raises_refine_error_on_non_mapping_named_section``.

    **One layer deeper (2026-07-29 Argus P2, round 5): a ``classes:``/
    ``enums:`` value CAN be a mapping and still crash.** The C1 fix above
    validated the section's CONTAINER type but not its KEY type — a
    hand-edited draft with an unquoted numeric class/enum key (e.g. a
    forgotten ``"2024"`` quote, an ordinary human mistake, not a hostile
    input) parses to a mapping whose keys are a mix of ``str`` and ``int``,
    which ``yaml.safe_load`` also accepts without complaint. Unguarded, that
    key flows into :func:`_merge_named_section`'s
    ``for name in sorted(names)`` — a raw
    ``TypeError: '<' not supported between instances of 'int' and 'str'``
    (Python 3's ``sorted()`` refuses to order a mixed-type sequence),
    escaping the CLI's reviewed disclosure surface exactly like the
    container-type gap C1 closed one layer up. This function now ALSO
    rejects any non-``str`` key found directly under a ``classes:``/
    ``enums:`` mapping, naming the side, the section, and the offending key
    (``repr()``'d — a mechanical YAML key, never row content). Live-proven
    by Argus end-to-end through the CLI (fresh run, hand-add an unquoted
    numeric class key, ``--refine`` -> raw traceback before this fix). See
    ``tests/test_refine.py::test_three_way_merge_raises_refine_error_on_non_string_section_key``
    (unit layer, bidirectional: quoted control still merges and lands) and
    ``tests/test_workspace.py::test_refine_non_string_class_key_raises_refine_error_and_writes_nothing``
    (end-to-end repro, byte-identical workspace afterwards). A COMPLEX key
    (e.g. a YAML sequence used as a mapping key) is a DIFFERENT PyYAML
    failure mode — ``yaml.safe_load`` itself raises ``ConstructorError``
    (unhashable key) while CONSTRUCTING the document, before this function
    ever sees a loaded value — already caught by the ``yaml.YAMLError``
    handler above; see
    ``tests/test_refine.py::test_three_way_merge_complex_yaml_key_raises_refine_error``,
    which turns that sweep reasoning into a test rather than leaving it
    asserted in this docstring alone.
    """
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RefineError(f"{label} is not valid YAML: {_position_only_message(exc)}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RefineError(
            f"{label} does not parse to a YAML mapping at the top level (got "
            f"{type(loaded).__name__})"
        )
    for section in _NAMED_SECTIONS:
        value = loaded.get(section)
        if value is not None and not isinstance(value, dict):
            raise RefineError(
                f"{label} has a non-mapping value for `{section}:` (got "
                f"{type(value).__name__}); expected a YAML mapping of name -> definition"
            )
        if isinstance(value, dict):
            for key in value:
                if not isinstance(key, str):
                    raise RefineError(
                        f"{label} has a non-string key under `{section}:` (got {key!r} of "
                        f"type {type(key).__name__}); YAML keys under {section}: must be "
                        f'strings — quote it (e.g. "2024") in the draft'
                    )
    return loaded


def three_way_merge(
    base_text: str,
    ours_text: str,
    theirs_text: str,
    *,
    tombstoned_classes: Sequence[str],
    tombstoned_enums: Sequence[str],
) -> MergeResult:
    """Deterministically merge ``ours`` (human-edited) and ``theirs`` (new
    machine output) against their common ``base`` (previous machine output),
    with the human side winning every conflict.

    Applied independently to the ``classes`` and ``enums`` top-level
    sections, PER NAMED ENTRY (see the module docstring for why this is
    entry-level, not per-field). For every name in the union of
    ``base``/``ours``/``theirs``/the relevant tombstone list, the FIRST
    matching rule below wins:

    1. Tombstoned AND present in ``ours`` -> the human re-added it: keep
       ``ours``, clear the tombstone, record in ``human_readded``.
    2. Tombstoned (and not re-added) -> excluded; if ``theirs`` also
       proposes it, record in ``blocked_resurrections``.
    3. Present in ``base`` AND absent from ``ours`` -> the human deleted
       it: excluded, and the name is ADDED to the tombstone list.
    4. Present in ``ours`` AND (absent from ``base`` OR ``ours`` differs
       structurally from ``base``) -> take ``ours`` (human-added or
       human-edited; human wins); record in ``human_kept``.
    5. Present in ``theirs`` -> take ``theirs`` (an untouched-by-human
       machine update, or a brand-new machine entry); record in
       ``machine_updated`` when it differs from ``base``.
    6. Otherwise (present in ``ours``, ``ours == base``, absent from
       ``theirs``) -> excluded: an untouched entry the machine itself
       removed flows through as a deletion.

    Every OTHER top-level key (``id``, ``name``, ``title``, ``description``,
    ``version``, ``license``, ``prefixes``, ``imports``, ``default_range``,
    ``slots``, ...) is merged by :func:`_merge_scalar_keys`: take ``ours``
    if it differs from ``base`` at that key, else ``theirs``; a key existing
    on only one side is kept; a key present in ``base`` and absent from
    ``ours`` is a human deletion and is EXCLUDED even if ``theirs`` still
    emits it (the scalar mirror of Rule (3) above — see
    ``tests/test_refine.py::
    test_scalar_key_deletion_by_human_is_not_resurrected_by_theirs``).
    ``classes``/``enums`` are omitted from the merged document entirely
    when empty (mirroring ``emit.py``'s own
    ``if enums: data["enums"] = enums`` convention).

    **The top-level ``slots:`` block is one of these keys, and is merged as
    ONE OPAQUE value — a declared residual, not a fixed gap.** A single
    human edit anywhere inside ``slots:`` freezes the ENTIRE shared-slot
    block to ``ours`` on every future refine, silently discarding a later
    machine update to a DIFFERENT slot in that same block. This is the same
    freeze class the tree-root-container case has (diagnosed there by
    :func:`orphaned_classes`) but carries NO diagnostic here. Proven,
    bidirectionally (the freeze fires when the human touches the block; an
    untouched block still takes the machine's update), by
    ``tests/test_refine.py::test_top_level_slots_block_merges_as_one_opaque_key``.

    Serialized with ``yaml.safe_dump(data, sort_keys=False,
    allow_unicode=True)`` — deterministic for a given set of inputs (see
    ``tests/test_refine.py::test_merge_is_deterministic``). Human comments
    and formatting in ``ours_text`` do NOT survive — see the module
    docstring's documented limitation.
    """
    base = _safe_load_draft(base_text, label="base draft (schema-draft.machine.yaml)")
    ours = _safe_load_draft(ours_text, label="ours draft (schema-draft.yaml)")
    theirs = _safe_load_draft(theirs_text, label="theirs draft (new machine output)")

    merged_classes, new_tombstoned_classes, kept_c, updated_c, blocked_c, readd_c = (
        _merge_named_section(
            "classes",
            base.get("classes", {}) or {},
            ours.get("classes", {}) or {},
            theirs.get("classes", {}) or {},
            tombstoned_classes,
        )
    )
    merged_enums, new_tombstoned_enums, kept_e, updated_e, blocked_e, readd_e = (
        _merge_named_section(
            "enums",
            base.get("enums", {}) or {},
            ours.get("enums", {}) or {},
            theirs.get("enums", {}) or {},
            tombstoned_enums,
        )
    )

    merged_scalars = _merge_scalar_keys(base, ours, theirs)

    # Top-level key order: theirs-order first (classes/enums land at their
    # theirs-side position), then ours-only keys, then base-only keys.
    ordered_keys = list(theirs.keys())
    for k in ours:
        if k not in ordered_keys:
            ordered_keys.append(k)
    for k in base:
        if k not in ordered_keys:
            ordered_keys.append(k)

    merged: dict[str, Any] = {}
    for key in ordered_keys:
        if key == "classes":
            if merged_classes:
                merged["classes"] = merged_classes
        elif key == "enums":
            if merged_enums:
                merged["enums"] = merged_enums
        elif key in merged_scalars:
            merged[key] = merged_scalars[key]
    # A key that exists ONLY on a side never touched above (defensive; every
    # scalar key present anywhere is already covered by `ordered_keys`
    # having been built from the union of all three documents' keys).

    merged_yaml_text = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)

    return MergeResult(
        merged_yaml_text=merged_yaml_text,
        tombstoned_classes=sorted(new_tombstoned_classes),
        tombstoned_enums=sorted(new_tombstoned_enums),
        human_kept=sorted(kept_c + kept_e),
        machine_updated=sorted(updated_c + updated_e),
        blocked_resurrections=sorted(blocked_c + blocked_e),
        human_readded=sorted(readd_c + readd_e),
    )


def parse_merged_yaml(merged_yaml_text: str) -> dict[str, Any]:
    """``yaml.safe_load`` a merged draft's text into a plain ``dict``.

    Exists so callers outside this module (in particular
    ``ne.schema_bootstrap.workspace``, which is deliberately stdlib+relative-
    import-only — see that module's docstring) never need their own ``yaml``
    import just to hand :func:`find_tree_root_class`/
    :func:`merged_expected_names` a parsed document — this package's ``yaml``
    import stays confined to ``emit.py`` and this module (see
    ``tests/test_boundaries.py::test_refine_allowance_permits_yaml_but_nothing_else``).

    **Input contract (2026-07-29 round 5):** ``merged_yaml_text`` is, in
    every caller in this codebase, ``three_way_merge``'s own
    ``MergeResult.merged_yaml_text`` — text this package itself serialized
    via ``yaml.safe_dump``. A raw ``yaml.YAMLError`` on text this package
    did NOT itself produce (e.g. a hypothetical future caller handing this
    function arbitrary hand-edited YAML directly) is an UNHANDLED shape —
    intentionally not wrapped in :class:`~ne.schema_bootstrap.errors.RefineError`
    here, because every internal caller only ever feeds it already-merged,
    already-validated ``three_way_merge`` output, not raw operator input.
    """
    return yaml.safe_load(merged_yaml_text) or {}


def find_tree_root_class(merged: dict[str, Any]) -> str:
    """Return the unique class name with ``tree_root: true`` in ``merged``.

    Raises :class:`RefineError` naming every candidate found when there is
    not EXACTLY one — zero (nothing to generate against) or two-or-more
    (ambiguous which class ``validate_draft_generates`` should treat as
    ``top_class``) are both refused rather than guessed at. See
    ``tests/test_refine.py::test_find_tree_root_unique_and_errors``.

    **Input contract (2026-07-29 round 5):** ``merged`` is expected to be a
    draft parsed (via :func:`parse_merged_yaml`) from ``three_way_merge``
    output, whose ``classes:``/``enums:`` keys are guaranteed ``str`` by
    ``_safe_load_draft``'s own key-type check. A caller-BUILT mapping with a
    non-string key under ``classes:`` is an UNHANDLED shape here: the
    ``sorted(...)`` call below raises a raw ``TypeError`` on a mixed
    ``str``/``int`` key set, same as the gap ``_safe_load_draft`` closes for
    every parsed-from-text draft — this function has no text to validate,
    only an already-parsed mapping, so it trusts its caller.
    """
    classes = merged.get("classes", {}) or {}
    candidates = sorted(
        name
        for name, entry in classes.items()
        if isinstance(entry, dict) and entry.get("tree_root") is True
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RefineError(
            f"merged draft has no class with `tree_root: true` (classes present: "
            f"{sorted(classes)!r})"
        )
    raise RefineError(f"merged draft has multiple classes with `tree_root: true`: {candidates!r}")


def merged_expected_names(merged: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return ``(class_names, enum_names)`` for a merged draft's
    ``validate_draft_generates`` call: every NON-ABSTRACT class key
    (``OperationalEntity`` is excluded via its ``abstract: true`` flag,
    mirroring ``roundtrip.py``'s own ``expected_classes`` contract) and
    every enum key, both sorted for determinism. See
    ``tests/test_refine.py::test_merged_expected_names_excludes_abstract``.

    **Input contract (2026-07-29 round 5):** same as :func:`find_tree_root_class`
    above — ``merged`` is expected to be ``three_way_merge`` output with
    ``str``-only ``classes:``/``enums:`` keys; a caller-built mapping
    violating that is an UNHANDLED shape (raw ``TypeError`` from
    ``sorted(...)``), not guarded here.
    """
    classes = merged.get("classes", {}) or {}
    class_names = sorted(
        name
        for name, entry in classes.items()
        if not (isinstance(entry, dict) and entry.get("abstract") is True)
    )
    enums = merged.get("enums", {}) or {}
    return class_names, sorted(enums)


def orphaned_classes(merged: dict[str, Any], top_class: str) -> list[str]:
    """Return every non-abstract class in ``merged`` that is NOT reachable
    from ``top_class`` by following ``range:`` references through classes'
    (and the tree-root container's) ``attributes`` — sorted, for
    determinism (2026-07-29 Argus P1).

    **Why this exists.** ``--refine``'s merge is entry-level (see the module
    docstring): rule 4 takes a human-edited class WHOLESALE from ``ours``,
    including the tree-root container class itself if a human ever edits it
    (e.g. removing an attribute when rejecting a class — exactly what
    ``tests/test_workspace.py::test_refine_deleted_class_stays_deleted_across_two_refines``
    does). Once the container is human-frozen this way, it never picks up
    ``theirs``'s container updates again (rule 4 always wins for it), so any
    class the machine adds or updates on a LATER refine lands in
    ``classes:`` but is never wired into the container's ``attributes`` —
    silently unreachable from ``top_class``. ``validate_draft_generates``
    does not catch this: LinkML happily generates a class that nothing
    references. This function is therefore a DIAGNOSTIC (surfaced by
    ``ne.schema_bootstrap.workspace.run_bootstrap`` as
    ``BootstrapRunResult.orphaned_classes`` and printed as a CLI warning by
    ``tools/ne-cli/src/ne/cli/schema.py``), not a validation gate — it never
    raises. See
    ``tests/test_workspace.py::test_refine_container_freeze_orphans_a_later_machine_class``.

    **Input contract (2026-07-29 round 5):** same as
    :func:`find_tree_root_class` above — ``merged`` is expected to be
    ``three_way_merge`` output with ``str``-only ``classes:`` keys; a
    caller-built mapping violating that is an UNHANDLED shape (raw
    ``TypeError`` from the final ``sorted(...)`` over a mixed-type set), not
    guarded here.

    Traverses BFS from ``top_class`` through every ``range:`` value found in
    a class's own ``attributes`` (own slots AND, for the tree-root
    container, its per-table record-set attributes — both use the same
    ``range:`` key, see ``emit.py``'s ``_slot_attribute``/tree-root
    construction), so a class reachable only transitively (through another
    reachable class's FK attribute) is still counted as reachable, not just
    classes directly under the container.
    """
    classes = merged.get("classes", {}) or {}
    reachable: set[str] = set()
    frontier = [top_class] if top_class in classes else []
    while frontier:
        name = frontier.pop()
        if name in reachable:
            continue
        reachable.add(name)
        entry = classes.get(name)
        if not isinstance(entry, dict):
            continue
        attrs = entry.get("attributes")
        if not isinstance(attrs, dict):
            continue
        for attr in attrs.values():
            if not isinstance(attr, dict):
                continue
            rng = attr.get("range")
            if isinstance(rng, str) and rng in classes and rng not in reachable:
                frontier.append(rng)

    non_abstract = {
        name
        for name, entry in classes.items()
        if not (isinstance(entry, dict) and entry.get("abstract") is True)
    }
    return sorted(non_abstract - reachable)


__all__ = [
    "MergeResult",
    "find_tree_root_class",
    "merged_expected_names",
    "orphaned_classes",
    "parse_merged_yaml",
    "three_way_merge",
]
