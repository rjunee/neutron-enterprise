# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""S8 — temporal provenance (the version beat), MUTATING + Graph-credentialed.

The loop, exactly as rehearsed live on 2026-08-28 with real Graph edits:

  ask -> Answer A quotes the standard tier's "within N business days"
  -> edit the SOP in SharePoint (flip N: 10 <-> 7) -> Sources sync (delta sees
  exactly one change) -> re-ask THE SAME WORDS -> Answer B quotes the NEW
  number -> the answer ledger stamps each event with ITS OWN document version.

VERSION SEMANTICS THE SUITE MUST HONOUR: versions only go FORWARD; there is no
reset path (lineage is append-only; even an A->B->A revert mints a NEW version
with the old content hash). So every assertion here is on MONOTONICITY and on
BEFORE/AFTER DELTAS — never on absolute version numbers, which grow with every
rehearsal and every scheduled run of this very suite.

Requirements beyond the rest of the suite (skipped loudly when absent):
* NE_E2E_MUTATING=1 — the edit permanently advances the SOP's version history;
* NE_E2E_GRAPH_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET / _DRIVE_ID (and
  optionally NE_E2E_GRAPH_SOP_ITEM_PATH) — an app-only Graph credential with
  Files.ReadWrite.All to perform the live SharePoint edit. Without it the doc
  cannot be edited programmatically and this scenario needs a human (see
  e2e/README.md, "what is not automated").
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
from nelib.answers import standard_tier_business_days
from nelib.client import MERIDIAN_ANALYST, NeutronClient, RunResult
from nelib.flakes import FlakeLedger, run_with_attribution
from nelib.graphedit import GraphEditConfig, flip_standard_tier_days

pytestmark = [pytest.mark.mutating, pytest.mark.frontier]

_QUESTION = (
    "Under the current Debit Card Dispute Handling SOP, within how many business days must "
    "Meridian provide provisional credit for a standard-tier dispute, and for an "
    "expedited-tier dispute?"
)
_SOP_TITLE_FRAGMENT = "Debit Card Dispute Handling SOP"


def _graph_config() -> GraphEditConfig:
    values = {}
    for key in ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET", "DRIVE_ID"):
        value = os.environ.get(f"NE_E2E_GRAPH_{key}", "").strip()
        if not value:
            pytest.skip(
                f"S8 needs NE_E2E_GRAPH_{key} for the live SharePoint edit (app-only Graph "
                "credential with Files.ReadWrite.All). Without it the edit step needs a "
                "human — see e2e/README.md."
            )
        values[key.lower()] = value
    return GraphEditConfig(
        tenant_id=values["tenant_id"],
        client_id=values["client_id"],
        client_secret=values["client_secret"],
        drive_id=values["drive_id"],
        item_path=os.environ.get(
            "NE_E2E_GRAPH_SOP_ITEM_PATH", "Debit Card Dispute Handling SOP.docx"
        ),
    )


def _sop_versions(ledger_view: dict[str, Any]) -> list[int]:
    """The SOP's document_version stamped on each ledger event, oldest-first."""
    versions: list[int] = []
    for event in ledger_view.get("events", []):
        for item in event.get("corpus_stamp", []) or []:
            title = str(item.get("document_title") or "")
            if _SOP_TITLE_FRAGMENT in title:
                versions.append(int(item.get("version") or item.get("document_version") or 0))
                break
    return versions


def _wait_sync_success(ops: NeutronClient, request_id: str, *, deadline_s: float = 300.0) -> dict:
    deadline = time.monotonic() + deadline_s
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = ops.sync_status(request_id)
        if str(status.get("status")) in {"success", "succeeded", "synced", "completed"}:
            return status
        if str(status.get("status")) in {"failed", "error"}:
            pytest.fail(f"S8 sync failed: {status}")
        time.sleep(10.0)
    pytest.fail(f"S8 sync did not complete within {deadline_s:.0f}s: {status}")
    raise AssertionError  # unreachable


