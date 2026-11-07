# Neutron Enterprise — Security Dossier v1 (questionnaire-ready)

**Product:** Neutron Enterprise — a customer-deployed AI platform, run entirely
inside the customer's own infrastructure (one customer, one deployment).
**Maintainer:** the Neutron Enterprise maintainer (github.com/rjunee).
**Scope of this dossier:** the **pilot (Compose) tier** as merged in this repo at
the commit it ships against (pre-v0 — no release tag cut yet; see
[supply-chain.md](supply-chain.md) and §D). It is installed via the
[pilot install guide](../pilot/install-guide.md).

> **Honesty rule (the core discipline of this document).** Every control below
> carries a status — **VERIFIED** (implemented and cited to code/tests in this
> tree), **PARTIAL** (implemented in part; the gap is stated), or **GAP** (not
> yet implemented; disclosed). An overclaim in a customer security dossier is a
> defect. Where the merged reality is narrower than the SPEC §7 aspiration, this
> document states the narrower reality.

---

## Section A — Day-one security controls (SPEC §7)

### A1. OIDC SSO on every surface — **PARTIAL**

Fail-closed OIDC authentication is implemented and CI-tested in `ne-identity`:
JWT verification with **JIT provisioning** and a **local-issuer-only trust
model**, with **algorithm-confusion closed structurally** and **opaque 401s** (a
caller cannot branch on *why* a token was rejected — no auth oracle;
`packages/ne-identity/src/ne/identity/errors.py:34`, `.../config.py`
"fail-closed at construction time", `.../factory.py` wires verifier + store +
JIT provisioner + middleware). The pilot deploys a Keycloak 26.6.4 broker in the
`core` profile (`deploy/compose/pilot/compose.yaml:102`).

**Why PARTIAL, not VERIFIED:** the pilot compose currently serves a **placeholder
on `443`** — there are no first-party application images in the repo yet
(`deploy/compose/pilot/caddy/Caddyfile:25-28`, README "Status / boundaries").
The identity spine exists and is tested, but no production application surface is
yet live *behind* it in the pilot. Closes before NE-077 when the console/gateway
images (NE-067 + packaging) land. **Owner unit:** pre-NE-077 app images.

### A2. Structured audit events + SIEM export via OTel — **PARTIAL**

The audit substrate is **VERIFIED**: `ne-audit` is a **hash-chained,
append-only** sink with **signed WORM checkpoints**
(`packages/ne-audit/src/ne/audit/__init__.py`, `engine.py`), and the operator
CLI `ne audit verify` / `checkpoint` / `replay` exists
(`tools/ne-cli/src/ne/cli/audit.py:30,120,170`). OTel spans/metrics/logs
plumbing and a collector are deployed (`obs` profile,
`deploy/compose/pilot/compose.yaml:255`).

**GAPs disclosed:**
- The pilot OTel collector ships the **`debug` (stdout) exporter only** —
  traces/metrics/logs are printed to the collector's own log, with **no shipped
  SIEM backend** (`deploy/compose/pilot/otel/otel-collector-config.yaml:20-37`).
  Wiring a customer SIEM is a documented config swap of that exporter.
- There is **no shipped audit-outbox → OTel-logs exporter** — the audit chain
  and the OTel pipeline are not yet joined by a first-party exporter class
  (verified absent across `packages/ne-events` and `packages/ne-obs`).
- **pgAudit is not enabled** in the pilot Postgres. A `pgaudit.py` module exists
  (`packages/ne-audit/src/ne/audit/pgaudit.py`), but the pilot's `ne-postgres-age`
  image (Chainguard PostgreSQL 18.6 + AGE) loads no pgAudit, and neither does the
  dev/Helm `postgres:18.6-alpine` (`shared_preload_libraries` unset in the pilot
  compose — verified absent). This is on the SPEC §11 pending-live-verification ledger.

**Owner units:** SIEM exporter + audit-log exporter; pgAudit enablement
(pre-production).

