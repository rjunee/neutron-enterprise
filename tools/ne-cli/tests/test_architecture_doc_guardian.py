# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Guardian: `docs/architecture/neutron-enterprise-architecture.md` cannot silently drift.

**Root cause this guardian closes.** The spec and build ledger are re-read and
enforced by a session hook on every build/decision turn, so they cannot drift far
before someone notices. The customer-facing master architecture doc had, until this
guardian, ZERO enforcement of any kind (grep-verified at dispatch: no test, CI
workflow, or guardian referenced it) — it could go stale for months across dozens
of merges with nothing red anywhere. That is exactly what happened: by the time
this guardian was written the doc had gone 7 days and 29 merges without a touch and
was missing whole shipped subsystems (OpenFGA/ReBAC, the live-proof programme,
PgBouncer/pooled Postgres, the assurance/provenance model).

**Why this shape, and not the obvious alternatives.** Two tempting alternatives
were considered and rejected:

1. A *freshness* check tying the doc's last-touch date (or a version string) to
   the build ledger's newest row. Rejected: it is satisfiable by bumping a date
   literal with zero content change — it would manufacture false confidence
   (CI green, doc still rotting) rather than prevent the failure mode observed.
2. An *N-merges-without-a-doc-touch* CI signal. Rejected: satisfiable by a
   whitespace-only touch, requires an unshallow git history in CI to compute,
   and — like (1) — fails for a reason unconnected to the doc's actual content.

This guardian instead fails **only when the doc's content is actually wrong**:
either a shipped `packages/ne-*` module has no mention anywhere in the prose (the
observed failure mode, replayed), or a rename-resistant proper-noun anchor for a
load-bearing shipped concept has disappeared from the text. Both are checked
against the REAL tree on every run (`test_every_shipped_module_is_documented`,
`test_concept_anchors_present`), and both are proven non-vacuous in-suite forever
via synthetic red-case tests that do not depend on the doc ever having been wrong
at test-writing time (`test_new_module_without_doc_mention_goes_red`,
`test_anchor_removal_goes_red`, etc.) — see the synthetic red-case tests at the bottom of this file.

**No new CI wiring needed.** The root `pyproject.toml` sets
`testpaths = ["packages/*/tests", "tools/*/tests"]`, so this file is picked up by
the existing blocking `uv run pytest` job in `ci.yml` automatically, exactly like
every other `tools/ne-cli/tests/*.py` guardian.

**Known residual, deliberately deferred.** This guardian is one-directional:
it catches a shipped `packages/ne-*` module missing from the doc, but not the
reverse — a module the doc still describes after being removed from
`packages/`. A symmetric reverse check is feasible (scan the doc for
backtick-fenced `` `ne-...` `` tokens and diff against `shipped_modules()`)
but was deliberately not built here: the doc legitimately contains bare
`ne-[a-z-]*`-shaped substrings from ordinary hyphenated English
("machine-checkable", "fine-grained", "one-page", "one-shot" all match a
naive scan), so a real reverse check needs a backtick-only scan plus a small
allowlist to avoid false positives on that prose — a real piece of work, not
a one-line addition, and out of scope for this review pass.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC_PATH = REPO_ROOT / "docs" / "architecture" / "neutron-enterprise-architecture.md"
PACKAGES_DIR = REPO_ROOT / "packages"

# ---------------------------------------------------------------------------
# The escape hatch. Target is EMPTY: every architecturally-relevant shipped
# module should be named in the doc's Part IV component/module inventory. A
# package only belongs here if it is genuinely NOT a customer-visible
# architectural component (e.g. a pure internal build-tooling leaf) — and the
# rationale must say so in a full sentence a reviewer can evaluate on its own.
# Hygiene on this dict is itself guarded below (test_allowlist_hygiene): a
# stale key (package no longer exists) or an empty/whitespace rationale fails.
# ---------------------------------------------------------------------------
ALLOWED_UNDOCUMENTED: dict[str, str] = {
    "ne-envbuild": (
        "Internal sidecar-venv build-tooling leaf (provisions hash-pinned uv "
        "virtualenvs to keep heavy dependency closures such as docling/torch "
        "and presidio/spaCy out of the platform lock/SBOM) — it is a build-time "
        "implementation detail with no customer-visible behavior, deployment "
        "footprint, or architectural role, so it does not belong in a "
        "customer-facing system-architecture narrative."
    ),
}

