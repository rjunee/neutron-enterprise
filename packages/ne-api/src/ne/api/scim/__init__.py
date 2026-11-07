# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SCIM 2.0 provisioning (NE-089) — a thin first-party provider mounted at
``/scim/v2`` on ne-api, OUTSIDE the OIDC wrap, gated by its own bearer secret.

Every write goes ONLY through the EXISTING ``ne.permissions.store.PrincipalStore``
public API (never a new read/trim path): ``deactivate`` maps to
``mark_removed`` — the tombstone + membership-edge-cascade primitive — so access
removal is fail-closed by construction. See ``ne.api.scim.service`` for the
governed provisioning logic and ``ne.api.scim.router`` for the mounted ASGI app.

Zero new third-party dependencies: the python-scim project (``scim2-models`` /
``scim2-server``) was surveyed and REJECTED per the SPEC row's explicit
"prefer a thin first-party SCIM handler" directive — see ``packages/ne-api/README.md``
and the unit's PR body for the full adopt-rejected rationale.
"""

from __future__ import annotations

from ne.api.scim.router import build_scim_app
from ne.api.scim.service import ScimProvisioningService

__all__ = ["ScimProvisioningService", "build_scim_app"]