### A3. TLS 1.2+/1.3 everywhere — **VERIFIED (off-box boundary)**

Caddy is the **only** off-box listener and terminates TLS on both `443` and
`8443`; every other service binds `127.0.0.1` (on-VM only). This is
mechanically enforced by
`tools/ne-cli/tests/test_pilot_stack.py::test_caddy_is_the_only_off_box_publisher`
(lines 73–85: any non-`127.0.0.1` publish by a non-caddy service fails the test;
caddy must publish `443` + `8443`). Caddy's default TLS floor is 1.2 (1.3
preferred). On-VM loopback services are documented as on-box plaintext by
design (they are never reachable off the VM).

### A4. Secrets via SecretStore — no committed literals, no values in logs, no env file at rest (C12) — **VERIFIED (scope disclosed)**

Zero secret literals exist in the pilot tier: every credential is a
`${NE_PILOT_*:?...}` **fail-closed required interpolation**
(`deploy/compose/pilot/compose.yaml:43,82,116,122`). The guardian
`tools/ne-cli/tests/test_pilot_secret_hygiene.py` fails if any committed pilot
file carries a literal secret value. `ne up` resolves the two required secrets
into an **ephemeral `0600` env file that is deleted after compose returns**
(`up.py:_write_runtime_env` / `_remove_runtime_env`), and **values are never
echoed** (a missing secret is reported by env-var NAME only, `up.py:200-205`).
Envelope encryption of the store is delegated to `sops` + the customer KMS
(`packages/ne-secrets/README.md`). Pilot scope note: FileSecretStore is
single-process; `openbao` is the prod profile.

**Runtime-scope disclosure (so this claim is not read wider than it is):**
after `ne up`, the resolved Postgres/Keycloak passwords **do exist as process
environment variables inside the running `postgres` and `keycloak`
containers** — the stock upstream images accept credentials only via env
(`POSTGRES_PASSWORD`, `KC_BOOTSTRAP_ADMIN_PASSWORD`, `KC_DB_PASSWORD`;
`deploy/compose/pilot/compose.yaml:82,116,122`) — and are therefore readable
via the Docker API (`docker inspect`) by anyone with Docker-socket access. On
this single-VM tier, Docker-socket access is root-equivalent host access, so
this does not extend the trust boundary; it is disclosed so "no secrets in
env" is read as scoped to **no committed literals, no secret values in any log
or CLI output, and no env file at rest** — not as an absence of credentials
from the runtime container environment. Moving off container-env injection
(e.g. file-based secrets) is a possible post-pilot hardening (§D).

### A5. Encryption at rest with customer-managed keys — **PARTIAL**

Stated precisely, so no surface is overclaimed:
- **Backups: VERIFIED.** The **NEB1 envelope** — per-backup random 256-bit DEK,
  wrapped by the customer **KEK**, framed as chunked **AES-256-GCM**
  (`tools/ne-backup/src/ne/backup/crypto.py:3-12,32`). The KEK lives **only** in
  the bundle SecretStore at `pilot/backup` field `key_b64` and is never written
  to a manifest, artifact, or log (`tools/ne-backup/tests/test_c12_key_hygiene.py`).
- **Secrets: VERIFIED.** sops / customer-KMS envelope (A4).
- **Live Postgres / Qdrant volumes: delegated.** The live container volumes are
  **not** platform-encrypted; encryption-at-rest for them is provided by **host
  disk encryption** (Azure Disk Encryption / encryption-at-host), a hard
  prerequisite of the [install guide](../pilot/install-guide.md) (§1.1).

So "customer-managed keys" holds for **backups and secrets**; live-volume
at-rest encryption is a **host responsibility**, disclosed. **Owner unit:**
platform-managed live-volume encryption (post-pilot, if required).

### A6. SBOM + scan + sign + SLSA per release — **VERIFIED (machinery; no tag cut)**

