# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""M365 seeder: driveItem uploads, ACL-invite mapping, idempotency, unseed."""

from __future__ import annotations

import asyncio
import json

import httpx

from ne.acme_seed.m365 import M365Seeder
from ne.acme_seed.personas import PrincipalsMap
from ne.acme_seed.slice import slice_corpus

from .conftest import build_m365_transport, m365_settings

_GROUPS = {
    "executive": "g-exec-id",
    "dept-engineering": "g-eng-id",
    "dept-sales": "g-sales-id",
    "dept-legal": "g-legal-id",
}
_USERS = {
    "per-0000": "u-exec@acme.example",
    "per-0006": "u-eng@acme.example",
    "per-0016": "u-sales@acme.example",
    "per-0038": "u-legal@acme.example",
}


def _puts(recorder: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in recorder if r.method == "PUT"]


def _invites(recorder: list[httpx.Request]) -> dict[str, set[str]]:
    """item_id -> set of granted objectIds."""
    out: dict[str, set[str]] = {}
    for r in recorder:
        if r.method == "POST" and r.url.path.endswith("/invite"):
            item = r.url.path.split("/drive/items/")[1].split("/invite")[0]
            body = json.loads(r.content.decode())
            out[item] = {rec["objectId"] for rec in body["recipients"]}
    return out


def _run_push(recorder: list[httpx.Request], principals: PrincipalsMap):
    transport = build_m365_transport(recorder=recorder)
    seeder = M365Seeder(m365_settings(), principals, transport=transport)
    return asyncio.run(seeder.push(slice_corpus()))


def test_push_uploads_each_doc_as_markdown_under_acme_folder(principals: PrincipalsMap) -> None:
    recorder: list[httpx.Request] = []
    manifest = _run_push(recorder, principals)
    selection = slice_corpus()
    content_by_doc = {d.doc_id: d.content for d in selection.docs}

    puts = _puts(recorder)
    assert len(puts) == 12
    for put in puts:
        assert "/sites/" in put.url.path
        assert "/drive/root:/Acme/" in str(put.url)
        assert str(put.url).endswith(".md:/content")
        assert put.headers["Content-Type"] == "text/markdown"
        doc_id = str(put.url).split("/Acme/")[1].split(".md")[0]
        assert put.content.decode("utf-8") == content_by_doc[doc_id]
    assert set(manifest.docs) == set(content_by_doc)


def test_push_folder_conflict_is_treated_as_success(principals: PrincipalsMap) -> None:
    recorder: list[httpx.Request] = []
    transport = build_m365_transport(recorder=recorder, folder_conflict=True)
    seeder = M365Seeder(m365_settings(), principals, transport=transport)
    manifest = asyncio.run(seeder.push(slice_corpus()))
    # The 409 on folder-create did not abort — all 12 docs still uploaded.
    assert len(manifest.docs) == 12


def test_push_grants_match_acl_mapping(principals: PrincipalsMap) -> None:
    recorder: list[httpx.Request] = []
    _run_push(recorder, principals)
    grants = _invites(recorder)

    def granted(doc_id: str) -> set[str]:
        return grants[f"item-{doc_id}"]

    # public -> all 4 groups.
    assert granted("doc-0011") == set(_GROUPS.values())
    # department -> {its dept group, executive}.
    assert granted("doc-0062") == {_GROUPS["dept-engineering"], _GROUPS["executive"]}
    assert granted("doc-0061") == {_GROUPS["dept-sales"], _GROUPS["executive"]}
    assert granted("doc-0000") == {_GROUPS["dept-legal"], _GROUPS["executive"]}
    # exec_only -> {executive}.
    assert granted("doc-0010") == {_GROUPS["executive"]}
    # named -> ONLY the persona user id.
    assert granted("doc-0310") == {_USERS["per-0006"]}
    assert granted("doc-0377") == {_USERS["per-0016"]}
    assert granted("doc-0077") == {_USERS["per-0038"]}


def test_named_doc_never_grants_executive_or_p1(principals: PrincipalsMap) -> None:
    recorder: list[httpx.Request] = []
    _run_push(recorder, principals)
    grants = _invites(recorder)
    for doc_id in ("doc-0310", "doc-0377", "doc-0077"):
        recipients = grants[f"item-{doc_id}"]
        assert _GROUPS["executive"] not in recipients
        assert _USERS["per-0000"] not in recipients


def test_push_is_idempotent_second_run_replaces_not_duplicates(principals: PrincipalsMap) -> None:
    recorder: list[httpx.Request] = []
    transport = build_m365_transport(recorder=recorder)
    seeder = M365Seeder(m365_settings(), principals, transport=transport)
    first = asyncio.run(seeder.push(slice_corpus()))
    second = asyncio.run(seeder.push(slice_corpus()))

    # Same live item ids both runs (path-addressed PUT replace, not a new create).
    assert first.docs == second.docs
    # 12 doc uploads per run, all PUT (replace) — never a POST-create of a doc.
    assert len(_puts(recorder)) == 24
    # The only POSTs are token / folder-create (/children) / ACL invite — no
    # POST that creates a document item (a re-run does not duplicate docs).
    stray_posts = [
        r
        for r in recorder
        if r.method == "POST"
        and not r.url.path.endswith("/oauth2/v2.0/token")
        and not r.url.path.endswith("/children")
        and not r.url.path.endswith("/invite")
    ]
    assert stray_posts == []


def test_unseed_deletes_acme_folder(principals: PrincipalsMap) -> None:
    recorder: list[httpx.Request] = []
    transport = build_m365_transport(recorder=recorder)
    seeder = M365Seeder(m365_settings(), principals, transport=transport)
    asyncio.run(seeder.unseed())
    deletes = [r for r in recorder if r.method == "DELETE"]
    assert len(deletes) == 1
    assert str(deletes[0].url).endswith("/drive/root:/Acme:")
