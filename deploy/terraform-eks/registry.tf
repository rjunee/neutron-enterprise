# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# The `ne release push` target for the smoke — proves the registry story is
# cloud-neutral (design §2.5). Mutable tags: digest pinning is the C11
# mechanism (the chart's image.digest field), not registry immutability.
# Images at rest are CMK-encrypted (the s3-backup CMK, NE-AWS-8) — KMS
# instead of the AES256 default.
resource "aws_ecr_repository" "ne_app" {
  name                 = "neutron/ne-app"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = module.kms.key_arns["s3-backup"]
  }

  tags = var.tags
}
