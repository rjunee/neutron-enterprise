# ne-schema-bootstrap

U1+U2 of the NE-SCHEMA-BOOTSTRAP epic:
a bounded, deterministic sample profiler and pure candidate-model inference
core (U1), plus a house-dialect LinkML emitter, a round-trip validator that
PROVES the draft generates through the real isolated codegen venv, a
total-traceability inference report, and a `pr-body.md` emitter (U2). The
`ne schema bootstrap` CLI, live-DB sampling, and LLM-assisted labeling are
later units (U3-U7) — **not built yet**. **`linkml` is NEVER imported
in-process** — generation runs only as a subprocess of the isolated,
hash-pinned codegen venv (`ne.schema.codegen`, C4 unchanged; see
[`src/ne/schema_bootstrap/roundtrip.py`](src/ne/schema_bootstrap/roundtrip.py)'s
module docstring). Third-party runtime deps: `pydantic` + `pyyaml` (both
MIT, already locked — see [`NOTICE`](NOTICE)).

## The pure-function guarantee (U1)

`profile_samples` + `infer_candidate_model` together form a pure pipeline:

- **No LLM.** No `ModelGateway`, no provider SDK, no prompt.
- **No network.** No HTTP client, no socket, anywhere in the import graph
  (structurally enforced — see [`tests/test_boundaries.py`](tests/test_boundaries.py),
  an AST-based import allowlist over every module in `src/`, now PER-MODULE:
  only `emit.py` may import `yaml` + `unicodedata` + `keyword` (`unicodedata`
  because its enum charset gate's R4 rule and the `_member_label` mangle
  mirror both call `unicodedata.name()` — the SAME call the real generator's
  `generate_enum_label` member-name mangle makes, so the gate predicts real
  generator behaviour instead of guessing; `keyword` because `_member_label`
  calls `keyword.iskeyword()` — the SAME check the real generator's
  `EnumValue.alias_python_keywords` runs, `pydanticgen/template.py:127-131`,
  to mask a Python-keyword member label by appending `_`) and only
  `roundtrip.py` may import
  `ne.schema`/`ne.envbuild`/`subprocess`/`sys`/`tempfile` — every other
  module still gets the bare stdlib+pydantic+self allowlist, and
  `linkml`/`httpx`/`requests`/`socket`/`litellm`/`ne.flywheel`/`ne.contracts`
  stay forbidden EVERYWHERE with no carve-out).
- **No `linkml` import.** Neither `profile_samples`/`infer_candidate_model`
  (U1) nor `emit_linkml`/`render_inference_report`/`render_pr_body` (U2)
  touch `linkml` in-process — U2's `roundtrip.validate_draft_generates`
  invokes it only as a subprocess of the isolated venv.
- **Byte-for-byte deterministic.** The same sample directory, profiled and
  inferred twice — or profiled from an identical directory whose files were
  created in a different order — produces `model_dump_json()` output that is
  byte-identical every time. This is proven, not asserted: see
  [`tests/test_determinism.py`](tests/test_determinism.py)'s
  `test_pipeline_byte_determinism`, a seeded property-style test across five
  randomly-generated corpora, with a sensitivity control (a one-row mutation
  must change the output) so the equality checks aren't vacuous. U2's
  `emit_linkml`/`render_inference_report`/`render_pr_body` are pure functions
  too — see `tests/test_emit.py::test_emit_is_deterministic_byte_identical`
  and `tests/test_prbody.py::test_prbody_is_deterministic`.

That determinism is the whole point of the epic: a customer's schema draft is
reproducible from their sample data, and the (separate, optional) LLM-labeling
layer added in U5 can only ever propose *names* on top of this deterministic
skeleton — never invent or alter structure.

## U2 — LinkML emission, round-trip proof, and the inference report

### The house-dialect emitter (`emit.py`)

`emit_linkml(model, engagement=...)` renders a `CandidateModel` into a
house-dialect LinkML YAML draft, following the platform's own
`operational.yaml` idioms (abstract provenance base class, `is_a` classes,
`inlined: false` FK relations, a `tree_root` record-set container). Several
answers here are DECIDED, not obvious, and worth stating plainly:

- **The abstract provenance base class is named literally `OperationalEntity`**
  — not `<Engagement>Entity`, despite the design doc's own prose. The real
  generator's Tier-2 entity-graph emitter hardwires that exact class name
  (`packages/ne-schema/codegen/generate.py:211,215`, outside this package's
  `mayTouch`) when deciding which classes are concrete operational entities.
  Any landed engagement schema wanting Tier-2 graph contracts needs this name
  TODAY; renaming it is a follow-up unit's job (editing `packages/ne-schema`),
  not this emitter's.
- **Seed anchors are reviewer proposals only.** No class is ever emitted
  `is_a: <SeedClass>` — adopting the platform's seed model would require
  importing `operational.yaml` into a standalone draft, which would break the
  "generates on its own" property this whole unit proves. Seed-anchor
  evidence surfaces in the inference report instead.
- **Residual #1 (inherited from U1), decided:** `ColumnProfile` carries both
  `distinct_count` (raw, pre-scrub) and `distinct_values` (retained,
  post-scrub-and-dedupe — can be SMALLER when raw-distinct values collapse
  after scrub/truncate). The emitted `permissible_values` are ALWAYS the
  RETAINED list (`CandidateEnum.permissible_values`) — the only PII-safe
  artifact. The raw count never reaches the YAML at all, so the emitted
  schema can never state an inconsistent count/value-list pair; the
  divergence, when it exists, is disclosed in the inference report instead
  (`distinct (raw): N` / `permissible values (retained): M` / a `collapsed by
  scrub/truncate` note when `M < N`). The `infer.py` enum GATE stays
  conservative (gated on the larger raw `distinct_count`) — untouched.
