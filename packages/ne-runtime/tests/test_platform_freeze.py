# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Composed platform-freeze wiring (ADR-0003, NE-CKC12).

Proves the ne-runtime composition root freezes the SPEC §4 replay/audit
``config_hash`` over the COMPOSED platform mapping (ne-runtime + ne-govern +
caller-passed modules) via :func:`ne.runtime.freeze_platform_config`, and that
downstream ``AuditEvents`` stamp that composed digest — not the NE-033
single-module subset. All hashing/serialization is delegated to the frozen
``ne.contracts.config`` functions (NE-CKC5 determinism/order-independence
inherited); this suite pins the wiring at the ne-runtime seam.

The conftest autouse ``frozen_config_hash`` fixture freezes the single-module
``RuntimeSettings`` hash before each test, so every platform-freeze test (except
the mixed-freeze pin) resets first; the fixture teardown resets afterward.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from ne.contracts import (
    ConfigHashMismatchError,
    compute_config_hash,
    compute_platform_config_hash,
    get_config_hash,
    reset_config_hash_for_testing,
)
from ne.events import new_audit_event
from ne.govern.config import GovernSettings
from ne.runtime import freeze_platform_config
from ne.runtime.config import RuntimeSettings
from ne.schema import AuditOutcome


class _GatewayStub(BaseModel):
    default_model: str = "m"


def _oracle() -> str:
    """The composed platform digest over the two always-frozen modules."""
    return compute_platform_config_hash(
        {"ne-runtime": RuntimeSettings(), "ne-govern": GovernSettings()}
    )


def test_freeze_platform_config_covers_runtime_and_govern() -> None:
    reset_config_hash_for_testing()
    digest = freeze_platform_config()
    assert digest == _oracle()
    # The frozen slot holds the composed digest unchanged.
    assert get_config_hash() == digest


def test_platform_hash_is_not_the_single_module_subset() -> None:
    reset_config_hash_for_testing()
    digest = freeze_platform_config()
    # Domain separation: composed mapping preimage != any bare single-module tree.
    assert digest != compute_config_hash(RuntimeSettings())
    assert digest != compute_config_hash(GovernSettings())


def test_audit_events_stamp_the_composed_platform_hash() -> None:
    reset_config_hash_for_testing()
    digest = freeze_platform_config()
    event = new_audit_event(
        action="platform.freeze.check",
        resource="config:platform",
        outcome=AuditOutcome.success,
        actor="test",
        detail="",
        provenance={"source": "ne-runtime", "component": "test"},
    )
    assert event.config_hash == digest


def test_freeze_platform_config_is_idempotent() -> None:
    reset_config_hash_for_testing()
    first = freeze_platform_config()
    second = freeze_platform_config()
    assert first == second == _oracle()


def test_divergent_refreeze_raises() -> None:
    reset_config_hash_for_testing()
    freeze_platform_config()
    with pytest.raises(ConfigHashMismatchError):
        freeze_platform_config(extra_modules={"ne-gateway": _GatewayStub()})


def test_extra_modules_extend_hash_order_independently() -> None:
    reset_config_hash_for_testing()
    a = _GatewayStub(default_model="a")
    b = _GatewayStub(default_model="b")
    expected = compute_platform_config_hash(
        {
            "ne-runtime": RuntimeSettings(),
            "ne-govern": GovernSettings(),
            "ne-alpha": a,
            "ne-beta": b,
        }
    )
    digest = freeze_platform_config(extra_modules={"ne-alpha": a, "ne-beta": b})
    assert digest == expected
    # Insertion order of extra_modules is irrelevant (NE-CKC5 order-independence).
    reset_config_hash_for_testing()
    reordered = freeze_platform_config(extra_modules={"ne-beta": b, "ne-alpha": a})
    assert reordered == expected


def test_reserved_key_collision_and_mixed_freeze() -> None:
    # (a) A key colliding with a module the root already freezes is a ValueError.
    reset_config_hash_for_testing()
    with pytest.raises(ValueError, match="collides"):
        freeze_platform_config(extra_modules={"ne-runtime": _GatewayStub()})
    # (b) WITHOUT resetting the conftest autouse single-module freeze, the platform
    # freeze diverges from the already-frozen freeze_config_hash(RuntimeSettings())
    # slot — one frozen hash per process (ADR-0003).
    reset_config_hash_for_testing()
    from ne.contracts import freeze_config_hash

    freeze_config_hash(RuntimeSettings())
    with pytest.raises(ConfigHashMismatchError):
        freeze_platform_config()
