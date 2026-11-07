# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Slack seeder: channel privacy, name_taken reuse, history gating, placement, unseed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from ne.acme_seed.personas import PrincipalsMap
from ne.acme_seed.slack import SeedSlackClient, SlackSeeder
from ne.acme_seed.slice import slice_corpus
from ne.connect.errors import SlackSyncError

from .conftest import build_slack_transport, slack_settings


def _create_bodies(recorder: list[httpx.Request]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for r in recorder:
        if r.url.path.endswith("/conversations.create"):
            body = json.loads(r.content.decode())
            out[body["name"]] = bool(body["is_private"])
    return out


def _posts(recorder: list[httpx.Request]) -> list[tuple[str, str]]:
    """(channel_id, fact_id) for each chat.postMessage."""
    out: list[tuple[str, str]] = []
    for r in recorder:
        if r.url.path.endswith("/chat.postMessage"):
            body = json.loads(r.content.decode())
            text = body["text"]
            marker = text.split("[acme:")[1].rstrip("]")
            out.append((body["channel"], marker))
    return out


def _run_push(recorder: list[httpx.Request], principals: PrincipalsMap, **kwargs):
    transport = build_slack_transport(recorder=recorder, **kwargs)
    seeder = SlackSeeder(slack_settings(), principals, transport=transport)
    return asyncio.run(seeder.push(slice_corpus()))


def test_push_creates_three_channels_with_expected_privacy(principals: PrincipalsMap) -> None:
    recorder: list[httpx.Request] = []
    manifest = _run_push(recorder, principals)
    privacy = _create_bodies(recorder)
    assert privacy == {"acme-general": False, "eng-private": True, "legal-confidential": True}
    assert set(manifest.channels) == {"acme-general", "eng-private", "legal-confidential"}


def test_name_taken_reuses_existing_channel_id(principals: PrincipalsMap) -> None:
    recorder: list[httpx.Request] = []
    manifest = _run_push(
        recorder,
        principals,
        name_taken=frozenset({"acme-general"}),
        existing_channels={"acme-general": "C-EXISTING"},
    )
    # The taken channel resolved to the existing id via conversations.list.
    assert manifest.channels["acme-general"] == "C-EXISTING"
    # Only ONE create attempt for it — no duplicate channel.
    creates = [r for r in recorder if r.url.path.endswith("/conversations.create")]
    assert sum(1 for r in creates if json.loads(r.content.decode())["name"] == "acme-general") == 1


def test_channel_placement_matches_visibility_mapping(principals: PrincipalsMap) -> None:
    recorder: list[httpx.Request] = []
    _run_push(recorder, principals)
    selection = slice_corpus()
    posts = _posts(recorder)
    channel_of = {fid: cid for cid, fid in posts}

    def facts(doc_id: str) -> list[str]:
        return [f.fact_id for f in selection.doc(doc_id).facts[:2]]

    for fid in facts("doc-0011"):
        assert channel_of[fid] == "C-acme-general"
    for fid in facts("doc-0062") + facts("doc-0310"):
        assert channel_of[fid] == "C-eng-private"
    for fid in facts("doc-0000") + facts("doc-0010") + facts("doc-0077"):
        assert channel_of[fid] == "C-legal-confidential"
    # dept-sales doc + named-P3 doc are posted NOWHERE.
    for fid in facts("doc-0061") + facts("doc-0377"):
        assert fid not in channel_of


def test_messages_gate_on_history_markers(principals: PrincipalsMap) -> None:
    selection = slice_corpus()
    public_facts = [
        f.fact_id for doc in selection.docs_by_visibility("public") for f in doc.facts[:2]
    ]
    recorder: list[httpx.Request] = []
    _run_push(
        recorder,
        principals,
        history_markers={"C-acme-general": public_facts},
    )
    posted = {fid for _, fid in _posts(recorder)}
    # Facts already present in history are NOT re-posted...
    assert posted.isdisjoint(public_facts)
    # ...but facts missing from the private channels still are.
    eng_facts = {f.fact_id for f in selection.doc("doc-0062").facts[:2]}
    assert eng_facts <= posted


def test_write_allowlist_refuses_unknown_method(principals: PrincipalsMap) -> None:
    recorder: list[httpx.Request] = []
    transport = build_slack_transport(recorder=recorder)
    client = SeedSlackClient(slack_settings(), transport=transport)

    async def _run() -> None:
        async with client.session() as http:
            await client.call_write(http, "users.admin.invite", {"foo": "bar"})

    with pytest.raises(SlackSyncError):
        asyncio.run(_run())
    assert recorder == []  # refused before any HTTP request


def test_unseed_archives_channels_and_deletes_bot_messages(
    principals: PrincipalsMap, tmp_path: Path
) -> None:
    recorder: list[httpx.Request] = []
    manifest = _run_push(recorder, principals)
    manifest.save(tmp_path)

    recorder2: list[httpx.Request] = []
    transport = build_slack_transport(recorder=recorder2)
    seeder = SlackSeeder(slack_settings(), principals, transport=transport)
    asyncio.run(seeder.unseed(tmp_path))

    deletes = [r for r in recorder2 if r.url.path.endswith("/chat.delete")]
    archives = [r for r in recorder2 if r.url.path.endswith("/conversations.archive")]
    assert len(deletes) == len(manifest.messages)
    assert len(archives) == 3
    # chat.delete precedes conversations.archive (messages gone before archiving).
    first_archive = next(i for i, r in enumerate(recorder2) if r.url.path.endswith("archive"))
    last_delete = max(
        (i for i, r in enumerate(recorder2) if r.url.path.endswith("chat.delete")), default=-1
    )
    assert last_delete < first_archive
