# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

variable "prefix" {
  description = "Short resource-name prefix for every resource in this reference (e.g. \"ne\")."
  type        = string
  default     = "ne"
}

variable "location" {
  description = <<-EOT
    Azure region. Defaults to the owner's GPU-quota target region
    (East US 2).
  EOT
  type        = string
  default     = "eastus2"
}

variable "hostname" {
  description = <<-EOT
    The console/API vhost (chart's global.hostname analogue). Also used to
    derive the Keycloak vhost (auth.<hostname>) for the Entra broker redirect
    URI. REQUIRED before a real plan/apply; left unset for validate-only CI.
  EOT
  type        = string
  default     = ""
}

variable "kubernetes_version" {
  description = <<-EOT
    Any cloud-supported minor >= 1.31 (the chart's kubeVersion floor). Default
    tracks Azure's recommended (isDefault) AKS minor on the standard
    KubernetesOfficial support plan — verified 2026-07-24 via `az aks
    get-versions --location eastus2` after live run 30077223244 failed with
    K8sVersionNotSupported (1.32 had moved to the LTS-only support plan).
    Re-bump whenever a live apply reports the pinned minor is no longer on
    the standard support plan.
  EOT
  type        = string
  default     = "1.35"
}

variable "egress_mode" {
  description = <<-EOT
    "firewall" (default): Azure Firewall FQDN allowlist is the C2 perimeter
    control — exact frontier-lane FQDN enforcement.
    "nat": NAT Gateway + NSG service-tag rules only — a minimal-cost variant
    that LOSES FQDN precision (pilot-adjacent budgets only). Never silent:
    the README states this loss plainly (Risk R4).
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
    The C2 single-frontier-lane allowlist: exact FQDNs the Azure Firewall's
    application-rule collection permits over HTTPS from the AKS subnets. This
    is the ONLY sanctioned egress path out of the cluster besides AKS's own
    required infrastructure traffic (control plane, ACR, etc — §ne-egress
    module). An empty list means air-gap: zero Internet application rules.
  EOT
  type        = list(string)
  default     = ["api.anthropic.com"]
}

variable "gpu_node_pool" {
  description = <<-EOT
    Owner-quota-gated (Risk R2): Standard_NV36ads_A10_v5 in East US 2 requires
    an approved quota request.
    Defaults to false so `terraform apply` succeeds before the quota lands.
    CPU-mode vLLM keeps the serve lane testable without this flag.
  EOT
  type        = bool
  default     = false
}

variable "managed_postgres" {
  description = <<-EOT
    Provisions a zone-redundant Azure Database for PostgreSQL Flexible Server
    feeding the chart's `postgres.external` values (design §1.3/§2.2) instead
    of the in-cluster postgres subchart. Defaults to false (stage-2, owner
    decision to adopt managed HA postgres).
  EOT
  type        = bool
  default     = false
}

variable "install_velero" {
  description = <<-EOT
    Installs Velero via the helm_release in velero.tf. Defaults to false: the
    helm provider needs the PRIVATE AKS API server to be reachable from the
    Terraform runner (Risk R7), so this is an explicit stage-2 opt-in from an
    ops-runner inside the VNet/VPN, never a default-on dependency of `apply`.
  EOT
  type        = bool
  default     = false
}

variable "postgres_admin_password" {
  description = <<-EOT
    Administrator password for the managed Postgres Flexible Server (only
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

variable "key_vault_id" {
  description = <<-EOT
    Existing Key Vault resource ID to reuse. Empty string (default) means the
    identity module creates a new, private, RBAC-authorized Key Vault.
  EOT
  type        = string
  default     = ""
}

variable "create_entra_broker" {
  description = <<-EOT
    Creates the Entra broker-only app registration for Keycloak, plus (when
    key_vault_id == "") the private Key Vault / private endpoint / RBAC
    grant that exist solely to hold its client secret. Default true is the
    shipped customer posture — byte-identical to pre-NE-LP-AZURE-KV-REACH
    behavior. Set false only for infra-proof smoke lanes that don't
    exercise the broker/realm integration: the client-secret write targets
    a private-endpoint-only vault a GitHub-hosted runner cannot reach.
    .github/workflows/azure-live-apply.yml is the only shipped false-setter.
  EOT
  type        = bool
  default     = true
}

variable "tags" {
  description = "Common resource tags applied across every module."
  type        = map(string)
  default     = {}
}
