# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The CLI: offline slice output, tenant choice enforcement, push dispatch + manifest."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ne.acme_seed import cli
from ne.acme_seed.manifest import SeedManifest
from ne.acme_seed.slice import SliceSelection

_PINNED = [
    "doc-0000",
    "doc-0010",
    "doc-0011",
    "doc-0012",
    "doc-0017",
    "doc-0021",
    "doc-0027",
    "doc-0061",
    "doc-0062",
    "doc-0077",
    "doc-0310",
    "doc-0377",
]

_CLI_TOKEN = "xoxb-fake-cli-token-not-real"


def test_cli_slice_prints_pinned_ids(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["slice"])
    assert rc == 0
    out = capsys.readouterr().out
    for doc_id in _PINNED:
        assert doc_id in out


def test_cli_rejects_unknown_tenant() -> None:
    with pytest.raises(SystemExit):
        cli.main(["push", "--tenant", "dropbox"])


def test_cli_push_dispatches_to_seeder_and_writes_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: dict[str, object] = {}

    class FakeSlackSeeder:
        def __init__(
            self, settings: object, principals: object, *, transport: object = None
        ) -> None:
            received["settings"] = settings
            received["principals"] = principals

        async def push(self, selection: SliceSelection) -> SeedManifest:
            received["selection"] = selection
            return SeedManifest(
                tenant="slack",
                seeded_at=datetime.now(UTC).isoformat(),
                corpus_version=selection.corpus_version,
                channels={"acme-general": "C-1"},
                messages={"doc-0011-f0": {"channel": "C-1", "ts": "ts-1"}},
            )

    monkeypatch.setenv("NE_SLACK_BOT_TOKEN", _CLI_TOKEN)
    monkeypatch.setattr(cli, "SlackSeeder", FakeSlackSeeder)

    principals_path = tmp_path / "principals.json"
    principals_path.write_text(
        json.dumps({"slack": {"users": {"per-0006": "U6"}}}), encoding="utf-8"
    )
    manifest_dir = tmp_path / "manifests"

    rc = cli.main(
        [
            "push",
            "--tenant",
            "slack",
            "--manifest-dir",
            str(manifest_dir),
            "--principals",
            str(principals_path),
        ]
    )
    assert rc == 0

    # The seeder received a real slice + the constructed settings.
    assert isinstance(received["selection"], SliceSelection)
    assert received["settings"] is not None

    # Manifest written to the requested dir.
    written = SeedManifest.load(manifest_dir, "slack")
    assert written.channels == {"acme-general": "C-1"}

    # Output carries counts but NEVER the bot token (C12).
    out = capsys.readouterr().out
    assert "1 channels" in out
    assert _CLI_TOKEN not in out
