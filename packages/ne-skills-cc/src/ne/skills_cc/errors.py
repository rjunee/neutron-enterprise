# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed errors for the Claude-Code SkillSubstrate binding."""

from __future__ import annotations


class SkillAuthoringDispatchError(RuntimeError):
    """The CC author subprocess failed: non-zero exit, timeout, or empty output.

    Carries the process's return code and a TAIL of stderr only (C12-cautious —
    a subprocess's stderr can echo back fragments of the fenced task; the tail is
    capped so this error never becomes a large-payload leak vector).
    """
