# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import inspect

from ne.worker import main


def test_worker_requires_shared_default_ask_pin_and_registers_fixed_queue() -> None:
    source = inspect.getsource(main)
    dormant_guard = source.index("if platform.default_ask_generation:")
    readiness_guard = source.index(
        'platform.default_ask_generation != "default-ask/canonical-run/v1"'
    )
    activation = source.index("activate_default_ask_at_boot")
    assert dormant_guard < readiness_guard < activation
    assert 'platform.default_ask_generation != "default-ask/canonical-run/v1"' in source
    assert 'dbos_mod.Queue("agent-runs"' in source
    assert "activate_default_ask_at_boot" in source
    assert 'import_module("ne.worker.workflows")' in source