- **The enum-value safety gate** (`_enum_decline_reason`, revised
  2026-07-28, R6 P0-fixed 2026-07-28, differential-fuzz round 2026-07-28)
  declines an enum (falls its slot back to `range: string`, records the
  reason in `EmittedSchema.declined_enums`) when its permissible values trip
  one of its EIGHT checks, evaluated in order: (R1) empty/whitespace-only,
  (R2) a raw newline/carriage-return, (R3) a backslash anywhere, (R4) an
  unnamed-ON-THE-PLATFORM control character (NUL/BEL/ESC/DEL — the
  generator's own `unicodedata.name()` member-name fallback raises on
  these), **(R4b, new this round) a codepoint assigned a name in the
  PLATFORM's Unicode database but UNASSIGNED in the pinned `.codegen-venv`'s
  (older) Unicode database** — R4 alone cannot see this, because R4 queries
  the platform's database and the real generator's mangle queries the
  venv's; probed live: `'grade\U0002EBF0'` passes R4 (the platform names it)
  but crashes the real generator with `ValueError: no such name` inside the
  venv, (R5) the value's RAW member label — computed by
  `_member_label_raw`, the real generator's own two-step mangle, with NO
  NFKC applied — is not a valid Python identifier (`not raw.isidentifier()`,
  **checked on the RAW label, not the NFKC-normalized one — this is the P0
  fix**, see below), collides with `Enum.mro` (checked on the NFKC-NORMALIZED
  identity, via `_member_label`, including NFKC-equivalent spellings like
  fullwidth `'ｍｒｏ'`), or is underscore-bookended (also checked on the
  normalized identity; `_name_`, `__init__`, ...), and (R6) two DIFFERENT
  values whose NORMALIZED member labels (`_member_label`) collide —
  including values that are only NFKC-equivalent, e.g. `'µm'` (MICRO SIGN)
  vs `'μm'` (GREEK SMALL LETTER MU), an ordinary uncleaned units-column
  pair, and values that only collide because of the venv-isidentifier
  emulation (below), e.g. `'a・b'` (KATAKANA MIDDLE DOT) vs
  `'aKATAKANA_MIDDLE_DOTb'` — (the generator either silently DROPS one
  member with no exception at all — so only a member-content assertion can
  catch it, not a pass/fail round-trip — or fails LOUD with a Python-level
  `TypeError` at Enum-class construction whenever the collision is INVISIBLE
  to the generator's own label-mangle dedup: keyword-masking (e.g.
  `TypeError: 'class_' already defined as 'class'`) or CPython's own NFKC
  normalization (e.g. `TypeError: 'μm' already defined as 'µm'`)). Every
  check but two is EMPIRICAL — tied to a real failure observed against the
  real `.codegen-venv`; R5's underscore-bookended branch is explicitly
  CONSERVATIVE, over-declining three probe-proven-benign shapes
  (`_missing_`/`_ignore_`/`__str__`) alongside the genuinely fatal ones, on
  purpose, because distinguishing them member-by-member isn't worth the
  complexity; R4b is ALSO explicitly conservative, in the opposite
  direction — see below. See `_enum_decline_reason`'s docstring for the full
  per-check empirical/conservative tagging.

  **Legality vs. identity — why R5's own check moved to the RAW label
  (2026-07-28, differential-fuzz round, the P0 fix).** CPython's tokenizer
  validates an identifier's LEGALITY against the raw source characters it
  actually reads, and applies NFKC normalization only afterwards, purely to
  decide identifier IDENTITY (whether two already-legal spellings are "the
  same name"). Checking legality on the NFKC-normalized form — what this
  gate did before this round — silently admits values whose raw token is
  actually illegal: `unicodedata.normalize("NFKC", "m²")` folds the
  SUPERSCRIPT TWO into an ordinary digit, producing the legal-looking
  string `"m2"`, even though the real generator writes the raw token
  `m² = "m²"` into the module and CPython's tokenizer rejects it outright
  (`SyntaxError: invalid character '²' (U+00B2)`). `'m²'`/`'10 m³'` were
  wrongly ADMITTED by this bug and are now correctly DECLINED — pinned by
  `tests/test_roundtrip.py`'s `trailing-superscript`/
  `trailing-superscript-spaced` regression cases and
  `tests/test_differential_fuzz.py`, which found this exact class of defect
  against a real corpus value on its first run (see "Curated vs. systematic
  coverage" below).

  **The platform/venv Unicode-database and CPython-XID skew (2026-07-28,
  differential-fuzz round, the P1 fix).** Two distinct interpreter-level
  divergences affect this gate, each named and closed with a declared,
  test-proven mechanism rather than guessed at:
  - `_UNICODE_DB_SKEW` (627 codepoints: `U+2FFC-U+2FFF`, `U+31EF`,
    `U+2EBF0-U+2EE5D`) — codepoints assigned a name in the platform's
    Unicode 15.1 database (python 3.13, today) but UNASSIGNED in the pinned
    `.codegen-venv`'s Unicode 15.0 database (python 3.12, today). R4b
    declines any value containing one. Ten already-named-in-both CJK
    ideographs whose `isnumeric()` classification merely flipped between
    the two databases (e.g. `U+4E24`) are DELIBERATELY NOT included —
    every predicate this gate/mirror consults tests `isalpha() OR
    isnumeric()`, and those ten satisfy `isalpha()` in both databases, so
    the flip is invisible to every check that matters.
  - `_XID_SKEW` (4 codepoints: `U+200C` ZWNJ, `U+200D` ZWJ, `U+30FB`
    KATAKANA MIDDLE DOT, `U+FF65` HALFWIDTH KATAKANA MIDDLE DOT) —
    identifier-continue-legal on the platform's CPython 3.13 but NOT on the
    venv's CPython 3.12. `_venv_isidentifier` emulates the VENV
    interpreter's `str.isidentifier()` by subtracting this set from the
    platform's own answer, and `_member_label_raw` uses it (not plain
    `str.isidentifier()`) for the branch decision the real generator makes
    INSIDE the venv. Without this, the mirror silently disagrees with the
    real generator on any value containing one of these four characters —
    e.g. a platform-only mirror predicts `'a・b'`'s label unchanged (already
    "looks like" a legal identifier on the platform), while the real
    venv-side generator transliterates it to `'aKATAKANA_MIDDLE_DOTb'`.

  Both sets are proven EXHAUSTIVE, not just plausible, by
  `tests/test_roundtrip.py::
  test_platform_and_venv_unicode_property_signatures_match_outside_declared_skew`:
  a sha256 digest over the 8-predicate signature (name, isspace,
  isalpha-or-isnumeric, isidentifier, continue-position isidentifier, NFKC,
  regex `\d`, regex `\s`) of every non-surrogate codepoint EXCLUDING the two
  declared sets, computed on both the platform interpreter and (via
  subprocess) the real venv interpreter, must match exactly — proving every
  OTHER codepoint's mirror-relevant behavior is byte-identical across the
  two interpreters. A sensitivity control re-leaks one codepoint back into
  the comparison, on BOTH sides (platform AND venv, same exclusion set —
  2026-07-28 Argus P2, fifth review round: the previous version compared a
  reduced-exclusion PLATFORM digest against the FULL-exclusion VENV digest,
  which differ in cardinality regardless of skew and so passed
  vacuously), and confirms the digest then diverges; a paired control that
  excludes one extra BENIGN codepoint from BOTH sides confirms the digest
  does NOT diverge — together proving the comparison discriminates actual
  skew, not merely the exclusion set's size.

  A decline is not free: the slot silently falls back to `range: string`,
  losing the constraint this whole unit exists to infer — which is why this
  revision widened the gate rather than narrowed it. The PRE-REVISION
  charset (a single regex, `^[A-Za-z0-9][A-Za-z0-9 _.\-/]*$`) wrongly
  declined at least 19 real-world value shapes that generate fine —
  backtick, `@`-mentions, `%`, quotes, `<>`, embedded colons, non-ASCII
  (`Größe`, emoji, CJK, a KATAKANA MIDDLE DOT, a ZWJ emoji sequence),
  digit-leading values (`1st`), and Python keywords (`class`) among them —
  while its docstring claimed "calibrated against the REAL generator ...
  the test is the oracle, not a guess" with **no test backing that claim at
  all**. The gate now genuinely IS test-backed — by TWO oracles, a curated
  one and a systematic one (see "Curated vs. systematic coverage" below):
  `tests/test_roundtrip.py::test_charset_gate_accepted_values_roundtrip_through_real_generator`
  (the curated acceptance half — those 19 shapes actually generate) and
  `::test_charset_gate_rejected_values_actually_break_the_real_generator`
  (the rejection half — each empirical rule's decline is proven against a
  real generator failure, not asserted in prose).

  **The R6 P0 fix (2026-07-28, NE-ESB-EMIT CONTINUATION 2).** The rule R6
  exists to enforce — "two different permissible values must never mangle
  to the same generated member name" — was itself checked against a helper
  that reproduced ONLY the whitespace-collapse step of the real generator's
  mangle and stopped. That under-approximation let R6 miss most REAL
  collisions: uncleaned customer CSV data routinely has the same status
  value spelled with a hyphen, a space, and an underscore in the same
  column (`in-progress` / `in progress` / `in_progress`), and the pre-fix
  gate admitted all three, silently dropping two of them. The fix replaces
  that helper with `_member_label`, a mirror DERIVED (not transliterated)
  from three real
  `.codegen-venv` (linkml 1.11.1) source locations: `underscore()`
  (`linkml_runtime/utils/formatutils.py:30` — strip, whitespace-collapse,
  comma-drop, hyphen-collapse), `generate_enum_label`'s digit-prefix +
  `unicodedata.name()` transliteration fallback
  (`linkml/generators/oocodegen.py:184-196`), and
  `EnumValue.alias_python_keywords`'s keyword-masking step
  (`linkml/generators/pydanticgen/template.py:127-131` — appends `_` to a
  label that is a Python keyword; this third step was missed by the initial
  fix sketch and is the source of the `keyword-suffix` collision family
  below). Equivalence is pinned by an oracle, not prose: see
  `tests/test_roundtrip.py::test_charset_gate_accepted_values_roundtrip_through_real_generator`
  (set-equality between the real generator's actual member names and
  `_member_label`'s output, on 17 values spanning every mangle step) and
  `::test_real_mangle_collision_families_decline_and_prove_member_loss` (six
  collision families — hyphen-vs-space, hyphen-vs-underscore, comma-elision,
  digit-prefix, a three-way family, and keyword-suffix — each declined by
  the gate and forced through the real generator to prove the actual failure
  mode: five silently collapse to exactly one surviving member, one fails
  loud). `::test_near_miss_families_accepted_and_roundtrip_full_member_count`
  is the over-declining guard: seven near-collision values that do NOT
  actually collide under the real mangle are still ACCEPTED with their full
  member count intact — the stricter rule must not degrade into "decline
  anything with punctuation".

  **Four more findings closed in the SAME review round (2026-07-28, NE-ESB-EMIT
  CONTINUATION 2, Argus P1/P2/P3/P3).**
  1. *(P1)* R5's label check was `label == ""`, which only catches an EMPTY
     mangled label. `_member_label`'s char-by-char transliteration step
     passes through any character satisfying `str.isalpha()` or
     `str.isnumeric()` unchanged (mirroring
     `replace_invalid_identifier_character`'s own test exactly) — but ~923
     Unicode codepoints satisfy that test while being illegal in a Python
     identifier, including `½`/`¼`/`¾`/`²`/`³`/`¹` (ordinary content in
     size/measurement columns). Probe-verified: `['½ inch', 'plain']` was
     previously ADMITTED, then broke the real generator with
     `SyntaxError: invalid character '½' (U+00BD)` deep inside
     `schema_draft.py`. R5 now checks `not label.isidentifier()` directly
     (a strict superset of the empty check, verified non-regressive against
     both the 17-value acceptance set and the 7-value near-miss set — no
     currently-accepted value starts being declined).
  2. *(P2)* The docstring previously claimed the mangle mirror was "a FULL
     derivation ... with NO known residual gap" — an absolute claim the P1
     finding immediately falsified. `_enum_decline_reason`'s docstring now
     states the claim is BOUNDED to what the oracle tests actually exercise
     (the acceptance set, the near-miss set, the six collision families),
     not asserted over the entire input space.
  3. *(P3)* The keyword-suffix collision family's failure mode was
     documented (and asserted only via bare `DraftGenerationError`) as a
     `ValueError`. Observed live, it is a `TypeError` —
     `EnumDict.__setitem__` (`enum.py:403`) raising `TypeError: 'class_'
     already defined as 'class'`. Docs corrected and the oracle test
     (`test_real_mangle_collision_families_decline_and_prove_member_loss[keyword-suffix]`)
     now asserts the exception text itself, not merely its class.
  4. *(P3)* The keyword-masking mirror calls `keyword.iskeyword` on the
     PLATFORM interpreter while the real `alias_python_keywords` runs
     inside the `.codegen-venv`'s interpreter — a different Python minor
     version (platform 3.13, venv 3.12 today). Correctness silently depends
     on `keyword.kwlist` parity between the two. That parity is no longer
     assumed: `test_platform_keyword_list_matches_codegen_venv_keyword_list`
     subprocesses the real venv interpreter and asserts set-equality
     directly, so a future venv pin bump that changes its keyword list
     fails this test rather than silently drifting.

  **Four more findings closed in the THIRD review round (2026-07-28,
  NE-ESB-EMIT CONTINUATION 2, Argus P1/P2/P3/P3).**
  1. *(P1)* Even the corrected derivation above stopped at the real
     generator's OWN contribution to the mangle — but the generated
     artifact is Python SOURCE, and CPython's lexer performs one more
     transform the generator itself never applies: NFKC identifier
     normalization ("Identifiers are compared by their NFKC normal form",
     Python Language Reference). Two members whose labels were merely
     NFKC-equivalent, not textually identical — `'µm'` (MICRO SIGN) vs
     `'μm'` (GREEK SMALL LETTER MU), an ordinary uncleaned units-column
     pair — passed R5/R6 unnoticed and broke the real generator with
     `TypeError: 'μm' already defined as 'µm'`; a fullwidth `'ｍｒｏ'`
     bypassed R5's own `Enum.mro` check the same way. `_member_label` now
     applies `unicodedata.normalize("NFKC", ...)` as its final step, AFTER
     keyword-masking (mask-then-normalize is the correct order: masking
     itself runs on the generator's own un-normalized label, exactly as the
     real generator does, so normalizing first would mask NFKC-equivalent
     keyword spellings the real generator never masks).
  2. *(P2)* `tests/test_roundtrip.py`'s `_enum_member_names` AST helper
     silently benefited from `ast.Name.id` being NFKC-normalized by
     CPython's own parser, so the equivalence oracle would have passed for
     the WRONG reason (comparing normalized-real against un-normalized-
     mirror) had a single NFKC-diverging value ever reached it — no test
     forced one. Now that `_member_label` normalizes its own output too,
     both sides of every equivalence assertion are normalized ON PURPOSE,
     not by accident of the probed sets happening to be NFKC-invariant —
     documented explicitly in the helper's docstring, and pinned by the two
     new `nfkc-*` collision families below.
  3. *(P3)* `_enum_decline_reason`'s R6 collision reason unconditionally
     claimed "the generator silently drops one member (probe-proven member
     loss, not a generation failure)" for EVERY collision — false for the
     keyword-suffix family (and now also false for both `nfkc-*` families),
     all three of which are loud `TypeError`s, not silent drops. This
     customer-facing string lands in `EmittedSchema.declined_enums` and the
     traceability report. Reworded to a claim that is true for every family:
     "depending on the collision shape, the generator either silently drops
     one member or fails during generation" — pinned by an assertion in
     `test_real_mangle_collision_families_decline_and_prove_member_loss` run
     against EVERY family (not only the loud ones), so a regression back to
     the family-blind claim goes RED regardless of which family runs first.
  4. *(P3)* The "Guard mutations verified" table's rows 44-50 were orphaned
     from their header/delimiter row by an interposed prose paragraph (row
     43's "superseded, not wrong" scope note), so GitHub-flavoured Markdown
     would have rendered them as literal pipe-delimited text, not table
     rows. Fixed by moving that scope note to AFTER the table's final row
     (see below) so rows 1-52 render as one unbroken table.

  **A P0 regression and a P1 gap closed in the FOURTH review round
  (2026-07-28, NE-ESB-EMIT CONTINUATION 3 / differential-fuzz round —
  Argus P0/P1/P3), and a correction to round 2's own claim above.**
  1. *(P0, regression)* Round 2's fix above ("R5 now checks `not
     label.isidentifier()` directly") checked that on
     `_member_label`'s NFKC-NORMALIZED output — which is exactly wrong,
     and is what let `'m²'`/`'10 m³'` (TRAILING-position superscripts) slip
     through ADMITTED even though round 2's own `'½ inch'` fix already
     declined the LEADING-position case. The claim two paragraphs above
     ("`½`/`¼`/`¾`/`²`/`³`/`¹` (ordinary content in size/measurement
     columns)" are declined) was therefore only ever true in LEADING
     position — trailing position silently escaped until this round. R5's
     legality check now runs on the RAW, pre-NFKC label
     (`_member_label_raw`) — see the gate description above for the
     legality-vs-identity argument — and BOTH positions are now genuinely
     declined for every one of `½`/`¼`/`¾`/`²`/`³`/`¹`, pinned
     leading-and-trailing by `tests/test_emit.py::
     test_enum_gate_decline_reasons_rule_specific`'s twelve dedicated
     params (one per character, per position) and by
     `tests/test_roundtrip.py`'s `trailing-superscript`/
     `trailing-superscript-spaced` regression cases against the real
     generator.
  2. *(P1)* The platform/venv Unicode-database skew (R4b) and the
     CPython-XID skew (`_venv_isidentifier`) — see the gate description
     above for both. Neither was declared or handled before this round;
     `'grade\U0002EBF0'` was silently ADMITTED and crashed the real
     generator, and `'a・b'`/ZWJ-emoji values were mis-predicted by the
     mirror (the mirror said "already legal, no transliteration" where the
     real venv-side generator transliterates). Both closed with a declared
     mechanism, not a patch — see the cross-interpreter parity-digest test.
  3. *(P3)* Every prose passage in this README and in `emit.py` naming a
     specific character class (`½`/`¼`/`¾`/`²`/`³`/`¹`, the fullwidth
     spellings, the NFKC-folding pairs, the XID-skew characters) is now
     backed by a test containing that exact character — the third round in
     which this unit's prose claimed a property the code lacked (see the
     "Curated vs. systematic coverage" section below for why: a hand-curated
     oracle cannot catch what its curator did not imagine, and neither can
     hand-written prose).

  **A stopping-rule direction (i) defect and two test-quality findings
  closed in the FIFTH review round (2026-07-28, NE-ESB-EMIT CONTINUATION 3
  — Argus P1/P2/P3).**
  1. *(P1, accept-side silent drop, stopping-rule direction (i))* Even the
     differential fuzz's systematic, ground-truth-backed proof has a scope
     boundary it names honestly (see "Curated vs. systematic coverage"
     below): it proves label computation against the real generator's own
     mangle, never the enum-CLASS-CONSTRUCTION machinery that only exists
     once `enum.EnumMeta` actually builds the class. That machinery has its
     own reserved-name rule the gate never mirrored: `enum._EnumDict.
     __setitem__` silently refuses to record an assignment whose target is
     Python-mangled PRIVATE to the class currently being defined
     (`_<ClassName>__x`, not ending in `__`) — no exception, no warning.
     `_enum_decline_reason` previously admitted
     `'_WidgetsTagEnum__foo'`: the generated module imported fine and
     "looked" complete while the member was silently absent from
     `WidgetsTagEnum.__members__` — exactly the class of accept-side
     silent-corruption bug the R6 collision rule exists to prevent, just
     from name-mangling rather than mangle-collision. Fixed by
     `_is_enum_private_member_name`, a verbatim-derived mirror of CPython's
     own `enum._is_private` (same string logic on both interpreters — no
     Unicode-database skew treatment needed, unlike `_venv_isidentifier`),
     wired into R5 next to the `mro`/underscore-bookend checks. This is
     inherently a CURATED-oracle-only fix, not a fuzz one: the predicate is
     keyed to the enum CLASS name, and the fuzz proves label computation
     independent of which class an enum ends up in (see its own scope
     boundary) — proof:
     `tests/test_emit.py::test_is_enum_private_member_name_matches_stdlib`
     (exhaustive parity against the real stdlib predicate) and
     `tests/test_roundtrip.py::
     test_charset_gate_declines_private_mangled_enum_class_name` (live,
     forced-through proof that the generated SOURCE still contains the
     assignment textually while the RUNTIME `__members__` silently drops
     it — the textual/AST oracle `_enum_member_names` cannot see this
     class of defect at all, which is why the new
     `_real_enum_member_names_at_runtime` helper imports the module and
     inspects it live, in a fresh interpreter, instead).
  2. *(P2, vacuous sensitivity control)* The cross-interpreter parity-digest
     test's own sensitivity control (added the previous round to satisfy
     the vacuous-guard rule) was itself vacuous: it compared a platform
     digest computed with one skew codepoint LEAKED BACK IN against the
     VENV digest computed with the FULL exclusion set — two different
     exclusion-set sizes, so the inequality held regardless of whether any
     real skew was involved (confirmed: excluding one BENIGN codepoint
     instead of the genuine skew one also broke that comparison). Fixed by
     computing BOTH the leaked-skew case and a same-cardinality
     leaked-benign control on BOTH interpreters (platform AND venv, one
     extra ~2.5s venv subprocess per case) — see the gate description
     above.
  3. *(P3, fuzz corpus independence)* The differential fuzz's own
     DB-skew corpus pool was built FROM `emit_mod._UNICODE_DB_SKEW` (the
     constant the fuzz exists to help validate), so the fuzz was
     structurally incapable of catching a bug in that constant itself —
     e.g. emptying it (mutation M-d) previously crashed the CORPUS BUILDER
     (`rng.choice([])`) rather than tripping the gate/ground-truth
     assertion, a weaker RED than the mutation table originally claimed.
     `tests/test_differential_fuzz.py`'s `_DB_SKEW_CPS`/`_XID_SKEW_CHARS`
     are now independent literal transcriptions of the same three measured
     ranges, not aliases — M-d's row below now reports the real RED this
     produces.

  **Accepted residual from the fifth review round (Argus P3, declared, not
  fixed).** The fuzz's ground-truth equivalence assertion runs only for
  gate-ACCEPTED values (plus the collision phase, both directions) — there
  is no systematic per-value check that every gate-DECLINED value's real
  label, when it binds cleanly, falls into an ENUMERATED set of declared
  decline reasons (stopping-rule direction (ii) is still covered only by the
  curated `test_charset_gate_conservative_declines_are_labeled_conservative`
  at the single-example level). No live defect was found under this round's
  live corpus (zero of 882 declined values had a real label binding cleanly
  under an undeclared reason); filed as a follow-up strengthening, not a
  blocker, per the stopping rule.

### Curated vs. systematic coverage — the differential fuzz (FINAL round)

Every round through the third proved this gate's equivalence to the real
generator against a HAND-CURATED oracle: a fixed list of accepted values
(19), rejected values (14), collision families (9), and near-miss values (8)
that a reviewer happened to think of. That has a structural ceiling — it can
only ever catch a defect in a family the curator imagined — and in fact did
not catch either of the fourth round's two real defects: the P0 regression
(`'m²'`/`'10 m³'` wrongly admitted) and the P1 database skew
(`'grade\U0002EBF0'` wrongly admitted) both escaped a suite that, at the
point they were introduced, had 243 green tests and six mutation rows
written specifically about this exact function. **This is the same lesson
NE-VG-0 hit the same night from the other direction: curated coverage does
not converge on correctness, systematic coverage does.**

The fifth review round's own P1 finding (the enum-private-name silent drop,
above) reinforces the SAME lesson from a third angle: it escaped the fourth
round's differential fuzz too — not because the fuzz is weak, but because
its own honestly-stated scope boundary (label computation, not
enum-class-construction machinery) doesn't cover a class-name-keyed
predicate by construction. The curated oracle (now 19 accepted, 15
rejected, 9 collision families, 8 near-miss values) and the systematic fuzz
each catch what the other structurally cannot — neither alone converges.

`tests/test_differential_fuzz.py` closes that ceiling. It computes GROUND
TRUTH by importing the real generator's own
`OOCodeGenerator.generate_enum_label`/`EnumValue` mangle machinery and
running it INSIDE the pinned `.codegen-venv` (never reimplemented), over a
large, deterministically-seeded corpus (`_FUZZ_SEED = 20260728`), then
asserts this package's gate/mirror agrees with that ground truth in both
directions: every value the gate ACCEPTS must bind cleanly to exactly the
identifier `_member_label` predicts, and R6's collision verdict must agree
with the real generator's collision identity for both colliding AND
non-colliding pairs. One run against this seed/corpus: 5291 values, 4409
accepted, 882 declined, 499 ground-truth errors (the venv-crash space — DB
skew and similar), 313 multi-value collision groups, 300 over-decline
control pairs checked — all green. Reverting the P0 fix (mutation M-a) or
the venv-isidentifier emulation (mutation M-c) turns the fuzz RED
immediately (see the mutation table below) — that is this round's thesis
demonstrated directly, not merely claimed.

The fuzz SUPPLEMENTS the curated oracle, it does not replace it: it proves
label computation, binding legality, and collision identity at the LABEL
level against real ground truth, but the curated oracle remains the
authority for the enum-MACHINERY branches (`Enum.mro`, sunder/dunder
collisions, the silent-drop-vs-loud-`TypeError` distinction) and for R1-R3's
claims about the raw VALUE (not the label) breaking the generated string
literal — those require actually generating and parsing/importing the full
module, which the fuzz deliberately does not do (see
`tests/test_differential_fuzz.py`'s module docstring for the full scope
boundary). Every specific defect found across all four rounds keeps its
named regression pin in the curated oracle — the fuzz did not replace their
documentation value, it replaced the CLAIM that the curated set alone proves
general equivalence.

**Accepted residuals (FINAL round).** Per this round's stopping rule, a
finding is fixed only if it is (i) the gate accepting a value that breaks or
silently corrupts real generation, (ii) the gate declining a value that
generates fine, or (iii) the gate crashing instead of returning a verdict.
Two design choices are explicitly NOT findings under that rule and are
declared here rather than "fixed":
- R4b's conservative decline of the ten isnumeric-classification-changed CJK
  ideographs would have been over-cautious — deliberately NOT applied (see
  the gate description above); this is a design choice, not a residual gap.
- The underscore-bookend branch (R5, CONSERVATIVE since round 2) continues
  to over-decline three probe-proven-benign shapes; unchanged this round,
  filed as a pre-existing accepted residual, not reopened.

The fifth review round's own P1 finding (the enum-private-name silent drop)
WAS a stopping-rule direction (i) defect — accept-side silent corruption —
and is fixed above, not filed as a residual; a prior draft of this section
claimed no new residual class fell outside the stopping rule's three
directions, which that finding falsified — corrected here rather than left
standing. One genuine residual remains from this round's review, explicitly
declared rather than fixed because it names no live defect:
- The differential fuzz's equivalence assertion runs only for gate-ACCEPTED
  values (plus the collision phase, both directions); there is no
  systematic per-value check that every gate-DECLINED value's real label,
  when it binds cleanly, falls into an enumerated set of declared decline
  reasons — direction (ii) coverage still rests on the curated oracle's
  hand-picked examples at the single-value level. Classified against this
  round's own corpus: zero of 882 declined values had a real label binding
  cleanly under an undeclared reason, so no live defect exists today; filed
  as a follow-up strengthening (an enumerated-reason systematic check),
  not reopened here.

- **Identifier columns** (a table's own natural key, e.g. a self-referential
  `customer_id`) emit as a plain `required: true` attribute with NO
  `identifier: true` — the base `id` slot is the platform-canonical
  identifier; a second `identifier: true` slot would conflict with it.
- **Provenance-named columns** (a sample column that happens to be named
  `id`/`source_system`/`source_record_id`/`created_at`/`updated_at`) are
  SKIPPED at emission — the base class already covers them — and recorded in
  `EmittedSchema.provenance_mapped` so the report can disclose the omission.
- **Digit-leading sample-file stems are rejected EARLY, naming the file**
  (`InvalidClassNameError`, 2026-07-28 P2). `class_name_for_table` is a
  mechanical `CamelCase(stem) + "Record"` transform with no leading-digit
  guard — a stem like `2023.csv` mechanically derives `2023Record`, which
  the real codegen venv's `PydanticGenerator` rejects with `ValueError: ...
  is not a valid Python identifier`, verified live. `emit_linkml` now checks
  every derived class name before doing any other work, so the operator sees
  the offending FILE immediately instead of a late, cryptic
  `DraftGenerationError` — proof:
  `tests/test_roundtrip.py::test_derived_class_name_oracle_bidirectional`
  (both the valid-stem control and the digit-leading failure, against the
  real generator).

### The round-trip validator (`roundtrip.py`) — the defining proof

`validate_draft_generates` writes the emitted YAML to a temp directory and
subprocesses the REAL isolated codegen venv
(`ne.schema.codegen.ensure_env` + `packages/ne-schema/codegen/generate.py`,
inheriting that module's AIR_GAP fail-closed posture unchanged), then PROVES
the draft actually generates — not merely that the subprocess exited 0 — by
(1) confirming all four artifacts exist, (2) IMPORTING the generated Pydantic
module in a FRESH platform-python subprocess (no `linkml_runtime` on that
interpreter's path) and instantiating the tree-root class with no arguments,
(3) parsing the JSON Schema and confirming the tree-root class is a defined
`$defs` entry, (4) textually verifying `draft_graph.py`'s `EDGE_DEFS` match
the model's edges exactly. A YAML-well-formedness check would prove none of
this — see `tests/test_roundtrip.py::test_broken_schema_fails_the_roundtrip_loudly`
for the non-vacuity control (a hand-broken draft genuinely fails).

**Known residual, filed rather than worked around out-of-lane:**
`draft_graph.py`'s enum import is hardwired to
`ne.schema.generated.operational` (`generate.py:250-257`, outside this
package's `mayTouch`), so an enum-bearing draft's generated graph module is
not importable as a Python module. The validator therefore only proves
`EDGE_DEFS` textually and never imports the graph module. A future
`ne-schema` unit needs a `--graph-enum-module` argument added to
`generate.py` to lift this (U7 will need it for the mapping-draft work).

### The inference report and `pr-body.md` (`report.py`, `prbody.py`)

`render_inference_report` renders the candidate model + emitted schema into
a reviewable markdown document with a TOTAL-traceability guarantee (design
§5.1): `verify_mention_completeness` independently re-derives the exact
anchor set every emitted class/slot/edge/enum must produce and counts each
anchor's occurrences in the SERIALIZED report — a class/slot/edge missing
zero times, or an anchor mentioned more than once, raises
`MentionCompletenessError` naming exactly which element. `render_pr_body`
follows the `ne.flywheel.diffs` PR-body conventions (metadata bullets, a
dynamically-fenced schema-draft section, a tracked-manual-apply footer
stating the tool NEVER applies or auto-lands anything).

Every profile-derived, untrusted string (table names, raw column headers,
exemplars, min/max, permissible values, declined-enum reasons) reaches
either rendered artifact through EXACTLY ONE of
[`sanitize.py`](src/ne/schema_bootstrap/sanitize.py)'s two boundaries
(`safe_inline` for single-line interpolation, `fenced` for the dynamically-
fenced YAML section) — a LOCAL reimplementation of the
`ne.flywheel.diffs` three-boundary rule (same escape set, same fence-length
formula), proven byte-for-byte over the same behaviors in
[`tests/test_sanitize.py`](tests/test_sanitize.py). PII-flagged columns
render zero value-bearing content anywhere in the report — proven over the
serialized report text, not the in-memory objects
(`tests/test_report.py::test_pii_column_values_absent_from_serialized_report`).

## Data-handling posture (design §3.3)

Customer data flows into this package's profiler, and the profile it produces
is the artifact meant to be persisted and reviewed (in a later unit, as part
of a git PR). Accordingly:

- **Bounded profiles.** Row sampling is capped (`BootstrapConfig.rows_per_table`,
  default 200) via a deterministic head+reservoir selection seeded from the
  table name — never wall-clock, never a global RNG. This cap is now applied
  DURING reading, not after: `.csv`/`.tsv`/`.jsonl` are read as a single lazy
  stream (`sampling.sample_stream`) that never holds more than the cap in
  memory regardless of file size — a much-larger-than-cap file no longer has
  to be materialized in full first (`.json`, a single top-level JSON array,
  is the one format the stdlib `json` module cannot stream; only the
  RETAINED row list is bounded there, not the transient parse — see
  [`readers.py`](src/ne/schema_bootstrap/readers.py)'s module docstring).
- **Two-phase, bounded, EXACT FK-containment evidence (SPEC §6.1.1 arbiter
  ruling).** FK containment never needs the target column's full value set —
  only membership of the SOURCE's already-*sampled* FK values (at most
  `rows_per_table` per column) in the target column. `profile_samples` runs a
  second phase after profiling: it selects each table's identifier column,
  computes FK-candidate pairs from the sampled data using the same `_id`-name
  heuristic `infer.py` uses for edge construction
  (`model.fk_candidate_targets` — one helper, not two independently
  transliterated copies of the rule), then re-streams ONLY the specific
  targeted identifier column of the specific targeted file
  (`readers.stream_column_values`), testing membership one value at a time
  with an early exit once every probed value is found. Peak memory for this
  phase is `O(#fk-columns x rows_per_table)` — exact, deterministic, and
  independent of the target table's true row count or column cardinality.
  This replaces an earlier design that collected a per-column FULL
  distinct-value set during phase 1 ("bounded to a small number of columns"
  — true of column COUNT, false of column CARDINALITY: an identifier column
  on an arbitrarily large target file was still fully materialized). See
  [`tests/test_infer_fk_junction.py`](tests/test_infer_fk_junction.py)`::test_fk_pipeline_memory_bounded_with_over_cap_identifier_target`
  (bounded AND exact, together) and its sibling
  `::test_memory_bound_would_catch_unbounded_identifier_collection` (the
  non-vacuity control proving the fixture is large enough to matter).
- **Bounded exemplars, min, and max — through a SINGLE retention choke
  point.** At most `exemplars_k` (default 5) example values per column, PLUS
  `min_value`/`max_value` for orderable types, are all truncated to
  `exemplar_max_chars` (default 80) and passed through a local,
  dependency-free credential-shape scrubber
  ([`hygiene.py`](src/ne/schema_bootstrap/hygiene.py) — a deliberate copy of
  the `ne-flywheel` pattern set, not an import, to keep this package
  dependency-free). Every one of these fields is derived from a raw sampled
  value through exactly one function, `profiles._retain_value` (PII-gate,
  then scrub, then truncate) — no field re-implements that sequence on its
  own, closing off the recurring failure class where a new value-bearing
  field was a new opportunity to leak (`min_value`/`max_value` were fixed
  TWICE, independently, for parallel reasons, before this consolidation).
  min/max additionally exclude non-finite float parses (`_is_float` rejects
  `inf`/`nan`) — a numeral long enough to overflow `float()` still parses
  "successfully" to `inf`, which would otherwise become a silently WRONG
  value in a reviewed artifact.
- **Deterministic PII flags, value-shape checks gated on inferred STRING
  type.** Column-name-family and value-shape heuristics
  ([`pii.py`](src/ne/schema_bootstrap/pii.py)) — regex/lookup only, no
  scoring, no randomness. The name-family check fires regardless of a
  column's inferred type; every value-SHAPE check (email/phone/national-id)
  now runs ONLY when `inferred_type == "string"` (mirroring
  `_format_pattern`'s existing gate) — without this gate, an ordinary numeric
  column's string rendering could coincidentally satisfy the phone-shape
  charset (digits plus `.`/`-` as "separators") and get silently
  PII-suppressed; see
  `tests/test_pii.py::test_numeric_amount_and_balance_columns_not_flagged_as_phone`
  for the direct repro and
  `tests/test_pii.py::test_phone_shaped_string_column_still_flags` for the
  bidirectional control (the string case must still flag). A flagged column
  has its exemplars, its distinct-value list, **and** its min/max **all**
  suppressed (routed through the single retention choke point above) — only
  aggregate stats (counts, ratios) are retained. Suppression is proven over
  the column profile's *serialized* JSON form, not just the in-memory
  object, AND over a reflection-derived set of every value-bearing field
  (see [`tests/test_retention_choke.py`](tests/test_retention_choke.py) —
  the guard enumerates `ColumnProfile`'s value-bearing string fields FROM
  the pydantic schema itself, not a hardcoded list, so it auto-catches the
  next field added). The value-shape heuristics are also order-sensitive and
  charset-bounded within the string-typed case: `shape:national_id` is
  excluded from the `shape:phone` check (both an SSN-shaped value and an
  ISO-8601 date otherwise clear the phone charset's digit/length floor), and
  a phone shape additionally requires at least one explicit separator
  character (or a leading `+`) so a bare, separator-free digit run — a plain
  identifier like an order number or account id, not a phone number — is
  never falsely PII-suppressed. **Residual gap, stated deliberately rather
  than left implicit — and CORRECTED after review, because the first wording
  of this paragraph was false:** value-shape checks now run only when
  `inferred_type == "string"`, so a phone column is value-shape-flagged only
  if its values fail to parse as a number. Measured, not reasoned:
  `["+15551234560", ...]` infers as `int` (Python's `int()` accepts a leading
  `+`) and returns `(False, None)`; `["(415) 555-0134", ...]` infers as
  `string` and returns `(True, "shape:phone")`. So the gap is any column of
  genuine phone numbers stored in a form that parses as a number —
  separator-free (`"5551234567"`) **or plus-prefixed (`"+15551234560"`)** —
  under an opaque column name. An earlier version of this paragraph asserted
  the shape check still fires on "a leading `+`"; that was WRONG after the
  type gate landed, and is the [[written-rules-disarm-review]] failure this
  repo keeps catching — normative prose that stopped matching the code.
  The column-NAME heuristic still catches any column literally named from the
  `phone` family regardless of value shape, which is the mitigation; the gap
  is opaque-name-plus-numeric-parseable-storage specifically, judged an
  acceptable trade against the false-positive it replaced (ordinary `amount`
  and `balance` columns were being flagged as phone numbers and having their
  ranges silently blanked).
- **Malformed/pathological input is refused, not silently corrupted or
  left to leak.** A non-UTF-8 sample file, a CSV header with a
  literally-repeated column name, an oversized delimited cell, an over-wide
  delimited row (more cells than the header — the excess used to be silently
  dropped with no error and no signal), two files whose stems collide into
  the same table name (e.g. `a.csv` + `a.jsonl`), and two tables whose
  mechanically-derived class names collide (e.g. `customer_orders.csv` +
  `customer-orders.csv`, both -> `CustomerOrdersRecord`) each raise a typed,
  file/name-carrying error (`SampleFormatError` from
  [`readers.py`](src/ne/schema_bootstrap/readers.py), or
  `DuplicateClassNameError` from `infer.py`) rather than an untyped
  exception or a silently last-wins-collapsed table/column/class. The
  `readers.py` wrapping raises use `from None`, severing `__cause__` and
  setting `__suppress_context__` — both the wrapper's own `repr()` AND
  default `traceback`-module rendering (default exception printing,
  `logging.exception`, `traceback.format_exc` — the realistic
  "routinely-copied artifact" channels) stay clean of the original
  exception's raw content.
- **`ProfileBundle` never serializes.** FK-containment inference needs
  bounded raw-value evidence, not just profiles — so `profile_samples`
  returns a plain `dataclass` (`ProfileBundle`), deliberately **not** a
  pydantic model, pairing the serializable `SampleProfile` with two
  in-memory-only carriers that have no `model_dump`: `column_values`
  (sampled/capped) and `fk_target_membership` (the bounded, EXACT
  containment evidence described above — keyed by `(target_table,
  target_identifier_column)`, holding only the SUBSET of probed source
  values confirmed present in that target column). Only `SampleProfile` is
  ever meant to reach disk.

All thresholds governing the above live in `BootstrapConfig`; see that
module's docstring for the design citation behind each field. Threshold
*values* are asserted only by tests (`tests/test_infer_enums.py`,
`tests/test_infer_fk_junction.py`, `tests/test_pii.py`, etc.) — this README
intentionally does not restate them as normative prose.

## U3 — CLI, workspace, refine

`ne schema bootstrap` (registered on the existing `schema` click group,
`tools/ne-cli/src/ne/cli/schema.py`) is the delivery-facing entrypoint:
point it at a directory of sample files and get a reviewable workspace
containing a schema draft, a traceability report, and a `pr-body.md` — with
the real-codegen-venv round-trip gate proven to pass BEFORE any of it lands
on disk. The CLI itself is a thin, lazily-importing caller (proof:
`tools/ne-cli/tests/test_schema_bootstrap_cli.py::
test_cli_import_does_not_import_schema_bootstrap`); all the logic lives in
two library modules, `workspace.py` and `refine.py`.

### Workspace layout and the `.gitignore` guard-first decision

A fresh run (`workspace.run_bootstrap`, `refine=False`) writes, into
`--workspace` (default `./discovery/<engagement>/`):

| File | Content |
|---|---|
| `schema-draft.machine.yaml` | the pure machine output (byte-identical to `schema-draft.yaml` on a fresh run) |
| `schema-draft.yaml` | the reviewed/edited artifact — a human edits THIS file |
| `inference-report.md` | the U2 total-traceability report |
| `pr-body.md` | the U2 PR-body emitter's output |
| `tombstones.json` | `{"classes": [], "enums": []}` on a fresh run |
| `.gitignore` | `samples/\n.bootstrap-staging/\n` |

**The `.gitignore` guard is written, and the workspace directory created,
BEFORE any sample is read or profiled** — design §3.3's "samples never enter
git" guard must not wait on a successful run. If profiling/inference/
emission/validation fails after that point, the workspace directory and its
`.gitignore` are the ONLY things that exist afterwards — proof:
`tests/test_workspace.py::test_fresh_run_errors_write_nothing_when_workspace_absent`
and `::test_generation_failure_leaves_no_partial_artifacts`.

### Validate-before-write, proven not asserted

Every round-trip validation an invocation performs
(`validate_draft_generates`) succeeds BEFORE any of the five run artifacts
is written. The commit itself stages every file in a directory with a FIXED
name (`.bootstrap-staging`, one of the two `.gitignore` entries above)
created INSIDE the workspace (same filesystem) and lands each one with
`Path.replace` — atomic PER FILE (POSIX `rename(2)`), NOT as a set: a crash
partway through the per-file `replace` loop can leave a mixed set (narrowed
2026-07-29, Argus P3 — an earlier revision of this paragraph over-claimed
set-level atomicity that `_write_staged_and_commit`'s own implementation
never provided). What IS proven, and test-covered, is the narrower and more
important property: a failure BEFORE the commit step runs at all (i.e.
before validation has succeeded) leaves nothing partial, because validation
always precedes commit — mirroring the fix the T2X epic's `ExtractionWriter`
needed for exactly this failure class (a mixed batch that wrote entities
then raised). Proven with forced-failure tests, each paired with an
unforced control that succeeds:
`tests/test_workspace.py::test_generation_failure_leaves_no_partial_artifacts`
(fresh path) and `::test_refine_failure_leaves_workspace_byte_identical`
(refine path — snapshots every file's bytes before/after a forced failure on
the SECOND, merged-draft validation call and asserts byte-identity). The
staging directory's FIXED name (replacing an earlier `tempfile.mkdtemp`
random name) exists so a SIGKILL mid-commit cannot leave an un-gitignored
`tmpXXXXXX/` directory sitting in the workspace where it could be
accidentally committed into the engagement PR (2026-07-29 Argus P3); a
leftover staging directory from a prior crash is cleared before reuse on the
next commit.

**Single-writer assumption (2026-07-29 Argus P3, round 2).** The fixed
staging name trades crash-recovery for concurrent-safety: `_clear_staging`
unconditionally removes anything already at `.bootstrap-staging` before
(re)creating it, so two `run_bootstrap` calls racing on the SAME
`workspace_dir` at once can delete each other's in-flight staged files. `ne
schema bootstrap` is a single-operator discovery CLI (design §4.4) with no
caller in this codebase running concurrent invocations against one
workspace, so this is accepted here, not fixed; a future multi-writer
caller would need an `O_EXCL` lock file in `workspace_dir`, not a change to
this staging directory's name.

### `--refine`: the three-way merge (`refine.py`)

`--refine` re-runs the profiler/inferencer/emitter over (possibly updated)
samples to produce a NEW machine draft, then three-way-merges it into the
existing human draft: `base` = the previous `schema-draft.machine.yaml`,
`ours` = the current, human-edited `schema-draft.yaml`, `theirs` = the new
machine output. **The human side wins every conflict, and every
human-deleted class/enum stays deleted** (recorded in `tombstones.json`, so
a later refine cannot resurrect it unless the human re-adds it themselves).
`three_way_merge` applies six LOCKED rules, in this order, independently to
the `classes` and `enums` sections, per NAMED ENTRY:

1. Tombstoned AND re-added by the human -> keep `ours`, clear the tombstone.
2. Tombstoned (not re-added) -> excluded; a machine re-proposal is recorded
   as a blocked resurrection, not silently dropped.
3. In `base`, absent from `ours` -> the human deleted it: excluded, and the
   name is added to the tombstone list.
4. In `ours`, and absent from `base` or different from `base` -> `ours`
   wins (human-added or human-edited).
5. In `theirs` -> `theirs` wins (an untouched-by-human machine update, or a
   brand-new machine entry).
6. Otherwise (in `ours`, `ours == base`, absent from `theirs`) -> excluded:
   an untouched machine deletion flows through.

Every rule has a positive AND a negative/control assertion in
`tests/test_refine.py` (e.g. `test_human_edit_wins_over_machine`'s own
control proves an UNTOUCHED sibling entry takes `theirs`, not `ours`). The
merge is entry-level, not per-field: a class or enum the human touched at
all is taken WHOLESALE from `ours` — see `refine.py`'s module docstring for
why (per-field merging would need a second, independent LinkML-shape diff
algorithm, and would blur "the human decided this class needs editing" into
a field-by-field negotiation). All OTHER top-level scalar keys (`id`,
`title`, `description`, `version`, ...) take `ours` if it differs from
`base` at that key, else `theirs`; a key existing on only one side is kept;
a key present in `base` and absent from `ours` is a human deletion and is
EXCLUDED even if `theirs` still emits it — the scalar-key mirror of Rule
(3) above, fixed 2026-07-30 (a real correctness gap: `theirs` re-emitting
an auto-generated field the human had deliberately removed was silently
resurrecting it every refine). Proven by `tests/test_refine.py::
test_scalar_key_deletion_by_human_is_not_resurrected_by_theirs` (row 74
below).
**Declared residual (2026-07-29 round 4):** the top-level `slots:` block is
one of these scalar keys, so it is merged as ONE OPAQUE value too — a single
human edit anywhere inside `slots:` freezes the WHOLE shared-slot section to
`ours` on every future refine, silently discarding a later machine update to
a DIFFERENT sibling slot in the same block. This is the same freeze class
the tree-root-container case has (diagnosed there by `orphaned_classes`, see
below) but carries NO diagnostic for `slots:` — a residual of the
entry/key-level merge design, not fixed here. Proven bidirectionally by
`tests/test_refine.py::test_top_level_slots_block_merges_as_one_opaque_key`.
Serialization is `yaml.safe_dump(data, sort_keys=False,
allow_unicode=True)`, which is deterministic for fixed inputs
(`tests/test_refine.py::test_merge_is_deterministic`) but does NOT preserve
a human's inline comments or blank-line formatting — a declared limitation
of the file-based iterate-to-approval loop (design §4.4), not a defect: a
YAML round-trip through parsed Python objects cannot do otherwise without a
comment-preserving library this dependency-free package does not carry.

On `--refine`, BOTH the fresh machine draft (standalone) AND the landing
merged draft are validated through the real codegen venv in the SAME call,
before either is written — so `round_trip_verified=True` on the refined
`pr-body.md` is honest for the same reason it is on a fresh run: the caller
actually observed both validations succeed for this exact invocation.

**`pr-body.md` embeds the LANDING draft, not the stale machine draft
(2026-07-29 Argus P1 fix).** `render_pr_body` takes an `EmittedSchema`
object, not raw YAML text; an earlier revision passed it THIS RUN'S fresh
machine `emitted` unchanged on `--refine`, so the `## schema-draft.yaml`
block embedded in the review artifact showed the machine's output —
including any class a human had just tombstoned, if its source sample was
still present (the machine proposes it again every run; only the merge
excludes it). A reviewer reading that pr-body could approve a schema that
was not the one that actually landed as `schema-draft.yaml`, and a
human-edited description was invisible in the one artifact whose entire
purpose is the human review gate. Fixed by building a LANDING view via
`emitted.model_copy(update={...})` (yaml_text/top_class/emitted_enums set
from the merged draft) — reusing `render_pr_body`, not a second rendering
path — before calling it. Because the LANDING view's `emitted_enums` is set
from the merged draft's enum names, the `enums emitted` metadata bullet
ITSELF already describes the LANDING draft, not the fresh machine run —
unlike `classes`/`slots`/`edges`, which `render_pr_body` still computes from
THIS RUN'S fresh machine `model` and so still describe the machine draft.
`workspace.py` then appends a small, purely mechanical "## Refine merge
summary" section (integer counts plus class/enum NAMES already produced by
`three_way_merge`'s own mechanical naming — the same disclosure argument
`errors.py` makes, never row content) stating this distinction explicitly —
that `classes`/`slots`/`edges` ABOVE the embedded schema describe this run's
fresh machine draft while `enums emitted` already describes the landing
draft — repeats the full landing counts for clarity, and names any
tombstoned or orphaned class/enum (2026-07-29 Argus P3, round 2: an earlier
revision of this addendum's own prose lumped `enums emitted` in with the
three machine-draft bullets, which was not true — the enum override above
predates that prose and the two had drifted). See
`tests/test_workspace.py::test_refine_pr_body_reflects_landing_schema_not_stale_machine_draft`,
which proves: the tombstoned name is absent from the fenced schema block
specifically, present (labeled "tombstoned") in the merge-summary section,
and the fenced block's content matches the file that actually landed on
disk byte-for-byte; and
`tests/test_workspace.py::test_refine_summary_enums_bullet_describes_landing_draft`,
which proves the `enums emitted`/`classes` bullets' divergent provenance
directly (a human-rejected enum makes the fresh-machine count and the
landing count differ, and the rendered bullet shows the landing one).

**`inference-report.md` states what landed, explicitly (2026-07-29 round 4,
Argus P2 fix).** The pr-body fix above closed one half of the divergence;
`inference-report.md` was the other half, and the worse one. Before this
fix, `render_inference_report` was called on `--refine` with only THIS
RUN'S fresh machine `model`/`emitted` — no indication anywhere that the
draft it was describing might not be the one that landed. Argus verified
this live: a human tombstones a class whose sample file is still present
(the machine re-proposes it every run); `--refine` exits 0; the landing
draft correctly excludes the class — and the report still fully documents
it, with nothing telling the reader the deletion had taken. Since this
report's entire purpose is traceability (what was inferred, mapped to what
shipped), a report that silently describes an un-landed draft is worse than
no report at all — it tells a reviewer the deletion did not take. Fixed by
an explicit split rather than a silent switch: `render_inference_report`
now takes an optional `landing: RefineLanding | None` view (built from the
same `MergeResult`/`merged_expected_names`/`orphaned_classes` this call
already computes). When present, it adds a leading `## What landed
(--refine)` section (landing/tombstoned/blocked/human-kept/machine-updated
counts and names) AND a "landing status" line as the first bullet in every
`### Class`/`### Enum` section — one of "did NOT land — tombstoned", "landed
as the HUMAN'S version" (the evidence below is this run's machine inference,
not necessarily what's on disk), or "landed (machine version)". The
`## Classes`/`## Enums` sections themselves are UNCHANGED in what they
evidence (this run's fresh profile/model — the only thing they can
truthfully evidence) — they now just SAY SO, explicitly, instead of leaving
it to be inferred. `landing=None` (the fresh-run default) renders
byte-identically to before this fix. Landing names may be human-typed (a
human-added/renamed class key), so they are treated as untrusted and
rendered double-quoted, never backtick-wrapped — this module's anchors are
backtick-delimited, and a human-typed name spelling a real anchor's inner
text could forge one if ever wrapped in backticks the same way. See
`tests/test_report.py::test_landing_view_marks_statuses_and_survives_mention_check`,
`tests/test_report.py::test_hostile_landing_name_cannot_forge_an_anchor_token`,
and
`tests/test_workspace.py::test_refine_report_reflects_landing_draft_not_fresh_machine_draft`
(the closed live repro: the tombstoned class's report section says it did
NOT land while still existing — labeled, not deleted — and the merged
draft's landing/tombstoned bullets name the right classes).

**Orphaned classes — a diagnostic, not a validation gate (2026-07-29 Argus
P1).** The merge is entry-level (see above): if a human ever edits the
tree-root container class itself (e.g. removing an attribute when rejecting
a class — exactly what a tombstoning refine does), rule 4 freezes that
container entry to `ours` on every subsequent refine, so a class the
machine adds or updates on a LATER refine lands in `classes:` but is never
wired into the container's `attributes` — unreachable from the tree root.
`validate_draft_generates` does not catch this (LinkML happily generates a
class nothing references). `ne.schema_bootstrap.refine.orphaned_classes`
detects it by BFS over `range:` references from the tree-root class,
surfaced as `BootstrapRunResult.orphaned_classes` and printed by the CLI as
a `WARNING:` line naming the orphaned classes. Always `[]` on a fresh run
(`emit_linkml` wires every class into the container unconditionally, by
construction — see
`tests/test_workspace.py::test_fresh_run_never_orphans_classes`). See
`tests/test_workspace.py::test_refine_container_freeze_orphans_a_later_machine_class`
for the reproduction and
`tests/test_refine.py::test_orphaned_classes_finds_unreachable_non_abstract_classes`
for the pure-function unit test.

**Malformed hand-edited state files raise `RefineError`, not a bare
traceback (2026-07-29 Argus P2, deepened round 5).** `--refine` reads three
hand-editable files (`schema-draft.yaml`, and — less commonly hand-touched
but still operator-writable — `schema-draft.machine.yaml` and
`tombstones.json`). A YAML syntax error, a draft that parses to something
other than a mapping, a `classes:`/`enums:` value that isn't a mapping, a
`classes:`/`enums:` mapping with a NON-STRING key (round 5 — an unquoted
`2024:`, an ordinary hand-edit mistake, previously crashed
`sorted()` inside the merge), a `tombstones.json` that is not a JSON object,
or a state file that exists but cannot be read as UTF-8 (round 5 — names
the path plus a byte position, never the bytes) all now raise `RefineError`
naming the offending path/key plus the parser's own position-only
diagnostic or a generic OS/decode detail, rather than an unhandled
`yaml.YAMLError`/`AttributeError`/`TypeError`/`json.JSONDecodeError`/
`OSError`/`UnicodeDecodeError` traceback bypassing the CLI's own reviewed
disclosure surface. **The full, authoritative shape-by-shape enumeration —
kept in exactly one place so it cannot drift — lives on `RefineError`'s own
docstring in `errors.py`, pinned by an AST raise-site test
(`tests/test_refine.py::test_refine_error_raise_sites_are_exactly_the_documented_shapes`)
rather than trusted prose.** A parallel sweep closed the same crash-instead-
of-error class across the workspace/staging filesystem machinery — see the
"Raw-exception escape sweep" section below.

**"Position-only" is enforced, not just claimed (2026-07-29 Argus P1,
round 2).** PyYAML's own `MarkedYAMLError.__str__`/`Mark.__str__` call
`Mark.get_snippet()`, which echoes back the surrounding SOURCE LINES from
the file being parsed — so naively formatting a `yaml.YAMLError` with
`f"...{exc}"` (round 1's approach) put a malformed draft's OWN CONTENT,
including any planted/sample-derived value near the broken line, on stderr.
`refine._position_only_message` instead reads only PyYAML's own generic
diagnostic text (`exc.problem`/`exc.context`, e.g. "expected ',' or ']',
but got ':'" — never derived from the input buffer) plus
`exc.problem_mark`/`exc.context_mark`'s `.line`/`.column` INTEGERS, never
calling `Mark.get_snippet()` or `str(mark)`. See
`tests/test_refine.py::test_refine_error_excludes_source_snippet_from_malformed_yaml`,
which plants two sentinel values on the broken line, asserts both are
absent from `str(RefineError(...))`, and independently confirms PyYAML's
raw `str(exc)` DOES contain one of them (so the test would fail if this
module ever reverted to relaying `exc` directly).

**A missing samples directory raises `EmptySampleError` naming the path,
not a bare traceback (2026-07-29 Argus P2).** `ne schema bootstrap
--engagement <slug>` with no samples directory yet — the exact shape the
CLI's own `--samples` help text advertises as the default
(`<workspace>/samples`) — previously let `profile_samples`'s own
`Path.iterdir()` raise an unhandled `FileNotFoundError`/
`NotADirectoryError` straight through `run_bootstrap` and the CLI.
`workspace._require_samples_dir` now checks `samples_dir.is_dir()` before
profiling starts (on both the fresh and `--refine` paths) and raises the
SAME `EmptySampleError` an existing-but-empty directory already raised —
from the operator's point of view, "no usable sample files found in
`<path>`" is equally true either way. See
`tests/test_workspace.py::test_missing_samples_directory_raises_empty_sample_error`
and
`tools/ne-cli/tests/test_schema_bootstrap_cli.py::test_bootstrap_missing_samples_directory_names_path`.

**`--refine`'s `.gitignore` guard is now unconditional AND content-aware
(2026-07-29 Argus P3, round 2 + round-4 P3-2 fix).** The fresh path always
wrote `.gitignore` before profiling; `--refine` did not, so a workspace
whose `.gitignore` was deleted, or one written by an older revision missing
the staging-directory entry, went through a full refine (including the
staging-directory commit machinery) with the guard absent or stale.
`_run_bootstrap_refine` now calls `_ensure_gitignore` too (after the three
required-state-file checks, so a `--refine` against an incomplete workspace
still fails with `RefineError` naming the missing file rather than
side-effecting a `.gitignore` write first). Round 2 shipped `_ensure_gitignore`
absent-only (it wrote the canonical content when the file was missing, but
never touched a file that already existed) — round 2's own prose here
already claimed it covered a file "absent **or stale**", which was not
true: a STALE file (one missing a required entry) was silently never
repaired. `_ensure_gitignore` is now CONTENT-AWARE, closing that gap for
real: an absent file still gets the canonical two-line content; a stale
file gets exactly the MISSING entries appended, preserving every existing
line — including human-added ones — verbatim and in order; a file that
already carries both required entries is left completely untouched (no
write at all). See
`tests/test_workspace.py::test_ensure_gitignore_repairs_stale_and_preserves_human_lines`
(direct unit coverage of all four shapes) and the stale-repair assertion
added to `tests/test_workspace.py::test_refine_human_edit_survives` (proof
the refine PATH actually repairs a stale file end to end, not just the
function in isolation).

### Residual #1 (reserved-key collision) — dispositioned, not reachable via inference

U2 declared that a class named `OperationalEntity` or `<Engagement>RecordSet`
silently overwrites the provenance base / tree-root container INSIDE
`emit_linkml`'s own dict construction, given an arbitrary caller-built
`CandidateModel`. This CLI reaches `emit_linkml` ONLY via
`infer_candidate_model` — both on a fresh run and for the machine ("theirs")
side of `--refine` — whose class names are always
`class_name_for_table(stem) = CamelCase(stem) + "Record"` (never bare
`OperationalEntity`) and whose record-set container name is
`to_camel_case(engagement) + "RecordSet"`, built from an `engagement` value
`_validate_engagement` has already forced to match
`^[a-z][a-z0-9-]{0,62}$` — neither can mechanically produce the literal
reserved names. **`emit_linkml`'s own collision is therefore unreachable
through this CLI's automatic inference path**, on both the fresh and refine
machine sides.

`--refine`'s three-way merge is a SEPARATE mechanism from `emit_linkml` and
is not covered by the sentence above: it never calls `emit_linkml` on the
human's edited YAML text at all, so a human who hand-renames one of their
own classes' YAML keys in `schema-draft.yaml` to the literal string
`OperationalEntity` (or the engagement's `RecordSet` name) is merging a
same-named entry into `three_way_merge`, which treats every class purely by
its dict key — rule 4 (human wins) would let that renamed content silently
replace the reserved entry in the merged draft. This is a NEW, narrower
surface than U2's residual, not yet proven safe or unsafe by a test in this
unit — declared here rather than asserted away, per
[[written-rules-disarm-review]]. It requires a human to deliberately type a
reserved identifier as a class name inside a file whose whole purpose is
manual review before landing, so it is judged low-risk; closing it (e.g. by
extending `_validate_no_derived_name_collisions`-style checking to
`three_way_merge`'s output) is left as a follow-up rather than in scope
here.

**One probed shape is now proven, not just plausible (2026-07-29 round 4,
Argus P3-1).** The round-3 commit message asserted that this NEW surface's
collision is unreachable because "a hand-edited collision would fail the
round-trip gate and write nothing" — a UNIVERSAL claim (every hand-edited
collision) stated with no supporting test, while this README's own hedge
above says the opposite: not yet proven safe or unsafe. That commit-message
phrasing is hereby corrected: it is narrowed to match the hedge, not the
other way around. What round 4 actually proves is ONE shape — Argus's own
live probe — locked by
`tests/test_workspace.py::test_refine_reserved_name_collision_one_probed_shape_fails_gate_and_writes_nothing`:
a human renames a class key to the literal `OperationalEntity` and repoints
the tree-root container's attribute range at it; `--refine` raises
`DraftGenerationError` (Validation #2, the merged-draft round-trip gate) and
the workspace is left byte-identical to before the call (validate-before-write
held; nothing landed), preceded by a passing control (an unedited refine
still succeeds). This proves that ONE shape fails safely. It does **not**
prove the universal claim — a different collision shape (e.g. one that
happens to leave the merged draft still linkml-generatable, just
semantically wrong) is not ruled out, and no such claim is made here or in
that test's own docstring.

### Raw-exception escape sweep (2026-07-29 round 5)

**The class, not the two reported instances.** Argus's round-5 review found
TWO crash-instead-of-error findings in the SAME shape: `_safe_load_draft`
validated `classes:`/`enums:`'s CONTAINER type but not its KEY type (a raw
`TypeError` from `sorted()`), and `_clear_staging` deleted anything at
`.bootstrap-staging/` unconditionally (a raw `IsADirectoryError`/
`PermissionError` for a foreign subdirectory). Two independent findings of
one class in one review is the signal that the class, not the instances,
needed sweeping — this round enumerated every point in the U3 surface
(`refine.py`'s three-way merge, `workspace.py`'s staging/commit/gitignore/
state-file I/O, and the CLI relay, which already catches
`SchemaBootstrapError` uniformly — see `tools/ne-cli/src/ne/cli/schema.py`)
where a hand-edited YAML/JSON shape or an operator-visible filesystem state
could crash with a raw traceback instead of a typed
`SchemaBootstrapError` naming the offending path/key.

**Method.** For every place this package (a) parses operator-writable text
or (b) touches an operator-visible filesystem path, ask: does the TYPE
check already in place also cover the CONTENT/SHAPE a hand-edit or a
crash-recovery run could produce? Where the answer was no, either convert
the escape to a typed error naming the path/key, or state explicitly in the
function's docstring that the shape is UNHANDLED and why (an internal-only
caller contract, not an operator-facing surface).

**Disposition table:**

| Escape point | Disposition |
|---|---|
| `classes:`/`enums:` non-mapping value (container type) | Already typed (`RefineError`, C1 fix, round 4) |
| `classes:`/`enums:` non-string key (Argus P2, round 5) | **NEW typed** — `RefineError` naming side/section/key (`refine._safe_load_draft`) |
| A YAML complex key (`? [a, b]`) under `classes:`/`enums:` | Admitted-and-handled — already inside the existing `yaml.YAMLError` catch (PyYAML's own `ConstructorError`); pinned by a new test, not a new code path |
| A non-string SCALAR key at the TOP LEVEL (outside `classes:`/`enums:`) | Docstring-declared admitted, non-crashing shape — `_merge_scalar_keys` has no type check and needs none (`sorted()` is never called on top-level keys) |
| `find_tree_root_class`/`merged_expected_names`/`orphaned_classes` given a caller-built (not `three_way_merge`-produced) mapping with non-string keys | Docstring-declared UNHANDLED shape (raw `TypeError` from `sorted()`) — every internal caller only ever passes already-validated `three_way_merge` output |
| `parse_merged_yaml` given text this package did not itself serialize | Docstring-declared UNHANDLED shape (raw `yaml.YAMLError`) — every internal caller only ever passes `three_way_merge` output |
| A state file (`schema-draft.machine.yaml`/`schema-draft.yaml`/`tombstones.json`) missing | Already typed (`RefineError`, `_require_state_file`) |
| A state file present but not readable as UTF-8 (permission, race, or a non-UTF-8 hand-edit) | **NEW typed** — `RefineError` naming the path + byte position (`workspace._read_state_text`) |
| `tombstones.json` malformed JSON / not an object / non-string list entries | Already typed (`RefineError`, `_load_tombstones`) |
| `.bootstrap-staging/` containing a subdirectory this module never creates (Argus finding 4, round 5) | **NEW typed** — `WorkspaceIOError`, REFUSED not deleted (`_clear_staging`) |
| `.bootstrap-staging` (or the workspace dir, or `.gitignore`) occupied by the wrong filesystem shape (a file where a directory is expected, or vice versa) | **NEW typed** — `WorkspaceIOError` naming the path (`_clear_staging`/`_ensure_gitignore`) |
| Any remaining `OSError` from `mkdir`/`write_text`/`read_text`/`replace`/`unlink`/`rmdir`/`iterdir` in the staging/commit/gitignore machinery | **NEW typed** — `WorkspaceIOError` wrapping `_io_detail(exc)` at every call site |
| A directory squatting a run artifact's landing name (e.g. `inference-report.md/`) | **NEW typed** — `WorkspaceIOError` at `staged_path.replace(dest)` (`_write_staged_and_commit`) |
| An invalid `engagement` slug | **NEW ordering** — validated FIRST, before any workspace write (`run_bootstrap`, was previously validated only inside `emit_linkml`, after `.gitignore` was already written on a fresh run) |
| A tree-root cardinality failure | Already typed (`RefineError`, `find_tree_root_class`) |
| `_find_repo_root`'s unreadable-`pyproject.toml` `PermissionError` (`tools/ne-cli/src/ne/cli/checks.py`) | **Declared residual, out of scope** — a shared CLI helper outside this unit's `ne.schema_bootstrap` surface; not touched here |

**Enumeration pinned by an AST scan, not trust.** Every `raise RefineError`/
`raise WorkspaceIOError` site in `refine.py`/`workspace.py` is enumerated by
name in `RefineError`'s own docstring (`errors.py`) and locked by
`tests/test_refine.py::test_refine_error_raise_sites_are_exactly_the_documented_shapes`
— a new, unenumerated raise site turns that test RED until the docstring is
updated, so the enumeration cannot silently drift the way the "exactly four
shapes" claim did across three separate restatements before this round.

**That enumeration test pins WHERE the typed errors are raised — it does not
pin that nothing ELSE is ever raised (NE-ESB-CLI continuation, 2026-07-29/30).**
A probe planted an uncalled `raise ValueError("probe")` in `workspace.py` and
every existing test, including the enumeration test above, stayed GREEN —
proof that a brand-new raw raise in an unexercised branch of
`refine.py`/`workspace.py` could ship completely undetected (row 67 below).
`tests/test_refine.py::test_raw_exception_sweep_is_complete_no_untyped_raise_sites`
closes that gap: it AST-scans every module of the package (row 72 below —
widened from the original two-file scope of `refine.py`/`workspace.py` to
every submodule reachable via `pkgutil.iter_modules`, PLUS the package's own
`__init__.py`, which `iter_modules` never yields on its own and had to be
added back in explicitly — see `_package_modules_to_scan`'s docstring) for
every `raise` (a bare re-raise, which re-propagates a caught raw exception,
is treated as a hard failure too) and asserts the raised names are a subset
of this package's own typed errors — derived from `errors.py`, a module none
of the scanned files' edits can touch, so a new raw raise cannot auto-satisfy
the check the way an edit to the same file could. `errors.py` itself is
excluded from the scan (it is the source of the allowed set, not a target of
it) but is independently known raise-free outside its own docstring.

## Public API

```python
from pathlib import Path
from ne.schema_bootstrap import profile_samples, infer_candidate_model

bundle = profile_samples(Path("./discovery/acme/samples"))
model = infer_candidate_model(bundle)

print(model.model_dump_json(indent=2))
# -> CandidateModel: classes (with slots, FK edges, junction flags, seed
#    anchors) and enums, inferred deterministically from the sample.
```

Exported from `ne.schema_bootstrap`:

- `BootstrapConfig`, `DEFAULT_CONFIG` — every tunable threshold.
- `profile_samples(path, *, config=DEFAULT_CONFIG) -> ProfileBundle`
- `ProfileBundle` — `.profile: SampleProfile` (serializable) +
  `.column_values` / `.fk_target_membership` (both in-memory only,
  non-serializable; `.fk_target_membership` is the bounded, exact
  FK-containment evidence — see the data-handling section above).
- `SampleProfile`, `TableProfile`, `ColumnProfile` — the bounded profile
  types.
- `infer_candidate_model(bundle, *, config=DEFAULT_CONFIG) -> CandidateModel`
- `CandidateModel`, `CandidateClass`, `CandidateSlot`, `CandidateEnum`,
  `CandidateEdge` — the typed inference output.
- `SchemaBootstrapError`, `SampleFormatError`, `EmptySampleError`.
- `DuplicateClassNameError` — raised by `infer_candidate_model` when two
  source tables mechanically produce the same class name; carries both
  table stems.
- `DuplicateSlotNameError` — raised by `infer_candidate_model` when two
  columns on the SAME source table mechanically produce the same slot name
  (e.g. `first-name` and `first_name` both normalize to
  `mechanical_name="first_name"`); carries the table name and both column
  headers.
- `scrub_text` — the local credential-shape scrubber.
- `emit_linkml(model, *, engagement, config=DEFAULT_CONFIG) -> EmittedSchema` (U2)
  — the house-dialect LinkML emitter.
- `EmittedSchema` — `.yaml_text`, `.top_class`, `.schema_name`,
  `.emitted_enums`, `.declined_enums`, `.provenance_mapped`.
- `validate_draft_generates(yaml_text, *, repo_root, top_class,
  expected_classes, expected_enums, expected_edges=None) -> RoundTripArtifacts`
  (U2) — the real-codegen-venv round-trip proof.
- `RoundTripArtifacts` — `.pydantic_module`, `.json_schema`, `.manifest`,
  `.graph_module` (the generated artifact TEXTS).
- `render_inference_report(profile, model, emitted, *, engagement,
  config=DEFAULT_CONFIG, landing=None) -> str` (U2; `landing` added U3 round
  4) — the total-traceability report. `landing: RefineLanding | None` is
  `None` on a fresh run (byte-identical to before the round-4 fix); on
  `--refine`, passing the same call's `RefineLanding` adds a leading
  "## What landed (--refine)" section plus a per-class/per-enum "landing
  status" line, so the report states explicitly which draft each part of it
  is evidence for — see "U3" below.
- `RefineLanding` (U3 round 4) — `.class_names`, `.enum_names`,
  `.tombstoned_classes`, `.tombstoned_enums`, `.human_kept`,
  `.machine_updated`, `.blocked_resurrections`, `.human_readded`,
  `.orphaned_classes`; the view `render_inference_report`'s `landing`
  parameter takes, built directly from one `--refine` call's `MergeResult` +
  `merged_expected_names` + `orphaned_classes` — see "U3" below.
- `expected_anchors(model, emitted) -> dict[str, str]` /
  `verify_mention_completeness(report, model, emitted) -> None` (U2) — the
  §5.1 self-check, independently callable.
- `render_pr_body(model, emitted, *, engagement, config=DEFAULT_CONFIG,
  round_trip_verified=None) -> str` (U2) — the `pr-body.md` emitter.
  `round_trip_verified` states ONLY what the CALLER proved:
  `True`/`False` after actually observing `validate_draft_generates`
  succeed/fail for this exact draft, `None` (the default) when it was not
  run before this call — the function never runs the round-trip itself and
  never claims more than it was told.
- `InvalidClassNameError`, `InvalidEngagementNameError`,
  `DraftGenerationError`, `MentionCompletenessError` (U2).
- `DuplicateRecordSetSlotNameError` (U2 residual #2, fixed in U3) — raised by
  `emit_linkml` when two source tables' `to_snake_case(source_table)` keys
  collide on the top-level record-set container (see `errors.py`'s
  docstring). Present in `emit_linkml` since NE-ESB-EMIT but absent from
  this list/`__all__` until now — `from ne.schema_bootstrap import
  DuplicateRecordSetSlotNameError` would previously raise `ImportError`.
  Closed with an `__all__`-parity ratchet, not just an added export, so it
  cannot silently drift again — see
  `tests/test_boundaries.py::test_public_api_all_matches_expected_list`.
- `run_bootstrap(samples_dir, workspace_dir, *, engagement, repo_root,
  config=DEFAULT_CONFIG, refine=False) -> BootstrapRunResult` (U3) — drives
  the U1/U2 pipeline end to end into a workspace; see "U3" below.
- `BootstrapRunResult` — `.workspace`, `.refined`, `.files_written`,
  `.table_count` (source tables profiled — `len(bundle.profile.tables)`, not
  the same expression as `.class_count`, see the class docstring's
  2026-07-29 note)/`.class_count`/`.enum_count`/`.edge_count` (this
  invocation's machine run — see the class docstring for the `--refine`
  caveat), `.merge` (`MergeResult | None`, present only on `--refine`),
  `.orphaned_classes` (`list[str]`, always `[]` on a fresh run — see "U3"
  below).
- `three_way_merge(base_text, ours_text, theirs_text, *,
  tombstoned_classes, tombstoned_enums) -> MergeResult` (U3) — the pure,
  deterministic `--refine` merge; see "U3" below for the six-rule order.
  Raises `RefineError` if any of `base_text`/`ours_text`/`theirs_text` is
  not valid YAML, does not parse to a mapping at the top level, has a
  non-mapping `classes:`/`enums:` value, or a non-string key under those
  sections (2026-07-29 round 5).
- `MergeResult` — `.merged_yaml_text`, `.tombstoned_classes`,
  `.tombstoned_enums`, `.human_kept`, `.machine_updated`,
  `.blocked_resurrections`, `.human_readded`.
- `orphaned_classes(merged, top_class) -> list[str]` (U3) — every
  non-abstract class in a parsed merged draft not reachable from
  `top_class` by following `range:` references; a diagnostic, not a
  validation gate (see "U3" below).
- `WorkspaceExistsError`, `RefineError`, `WorkspaceIOError` (U3, the last
  NEW in round 5). The authoritative, shape-by-shape enumeration of what
  `RefineError` is raised for lives in ONE place —
  [`RefineError`'s own docstring in `errors.py`](src/ne/schema_bootstrap/errors.py)
  — rather than restated here; see the "Raw-exception escape sweep" section
  below for how that enumeration is kept honest. `WorkspaceIOError` is
  raised for operator-visible filesystem failures or shape conflicts in the
  workspace + `.bootstrap-staging/` machinery (a foreign subdirectory where
  this module only ever writes plain files, a non-directory/non-regular-file
  occupying a directory-shaped/`.gitignore` name, a directory occupying a
  run-artifact's landing name, or any wrapped `OSError`) — carries only
  paths and a generic OS/shape detail, never file content.

## Scope (U1+U2+U3)

These three units ship the package skeleton, the profiler, the deterministic
inference core, the LinkML emitter, the real-codegen-venv round-trip
validator, the inference-report/pr-body renderers, and the `ne schema
bootstrap` CLI + workspace + `--refine` merge loop. Explicitly **not**
built yet:

- Live-database sampling via `ne.connect.sql` (U4).
- LLM-assisted semantic labeling (U5) — `--llm` is wired into the CLI and
  fails closed (`click.UsageError`, zero egress) until U5 lands.
- Unstructured/prose discovery (U6).
- The `GraphSourceMapping` draft + landing runbook (U7).

## Guard mutations verified (U1)

Per the orchestrator's quality bar, every threshold/security guard below was
manually mutated in source, observed to fail its named test (RED), then
reverted. Rows 1-7 were first recorded 2026-07-27; rows 8-12 were added
during the 2026-07-28 pre-review hardening pass; rows 13-19 were added
during a subsequent Argus adversarial-review pass (same date), which found
and fixed five additional real defects: an unreachable `shape:national_id`
branch, a phone-shape heuristic over-firing on bare digit runs, an untyped
`UnicodeDecodeError` leaking raw file bytes, a silently-collapsing duplicate
column/table name, and a cardinality-trivial sampling-determinism test. Rows
20-26 were added during a SECOND Argus adversarial-review pass on the
resulting branch (also 2026-07-28), which found and fixed a headline-output
correctness bug (FK containment measured against a capped sample, silently
rejecting the common case of a target table above the row cap), an unbounded
min/max retention/scrub gap plus a secondary non-finite-float ("inf") leak on
the same code path, a fully-unbounded file-ingestion path (the reader used to
materialize an entire file before any row cap applied, contrary to its own
docstring's claim), a latent false-positive in the boundary guardian, and
converted `seeds.py`'s untested "verified at build time" transcription claim
into a hard parity test (mirroring the hygiene-copy guard from row 11).

Round 2's fix (row 20) DIRECTLY CREATED a round-3 P1 in the same commit: it
introduced a per-column, per-table `RawTable`/`ProfileBundle` field carrying
the FULL (uncapped) identifier distinct-value set — UNBOUNDED — defended in
prose as "bounded to a small number of columns" — true of column COUNT,
false of column CARDINALITY — with the memory test's own fixture
deliberately built to avoid any identifier column (dodging the failing case
rather than proving the bound). Three review rounds each surfacing a NEW P1
at flat severity triggered a Fable arbiter at max reasoning (SPEC §6.1.1,
binding ruling). The arbiter found the package DECOMPOSITION sound
(`CandidateModel`, what U2-U7 inherit, had essentially zero findings across
all three rounds) and ordered a targeted restructure of exactly two seams,
not a package rework: **Seam 1** replaces that carrier with
`ProfileBundle.fk_target_membership` — bounded, two-phase, EXACT
FK-containment evidence (phase 1 samples with no identifier collection at
all; phase 2 re-streams only the specific targeted identifier column of the
specific targeted file, testing membership of the deduplicated sampled
SOURCE values — `O(#fk-columns x rows_per_table)`, never the target's
cardinality).
**Seam 2** funnels every value-bearing `ColumnProfile` field through one
retention choke point (`profiles._retain_value`), guarded by a
reflection-shaped test that derives the value-bearing field set FROM the
pydantic schema rather than a hardcoded list (`min_value`/`max_value` had
been fixed TWICE, independently, for parallel reasons — the recurring
symptom the choke point eliminates structurally). Rows 27-34 below are the
mutations verifying these two seams plus the arbiter's five in-pass items
(numeric-vs-phone PII gating, duplicate class names, over-wide rows, the
dead substring loop, and the STRIKE below).

A Codex adversarial-review pass on this arbiter-directed branch (also
2026-07-28) found and fixed three further real defects, all fixed in-round
per the SPEC §6.1.1 stopping rule (none were in a BLOCKING class — no
unbounded retention, no guard satisfiable by the change it forbids — but
each was a silent-wrong-answer risk on realistic input, so fixed rather than
filed): a `TypeError` crash on a datetime column mixing timezone-aware and
naive ISO-8601 values (`_min_max_raw` now orders by a normalized-to-naive-UTC
key instead of comparing the raw objects directly); `distinct_values`
deduplicating only the RAW pre-retention set, so two values that become
identical after scrub/truncate could both survive into a supposedly
DISTINCT list (dedup now happens on the RETAINED value); and two column
headers on one table that mechanically normalize to the same
`mechanical_name` (e.g. `first-name` / `first_name`) being silently emitted
as two colliding slots instead of refused, mirroring the existing
duplicate-class-name check (rows 35-37).

**STRIKE — stale finding retracted (2026-07-28, this arbiter-directed
pass):** the round-3 ledger (prior to this pass) contained an entry claiming
`shape:national_id` was unreachable dead code. The arbiter re-ran it against
HEAD and confirmed SSN-shaped columns DO correctly return `shape:national_id`
— that defect was fixed in round 1 (see row 13 above, which added the
`NATIONAL_ID_PATTERN` exclusion to `_is_phone_shaped`) and proven by
`tests/test_pii.py::test_value_shape_flags_opaque_national_id_shaped_column`,
which has been green ever since. The round-3 finding was stale at the time
it was filed; it does not describe the code in this or any prior committed
state.

**NE-ESB-EMIT REVISE round (2026-07-28), rows 38-41:** Argus found the
enum-value "safety charset" (`_ENUM_VALUE_SAFE`) was a single regex whose
docstring claimed to be "calibrated against the REAL generator ... the test
is the oracle, not a guess" while NO test backed that claim at all — and
Argus disproved it directly, showing at least 17 real value shapes the
regex wrongly declined generate fine, while the regex admitted at least one
shape (whitespace-collision member drops) that silently corrupts the
generated enum. This round replaced the single regex with six
evidence-derived rules (`_enum_decline_reason`), each tagged EMPIRICAL (tied
to a real observed generator failure) or CONSERVATIVE (declined out of
caution, not because it actually breaks), fixed the collateral P2 (digit-
leading sample stems produced an unbuildable class name with no early
error) and three P3s (an inaccurate sanitizer-boundary inventory sentence,
an under-controlled instantiation-proof test, and a stale README API
listing). Rows 38-41 below are the mutations verifying the P1/P2/P3-2 fixes.

**NE-ESB-EMIT CONTINUATION (2026-07-28), rows 42-43:** the R4 rule above
calls `unicodedata.name()` — the same call the real generator's
`generate_enum_label` member-name mangle makes, chosen deliberately over a
hand-rolled control-character table so the gate PREDICTS real generator
behaviour instead of guessing at CPython's own Unicode tables. That import
is new and was correctly caught by the boundary guardian
(`tests/test_boundaries.py`), which had no allowance for it — an
orchestrator brief gap (the file governing imports wasn't in `mayTouch`),
not a build defect; ORCHESTRATOR RULING (a) added the allowance to
`emit.py`'s existing per-module entry (narrowest placement — NOT the global
`_STDLIB_ALLOWLIST`, closing off the NE-VG-2 over-broad-allowlist shape).
Rows 42-43 are the mutations verifying that placement plus a re-emphasis of
the R6 silent-member-drop guard (the most important finding this unit
produced: a gate that silently loses a customer's enum member is worse than
one that declines it loudly).

| # | Mutation | Guard / named test | Result |
|---|---|---|---|
| 1 | `containment_threshold` default `0.95` → `0.0` (effectively disables the FK-containment floor) | `tests/test_infer_fk_junction.py::test_fk_containment_below_threshold_rejected` | RED — an 18/20 (0.90) containment column produced an edge that should have been rejected |
| 2 | `enum_max_distinct` default `24` → `1_000_000_000` | `tests/test_infer_enums.py::test_enum_too_many_distinct_rejected` | RED — a 25-distinct column was accepted as an enum |
| 3 | Junction rule relaxed to "at least two FK-candidate columns present" (`is_junction = sum(1 for col in remaining if col.name in fk_by_col) >= 2`, dropping the "exactly two, all-FK" exactness) | `tests/test_infer_fk_junction.py::test_junction_extra_payload_column_not_flagged` | RED — a table with two FK columns *plus* an unrelated payload column (`role`) was incorrectly flagged as a junction. **2026-07-28 correction:** the row-3 mutation as previously worded (`len(remaining) == 2` → `len(remaining) >= 2`, keeping the existing `all(...)` exactness check) was re-executed and does **not** reproduce RED for this test — the retained `all()` clause already rejects the `role`-augmented table regardless of the `==`/`>=` change, since `role` is never an FK column. The wording above is the mutation that actually isolates and breaks the "exactly two" guard; the guard itself was reconfirmed to bite. |
| 4 | PII exemplar/distinct-value suppression branch disabled (`if pii_flag:` → `if False:`) | `tests/test_pii.py::test_pii_exemplars_absent_from_serialized_profile` | RED — raw flagged-column values (`userN@customer-example.com`) appeared in the serialized profile JSON |
| 5 | `scrub_text` made an identity function (no redaction) | `tests/test_pii.py::test_credential_exemplar_scrubbed_in_serialized_profile` | RED — a raw AWS-shaped credential (`AKIA...`) appeared in the serialized profile JSON instead of `[REDACTED]` |
| 6 | Sampling RNG changed from `random.Random(seed_from_table_name)` to unseeded `random.Random()` | `tests/test_determinism.py::test_pipeline_byte_determinism` | RED — profile JSON diverged across repeated runs over the same directory |
| 7 | Seed-anchor thresholds `anchor_min_matching_attrs` `2`→`0` and `anchor_min_overlap_ratio` `0.5`→`0.0` | `tests/test_seed_anchoring.py::test_weak_overlap_does_not_anchor` | RED — a table sharing only `status` with three seed classes was force-anchored instead of left unanchored |
| 8 (2026-07-28) | `import socket` inserted at the top of `infer.py` | `tests/test_boundaries.py::test_every_module_imports_only_stdlib_pydantic_or_self` and `::test_forbidden_surfaces_never_appear` | RED — both AST-allowlist tests flagged the planted import; the boundary guardian genuinely bites |
| 9 (2026-07-28) | PII min/max suppression gate reverted to unconditional `_min_max(...)` (`min_value, max_value = (None, None) if pii_flag else _min_max(...)` → `_min_max(non_null, inferred_type)` unconditionally) | `tests/test_pii.py::test_pii_min_max_suppressed_in_serialized_profile` | RED — the real min/max birth dates of a `dob` column reappeared in `min_value`/`max_value` |
| 10 (2026-07-28) | `_read_delimited`'s `except csv.Error` wrapper removed (raw stdlib exception left to escape) | `tests/test_readers.py::test_oversized_delimited_cell_raises_sample_format_error` | RED — a >131072-char CSV cell raised uncaught `_csv.Error` instead of the typed `SampleFormatError` |
| 11 (2026-07-28) | `hygiene.py`'s AWS-key pattern (`_PATTERNS[0]`) deleted from the **local copy only** (ne-flywheel's copy left untouched) | `tests/test_hygiene.py::test_pattern_set_matches_flywheel_copy` | RED — the ordered pattern-literal lists diverged between the two copies |
| 12 (2026-07-28) | `_is_phone_shaped`'s `_ISO_DATE_SHAPE` exclusion removed (real defect found during this review: ISO-8601 dates like `2024-03-01` satisfy `PHONE_PATTERN`'s digit/dash charset and have ≥7 digits, so an ordinary date column was being silently PII-flagged as `shape:phone` and having its exemplars/distinct-values/min-max suppressed) | `tests/test_pii.py::test_iso_date_shaped_values_not_falsely_flagged_as_phone` | RED — an `opened_at` column of plain ISO dates was flagged `True`/`shape:phone` instead of `False`/`None` |
| 13 (2026-07-28, Argus P2) | `_is_phone_shaped`'s `NATIONAL_ID_PATTERN` exclusion and separator/`+` requirement both removed (real defect: an SSN-shaped value clears `PHONE_PATTERN`'s charset/length floor too, and the phone check ran first with no exclusion, so `shape:national_id` was permanently unreachable dead code) | `tests/test_pii.py::test_value_shape_flags_opaque_national_id_shaped_column` and `::test_bare_digit_run_not_flagged_as_phone` | RED — an SSN-shaped opaque column was labeled `shape:phone` instead of `shape:national_id`, and a 7-digit order-number column was flagged `True` instead of `False` |
| 14 (2026-07-28, Argus P2) | `_is_phone_shaped` regressed to the pre-fix form (no national-id/separator exclusions) | `tests/test_profiles.py::test_min_max_retained_for_bare_digit_run_column_not_falsely_pii_flagged` | RED — reproduced the amplified regression exactly: `order_number` (a literal `Order` attribute in `seeds.py`) came back `pii_flag=True`, `pii_reason='shape:phone'`, `min_value=None`, `max_value=None`, `exemplars=[]` — every value-bearing field blanked on a genuinely non-PII column |
| 15 (2026-07-28, Argus P1) | `_read_delimited`'s outer `except UnicodeDecodeError` wrapper removed | `tests/test_readers.py::test_non_utf8_delimited_file_raises_sample_format_error_no_pii_in_repr` | RED — a Windows-1252/latin-1 CSV (`Jos\xe9 Garcia,123-45-6789`) raised a raw `UnicodeDecodeError` whose `.object`/`.args[1]` carried the entire raw byte buffer, including the SSN |
| 16 (2026-07-28, Argus P1) | `_read_jsonl`'s `except UnicodeDecodeError` wrapper removed | `tests/test_readers.py::test_non_utf8_jsonl_file_raises_sample_format_error` | RED — raw `UnicodeDecodeError` escaped instead of typed `SampleFormatError` |
| 17 (2026-07-28, Argus P3) | The duplicate-column-name check (`len(set(header)) != len(header)`) removed from `_read_delimited` | `tests/test_readers.py::test_duplicate_column_names_raise_sample_format_error` | RED — a CSV with header `id,val,val` was silently accepted instead of refused, which (pre-fix) would have collapsed `ProfileBundle.column_values`'s last-wins keying and corrupted FK containment for the shadowed column |
| 18 (2026-07-28, Argus P3) | The duplicate-table-name check (`seen_table_names`) removed from `read_sample_directory` | `tests/test_readers.py::test_duplicate_table_names_across_formats_raise_sample_format_error` | RED — `a.csv` + `a.jsonl` in one directory was silently accepted instead of refused, which (pre-fix) would have silently dropped the first file's table |
| 19 (2026-07-28, Argus P3) | `_seed_for` changed to hash only `table_name[:1]` (first character) instead of the full name | `tests/test_sampling.py::test_seed_derives_from_table_name_different_names_differ_for_every_pair` | RED — `table_a`/`table_b` (shared leading `"t"`) selected the *same* reservoir; this is exactly the regression the original `differing >= 1` assertion (4/5 pairs still differ) would have missed, which is why it was tightened to a per-pair assertion |
| 20 (2026-07-28, Argus round 2 P1) | `_try_fk_edge`'s containment reverted to measuring against the SAMPLED `bundle.column_values` instead of the (then-current) full-value-set carrier this row's fix introduced (superseded by `fk_target_membership` — see rows 27-28) | `tests/test_infer_fk_junction.py::test_fk_full_containment_holds_when_target_table_exceeds_row_cap` | RED — a 5,000-row target table with every FK value genuinely valid measured ~4% containment (200 sampled / 5000 total) instead of 1.0, and produced zero edges |
| 21 (2026-07-28, Argus round 2 P2) | `_min_max`'s scrub/truncate wrapper removed (returned the raw `str(min(...))`/`str(max(...))` unbounded) | `tests/test_profiles.py::test_min_max_bounded_and_scrubbed_in_serialized_profile` | RED — a legitimate 4,000-digit `int` value reached `max_value` raw and unbounded (4000 chars, not ≤80) and appeared verbatim in the serialized profile |
| 22 (2026-07-28, Argus round 2 P2) | `_is_float`'s `math.isfinite` guard removed (accepted any successful `float()` parse, including `inf`) | `tests/test_profiles.py::test_min_max_excludes_non_finite_overflow_value_no_inf_leak` | RED — a 300,000-digit numeral (beyond `int()`'s own digit limit, but `float()`-parseable to `inf`) became `max_value == "inf"`, a silently wrong value |
| 23 (2026-07-28, Argus round 2 P2) | `_read_delimited`'s lazy row generator forced into a full list (`list(_rows())`) before `sample_stream`, defeating the streaming property | `tests/test_readers.py::test_large_delimited_file_bounds_memory_not_row_count` | RED — peak traced memory for a 50,000-row file jumped from ~0.1 MB to ~10.2 MB, over the 3 MB bound |
| 24 (2026-07-28, Argus round 2 P3) | The delimited UTF-8 wrapper's `from None` reverted to `from exc` | `tests/test_readers.py::test_non_utf8_delimited_error_severs_exception_chain` | RED — `excinfo.value.__cause__` was the raw `UnicodeDecodeError` (carrying the full offending byte buffer) instead of `None` |
| 25 (2026-07-28, Argus round 2 P3) | `_check_source`'s allowed-name carve-out removed (substring check ran unconditionally again) | `tests/test_boundaries.py::test_checker_allows_absolute_self_import_despite_ne_schema_substring` | RED — an absolute self-import (`ne.schema_bootstrap.model`) was falsely flagged as importing the forbidden `ne.schema` surface, purely from incidental substring overlap |
| 26 (2026-07-28, Argus round 2 P2) | `SEED_CLASS_SIGNATURES["Account"]` had `"name"` deleted (local copy only; `operational.yaml` left untouched) | `tests/test_seeds_parity.py::test_seed_class_signatures_match_operational_yaml` | RED — the local transcription diverged from `operational.yaml`'s actual `Account` attribute set |
| 27 (2026-07-28, SPEC §6.1.1 arbiter-directed, Seam 1) | `_try_fk_edge`'s containment measured against `bundle.column_values` (the SAMPLED target values) instead of `bundle.fk_target_membership` (the bounded, exact phase-2 evidence) | `tests/test_infer_fk_junction.py::test_fk_full_containment_holds_when_target_table_exceeds_row_cap` | RED — a 5,000-row target table with every FK value genuinely valid produced ZERO edges (`assert 0 == 1`) instead of one edge at containment 1.0 |
| 28 (2026-07-28, SPEC §6.1.1 arbiter-directed, Seam 1) | Phase-2 collection retained the FULL target-column distinct set instead of only the probed values (dropped the `in probe` membership test and the early exit) | `tests/test_infer_fk_junction.py::test_fk_pipeline_memory_bounded_with_over_cap_identifier_target` | RED — peak traced memory for the 50,000-row over-cap target jumped to 6,997,106 bytes, over the 3,000,000-byte bound |
| 29 (2026-07-28, SPEC §6.1.1 arbiter-directed, Seam 2) | `_retain_value`'s `if pii_flag: return None` gate removed (always scrubs, never suppresses) | `tests/test_retention_choke.py::test_every_value_bearing_field_blanked_when_pii_flagged` and `tests/test_pii.py::test_pii_min_max_suppressed_in_serialized_profile` | RED (both) — a name-flagged, ORDERABLE `dob` column's real birth dates (e.g. `'2001-06-06'`, `'1961-03-04'`) reappeared in `min_value`/`max_value` instead of `None`. (Confirms the reflection guard's fixture must include an orderable PII column — `email`/`reachpoint`, both non-orderable `string` type, have `min_value`/`max_value` already `None` from `_min_max_raw` regardless of this gate, so `dob` is what makes the guard non-vacuous for this mutation.) |
| 30 (2026-07-28, SPEC §6.1.1 arbiter-directed, Seam 2) | `_retain_value` made an identity function on the non-PII path (returned `raw` directly, skipping `scrub_text`/truncation) | `tests/test_profiles.py::test_min_max_bounded_and_scrubbed_in_serialized_profile` and `tests/test_profiles.py::test_exemplars_truncated_to_max_chars` | RED (both) — a legitimate 4,000-digit `int` `max_value` reached the profile unbounded (`assert 4000 <= 80` failed), and a 200-char exemplar was retained unbounded |
| 31 (2026-07-28, SPEC §6.1.1 arbiter-directed, Seam 2 guard) | `test_retention_choke.py`'s `_METADATA_EXCLUSIONS` widened to also exclude `"min_value"` (weakening the reflection derivation itself) | `tests/test_retention_choke.py::test_derived_fields_include_all_four_known_value_bearing_fields` | RED — the derived-set superset meta-assertion failed (`'min_value'` no longer in the derived set), proving the meta-assertion actually constrains the derivation and isn't vacuously satisfied by an empty/narrow set |
| 32 (2026-07-28, SPEC §6.1.1 arbiter-directed, in-pass) | `pii_flags`'s `inferred_type != "string"` gate removed (value-shape checks ran regardless of type again) | `tests/test_pii.py::test_numeric_amount_and_balance_columns_not_flagged_as_phone` (RED) / `tests/test_pii.py::test_phone_shaped_string_column_still_flags` (control, stayed GREEN) | RED on the numeric case — `pii_flags("amount", [...], inferred_type="float")` returned `(True, 'shape:phone')` instead of `(False, None)`; the STRING-typed phone control correctly stayed green throughout, confirming the gate narrows the false positive without touching the true positive |
| 33 (2026-07-28, SPEC §6.1.1 arbiter-directed, in-pass) | The `_check_no_duplicate_class_names(tables, class_names)` call removed from `infer_candidate_model` | `tests/test_infer_types.py::test_duplicate_candidate_class_names_refused_naming_both_tables` | RED — `customer_orders.csv` + `customer-orders.csv` (both -> `CustomerOrdersRecord`) silently produced a model instead of raising `DuplicateClassNameError` (`Failed: DID NOT RAISE`) |
| 34 (2026-07-28, SPEC §6.1.1 arbiter-directed, in-pass) | The over-wide-row `len(raw_row) > width` check removed from `_read_delimited`'s row generator | `tests/test_readers.py::test_over_wide_delimited_row_refused` (RED) / `tests/test_readers.py::test_narrow_delimited_row_still_pads_with_none` (control, stayed GREEN) | RED on the over-wide row (`Failed: DID NOT RAISE SampleFormatError` — the extra cell was silently dropped again); the narrow-row control correctly stayed green throughout |
| 35 (2026-07-28, Codex review) | `_datetime_order_key`'s aware-to-naive-UTC normalization removed from `_min_max_raw` (reverted to bare `min(datetimes)`/`max(datetimes)`) | `tests/test_profiles.py::test_min_max_datetime_column_mixed_aware_and_naive_does_not_crash` | RED — a `datetime` column mixing one aware value with one naive value raised `TypeError: can't compare offset-naive and offset-aware datetimes` instead of returning bounded min/max |
| 36 (2026-07-28, Codex review) | `_profile_column`'s post-retention distinct-value dedup reverted to the pre-retention form (`[_retain_non_pii(v, config=config) for v in sorted(distinct_set)]`, no `seen_retained` gate) | `tests/test_profiles.py::test_distinct_values_deduplicated_after_truncation_not_only_before` | RED — two RAW-distinct values sharing an 80-char prefix both survived retention as duplicate entries (`assert [...] == [shared_prefix]` failed with a 2-element list instead of 1) |
| 37 (2026-07-28, Codex review) | `_check_no_duplicate_mechanical_names` call removed from `infer_candidate_model` | `tests/test_infer_types.py::test_duplicate_mechanical_slot_names_refused_naming_both_columns` | RED — `first-name` and `first_name` on the same table (both -> `mechanical_name="first_name"`) silently produced two colliding slots instead of raising `DuplicateSlotNameError` (`Failed: DID NOT RAISE`) |
| 38 (2026-07-28, NE-ESB-EMIT REVISE, P1) | `_enum_decline_reason` reverted to the PRE-REVISION single-regex charset (`^[A-Za-z0-9][A-Za-z0-9 _.\-/]*$`) | `tests/test_roundtrip.py::test_charset_gate_accepted_values_roundtrip_through_real_generator` | RED — the widened acceptance set (backtick/@/%/quote/`<>`/unicode/digit-leading/keyword values) was declined again instead of emitted (`assert [] == ['WidgetsTagEnum']`), reproducing exactly the false-claim defect this revision fixes |
| 39 (2026-07-28, NE-ESB-EMIT REVISE, P1) | `_enum_decline_reason` made an unconditional `return None` (gate disabled) | `tests/test_roundtrip.py::test_charset_gate_rejected_values_actually_break_the_real_generator` (all six rule cases) | RED (all six) — every empirical-rule hostile value (empty, newline, backslash, NUL, `mro`, `_name_`) was silently ADMITTED instead of declined, failing at the gate-declines assertion before the real-generator proof ever ran |
| 40 (2026-07-28, NE-ESB-EMIT REVISE, P3-2) | The `cls()` instantiation line removed from `_IMPORT_CHECK_SCRIPT` | `tests/test_roundtrip.py::test_import_check_helper_bidirectional` (fourth block only) | RED (fourth block only, exactly as predicted) — a module whose `top_class` imports fine but whose `__init__` raises `RuntimeError` no longer failed the import check (`Failed: DID NOT RAISE DraftGenerationError`); the prior three controls (good/syntax-error/missing-name) stayed green throughout, confirming they never exercised instantiation at all |
| 41 (2026-07-28, NE-ESB-EMIT REVISE, P2) | The `_validate_class_names(model)` call removed from `emit_linkml` | `tests/test_emit.py::test_digit_leading_table_stem_rejected_early_naming_the_file` and `tests/test_roundtrip.py::test_derived_class_name_oracle_bidirectional` | RED (both) — a `2023.csv` sample stem no longer raised `InvalidClassNameError` at all (`Failed: DID NOT RAISE`), silently deferring the failure to the real generator deep inside a later subprocess instead of surfacing it immediately with the offending file name |
| 42 (2026-07-28, NE-ESB-EMIT CONTINUATION, A+B) | **A** (allowance removed): `"unicodedata"` deleted from `emit.py`'s `_PER_MODULE_ALLOWANCES` entry entirely (pre-edit HEAD state, captured before this row's fix). **B** (over-broad placement, transient): `"unicodedata"` moved from the per-module entry into the GLOBAL `_STDLIB_ALLOWLIST` instead | **A:** `tests/test_boundaries.py::test_every_module_imports_only_stdlib_pydantic_self_or_its_own_allowance`. **B:** `tests/test_boundaries.py::test_unicodedata_import_rejected_without_emit_allowance` (mutated), with `tests/test_boundaries.py::test_emit_allowance_permits_yaml_and_unicodedata_but_nothing_else` as the GREEN control | **A: RED** — `AssertionError: forbidden imports found: {'ne/schema_bootstrap/emit.py': ['unicodedata']}`, exactly one module, exactly one name, exactly as predicted; fixed by adding the narrow per-module allowance. **B: RED** — `assert 'unicodedata' in []` (the bidirectional control now falsely passes for every OTHER module once the name is global), while the acceptance test stayed **GREEN** throughout, proving the failure is specific to the narrow-placement guard and the allowance itself still works — pins the allowance to the per-module dict, not `_STDLIB_ALLOWLIST` |
| 43 (2026-07-28, NE-ESB-EMIT CONTINUATION, R6 re-verification) | The cross-value collision loop (`emit.py::_enum_decline_reason`'s trailing `labels: dict` block) TRANSIENTLY deleted | `tests/test_roundtrip.py::test_colliding_member_labels_silently_drop_a_member_when_forced` and `tests/test_emit.py::test_enum_gate_collision_reason_names_both_values`, with `tests/test_roundtrip.py::test_charset_gate_accepted_values_roundtrip_through_real_generator` as the GREEN control | RED (both) — `assert 'WidgetsTagEnum' in {}` (`emitted.declined_enums`): with the loop removed, `["a b", "a_b"]` (both mangling to member label `a_b`) no longer declined the enum at all — `emitted.emitted_enums == ['WidgetsTagEnum']`, meaning the real generator would silently keep one member and drop the other with zero error signal; the unrelated acceptance oracle stayed **GREEN** throughout. Fully reverted; `git diff -- packages/ne-schema-bootstrap/src` confirmed empty (SHA256 of `emit.py` matched pre-mutation) before commit |
| 44 (2026-07-28, NE-ESB-EMIT CONTINUATION 2, R6 P0 fix, Mutation D) | `_member_label`'s body reverted to the pre-fix whitespace-only mangle (`re.sub(r"\s+", "_", value.strip())`) | RED: `test_real_mangle_collision_families_decline_and_prove_member_loss[hyphen-vs-space]`, `[hyphen-vs-underscore]`, `[comma-elision]`, `[digit-prefix]`, `[keyword-suffix]`; `test_charset_gate_rejected_values_actually_break_the_real_generator[mro-comma-elided]`, `[sunder-hyphen-mangled]`, `[empty-label-after-mangle]`; and `test_charset_gate_accepted_values_roundtrip_through_real_generator`'s equivalence assert. GREEN control: `test_colliding_member_labels_silently_drop_a_member_when_forced`, `test_enum_gate_widened_acceptance_set_emits` | RED (9 of 9 named cases, ACTUAL) — 5 collision-family cases failed at the gate-declined assert (`assert 'WidgetsTagEnum' in {}`, e.g. `['in-progress', 'in progress']` no longer declined at all); the 3 rejected-empirical cases were admitted instead of declined; the acceptance-set equivalence assert failed on every value needing a step beyond whitespace-collapse (e.g. `'GRAVE_ACCENTbacktick'` not in the mirror's output). **One deviation from the row's own initial prediction, observed and recorded rather than forced to match**: `test_real_mangle_collision_families_decline_and_prove_member_loss[three-way]` (`["in-progress", "in progress", "in_progress"]`) stayed **GREEN** — the pairwise collision between `"in progress"` and `"in_progress"` already collides under the whitespace-only mangle alone (both -> `"in_progress"`), so the family is still caught even with hyphen-handling absent; this mutation isolates the OTHER 5 families correctly but does not, on its own, isolate `three-way`. Named GREEN controls confirmed green throughout |
| 45 (2026-07-28, NE-ESB-EMIT CONTINUATION 2, R6 P0 fix, Mutation E) | Only the `number_` digit-prefix line (`label = re.sub(r"(?=^\d)", "number_", label)`) deleted from `_member_label` | RED: `test_real_mangle_collision_families_decline_and_prove_member_loss[digit-prefix]`, `test_charset_gate_accepted_values_roundtrip_through_real_generator`'s equivalence assert. GREEN: `[hyphen-vs-space]`, `[comma-elision]` | RED (2 of 2, exactly as predicted) — `digit-prefix` (`["1st", "number_1st"]`) failed at the gate-declined assert (both values independently mangle to `'1st'` without the prefix, so they stop colliding with each other and both got admitted — `assert 'WidgetsTagEnum' in {}`); the equivalence assert failed with `'1st'` (mirror) vs `'number_1st'` (real generator) as the sole mismatch. GREEN controls confirmed green (2 of 2) |
| 46 (2026-07-28, NE-ESB-EMIT CONTINUATION 2, R6 P0 fix, Mutation F) | Only `.replace(",", "")` deleted from `_underscore` | RED: `test_real_mangle_collision_families_decline_and_prove_member_loss[comma-elision]`, `test_charset_gate_rejected_values_actually_break_the_real_generator[mro-comma-elided]`, `test_charset_gate_accepted_values_roundtrip_through_real_generator`'s equivalence assert. GREEN: `[hyphen-vs-space]`, `[hyphen-vs-underscore]` | RED (3 of 3, exactly as predicted) — `comma-elision` (`["a,b", "ab"]`) no longer collided (`'a,b'` now mangles to `'aCOMMAb'` via the unicodedata-name fallback instead of dropping the comma, so it stays distinct from `'ab'`) and the gate stopped declining it; `'mro,'` likewise stopped mangling down to `'mro'` and was admitted instead of declined; the equivalence assert's sole mismatch was `'ab'` (mirror) vs `'aCOMMAb'` (real generator). GREEN controls confirmed green (2 of 2) |
| 47 (2026-07-28, NE-ESB-EMIT CONTINUATION 2, R6 P0 fix, Mutation G) | Only `.replace("-", "_")` deleted from `_underscore` | RED: `test_real_mangle_collision_families_decline_and_prove_member_loss[hyphen-vs-space]`, `[hyphen-vs-underscore]`, `test_charset_gate_rejected_values_actually_break_the_real_generator[sunder-hyphen-mangled]`. GREEN: `[comma-elision]`, `[digit-prefix]` | RED (3 of 3, exactly as predicted) — `hyphen-vs-space` (`["in-progress", "in progress"]`) and `hyphen-vs-underscore` (`["a-b", "a_b"]`) both stopped colliding (hyphens now survive into the label via the unicodedata-name fallback instead of collapsing to `_`) and were admitted instead of declined; `'-name-'` no longer mangled to the fatal sunder shape `'_name_'` and was likewise admitted. **One deviation from the row's own initial prediction, observed and recorded**: `test_real_mangle_collision_families_decline_and_prove_member_loss[three-way]` was initially predicted RED but stayed **GREEN** — same root cause as row 44's three-way finding: `"in progress"` and `"in_progress"` already collide via the whitespace-collapse step alone, with no dependency on hyphen-handling, so this mutation does not isolate that family either. GREEN controls (`comma-elision`, `digit-prefix`) confirmed green (2 of 2) |
| 48 (2026-07-28, NE-ESB-EMIT CONTINUATION 2, R6 P0 fix, Mutation H) | Only the `if iskeyword(label): label += "_"` step deleted from `_member_label` | RED: `test_real_mangle_collision_families_decline_and_prove_member_loss[keyword-suffix]`, `test_charset_gate_accepted_values_roundtrip_through_real_generator`'s equivalence assert. GREEN: `[hyphen-vs-space]`, `[hyphen-vs-underscore]`, `[comma-elision]`, `[digit-prefix]`, `[three-way]` | RED (2 of 2, exactly as predicted) — `keyword-suffix` (`["class", "class_"]`) no longer collided (`'class'` stayed `'class'`, an unmasked Python keyword, instead of masking to `'class_'`) and the gate stopped declining it; the equivalence assert's sole mismatch was `'class_'` (mirror) vs `'class'` (real generator, which independently masks `class` -> `class_` regardless of this mutation, so the mirror now disagrees). All 5 named GREEN controls confirmed green (5 of 5) — every family whose collision arises purely from the `underscore()`-step mangle is unaffected by removing the separate keyword-masking step |
| 49 (2026-07-28, NE-ESB-EMIT CONTINUATION 2, allowance placement, Mutation I) | **A** (allowance removed): `"keyword"` deleted from `emit.py`'s `_PER_MODULE_ALLOWANCES` entry. **B** (over-broad placement, transient): `"keyword"` moved from the per-module entry into the GLOBAL `_STDLIB_ALLOWLIST` instead | **A:** `tests/test_boundaries.py::test_every_module_imports_only_stdlib_pydantic_self_or_its_own_allowance` and `::test_emit_allowance_permits_yaml_unicodedata_and_keyword_but_nothing_else`. **B:** `tests/test_boundaries.py::test_keyword_import_rejected_without_emit_allowance`, with the renamed allowance test as the GREEN control | **A: RED (both)** — `AssertionError: forbidden imports found: {'ne/schema_bootstrap/emit.py': ['keyword']}`, exactly one module, exactly one name, exactly as predicted; the allowance-permits test failed identically (`assert ['keyword'] == []`). **B: RED** — `assert 'keyword' in []` (the bidirectional control now falsely passes for every OTHER module once `keyword` is global), while the renamed allowance-permits test stayed **GREEN** throughout — same shape as row 42, pins the `keyword` allowance to the per-module dict, not `_STDLIB_ALLOWLIST` |
| 50 (2026-07-28, NE-ESB-EMIT CONTINUATION 2, second Argus round, R5 identifier check, Mutation J) | R5's label check narrowed back from `if not label.isidentifier():` to `if label == "":` (the pre-P1-fix check it replaced) | RED: `test_charset_gate_rejected_values_actually_break_the_real_generator[identifier-illegal-char]`, `test_enum_gate_decline_reasons_rule_specific[½ inch-not a valid Python identifier]`. GREEN control: `test_charset_gate_rejected_values_actually_break_the_real_generator[empty-label-after-mangle]` (the other 19 params of both parametrized tests) | RED (2 of 2, exactly as predicted) — `'½ inch'` (mangles to `'½_inch'`, non-empty but illegal — `'½'` satisfies `str.isnumeric()` so the mirror's transliteration step leaves it untouched) was ADMITTED instead of declined in both tests (`assert 'WidgetsTagEnum' in {}`). GREEN control confirmed green — `',,'` (mangles to `''`, genuinely empty) is still caught by the narrowed `== ""` check, proving the mutation isolates exactly the non-empty-but-illegal class the P1 fix added, not the pre-existing empty-label case |
| 51 (2026-07-28, NE-ESB-EMIT CONTINUATION 2, third Argus round, P1 NFKC fix, Mutation K) | `_member_label`'s final `return unicodedata.normalize("NFKC", label)` reverted to plain `return label` (the pre-P1-third-round line it replaced) | RED: `test_real_mangle_collision_families_decline_and_prove_member_loss[nfkc-micro-sign]`, `[nfkc-fullwidth-letter]`; `test_charset_gate_rejected_values_actually_break_the_real_generator[nfkc-fullwidth-mro]`. GREEN control: all 9 non-NFKC params of the same two parametrized tests (`hyphen-vs-space` through `keyword-suffix`, `empty-or-whitespace` through `identifier-illegal-char`) | RED (3 of 3, exactly as predicted) — `['µm', 'μm']` and `['A', 'Ａ']` no longer collided (`assert 'WidgetsTagEnum' in {}`, both admitted as two textually-distinct labels); `'ｍｒｏ'` no longer matched `_FATAL_MEMBER_LABEL` (`'mro'`) and was admitted instead of declined. GREEN controls confirmed green (9 of 9) — every family/case whose collision or decline doesn't depend on NFKC-equivalence is unaffected by removing the normalization step. Fully reverted; `diff` against the pre-mutation backup confirmed byte-identical before commit |
| 52 (2026-07-28, NE-ESB-EMIT CONTINUATION 2, third Argus round, P3 reason-wording fix, Mutation L) | The R6 collision reason string reverted to the pre-fix unconditional `"— the generator silently drops one member (probe-proven member loss, not a generation failure)"` (dropping the family-neutral `"— depending on the collision shape, the generator either silently drops one member or fails during generation"` wording it replaced) | RED: `test_real_mangle_collision_families_decline_and_prove_member_loss`'s `assert "not a generation failure" not in reason`, run for ALL 8 collision-family params (not only the loud ones). GREEN control: `test_enum_gate_collision_reason_names_both_values` (all 6 params — checks only that both values are named in the reason, not the specific wording) | RED (8 of 8, exactly as predicted) — every family's reason once again contained the literal false claim `"not a generation failure"`, including `keyword-suffix`/`nfkc-micro-sign`/`nfkc-fullwidth-letter`, all three of which ARE generation failures (`TypeError` at Enum-class construction). GREEN control confirmed green (6 of 6) — the wording regression is invisible to a test that only checks the colliding values are named, which is exactly why this dedicated wording assertion was added rather than relying on the existing test to catch it |
| 53 (2026-07-28, NE-ESB-EMIT CONTINUATION 3, differential-fuzz round, THE P0 fix, Mutation M-a) | R5's legality check restored to the exact pre-fix regression diff: `if not raw.isidentifier():` changed to `if not _member_label(value).isidentifier():` (checking the NFKC-NORMALIZED label instead of the RAW one) | RED: `tests/test_differential_fuzz.py::test_differential_fuzz_gate_matches_venv_ground_truth`, `tests/test_roundtrip.py::test_charset_gate_rejected_values_actually_break_the_real_generator[trailing-superscript]` and `[trailing-superscript-spaced]`. GREEN control: `[empty-label-after-mangle]` (the other 12 params of the same parametrized test) | RED (3 of 3, exactly as predicted) — `'m²'`/`'10 m³'` were ADMITTED again instead of declined (`assert 'WidgetsTagEnum' in {}`); the fuzz caught the SAME class independently on a corpus-generated value (`'test³'`) it had never seen curated — `gate ACCEPTED 'test³' ... but the real generated label 'test³' does not bind as a Python identifier: invalid character '³' (U+00B3)`. GREEN control confirmed green — this is the round's thesis (the differential oracle subsumes the curated one) demonstrated directly |
| 54 (2026-07-28, NE-ESB-EMIT CONTINUATION 3, differential-fuzz round, THE P1 fix half 1, Mutation M-b) | `_member_label`'s NFKC step deleted (`return unicodedata.normalize("NFKC", _member_label_raw(value))` reverted to `return _member_label_raw(value)`) | RED: `tests/test_differential_fuzz.py::test_differential_fuzz_gate_matches_venv_ground_truth`, `tests/test_roundtrip.py::test_real_mangle_collision_families_decline_and_prove_member_loss[nfkc-micro-sign]` and `[nfkc-fullwidth-letter]` | RED (3 of 3) — `['µm', 'μm']` and `['A', 'Ａ']` no longer collided (both admitted as two textually-distinct labels); the fuzz's bound-name equivalence assertion failed on any NFKC-diverging accepted value |
| 55 (2026-07-28, NE-ESB-EMIT CONTINUATION 3, differential-fuzz round, THE P1 fix half 2, Mutation M-c) | `_member_label_raw`'s branch decision reverted from `_venv_isidentifier(label)` to plain `label.isidentifier()` | RED: `tests/test_differential_fuzz.py::test_differential_fuzz_gate_matches_venv_ground_truth`, `tests/test_roundtrip.py::test_charset_gate_accepted_values_roundtrip_through_real_generator` (equivalence assert), `test_real_mangle_collision_families_decline_and_prove_member_loss[xid-skew-transliteration]` | RED (3 of 3, exactly as predicted) — the accepted-set equivalence assert failed (`'aKATAKANA_MIDDLE_DOTb'` vs `'a・b'`, extra items on both sides of the set comparison); the xid-skew-transliteration family stopped colliding (`assert 'WidgetsTagEnum' in {}`); the fuzz failed identically on ZWJ/middle-dot corpus values. This is the round's thesis's OTHER half — the fuzz catches the emulation defect the curated set was specifically built to also catch, proving both agree |
| 56 (2026-07-28, NE-ESB-EMIT CONTINUATION 3, differential-fuzz round, THE P1 fix half 3, Mutation M-d, RE-VERIFIED fifth round after the P3 corpus-independence fix) | `_UNICODE_DB_SKEW` emptied (`frozenset()`) | RED: `tests/test_differential_fuzz.py::test_differential_fuzz_gate_matches_venv_ground_truth`, `tests/test_roundtrip.py::test_charset_gate_rejected_values_actually_break_the_real_generator[unicode-db-skew]`. The parity-digest test's own assertions are UNTOUCHED by this mutation (it recomputes the exclusion set from the same now-empty constant, so digest equality still holds) — only the gate-facing rows go red, exactly as the row predicts | RED (2 of 2, RE-RUN 2026-07-28 fifth review round after decoupling the fuzz's `_DB_SKEW_CPS`/`_XID_SKEW_CHARS` pools from `emit_mod._UNICODE_DB_SKEW`/`_XID_SKEW`, Argus P3) — `'grade\U0002EBF0'` was ADMITTED again instead of declined; the fuzz now trips its OWN accept-side ground-truth assertion directly on a DIFFERENT corpus-generated DB-skew value it had never seen curated (`'𮵗'`, U+2ED57): `gate ACCEPTED '𮵗' ... but the real venv's own mangle machinery crashed computing its label: ValueError: no such name`. **Correction to this row's PREVIOUS text**: before the P3 fix, this mutation instead crashed the fuzz's CORPUS BUILDER (`rng.choice([])` on the then-self-referential `_DB_SKEW_CPS` pool) — a real RED, but for the wrong reason (the harness broke, not the gate/ground-truth comparison), and NOT the assertion this row originally claimed. The corpus pool is now an independent literal transcription of the same three measured ranges, so this mutation reaches the actual gate defect the row always intended to demonstrate |
| 57 (2026-07-28, NE-ESB-EMIT CONTINUATION 3, differential-fuzz round, corpus non-vacuity, Mutation M-e) | Corpus builder restricted to ONLY the first (ASCII-words) pool — every non-ASCII/whitespace/control/keyword/DB-skew/XID-skew/collision-mutant pool removed, keeping the same 600-iteration count | RED: `tests/test_differential_fuzz.py::test_differential_fuzz_gate_matches_venv_ground_truth`'s own floors, actually run standalone against the isolated ASCII-only pool | RED — observed corpus size 600 (< `_MIN_CORPUS = 4000`, the FIRST floor tripped), 0 ground-truth errors (< `_MIN_GROUND_TRUTH_ERRORS = 200` — never reaches the venv-crash space at all) and 8 multi-value collision groups (< `_MIN_COLLISION_GROUPS = 150`); every non-vacuity floor is independently violated, not just the corpus-size one, proving the floors are load-bearing at every layer, not merely the total-count check |
| 58 (2026-07-28, NE-ESB-EMIT CONTINUATION 3, differential-fuzz round, parity-set precision, Mutation M-f) | A benign, unrelated codepoint (`0x41`, `'A'`) added to `_XID_SKEW` | RED: `tests/test_roundtrip.py::test_platform_and_venv_unicode_property_signatures_match_outside_declared_skew`'s per-codepoint venv-False assertion for `U+0041` specifically — **NOT the digest assertion**, which still passes (excluding one extra ordinary codepoint from a byte-identical-elsewhere comparison cannot itself break equality) | RED — `AssertionError: U+0041 claimed as _XID_SKEW but IS identifier-continue-legal on the venv too — the skew set is stale (over-broad)`; confirms the per-codepoint checks (not just the aggregate digest) are load-bearing, catching an over-broad skew set the digest alone would miss |
| 59 (2026-07-28, NE-ESB-EMIT CONTINUATION 3, differential-fuzz round, keyword-masking re-verification, Mutation M-g) | The `if iskeyword(label): label += "_"` step deleted from `_member_label_raw` (same step as row 48, re-verified after the raw/identity split moved it) | RED: `tests/test_differential_fuzz.py::test_differential_fuzz_gate_matches_venv_ground_truth`, `tests/test_roundtrip.py::test_real_mangle_collision_families_decline_and_prove_member_loss[keyword-suffix]` | RED (2 of 2) — `['class', 'class_']` no longer collided (`'class'` stayed an unmasked Python keyword); the fuzz's bound-name equivalence assert failed on every keyword-valued accepted corpus entry |
| 60 (2026-07-28, NE-ESB-EMIT CONTINUATION 3, fifth review round, THE P1 fix (private-name silent drop), Mutation M-h) | The `_is_enum_private_member_name(enum_name, label)` check (and its `return` block) deleted from R5 in `_enum_decline_reason` | RED: `tests/test_roundtrip.py::test_charset_gate_declines_private_mangled_enum_class_name`, `tests/test_emit.py::test_enum_gate_decline_reasons_rule_specific[_WidgetsTagEnum__foo-PRIVATE]`. GREEN control (deliberately checked, not assumed): `tests/test_emit.py::test_is_enum_private_member_name_matches_stdlib` (the PREDICATE itself is untouched, only its WIRING into the gate was removed) and `tests/test_differential_fuzz.py::test_differential_fuzz_gate_matches_venv_ground_truth` (also GREEN — this is the deliberate, documented asymmetry: the fuzz's `_FUZZ_ENUM_NAME` corpus doesn't draw values shaped to trigger a class-name-keyed predicate at scale, so this mutation is invisible to it by construction, unlike M-a/M-c/M-d/M-g above) | RED (2 of 2, exactly as predicted) — `'_WidgetsTagEnum__foo'` was ADMITTED again instead of declined (`assert 'WidgetsTagEnum' in {}`). GREEN controls confirmed green (both) — the predicate-parity test proves the FUNCTION is still correct in isolation, and the fuzz staying green on this specific mutation is itself evidence for, not against, the round's "curated + systematic, not curated OR systematic" thesis: each oracle catches what the other structurally cannot |
| 61 (2026-07-29, NE-ESB-CLI REVISE 5, mutation a) | `refine._safe_load_draft`'s `isinstance(key, str)` check (the round-5 P2 fix) deleted entirely | RED: `tests/test_refine.py::test_three_way_merge_raises_refine_error_on_non_string_section_key` (all 6 parametrized cases: side × section). GREEN control (same test): the quoted-key control case is unaffected by this mutation (it never reaches the deleted branch) | RED (6 of 6, exactly as predicted) — a raw `TypeError: '<' not supported between instances of 'str' and 'int'` escaped from `_merge_named_section`'s `sorted(names)`, reproducing the exact Argus P2 crash instead of `RefineError` |
| 62 (2026-07-29, NE-ESB-CLI REVISE 5, mutation b) | `_clear_staging`'s explicit `leftover.is_dir() and not leftover.is_symlink()` refusal check deleted | RED: `tests/test_workspace.py::test_clear_staging_refuses_subdirectory_with_typed_error`'s message-specific assertion (`"subdirectory this tool never creates" in str(exc_info.value)`). GREEN control (same test): the plain-file-leftover recovery case still succeeds — this mutation only removes the SHAPE check, not the surrounding OSError wrap | RED — the test's own `WorkspaceIOError`/path/`is_dir()` assertions ALL still passed under this mutation (`leftover.unlink()` on a directory independently raises an OS error — `PermissionError`/"Operation not permitted" on this platform — that the generic per-entry `except OSError` wrap converts to a WorkspaceIOError naming the same path), which is itself a [[vacuous-guard-assertion-shapes]] finding: the original test assertions were NOT discriminating. Fixed by adding the message-specific assertion above (committed separately, "strengthen subdirectory-refusal test"); re-run against the strengthened test: RED as predicted (`AssertionError: assert 'subdirectory this tool never creates' in "...cannot remove staged file: Operation not permitted"`), control still GREEN |
| 63 (2026-07-29, NE-ESB-CLI REVISE 5, mutation c) | `_read_state_text`'s `try`/`except (OSError, UnicodeDecodeError)` wrap deleted (bare `return path.read_text(encoding="utf-8")`) | RED: `tests/test_workspace.py::test_state_file_not_utf8_raises_refine_error_naming_path` (all 3 parametrized state files) | RED (3 of 3, exactly as predicted) — a raw `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 12: invalid start byte` escaped from `path.read_text()`, uncaught, instead of `RefineError` |
| 64 (2026-07-29, NE-ESB-CLI REVISE 5, mutation d) | `_validate_engagement(engagement)` moved from the first statement of `run_bootstrap` to after `_ensure_gitignore(workspace_dir)` inside `_run_bootstrap_fresh` (simulating the pre-round-5 call site) | RED: `tests/test_workspace.py::test_engagement_validated_before_any_workspace_write`'s `assert not workspace_dir.exists()` after the invalid-slug call. GREEN control (same test): the valid-slug case is unaffected | RED, exactly as predicted — `InvalidEngagementNameError` still raised (correctly), but `workspace_dir` now EXISTED afterwards (`AssertionError: assert not True`), proving the side-effect-before-validation gap reopens without the reordering |
| 65 (2026-07-29, NE-ESB-CLI REVISE 5, mutation e) | The `report.py` "- landing classes (N)" bullet's non-abstract-exclusion clause reverted to its pre-round-5 text (`f"- landing classes ({len(landing.class_names)}): "`) | RED: `tests/test_workspace.py::test_refine_report_reflects_landing_draft_not_fresh_machine_draft`'s extended `assert "non-abstract only" in landing_line` | RED, exactly as predicted — `AssertionError: assert 'non-abstract only' in '- landing classes (3): "AcmeCoRecordSet", "CustomersRecord", "OrdersRecord"'` |
| 66 (2026-07-29, NE-ESB-CLI REVISE 5, mutation f) | A stray, unreachable `_stray_helper_for_mutation_testing()` function raising `RefineError("stray")` planted in `refine.py`, uncalled from anywhere | RED: `tests/test_refine.py::test_refine_error_raise_sites_are_exactly_the_documented_shapes` | RED, exactly as predicted — `AssertionError`, "Extra items in the left set: '_stray_helper_for_mutation_testing'" — the docstring-completeness ratchet catches an unenumerated raise site even though it is never actually invoked at runtime (an AST scan, not a coverage/runtime check) |
| 67 (2026-07-29/30, NE-ESB-CLI continuation, completeness-guard probe) | An uncalled helper planted in `workspace.py`, `def _stray_raw_probe() -> None: raise ValueError("probe")` (this row is the unit's THESIS, run first as a probe BEFORE the new test existed) | Run against every existing test in `test_refine.py`/`test_workspace.py`/`test_boundaries.py` (71 tests). Deliberately-recorded GREEN control: `tests/test_refine.py::test_refine_error_raise_sites_are_exactly_the_documented_shapes` — the pre-existing enumeration test, to prove it does NOT catch this class | **GREEN, all 71 of 71** — `71 passed in 289.36s`, including the named enumeration test: no existing test noticed the planted raw `ValueError`. This is the gap `test_raw_exception_sweep_is_complete_no_untyped_raise_sites` (added this round) exists to close. Re-run with the new test present and the SAME mutation still applied: `1 failed, 26 passed` — `AssertionError: raw (non-package-typed) exception raised in refine.py/workspace.py: {'ValueError'}` — `test_refine_error_raise_sites_are_exactly_the_documented_shapes` stayed GREEN in this run too, confirming the split is real: the new test is what catches it, not the old one |
| 68 (2026-07-29/30, NE-ESB-CLI continuation, bare-re-raise probe) | An uncalled helper planted in `refine.py`: `def _stray_bare_reraise_probe() -> None: try: open("does-not-exist")` / `except OSError: raise` (a bare re-raise, which re-propagates the caught raw `OSError`) | RED: `tests/test_refine.py::test_raw_exception_sweep_is_complete_no_untyped_raise_sites` | RED, exactly as predicted — `Failed: bare `raise` (re-propagates a caught raw exception, exactly the untyped-escape class round 5's sweep closed) found in scanned source near '        raise'`, tripped at the scanner's explicit bare-raise check before it ever reaches the subset assertion |
| 69 (2026-07-29/30, NE-ESB-CLI continuation, non-vacuity control, two-part) | (a) Unmutated tree, both directions checked as controls: `raised <= allowed` assertion and the `⊇{RefineError, WorkspaceIOError, EmptySampleError, WorkspaceExistsError}` cardinality assertion. (b) `_all_raise_exception_names` neutered to `return set()` immediately (mirrors the "scanner never finds anything" vacuous-guard shape). (c) With the tree otherwise unmutated, the `assert raised <= allowed` block commented out, keeping only the cardinality control | (a) GREEN: `test_raw_exception_sweep_is_complete_no_untyped_raise_sites`. (b) RED at the cardinality control specifically, not the subset line. (c) GREEN: same test, cardinality control alone | (a) GREEN, `1 passed` — control confirmed passing on the real, unmutated tree. (b) RED — `assert {'EmptySample...spaceIOError'} <= set()` / `Extra items in the left set: 'WorkspaceExistsError' 'EmptySampleError' 'WorkspaceIOError' 'RefineError'` — a scanner that always returns the empty set trivially satisfies `raised <= allowed` (vacuously true of the empty set) but is caught by the cardinality control, exactly the [[vacuous-guard-assertion-shapes]] cardinality-trivial shape this control exists to rule out. (c) GREEN, `1 passed` — with the subset assertion removed entirely, the cardinality control alone still passes on the unmutated tree, proving the two assertions are independent checks (the subset line is what m67/m68 above actually trip; the cardinality line is what (b) trips) rather than one accidentally subsuming the other — bidirectional evidence per [[vacuous-guard-assertion-shapes]] |
| 70 (2026-07-30, NE-ESB-CLI continuation round 2, Argus P2 fix, import-list completeness) | The `from ne.schema_bootstrap import (...)` list AND its matching `assert X is not None` line, both for `WorkspaceIOError`, deleted from `test_public_api_all_importable` (simulating the drift shape Argus flagged: a name added to `__all__`/`_EXPECTED_PUBLIC_API` that this test's import list simply never picked up, as opposed to a partial edit that would instead raise `NameError`) | RED: `tests/test_boundaries.py::test_public_api_all_importable`'s new self-scan completeness assertion | RED, exactly as predicted — `AssertionError: test_public_api_all_importable's import list drifted from _EXPECTED_PUBLIC_API: missing=['WorkspaceIOError'] extra=[]`. Control (unmutated tree): GREEN, `18 passed` |
| 71 (2026-07-30, NE-ESB-CLI continuation round 2, `__all__` regression re-probe, honestly recorded non-vacuity note) | `"WorkspaceIOError"` removed from `src/ne/schema_bootstrap/__init__.py`'s `__all__` | RED: `tests/test_boundaries.py::test_public_api_all_matches_expected_list` | RED, exactly as predicted — `AssertionError` (`sorted(sb.__all__) != _EXPECTED_PUBLIC_API`, index-28 diff). Note (matches Argus's own P2 observation, recorded honestly rather than papered over): `test_public_api_all_importable` stays **GREEN** under this SAME mutation — `from ne.schema_bootstrap import WorkspaceIOError` and `getattr` both still resolve, because `__all__` only governs `from module import *`, not an explicit `import name`; the row-70 guard checks a DIFFERENT property (this test's import list vs. `_EXPECTED_PUBLIC_API`, not vs. `sb.__all__` directly), which is why both checks are needed and neither subsumes the other |
| 72 (2026-07-30, NE-ESB-CLI continuation round 2, Argus P3 fix, package-wide scope re-verification) | `test_raw_exception_sweep_is_complete_no_untyped_raise_sites`'s scan was widened from two hard-coded modules (`refine.py`/`workspace.py`) to every module in the package via `pkgutil.iter_modules` (except `errors.py`, the `allowed`-set source). Rows 67-69's three mutations were re-run against the widened scanner to confirm the widening didn't change the guard's bite | 67-repeat: uncalled `raise ValueError("probe")` in `workspace.py` -> RED (same test); GREEN control: `test_refine_error_raise_sites_are_exactly_the_documented_shapes`. 68-repeat: bare re-raise in `refine.py` -> RED. 69-repeat(b): scanner neutered to `return set()` -> RED at the cardinality control | 67-repeat: RED — `AssertionError: raw (non-package-typed) exception raised somewhere in ne.schema_bootstrap: {'ValueError'}`; control test stayed GREEN (`1 failed, 1 passed` on the two-test subset). 68-repeat: RED — `Failed: bare `raise` ... found in scanned source near '        raise'`. 69-repeat(b): RED — `assert {'EmptySample...spaceIOError'} <= set()`, `Extra items in the left set: 'EmptySampleError' 'RefineError' 'WorkspaceIOError' 'WorkspaceExistsError'`. All three source-file mutations reverted with `git checkout --` immediately after observation (confirmed via `git diff --stat`); the test-file-only (b) probe was reverted by restoring the un-neutered `_all_raise_exception_names` body, using a full-file backup rather than `git checkout --` on the test file itself (which would have discarded this round's own durable edits) |
| 73 (2026-07-30, NE-ESB-CLI continuation round 3, Argus P2 re-review fix, `__init__.py` scan gap) | (a) BEFORE the fix: an uncalled helper `def _argus_reprobe_init() -> None: raise ValueError("reprobe in __init__.py")` planted at the end of `src/ne/schema_bootstrap/__init__.py`, run against the round-72 scanner (`pkgutil.iter_modules` only, no explicit `sb_pkg` entry) — reproduces Argus's exact finding that `iter_modules` never yields the package's own `__init__.py`. (b) `_package_modules_to_scan` changed to seed its return list with `[sb_pkg]` before appending the `iter_modules` submodules. (c) Same helper re-planted in `__init__.py`, run again against the FIXED scanner | (a) expected GREEN (the gap): `test_raw_exception_sweep_is_complete_no_untyped_raise_sites`. (c) expected RED, same test | (a) **GREEN, `1 passed in 0.81s`** — confirmed the gap exactly as Argus reported: a raw raise in `__init__.py` shipped past the "package-wide" scanner undetected. (c) RED, `1 failed in 1.32s` — `AssertionError: raw (non-package-typed) exception raised somewhere in ne.schema_bootstrap: {'ValueError'}`. Reverted with `git checkout --` after each of (a) and (c); re-ran unmutated: GREEN, `1 passed in 1.12s`. `git diff --stat` on `__init__.py` confirmed empty after the final revert |
| 74 (2026-07-30, NE-ESB-CLI continuation round 4, Codex C1 fix, scalar-key deletion resurrection) | `_merge_scalar_keys`'s new `if b is not _MISSING and o is _MISSING: continue` guard removed and the old fallthrough (`elif b is not _MISSING: merged[key] = b`) restored, reproducing the pre-fix behavior where a human deletion of a top-level scalar key (e.g. `description`) is silently resurrected from a fresh machine draft that still emits it | RED: `tests/test_refine.py::test_scalar_key_deletion_by_human_is_not_resurrected_by_theirs`. GREEN control (same mutated tree): `tests/test_refine.py::test_scalar_keys_ours_iff_changed_else_theirs` and `::test_top_level_slots_block_merges_as_one_opaque_key` (both exercise `_merge_scalar_keys` on keys the mutation doesn't touch — human-edited/unchanged/human-only keys — proving the mutation is narrowly targeted, not a wholesale break) | RED, exactly as predicted — `AssertionError: assert 'description' not in {'id': 'x', 'title': 'human title', 'description': 'machine description v2'}`. Both control tests stayed GREEN under the same mutation (`1 failed, 3 passed` on the four-test subset). Reverted immediately; `git diff` on `refine.py` confirmed identical to the pre-mutation (fixed) tree before the durable commit |

All six mutations in rows 61-66 were reverted after observing the failure; `git status --porcelain` confirmed a clean tree (matching the un-mutated committed source) before their respective round's final commit, except for mutation b's test-strengthening fix, which was itself committed (see the row-62 entry above).

Rows 67-69's source-file mutations (67, 68) were reverted with `git checkout --` immediately after each observation; the test-file-only probes in row 69 ((b), (c)) were reverted by restoring the original `_all_raise_exception_names` body / `assert raised <= allowed` block. `git diff --stat` after each revert showed only `tests/test_refine.py`'s durable additions (the new helper + test), never the mutated line, confirmed before the continuation's single commit.

Rows 70-72's mutations were all reverted the same way: row 70's deleted import/assert line in `test_boundaries.py::test_public_api_all_importable` was restored from the un-mutated version; row 71's deleted `"WorkspaceIOError"` entry in `src/ne/schema_bootstrap/__init__.py`'s `__all__` was restored with `git checkout --`; row 72's three source-file re-probes (the `workspace.py`/`refine.py` raw-raise repeats) were reverted with `git checkout --` immediately after each observation, and its test-file-only cardinality re-probe was reverted by restoring the un-neutered scanner body. `git diff --stat` was confirmed empty for every mutated file before the continuation-round-2 commit.

Row 73's two `__init__.py` probes ((a) before the fix, (c) after) were both reverted with `git checkout -- packages/ne-schema-bootstrap/src/ne/schema_bootstrap/__init__.py` immediately after each observation; `git diff --stat` on that file was confirmed empty both times before the continuation-round-3 commit.

**Row 43 is superseded, not wrong — a scope note (2026-07-28, NE-ESB-EMIT
CONTINUATION 2, Argus P0/P3).** Row 43's mutation deleted the ENTIRE
collision-detection loop, so both of its named tests died at the
gate-declined precondition (`assert 'WidgetsTagEnum' in emitted.declined_enums`)
before either test's real assertion ever ran. That only proves the R6 RULE
*exists* — it says nothing about whether the label mangle the rule checks
against is itself correct, which is exactly the gap that let the prior
whitespace-only approximation ship as R6's oracle. Rows 44-48 above
supersede row 43 by mutating the MANGLE (`_member_label`'s
internals) while leaving the collision rule itself fully in place — so what
goes RED is the venv-backed count/equivalence oracle
(`test_real_mangle_collision_families_decline_and_prove_member_loss`,
`test_charset_gate_accepted_values_roundtrip_through_real_generator`'s
equivalence assert), not merely the rule's presence. Row 49 closes the
companion Mutation I gap for the new `keyword` allowance, the same shape as
row 42. **This note was itself relocated here, from between rows 43 and 44
(2026-07-28, third Argus round, P3) — sitting there orphaned rows 44-50 from
the table's header/delimiter row, which GitHub-flavoured Markdown would have
rendered as literal pipe-delimited text rather than table rows; moving it to
after the table's final row (52) keeps rows 1-52 as one unbroken table.**

All fifty-two mutations were reverted after observing the failure; the
worktree is clean and every check command is green with the un-mutated
source. (Argus P2 and both P3 findings from the second review round were
docstring/oracle-strengthening fixes with no standalone decline rule to
mutate — see the "Four more findings" paragraph above; P3's `TypeError`
correction and interpreter-parity pin are both proven directly against the
real venv by `test_real_mangle_collision_families_decline_and_prove_member_loss[keyword-suffix]`'s
exception-text assertion and
`test_platform_keyword_list_matches_codegen_venv_keyword_list`'s live
subprocess comparison, not by a source-code mutation.) Rows 51-52 are the
third-round P1 (NFKC step) and P3 (reason-wording) mutations; the third
round's P2 (the `_enum_member_names` NFKC-equivalence docstring) and its
second P3 (this table's own structure) are, like the second round's P2/P3
findings above, docstring/formatting fixes with no standalone decline rule
of their own to mutate — proven instead by the `nfkc-*` collision-family
oracle tests (P2) and by this table itself now rendering as one unbroken
sequence (P3).

**Defect found and fixed in this review (mutation 12's root cause):** the
value-shape phone-number heuristic in [`pii.py`](src/ne/schema_bootstrap/pii.py)
matched plain ISO-8601 date strings before this review (see
`tests/test_pii.py::test_value_shape_flags_genuine_phone_numbers` for the
positive control that was previously entirely untested). This silently
PII-suppressed exemplars/distinct-values on any digit-dash-shaped date
column even before the min/max fix below made the effect visible on
`min_value`/`max_value`. Fixed by excluding the ISO-date shape from the
phone-shape check.
