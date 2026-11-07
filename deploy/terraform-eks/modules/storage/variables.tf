# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

variable "prefix" {
  description = "Resource-name prefix; the bucket is named <prefix>-backup."
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "kms_key_arn" {
  description = "ARN of the s3-backup CMK (NE-AWS-4) used for SSE-KMS default encryption and enforced on PutObject."
  type        = string
}

variable "s3_vpc_endpoint_id" {
  description = "ID of the s3 GATEWAY VPC endpoint (endpoints.tf, NE-AWS-3). The bucket policy denies object-data actions (GetObject/PutObject/DeleteObject) whose request doesn't carry this aws:sourceVpce, so an authorized-but-leaked credential used from outside the VPC still can't read or write backup data over the public S3 endpoint — the public-access-block only stops public ACLs/policies, not an authenticated principal reaching the bucket over the internet."
  type        = string
}

variable "enable_immutability" {
  description = "Documented, off by default. Enables S3 Object Lock on the backup bucket with a 30-day COMPLIANCE-mode default retention (the WORM twin of terraform-azure's flag). IRREVERSIBLE: COMPLIANCE-mode locked object versions cannot be deleted or shortened by ANY principal, including the account root, until retention expires — never enable without an explicit customer decision (README). Toggling on an existing deployment also forces bucket replacement (object_lock_enabled is create-time)."
  type        = bool
  default     = false
}
