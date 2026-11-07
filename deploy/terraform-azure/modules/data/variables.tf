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

variable "postgres_subnet_id" {
  description = "The delegated snet-postgres subnet ID (../network output)."
  type        = string
}

variable "private_dns_zone_id" {
  description = "privatelink.postgres.database.azure.com zone ID (../network output)."
  type        = string
}

variable "admin_password" {
  description = "C12: sourced from TF_VAR_postgres_admin_password, never a literal."
  type        = string
  sensitive   = true
}

variable "backup_retention_days" {
  description = "RDS-parity automated-backup retention in days (NE-DR1). Flexible Server PITR is automatic for any retention in 7-35 — restore to any point within the window. Default 7 mirrors the AWS RDS posture."
  type        = number
  default     = 7
  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 35
    error_message = "backup_retention_days must be between 7 and 35 days."
  }
}

variable "geo_redundant_backup_enabled" {
  description = "Geo-redundant (paired-region) backup storage (NE-DR1). Default false is a cost + data-residency decision (parity: the AWS posture is same-region automated backups). CREATE-TIME ONLY — flipping this on an existing server FORCES REPLACEMENT (destroy+create), so decide before the first apply."
  type        = bool
  default     = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
