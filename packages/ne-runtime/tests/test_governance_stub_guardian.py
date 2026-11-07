# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE GOVERNANCE-STUB GUARDIAN — stubbing a chokepoint must be VISIBLE.

Why this file exists. On 2026-08-28 an approve path that returned HTTP 200 and
then stranded every parked run shipped with GREEN CI, and so did a second defect
that made NO parked run terminalizable on ANY outcome — not even Deny. Neither
was exotic. Both were invisible for one reason:

* ``packages/ne-api/tests/test_approvals_api.py`` assigned a fake over
  ``state.gate._run_approval_checkpoints`` and set ``state.source_guard`` to a
  bare ``SimpleNamespace``, so the whole guard/expiry chain was replaced by a
  call-order list; and
* ``test_run_approval_decision_end_to_end.py::_park`` called
  ``RunStore.record_audit_receipt`` directly, hand-writing the row that
  production never wrote.

Neither line looked alarming in review. That is the actual problem: a
governance stub reads exactly like ordinary test plumbing. This guardian makes
it read like what it is.

THE RULE

  A test may not replace a governance chokepoint, and may not hand-write a row a
  chokepoint is supposed to produce, unless the line carries an explicit waiver
  comment naming a reason:

      state.source_guard = _Guard()  # governance-stub: <reason>

  The marker is the point. It is not a suppression: it is the word "governance"
  appearing in the diff, so a reviewer is asked the question that nobody was
  asked on 2026-08-28 — *what defect can now ship green?*

Files listed in :data:`_KNOWN_DEBT` are the audit's pre-existing offenders
(2026-08-28 stub audit). They are a WORK BOARD, not an exemption: the list may
only shrink. Adding a file to it is a deliberate, reviewable act. The guardian
also fails if an entry stops offending, so cleaning one up forces its removal.

SCOPE, stated honestly. This scan catches the two shapes that actually caused
the 2026-08-28 outage — assigning over a chokepoint attribute, and calling a
governance-row writer from test setup — plus the worker-platform swap. It does
NOT see a chokepoint faked through a constructor argument (``_NullSink()``,
``ScriptedPdp()``, ``source_guard=object()``). It is a RATCHET, not a proof of
absence: the 2026-08-28 stub audit is the map, this file is the brake.

Where the un-stubbed pattern is written down: ``packages/ne-api/tests/
run_approval_park.py`` (the real park leg) plus its two consumers,
``test_run_approval_decision_end_to_end.py`` and
``test_run_approval_resume_continuation.py``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The attribute names that ARE the governed spine. Assigning over one of these
#: on any object replaces a chokepoint.
_CHOKEPOINT_ATTRIBUTES = frozenset(
    {
        "_run_approval_checkpoints",  # the run-approval checkpoint coordinator
        "source_guard",  # ProtectedSourceResultGuard
        "_pdp",  # the policy decision point inside the executor
        "_guardrails",  # the guardrail engine inside the executor
        "worker_platform",  # the worker's whole composed governance platform
    }
)

#: Methods a TEST must not call directly, because production is supposed to be
#: the thing that calls them. Hand-writing their rows is what made the
#: undrained-approval-outbox defect invisible.
_FABRICATED_ROW_CALLS = frozenset({"record_audit_receipt"})

#: The receipt writer's OWN tests. Calling ``record_audit_receipt`` there is the
#: point of the file, not a fabrication.
_RECEIPT_WRITER_TESTS = frozenset(
    {
        "packages/ne-runtime/tests/test_run_store.py",
        "packages/ne-runtime/tests/test_run_store_postgres.py",
    }
)

#: Attribute assignments that only count when the target reads as the composed
#: application/platform state — ``state.executor = ...`` replaces the whole
#: GovernedToolExecutor, while ``self.executor = ...`` inside a test double does
#: not.
#: ``sink`` is deliberately ABSENT: every state-level sink assignment found in
#: the 2026-08-28 audit was fault injection into the REAL sink (proving
#: fail-closed), which is a pattern that reveals rather than hides. The audit's
#: sink concern is the ``_NullSink`` constructor argument (M15), a shape this
#: scan does not see - see the audit, not this list, for that work.
_STATE_SCOPED_ATTRIBUTES = frozenset({"executor", "gate", "model_selection"})
_STATE_RECEIVERS = frozenset({"state", "platform", "app_state", "ctx"})

_WAIVER = "governance-stub:"

