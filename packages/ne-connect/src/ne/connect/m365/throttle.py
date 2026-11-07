# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Compatibility shim (NE-045 hoist).

``AdaptiveThrottle`` moved to :mod:`ne.connect.throttle` (it now also serves the
Slack connector). This module re-exports it so ``ne.connect.m365.graph`` and any
existing imports keep working unchanged.
"""

from __future__ import annotations

from ne.connect.throttle import AdaptiveThrottle

__all__ = ["AdaptiveThrottle"]
