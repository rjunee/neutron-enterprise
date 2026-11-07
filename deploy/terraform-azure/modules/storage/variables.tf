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
  description = "privatelink.blob.core.windows.net zone ID (../network output)."
  type        = string
}

variable "enable_immutability" {
  description = "Documented, off by default. Controls the OPTIONAL time-based (non-VLW) WORM retention policy on the velero container ONLY — this is NOT the version-level immutability (VLW) that AzureBlobObjectStore requires (that is the always-on azapi_resource.worm container, a distinct Azure feature)."
  type        = bool
  default     = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
