# Supply-chain security (NE-002)

This is the standing reference for how Neutron Enterprise enforces SPEC §2 **C4**
(license gates) and **C11** (supply-chain integrity from first commit). Everything
here is CI/dev-time: no runtime component gains network egress (C1/C2). The
license scanner and NOTICE aggregator run offline from `uv.lock`; scanner
vulnerability databases are fetched only inside CI.

## C4 — license classes and enforcement

The policy lives in [`tools/supply-chain/license-policy.toml`](../../tools/supply-chain/license-policy.toml)
and is enforced by `ne check licenses` (module `ne.cli.licenses`), which runs both
locally and as the `license-gate` job in `.github/workflows/supply-chain.yml`.

| Class | Licenses | Gate behaviour |
| --- | --- | --- |
| **BANNED** | AGPL, SSPL, BUSL, FSL, fair-code, Commons-Clause, any non-commercial (`-NC`), RSAL, Sustainable Use, Elastic License 1.x | Violation. **Never overridable** — an override targeting a banned license is itself a policy error. |
| **OUT-OF-PROCESS-ONLY** | GPL | Violation for in-venv (in-process) Python deps unless a `class="out-of-process-approved"` override exists. |
| **CASE-BY-CASE** | LGPL (static) | Violation unless a `class="case-by-case-approved"` override exists. |
| **SEPARATE-SERVICE** | Elastic-2.0 (ELv2) | Violation for an in-venv dep unless a `class="separate-service-approved"` override exists (ELv2 is allowed only as a separately deployed service). |
| **FREE to embed** | MIT, Apache-2.0, BSD-2/3-Clause, MPL-2.0, PSF-2.0/Python-2.0, ISC, 0BSD, Unlicense, Zlib | Allowed. |
| **UNKNOWN** | anything unresolved/unclassifiable | Violation — **fail closed**. |

Design guarantees (test-locked in `tools/ne-cli/tests/test_license_gate.py`):

- **Banned is structurally non-overridable.** `_apply()` rejects a banned license
  before any override is consulted; an override naming a banned package raises a
  *policy error*, not a pass.
- **Fail closed on unknown.** Missing/complex/unparseable license metadata →
  UNKNOWN → violation. Parenthesized SPDX expressions are treated as opaque
  (UNKNOWN). `A OR B` passes iff any operand is free; `A AND B` requires all free.
- **LGPL is matched before GPL** (because `"LGPL-3.0"` contains `"GPL"`).
- **First-party packages** (editable/virtual/directory sources in `uv.lock`) are
  exempt from third-party classes but must declare `Apache-2.0`.
- **Env drift** — an installed distribution absent from `uv.lock` is a violation.

