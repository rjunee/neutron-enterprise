# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# required_version is a RANGE, not an exact pin: it admits OpenTofu >= 1.9 as
# well as Terraform (see README "OpenTofu compatibility" — Terraform CLI is
# BUSL-1.1, CI pins the exact binary at 1.15.8; this repo does not ship or
# embed the CLI, so BUSL applies to tooling only, never to a shipped artifact).
terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "4.80.0" # exact pin (C11) — verified registry.terraform.io 2026-07-09
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "3.9.0" # exact pin (C11)
    }
    helm = {
      source  = "hashicorp/helm"
      version = "3.2.0" # exact pin (C11) — only used by velero.tf (install_velero=true)
    }
    azapi = {
      source  = "Azure/azapi"
      version = "2.11.0" # exact pin (C11) — MPL-2.0, Microsoft — verified registry.terraform.io 2026-07-23
    }
  }
}
