# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# The C2 perimeter: NSGs (../network) bound "which pods may egress" is
# L4-coarse; THIS module is where "where to" gets FQDN-exact, via Azure
# Firewall's application-rule allowlist. The "nat" mode is a documented,
# never-silent degraded variant (loses FQDN precision — README Risk R4).

# --- firewall mode --------------------------------------------------------

resource "azurerm_public_ip" "firewall" {
  count               = var.mode == "firewall" ? 1 : 0
  name                = "${var.prefix}-fw-pip"
  location            = var.location
  resource_group_name = var.resource_group_name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = var.tags
}

resource "azurerm_firewall_policy" "this" {
  count               = var.mode == "firewall" ? 1 : 0
  name                = "${var.prefix}-fw-policy"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "Standard"
  tags                = var.tags
}

resource "azurerm_firewall" "this" {
  count               = var.mode == "firewall" ? 1 : 0
  name                = "${var.prefix}-fw"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku_name            = "AZFW_VNet"
  sku_tier            = "Standard" # SNI-based FQDN app rules need no TLS inspection
  firewall_policy_id  = azurerm_firewall_policy.this[0].id
  tags                = var.tags

  ip_configuration {
    name                 = "fw-ipconfig"
    subnet_id            = var.firewall_subnet_id
    public_ip_address_id = azurerm_public_ip.firewall[0].id
  }
}

resource "azurerm_firewall_policy_rule_collection_group" "this" {
  count              = var.mode == "firewall" ? 1 : 0
  name               = "${var.prefix}-fw-rules"
  firewall_policy_id = azurerm_firewall_policy.this[0].id
  priority           = 200

  # THE C2 lane: HTTPS to exactly var.frontier_fqdns from the AKS subnets.
  # Empty frontier_fqdns => no rules in this collection => air-gap.
  dynamic "application_rule_collection" {
    for_each = length(var.frontier_fqdns) > 0 ? [1] : []
    content {
      name     = "frontier-lane"
      priority = 300
      action   = "Allow"

      rule {
        name              = "frontier-fqdns"
        source_addresses  = var.aks_subnet_cidrs
        destination_fqdns = var.frontier_fqdns

        protocols {
          type = "Https"
          port = 443
        }
      }
    }
  }

  # AKS-required infrastructure (control plane, image pulls, OS updates) —
  # see https://learn.microsoft.com/azure/aks/outbound-rules-control-egress
  # and https://learn.microsoft.com/azure/aks/limit-egress-traffic for the
  # exact FQDN list this pins.
  application_rule_collection {
    name     = "aks-required-infra"
    priority = 310
    action   = "Allow"

    rule {
      name             = "aks-control-plane-and-images"
      source_addresses = var.aks_subnet_cidrs
      destination_fqdns = [
        "*.hcp.${var.location}.azmk8s.io",
        "mcr.microsoft.com",
        "*.data.mcr.microsoft.com",
        "management.azure.com",
        "login.microsoftonline.com",
        "packages.microsoft.com",
        "acs-mirror.azureedge.net",
        "packages.aks.azure.com",
      ]

      protocols {
        type = "Https"
        port = 443
      }
    }
  }

  network_rule_collection {
    name     = "ntp"
    priority = 320
    action   = "Allow"

    rule {
      name                  = "ntp"
      source_addresses      = var.aks_subnet_cidrs
      destination_addresses = ["*"]
      destination_ports     = ["123"]
      protocols             = ["UDP"]
    }
  }
}

resource "azurerm_route_table" "firewall" {
  count               = var.mode == "firewall" ? 1 : 0
  name                = "${var.prefix}-rt-firewall"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags

  route {
    name                   = "default-via-firewall"
    address_prefix         = "0.0.0.0/0"
    next_hop_type          = "VirtualAppliance"
    next_hop_in_ip_address = azurerm_firewall.this[0].ip_configuration[0].private_ip_address
  }
}

resource "azurerm_subnet_route_table_association" "firewall" {
  for_each       = var.mode == "firewall" ? var.aks_subnet_ids : {}
  subnet_id      = each.value
  route_table_id = azurerm_route_table.firewall[0].id
}

# --- nat mode (minimal-cost variant — FQDN precision lost, README R4) ----

resource "azurerm_public_ip" "nat" {
  count               = var.mode == "nat" ? 1 : 0
  name                = "${var.prefix}-nat-pip"
  location            = var.location
  resource_group_name = var.resource_group_name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = var.tags
}

resource "azurerm_nat_gateway" "this" {
  count               = var.mode == "nat" ? 1 : 0
  name                = "${var.prefix}-natgw"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku_name            = "Standard"
  tags                = var.tags
}

resource "azurerm_nat_gateway_public_ip_association" "this" {
  count                = var.mode == "nat" ? 1 : 0
  nat_gateway_id       = azurerm_nat_gateway.this[0].id
  public_ip_address_id = azurerm_public_ip.nat[0].id
}

resource "azurerm_subnet_nat_gateway_association" "this" {
  for_each       = var.mode == "nat" ? var.aks_subnet_ids : {}
  subnet_id      = each.value
  nat_gateway_id = azurerm_nat_gateway.this[0].id
}
