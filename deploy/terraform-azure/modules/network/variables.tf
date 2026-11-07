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

variable "managed_postgres" {
  description = "Creates the delegated snet-postgres subnet + privatelink.postgres.database.azure.com zone."
  type        = bool
  default     = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
