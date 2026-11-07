# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic proof for ``ne release required-ci-receipt``."""

from __future__ import annotations

import copy
import hashlib
import json
import stat
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from ne.cli import main
from ne.cli.release import build_required_ci_receipt

release_module = sys.modules["ne.cli.release"]

_SHA = "a" * 40
_OTHER_SHA = "b" * 40
_ACTIONS_APP = 15368
_OBSERVATION_CUTOFF = datetime(2026, 8, 23, 11, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fixed_observation_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_module, "_utc_now", lambda: _OBSERVATION_CUTOFF)


def _main_ref(sha: str = _SHA) -> dict[str, object]:
    return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": sha}}


def _required_rule(*contexts: str, integration_id: int = _ACTIONS_APP) -> dict[str, object]:
    return {
        "type": "required_status_checks",
        "parameters": {
            "required_status_checks": [
                {"context": context, "integration_id": integration_id} for context in contexts
            ]
        },
    }


def _run(
    run_id: int,
    name: str,
    *,
    head_sha: str = _SHA,
    app_id: int = _ACTIONS_APP,
    app_slug: str = "github-actions",
    status: str = "completed",
    conclusion: str | None = "success",
    started_at: str = "2026-08-23T10:00:00Z",
    completed_at: str = "2026-08-23T10:01:00Z",
) -> dict[str, object]:
    return {
        "id": run_id,
        "name": name,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "started_at": started_at,
        "completed_at": completed_at if status == "completed" else None,
        "app": {"id": app_id, "slug": app_slug},
    }


def _check_pages(runs: list[dict[str, object]], *, total: int | None = None) -> list[object]:
    return [{"total_count": len(runs) if total is None else total, "check_runs": runs}]


