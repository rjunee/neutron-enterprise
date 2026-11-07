# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The operator workspace: ``run_bootstrap`` drives U1's profiler + U2's
emitter/round-trip/renderers end to end into a reviewable directory
(NE-ESB-CLI, U3).

**Validate BEFORE you write (the ordering this module exists to prove).**
Every round-trip validation this invocation performs —
:func:`ne.schema_bootstrap.roundtrip.validate_draft_generates` — succeeds
BEFORE any of the five run artifacts (``schema-draft.machine.yaml``,
``schema-draft.yaml``, ``inference-report.md``, ``pr-body.md``,
``tombstones.json``) is written. The commit itself stages every file in a
FIXED-name directory (``_STAGING_DIRNAME``, gitignored — see below) inside
``workspace_dir`` (same filesystem) and lands each one with ``Path.replace``
(POSIX ``rename(2)``/``os.replace`` — atomic PER FILE, not as a set: see
``_write_staged_and_commit``'s own docstring for the precise, narrower
claim). See ``tests/test_workspace.py::
test_generation_failure_leaves_no_partial_artifacts`` (fresh path) and
``::test_refine_failure_leaves_workspace_byte_identical`` (refine path) for
the forced-failure proofs, each paired with a passing control.

**The ``.gitignore`` guard is written BEFORE profiling starts, deliberately
(design §3.3's "samples never enter git" guard must not wait on a
successful run).** A fresh invocation creates the workspace directory and
its ``.gitignore`` (``samples/`` and the staging directory ignored) first,
then profiles/infers/emits/validates, then commits the five run artifacts.
If anything after the guard fails, the workspace directory and its
``.gitignore`` are the ONLY things that exist afterwards — see
``tests/test_workspace.py::test_fresh_run_errors_write_nothing_when_workspace_absent``.

