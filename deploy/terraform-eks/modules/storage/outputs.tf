# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "bucket_name" {
  description = "Backup bucket name."
  value       = aws_s3_bucket.backup.id
}

output "bucket_arn" {
  description = "Backup bucket ARN."
  value       = aws_s3_bucket.backup.arn
}
