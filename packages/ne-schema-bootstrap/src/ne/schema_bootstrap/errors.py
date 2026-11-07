# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Error hierarchy for ``ne.schema_bootstrap``.

Error messages carry file names for diagnosability but deliberately NEVER carry
row content — the profiler ingests customer data, and an error message is a
routinely-copied artifact (terminal output, an issue report) that must not
become a second, uncontrolled channel for that data to leak through.
"""

from __future__ import annotations


class SchemaBootstrapError(Exception):
    """Base class for all ``ne.schema_bootstrap`` errors."""


class SampleFormatError(SchemaBootstrapError):
    """A sample file could not be parsed into a table.

    Carries the offending file's name and a generic, non-content-bearing
    description of the problem (e.g. "top-level JSON value is not an array").
    Never includes the row/line content that triggered the failure.
    """

    def __init__(self, filename: str, detail: str) -> None:
        self.filename = filename
        self.detail = detail
        super().__init__(f"malformed sample file {filename!r}: {detail}")


class EmptySampleError(SchemaBootstrapError):
    """A sample directory contained zero usable sample files."""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        super().__init__(f"no usable sample files (.csv/.tsv/.jsonl/.json) found in {directory!r}")


class DuplicateClassNameError(SchemaBootstrapError):
    """Two distinct source tables mechanically produce the same class name.

    ``class_name_for_table`` is a lossy mechanical transform (CamelCase +
    "Record") — table stems that differ only in non-alnum runs (e.g.
    `customer_orders` and `customer-orders`) collide. Carries both table
    stems (metadata, never row content) so the operator can see and rename
    one of the source files, rather than silently letting the second table
    shadow the first's inferred class.
    """

    def __init__(self, class_name: str, table_a: str, table_b: str) -> None:
        self.class_name = class_name
        self.table_a = table_a
        self.table_b = table_b
        super().__init__(
            f"duplicate candidate class name {class_name!r}: produced by both "
            f"tables {table_a!r} and {table_b!r}"
        )


class InvalidClassNameError(SchemaBootstrapError):
    """A mechanically-derived class name is not a valid Python identifier.

    Carries the offending ``class_name`` and the ``source_table`` (sample
    file stem) it was derived from — file-STEM metadata only, never row
    content, matching this module's policy (see the module docstring).

    Raised EARLY, before ``emit_linkml`` returns and long before any
    round-trip subprocess, replacing a late, cryptic failure verified live
    against the real isolated codegen venv: a sample named ``2023.csv``
    yields ``class_name_for_table('2023') == '2023Record'``, and feeding
    that draft to the real generator raises
    ``ValueError: Class name '2023Record' (Python: '2023Record') is not a
    valid Python identifier`` deep inside ``PydanticGenerator``. This error
    surfaces the same fact immediately, naming the file the operator needs
    to rename.
    """

    def __init__(self, class_name: str, source_table: str) -> None:
        self.class_name = class_name
        self.source_table = source_table
        super().__init__(
            f"derived class name {class_name!r} (from sample file stem {source_table!r}) "
            "is not a valid Python identifier — the codegen venv's PydanticGenerator "
            "rejects it with 'is not a valid Python identifier'; rename the sample file "
            f"so its stem starts with a letter (e.g. 'tbl_{source_table}')"
        )


class DuplicateRecordSetSlotNameError(SchemaBootstrapError):
    """Two distinct source tables mechanically produce the same top-level
    record-set container attribute name (Codex C1, 2026-07-28).

    ``emit_linkml``'s top-level record-set container keys one attribute per
    class by ``to_snake_case(cls.source_table)`` — the SAME lossy
    normalization :class:`DuplicateClassNameError` already guards for class
    names, but checked here against an INDEPENDENT dictionary key: nothing
    couples a :class:`~ne.schema_bootstrap.model.CandidateClass`'s ``name``
    field to its ``source_table`` field, so two classes with already-unique,
    non-colliding ``name``s can still collide here if their ``source_table``
    stems normalize to the same ``to_snake_case`` key (e.g. table stems
    ``customer-orders`` and ``customer_orders``, hand-assigned distinct class
    names). Left unrefused, the second table's ``record_set_attrs[...]``
    assignment in :func:`~ne.schema_bootstrap.emit.emit_linkml` silently
    overwrites the first's, dropping an entire sampled table's records
    attribute from the emitted schema with no error and no trace anywhere in
    the returned :class:`~ne.schema_bootstrap.emit.EmittedSchema`. Carries
    both table stems (metadata, never row content).
    """

    def __init__(self, slot_name: str, table_a: str, table_b: str) -> None:
        self.slot_name = slot_name
        self.table_a = table_a
        self.table_b = table_b
        super().__init__(
            f"duplicate record-set container attribute name {slot_name!r}: produced by "
            f"both source tables {table_a!r} and {table_b!r}"
        )


class InvalidEngagementNameError(SchemaBootstrapError):
    """An ``engagement`` slug failed the ``^[a-z][a-z0-9-]{0,62}$`` validation.

    The engagement slug is interpolated, unsanitized, into the emitted
    schema's ``id:``/``name:``/``prefixes:`` header fields — this validation
    is what makes that interpolation safe, not a sanitizer applied at emit
    time. Carries the offending name (already operator-supplied, never
    customer row content).
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"invalid engagement name {name!r}: must match ^[a-z][a-z0-9-]{{0,62}}$")