This module is deliberately STDLIB + INTRA-PACKAGE-RELATIVE IMPORTS ONLY —
no ``ne.schema`` (the real generator's package), reached only indirectly via
:mod:`ne.schema_bootstrap.roundtrip`'s own narrow, already-audited
allowance — structurally enforced by ``tests/test_boundaries.py`` (this
module needs, and gets, no entry in ``_PER_MODULE_ALLOWANCES`` at all — see
``test_workspace_module_needs_no_per_module_allowance``). **This module used
to import ``tempfile`` for ``tempfile.mkdtemp(dir=workspace_dir)`` and an
earlier revision widened the GLOBAL ``_STDLIB_ALLOWLIST`` to cover it
(2026-07-29 Argus P2 finding: a global widening for one module's narrow
need, undetected because a per-module allowance would have contradicted
this docstring's own "no allowance" claim).** The fix removes the need
entirely rather than choosing between a wrong-scoped global allowance and a
docstring rewrite: the staging directory now has a FIXED name
(``_STAGING_DIRNAME``, gitignored — see below, 2026-07-29 Argus P3, which
independently wanted a fixed name so a crash mid-commit cannot leave an
un-gitignored random-named directory in the workspace), so ``tempfile`` is
no longer imported anywhere in this module and the global allowlist no
longer carries the entry either. ``validate_draft_generates`` is imported
here at MODULE level (a library module, not the lazy-importing CLI — see
``tools/ne-cli/src/ne/cli/schema.py``'s docstring for that discipline)
specifically so tests can
``monkeypatch.setattr(ne.schema_bootstrap.workspace, "validate_draft_generates", ...)``
to force a generation failure without touching the real codegen venv.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .config import DEFAULT_CONFIG, BootstrapConfig
from .emit import EmittedSchema, _validate_engagement, emit_linkml
from .errors import EmptySampleError, RefineError, WorkspaceExistsError, WorkspaceIOError
from .infer import infer_candidate_model
from .prbody import render_pr_body
from .profiles import profile_samples
from .refine import (
    MergeResult,
    find_tree_root_class,
    merged_expected_names,
    parse_merged_yaml,
)
from .refine import (
    orphaned_classes as _orphaned_classes,
)
from .refine import three_way_merge as _three_way_merge
from .report import RefineLanding, render_inference_report
from .roundtrip import validate_draft_generates

MACHINE_DRAFT = "schema-draft.machine.yaml"
DRAFT = "schema-draft.yaml"
REPORT = "inference-report.md"
PR_BODY = "pr-body.md"
TOMBSTONES = "tombstones.json"

#: The FIXED name of the commit-staging directory created (and removed)
#: inside ``workspace_dir`` during ``_write_staged_and_commit`` (2026-07-29
#: Argus P3, replacing an earlier ``tempfile.mkdtemp``-random name): a
#: SIGKILL mid-commit can leave this directory behind, and a fixed,
#: gitignored name means it can never accidentally be committed into the
#: engagement PR the way a random ``tmpXXXXXX`` name — absent from
#: ``.gitignore`` — could have been.
#:
#: **Single-writer assumption, stated (2026-07-29 Argus P3, round 2).** A
#: fixed name means ``_write_staged_and_commit``'s ``_clear_staging(staging)``
#: call — which unconditionally removes anything already at
#: ``_STAGING_DIRNAME`` before (re)creating it, to recover from a prior
#: crashed commit — can also delete a DIFFERENT, still-in-flight
#: ``run_bootstrap`` call's staged files if two invocations ever race on the
#: SAME ``workspace_dir`` concurrently; the crash-recovery property and the
#: concurrent-safety property trade off against each other under one fixed
#: name, and this module picks crash-recovery + a gitignored name deliberately
#: (a random per-run name would restore concurrent-safety but reintroduce the
#: un-gitignored-litter hazard above). ``ne schema bootstrap`` is a
#: single-operator discovery CLI (design §4.4) — no caller in this codebase
#: runs two ``run_bootstrap`` invocations against one workspace at once — so
#: this is accepted, not fixed, here; a future multi-writer caller would need
#: an ``O_EXCL`` lock file in ``workspace_dir``, not a change to this name.
_STAGING_DIRNAME = ".bootstrap-staging"

#: The samples directory a fresh run reads from, and the commit-staging
#: directory, are never meant to enter git — written into the workspace's
#: ``.gitignore`` BEFORE profiling starts (see the module docstring's
#: guard-first paragraph). Also the canonical set ``_ensure_gitignore``
#: REPAIRS a stale file against (2026-07-29 round 4, Argus P3-2) — kept as
#: one tuple, not duplicated between the canonical content and the repair
#: list, so the two can never drift apart.
_REQUIRED_GITIGNORE_ENTRIES: tuple[str, ...] = ("samples/", f"{_STAGING_DIRNAME}/")
GITIGNORE = "".join(f"{entry}\n" for entry in _REQUIRED_GITIGNORE_ENTRIES)

_RUN_ARTIFACTS = (MACHINE_DRAFT, DRAFT, REPORT, PR_BODY, TOMBSTONES)


class BootstrapRunResult(BaseModel):
    """Summary of one ``run_bootstrap`` invocation, for the CLI to report.

    ``table_count`` is the number of SOURCE TABLES this invocation profiled
    (``len(bundle.profile.tables)``) — deliberately NOT the same expression
    as ``class_count`` (2026-07-29 Argus P3: an earlier revision computed
    both from ``len(model.classes)``, so the two figures could never
    disagree by construction even though they measure different things; a
    future inference change that drops a table without a corresponding
    class would have been invisible). ``class_count``/``enum_count``/
    ``edge_count`` describe THIS INVOCATION'S machine inference run (the
    profiler/inferencer output over the samples just read) — on
    ``--refine`` that is the NEW machine draft, not the landing merged draft
    (whose own class/enum accounting is already fully available on
    ``merge`` via ``human_kept``/``machine_updated``/
    ``blocked_resurrections``/``human_readded``, plus
    ``orphaned_classes`` below). Stated explicitly here rather than left
    ambiguous.

    ``orphaned_classes`` (2026-07-29 Argus P1): every non-abstract class in
    the LANDING draft (the fresh draft on a first run; the merged draft on
    ``--refine``) that :func:`ne.schema_bootstrap.refine.orphaned_classes`
    found unreachable from the tree-root container. Always ``[]`` on a
    fresh run — ``emit_linkml`` wires every one of ``model.classes`` into
    the container's ``attributes`` unconditionally, so nothing can be
    orphaned by construction; see
    ``tests/test_workspace.py::test_fresh_run_never_orphans_classes`` for
    the test that actually checks this rather than assuming it. On
    ``--refine`` a human-frozen container (see the field-level docstring on
    ``ne.schema_bootstrap.refine.orphaned_classes``) can leave a
    later-refine's new machine class unreachable — see
    ``tests/test_workspace.py::test_refine_container_freeze_orphans_a_later_machine_class``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: str
    refined: bool
    files_written: list[str]
    table_count: int
    class_count: int
    enum_count: int
    edge_count: int
    merge: MergeResult | None
    orphaned_classes: list[str]


def _empty_tombstones_json() -> str:
    return json.dumps({"classes": [], "enums": []}, indent=2, sort_keys=True) + "\n"


def _tombstones_json(classes: list[str], enums: list[str]) -> str:
    return (
        json.dumps({"classes": sorted(classes), "enums": sorted(enums)}, indent=2, sort_keys=True)
        + "\n"
    )


def _io_detail(exc: OSError | UnicodeDecodeError) -> str:
    """Render an ``OSError``/``UnicodeDecodeError`` down to a generic,
    non-content-bearing detail string (2026-07-29, NE-ESB-CLI REVISE 5):
    the OS's own ``strerror`` (e.g. "Permission denied"), or the exception's
    type name when ``strerror`` is unset, for an ``OSError``; ``reason`` at
    ``byte position N`` for a ``UnicodeDecodeError`` — deliberately NEVER
    ``exc.object`` (the raw undecodable buffer/filename bytes), the same
    disclosure argument ``errors.py``'s module docstring and
    :func:`~ne.schema_bootstrap.refine._position_only_message` make for
    every other error surface in this package. See
    ``tests/test_workspace.py::test_state_file_not_utf8_raises_refine_error_naming_path``
    (the sentinel-absence check that would fail if this ever touched
    ``exc.object``).
    """
    if isinstance(exc, UnicodeDecodeError):
        return f"not valid UTF-8: {exc.reason} at byte position {exc.start}"
    return exc.strerror or type(exc).__name__


def _clear_staging(staging: Path) -> None:
    """Clear the FIXED-name staging directory, REFUSING (never recursively
    deleting) any shape this module did not itself write (2026-07-29,
    NE-ESB-CLI REVISE 5 — closing Argus's live finding one layer deeper than
    the reported instance).

    The old claim here — "flat, this module never nests directories" — was
    true of this module's OWN writes, but ``_STAGING_DIRNAME`` is a FIXED,
    operator-visible path (see the module-level constant's docstring), and
    this function runs on the RECOVERY path: a prior crashed commit, or an
    operator poking around the workspace, can leave something at that name
    this module never put there. The unconditional ``leftover.unlink()`` this
    replaces raised a raw ``IsADirectoryError``/``PermissionError`` for any
    such shape, escaping the reviewed ``SchemaBootstrapError`` surface.

    Shapes handled, each naming the offending path via
    :class:`~ne.schema_bootstrap.errors.WorkspaceIOError`, never silently:

    * ``staging`` exists but is not a directory (e.g. a plain file squatting
      the name) -> refused, naming ``staging``.
    * ``staging`` cannot be listed (a permission failure) -> refused, naming
      ``staging``.
    * An entry inside ``staging`` is itself a directory (and not a symlink)
      -> REFUSED, not recursively deleted — this module deletes only the
      flat plain files it itself wrote; a subdirectory is by definition
      something else's, and destroying it silently would be the wrong
      failure mode for a crash-recovery routine. Naming the subdirectory.
    * A file entry's ``unlink()``, or the now-empty directory's ``rmdir()``,
      raises ``OSError`` (permission, race) -> refused, naming that path.

    The crash-recovery PROPERTY this function exists for is preserved, not
    traded away: a plain-file leftover from a crashed commit is still
    silently cleared and the run proceeds — only a FOREIGN shape (a
    subdirectory, a non-directory at the name) blocks. See
    ``tests/test_workspace.py::test_clear_staging_refuses_subdirectory_with_typed_error``
    (refusal + control: plain-file leftovers still cleared, run succeeds)
    and ``::test_staging_name_occupied_by_file_raises_typed_error``.

    Stdlib ``pathlib`` only — no ``shutil``/``os`` import needed, keeping
    this module's allowance-free stdlib posture (see the module docstring).
    """
    if staging.exists() and not staging.is_dir():
        raise WorkspaceIOError(
            str(staging), "exists but is not a directory; remove it by hand and rerun"
        )
    if not staging.is_dir():
        return
    try:
        entries = list(staging.iterdir())
    except OSError as exc:
        raise WorkspaceIOError(
            str(staging), f"cannot read staging directory: {_io_detail(exc)}"
        ) from exc
    for leftover in entries:
        if leftover.is_dir() and not leftover.is_symlink():
            raise WorkspaceIOError(
                str(leftover),
                "staging directory contains a subdirectory this tool never creates; "
                "remove it by hand and rerun",
            )
        try:
            leftover.unlink()
        except OSError as exc:
            raise WorkspaceIOError(
                str(leftover), f"cannot remove staged file: {_io_detail(exc)}"
            ) from exc
    try:
        staging.rmdir()
    except OSError as exc:
        raise WorkspaceIOError(
            str(staging), f"cannot remove staging directory: {_io_detail(exc)}"
        ) from exc


def _write_staged_and_commit(workspace_dir: Path, files: dict[str, str]) -> None:
    """Write every ``files`` entry (relative filename -> text) into a
    FIXED-name staging directory (``_STAGING_DIRNAME``, gitignored) INSIDE
    ``workspace_dir`` (same filesystem), then land each one with
    ``Path.replace`` (POSIX ``rename``/``os.replace`` under the hood — atomic
    PER FILE). Only after every file is staged does any ``replace`` run, so
    a write failure partway through staging never lands a partial set — but
    a crash partway through the ``replace`` LOOP below (e.g. a SIGKILL
    between two individual ``replace`` calls) can leave a MIXED set: some
    files landed at their new content, others still at whatever was there
    before this call (or absent, on a fresh run). This function does not
    claim set-level atomicity — only the narrower, true claim above; see
    ``tests/test_workspace.py::test_generation_failure_leaves_no_partial_artifacts``
    and ``::test_refine_failure_leaves_workspace_byte_identical`` for what
    IS proven (a failure BEFORE this function runs at all leaves nothing
    partial, because validation always precedes it).

    A leftover ``_STAGING_DIRNAME`` from a PRIOR crashed commit (this
    function's own ``finally`` block did not get to run) is cleared before
    reuse, then the directory is recreated fresh for this call.

    **Every filesystem call below is wrapped into a typed
    :class:`~ne.schema_bootstrap.errors.WorkspaceIOError`, naming the
    offending path (2026-07-29, NE-ESB-CLI REVISE 5).** In particular
    ``staged_path.replace(dest)`` covers the case where an operator has left
    a DIRECTORY at an artifact's landing name (e.g. an
    ``inference-report.md/`` directory) — POSIX ``rename(2)`` raises
    ``IsADirectoryError``/``OSError`` for that, previously an unhandled
    traceback deep inside the one function this module's whole docstring
    says validates before it writes. See
    ``tests/test_workspace.py::test_commit_destination_directory_raises_typed_error``.
    **The mixed-set caveat above is UNCHANGED by this wrapping** — a typed
    ``WorkspaceIOError`` raised partway through the ``replace`` loop leaves
    exactly the same per-file (not set-level) state a raw ``OSError`` would
    have; only the ERROR TYPE at the boundary changed, not the atomicity
    this function has always honestly claimed.
    """
    staging = workspace_dir / _STAGING_DIRNAME
    _clear_staging(staging)
    try:
        staging.mkdir()
    except OSError as exc:
        raise WorkspaceIOError(
            str(staging), f"cannot create staging directory: {_io_detail(exc)}"
        ) from exc
    try:
        staged_paths: dict[str, Path] = {}
        for name, text in files.items():
            staged_path = staging / name
            try:
                staged_path.write_text(text, encoding="utf-8")
            except OSError as exc:
                raise WorkspaceIOError(
                    str(staged_path), f"cannot stage file: {_io_detail(exc)}"
                ) from exc
            staged_paths[name] = staged_path
        for name, staged_path in staged_paths.items():
            dest = workspace_dir / name
            try:
                staged_path.replace(dest)
            except OSError as exc:
                raise WorkspaceIOError(
                    str(dest), f"cannot land staged file: {_io_detail(exc)}"
                ) from exc
    finally:
        # A successful run has already moved every staged file out, leaving
        # `staging` empty; a write failure partway through the loop above
        # can leave some files still sitting in `staging`, so clean those up.
        _clear_staging(staging)


def _ensure_gitignore(workspace_dir: Path) -> None:
    """Create ``workspace_dir/.gitignore`` if absent, or REPAIR it if it is
    STALE — CONTENT-AWARE, not just absent-vs-present (2026-07-29 round 4,
    Argus P3-2). An absent file gets the canonical :data:`GITIGNORE` text. A
    present file missing either required entry (e.g. one written by an older
    revision before the staging-directory entry existed, or one a human
    hand-edited) gets exactly the MISSING entries appended — every existing
    line, including any human-added ones, is preserved verbatim and in
    order; nothing is rewritten or reordered. A file that already carries
    both required entries is left completely untouched (no write at all).
    See
    ``tests/test_workspace.py::test_ensure_gitignore_repairs_stale_and_preserves_human_lines``
    and the stale-repair assertion in
    ``tests/test_workspace.py::test_refine_human_edit_survives``.

    **Filesystem-shape and OSError sweep (2026-07-29, NE-ESB-CLI REVISE 5).**
    ``--workspace``/``.gitignore`` are both operator-visible paths this
    function writes to; two shapes and every remaining ``OSError`` are now
    guarded, each raising a typed
    :class:`~ne.schema_bootstrap.errors.WorkspaceIOError` naming the path
    instead of letting a raw ``NotADirectoryError``/``IsADirectoryError``/
    ``OSError``/``UnicodeDecodeError`` escape: ``workspace_dir`` existing as
    a non-directory (checked BEFORE the ``mkdir`` call below, so a bad
    ``--workspace`` value fails with one clear message rather than a
    cryptic ``mkdir`` error), and ``.gitignore`` existing as a non-regular
    file (e.g. a directory). See
    ``tests/test_workspace.py::test_workspace_path_is_file_raises_typed_error``
    and ``::test_gitignore_directory_raises_typed_error``.
    """
    if workspace_dir.exists() and not workspace_dir.is_dir():
        raise WorkspaceIOError(
            str(workspace_dir),
            "exists but is not a directory; --workspace must point at a directory "
            "(or a creatable path)",
        )
    try:
        workspace_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceIOError(
            str(workspace_dir), f"cannot create workspace directory: {_io_detail(exc)}"
        ) from exc
    gitignore_path = workspace_dir / ".gitignore"
    if gitignore_path.exists() and not gitignore_path.is_file():
        raise WorkspaceIOError(
            str(gitignore_path), "exists but is not a regular file; remove it and rerun"
        )
    if not gitignore_path.is_file():
        try:
            gitignore_path.write_text(GITIGNORE, encoding="utf-8")
        except OSError as exc:
            raise WorkspaceIOError(
                str(gitignore_path), f"cannot write .gitignore: {_io_detail(exc)}"
            ) from exc
        return
    try:
        text = gitignore_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkspaceIOError(
            str(gitignore_path), f"cannot read .gitignore: {_io_detail(exc)}"
        ) from exc
    present = {line.strip() for line in text.splitlines()}
    missing = [entry for entry in _REQUIRED_GITIGNORE_ENTRIES if entry not in present]
    if not missing:
        return
    repaired = text if text.endswith("\n") or not text else text + "\n"
    repaired += "".join(f"{entry}\n" for entry in missing)
    try:
        gitignore_path.write_text(repaired, encoding="utf-8")
    except OSError as exc:
        raise WorkspaceIOError(
            str(gitignore_path), f"cannot write .gitignore: {_io_detail(exc)}"
        ) from exc


def _require_samples_dir(samples_dir: Path) -> None:
    """Raise :class:`~ne.schema_bootstrap.errors.EmptySampleError` naming
    ``samples_dir`` when it does not exist (or is not a directory), rather
    than letting :func:`~ne.schema_bootstrap.profiles.profile_samples`'s own
    ``Path.iterdir()`` raise an unhandled ``FileNotFoundError``/
    ``NotADirectoryError`` straight through this module and the CLI
    (2026-07-29 Argus P2 live repro: ``ne schema bootstrap --engagement
    acme-co`` with no samples directory yet — the CLI's OWN advertised
    default, ``<workspace>/samples`` — previously exited 1 with a bare
    Python traceback naming an absolute path, bypassing the reviewed
    disclosure surface described in ``ne.schema_bootstrap.errors``'s module
    docstring). ``EmptySampleError`` is reused rather than a new error type:
    from the operator's point of view "no usable sample files found in
    ``<path>``" is equally true whether the directory is missing or merely
    empty, and its existing message already names only the path. See
    ``tests/test_workspace.py::test_missing_samples_directory_raises_empty_sample_error``.
    """
    if not samples_dir.is_dir():
        raise EmptySampleError(str(samples_dir))


def _require_state_file(workspace_dir: Path, name: str) -> Path:
    path = workspace_dir / name
    if not path.is_file():
        raise RefineError(f"missing required workspace file for --refine: {path}")
    return path


def _read_state_text(path: Path) -> str:
    """Read one of the three ``--refine`` state files
    (``schema-draft.machine.yaml``/``schema-draft.yaml``/
    ``tombstones.json``) as UTF-8 text, raising :class:`RefineError` —
    naming ``path`` and a generic OS ``strerror``/decode
    reason+byte-position (never file content, see :func:`_io_detail`) —
    instead of letting a raw ``OSError``/``UnicodeDecodeError`` escape
    (2026-07-29, NE-ESB-CLI REVISE 5: closes the read-side twin of the
    parse-side guards :func:`_load_tombstones` and
    :func:`~ne.schema_bootstrap.refine._safe_load_draft` already had — a
    file that IS present, per :func:`_require_state_file`'s check, but
    cannot be READ (permission denied, a non-UTF-8 hand-edit, a race where
    it vanished between the check and the read) previously crashed with a
    bare traceback despite every OTHER malformed-content shape on the same
    three files already being reviewed-surface errors). See
    ``tests/test_workspace.py::test_state_file_not_utf8_raises_refine_error_naming_path``.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RefineError(f"cannot read {path}: {_io_detail(exc)}") from exc


def _load_tombstones(tombstones_path: Path) -> tuple[list[str], list[str]]:
    """Load ``tombstones.json`` into ``(tombstoned_classes, tombstoned_enums)``,
    raising :class:`RefineError` — naming ``tombstones_path`` and the
    parser's own position-only diagnostic — instead of letting a raw
    ``json.JSONDecodeError`` escape (2026-07-29 Argus P2: a hand-corrupted
    ``tombstones.json`` previously produced an unhandled traceback from
    inside ``--refine``). See
    ``tests/test_workspace.py::test_refine_malformed_tombstones_json_raises_refine_error``.
    The READ itself goes through :func:`_read_state_text` (2026-07-29 round
    5), so a non-UTF-8 or unreadable ``tombstones.json`` is covered too.
    """
    text = _read_state_text(tombstones_path)
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RefineError(f"{tombstones_path} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RefineError(
            f"{tombstones_path} does not contain a JSON object at the top level "
            f"(got {type(loaded).__name__})"
        )
    classes = loaded.get("classes", [])
    enums = loaded.get("enums", [])
    if not isinstance(classes, list) or not all(isinstance(name, str) for name in classes):
        raise RefineError(f"{tombstones_path}'s 'classes' key must be a list of strings")
    if not isinstance(enums, list) or not all(isinstance(name, str) for name in enums):
        raise RefineError(f"{tombstones_path}'s 'enums' key must be a list of strings")
    return classes, enums


def _refine_summary_section(
    merge: MergeResult,
    orphaned: list[str],
    *,
    landing_class_count: int,
    landing_enum_count: int,
) -> str:
    """A short, mechanical-only addendum appended to ``pr-body.md`` on
    ``--refine`` (2026-07-29 Argus P1). Every value here is either an
    integer count or a class/enum NAME already produced by
    ``three_way_merge``/``emit_linkml``'s own mechanical naming — the same
    disclosure argument ``errors.py``'s module docstring makes for
    ``WorkspaceExistsError``/``RefineError`` — never row content, so this is
    not a second, PII-bearing rendering path; see
    ``tests/test_workspace.py::test_refine_pr_body_reflects_landing_schema_not_stale_machine_draft``.

    Exists because ``render_pr_body``'s ``classes``/``slots``/``edges``
    bullets are computed from THIS RUN'S FRESH machine ``model`` (that
    function has no notion of a three-way merge), so on ``--refine`` those
    THREE bullets describe the machine draft, not necessarily the LANDING
    merged draft embedded under the ``## schema-draft.yaml`` heading above it
    (already three-way-merged and round-trip validated in this same call —
    see ``_run_bootstrap_refine``).

    ``enums emitted`` is the ONE EXCEPTION, stated precisely rather than
    lumped in with the three above (2026-07-29 Argus P3, round 2:
    ``_run_bootstrap_refine`` passes a ``landing_emitted`` view — an
    ``emitted.model_copy`` with ``emitted_enums`` overridden to the MERGED
    draft's enum names — into ``render_pr_body``, so that one bullet already
    describes the LANDING draft, same as the counts in this addendum, not
    the fresh machine run). See
    ``tests/test_workspace.py::test_refine_summary_enums_bullet_describes_landing_draft``.

    Tombstoned/orphaned class and enum names are
    listed explicitly here, in a clearly-labeled section, rather than
    silently appearing (or silently NOT appearing with no explanation) under
    the schema heading itself — the fenced schema block above never
    contains a tombstoned name (excluded by ``three_way_merge`` itself), so
    a name appearing HERE is never ambiguous about whether it landed.
    """
    lines = [
        "",
        "## Refine merge summary",
        "",
        "The `classes`/`slots`/`edges` counts above describe THIS RUN'S "
        "freshly re-profiled machine draft, not necessarily the LANDING "
        "draft embedded under `## schema-draft.yaml` above (already "
        "three-way-merged and round-trip validated in this same call). "
        "`enums emitted` above is the one exception — it already describes "
        "the LANDING (merged) draft, same as the counts below. "
        f"Full landing counts: classes={landing_class_count} enums={landing_enum_count}.",
        "",
        f"- human-kept (human-added/edited; machine changes discarded for these): "
        f"{len(merge.human_kept)}",
        f"- machine-updated (untouched by the human; took the new machine output): "
        f"{len(merge.machine_updated)}",
        f"- blocked resurrections (machine re-proposed a tombstoned entry; still "
        f"excluded): {len(merge.blocked_resurrections)}",
        f"- human re-added (a previously tombstoned entry the human brought back): "
        f"{len(merge.human_readded)}",
    ]
    if merge.tombstoned_classes or merge.tombstoned_enums:
        lines.append(
            "- tombstoned (human-deleted; excluded from the landing draft above, "
            "will not resurrect on a future refine unless re-added by hand):"
        )
        lines.extend(f"  - class `{name}`" for name in merge.tombstoned_classes)
        lines.extend(f"  - enum `{name}`" for name in merge.tombstoned_enums)
    else:
        lines.append("- tombstoned: none.")
    if orphaned:
        lines.append(
            "- WARNING — orphaned classes (present in `classes:` above but NOT "
            "reachable from the tree-root container's attributes, most often "
            "because a human edit froze the container before a later refine added "
            "these; the draft still generates, but these need re-wiring by hand): "
            + ", ".join(f"`{name}`" for name in orphaned)
        )
    return "\n".join(lines) + "\n"


def _run_bootstrap_fresh(
    samples_dir: Path,
    workspace_dir: Path,
    *,
    engagement: str,
    repo_root: Path,
    config: BootstrapConfig,
) -> BootstrapRunResult:
    draft_path = workspace_dir / DRAFT
    if draft_path.is_file():
        raise WorkspaceExistsError(str(workspace_dir))

    # Guard-first: the workspace directory and its samples-excluding
    # `.gitignore` exist before any sample is ever read (see module
    # docstring). This must survive every failure below.
    _ensure_gitignore(workspace_dir)

    _require_samples_dir(samples_dir)
    bundle = profile_samples(samples_dir, config=config)
    model = infer_candidate_model(bundle, config=config)
    emitted = emit_linkml(model, engagement=engagement, config=config)

    validate_draft_generates(
        emitted.yaml_text,
        repo_root=repo_root,
        top_class=emitted.top_class,
        expected_classes=[c.name for c in model.classes],
        expected_enums=emitted.emitted_enums,
        expected_edges=[e for c in model.classes for e in c.edges],
    )

    # Only reached once the round-trip above has been OBSERVED to succeed
    # for this exact draft — `round_trip_verified=True` below is honest.
    report = render_inference_report(
        bundle.profile, model, emitted, engagement=engagement, config=config
    )
    pr_body = render_pr_body(
        model, emitted, engagement=engagement, config=config, round_trip_verified=True
    )

    _write_staged_and_commit(
        workspace_dir,
        {
            MACHINE_DRAFT: emitted.yaml_text,
            DRAFT: emitted.yaml_text,
            REPORT: report,
            PR_BODY: pr_body,
            TOMBSTONES: _empty_tombstones_json(),
        },
    )

    # `emit_linkml` wires every one of `model.classes` into the tree-root
    # container's `attributes` unconditionally (one entry per class, keyed
    # by `to_snake_case(cls.source_table)` — see that function's own
    # docstring), so a fresh run can never orphan a class BY CONSTRUCTION.
    # Checked here, not assumed, so that guarantee is test-proven rather
    # than asserted in prose alone — see
    # `tests/test_workspace.py::test_fresh_run_never_orphans_classes`.
    fresh_dict = parse_merged_yaml(emitted.yaml_text)
    orphans = _orphaned_classes(fresh_dict, emitted.top_class)

    edge_count = sum(len(c.edges) for c in model.classes)
    return BootstrapRunResult(
        workspace=str(workspace_dir),
        refined=False,
        files_written=sorted(_RUN_ARTIFACTS),
        table_count=len(bundle.profile.tables),
        class_count=len(model.classes),
        enum_count=len(emitted.emitted_enums),
        edge_count=edge_count,
        merge=None,
        orphaned_classes=orphans,
    )


def _run_bootstrap_refine(
    samples_dir: Path,
    workspace_dir: Path,
    *,
    engagement: str,
    repo_root: Path,
    config: BootstrapConfig,
) -> BootstrapRunResult:
    machine_path = _require_state_file(workspace_dir, MACHINE_DRAFT)
    draft_path = _require_state_file(workspace_dir, DRAFT)
    tombstones_path = _require_state_file(workspace_dir, TOMBSTONES)

    # Guard-first, made UNCONDITIONAL (2026-07-29 Argus P3): a fresh run
    # always writes `.gitignore` before profiling, but until this call the
    # refine path never did — so a workspace whose `.gitignore` was deleted,
    # or one written by an older revision missing the staging-dir entry,
    # went through a full refine (including `_write_staged_and_commit`
    # creating `_STAGING_DIRNAME`) with the guard absent or stale.
    # `_ensure_gitignore` now REPAIRS a stale file (2026-07-29 round 4, Argus
    # P3-2: it used to write ONLY when the file was absent, silently leaving
    # a stale one — missing the staging-dir entry — unrepaired forever), by
    # appending any missing required entries while preserving every existing
    # (possibly human-added) line; it is a no-op when both entries are
    # already present, and still writes the canonical content when the file
    # is absent. Placed AFTER the three state-file checks above so a
    # `--refine` against a workspace missing its required state entirely
    # still fails with `RefineError` naming the missing path, not a
    # side-effecting `.gitignore` write, matching
    # ``test_refine_missing_state_file_raises_refine_error_naming_path``'s
    # existing behavior. See
    # ``tests/test_workspace.py::test_ensure_gitignore_repairs_stale_and_preserves_human_lines``
    # and the end-to-end stale-repair assertion in
    # ``tests/test_workspace.py::test_refine_human_edit_survives``.
    _ensure_gitignore(workspace_dir)

    base_text = _read_state_text(machine_path)
    ours_text = _read_state_text(draft_path)
    tombstoned_classes, tombstoned_enums = _load_tombstones(tombstones_path)

    _require_samples_dir(samples_dir)
    bundle = profile_samples(samples_dir, config=config)
    model = infer_candidate_model(bundle, config=config)
    emitted = emit_linkml(model, engagement=engagement, config=config)

    # Validation #1: the freshly-emitted MACHINE draft, on its own, must
    # still generate — same shape as the fresh path's proof.
    validate_draft_generates(
        emitted.yaml_text,
        repo_root=repo_root,
        top_class=emitted.top_class,
        expected_classes=[c.name for c in model.classes],
        expected_enums=emitted.emitted_enums,
        expected_edges=[e for c in model.classes for e in c.edges],
    )

    merge = _three_way_merge(
        base_text,
        ours_text,
        emitted.yaml_text,
        tombstoned_classes=tombstoned_classes,
        tombstoned_enums=tombstoned_enums,
    )

    merged_dict = parse_merged_yaml(merge.merged_yaml_text)
    top_class = find_tree_root_class(merged_dict)
    class_names, enum_names = merged_expected_names(merged_dict)

    # Validation #2: the LANDING merged draft must also generate.
    # `expected_edges=None` is decided: the textual EDGE_DEFS check is
    # machine-emit-shaped, and a human's edits may legitimately change
    # edges — the import/instantiation/JSON-Schema proofs still run.
    validate_draft_generates(
        merge.merged_yaml_text,
        repo_root=repo_root,
        top_class=top_class,
        expected_classes=class_names,
        expected_enums=enum_names,
        expected_edges=None,
    )

    # Only reached once BOTH validations above have been OBSERVED to
    # succeed in this same call — `round_trip_verified=True` below is
    # honest for the same reason the fresh path's is. `orphans` is computed
    # here (pure; safe post-gate) so it can drive BOTH the report's landing
    # view below AND the merge-summary/BootstrapRunResult uses further down
    # — one computation, not two.
    orphans = _orphaned_classes(merged_dict, top_class)

    # `inference-report.md` now renders a LANDING split too (2026-07-29
    # round 4, Argus P2 fix): before this, the report was rendered from
    # THIS RUN'S fresh machine `model`/`emitted` alone, describing a draft
    # that had not necessarily landed — a human-tombstoned class stayed
    # fully documented as if it were still in schema-draft.yaml, with
    # nothing telling the reader the deletion had taken. `RefineLanding`
    # carries the merge's own accounting (tombstones, human-kept,
    # machine-updated, blocked resurrections, human re-adds, orphans) so
    # `render_inference_report` can state explicitly, per class/enum, which
    # draft it landed as — see ``tests/test_workspace.py::
    # test_refine_report_reflects_landing_draft_not_fresh_machine_draft``.
    landing = RefineLanding(
        class_names=class_names,
        enum_names=enum_names,
        tombstoned_classes=merge.tombstoned_classes,
        tombstoned_enums=merge.tombstoned_enums,
        human_kept=merge.human_kept,
        machine_updated=merge.machine_updated,
        blocked_resurrections=merge.blocked_resurrections,
        human_readded=merge.human_readded,
        orphaned_classes=orphans,
    )
    report = render_inference_report(
        bundle.profile, model, emitted, engagement=engagement, config=config, landing=landing
    )

    # `pr-body.md`'s embedded "## schema-draft.yaml" block must show the
    # LANDING (merged) draft — the file that actually lands as
    # `schema-draft.yaml` — never the fresh machine draft alone (2026-07-29
    # Argus P1: a reviewer reading a pr-body embedding the machine draft
    # could approve a schema that is not the one on disk, and every human
    # edit/rejection was invisible in the artifact whose whole purpose is
    # the human review gate). `render_pr_body` takes an `EmittedSchema`, not
    # raw text, so a LANDING view is built via `model_copy` rather than a
    # second rendering path — every other field of `emitted` (declined
    # enums, provenance mapping) is kept as-is: those describe THIS RUN'S
    # machine reasoning, which remains true independent of what the human
    # merge kept. See test_workspace.py::
    # test_refine_pr_body_reflects_landing_schema_not_stale_machine_draft.
    landing_emitted: EmittedSchema = emitted.model_copy(
        update={
            "yaml_text": merge.merged_yaml_text,
            "top_class": top_class,
            "emitted_enums": enum_names,
        }
    )
    pr_body = render_pr_body(
        model, landing_emitted, engagement=engagement, config=config, round_trip_verified=True
    )

    pr_body += _refine_summary_section(
        merge,
        orphans,
        landing_class_count=len(class_names),
        landing_enum_count=len(enum_names),
    )

    _write_staged_and_commit(
        workspace_dir,
        {
            MACHINE_DRAFT: emitted.yaml_text,
            DRAFT: merge.merged_yaml_text,
            REPORT: report,
            PR_BODY: pr_body,
            TOMBSTONES: _tombstones_json(merge.tombstoned_classes, merge.tombstoned_enums),
        },
    )

    edge_count = sum(len(c.edges) for c in model.classes)
    return BootstrapRunResult(
        workspace=str(workspace_dir),
        refined=True,
        files_written=sorted(_RUN_ARTIFACTS),
        table_count=len(bundle.profile.tables),
        class_count=len(model.classes),
        enum_count=len(emitted.emitted_enums),
        edge_count=edge_count,
        merge=merge,
        orphaned_classes=orphans,
    )


def run_bootstrap(
    samples_dir: Path,
    workspace_dir: Path,
    *,
    engagement: str,
    repo_root: Path,
    config: BootstrapConfig = DEFAULT_CONFIG,
    refine: bool = False,
) -> BootstrapRunResult:
    """Drive the U1 profiler + U2 emitter/round-trip/renderers end to end
    into ``workspace_dir``, writing the five run artifacts ONLY after every
    round-trip validation this invocation performs has succeeded (see the
    module docstring).

    Fresh (``refine=False``): raises :class:`~ne.schema_bootstrap.errors.WorkspaceExistsError`
    if ``workspace_dir/schema-draft.yaml`` already exists. Otherwise creates
    the workspace + ``.gitignore`` guard first, then profiles ``samples_dir``,
    infers, emits, and validates before writing anything else.

    Refine (``refine=True``): raises :class:`~ne.schema_bootstrap.errors.RefineError`
    if any of the three required state files
    (``schema-draft.machine.yaml``, ``schema-draft.yaml``,
    ``tombstones.json``) is missing from ``workspace_dir``. Otherwise
    re-profiles/infers/emits a NEW machine draft, validates it standalone,
    three-way-merges it against the base/human drafts
    (:func:`ne.schema_bootstrap.refine.three_way_merge`), validates the
    MERGED draft too, and only then commits both drafts + regenerated
    report/pr-body + updated tombstones.

    **Validation order (2026-07-29, NE-ESB-CLI REVISE 5): ``engagement`` is
    validated FIRST, before any per-path check or filesystem write.**
    ``_validate_engagement`` (reused from
    :mod:`ne.schema_bootstrap.emit`, which already calls it for the same
    reason before emission) previously ran only inside ``emit_linkml`` —
    late enough that an invalid slug on a FRESH run still let
    ``_ensure_gitignore`` create the workspace directory and write
    ``.gitignore`` before the slug was ever checked (a side-effect-before-
    validation gap, the same class this unit's round exists to close, just
    for a validation failure rather than a filesystem one). An invalid
    ``engagement`` now raises
    :class:`~ne.schema_bootstrap.errors.InvalidEngagementNameError` with
    ZERO filesystem side effects, on both the fresh and ``--refine`` paths.
    One deliberate precedence consequence: an invalid slug against an
    ALREADY-EXISTING workspace now reports the slug error first, ahead of
    :class:`~ne.schema_bootstrap.errors.WorkspaceExistsError` — both are
    strictly-earlier typed failures, not a behavior regression. See
    ``tests/test_workspace.py::test_engagement_validated_before_any_workspace_write``.
    """
    _validate_engagement(engagement)
    if refine:
        return _run_bootstrap_refine(
            samples_dir, workspace_dir, engagement=engagement, repo_root=repo_root, config=config
        )
    return _run_bootstrap_fresh(
        samples_dir, workspace_dir, engagement=engagement, repo_root=repo_root, config=config
    )


__all__ = ["BootstrapRunResult", "run_bootstrap"]
