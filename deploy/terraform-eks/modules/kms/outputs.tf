# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "key_arns" {
  description = "CMK ARNs keyed by data class."
  value       = { for k, key in aws_kms_key.this : k => key.arn }
}

output "key_ids" {
  description = "CMK key ids keyed by data class."
  value       = { for k, key in aws_kms_key.this : k => key.key_id }
}

output "alias_names" {
  description = "CMK alias names keyed by data class."
  value       = { for k, a in aws_kms_alias.this : k => a.name }
}
