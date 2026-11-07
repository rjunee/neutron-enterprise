# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Backend-agnostic C12 PII redaction.

:class:`~ne.govern.redaction.stage.RedactionStage` implements the frozen
:class:`ne.contracts.governance.Redactor` Protocol over a pluggable backend and
guarantees fail-closed behaviour (a backend error withholds the text). Two
backends ship:

  * :class:`~ne.govern.redaction.presidio_backend.PresidioRedactor` — the
    adopt-first, industry-standard detector, ADOPTED VIA ISOLATION (a hash-pinned
    ``presidio_env`` venv + subprocess worker; its spaCy/model closure never
    enters the platform lock, SPEC §11);
  * :class:`~ne.govern.redaction.regex_backend.RegexRedactor` — a stdlib-only
    air-gap SUPPLEMENT/fallback, never the primary detector.
"""

from __future__ import annotations

from ne.govern.redaction.presidio_backend import PresidioRedactor
from ne.govern.redaction.regex_backend import RegexRedactor
from ne.govern.redaction.stage import RedactionStage

__all__ = [
    "PresidioRedactor",
    "RedactionStage",
    "RegexRedactor",
]
