# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-api — the governed console-backend HTTP service (composition root).

The four public names are resolved LAZILY (PEP 562 ``__getattr__``) rather than
imported here. Importing this package used to import ``ne.api.app`` eagerly,
and because every ``ne.api.<submodule>`` import runs this ``__init__`` first,
``from ne.api.agent_manifest import load_agent_manifest`` -- a 21µs module --
cost 5.4s (measured 2026-09-26: ``ne.api.app`` 4.5s, of which litellm 1.7s).
Callers that only need a small submodule paid that on every call.
``from ne.api import create_app`` still works and still imports the app on
first access; only the cost has moved to the callers who want it.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ne.api.app import AppSeams, app_factory, create_app
    from ne.api.settings import ApiSettings

__all__ = ["ApiSettings", "AppSeams", "app_factory", "create_app"]

_LAZY = {
    "AppSeams": "ne.api.app",
    "app_factory": "ne.api.app",
    "create_app": "ne.api.app",
    "ApiSettings": "ne.api.settings",
}


def __getattr__(name: str) -> object:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
