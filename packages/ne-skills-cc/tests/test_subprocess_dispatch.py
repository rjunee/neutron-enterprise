# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The CC author subprocess dispatch: local stub subprocesses only (C2, no network)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from ne.skills_cc.errors import SkillAuthoringDispatchError
from ne.skills_cc.subprocess_dispatch import claude_cli_author_dispatch

_CANNED_MD = "---\nname: canned\ndescription: d\nversion: 1\n---\n## When to Use\nx\n"

_SUCCESS_SCRIPT = (
    "import sys, os\n"
    "sys.stdin.read()\n"
    "with open(sys.argv[1], 'w', encoding='utf-8') as f:\n"
    "    f.write(os.environ.get('ANTHROPIC_BASE_URL', ''))\n"
    f"sys.stdout.write({_CANNED_MD!r})\n"
)

_FAIL_SCRIPT = "import sys\nsys.stdin.read()\nsys.stderr.write('boom-error-tail')\nsys.exit(3)\n"

_EMPTY_STDOUT_SCRIPT = "import sys\nsys.stdin.read()\n"

_SLEEP_SCRIPT = "import time, sys\nsys.stdin.read()\ntime.sleep(10)\n"


def test_success_returns_stdout_and_propagates_base_url(tmp_path: Path) -> None:
    side_channel = tmp_path / "side-channel.txt"
    dispatch = claude_cli_author_dispatch(
        gateway_base_url="http://gateway.test/v1",
        command=(sys.executable, "-c", _SUCCESS_SCRIPT, str(side_channel)),
        timeout_s=10.0,
    )
    result = asyncio.run(dispatch("author this"))
    assert result == _CANNED_MD
    assert side_channel.read_text(encoding="utf-8") == "http://gateway.test/v1"


def test_nonzero_exit_raises_with_stderr_tail() -> None:
    dispatch = claude_cli_author_dispatch(
        gateway_base_url="http://gateway.test/v1",
        command=(sys.executable, "-c", _FAIL_SCRIPT),
        timeout_s=10.0,
    )
    with pytest.raises(SkillAuthoringDispatchError, match="boom-error-tail"):
        asyncio.run(dispatch("author this"))


def test_empty_stdout_raises() -> None:
    dispatch = claude_cli_author_dispatch(
        gateway_base_url="http://gateway.test/v1",
        command=(sys.executable, "-c", _EMPTY_STDOUT_SCRIPT),
        timeout_s=10.0,
    )
    with pytest.raises(SkillAuthoringDispatchError, match="empty stdout"):
        asyncio.run(dispatch("author this"))


def test_timeout_raises_and_does_not_hang() -> None:
    dispatch = claude_cli_author_dispatch(
        gateway_base_url="http://gateway.test/v1",
        command=(sys.executable, "-c", _SLEEP_SCRIPT),
        timeout_s=0.5,
    )
    with pytest.raises(SkillAuthoringDispatchError, match="timed out"):
        asyncio.run(dispatch("author this"))


@pytest.mark.parametrize("bad_url", ["", "   "])
def test_empty_gateway_base_url_raises_before_spawn(bad_url: str) -> None:
    with pytest.raises(ValueError, match="C3"):
        claude_cli_author_dispatch(gateway_base_url=bad_url)
