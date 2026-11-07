# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# VNet + subnets + deny-by-default NSGs + private DNS zones. The Azure
# Firewall / route-table wiring lives in ../egress (it needs this module's
# subnet outputs; keeping the route table there avoids a dependency cycle).

resource "azurerm_virtual_network" "this" {
  name                = "${var.prefix}-vnet"
  location            = var.location
  resource_group_name = var.resource_group_name
  address_space       = ["10.20.0.0/16"]
  tags                = var.tags
}

resource "azurerm_subnet" "aks_system" {
  name                 = "snet-aks-system"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = ["10.20.0.0/22"]
}

resource "azurerm_subnet" "aks_user" {
  name                 = "snet-aks-user"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = ["10.20.4.0/22"]
}

resource "azurerm_subnet" "aks_gpu" {
  name                 = "snet-aks-gpu"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = ["10.20.8.0/24"]
}

resource "azurerm_subnet" "private_endpoints" {
  name                              = "snet-private-endpoints"
  resource_group_name               = var.resource_group_name
  virtual_network_name              = azurerm_virtual_network.this.name
  address_prefixes                  = ["10.20.9.0/24"]
  private_endpoint_network_policies = "Disabled"
}

# AzureFirewallSubnet is a literal, Azure-mandated subnet name — the platform
# looks it up by name when the Azure Firewall is deployed into this VNet.
# Azure does not permit an NSG on this subnet.
resource "azurerm_subnet" "firewall" {
  name                 = "AzureFirewallSubnet"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = ["10.20.10.0/26"]
}

