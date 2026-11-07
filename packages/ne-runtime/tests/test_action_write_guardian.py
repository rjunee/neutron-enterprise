# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE PERMANENT C7-WRITE STRUCTURAL GUARDIAN (NE-050, build-orchestration plan §2.1).

A durable, UN-SKIPPABLE structural test that a write-back action can NEVER reach a
source system except through the governed executor. ZERO skip/skipif/xfail markers
(self-checked). If a future change opens a bypass — a perform reachable off the
governed handler, a GovernedAction that exposes more than ``execute``, a reference
action importing a transport, an under-tiered action — one of these tests fails.
Do not weaken them.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from ne.govern.errors import GuardrailViolation
from ne.runtime.action_tools import (
    ActionDedupStore,
    GovernedAction,
    build_action_binding,
    ensure_action_dedup_schema,
)
from ne.runtime.actions import tier_for_action
from ne.runtime.approval_store import SqlAlchemyApprovalStore, ensure_approval_schema
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.errors import ApprovalNotGrantedError, ToolDeniedError
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.reference_actions import (
    crm_note_definition,
    email_draft_definition,
    email_send_definition,
    ticket_update_definition,
)

from ._write_fixtures import (
    GRAPH_MAILBOX,
    SF_RECORD_ID,
    graph_write_client,
    salesforce_write_client,
    sql_tickets_engine,
    sql_write_client,
    ticket_rows,
    write_guardrails,
)
from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    approval_settings,
    make_pdp,
    make_principal,
)

_SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "ne" / "runtime"
_REF_DIR = _SRC_DIR / "reference_actions"

_DENY = {
    "effect": "deny",
    "tier": 2,
    "reason": "denied",
    "obligations": {},
    "policy_version": "1.0.0",
}


class _StubCrm:
    async def create_record(self, sobject: str, fields: Any) -> str:
        return "stub"


class _StubTicket:
    async def update_record(
        self, *, table: str, key_column: str, key_value: Any, fields: Any
    ) -> int:
        return 1


class _StubDraft:
    async def create_draft(self, *, mailbox: str, subject: str, body: str, to: str) -> str:
        return "stub"


class _StubSend:
    async def read_draft_recipients(self, *, mailbox: str, message_id: str) -> tuple[str, ...]:
        return ("ally@corp.example",)

    async def send_draft(self, *, mailbox: str, message_id: str) -> None:
        return None


class _StubExecutor:
    async def execute(self, tool: str, args: Any, *, principal: Any) -> str:
        return "{}"


def _dedup() -> ActionDedupStore:
    engine = sa.create_engine("sqlite://")
    ensure_action_dedup_schema(engine)
    return ActionDedupStore(engine)


def _executor(
    definition: Any, sink: Any, *, gate: Any, overrides: Any = None
) -> GovernedToolExecutor:
    binding = build_action_binding(definition, dedup=_dedup())
    return GovernedToolExecutor(
        [binding],
        pdp=make_pdp(sink, overrides=overrides),
        guardrails=write_guardrails(sink),
        approval_gate=gate,
        sink=sink,
    )


# --- (1) GovernedAction public surface is execute-only ---------------------------


def test_governed_action_public_surface_is_execute_only() -> None:
    public_callables = {
        name
        for name, member in inspect.getmembers(GovernedAction)
        if not name.startswith("_")
        and (inspect.isfunction(member) or inspect.iscoroutinefunction(member))
    }
    assert public_callables == {"execute"}

    action = GovernedAction(
        spec=crm_note_definition(_StubCrm()).spec,
        source_system="salesforce",
        executor=_StubExecutor(),
    )
    # The instance holds no perform/handler/client attribute (only the executor + spec).
    for banned in ("perform", "handler", "client", "_perform", "_handler", "_client"):
        assert banned not in action.__dict__


# --- (2) GovernedAction requires an executor -------------------------------------


