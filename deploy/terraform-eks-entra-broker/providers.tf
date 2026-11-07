# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# WHY THIS ROOT EXISTS (re-homed 2026-07-20, NE-AWS-AZUREAD):
# hashicorp/azuread 3.9.0 eagerly BUILDS + VALIDATES an Azure authorizer at
# provider Configure() whenever ANY resource references the provider — even a
# count = 0 resource. On a host with `az` present-but-not-logged-in (every
# GitHub-hosted runner is an Azure VM with the az CLI installed) a real
# `terraform apply` therefore dies at provider-config with
# "could not configure AzureCli Authorizer: Please run 'az login'", regardless
# of enable_entra_broker. That broke the credential-less AWS-only apply in
# ../terraform-eks, so the azuread surface was moved OUT of that lane into this
# separate, opt-in root. Proven by the eks-provider-config-smoke CI job in
# .github/workflows/terraform-validate.yml (closure assert + credential-less
# apply of the fixed AWS-only provider set + a negative control that re-runs
# the original azuread repro and confirms it still eager-auth-fails).
#
# Applying THIS root REQUIRES working Azure credentials (az login, or ARM_* env
# with Application.ReadWrite.All) AND AWS credentials able to write Secrets
# Manager in var.region. Apply it SEPARATELY, after ../terraform-eks.
provider "azuread" {}

provider "aws" {
  region = var.region
}
