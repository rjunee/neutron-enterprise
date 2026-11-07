# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-permissions-rebac — optional OpenFGA ReBAC module (SPEC §5 NE-087).

A strictly ADDITIVE, fine-grained document-sharing grant channel that unions
(OR) with the existing source-ACL label leg inside the single C7 trimming
chokepoint. ``RebacSettings.enabled`` defaults to ``False`` — a deployment
without an explicit fine-grained-sharing requirement has ZERO behavior change,
and nothing in the platform imports this package until the NE-087-WIRING (O-4)
rider wires it at the composition root.

Governance invariants (grep-provable): the union is fail-closed on both legs;
``$any`` is rejected in caller filter position; :class:`ShareService` is the ONLY
tuple writer and is PDP-gated + audited (no sink-less constructor); a server
outage fails closed (∅ in degrade, raise in deny — never wider); the OpenFGA SDK
is confined to :mod:`ne.permissions_rebac._client` (the C3 chokepoint).
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.permissions_rebac import errors
from ne.permissions_rebac.config import RebacSettings
from ne.permissions_rebac.resolver import OpenFgaSharedDocumentResolver
from ne.permissions_rebac.share import ShareService

__all__ = [
    "OpenFgaSharedDocumentResolver",
    "RebacSettings",
    "ShareService",
    "__version__",
    "errors",
]