# Rename-resistant proper-noun anchors for exactly the subsystems whose
# absence from the doc was this unit's motivating drift. Each must appear
# verbatim (substring match is fine here — these are multi-word/distinctive
# enough that false positives are not a practical concern, unlike the
# short package-name case above which needs the boundary regex).
CONCEPT_ANCHORS: tuple[str, ...] = (
    "OpenFGA",
    "ReBAC",
    "Apache AGE",
    "SHACL",
    "BFO",
    "Object Lock",
    "docs/live-proof-coverage.md",
    "config_hash",
    "AIR_GAP",
    "Model Context Protocol",
    # "connection pooler" (round-2's original choice) is a common noun that a
    # legitimate future edit ("transaction-mode pooler", etc.) could trip as
    # a false positive, violating this tuple's own "rename-resistant proper
    # noun" contract. "PgBouncer" is the actual pinned pooler product
    # (edoburu/pgbouncer:v1.25.2-p0 per docs/live-proof-coverage.md) and is
    # genuinely rename-resistant.
    "PgBouncer",
)


def shipped_modules(packages_dir: Path) -> list[str]:
    """Sorted list of `ne-*` directory names directly under ``packages_dir``.

    Deliberately just "a directory named ne-*" — not "has a pyproject.toml" or
    any other maturity gate. A brand-new module directory is exactly the
    drift this guardian must catch from day one, before it has grown a
    pyproject.toml or anything else; a stricter membership test would let a
    freshly-scaffolded, genuinely-shipping module slip past undetected for
    however long it takes to acquire the extra marker. (Exactly what the
    in-suite `test_new_module_without_doc_mention_goes_red` and the by-hand
    check below exercise: an empty directory must still be caught.)
    """
    if not packages_dir.is_dir():
        return []
    names = [p.name for p in packages_dir.iterdir() if p.is_dir() and p.name.startswith("ne-")]
    return sorted(names)


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def visible_doc_text(doc_text: str) -> str:
    """Strip HTML comments before any content check runs.

    A mention or anchor placed inside ``<!-- -->`` renders to nothing in the
    rendered page and is easy to miss when skimming a raw-Markdown diff, so it
    would silently satisfy a naive substring check without any visible
    content change — a doc author (or an adversarial diff) could "document" a
    module by dropping its name inside a comment and every check below would
    go green over an otherwise-empty page. All real-tree checks therefore run
    on visible text only.

    Scope, deliberately narrow: this sweeps HTML comments only, not every way
    Markdown-adjacent HTML can hide text (e.g. an inline `display:none` div).
    Those constructs are glaring in a rendered diff and are left to human
    review, consistent with the recorded design decision that this guardian
    catches *absence*, not prose quality — see the module docstring above.
    """
    return _HTML_COMMENT_RE.sub("", doc_text)


def _name_boundary_pattern(name: str) -> re.Pattern[str]:
    """A regex matching ``name`` only at a non-word/non-hyphen boundary.

    This is the load-bearing anti-cheat: without the trailing
    ``(?![\\w-])``, a doc that only ever says ``ne-permissions`` would
    satisfy the longer ``ne-permissions-rebac`` requirement (and vice
    versa with the leading lookbehind), because plain substring search
    treats one package name as a prefix of the other. Hyphenated package
    names make this a real collision, not a hypothetical one.
    """
    return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])")


def undocumented_modules(
    module_names: list[str], doc_text: str, allowed: dict[str, str]
) -> list[str]:
    """Modules in ``module_names`` mentioned nowhere in ``doc_text`` and not allowlisted.

    Returns a sorted list (empty means everything is covered). A module
    counts as "documented" only on a genuine word-boundary match of its
    exact name — see `_name_boundary_pattern`.
    """
    missing = []
    for name in module_names:
        if name in allowed:
            continue
        if not _name_boundary_pattern(name).search(doc_text):
            missing.append(name)
    return sorted(missing)


