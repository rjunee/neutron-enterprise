# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for `ne release push` (NE-071a — the C11 first-party image push-digest
pin). Docker is mocked (CI has no registry to push to); the LIVE path is
exercised by the builder's local kind pre-flight and helm-smoke.yml's
kind-smoke job on the PR (docker push to a local registry, live).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from click.testing import CliRunner

from ne.cli import main

release_module = sys.modules["ne.cli.release"]

_GOOD_DIGEST = "sha256:" + "a" * 64


class _FakeDocker:
    """Dispatches on the docker subcommand; records every call.

    ``repo_digests`` mirrors `docker image inspect --format '{{json
    .RepoDigests}}'` — a JSON list of ``repository@digest`` strings. An image
    ID can legitimately carry MULTIPLE entries (e.g. one from the upstream
    image it was originally pulled/tagged as, one from the just-pushed
    repository) — the fix under test picks the entry matching the pushed
    repo, never a bare ``index 0``.
    """

    def __init__(self, *, repo_digests: list[str] | None, push_stdout: str = "") -> None:
        self.repo_digests = repo_digests
        self.push_stdout = push_stdout
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: object) -> types.SimpleNamespace:
        self.calls.append(list(cmd))
        if cmd[1] == "tag":
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[1] == "push":
            return types.SimpleNamespace(returncode=0, stdout=self.push_stdout, stderr="")
        if cmd[1] == "image":  # docker image inspect
            if self.repo_digests is None:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="no such image")
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps(self.repo_digests), stderr=""
            )
        raise AssertionError(f"unexpected docker subcommand: {cmd}")


def test_push_happy_path_writes_overlay(monkeypatch, tmp_path: Path) -> None:
    # A realistic multi-entry RepoDigests list: the unrelated upstream entry
    # FIRST (index 0) — proves the picker matches by repo, not by position.
    fake = _FakeDocker(
        repo_digests=[
            "docker.io/library/some-base-image@sha256:" + "b" * 64,
            f"localhost:5001/neutron/ne-app@{_GOOD_DIGEST}",
        ]
    )
    monkeypatch.setattr(release_module.subprocess, "run", fake)
    out = tmp_path / "digest.json"

    result = CliRunner().invoke(
        main,
        [
            "release",
            "push",
            "--image",
            "ne-app:0.1.0",
            "--registry",
            "localhost:5001/neutron",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert _GOOD_DIGEST in result.output

    # tag -> push -> inspect sequence.
    subcommands = [c[1] for c in fake.calls]
    assert subcommands == ["tag", "push", "image"]
    assert fake.calls[0] == ["docker", "tag", "ne-app:0.1.0", "localhost:5001/neutron/ne-app:0.1.0"]
    assert fake.calls[1] == ["docker", "push", "localhost:5001/neutron/ne-app:0.1.0"]

    overlay = json.loads(out.read_text(encoding="utf-8"))
    assert overlay == {
        "global": {
            "image": {
                "repository": "localhost:5001/neutron/ne-app",
                "tag": "0.1.0",
                "digest": _GOOD_DIGEST,
            }
        }
    }


def test_push_falls_back_to_push_stdout_digest(monkeypatch, tmp_path: Path) -> None:
    fake = _FakeDocker(
        repo_digests=[],  # nothing usable from inspect
        push_stdout=f"latest: digest: {_GOOD_DIGEST} size: 1234\n",
    )
    monkeypatch.setattr(release_module.subprocess, "run", fake)
    out = tmp_path / "digest.json"

    result = CliRunner().invoke(
        main,
        ["release", "push", "--registry", "localhost:5001/neutron", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    overlay = json.loads(out.read_text(encoding="utf-8"))
    assert overlay["global"]["image"]["digest"] == _GOOD_DIGEST


def test_push_fails_closed_when_no_digest_captured(monkeypatch, tmp_path: Path) -> None:
    fake = _FakeDocker(repo_digests=None, push_stdout="pushed, no digest here")
    monkeypatch.setattr(release_module.subprocess, "run", fake)
    out = tmp_path / "digest.json"

    result = CliRunner().invoke(
        main,
        ["release", "push", "--registry", "localhost:5001/neutron", "--out", str(out)],
    )
    assert result.exit_code != 0
    assert not out.exists()


def test_push_fails_closed_when_no_matching_repo_digest(monkeypatch, tmp_path: Path) -> None:
    # RepoDigests has entries, but NONE match the pushed repo (e.g. push
    # succeeded but the local image metadata never got the new entry) —
    # must fail closed rather than pin the wrong repo's digest.
    fake = _FakeDocker(
        repo_digests=["docker.io/library/some-base-image@sha256:" + "b" * 64],
        push_stdout="no digest line here either",
    )
    monkeypatch.setattr(release_module.subprocess, "run", fake)
    out = tmp_path / "digest.json"

    result = CliRunner().invoke(
        main,
        ["release", "push", "--registry", "localhost:5001/neutron", "--out", str(out)],
    )
    assert result.exit_code != 0
    assert not out.exists()


def test_push_refuses_third_party_image(monkeypatch, tmp_path: Path) -> None:
    fake = _FakeDocker(repo_digests=[f"whatever@{_GOOD_DIGEST}"])
    monkeypatch.setattr(release_module.subprocess, "run", fake)
    out = tmp_path / "digest.json"

    result = CliRunner().invoke(
        main,
        [
            "release",
            "push",
            "--image",
            "postgres:18.6-alpine",
            "--registry",
            "localhost:5001/neutron",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code != 0
    assert "first-party only" in result.output
    assert fake.calls == []  # refused before any docker call


def test_push_normalizes_trailing_slash_registry(monkeypatch, tmp_path: Path) -> None:
    fake = _FakeDocker(repo_digests=[f"localhost:5001/neutron/ne-app@{_GOOD_DIGEST}"])
    monkeypatch.setattr(release_module.subprocess, "run", fake)
    out = tmp_path / "digest.json"

    result = CliRunner().invoke(
        main,
        [
            "release",
            "push",
            "--registry",
            "localhost:5001/neutron/",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake.calls[0][3] == "localhost:5001/neutron/ne-app:0.1.0"
    overlay = json.loads(out.read_text(encoding="utf-8"))
    assert overlay["global"]["image"]["repository"] == "localhost:5001/neutron/ne-app"
