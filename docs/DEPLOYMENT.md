# Neutron Enterprise — Operator Deployment Runbook

This document **orients and sequences** an engagement operator from zero to a
running, loginable, governed platform. It is a map, not a replacement: each
deploy tier's authoritative runbook is its own README (linked inline), and this
doc never duplicates their command detail beyond the pilot quickstart spine. Where
a path is live-parked or owner-gated, this doc **says so** rather than papering
over it — check the linked README before attempting any
step here.

Positioning, if you need the one-liner for a customer: *AI in production, in your
infrastructure, never locked to a vendor.*

---

## 1. Overview — one image, four deploy paths

### 1.1 The ONE first-party image

The entire platform runtime is a **single** first-party OCI image, `ne-app:0.1.0`
(built from [`../deploy/docker/ne-app/Dockerfile`](../deploy/docker/ne-app/README.md)).
It is **multi-role by command**: the image carries **no `ENTRYPOINT`** and a
default `CMD ["python", "-m", "ne.api"]` (the `ne-api` HTTP backend). The durable
worker runs the **same image** with a command override
`command: ["python", "-m", "ne.worker"]` — "one image, different command". Every
deploy path below ships this identical image plus a promoted per-customer config
bundle (produced by the common `ne pilot promote` machinery); only the packaging
differs. Sources: [`../packages/ne-worker/README.md`](../packages/ne-worker/README.md),
[`../deploy/helm/README.md`](../deploy/helm/README.md) §ne-worker.

### 1.2 Decision tree — which deploy path?

| Deploy path | Use when | Bring-up | Runbook |
|---|---|---|---|
| **Pilot Compose** | A single hardened VM — the engagement pilot. | the `ne up` installer (never raw `docker compose`) | [`../deploy/compose/pilot/README.md`](../deploy/compose/pilot/README.md) |
| **Helm (prod K8s)** | Production Kubernetes, connected cluster. **Full-platform: core + brain by default; obs/serve behind default-OFF profiles** (see §3). | `ne k8s up` (installer mode) | [`../deploy/helm/README.md`](../deploy/helm/README.md) |
| **Terraform (cloud)** | When the **cloud infrastructure itself** must be provisioned (private AKS reference / hardened EKS reference) — then it deploys the same Helm chart. | `terraform apply` → `ne k8s up` | [`../deploy/terraform-azure/README.md`](../deploy/terraform-azure/README.md), [`../deploy/terraform-eks/README.md`](../deploy/terraform-eks/README.md) |
| **Air-gap Zarf** | A **fully disconnected** Kubernetes cluster installed from one tarball, zero egress at deploy time. | `zarf package deploy` | [`../deploy/airgap/zarf/README.md`](../deploy/airgap/zarf/README.md) |

All four deploy the **same `ne-app` image** and the **same promoted config bundle**.
Helm, Terraform, and Zarf all install the **same umbrella Helm chart**
(`deploy/helm/neutron-enterprise`); Terraform provisions the cluster the chart
lands on, Zarf vendors the chart + its images into a disconnected tarball.

---

## 2. Pilot Compose quickstart (the command spine)

This is the only section with a full command spine. Every command here is verbatim
from [`../deploy/compose/pilot/README.md`](../deploy/compose/pilot/README.md) — the
hardened single-VM pilot tier (`core` + `brain` + `obs` profiles). **Never run
`docker compose` directly**; the `ne up` installer runs preflights, injects secrets
from the bundle into an ephemeral `0600` env file, and waits on healthchecks.

**1. Build the first-party image** in the egress-open staging window:

```sh
bash deploy/airgap/scripts/build-ne-app.sh
```

**2. Promote a per-customer config bundle:**

```sh
ne pilot promote --customer acme --hostname pilot.acme.example --out ./acme-bundle
```

**3. Bring up the pilot stack** (`ne up --profile pilot` expands to `core` + `brain`
+ `obs`; add `--serve-cpu` for the CPU tiny-model vLLM lane):

```sh
ne up --profile pilot --bundle ./acme-bundle
ne up --profile pilot --serve-cpu --bundle ./acme-bundle   # + CPU vLLM lane
```

Tear down with `ne down --bundle ./acme-bundle [--volumes]`.

**4. Log in.** Caddy is the only off-box service and terminates TLS on every port:

- **Console** at `https://<hostname>:443` — Caddy TLS reverse-proxies to `ne-api`
  same-origin (console SPA + `/api` behind one front door). The SPA reads its OIDC
  config at runtime from `GET /config.json` (`{issuer, client_id, audience,
  api_base}`, `Cache-Control: no-store`, never a secret) — the **NE-CONSOLE-ISSUER**
  runtime-issuer seam ([`../packages/ne-api/README.md`](../packages/ne-api/README.md)).
- **Keycloak** at `https://<hostname>:8443` — the checked-in `ne-pilot` demo realm
  imports on start with three seeded demo users
  ([`../deploy/compose/pilot/keycloak/README.md`](../deploy/compose/pilot/keycloak/README.md)):

  | Username | Password | Role |
  |---|---|---|
  | `approver` | `approver-demo` | HITL approval |
  | `steward` | `steward-demo` | flywheel review |
  | `ops` | `ops-demo` | audit read |

  These are the **only** sanctioned literal credentials in the repo — demo-only,
  replaced entirely by real IdP brokering (Entra ID / Okta federated into Keycloak)
  in a real engagement. **Re-import caveat:** `--import-realm` skips a realm that
  already exists; editing the JSON needs a fresh volume to re-apply.

**5. Run a governed turn.** `ne-api` ships `NE_API_TURN_EXECUTION=enqueue`, so a
chat turn is enqueued onto the DBOS queue and executed by the **durable `ne-worker`**
(the same image, `python -m ne.worker`) — the C6 `PDP decide → HITL tier → audit`
path runs in the worker ([`../packages/ne-worker/README.md`](../packages/ne-worker/README.md)).

