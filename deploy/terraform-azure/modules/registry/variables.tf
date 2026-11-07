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

variable "private_endpoints_subnet_id" {
  type = string
}

variable "private_dns_zone_id" {
  description = "privatelink.azurecr.io zone ID (../network output)."
  type        = string
}

variable "kubelet_identity_object_id" {
  description = "AKS kubelet managed identity — gets the AcrPull role."
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
