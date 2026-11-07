# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CS-USABLE-ADDRESS: full --live-writeback mode admits an exact draft mailbox.

The mailbox becomes ``NE_API_EMAIL_DRAFT_ALLOWED_MAILBOXES`` in the demo
overlay — destination authority for ``draft.email.create``, the composition
root's default mailbox (NE-162 keeps mailbox addresses out of frontier-visible
text, so the model cannot quote one), and a pre-egress redaction exemption.
Omitted, the staging and overlay are byte-identical to before.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
import pytest
import yaml

from ne.cli.demo import live

_SF_ENV: dict[str, str] = {
    "NE_SALESFORCE_AUTH_METHOD": "client_credentials",
    "NE_SALESFORCE_CLIENT_ID": "cid",
    "NE_SALESFORCE_CLIENT_SECRET": "fixture-secret",
    "NE_SALESFORCE_OWNER": "data-eng",
    "NE_SALESFORCE_INSTANCE_URL": "https://acme.my.salesforce.com",
    "NE_SALESFORCE_OBJECTS": '[{"name": "Account", "content_fields": ["Name"]}]',
}
_M365_ENV: dict[str, str] = {
    "NE_M365_AUTH_METHOD": "secret",
    "NE_M365_TENANT_ID": "tid",
    "NE_M365_CLIENT_ID": "cid",
    "NE_M365_CLIENT_SECRET": "fixture-secret",
    "NE_M365_OWNER": "data-eng",
    "NE_M365_SITE_IDS": '["contoso.sharepoint.com,00000000-0000-0000-0000-000000000000"]',
}


def _stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs: object):
    for name in [k for k in os.environ if k.startswith(("NE_SQL_", "NE_SALESFORCE_", "NE_M365_"))]:
        monkeypatch.delenv(name, raising=False)
    for k, v in {**_SF_ENV, **_M365_ENV}.items():
        monkeypatch.setenv(k, v)
    workdir = tmp_path / "wd"
    workdir.mkdir(exist_ok=True)
    return workdir, live._stage_live_writeback(workdir, tmp_path / "manifest", **kwargs)  # type: ignore[arg-type]


def test_full_mode_mailbox_is_normalized_and_reaches_the_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir, staging = _stage(monkeypatch, tmp_path, draft_mailbox="Owner@Contoso.OnMicrosoft.Com")
    assert staging.draft_mailbox == "owner@contoso.onmicrosoft.com"

    seed_dir = live._stage_demo_seed(workdir)
    overlay = yaml.safe_load(
        live._write_demo_overlay(workdir, seed_dir, live_writeback=staging).read_text(
            encoding="utf-8"
        )
    )
    api_env = overlay["services"]["ne-api"]["environment"]
    worker_env = overlay["services"]["ne-worker"]["environment"]
    assert api_env == worker_env
    assert json.loads(api_env["NE_API_EMAIL_DRAFT_ALLOWED_MAILBOXES"]) == [
        "owner@contoso.onmicrosoft.com"
    ]
    # Full mode still selects via the legacy switch, not the tool selector.
    assert api_env["NE_API_WRITE_ACTIONS_ENABLED"] == "true"
    assert "NE_API_WRITE_ACTION_TOOLS" not in api_env


def test_full_mode_without_mailbox_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir, staging = _stage(monkeypatch, tmp_path)
    assert staging.draft_mailbox is None

    seed_dir = live._stage_demo_seed(workdir)
    overlay = yaml.safe_load(
        live._write_demo_overlay(workdir, seed_dir, live_writeback=staging).read_text(
            encoding="utf-8"
        )
    )
    assert (
        "NE_API_EMAIL_DRAFT_ALLOWED_MAILBOXES" not in overlay["services"]["ne-api"]["environment"]
    )


def test_full_mode_rejects_an_invalid_mailbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(click.ClickException, match="not a valid mailbox"):
        _stage(monkeypatch, tmp_path, draft_mailbox="not a mailbox")
