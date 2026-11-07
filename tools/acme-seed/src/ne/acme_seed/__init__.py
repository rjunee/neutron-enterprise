# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne-acme-seed`` — the live-tenant Acme corpus seeder (NE-102).

An egress-by-design DEV tool, deliberately housed under ``tools/`` and OUTSIDE
the shipped ``ne-connect`` product package (and its C2 egress guardian). It
pushes a deterministic, lightest-sufficient slice of the committed ``tools/acme``
fixture corpus INTO the three dev tenants (M365 SharePoint, Salesforce, Slack) so
the already-built READ connectors can pull it back — the NE-077 LIVE gate.

It reuses the connector settings/auth/client classes by SUBCLASSING (adding the
thin write verbs in the tool), leaving ``packages/ne-connect`` byte-identical.
CI is 100% mocked; live seeding is an owner-run step. See ``README.md``.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
