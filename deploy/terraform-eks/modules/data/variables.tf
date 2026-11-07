# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

variable "prefix" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "private_subnet_ids" {
  description = "The 3 private worker subnets (root module.vpc.private_subnets) — the RDS subnet group spans all 3 AZs."
  type        = list(string)
}

variable "vpc_id" {
  type = string
}

variable "node_security_group_id" {
  description = "EKS node security group id (module.eks.node_security_group_id) — the ONLY allowed 5432 ingress source."
  type        = string
}

variable "kms_key_arn" {
  description = "The rds-class CMK ARN (root module.kms.key_arns[\"rds\"], NE-AWS-4)."
  type        = string
}

variable "admin_password" {
  description = "C12: sourced from TF_VAR_postgres_admin_password, never a literal."
  type        = string
  sensitive   = true
}

variable "backup_retention_period" {
  description = "Automated-backup/PITR retention in days (NE-DR1). Default 7 = the established RDS posture; AWS range is 0-35 but <7 is forbidden here (0 disables PITR — never acceptable for the transactional DB, SPEC §11 DR posture)."
  type        = number
  default     = 7
  validation {
    condition     = var.backup_retention_period >= 7 && var.backup_retention_period <= 35
    error_message = "backup_retention_period must be between 7 and 35 days."
  }
}
