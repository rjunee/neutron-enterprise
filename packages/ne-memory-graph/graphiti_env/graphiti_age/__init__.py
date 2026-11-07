# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
# pyright: reportUnsupportedDunderAll=false
"""``graphiti_age`` — a Graphiti ``GraphDriver`` over Apache AGE (NE-083c).

Self-contained sidecar-venv package (zero ``ne.*`` imports) so it is separable for the
Option-B upstream contribution. The pure siblings (:mod:`_codec`, :mod:`_dialect`) and
the psycopg chokepoint (:mod:`_pg`) import no ``graphiti_core``; only :mod:`driver`
does. ``AgeDriver`` is therefore exported lazily via ``__getattr__`` so the pure
modules stay importable (and testable) even where ``graphiti_core`` is absent.
"""

from __future__ import annotations

from typing import Any

__all__ = ["AgeDriver", "AgeDriverSession", "AgeSearchInterface"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from graphiti_age import driver

        return getattr(driver, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