def test_governed_action_requires_executor() -> None:
    with pytest.raises(TypeError):
        GovernedAction(
            spec=crm_note_definition(_StubCrm()).spec,
            source_system="salesforce",
            executor=None,  # type: ignore[arg-type]
        )


# --- (3) perform call sites: only inside action_tools.py -------------------------


def test_perform_call_sites() -> None:
    offenders: list[str] = []
    for path in _SRC_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if ".perform(" in text and path.name != "action_tools.py":
            offenders.append(path.relative_to(_SRC_DIR).as_posix())
    assert offenders == [], (
        f"a source-system perform is reachable outside action_tools.py: {offenders}"
    )


# --- (4) reference actions import no transport / connector / sqlalchemy -----------


#: The reference-action package these imports are resolved against — bare
#: relative forms (``from . import x``, ``from .. import x``, ``from ... import
#: x``) only make sense relative to SOME package, and this is the one the
#: guardian scans (see :func:`_full_imports`).
_REF_PACKAGE = "ne.runtime.reference_actions"


def _imports(tree: ast.AST) -> set[str]:
    return {p for f in _full_imports(tree) for p in (f, f.split(".")[0])}


def _full_imports(tree: ast.AST, *, package: str = _REF_PACKAGE) -> set[str]:
    """Full dotted import paths, ABSOLUTE **and RELATIVE**, resolved against
    ``package`` — precise enough for an ALLOWLIST check.

    Unlike a naive walk (which drops ``ast.ImportFrom`` entirely when
    ``node.module is None`` — exactly the bare-relative form ``from . import
    X`` / ``from .. import X`` / ``from ... import X``), this resolves every
    relative import to its fully-qualified dotted path so it is visible to the
    allowlist below. Resolution:

    * ``level == 0`` (absolute): the import path is ``node.module`` as-is.
    * ``level >= 1`` and the import climbs no higher than the package root
      (``level - 1 < len(package.split('.'))``): strip ``level - 1`` trailing
      components off ``package``, then append ``node.module`` (if present) or
      each imported name (if the form is bare ``from . import X``).
    * ``level >= 1`` and the import climbs ABOVE the package root: this cannot
      be resolved to a real dotted path, so it is emitted as the literal dot
      marker (e.g. ``"...."​``) — which matches no allowlist prefix and is
      therefore FAIL-CLOSED (always a violation), never silently dropped.

    Only ``import x.y.z`` / ``from x.y.z import ...`` full paths are kept
    (never the split top-level component — that belongs to :func:`_imports`,
    used only for the explicit-message ``banned_top`` belt), so an allowlist
    entry like ``"ne.contracts.integration"`` cannot be satisfied by a
    same-top-level-but-different-submodule import.
    """
    parts = package.split(".")
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module is not None:
                    found.add(node.module)
                continue
            if node.level - 1 >= len(parts):
                found.add("." * node.level + (node.module or ""))
                continue
            base_parts = parts[: len(parts) - (node.level - 1)]
            if node.module is not None:
                found.add(".".join([*base_parts, node.module]))
            else:
                base = ".".join(base_parts)
                for alias in node.names:
                    found.add(f"{base}.{alias.name}")
    return found