# A real reviewable sentence, not a token gesture.
MIN_RATIONALE_LEN = 40


def hygiene_errors(module_names: list[str], allowed: dict[str, str]) -> list[str]:
    """Guard the escape hatch itself: stale keys and empty/token rationales are errors.

    - A key that is not an existing package name is stale (the package was
      renamed/removed and the allowlist entry was never cleaned up).
    - A rationale that is empty or whitespace-only defeats the "written,
      defensible reason" requirement without technically being absent.
    - A rationale shorter than `MIN_RATIONALE_LEN` characters defeats the same
      requirement more subtly: a bare word like "internal" is non-empty but is
      not a reviewable sentence a reviewer can evaluate on its own.
    """
    errors = []
    module_set = set(module_names)
    for key, rationale in allowed.items():
        if key not in module_set:
            errors.append(
                f"ALLOWED_UNDOCUMENTED key {key!r} is not a package under packages/ "
                "— stale allowlist entry, remove it"
            )
        if not rationale or not rationale.strip():
            errors.append(f"ALLOWED_UNDOCUMENTED[{key!r}] has an empty/whitespace rationale")
        elif len(rationale.strip()) < MIN_RATIONALE_LEN:
            errors.append(
                f"ALLOWED_UNDOCUMENTED[{key!r}] rationale is too short to be a reviewable "
                f"justification (< {MIN_RATIONALE_LEN} chars)"
            )
    return errors


# ---------------------------------------------------------------------------
# Real-tree tests
# ---------------------------------------------------------------------------


def test_every_shipped_module_is_documented() -> None:
    modules = shipped_modules(PACKAGES_DIR)
    assert modules, "expected at least one packages/ne-* module — packages/ layout changed?"
    doc_text = visible_doc_text(DOC_PATH.read_text(encoding="utf-8"))
    missing = undocumented_modules(modules, doc_text, ALLOWED_UNDOCUMENTED)
    assert missing == [], (
        f"new module(s) packages/{{{', '.join(missing)}}} are not mentioned in the "
        "architecture doc — integrate each one into the narrative at the layer it "
        "belongs (see doc Part III for customer-facing framing, Part IV §4.1 module "
        "inventory / §4.4 component table for the architect-reference listing), or "
        "add an ALLOWED_UNDOCUMENTED entry with a written rationale explaining why "
        "it is not an architectural component. Do NOT satisfy this check with a "
        "bare name-drop that reads as a changelog bolt-on."
    )


def test_concept_anchors_present() -> None:
    doc_text = visible_doc_text(DOC_PATH.read_text(encoding="utf-8"))
    missing = [anchor for anchor in CONCEPT_ANCHORS if anchor not in doc_text]
    assert missing == [], (
        f"load-bearing concept anchor(s) missing from the architecture doc: {missing} "
        "— these are rename-resistant proper nouns for shipped subsystems that were "
        "the motivating drift for this guardian; if a rewrite genuinely renames the "
        "underlying concept, update CONCEPT_ANCHORS in the same diff as the doc edit "
        "so the anchor keeps tracking something real."
    )


def test_allowlist_hygiene() -> None:
    modules = shipped_modules(PACKAGES_DIR)
    errors = hygiene_errors(modules, ALLOWED_UNDOCUMENTED)
    assert errors == [], "\n".join(errors)
    assert len(ALLOWED_UNDOCUMENTED) <= 3, (
        "ALLOWED_UNDOCUMENTED is capped at 3 entries by design — if you need a 4th, "
        "that is a signal the doc is accumulating undocumented architecture rather "
        "than genuinely-non-architectural build tooling; integrate the module into "
        "the doc instead of widening the escape hatch"
    )


def test_doc_references_liveproof_ledger() -> None:
    """Named separately from the anchor sweep to pin the assurance-narrative tie.

    Subsumed by `test_concept_anchors_present` today, but kept as its own test
    so a future refactor of CONCEPT_ANCHORS cannot accidentally drop ledger
    coverage without a test failing specifically on that fact.
    """
    doc_text = visible_doc_text(DOC_PATH.read_text(encoding="utf-8"))
    assert "docs/live-proof-coverage.md" in doc_text, (
        "the assurance/provenance section must cite the truth-gated live-proof "
        "ledger by its exact path, not merely describe live-proofing in prose"
    )


