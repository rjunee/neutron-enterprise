# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# This tree is the C9 cloud-portability proof — being PROMOTED in place
# toward C2/C9 parity with ../terraform-azure
# (SPEC §11 2026-07-17). Root
# files are split per concern (D3) so promotion units mostly ADD files
# rather than editing this one. A live apply remains LIVE-PARKED (owner
# AWS account); this tree is validated by
# .github/workflows/terraform-validate.yml only.

provider "aws" {
  region = var.region
}
