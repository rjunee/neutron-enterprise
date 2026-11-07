# deploy/terraform-eks

The hardened **C2/C9-parity AWS reference** for Neutron Enterprise's
production tier — the same `ne-app` image and promoted config bundle the
pilot Compose tier uses, expressed as cloud infrastructure, on a second
cloud. This tree has been **promoted in place** across NE-AWS-1 through
NE-AWS-13 (SPEC §11
2026-07-17) — it is **no longer** the minimal "C9 smoke" it started as: it
now mirrors `../terraform-azure` control-for-control (private 3-AZ network,
FQDN-exact deny-by-default egress, KMS CMKs, IRSA, flag-gated GPU + managed
Postgres, opt-in Velero).

`../terraform-azure` remains the **primary** reference (design D7 — no unit
here ever touches that tree); this tree proves the same umbrella chart,
pushed through the same `ne release push` mechanism, deploys hardened onto a
second cloud (C9). `egress_mode = "nat"` preserves the original cheap-smoke
posture as a documented degraded variant (§4), so the original "same chart,
different cloud" proof is unchanged even as the default posture hardened
around it.

**Status:** `terraform validate` / `fmt` / `tflint` clean, CI-gated
(`.github/workflows/terraform-validate.yml`), plus an offline
`terraform test` golden-plan harness (§3) — all with **zero AWS
credentials**. A real `terraform apply`/live evidence run against an AWS
account is **owner-gated** — see §14, the OWNER RUNBOOK.

---

## 1. Prerequisites

- An AWS account + a role the applier can assume (owner runbook, §14) —
  region `us-east-1` by default (`var.region`).
- **Terraform >= 1.9, < 2.0** — or **OpenTofu >= 1.9**; see
  [`../terraform-azure/README.md`](../terraform-azure/README.md)'s
  OpenTofu-compatibility note (§1 there) rather than duplicating it here —
  it applies identically to this tree. CI pins Terraform 1.15.8 exactly.
- `tflint` (core ruleset only — see `.tflint.hcl`).
- `aws` CLI, configured via OIDC-assumed role or `AWS_PROFILE` — never
  static access keys (C12).
- For the opt-in Velero install (`install_velero = true`): a runner/host
  that can reach the **private** EKS API server (Risk R7 — §5 below).

---

## 2. Component pin / provenance table (dependency-intake evidence)

Re-verified 2026-07-18 (SPEC §3.3 build-time re-check; this unit adds no new
components, it documents pins already committed in the tree — see prior
per-unit re-verification dates below for the ones this pass reconfirmed
unchanged).

