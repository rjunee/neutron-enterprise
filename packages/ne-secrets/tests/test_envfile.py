# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the .env-file -> SecretStore bridge."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ne.secrets.envfile import import_env_file, parse_env_file
from ne.secrets.file_store import FileSecretStore


def test_parse_env_file_basic_and_export_and_quotes(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "PLAIN=dummy-value",
                "export EXPORTED=another-dummy",
                'DOUBLE_QUOTED="quoted-dummy"',
                "SINGLE_QUOTED='quoted-dummy-2'",
                "",
            ]
        )
    )
    result = parse_env_file(env_path)
    assert result == {
        "PLAIN": "dummy-value",
        "EXPORTED": "another-dummy",
        "DOUBLE_QUOTED": "quoted-dummy",
        "SINGLE_QUOTED": "quoted-dummy-2",
    }


def test_parse_env_file_malformed_line_raises_without_content(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("GOOD=dummy\nNOT_VALID_LINE_NO_EQUALS\n")
    with pytest.raises(ValueError) as excinfo:
        parse_env_file(env_path)
    message = str(excinfo.value)
    assert "line 2" in message
    assert "NOT_VALID_LINE_NO_EQUALS" not in message


def test_import_env_file_writes_one_version(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("KEY_ONE=dummy-one\nKEY_TWO=dummy-two\n")

    store = FileSecretStore(tmp_path / "secrets.json")

    async def scenario():
        return await import_env_file(store, env_path, "app/imported")

    version = asyncio.run(scenario())
    assert version.version == 1
    assert dict(version.data) == {"KEY_ONE": "dummy-one", "KEY_TWO": "dummy-two"}
