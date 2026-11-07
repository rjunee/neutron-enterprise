# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 3)
}

# vpc v6.6.1
module "vpc" {
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-vpc.git?ref=3ffbd46fb1c7733e1b34d8666893280454e27436" # v6.6.1

  name = "${var.prefix}-vpc"
  cidr = "10.30.0.0/16"

  azs = local.azs

  # Worker nodes (incl. future GPU) — /20 x3, one per AZ.
  private_subnets = ["10.30.0.0/20", "10.30.16.0/20", "10.30.32.0/20"]

  # NAT gateways ONLY — /24 x3. Re-addressed from the old /20s: safe, this
  # tree has never been live-applied, no state exists.
  public_subnets = ["10.30.128.0/24", "10.30.129.0/24", "10.30.130.0/24"]

  # Reserved for AWS Network Firewall endpoints — the AzureFirewallSubnet
  # twin, consumed by NE-AWS-5. The vpc module gives intra subnets NO
  # internet route by construction.
  intra_subnets = ["10.30.255.0/28", "10.30.255.16/28", "10.30.255.32/28"]

  # No auto-assigned public IPs anywhere; workers live only in private
  # subnets.
  map_public_ip_on_launch = false

  enable_nat_gateway = true
  single_nat_gateway = false
  # 3-AZ HA egress, one NAT per AZ — an AZ loss cannot black-hole another
  # AZ's egress; per-AZ NAT cost is the D8-documented price of HA.
  one_nat_gateway_per_az = true

  # NE-AWS-5: in firewall mode the egress module owns the private-RT default
  # route (0.0.0.0/0 -> same-AZ firewall endpoint); the vpc module's own
  # managed NAT default route would collide (RouteAlreadyExists). In nat mode
  # this keeps today's degraded smoke posture unchanged.
  create_private_nat_gateway_route = var.egress_mode == "nat"

  # NE-AWS-5: one route table PER intra (firewall-endpoint) subnet so each
  # AZ's firewall egresses via the SAME-AZ NAT (an AZ loss cannot black-hole
  # another AZ, matching the per-AZ NAT decision above). In nat mode these
  # route tables are created but stay empty/unused — harmless.
  create_multiple_intra_route_tables = true

  tags = var.tags
}
