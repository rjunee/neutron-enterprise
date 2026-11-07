# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The injected ``claude -p`` author-dispatch seam (C3-governed).

:func:`claude_cli_author_dispatch` builds an :class:`~ne.skills.substrate.AuthorDispatch`
callable (the SAME Protocol the pydantic-ai binding's injected dispatcher
satisfies — deliberate seam reuse) that shells out to the Claude Code CLI as a
subprocess. This module imports NO provider SDK (no ``anthropic``, no
``litellm``) — the whole point is that the CLI subprocess, not this package, is
what ever speaks to a model, and even IT is required to be pointed at
ne-gateway (C3: model access only via the gateway).

``gateway_base_url`` is REQUIRED and non-empty: a bare, provider-locked
subprocess (one that would default to Anthropic's public API) is not permitted
here. The returned dispatcher sets ``ANTHROPIC_BASE_URL`` in the child's
environment to enforce that at the transport level, per design §3.5/§10-Q8.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence

from ne.skills.substrate import AuthorDispatch
from ne.skills_cc.errors import SkillAuthoringDispatchError

_STDERR_TAIL_CHARS = 500


def claude_cli_author_dispatch(
    *,
    gateway_base_url: str,
    command: Sequence[str] = ("claude", "-p"),
    timeout_s: float = 300.0,
) -> AuthorDispatch:
    """Build an ``AuthorDispatch`` that shells out to ``command`` (default ``claude -p``).

    Raises ``ValueError`` immediately (before any process is spawned) when
    ``gateway_base_url`` is empty/whitespace. The returned callable pipes the
    fenced author task to the child's stdin and returns its stdout, decoded as
    UTF-8. A non-zero exit code, a timeout (the process is killed first), or
    empty stdout raises :class:`SkillAuthoringDispatchError`.
    """
    if not gateway_base_url or not gateway_base_url.strip():
        raise ValueError(
            "C3: the CC author subprocess must be pointed at ne-gateway via "
            "ANTHROPIC_BASE_URL; a bare provider-locked subprocess is not permitted here"
        )

    resolved_command = tuple(command)
    child_env = {**os.environ, "ANTHROPIC_BASE_URL": gateway_base_url}

    async def _dispatch(task: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *resolved_command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(task.encode("utf-8")), timeout=timeout_s
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise SkillAuthoringDispatchError(
                f"CC author subprocess timed out after {timeout_s}s: {resolved_command!r}"
            ) from exc

        if process.returncode != 0:
            tail = stderr.decode("utf-8", errors="replace")[-_STDERR_TAIL_CHARS:]
            raise SkillAuthoringDispatchError(
                f"CC author subprocess exited {process.returncode}: ...{tail}"
            )
        if not stdout.strip():
            tail = stderr.decode("utf-8", errors="replace")[-_STDERR_TAIL_CHARS:]
            raise SkillAuthoringDispatchError(
                f"CC author subprocess produced empty stdout: ...{tail}"
            )
        return stdout.decode("utf-8")

    return _dispatch
