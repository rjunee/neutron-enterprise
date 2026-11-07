# Neutron Enterprise — Pilot Install Guide (Compose tier)

**Audience:** the engineer performing the NE-077 pilot deployment on a fresh,
hardened Azure VM. **This document is the deployment runbook the gate installs
from.** Every command, flag, profile name, port, env var, and secret path below
is verified against the merged NE-070 installer (`tools/ne-cli`,
`deploy/compose/pilot`) and NE-100 backup tool (`tools/ne-backup`,
`deploy/backup`) at the commit this guide ships against.

Security posture claims are in the companion
[security dossier](../security/security-dossier.md); this guide is the
*procedure*. Where the installer has a tracked gap, it is flagged inline under
**§10 Known limitations** rather than papered over.

---

## 0. Scope and version stamp

- **Tier:** Compose (single-VM) pilot — the `deploy/compose/pilot` stack driven
  by `ne up` / `ne down`. The Helm/K8s production tier is v1.1 (NE-071/072), out
  of scope here.
- **Platform version:** **pre-v0** — no `vX.Y.Z` git tag has been cut yet. The
  SemVer policy exists (`RELEASES.md`) but the first tag lands at a Checkpoint /
  first deploy. Record the exact `git rev-parse HEAD` of the repo checkout you
  deploy from; that commit *is* your build identity until a tag exists.
- **What is NOT yet in the box:** there are no first-party application (console /
  gateway) images in the repo yet. The pilot `443` vhost serves a placeholder
  (see §5, §10). This guide brings up the full *infrastructure* stack (identity,
  DB, PDP, vector store, embeddings, collector, TLS ingress) that the app images
  will sit on once they land (pre-NE-077, NE-067 packaging).

---

## 1. Prerequisites

### 1.1 Host

- **Azure VM, Ubuntu 24.04 LTS**, 16–32 vCPU / ≥ 32 GiB RAM (the pilot compose
  resource limits are sized for a 16–32 vCPU reference VM — see
  `deploy/compose/pilot/README.md`). `--serve-cpu` and the brain tier (two TEI
  services at ~4 GiB each) push memory; size accordingly.
- **≥ 10 GiB free on `/`.** `ne up`'s `_check_disk` preflight fails closed below
  10 GiB *on the root filesystem specifically* (`shutil.disk_usage("/")`,
  `up.py`). ⚠️ **Tracked gap (NE-070-FOLLOWUP):** the preflight always stats `/`
  even when Docker's data-root lives on another volume (e.g. `/mnt`). If your
  Docker data-root is elsewhere, keep `/` above 10 GiB anyway or the preflight
  will refuse to start. See §10.
- **Host disk encryption ENABLED.** Enable Azure Disk Encryption or
  encryption-at-host on the OS + data disks. **This is what provides
  encryption-at-rest for the live Postgres and Qdrant volumes** — the platform
  does not separately encrypt live container volumes (only backups and the
  SecretStore are platform-encrypted). This is a hard prerequisite, not
  optional; see the dossier's at-rest-encryption section (control A5).

### 1.2 Network / NSG

- **Only `443` and `8443` are published off-box** (`_OFF_BOX_PORTS = (443,
  8443)` in `up.py`; Caddy is the only off-box publisher). Configure the NSG /
  host firewall to allow **inbound `443` and `8443`** (+ admin SSH `22` from
  your bastion / management CIDR only). Every other service binds `127.0.0.1`.
- Both `443` and `8443` must be **free** at `ne up` time — the installer
  bind-probes them and fails with `port <n> is already in use` if not.
- A **DNS A record** for the pilot hostname (the FQDN you pass to
  `ne pilot promote --hostname`) resolving to the VM's public IP.

### 1.3 Software

- **Docker Engine + the Compose plugin, `docker compose` ≥ 2.20** (`_MIN_COMPOSE
  = (2, 20)` — required for compose `include` support). `ne up` checks the
  daemon is reachable (`docker info`) and the plugin version.
