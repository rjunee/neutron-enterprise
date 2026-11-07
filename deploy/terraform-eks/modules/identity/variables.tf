# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

variable "prefix" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "oidc_provider_arn" {
  description = "IRSA OIDC provider ARN (module.eks.oidc_provider_arn) — the Velero role's trust-policy Federated principal."
  type        = string
}

variable "oidc_provider" {
  description = "IRSA OIDC provider issuer HOST only, no https:// prefix (module.eks.oidc_provider) — the form the :sub/:aud condition-key syntax requires."
  type        = string
}

variable "backup_bucket_arn" {
  description = "NE-AWS-8 S3 backup bucket ARN — scopes the Velero S3 policy."
  type        = string
}

variable "s3_backup_kms_key_arn" {
  description = "NE-AWS-4 s3-backup CMK ARN — scopes the Velero KMS policy."
  type        = string
}
