# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

variable "prefix" {
  description = "Short resource-name prefix for every resource in this module."
  type        = string
}

variable "mode" {
  description = "\"firewall\" (default, FQDN-exact C2 lane) or \"nat\" (FQDN precision lost — README Risk R4 twin)."
  type        = string

  validation {
    condition     = contains(["firewall", "nat"], var.mode)
    error_message = "mode must be either \"firewall\" or \"nat\"."
  }
}

variable "vpc_id" {
  description = "The VPC id the firewall attaches to."
  type        = string
}

variable "azs" {
  description = "The availability zones this network spans — index-aligned with every other *_ids/*_cidrs list below."
  type        = list(string)
}

variable "firewall_subnet_ids" {
  description = "The intra subnets — the AzureFirewallSubnet twin — same order as azs. One firewall endpoint is created per subnet."
  type        = list(string)
}

variable "private_route_table_ids" {
  description = "Route tables for the private (worker) subnets — same order as azs."
  type        = list(string)
}

variable "private_subnet_cidrs" {
  description = "CIDRs of the private (worker) subnets — same order as azs. Used as the public-RT return-route destinations."
  type        = list(string)
}

variable "public_route_table_ids" {
  description = "Route tables for the public (NAT) subnets. Length 1 today (single shared public RT); element() wraparound also supports per-subnet RTs."
  type        = list(string)
}

variable "intra_route_table_ids" {
  description = "Route tables for the intra (firewall-endpoint) subnets — per-AZ, requires create_multiple_intra_route_tables = true on the vpc module."
  type        = list(string)
}

variable "nat_gateway_ids" {
  description = "NAT gateway ids — same order as azs."
  type        = list(string)
}

variable "frontier_fqdns" {
  description = "The C2 single-frontier-lane allowlist rendered as exact-match tls.sni pass rules. Empty means air-gap: no rule group is created, zero pass rules."
  type        = list(string)
}

variable "logs_kms_key_arn" {
  description = "The NE-AWS-4 `logs` CMK ARN — encrypts the ALERT/FLOW CloudWatch log groups."
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch log group retention for ALERT/FLOW firewall logs."
  type        = number
  default     = 90
}

variable "tags" {
  type    = map(string)
  default = {}
}