#: DERIVED allowlist (design §3-V3 fixed form: "prefer derivation/allowlist over
#: enumeration") — exactly what a spec-only reference-action definition needs:
#: stdlib typing/collection helpers, pydantic (schema fields), and the four
#: first-party seams that carry no transport of their own (``ne.contracts.*``
#: types, ``ne.govern.config`` arg-policy shapes, ``ne.runtime.action_tools``'
#: the definition/registration seam, and the package's own sibling modules).
#: This is the PRIMARY defense: every STATIC import — absolute OR relative,
#: resolved against ``ne.runtime.reference_actions`` by :func:`_full_imports`
#: — that is not on this allowlist is a violation. Prefix matches are
#: SEGMENT-EXACT (``name == prefix or name.startswith(prefix + ".")``), so
#: ``ne.govern.configuration_transport`` and ``ne.runtime.action_tools_http``
#: do NOT satisfy ``ne.govern.config`` / ``ne.runtime.action_tools`` merely by
#: sharing a string prefix. A future transport library — aiohttp, pycurl,
#: grpc, boto3, or anything not yet invented, spelled either absolutely or via
#: a relative import — is caught without ever being named, unlike
#: ``banned_top`` below (kept only as an explicit-message belt, per design
#: §3-V3's "enumeration is unavoidable" fallback discipline — it is not the
#: primary check here since it CAN be enumerated around).
#:
#: DECLARED RESIDUAL: this is a STATIC scan over the AST. A dynamic import —
#: ``importlib.import_module("ne.connect...")``, a string-built module name,
#: ``__import__`` — is invisible to it. There is no backstop for that
#: residual, the same disclosed-not-closed discipline as the C6 guardian's
#: declared residuals in ``test_c6_no_bypass_guardian.py``.
#: ``json`` is admitted (NE-WORKFLOWS) because the READ reference binding
#: (``crm_lookup``) must return a STRUCTURED result to its caller and a governed tool
#: handler's return type is ``str``. It is stdlib serialisation over values the handler
#: already holds: it opens no socket, resolves no name, and reads no file — so it
#: cannot be the transport this guardian exists to keep out. The allowlist stays an
#: allowlist; this is one named, inert addition, not a relaxation of the rule.
_ALLOWED_REFERENCE_ACTION_IMPORTS_EXACT = frozenset(
    {"__future__", "collections.abc", "json", "typing", "pydantic"}
)
#: ``ne.runtime.tools`` joins ``ne.runtime.action_tools`` for the same reason it was
#: already there: it is the local spec/binding type (``ToolBinding``), the READ
#: counterpart of ``ActionDefinition``. A read has no write to deduplicate, so the
#: read reference binding is a plain ``ToolBinding`` — and the module that declares
#: that type is no more a transport than the one that declares ``ActionDefinition``.
_ALLOWED_REFERENCE_ACTION_IMPORT_PREFIXES = (
    "ne.contracts",
    "ne.govern.config",
    "ne.runtime.action_tools",
    "ne.runtime.reference_actions",
    "ne.runtime.tools",
)


def _reference_action_import_violations(full_imports: set[str]) -> set[str]:
    """ALLOWLIST-derived check core (testable against a synthetic import set,
    not just real files on disk) — anything not on the derived allowlist is a
    violation, independent of whether it happens to also be a KNOWN-bad name."""
    violations: set[str] = set()
    for name in full_imports:
        if name in _ALLOWED_REFERENCE_ACTION_IMPORTS_EXACT:
            continue
        if any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in _ALLOWED_REFERENCE_ACTION_IMPORT_PREFIXES
        ):
            continue
        violations.add(name)
    return violations


def test_reference_actions_import_no_transport() -> None:
    banned_top = {"httpx", "requests", "socket", "sqlalchemy"}
    for path in _REF_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = _imports(tree)
        # Belt: the historically-named transports, explicit message on match.
        assert not (imports & banned_top), (
            f"{path.name} imports a banned module: {imports & banned_top}"
        )
        assert not any(
            name == "ne.connect" or name.startswith("ne.connect.") for name in imports
        ), (
            f"{path.name} imports ne.connect — reference actions reach a source system ONLY "
            "through an injected Protocol client from inside the governed handler"
        )
        # Primary: the derived allowlist — catches anything banned_top does not
        # name, not just the four historical entries.
        violations = _reference_action_import_violations(_full_imports(tree))
        assert not violations, (
            f"{path.name} imports {violations}, not on the derived reference-action "
            "allowlist (design §3-V3) — a reference action must be spec-only"
        )


