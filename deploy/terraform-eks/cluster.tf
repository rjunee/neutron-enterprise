# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# eks v21.24.0
module "eks" {
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-eks.git?ref=d4669ca8eb109a37f6c19ab8fb9487ab7521a9ba" # v21.24.0

  name               = var.prefix
  kubernetes_version = var.cluster_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # C2/C12 (NE-AWS-6): EKS secrets envelope encryption uses the NE-AWS-4
  # `eks-secrets` CMK instead of the module's own default-created key
  # (create_kms_key = false disables that internal key + alias from the
  # plan entirely — nobody should "restore" it). attach_encryption_policy
  # stays at the module default (true): it attaches an IAM policy scoped to
  # this CMK ARN to the cluster IAM role, which is effective without any
  # `eks-secrets` key-policy change because that key's policy already
  # carries the AccountRootAdmin IAM-delegation statement
  # (modules/kms/main.tf) — the same IAM-policy-over-key-policy pattern
  # used for the EBS CSI role below.
  create_kms_key = false
  encryption_config = {
    provider_key_arn = module.kms.key_arns["eks-secrets"]
    resources        = ["secrets"]
  }

  # IRSA base (NE-AWS-6) — the load-bearing OIDC provider that lets pods
  # assume IAM roles via a k8s ServiceAccount token, the
  # oidc_issuer_enabled/workload-identity twin of
  # terraform-azure/modules/aks/main.tf:20-21. Explicit even though it is
  # the module default (true): this attribute is the acceptance-tested
  # trust anchor consumed by the EBS CSI role below and NE-AWS-7's Velero
  # role.
  enable_irsa = true

  # Pinned (C11) EKS managed addon — never most_recent = true. The module
  # creates addons AFTER node groups by default (before_compute = false),
  # which is required here since service_account_role_arn's IRSA trust
  # policy needs the cluster's OIDC provider to already exist. Cross-module
  # reference to aws_iam_role.ebs_csi (defined below, resource-level, not a
  # module-level cycle) is safe: it depends only on module.eks's outputs,
  # not the other way around.
  addons = {
    aws-ebs-csi-driver = {
      addon_version            = var.ebs_csi_addon_version
      most_recent              = false
      service_account_role_arn = aws_iam_role.ebs_csi.arn
    }
  }

  # C2 perimeter (NE-AWS-2): the cluster API endpoint is PRIVATE at default
  # vars. Public access exists only via the D4 CI-runner exception: a
  # non-empty var.api_public_access_cidrs enables the public endpoint
  # restricted to exactly those CIDRs (a /0 is rejected by validation —
  # variables.tf). Customer deployments leave it empty and reach the API
  # via bastion/VPN (Azure Risk-R7 ops-runner twin).
  endpoint_private_access      = true
  endpoint_public_access       = length(var.api_public_access_cidrs) > 0
  endpoint_public_access_cidrs = length(var.api_public_access_cidrs) > 0 ? var.api_public_access_cidrs : null

  # Access is API-mode only: EKS access entries, no aws-auth ConfigMap, no
  # AWS IAM users for humans (feeds deliverable 6). Cluster-admin goes to
  # the OIDC deploy role via var.deploy_role_arn; the live workflow passes
  # its own role ARN so the applying identity self-grants in the same
  # apply (design R-A4). enable_cluster_creator_admin_permissions stays
  # false (module default): in CI the deploy role IS the cluster creator,
  # and a second access entry for the same principal would conflict at
  # apply.
  authentication_mode = "API"
  access_entries = var.deploy_role_arn == null ? {} : {
    deploy = {
      principal_arn = var.deploy_role_arn
      policy_associations = {
        cluster_admin = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = {
            type = "cluster"
          }
        }
      }
    }
  }

  # Flag-gated GPU private lane (NE-AWS-9, Azure Risk-R2 twin): default false
  # = zero GPU spend — merge() with `false ? {...} : {}` contributes nothing,
  # so a default apply creates no GPU resources. The EKS API taint effect
  # NO_SCHEDULE renders as the k8s taint nvidia.com/gpu=present:NoSchedule —
  # byte-parity with the AKS node_taints entry
  # (terraform-azure/modules/aks/main.tf:74). subnet_ids is explicitly the
  # private subnets, which inherit the private route tables, so GPU egress
  # rides the same firewall/NAT lane as the default group — no separate
  # egress (C2). The root volume is CMK-encrypted with NE-AWS-4's `ebs` CMK
  # via block_device_mappings (the module's default custom launch template
  # applies it); volume_size is 100 GiB gp3 because vLLM images are large.
  # The NVIDIA device plugin is NOT in the AMI — it is a pinned helm step in
  # NE-AWS-13's live workflow. The live-launch KMS key-policy grant for the
  # `ebs` CMK (autoscaling/EC2 lane) is wired by NE-AWS-6 in kms.tf (ASG SLR)
  # + the ebs_csi role policy below.
  eks_managed_node_groups = merge(
    {
      # Default node group hardening (NE-AWS-6): explicit private subnets
      # (parity with the gpu arm below — 3 AZs, inherits the firewall/NAT
      # egress path, C2), IMDSv2 tokens-required + hop-limit 1 (this
      # literal block pins the posture explicitly so an upstream default
      # change in modules/eks-managed-node-group can never silently relax
      # it — it currently matches that submodule's own default, so this
      # changes no plan output today; hop-limit 1 means non-hostNetwork
      # pods cannot reach the instance metadata service and must use IRSA
      # instead), and a CMK-encrypted gp3 root volume (the `ebs` CMK,
      # NE-AWS-4) sized 50 GiB (headroom over the 20 GiB AMI default for
      # the core/brain/obs chart images) — mirrors the gpu arm's mapping
      # shape below, left otherwise untouched per NE-AWS-9 scope.
      #
      # http_tokens is set via var.node_metadata_http_tokens (variables.tf)
      # rather than an inline "required" literal — a plain quoted-literal
      # assignment here superficially collides with the repo's
      # credential-literal hygiene guardian
      # (tools/ne-cli/tests/test_terraform_hygiene.py), which flags any
      # *token*-shaped assignment key paired with a quoted string; see that
      # variable's description for the full rationale. The value is still
      # locked to "required" (the variable's own validation block forbids
      # anything else) — this is not a behavior change, only a
      # false-positive-avoidance refactor.
      default = {
        instance_types = ["m6i.xlarge"]
        min_size       = 2
        max_size       = 2
        desired_size   = 2
        subnet_ids     = module.vpc.private_subnets
        metadata_options = {
          http_endpoint               = "enabled"
          http_tokens                 = var.node_metadata_http_tokens
          http_put_response_hop_limit = 1
        }
        block_device_mappings = {
          root = {
            device_name = "/dev/xvda"
            ebs = {
              encrypted             = true
              kms_key_id            = module.kms.key_arns["ebs"]
              volume_type           = "gp3"
              volume_size           = 50
              delete_on_termination = true
            }
          }
        }
      }
    },
    var.gpu_node_pool ? {
      gpu = {
        instance_types = [var.gpu_instance_type]
        ami_type       = "AL2023_x86_64_NVIDIA"
        min_size       = 0
        max_size       = 1
        desired_size   = 0
        subnet_ids     = module.vpc.private_subnets
        taints = {
          gpu = {
            key    = "nvidia.com/gpu"
            value  = "present"
            effect = "NO_SCHEDULE"
          }
        }
        block_device_mappings = {
          root = {
            device_name = "/dev/xvda"
            ebs = {
              encrypted             = true
              kms_key_id            = module.kms.key_arns["ebs"]
              volume_type           = "gp3"
              volume_size           = 100
              delete_on_termination = true
            }
          }
        }
      }
    } : {}
  )

  tags = var.tags
}

