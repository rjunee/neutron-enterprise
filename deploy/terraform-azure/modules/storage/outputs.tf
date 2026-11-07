# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "account_name" {
  value = azurerm_storage_account.backup.name
}

output "account_id" {
  value = azurerm_storage_account.backup.id
}

output "container_name" {
  value = azurerm_storage_container.velero.name
}

output "worm_container_name" {
  value = azapi_resource.worm.name
}