# ---------------------------------------------------------------------------
# Synthetic RED-case tests — prove the guardian is non-vacuous forever.
#
# These do not touch the filesystem (no mkdir/rmdir at test time): they call
# the same pure helper functions the real-tree tests above call, with
# synthetic inputs, so the guardian's failure modes are locked in-suite
# permanently rather than relying on today's tree happening to be wrong.
# The end-to-end collection path is cheap to re-verify by hand at any time:
# `mkdir packages/ne-drift-demo` turns test_every_shipped_module_is_documented
# red naming exactly that directory; removing it restores green.
# ---------------------------------------------------------------------------


def test_new_module_without_doc_mention_goes_red() -> None:
    real_doc_text = visible_doc_text(DOC_PATH.read_text(encoding="utf-8"))
    result = undocumented_modules(["ne-quantum-widgets"], real_doc_text, {})
    assert result == ["ne-quantum-widgets"]


def test_documented_module_passes() -> None:
    # ne-api and ne-worker are named literally in the doc's Part IV process
    # table — this is the positive control for the same code path the
    # red-case test above exercises negatively.
    real_doc_text = visible_doc_text(DOC_PATH.read_text(encoding="utf-8"))
    assert undocumented_modules(["ne-api"], real_doc_text, {}) == []
    assert undocumented_modules(["ne-worker"], real_doc_text, {}) == []


def test_substring_name_does_not_satisfy_longer_name() -> None:
    # A doc that only ever says the shorter name must NOT satisfy a check for
    # the longer, hyphen-extended name, and vice versa — this is the exact
    # collision `ne-permissions` / `ne-permissions-rebac` and `ne-brain` /
    # `ne-brain-graph` / `ne-brain-ontology` present in the real package set.
    doc_text = "This deployment ships ne-permissions for source-ACL enforcement."
    assert undocumented_modules(["ne-permissions-rebac"], doc_text, {}) == ["ne-permissions-rebac"]

    doc_text_2 = "The optional ne-permissions-rebac sharing layer is disabled by default."
    assert undocumented_modules(["ne-permissions"], doc_text_2, {}) == ["ne-permissions"]


def test_allowlist_empty_rationale_goes_red() -> None:
    modules = ["ne-envbuild"]
    errors = hygiene_errors(modules, {"ne-envbuild": "   "})
    assert any("empty/whitespace rationale" in e for e in errors)


def test_allowlist_token_rationale_goes_red() -> None:
    # A non-empty but token-length rationale ("internal tool") is not a
    # reviewable sentence — it must fail hygiene just as an empty one does.
    modules = ["ne-envbuild"]
    errors = hygiene_errors(modules, {"ne-envbuild": "internal tool"})
    assert any("too short" in e for e in errors)


def test_stale_allowlist_entry_goes_red() -> None:
    modules = ["ne-api", "ne-worker"]
    errors = hygiene_errors(modules, {"ne-does-not-exist": "a real, non-empty rationale"})
    assert any("stale allowlist entry" in e for e in errors)


def test_anchor_removal_goes_red() -> None:
    real_doc_text = visible_doc_text(DOC_PATH.read_text(encoding="utf-8"))
    assert "OpenFGA" in real_doc_text, "precondition: the real doc must mention OpenFGA today"
    mutated_text = real_doc_text.replace("OpenFGA", "")
    missing = [anchor for anchor in CONCEPT_ANCHORS if anchor not in mutated_text]
    assert "OpenFGA" in missing


def test_mention_only_inside_html_comment_does_not_count() -> None:
    # A module name (or concept anchor) hidden inside an HTML comment renders
    # to nothing in every Markdown viewer — it must not satisfy either check.
    # This is the gaming vector visible_doc_text() exists to close.
    doc_text = "Real prose.\n<!-- ne-quantum-widgets OpenFGA -->\nMore prose."
    visible = visible_doc_text(doc_text)
    assert undocumented_modules(["ne-quantum-widgets"], visible, {}) == ["ne-quantum-widgets"]
    assert "OpenFGA" not in visible
