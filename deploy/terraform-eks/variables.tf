# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

variable "prefix" {
  description = "Short resource-name prefix for the smoke stack."
  type        = string
  default     = "ne-eks-smoke"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "cluster_version" {
  description = "Any cloud-supported minor >= 1.31 (the chart's kubeVersion floor)."
  type        = string
  default     = "1.33"
}

variable "tags" {
  type    = map(string)
  default = { purpose = "c9-portability-smoke" }
}

variable "api_public_access_cidrs" {
  description = "CIDR allowlist for the cluster's public API endpoint. Empty (default) keeps the API fully private — the C2 perimeter posture. Non-empty is the tightly-restricted CI-runner exception (design D4): the public endpoint is enabled restricted to exactly these CIDRs (the live workflow injects its runner egress /32)."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for cidr in var.api_public_access_cidrs : !endswith(cidr, "/0")])
    error_message = "api_public_access_cidrs must never contain a /0 CIDR (e.g. 0.0.0.0/0): the CI-runner exception is a tightly-restricted allowlist, not the public internet (design D4, SPEC C2)."
  }
}

variable "deploy_role_arn" {
  description = "IAM role ARN of the GitHub-OIDC deploy role (secrets.AWS_EKS_SMOKE_ROLE_ARN) granted cluster-admin via an EKS access entry. Default null creates no entry (offline validation needs no AWS account); the live workflow always passes its own role ARN. Roles only — no AWS IAM users for humans."
  type        = string
  default     = null
}

variable "gpu_node_pool" {
  description = <<-EOT
    Owner-spend-gated (the Azure Risk-R2 twin): when true, adds the tainted
    scale-to-zero `gpu` managed node group (g5.2xlarge / 1x NVIDIA A10G by
    default). Defaults to false so `terraform apply` incurs zero GPU cost
    until the owner flags it on (G-family on-demand quota in var.region
    required — NE-AWS-OWNER). CPU-mode vLLM keeps the serve lane testable
    without this flag.
  EOT
  type        = bool
  default     = false
}

variable "gpu_instance_type" {
  description = "Instance type for the flag-gated gpu node group. Default g5.2xlarge (1x NVIDIA A10G — the Standard_NV36ads_A10_v5 / A10 twin). g6.2xlarge (1x NVIDIA L4) is the documented newer/cheaper alternative (README §3)."
  type        = string
  default     = "g5.2xlarge"
}

variable "enable_immutability" {
  description = "Documented, off by default. Enables S3 Object Lock on the backup bucket with a 30-day COMPLIANCE-mode default retention (the WORM twin of terraform-azure's flag). IRREVERSIBLE: COMPLIANCE-mode locked object versions cannot be deleted or shortened by ANY principal, including the account root, until retention expires — never enable without an explicit customer decision (README). Toggling on an existing deployment also forces bucket replacement (object_lock_enabled is create-time)."
  type        = bool
  default     = false
}

variable "managed_postgres" {
  description = <<-EOT
    Provisions a multi-AZ Amazon RDS for PostgreSQL instance feeding the
    chart's postgres.external values instead of the in-cluster postgres
    subchart — the ZoneRedundant Flexible Server twin. Defaults to false
    (stage-2, owner decision to adopt managed HA postgres — no RDS cost
    unless flagged).
  EOT
  type        = bool
  default     = false
}

variable "install_velero" {
  description = <<-EOT
    Installs Velero via the helm_release in velero.tf. Defaults to false: the
    helm provider needs the PRIVATE EKS API server to be reachable from the
    Terraform runner (Risk R7), so this is an explicit stage-2 opt-in from an
    ops-runner inside the VPC (VPN/DX-peered network), never a default-on
    dependency of `apply`.
  EOT
  type        = bool
  default     = false
}

