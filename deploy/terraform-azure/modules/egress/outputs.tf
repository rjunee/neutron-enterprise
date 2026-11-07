# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "firewall_private_ip" {
  description = "Null when mode = \"nat\"."
  value       = var.mode == "firewall" ? azurerm_firewall.this[0].ip_configuration[0].private_ip_address : null
}

output "outbound_type" {
  description = "The AKS network_profile.outbound_type value matching this egress mode."
  value       = var.mode == "firewall" ? "userDefinedRouting" : "userAssignedNATGateway"
}