class DraftGenerationError(SchemaBootstrapError):
    """A draft schema failed to generate through the real isolated codegen venv.

    Carries the subprocess ``returncode`` and the LAST 2000 characters of the
    generator's stderr. linkml/PydanticGenerator diagnostics reference
    mechanical schema names (class/slot/enum identifiers, which are
    alnum/underscore-only by construction — see
    :mod:`ne.schema_bootstrap.sanitize`'s docstring) — never raw sampled
    rows, since the generator only ever sees the emitted YAML, which itself
    never carries raw row content. Truncation (not the presence of stderr at
    all) is the defensive measure here: a pathological generator failure
    message is bounded before it becomes a routinely-copied artifact (a
    terminal log, an issue report).
    """

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr[-2000:]
        super().__init__(
            f"draft schema failed to generate through the codegen venv "
            f"(exit code {returncode}): {self.stderr}"
        )


class MentionCompletenessError(SchemaBootstrapError):
    """The rendered inference report failed the §5.1 mention-completeness self-check.

    Carries the sorted list of expected anchor LABELS (never the raw anchor
    strings, which are mechanical-safe but this keeps the error message
    focused on what a reviewer needs: which class/slot/edge/enum) that are
    ``missing`` (mentioned zero times) or ``duplicated`` (mentioned more than
    once) in the serialized report. ``unexpected`` carries the SOUNDNESS
    direction (2026-07-28 Argus P2): raw anchor-SHAPED heading lines
    (``### Class \\`...\\```` / ``### Enum \\`...\\```` ) found in the report
    that do not correspond to any element ``expected_anchors`` derived from
    the model/emitted-schema — e.g. a heading rendered for an inferred enum
    that was never actually emitted (neither in ``emitted_enums`` nor
    ``declined_enums``, such as one whose only slot is provenance-mapped).
    Completeness alone (every expected anchor present exactly once) is blind
    to this direction: a report can mention every real element correctly AND
    additionally advertise an element that isn't in the schema at all.
    """

    def __init__(
        self, missing: list[str], duplicated: list[str], unexpected: list[str] | None = None
    ) -> None:
        self.missing = missing
        self.duplicated = duplicated
        self.unexpected = unexpected or []
        super().__init__(
            f"inference report failed mention-completeness: missing={missing!r} "
            f"duplicated={duplicated!r} unexpected={self.unexpected!r}"
        )


class WorkspaceExistsError(SchemaBootstrapError):
    """A fresh ``run_bootstrap`` invocation targeted a workspace that already
    has a ``schema-draft.yaml`` (NE-ESB-CLI, U3).

    Carries only the workspace PATH — never any content from the existing
    draft or the samples that produced it, matching this module's
    file/name-only disclosure policy (see the module docstring).
    """

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        super().__init__(
            f"workspace {workspace!r} already contains schema-draft.yaml; rerun with "
            "--refine to merge, or point --workspace elsewhere"
        )


