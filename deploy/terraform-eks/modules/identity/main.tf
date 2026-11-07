# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Design deliverable 5 identity — the AWS half of the
# deploy/terraform-azure/modules/identity twin: (a) the Velero IRSA role —
# federated-trusted to the cluster's own OIDC provider, least-privilege
# S3 + KMS + EC2-snapshot policy.
#
# The Entra->Keycloak broker (the app registration + service principal +
# password + its Secrets Manager writer) was re-homed to
# ../../terraform-eks-entra-broker (NE-AWS-AZUREAD, 2026-07-20): the Azure AD /
# Entra provider (v3.9.0) eager-auths an Azure authorizer at Configure()
# whenever referenced, which broke this lane's credential-less AWS-only apply
# — see that root's README + the eks-provider-config-smoke CI job. This module
# is now Velero-IRSA only. (This lane must not reference that provider by name;
# the CI grep guard enforces it.)

# --- (A) Velero IRSA role --------------------------------------------------

# Trust policy: mirrors cluster.tf's ebs_csi_assume shape verbatim. The
# StringEquals ":sub" condition value below is BYTE-PARITY with
# azurerm_federated_identity_credential.velero.subject
# (deploy/terraform-azure/modules/identity/main.tf) — the same k8s
# ServiceAccount subject string federates trust on both clouds.
data "aws_iam_policy_document" "velero_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider}:sub"
      values   = ["system:serviceaccount:velero:velero"]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "velero" {
  name               = "${var.prefix}-velero"
  assume_role_policy = data.aws_iam_policy_document.velero_assume.json
  tags               = var.tags
}

# Least-privilege policy — the velero-plugin-for-aws documented minimal
# policy (github.com/vmware-tanzu/velero-plugin-for-aws), scoped to the
# NE-AWS-8 backup bucket and the NE-AWS-4 s3-backup CMK only. NEVER
# Resource "*" for the S3 or KMS statements.
data "aws_iam_policy_document" "velero" {
  # velero-plugin-for-aws documented object-level operations.
  statement {
    sid    = "VeleroObjectOps"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:DeleteObject",
      "s3:PutObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${var.backup_bucket_arn}/*"]
  }

  # velero-plugin-for-aws documented bucket-level operation — separate
  # resource shape from the object-level statement above (bucket ARN, no
  # /* suffix).
  statement {
    sid    = "VeleroBucketList"
    effect = "Allow"

    actions   = ["s3:ListBucket"]
    resources = [var.backup_bucket_arn]
  }

  # Same action set as kms.tf's WorkloadRoleUsage statement, granted here
  # via an IAM identity policy under the s3-backup CMK's AccountRootAdmin
  # IAM-delegation baseline — identical rationale to cluster.tf's
  # ebs_csi_kms (avoids a key-policy edit + a not-yet-existing-principal
  # MalformedPolicyDocument on a fresh apply). GenerateDataKey* is what
  # SSE-KMS PutObject actually calls under the hood.
  statement {
    sid    = "VeleroKmsUsage"
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = [var.s3_backup_kms_key_arn]
  }

  # The upstream velero-plugin-for-aws documented EC2 snapshot set (fs-backup
  # / volume snapshot path). ec2:Describe* actions do not support
  # resource-level scoping (AWS IAM reference), so "*" here is the
  # least-privilege floor for this statement — never relaxed to a wildcard
  # action set beyond what the plugin documents.
  statement {
    sid    = "VeleroSnapshotOps"
    effect = "Allow"

    actions = [
      "ec2:DescribeVolumes",
      "ec2:DescribeSnapshots",
      "ec2:CreateTags",
      "ec2:CreateVolume",
      "ec2:CreateSnapshot",
      "ec2:DeleteSnapshot",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "velero" {
  name   = "${var.prefix}-velero"
  role   = aws_iam_role.velero.id
  policy = data.aws_iam_policy_document.velero.json
}