**Login proof.** The sealed-netns no-egress CI runs
`deploy/airgap/tests/test_airgap_phase_a.py::test_password_grant_login_end_to_end`:
a real Keycloak-minted token authorized by `ne-api` via the in-network JWKS
(NE-103b — the pilot is loginable end-to-end today). The **browser auth-code+PKCE
redirect UX** and the brain-retrieval console demo are still parked to the NE-077
demo arc (§8).

**6. Seed + refresh the corpus (ingestion).** Brain search returns an EMPTY corpus
until a connector sync runs. Ingestion is the governed connector-sync surface (P1-4;
SPEC §11 decision 2026-07-10) — NO new process, just NE-034 cron jobs in `ne-worker`
plus a one-shot CLI, both on the EXISTING governed `KnowledgeWriter` path (C7
fail-closed ACL derivation).

- **Connectors are OFF by default.** No configured source ⇒ no cron ⇒ a clean no-op
  (never a crash). Turn ingestion on by naming sources in the env below.
- **Env surface** (set on **BOTH `ne-api` and `ne-worker`** — the per-source tool
  policy is hashed into the frozen `config_hash`, so when sources are configured both
  services must agree or the §6.2 boot cross-check fails). Tuple/dict values are JSON
  strings (pydantic-settings; a malformed value fails loud at boot):

  | Env var | Example | Meaning |
  |---|---|---|
  | `NE_CONNECT_SYNC_SOURCES` | `'["m365","jira"]'` | JSON array of sources to seed (subset of `m365`/`teams`/`slack`/`jira`/`zendesk`/`salesforce`/`sql`/`fixture`/`csv`). |
  | `NE_CONNECT_SYNC_CRON` | `"0 * * * *"` | Default cron (hourly). |
  | `NE_CONNECT_SYNC_CRON_OVERRIDES` | `'{"m365":"*/15 * * * *"}'` | JSON per-source cron override. |
  | `NE_CONNECT_SYNC_STATE_PATH` | `/data/connect/state.db` | Cursor + dedup sqlite (required when sources set). |
  | `NE_CONNECT_SYNC_QUARANTINE_PATH` | `/data/connect/quarantine.db` | Write-quarantine sqlite (required when sources set). |
  | `NE_CONNECT_SYNC_KPI_TAG` | `acme-ingest` | Optional KPI tag threaded into every run (C8). |

  Per-connector credentials are the connector's own `NE_*` settings
  ([`../packages/ne-connect/README.md`](../packages/ne-connect/README.md)); the
  demo/first-seed `fixture` source reads `NE_CONNECT_FIXTURE_PATH` (a recorded JSON)
  and `NE_CONNECT_FIXTURE_OWNER`. The `csv` source (a POLLED spreadsheet export —
  full-snapshot re-read each cron fire; unchanged files dedup to a no-op) reads
  `NE_CONNECT_CSV_PATH` (the CSV file), `NE_CONNECT_CSV_SCHEMA_PATH` (the
  operator-owned ODCS row JSON-Schema), `NE_CONNECT_CSV_OWNER`,
  `NE_CONNECT_CSV_ID_COLUMN`, and `NE_CONNECT_CSV_DEFAULT_ACL` (a JSON array — a
  sheet has no source ACL system, so the audience is declared; empty is rejected).
  Rows that fail the schema are parked in the write quarantine with the contract
  validator's own message as the reason (inspectable via the ops-gated
  `GET /v1/connect/quarantine` drill-down and the Sources screen).
