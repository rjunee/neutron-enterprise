# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-T2X-2: the optional, configurable LLM entity-extraction pathway (mode 1).

Ships ONLY ``ingest_review`` (design §2.2) against a scripted
:class:`~ne.contracts.models.ModelGateway`: :mod:`.job` (the T-ER-1-mirrored,
scan-only-store proposal producer), :mod:`.writer` (the ONLY module that may
compose a ``source_system='ne:llm'`` node/edge, stamping every §2.1 floor field
itself), :mod:`.apply` (the gate-checked apply/revert, mirroring
``er.merge.apply_merge``/``unmerge``), and :mod:`.identity` (deterministic,
namespace-separated keys — no fuzzy matching, ever).

Modes 2 (``read_flagged``) and 3 (``autonomous``) ride the SAME spine but are
NOT this unit's scope (NE-T2X-4).

Module-level constants: ``LLM_SOURCE_SYSTEM`` is the permanent provenance label
stamped on every element this pathway writes (never confused with the
deployment-posture ``extraction_mode`` — design §0 D-P2-2 rejected folding the
two together); ``EXTRACTION_TASK`` is the ne-gateway task name every completion
request in this package MUST carry (design B-P1-4/C3 — the dead-assist lesson);
``ASSURANCE_VALUES`` is the single-sourced set of assurance labels this pathway
may stamp (Argus P3-1 — re-exported by :mod:`.writer`, imported directly by
``read.py``, never duplicated).
"""

from __future__ import annotations

LLM_SOURCE_SYSTEM = "ne:llm"
EXTRACTION_TASK = "graph-extract"

#: The two assurance labels this pathway may stamp. ``"source"`` (the
#: contracts default, backbone/ER) is deliberately absent — this pathway
#: never produces it. Single-sourced HERE (the dependency-free constants
#: leaf): ``extraction.writer`` re-exports this exact object for API
#: stability, and ``read.py`` imports it directly for its fail-closed
#: assurance truth-table — neither module holds its own copy (Argus P3-1:
#: derive, don't transliterate).
ASSURANCE_VALUES: frozenset[str] = frozenset({"extracted", "confirmed"})

__all__ = ["ASSURANCE_VALUES", "EXTRACTION_TASK", "LLM_SOURCE_SYSTEM"]
