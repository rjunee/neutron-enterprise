# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# The S3 backup bucket (design deliverables 2/4/8 storage halves,
# NE-AWS-8) — the Azure modules/storage twin. Consumes NE-AWS-4's
# s3-backup CMK; consumed by NE-AWS-7 (Velero IRSA policy scoped to this
# bucket) and NE-AWS-11 (Velero BackupStorageLocation).

module "storage" {
  source = "./modules/storage"

  prefix = var.prefix
  tags   = var.tags

  kms_key_arn = module.kms.key_arns["s3-backup"]

  s3_vpc_endpoint_id = module.vpc_endpoints.endpoints["s3"].id

  enable_immutability = var.enable_immutability
}
