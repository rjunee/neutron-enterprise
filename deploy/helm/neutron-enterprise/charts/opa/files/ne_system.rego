# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Dev placeholder policy bundle loaded by the OPA container. It exists only
# so the dev-stack OPA server has something to serve; it makes no real
# authorization decisions. Real policy bundles for ne-govern arrive with
# NE-031 — do not extend this file for product policy.

package ne.dev

default allow := false
