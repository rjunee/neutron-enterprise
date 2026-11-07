# deploy/terraform-azure

Private AKS 1.31+ reference for Neutron Enterprise's production tier
(design: `docs/architecture/prod-tier-helm-terraform-design.md` §2). This is
**packaging only** — the same `ne-app` image and promoted config bundle the
pilot Compose tier uses, expressed as cloud infrastructure. It is the AKS
**reference**; `../terraform-eks` is the **hardened C2/C9-parity AWS
reference** — the same chart on a second cloud, with the Azure hardened
posture (deny-by-default FQDN egress, private endpoints, CMKs, IRSA, GPU
lane, HA data) mirrored (NE-AWS-1..14; its live apply is owner-gated).

**Status:** `terraform validate` / `fmt` / `tflint` clean, CI-gated
(`.github/workflows/terraform-validate.yml`). A real `terraform plan`/`apply`
against an Azure subscription is **LIVE-PARKED** — see the LIVE-PARKED
section at the bottom.

---

## 1. Prerequisites

- An Azure subscription, logged in via `az login` (or a service principal
  exported as `ARM_*` env vars) with, at subscription scope, **Owner**, OR
  **Contributor PLUS Role Based Access Control Administrator** (or User
  Access Administrator) — Contributor alone is NOT sufficient, since this
  root creates `azurerm_role_assignment` resources (Key Vault Secrets
  Officer, AcrPull, Storage Blob Data Contributor) via
  `Microsoft.Authorization/roleAssignments/write`, which Contributor
  explicitly excludes by design. Also needs Entra ID Graph
  `Application.ReadWrite.All` (the broker app registration needs this).
- **Terraform >= 1.9, < 2.0** — or **OpenTofu >= 1.9** (see the
  OpenTofu-compatibility note immediately below). CI pins Terraform 1.15.8
  exactly.
- `tflint` (core ruleset only — see `.tflint.hcl`).
- For the opt-in Velero install (`install_velero = true`): `kubelogin`
  installed on whichever host runs `terraform apply` for that resource, plus
  network reachability to the private AKS API server (Risk R7, §6 below).

### OpenTofu compatibility

Terraform CLI is licensed **BUSL-1.1**. This repository never ships or
embeds the Terraform binary — it is a customer-side/CI tool only, exactly
like `kubectl` or `az`, and the SPEC's NE-072 row explicitly mandates
Terraform. `versions.tf` pins `required_version = ">= 1.9.0, < 2.0.0"` — a
**range**, not an exact binary pin — specifically so this HCL also runs
unmodified under **OpenTofu >= 1.9** (MPL-2.0, Linux Foundation/CNCF; current
release 1.12.2, `opentofu.org`). If BUSL is a blocker for a given engagement,
substitute `tofu` for `terraform` in every command below; the module tree,
provider pins, and lock files are identical either way.

---

## 2. Component pin / provenance table (dependency-intake evidence)

Re-verified 2026-07-23 (SPEC §3.3 build-time re-check):