#: Pre-existing offenders from the 2026-08-28 stub audit, keyed to their audit
#: id. THIS LIST MAY ONLY SHRINK. See the audit for what each one hides.
_KNOWN_DEBT: dict[str, str] = {
    "packages/ne-api/tests/test_run_approval_decision_end_to_end.py": (
        "DECLARED ON SALVAGE, 2026-08-29 — this file was one of the three this "
        "guardian was BORN from and it was repaired on ne/test-debt-ungate-stubs, "
        "but U2's later rewrite (on the reconciliation train, which supersedes that "
        "branch) reintroduced the pattern: it hand-writes two record_audit_receipt "
        "rows with a fabricated audit_record_hash and config_hash='1'*64 instead of "
        "letting the real path write them. Recorded as debt rather than waived, and "
        "rather than blocking the guardian itself from landing -- the ratchet in "
        "test_known_debt_list_only_shrinks means this entry can only be deleted, "
        "never joined by a new one. Repairing it is real work against a careful U2 "
        "test and is tracked as its own backlog item; it is NOT done here, and this "
        "comment exists so nobody reads a green guardian as meaning it was."
    ),
    "packages/ne-api/tests/test_run_approval_resume_continuation.py": (
        "DECLARED ON SALVAGE, 2026-08-29 — the other file this guardian was born "
        "from and repaired on the branch; U1's later rewrite on the reconciliation "
        "train monkeypatches `worker_platform` (line ~662) and hand-writes a "
        "record_audit_receipt row (line ~1535). Debt, not clean, not waived."
    ),
    "packages/ne-runtime/tests/test_agentic_ladder_parks_canonical_approval.py": (
        "DECLARED ON SALVAGE, 2026-08-29 — U3's un-stubbed ladder test, added AFTER "
        "this guardian was written, still assigns over `._run_approval_checkpoints` "
        "(line ~288). Worth stating plainly because this same test is cited as "
        "mutation evidence on PR #932: what it genuinely proves is that the real "
        "planner drives a real park through a >=2-read ladder, which it does. That "
        "claim stands. It is ALSO not free of the pattern this guardian scans for, "
        "and both things are true at once."
    ),
    "packages/ne-api/tests/test_approval_payload_api.py": (
        "H1 — fakes checkpoints + guard + run store, so the whole "
        "_refuse_expired_authorization chain is unreachable over HTTP"
    ),
    "packages/ne-worker/tests/test_workflows_passthrough.py": (
        "H3 — fakes the worker platform's gate/guard, so the worker's own "
        "approval-resume and terminalize_authorization_expired branch is simulated"
    ),
    "packages/ne-runtime/tests/test_middleware_with_durable_gate.py": (
        "H4 — fakes create_wait, so the approval audit outbox write is never exercised"
    ),
    "packages/ne-api/tests/test_authority_attempts_api.py": (
        "H5 — replaces state.executor for the authority-retry endpoint"
    ),
    "packages/ne-api/tests/test_workflows_api.py": (
        "H6 — what THIS scan sees is four PDP/guardrail swaps inside the real "
        "executor (the _Deny*/_Block* deny-injections at ~2144/2316/2522/2536). "
        "The audit's H6 finding proper is WorkflowContext(gate=object(), "
        "source_guard=_Guard()) — a constructor argument this scan cannot see — "
        "so a withholding guard is never exercised through the workflow seam. "
        "Removing the four setattr lines makes this entry stale: delete it in "
        "the same change, and track the constructor half in the audit."
    ),
    "packages/ne-api/tests/test_inference_lifecycle_api.py": (
        "M10 — replaces the PDP inside the real executor"
    ),
    "packages/ne-api/tests/test_chat_model_selection.py": (
        "M14 — swaps the composed model-authority service post-boot"
    ),
    "packages/ne-api/tests/test_chat_multi_turn.py": "M6 — hand-writes run audit receipts",
    "packages/ne-api/tests/test_search_claim_citation_end_to_end.py": (
        "M6 — hand-writes run audit receipts"
    ),
    "packages/ne-api/tests/test_run_output_preview.py": "M6 — hand-writes run audit receipts",
    "packages/ne-runtime/tests/test_run_finalizer.py": "M6 — hand-writes run audit receipts",
    "packages/ne-runtime/tests/test_model_continuity.py": "M6 — hand-writes run audit receipts",
}


@dataclass(frozen=True)
class _Offence:
    path: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.what}"


def _test_files() -> Iterator[Path]:
    for pattern in ("packages/*/tests/**/*.py", "tools/*/tests/**/*.py"):
        yield from sorted(_REPO_ROOT.glob(pattern))


def _is_state_receiver(node: ast.expr) -> bool:
    """``state.executor`` / ``h.state.executor`` — but never ``self.executor``."""

    if isinstance(node, ast.Name):
        return node.id in _STATE_RECEIVERS
    if isinstance(node, ast.Attribute):
        return node.attr in _STATE_RECEIVERS
    if isinstance(node, ast.Call):
        # get_state(h.app).executor = ...
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return "state" in str(name)
    return False


