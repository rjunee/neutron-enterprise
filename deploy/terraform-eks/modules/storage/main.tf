# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Backup/Velero bucket: private, versioned, CMK-encrypted. The destination
# for ne-backup's object-store target and Velero's BackupStorageLocation
# (NE-AWS-11); the design deliverable 2/4/8 storage halves. C2: no public
# access — reachable only via the s3 gateway endpoint (endpoints.tf,
# NE-AWS-3) from inside the VPC.

# S3 bucket names are globally unique across ALL AWS accounts (unlike the
# other <prefix>-* resource names in this stack, which only need to be
# unique within one account/region) — suffix with the account ID so two
# accounts running this smoke stack with the same default prefix never
# collide on BucketAlreadyExists.
data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "backup" {
  bucket = "${var.prefix}-backup-${data.aws_caller_identity.current.account_id}"

  object_lock_enabled = var.enable_immutability

  # Hardened posture: destroying a bucket holding backups must be a
  # deliberate manual purge, not a terraform default — force_destroy is
  # deliberately omitted.

  tags = var.tags
}

resource "aws_s3_bucket_versioning" "backup" {
  bucket = aws_s3_bucket.backup.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "backup" {
  bucket = aws_s3_bucket.backup.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Uploads must explicitly request SSE-KMS — the deny pair is AWS's
# documented belt-and-braces pattern; Velero's BSL sets kmsKeyId (NE-AWS-11)
# so its plugin sends the header. TLS-only is the min_tls_version twin.
data "aws_iam_policy_document" "backup" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:*"]
    resources = [aws_s3_bucket.backup.arn, "${aws_s3_bucket.backup.arn}/*"]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid    = "DenyIncorrectEncryptionHeader"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.backup.arn}/*"]

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }

  statement {
    sid    = "DenyUnEncryptedObjectUploads"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.backup.arn}/*"]

    condition {
      test     = "Null"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["true"]
    }
  }

  # C2 defense-in-depth: the s3 GATEWAY endpoint (endpoints.tf, NE-AWS-3)
  # already keeps in-VPC traffic off the public internet, but that's a
  # network-path fact, not an IAM guarantee — a credential valid for this
  # bucket could still call GetObject/PutObject over the public S3 endpoint
  # from outside the VPC. Deny object-data actions whose request doesn't
  # carry the expected aws:sourceVpce so backup data is reachable only via
  # that endpoint, regardless of where the credential leaks to. Scoped to
  # data actions only (not s3:*) so Terraform itself — which applies and
  # destroys this stack from a GitHub-hosted runner outside the VPC, see
  # .github/workflows/eks-smoke.yml — can still create/tag/version/destroy
  # the bucket; only the object payload is fenced to the endpoint.
  statement {
    sid    = "DenyDataAccessOutsideVpce"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.backup.arn}/*"]

    condition {
      test     = "StringNotEquals"
      variable = "aws:sourceVpce"
      values   = [var.s3_vpc_endpoint_id]
    }
  }
}

resource "aws_s3_bucket_policy" "backup" {
  bucket = aws_s3_bucket.backup.id
  policy = data.aws_iam_policy_document.backup.json

  # Avoids the live-apply OperationAborted race between policy put and
  # access-block put.
  depends_on = [aws_s3_bucket_public_access_block.backup]
}

# Documented, off by default (var.enable_immutability): 30-day
# COMPLIANCE-mode Object Lock — the WORM twin of
# azurerm_storage_container_immutability_policy. COMPLIANCE retention is
# irreversible for locked versions (no principal, including account root,
# can delete/shorten) — never enable without an explicit customer decision
# (README).
resource "aws_s3_bucket_object_lock_configuration" "backup" {
  count  = var.enable_immutability ? 1 : 0
  bucket = aws_s3_bucket.backup.id

  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = 30
    }
  }

  depends_on = [aws_s3_bucket_versioning.backup]
}
