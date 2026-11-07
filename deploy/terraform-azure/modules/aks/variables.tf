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

variable "kubernetes_version" {
  type = string
}

variable "system_subnet_id" {
  type = string
}

variable "user_subnet_id" {
  type = string
}

variable "gpu_subnet_id" {
  type = string
}

variable "outbound_type" {
  description = "\"userDefinedRouting\" (egress_mode=firewall) or \"userAssignedNATGateway\" (egress_mode=nat)."
  type        = string
}

variable "gpu_node_pool" {
  description = "Owner-quota-gated (Risk R2). Standard_NV36ads_A10_v5, scale-to-zero, tainted."
  type        = bool
  default     = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
