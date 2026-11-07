# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

variable "prefix" {
  description = "Resource-name prefix; aliases are alias/<prefix>-<class>."
  type        = string
}

variable "key_classes" {
  description = "Data-class name -> CMK description, one CMK per class — the five classes are declared at the root."
  type        = map(string)
}

variable "deletion_window_in_days" {
  type    = number
  default = 30
}

variable "key_usage_role_arns" {
  description = "PLACEHOLDER wired by NE-AWS-5/6/8/10/11 — class -> IAM role ARNs granted scoped usage; empty by default so day-one policies contain only the account root."
  type        = map(list(string))
  default     = {}
}

variable "key_service_principals" {
  description = "PLACEHOLDER wired by later units — class -> AWS service principals, e.g. logs.<region>.amazonaws.com for NE-AWS-5's CloudWatch log groups, guarded by a kms:CallerAccount condition."
  type        = map(list(string))
  default     = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}
