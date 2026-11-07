# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "acr_login_server" {
  description = "The `ne release push --registry` target."
  value       = module.registry.login_server
}

output "aks_name" {
  value = module.aks.name
}

output "resource_group_name" {
  description = "The main resource group. azure-live-apply.yml's evidence legs (az rest / az aks show) and the orphan-cleanup runbook key off this."
  value       = azurerm_resource_group.this.name
}

output "aks_oidc_issuer_url" {
  value = module.aks.oidc_issuer_url
}

output "key_vault_id" {
  value = module.identity.key_vault_id
}

output "entra_broker_client_id" {
  value = module.identity.entra_broker_client_id
}

output "entra_tenant_id" {
  value = module.identity.entra_tenant_id
}

output "velero_identity_client_id" {
  value = module.identity.velero_identity_client_id
}

output "backup_storage_account_name" {
  value = module.storage.account_name
}

output "backup_storage_container_name" {
  value = module.storage.container_name
}

output "worm_container_name" {
  description = "The VLW-enabled WORM container AzureBlobObjectStore/`ne audit checkpoint` archives into (C10). Feeds the ne-storage azure settings.container."
  value       = module.storage.worm_container_name
}

output "postgres_fqdn" {
  description = "Null when managed_postgres = false. Feeds the chart's postgres.external mapping (README)."
  value       = var.managed_postgres ? module.data[0].fqdn : null
}

output "postgres_database_name" {
  value = var.managed_postgres ? module.data[0].database_name : null
}

output "firewall_private_ip" {
  description = "Null in \"nat\" egress_mode."
  value       = module.egress.firewall_private_ip
}

# --- Private-Endpoint DNS verification checklist (Risk R3) ---------------
# `terraform output pe_dns_checklist` prints the exact zones to `dig` from a
# host inside the VNet/VPN (README carries the full runbook step).
output "pe_dns_checklist" {
  value = concat(
    [
      "privatelink.azurecr.io",
      "privatelink.blob.core.windows.net",
      "privatelink.vaultcore.azure.net",
    ],
    var.managed_postgres ? ["privatelink.postgres.database.azure.com"] : []
  )
}