@pytest.fixture(scope="module")
def s8(
    ops: NeutronClient,
    warm_box: None,
    mutating_enabled: None,
    flake_ledger: FlakeLedger,
) -> dict[str, Any]:
    del warm_box, mutating_enabled
    config = _graph_config()

    def ask(label: str) -> RunResult:
        return run_with_attribution(
            f"s8-{label}",
            lambda attempt: ops.run_turn(
                MERIDIAN_ANALYST,
                _QUESTION,
                requested_model="frontier-default",
                label=f"s8-{label}-a{attempt}",
            ),
            ledger=flake_ledger,
            max_attempts=2,
            settle_ok=lambda r: r.status == "completed",
        )

    answer_a = ask("answer-a")
    n_before = standard_tier_business_days(answer_a.answer)
    if n_before is None:
        pytest.fail(f"S8 Answer A quoted no parseable standard-tier window: {answer_a.answer!r}")

    # Pin + reask so the ledger holds a BEFORE event stamped with the current
    # version. 409 = already pinned (fine — the ledger keeps one question).
    pin = ops.ledger_pin(_QUESTION)
    assert pin.status_code in {200, 409}, f"ledger pin failed: {pin.status_code} {pin.text}"
    ledger_before = ops.ledger_reask()
    versions_before = _sop_versions(ledger_before)
    if not versions_before:
        pytest.fail(
            "S8: the ledger's before-event carries no version stamp for the SOP — cannot "
            f"prove provenance. Ledger: {str(ledger_before)[:500]}"
        )

    n_after = 7 if n_before != 7 else 10
    flip_standard_tier_days(config, current=n_before, new=n_after)

    sync = ops.request_sync("m365")
    sync_final = _wait_sync_success(ops, str(sync["request_id"]))

    answer_b = ask("answer-b")
    ledger_after = ops.ledger_reask()

    return {
        "answer_a": answer_a,
        "answer_b": answer_b,
        "n_before": n_before,
        "n_after": n_after,
        "versions_before": versions_before,
        "versions_after": _sop_versions(ledger_after),
        "sync_final": sync_final,
    }


def test_s8_answers_quote_the_before_and_after_numbers(s8: dict[str, Any]) -> None:
    answer_b: RunResult = s8["answer_b"]
    quoted = standard_tier_business_days(answer_b.answer)
    assert quoted == s8["n_after"], (
        f"Answer B quotes {quoted} business days; the SOP now says {s8['n_after']} "
        f"(was {s8['n_before']}). Either the sync did not land or the answer is stale. "
        f"Answer B: {answer_b.answer!r}"
    )


def test_s8_sync_saw_the_edit(s8: dict[str, Any]) -> None:
    counts = s8["sync_final"].get("counts") or {}
    written = counts.get("written")
    if written is not None:
        assert int(written) >= 1, (
            f"the delta sync wrote no documents — the edit never landed: {s8['sync_final']}"
        )


def test_s8_ledger_stamps_each_event_with_its_own_version(s8: dict[str, Any]) -> None:
    """The provenance claim itself: the AFTER event is stamped with a STRICTLY
    NEWER SOP version than the BEFORE event. Absolute numbers are never
    asserted — versions only walk forward, forever."""
    before = s8["versions_before"]
    after = s8["versions_after"]
    assert after, "the after-ledger carries no SOP version stamps"
    assert max(after) > max(before), (
        f"the ledger's newest SOP version stamp did not advance across the edit+sync "
        f"(before {before}, after {after}) — supersession was not recorded."
    )
    assert max(before) in after or before[-1] in after, (
        "the BEFORE event's version stamp vanished from the ledger — history must keep "
        f"each answer bound to ITS version (before {before}, after {after})"
    )


def test_s8_versions_are_monotonic(s8: dict[str, Any]) -> None:
    after = s8["versions_after"]
    assert after == sorted(after), (
        f"ledger version stamps regressed within the timeline: {after} — versions only go "
        "forward (append-only lineage)"
    )
