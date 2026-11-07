# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

variable "prefix" {
  type = string
}

variable "location" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "key_vault_id" {
  description = "Existing Key Vault ID to reuse; empty string creates a new one."
  type        = string
  default     = ""
}

variable "private_endpoints_subnet_id" {
  type = string
}

variable "private_dns_zone_id" {
  description = "privatelink.vaultcore.azure.net zone ID (../network output). Only consumed when this module creates the Key Vault."
  type        = string
}

variable "hostname" {
  description = "The console/API vhost — derives the DEFAULT Entra broker redirect URIs (both tier forms — see broker_redirect_uris) when broker_redirect_uris is left at its empty default."
  type        = string
}

variable "broker_redirect_uris" {
  description = <<-EOT
    Redirect URIs registered on the Entra broker app. Empty default (the
    normal case) DERIVES both shipped tiers' actual Keycloak external broker
    URL from var.hostname: the prod/Helm ingress host-split form
    (https://auth.<hostname>/realms/ne-pilot/broker/entra/endpoint) AND the
    pilot/compose port-split form (https://<hostname>:8443/realms/ne-pilot/broker/entra/endpoint
    — matches deploy/compose/pilot/compose.yaml's KC_HOSTNAME, which is
    https://<NE_PILOT_HOSTNAME>:8443). Entra requires the registered redirect
    URI to EXACTLY match the actual callback endpoint, so both tier forms are
    registered by default rather than picking one. Set explicitly to override
    (used verbatim, not merged with the default) — e.g. for a custom vhost
    split not covered by the two shipped tiers.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for u in var.broker_redirect_uris :
      can(regex("^https://.+/realms/ne-pilot/broker/entra/endpoint$", u))
    ])
    error_message = "every broker_redirect_uris entry must be https and end with /realms/ne-pilot/broker/entra/endpoint — that path is Keycloak's broker callback for the identityProvider alias \"entra\"."
  }
}

variable "oidc_issuer_url" {
  description = "AKS OIDC issuer URL (../aks output) — the federated credential issuer for Velero workload identity."
  type        = string
}

variable "storage_account_id" {
  description = "Backup storage account ID (../storage output) — Velero identity gets Storage Blob Data Contributor here."
  type        = string
}

variable "create_entra_broker" {
  description = <<-EOT
    Creates the Entra broker-only app registration + service principal +
    client secret + the Key Vault secret write, AND (when key_vault_id ==
    "") the private Key Vault / private endpoint / Secrets Officer RBAC
    grant that exist SOLELY to host that secret. Default true is the
    shipped customer posture; set false ONLY for infra-proof smoke lanes
    (.github/workflows/azure-live-apply.yml) that don't exercise the
    broker/realm integration, because the client-secret write targets a
    private-endpoint-only vault (public_network_access_enabled=false) that
    a GitHub-hosted runner has no VNet route or private-DNS resolution to
    (NE-LP-AZURE-KV-REACH; resolves PR #311 Codex C1). Nothing else in this
    root consumes the broker — realm wiring is NE-103b's identityProviders
    artifact.
  EOT
  type        = bool
  default     = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