class RefineError(SchemaBootstrapError):
    """A ``--refine`` invocation failed (NE-ESB-CLI, U3).

    Carries only paths (``ne.schema_bootstrap.workspace``'s three state
    files, named by their relative filename), a parser's own position-only
    diagnostic (a YAML/JSON syntax error names a line/column, never the
    surrounding content), an ``OSError``/``UnicodeDecodeError``'s generic
    strerror or reason+byte-position (never file content — see
    :class:`WorkspaceIOError`'s docstring for the identical argument), and
    mechanical class/enum names drawn from parsed YAML documents' keys —
    never a sample value or any other row-derived content, the same
    disclosure argument :class:`DraftGenerationError` documents above.

    **This docstring is the SINGLE authority for every shape ``RefineError``
    is raised for** (2026-07-29, NE-ESB-CLI REVISE 5 — replacing three
    separate, drifting restatements: this docstring's own prior "exactly
    four shapes" count, ``README.md``'s "Malformed hand-edited state files"
    paragraph, and its `three_way_merge`/errors API bullets; all three now
    point HERE instead of re-enumerating). The enumeration is pinned by an
    AST scan, not trusted prose — see
    ``tests/test_refine.py::test_refine_error_raise_sites_are_exactly_the_documented_shapes``,
    which fails the moment a new ``raise RefineError`` site is added
    anywhere in :mod:`ne.schema_bootstrap.refine` or
    :mod:`ne.schema_bootstrap.workspace` without a matching update below.

    ``RefineError`` is raised by this package for exactly these five shapes:

    1. A missing state file
       (``ne.schema_bootstrap.workspace._require_state_file``) — see
       ``tests/test_workspace.py::test_refine_missing_state_file_raises_refine_error_naming_path``.
    2. A state file that exists but cannot be read as UTF-8 text — an
       ``OSError`` (permission denied, a race where the file vanished
       between the existence check and the read) or a
       ``UnicodeDecodeError`` (2026-07-29 round 5, NEW —
       ``ne.schema_bootstrap.workspace._read_state_text``, used for
       ``schema-draft.machine.yaml``/``schema-draft.yaml``/
       ``tombstones.json`` alike). Names the path plus a generic OS
       ``strerror`` or the decode error's ``reason`` at ``byte position N``
       — NEVER the raw bytes (``UnicodeDecodeError.object`` is deliberately
       never touched) — see
       ``tests/test_workspace.py::test_state_file_not_utf8_raises_refine_error_naming_path``.
    3. A base/ours/theirs draft that is not valid YAML (including PyYAML's
       own ``ConstructorError`` for an unhashable complex key, e.g. a
       sequence used as a mapping key — already caught by the same
       ``yaml.YAMLError`` handler, see
       ``tests/test_refine.py::test_three_way_merge_complex_yaml_key_raises_refine_error``),
       or that parses to something other than a mapping at the top level,
       or gives ``classes:``/``enums:`` a non-mapping value, or (2026-07-29
       round 5, NEW) gives ``classes:``/``enums:`` a mapping with a
       NON-STRING key (e.g. an unquoted ``2024:`` — an ordinary hand-edit
       mistake, not a hostile input; the C1 fix validated the section's
       CONTAINER type but not its KEY type, one layer shallower than this
       shape needed)
       (``ne.schema_bootstrap.refine.three_way_merge``'s ``_safe_load_draft``).
       The message carries only PyYAML's own generic problem text and a
       line/column — never ``Mark.get_snippet()``'s echoed source lines
       (``_safe_load_draft``'s ``_position_only_message`` helper
       deliberately avoids ``str(exc)``/``str(mark)`` for exactly this
       reason) — see
       ``tests/test_refine.py::test_three_way_merge_raises_refine_error_on_malformed_yaml``,
       ``tests/test_refine.py::test_three_way_merge_raises_refine_error_on_non_mapping_named_section``,
       ``tests/test_refine.py::test_refine_error_excludes_source_snippet_from_malformed_yaml``,
       and (round 5)
       ``tests/test_refine.py::test_three_way_merge_raises_refine_error_on_non_string_section_key``.
    4. A ``tombstones.json`` that is not valid JSON, not a JSON object at
       the top level, or whose ``classes``/``enums`` key is not a list of
       strings (``ne.schema_bootstrap.workspace._load_tombstones``) — see
       ``tests/test_workspace.py::test_refine_malformed_tombstones_json_raises_refine_error``.
    5. A tree-root cardinality failure
       (``ne.schema_bootstrap.refine.find_tree_root_class``) — see
       ``tests/test_refine.py::test_find_tree_root_unique_and_errors``.

    Every one of the five call sites above passes only a path, a parser's
    position-only diagnostic, an OS/decode error's generic detail, or a
    mechanical name into this constructor — proven, not just asserted, by
    the tests named above AND by the AST raise-site ratchet.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"refine failed: {detail}")


class WorkspaceIOError(SchemaBootstrapError):
    """An operator-visible workspace/staging filesystem operation failed, or
    met a shape this module refuses to destroy (2026-07-29, NE-ESB-CLI
    REVISE 5 — the sweep closing Argus's ``_clear_staging`` finding one
    layer deeper than the reported instance: not just "don't unlink a
    directory", but every filesystem state the crash-RECOVERY path can meet
    on a fixed, operator-visible name).

    Two families of trigger:

    * A raw ``OSError`` from a workspace/staging filesystem call
      (``mkdir``/``write_text``/``read_text``/``replace``/``unlink``/
      ``rmdir``/``iterdir``) — permission denied, disk full, a race. Carries
      the offending path and the OS's own generic ``strerror`` (or the
      exception's type name when ``strerror`` is unset).
    * A SHAPE the loader admits as a plain filesystem entity but this module
      refuses to destroy or cannot use: a subdirectory sitting where this
      module only ever writes plain files (``.bootstrap-staging/`` — this
      module deletes only what it wrote, and NEVER recurses into a
      directory it did not create), a non-directory occupying a
      directory-shaped name (the staging dir, the workspace dir), a
      non-regular-file occupying ``.gitignore``, or a directory occupying a
      run-artifact's landing name (``inference-report.md`` et al.).

    Carries only paths and a generic OS/shape description — never file
    content — the same disclosure argument :class:`RefineError` and this
    package's ``errors`` module docstring make for every other error here.

    See
    ``tests/test_workspace.py::test_clear_staging_refuses_subdirectory_with_typed_error``,
    ``::test_staging_name_occupied_by_file_raises_typed_error``,
    ``::test_workspace_path_is_file_raises_typed_error``,
    ``::test_gitignore_directory_raises_typed_error``, and
    ``::test_commit_destination_directory_raises_typed_error``.
    """

    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"workspace filesystem error at {path!r}: {detail}")


class DuplicateSlotNameError(SchemaBootstrapError):
    """Two columns on the same source table mechanically produce one slot name.

    ``to_snake_case`` is a lossy mechanical transform — column headers that
    differ only in non-alnum runs (e.g. `first-name` and `first_name`) both
    normalize to ``mechanical_name="first_name"``. A schema consumer keyed by
    the normalized name can only represent one of them; silently emitting
    both slots would let the second column's data collide with, or shadow,
    the first's under that shared key. Carries the table name and both
    column headers (metadata, never row content) so the operator can see
    and rename one of the source columns.
    """

    def __init__(self, table_name: str, mechanical_name: str, column_a: str, column_b: str) -> None:
        self.table_name = table_name
        self.mechanical_name = mechanical_name
        self.column_a = column_a
        self.column_b = column_b
        super().__init__(
            f"duplicate mechanical slot name {mechanical_name!r} on table {table_name!r}: "
            f"produced by both columns {column_a!r} and {column_b!r}"
        )
