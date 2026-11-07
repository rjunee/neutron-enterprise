#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

[[ "$(id -u)" == 0 ]] || exit 70
systemctl=/usr/bin/systemctl
condition=/usr/local/bin/ne-inference-provider-air-gap-condition
lock=/run/lock/neutron-live-upgrade.lock
[[ -x "$systemctl" && -x "$condition" ]] || exit 71
command -v flock >/dev/null 2>&1 || exit 71

# Refresh is a deployment-generation writer. It drains the broker before
# changing its pinned namespace/receipt and cannot race an in-flight target
# operation holding the shared side of this same lock.
exec 9>"$lock"
flock -x 9
"$systemctl" stop ne-inference-provider-broker.service
if "$condition"; then
  "$systemctl" restart ne-inference-provider-network.service
else
  # ExecStop teardown is deliberately AIR_GAP-independent and performs no DNS.
  "$systemctl" stop ne-inference-provider-network.service
  /opt/neutron/inference-operator/bin/provider-network-policy.sh teardown
fi
# Preserve the stable listener identity. A later broker connection is
# socket-activated and repeats the live AIR_GAP condition before execution.
"$systemctl" start ne-inference-provider-broker.socket
