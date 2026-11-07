# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Non-hashicorp providers MUST be declared in every module that uses them:
# implicit provider inference assumes the `hashicorp/` namespace, which would
# resolve `azapi` to the nonexistent `hashicorp/azapi` and fail init. This pin
# is kept in lockstep with the root ../../versions.tf (a divergent version
# here makes `terraform init` fail loudly rather than silently resolving two
# different azapi versions across the module graph).
terraform {
  required_providers {
    azapi = {
      source  = "Azure/azapi"
      version = "2.11.0"
    }
  }
}
