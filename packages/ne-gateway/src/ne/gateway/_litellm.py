# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The single, repo-wide litellm import chokepoint (C3 model independence).

This module is the ONLY place in the entire repository permitted to
``import litellm``. Every other gateway module imports ``litellm`` / ``Router``
FROM here (``from ne.gateway._litellm import litellm, Router``). NE-012's
import-linter contract now enforces this mechanically — it lives at ``/.importlinter``
(the ``c3-provider-sdk-chokepoint`` forbidden contract, sole ignored edge
``ne.gateway._litellm -> litellm``) and is CI-enforced. The
``test_only_litellm_module_imports_litellm`` grep test remains as belt-and-braces.

Import order matters. The two environment forces below MUST happen *before*
``import litellm`` because litellm reads them at import time:

* ``LITELLM_LOCAL_MODEL_COST_MAP=True`` — use the packaged cost map, never fetch
  it over the network (no egress at import).
* ``LITELLM_TELEMETRY=False`` — defense-in-depth. NOTE: litellm 1.90.3 does NOT
  read this env var anywhere (``litellm/__init__.py`` hardcodes
  ``telemetry = True``); the post-import ``litellm.telemetry = False`` assignment
  below is the EFFECTIVE control. The env force is kept for future versions.
"""

from __future__ import annotations

import os

# Force pre-import (plain assignment, NOT setdefault — these are non-negotiable).
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["LITELLM_TELEMETRY"] = "False"

import litellm
from litellm.router import Router  # public Router class; avoids reportPrivateImportUsage

# Post-import hard-off — the effective telemetry/callback kill switch in 1.90.3.
litellm.telemetry = False
litellm.suppress_debug_info = True
litellm.success_callback = []
litellm.failure_callback = []
litellm.callbacks = []

__all__ = ["Router", "litellm"]
