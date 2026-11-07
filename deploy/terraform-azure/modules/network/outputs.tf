# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "vnet_id" {
  value = azurerm_virtual_network.this.id
}

output "aks_system_subnet_id" {
  value = azurerm_subnet.aks_system.id
}

output "aks_user_subnet_id" {
  value = azurerm_subnet.aks_user.id
}

output "aks_gpu_subnet_id" {
  value = azurerm_subnet.aks_gpu.id
}

output "private_endpoints_subnet_id" {
  value = azurerm_subnet.private_endpoints.id
}

output "firewall_subnet_id" {
  value = azurerm_subnet.firewall.id
}

output "postgres_subnet_id" {
  description = "Empty list when managed_postgres = false."
  value       = azurerm_subnet.postgres[*].id
}

output "acr_private_dns_zone_id" {
  value = azurerm_private_dns_zone.acr.id
}

output "blob_private_dns_zone_id" {
  value = azurerm_private_dns_zone.blob.id
}

output "vault_private_dns_zone_id" {
  value = azurerm_private_dns_zone.vault.id
}

output "postgres_private_dns_zone_id" {
  description = "Empty list when managed_postgres = false."
  value       = azurerm_private_dns_zone.postgres[*].id
}