def test_reference_action_import_allowlist_catches_unenumerated_transport() -> None:
    """The V3 defect ``banned_top`` alone is vulnerable to: an aiohttp/pycurl-class
    library never named in the enumeration. Proven here with a synthetic import
    set naming something that is NOT in ``banned_top`` (nor in the C2 egress
    guardians' lists) — 'aiohttp' itself, plus a made-up transport-shaped module
    that could never be pre-enumerated — and asserting the ALLOWLIST form (not
    the enumeration form) flags both, because it does not need to have heard of
    them."""
    for offender in ("aiohttp", "pycurl", "ne.connect.transport.exotic_future_sdk"):
        synth = {"__future__", "typing", "ne.contracts.integration", offender}
        violations = _reference_action_import_violations(synth)
        assert offender in violations, f"allowlist failed to catch unenumerated {offender!r}"
        # Cross-check: banned_top (the enumeration form) does NOT know these two —
        # this is exactly the gap the allowlist inversion closes.
        if offender in ("aiohttp", "pycurl"):
            assert offender not in {"httpx", "requests", "socket", "sqlalchemy"}


def test_reference_action_import_allowlist_negative_control() -> None:
    """Every REAL, legitimate reference action still passes clean — the allowlist
    inversion must not turn into a false-positive machine (F-P1-5)."""
    for path in _REF_DIR.glob("*.py"):
        full = _full_imports(ast.parse(path.read_text(encoding="utf-8")))
        violations = _reference_action_import_violations(full)
        assert violations == set(), f"{path.name}: {violations}"


def test_relative_import_offender_is_caught() -> None:
    """P1-2 (Argus): the previous ``_full_imports`` dropped ``ast.ImportFrom``
    whenever ``node.module is None`` — exactly the bare-relative form. Argus's
    exact planted offender: appending a bare-relative import that resolves to
    ``ne.connect`` to a REAL reference action's source used to leave the
    reference-action allowlist blind to it (9 passed on the unfixed tree)."""
    clean_source = (_REF_DIR / "crm_note.py").read_text(encoding="utf-8")

    offender_source = clean_source + "\nfrom ... import connect as _c\n"
    tree = ast.parse(offender_source)
    violations = _reference_action_import_violations(_full_imports(tree))
    assert "ne.connect" in violations, (
        "the bare-relative `from ... import connect as _c` must resolve to "
        f"ne.connect and be flagged; got {violations}"
    )

    # CONTROL 1: the unmodified real file is clean (no false positive from the
    # resolution machinery itself).
    clean_violations = _reference_action_import_violations(_full_imports(ast.parse(clean_source)))
    assert clean_violations == set()

    # CONTROL 2: probe validity in the other direction — an ABSOLUTE offender
    # (httpx, already covered by banned_top) is independently still caught by
    # the same allowlist-derived core, proving the gap was specifically the
    # relative form and not the whole mechanism.
    absolute_offender_source = clean_source + "\nimport httpx\n"
    absolute_violations = _reference_action_import_violations(
        _full_imports(ast.parse(absolute_offender_source))
    )
    assert "httpx" in absolute_violations


def test_relative_import_resolution_shapes() -> None:
    """Pins the five decided relative-import resolution semantics against the
    scanned package ``ne.runtime.reference_actions`` (parts: ne / runtime /
    reference_actions)."""
    # `from ... import connect as _c` -> ne.connect (climbs to package root's
    # grandparent: ne).
    tree = ast.parse("from ... import connect as _c\n")
    assert "ne.connect" in _full_imports(tree)

    # `from . import crm_note` -> ne.runtime.reference_actions.crm_note (sibling
    # module within the package itself — legitimate, allowlisted).
    tree = ast.parse("from . import crm_note\n")
    full = _full_imports(tree)
    assert "ne.runtime.reference_actions.crm_note" in full
    assert _reference_action_import_violations(full) == set()

    # `from .. import actions` -> ne.runtime.actions (one level up from the
    # package: the runtime package's own actions module) — NOT allowlisted.
    tree = ast.parse("from .. import actions\n")
    full = _full_imports(tree)
    assert "ne.runtime.actions" in full
    assert "ne.runtime.actions" in _reference_action_import_violations(full)

    # `from ...connect import X` -> ne.connect (module-form relative import).
    tree = ast.parse("from ...connect import X\n")
    assert "ne.connect" in _full_imports(tree)

    # `from .... import os` climbs ABOVE the package root (level-1=3 >=
    # len(parts)=3) -> fail-closed literal dot marker, never resolvable to a
    # real path, and therefore never allowlisted.
    tree = ast.parse("from .... import os\n")
    full = _full_imports(tree)
    assert "...." in full
    assert "...." in _reference_action_import_violations(full)


