# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO-LIVE-SEAMS — the default-off demo-deployment seams (fixture/demo only).

These demo seams light up the available governed ``ne demo`` exhibits without changing any
frozen contract, any guardian-pinned pilot file, or the C6 executor semantics.
Every seam is DEFAULT-OFF: when the related ``ApiSettings`` demo fields are unset the
composed ``config_hash``, the executor tool menu, and the retriever are all
byte-identical to a non-demo deployment (the 085c/087 conditional-stamp discipline).

  * :func:`demo_writeback_binding` / :data:`DEMO_WRITEBACK_POLICY` — a governed
    TIER-2 write tool (``write.external.demo.send``) that does NO I/O and returns a
    short confirmation string. It is appended into ``build_platform``'s executor
    binding tuple, so it runs ONLY inside the frozen ``GovernedToolExecutor``
    (PDP → guardrails → HITL gate → audit) — never a bespoke bypass. This module
    is the SINGLE source of the action/policy/binding (the test fixture harness
    imports it rather than re-declaring one).

    NE-DEMO-WRITEBACK-LIVE open decision (KEEP, not delete): this inert binding
    stays registered as Beat 4's DEFAULT tool even though the real governed
    ``write.internal.crm.note`` (NE-050, wired live in NE-ACTION-WIRE) now exists
    and ``ne demo up --live-writeback`` can swap Beat 4 onto it. Three reasons:
    (a) ``ApiSettings.write_actions_enabled`` cannot even BOOT without all 3
    connector legs configured (``_load_write_action_env_settings`` fails loud in
    ``ne.api.platform``) — a prospect demo deployment with no tenant creds has
    NO OTHER way to run Beat 4 live; (b) the CI-side ``_FakeCrmClient`` double
    (``tools/ne-cli/tests/demo_fixture_harness.py``) can be seam-injected into a
    TEST composition but not into a real ``ne demo up`` deployment; (c) the
    demo-interface design (BEAT 4) leans toward
    keeping an always-available no-egress fallback. No behavior/handler change
    accompanies this annotation — every existing test still pins the SAME inert
    confirmation string.
  * :func:`run_demo_ontology_lift` — runs the existing ``PropertyGraphLift`` from
    the composed graph store into the T3 ``ValidatingTripleStore`` at boot so a
    verified-answer template returns a real deterministic table.
  * :func:`run_demo_meridian_fixture_load` — loads the existing manifest- and
    ACL-validated Meridian projection into that same resolved T3 store.
  * :func:`demo_agent_definitions` (NE-168) — the SINGLE source of the 3
    ``AgentDefinition``s (``triage``/``reason``/``adjudicate``) a demo
    ``GovernedSubAgentDispatcher`` registers, so ``ne demo`` visibly exercises
    governed multi-lane agent-to-model delegation under one run trace_id. The
    ``ne.runtime.dispatch`` import is LAZY (inside the function) — it transitively
    imports ``pydantic_ai``, and ``build_platform`` imports this module
    UNCONDITIONALLY (for the policy constants above), so a top-level import here
    would drag that weight into every ne-api boot regardless of the flag.
  * :class:`DemoGroundedHallucinationGateway` (NE-GA-G7) — a ``ModelGateway``
    wrapper composed OVER the resolved gateway in ``build_platform`` when
    ``ApiSettings.demo_grounded_hallucination`` is on. For a prompt carrying the
    ``[grounded-hallucination]`` marker (:data:`DEMO_GROUNDED_HALLUCINATION_MARKER`
    — the SAME literal ``ne.cli.demo.corpus.G_REFUSE_PROMPT`` carries) it scripts
    one governed accounts fetch, then emits a canned ungrounded draft
    (:data:`DEMO_HALLUCINATED_DRAFT`), and junk-fails the marked grounding-assist
    re-check (:data:`DEMO_GROUNDED_ASSIST_JUNK`) — so the demo's REAL, unmodified
    G1 grounding gate refuses it LIVE: fallback, an audited
    ``grounding.ungrounded-claim``, and the withheld-notice table in the output.
    The seam supplies ONLY the fabrication; it performs zero I/O and executes no
    tool itself (the scripted fetch runs through the SAME frozen
    ``GovernedToolExecutor`` path as any grounded turn). Unmarked traffic is
    delegated to the wrapped gateway byte-transparently. A marked prompt sent to
    a non-grounded agent in a demo deployment yields a governed unknown-tool
    rejection — documented, demo-only (the seam is agent-agnostic at the
    gateway seam; the flag is overlay-only, never set in production).