- **SBOM:** `ne release sbom --all` emits per-member **CycloneDX 1.6**, bespoke
  stdlib-only from `uv.lock` (`tools/ne-cli/src/ne/cli/sbom.py`); release builds
  use `--deterministic` (content-derived uuid5 serials) so uploaded SBOMs
  byte-match the bundle manifest digests, fail-closed-verified in
  `.github/workflows/release.yml:40-71` (see [supply-chain.md](supply-chain.md)).
- **Scan:** `supply-chain.yml` runs **Trivy** (`CRITICAL,HIGH`, `--exit-code 1`,
  `--ignore-unfixed`) + **Grype** (`--fail-on high --only-fixed`) + reviewed
  **OpenVEX**, scanners installed as checksum-pinned binaries (never the
  compromised aquasecurity actions). The build fails on HIGH/CRITICAL findings
  that have a fix available; findings with no fix are report-only, listed in the
  job summary and the `vuln-reports` artifact.
- **Sign + SLSA:** **key-based cosign** `sign-blob` + in-toto **SLSA-v1**
  provenance per artifact, offline-verifiable (`docs/security/signing.md`).

**Disclosed:** the first `vX.Y.Z` tag has **not** been cut yet — `release.yml` is
provisioned and live-parked; the cosign key is an owner action
([signing.md](signing.md)).

### A7. License gate (C4) — **VERIFIED**

`ne check licenses` (`tools/ne-cli/src/ne/cli/licenses.py:732`) is a fail-closed
Python gate; **banned classes (AGPL/SSPL/BUSL/FSL/NC/…) are structurally
non-overridable** and UNKNOWN fails closed. The npm closure over the console is
gated by the same classifier (`tools/supply-chain/npm_license_gate.py`). Full
policy in [supply-chain.md](supply-chain.md).

### A8. Backup / restore with tested drill + RPO/RTO — **VERIFIED**

`.github/workflows/backup-restore-drill.yml` is an executable
backup→wipe→restore→verify drill; **RPO ≤ 24h**, **RTO ≤ 30min** asserted
(`NE_BACKUP_DRILL_RTO_SECONDS` default 900s; `deploy/backup/README.md`). Restore
is safe-by-construction with a pre-restore safety snapshot (`tools/ne-backup`).

### A9. Semver + N-1 support + reversible migrations — **VERIFIED (policy; pre-tag)**

`RELEASES.md` defines platform SemVer (a release = git tag + per-member SBOMs +
composed config-hash). Migrations are **fail-closed reversible**: every
`Migration` must declare a `down` step or an explicit `irreversible_justification`
(`tools/ne-cli/src/ne/cli/migrate.py:3-12`); `ne migrate status/up/down`
(`migrate.py:311,324,338`). N-1 upgrade discipline is policy;
first tag not yet cut (A6).

### A10. CVD policy + security contact + CRA — **VERIFIED (docs); alias provisioned 2026-07-17**

- CVD policy: `docs/security/cvd-policy.md` — 48h acknowledgment, 90-day default
  disclosure, severity patch SLAs, safe harbor.
- CRA: `docs/security/cra-readiness.md` — obligations from **2026-09-11**, the
  **24h / 72h / 14-day** ENISA-platform cadence runbook.
- **Resolved:** GitHub private vulnerability reporting is the published security contact (`.github/SECURITY.md`, cvd-policy.md).

### A11. Per-deployment isolation (follows from C1) — **VERIFIED (architectural)**

One customer = one deployment; no cross-customer state (architecture per
ADR-001). The **per-customer bundle** (`ne pilot promote`) parameterizes exactly
one customer/hostname; there are no shared-tenant code paths. Enforcement is
architecture review + the absence of multi-tenant routing.

