# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Offline regression trip-wire for NE-LP-AZURE-EGRESS-FOREACH — the defect
# found by the FIRST real Azure apply ever run against this repo (2026-07-24):
# both egress-module for_each expressions used toset(var.aks_subnet_ids) over
# a LIST of same-apply subnet IDs, whose values are unknown at plan time —
# but for_each requires its KEY SET to be statically known, and Terraform
# aborted with "Invalid for_each argument" before either evidence leg ran.
#
# Unlike this directory's other tftest files (module-targeted at
# ./modules/egress with LITERAL subnet-ID variables), those runs are
# structurally BLIND to this bug class: a literal string is always known at
# plan, so a toset() of literals never reproduces the failure. That is
# precisely why the defect went uncaught until a real apply. The first two
# runs below instead plan tests/harness-egress-foreach, which wires
# ../../modules/network -> ../../modules/egress exactly like the root
# main.tf, so the subnet IDs really are computed, same-apply unknowns. THE
# PLAN COMPLETING IS THE ASSERTION for those two runs.
#
# Non-vacuity was verified by hand: with for_each reverted to
# toset(values(var.aks_subnet_ids)) (the semantically-old unknown-SET form),
# both harness runs fail with "Invalid for_each argument" at
# modules/egress/main.tf — see the commit message for the transcript.
#
# The last two runs are module-targeted at ./modules/egress with literal
# variables (safe now that the harness above proves the real-world case) and
# pin the planned instance ADDRESSES to the static system/user/gpu keys, so a
# future re-keying is caught as a state-address change.

mock_provider "azurerm" {}

run "firewall_mode_foreach_survives_unknown_subnet_ids" {
  command = plan

  module {
    source = "./tests/harness-egress-foreach"
  }

  # A run block whose module contains ONLY child modules does not bind mock
  # providers implicitly — this explicit map is REQUIRED (without it:
  # "Invalid provider configuration" / "The argument \"features\" is
  # required").
  providers = {
    azurerm = azurerm
  }

  variables {
    # egress_mode deliberately UNSET — locks the shipped default (firewall).
  }

  assert {
    condition     = module.egress.outbound_type == "userDefinedRouting"
    error_message = "default egress_mode must plan the firewall (userDefinedRouting) leg"
  }
}

run "nat_mode_foreach_survives_unknown_subnet_ids" {
  command = plan

  module {
    source = "./tests/harness-egress-foreach"
  }

  providers = {
    azurerm = azurerm
  }

  variables {
    egress_mode = "nat"
  }

  assert {
    condition     = module.egress.outbound_type == "userAssignedNATGateway"
    error_message = "egress_mode = \"nat\" must plan the NAT (userAssignedNATGateway) leg"
  }
}

run "firewall_association_instances_keyed_statically" {
  command = plan

  module {
    source = "./modules/egress"
  }

  variables {
    prefix              = "t"
    location            = "eastus"
    resource_group_name = "t-rg"
    mode                = "firewall"
    firewall_subnet_id  = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/virtualNetworks/t-vnet/subnets/AzureFirewallSubnet"
    aks_subnet_ids = {
      system = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/virtualNetworks/t-vnet/subnets/snet-aks-system"
      user   = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/virtualNetworks/t-vnet/subnets/snet-aks-user"
      gpu    = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/virtualNetworks/t-vnet/subnets/snet-aks-gpu"
    }
    aks_subnet_cidrs = ["10.20.0.0/22", "10.20.4.0/22", "10.20.8.0/24"]
    frontier_fqdns   = ["api.anthropic.com"]
    tags             = {}
  }

  assert {
    condition     = length(azurerm_subnet_route_table_association.firewall) == 3
    error_message = "firewall mode must associate the route table with exactly the 3 AKS subnets"
  }

  assert {
    condition     = endswith(azurerm_subnet_route_table_association.firewall["system"].subnet_id, "/snet-aks-system")
    error_message = "the \"system\" for_each key must map to the snet-aks-system subnet"
  }

  assert {
    condition     = endswith(azurerm_subnet_route_table_association.firewall["user"].subnet_id, "/snet-aks-user")
    error_message = "the \"user\" for_each key must map to the snet-aks-user subnet"
  }

  assert {
    condition     = endswith(azurerm_subnet_route_table_association.firewall["gpu"].subnet_id, "/snet-aks-gpu")
    error_message = "the \"gpu\" for_each key must map to the snet-aks-gpu subnet"
  }

  assert {
    condition     = length(azurerm_subnet_nat_gateway_association.this) == 0
    error_message = "firewall mode must not plan any NAT gateway associations"
  }
}

run "nat_association_instances_keyed_statically" {
  command = plan

  module {
    source = "./modules/egress"
  }

  variables {
    prefix              = "t"
    location            = "eastus"
    resource_group_name = "t-rg"
    mode                = "nat"
    firewall_subnet_id  = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/virtualNetworks/t-vnet/subnets/AzureFirewallSubnet"
    aks_subnet_ids = {
      system = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/virtualNetworks/t-vnet/subnets/snet-aks-system"
      user   = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/virtualNetworks/t-vnet/subnets/snet-aks-user"
      gpu    = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/virtualNetworks/t-vnet/subnets/snet-aks-gpu"
    }
    aks_subnet_cidrs = ["10.20.0.0/22", "10.20.4.0/22", "10.20.8.0/24"]
    frontier_fqdns   = ["api.anthropic.com"]
    tags             = {}
  }

  assert {
    condition     = length(azurerm_subnet_nat_gateway_association.this) == 3
    error_message = "nat mode must associate the NAT gateway with exactly the 3 AKS subnets"
  }

  assert {
    condition     = endswith(azurerm_subnet_nat_gateway_association.this["system"].subnet_id, "/snet-aks-system")
    error_message = "the \"system\" for_each key must map to the snet-aks-system subnet"
  }

  assert {
    condition     = endswith(azurerm_subnet_nat_gateway_association.this["user"].subnet_id, "/snet-aks-user")
    error_message = "the \"user\" for_each key must map to the snet-aks-user subnet"
  }

  assert {
    condition     = endswith(azurerm_subnet_nat_gateway_association.this["gpu"].subnet_id, "/snet-aks-gpu")
    error_message = "the \"gpu\" for_each key must map to the snet-aks-gpu subnet"
  }

  assert {
    condition     = length(azurerm_subnet_route_table_association.firewall) == 0
    error_message = "nat mode must not plan any route table associations"
  }
}