Every ``ne.brain_graph`` / ``ne.brain_ontology`` / ``ne.runtime.dispatch`` import is
LAZY (inside functions) — importing this module drags no heavy dependency.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ne.contracts.models import (
    Completion,
    CompletionRequest,
    TurnContextBudget,
    TurnModelBudget,
    Usage,
)
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Lane
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.tools import ToolBinding

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Mapping

    from ne.brain_ontology import MeridianVerifiedLoadReport
    from ne.brain_ontology.gateway import ValidatingTripleStore
    from ne.contracts.models import CompletionChunk, ModelGateway
    from ne.runtime.dispatch import AgentDefinition

_LOG = logging.getLogger(__name__)

__all__ = [
    "DEMO_GROUNDED_ASSIST_JUNK",
    "DEMO_GROUNDED_HALLUCINATION_MARKER",
    "DEMO_HALLUCINATED_DRAFT",
    "DEMO_WRITEBACK_ACTION",
    "DEMO_WRITEBACK_POLICY",
    "DemoGroundedHallucinationGateway",
    "demo_agent_definitions",
    "demo_writeback_binding",
    "run_boot_coroutine",
    "run_demo_meridian_fixture_load",
    "run_demo_ontology_lift",
]

#: The governed TIER-2 demo write action. Defined EXACTLY once (grep-provable); the
#: fixture harness imports this rather than re-declaring the string.
DEMO_WRITEBACK_ACTION = "write.external.demo.send"

#: The guardrail arg policy merged into ``guardrails.tools`` so the tier-2 tool is
#: admitted (a tool absent from that map is fail-closed-rejected). One required
#: ``to`` text arg — no injectable surface beyond the recipient label.
DEMO_WRITEBACK_POLICY = ToolArgPolicy(args={"to": ArgSpec(kind=ToolArgKind.text, required=True)})


async def _demo_writeback_handler(_args: Mapping[str, object]) -> str:
    """Perform NO network I/O / egress (C2) — return a short confirmation string.

    A demo external write-back is a REAL governed tool: the point of the beat is the
    PDP → HITL → audit path, not a real egress. The handler is deliberately inert.
    """
    return "external note queued (demo write-back — no egress)"


def demo_writeback_binding() -> ToolBinding:
    """The governed TIER-2 demo write-back binding (single source of truth)."""
    return ToolBinding(
        spec=ToolSpec(
            name=DEMO_WRITEBACK_ACTION,
            description="demo external write-back (governed tier-2, no egress)",
            tier=ApprovalTier.TIER_2,
            input_schema={
                "type": "object",
                "properties": {"to": {"type": "string"}},
                "required": ["to"],
            },
        ),
        handler=_demo_writeback_handler,
    )


