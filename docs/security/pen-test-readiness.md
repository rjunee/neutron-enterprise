# Neutron Enterprise — Penetration-Test Readiness Checklist v1

Companion to the [security dossier](security-dossier.md). A **third-party
penetration test is required before the first production deployment** (SPEC §7);
this checklist scopes that engagement and records the current, code-verified
readiness of each item so the tester (and the customer's security team) start
from an accurate baseline. Each item carries a status **CLOSED** (implemented +
cited), **PARTIAL**, or **OPEN** (a real readiness gap to fix).

---

## 1. Scope and rules of engagement (skeleton)

- **Target:** one hardened Azure VM running the pilot (Compose) tier, deployed
  per the [install guide](../pilot/install-guide.md).
- **External attack surface:** exactly two off-box ports, both TLS-terminated by
  Caddy — `443` (console/gateway slot, currently a placeholder) and `8443`
  (Keycloak). Every other service binds `127.0.0.1` (on-VM only).
- **Internal surface (on-VM, for an assumed-foothold / post-exploitation phase):**
  Postgres `5432`, OPA `8181`, Qdrant `6333/6334`, TEI `8085/8086`, OTel
  `4317/4318` — all loopback.
- **Deployment isolation:** one customer = one deployment, in the customer's own
  environment — a consequence of C1 (self-host), not a separate constraint (the
  former C10 was retired 2026-07-17; see SPEC §11 and dossier A11).
- **Vendor selection:** open item (SPEC §12) — the customer/maintainer select an
  accredited third party.

---

## 2. Checklist items (status + citation)

### [a] No secrets in logs — **CLOSED**

*The mandated line item.* NE-012's Codex P2 finding (embedded userinfo
credentials, e.g. `https://user:secret@host`, echoed unredacted in
SSRF / config-validation error messages) **was fixed and has landed.**

- **Note on provenance:** the fix landed via **NE-014**, *not* NE-032 — SPEC
  §CURRENT line 22 carries a stale "via NE-032" reference. The code is present
  and tested regardless of which unit is credited.
- **Redaction (verified):** `redact_url_for_logs`
  (`packages/ne-gateway/src/ne/gateway/ssrf.py:58`) masks netloc userinfo
  unconditionally — `https://user:pass@host` → `https://***:***@host`, and a
  bearer-style `https://token@host` → `https://***@host`. It also handles the
  URLs `urllib` does *not* parse into a `//netloc` (scheme-less
  `user:pass@host/v1`, single-slash `https:/user:pass@host`) by pre-masking with
  `_USERINFO_RE` (`ssrf.py:41,87-92`). The helper is **total — it never raises**
  (a malformed/out-of-range port is tolerated, not fatal, `ssrf.py:98-103`).
- **Applied at the error seam:** `validate_endpoint_url` passes
  `redact_url_for_logs` as the message decorator so every gateway SSRF error
  carries the redacted URL, never the raw one (`ssrf.py:124-165`).
- **Shared validator never echoes URLs/userinfo:** the consolidated
  `ne.net.ssrf` (used by ne-gateway / ne-brain / ne-serve since NE-CKC10)
  **rejects credentialed URLs outright** and, when no redactor is supplied,
  its messages name **at most the parsed hostname / scheme — never the URL and
  never userinfo** (`packages/ne-net/src/ne/net/ssrf.py:120-140`).
- **Tests (re-run in this worktree — 74 passed):**
  `packages/ne-gateway/tests/test_ssrf.py::test_redact_url_for_logs_masks_userinfo`,
  `::test_redact_url_for_logs_masks_username_only_userinfo`,
  `::test_redact_url_for_logs_masks_sensitive_query_keys`,
  `::test_redact_url_for_logs_handles_malformed_port_without_raising`.
- **Broader secret-in-log hygiene:** the C12 guardian
  `tools/ne-cli/tests/test_pilot_secret_hygiene.py`; `ne up` reports missing
  secrets by NAME only (`up.py:200-205`); `ne-backup keygen` prints only a
  fingerprint (`tools/ne-backup/tests/test_c12_key_hygiene.py`).

### [b] AuthN / AuthZ — **PARTIAL**

Fail-closed OIDC verification, algorithm-confusion closed structurally, no auth
oracle in 401 responses (`packages/ne-identity/src/ne/identity/errors.py:34`);
PDP-on-every-tool-call with no bypass (C6,
`packages/ne-runtime/src/ne/runtime/agent.py:11`); never-auto-approve
Tier-2/Tier-3 (C12). **PARTIAL because** the production application surface is
not yet live behind OIDC in the pilot (dossier A1 — `443` placeholder).

### [c] Injection resistance — **CLOSED (guardrails present)**

C12 deterministic guardrails run in code **before** model judgment
(`packages/ne-govern/src/ne/govern/config.py:154-182`); ne-govern red-team
suite; tool-arg validation; `AgentScopeGuardrail` wired at the composition root
(Checkpoint-D boundary audit closed the "wired into zero roots" gap). Prompt
injection is assumed by design (C12).

