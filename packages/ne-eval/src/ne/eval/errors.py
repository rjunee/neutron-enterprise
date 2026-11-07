# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Error hierarchy for ne-eval.

Every fault raised by the harness derives from :class:`NeEvalError`, so a caller
can catch the whole family with one ``except``. Judge-policy enforcement is
deliberately *fail-closed*: a missing/stale calibration artifact, a same-family
judge, or a config error raises **before** any scoring runs, and per-item judge
faults are caught and turned into a 0.0 score with an error string (never a crash).
"""

from __future__ import annotations


class NeEvalError(Exception):
    """Base class for all ne-eval faults."""


class GoldenSetError(NeEvalError):
    """A golden set could not be loaded, saved, or validated."""


class RubricError(NeEvalError):
    """A rubric is unknown, malformed, or a render placeholder is missing."""


class JudgeError(NeEvalError):
    """A judge call failed (transport, protocol, or output)."""


class JudgeOutputError(JudgeError):
    """A judge produced output that could not be parsed/validated after retry."""


class CrossFamilyError(NeEvalError):
    """The judge family equals the evaluated family — cross-family judging is mandatory."""


class CalibrationError(NeEvalError):
    """Base for judge-vs-human calibration faults."""


class CalibrationMissingError(CalibrationError):
    """No calibration artifact exists for a judge-scored rubric + judge."""


class CalibrationStaleError(CalibrationError):
    """A calibration artifact exists but its rubric hash or judge id no longer matches."""


class EvalConfigError(NeEvalError):
    """The eval configuration is invalid (unknown metric, empty family, bad top_k, ...)."""


class EvalStoreError(NeEvalError):
    """The eval-run store could not persist or read a record."""
