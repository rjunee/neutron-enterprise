# ne-secrets

Implements the FROZEN `ne.contracts.secrets.SecretStore` Protocol — Vault
KV-v2 semantics (versioned reads, soft-delete, undelete, permanent destroy,
prefix listing) — with two adapters selected via the `ne.plugins.secret_store`
plugin registry:

| plugin name  | class                              | profile             |
|--------------|-------------------------------------|----------------------|
| `sops-file`  | `ne.secrets.file_store.FileSecretStore` | **pilot default** |
| `openbao`    | `ne.secrets.openbao.OpenBaoSecretStore` | prod              |
| `memory`     | `ne.contracts.testing.memory.MemorySecretStore` | test reference (ships in ne-contracts) |

## FileSecretStore (`sops-file`) — pilot default

A versioned KV-v2 JSON document on disk, behind a codec seam:

- `PlainJsonCodec` — no encryption, dev/CI only.
- `SopsCodec` — shells out to the `sops` binary, which supplies the
  customer-KMS envelope (age / AWS KMS / Azure Key Vault / GCP KMS) via a
  `.sops.yaml` creation-rules file. This package implements zero
  cryptography itself; `sops` does all of it.

Every write re-encrypts the whole document (sops output is non-deterministic
by design — do not assert on ciphertext stability).

**Limitation**: single-process. Coordination is a single `asyncio.Lock`; there
is no cross-process locking. This is acceptable pilot scope — `OpenBaoSecretStore`
is the concurrent/prod profile.

**`sops` timeout (`NE_SECRETS_FILE__SOPS_TIMEOUT_SECONDS`, default `30.0`).** Every
`sops` invocation runs with a wall-clock `timeout=` and `stdin=DEVNULL`. Under
`AIR_GAP` a KMS-unreachable `sops` would otherwise retry network I/O forever (or an
age/PGP backend would block prompting on inherited stdin), wedging the single
store lock and hanging every subsequent read/write. A timeout instead raises
`SecretsBackendError` (message carries no plaintext) and releases the lock.

### `.sops.yaml` examples (placeholder keys only)

age (local dev):

```yaml
creation_rules:
  - path_regex: .*
    age: age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq  # PLACEHOLDER
```

AWS KMS:

```yaml
creation_rules:
  - path_regex: .*
    kms: arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000000  # PLACEHOLDER
```

Azure Key Vault:

```yaml
creation_rules:
  - path_regex: .*
    azure_keyvault: https://PLACEHOLDER-VAULT.vault.azure.net/keys/PLACEHOLDER-KEY/00000000000000000000000000000000
```

sops version expectation: **>= 3.10** (re-verified 2026-07-03: current release
v3.13.2, MPL-2.0). Invoked as an external binary via `subprocess` — no sops
code is distributed with this package.

## OpenBaoSecretStore (`openbao`) — prod profile

Speaks the KV-v2 REST API via stdlib `urllib` inside `asyncio.to_thread` — ZERO
new Python dependencies. All cryptography/storage is OpenBao's job; this
adapter is a thin, six-call REST mapping (read/write/delete/undelete/destroy/list).

Dev instance (from the NE-006 compose stack):

```
docker compose -f deploy/compose/dev/compose.yaml up -d openbao
# or: ne dev up
```

- URL: `http://localhost:8200`
- dev root token: `ne-dev-root`
- KV-v2 mount: `secret`

OpenBao version: compose pins `openbao/openbao:2.5.5` (re-verified
2026-07-03; 2.6.x went GA 2026-06-22 with an identical KV-v2 API surface — the
compose pin is owned by the NE-006/compose lane, not this package). MPL-2.0,
Linux Foundation.

**Egress hardening (C2/C12).** The OpenBao client uses a dedicated urllib opener
built once with `ProxyHandler({})`, so a set `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`
can NEVER route the `X-Vault-Token` (or secret payloads) in cleartext through an
unsanctioned proxy. The opener also refuses any cross-host redirect (raising
`SecretsBackendError`) so the token is never replayed to a foreign host.

**Fail-closed status mapping.** `read()` maps ONLY a `404` to `SecretNotFoundError`
(confirmed absence). Every other non-200 — `403`, `429` (standby/rate-limit),
`412` (stale-index), `400`, `5xx`, or a `200` with an empty body — raises
`SecretsBackendError`, so a transient backend state is never misreported as "the
secret does not exist".

## Configuration — `SecretsSettings`

Layered per `NeSettings` (init kwargs > env > `ne.toml` > defaults):

