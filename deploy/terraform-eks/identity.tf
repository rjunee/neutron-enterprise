# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Design deliverable 5 (NE-AWS-7): the Velero IRSA role (consumed by
# NE-AWS-11's helm_release SA annotation).
#
# The Entra->Keycloak broker was re-homed to ../terraform-eks-entra-broker
# (NE-AWS-AZUREAD, 2026-07-20): the Azure AD / Entra provider (v3.9.0)
# eager-auths at Configure() whenever referenced, which broke the
# credential-less AWS-only apply — see that root's README + the
# eks-provider-config-smoke CI job. (This lane must not reference that
# provider by name; the CI grep guard enforces it.)

module "identity" {
  source = "./modules/identity"

  prefix = var.prefix
  tags   = var.tags

  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider     = module.eks.oidc_provider

  backup_bucket_arn     = module.storage.bucket_arn
  s3_backup_kms_key_arn = module.kms.key_arns["s3-backup"]
}
