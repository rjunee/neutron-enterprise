# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Core terraform ruleset only — no cloud-provider plugin (avoids an
# unpinned plugin fetch in CI).

config {
  format = "compact"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
