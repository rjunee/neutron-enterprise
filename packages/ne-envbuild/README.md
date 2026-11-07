# ne-envbuild

The ONE consolidated **sidecar-venv env-builder** leaf. Home of the shared
probe/lock/rebuild flow that provisions the isolated, hash-pinned `uv`
virtualenvs used to keep heavy dependency closures out of the platform
lock/SBOM (NE-CKC8).

## Why this package exists — the TOCTOU

Three platform-side drivers isolate a heavy closure in a dedicated venv and skip
the (re)build when a stored `.requirements.sha256` marker still matches the
committed `requirements.txt`:

- `ne.brain.ingest.docling_parser` — docling / torch / CUDA
- `ne.govern.redaction.presidio_backend` — presidio / spaCy
- `ne.schema.codegen` — linkml

Each had (or, for codegen, lacked) its own copy of the fast-path probe. The old
probe was a **time-of-check / time-of-use race**: the marker was written with a
non-atomic `write_text`, and read with an unlocked `is_file() -> read_text()`
probe performed BEFORE the rebuild lock (and, in the docling/presidio twins,
re-checked with the same non-atomic read INSIDE the lock). A concurrent rebuild
writing the marker could be observed mid-write — a partial, short, or otherwise
stale value — producing either:

- a **false "up-to-date" skip**, leaving a venv built against the OLD
  requirements silently in use, or
- a redundant full rebuild.

`ne.schema.codegen` was worse: it had **no lock at all**, so two concurrent runs
(e.g. `generate` and `check_freshness` in parallel CI shards) raced the
`rmtree`/install directly.

## The fix

- **Atomic marker write** (`write_marker_atomic`): write to a same-directory
  temp file, `fsync`, then `os.replace` into place. A reader can only ever
  observe a COMPLETE old or COMPLETE new marker — never a partial one — and a
  crash mid-write leaves the previous complete marker intact (a plain
  `write_text` would truncate it).
- **Fail-safe marker read** (`read_marker`): missing / unreadable / a directory /
  empty all map to `None`, and a short/partial hash can never equal the 64-hex
  want, so every degenerate marker state becomes needs-rebuild — never a skip,
  never an unhandled exception.
- **Authoritative under-lock re-check** (`ensure_sidecar_venv`): the unlocked
  fast-path is kept only as an advisory optimisation; the skip decision that
  gates the rebuild is re-made UNDER the filelock (double-checked locking).
  Codegen thereby gains the filelock serialisation + atomic marker it never had.

## Call sites

Each site keeps its public function signature (`ensure_env_at` / `ensure_env`)
and passes only its divergences to `ensure_sidecar_venv`:

- **docling** — `install_args=()`, and its AIR_GAP fail-closed guard as
  `pre_lock_check` (fires on the stale/absent path, before any build).
- **presidio** — `install_args=("--no-deps",)` (NE-CKC9: the requirements file
  is a complete compiled closure; metadata resolution would hard-fail on the
  overridden `cryptography<47` ceiling).
- **codegen** — `install_args=("--overrides", <overrides.txt>)`.

## Why a new leaf, not `ne-net`?

`ne-net` is the **network-security** leaf (the SSRF/sovereignty endpoint
validator). Building venvs is an unrelated concern; folding it into `ne-net`
would break that package's cohesion (constraint C5). This leaf's only
third-party dependency is `filelock` — already a shared, vetted direct
dependency of ne-brain and ne-govern — so it adds no heavy-dependency drag (C3)
and no new third-party component (C4). Its leaf-ness is enforced by the
`ne-envbuild-leaf` import-linter contract (`ne.envbuild` may import no other
`ne.*` package, nor `httpx`/`requests`) and a subprocess import guard in
`tests/test_import.py`.

## Known sibling out of scope

`ne.eval.ragas_oracle.ensure_ragas_env_at` is a **FOURTH** copy of this same
probe/lock/rebuild pattern that is still unlocked and non-atomic. It lives
outside NE-CKC8's lanes (ne-brain + ne-govern + ne-schema) and is **not** edited
here — it is flagged for a follow-up unit. Do not edit ne-eval as part of this
package.