License resolution precedence: `License-Expression` (PEP 639) → normalized trove
classifiers → a short `License` field → the `[known]` map → UNKNOWN. The
`[known]` map serves two verified purposes: (1) absent-metadata fallback for
platform-gated dists that never install on the build platform (e.g.
`colorama`), and (2) an UNKNOWN rescue — when metadata *does* resolve but the
resulting string classifies UNKNOWN (a present-but-non-SPDX name, e.g.
protobuf's PyPI `License` field literally reading `"3-Clause BSD License"`), a
matching `[known]` entry supplies the human-verified SPDX id in its place
(NE-CKB1, 2026-07-03). Banned dominance still wins first: a BANNED signal in
any metadata source is returned before any `[known]` lookup is consulted, so
the rescue can never mask a banned license, and a `[known]` value that itself
classifies BANNED still fails via `classify_license` in `scan` — the map is not
an escape hatch.

### Overrides

Per-package exceptions live in `[[overrides]]` and require `name`, `version`,
`class`, a non-empty `justification`, `approved_by`, and `date`. Overrides are an
**owner decision** — a builder must not self-issue one for a copyleft/ELv2 dep.

## SBOM — bespoke stdlib CycloneDX generator (decided 2026-07-03)

**Standing finding (preserved rationale).** The briefed SBOM generator,
`cyclonedx-bom` 7.3.0, has a **hard dependency on `chardet` 5.2.0, which is
LGPL-2.1+**. Adopting it would install an unapproved CASE-BY-CASE (LGPL) package
into the workspace, which our own C4 gate correctly rejects — so the mandated tool
fails the very license policy this unit enforces. `cyclonedx-bom` was **not** added
to the tree, and no self-issued LGPL override was granted; the lock and the license
gate remain clean and green.

**Decision.** We ship a **bespoke, zero-dependency CycloneDX generator**,
`ne release sbom` (module `ne.cli.sbom`), built from `uv.lock` with stdlib
`tomllib`/`json` only — exactly the rationale that produced the bespoke license
scanner, and more aligned with C1 (self-host, no surprise deps) and C11 (the lock
is already the pin+hash substrate). No third-party generator, no new license
surface.

**Spec-version choice — CycloneDX 1.6, not 1.7 (recorded explicitly).** The current
CycloneDX release is **1.7 / 1.7.1** (1.7 shipped 2026-03). We deliberately emit
**specVersion `1.6`** for tool-free stability and universal consumer support;
CycloneDX documents 1.7 as backward-compatible with 1.4–1.6, so 1.6 output is
consumable by 1.7 tooling. Revisit the spec-version bump at **NE-070**.

**Scope (superseded 2026-07-03, NE-CKB1).** `components[]` is the target
member's **transitive runtime dependency closure**, walked from `uv.lock`'s
`[[package]].dependencies` plus extras-gated
`[package.optional-dependencies]` edges (`_dependency_closure` in
`ne.cli.sbom`) — **not** the whole workspace's third-party set. Dev
dependencies (`[package.dev-dependencies]`) are excluded: they are never
shipped. This supersedes the interim "full third-party set" decision recorded
here at NE-002 launch, which over-scoped every per-member SBOM to the entire
workspace's dependency set. Per-OCI-image SBOMs still land with **NE-006/070**.
Licenses come from the same resolution path as the C4 gate (`resolve_license`:
License-Expression → trove classifiers → short License field → the `[known]`
map, including its UNKNOWN-rescue behaviour). Hashes are the `uv.lock`
sha256s, emitted as `alg: SHA-256`.

**Determinism.** No timestamp, no `dependencies[]` graph. The top-level
`serialNumber` is the one non-deterministic field by default: `ne release sbom`
emits a fresh `uuid4` every run (a unique per-document serial, exactly what
CycloneDX intends for the per-PR `sbom` job). **NE-069R** added
`ne release sbom --deterministic`, which instead derives a stable **`uuid5`**
serial from the document's own canonical content, so a deterministic SBOM (minus
its serial) is byte-identical across regenerations. Release builds use
`--deterministic` (`.github/workflows/release.yml`) so each uploaded
`*.cdx.json` byte-matches the bundle-manifest-recorded sha256, which
`release.yml`'s fail-closed "Verify SBOM digests match the bundle manifest" step
asserts. `metadata.component` carries the target member dist
(`Apache-2.0`, no purl — first-party dists are not on PyPI).

**CI.** The `sbom` job in `supply-chain.yml` builds per-member SBOMs on every PR
and uploads them (`actions/upload-artifact`, SHA-pinned). `release.yml` signs and
attests each `*.cdx.json` with the same key-based cosign flow as the wheels.

## C11 — pin + hash

- **`uv.lock` is the pin+hash substrate.** Every third-party artifact carries a
  structurally valid `sha256:<64-hex>` digest. `ne check lock` asserts (a) every
  artifact carries such a digest — a mere substring match (e.g.
  `not-a-real-sha256`) is rejected, not accepted (fail closed, NE-CKB1) — and
  (b) `uv lock --check` confirms the lockfile is current. Never hand-edit the
  lock.
- **GitHub Actions are pinned to full 40-hex commit SHAs** (test-enforced by
  `tools/ne-cli/tests/test_workflow_pins.py`), with a `# vX.Y.Z` comment.
- **Scanner binaries are checksum-pinned.** `tools/supply-chain/install-scanners.sh`
  verifies each download against `tools/supply-chain/tool-checksums.txt` and fails
  closed on any mismatch or missing pin.

## Vulnerability scanning + VEX