| Component | Version | License | Source |
|---|---|---|---|
| Terraform CLI | 1.15.8 | BUSL-1.1 (tooling only, never shipped) | `github.com/hashicorp/terraform/releases` |
| OpenTofu (compat target) | 1.12.2 | MPL-2.0 | `github.com/opentofu/opentofu/releases` |
| `hashicorp/azurerm` provider | 4.80.0 (exact) — **drift flagged**: 4.81.0 is current but adds no VLW support (hashicorp/terraform-provider-azurerm#21512 / #31438 both still open); deliberately not bumped (out of scope for NE-LP-AZURE-VLW-FIX) | MPL-2.0 | `registry.terraform.io/providers/hashicorp/azurerm` |
| `hashicorp/azuread` provider | 3.9.0 (exact, current, no drift) | MPL-2.0 | `registry.terraform.io/providers/hashicorp/azuread` |
| `hashicorp/helm` provider | 3.2.0 (exact, current, no drift) | MPL-2.0 | `registry.terraform.io/providers/hashicorp/helm` |
| `Azure/azapi` provider | 2.11.0 (exact) — provisions the `worm` VLW container `AzureBlobObjectStore.archive()` requires (C10); `azurerm` has no equivalent argument | MPL-2.0 | `registry.terraform.io/providers/Azure/azapi` |
| `tflint` | 0.63.1 | MPL-2.0 (some BUSL-1.1-licensed vendored files; CI tool only, never shipped — terraform-linters/tflint#1826) | `github.com/terraform-linters/tflint/releases` |
| Velero Helm chart | 12.1.0 (appVersion 1.18.1) | Apache-2.0 (VMware Tanzu / CNCF) | `vmware-tanzu.github.io/helm-charts` |
| `velero/velero-plugin-for-microsoft-azure` initContainer | v1.14.2 (NE-DR1 — bumped from v1.11.0; the v1.14.x line targets Velero 1.18.x, matching the chart's appVersion 1.18.1) | Apache-2.0 | `github.com/vmware-tanzu/velero-plugin-for-microsoft-azure` |
| `azure/login` GitHub Action | v2.3.0 (SHA-pinned `a457da9e…`; v3.0.0 exists — Node 20→24 runtime bump only, deliberate hold on the SPEC-mandated v2 line) | MIT | `github.com/Azure/login` |

Provider hash verification comes from the committed `.terraform.lock.hcl`
(C11) — every declared provider has `h1:` entries for `linux_amd64`,
`linux_arm64`, `darwin_arm64`, and `darwin_amd64`.

---

## 3. Layout / module map

```
deploy/terraform-azure/
  versions.tf, main.tf, variables.tf, outputs.tf, velero.tf
  terraform.tfvars.example, .gitignore, .tflint.hcl
  modules/
    network/    VNet, subnets, deny-by-default NSGs, private DNS zones
    egress/     Azure Firewall (FQDN allowlist) OR NAT Gateway; route table
    aks/        The private AKS cluster + node pools (system/user/gpu)
    registry/   Premium ACR + Private Endpoint + AcrPull role assignment
    storage/    Backup/Velero storage account + Private Endpoint + `worm` VLW container (C10)
    identity/   Key Vault + Entra broker app (both gated by var.create_entra_broker), Velero identity
    data/       Optional managed Postgres Flexible Server (var.managed_postgres)
```

**Apply order** (Terraform resolves this automatically from the module
graph — listed here for the mental model): resource group → `network` →
`egress` (needs network's subnet IDs) → `aks` (needs egress's
`outbound_type` + depends_on egress for route-table/NAT ordering) →
`registry` / `storage` / `identity` (need aks's OIDC issuer / kubelet
identity) → `data` (optional) → `velero.tf`'s `helm_release` (optional,
needs aks's private API server to be reachable).

---

## 4. Network posture (C2) — read this before anything else

**In-cluster `NetworkPolicy` is L4-coarse.** The chart's gateway-pod egress
policy (design §1.7) can only say "allow 443 from these pods" — vanilla
Kubernetes `NetworkPolicy` cannot express FQDNs. **The Azure Firewall's
application-rule allowlist (`modules/egress`, `var.frontier_fqdns`, default
`["api.anthropic.com"]`) is the control that carries FQDN exactness** — this
is the actual C2 single-frontier-lane enforcement point for this tier. An
empty `frontier_fqdns` list means air-gap: zero Internet application rules,
only the fixed AKS-required-infrastructure allowlist remains.

- **`egress_mode = "firewall"` (default):** `userDefinedRouting` through
  Azure Firewall Standard. FQDN-exact.
- **`egress_mode = "nat"`:** NAT Gateway + NSG service-tag rules only. This
  **loses FQDN precision** — a pod that can reach 443 outbound at all can
  reach any HTTPS FQDN, not just the sanctioned frontier lane. This is a
  pilot-adjacent-budget variant, never the recommended posture, and this
  degradation is never silent: it is stated here, in `variables.tf`'s
  description, and in the egress module's comments.
- **Subnet associations `for_each` over a STATIC-KEY map** (`system` /
  `user` / `gpu` → subnet ID), never a `toset()` of the IDs. Load-bearing,
  not style: the subnet IDs are computed in the same apply and unknown at
  plan time, and `for_each` demands statically-known keys — the list form
  failed the first real apply with "Invalid for_each argument"
  (NE-LP-AZURE-EGRESS-FOREACH). Regression-locked by
  `tests/egress_foreach.tftest.hcl`, whose harness runs plan network →
  egress together so the IDs are genuinely unknown.
- NSGs on every non-firewall AKS subnet allow intra-VNet traffic, the
  AKS-required `AzureCloud.<region>` service-tag endpoints (443/9000 per
  [AKS outbound rules](https://learn.microsoft.com/azure/aks/outbound-rules-control-egress)),
  NTP, Azure DNS — and, deliberately, the remaining Internet-bound traffic
  too. This is not a gap: NSGs evaluate a rule's destination against a
  packet's real, final destination, never against the route table's next
  hop, so both egress modes' UDR (0.0.0.0/0 → the firewall's private IP, or
  the NAT Gateway) still leaves that traffic classified `Internet` at the
  NSG. A hard "deny Internet" NSG rule would therefore also block the
  firewall-routed frontier lane and AKS-required-infra application rules —
  silently defeating C2 instead of enforcing it. The actual
  deny-by-default, FQDN-exact enforcement lives entirely at the Azure
  Firewall's application-rule allowlist (`modules/egress`); the
  private-endpoint subnet's NSG (never on the firewall/NAT route table)
  keeps a genuine hard deny.
- Clusters running Cilium can additionally tighten in-cluster enforcement
  with `CiliumNetworkPolicy` FQDN rules at the chart layer (design §1.7,
  optional, off by default) — this is a chart-level (NE-071) knob, not
  built here.

---

## 5. Private-Endpoint DNS verification checklist (Risk R3)

The classic Azure field failure: Private Endpoint NICs exist, but the
consuming client can't resolve the `privatelink.*` name because it isn't on
the linked VNet, or a peered/hub-spoke VNet lacks the zone link. After
`apply`, from a host **inside the VNet (or the VPN/peered network that
reaches it)**, verify each zone `terraform output pe_dns_checklist` lists:

```sh
# Expect a 10.20.9.0/24 (snet-private-endpoints) private IP for each:
dig +short <acr-login-server>              # privatelink.azurecr.io
dig +short <storage-account>.blob.core.windows.net   # privatelink.blob.core.windows.net
dig +short <key-vault-name>.vault.azure.net          # privatelink.vaultcore.azure.net
# only when managed_postgres = true:
dig +short <postgres-server>.postgres.database.azure.com  # privatelink.postgres.database.azure.com
```

If any of these resolve to a public IP (or fail to resolve at all), the zone
isn't linked to the VNet your resolver is using — re-check
`azurerm_private_dns_zone_virtual_network_link` in `modules/network`, or (for
a hub-spoke topology) add the spoke VNet as an additional link. The AKS API
server's own zone (`privatelink.<region>.azmk8s.io`) is system-managed by
the platform when `private_cluster_enabled = true` — no zone/link is created
here for it; a BYO hub-spoke variant is a documented customization, not
built by default.

---

## 6. Private-cluster operability (Risk R7)

`private_cluster_enabled = true` means the AKS API server has **no public
endpoint**. Every command that talks to the cluster — `kubectl`, `helm`, `ne
k8s up`, and (if `install_velero = true`) `terraform apply` itself for the
`helm_release.velero` resource — must run from **inside the VNet, or a
VPN/ExpressRoute-peered network that reaches it**. This is why
`install_velero` defaults to `false`: the base `terraform apply` (network,
AKS, registry, storage, identity) can run from anywhere with Azure API
reachability, but the Velero `helm_release` needs the ops-runner pattern.
Run it from a jumpbox/self-hosted CI runner inside the VNet, or over a
point-to-site VPN, with `kubelogin` installed (the AAD-RBAC-only cluster —
`local_account_disabled = true` — authenticates via `kubelogin`'s `azurecli`
exec plugin, not a static kubeconfig).

---

## 7. GPU node pool (Risk R2 — owner-quota-gated)

`var.gpu_node_pool` defaults to `false`. The GPU pool
(`Standard_NV36ads_A10_v5`, scale-to-zero, tainted
`nvidia.com/gpu=present:NoSchedule`) targets the owner's East US 2 quota
request (36 vCPU limit ⇒ exactly one NV36 node). `terraform apply` succeeds
today, before the quota lands, because the pool is entirely absent when the
flag is off. **CPU-mode vLLM** (the chart's `profiles.serve.mode = cpu`)
keeps the serve lane fully testable in the interim — the same trick `ne up
--serve-cpu` uses on the pilot tier.

---

## 8. Managed Postgres → chart `postgres.external` mapping (design §1.3/§2.2)

Set `var.managed_postgres = true` (and export
`TF_VAR_postgres_admin_password` — see §10) to provision a zone-redundant
Azure Database for PostgreSQL Flexible Server instead of the chart's
in-cluster `postgres` subchart. This is the recommended production HA
answer and the OpenShift-postgres escape hatch (design §1.5.3 — the
official `postgres` image is not arbitrary-UID-safe under restricted SCC).

| Terraform output | Chart `postgres.external` value |
|---|---|
| `postgres_fqdn` | `hostSecretRef` host |
| `5432` (fixed) | `hostSecretRef` port |
| `administrator_login` (`"ne"`) | DSN user — matches the pilot's `ne` user (compose.yaml) |
| `postgres_database_name` (`"neutron"`) | DSN database — matches the pilot's single `neutron` DB |
| (not output) `var.postgres_admin_password` | goes to the chart's Secret, never a Terraform output |

**NOTE — cross-check status (NE-072 synthesis, 2026-07-09):** NE-071a's
chart has merged (`deploy/helm/neutron-enterprise`; `global.hostname` and
`profiles.core` confirmed against its `values.yaml`), but `postgres.external`
is NOT in the chart yet — it lands with the NE-071b increment. The key names
in this table are per the design doc (§1.3, §2.2) and must be re-confirmed
when NE-071b introduces `postgres.external` — a known, tracked follow-up,
not a silent assumption.

**Backups & PITR (NE-DR1).** `backup_retention_days` (module `data` var,
default **7**, range 7–35) sets the automated-backup / PITR retention window;
Flexible Server PITR is **automatic** once retention > 0 (the granularity itself
is a managed-service property, typically minutes). `geo_redundant_backup_enabled`
is a flag, **default false** — enabling geo-redundant (paired-region) backup is
a cost + data-residency decision and is **create-time only**: flipping it on an
existing server **forces replacement** (destroy + create), so decide before the
first apply. This managed-native backup + PITR is the **PRIMARY** protection for
the transactional DB per SPEC §11 (the Velero VSL in §13 excludes the DB). See
`docs/ops/disaster-recovery.md` for the RPO/RTO matrix and the Flexible Server
PITR restore drill.

---

## 9. Entra broker app + realm join (design §2.4)

`modules/identity` provisions the **Entra-side, broker-only** app
registration for Keycloak: Entra remains the IdP of record, Keycloak brokers
only. It creates the app registration + service principal, generates a
client secret, and writes that secret **directly to Key Vault**
(`entra-broker-client-secret`) — it is never a Terraform output. Terraform
state still contains the secret in plaintext (Terraform's own limitation,
not this module's — see §11), so an encrypted remote backend is mandatory.

The Key Vault this writes to (when `var.key_vault_id == ""`, the default) is
created with `public_network_access_enabled = false` and reached only via a
private endpoint on the AKS VNet — so **whoever runs `terraform apply` must
have network reachability (route + private DNS resolution) to that
endpoint**. Three options exist for a runner that doesn't: (1) a
VNet-connected/self-hosted runner (the full default posture, broker +
private vault both provisioned); (2) `var.key_vault_id` pointing at a
pre-existing, reachable vault (still creates the broker, skips only the
local vault); or (3) `var.create_entra_broker = false`, which skips the
broker app *and* the private vault/PE/RBAC-grant trio entirely — because
that trio exists only to host the broker's secret, there is nothing left
for it to host once the broker is gated off. Option 3 is what
`azure-live-apply.yml`'s smoke dispatch does (NE-LP-AZURE-KV-REACH). See
§12.

Wiring the broker **into the Keycloak realm** — adding an
`identityProviders` entry pointing at this app registration — is the
**NE-BROKER-ENTRA-WIRE** epic's realm artifact
(`deploy/compose/pilot/keycloak/ne-pilot-realm.json` `identityProviders`
entry, alias pinned `entra` — unit **NE-BEW-REALM**); this module registers
the Entra-side redirect URIs for **both shipped tiers** so the registered URI
matches the actual Keycloak broker callback on either tier.

Entra requires the registered redirect URI to match the actual callback
**exactly**, and the two shipped tiers expose Keycloak at different external
forms — prod/Helm serves it at the ingress host-split
`https://auth.<hostname>/...` while pilot/compose serves it at the
port-split `https://<hostname>:8443/...` (per
`deploy/compose/pilot/compose.yaml`'s `KC_HOSTNAME`). So the module's
`broker_redirect_uris` variable defaults to registering **both** forms; the
`/broker/entra/endpoint` segment is fixed by the realm identityProvider
alias `entra` (design §3.4). Override via the module-level
`broker_redirect_uris` variable — note this root does not currently expose a
pass-through for it, so overriding requires a root-level variable addition
(deliberately out of NE-BEW-REDIRECT's scope; the module default applies as
shipped).

---

## 10. State hygiene (C12)

- **Never commit `*.tfstate`** (`.gitignore` enforces this;
  `test_terraform_hygiene.py::test_no_state_or_tfvars_committed` locks it).
- **An encrypted remote backend is REQUIRED** for any real apply: the Entra
  broker's client secret transits state as a `azuread_application_password`
  resource attribute, and the Postgres admin password transits state as an
  `azurerm_postgresql_flexible_server` attribute, whenever those features are
  used. Terraform has no mechanism to keep secrets out of state for
  resources that must know them — configure an `azurerm` backend with a
  storage account that has encryption-at-rest + strict RBAC, or an
  equivalent encrypted backend.
- **No secret literal ever belongs in `.tf`, `terraform.tfvars`, or a
  committed example** — `postgres_admin_password` has no default; supply it
  via `TF_VAR_postgres_admin_password` sourced from your own secret store at
  apply time. `terraform.tfvars.example` carries placeholder names only.
- The git-scan guardian is `tools/ne-cli/tests/test_terraform_hygiene.py`.

---

## 11. Promotion runbook (design §2.4)

```
terraform apply                          # this root — provisions the cluster
ne release push --registry <acr_login_server>   # pushes ne-app, captures the digest
ne pilot promote                         # the customer bundle (module TOMLs + secrets manifest)
ne k8s render --bundle <dir> --out values-<customer>.yaml
ne k8s up --bundle <dir>                 # installer Secret + helm upgrade --install
# smoke: GET https://<hostname>/healthz, one governed console call
```

`ne release push`, `ne k8s render`, and `ne k8s up` landed with **NE-071a**
(confirmed against `tools/ne-cli/src/ne/cli/release.py` / `k8s.py` at
synthesis, 2026-07-09) — this order is the design's contract (§2.4), not yet
independently exercised end-to-end by this unit (a live apply is parked, §12).

Velero (stage-2, `var.install_velero = true`) installs from an ops-runner
inside the VNet/VPN (§6) once the base cluster is up.

---

## 12. LIVE-PARKED (owner action required)

These are **not built here** — they require owner credentials, quota, or an
account that does not exist in CI:

- A real `terraform plan`/`terraform apply` against an Azure subscription is
  now available as an **owner-gated `workflow_dispatch` lane**:
  `azure-live-apply.yml` (OIDC via repo variables
  `AZURE_CLIENT_ID`/`AZURE_TENANT_ID`/`AZURE_SUBSCRIPTION_ID`, no client
  secret) applies this root, verifies the WORM container's VLW property and
  the AKS `networkProfile.networkPolicy` live, then destroys. It still
  never runs in PR CI — PR CI only runs `fmt` / `init -backend=false` /
  `validate` / `tflint` (`.github/workflows/terraform-validate.yml`); the
  live-apply lane is a separate, deliberate, owner-initiated dispatch.
  **Resolved by NE-LP-AZURE-KV-REACH:** the runner has no network route to
  the private-endpoint-only Key Vault `modules/identity` creates (§9), so
  an earlier version of this lane's first apply failed on the
  `entra_broker_client_secret` write before either evidence leg ran (PR
  #311 Codex C1). The lane's runner-only tfvars now set
  `create_entra_broker = false`, which skips the broker app and its
  private vault/PE/RBAC trio entirely. This narrows what the lane proves —
  it does **not** claim to exercise the broker/realm integration path;
  that path remains exercised only from a VNet-connected/self-hosted-
  runner apply with the shipped default (`create_entra_broker = true`).
- The GPU node pool (`var.gpu_node_pool = true`) — owner NVadsA10v5 East US 2
  quota grant. Not exercised
  by `azure-live-apply.yml`.
- A live AKS smoke (deploying the chart onto a real cluster) — depends on
  both a real `apply` and NE-071's chart merging. Not exercised by
  `azure-live-apply.yml`, which is private-cluster-API-plane-only (§6).

---

## 13. DR posture: Velero VolumeSnapshotLocation (NE-DR1)

`velero.tf` (opt-in, `var.install_velero = true`) configures a
`volumeSnapshotLocation` named `default` (provider `azure`,
`config.resourceGroup` = the main RG) for **non-DB PVs only** (qdrant vector
store, the `ne-backup-storage` PVC). Snapshots land in **our** resource group,
not the disposable `MC_*` AKS node RG, so they survive cluster deletion.

**Custom disk-snapshot role.** The Velero workload identity gets a least-privilege
custom role `<prefix>-velero-disk-snapshots` carrying exactly the
plugin's documented minimal actions (`Microsoft.Compute/disks/{read,write,
endGetAccess/action,beginGetAccess/action}` +
`Microsoft.Compute/snapshots/{read,write,delete}`). It is assigned at **two
scopes** and here is why:

- **AKS node RG** — the PV managed disks live there, so the identity needs
  `disks/read` + `disks/beginGetAccess` there to snapshot them.
- **Main RG** — snapshots are **written** into the main RG per the VSL config.

The principal is resolved via a `data.azurerm_user_assigned_identity` /
`data.azurerm_kubernetes_cluster` lookup in `velero.tf` (the `modules/identity`
outputs expose only `client_id`/`id`, not `principal_id`, and editing that module
is outside this unit's diff surface). The storage-account-key actions from the
plugin README are deliberately **omitted** (`useAAD=true`; the blob role is
already assigned in `modules/identity`).

**Plugin bump.** The `velero-plugin-for-microsoft-azure` initContainer image is
bumped **v1.11.0 → v1.14.2** (§2 pin table): v1.14.x is the Velero 1.18.x line
(v1.11.x targeted ~1.15). The mismatch was latent while the plugin only served
the BSL; the VSL makes its native disk snapshotter load-bearing.

**DB exclusion.** The transactional DB is external managed Flexible Server (never
a cluster PV), protected by native backup/PITR (§8), so it is excluded by
posture; in-cluster Postgres deployments are additionally excluded by the
chart-side fail-closed guard. Non-DB PV snapshots are **opt-in** via
`backup.velero.snapshotVolumes=true`. The cross-scope role assignment is only
fully exercisable at live apply (parked, §12). See
`docs/ops/disaster-recovery.md`.

---

## 14. WORM (VLW) container (C10, NE-LP-AZURE-VLW-FIX)

`ne.storage.azure_blob.AzureBlobObjectStore.archive()` preflights
`get_container_properties()` and requires
`immutable_storage_with_versioning_enabled` — Azure's **"immutable storage
with versioning"** feature (VLW) — to be true. This is a **different Azure
feature** from the optional, time-based `azurerm_storage_container_immutability_policy`
on the `velero` container (§13 has nothing to do with this).

**Why `azapi`, not `azurerm`.** `hashicorp/azurerm` has no argument to enable
VLW at any level, at either the pinned 4.80.0 or the current 4.81.0
(upstream `hashicorp/terraform-provider-azurerm#21512` and `#31438`, both
still open as of 2026-07-23). `modules/storage/main.tf` therefore provisions
`azapi_resource.worm` — a raw ARM PUT of
`Microsoft.Storage/storageAccounts/blobServices/containers` (api-version
2023-05-01) with `properties.immutableStorageWithVersioning.enabled = true`.

**Container-level, not account-level.** Azure also supports VLW at the
*storage account* level, but only **at account creation** — enabling it later
requires replacing the account. The `backup` storage account is already
provisioned in every applied environment, so account-level VLW was rejected
as destructive. Container-level VLW can be enabled at container creation
without touching the account, which is why this fix adds a **new**,
dedicated `worm` container rather than converting the existing `velero`
container (a container cannot carry both the time-based policy and VLW at
once, and Velero's snapshot workflow needs ordinary, non-WORM mutation).

**Existing-deployment remediation.** This is purely additive: an
already-applied environment simply gains the `worm` container on its next
`terraform apply` — no state migration, no account recreation, no downtime.

**Irreversibility.** Enabling VLW on the container is irreversible, but it
locks nothing by itself — per-version retention is set by `archive()`'s
`set_immutability_policy` call, not by this resource.

**Destroy note.** A VLW-enabled container must be **emptied of blobs before
it can be deleted** — this is why `azure-live-apply.yml`'s teardown succeeds:
that lane never writes a blob into the `worm` container, only live-reads its
`immutableStorageWithVersioning.enabled` property (§12).

**Proof.** `deploy/terraform-azure/tests/storage_worm.tftest.hcl` is a
credential-free, `mock_provider`-backed `terraform test` (CI: "terraform test
(offline golden-plan guardrails)" in `.github/workflows/terraform-validate.yml`,
now running for all three matrix roots) that asserts the `worm` container's
plan carries `immutableStorageWithVersioning.enabled = true` and that the
distinct time-based `velero` policy stays opt-in. A real-Azure apply +
live ARM read of `immutableStorageWithVersioning.enabled` is now the first
evidence leg of `azure-live-apply.yml` (§12,
`NE-LP-INFRA-AZURE`) — owner-gated, not yet exercised by an actual green
run. Until that happens, this unit proves the IaC is correct by
construction, not that a real Azure API accepted it.
