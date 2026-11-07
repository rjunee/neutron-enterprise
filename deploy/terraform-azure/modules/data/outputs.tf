# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "fqdn" {
  description = "Maps to the chart's postgres.external.hostSecretRef host (README mapping table)."
  value       = azurerm_postgresql_flexible_server.this.fqdn
}

output "port" {
  value = 5432
}

output "administrator_login" {
  value = azurerm_postgresql_flexible_server.this.administrator_login
}

output "database_name" {
  value = azurerm_postgresql_flexible_server_database.neutron.name
}

output "backup_retention_days" {
  description = "Days of automated-backup/PITR retention (NE-DR1 DR posture — see docs/ops/disaster-recovery.md)."
  value       = azurerm_postgresql_flexible_server.this.backup_retention_days
}

output "geo_redundant_backup_enabled" {
  description = "Whether geo-redundant backup storage is enabled (NE-DR1 — a flag, off by default)."
  value       = azurerm_postgresql_flexible_server.this.geo_redundant_backup_enabled
}
