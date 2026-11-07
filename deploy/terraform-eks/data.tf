# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Managed-data variant (var.managed_postgres = true) — deliverable 8's
# managed half, the RDS multi-AZ twin of Azure's Flexible Server
# ZoneRedundant (NE-AWS-10);
# default-off = zero DB cost.

module "data" {
  count  = var.managed_postgres ? 1 : 0
  source = "./modules/data"

  prefix                 = var.prefix
  tags                   = var.tags
  private_subnet_ids     = module.vpc.private_subnets
  vpc_id                 = module.vpc.vpc_id
  node_security_group_id = module.eks.node_security_group_id
  kms_key_arn            = module.kms.key_arns["rds"]
  admin_password         = var.postgres_admin_password
}
