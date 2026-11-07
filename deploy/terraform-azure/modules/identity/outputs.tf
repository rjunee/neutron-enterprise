# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "key_vault_id" {
  description = "Null when create_entra_broker=false and no reuse vault (key_vault_id) was supplied — the vault trio is skipped in that case."
  value       = local.key_vault_id
}

output "entra_broker_client_id" {
  description = "The Entra app (client) ID for the Keycloak broker — the secret lives ONLY in Key Vault (entra-broker-client-secret). Null when create_entra_broker=false."
  value       = one(azuread_application.broker[*].client_id)
}

output "entra_tenant_id" {
  value = data.azurerm_client_config.current.tenant_id
}

output "velero_identity_client_id" {
  value = azurerm_user_assigned_identity.velero.client_id
}

output "velero_identity_id" {
  value = azurerm_user_assigned_identity.velero.id
}