`supply-chain.yml`'s `vuln-scan` job runs **Trivy** (`fs --scanners vuln
--exit-code 1 --severity CRITICAL,HIGH --ignore-unfixed`) and **Grype**
(`--fail-on high --only-fixed`) over the repository, and the same pair over the
built `ne-postgres-age` image, on PRs, pushes to main, and a weekly cron.

**Gate policy: a HIGH or CRITICAL finding fails the build when a fix is
available. A HIGH or CRITICAL finding with no fix is report-only.** It does not
fail the build, and it is never dropped: before any gate runs, the job writes
full, unfiltered Trivy and Grype JSON reports (every severity, fixed and
unfixed), and a final `if: always()` pass (`tools/supply-chain/vuln_report.py`)
lists every HIGH/CRITICAL finding with its fix state in the job summary and
uploads the raw reports as the `vuln-reports` artifact. "Fix available" is each
scanner's own predicate: a non-empty Trivy `FixedVersion`, or Grype
`fix.state == "fixed"`. The two VEX gates (`vex_dualkey_gate.py`,
`image_vex_gate.py`) apply the same predicate and print unfixed findings as
`report-only` lines.

Fixable findings are suppressed only via reviewed **OpenVEX** statements
(`tools/supply-chain/vex/`), passed to both scanners with `--vex` when
`openvex.json` exists with ≥1 statement. We never lower a threshold or disable a
scanner to go green.

### ⚠️ The 2026-03-19 Trivy-action compromise (standing rationale)

On **2026-03-19**, `aquasecurity/trivy-action` (tags 0.0.1–0.34.2) and **all**
`aquasecurity/setup-trivy` releases were compromised: imposter commits shipped a
**credential stealer**, and a malicious Trivy binary (**v0.69.4**, with tainted
Docker images v0.69.5/6) phoned home to a C2. This is the LiteLLM-class
supply-chain attack landing squarely in scanner tooling.

**Policy for this repo:** we NEVER use the aquasecurity (or anchore) GitHub
Actions. We install the official release **binary tarball** and verify its sha256
against a committed checksum. This is regression-locked by
`test_no_aquasecurity_actions`. Trivy **v0.72.0** (used here) is post-incident and
safe.

## Signing + provenance (SLSA L2)

- **Key-based cosign**, never keyless. Keyless (Fulcio/Rekor) requires network
  egress at verify time and breaks offline/air-gapped customer verification
  (C1/C2). We sign blobs with a self-managed key and emit offline bundles.
- **SLSA Level 2**: builds run on hosted GitHub Actions runners and emit a signed
  provenance attestation. `ne release provenance` produces an in-toto Statement
  with a `https://slsa.dev/provenance/v1` predicate (builder = the GH Actions
  workflow ref); `release.yml` signs it with `cosign attest-blob` using our key.
  We do **not** use `slsa-github-generator` (it assumes keyless).
- **Offline customer verification**: `tools/supply-chain/verify-release.sh` runs
  `cosign verify-blob` + `verify-blob-attestation` with `--insecure-ignore-tlog`
  against the committed `cosign.pub` — no Rekor, no network. See
  [signing.md](signing.md) for key provisioning.

## Dependency-intake checklist (SPEC §2)

Every new dependency must clear all of:

1. **OSI license** verified at the *pinned version*, in a C4-allowed class
   (banned: AGPL/SSPL/BUSL/FSL/fair-code/NC). **Transitive deps count** — see the
   `cyclonedx-bom → chardet (LGPL)` finding above.
2. **Pin + hash** via `uv.lock` (never hand-edited).
3. **CISA-KEV / security-history** check.
4. **Maintainer viability** (bus factor, release cadence).
5. **No telemetry**, or telemetry off by default.

Record the dependency in the owning package's `NOTICE`; the release aggregate is
`ne release notice`.

## CI tool inventory (pinned, verified 2026-07-03)

| Tool | Version | License | Install |
| --- | --- | --- | --- |
| Trivy | 0.72.0 | Apache-2.0 | pinned tarball + sha256 (never aquasecurity actions) |
| Grype | 0.115.0 | Apache-2.0 | pinned tarball + sha256 |
| cosign | 3.0.2 | Apache-2.0 | pinned binary + sha256 |
| uv | 0.8+ | Apache-2.0 / MIT | `astral-sh/setup-uv` (SHA-pinned) |
| CycloneDX SBOM | `ne release sbom` (bespoke stdlib, spec 1.6) | first-party (Apache-2.0) | no third-party tool — see SBOM section above |

## Owner follow-ups (repo-settings / provisioning)

- Provision the cosign key pair and `COSIGN_PRIVATE_KEY` / `COSIGN_PASSWORD`
  secrets; commit `tools/supply-chain/cosign.pub` (see [signing.md](signing.md)).
- Mark the `license-gate` job a **required status check** in branch protection so
  a banned dependency cannot merge.
- Security contact is GitHub private vulnerability reporting (published in `.github/SECURITY.md`).
