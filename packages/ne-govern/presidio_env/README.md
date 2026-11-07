# Presidio redaction env — isolated hash-pinned venv payload

This directory is the **isolated, hash-pinned virtualenv payload** for the C12
PII-redaction backend (`ne.govern.redaction.PresidioRedactor`). It mirrors the
isolation pattern `packages/ne-brain/parser_env/` uses for docling: Presidio's
heavy spaCy + `en_core_web_lg` (~560 MB) NER closure is installed into a
dedicated venv (`packages/ne-govern/.presidio-venv`, git-ignored) and driven as a
subprocess, so it **never enters the platform `uv.lock`/SBOM** and never trips the
C4 license gate (SPEC §11 / C4). The platform package imports nothing from
presidio.

- `requirements.in` — the top-level presidio + `en_core_web_lg` pins.
- `overrides.txt` — a uv compile-time `--override` that lifts
  presidio-anonymizer's stale `cryptography<47` ceiling (see below).
- `requirements.txt` — fully resolved, `--generate-hashes`, installed with
  `--require-hashes --no-deps`. Regenerate with:

  ```
  uv pip compile packages/ne-govern/presidio_env/requirements.in --universal \
      --python-version 3.12 --generate-hashes \
      --override packages/ne-govern/presidio_env/overrides.txt \
      -o packages/ne-govern/presidio_env/requirements.txt
  ```

  **Never hand-edit `requirements.txt`.**
- `redact_text.py` — runs INSIDE the venv over an `--input`/`--output` JSON-file
  protocol.

The platform-side driver is `ne.govern.redaction.presidio_backend` (imports
nothing from presidio); the live end-to-end test is
`packages/ne-govern/tests/test_presidio_live.py` (skip-unless
`NE_PRESIDIO_LIVE=1`).

## Custom recognizers (NE-CKC11)

presidio-analyzer 2.2.363's builtin `UsSsnRecognizer` invalidates blocklisted
sample numbers (`123-45-6789` et al.) even with explicit SSN context — an
under-redaction on the C12 PII path. `redact_text.py` therefore registers
`NeUsSsnContextRecognizer`: additive (the builtin stays registered and
unchanged), deterministic, structured 3-2-4 single-delimiter patterns
(dashed/spaced/dotted), that fires ONLY when an `ssn`/`social security` keyword
appears within the preceding 64 chars — that context requirement is the
over-redaction guard (the engine's `default_score_threshold` is 0, so any
emitted result redacts, which is why the gate lives in the recognizer, not a
score threshold). No dependency or pin changes. Live proof is in
`packages/ne-govern/tests/test_presidio_live.py`; pure-helper unit tests (no
venv) are in `packages/ne-govern/tests/test_presidio_env_ssn_gate.py`.

## Why the cryptography override

`presidio-anonymizer==2.2.363` is the **latest** release (re-verified 2026-07-06;
PyPI shows no newer) and declares `cryptography>=46.0.4,<47.0.0`. That ceiling is
**stale**: **GHSA-537c-gmf6-5ccf** (HIGH, CVSS 7.5 — an out-of-bounds read in the
statically-linked OpenSSL shipped in cryptography's wheels) is fixed **only** at
`cryptography>=48.0.1`. `overrides.txt` lifts the `<47` ceiling at compile time so
the resolver takes the real fix; the recompile pins `cryptography==49.0.0`.

This is a **full version bump, not a suppression** — no `.trivyignore`, no VEX
waiver. cryptography's API is stable across 46→49 for presidio's use, and our
redaction path uses the **replace/redact** operators, **not** the AES-encrypt
operator that is the only place presidio-anonymizer touches cryptography. The
proof that the bump is safe end-to-end is the live gate
(`test_presidio_live.py`), which redacts PERSON / EMAIL_ADDRESS / US_SSN on the
rebuilt venv running cryptography 49.

## Why the venv installs with `--no-deps`

The venv is (re)built by `ensure_env_at` via `uv venv` + `uv pip install
--require-hashes --no-deps -r requirements.txt`. The `--no-deps` flag is
**required**, not cosmetic: `requirements.txt` is already the complete compiled
closure, so nothing needs resolving at install time. Without `--no-deps`, uv's
hash-mode installer still resolves package **metadata** and **hard-fails** on
presidio-anonymizer 2.2.363's declared `cryptography<47` ceiling — the very
ceiling `overrides.txt` lifts at compile time. The override **cannot** be passed
to `uv pip install`: `--require-hashes` rejects the override line (unpinned →
pin error; pinned-but-unhashed → hash error). `--no-deps` skips the metadata
resolution that trips, and is strictly **tighter** supply-chain-wise: only the
audited, hashed closure can ever be installed.

(This is a deliberate divergence from the docling/ragas env-builder twins, which
still resolve metadata at install time. Unification is tracked under NE-CKC8.)

**Unpin trigger:** a `presidio-anonymizer` release that raises its `cryptography`
ceiling to `>=48` — then delete `overrides.txt`, drop `--override` from the
compile command, recompile, and (only if desired) drop `--no-deps` from the
installer.

## Writable venv root for hardened deploys (`NE_SIDECAR_VENV_ROOT`)

The ne-app production image installs into a **root-owned, read-only** site-packages
tree and runs as a non-root `USER 10001`, so the in-package default venv location
(`packages/ne-govern/.presidio-venv`) is **unwritable at runtime**. Set
**`NE_SIDECAR_VENV_ROOT`** to a writable mount and the sidecar resolves its venv to
`$NE_SIDECAR_VENV_ROOT/.presidio-venv` instead.

Resolution rule (NE-SVENV): when `NE_SIDECAR_VENV_ROOT` is **set-and-nonempty** the
venv is `$NE_SIDECAR_VENV_ROOT/.presidio-venv`; otherwise it is the in-package
sibling above (default byte-identical). Precedence: an explicit `venv_dir=`
constructor kwarg and the **per-sidecar `NE_GOVERN_PRESIDIO_VENV_DIR`** env var both
win over the generic `NE_SIDECAR_VENV_ROOT` (specific beats generic).

Pre-build at the resolved path — keeping presidio's `--no-deps` + override closure
(see "Why the venv installs with `--no-deps`" above):

```
uv venv "$NE_SIDECAR_VENV_ROOT/.presidio-venv" --python 3.12
uv pip install --python "$NE_SIDECAR_VENV_ROOT/.presidio-venv/bin/python" \
    --require-hashes --no-deps -r packages/ne-govern/presidio_env/requirements.txt
```

**MUST MATCH:** the runtime's `NE_SIDECAR_VENV_ROOT` must equal the root the venv was
pre-built under. If they differ, an AIR_GAP deploy **fail-closes** on the "missing"
venv (C2) and a non-air-gap deploy attempts an unwanted `uv pip install`
rebuild/egress.
