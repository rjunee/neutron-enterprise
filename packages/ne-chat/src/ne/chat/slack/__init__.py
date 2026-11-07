# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Slack-specific implementation of the ChatAdapter contract (NE-063).

Everything under this subpackage is Slack-specific (signature scheme, Block Kit,
Web API client, event/interaction parsing, ingress routes, app manifest). The
channel-agnostic services live at the :mod:`ne.chat` root so Teams (v1.1) reuses
them behind a parallel :mod:`ne.chat.teams` subpackage.
"""

from __future__ import annotations
