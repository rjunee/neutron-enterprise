# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy for ne-contracts.

The family covers BOTH contract-layer configuration/registry errors (plugin
resolution, config-hash freeze, the in-memory reference adapters) AND
operationally-raised GOVERNED errors — chiefly
:class:`ContextBudgetExceededError`, a named refusal that flows end-to-end
through the SSE relay.

Because the family now spans governed operational refusals, production code MUST
catch SPECIFIC members (e.g. ``except ContextBudgetExceededError``), NEVER the
broad ``except NeContractsError`` — a broad family catch would silently swallow a
governed refusal and convert a typed, audited "budget exceeded" into an invisible
degradation. This rule is not merely stated here: it is ENFORCED across all
production code by ``tests/test_error_family_guardian.py`` (an AST scan), not by
this docstring.
"""

from __future__ import annotations

from typing import Literal


class NeContractsError(Exception):
    """Base class for all ne-contracts errors."""


class PluginNotFoundError(NeContractsError):
    """No plugin named ``name`` is registered in ``slot``.

    Carries the requested ``slot`` and ``name`` plus the sorted list of
    ``available`` plugin names so operators can see what *is* installed.
    """

    def __init__(self, slot: str, name: str, available: list[str]) -> None:
        self.slot = slot
        self.name = name
        self.available = available
        avail = ", ".join(available) if available else "(none)"
        super().__init__(f"no plugin '{name}' in slot 'ne.plugins.{slot}'; available: {avail}")


class ObjectNotFoundError(NeContractsError):
    """An object key does not exist in the object store."""


class WormViolationError(NeContractsError):
    """A mutating operation targeted a WORM-archived key before ``retain_until``.

    Raised by ``ObjectStore.put`` / ``ObjectStore.delete`` against a key whose
    archive retention window has not yet elapsed. Also raised by
    ``ObjectStore.archive`` when a call would SHORTEN an active retention window
    (retention may only be extended, never reduced).
    """


class SecretNotFoundError(NeContractsError):
    """A secret path/version is missing, soft-deleted, or destroyed."""


class ConfigHashNotFrozenError(NeContractsError):
    """``get_config_hash`` was called before the hash was frozen at process start."""


class ConfigHashMismatchError(NeContractsError):
    """A re-freeze was attempted with a config that hashes differently.

    The process-start ``config_hash`` is frozen once; attempting to freeze a
    *different* hash indicates configuration changed mid-flight, which is a bug.
    """


class ConfigFileNotFoundError(NeContractsError):
    """An explicitly selected TOML config file — via ``NeSettings.from_config_file()``
    or the ``NE_CONFIG_FILE`` environment variable — does not exist."""


class ContextBudgetExceededError(NeContractsError):
    """Assembled prompt cannot fit the input budget even after governed trimming.

    Counts ONLY (C12): never message text. The five fields are numeric/enum and
    the ``str()`` template is pinned by ``tests/test_context_budget_error.py``.
    Raised operationally at two ``stage``s: ``"runtime-trim"`` (Layer-2 dropped
    ``dropped_turns`` oldest turns and still overflowed) and
    ``"gateway-preflight"`` (Layer-1 per-candidate belt; ``dropped_turns`` is 0).
    """

    def __init__(
        self,
        *,
        stage: Literal["runtime-trim", "gateway-preflight"],
        estimated_tokens: int,
        input_budget: int,
        context_window: int,
        dropped_turns: int = 0,
    ) -> None:
        if stage not in ("runtime-trim", "gateway-preflight"):
            raise ValueError("stage must be 'runtime-trim' or 'gateway-preflight'")
        if estimated_tokens < 0:
            raise ValueError("estimated_tokens must be >= 0")
        if input_budget < 0:
            raise ValueError("input_budget must be >= 0")
        if context_window < 0:
            raise ValueError("context_window must be >= 0")
        if dropped_turns < 0:
            raise ValueError("dropped_turns must be >= 0")
        self.stage = stage
        self.estimated_tokens = estimated_tokens
        self.input_budget = input_budget
        self.context_window = context_window
        self.dropped_turns = dropped_turns
        super().__init__(
            f"context budget exceeded at {stage}: "
            f"estimated_tokens={estimated_tokens} "
            f"input_budget={input_budget} "
            f"context_window={context_window} "
            f"dropped_turns={dropped_turns}"
        )