variable "postgres_admin_password" {
  description = <<-EOT
    Administrator password for the managed RDS Postgres instance (only
    consumed when managed_postgres = true). NO DEFAULT — C12: source this via
    TF_VAR_postgres_admin_password from the customer's own secret store,
    never a literal in tfvars or version control. NOTE: it still transits
    Terraform state in plaintext (Terraform's own limitation, not this
    module's) — the README requires an encrypted remote backend.
  EOT
  type        = string
  sensitive   = true
  default     = null
}

variable "egress_mode" {
  description = <<-EOT
    "firewall" (default): AWS Network Firewall STRICT_ORDER deny-by-default
    with exact-match TLS-SNI frontier-lane pass rules — the C2 perimeter
    control. "nat": per-AZ NAT only — a minimal-cost variant that LOSES FQDN
    precision (any pod that can reach 443 can reach any HTTPS FQDN). Never
    silent: README states this loss plainly (Azure Risk-R4 twin).
  EOT
  type        = string
  default     = "firewall"

  validation {
    condition     = contains(["firewall", "nat"], var.egress_mode)
    error_message = "egress_mode must be either \"firewall\" or \"nat\"."
  }
}

variable "frontier_fqdns" {
  description = <<-EOT
    The C2 single-frontier-lane allowlist: exact FQDNs permitted as TLS SNI
    over 443 out of the firewall-inspected subnets. This is the ONLY
    sanctioned egress path out of the cluster besides AWS's own
    PrivateLink-backed infrastructure traffic (VPC endpoints, NE-AWS-3). An
    empty list means air-gap: zero pass rules, the STRICT_ORDER default-drop
    policy drops ALL egress.
  EOT
  type        = list(string)
  default     = ["api.anthropic.com"]

  validation {
    condition     = alltrue([for f in var.frontier_fqdns : !strcontains(f, "*") && !startswith(f, ".")])
    error_message = "frontier_fqdns entries are exact-match SNI values - no wildcards or leading dots (C2 single-lane exactness)."
  }
}

variable "ebs_csi_addon_version" {
  description = "Pinned aws-ebs-csi-driver EKS managed-addon version (C11). v1.62.0 = driver release 2026-06-16 (Apache-2.0, kubernetes-sigs), compatible with cluster_version 1.33; re-resolve via aws eks describe-addon-versions --addon-name aws-ebs-csi-driver --kubernetes-version <ver> when bumping cluster_version."
  type        = string
  default     = "v1.62.0-eksbuild.1"
}

variable "manage_autoscaling_service_linked_role" {
  description = <<-EOT
    Codex P1 fix (NE-AWS-6, the NE-AWS-9 carry-forward): when true (default),
    Terraform itself creates the AWSServiceRoleForAutoScaling service-linked
    role (aws_iam_service_linked_role.autoscaling in kms.tf) and the `ebs`
    CMK key policy references that managed resource's ARN directly — so
    Terraform enforces the create-before-key-policy ordering and a
    brand-new AWS account (one that has never had an Auto Scaling group)
    applies cleanly with no manual preflight step. Set to false only for an
    account where the SLR already exists AND you do not want Terraform to
    adopt/manage it (e.g. it was created by another stack/root module) —
    in that case the `ebs` key policy falls back to the same ARN
    constructed from data sources (still correct, since the role already
    exists); flipping this from true to false after the role is
    Terraform-managed requires `terraform state rm
    aws_iam_service_linked_role.autoscaling` first. NOTE: `terraform apply`
    with this at its default `true` fails with EntityAlreadyExists if the
    role already exists in the account — re-run with `false` (or `terraform
    import aws_iam_service_linked_role.autoscaling[0]
    arn:<partition>:iam::<account-id>:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling`
    to adopt it instead) — see README §3 EKS/GPU live-launch note.
  EOT
  type        = bool
  default     = true
}

variable "node_metadata_http_tokens" {
  description = "IMDSv2 enforcement mode for the default EKS managed node group's metadata_options.http_tokens (C12 node hygiene). Extracted to a variable rather than an inline literal so the value is a var.* reference at its point of use, not a quoted-string assignment — the repo's credential-literal hygiene guardian (tools/ne-cli/tests/test_terraform_hygiene.py) pattern-matches any *token*/*_key*/*secret*/*password* assignment key against a quoted literal value, which \"http_tokens\" superficially collides with even though it carries no secret. Locked to \"required\" by the validation block below: IMDSv2 tokens-required is a load-bearing security invariant, not a tunable."
  type        = string
  default     = "required"

  validation {
    condition     = var.node_metadata_http_tokens == "required"
    error_message = "node_metadata_http_tokens must stay \"required\" (IMDSv2 enforced) — \"optional\" would permit IMDSv1 fallback and reopen the pod-to-instance-credential-theft path this hardening closes."
  }
}
