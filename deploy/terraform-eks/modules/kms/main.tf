# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# One CMK per data class for the C2/C9-parity promotion — the Key Vault twin
# of deploy/terraform-azure/modules/identity. Key policies are least-privilege:
# account root only at defaults (IAM delegation, the standard KMS admin
# baseline), NO wildcard principals; workload-role/service access is injected
# by later units through the two placeholder variables. C12: KMS generates
# and holds all key material — nothing exportable, nothing in state beyond
# metadata.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_root_arn = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"
}

data "aws_iam_policy_document" "key" {
  for_each = var.key_classes

  statement {
    sid    = "AccountRootAdmin"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = [local.account_root_arn]
    }

    actions   = ["kms:*"]
    resources = ["*"]
  }

  dynamic "statement" {
    for_each = length(lookup(var.key_usage_role_arns, each.key, [])) > 0 ? [1] : []

    content {
      sid    = "WorkloadRoleUsage"
      effect = "Allow"

      principals {
        type        = "AWS"
        identifiers = var.key_usage_role_arns[each.key]
      }

      actions = [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:ReEncrypt*",
        "kms:GenerateDataKey*",
        "kms:DescribeKey",
      ]
      resources = ["*"]
    }
  }

  dynamic "statement" {
    for_each = length(lookup(var.key_usage_role_arns, each.key, [])) > 0 ? [1] : []

    content {
      sid    = "WorkloadRoleAwsResourceGrants"
      effect = "Allow"

      principals {
        type        = "AWS"
        identifiers = var.key_usage_role_arns[each.key]
      }

      actions = [
        "kms:CreateGrant",
        "kms:ListGrants",
        "kms:RevokeGrant",
      ]
      resources = ["*"]

      condition {
        test     = "Bool"
        variable = "kms:GrantIsForAWSResource"
        values   = ["true"]
      }
    }
  }

  dynamic "statement" {
    for_each = length(lookup(var.key_service_principals, each.key, [])) > 0 ? [1] : []

    content {
      sid    = "ServiceUsage"
      effect = "Allow"

      principals {
        type        = "Service"
        identifiers = var.key_service_principals[each.key]
      }

      actions = [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:ReEncrypt*",
        "kms:GenerateDataKey*",
        "kms:DescribeKey",
      ]
      resources = ["*"]

      condition {
        test     = "StringEquals"
        variable = "kms:CallerAccount"
        values   = [data.aws_caller_identity.current.account_id]
      }
    }
  }
}

resource "aws_kms_key" "this" {
  for_each = var.key_classes

  description             = each.value
  enable_key_rotation     = true
  deletion_window_in_days = var.deletion_window_in_days
  policy                  = data.aws_iam_policy_document.key[each.key].json
  tags                    = var.tags
}

resource "aws_kms_alias" "this" {
  for_each = var.key_classes

  name          = "alias/${var.prefix}-${each.key}"
  target_key_id = aws_kms_key.this[each.key].key_id
}
