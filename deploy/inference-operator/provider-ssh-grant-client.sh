#!/bin/sh
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
set -eu
exec /usr/bin/sudo -n /opt/neutron/inference-operator/bin/provider-ssh-grant