### [d] SSRF / egress — **CLOSED**

Shared `ne.net.ssrf` validator rejects metadata IPs (`169.254.169.254` /
`metadata.google.internal`), private/loopback ranges (policy-gated),
packed-integer IPv4 forms, credentialed URLs, and query/fragment
(`packages/ne-net/src/ne/net/ssrf.py:105-176`); egress allowlist + `AIR_GAP` in
ne-govern; NE-073 network-level no-egress proof (dossier A12).

### [e] Audit tamper-evidence — **CLOSED**

Hash-chained append-only audit + signed WORM checkpoints + `ne audit verify`
(`packages/ne-audit/src/ne/audit/__init__.py`;
`tools/ne-cli/src/ne/cli/audit.py:30`).

### [f] Container / VM hardening — **CLOSED (compose-level)**

`no-new-privileges:true` on every pilot service; `cap_drop: [ALL]` on the
stateless proxies/sidecars (caddy adds back only `NET_BIND_SERVICE`); resource
limits + healthchecks on all; digest-pinned images; loopback binds; caddy-only
off-box publisher guardian
(`deploy/compose/pilot/compose.yaml`; `tools/ne-cli/tests/test_pilot_stack.py`).

### [g] Supply chain — **CLOSED (machinery; no tag cut)**

Trivy + Grype + VEX fail-closed in CI on HIGH/CRITICAL findings with a fix
available (unfixed findings are report-only, listed in the job summary and the
`vuln-reports` artifact); SHA-pinned actions; checksum-pinned
scanner binaries (never the compromised aquasecurity actions); key-based cosign
+ SLSA-v1; per-member CycloneDX SBOM (deterministic serials on release). See
[supply-chain.md](supply-chain.md). Disclosed: no `vX.Y.Z` tag cut yet.

### [h] Key management — **CLOSED**

Customer KEK lifecycle: `ne-backup keygen` (fingerprint-only, refuses overwrite
without `--rotate`, no escrow — losing the key is unrecoverable by design);
sops customer-KMS for secrets; agent-credential rotation (NE-036).

### [i] Backup / restore integrity — **CLOSED**

Verify-before-restore (manifest → KEK fingerprint → ciphertext + plaintext
hashes, all before any destructive step), safety-snapshot-first, and mandatory
tar traversal/symlink/hardlink escape guards
(`tools/ne-backup/src/ne/backup/tarsafe.py`, `.../runtime.py`).

---

## 3. Open pen-test-readiness gaps (fix before the production test)

From the dossier's open-gaps register (Section D):

- **Pilot application surface is a placeholder** — no production console/gateway
  image behind OIDC yet (pre-NE-077).
- **SIEM export is a config swap, and no audit-log exporter ships yet** — a
  tester should note that audit → SIEM is not wired end-to-end in the pilot.
- **pgAudit not enabled** in the pilot Postgres.
- **NE-070-FOLLOWUP installer items — RESOLVED** (disk preflight stats the
  data-root, conditional `443/8443` preflight, secret-free `ne down`, and a
  `manifest_hash` integrity check over `customer`/`hostname`). **Residual:** the
  `manifest_hash` is tamper-evidence, not authentication — a tester should
  confirm the operator runbook includes the **out-of-band** `config_hash`/
  `manifest_hash` comparison; cosign bundle signing is the v1.1 hardening.
- **FileSecretStore is single-process**; **no operator secret-seeding CLI** yet.
- **Runtime container env carries the Postgres/Keycloak passwords** (stock
  upstream images accept credentials only via env) — visible via `docker
  inspect` to anyone with Docker-socket access (root-equivalent on this
  single-VM tier). Disclosed in dossier A4; a foothold-phase tester should
  treat the Docker socket as a crown-jewel target.
- **No first release tag cut**; **cosign key not yet provisioned**.

---

## 4. CVD, security contact, and CRA reporting

- **CVD policy is live:** [cvd-policy.md](cvd-policy.md) — 48h acknowledgment,
  severity-based patch SLAs (Critical 14d / High 30d / Medium 90d), 90-day
  default coordinated disclosure, safe harbor.
- **Security intake:** `.github/SECURITY.md` routes to the CVD policy.
- **Security contact:** GitHub private vulnerability reporting, published in `.github/SECURITY.md`.
- **CRA reporting readiness:** [cra-readiness.md](cra-readiness.md) — obligations
  apply from **2026-09-11**; the statutory cadence via the ENISA single
  reporting platform is **≤ 24h early warning / ≤ 72h notification / ≤ 14-day
  final report** for an actively exploited vulnerability or severe incident. The
  CVD intake is the trigger that starts the clock.
- **Pre-2026-09-11 readiness task list** (from cra-readiness.md): identify the
  ENISA entry point + coordinating CSIRT; provision `security@`; dry-run the 24h
  early-warning path; confirm SBOM + provenance attach to every release so a
  report can cite exact affected component versions.