def _scan(path: Path, source: str) -> list[_Offence]:
    waived = {index + 1 for index, line in enumerate(source.splitlines()) if _WAIVER in line}
    tree = ast.parse(source, filename=str(path))
    rel = path.relative_to(_REPO_ROOT).as_posix()
    offences: list[_Offence] = []

    def _record(node: ast.AST, what: str) -> None:
        line = getattr(node, "lineno", 0)
        if line in waived:
            return
        offences.append(_Offence(rel, line, what))

    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if not isinstance(target, ast.Attribute):
                continue
            if target.attr in _CHOKEPOINT_ATTRIBUTES:
                _record(node, f"assigns over the governance chokepoint `.{target.attr}`")
            elif target.attr in _STATE_SCOPED_ATTRIBUTES and _is_state_receiver(target.value):
                _record(
                    node,
                    f"replaces the composed `.{target.attr}` on application state",
                )
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _FABRICATED_ROW_CALLS
                and rel not in _RECEIPT_WRITER_TESTS
            ):
                _record(
                    node,
                    f"calls `.{func.attr}(...)` directly — a governance row that "
                    "production is supposed to write",
                )
            # setattr(state, "source_guard", ...) / monkeypatch.setattr(x, "_pdp", ...)
            if isinstance(func, ast.Attribute) and func.attr == "setattr":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and arg.value in (
                        _CHOKEPOINT_ATTRIBUTES | _STATE_SCOPED_ATTRIBUTES
                    ):
                        _record(node, f"monkeypatches the chokepoint `{arg.value}`")
    return offences


def _offending_files() -> dict[str, list[_Offence]]:
    found: dict[str, list[_Offence]] = {}
    for path in _test_files():
        if path.name == Path(__file__).name:
            continue
        offences = _scan(path, path.read_text(encoding="utf-8"))
        if offences:
            found[path.relative_to(_REPO_ROOT).as_posix()] = offences
    return found


def test_no_new_governance_chokepoint_stubs() -> None:
    """No test may replace a governance chokepoint without saying so out loud.

    If this fails on a file you just wrote: do not add it to ``_KNOWN_DEBT``.
    Drive the real chain instead — ``packages/ne-api/tests/run_approval_park.py``
    is the worked example. If the stub is genuinely a boundary double (a vendor
    SDK, a clock, the DBOS enqueue transport), say so on the line:

        state.agent_run_enqueuer = _Recorder()  # governance-stub: DBOS transport edge
    """

    offending = _offending_files()
    new = {path: hits for path, hits in offending.items() if path not in _KNOWN_DEBT}
    assert not new, "governance chokepoints stubbed without a waiver:\n" + "\n".join(
        str(hit) for hits in new.values() for hit in hits
    )


def test_known_debt_list_only_shrinks() -> None:
    """A cleaned-up file must be REMOVED from the debt list, not left to rot.

    The list is a work board. An entry that no longer offends is a finished item
    whose removal keeps the board honest about what is left.
    """

    offending = _offending_files()
    stale = sorted(path for path in _KNOWN_DEBT if path not in offending)
    assert not stale, (
        "these files no longer stub a governance chokepoint — delete their "
        "_KNOWN_DEBT entries:\n" + "\n".join(stale)
    )


def test_the_scan_catches_a_planted_offender() -> None:
    """A positive control: the guardian is worthless if it cannot see the real thing."""

    planted = (
        "state.gate._run_approval_checkpoints = _Checkpoints()\n"
        "state.source_guard = SimpleNamespace()\n"
        "state.executor = _OrderedExecutor(events)\n"
        "monkeypatch.setattr(state.executor, '_pdp', _Deny())\n"
        "await runs.record_audit_receipt(run_id=run_id)\n"
    )
    offences = _scan(_REPO_ROOT / "packages" / "ne-runtime" / "tests" / "planted.py", planted)
    assert [hit.line for hit in offences] == [1, 2, 3, 4, 5], offences

    waived = planted.replace(
        "state.source_guard = SimpleNamespace()",
        "state.source_guard = SimpleNamespace()  # governance-stub: probe",
    )
    waived_offences = _scan(_REPO_ROOT / "packages" / "ne-runtime" / "tests" / "planted.py", waived)
    assert [hit.line for hit in waived_offences] == [1, 3, 4, 5]


def test_the_repaired_approval_tests_are_clean() -> None:
    """The three files this guardian was born from must stay un-stubbed."""

    offending = _offending_files()
    for repaired in (
        # Of the four files this guardian was born from, exactly ONE is clean on the
        # reconciliation train: test_approvals_api.py, repaired by hand in this same
        # salvage (branch un-stubbing + the train's three Sol-round tests kept). The
        # other three are declared in _KNOWN_DEBT above with their reasons. That is
        # the honest state, and the ratchet keeps it from growing.
        "packages/ne-api/tests/test_approvals_api.py",
        # test_run_approval_decision_end_to_end.py belongs here and is NOT here:
        # see its _KNOWN_DEBT entry above. It was repaired on the branch this
        # guardian was salvaged from; the train's newer U2 rewrite reintroduced
        # the hand-written-receipt pattern, so it is declared debt, not clean.
        "packages/ne-api/tests/run_approval_park.py",
    ):
        assert repaired not in offending, offending.get(repaired)
