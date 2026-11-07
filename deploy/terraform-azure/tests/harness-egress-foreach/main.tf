# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Test harness for egress_foreach.tftest.hcl (NE-LP-AZURE-EGRESS-FOREACH).
# Instantiates ../../modules/network -> ../../modules/egress with the SAME
# wiring shape as the root main.tf, and nothing else. The point: the subnet
# IDs consumed by the egress for_each are computed attributes of resources
# planned in the SAME run — unknown at plan time, exactly the first-real-apply
# condition that produced "Invalid for_each argument". Module-targeted runs
# with literal subnet-ID variables can never reproduce that condition; this
# harness exists so `terraform test` can. If the root main.tf egress wiring
# changes shape, mirror it here.

variable "egress_mode" {
  type    = string
  default = "firewall"
}

module "network" {
  source = "../../modules/network"

  prefix              = "t"
  location            = "eastus"
  resource_group_name = "t-rg"
  managed_postgres    = false
  tags                = {}
}

module "egress" {
  source = "../../modules/egress"

  prefix              = "t"
  location            = "eastus"
  resource_group_name = "t-rg"
  mode                = var.egress_mode
  firewall_subnet_id  = module.network.firewall_subnet_id
  aks_subnet_ids = {
    system = module.network.aks_system_subnet_id
    user   = module.network.aks_user_subnet_id
    gpu    = module.network.aks_gpu_subnet_id
  }
  aks_subnet_cidrs = ["10.20.0.0/22", "10.20.4.0/22", "10.20.8.0/24"]
  frontier_fqdns   = ["api.anthropic.com"]
  tags             = {}
}