- **`uv`** (the workspace runner). Clone the repo and sync the workspace:

  ```sh
  git clone <repo-url> neutron-enterprise && cd neutron-enterprise
  uv sync --all-packages
  ```

  `ne` and `ne-backup` are uv-workspace console scripts (entry points
  `ne = "ne.cli:main"`, `ne-backup = "ne.backup.cli:main"`). **The canonical
  invocation on the VM is `uv run ne …` and `uv run ne-backup …`** from the repo
  root. Every command below uses that form.
- **`sops` ≥ 3.10** on `PATH` — **only if** you use the `sops` SecretStore codec
  (§2). The pilot default codec is `sops` (customer-KMS envelope); the `plain`
  codec needs no binary but is dev/CI only and unacceptable for a real pilot.
- **`nvidia-container-toolkit`** — **only if** you intend `--serve-gpu`. The GPU
  host is Azure-owner-gated; the tested serving path for a pilot is
  `--serve-cpu` (tiny-model CPU vLLM). Without an NVIDIA runtime, `--serve-gpu`
  fails loud (`no NVIDIA runtime detected`).

---

## 2. SecretStore setup (C12)

No secret value ever lives in a compose file, env file at rest, or log line. The
installer resolves secrets from a per-customer **SecretStore** at `ne up` time
into an ephemeral `0600` env file that is deleted the moment compose returns.

### 2.1 Choose a store location OUTSIDE the bundle

The SecretStore's on-disk file **must not** live inside the bundle directory you
will create in §3: `ne pilot verify` rejects any unrecorded file inside the
bundle, so a plaintext/sops secrets file there would fail verification (this is
exactly why `deploy/airgap/scripts/make_ci_bundle.py` puts the store in a
separate `--store-dir`). Put it somewhere like `/opt/neutron/secretstore/`.

```sh
sudo mkdir -p /opt/neutron/secretstore && sudo chmod 700 /opt/neutron/secretstore
```

### 2.2 Write `ne-secrets.toml` (sops / customer-KMS envelope)

The pilot default store is `sops-file` with the `sops` codec — this package
implements **zero cryptography itself**; `sops` supplies the customer-KMS
envelope (age / AWS KMS / Azure Key Vault / GCP KMS) via a `.sops.yaml`
creation-rules file. Create `/opt/neutron/secretstore/ne-secrets.toml`:

```toml
store = "sops-file"

[file]
path = "/opt/neutron/secretstore/secrets.ne.json"
mode = "sops"
# sops_binary defaults to "sops"; set sops_config to your .sops.yaml if it is
# not auto-discovered from the working directory.
# sops_config = "/opt/neutron/secretstore/.sops.yaml"
```

Create `.sops.yaml` naming your customer key. **Placeholder keys shown — replace
with the customer's real key IDs; never commit real key material.** (Copied from
`packages/ne-secrets/README.md`.)

Azure Key Vault (the reference target):

```yaml
creation_rules:
  - path_regex: .*
    azure_keyvault: https://PLACEHOLDER-VAULT.vault.azure.net/keys/PLACEHOLDER-KEY/00000000000000000000000000000000
```

AWS KMS:

```yaml
creation_rules:
  - path_regex: .*
    kms: arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000000  # PLACEHOLDER
```

age (dev-pilot only):

```yaml
creation_rules:
  - path_regex: .*
    age: age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq  # PLACEHOLDER
```

> **FileSecretStore is single-process** (one `asyncio.Lock`, no cross-process
> locking) — acceptable at pilot scope; `openbao` is the concurrent/prod
> profile (SPEC §3.3). Do not run concurrent writers against the pilot store.

### 2.3 Seed the two required secrets

The pilot requires exactly two secrets (from `pilotcfg.REQUIRED_SECRETS`):