*Framing note (2026-07-17):* this was previously tracked as a distinct constraint
(C10, "single-tenant invariant") and that constraint was **retired** — not
weakened. Single-vs-multi-tenant is a distinction that only exists for a *shared*
control plane; this is custom software deployed inside the customer's own network,
so there is no other tenant **by construction** and **C1 (self-host) already
implies it**. Every isolation property above remains true and verified — its own
database, vector store, identity broker, and keys per deployment. See SPEC §11.

### A11b. Audit-log integrity (C10) — **VERIFIED**

The audit log is **append-only and hash-chained**. Database-level triggers reject
UPDATE and DELETE, the module exposes no mutation API at all (asserted by
`packages/ne-audit/tests/test_append_only.py::test_module_exposes_no_update_or_delete_api`),
and every record binds to its predecessor by SHA-256 over canonical bytes
(`packages/ne-audit/src/ne/audit/store.py` `compute_record_hash`, with a UNIQUE
`record_hash` column and head-row `FOR UPDATE` serialization). `ne audit verify`
exits non-zero and **names the offending record**
(`tools/ne-cli/tests/test_audit_cli.py`).

Coverage is deliberately adversarial: payload tamper, broken link, missing record,
forged tail with head mismatch, genesis tamper, sequence-swap reordering, prefix
deletion, and an emptied table (`test_chain_verify.py`); exhaustive per-text-column
tamper (`test_column_tamper.py`); backdate-then-rehash and forged-record insertion
(`test_redteam_tamper.py`). These run on every PR and re-run against real Postgres
in the `live` CI job.

Signed HMAC checkpoints archived to WORM (`test_checkpoint.py`) close the residual
gap: an attacker privileged enough to rewrite the entire table could otherwise
recompute a consistent chain, but cannot forge a checkpoint without the signing key
and cannot delete an archived one under WORM retention.

The WORM target is the deployment's own storage, behind a pluggable `ObjectStore`
adapter: **AWS S3 with Object Lock (COMPLIANCE mode)**, **Azure Blob immutable
storage** (time-based retention), or an S3-compatible on-premises appliance.
Selecting one is configuration (`NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE`), not
code, so no audit-integrity guarantee is coupled to a single cloud vendor. In all
three the retention is enforced by the storage service itself and the platform can
only ever *extend* a retention, never shorten one. **Scope of the CI evidence, stated
plainly:** the in-CI conformance suite runs against a COMPLIANCE-mode-capable
container and proves the *adapter's* Object Lock logic — it is regression cover, not
an attestation about any customer's storage. Retention behaviour on the real backend
is verified against that deployment's own storage during the engagement, via the
owner-gated live evidence lane.

**Stated precisely, so this is not read wider than it is:** the guarantee is
**detection, not prevention**. An operator with sufficient database privilege can
still destroy or drop the log; what they cannot do is alter it *undetectably*.

**Scope disclosure:** checkpoint *scheduling* is on-demand today (`ne audit
checkpoint`); the automatic periodic loop is unit NE-AUDIT-CRON, in flight at the
time of writing. The chain, the triggers, and the verification path — the substance
of this control — are live and CI-blocking now.

### A12. C1/C2 sovereignty proof — **VERIFIED (strongest evidence in the repo)**

The **NE-073 no-egress CI harness** (`.github/workflows/no-egress.yml`,
`deploy/airgap/README.md`) installs the full NE-070 pilot stack **inside a
sealed network namespace** (staging veth pair + default route deleted for IPv4
**and** IPv6, plus a belt-and-braces `nftables` counter-drop) and proves, in the
same sealed run:
- **the frontier lane is provably dead** — `api.anthropic.com` / `api.openai.com`
  don't resolve, a raw-IP connect fails, and even a policy-bypassed `httpx` POST
  fails at the transport layer; *and* the software `EgressPolicy(air_gap=True)`
  raises `AirGapViolationError` before litellm is invoked (both layers);
- **the open-weight (CPU vLLM) lane keeps serving** real governed inference
  (a real CPU forward pass, not a mock) **while** egress is impossible.
- An **anti-vacuous canary** (`expect-reachable` before the seal) proves the seal
  is real, not a network that was already broken.

