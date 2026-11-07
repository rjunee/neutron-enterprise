# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Composition-root factory for the confined OpenFGA client (O-4 wiring).

:func:`create_openfga_client` is the ONE seam the ne-api / ne-worker composition
roots call to obtain an :class:`~ne.permissions_rebac.ports.OpenFgaTupleClient`
over the real SDK. The ``openfga_sdk`` import stays confined to
:mod:`ne.permissions_rebac._client` — this module imports ``_client`` at FUNCTION
level so ne-api's eager import graph never drags in the SDK (the c3 chokepoint's
ignore-edge kills every chain; ``lint-imports`` stays green with no contract
change). It never constructs the un-pinned bootstrap surface (that lives only in
the operator CLI), so a resolver can only ever be wired over a pinned client.
"""

from __future__ import annotations

from ne.permissions_rebac.config import RebacSettings
from ne.permissions_rebac.ports import OpenFgaTupleClient

__all__ = ["create_openfga_client"]


def create_openfga_client(settings: RebacSettings) -> OpenFgaTupleClient:
    """Construct the pinned, credentialed HTTP client from settings.

    Fail-closed by delegation: :class:`OpenFgaHttpClient` raises if constructed
    while disabled or without a pinned ``authorization_model_id``. No network
    contact happens here — only the SDK client object is built.
    """
    from ne.permissions_rebac._client import OpenFgaHttpClient

    return OpenFgaHttpClient(settings)
