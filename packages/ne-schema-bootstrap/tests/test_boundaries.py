# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Structural guardian for "no LLM, no network, no linkml" (design §4.1/§3.2).

AST-walks every module under the package's ``src/`` and asserts every import
resolves to the stdlib allowlist, ``pydantic``, an intra-package
``ne.schema_bootstrap.*`` import, or that MODULE'S OWN narrow per-module
allowance (:data:`_PER_MODULE_ALLOWANCES`) — nothing else, and in particular
none of the forbidden surfaces (linkml, any HTTP/network library, or a
sibling platform package) EVER, in ANY module, allowance or no allowance.
The checker is verified non-vacuous by feeding it planted forbidden imports
— both without any allowance and WITH a module's own allowance — and
confirming both are rejected.

**Two forbidden tiers, and why the split matters (2026-07-28 Argus P2).**
:data:`_HARD_FORBIDDEN` (linkml, every HTTP/network surface, sibling
platform packages ``ne.flywheel``/``ne.contracts``) is checked FIRST, inside
:func:`_is_allowed`, BEFORE any ``extra_allowed`` lookup runs — these names
can never be allowed, full stop, no matter what
:data:`_PER_MODULE_ALLOWANCES` says, today or in any future edit. ``ne.schema``
is deliberately NOT in ``_HARD_FORBIDDEN``: it is forbidden-BY-DEFAULT
(rejected for every module with no allowance) but legitimately
allowance-gated for :mod:`ne.schema_bootstrap.roundtrip` alone. Before this
split, ``_is_allowed`` consulted ``extra_allowed`` for every name
uniformly — so a hypothetical (or future-mistaken) entry like
``_PER_MODULE_ALLOWANCES["ne/schema_bootstrap/emit.py"] = frozenset({"yaml",
"linkml"})`` would have made the checker silently ACCEPT ``import linkml``
in ``emit.py``, and ``test_forbidden_surfaces_never_appear``'s scan (which
only inspects names ``_is_allowed`` already rejected) would never have seen
it — the docstring's "allowance or no allowance" claim was aspirational
prose, not a property the code enforced.
:func:`test_hard_forbidden_rejected_even_if_planted_into_an_allowance` plants
exactly that shape and proves the checker rejects it regardless.