**The true story is stronger for being adversarially hardened:** during bring-up
a **vacuous-seal near-miss** was caught (SPEC §11, 2026-07-08) — the inner
dockerd had attached to the *host's* containerd, so "sealed" container tasks ran
in the host netns with full egress; the proof was vacuous until a dedicated
in-namespace containerd was launched and a container-run smoke added to the
readiness gate. This is disclosed as evidence the guarantee is tested against its
own failure modes. The full **signed offline bundle** (Zarf) for real air-gap
installs is a later unit — **NE-086 (v1.1)** — not shipped here.

---

## Section B — C1–C12 hard-constraint enforcement

Each SPEC §2 constraint, its enforcement mechanism, and its structural guardian
(the "guardian per constraint" methodology, plan §2.1). Verified present in this
tree.

| # | Constraint | Enforcement / structural guardian |
|---|---|---|
| **C1** | Self-host; no SaaS control plane / phone-home | NE-073 no-egress harness (`deploy/airgap`); dependency-intake review; telemetry-off flags on every pilot service |
| **C2** | Deny-by-default governed egress — no outbound call leaves except to an allowlisted destination, and every model call, connector pull, external tool call, and write-back is policy-checked + audited (any path can carry context out, so none is a "safe" lane); frontier APIs via gateway are the model-inference lane; `AIR_GAP` disables external egress | `EgressPolicy` + `AirGapViolationError` (ne-gateway C2 guardian, `test_no_egress.py`); egress allowlist in ne-govern; NE-073 network-level proof |
| **C3** | Every frontier call via `ne-gateway`; no provider SDK imported elsewhere | **import-linter** rule in CI (the guardian template); ne-net/ssrf is stdlib-only, no litellm import |
| **C4** | License gates (banned/out-of-process/case-by-case/free) | `ne check licenses` (Python) + `npm_license_gate.py` (console); banned non-overridable, UNKNOWN fail-closed (`test_license_gate.py`) |
| **C5** | Loose coupling via `ne-contracts` Protocols; swappable slots | contract-conformance test kit; frozen ne-contracts (1.2.0) |
| **C6** | Every tool call → PDP **and** audit event; no bypass | `GovernedToolExecutor` — the session never reaches a handler without it (`packages/ne-runtime/src/ne/runtime/agent.py:11`); real-OPA-in-CI; audit-completeness test; Checkpoint-D boundary audit closed 8 cross-seam bypasses |
| **C7** | Knowledge plane read **and** write, tier-enforced; fail-closed ACL trimming | ne-permissions ACL model + `sovereignty.py`/`expand.py`; C7 ACL guardian (NE-043) trimming both read paths |
| **C8** | KPI-first instrumentation (`kpi_tag`) | event schema (NE-005); report templates lead with named KPIs |
| **C9** | Deploy anywhere; profile-agnostic OCI images; no cloud-specific dep | image-portability review; Azure is a **test target**, not a dependency |
| **C10** | Audit-log integrity — append-only + hash-chained; tamper/deletion/reordering/truncation detectable | DB-level append-only triggers + no mutation API (`packages/ne-audit/tests/test_append_only.py`); SHA-256 chain over canonical bytes (`store.py` `compute_record_hash`, UNIQUE `record_hash`); verification suites `test_chain_verify.py` / `test_column_tamper.py` / `test_redteam_tamper.py`; `ne audit verify` exit-code contract (`tools/ne-cli/tests/test_audit_cli.py`); signed WORM checkpoints (`test_checkpoint.py`). **Detection, not prevention** — see A11b. |
| **C11** | Supply chain from first commit (SBOM/scan/sign/SLSA/pin+hash) | `uv.lock` pin+hash + `ne check lock`; digest-pinned images; SHA-pinned actions (`test_workflow_pins.py`); checksum-pinned scanners |
| **C12** | Deterministic guardrails BEFORE model judgment; never-auto-approve Tier-3 | ne-govern deterministic guardrails (`packages/ne-govern/src/ne/govern/config.py:154-182`); HITL tiers (§4.2), never-auto-approve Tier-2/3; red-team suite |