- **Ongoing freshness = the seeded cron.** `ne-worker` seeds ONE governed NE-034 job
  per configured source (`connect-sync-<source>`, granting exactly the tier-1 tool
  `write.internal.connect.sync_<source>`). Each fire is an audited job
  (`job.schedule`/`job.run` + the connector's `connector.sync` event) writing through
  the governed `KnowledgeWriter` (C7). Empty config ⇒ zero jobs.
- **First-seed / manual re-sync = `ne connect sync [<source>]`.** Run it with the
  platform env available and network reach to Postgres/Qdrant/TEI:
  ```sh
  ne connect sync            # all configured sources
  ne connect sync fixture    # one named source (e.g. first-seed the demo corpus)
  ```
  Exit 0 on success (incl. the no-source no-op); exit 1 if a source is unconfigured,
  unknown, or errors. On the dev stack and any reachable deployment this works as-is.
  On the **hardened pilot** the in-network services are NOT published to the host, so
  either just wait for the seeded cron's first fire, or run the CLI in-network via a
  compose override that also mounts a writable volume for the state/quarantine sqlite
  files (the rootfs is read-only). Set the `NE_CONNECT_SYNC_*` env on **both** services:
  ```yaml
  # docker compose -f compose.yml -f connect-override.yml run --rm ne-worker \
  #   python -m ne.cli connect sync fixture
  services:
    ne-api:      { environment: [ "NE_CONNECT_SYNC_SOURCES", "NE_CONNECT_SYNC_STATE_PATH", "NE_CONNECT_SYNC_QUARANTINE_PATH" ] }
    ne-worker:
      environment: [ "NE_CONNECT_SYNC_SOURCES", "NE_CONNECT_SYNC_STATE_PATH", "NE_CONNECT_SYNC_QUARANTINE_PATH" ]
      volumes: [ "connect-state:/data/connect" ]
  volumes: { connect-state: {} }
  ```
- **HONESTY NOTE:** first-class bundle/compose plumbing for the connector env + an
  in-image `ne connect sync` one-shot are tracked follow-ups
  (**NE-INGEST-COMPOSE-ENV** / **NE-INGEST-IMAGE-CLI**) — this unit deliberately does
  NOT edit `deploy/compose` or `deploy/helm` (the Helm/compose byte-parity guardians);
  the runbook closes "no supported way to ingest" at the runbook level.
- **CDC is push-based (§6.4)**, NOT part of `ne connect sync` — Debezium change events
  ride the `/cdc` webhook onto the SAME governed writer, governed separately.

**Principal sync & revocation lag (P2-20).** When `NE_PERMISSIONS_MODE=entra` (or
`fixture`) the worker seeds ONE governed NE-034 cron `permissions-principal-sync` (the
tier-1 tool `write.internal.permissions.principal_sync`) at
`NE_PERMISSIONS_SYNC_INTERVAL_SECONDS` (default **900s**). **That interval IS the max
revocation lag** — the SPEC §5 ≤15-min target holds at the default; a larger value
moves the SLA (documented, never silently capped) and the cron granularity floors to
whole minutes. First-seed / manual re-sync is `ne permissions sync`. The default
`oidc-claims` mode seeds **NO** cron (JIT observation buffer in ne-api — nothing to
pull) and SCIM is push-provisioned via routes, so both are a clean no-op.

**SIEM export (day one, P2-21).** The `ne-worker` outbox drain is **default-on**
(`NE_WORKER_OUTBOX_CONSUMER=siem-otel`) and ships audit/cost/approval events as OTel
logs to the in-cluster `otel-collector` (`NE_WORKER_OUTBOX_OTLP_ENDPOINT=
http://otel-collector:4318`; the `obs` profile is part of the default `ne up` pilot
expansion). **The customer forwards from the collector to their SIEM — the platform
itself never egresses** (C2: `validate_in_vpc_endpoint` rejects any external endpoint
at worker boot). Retention `NE_WORKER_OUTBOX_RETENTION_S` (default **7d**) prunes rows
acked by **every** consumer so `ne_outbox_events` stays bounded. Disabling the
collector **without** clearing `NE_WORKER_OUTBOX_CONSUMER` retains rows (fail-open,
at-least-once — cursor unmoved, a throttled warning, nothing lost); clear the consumer
to switch the export off deliberately.

The pilot README carries the detail this spine omits: the JWKS in-network-fetch
residual (tracked NE-IDENTITY-JWKS-CA), self-signed vs. custom-cert provisioning,
the five `*_DATABASE_URL` env wiring, and the C12 secret flow. A fresh-VM install
walkthrough is [`pilot/install-guide.md`](pilot/install-guide.md).

---

## 3. Production Helm

The production Kubernetes tier is a Helm umbrella chart
(`deploy/helm/neutron-enterprise`) reproducing the hardened pilot Compose tier as
K8s manifests. [`../deploy/helm/README.md`](../deploy/helm/README.md) is
authoritative; the promotion runbook in brief:

1. `bash deploy/airgap/scripts/build-ne-app.sh` — build the image (egress-open
   staging window).
2. **C11 registry-digest pin** — push to the customer registry and capture the
   digest (fail-closed if empty):
   ```sh
   ne release push --image ne-app:0.1.0 --registry <customer-registry>/neutron \
     --out /tmp/digest.json
   ```
3. `ne pilot promote --customer <name> --hostname <hostname> --out /tmp/bundle`
   (same bundle machinery as the pilot tier).
4. Install (`ne k8s render` is optional — `ne k8s up` renders internally):
   ```sh
   ne k8s up --bundle /tmp/bundle --namespace neutron --release ne \
     -f /tmp/digest.json --wait --timeout 15m
   ```

   The default remains pre-cutover. A genuinely empty first install may opt in
   to protected transport with `--admit-protected-transport` after provisioning
   the two external database URLs described in the Helm README.

**Secrets are installer-mode ONLY (C12):** Helm **never** renders a `Secret`
object. `ne k8s up` resolves bundle secrets through the SecretStore and applies a
single `Secret/ne-platform-secrets` via `kubectl apply -f -` on **STDIN only**;
if secret resolution fails it aborts before touching the cluster at all. The
chart's `values.schema.json` + a `_helpers.tpl fail` refuse to render with an
empty `global.image.digest` — the same fail-closed posture as the pilot preflight.

The chart deploys `ne-api` + `ne-worker` Deployments on the **core profile** (the
worker is the same image, no Service, **exec** health probes). Default-**OFF**
surfaces:

- **HA overlay** (`examples/values-production-ha.yaml`) — PDB / anti-affinity /
  topology-spread pass-throughs. **Stateful HA (Postgres / Keycloak / Qdrant) needs
  operators or an external managed service, NOT a `replicas:` bump** — a bump alone
  yields N unreplicated instances / split-brain.
- **NetworkPolicy** — default-deny + least-privilege allow set; **inert without a
  NetworkPolicy-enforcing CNI** (Calico / Cilium).
- **OpenShift Route** alternative (`route.enabled`) and a **pg_dumpall backup
  CronJob** + restore runbook (`backup.enabled`).

The chart is now **full-platform (NE-071c)**. The **brain** services — `qdrant`
+ TEI `embed`/`rerank` — ride the **default deploy** (`profiles.brain`,
default-ON), so a K8s deploy DOES have an in-cluster vector/embedding stack;
`otel-collector` is behind `profiles.obs` and `vLLM` behind
`profiles.serve.enabled` (GPU-gated, **render-only in CI**, needs a pre-staged
weights PVC for a real deploy). The **brain off-switch** (`profiles.brain=false`)
exists for sealed installs that have NOT pre-staged the TEI weights — a fully
disconnected install cannot download them (`HF_HUB_OFFLINE` default `0` mirrors
the pilot), so either pre-stage the `cache` PVCs (`tei.hfOffline=1`) or turn the
profile off.

**SIEM export on Helm (P2-21).** The ne-worker Deployment carries the same
default-on `NE_WORKER_OUTBOX_CONSUMER=siem-otel` +
`NE_WORKER_OUTBOX_OTLP_ENDPOINT=http://otel-collector:4318`, but `profiles.obs`
stays **default-OFF** (flipping it would break the zarf air-gap posture — the otel
image is deliberately never vendored, §5). So a default Helm install drains against
an `otel-collector` Service that does not yet exist: the loop **fail-opens** (cursor
unmoved, rows retained under the retention window, a throttled 60s warning) — data is
never lost. To activate the drain, enable `profiles.obs` (or point
`NE_WORKER_OUTBOX_OTLP_ENDPOINT` at an in-cluster collector you run); to switch the
export off deliberately, clear `NE_WORKER_OUTBOX_CONSUMER`.

> **Remaining gaps:** the `external-secrets` / CSI secret modes are documented but
> not built (installer-mode secrets are what ship). **Velero** backup Schedule
> templating shipped in NE-071d (PR #224, 2026-07-17) — this line was stale.
> **Langfuse** is deliberately NOT
> templated — it is dev-tier-only (unpinned tags; needs clickhouse+S3+redis;
> excluded from the pilot as a C2 invariant), so it can never satisfy the C11
> byte-parity mandate; observability in prod is the OTLP collector only.

The authoritative live proof is the `kind-smoke` job in
`.github/workflows/helm-smoke.yml` (build → push → promote → `ne k8s up` →
`/healthz` 200 + pushed-digest assertion).

---

## 4. Cloud Terraform

Infrastructure-as-code that provisions the cluster and then deploys the **same**
Helm chart. Two roots:

- **[`../deploy/terraform-azure/README.md`](../deploy/terraform-azure/README.md)** —
  the private **AKS 1.31+** reference: VNet, Private Endpoints + private DNS,
  **deny-by-default NSGs with the Azure Firewall FQDN allowlist as THE C2 egress
  lane** (the firewall carries FQDN exactness; vanilla NSG/NetworkPolicy is
  L4-coarse — an empty `frontier_fqdns` list means full air-gap), the Entra
  broker-only app registration (secret → Key Vault), a flag-gated GPU node pool,
  and opt-in Velero.
- **[`../deploy/terraform-eks/README.md`](../deploy/terraform-eks/README.md)** —
  the hardened **C2/C9-parity AWS reference** (promoted in place across
  NE-AWS-1..13): private 3-AZ VPC + PrivateLink endpoints, AWS Network
  Firewall FQDN-exact deny-by-default egress (`frontier_fqdns=[]` ⇒
  air-gap), KMS CMKs, IRSA, flag-gated GPU + RDS multi-AZ, opt-in Velero —
  same chart, same `ne release push`, different cloud (C9). Live apply is
  owner-gated via the `eks-hardened-apply` workflow.

**Licensing (C4):** Terraform CLI is **BUSL-1.1** and is a customer-side / CI tool
**only — never shipped or embedded** (exactly like `kubectl` or `helm`); the HCL
also runs unmodified under **OpenTofu ≥ 1.9 (MPL-2.0)** — substitute `tofu` for
`terraform`. CI pins Terraform 1.15.8; provider hashes are verified via the
committed `.terraform.lock.hcl` (C11).

> **LIVE-PARKED (owner action required):** CI proves **only** `fmt` / `validate` /
> `tflint` (`.github/workflows/terraform-validate.yml`) — **never** a real
> `plan` / `apply`. A live cloud apply + the GPU pool are owner-gated (**NE-072-LIVE**:
> owner creds / quota). A tracked pre-apply hardening item, **NE-072-FOLLOWUP** (the
> `azurerm_key_vault_secret` write could race the Key Vault private endpoint on a
> live apply), was addressed in the NE-072 increment per AS-BUILT/NE-CLEAN5 —
> confirm against the AKS README §12 LIVE-PARKED section before the first live apply.
> Do not expect these cloud paths to run today.

---

## 5. Air-gap Zarf

Two distinct air-gap artifacts — do not conflate them:

- **[`../deploy/airgap/README.md`](../deploy/airgap/README.md)** is the CI **proof**
  (NE-073): a sealed network-namespace stage→seal→prove harness that demonstrates
  the no-egress sovereignty property (frontier lane dead, open-weight vLLM lane
  alive) in CI.
- **[`../deploy/airgap/zarf/README.md`](../deploy/airgap/zarf/README.md)** is the
  shippable **artifact** (NE-086): a single self-contained [Zarf](https://zarf.dev)
  package that installs the entire platform into a fully disconnected cluster.

**Flow:**

1. In the **egress-open staging window**, `zarf package create` runs **once** — it
   vendors the Helm chart **plus the chart's exact byte-parity image set**,
   including the build-in-seal `ne-app` image (via `ne release push`, owner dec ii):
   ```sh
   bash deploy/airgap/scripts/build-ne-app.sh
   # ... ne release push captures the digest into digest.json ...
   zarf package create deploy/airgap/zarf -o . --plain-http --confirm --set ...
   ```
2. Transfer the single tarball (+ the pinned zarf binary, zarf-init tarball, and
   the promoted bundle) onto the air-gapped site.
3. On the **disconnected side with ZERO egress**: `zarf init --confirm`, then
   create the target namespace and apply the installer-mode C12 secret **before**
   deploying the package (the chart never renders a Secret — same stdin-pipe seam as
   the `ne k8s up` Helm path in §3), then `zarf package deploy ... --confirm`:
   ```sh
   zarf init --confirm                       # in-cluster registry + agent, LOCAL tarball
   kubectl create namespace neutron --dry-run=client -o json | kubectl apply -f -
   uv run python deploy/airgap/zarf/scripts/emit_ci_secret.py --bundle <bundle> \
     | kubectl apply -f -
   zarf package deploy zarf-package-neutron-enterprise-amd64-0.1.0.tar.zst --confirm \
     --set NE_HOSTNAME=<vhost> --set NE_CONFIG_HASH=<promoted-config-hash>
   ```
   Zarf pushes the vendored images into its own in-cluster registry and rewrites every
   image ref to it (digest-preserving) — no external pull ever happens. See the
   authoritative [`../deploy/airgap/zarf/README.md`](../deploy/airgap/zarf/README.md)
   §5 runbook for the full step list (signing key, verify).

Zarf is pinned **v0.80.0**, **Apache-2.0**, a **customer-side / CI tool only —
never in `uv.lock`** (C4). The `zarf.yaml` image set is byte-parity-guarded against
the chart (`tools/ne-cli/tests/test_zarf_package.py`, C11 no-drift).

**SIEM export in the air-gap (P2-21).** Zarf **never vendors the otel-collector**
image (the air-gap posture depends on it — `test_zarf_package.py` asserts it absent),
so a sealed install has no in-cluster collector for the default-on ne-worker drain.
The drain **fail-opens** (cursor unmoved, rows retained, throttled warning — nothing
lost); either add an in-cluster collector you run and point
`NE_WORKER_OUTBOX_OTLP_ENDPOINT` at it, or clear `NE_WORKER_OUTBOX_CONSUMER` to switch
the export off deliberately.

> **HONEST GAP:** the PR-gating CI lane proves package **create** + **contents** +
> **seal** (+ anti-vacuous canaries). The **full disconnected `zarf init` /
> `package deploy` / verify** is **live-parked to the weekly schedule +
> `workflow_dispatch`** (not PRs) — the disconnected deploy itself succeeds, but
> full-stack readiness under a sealed netns is CI-flaky against the job budget.
> **NE-086-FOLLOWUP** tracks re-greening it so it can gate PRs again; the README
> §5 runbook is the interim manual proof. The package is **amd64-only** for now.

---

## 6. Optional add-ons + their enable-gates

**All add-ons below are OFF by default.** Enabling any one is a per-engagement
decision with the governance implications stated per add-on. None is required for a
governed pilot.

### 6.1 SCIM provisioning (NE-089)

A first-party SCIM 2.0 provider at `/scim/v2` on `ne-api` so an enterprise IdP can
push Users + Groups. **The mount is ABSENT unless `NE_API_SCIM_BEARER_TOKEN`
(≥16 chars) is set** — unset (default) ⇒ the `/scim/v2` route returns **404**
(fail-closed by omission; a SCIM token can never authenticate an `/api/*` request).
Provisioning writes are **governed and audited** through the PrincipalStore;
deprovisioning is a **fail-closed access removal** through the C7 permission trim
(a deactivate structurally removes retrieval access). Point the IdP's SCIM tenant
URL at `https://<host>/scim/v2`. Detail:
[`../packages/ne-api/README.md`](../packages/ne-api/README.md) §SCIM. **Live
Entra / Okta push is PENDING_OWNER (NE-089-LIVE).**

### 6.2 Flowise (NE-088)

An off-by-default overlay ([`../deploy/compose/flowise/README.md`](../deploy/compose/flowise/README.md))
that runs the OSS Flowise visual flow builder as a **separate governed service** —
every platform tool call from a flow routes through the **C6 GovernedToolExecutor**
(`POST /api/v1/flowise/tools/execute`), identical governance to a first-party turn
(PDP-deny → 403, tier-2/3 → blocks on human approval, every call audited). It
requires the **explicit** enable-gate `NE_FLOWISE_ACCEPT_CVE_RISK=true` (enforced at
compose config-time AND container runtime; without it the overlay refuses to even
`config`), then:

```sh
docker compose --env-file <secure-env> \
  -f deploy/compose/flowise/compose.yaml \
  --profile flowise up -d
```

> ⚠️ **SECURITY:** Flowise carries **CVE-2025-59528**, a **CVSS 10.0**
> unauthenticated RCE (CustomMCP node) that went into **active in-the-wild
> exploitation ~April 2026** and is **CISA-KEV-tracked**. The pinned `3.1.4` is the
> **patched** release (fix floor 3.0.6); defense-in-depth: the **full code-execution
> node class is disabled** (not just customMCP), loopback-only publish, and — on
> Kubernetes — a NetworkPolicy egress clamp to `ne-api` only. The load-bearing
> mitigation is that it **ships OFF and cannot start by accident**. Turn it on only
> with a conscious decision.

### 6.3 OpenFGA ReBAC module (NE-087)

A strictly **additive** fine-grained document-sharing channel
([`../packages/ne-permissions-rebac/README.md`](../packages/ne-permissions-rebac/README.md)).
The **module is merged and disabled by default** (`RebacSettings.enabled = False`).
Governance implication (union rule **I-1**, the **O-1** owner policy default): a
governed, PDP-approved, audited, revocable `rebac.share` **can grant access beyond
the source ACL** (but can never remove source-granted access, and there are no
negative tuples).

> **ENABLE PATH (NE-087-WIRING — MERGED, PR #127).** The live OpenFGA service, the
> `rebac` compose profile (`deploy/compose/pilot/compose.yaml`,
> `profiles: ["rebac"]`, with a one-shot `openfga migrate` first), and the
> composition-root wiring are merged: `build_platform` injects the OpenFGA ReBAC
> resolver as the additive C7 union seam (`rebac_settings`/`rebac_client` in
> ne-api's `platform.py`), OFF unless `RebacSettings.enabled`.
> `ne up --profile pilot --profile rebac` boots ne-api with an ACTIVE resolver —
> see the "ReBAC (rebac profile)" section in
> [`deploy/compose/pilot/README.md`](../deploy/compose/pilot/README.md) and the
> module README for store bootstrap. The default remains OFF
> (`enabled = False`) — byte-identical to the pre-ReBAC filter until an operator
> flips it.

### 6.4 Debezium CDC (NE-084)

An off-by-default change-data-capture lane
([`../deploy/compose/cdc/README.md`](../deploy/compose/cdc/README.md)): a standalone
`ne-cdc` overlay (`quay.io/debezium/server` 3.6.0.Final, digest-pinned) plus a
`pilot-override.yaml` that turns ON the **fail-closed** `ne-api` sink. **CDC is not
an ungoverned back-door** — the sink returns **404 unless the webhook secret is
set**, HMAC-verifies every POST (Standard-Webhooks, ±300s replay window), and every
captured row enters through the **same governed `KnowledgeWriter` ACL path** the
polling connectors use (unresolvable/tainted ACL → quarantine, never written).
Bring-up is `docker compose -f ... pilot-override.yaml` + the standalone overlay
(README §Bring-up). **A live source-DB end-to-end proof is live-parked.**

### 6.5 S3 ObjectStore WORM adapter for audit checkpoints (NE-S3-OBJECTSTORE)

NE-AUDIT-CRON's periodic signed audit-checkpoint loop archives to any
`ObjectStore`. `ne-storage` ships **two** production adapters — this section
covers the `s3` plugin (`ne.storage.s3.S3ObjectStore`), backed by real S3
Object Lock in COMPLIANCE mode; for Azure deployments see
[§6.6](#66-azure-blob-objectstore-worm-adapter-ne-azblob-objectstore).
Pointing the loop at real write-once storage is a **config-only** change:

```
NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE=s3
NE_STORAGE_S3_BUCKET=<your-worm-bucket>
NE_STORAGE_S3_REGION=us-east-1
# endpoint_url / access_key_id / secret_access_key: leave empty for AWS's
# default endpoints + the botocore IAM credential chain (recommended); set
# endpoint_url for an S3-compatible appliance (validated as in-VPC / C2).
```

Minimal IAM actions for the checkpoint role: `s3:PutObject`, `s3:GetObject`,
`s3:ListBucket`, `s3:GetObjectVersion`, `s3:ListBucketVersions`,
`s3:GetObjectRetention`, `s3:GetObjectLegalHold`, `s3:PutObjectRetention`,
`s3:GetBucketLocation`, `s3:GetBucketObjectLockConfiguration`.

**`s3:GetObjectLegalHold` is REQUIRED, and omitting it fails SILENTLY.** On
real AWS, `HeadObject` returns the `x-amz-object-lock-legal-hold` header only
if the caller holds this action; without it the request still succeeds with
`200` and the header is simply absent. A version protected by a legal hold
therefore reads as *unprotected*: the adapter misses it when resolving the
locked version, `delete()` lays a delete marker over it, and the archived
checkpoint disappears from listings — a C10 downgrade with no error anywhere.
The same gating applies to `s3:GetObjectRetention`, which covers
`x-amz-object-lock-mode` and `x-amz-object-lock-retain-until-date` and is
already listed. **Grant both.** Note that a local MinIO deployment returns
these headers regardless of permission, so a MinIO-based staging environment
will not reproduce the AWS failure.

**`s3:GetBucketObjectLockConfiguration` (bucket-scoped) is REQUIRED, not
nice-to-have.** `archive()` calls `GetObjectLockConfiguration` at the top of
every invocation so a bucket without Object Lock fails loud with the real
reason. Omit the permission and the failure is silent where it matters:
`put()` still succeeds and only `archive()` fails, and the checkpoint loop does
`put` **then** `archive` — so every checkpoint object lands in the bucket
**unlocked** before the loop stops. Verified empirically against a credential
holding exactly this list minus that action.

**`s3:DeleteObject` is deliberately absent** — see the required bucket policy
below.

**`s3:BypassGovernanceRetention` is also deliberately absent, and you should
not grant it by default.** It is needed only to upgrade a pre-existing
`GOVERNANCE`-mode lock to `COMPLIANCE` — a migration case that arises when
checkpoints were previously written by another tool or under a bucket default.
Without it, ordinary archiving of unlocked objects works normally and only that
upgrade path fails, with an error naming the missing permission. Granting it
permanently gives the checkpoint role the ability to bypass governance
retention on **every** governance-locked object in the bucket, which is a
materially wider blast radius than the checkpoint loop needs. If you are
migrating such locks, grant it for the migration and remove it afterwards.

**REQUIRED bucket policy — deny `s3:DeleteObject` on the checkpoint prefix.**
This is not optional; without it the retention this adapter applies does not
deliver the guarantee it appears to. S3 Object Lock protects object
**VERSIONS**. A `DeleteObject` carrying **no `VersionId`** deletes nothing —
it lays a **delete marker** as the new current version, and Object Lock does
not prevent it, because no locked version is being modified. The archived
bytes survive underneath, but the key reads as absent to `GetObject` and
`ListObjectsV2`. The adapter reads through delete markers to the locked
version, but the guarantee must not depend on everyone using the adapter — a
server-enforced control has to hold against a raw client and the account root
too. Only a bucket policy closes it:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyCheckpointDeletes",
      "Effect": "Deny",
      "Principal": "*",
      "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
      "Resource": "arn:aws:s3:::ne-audit-checkpoints/checkpoints/*"
    }
  ]
}
```

Substitute your bucket and your `NE_STORAGE_S3_KEY_PREFIX`. The `Resource` is
**prefix-scoped deliberately** — a bucket-wide deny would block lifecycle
cleanup of anything else in the bucket; prefer a dedicated bucket where
practical. `"Principal": "*"` with `Effect: Deny` covers every principal
including root, which is the intent: an explicit `Deny` cannot be overridden
by any `Allow`. Apply it at provisioning time alongside
`--object-lock-enabled-for-bucket`. Unlike Object Lock a bucket policy CAN be
added later — but any delete marker laid before you add it is already there.

**Recovery if a delete marker or newer version is layered on top.** The locked
version is still present and still readable. List the versions, find the one
carrying COMPLIANCE retention, then read it by explicit `VersionId`:

```bash
aws s3api list-object-versions --bucket ne-audit-checkpoints \
  --prefix checkpoints/2026-07-19T00-00-00Z.json

aws s3api get-object-retention --bucket ne-audit-checkpoints \
  --key checkpoints/2026-07-19T00-00-00Z.json --version-id <VERSION_ID>

aws s3api get-object --bucket ne-audit-checkpoints \
  --key checkpoints/2026-07-19T00-00-00Z.json \
  --version-id <LOCKED_VERSION_ID> ./recovered-checkpoint.json
```

To make the locked version current again, **copy it forward** (never weaken
the lock, and do not attempt to delete the marker — the policy above denies
`s3:DeleteObjectVersion` by design):

```bash
aws s3api copy-object --bucket ne-audit-checkpoints \
  --key checkpoints/2026-07-19T00-00-00Z.json \
  --copy-source 'ne-audit-checkpoints/checkpoints/2026-07-19T00-00-00Z.json?versionId=<LOCKED_VERSION_ID>'
```

**Two operational notes.** S3 **lowercases user metadata keys**, so
`stat().metadata` will not compare equal to mixed-case input (`CheckpointID`
returns as `checkpointid`) — write lowercase or normalize before comparing;
`MemoryObjectStore` preserves case, so this is a documented divergence.
And `list_objects` is **N+1, now N keys x V versions** on the verify path
(retention and content-type are not in `ListObjectsV2`, and delete-marked keys
need version resolution). It is sized for checkpoint volumes — **order
10^3–10^4 objects per prefix**, which is what `ne-audit`'s checkpoint cadence
produces across a multi-year retention window. Past that ceiling, partition
`key_prefix` (e.g. by year or month) rather than paginating harder.

**The per-key version cap, and its tradeoff — one key can halt ALL listing.**
The version walk is capped at `NE_STORAGE_S3_MAX_VERSIONS_PER_KEY` (default
`1000`) and a breach raises `TooManyObjectVersionsError` naming the offending
key. The cap is checked per key but **`list_objects` raises for the entire
call**, so a single key over the cap means no key is returned at all —
including healthy, locked checkpoints far below the cap. The vector is
`s3:PutObject`, which the checkpoint role must hold and which the required
deny-`s3:DeleteObject` bucket policy does not restrict, and the poisoned key
**does not have to be a checkpoint key** — any key under the listed prefix
works. The cap also lowered the cost of triggering this: previously ~100k
versions bought a slowdown, now ~1001 cheap puts buy a hard stop.

This is a deliberate fail-closed choice and will not be softened to a partial
listing: a listing that silently omits a key is the exact C10 failure this
adapter exists to prevent, since nothing downstream can distinguish "absent
because deleted" from "absent because the scan gave up". **Operationally:**
alert on `TooManyObjectVersionsError` from `verify_checkpoints` and treat it
as a tamper signal rather than a tuning problem. The error names the key —
remove the layered versions on it (or raise the cap if the volume is
genuinely legitimate) and listing recovers.

**C2 note:** with `endpoint_url` empty (the AWS-default-endpoints posture
above), AWS S3's regional FQDNs (e.g. `s3.us-east-1.amazonaws.com` and the
bucket's virtual-hosted/path-style equivalents) must be in the egress
allowlist — this is real internet egress, the same governed-egress posture
every other frontier-bound call follows. Under `AIR_GAP`, an empty
`endpoint_url` is refused at construction (`AirGapViolationError`) —
air-gapped deployments must point `endpoint_url` at an in-VPC or explicitly
allowlisted S3-compatible appliance instead.

**Two footguns, stated plainly (full detail:
[`../packages/ne-storage/README.md`](../packages/ne-storage/README.md)):**
Object Lock can only be enabled AT bucket creation, never retrofitted onto an
existing bucket; and COMPLIANCE-mode retention cannot be shortened or removed
by anyone, including the account root, once set — choose the retention
period deliberately (`CheckpointConfig`'s default is 365 days).

### 6.6 Azure Blob ObjectStore WORM adapter (NE-AZBLOB-OBJECTSTORE)

Azure Blob is **not** S3-compatible, so §6.5's `s3` adapter cannot serve an
Azure deployment at all. `ne-storage`'s `azure_blob` plugin
(`ne.storage.azure_blob.AzureBlobObjectStore`) is the Azure production WORM
backend, backed by real **version-level immutability policies** in `Locked`
mode. Same frozen `ObjectStore` Protocol, same checkpoint loop, config-only
swap:

```
NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE=azure_blob
NE_STORAGE_AZURE_BLOB_ACCOUNT_URL=https://<account>.blob.core.windows.net
NE_STORAGE_AZURE_BLOB_CONTAINER=<worm-container>
# account_key: leave EMPTY in production — falls through to AKS workload
# identity / managed identity (the mirror of the S3 lane's IAM chain). Set it
# only for dev or a shared-key account.
```

Minimal RBAC (**documented, not live-verified** — confirm against your
tenant): **Storage Blob Data Contributor** on the container, plus the
immutability-policy permission
`Microsoft.Storage/storageAccounts/blobServices/containers/blobs/immutabilityPolicies/write`.

**C2 note:** `*.blob.core.windows.net` is real internet egress and must be in
the firewall egress allowlist — the same governed-egress posture as AWS's
regional S3 FQDNs. Under `AIR_GAP` a public Azure FQDN is **refused at
construction** (`AirGapViolationError`) unless it is listed in
`NE_STORAGE_AZURE_BLOB_ALLOWED_HOSTS`; that allowlist entry is how you assert
an **Azure Private Endpoint** resolves the FQDN privately in-VNet (Private
Endpoints keep the public FQDN, so only the operator can tell the two apart).

**Provisioning footgun:** blob **versioning and version-level immutability
(VLW) must be enabled at provisioning time** on the container (cf.
`deploy/terraform-azure/modules/storage/main.tf`) — neither is settable from
the data plane, so the adapter cannot turn them on. And once a policy is
`Locked`, its expiry can only be **extended**, never shortened — choose the
retention period deliberately (`CheckpointConfig`'s default is 365 days).

**Testing honesty:** Azurite does **not** implement immutability policies, so
unlike the S3 lane there is no local emulator that can prove this guarantee.
The adapter's retention state machine is proven against a fake in CI; the
server-side proof is live-parked against a real storage account for the
owner-gated NE-WORM-LIVE lane. Full detail:
[`../packages/ne-storage/README.md`](../packages/ne-storage/README.md).

An operator picks the cloud purely by
`NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE=s3|azure_blob` plus the matching
`NE_STORAGE_S3_*` / `NE_STORAGE_AZURE_BLOB_*` env block — no code change, no
rebuild.

---

## 7. The hard-constraint posture you inherit (C1–C12)

Every deployment inherits SPEC §2's twelve hard constraints. (The C10 slot changed
hands on 2026-07-18: the old "single-tenant" C10 was retired — a non-concept for
per-customer software in the customer's own environment, where the isolation
properties follow from C1 — and the slot was refilled with **audit-log integrity**,
which reconciled the customer-doc and SPEC numbering without renumbering C11/C12.
See SPEC §11.) One operator-facing
paragraph each.

- **C1 — Self-host, no phone-home.** Every component runs in the customer's
  VPC / on-prem; no SaaS control plane, no phone-home. Proven by the sealed-netns
  no-egress CI harness (NE-073).
- **C2 — Governed, deny-by-default egress.** No outbound call leaves except to an
  explicitly allowlisted destination, and every model call, connector pull, external
  tool call, and write-back is policy-checked and audited — any egress path can carry
  context out, so none is trusted as a "safe" lane. Frontier-model APIs through
  `ne-gateway` are the model-inference egress lane, and the `AIR_GAP` flag provably
  disables external egress (open-weight lane only). The air-gap harness is the proof;
  on cloud, the Azure Firewall FQDN allowlist is the enforcement point.
- **C3 — Provider-SDK chokepoint.** Every generative/frontier model call goes
  through `ne-gateway`; no module imports a provider SDK directly — enforced by an
  import-linter contract in CI. In-VPC inference (TEI, vLLM) is exempt from gateway
  routing but registry-pinned and AIR_GAP-accounted.
- **C4 — License gate.** AGPL / SSPL / BUSL / FSL / non-commercial are banned;
  the gate runs in supply-chain CI. Customer-side tools that are BUSL (Terraform)
  or pre-1.0 (Zarf, Apache-2.0) are **never shipped or added to `uv.lock`** — they
  run on the operator's / CI's side only.
- **C5 — Contract-swappable slots.** Modules interact only through `ne-contracts`
  Protocols; every slot is config-swappable, proven by conformance kits. Evidence:
  the shipped alternates (pgvector alongside Qdrant, Cedar alongside Rego) pass
  swap-parity tests.
- **C6 — Governed tool execution.** Every agent tool call passes the PDP **and**
  emits an audit event; there is no bypass path (guardian-tested across
  runtime / api / worker). This is why Flowise and CDC route through the same
  governed surface.
- **C7 — Read AND write, permission-trimmed.** The Knowledge Layer is read and
  write; retrieval is provably ACL-trimmed (vector + graph) and write-back is
  tier-enforced. SCIM deactivation removes access structurally through this trim.
- **C8 — KPI-first instrumentation.** Platform events carry an optional `kpi_tag`;
  reporting leads with named business KPIs, hours-saved is a supporting metric only.
- **C9 — Deploy anywhere.** OCI images are profile-agnostic with no
  cloud-provider-specific dependency; Azure is the reference test target, not a
  dependency. The EKS tree — now the hardened AWS reference (NE-AWS-1..13) —
  proves the same chart deploys on a second cloud.
- **C10 — Audit-log integrity (tamper-evident).** The audit log is append-only and
  hash-chained: database triggers reject UPDATE/DELETE, no module exposes a mutation
  API, and every record binds to its predecessor by SHA-256. Any tamper, deletion,
  reordering, or truncation is detectable — `ne audit verify` exits non-zero and
  names the offending record. Signed HMAC checkpoints archived to WORM anchor the
  chain against a whole-table rewrite. This is **detection, not prevention**: a
  sufficiently privileged operator can still destroy the database; what they cannot
  do is alter it undetectably.
- **C11 — Supply-chain integrity.** Pinned + hash-verified deps, SBOM (CycloneDX),
  vuln scan, cosign signing, SLSA provenance. The **one** exception is the
  first-party `ne-app` image: it is not on a public registry, so it is pinned by a
  local **build-in-seal** image id (Compose) or a captured **registry push-digest**
  (`ne release push`, K8s / Zarf) — a narrow, guardian-restricted exception no
  third-party image may adopt.
- **C12 — Deterministic guardrails before model judgment.** Spend caps, rate
  limits, egress/recipient allowlists, and PII denylists run in code **before**
  model judgment; a fully hijacked agent cannot execute a Tier-3 action without a
  human (HITL tiers). Secret hygiene: no secret literal in any committed file
  (the seeded demo users of §2 are the only sanctioned literals, demo-only).

> The console's posture view (`GET /v1/posture`) reports the per-constraint
> attestation table.

---

## 8. PENDING-OWNER / live-parked boundary

**Everything above this section is merged and CI-proven.** Everything in the table
below needs **owner-provisioned external resources** (tenants, cloud credentials,
quota, recorded demo runs) and **will not work today** — the operator must not
expect these paths to function on a fresh install. 

| Item | ID | Status | Why parked |
|---|---|---|---|
| Owner dev tenants | NE-000 | PENDING_OWNER | M365 dev tenant, Salesforce dev org, Slack workspace, Azure GPU quota — owner-executed external prerequisites. |
| Live cloud apply + GPU pool | NE-072-LIVE / SERVE-GPU-LIVE | PENDING_OWNER | A real Terraform plan/apply + the Azure NVadsA10v5 GPU lane — owner creds/quota; CI proves fmt/validate/tflint only. |
| Live Jira / Zendesk seeding | NE-093-LIVE | PENDING_OWNER | Connectors are BUILT fixture-first; live instance seeding needs owner instances. |
| Live Teams tenant e2e | NE-080-LIVE | PENDING_OWNER | Teams adapter is BUILT+merged; live e2e needs a Microsoft protected-API approval + tenant. |
| Live SCIM provisioning | NE-089-LIVE | PENDING_OWNER | SCIM provider is BUILT; live push needs a real Entra/Okta. |
| Acme seeding into tenants | NE-102 | PENDING_OWNER | Depends on NE-000 dev tenants. |
| Acme golden set + eval baseline | NE-075 | PENDING_OWNER | Demo arc; depends on NE-102. |
| Demo script (15-min arc) | NE-076 | PENDING_OWNER | Demo arc; depends on NE-075. |
| Recorded engagement-ready gate run | NE-077-LIVE | PENDING_OWNER | The recorded gate verification: fresh Azure-VM compose deploy from the install guide, ingest, brain gate, console write-back, model swap, audit verify, restore drill. The ingest INVOKER now exists (P1-4 — `ne connect sync` + the seeded connector-sync cron, §2 step 6); only the recorded run against owner-provisioned tenants stays parked here. |

Additionally live-parked (component built, e2e deferred), covered above:
Zarf full disconnected deploy (**NE-086-FOLLOWUP**, §5), CDC live source-DB proof
(§6.4). The OpenFGA ReBAC enable path (**NE-087-WIRING**, §6.3) is **merged**
(PR #127); enabling remains an explicit operator decision (`--profile rebac`).