| field                        | env var                       | default                  |
|-------------------------------|--------------------------------|--------------------------|
| `store`                       | `NE_SECRETS_STORE`              | `sops-file`              |
| `file.path`                   | `NE_SECRETS_FILE__PATH`         | `secrets.ne.json`        |
| `file.mode`                   | `NE_SECRETS_FILE__MODE`         | `sops` (`plain` for CI)  |
| `file.sops_binary`            | `NE_SECRETS_FILE__SOPS_BINARY`  | `sops`                   |
| `file.sops_config`            | `NE_SECRETS_FILE__SOPS_CONFIG`  | `None`                   |
| `file.sops_timeout_seconds`   | `NE_SECRETS_FILE__SOPS_TIMEOUT_SECONDS` | `30.0`           |
| `openbao.url`                 | `NE_SECRETS_OPENBAO__URL`       | `http://localhost:8200`  |
| `openbao.mount`                | `NE_SECRETS_OPENBAO__MOUNT`     | `secret`                 |
| `openbao.base_path`           | `NE_SECRETS_OPENBAO__BASE_PATH` | `""`                     |
| `openbao.token_env_var`       | `NE_SECRETS_OPENBAO__TOKEN_ENV_VAR` | `BAO_TOKEN`         |
| `openbao.timeout_seconds`     | `NE_SECRETS_OPENBAO__TIMEOUT_SECONDS` | `5.0`              |

**Security note**: the OpenBao token is NEVER a settings field — only
`token_env_var` (the NAME of an environment variable) is configurable. The
adapter factory reads `os.environ[token_env_var]` at construction time. This
keeps token material out of TOML files and out of the `config_hash` preimage
(SPEC §4).

```python
from ne.secrets import SecretsSettings, create_secret_store

settings = SecretsSettings(store="openbao")  # openbao.token_env_var defaults to BAO_TOKEN
store = create_secret_store(settings)
```

## `.env` import bridge

```python
from pathlib import Path
from ne.secrets import import_env_file

await import_env_file(store, Path(".env"), "app/dev-secrets")
```

`parse_env_file` handles `KEY=VALUE`, an optional `export ` prefix, one layer
of matching quotes, and blank/`#`-comment lines. A malformed line raises
`ValueError` naming the line NUMBER only — never the line content, which may
hold a secret.

## Test-skip matrix

| test class                                | runs in bare CI? | prerequisite |
|--------------------------------------------|-------------------|--------------|
| `TestFileSecretStorePlainConformance`      | YES, always       | none |
| unit tests (file store, sops-codec plumbing, OpenBao mapping, config/factory, envfile) | YES, always | none (fake binaries / monkeypatched transport) |
| `TestFileSecretStoreSopsConformance`       | YES in ci.yml `check` lane (pinned `sops` v3.13.2 + `age` v1.3.1 installed, must-run gate); skips locally without binaries | `sops` (>=3.10) + `age-keygen` on PATH |
| `TestOpenBaoSecretStoreConformance`        | skips             | reachable OpenBao (`ne dev up`) |

## Dependency intake (re-verified 2026-07-03, build time)

- **SOPS** (getsops/sops): v3.13.2, MPL-2.0. Invoked as an external binary —
  no code distributed, no Python pin/hash applies. **Re-verified 2026-07-10:**
  v3.13.2 (2026-06-30) is still the current GA release; the ci.yml `check` lane
  now installs it pinned + sha256-verified (`sops-v3.13.2.linux.amd64` →
  `154dfe4c…`) so the real-seam conformance test runs under a must-run gate.
- **age** (FiloSottile/age): v1.3.1 (2025-12-28), BSD-3-Clause. Added
  **2026-07-10** as a **CI-test-only** binary — installed pinned + sha256-verified
  (`age-v1.3.1-linux-amd64.tar.gz` → `bdc69c09…`) purely to drive the sops
  conformance seam in CI. NOT distributed with this package and NOT a runtime
  dependency, so no `NOTICE` change is required.
- **OpenBao**: 2.5.5 dev-pin (compose), 2.6.x current GA, MPL-2.0. External
  service, not distributed.
- **KMS/crypto Python libs**: none added — envelope encryption is fully
  delegated to the `sops` binary.
- **httpx**: evaluated and REJECTED for the OpenBao client. Its stewardship
  moved to Pydantic as "httpx2" (2.5.0, 2026-06-25) while the original
  `encode/httpx` package's own security-update path became uncertain — stdlib
  `urllib` via `asyncio.to_thread` was chosen instead (six fixed REST calls,
  zero supply-chain surface).
- **ne-contracts** (internal, workspace dep): 1.0.0, FROZEN — consumed
  unmodified.

Net new third-party Python dependency: **none**.
