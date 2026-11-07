# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Load sidecar ``graphiti_env`` files by path for platform-side always-on tests.

The sidecar modules live outside any installed package (they run inside the isolated
sidecar venv), so tests load them by absolute path — the
``test_sidecar_service_auth.py::_load_service_module`` pattern, factored here. The
pure ``graphiti_age`` siblings (``_codec``/``_dialect``) import no ``graphiti_core``;
``_pg`` imports only psycopg (present in the platform env via ne-brain-graph), so all
three load standalone without the sidecar venv.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from ne.memory_graph.sidecar import default_graphiti_env_dir

__all__ = ["graphiti_age_dir", "load_sidecar_module"]


def graphiti_age_dir() -> Path:
    return default_graphiti_env_dir() / "graphiti_age"


def load_sidecar_module(relative_path: str, module_name: str) -> ModuleType:
    """Load ``graphiti_env/<relative_path>`` as a standalone module."""
    path = default_graphiti_env_dir() / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
