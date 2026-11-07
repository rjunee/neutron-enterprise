# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Offline golden-plan guardrails (NE-AWS-12, design D5). Runs with ZERO AWS
# credentials via mock_provider "aws" — command = plan throughout, no cloud
# account required. Each run block turns a merged unit's acceptance proxy
# into an executable plan-time assert.
#
# Split with tests/static_guards.sh: invariants that live in ARGUMENTS
# passed to the pinned upstream modules (the eks module's private-endpoint
# derivation, the network-firewall module's drop_strict/forward_to_sfe
# policy actions, encryption_config's CMK reference, GPU scale-to-zero) are
# NOT addressable from a tftest assert condition — empirically verified on
# Terraform 1.15.8, a run block's `module.<name>` resolves to that module's
# OUTPUTS object only, never its child resources/arguments. Those
# invariants are enforced textually by static_guards.sh instead; everything
# below is a real plan-value assert against first-party module outputs or
# module-targeted runs of this repo's own modules/egress, modules/storage,
# modules/data.

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names    = ["us-east-1a", "us-east-1b", "us-east-1c"]
      zone_ids = ["use1-az1", "use1-az2", "use1-az4"]
    }
  }

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111111111111"
      arn        = "arn:aws:iam::111111111111:root"
      user_id    = "AIDAMOCKMOCKMOCKMOCK"
    }
  }

  mock_data "aws_partition" {
    defaults = {
      partition          = "aws"
      dns_suffix         = "amazonaws.com"
      reverse_dns_prefix = "com.amazonaws"
    }
  }

  mock_data "aws_iam_session_context" {
    defaults = {
      issuer_arn = "arn:aws:iam::111111111111:role/mock-deploy-role"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json          = "{}"
      minified_json = "{}"
    }
  }
}

# NE-AWS-11 added the helm provider (velero.tf). Its provider block configures
# eagerly during a test run — helm tries `aws eks get-token` — and would fail
# offline even though the helm_release is count-gated off at defaults. Mock it
# so `terraform test` stays credential-free (D5); its resources are absent at
# the tested defaults, so a bare mock suffices. (The Azure AD / Entra provider
# left this tree with NE-AWS-AZUREAD — the Entra broker now lives in
# ../terraform-eks-entra-broker with its own tests; this lane no longer
# references that provider at all, so no mock for it is needed here.)
mock_provider "helm" {}

run "hardened_defaults" {
  command = plan

  # (3) 3 NAT gateways, one per AZ
  assert {
    condition     = length(module.vpc.natgw_ids) == 3
    error_message = "expected 3 NAT gateways (one per AZ)"
  }

  assert {
    condition     = length(module.vpc.private_route_table_ids) == 3
    error_message = "expected 3 private route tables"
  }

  # (6-positive) default single frontier lane renders exactly 1 SNI pass rule
  assert {
    condition     = module.egress.sni_pass_rule_count == 1
    error_message = "default frontier_fqdns must render exactly one SNI pass rule"
  }

  # (7-structural) all five CMK data classes exist
  assert {
    condition     = sort(keys(module.kms.key_arns)) == tolist(["ebs", "eks-secrets", "logs", "rds", "s3-backup"])
    error_message = "expected CMKs for all five data classes"
  }

  # VPC endpoint set (NE-AWS-3)
  assert {
    condition = length(setsubtract(
      toset(["s3", "ecr_api", "ecr_dkr", "ec2", "sts", "eks", "logs", "kms", "secretsmanager", "elasticloadbalancing", "autoscaling"]),
      toset(keys(module.vpc_endpoints.endpoints)),
    )) == 0
    error_message = "expected all 11 VPC endpoints"
  }

  # (9-gate) no RDS at defaults
  assert {
    condition     = length(module.data) == 0
    error_message = "managed_postgres must default off"
  }

  # (10-gate) no GPU node group at defaults
  assert {
    condition     = !contains(keys(module.eks.eks_managed_node_groups), "gpu")
    error_message = "gpu_node_pool must default off"
  }

  # (NE-DR1) Velero (incl. the VSL) is opt-in — no helm_release at defaults.
  assert {
    condition     = length(helm_release.velero) == 0
    error_message = "install_velero must default off"
  }
}

run "air_gap_zero_pass_rules" {
  command = plan

  variables {
    frontier_fqdns = []
  }

  # (6) AIR_GAP: empty frontier_fqdns => zero SNI pass rules => no rule group
  assert {
    condition     = module.egress.sni_pass_rule_count == 0
    error_message = "air-gap: sni pass rule count must be 0"
  }

  assert {
    condition     = module.egress.frontier_rule_group_arn == null
    error_message = "air-gap: no frontier rule group may be created"
  }
}

