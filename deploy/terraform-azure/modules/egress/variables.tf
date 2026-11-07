# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

variable "prefix" {
  type = string
}

variable "location" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "mode" {
  description = "\"firewall\" (default, FQDN-exact C2 lane) or \"nat\" (FQDN precision lost — README Risk R4)."
  type        = string

  validation {
    condition     = contains(["firewall", "nat"], var.mode)
    error_message = "mode must be either \"firewall\" or \"nat\"."
  }
}

variable "firewall_subnet_id" {
  description = "The AzureFirewallSubnet ID (only used when mode = \"firewall\")."
  type        = string
}

variable "aks_subnet_ids" {
  description = "Map of statically-known keys (system/user/gpu) to AKS subnet IDs that get the route table (firewall mode) or NAT gateway association (nat mode). MUST be a map, not a list: the IDs are unknown on first apply, and for_each only tolerates unknown VALUES, never unknown keys (NE-LP-AZURE-EGRESS-FOREACH)."
  type        = map(string)

  validation {
    condition     = toset(keys(var.aks_subnet_ids)) == toset(["system", "user", "gpu"])
    error_message = "aks_subnet_ids must have exactly the keys \"system\", \"user\", \"gpu\" — they become the for_each instance addresses."
  }
}

variable "aks_subnet_cidrs" {
  description = "CIDRs matching aks_subnet_ids, used as the source_addresses for the frontier application rule."
  type        = list(string)
}

variable "frontier_fqdns" {
  description = "C2 single-frontier-lane FQDN allowlist. Empty = air-gap: zero Internet application rules."
  type        = list(string)
}

variable "tags" {
  type    = map(string)
  default = {}
}