| Component | Version | License | Source |
|---|---|---|---|
| Terraform CLI | 1.15.8 | BUSL-1.1 (tooling only, never shipped) | `github.com/hashicorp/terraform/releases` |
| OpenTofu (compat target) | 1.12.2 | MPL-2.0 | `github.com/opentofu/opentofu/releases` |
| `tflint` | 0.63.1 | MPL-2.0 (some BUSL-1.1-licensed vendored files; CI tool only, never shipped — terraform-linters/tflint#1826) | `github.com/terraform-linters/tflint/releases` |
| `hashicorp/aws` provider | 6.55.0 (exact) | MPL-2.0 | `registry.terraform.io/providers/hashicorp/aws` |
| `hashicorp/helm` provider | 3.2.0 (exact) | MPL-2.0 | `registry.terraform.io/providers/hashicorp/helm` |
| `terraform-aws-modules/terraform-aws-vpc` | v6.6.1, commit `3ffbd46fb1c7733e1b34d8666893280454e27436` | Apache-2.0 | `github.com/terraform-aws-modules/terraform-aws-vpc` |
| `terraform-aws-modules/terraform-aws-eks` | v21.24.0, commit `d4669ca8eb109a37f6c19ab8fb9487ab7521a9ba` | Apache-2.0 | `github.com/terraform-aws-modules/terraform-aws-eks` |
| `terraform-aws-modules/terraform-aws-network-firewall` | v2.1.0, commit `9bb904be0622dd0d71164e9713ae506f34d0fb66` | Apache-2.0 | `github.com/terraform-aws-modules/terraform-aws-network-firewall` |
| `aws-ebs-csi-driver` EKS managed addon | `v1.62.0-eksbuild.1` (driver v1.62.0, 2026-06-16) | Apache-2.0 | `github.com/kubernetes-sigs/aws-ebs-csi-driver` |
| Velero Helm chart | 12.1.0 (appVersion 1.18.1) | Apache-2.0 (VMware Tanzu / CNCF) | `vmware-tanzu.github.io/helm-charts` |
| `velero/velero-plugin-for-aws` initContainer | v1.14.2 (the v1.14.x line targets Velero 1.18.x per its release notes — the upstream README compatibility matrix is stale, ending at v1.13↔1.17) | Apache-2.0 | `github.com/vmware-tanzu/velero-plugin-for-aws` |
| `crane` (go-containerregistry) | v0.21.7 (linux amd64 tarball sha256 `1a57bc98207fa1c0d04bf760699099e26f8383499bfd55b99c1b919a928a7230`) | Apache-2.0 | `github.com/google/go-containerregistry` |
| `nvidia-device-plugin` Helm chart | 0.19.3 (chart tgz sha256 `8f01067500e712508fe55fe060b6f895814dc7efc1cb96323db2ce4f777f6e96`; image `nvcr.io/nvidia/k8s-device-plugin:v0.19.3`, mirrored into ECR by resolved digest at run time) | Apache-2.0 | `nvidia.github.io/k8s-device-plugin` |

The `hashicorp/azuread` provider is **no longer part of this lane** — it was
removed entirely by NE-AWS-AZUREAD (2026-07-20) and re-homed to the opt-in
`../terraform-eks-entra-broker` root (which pins `azuread` 3.9.0 + `aws`
6.55.0 in its own `versions.tf`/`.terraform.lock.hcl`). See §8 for why. The
`aws` pin was advanced one point release to **6.55.0** (published 2026-07-15,
re-verified on registry.terraform.io 2026-07-20) as part of this change;
`versions.tf` and `.terraform.lock.hcl` now carry 6.55.0.

Both upstream modules (`vpc`, `eks`) and the newly-consolidated
`network-firewall` module are consumed as `git::…?ref=<40-hex-sha>`
sources — the version+hash pin mechanism (C11): registry-style module
references have no hash-verification mechanism, so a full commit SHA is the
equivalent guarantee. Provider hash verification comes from the committed
`.terraform.lock.hcl` (`h1:` entries for `linux_amd64`, `linux_arm64`,
`darwin_arm64`, `darwin_amd64`). The GPU node group (NE-AWS-9) and the
`aws-ebs-csi-driver` addon (NE-AWS-6) add no new pins — both are native
resources of the already-pinned `eks`/`aws` sources, so `.terraform.lock.hcl`
stays byte-identical for them (C11). The exact `-eksbuild.N` suffix is only
enumerable via `aws eks describe-addon-versions` (needs AWS credentials);
`eksbuild.1` is the invariant convention for every driver release, and any
mismatch surfaces at the first live plan (owner-gated, §14) as a trivial
one-line suffix bump. The `crane`/`nvidia-device-plugin` pins are consumed
only by `eks-hardened-apply.yml`, not by this Terraform
tree; both tarballs were independently downloaded and byte-verified against
the sha256s above at NE-AWS-13 build time — no drift on either spec-named
pin.

---

## 3. Layout / module map + offline test harness

```
deploy/terraform-eks/
  versions.tf   # aws = 6.55.0 exact, helm = 3.2.0 exact; required_version range (see ../terraform-azure/README.md OpenTofu note). NO azuread — the Entra broker moved to ../terraform-eks-entra-broker (§8)
  providers.tf  # provider "aws" block + tree-story header
  network.tf    # AZ data source + locals + module.vpc (3-AZ private HA network)
  endpoints.tf  # module.vpc_endpoints — 10 interface + s3 gateway endpoints (NE-AWS-3)
  cluster.tf    # module.eks (default node group + flag-gated GPU node group)
  registry.tf   # aws_ecr_repository.ne_app
  kms.tf        # module.kms — customer-managed CMK plane, one CMK per data class
  modules/kms/  # aws_kms_key + aws_kms_alias + least-privilege key policies
  storage.tf    # module.storage — S3 backup bucket (NE-AWS-8)
  modules/storage/  # versioned + SSE-KMS + public-access-block + TLS-only/deny-unencrypted policy + opt-in Object Lock
  data.tf       # module.data — flag-gated RDS Postgres multi-AZ (NE-AWS-10)
  modules/data/ # aws_db_subnet_group + dedicated SG + aws_db_instance (postgres 18, multi-AZ, rds-CMK)
  egress.tf     # module.egress — Network Firewall deny-by-default egress perimeter (NE-AWS-5)
  modules/egress/ # frontier rule group + firewall + CloudWatch logs + route insertion
  identity.tf   # module.identity — Velero IRSA role only (NE-AWS-7). The Entra broker moved to ../terraform-eks-entra-broker (NE-AWS-AZUREAD, §8)
  modules/identity/ # Velero IRSA trust + least-privilege policy only
  velero.tf     # opt-in stage-2 Velero install (NE-AWS-11): helm provider (aws eks get-token exec) + helm_release, IRSA + S3+CMK BSL
  variables.tf  # region (us-east-1), cluster_version (1.33, >=1.31 floor), prefix, tags,
                # api_public_access_cidrs ([] = private), deploy_role_arn,
                # gpu_node_pool (false = no GPU spend), gpu_instance_type,
                # enable_immutability, managed_postgres (false),
                # postgres_admin_password (sensitive, no default),
                # egress_mode ("firewall"/"nat"), frontier_fqdns (["api.anthropic.com"]),
                # ebs_csi_addon_version (v1.62.0-eksbuild.1, pinned C11),
                # install_velero (false)
  outputs.tf
  terraform.tfvars.example
  tests/hardened.tftest.hcl  # NE-AWS-12 offline golden-plan guardrails — terraform test + mock_provider, zero credentials
  tests/static_guards.sh     # NE-AWS-12 textual guards for invariants inside pinned upstream-module arguments

../terraform-eks-entra-broker/  # opt-in Entra->Keycloak broker root (NE-AWS-AZUREAD): its OWN azuread+aws pins/lock, applied SEPARATELY by an Azure-credentialed operator — see §8 + that root's README
```

**Offline test harness (NE-AWS-12, design D5):** run `terraform init
-backend=false -lockfile=readonly && terraform test` from
`deploy/terraform-eks` with **no AWS credentials** — `tests/hardened.tftest.hcl`
mocks the `aws` provider (`mock_provider "aws"`, `command = plan`
throughout) and executes each merged unit's acceptance proxy as a plan-time
assert: private-API endpoint gating, `/0` + wildcard-FQDN validation
rejections, 3-NAT HA, firewall route insertion, the AIR_GAP zero-pass-rules
invariant, S3 public-access-block + SSE-KMS, RDS multi-AZ/encrypted/private,
GPU taint + flag gating, and the CMK plane. Honest split: `terraform test`
assert conditions can only see module **outputs** plus root/module-targeted
first-party resources — arguments passed to the pinned upstream
`eks`/`network-firewall` modules (the private-endpoint derivation,
`drop_strict`/`forward_to_sfe` policy actions, `encryption_config`'s CMK
reference, GPU scale-to-zero) are not addressable from an assert, so those
invariants are enforced textually by `bash tests/static_guards.sh` instead.
Both are wired into the eks leg of `.github/workflows/terraform-validate.yml`
and gate every PR touching this tree. **R-A2:** mocked plans do not
exercise AWS-side schema/rule validation (e.g. Network Firewall rule
syntax, IAM policy shape) — the owner's first live plan (§14) remains the
real schema check.

- **VPC:** 3 AZs, private `/20` x3 (worker nodes), public `/24` x3 (NAT
  only, `map_public_ip_on_launch = false`), intra `/28` x3 reserved for AWS
  Network Firewall (the AzureFirewallSubnet twin, consumed by `egress.tf`),
  one NAT gateway per AZ.
- **VPC endpoints:** interface endpoints (`ecr.api`, `ecr.dkr`, `ec2`,
  `sts`, `eks`, `logs`, `kms`, `secretsmanager`, `elasticloadbalancing`,
  `autoscaling`) with private DNS enabled, one ENI per AZ in the private
  worker subnets, behind a dedicated security group allowing 443 from the
  VPC CIDR only; plus an `s3` **gateway** endpoint on the private route
  tables (ECR layer pulls resolve to S3). Consumed via the `vpc-endpoints`
  submodule of the already-pinned vpc module repo at the identical commit
  SHA — no new pin, `.terraform.lock.hcl` unchanged (C11). See §4 for how
  this interacts with the egress perimeter.
- **ECR:** `neutron/ne-app`, mutable tags + `scan_on_push`. Mutable tags are
  fine here — **digest pinning is the C11 mechanism** (the chart's
  `image.digest` field), not registry tag immutability; this matches the
  posture the ACR reference takes. Images at rest are CMK-encrypted (the
  `s3-backup` CMK, NE-AWS-8).
- **KMS:** five customer-managed CMKs — one per data class (`eks-secrets`,
  `ebs`, `rds`, `s3-backup`, `logs`) — `enable_key_rotation = true`, aliases
  `alias/<prefix>-<class>`. Key policies are least-privilege: the account
  root (IAM delegation) is the only principal at defaults — no wildcard
  principals; workload-role and service access is wired per-unit through
  the module's `key_usage_role_arns` / `key_service_principals` inputs.
  `eks-secrets` needs no key-policy grant at all — it is consumed via
  `encryption_config.provider_key_arn` (`cluster.tf`), which the module
  covers with an IAM policy on the cluster role instead. No key material
  exists outside KMS or in state (C12).

---

## 4. Network posture (C2) — read this before anything else

**Security groups are stateful allow-lists, not the FQDN control.** The
default node group's SG (and the GPU node group's, §6) is deliberately
egress-open at 443 — an SG evaluates a rule against a packet's real, final
destination, never against a route table's next hop, so both `egress_mode`
values' route (0.0.0.0/0 → the firewall's VPC endpoint, or the NAT
gateway) still leaves that traffic classified as ordinary Internet-bound
traffic at the SG layer. A hard "deny Internet" SG rule would therefore
also block the firewall-routed frontier lane and every AWS PrivateLink
dependency — silently defeating C2 instead of enforcing it. This is the
exact twin of `../terraform-azure/README.md` §4's NSG/UDR explanation:
**the actual deny-by-default, FQDN-exact enforcement point is AWS Network
Firewall** (`egress.tf` + `modules/egress/`, wrapping
`terraform-aws-modules/terraform-aws-network-firewall` v2.1.0), not the SGs.