---

## Section C — Questionnaire-theme mapping

A security team can answer a standard questionnaire from the rows below (each
points at Section A/B evidence).

| Questionnaire theme | Controls (this dossier) |
|---|---|
| **Access control / IAM** | OIDC SSO fail-closed + JIT (A1, PARTIAL — surface placeholder); PDP-on-every-tool-call, no bypass (B/C6); HITL never-auto-approve Tier-3 (B/C12); per-deployment isolation (A11 — follows from C1) |
| **Data protection / encryption** | TLS 1.2+/1.3 off-box (A3); secrets via SecretStore — no committed literals, no values in logs, no env file at rest (A4/C12; runtime container-env residence disclosed); at-rest: backups + secrets platform-encrypted, live volumes via host disk encryption (A5, PARTIAL) |
| **Supply chain** | SBOM + Trivy/Grype/VEX + cosign + SLSA (A6/C11); license gate (A7/C4); pinned+hashed deps, digest-pinned images, SHA-pinned actions (C11) |
| **Logging / monitoring / incident response** | Hash-chained tamper-evident audit + signed checkpoints + `ne audit verify` (A2 VERIFIED); SIEM export = config swap + no shipped audit-log exporter yet (A2 GAP); CVD + CRA runbooks (A10) |
| **Sovereignty / data residency** | Self-host, no phone-home (C1); `AIR_GAP` frontier-lane kill proven in a sealed netns while the open-weight lane serves (A12/C2); one customer / one deployment in the customer's own VPC (A11 — follows from C1) |
| **Operational resilience** | Backup/restore with tested drill, RPO ≤24h / RTO ≤30min (A8); reversible fail-closed migrations + SemVer + N-1 (A9); safety-snapshot-before-restore (A8) |

---

## Section D — Open gaps register (PARTIAL/GAP summary)

| Gap | Status | Owner unit |
|---|---|---|
| No first-party app images — `443` is a placeholder; no production OIDC surface live yet | PARTIAL (A1) | pre-NE-077 app images (NE-067 + packaging) |
| OTel collector ships `debug` exporter only — no shipped SIEM backend | GAP (A2) | SIEM exporter (config swap + exporter class) |
| No audit-outbox → OTel-logs exporter shipped | GAP (A2) | audit-log exporter |
| pgAudit not enabled in pilot Postgres | GAP (A2) | pgAudit enablement (pre-production) |
| Live-volume at-rest encryption delegated to host disk encryption | PARTIAL (A5) | platform-managed live-volume encryption (if required) |
| DB/Keycloak passwords live in runtime container env (stock-image constraint; Docker socket = root-equivalent on this tier) | Disclosed (A4) | file-based secret injection (post-pilot hardening, if required) |
| Private vulnerability reporting channel published | DONE (A10, 2026-07-17) | owner |
| No `vX.Y.Z` release tag cut; cosign key not provisioned | Disclosed (A6/A9) | first release cut + owner key provisioning |
| Third-party penetration test not yet performed | Required before first production deployment (SPEC §7) | see [pen-test-readiness.md](pen-test-readiness.md) |
| NE-070-FOLLOWUP installer items (disk preflight, port preflight, `ne down` secrets, manifest integrity) | RESOLVED (merged) — residual: `manifest_hash` is tamper-evidence not authentication (verify out-of-band; cosign bundle signing = v1.1) | v1.1 (cosign) |

---

*Companion documents:* [pilot install guide](../pilot/install-guide.md) ·
[pen-test-readiness checklist](pen-test-readiness.md) ·
[supply-chain security](supply-chain.md) · [release signing](signing.md) ·
[CVD policy](cvd-policy.md) · [CRA readiness](cra-readiness.md).