def test_allowlist_prefix_boundaries() -> None:
    """Codex (independently confirmed): the prefixes must be SEGMENT-exact in
    both directions. A bare ``startswith`` with no trailing-dot discipline lets
    ``ne.govern.configuration_transport`` ride in on ``ne.govern.config`` and
    ``ne.runtime.action_tools_http`` ride in on ``ne.runtime.action_tools`` —
    while the opposite direction (requiring a literal trailing dot in the
    allowlist entry) would falsely flag the bare package name itself
    (``ne.contracts``)."""
    flagged = {
        "ne.govern.configuration_transport",
        "ne.runtime.action_tools_http",
        "ne.contractsx",
    }
    for name in flagged:
        assert name in _reference_action_import_violations({name}), name

    allowed = {
        "ne.govern.config",
        "ne.govern.config.sub",
        "ne.runtime.action_tools",
        "ne.contracts",
        "ne.contracts.integration",
    }
    for name in allowed:
        assert _reference_action_import_violations({name}) == set(), name


# --- (5) every reference action is Tier-2+, classified, and declared >= class -----


def test_every_reference_action_declares_its_exact_ladder_tier() -> None:
    """NE-CS-SEND: the tier ladder is DELIBERATE, pinned action by action.

    The old absolute Tier-2 write floor is gone (owner decision 2026-08-27: the
    ladder is SPEC §4.2's own rungs — draft autonomous, internal autonomous +
    digest, external per-item human approval). What replaces it here is a
    STRICTER pin, not a weaker one: each action's declared tier is asserted
    EXACTLY, and the one-directional class floor (declared >= class, unclassed
    names raise) still holds for every action — so `write.external.*` can never
    be declared below Tier-2 and any tier change is a deliberate edit HERE.
    """
    expected = {
        "draft.email.create": 0,
        "write.internal.crm.note": 1,
        "write.external.email.send": 2,
        "write.external.ticket.update": 3,
    }
    definitions = [
        crm_note_definition(_StubCrm()),  # type: ignore[arg-type]
        email_draft_definition(_StubDraft()),  # type: ignore[arg-type]
        email_send_definition(
            _StubSend(),  # type: ignore[arg-type]
            allowed_mailboxes=(GRAPH_MAILBOX,),
            allowed_recipients=("ally@corp.example",),
        ),
        ticket_update_definition(_StubTicket()),  # type: ignore[arg-type]
    ]
    assert {d.spec.name for d in definitions} == set(expected)
    for definition in definitions:
        tier = int(definition.spec.tier)
        assert tier == expected[definition.spec.name], definition.spec.name
        class_tier = int(tier_for_action(definition.spec.name))  # raises if unclassifiable
        assert tier >= class_tier
    # The irreversible external send keeps its human gate BY CLASS: even a
    # mis-declared spec could not lower it (ActionDefinition refuses declared <
    # class at registration).
    assert int(tier_for_action("write.external.email.send")) == 2


# --- (6) unapproved writes never reach the transport (deny / reject / block) ------


