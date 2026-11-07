# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# NE-AWS-3 — VPC endpoints / PrivateLink.
#
# C2: keeps control-plane calls and image pulls on the AWS backbone instead
# of the public internet. Every AWS service the chart/cluster touches gets
# an endpoint: 10 interface endpoints (private DNS enabled, one ENI per AZ
# in the private worker subnets) plus one s3 GATEWAY endpoint (ECR image
# layers are served from S3) attached to the private route tables. The
# live deny-by-default-egress proof that traffic actually rides these
# endpoints is NE-AWS-OWNER step 9 — not this unit.
#
# C11: `vpc-endpoints` is a SUBMODULE of the same
# terraform-aws-modules/terraform-aws-vpc repo already pinned for
# module.vpc in network.tf, consumed at the IDENTICAL commit SHA (v6.6.1)
# — zero new supply-chain surface. No new pin, .terraform.lock.hcl
# unchanged: the submodule's `aws >= 6.28` provider constraint is already
# present in the recorded constraint set.
module "vpc_endpoints" {
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-vpc.git//modules/vpc-endpoints?ref=3ffbd46fb1c7733e1b34d8666893280454e27436" # v6.6.1

  vpc_id = module.vpc.vpc_id

  # Interface endpoints live in the private worker subnets only — one ENI
  # per AZ, never public or intra subnets.
  subnet_ids = module.vpc.private_subnets

  create_security_group      = true
  security_group_name_prefix = "${var.prefix}-vpce-"
  security_group_description = "VPC interface endpoints: allow 443 from inside the VPC only"
  security_group_rules = {
    ingress_https = {
      description = "HTTPS from the VPC CIDR"
      cidr_blocks = [module.vpc.vpc_cidr_block]
    }
  }

  tags = var.tags

  endpoints = {
    # ECR image layers are served from S3; attaching to the private route
    # tables keeps layer pulls on the backbone. Gateway endpoints take no
    # SG and no private_dns_enabled flag — neither is set here.
    s3 = {
      service         = "s3"
      service_type    = "Gateway"
      route_table_ids = module.vpc.private_route_table_ids
      tags            = { Name = "${var.prefix}-s3" }
    }

    # ECR registry auth/metadata.
    ecr_api = {
      service             = "ecr.api"
      private_dns_enabled = true
      tags                = { Name = "${var.prefix}-ecr-api" }
    }

    # ECR docker image layer pulls.
    ecr_dkr = {
      service             = "ecr.dkr"
      private_dns_enabled = true
      tags                = { Name = "${var.prefix}-ecr-dkr" }
    }

    # CNI / node EC2 API calls.
    ec2 = {
      service             = "ec2"
      private_dns_enabled = true
      tags                = { Name = "${var.prefix}-ec2" }
    }

    # IRSA credential vending (NE-AWS-6/7).
    sts = {
      service             = "sts"
      private_dns_enabled = true
      tags                = { Name = "${var.prefix}-sts" }
    }

    # Cluster bootstrap / control-plane API.
    eks = {
      service             = "eks"
      private_dns_enabled = true
      tags                = { Name = "${var.prefix}-eks" }
    }

    # CloudWatch Logs.
    logs = {
      service             = "logs"
      private_dns_enabled = true
      tags                = { Name = "${var.prefix}-logs" }
    }

    # CMK operations (NE-AWS-4/6).
    kms = {
      service             = "kms"
      private_dns_enabled = true
      tags                = { Name = "${var.prefix}-kms" }
    }

    # Broker secret (NE-AWS-7).
    secretsmanager = {
      service             = "secretsmanager"
      private_dns_enabled = true
      tags                = { Name = "${var.prefix}-secretsmanager" }
    }

    # Load balancer provisioning for Services.
    elasticloadbalancing = {
      service             = "elasticloadbalancing"
      private_dns_enabled = true
      tags                = { Name = "${var.prefix}-elasticloadbalancing" }
    }

    # Managed-node-group ASG operations.
    autoscaling = {
      service             = "autoscaling"
      private_dns_enabled = true
      tags                = { Name = "${var.prefix}-autoscaling" }
    }
  }
}