| Env var | SecretStore path | field |
|---|---|---|
| `NE_PILOT_POSTGRES_PASSWORD` | `pilot/postgres` | `password` |
| `NE_PILOT_KEYCLOAK_ADMIN_PASSWORD` | `pilot/keycloak` | `admin_password` |

⚠️ **There is no `ne secret set` operator CLI yet** (tracked gap, candidate
NE-070-FOLLOWUP — `ne up`'s own error message says "populate them, e.g. via `ne`
secret tooling", which does not yet exist as a subcommand). Seed them with a
short one-off Python snippet that uses the same `ne.secrets` API the CI seeder
(`make_ci_bundle.py`) uses. **It never prints a value.** Choose strong random
values (e.g. from `python -c "import secrets;print(secrets.token_urlsafe(24))"`)
out of band and paste them in a shell that is not persisted to history:

```python
# seed_pilot_secrets.py — run once: uv run python seed_pilot_secrets.py
import asyncio, os
from ne.secrets import SecretsSettings, create_secret_store

async def main() -> None:
    settings = SecretsSettings.from_config_file(
        "/opt/neutron/secretstore/ne-secrets.toml"
    )
    store = create_secret_store(settings)
    # Values are read from the environment so they are never hard-coded here.
    await store.write("pilot/postgres", {"password": os.environ["PG_PW"]})
    await store.write(
        "pilot/keycloak", {"admin_password": os.environ["KC_PW"]}
    )
    print("seeded pilot/postgres and pilot/keycloak (values not shown)")

asyncio.run(main())
```

```sh
PG_PW='<generated>' KC_PW='<generated>' uv run python seed_pilot_secrets.py
```

(The backup KEK — a third secret at `pilot/backup` field `key_b64` — is
provisioned later by `ne-backup keygen`, not here; see §7.)

---

## 3. Promote the per-customer bundle

A bundle parameterizes the hardened pilot for one customer. It is reproducible
(same inputs → identical `config_hash` and byte-identical `manifest.json`) and
carries secret **names/paths only, never values** (C12).

```sh
uv run ne pilot promote \
  --customer <customer-name> \
  --hostname <pilot.fqdn> \
  --out /opt/neutron/bundle \
  --secrets-config /opt/neutron/secretstore/ne-secrets.toml
  # optional: --runtime-config <ne-runtime.toml>  --govern-config <ne-govern.toml>
```

This writes:

```
/opt/neutron/bundle/
  config/ne-runtime.toml     # empty = platform defaults
  config/ne-govern.toml
  config/ne-secrets.toml      # copied verbatim from --secrets-config
  secrets/required.json       # env var -> {path, field}  (NAMES only)
  manifest.json               # {schema_version, customer, hostname, config_hash, files:{...sha256}}
```

`config_hash` is the composed **NE-CKC12 platform hash** over the `ne-runtime` +
`ne-govern` module set — the **same hash stamped on every `AuditEvent`** — so a
bundle's identity ties to the running platform's config identity. The output
prints `customer`, `hostname`, and `config_hash`; record the `config_hash`.

Verify the bundle is intact (tamper-evident — names the exact mismatched file on
any tampered byte):

```sh
uv run ne pilot verify /opt/neutron/bundle
# -> bundle OK: /opt/neutron/bundle (config_hash <hash>)
```

> **Authenticate the bundle out-of-band.** `ne pilot verify` is tamper-*evidence*
> — it recomputes `config_hash` and a `manifest_hash` (over the whole manifest,
> incl. `customer`/`hostname`) and fails on any mismatch — but a self-contained
> hash is **not authentication**: an actor who rewrites the entire bundle can
> recompute it. Obtain the expected `config_hash` from the platform team over a
> **separate trusted channel** (not the bundle itself) and confirm it matches the
> value `ne pilot verify` prints above. Until cosign bundle signing ships (v1.1),
> this out-of-band comparison is what makes bundle integrity authentication-grade.

> ⚠️ **Tracked gap (NE-070-FOLLOWUP):** `verify` integrity-protects the config
> files and their hashes but **not** the manifest's `customer` / `hostname`
> fields (which `ne up` trusts for the Keycloak external hostname and the Caddy
> vhost). Treat the bundle directory as trusted storage. See §10.

---

## 4. TLS / Caddy

Caddy is the only off-box listener and terminates TLS on `443` and `8443`. Caddy
enforces a modern floor (TLS 1.2 minimum, TLS 1.3 preferred) by default. Two
options:

### 4.1 Local-CA default (zero egress — C2)

The default `./caddy/Caddyfile` uses `tls internal`: Caddy's **local CA** issues
the certs — no ACME, no egress, air-gap-safe. Nothing to configure. Clients
trust the CA root at `/data/caddy/pki/authorities/local/root.crt` inside the
`caddy-data` volume, or connect with `curl -k`. Good for a dev-pilot / CI.

### 4.2 Operator-provided real certificates

To serve customer-provisioned certs, drop the cert and key into the pilot
`certs/` dir and select the custom-cert Caddyfile:

```sh
cp <your-cert>  deploy/compose/pilot/certs/tls.crt
cp <your-key>   deploy/compose/pilot/certs/tls.key   # ./certs is git-ignored — never commit
export NE_PILOT_CADDYFILE=./caddy/Caddyfile.customcerts
```

**Mechanism (verified against `compose.yaml`):** the caddy service mounts
`${NE_PILOT_CADDYFILE:-./caddy/Caddyfile}`. `ne up`'s ephemeral env file carries
only the two secrets + `NE_PILOT_HOSTNAME`, so `NE_PILOT_CADDYFILE` is **not**
written there — it is resolved from the **shell environment** that `ne up`
inherits and passes to the `docker compose` subprocess (shell env overrides the
`--env-file`). Therefore you must `export NE_PILOT_CADDYFILE=…` in the same
shell **before** running `ne up` (§5). The `Caddyfile.customcerts` variant names
cert/key **paths only** (`/certs/tls.crt`, `/certs/tls.key`) — no key material
in any tracked file (C12).

---

## 5. Bring-up

```sh
uv run ne up --profile pilot --bundle /opt/neutron/bundle
```

- `--profile pilot` expands to **`core` + `brain` + `obs`** (`_PILOT_EXPANSION`).
- Valid `--profile` values are exactly **`pilot`, `core`, `brain`, `agents`,
  `obs`** (repeat the flag to combine, e.g. `--profile core --profile obs`).
  `agents` is reserved — `ne up --profile agents` warns "nothing to start yet"
  and starts nothing (no first-party app images exist).
- Serve lane: add **`--serve-cpu`** (tiny-model CPU vLLM — the tested path) or
  **`--serve-gpu`** (needs the NVIDIA runtime). They are **mutually exclusive**
  (`pass at most one of --serve-gpu / --serve-cpu`). Note the flag spelling:
  `--serve-cpu` / `--serve-gpu`, *not* `--profile serve-cpu`.

**Preflights** (each fails closed with a specific one-line message, in order):
docker on `PATH` → daemon reachable (`docker info`) → `docker compose` ≥ 2.20 →
`443`/`8443` free → ≥ 10 GiB free on `/` → bundle `manifest.json` present and
`ne pilot verify` clean → (only if `--serve-gpu`) NVIDIA runtime present.

`ne up` runs `docker compose … up --detach --wait`, so it **blocks on
healthchecks** and only returns once services report healthy (Keycloak's
first-boot DB migration can take a while; the healthcheck gates on
`/health/ready`, not just an open socket). On success it prints the endpoint
summary:

```
Pilot stack is up (project ne-pilot). Endpoints:
  console/gateway  https://<host>:443 (placeholder until NE-067 images)
  keycloak         https://<host>:8443
  postgres         127.0.0.1:5432 (on-VM)
  opa              127.0.0.1:8181 (on-VM)
  qdrant           127.0.0.1:6333 (grpc 6334) (on-VM)
  tei-embed        127.0.0.1:8085 (on-VM)
  tei-rerank       127.0.0.1:8086 (on-VM)
  otel-collector   127.0.0.1:4317 / 4318 (on-VM)
Tear down with `ne down --bundle <dir>`. Off-box: only https://<host>:443/8443.
```

The `443` endpoint is a **placeholder** (Caddy returns a 200 "console arrives
with NE-067 images") until the first-party console image lands — expected in a
pre-NE-077 pilot. `8443` is a live Keycloak. All other ports are loopback-only.
The ephemeral `runtime/pilot.env` (mode `0600`) is deleted after compose
returns, so resolved secret values never sit at rest.

---

## 6. Verify / healthcheck

`ne up --wait` already blocks on healthchecks, so a clean return is the first
signal. Additional checks from the VM:

```sh
docker compose -p ne-pilot ps                 # every service should read healthy
curl -k https://<host>:8443                    # Keycloak reachable through Caddy (TLS)
uv run ne pilot verify /opt/neutron/bundle     # bundle still intact (idempotent)
```

Optional loopback probes on the VM (brain tier): Qdrant `curl 127.0.0.1:6333`,
TEI embed `127.0.0.1:8085`, TEI rerank `127.0.0.1:8086`. From off-box, **only**
`https://<host>:443` and `https://<host>:8443` must be reachable — nothing else.

---

## 7. Backup / restore drill (NE-100)

Encryption-at-rest under a **customer-managed key** for pilot data. Every
artifact is sealed with the **NEB1 envelope** (per-backup random 256-bit DEK,
wrapped by the customer KEK, chunked AES-256-GCM). The KEK lives **only** in the
bundle SecretStore at `pilot/backup` field `key_b64`.

### 7.1 Provision the customer KEK (once per bundle)

```sh
uv run ne-backup keygen --bundle /opt/neutron/bundle
# prints only a fingerprint; the 256-bit key is stored ONLY in the SecretStore
```

`keygen` **refuses to overwrite** an existing KEK (exits non-zero) unless you
pass `--rotate`. **Losing this key makes every backup permanently unrecoverable
— by design; there is no escrow.** Back up the bundle SecretStore through the
customer's own key-management process.

### 7.2 Install the scheduled backup (systemd)

Copy `deploy/backup/systemd/ne-backup.service` and `ne-backup.timer` into
`/etc/systemd/system/`, then **edit the service unit for your host — the
shipped unit is a template, not turnkey.** The runner
(`deploy/backup/run-backup.sh`) invokes the bare `ne-backup` console script,
which after `uv sync --all-packages` lives in the workspace venv at
`<repo>/.venv/bin/ne-backup` — it is **not** on the system `PATH`:

1. **`ExecStart`** — the shipped path assumes the checkout is at
   `/opt/neutron/neutron-enterprise`. If you cloned elsewhere (§1.3), point it
   at `<your-checkout>/deploy/backup/run-backup.sh`.
2. Uncomment **`WorkingDirectory=`** and set it to the repo root.
3. Uncomment the two **`Environment=`** lines and set `NE_BACKUP_BUNDLE`
   (bundle dir) and `NE_BACKUP_DEST` (backup destination).
4. Add a `PATH` so the runner can resolve `ne-backup` (and `docker`):

   ```ini
   Environment=PATH=/opt/neutron/neutron-enterprise/.venv/bin:/usr/local/bin:/usr/bin:/bin
   ```

Then enable the timer **and fire one run immediately to prove the unit works**
— a mis-set `ExecStart`/`PATH` otherwise fails only at 02:00 inside systemd,
and scheduled backups (your RPO) would be silently absent:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now ne-backup.timer
sudo systemctl start ne-backup.service          # one run NOW
systemctl status ne-backup.service              # must show status=0/SUCCESS
uv run ne-backup list --dest <NE_BACKUP_DEST>   # the new scheduled backup must appear
```

The timer fires daily at 02:00 local (`Persistent=true` catches up a missed
run). **RPO ≤ 24 hours** (bounded by the daily cadence); **RTO ≤ 30 minutes**
for pilot-scale corpora (≤ 10 GiB) — the CI drill
(`.github/workflows/backup-restore-drill.yml`) measures real restore time and
asserts it stays under `NE_BACKUP_DRILL_RTO_SECONDS` (default 900s).

### 7.3 Backup / verify / restore commands

```sh
uv run ne-backup create  --bundle /opt/neutron/bundle --dest /mnt/customer-backups
uv run ne-backup list    --dest /mnt/customer-backups
uv run ne-backup verify  <id> --dest /mnt/customer-backups --bundle /opt/neutron/bundle
uv run ne-backup restore <id> --dest /mnt/customer-backups --bundle /opt/neutron/bundle \
                          --restore-bundle-to /opt/neutron/restored
uv run ne-backup prune   --dest /mnt/customer-backups --keep 7
```

Restore is **safe-by-construction**: validate manifest → KEK fingerprint →
ciphertext hashes → decrypt-to-staging with plaintext-hash checks (all *before*
any destructive step) → **safety snapshot** → destructive restore → verify. A
flipped byte, wrong key, or missing artifact aborts before live data is touched;
`restore` also prompts for confirmation unless `--yes`. Skipping the safety
snapshot (`--no-safety`) is discouraged. Note: a backup run is internally
MVCC-consistent per database but is **not** a single global point-in-time across
Postgres *and* Qdrant — the skew is bounded by the run's own duration
(documented pilot-tier property). Backups land on a local/customer-mounted
`--dest` only; the tool makes **zero off-VM connections** (C2).

### 7.4 Mandatory install-time drill

Prove the KEK + restore path work **before** you rely on them. First understand
where a restore actually lands:

> ⚠️ **`ne-backup restore` restores into the LIVE pilot.** Postgres, Qdrant, and
> the allowlisted volumes are restored into the stack named by the backup config
> (`compose_project = ne-pilot`, `postgres_container = ne-pilot-postgres-1`,
> `qdrant_url = http://127.0.0.1:6333` — the `BackupConfig` defaults in
> `tools/ne-backup/src/ne/backup/config.py`). **`--restore-bundle-to` only
> redirects where the *config bundle* is extracted — it does NOT send the
> database or volume restore to a scratch location.** A restore always WIPES and
> replaces current pilot data (a safety snapshot is taken first unless
> `--no-safety`).

**At install time the pilot is freshly brought up and holds no customer data
yet**, so restoring a seed backup into it is non-destructive in practice. Run the
drill **now** — immediately after §6 verify and *before* loading any customer
data — as a deliberate **live** restore into the still-empty pilot:

```sh
uv run ne-backup create  --bundle /opt/neutron/bundle --dest /mnt/customer-backups
uv run ne-backup verify  <id> --dest /mnt/customer-backups --bundle /opt/neutron/bundle
uv run ne-backup restore <id> --dest /mnt/customer-backups --bundle /opt/neutron/bundle \
    --restore-bundle-to /tmp/restore-drill --yes   # LIVE restore into the empty pilot
```

Confirm the restore report verifies (matching plaintext hashes) and the services
return healthy (`docker compose -p ne-pilot ps`).

**Re-drilling after go-live (once the pilot holds real customer data) MUST NOT
run a live restore against `ne-pilot`.** Restore into a **separate scratch
stack** instead: bring up throwaway postgres/qdrant containers and point a drill
config at them via `--config`, e.g. `drill-backup.toml`:

```toml
postgres_container = "ne-drill-postgres"
qdrant_url         = "http://127.0.0.1:7333"
compose_project    = "ne-drill"
stop_containers    = []            # nothing in the live stack is touched
volumes            = ["caddy-data"]
```

```sh
uv run ne-backup restore <id> --dest /mnt/customer-backups --bundle /opt/neutron/bundle \
    --config drill-backup.toml --restore-bundle-to /tmp/restore-drill --yes
```

This is exactly the pattern the CI restore drill uses
(`tools/ne-backup/tests/test_restore_drill.py`,
`.github/workflows/backup-restore-drill.yml`), which spins up isolated
`nedrill…` containers and sets `stop_containers=[]` so the restore never touches
a live deployment.

---

## 8. Teardown

```sh
uv run ne down --bundle /opt/neutron/bundle            # stop + remove containers
uv run ne down --bundle /opt/neutron/bundle --volumes  # also drop named volumes (destroys data)
```

`ne down` sweeps **all** profiles (`core`, `brain`, `obs`, `gpu`, `cpu`) so
nothing leaks regardless of how the stack was started. It regenerates the
ephemeral env file on demand (compose needs it for interpolation) and deletes
the one it generated afterward.

> ⚠️ **Tracked gap (NE-070-FOLLOWUP):** `ne down` currently resolves the full
> SecretStore to regenerate the env file, so teardown needs the secrets
> reachable even though only interpolation placeholders are strictly required.
> See §10.

---

## 9. Air-gap / offline notes

The pilot tier is air-gappable. For an offline bring-up, pre-stage weights and
set the offline flag **in the shell environment before `ne up`** (same
shell-inheritance mechanism as `NE_PILOT_CADDYFILE`, §4.2):

- **TEI weights** → pre-stage into the `tei-embed-cache` / `tei-rerank-cache`
  volumes, then `export NE_PILOT_HF_OFFLINE=1` (sets `HF_HUB_OFFLINE=1` → zero
  runtime egress).
- **vLLM weights** → pre-stage into the serve tier's `./models` (see
  `deploy/compose/serve/README.md`).

