# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.55.0" # exact pin (C11) — verified registry.terraform.io 2026-07-19
    }
    helm = {
      source  = "hashicorp/helm"
      version = "3.2.0" # exact pin (C11) — same verified pin as ../terraform-azure (2026-07-09); re-verified registry.terraform.io 2026-07-17 (3.2.0 still latest, MPL-2.0); only used by velero.tf (install_velero=true)
    }
  }
}
