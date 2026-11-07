# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "fqdn" {
  description = "Maps to the chart's postgres.external.hostSecretRef host (README mapping table)."
  value       = aws_db_instance.this.address
}

output "port" {
  value = 5432
}

output "administrator_login" {
  value = aws_db_instance.this.username
}

output "database_name" {
  value = aws_db_instance.this.db_name
}

output "backup_retention_period" {
  description = "Days of automated-backup/PITR retention (NE-DR1 DR posture — see docs/ops/disaster-recovery.md)."
  value       = aws_db_instance.this.backup_retention_period
}
