#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# ne-eval promptfoo CI gate runner.
#
# promptfoo is an EXTERNAL, pinned npm CLI (NOT a Python dependency). It requires
# Node >= 22 (promptfoo drops Node 20 support on 2026-07-30). Telemetry and
# update checks are disabled (C1 no phone-home). CI-workflow wiring of this script
# is a ROOT-lane concern (NE-025 / orchestrator), not this package.
set -euo pipefail

export PROMPTFOO_DISABLE_TELEMETRY=1
export PROMPTFOO_DISABLE_UPDATE=1

exec npx -y promptfoo@0.121.17 eval -c "$(dirname "$0")/promptfooconfig.json" --no-cache
