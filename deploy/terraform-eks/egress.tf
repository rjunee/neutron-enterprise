# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# NE-AWS-5 — the C2 twin of ../terraform-azure's modules/egress: AWS Network
# Firewall, STRICT_ORDER deny-by-default, exact-match tls.sni frontier-lane
# pass rules. See modules/egress/main.tf for the full rationale and
# README.md §3 "Egress perimeter (NE-AWS-5, the C2 twin)" for the narrative.
module "egress" {
  source = "./modules/egress"

  prefix = var.prefix
  mode   = var.egress_mode

  vpc_id = module.vpc.vpc_id
  azs    = local.azs

  firewall_subnet_ids     = module.vpc.intra_subnets
  private_route_table_ids = module.vpc.private_route_table_ids
  private_subnet_cidrs    = module.vpc.private_subnets_cidr_blocks
  public_route_table_ids  = module.vpc.public_route_table_ids
  intra_route_table_ids   = module.vpc.intra_route_table_ids
  nat_gateway_ids         = module.vpc.natgw_ids

  frontier_fqdns = var.frontier_fqdns

  logs_kms_key_arn = module.kms.key_arns["logs"]

  tags = var.tags
}
