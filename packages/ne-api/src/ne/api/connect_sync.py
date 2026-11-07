# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Governed API bindings for durable connector Sync now requests.

The API binding performs no connector I/O. After the standard executor has run
guardrails and the PDP, it records a short-lived, source-deduplicated request in
the existing scheduler database. The worker's existing cron tick later invokes
the registered connector job's sole tool through the scheduler's scoped governed
executor under the original requester principal.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

import anyio.to_thread

from ne.connect.runner import (
    ConnectSyncSettings,
    SyncNotConfiguredError,
    configured_source_name,
    sync_tool_name,
)
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.scheduler import ManualJobRequestStore
from ne.runtime.tools import ToolBinding

SYNC_REQUEST_TOOL_PREFIX = "write.internal.connect.request_sync_"
_LOG = logging.getLogger(__name__)


def sync_request_tool_name(source: str) -> str:
    """The tier-1, zero-argument request action for one public source alias."""
    return f"{SYNC_REQUEST_TOOL_PREFIX}{source}"


def build_connect_sync_request_bindings(
    settings: ConnectSyncSettings,
    store: ManualJobRequestStore,
    *,
    config_hash: str,
    principal_provider: Callable[[], Principal | None],
) -> tuple[ToolBinding, ...]:
    """Build governed request bindings only for executable pull sources."""
    bindings: list[ToolBinding] = []
    for public_source in settings.sources_to_run:
        try:
            runtime_source = configured_source_name(public_source)
        except SyncNotConfiguredError:
            # The API keeps read-only source/quarantine inspection available, but
            # no request tool exists until authoritative connector settings do.
            _LOG.warning(
                "connector sync request binding omitted for unconfigured source %s",
                public_source,
            )
            continue
        request_action = sync_request_tool_name(public_source)
        sync_action = sync_tool_name(public_source)
        job_name = f"connect-sync-{public_source}"

        async def _handler(
            _args: Mapping[str, Any],
            *,
            public_source: str = public_source,
            runtime_source: str = runtime_source,
            request_action: str = request_action,
            sync_action: str = sync_action,
            job_name: str = job_name,
        ) -> str:
            principal = principal_provider()
            if principal is None:
                raise RuntimeError("governed sync request has no verified principal")
            request = await anyio.to_thread.run_sync(
                functools.partial(
                    store.request,
                    job_name,
                    source=runtime_source,
                    requested_action=request_action,
                    tool_name=sync_action,
                    principal=principal,
                    config_hash=config_hash,
                )
            )
            return json.dumps(
                {
                    "request_id": request.request_id,
                    "source": public_source,
                    "status": request.status,
                    "created": request.created,
                    "created_at": request.created_at.isoformat(),
                    "expires_at": request.expires_at.isoformat(),
                },
                sort_keys=True,
            )

        bindings.append(
            ToolBinding(
                spec=ToolSpec(
                    name=request_action,
                    description=(
                        f"Queue one governed {public_source} connector sync. "
                        "Takes no arguments and returns request metadata only."
                    ),
                    tier=ApprovalTier(1),
                ),
                handler=_handler,
            )
        )
    return tuple(bindings)
