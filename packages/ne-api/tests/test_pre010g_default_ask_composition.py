# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import inspect

from ne.api import app, platform


def test_catalog_and_submission_share_the_complete_canonical_pin() -> None:
    platform_source = inspect.getsource(platform.build_platform)
    app_source = inspect.getsource(app.create_app)
    assert (
        'api_settings.default_ask_generation == "default-ask/canonical-run/v1"' in platform_source
    )
    assert "activate_default_ask_at_boot" in app_source
    assert "compose_default_ask_submission_service" in app_source
    assert "DbosAgentRunEnqueuer" in app_source