- **Policy shape:** `STRICT_ORDER` with stateful defaults `aws:drop_strict`
  + `aws:alert_strict` (deny-by-default, alert on every drop) — and,
  critically, the **stateless** default actions (normal + fragment) are
  explicitly overridden to `aws:forward_to_sfe`. The upstream module
  default is `["aws:pass"]`, which routes packets AROUND the stateful
  engine entirely — a fail-open bypass. **Reviewers: this override must
  never be reverted.**
- **The FQDN-exact allowlist:** one `STATEFUL` rule group rendered from
  `var.frontier_fqdns` (default `["api.anthropic.com"]`) holds exact-match
  `tls.sni` PASS rules (`startswith` + `endswith` + `nocase` together, so
  no substring/subdomain slip-through — this is what "single frontier
  lane" means for C2), plus one mandatory
  `flow:not_established` TCP/443 handshake PASS rule. Without the
  handshake rule, `drop_strict` kills the ClientHello before the SNI is
  even visible, so even an allowed FQDN would fail — the handshake rule
  authorizes no data egress, only the 3-way handshake itself.
- **AIR-GAP (design D6):** `frontier_fqdns = []` ⇒ zero PASS rules ⇒ no
  rule group is even created ⇒ the default-drop policy drops **all**
  egress. PrivateLink (the VPC endpoints, §3) keeps image pulls + every AWS
  API call working, since that traffic never transits the firewall at all
  — proving the air-gap and normal cluster operability aren't in tension.
- **Routing:** per-AZ insertion — private-subnet route tables' default
  route points at the same-AZ firewall VPC endpoint; the firewall (intra)
  subnet's route table points at the same-AZ NAT gateway; public-subnet
  route tables get a return route back to the firewall endpoint for
  symmetric inspection. The vpc module's own managed NAT default route is
  suppressed in `firewall` mode.
- **Logging:** ALERT and FLOW logs land in two CloudWatch log groups
  encrypted with the `logs` CMK (NE-AWS-4/5 wired the `logs` service
  principal onto that CMK).
- **`egress_mode = "nat"` (degraded variant):** per-AZ NAT only, **no**
  FQDN filtering — a pod that can reach 443 outbound at all can reach any
  HTTPS FQDN, not just the sanctioned frontier lane. This is a
  pilot-adjacent-budget variant, never the recommended posture, and the
  precision loss is **never silent**: it is stated here, in
  `variables.tf`'s description, in the egress module's comments, and the
  live workflow's egress-probe evidence legs write an explicit `SKIPPED`
  marker rather than silently omitting the check (Risk-R4 twin of Azure).
- **NTP:** AWS's link-local Amazon Time Sync service (`169.254.169.123`)
  needs no firewall hole at all — a *stronger* posture than Azure's
  aks-required-infra FQDN allowlist, which needs an explicit application
  rule for its infra dependencies.
- **The S3 backup bucket's `aws:sourceVpce` fence:** beyond the network
  path itself (reachable only via the `s3` gateway endpoint from inside
  the VPC), the bucket policy also denies `GetObject`/`PutObject`/
  `DeleteObject` whose request doesn't carry the s3 gateway endpoint's
  `aws:sourceVpce` condition key, so a leaked-but-valid credential can't
  read/write backup data over the public S3 endpoint from outside the VPC
  (Terraform's own apply/destroy of the bucket — CreateBucket, tags,
  versioning, policy, teardown — is unaffected since only the three
  object-data actions are fenced).
- **Object Lock (`var.enable_immutability`, default `false`):** opts the
  backup bucket in to S3 Object Lock 30-day **COMPLIANCE** retention — the
  WORM twin of the Azure immutability flag.
  **WARNING: irreversible.** COMPLIANCE-mode locked object versions cannot
  be deleted or shortened by any principal — including the account
  root — until retention expires, and toggling the flag on an existing
  deployment forces bucket replacement; never enable it without an
  explicit customer decision.

---

## 5. Private-cluster operability (D4 / Risk-R7)

The EKS API endpoint is **private at defaults**
(`endpoint_public_access` derived `false` when `api_public_access_cidrs =
[]`). Public access exists only as the D4 tightly-restricted CI-runner
CIDR exception: `var.api_public_access_cidrs` accepts a non-empty CIDR
list to open **exactly those addresses**; validation rejects `0.0.0.0/0`
outright. The live workflow (§12) injects its own detected runner egress
IP as a `/32` for the duration of the run — never a broad range.
Customers deploying this reference leave `api_public_access_cidrs = []`
and reach the cluster from a bastion, VPN, or self-hosted runner inside
the VPC — the same operational shape as `../terraform-azure/README.md`
§6's Risk-R7 ops-runner pattern.

Access is via EKS **access entries** in `API` authentication mode — no
`aws-auth` ConfigMap, no IAM users for humans. `helm`/`velero exec` auth is
`aws eks get-token --cluster-name … --region …`, the direct twin of the
Azure tree's `kubelogin` exec plugin: both authenticate against the cloud's
own IAM/RBAC rather than a static kubeconfig token.

---

## 6. GPU node group (Risk-R2 — owner-quota-gated)

**IMDSv2 required on both node groups (NE-AWS-6):** `cluster.tf` sets
`http_tokens` to `required` via `var.node_metadata_http_tokens` and
`http_put_response_hop_limit = 1` on the default node group; the GPU node
group (below) inherits the identical `metadata_options` submodule default
untouched, so both groups end up IMDSv2-hardened. A hop limit of 1 means
pods without host networking cannot reach the instance metadata service at
all and must use IRSA instead of stealing node credentials off the
metadata endpoint.

`var.gpu_node_pool` defaults to `false` — the same owner-spend gate as
Azure's Risk-R2 flag. At flag-on, a managed node group `gpu` is added:
1x `g5.2xlarge` (1x NVIDIA A10G — the direct A10 twin;
`g6.2xlarge`/L4 is the documented alternative via `var.gpu_instance_type`),
`min_size 0 / max_size 1 / desired_size 0` (scale-to-zero — a standing GPU
node is an explicit funded choice), taint `nvidia.com/gpu=present:NoSchedule`
(EKS API `effect = "NO_SCHEDULE"` — byte-parity with the AKS pool),
`ami_type = "AL2023_x86_64_NVIDIA"`, and explicit private `subnet_ids` — the
GPU lane inherits the private route tables and egresses through the same
firewall/NAT path as the default group, no separate egress (C2). The root
volume is CMK-encrypted (`ebs` CMK) via `block_device_mappings`.

**Quota:** flipping the flag on requires G-family on-demand quota (≥8 vCPU)
in `var.region` (request it **first**; it has the longest lead time of any owner step).

**Chart join:** the umbrella chart already carries the serve-side half —
`vllm.tolerations` + the `nvidia.com/gpu` resource limit (gpu mode) at
`deploy/helm/neutron-enterprise/values.yaml:340-343` — nothing to change
in the chart for this lane. The NVIDIA device plugin is not in the AMI; it
is installed as a pinned helm step by the live workflow (§12).

**Live-launch note:** the `ebs` CMK is not account-root-only — its key
policy grants the `AWSServiceRoleForAutoScaling` service-linked role usage
+ `kms:CreateGrant` (under `kms:GrantIsForAWSResource`) so EC2/ASG can
encrypt node root volumes on launch, and the EBS CSI IRSA role (§3) holds
the identical action set via a scoped IAM identity policy instead of a
key-policy grant — the split exists because a key-policy reference to the
CSI role would be a Terraform dependency cycle (`module.kms` → the role →
the cluster's OIDC provider → `module.kms`) and would fail KMS's
principal-existence validation on every fresh apply (keys are always
created before the cluster); see `kms.tf` and `cluster.tf` for the full
rationale in code comments. **Fresh-account self-containment:** `kms.tf`
manages the `AWSServiceRoleForAutoScaling` service-linked role itself
(`aws_iam_service_linked_role.autoscaling`, gated by
`var.manage_autoscaling_service_linked_role`, default `true`) instead of
documenting a manual preflight — Terraform creates the role BEFORE the
`ebs` CMK key policy references it, a real dependency edge, so a
brand-new account (one that has never launched an Auto Scaling group)
applies cleanly with no out-of-band step. If the SLR already exists in the
account (common — many services create it automatically), a default-`true`
apply fails with `EntityAlreadyExists`; either set `-var
manage_autoscaling_service_linked_role=false` (the key policy falls back
to the same ARN, constructed from data sources — still correct since the
role is already present) or adopt the existing role with `terraform import
aws_iam_service_linked_role.autoscaling[0]
arn:<partition>:iam::<account-id>:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling`.
The live vLLM-schedules-and-serves + model-swap-eval proof, and the GPU
node-launch proof of this grant, remain **NE-AWS-OWNER** (§14).

---

## 7. Managed Postgres → chart `postgres.external` mapping

Set `var.managed_postgres = true` (and export
`TF_VAR_postgres_admin_password` — see §10) to provision a multi-AZ Amazon
RDS for PostgreSQL instance instead of the chart's in-cluster `postgres`
subchart — the multi-AZ twin of `../terraform-azure/README.md` §8's
ZoneRedundant Flexible Server.

| Terraform output | Chart `postgres.external` value |
|---|---|
| `postgres_fqdn` | `hostSecretRef` host |
| `5432` (fixed) | `hostSecretRef` port |
| `administrator_login` (`"ne"`) | DSN user — matches the pilot's `ne` user (compose.yaml) |
| `postgres_database_name` (`"neutron"`) | DSN database — matches the pilot's single `neutron` DB |
| (not output) `var.postgres_admin_password` | goes to the chart's Secret, never a Terraform output |

**NOTE — do not claim this works end-to-end yet.** `postgres.external` is
**NOT in the chart today** — it lands with the **NE-071b** increment. The key
names in this table are per the design doc (§1.3/§2.2) and must be
re-confirmed when NE-071b introduces `postgres.external` — a known, tracked
follow-up, not a silent assumption. Re-verified in this worktree
(2026-07-17): `deploy/helm/neutron-enterprise/values.yaml` has no
`postgres.external` key.

For exactly this reason, the live workflow (§12) deliberately has **no
`managed_postgres` input** — an RDS the chart cannot yet consume is never
provisioned from CI. The honest claim about this path, even after a live
apply, is "provisioned multi-AZ/CMK/private" — never "end-to-end managed
database", until NE-071b ships and this table's mapping is re-verified
against the merged chart.

**Cost:** default `false` = zero RDS spend. Flag-on is a multi-AZ
`db.m6i.xlarge` — roughly 2x a single-AZ instance's cost (synchronous
standby in a second AZ) — a cost-gated owner decision, same posture as the
Azure `managed_postgres` flag.

**Teardown:** `deletion_protection = true` means a deliberate `terraform
destroy` needs it flipped off in a prior apply first; a final snapshot is
always taken via `final_snapshot_identifier`. The live AZ-loss/restore drill
and any real apply of the flag-on path are **NE-AWS-OWNER** (§14) — needs a
real AWS account + `TF_VAR_postgres_admin_password` + an encrypted backend.

**Backups & PITR (NE-DR1).** `backup_retention_period` (module `data` var,
default **7**, range 7–35) sets the automated-backup / point-in-time-restore
window — retention > 0 keeps PITR active, so you can restore to any point inside
the window (the PITR granularity itself is a managed-RDS property, typically
minutes, not something this Terraform configures). This is the **PRIMARY**
protection for the transactional DB per SPEC §11; the Velero VolumeSnapshotLocation
(§9) deliberately excludes the DB. See `docs/ops/disaster-recovery.md` for the
RPO/RTO matrix and the RDS PITR restore drill.

---

## 7a. gp3 StorageClass for the chart

The EBS CSI addon lands in this tree, but applying a `StorageClass` is a
`kubectl`/Helm-time step against a live cluster, not a Terraform resource —
the chart itself stays cloud-neutral (C9) and carries no cloud-specific
storage manifest. This is the note for whoever wires it up (the live
workflow, §12):

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3-encrypted
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  encrypted: "true"
  kmsKeyId: <gp3_kms_key_id output>   # full CMK ARN, e.g. arn:aws:kms:us-east-1:<account>:key/<id>
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
```

`kmsKeyId` takes the `gp3_kms_key_id` Terraform output verbatim (the full
`ebs` CMK ARN — the form the EBS CSI driver docs recommend over a bare key
ID). Applying this manifest — and optionally marking it the cluster
default (`storageclass.kubernetes.io/is-default-class: "true"`) — is left
to the live workflow; nothing in this repo applies it automatically.

---

## 8. Identity: Velero IRSA (Entra broker → separate opt-in root)

`identity.tf` + `modules/identity/` land the design's Velero IRSA deliverable.
The Entra→Keycloak broker was **removed from this lane** and re-homed to
`../terraform-eks-entra-broker` (NE-AWS-AZUREAD, 2026-07-20) — the "why" is
below, after the Velero half.

**Velero IRSA:** `module.identity.velero_role_arn` is an IAM role
federated-trusted to the cluster's own OIDC provider
(`module.eks.oidc_provider_arn`), with trust-policy condition
`system:serviceaccount:velero:velero` — **byte-parity** with
`azurerm_federated_identity_credential.velero.subject` in
`../terraform-azure/modules/identity/main.tf`, so the same k8s
ServiceAccount subject federates trust on both clouds. Its policy is
least-privilege: S3 object ops scoped to `${backup_bucket_arn}/*` +
`ListBucket` on the bucket ARN itself (never `*`), KMS Encrypt/Decrypt/
ReEncrypt*/GenerateDataKey*/DescribeKey scoped to the `s3-backup` CMK ARN
only, and the upstream velero-plugin-for-aws documented EC2 snapshot
action set (`ec2:Describe*` cannot be resource-scoped, so `*` is the
least-privilege floor there). `velero_irsa_role_arn` feeds `velero.tf`'s
`helm_release` ServiceAccount annotation
(`eks.amazonaws.com/role-arn`). Zero static credentials anywhere in this
tree — grep-provable: `grep -rn 'aws_iam_access_key' deploy/terraform-eks`
returns nothing.

**Entra broker (now in `../terraform-eks-entra-broker`):** Entra remains the
IdP of record; Keycloak brokers only. The broker-only app registration +
service principal + client secret and their Secrets Manager writer
(`<prefix>-entra-broker-client-secret`, CMK-backed with the `eks-secrets` CMK,
**never a Terraform output** — C12) were moved 1:1 into the opt-in
`../terraform-eks-entra-broker` root. Applied **separately** by an operator who
holds Azure creds (`az login` or `ARM_*` with `Application.ReadWrite.All`) and
AWS Secrets Manager write, **after** this lane. Its inputs (`prefix`/`region`
must match this lane; `secrets_kms_key_arn` = this lane's `eks-secrets` CMK ARN,
read from `terraform -chdir=../terraform-eks output -json kms_key_arns`) and its
apply runbook live in that root's README. Wiring the broker into the Keycloak
realm (an `identityProviders` entry) is still NE-103b's realm-import artifact.

*Why the broker had to leave this lane (the truth, replacing an earlier
falsified claim):* the AWS-only lane declared an empty `provider "azuread" {}`
block only for the count-gated broker, on the belief that a lazily-configured
provider referenced by `count = 0` resources costs nothing on a
credential-less host. **The first live `eks-hardened-apply` run (2026-07-20)
falsified that.** `hashicorp/azuread` 3.9.0 **eagerly builds and validates an
Azure authorizer at provider `Configure()`** whenever *any* resource references
the provider — a `count = 0` resource still references it. On any host with
`az` present or Azure ambient identity — and **every GitHub-hosted runner is an
Azure VM with `az` installed but not logged in** — `Configure()` picks the
AzureCLI authorizer and `terraform apply` dies at provider-config with
`could not configure AzureCli Authorizer: ... Please run 'az login'`,
regardless of `enable_entra_broker`. The earlier "verified" note was wrong
because its "verification" used `env -i` (an empty environment, no `az`) plus
`terraform validate`/a local apply — **none of which reproduce a runner-shaped
host**: `validate` never configures providers at all, and `env -i` hides the
very `az` binary that triggers the failure. Every in-place workaround was
proven dead on the runner too (`use_cli=false` → "no Authorizer could be
configured"; dummy `ARM_*` → eager token fetch AADSTS700038; dummy `tenant_id`
→ still tries the CLI; env-unset → still fails). So the only correct fix was to
make the AWS-only lane **not reference the provider at all** — hence the
re-home. **The real proof is a test, not this paragraph:** the
`eks-provider-config-smoke` job in
`.github/workflows/terraform-validate.yml` runs on `ubuntu-latest` and (1)
asserts the provider is absent from this lane's `versions.tf`/`identity.tf`/
`modules/identity` + lock + `terraform providers` closure, (2) confirms the
runner has `az` present but not logged in, (3) runs a **credential-less
`terraform apply`** of this lane's post-fix provider set (`aws`/`helm`/`null`,
a count-gated `helm_release` + a `null_resource` — the same Configure-forcing
shape) and asserts `Apply complete`, and (4) a **negative control** that
re-runs the original empty-`azuread`-provider repro and asserts it *still*
eager-auth-fails (so the guard can never pass vacuously).