run "gpu_flag_taint_and_gating" {
  command = plan

  variables {
    gpu_node_pool = true
  }

  # (10) GPU node group carries the nvidia taint
  assert {
    condition = anytrue([
      for t in module.eks.eks_managed_node_groups["gpu"].node_group_taints :
      t.key == "nvidia.com/gpu" && t.value == "present" && t.effect == "NO_SCHEDULE"
    ])
    error_message = "gpu node group must carry nvidia.com/gpu=present:NoSchedule"
  }
}

run "managed_postgres_wiring" {
  command = plan

  variables {
    managed_postgres        = true
    postgres_admin_password = join("-", ["mock", "offline", "plan", "only"])
  }

  assert {
    condition     = length(module.data) == 1
    error_message = "managed_postgres=true must create the data module"
  }

  assert {
    condition     = module.data[0].database_name == "neutron"
    error_message = "database must be named neutron (chart parity)"
  }
}

# (2) negative: a /0 public API CIDR must be rejected by validation
run "reject_open_api_cidr" {
  command = plan

  variables {
    api_public_access_cidrs = ["0.0.0.0/0"]
  }

  expect_failures = [var.api_public_access_cidrs]
}

# (6-negative) wildcard frontier FQDN must be rejected
run "reject_wildcard_frontier_fqdn" {
  command = plan

  variables {
    frontier_fqdns = ["*.anthropic.com"]
  }

  expect_failures = [var.frontier_fqdns]
}

run "reject_unknown_egress_mode" {
  command = plan

  variables {
    egress_mode = "open"
  }

  expect_failures = [var.egress_mode]
}

# ---- module-targeted runs (first-party modules) --------------------------

run "egress_firewall_mode_routes" {
  command = plan

  module {
    source = "./modules/egress"
  }

  variables {
    prefix                  = "t"
    mode                    = "firewall"
    vpc_id                  = "vpc-mock"
    azs                     = ["us-east-1a", "us-east-1b", "us-east-1c"]
    firewall_subnet_ids     = ["subnet-fa", "subnet-fb", "subnet-fc"]
    private_route_table_ids = ["rtb-pa", "rtb-pb", "rtb-pc"]
    private_subnet_cidrs    = ["10.30.0.0/20", "10.30.16.0/20", "10.30.32.0/20"]
    public_route_table_ids  = ["rtb-pub"]
    intra_route_table_ids   = ["rtb-ia", "rtb-ib", "rtb-ic"]
    nat_gateway_ids         = ["nat-a", "nat-b", "nat-c"]
    frontier_fqdns          = ["api.anthropic.com"]
    logs_kms_key_arn        = format("arn:aws:kms:us-east-1:111111111111:key/%s", "mock-logs-cmk")
    tags                    = {}
  }

  # (4) every private RT gets a default route via the firewall endpoint
  assert {
    condition     = length(aws_route.private_default_via_firewall) == 3
    error_message = "expected one firewall default route per private route table"
  }

  assert {
    condition = alltrue([
      for r in aws_route.private_default_via_firewall : r.destination_cidr_block == "0.0.0.0/0"
    ])
    error_message = "private firewall routes must be default routes"
  }

  assert {
    condition     = toset([for r in aws_route.private_default_via_firewall : r.route_table_id]) == toset(var.private_route_table_ids)
    error_message = "firewall default routes must cover every private route table"
  }

  assert {
    condition     = length(aws_route.firewall_subnet_to_nat) == 3
    error_message = "each firewall subnet needs a same-AZ NAT default route"
  }

  assert {
    condition     = length(aws_route.public_return_via_firewall) == 3
    error_message = "public return routes must re-enter the firewall"
  }

  assert {
    condition     = aws_cloudwatch_log_group.alert[0].kms_key_id == var.logs_kms_key_arn
    error_message = "alert log group must be CMK-encrypted"
  }
}

run "egress_nat_mode_no_firewall" {
  command = plan

  module {
    source = "./modules/egress"
  }

  variables {
    prefix                  = "t"
    mode                    = "nat"
    vpc_id                  = "vpc-mock"
    azs                     = ["us-east-1a", "us-east-1b", "us-east-1c"]
    firewall_subnet_ids     = ["subnet-fa", "subnet-fb", "subnet-fc"]
    private_route_table_ids = ["rtb-pa", "rtb-pb", "rtb-pc"]
    private_subnet_cidrs    = ["10.30.0.0/20", "10.30.16.0/20", "10.30.32.0/20"]
    public_route_table_ids  = ["rtb-pub"]
    intra_route_table_ids   = ["rtb-ia", "rtb-ib", "rtb-ic"]
    nat_gateway_ids         = ["nat-a", "nat-b", "nat-c"]
    frontier_fqdns          = ["api.anthropic.com"]
    logs_kms_key_arn        = format("arn:aws:kms:us-east-1:111111111111:key/%s", "mock-logs-cmk")
    tags                    = {}
  }

  assert {
    condition     = length(aws_route.private_default_via_firewall) == 0
    error_message = "nat mode must not create firewall routes"
  }

  assert {
    condition     = output.firewall_arn == null
    error_message = "nat mode must not create the firewall"
  }
}

