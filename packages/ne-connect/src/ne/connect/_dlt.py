# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The single, repo-wide dlt import chokepoint (C1 no-phone-home).

This module is the ONLY place in ne-connect permitted to ``import dlt``. Every
other ``ne.connect`` module that needs dlt imports it FROM here
(``from ne.connect import _dlt`` / ``_dlt.dlt``) — this now includes
``ne.connect.sql`` (NE-046), which imports ``sql_table`` / ``incremental`` from
this module rather than reaching into ``dlt.sources.sql_database`` /
``dlt.sources`` directly. Modeled exactly on the ne-gateway ``_litellm.py``
chokepoint pattern.

Import order is load-bearing. ``RUNTIME__DLTHUB_TELEMETRY=false`` MUST be forced
by a PLAIN assignment (not ``setdefault``) BEFORE ``import dlt``, because dlt
reads its telemetry configuration at import time — this disables dlt's anonymous
telemetry so the framework never phones home (C1).

Follow-up (out of scope here): adding ``ne.connect._dlt -> dlt`` as the sole
ignored edge of a C3-style forbidden ``dlt`` contract in ``/.importlinter`` is
orchestrator work; the ``test_dlt_confinement.py`` grep test is the in-package
guardian in the meantime (its ``rglob`` sole-import-site check covers ``sql/``
automatically — no update needed there).
"""

from __future__ import annotations

import os

# Force pre-import (plain assignment, NOT setdefault — non-negotiable, C1).
os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"

import dlt
from dlt.sources.sql_database import sql_table

DLT_VERSION = dlt.__version__
incremental = dlt.sources.incremental

__all__ = ["DLT_VERSION", "dlt", "incremental", "sql_table"]