The no-egress *proof itself* is CI-owned (`deploy/airgap`, NE-073) and cited as
evidence in the dossier (sovereignty control). The full **signed offline
bundle** (Zarf-pattern) for real customer air-gap installs is a later unit —
**NE-086 (v1.1)** — not shipped here; this tier provides the knobs only.

---

## 10. Known limitations and tracked issues

Disclosed, not hidden — several install-relevant items are tracked for
resolution before the NE-077 production gate:

- **No first-party app images yet.** The `443` vhost is a placeholder; the
  `agents` profile is reserved. NE-077's console-approval flow depends on NE-067
  + packaging landing later. The infra stack this guide brings up is complete;
  the app layer is not.
- **NE-070-FOLLOWUP installer items — RESOLVED** (merged):
  the disk preflight now stats Docker's data-root filesystem, the `443`/`8443`
  preflight is gated on the `core` profile, `ne down` tears down with a
  placeholder env (no SecretStore needed), and `ne pilot verify` now enforces a
  `manifest_hash` over the full manifest (incl. `customer`/`hostname`).
  **Residual (tracked, v1.1):** the `manifest_hash` is tamper-*evidence*, not
  authentication — an actor who can rewrite the whole bundle can recompute it.
  Verify it out-of-band (§3); cryptographic bundle signing (cosign at promote
  time) is the v1.1 hardening.
- **No operator secret-seeding CLI** — secrets are seeded via the Python snippet
  in §2.3 (candidate NE-070-FOLLOWUP).
- **FileSecretStore is single-process** — no concurrent writers (§2.2).
- **Live-volume at-rest encryption is delegated to host disk encryption** — a
  hard prerequisite (§1.1), not a platform-managed control for live volumes.
- **No release tag cut** — deploy identity is a git commit, not a `vX.Y.Z` tag,
  until the first release is cut (§0).

When NE-070-FOLLOWUP lands it will change installer behavior; this guide must be
re-touched at that point (the doc-drift queue tracks it).