run "storage_module_hardening" {
  command = plan

  module {
    source = "./modules/storage"
  }

  variables {
    prefix             = "t"
    kms_key_arn        = format("arn:aws:kms:us-east-1:111111111111:key/%s", "mock-s3-cmk")
    s3_vpc_endpoint_id = "vpce-mock-s3"
    tags               = {}
  }

  # (8) public access block: all four true
  assert {
    condition = (
      aws_s3_bucket_public_access_block.backup.block_public_acls &&
      aws_s3_bucket_public_access_block.backup.block_public_policy &&
      aws_s3_bucket_public_access_block.backup.ignore_public_acls &&
      aws_s3_bucket_public_access_block.backup.restrict_public_buckets
    )
    error_message = "backup bucket public access block must be all-true"
  }

  # (8) SSE-KMS with the CMK
  assert {
    condition = anytrue([
      for r in aws_s3_bucket_server_side_encryption_configuration.backup.rule :
      anytrue([
        for d in r.apply_server_side_encryption_by_default :
        d.sse_algorithm == "aws:kms" && d.kms_master_key_id == var.kms_key_arn
      ])
    ])
    error_message = "backup bucket must default to SSE-KMS with the s3-backup CMK"
  }

  assert {
    condition = anytrue([
      for r in aws_s3_bucket_versioning.backup.versioning_configuration :
      r.status == "Enabled"
    ])
    error_message = "backup bucket versioning must be enabled"
  }

  assert {
    condition     = length(aws_s3_bucket_object_lock_configuration.backup) == 0
    error_message = "object lock must be off by default (explicit customer decision)"
  }

  assert {
    condition     = aws_s3_bucket.backup.bucket == "t-backup-111111111111"
    error_message = "bucket name must be account-id suffixed"
  }
}

run "data_module_hardening" {
  command = plan

  module {
    source = "./modules/data"
  }

  variables {
    prefix                 = "t"
    private_subnet_ids     = ["subnet-pa", "subnet-pb", "subnet-pc"]
    vpc_id                 = "vpc-mock"
    node_security_group_id = "sg-mock-nodes"
    kms_key_arn            = format("arn:aws:kms:us-east-1:111111111111:key/%s", "mock-rds-cmk")
    admin_password         = join("-", ["mock", "offline", "plan", "only"])
    tags                   = {}
  }

  # (9) RDS: multi-AZ + encrypted + private
  assert {
    condition     = aws_db_instance.this.multi_az
    error_message = "managed postgres must be multi_az"
  }

  assert {
    condition     = aws_db_instance.this.storage_encrypted && aws_db_instance.this.kms_key_id == var.kms_key_arn
    error_message = "managed postgres storage must be CMK-encrypted"
  }

  assert {
    condition     = !aws_db_instance.this.publicly_accessible
    error_message = "managed postgres must never be publicly accessible"
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.postgres_from_nodes.referenced_security_group_id == var.node_security_group_id && aws_vpc_security_group_ingress_rule.postgres_from_nodes.from_port == 5432
    error_message = "postgres ingress must be 5432 from the node SG only"
  }

  assert {
    condition     = aws_db_instance.this.deletion_protection
    error_message = "managed postgres must have deletion protection"
  }

  # (NE-DR1) automated-backup/PITR posture: retention defaults to 7 days,
  # snapshots carry tags, and a destroy still takes a final snapshot.
  assert {
    condition     = aws_db_instance.this.backup_retention_period == 7
    error_message = "RDS automated backup/PITR retention must default to 7 days (NE-DR1)"
  }

  assert {
    condition     = aws_db_instance.this.copy_tags_to_snapshot
    error_message = "RDS must copy tags to snapshots (NE-DR1)"
  }

  assert {
    condition     = !aws_db_instance.this.skip_final_snapshot
    error_message = "RDS must take a final snapshot on destroy (NE-DR1)"
  }
}

# (NE-DR1) install_velero=true plans exactly one velero helm_release. The
# VSL/plugin CONTENT of the values yamlencode is asserted TEXTUALLY by
# static_guards.sh, not here: helm_release.velero[0].values interpolates mocked
# module outputs (bucket_name, kms key arns, region) that are unknown at plan,
# so a tftest condition can only assert the release COUNT (the NE-AWS-12
# outputs-only limitation).
run "velero_install_wired" {
  command = plan

  variables {
    install_velero = true
  }

  assert {
    condition     = length(helm_release.velero) == 1
    error_message = "install_velero=true must plan exactly one velero release (NE-DR1)"
  }
}