# Reserved for the managed-postgres variant (var.managed_postgres = true):
# PostgreSQL Flexible Server's "Private access" networking mode is VNet
# integration via a DELEGATED subnet + a linked private DNS zone — NOT a
# NIC-based Private Endpoint (that is why this subnet is delegated rather
# than sitting alongside the other PE-consuming resources in
# snet-private-endpoints).
resource "azurerm_subnet" "postgres" {
  count                = var.managed_postgres ? 1 : 0
  name                 = "snet-postgres"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = ["10.20.11.0/24"]

  delegation {
    name = "postgres-flexible-server"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

# --- AKS subnet NSGs (C2) ------------------------------------------------
#
# Every non-firewall AKS subnet gets: allow intra-VNet, allow the narrow set
# of AKS-required outbound endpoints (service tag + NTP + DNS — see
# https://learn.microsoft.com/azure/aks/outbound-rules-control-egress), and
# an explicit ALLOW for the remaining Internet-bound traffic (priority 4096).
#
# That last rule is deliberately Allow, not Deny, for a specific reason: NSGs
# evaluate a rule's destination against a packet's real, final destination —
# never against the UDR-selected next hop
# (https://learn.microsoft.com/en-us/answers/questions/2283453/firewall-and-workload-subnet-nsg).
# Both egress modes (../egress) route 0.0.0.0/0 to a next hop (the Azure
# Firewall's private IP, or the NAT Gateway) via a route table, but the
# packet's destination address is still classified "Internet" the whole
# time. A hard "deny Internet" NSG rule here would therefore ALSO block the
# firewall-routed frontier lane and AKS-required-infra application rules —
# silently defeating C2 (dropped before it ever reaches the firewall)
# instead of enforcing it. The actual deny-by-default, FQDN-exact
# enforcement lives entirely at the Azure Firewall's application-rule
# allowlist (../egress, var.frontier_fqdns) — an empty allowlist there means
# zero Internet application rules pass, i.e. air-gap, regardless of this
# NSG. NSG here is intentionally L4-coarse (README §4, Risk R4): it fences
# off only traffic with no legitimate route at all — see the
# private-endpoints NSG below, which is never associated with a
# firewall/NAT route table and correctly keeps a hard deny.
locals {
  aks_nsg_rules = [
    {
      name                       = "AllowVnetOutbound"
      priority                   = 100
      access                     = "Allow"
      protocol                   = "*"
      destination_port_range     = "*"
      destination_address_prefix = "VirtualNetwork"
    },
    {
      name                       = "AllowAzureCloudHttps"
      priority                   = 110
      access                     = "Allow"
      protocol                   = "Tcp"
      destination_port_range     = "443"
      destination_address_prefix = "AzureCloud.${var.location}"
    },
    {
      name                       = "AllowAzureCloud9000"
      priority                   = 111
      access                     = "Allow"
      protocol                   = "Tcp"
      destination_port_range     = "9000"
      destination_address_prefix = "AzureCloud.${var.location}"
    },
    {
      name                       = "AllowNtpOutbound"
      priority                   = 120
      access                     = "Allow"
      protocol                   = "Udp"
      destination_port_range     = "123"
      destination_address_prefix = "Internet"
    },
    {
      name                       = "AllowAzureDnsOutbound"
      priority                   = 130
      access                     = "Allow"
      protocol                   = "*"
      destination_port_range     = "53"
      destination_address_prefix = "168.63.129.16"
    },
    {
      name                       = "AllowInternetOutbound"
      priority                   = 4096
      access                     = "Allow"
      protocol                   = "*"
      destination_port_range     = "*"
      destination_address_prefix = "Internet"
    },
  ]
}

resource "azurerm_network_security_group" "aks" {
  name                = "${var.prefix}-nsg-aks"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags

  dynamic "security_rule" {
    for_each = local.aks_nsg_rules
    content {
      name                       = security_rule.value.name
      priority                   = security_rule.value.priority
      direction                  = "Outbound"
      access                     = security_rule.value.access
      protocol                   = security_rule.value.protocol
      source_port_range          = "*"
      destination_port_range     = security_rule.value.destination_port_range
      source_address_prefix      = "*"
      destination_address_prefix = security_rule.value.destination_address_prefix
    }
  }
}

resource "azurerm_subnet_network_security_group_association" "aks_system" {
  subnet_id                 = azurerm_subnet.aks_system.id
  network_security_group_id = azurerm_network_security_group.aks.id
}

resource "azurerm_subnet_network_security_group_association" "aks_user" {
  subnet_id                 = azurerm_subnet.aks_user.id
  network_security_group_id = azurerm_network_security_group.aks.id
}

resource "azurerm_subnet_network_security_group_association" "aks_gpu" {
  subnet_id                 = azurerm_subnet.aks_gpu.id
  network_security_group_id = azurerm_network_security_group.aks.id
}

# Private-endpoint NICs need no outbound Internet at all — deny-all is enough
# (their traffic is inbound-only via the private link service). Unlike the
# aks NSG above, this subnet is never associated with the firewall/NAT route
# table, so a hard deny here has no UDR-routed traffic to (incorrectly) also
# block — it is a genuine, safe deny-by-default.
resource "azurerm_network_security_group" "private_endpoints" {
  name                = "${var.prefix}-nsg-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags

  security_rule {
    name                       = "AllowVnetOutbound"
    priority                   = 100
    direction                  = "Outbound"
    access                     = "Allow"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "VirtualNetwork"
  }

  security_rule {
    name                       = "DenyInternetOutbound"
    priority                   = 4096
    direction                  = "Outbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "Internet"
  }
}

resource "azurerm_subnet_network_security_group_association" "private_endpoints" {
  subnet_id                 = azurerm_subnet.private_endpoints.id
  network_security_group_id = azurerm_network_security_group.private_endpoints.id
}

# --- Private DNS zones (created + VNet-linked here) ---------------------
#
# AKS's own API-server zone (privatelink.<region>.azmk8s.io) is system-managed
# by the platform when private_cluster_enabled = true — no zone/link needed
# here. The README documents the BYO-zone hub-spoke variant for customers
# peering multiple VNets to the same cluster.

resource "azurerm_private_dns_zone" "acr" {
  name                = "privatelink.azurecr.io"
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone" "blob" {
  name                = "privatelink.blob.core.windows.net"
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone" "vault" {
  name                = "privatelink.vaultcore.azure.net"
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone" "postgres" {
  count               = var.managed_postgres ? 1 : 0
  name                = "privatelink.postgres.database.azure.com"
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "acr" {
  name                  = "${var.prefix}-acr-link"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.acr.name
  virtual_network_id    = azurerm_virtual_network.this.id
  tags                  = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "blob" {
  name                  = "${var.prefix}-blob-link"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.blob.name
  virtual_network_id    = azurerm_virtual_network.this.id
  tags                  = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "vault" {
  name                  = "${var.prefix}-vault-link"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.vault.name
  virtual_network_id    = azurerm_virtual_network.this.id
  tags                  = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  count                 = var.managed_postgres ? 1 : 0
  name                  = "${var.prefix}-postgres-link"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.postgres[0].name
  virtual_network_id    = azurerm_virtual_network.this.id
  tags                  = var.tags
}