**Per-module allowances (U2).** Two modules need imports beyond the global
stdlib+pydantic+self set: :mod:`ne.schema_bootstrap.emit` (``yaml``, to
serialize the house-dialect LinkML draft; ``unicodedata``, added 2026-07-28
for R4, because ``emit.py``'s ``_enum_decline_reason`` calls
``unicodedata.name()``, the SAME call the real generator's
``generate_enum_label`` member-name mangle makes, so the gate predicts real
generator behaviour instead of guessing; and ``keyword``, added 2026-07-28
for the R6 P0 fix, because ``emit.py``'s ``_member_label`` calls
``keyword.iskeyword()``, the SAME check the real generator's
``EnumValue.alias_python_keywords`` runs
(``packages/ne-schema/.codegen-venv/.../linkml/generators/pydanticgen/
template.py:127-131`` — masks a Python-keyword member label by appending
``_``) — so this, too, derives from the real generator's own check rather
than guessing at a hand-rolled keyword list) and
:mod:`ne.schema_bootstrap.roundtrip` (``ne.schema``/``ne.envbuild``/
``subprocess``/``sys``/``tempfile``, to subprocess the REAL isolated codegen
venv — see that module's docstring). Each allowance is scoped to EXACTLY the
one module that needs it — a module not listed in
:data:`_PER_MODULE_ALLOWANCES` gets no allowance at all, so a planted import
of ``ne.schema`` in, say, :mod:`ne.schema_bootstrap.emit` is still rejected
(``ne.schema`` is roundtrip's allowance, not emit's) — and even roundtrip's
own allowance can never be stretched to cover a ``_HARD_FORBIDDEN`` name.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import ne.schema_bootstrap as sb

_STDLIB_ALLOWLIST = {
    "csv",
    "json",
    "re",
    "random",
    "hashlib",
    "datetime",
    "pathlib",
    "dataclasses",
    "typing",
    "collections",
    "collections.abc",
    "itertools",
    "math",
    "__future__",
}
#: 2026-07-29 Argus P2 history (deliberately NOT re-added, see below): an
#: earlier revision of NE-ESB-CLI (U3) added "tempfile" HERE, globally, so
#: that workspace.py's staging-then-atomic-replace commit protocol could use
#: `tempfile.mkdtemp` while workspace.py's own module docstring kept
#: claiming "no per-module allowance at all" — a global widening made for
#: ONE module's narrow need, silently permitting `import tempfile` in EVERY
#: OTHER module's boundary check too, for no reason connected to those
#: modules. The fix removes the need for `tempfile` in workspace.py
#: entirely (the staging directory now has a FIXED name — see
#: `ne.schema_bootstrap.workspace._STAGING_DIRNAME`) rather than re-scoping
#: the allowance narrower, so `tempfile` is no longer in this set, and
#: `test_workspace_module_needs_no_per_module_allowance` below is true
#: again in the strong sense its own name claims. `roundtrip.py`'s own,
#: always-correctly-scoped per-module "tempfile" entry (below) is
#: unaffected and still genuinely used
#: (`ne.schema_bootstrap.roundtrip`'s `tempfile.TemporaryDirectory`).

#: Path (relative to the package's ``src/`` root) -> the extra module names
#: THAT module (and only that module) may import beyond the global
#: stdlib+pydantic+self allowlist. See the module docstring.
_PER_MODULE_ALLOWANCES: dict[str, frozenset[str]] = {
    # emit.py: "yaml" serializes the house-dialect LinkML draft. "unicodedata"
    # is added for R4 (2026-07-28, NE-ESB-EMIT continuation): emit.py's
    # `_enum_decline_reason` calls `unicodedata.name()` because that is THE
    # SAME call the real generator's `generate_enum_label` member-name mangle
    # makes — the gate PREDICTS real generator behaviour instead of guessing
    # at a hand-rolled control-character table (see emit.py::_enum_decline_reason
    # R4). "keyword" is added for the R6 P0 fix (2026-07-28, NE-ESB-EMIT
    # continuation 2): `_member_label` calls `keyword.iskeyword()` because
    # that is THE SAME check the real generator's `EnumValue.alias_python_
    # keywords` runs (linkml pydanticgen/template.py:127-131) to mask a
    # Python-keyword member label by appending "_" — the mirror derives,
    # never guesses. Both are stdlib (PSF-2.0): no uv.lock change, no NOTICE
    # change, no license-gate surface.
    "ne/schema_bootstrap/emit.py": frozenset({"keyword", "unicodedata", "yaml"}),
    "ne/schema_bootstrap/roundtrip.py": frozenset(
        {"ne.schema", "ne.envbuild", "subprocess", "sys", "tempfile"}
    ),
    # refine.py (NE-ESB-CLI, U3): "yaml" parses/serializes the base/ours/
    # theirs drafts for the --refine three-way merge — the same house-dialect
    # YAML emit.py already produces, so this module needs the same
    # `yaml.safe_load`/`yaml.safe_dump` surface, nothing else (no unicodedata/
    # keyword — refine.py never touches enum member-name mangling).
    "ne/schema_bootstrap/refine.py": frozenset({"yaml"}),
}

#: Surfaces that are ALWAYS rejected, checked BEFORE any per-module
#: allowance is even consulted (see the module docstring's "Two forbidden
#: tiers" section) — no entry in :data:`_PER_MODULE_ALLOWANCES`, present or
#: future, can ever cover one of these.
_HARD_FORBIDDEN = frozenset(
    {
        "linkml",
        "httpx",
        "requests",
        "urllib",
        "socket",
        "ssl",
        "aiohttp",
        "litellm",
        "ne.flywheel",
        "ne.contracts",
    }
)

#: The full set the belt-and-suspenders substring scan
#: (``test_forbidden_surfaces_never_appear``) checks for. Includes
#: ``ne.schema`` too — forbidden BY DEFAULT (not hard-forbidden: it is
#: legitimately allowance-gated for :mod:`ne.schema_bootstrap.roundtrip`
#: alone).
_FORBIDDEN_SUBSTRINGS = (*sorted(_HARD_FORBIDDEN), "ne.schema")


def _iter_imported_module_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import (`from .foo import bar`) — always
                # intra-package, allowed unconditionally.
                names.append(f"<relative level={node.level}>{node.module or ''}")
            elif node.module:
                names.append(node.module)
    return names


def _is_hard_forbidden(module_name: str) -> bool:
    """``True`` iff ``module_name`` is in :data:`_HARD_FORBIDDEN` — checked

    UNCONDITIONALLY, with no ``extra_allowed`` parameter at all, so there is
    no code path by which any allowance can short-circuit it (see the module
    docstring).
    """
    top = module_name.split(".")[0]
    for forbidden in _HARD_FORBIDDEN:
        if module_name == forbidden or module_name.startswith(forbidden + ".") or top == forbidden:
            return True
    return False


def _is_allowed(module_name: str, *, extra_allowed: frozenset[str] = frozenset()) -> bool:
    if module_name.startswith("<relative"):
        return True
    # HARD_FORBIDDEN is checked FIRST and returns immediately — no allowance
    # lookup below can ever run for one of these names. This is what makes
    # `test_hard_forbidden_rejected_even_if_planted_into_an_allowance` pass
    # even when `extra_allowed` itself contains a forbidden name.
    if _is_hard_forbidden(module_name):
        return False
    if module_name == "pydantic" or module_name.startswith("pydantic."):
        return True
    if module_name == "ne.schema_bootstrap" or module_name.startswith("ne.schema_bootstrap."):
        return True
    top = module_name.split(".")[0]
    if module_name in _STDLIB_ALLOWLIST or top in _STDLIB_ALLOWLIST:
        return True
    for allowed in extra_allowed:
        if module_name == allowed or module_name.startswith(allowed + "."):
            return True
    return False


def _check_source(source: str, *, extra_allowed: frozenset[str] = frozenset()) -> list[str]:
    """Return the list of DISALLOWED imported module names found in ``source``.

    ``extra_allowed`` is the CALLING module's own narrow allowance (see
    :data:`_PER_MODULE_ALLOWANCES`) — it is never applied globally, only to
    the one source string being checked, so an allowance granted to one
    module can never leak into another module's check (proven by
    ``test_per_module_allowance_is_scoped_not_global`` below).
    """
    tree = ast.parse(source)
    violations = []
    for name in _iter_imported_module_names(tree):
        if _is_allowed(name, extra_allowed=extra_allowed):
            # An ALLOWED name (self-package, stdlib, pydantic, or this
            # module's own narrow allowance) can never be a forbidden
            # surface, even if it happens to contain a forbidden substring
            # incidentally — e.g. "ne.schema" is a substring of the allowed
            # "ne.schema_bootstrap"/"ne.schema_bootstrap.model". Skip it from
            # the substring check entirely rather than let string overlap
            # misfire on the package's own name (2026-07-28 Argus P3).
            continue
        if name not in violations:
            violations.append(name)
    return violations


def _package_src_root() -> Path:
    # ne/schema_bootstrap/__init__.py -> .../src/ne/schema_bootstrap -> .../src
    return Path(sb.__file__).resolve().parent.parent.parent


def _extra_allowed_for(py_file: Path, src_root: Path) -> frozenset[str]:
    return _PER_MODULE_ALLOWANCES.get(str(py_file.relative_to(src_root)), frozenset())


def test_every_module_imports_only_stdlib_pydantic_self_or_its_own_allowance() -> None:
    src_root = _package_src_root()
    py_files = sorted(src_root.rglob("*.py"))
    assert py_files, "expected to find package source files to check"

    all_violations: dict[str, list[str]] = {}
    for py_file in py_files:
        extra_allowed = _extra_allowed_for(py_file, src_root)
        violations = _check_source(py_file.read_text(encoding="utf-8"), extra_allowed=extra_allowed)
        if violations:
            all_violations[str(py_file.relative_to(src_root))] = violations

    assert not all_violations, f"forbidden imports found: {all_violations}"


def test_forbidden_surfaces_never_appear() -> None:
    # Belt-and-suspenders on top of the allowlist check above (which would
    # already reject e.g. "requests" as not-allowlisted): proves the
    # SPECIFIC named forbidden surfaces are absent from every import in the
    # package, by name, across every module — using each file's OWN
    # per-module allowance (never a different file's), so e.g.
    # roundtrip.py's legitimate "ne.schema" import is excluded from this
    # substring scan while linkml/httpx/etc. (never anyone's allowance) stay
    # caught EVERYWHERE, no carve-out, no exceptions.
    src_root = _package_src_root()
    joined_imports: list[str] = []
    for py_file in sorted(src_root.rglob("*.py")):
        extra_allowed = _extra_allowed_for(py_file, src_root)
        for name in _iter_imported_module_names(ast.parse(py_file.read_text())):
            if not _is_allowed(name, extra_allowed=extra_allowed):
                joined_imports.append(name)
    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert not any(forbidden in name for name in joined_imports), (
            f"forbidden surface {forbidden!r} imported somewhere in the package"
        )


def test_checker_rejects_a_planted_forbidden_import() -> None:
    # Control: the checker itself must not be vacuous — feed it a forbidden
    # import and confirm it is caught.
    planted_source = "import linkml_runtime\n\ndef f():\n    return 1\n"
    violations = _check_source(planted_source)
    assert violations, "checker failed to flag a planted `import linkml_runtime`"
    assert any("linkml" in v for v in violations)


def test_checker_rejects_planted_relative_look_alike_but_real_forbidden_absolute() -> None:
    planted_source = "import httpx\nimport ne.flywheel.hygiene\n"
    violations = _check_source(planted_source)
    assert "httpx" in violations
    assert "ne.flywheel.hygiene" in violations


def test_checker_allows_absolute_self_import_despite_ne_schema_substring() -> None:
    # 2026-07-28 Argus P3: "ne.schema" is a forbidden substring (guarding
    # against importing the sibling `ne-schema` package), but it is ALSO a
    # substring of this package's OWN name, "ne.schema_bootstrap". An
    # absolute self-import (the package currently only uses relative
    # imports, but nothing stops a future module from writing one) would be
    # misflagged as importing the forbidden `ne.schema` surface. The checker
    # must allow it.
    planted_source = "import ne.schema_bootstrap.model\nfrom ne.schema_bootstrap.config import X\n"
    violations = _check_source(planted_source)
    assert violations == [], f"self-import falsely flagged as forbidden: {violations}"

    # Control: a genuine `ne.schema` import (the sibling package, not this
    # one) must still be caught when the calling module has NO allowance for
    # it — proves the carve-out isn't vacuous.
    real_violation = _check_source("import ne.schema\n")
    assert "ne.schema" in real_violation


def test_emit_allowance_permits_yaml_unicodedata_and_keyword_but_nothing_else() -> None:
    extra = _PER_MODULE_ALLOWANCES["ne/schema_bootstrap/emit.py"]
    assert _check_source("import yaml\n", extra_allowed=extra) == []
    assert _check_source("import unicodedata\n", extra_allowed=extra) == []
    assert _check_source("import keyword\n", extra_allowed=extra) == []
    # Control: emit.py's allowance does NOT grant a free pass to a different
    # forbidden module — the allowance is exactly {"keyword", "unicodedata",
    # "yaml"}, not a wildcard.
    violations = _check_source("import socket\n", extra_allowed=extra)
    assert "socket" in violations


def test_yaml_import_rejected_without_emit_allowance() -> None:
    # Bidirectional control: `import yaml` is REJECTED for every module
    # EXCEPT emit.py (i.e. with no allowance passed at all).
    violations = _check_source("import yaml\n")
    assert "yaml" in violations


def test_unicodedata_import_rejected_without_emit_allowance() -> None:
    # Bidirectional control: `import unicodedata` is REJECTED for every
    # module EXCEPT emit.py (i.e. with no allowance passed at all) — proves
    # the allowance is per-module, not global. Mutation B (transiently moving
    # "unicodedata" into `_STDLIB_ALLOWLIST` instead of the per-module dict)
    # turns this test RED, pinning the narrow placement.
    violations = _check_source("import unicodedata\n")
    assert "unicodedata" in violations


def test_keyword_import_rejected_without_emit_allowance() -> None:
    # Bidirectional control (same shape as the unicodedata control above):
    # `import keyword` is REJECTED for every module EXCEPT emit.py (i.e.
    # with no allowance passed at all) — proves the "keyword" allowance is
    # per-module, not global. Row 49 Mutation I(B) (transiently moving
    # "keyword" into `_STDLIB_ALLOWLIST` instead of the per-module dict)
    # turns this test RED, pinning the narrow placement.
    violations = _check_source("import keyword\n")
    assert "keyword" in violations


def test_roundtrip_allowance_permits_its_own_imports_but_never_linkml() -> None:
    extra = _PER_MODULE_ALLOWANCES["ne/schema_bootstrap/roundtrip.py"]
    source = (
        "import subprocess\nimport sys\nimport tempfile\n"
        "from ne.schema import codegen\nfrom ne.envbuild import venv_python\n"
    )
    assert _check_source(source, extra_allowed=extra) == []

    # Control: linkml stays rejected EVEN WITH roundtrip.py's own allowance —
    # no carve-out ever covers the truly forbidden surfaces.
    violations = _check_source("import linkml\n", extra_allowed=extra)
    assert "linkml" in violations


def test_hard_forbidden_rejected_even_if_planted_into_an_allowance() -> None:
    # 2026-07-28 Argus P2: proves the structural property the module
    # docstring claims — no allowance, however constructed (including a
    # HYPOTHETICAL future entry in `_PER_MODULE_ALLOWANCES` that mistakenly
    # widens a module's allowance), can ever cover a `_HARD_FORBIDDEN`
    # surface. Before the hard/soft split, `_is_allowed` consulted
    # `extra_allowed` uniformly for every name, so this exact `extra_allowed`
    # would have made `_check_source` return `[]` for `import linkml` —
    # this test is the guard against that regression.
    poisoned_allowance = frozenset({"yaml", "linkml"})
    violations = _check_source(
        "import linkml\nimport linkml.generators\n", extra_allowed=poisoned_allowance
    )
    assert "linkml" in violations
    assert "linkml.generators" in violations

    # Every other hard-forbidden surface, individually poisoned into its own
    # allowance, is rejected too — not just linkml.
    for forbidden in sorted(_HARD_FORBIDDEN):
        poisoned = frozenset({forbidden})
        violations = _check_source(f"import {forbidden}\n", extra_allowed=poisoned)
        assert forbidden in violations, (
            f"{forbidden!r} was accepted despite being planted into its own allowance "
            f"(_HARD_FORBIDDEN split is not structural)"
        )


def test_ne_schema_import_rejected_without_roundtrip_allowance() -> None:
    # Bidirectional control: `from ne.schema import codegen` is REJECTED for
    # every module EXCEPT roundtrip.py (i.e. with no allowance passed).
    violations = _check_source("from ne.schema import codegen\n")
    assert "ne.schema" in violations


def test_per_module_allowance_is_scoped_not_global() -> None:
    # roundtrip.py's "ne.schema" allowance must never leak into a DIFFERENT
    # module's check — e.g. emit.py's own (yaml-only) allowance must still
    # reject "ne.schema", proving the driving tests' per-file
    # `_extra_allowed_for` lookup is genuinely scoped, not a shared global.
    emit_allowed = _PER_MODULE_ALLOWANCES["ne/schema_bootstrap/emit.py"]
    violations = _check_source("from ne.schema import codegen\n", extra_allowed=emit_allowed)
    assert "ne.schema" in violations


#: The full expected `ne.schema_bootstrap.__all__` contents, hand-maintained
#: HERE (not derived from `sb.__all__` itself, which would make this a
#: tautology) — this is the parity ratchet that closes U2's residual #2:
#: `DuplicateRecordSetSlotNameError` was raised by `emit_linkml` since
#: NE-ESB-EMIT but was never added to `__all__`/the README, so
#: `from ne.schema_bootstrap import DuplicateRecordSetSlotNameError` failed,
#: undetected because the OLD `test_public_api_all_importable` was a hand-
#: maintained import list with no assertion that it was COMPLETE (see
#: `test_public_api_all_matches_expected_list` below, and mutation m4 in the
#: PR body: removing an entry from `sb.__all__` now turns that test RED).
#:
#: DELIBERATELY HAND-MAINTAINED — do not "fix" this friction (2026-07-29,
#: NE-ESB-CLI continuation). If this list just failed because you added an
#: export, the ratchet is WORKING: a public-API change must be written twice,
#: once in ``__all__`` and once here, so the package boundary only grows by
#: explicit decision, never as a side effect of an implementation edit.
#: Deriving this list from ``sb.__all__`` would collapse the test into
#: ``sorted(sb.__all__) == sorted(sb.__all__)`` — a vacuous guard (see
#: docs/testing/vacuous-guards.md). Deriving the error subset from the
#: ``errors`` module was considered and rejected too: the same two-file edit
#: (errors.py + __init__.py) that adds an export would then auto-satisfy the
#: check, deleting the third, test-side touchpoint this ratchet exists to
#: force. The right response to a red run here is a decision, then one
#: sorted insertion below.
_EXPECTED_PUBLIC_API = sorted(
    [
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
)


def _names_imported_from_schema_bootstrap(func: object) -> set[str]:
    """AST-self-scan ``func``'s own source for every name pulled in by a
    ``from ne.schema_bootstrap import (...)`` statement.

    Used to pin :func:`test_public_api_all_importable`'s import list against
    :data:`_EXPECTED_PUBLIC_API` (2026-07-30, NE-ESB-CLI continuation, Argus
    P2). Without this, the import list is a SECOND hand-maintained public-API
    list sitting right next to a docstring (above, on ``_EXPECTED_PUBLIC_API``)
    that explains why the first one is deliberately hand-maintained and warns
    about exactly this drift shape — a list nobody re-checks for completeness.
    """
    source = textwrap.dedent(inspect.getsource(func))  # type: ignore[arg-type]
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "ne.schema_bootstrap":
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def test_public_api_all_importable() -> None:
    from ne.schema_bootstrap import (
        DEFAULT_CONFIG,
        BootstrapConfig,
        BootstrapRunResult,
        CandidateClass,
        CandidateEdge,
        CandidateEnum,
        CandidateModel,
        CandidateSlot,
        ColumnProfile,
        DraftGenerationError,
        DuplicateClassNameError,
        DuplicateRecordSetSlotNameError,
        DuplicateSlotNameError,
        EmittedSchema,
        EmptySampleError,
        InvalidClassNameError,
        InvalidEngagementNameError,
        MentionCompletenessError,
        MergeResult,
        ProfileBundle,
        RefineError,
        RefineLanding,
        RoundTripArtifacts,
        SampleFormatError,
        SampleProfile,
        SchemaBootstrapError,
        TableProfile,
        WorkspaceExistsError,
        WorkspaceIOError,
        emit_linkml,
        expected_anchors,
        infer_candidate_model,
        orphaned_classes,
        profile_samples,
        render_inference_report,
        render_pr_body,
        run_bootstrap,
        scrub_text,
        three_way_merge,
        validate_draft_generates,
        verify_mention_completeness,
    )

    assert BootstrapConfig is not None
    assert DEFAULT_CONFIG is not None
    assert BootstrapRunResult is not None
    assert CandidateClass is not None
    assert CandidateEdge is not None
    assert CandidateEnum is not None
    assert CandidateModel is not None
    assert CandidateSlot is not None
    assert ColumnProfile is not None
    assert DraftGenerationError is not None
    assert DuplicateClassNameError is not None
    assert DuplicateRecordSetSlotNameError is not None
    assert DuplicateSlotNameError is not None
    assert EmittedSchema is not None
    assert EmptySampleError is not None
    assert InvalidClassNameError is not None
    assert InvalidEngagementNameError is not None
    assert MentionCompletenessError is not None
    assert MergeResult is not None
    assert ProfileBundle is not None
    assert RefineError is not None
    assert RefineLanding is not None
    assert RoundTripArtifacts is not None
    assert SampleFormatError is not None
    assert SampleProfile is not None
    assert SchemaBootstrapError is not None
    assert TableProfile is not None
    assert WorkspaceExistsError is not None
    assert WorkspaceIOError is not None
    assert callable(emit_linkml)
    assert callable(expected_anchors)
    assert callable(infer_candidate_model)
    assert callable(orphaned_classes)
    assert callable(profile_samples)
    assert callable(render_inference_report)
    assert callable(render_pr_body)
    assert callable(run_bootstrap)
    assert callable(scrub_text)
    assert callable(three_way_merge)
    assert callable(validate_draft_generates)
    assert callable(verify_mention_completeness)

    # Completeness guard (2026-07-30, NE-ESB-CLI continuation, Argus P2):
    # the `from ne.schema_bootstrap import (...)` list above is ALSO a
    # hand-maintained public-API list — the exact trap
    # `_EXPECTED_PUBLIC_API`'s docstring warns about, sitting one function
    # away. Self-scanning this test's own source (rather than deriving from
    # `sb.__all__`, which would make this tautological) pins the import list
    # to `_EXPECTED_PUBLIC_API` so the two hand lists cannot silently drift
    # apart again the way U2's residual #2 did. A name added to `__all__`
    # without being added here now fails HERE, not just at
    # `test_public_api_all_matches_expected_list` below (which only proves
    # `getattr` succeeds, not that this test's own explicit-import surface
    # stays exhaustive).
    imported = _names_imported_from_schema_bootstrap(test_public_api_all_importable)
    expected = set(_EXPECTED_PUBLIC_API)
    assert imported == expected, (
        "test_public_api_all_importable's import list drifted from "
        f"_EXPECTED_PUBLIC_API: missing={sorted(expected - imported)!r} "
        f"extra={sorted(imported - expected)!r}"
    )


def test_public_api_all_matches_expected_list() -> None:
    """The __all__-parity ratchet (2026-07-29, NE-ESB-CLI U3): asserts
    ``sb.__all__`` equals the hand-maintained :data:`_EXPECTED_PUBLIC_API`
    list AND that every name in it actually resolves via ``getattr`` — the
    fix for why U2's residual #2 (``DuplicateRecordSetSlotNameError`` absent
    from ``__all__``) went undetected: the old test above imported a fixed
    list of names without ever checking that list was exhaustive against
    ``__all__``. Mutation m4 (removing an entry from ``sb.__all__`` without
    updating this list, or vice versa) turns this RED.
    """
    assert sorted(sb.__all__) == _EXPECTED_PUBLIC_API
    for name in sb.__all__:
        assert getattr(sb, name) is not None, f"{name!r} in __all__ but getattr failed"


def test_refine_allowance_permits_yaml_but_nothing_else() -> None:
    extra = _PER_MODULE_ALLOWANCES["ne/schema_bootstrap/refine.py"]
    assert _check_source("import yaml\n", extra_allowed=extra) == []

    # Control (mirrors emit.py's own socket control above): refine.py's
    # allowance does NOT grant a free pass to a different forbidden module —
    # the allowance is exactly {"yaml"}, not a wildcard.
    violations = _check_source("import socket\n", extra_allowed=extra)
    assert "socket" in violations

    # Bidirectional control: `import yaml` is REJECTED for every module
    # EXCEPT emit.py/refine.py (i.e. with no allowance passed at all).
    assert "yaml" in _check_source("import yaml\n")


def test_workspace_module_needs_no_per_module_allowance() -> None:
    # workspace.py's own module docstring claims "stdlib + intra-package
    # relative imports only, no allowance". Proven, not just asserted: the
    # module has no entry in `_PER_MODULE_ALLOWANCES` at all, and the driving
    # scan (`test_every_module_imports_only_stdlib_pydantic_self_or_its_own_
    # allowance`) already runs it with an EMPTY allowance (the `.get(...,
    # frozenset())` default in `_extra_allowed_for`) and finds zero
    # violations — if that were false this module's assertion would be
    # meaningless, so check both.
    assert "ne/schema_bootstrap/workspace.py" not in _PER_MODULE_ALLOWANCES

    src_root = _package_src_root()
    workspace_file = src_root / "ne" / "schema_bootstrap" / "workspace.py"
    assert workspace_file.is_file()
    violations = _check_source(workspace_file.read_text(encoding="utf-8"))
    assert violations == [], f"workspace.py needs an allowance it should not: {violations}"

    # Bidirectional control (2026-07-29 Argus P2 fix, replacing an earlier
    # revision's GLOBAL `_STDLIB_ALLOWLIST` entry for "tempfile"): `import
    # tempfile` is REJECTED with no allowance passed — proving `tempfile` is
    # genuinely absent from the module-wide allowlist now, not merely absent
    # from `_PER_MODULE_ALLOWANCES`'s keys (a global-allowlist regression
    # would pass the assertion above unchanged while still silently
    # widening every module's permitted surface).
    assert "tempfile" in _check_source("import tempfile\n")


def test_profile_bundle_has_no_model_dump_non_serializable_by_construction() -> None:
    from ne.schema_bootstrap.profiles import ProfileBundle, SampleProfile

    bundle = ProfileBundle(
        profile=SampleProfile(tables=[]), column_values={}, fk_target_membership={}
    )

    assert not hasattr(bundle, "model_dump")
    assert not hasattr(bundle, "model_dump_json")
