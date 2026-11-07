# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.55.0" # exact pin (C11) — same pin as ../terraform-eks; re-verified registry.terraform.io 2026-07-20
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "3.9.0" # exact pin (C11) — re-verified registry.terraform.io 2026-07-20 (3.9.0 latest, MPL-2.0)
    }
  }
}