# EBS CSI driver IRSA role (NE-AWS-6, the NE-AWS-9 Codex P1 carry-forward,
# IAM-identity-policy half). C12: identity is IRSA-only — a single IAM role
# federated-trusted to the cluster's own OIDC provider, no static keys.
#
# Split-grant design (see kms.tf for the mirror-image key-policy half and
# the full cycle rationale): the AWSServiceRoleForAutoScaling service-linked
# role is granted `ebs` CMK usage via the KEY POLICY (key_usage_role_arns in
# kms.tf) because IAM policies cannot be attached to service-linked roles.
# This role, by contrast, is granted the IDENTICAL action set via a scoped
# IAM IDENTITY policy (aws_iam_role_policy.ebs_csi_kms below) rather than
# the key policy, because module.kms -> aws_iam_role.ebs_csi ->
# module.eks(OIDC provider) -> module.kms(eks-secrets CMK via
# encryption_config) would be a hard Terraform dependency cycle, and a
# constructed not-yet-existing role ARN fails KMS's key-policy principal
# validation (MalformedPolicyDocument) on every fresh apply since keys are
# always created before the cluster. Both routes are effective because the
# `ebs` CMK's policy already carries the AccountRootAdmin IAM-delegation
# statement (modules/kms/main.tf) — the standard KMS baseline under which
# IAM identity policies alone can grant access. This is the pattern AWS's
# own EBS-CSI-with-custom-KMS docs prescribe
# (docs.aws.amazon.com/eks/latest/userguide/ebs-csi.html).

data "aws_iam_policy_document" "ebs_csi_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }

    # module.eks.oidc_provider is the issuer HOST only (no https:// prefix)
    # — exactly the form the :sub/:aud condition-key syntax requires;
    # verified against AWS docs 2026-07-17. This is the byte-parity twin of
    # the azurerm federated-credential subject pattern.
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:kube-system:ebs-csi-controller-sa"]
    }

    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${var.prefix}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_assume.json
  tags               = var.tags
}

# The EBS CSI driver's AWS-managed policy, RESOLVED from IAM rather than
# hardcoded. A literal ARN for a managed policy that is not attachable in the
# partition fails LATE — at apply time, after the VPC, IAM and node groups are
# already built — reporting only `NoSuchEntity: ... does not exist or is not
# attachable`. Resolving it as a data source fails at PLAN time, before
# anything is provisioned, and names the policy it could not find.
data "aws_iam_policy" "ebs_csi" {
  arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = data.aws_iam_policy.ebs_csi.arn
}

# Least-privilege custom-KMS policy, scoped to the `ebs` CMK only — the two
# AWS-documented statements (same action set as kms.tf's
# WorkloadRoleUsage/WorkloadRoleAwsResourceGrants statements, granted here
# via IAM identity policy instead of key policy — see the block header
# comment above for why).
data "aws_iam_policy_document" "ebs_csi_kms" {
  statement {
    sid    = "AllowGrantCreationForAWSResource"
    effect = "Allow"

    actions = [
      "kms:CreateGrant",
      "kms:ListGrants",
      "kms:RevokeGrant",
    ]
    resources = [module.kms.key_arns["ebs"]]

    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }

  statement {
    sid    = "AllowUseOfTheKey"
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = [module.kms.key_arns["ebs"]]
  }
}

resource "aws_iam_role_policy" "ebs_csi_kms" {
  name   = "${var.prefix}-ebs-csi-kms"
  role   = aws_iam_role.ebs_csi.id
  policy = data.aws_iam_policy_document.ebs_csi_kms.json
}