**State-encryption note** for the broker moved with it — see
`../terraform-eks-entra-broker/README.md`: the client secret transits state as
an `azuread_application_password` attribute whenever the broker is enabled, so
that root **REQUIRES an encrypted remote backend**, never local state.

---

## 9. Velero stage-2 install (opt-in)

`velero.tf` lands the backup-path deliverable. `var.install_velero`
defaults **false** (the Risk-R7 twin of `../terraform-azure/README.md` §6):
the base `terraform apply` (network, EKS, registry, storage, identity) runs
anywhere with AWS API reachability, but the `helm_release.velero` resource
needs an ops-runner that can reach the **private EKS API** (§5) — a VPC
jumpbox or self-hosted runner with the `aws` CLI and an EKS access entry
granting cluster-admin. **IRSA:** the `velero` ServiceAccount is annotated
`eks.amazonaws.com/role-arn = module.identity.velero_role_arn`
(`velero_irsa_role_arn` output) and `credentials.useSecret = false` — zero
static AWS credentials anywhere in this path (C12). **BSL:** provider
`aws`, bucket = `backup_bucket_name` (§3), `config.kmsKeyId` = the
`s3-backup` CMK (SSE-KMS). The in-cluster `backup.enabled` pg_dumpall
CronJob (chart `templates/backup-cronjob.yaml`) remains the
non-managed-postgres backup answer, **unchanged** by this tree. A live
backup/restore drill (`velero backup create` / `velero restore`) against a
real EKS cluster is **NE-AWS-OWNER** (§14).

