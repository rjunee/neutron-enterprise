# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The cedarpy in-process PDP alternate — install ``ne-govern[cedar]``.

:class:`~ne.govern.pdp.GovernedPdp` (OPA) is the default
:class:`~ne.contracts.governance.PolicyDecisionPoint`; this backend is selected
via ``GovernSettings.pdp = "cedar"`` (or ``NE_GOVERN_PDP=cedar``). Nothing in
:mod:`ne.govern` imports this subpackage — importing ``ne.govern`` alone never
pulls in ``cedarpy``.
"""

from __future__ import annotations

from ne.govern.cedar.pdp import CedarGovernedPdp

__all__ = ["CedarGovernedPdp"]