def demo_agent_definitions() -> Mapping[str, AgentDefinition]:
    """The 3 governed sub-agent definitions the NE-168 delegation demo dispatches.

    ``triage`` and ``reason`` route to the ``private-vllm`` lane (real, governed
    model calls); ``adjudicate`` routes to ``frontier-api`` — a definition the
    NE-161 RUNTIME GATE (``allow_frontier_subagent_routing=False``, the default)
    refuses BEFORE any model call, so it is a governed DENY exhibit, never a live
    frontier egress. Each has an EMPTY toolset (fail closed — no injectable tool
    surface for the demo delegation route). The lazy ``ne.runtime.dispatch`` import
    keeps this module's unconditional ``build_platform`` import cheap.

    All three declare ``kpi_tag='incident-response'`` (C8): the three legs are one
    business process, so their spend rolls up to one named outcome on the Insights
    page rather than three engineering-named fragments. The label is DEMO CONFIG —
    a real deployment names its own outcomes in its own ``AgentDefinition``s.
    """
    from ne.runtime.dispatch import AgentDefinition

    return {
        "triage": AgentDefinition(
            name="triage",
            instructions=(
                "You are the triage sub-agent. Classify the priority of the "
                "incident in one short sentence."
            ),
            toolset=(),
            route_task="triage",
            kpi_tag="incident-response",
            route_lane="private-vllm",
        ),
        "reason": AgentDefinition(
            name="reason",
            instructions=(
                "You are the reasoning sub-agent. Draft a one-sentence action "
                "plan for the incident."
            ),
            toolset=(),
            route_task="reason",
            kpi_tag="incident-response",
            route_lane="private-vllm",
        ),
        "adjudicate": AgentDefinition(
            name="adjudicate",
            instructions=(
                "You are the adjudication sub-agent. Decide whether the action "
                "plan may be released externally."
            ),
            toolset=(),
            route_task="adjudicate",
            kpi_tag="incident-response",
            route_lane="frontier-api",
        ),
    }


