# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# The customer-managed CMK plane (design deliverable 4, KEY half — the
# Secrets Manager broker-secret half lands with NE-AWS-7); one CMK per data
# class, consumed by NE-AWS-5 (logs), NE-AWS-6 (eks-secrets + ebs), NE-AWS-8
# (s3-backup, ECR), NE-AWS-10 (rds), NE-AWS-11 (s3-backup via Velero); later
# units grant workload roles/services access via the module's
# key_usage_role_arns / key_service_principals inputs — the key policies
# themselves stay account-root-only until then. NE-AWS-5's logs wiring and
# NE-AWS-6's ebs wiring (below) are now in place; eks-secrets (consumed via
# encryption_config.provider_key_arn, no key-policy grant needed — see
# cluster.tf) / rds / s3-backup role grants remain for their respective
# later units.

# Root-level identity data sources (the NE-AWS-9 carry-forward, key-policy
# half): needed to construct the AWSServiceRoleForAutoScaling service-linked
# role ARN below (the var.manage_autoscaling_service_linked_role = false
# fallback path, and the partition/account-id interpolation used by the
# managed resource's own ARN is Terraform-native so no data source is
# needed there). modules/kms already declares its own copies internally
# (for the AccountRootAdmin statement) — these are separate root-module data
# sources, not a cross-module reference.
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# Codex P1 fix (NE-AWS-6, closing the NE-AWS-9 carry-forward properly):
# Terraform itself creates the AWSServiceRoleForAutoScaling service-linked
# role instead of only constructing its ARN and relying on a README
# preflight command. This makes the ordering (role exists BEFORE the `ebs`
# CMK key policy references it) a real Terraform dependency edge, so a
# brand-new AWS account (one that has never launched an Auto Scaling group,
# and so has never had this SLR auto-created by AWS) applies cleanly with
# no manual step. See variables.tf `manage_autoscaling_service_linked_role`
# for the escape hatch covering accounts where the SLR already exists and
# you don't want this stack to adopt it (that toggle is also how you avoid
# an EntityAlreadyExists error on an account that already has the role —
# alternatively `terraform import` it, per that variable's description).
resource "aws_iam_service_linked_role" "autoscaling" {
  count            = var.manage_autoscaling_service_linked_role ? 1 : 0
  aws_service_name = "autoscaling.amazonaws.com"
}

locals {
  # When Terraform manages the SLR (default), depend on the actual created
  # resource's ARN — a real Terraform dependency edge, so `module.kms` (and
  # therefore the `ebs` CMK's key policy) is guaranteed to apply AFTER the
  # role exists. When it doesn't (var.manage_autoscaling_service_linked_role
  # = false, i.e. the role already exists outside this stack's management),
  # fall back to the ARN constructed from data sources — still correct
  # since the role is already present, just not Terraform-managed here.
  autoscaling_slr_arn = var.manage_autoscaling_service_linked_role ? aws_iam_service_linked_role.autoscaling[0].arn : "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
}

module "kms" {
  source = "./modules/kms"

  prefix = var.prefix
  tags   = var.tags

  key_classes = {
    "eks-secrets" = "EKS secrets envelope encryption CMK (NE-AWS-6)"
    "ebs"         = "EBS node/root volume encryption CMK (NE-AWS-6, NE-AWS-9)"
    "rds"         = "RDS Postgres storage encryption CMK (NE-AWS-10)"
    "s3-backup"   = "S3 backup bucket + ECR encryption CMK (NE-AWS-8, NE-AWS-11)"
    "logs"        = "CloudWatch log-group encryption CMK for firewall alert/flow logs (NE-AWS-5)"
  }

  # NE-AWS-5: the firewall's ALERT/FLOW CloudWatch log groups encrypt with
  # the `logs` CMK; CloudWatch Logs needs this service-principal grant (the
  # module scopes it to this AWS account via a kms:CallerAccount condition)
  # or log-group creation fails AccessDenied.
  key_service_principals = {
    logs = ["logs.${var.region}.amazonaws.com"]
  }

  # NE-AWS-6 (the NE-AWS-9 Codex P1 carry-forward, key-policy half): the
  # `ebs` CMK grants the AWSServiceRoleForAutoScaling service-linked role
  # usage + kms:CreateGrant (under the module's kms:GrantIsForAWSResource
  # condition) so EC2/ASG can encrypt node root volumes with this CMK when
  # launching instances for BOTH the default managed node group and the
  # NE-AWS-9 flag-gated GPU group. This is the ONLY way to grant a
  # service-linked role: IAM policies cannot be attached to SLRs, so the
  # grant MUST live in the KMS key policy (not an IAM identity policy).
  #
  # DELIBERATE OMISSION — the EBS CSI IRSA role (cluster.tf,
  # aws_iam_role.ebs_csi) is NOT listed here, even though it also needs
  # `ebs` CMK usage for CSI-provisioned PV encryption. Putting it here would
  # create a hard Terraform dependency cycle: module.kms ->
  # aws_iam_role.ebs_csi -> module.eks (for the OIDC provider ARN/issuer
  # consumed by the role's assume-role trust policy) -> module.kms (for the
  # eks-secrets CMK consumed by encryption_config). Beyond the cycle, KMS
  # key policies validate principal ARNs at PutKeyPolicy time
  # (MalformedPolicyDocument on a not-yet-existing role), and keys are
  # always created before the cluster/IRSA roles that would reference them
  # — so a key-policy reference to aws_iam_role.ebs_csi.arn would fail on
  # 100% of fresh applies. Instead, the EBS CSI role gets a scoped IAM
  # identity policy directly (cluster.tf, aws_iam_role_policy.ebs_csi_kms)
  # granting the identical action set against this same CMK ARN — the
  # AWS-documented pattern for EBS CSI + custom KMS keys
  # (docs.aws.amazon.com/eks/latest/userguide/ebs-csi.html). It works
  # because this key's policy always carries the "AccountRootAdmin"
  # IAM-delegation statement (modules/kms/main.tf) — the standard KMS
  # baseline that lets the account's own IAM policies govern access, no
  # further key-policy statement required for an IAM-policy grant to take
  # effect.
  key_usage_role_arns = {
    ebs = [local.autoscaling_slr_arn]
  }
}