**VolumeSnapshotLocation (NE-DR1).** `velero.tf` now also configures a
`volumeSnapshotLocation` named `default` (provider `aws`, `config.region` =
`var.region`) for **non-DB PVs only** (qdrant vector store, the
`ne-backup-storage` PVC). The `velero-plugin-for-aws` native EBS snapshotter
handles both in-tree and `ebs.csi.aws.com` volumes, and the NE-AWS-7 IRSA policy
already carries the plugin's EC2 snapshot statement — **no IAM change**. The
transactional DB is external managed RDS (never a cluster PV), protected by
native backup/PITR (§7), so it is excluded by posture; in-cluster Postgres
deployments are additionally excluded by the chart-side fail-closed guard.
Non-DB PV snapshots are **opt-in** via `backup.velero.snapshotVolumes=true`. See
`docs/ops/disaster-recovery.md`.

---

## 10. State hygiene (C12)

- **Never commit `*.tfstate`** (`.gitignore` enforces this;
  `tools/ne-cli/tests/test_terraform_hygiene.py::test_no_state_or_tfvars_committed`
  locks it).
- **An encrypted remote backend is REQUIRED** for any real apply: the
  Postgres admin password transits state as an `aws_db_instance` attribute
  whenever `managed_postgres = true` (Terraform's own limitation, not this
  module's). Configure an `s3` backend with SSE-KMS + DynamoDB locking +
  strict IAM, or an equivalent encrypted backend. (The Entra broker's client
  secret has the same requirement, but it now lives in the separate
  `../terraform-eks-entra-broker` root — see that root's README §state.)
- **No secret literal ever belongs in `.tf`, `terraform.tfvars`, or a
  committed example** — `postgres_admin_password` has no default; supply
  it via `TF_VAR_postgres_admin_password` sourced from your own secret
  store at apply time. `terraform.tfvars.example` carries placeholder
  names only.
- **Secrets are never Terraform outputs** — the Postgres admin password is
  consumed as a resource argument only (§7); the Entra broker client secret
  writes directly to Secrets Manager from its own root
  (`../terraform-eks-entra-broker`, §8).
- The git-scan guardian is `tools/ne-cli/tests/test_terraform_hygiene.py`.

---

## 11. Promotion runbook

```
terraform apply                                   # this root — provisions the cluster
ne release push --registry <ecr_repository_url>   # pushes ne-app, captures the digest
ne pilot promote                                   # the customer bundle (module TOMLs + secrets manifest)
ne k8s render --bundle <dir> --out values-<customer>.yaml
ne k8s up --bundle <dir>                           # installer Secret + helm upgrade --install
# smoke: GET https://<hostname>/healthz, one governed console call
```

The CI-shaped equivalent of this runbook is `eks-hardened-apply.yml`
(§12) — same order, automated, evidence-producing. Velero (stage-2,
`var.install_velero = true`, §9) installs from an ops-runner once the base
cluster is up.

---

## 12. The live workflow — `eks-hardened-apply.yml`

`eks-hardened-apply.yml` runs on `workflow_dispatch`
**ONLY** — the old weekly cron was dropped: hardened spend (Network
Firewall, an optional GPU node group, TEI/vLLM weight staging) is never
scheduled, only ever a deliberate owner action. The SPEC's C9 enforcement
row is satisfied by this workflow existing plus a recorded green run in
the release evidence, not by per-PR (or per-week) cloud spend.

The `preflight` job checks `secrets.AWS_EKS_SMOKE_ROLE_ARN`; when it is
unset the `hardened-apply` job is skipped cleanly with a `::notice::`
explaining why. This is the documented, intentional state until the owner
wires an AWS account (§14).

**Inputs:**

| Input | Type | Default | Meaning |
|---|---|---|---|
| `teardown` | boolean | `true` | Run `terraform destroy` (+ ECR/namespace cleanup) after the evidence legs. A standing environment is an explicit funded choice — flip to `false` only to leave a live env up on purpose. |
| `gpu_node_pool` | boolean | `false` | Add the tainted scale-to-zero GPU node group, install the SHA-pinned NVIDIA device plugin, and bring up a small-model (Qwen3-0.6B) vLLM GPU smoke. |
| `egress_mode` | choice (`firewall`/`nat`) | `firewall` | `firewall` proves the C2 deny-by-default Network Firewall perimeter (the egress evidence legs run); `nat` is a minimal-cost per-AZ-NAT-only variant that loses FQDN precision — the egress legs write an explicit `SKIPPED` marker, never silently. |

There is deliberately **no `managed_postgres` input** — see §7.

**Evidence legs** (`release-evidence` artifact, uploaded `if: always()`
before teardown runs):

1. `01-no-public-node-ips.txt` — `kubectl get nodes,pods -A -o wide` plus an
   assert that no node carries a public `ExternalIP`.
2. `02-describe-cluster.json` — `aws eks describe-cluster`, asserting the
   `encryptionConfig` CMK is set and the API endpoint is private
   (`endpointPrivateAccess=true`, `publicAccessCidrs` == exactly the
   runner's `/32`).
3. `03-cluster-health.txt` + `03-eval-golden-set.txt` — cluster half:
   `/healthz` + pushed-digest imageID proof (+ vLLM `/v1/models` when GPU);
   eval half: the repo's canonical public retrieval-baseline gate
   (`packages/ne-eval/tests/test_retrieval_baseline_public.py`,
   `NE_REQUIRE_PUBLIC_BASELINE=1`) — there is no live in-cluster EvalTarget
   runner yet, so this leg proves the golden-set gate on the CI runner, not
   against the live cluster; scoped truthfully in the workflow comment.
   This leg runs LAST among the evidence legs (design R-A5): the cheap
   infra asserts run first so an early failure doesn't burn GPU hours.
4. `04-egress-probes.txt` — (`egress_mode=firewall` only) a blocked probe
   (`https://example.com`, not in `frontier_fqdns`, MUST fail), an
   allowed-lane probe (`https://api.anthropic.com`, MUST succeed), and a
   CloudWatch alert-log query proving the Network Firewall actually dropped
   the blocked probe. `egress_mode=nat` writes an explicit `SKIPPED` marker
   instead (never silent about the precision loss — Risk-R4).
5. `05-airgap-apply.log` + `05-airgap-probe.txt` — (`egress_mode=firewall`
   only) re-applies with `frontier_fqdns=[]` (AIR_GAP) and re-runs the
   allowed-lane probe, now asserting it also fails. `egress_mode=nat` writes
   the same explicit `SKIPPED` marker.
6. `06-static-creds-grep.txt` — greps the repo tree, `deploy/terraform-eks/`,
   and the rendered Helm manifest for static-AWS-credential shapes; asserts
   zero matches.

Plus `terraform-apply.log` and `mirror-manifest.txt` (both redacted, C12).

**Caveats:**

- **Runner-local Terraform state.** This live-evidence lane keeps state
  ONLY on the GitHub-hosted runner (no remote backend). A mid-run job kill
  (cancel, runner eviction, the 300-minute job timeout) orphans whatever
  was applied with no state anywhere to reconcile against. Owner cleanup:
  find/destroy resources by the `ne-eks-smoke` prefix tag (`aws
  resourcegroupstaggingapi get-resources --tag-filters
  Key=purpose,Values=c9-portability-smoke` or the `prefix` tag on each
  resource type). Those literal identifiers — `ne-eks-smoke` /
  `c9-portability-smoke` — are the committed `variables.tf`/
  `terraform.tfvars.example` defaults; they are historical from this tree's original C9-smoke identity, and
  renaming them is a `.tf`/tfvars change out of this docs unit's scope.
  The workflow's `concurrency` group at least prevents two applies racing
  the same prefix.
- **NetworkPolicy vs. the real enforcement point.** The umbrella chart
  install sets `networkPolicy.enabled=true`, rendering the chart's
  default-deny NetworkPolicy objects — but the standard AWS VPC CNI does
  **not** enforce Kubernetes NetworkPolicy by default (that needs a day-2
  CNI upgrade, e.g. Calico/Cilium). Those objects are posture here, not the
  control under test. The C2 deny-by-default egress control this workflow
  actually proves is the **AWS Network Firewall** (evidence legs 4/5).
- Every run, whether or not the earlier steps succeed, **always** uploads
  the evidence artifact (`if: always()`), then — gated on
  `always() && inputs.teardown` — tears down: deletes the workload
  namespaces (so the EBS CSI driver releases PVC-backed volumes cleanly),
  deletes the crane-mirrored ECR repos, empties (then `terraform destroy`
  removes) the terraform-managed `neutron/ne-app` repo, destroys the
  Terraform-managed infra, and sweeps any orphaned CSI-tagged EBS volumes
  as a belt-and-braces check. Nothing from this tree is left running or
  billing between runs when `teardown=true` (the default).

---

## 13. Cost table (D8) — all figures ballpark, `us-east-1`

| Item | Gate | Ballpark |
|---|---|---|
| EKS control plane | always | ~$0.10/h |
| NAT gateways (×3) | always | ~$0.045/h each |
| Interface VPC endpoints (×10) | always | ~$0.01/h each + data processed |
| AWS Network Firewall | `egress_mode=firewall` (default) | ~$0.40/h/endpoint × 3 AZ + $0.065/GB processed |
| GPU node (`g5.2xlarge`) | `gpu_node_pool=true`, scale-to-zero | ~$1.21/h while up |
| RDS multi-AZ (`db.m6i.xlarge`) | `managed_postgres=true` | ~2x a single-AZ instance (synchronous standby) |
| **Full evidence run** (≤4h, incl. GPU leg) | one `workflow_dispatch` | **< $25** |
| **Standing full stack** (no teardown) | `teardown=false` — explicit owner choice | ≈ **$1.4k–2.5k/mo** |

`teardown=true` is the workflow's default (§12) — a standing environment is
an explicit funded choice, never the default outcome of a dispatch.

---

## 14. OWNER RUNBOOK (owner action required)

The Terraform + workflow are built; the only remaining blocker for a real,
evidence-producing run is the AWS account itself and the trust it grants.

1. **Pick/create the AWS account + region** —
   single-tenant class matching the claim; give the region (default
   `us-east-1`).
2. **Request GPU quota first** — longest lead time of any
   step; do this before anything else if a GPU-inclusive run is wanted.
3. **Create the GitHub-OIDC IdP + IAM role** — trust
   restricted to this repo + this workflow; no static AWS keys anywhere.
4. **Set `secrets.AWS_EKS_SMOKE_ROLE_ARN`** — the single
   most important unblock; the same secret gate this lane has waited on
   since its original C9-smoke incarnation.
5. **Dispatch `eks-hardened-apply`** with the inputs from §12 above;
   teardown auto-runs when `teardown=true` (the default).
6. **Download the `release-evidence` artifact** — a
   recorded green run is the actual C9 release evidence this tree's SPEC
   row asks for, and the only thing that can flip a claim row from
   NOT-YET-APPLIED to applied.