def run_boot_coroutine[T](coro: Awaitable[T]) -> T:
    """Drive ``coro`` to completion whether or not a loop is already running.

    ``asyncio.run`` when there is NO running loop; otherwise submit ``asyncio.run``
    to a one-shot thread and ``.result()`` it. REQUIRED: ne-worker composes
    ``build_platform`` INSIDE ``asyncio.run`` (``ne/worker/main.py``), so a bare
    ``asyncio.run`` here would raise ``RuntimeError: asyncio.run() cannot be called
    from a running event loop`` — a live-only crash CI would miss without the
    simulated-loop test.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()  # type: ignore[arg-type]


class DemoOntologyAuthorityIndex:
    """Boot-scoped, read-only-after-lift contributor authority index."""

    def __init__(self) -> None:
        self._stamps: dict[str, tuple[Any, ...]] = {}
        self._graph_stamps: dict[str, dict[str, Any]] = {}
        self._graphs: tuple[str, ...] = ()

    async def retain(
        self, subjects: tuple[str, ...], stamps: tuple[Any, ...], graphs: tuple[str, ...]
    ) -> None:
        if not stamps:
            raise ValueError("source-bearing lift retention requires stamps")
        if not graphs:
            raise ValueError("source-bearing lift retention requires scoped graphs")
        for subject in subjects:
            existing = self._stamps.get(subject)
            if existing is not None and existing != stamps:
                raise ValueError("a lifted contributor cannot bind divergent authority stamps")
            self._stamps[subject] = stamps
        for graph in graphs:
            graph_stamps = self._graph_stamps.setdefault(graph, {})
            for stamp in stamps:
                graph_stamps[stamp.authority_receipt_hash] = stamp

    async def resolve(self, contributor: str) -> Any | None:
        stamps = self._stamps.get(contributor, ())
        return stamps[0] if len(stamps) == 1 else None

    def authorized_graphs(self) -> tuple[str, ...]:
        return self._graphs

    async def snapshot_stamps(self, graphs: tuple[str, ...]) -> tuple[Any, ...]:
        """Return the complete contributor authority set for exact scoped graphs."""

        if not self._graphs or any(graph not in self._graphs for graph in graphs):
            return ()
        stamps = {
            stamp_hash: stamp
            for graph in graphs
            for stamp_hash, stamp in self._graph_stamps.get(graph, {}).items()
        }
        return tuple(stamps[key] for key in sorted(stamps))

    def seal_graphs(self, graphs: Any) -> None:
        if self._graphs:
            raise RuntimeError("the ontology authority index is already sealed")
        self._graphs = tuple(sorted(str(graph) for graph in graphs))


def run_demo_ontology_lift(
    graph_store: Any, t3_store: ValidatingTripleStore
) -> DemoOntologyAuthorityIndex:
    """Boot-time lift of the composed graph store into the T3 ValidatingTripleStore.

    Runs the EXISTING ``PropertyGraphLift.reconcile`` — the complete per-ACL-graph
    validate-then-atomic-swap path — so a verified-answer template returns a real
    deterministic table. NOT ``.run()`` (NE-LIFT-FIX, SPEC §11): the append-only
    per-element path validates a node's literal batch BEFORE its edges land, so any
    entity with a SHACL-required (minCount>=1) edge (``Order.account``/``placed_by``,
    ``Ticket.belongs_to``) fails SHACL on the node-only batch, is quarantined WHOLE,
    and never self-heals — zero Order rows in T3. ``reconcile()`` validates each ACL
    graph's nodes + edges TOGETHER, the correct primitive for a one-time complete
    load of static fixture data. Requires a scan-capable graph store
    (``scan_nodes``) — else a LOUD ``ValueError`` (the production AGE store and the
    reference store both satisfy it).
    """
    from ne.brain_ontology import PropertyGraphLift

    if not hasattr(graph_store, "scan_nodes"):
        raise ValueError(
            "NE_API_DEMO_ONTOLOGY_LIFT requires a scan-capable graph store "
            "(scan_nodes); the composed graph store does not expose it"
        )

    authority = DemoOntologyAuthorityIndex()

    async def _lift() -> Any:
        report = await PropertyGraphLift(
            graph_store,
            t3_store,
            require_source_authority=True,
            authority_sink=authority,
        ).reconcile()
        authority.seal_graphs(await t3_store.acl_graphs())
        return report

    report = run_boot_coroutine(_lift())
    _LOG.info(
        "demo ontology lift complete: nodes_lifted=%s edges_lifted=%s quarantined=%s",
        getattr(report, "nodes_lifted", "?"),
        getattr(report, "edges_lifted", "?"),
        len(getattr(report, "quarantined", ())),
    )
    return authority


def run_demo_meridian_fixture_load(
    fixture_dir: Path, t3_store: ValidatingTripleStore
) -> MeridianVerifiedLoadReport:
    """Load the existing manifest/ACL-validated Meridian fixture at boot.

    The synchronous wrapper deliberately reuses :func:`run_boot_coroutine`: ne-api
    composes without a running loop while ne-worker composes inside one. The
    loader itself owns manifest hashing, corpus validation, ACL graph labels,
    atomic replacement, and idempotency; this seam adds no second truth path.

    015A0-AUTHORITY-T3: the loader is the STAMPED production path, so the report it
    returns carries the credential-free release descriptor and this seam surfaces the
    descriptor and ``governed_local_t3`` SourceContract hashes rather than dropping
    them. The report is returned (it used to be discarded) so the production
    composition root can bind the same descriptor it just loaded instead of
    recomputing a second, possibly divergent one.
    """
    from ne.brain_ontology import load_meridian_verified_fixture, meridian_source_contract_identity

    report = run_boot_coroutine(load_meridian_verified_fixture(t3_store, fixture_dir))
    contract = meridian_source_contract_identity(report.release)
    _LOG.info(
        "demo Meridian T3 fixture load complete: fixture_hash=%s graphs=%d triples=%d "
        "dataset=%s release_descriptor=%s source_kind=%s source_contract=%s",
        report.fixture_hash,
        len(report.triple_counts),
        sum(report.triple_counts.values()),
        report.release.dataset_name,
        report.release.descriptor_hash,
        contract.source_kind,
        contract.identity_hash,
    )
    return report


# --- NE-GA-G7: the grounded-hallucination demo seam -------------------------

#: The demo-only sentinel marking a grounded-analyst prompt for the seam below.
#: ``ne.cli.demo.corpus.G_REFUSE_PROMPT`` carries this EXACT literal (grep-provable
#: parity, asserted by ``tools/ne-cli/tests/test_demo_corpus.py``) — corpus.py does
#: not import this module (no src-level ne-cli -> ne-api dependency); the two are
#: kept in sync by that test alone.
DEMO_GROUNDED_HALLUCINATION_MARKER: Final = "[grounded-hallucination]"

#: The fabricated draft the seam emits on a marked accounts-tool-return turn.
#: Moved byte-identically from ``tools/ne-cli/tests/demo_fixture_harness.py``
#: (its former test-local ``_HALLUCINATED_DRAFT``): built from tokens absent from
#: BOTH the corpus (the real acme fixture data) and the prompt, so the REAL
#: ``ne.govern.grounding`` gate refuses it on both numeral-ungrounded ("73" never
#: appears as any recomputed fact) and vocab-ungrounded ("Initech"/"Global" are
#: nowhere in the corpus or the prompt) grounds.
DEMO_HALLUCINATED_DRAFT: Final = "Initech Global: 73 placed orders."

#: The always-fails-re-check assist candidate the seam emits for a marked
#: ``task="grounding-assist"`` completion. The SINGLE source of these bytes;
#: ``tools/ne-cli/tests/demo_fixture_harness.py`` re-exports it as ``_ASSIST_JUNK``
#: (like ``_HALLUCINATED_DRAFT``). NE-HGATE-3 (design A-P1-4 / §5.3): the junk now
#: carries an UNGROUNDABLE NUMERAL (424242) so ``check_grounding`` refuses it on a
#: ``numeral-ungrounded`` finding — a FACT class, withheld at EVERY tier x profile by
#: the ENFORCEMENT matrix. A numeral-free junk ("zz unverifiable") would, at
#: STANDARD, produce only ADVISORY vocab findings, so the FAIL-branch re-check would
#: PASS and the junk would be ADOPTED — breaking beat 8's refuse-to-hallucinate
#: invariant. The ungrounded numeral pins the junk withheld by mechanism, not luck.
DEMO_GROUNDED_ASSIST_JUNK: Final = "assist candidate zz 424242 unverifiable"

#: ``VerifiedTemplate.tool_name`` for the ``accounts-directory`` template
#: (``ne.brain_ontology.verified.templates``: ``read.kg.<id>`` with ``-`` -> ``_``).
_ACCOUNTS_TOOL: Final = "read.kg.accounts_directory"

#: The tool-result content prefix ``GatewayModel._map_request_parts`` renders for a
#: ``ToolReturnPart`` (``ne.runtime.model``): ``f"{tool_name} returned: {content}"``.
_ACCOUNTS_TOOL_RETURNED: Final = "read.kg.accounts_directory returned:"


class DemoGroundedHallucinationGateway:
    """NE-GA-G7 demo-only seam (default-off): proves the refuse leg LIVE.

    Wraps the composed ``ModelGateway`` (over ``_default_gateway``, so
    pass-through traffic keeps the pre-egress redactor + egress guard — C2). For
    the ``[grounded-hallucination]``-marked grounded-analyst turn it scripts:

      1. a fresh marked user prompt -> an in-band ``read.kg.accounts_directory``
         tool call (the ONE governed fetch this leg makes);
      2. the marked accounts-tool-return -> the canned
         :data:`DEMO_HALLUCINATED_DRAFT` (never a second fetch — the refuse leg
         is deliberately under-fetched AND fabricated);
      3. a marked ``task="grounding-assist"`` completion -> the canned
         :data:`DEMO_GROUNDED_ASSIST_JUNK`, so the FAIL-branch assist re-check
         also fails and the withheld deterministic table is what reaches output.

    Every OTHER request (unmarked, or a marked prompt with the flag off) is
    delegated to the wrapped gateway UNCHANGED — byte-transparent pass-through.
    Marked turns make NO real model call (no new egress source, C2). The seam
    supplies ONLY the fabrication: the REAL ``ne.govern.grounding`` gate still
    runs on the scripted draft and refuses it (never weakened — this proves the
    guarantee, it does not bypass it). The scripted fetch executes through the
    SAME frozen ``GovernedToolExecutor`` path (PDP -> guardrails -> audit) as any
    other grounded turn (C6 — the seam registers no tool and executes nothing
    itself). A marked prompt sent to a NON-grounded agent in a demo deployment
    yields a governed unknown-tool rejection — documented, demo-only; not a
    production surface because ``NE_API_DEMO_GROUNDED_HALLUCINATION`` is
    overlay-only and default-off everywhere else.
    """

    def __init__(self, inner: ModelGateway) -> None:
        self._inner = inner

    def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        return self._inner.stream(request)

    async def context_budget_for_turn(
        self, task: str, *, requested_model: str | None = None, lane: Lane | None = None
    ) -> TurnModelBudget | None:
        """Forward the NE-CTX-IN-FORCE capability (byte-transparent, like ``stream``).

        This seam wraps the COMPOSED gateway for the whole process when
        ``NE_API_DEMO_GROUNDED_HALLUCINATION`` is on, so without this forward the
        Layer-2 trim guard's ``isinstance(..., TurnContextBudget)`` test fails
        and every turn in that deployment silently keeps the composed boot budget
        — a demo-only flag changing the enforced context window of unmarked
        production traffic, which is exactly what "byte-transparent pass-through"
        promises it does not do.
        """
        inner = self._inner
        if not isinstance(inner, TurnContextBudget):
            return None
        return await inner.context_budget_for_turn(task, requested_model=requested_model, lane=lane)

    async def complete(self, request: CompletionRequest) -> Completion:
        # (A) The bounded G4 assist boundary: checked FIRST (mirrors the fixture
        # harness's PromptRoutedGateway precedent) because a grounding-assist
        # completion is a bare 2-message request the composition root issues on
        # EITHER gate branch — it is never reached via the "last non-system
        # message" scan below.
        if request.task == "grounding-assist" and any(
            DEMO_GROUNDED_HALLUCINATION_MARKER in m.content for m in request.messages
        ):
            return Completion(
                text=DEMO_GROUNDED_ASSIST_JUNK,
                model_id="demo-grounded-hallucination-seam",
                usage=Usage(prompt_tokens=0, completion_tokens=0),
            )

        last_role = ""
        last_content = ""
        for message in request.messages:
            if message.role != "system":
                last_role = message.role
                last_content = message.content
        last_user_content = ""
        for message in request.messages:
            if message.role == "user":
                last_user_content = message.content

        # (B) A fresh marked user prompt -> the ONE governed accounts fetch.
        if last_role == "user" and DEMO_GROUNDED_HALLUCINATION_MARKER in last_content:
            return Completion(
                text=json.dumps({"tool_call": {"name": _ACCOUNTS_TOOL, "arguments": {}}}),
                model_id="demo-grounded-hallucination-seam",
                usage=Usage(prompt_tokens=0, completion_tokens=0),
            )
        # (C) The marked accounts-tool-return -> the canned fabricated draft.
        if (
            last_role == "tool"
            and last_content.startswith(_ACCOUNTS_TOOL_RETURNED)
            and DEMO_GROUNDED_HALLUCINATION_MARKER in last_user_content
        ):
            return Completion(
                text=DEMO_HALLUCINATED_DRAFT,
                model_id="demo-grounded-hallucination-seam",
                usage=Usage(prompt_tokens=0, completion_tokens=0),
            )
        # (D) Everything else — byte-transparent pass-through to the real model.
        return await self._inner.complete(request)
