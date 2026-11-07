# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "velero_role_arn" {
  description = "Velero IRSA role ARN — annotate the velero SA: eks.amazonaws.com/role-arn (NE-AWS-11)."
  value       = aws_iam_role.velero.arn
}

output "velero_role_name" {
  value = aws_iam_role.velero.name
}
