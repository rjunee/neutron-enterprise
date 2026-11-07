# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy for ne-secrets.

Contract-level "not found" semantics are ALWAYS raised as
:class:`ne.contracts.errors.SecretNotFoundError` (the conformance kit catches
that exact type) — never a local subclass. This module only covers
adapter/transport-level failures that are outside the contract's vocabulary.
"""

from __future__ import annotations


class NeSecretsError(Exception):
    """Base class for all ne-secrets errors."""


class SecretsBackendError(NeSecretsError):
    """An adapter/transport failure: not a contract-level not-found.

    Examples: the ``sops`` binary is missing or exits non-zero, OpenBao is
    unreachable, OpenBao returns a non-2xx/non-404 status, or a required token
    environment variable is not set.
    """