class _GitHubStub:
    def __init__(
        self,
        *,
        rules_pages: list[object] | None = None,
        check_pages: list[object] | None = None,
        final_rules_pages: list[object] | None = None,
        final_check_pages: list[object] | None = None,
        initial_main_sha: str = _SHA,
        final_main_sha: str = _SHA,
    ) -> None:
        self.rules_pages = rules_pages or [[_required_rule("check")]]
        self.check_pages = check_pages or _check_pages([_run(10, "check")])
        self.final_rules_pages = (
            self.rules_pages if final_rules_pages is None else final_rules_pages
        )
        self.final_check_pages = (
            self.check_pages if final_check_pages is None else final_check_pages
        )
        self.initial_main_sha = initial_main_sha
        self.final_main_sha = final_main_sha
        self.calls: list[tuple[str, bool]] = []
        self.ref_reads = 0
        self.rules_reads = 0
        self.check_reads = 0

    def __call__(self, endpoint: str, paginate: bool) -> object:
        self.calls.append((endpoint, paginate))
        if endpoint.endswith("/git/ref/heads/main"):
            self.ref_reads += 1
            sha = self.initial_main_sha if self.ref_reads == 1 else self.final_main_sha
            return copy.deepcopy(_main_ref(sha))
        if "/rules/branches/main?" in endpoint:
            self.rules_reads += 1
            pages = self.rules_pages if self.rules_reads == 1 else self.final_rules_pages
            return copy.deepcopy(pages)
        if f"/commits/{_SHA}/check-runs?" in endpoint:
            self.check_reads += 1
            pages = self.check_pages if self.check_reads == 1 else self.final_check_pages
            return copy.deepcopy(pages)
        raise AssertionError(f"unexpected GitHub endpoint: {endpoint}")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def test_receipt_binds_paginated_effective_rules_to_latest_exact_runs() -> None:
    rules = [
        [_required_rule("frontend", "check")],
        [
            {"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
            _required_rule("typecheck", "vuln-scan", "check"),
        ],
    ]
    runs = [
        _run(10, "check", conclusion="failure"),
        _run(11, "check", started_at="2026-08-23T10:00:30Z"),
        # Same context, much newer id, wrong app: it cannot replace the exact
        # app-bound required check.
        _run(999, "check", app_id=999, app_slug="foreign-checks"),
        _run(20, "frontend"),
        _run(30, "typecheck"),
        _run(40, "vuln-scan"),
        _run(50, "advisory"),
    ]
    pages: list[object] = [
        {"total_count": len(runs), "check_runs": runs[:3]},
        {"total_count": len(runs), "check_runs": runs[3:]},
    ]
    github = _GitHubStub(rules_pages=rules, check_pages=pages)

    receipt = build_required_ci_receipt("acme/neutron", _SHA, fetch=github)
    repeated = build_required_ci_receipt(
        "acme/neutron", _SHA, fetch=_GitHubStub(rules_pages=rules, check_pages=pages)
    )

    assert receipt == repeated
    assert receipt["schema"] == "neutron.enterprise.required-ci-receipt/v1"
    assert receipt["subject_sha"] == _SHA
    bindings = receipt["required_checks"]
    assert isinstance(bindings, list)
    assert [binding["required"] for binding in bindings] == [
        {"context": "check", "integration_id": _ACTIONS_APP},
        {"context": "frontend", "integration_id": _ACTIONS_APP},
        {"context": "typecheck", "integration_id": _ACTIONS_APP},
        {"context": "vuln-scan", "integration_id": _ACTIONS_APP},
    ]
    assert bindings[0]["check_run"] == {
        "id": 11,
        "name": "check",
        "head_sha": _SHA,
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-08-23T10:00:30Z",
        "completed_at": "2026-08-23T10:01:00Z",
        "app": {"id": _ACTIONS_APP, "slug": "github-actions"},
    }
    unsigned = dict(receipt)
    receipt_sha = unsigned.pop("receipt_sha256")
    assert receipt_sha == _digest(unsigned)
    assert receipt["response_sha256"] == _digest(
        {
            "main_ref_before": _main_ref(),
            "effective_rules_pages_before": rules,
            "check_run_pages_before": pages,
            "effective_rules_pages_after": rules,
            "check_run_pages_after": pages,
            "main_ref_after": _main_ref(),
        }
    )
    assert github.calls == [
        ("repos/acme/neutron/git/ref/heads/main", False),
        ("repos/acme/neutron/rules/branches/main?per_page=100", True),
        (f"repos/acme/neutron/commits/{_SHA}/check-runs?filter=all&per_page=100", True),
        ("repos/acme/neutron/rules/branches/main?per_page=100", True),
        (f"repos/acme/neutron/commits/{_SHA}/check-runs?filter=all&per_page=100", True),
        ("repos/acme/neutron/git/ref/heads/main", False),
    ]


def test_receipt_rejects_effective_rules_drift_during_read() -> None:
    github = _GitHubStub(
        rules_pages=[[_required_rule("check")]],
        final_rules_pages=[[_required_rule("check", "typecheck")]],
    )

    with pytest.raises(ValueError, match="required status checks changed"):
        build_required_ci_receipt("acme/neutron", _SHA, fetch=github)


def test_receipt_rejects_a_newer_same_head_rerun_during_read() -> None:
    github = _GitHubStub(
        check_pages=_check_pages([_run(10, "check")]),
        final_check_pages=_check_pages(
            [
                _run(10, "check"),
                _run(11, "check", started_at="2026-08-23T10:00:30Z"),
            ]
        ),
    )

    with pytest.raises(ValueError, match="selected required check runs changed"):
        build_required_ci_receipt("acme/neutron", _SHA, fetch=github)


@pytest.mark.parametrize(
    ("started_at", "completed_at", "message"),
    [
        (
            "2026-08-23T11:00:01Z",
            "2026-08-23T11:00:02Z",
            "started after receipt observation cutoff",
        ),
        (
            "2026-08-23T10:00:00Z",
            "2026-08-23T11:00:01Z",
            "completed after receipt observation cutoff",
        ),
    ],
)
def test_required_run_timestamps_cannot_postdate_receipt_observation(
    started_at: str, completed_at: str, message: str
) -> None:
    github = _GitHubStub(
        check_pages=_check_pages(
            [_run(10, "check", started_at=started_at, completed_at=completed_at)]
        )
    )

    with pytest.raises(ValueError, match=message):
        build_required_ci_receipt("acme/neutron", _SHA, fetch=github)


def test_command_atomically_replaces_with_canonical_owner_only_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github = _GitHubStub()
    monkeypatch.setattr(release_module, "_github_api", github)
    out = tmp_path / "evidence" / "required-ci.json"
    out.parent.mkdir()
    out.write_text("old world-readable bytes", encoding="utf-8")
    out.chmod(0o644)

    result = CliRunner().invoke(
        main,
        [
            "release",
            "required-ci-receipt",
            "--repo",
            "acme/neutron",
            "--sha",
            _SHA,
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    receipt = json.loads(out.read_text(encoding="utf-8"))
    assert out.read_bytes() == _canonical(receipt) + b"\n"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert list(out.parent.glob(f".{out.name}.*.tmp")) == []
    assert str(receipt["receipt_sha256"]) in result.output
    serialized = out.read_text(encoding="utf-8")
    assert "token" not in serialized.lower()
    assert "authorization" not in serialized.lower()


def test_atomic_replace_failure_preserves_prior_receipt_and_removes_temporary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "required-ci.json"
    prior = b"prior receipt bytes\n"
    out.write_bytes(prior)
    out.chmod(0o640)

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("replace sentinel")

    monkeypatch.setattr(release_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace sentinel"):
        release_module._write_owner_only_atomic(out, {"schema": "test-receipt"})

    assert out.read_bytes() == prior
    assert stat.S_IMODE(out.stat().st_mode) == 0o640
    assert list(out.parent.glob(f".{out.name}.*.tmp")) == []


def test_github_provider_enforces_slurped_pagination_without_token_argv(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> types.SimpleNamespace:
        calls.append(command)
        return types.SimpleNamespace(returncode=0, stdout='[[{"type":"pull_request"}]]')

    monkeypatch.setenv("GH_TOKEN", "super-secret")
    monkeypatch.setattr(release_module.subprocess, "run", fake_run)

    assert release_module._github_api("repos/acme/neutron/rules/branches/main", True) == [
        [{"type": "pull_request"}]
    ]
    command = calls[0]
    assert "--paginate" in command
    assert "--slurp" in command
    assert "super-secret" not in command


@pytest.mark.parametrize(
    ("status", "conclusion", "message"),
    [
        ("in_progress", None, "incomplete"),
        ("completed", "cancelled", "cancelled"),
        ("completed", "skipped", "skipped"),
        ("completed", "neutral", "neutral"),
        ("completed", "stale", "stale"),
    ],
)
def test_latest_required_run_must_be_completed_success(
    status: str, conclusion: str | None, message: str
) -> None:
    github = _GitHubStub(
        check_pages=_check_pages(
            [
                _run(10, "check"),
                _run(
                    11,
                    "check",
                    status=status,
                    conclusion=conclusion,
                    started_at="2026-08-23T10:00:30Z",
                ),
            ]
        )
    )

    with pytest.raises(ValueError, match=message):
        build_required_ci_receipt("acme/neutron", _SHA, fetch=github)


def test_required_context_cannot_be_omitted() -> None:
    github = _GitHubStub(
        rules_pages=[[_required_rule("check", "typecheck")]],
        check_pages=_check_pages([_run(10, "check")]),
    )

    with pytest.raises(ValueError, match="missing required check run 'typecheck'"):
        build_required_ci_receipt("acme/neutron", _SHA, fetch=github)


def test_required_context_cannot_join_to_wrong_app() -> None:
    github = _GitHubStub(
        check_pages=_check_pages([_run(10, "check", app_id=999, app_slug="foreign-checks")])
    )

    with pytest.raises(ValueError, match="integration_id 15368"):
        build_required_ci_receipt("acme/neutron", _SHA, fetch=github)


def test_exact_sha_join_rejects_wrong_head() -> None:
    github = _GitHubStub(check_pages=_check_pages([_run(10, "check", head_sha=_OTHER_SHA)]))

    with pytest.raises(ValueError, match="wrong SHA"):
        build_required_ci_receipt("acme/neutron", _SHA, fetch=github)


def test_initial_main_ref_must_already_equal_subject() -> None:
    github = _GitHubStub(initial_main_sha=_OTHER_SHA, final_main_sha=_SHA)

    with pytest.raises(ValueError, match="not the exact final main SHA"):
        build_required_ci_receipt("acme/neutron", _SHA, fetch=github)
    assert github.calls == [("repos/acme/neutron/git/ref/heads/main", False)]


def test_main_movement_rejects_stale_subject() -> None:
    github = _GitHubStub(final_main_sha=_OTHER_SHA)

    with pytest.raises(ValueError, match="not the exact final main SHA"):
        build_required_ci_receipt("acme/neutron", _SHA, fetch=github)


def test_duplicate_check_run_id_is_ambiguous() -> None:
    github = _GitHubStub(check_pages=_check_pages([_run(10, "check"), _run(10, "check")]))

    with pytest.raises(ValueError, match=r"ambiguous.*duplicate check-run id 10"):
        build_required_ci_receipt("acme/neutron", _SHA, fetch=github)


def test_tied_latest_check_runs_are_ambiguous() -> None:
    github = _GitHubStub(check_pages=_check_pages([_run(10, "check"), _run(11, "check")]))

    with pytest.raises(ValueError, match=r"ambiguous latest required check.*share started_at"):
        build_required_ci_receipt("acme/neutron", _SHA, fetch=github)


def test_check_run_pagination_omission_is_rejected() -> None:
    github = _GitHubStub(check_pages=_check_pages([_run(10, "check")], total=2))

    with pytest.raises(ValueError, match="pagination is incomplete"):
        build_required_ci_receipt("acme/neutron", _SHA, fetch=github)


@pytest.mark.parametrize(
    "github",
    [
        _GitHubStub(rules_pages=[[_required_rule("check", integration_id=0)]]),
        _GitHubStub(check_pages=_check_pages([{**_run(10, "check"), "app": None}])),
        _GitHubStub(check_pages=_check_pages([{**_run(10, "check"), "status": "mystery"}])),
    ],
)
def test_malformed_authority_responses_fail_closed(github: _GitHubStub) -> None:
    with pytest.raises(ValueError, match="malformed"):
        build_required_ci_receipt("acme/neutron", _SHA, fetch=github)


def test_invalid_inputs_fail_before_provider_read() -> None:
    github = _GitHubStub()

    with pytest.raises(ValueError, match="OWNER/REPO"):
        build_required_ci_receipt("acme/neutron/extra", _SHA, fetch=github)
    with pytest.raises(ValueError, match="40 lowercase"):
        build_required_ci_receipt("acme/neutron", _SHA.upper(), fetch=github)
    assert github.calls == []