def test_unapproved_write_never_reaches_transport() -> None:
    # DENY across all three actions => zero source-system effect.
    # (i) Salesforce CRM note.
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    ex = _executor(
        crm_note_definition(salesforce_write_client(recorder=recorder)),
        sink,
        gate=NamedApproverGate(),
        overrides={"write.internal.crm.note": _DENY},
    )
    with pytest.raises(ToolDeniedError):
        asyncio.run(
            ex.execute(
                "write.internal.crm.note",
                {
                    "request_id": "req-deny0001",
                    "record_id": SF_RECORD_ID,
                    "title": "t",
                    "body": "b",
                },
                principal=make_principal(),
            )
        )
    assert recorder == []

    # (ii) M365 email draft.
    sink = RecordingAuditSink()
    recorder = []
    ex = _executor(
        email_draft_definition(graph_write_client(recorder=recorder)),
        sink,
        gate=NamedApproverGate(),
        overrides={"draft.email.create": _DENY},
    )
    with pytest.raises(ToolDeniedError):
        asyncio.run(
            ex.execute(
                "draft.email.create",
                {
                    "request_id": "req-deny0002",
                    "mailbox": GRAPH_MAILBOX,
                    "subject": "s",
                    "body": "b",
                    "to": "ally@corp.example",
                },
                principal=make_principal(),
            )
        )
    assert recorder == []

    # (iii) SQL ticket update.
    sink = RecordingAuditSink()
    engine = sql_tickets_engine()
    ex = _executor(
        ticket_update_definition(sql_write_client(engine)),
        sink,
        gate=NamedApproverGate(),
        overrides={"write.external.ticket.update": _DENY},
    )
    with pytest.raises(ToolDeniedError):
        asyncio.run(
            ex.execute(
                "write.external.ticket.update",
                {"request_id": "req-deny0003", "ticket_id": 42, "status": "closed"},
                principal=make_principal(),
            )
        )
    assert ticket_rows(engine) == [(42, "open", "")]

    # A guardrail block (bad recipient) on the email draft => zero transport.
    sink = RecordingAuditSink()
    recorder = []
    ex = _executor(
        email_draft_definition(graph_write_client(recorder=recorder)),
        sink,
        gate=NamedApproverGate(),
    )
    with pytest.raises(GuardrailViolation):
        asyncio.run(
            ex.execute(
                "draft.email.create",
                {
                    "request_id": "req-block001",
                    "mailbox": GRAPH_MAILBOX,
                    "subject": "s",
                    "body": "b",
                    "to": "attacker@evil.example",
                },
                principal=make_principal(),
            )
        )
    assert recorder == []

    # A rejected approval on the Tier-2 SEND => zero transport. NE-CS-SEND moved
    # this scenario off the CRM note (now Tier-1 autonomous — it never parks) and
    # onto the action whose whole point is the human gate: a REAL send that a
    # human says no to must never reach Graph.
    sink = RecordingAuditSink()
    recorder = []
    apeng = sa.create_engine("sqlite://")
    ensure_approval_schema(apeng)
    store = SqlAlchemyApprovalStore(apeng)
    gate = DurableApprovalGate(
        store=store, sink=sink, settings=approval_settings(poll_interval_s=0.0)
    )
    ex = _executor(
        email_send_definition(
            graph_write_client(recorder=recorder),
            allowed_mailboxes=(GRAPH_MAILBOX,),
            allowed_recipients=("ally@corp.example",),
        ),
        sink,
        gate=gate,
    )

    async def reject_scenario() -> None:
        task = asyncio.create_task(
            ex.execute(
                "write.external.email.send",
                {
                    "request_id": "req-rej00001",
                    "mailbox": GRAPH_MAILBOX,
                    "draft_ref": "AAMkAG-fake-draft-message-id",
                },
                principal=make_principal(),
            )
        )
        while not await store.list_pending():
            await asyncio.sleep(0)
        rid = (await store.list_pending())[0].request_id
        await gate.reject(rid, approver="alice", reason="no")
        with pytest.raises(ApprovalNotGrantedError):
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(reject_scenario())
    # NOTHING reached the wire — not even the recipient read-back: the rejection
    # happened at the gate, before perform ran at all.
    assert recorder == []


# --- (7) the guardian has no skip markers ----------------------------------------


def test_guardian_has_no_skip_markers() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    prefix = "pytest.mark."
    for suffix in ("skip", "skipif", "xfail"):
        assert (prefix + suffix) not in source, f"guardian file must not contain {prefix + suffix}"
