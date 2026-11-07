# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""RedactionStage — the backend-agnostic C12 redaction contract (fail-closed).

The stage implements the frozen :class:`ne.contracts.governance.Redactor`
Protocol and delegates to a pluggable backend (Presidio via isolation by default,
the stdlib regex backend as an air-gap supplement). Its one non-delegated
responsibility is the fail-closed guarantee: if the backend raises for ANY
reason, the stage raises :class:`~ne.govern.errors.RedactionError` and the
original (possibly PII-bearing) text NEVER escapes.
"""

from __future__ import annotations

from collections.abc import Sequence

from ne.contracts.governance import RedactionResult, Redactor
from ne.govern.errors import RedactionError


class RedactionStage:
    """Backend-agnostic redaction stage; fails closed on any backend error."""

    def __init__(self, backend: Redactor) -> None:
        self._backend = backend

    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        try:
            return self._backend.redact(text, categories=categories)
        except RedactionError:
            raise
        except Exception as exc:
            # Fail closed: unredacted text must never be returned on failure.
            raise RedactionError(
                f"redaction backend {type(self._backend).__name__} failed; "
                "text withheld (C12 fail closed)"
            ) from exc
