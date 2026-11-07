# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The minimal governed-tool registration seam for verified answers (NE-085c).

Additive binding registration per the SPEC NE-085c row — NOT a ``create_app``
refactor (``app.py`` is untouched, the C6 chokepoints are untouched, ne-contracts is
unchanged). This module converts the runtime-free ``VerifiedAnswerToolDef``\\s that
``ne.brain_ontology.verified`` exports (``ToolSpec`` + handler + plain-data guardrail
arg specs) into standard :class:`~ne.runtime.tools.ToolBinding`\\s executed ONLY
through the frozen ``GovernedToolExecutor`` — the type conversion lives HERE because
``ne.brain_ontology`` may not import ``ne.runtime``/``ne.govern`` (the merged
brain-ontology-lane contract, C3).

NE-135 additions (the governed ontology-read HTTP seam): :func:`resolve_triple_store`
is the SINGLE store-resolution site (so ``build_platform`` opens the embedded backend
EXACTLY once and hands the SAME gateway to both the verified-answer bindings and the
read routes — no oxigraph double-open), and :func:`build_ontology_read_state`
precomputes the :class:`OntologyReadState` (the compiled SHACL shape list + the
quarantine lane) the ``routers/ontology.py`` handlers serve. The ontology READS still
ride the gateway's C12 ``sparql`` steward surface; invoke still rides ONLY the frozen
``GovernedToolExecutor`` — this module adds no ungoverned path.

Every ``ne.brain_ontology`` import is LAZY (inside functions), mirroring the
``platform._default_gateway`` discipline: importing this module pulls no rdflib.

NE-164b: :func:`build_verified_answer_bindings` also wires in the platform's ONE
audit sink and a ``trace_id_provider=current_run_trace_id`` factory kwarg. The
run-trace id reaches the T3 handler the same way the principal does (the
TURN_PRINCIPAL precedent): ``GovernedToolExecutor.execute`` has no ``trace_id``
parameter, so the provider is a zero-arg callable evaluated INSIDE the
``/v1/verified/invoke`` request context — it resolves the identical ContextVar
value the route itself would read, threaded to ``ne.brain_ontology`` as DATA
(never an import).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ne.api.knowledge_source_evidence import ExistingKnowledgeRead, KnowledgeSourceEvidenceAuthor
from ne.api.tools import TURN_PRINCIPAL
from ne.contracts.governance import AuditSink
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.correlation import current_run_trace_id
from ne.runtime.errors import ActionValidationError
from ne.runtime.grounding import current_answer_recorder
from ne.runtime.tools import ToolBinding

if TYPE_CHECKING:
    from ne.brain_ontology.config import OntologySettings
    from ne.brain_ontology.gateway import ValidatingTripleStore
    from ne.brain_ontology.quarantine import TripleQuarantine
    from ne.brain_ontology.verified.answers import VerifiedReadStore
    from ne.brain_ontology.verified.scope import GroupExpander
    from ne.brain_ontology.verified.templates import TemplateRegistry, TemplateRegistryFingerprint
    from ne.contracts.plugins import PluginRegistry
    from ne.contracts.source_authority import CurrentSourceAuthorityReader, SourceAuthorityStamp

__all__ = [
    "CONSOLE_VERIFIED_READ",
    "OntologyReadState",
    "ShapeInfo",
    "VerifiedAnswerSetup",
    "build_ontology_read_state",
    "build_tool_policies",
    "build_verified_answer_bindings",
    "load_verified_answer_setup",
    "resolve_triple_store",
    "resolve_verified_token_budget",
]

_CAPTURED_VERIFIED_ANSWER: ContextVar[Mapping[str, object] | None] = ContextVar(
    "ne_api_captured_verified_answer", default=None
)

#: Set (True) by the ``/v1/verified/invoke`` HTTP route for the duration of its two
#: governed executions, and NOWHERE else. It declares "this verified read is a
#: run-less console/API invocation": there is no admitted Run tool call, so there is
#: no canonical Run evidence trail the read could claim its receipts into — the
#: route's own GroundingRecorder captures the structured answer, and the executor
#: still emits every C6 audit event. ``_verified_evidence_handler`` consults it ONLY
#: for the ``existing is None`` case; an agent-lane read (which always runs inside an
#: admitted Run tool call) keeps the hard fail-closed LookupError when its evidence
#: context is missing or owned by another principal.
CONSOLE_VERIFIED_READ: ContextVar[bool] = ContextVar("ne_api_console_verified_read", default=False)

#: rdflib arg type → the deterministic guardrail arg CLASS (C12 defensive floor; the
#: authoritative strict validation is the generated Pydantic model in the handler).
_KIND_FOR_TYPE: dict[str, ToolArgKind] = {
    "str": ToolArgKind.text,
    "iri": ToolArgKind.text,
    "date": ToolArgKind.text,
    "datetime": ToolArgKind.text,
    "int": ToolArgKind.number,
    "bool": ToolArgKind.boolean,
}


@dataclass(frozen=True)
class VerifiedAnswerSetup:
    """The load-time result: the validated registry, its fingerprint, its arg policies."""

    registry: TemplateRegistry
    fingerprint: TemplateRegistryFingerprint
    policies: dict[str, ToolArgPolicy]


@dataclass(frozen=True, slots=True)
class VerifiedKnowledgeEvidence:
    """Existing-Run-only 009 authoring dependencies for verified T3 Tools."""

    author: KnowledgeSourceEvidenceAuthor
    context_provider: Callable[[], ExistingKnowledgeRead | None]


def load_verified_answer_setup(
    settings: OntologySettings, *, governed_store_url: str | None = None
) -> VerifiedAnswerSetup | None:
    """Load + validate the template registry and derive per-tool guardrail policies.

    ``None`` when verified answers are DISABLED (the default — the platform is then
    byte-identical to pre-085c). Enabled-but-no-templates-dir is a fail-loud boot
    error (templates are per-customer config-bundle content, never package-shipped).
    A malformed registry fails boot (``load_template_registry`` raises).

    ``governed_store_url`` (NE-ONTOLOGY-FLYWHEEL): when enabled AND non-empty, the
    boot bundle is OVERLAID with the governed ``ne_verified_templates`` HITL store
    (a short-lived engine, disposed in ``finally``) so a restart re-loads any
    steward-applied template. With an EMPTY store the overlay is a no-op and the
    fingerprint is BYTE-IDENTICAL to the boot-dir-only load (the parity anchor);
    ``None``/empty url ⇒ the pre-NE-ONTOLOGY-FLYWHEEL boot-dir-only load exactly.
    """
    if not settings.verified_answers_enabled:
        return None
    if settings.verified_templates_dir is None:
        raise ValueError(
            "verified_answers_enabled is True but verified_templates_dir is None: the "
            "verified-answer templates are per-customer config-bundle content and the "
            "package ships none — set NE_ONTOLOGY_VERIFIED_TEMPLATES_DIR (fail loud, never empty)"
        )
    from ne.brain_ontology.verified.templates import load_template_registry

    if governed_store_url:
        registry = _load_registry_with_governed_store(
            settings.verified_templates_dir, governed_store_url
        )
    else:
        registry = load_template_registry(settings.verified_templates_dir)
    return VerifiedAnswerSetup(
        registry=registry,
        fingerprint=registry.fingerprint,
        policies=build_tool_policies(registry),
    )


def build_tool_policies(registry: TemplateRegistry) -> dict[str, ToolArgPolicy]:
    """Derive the per-tool guardrail ``ToolArgPolicy`` map for a template registry.

    Shared by the boot ``load_verified_answer_setup`` AND the NE-ONTOLOGY-FLYWHEEL
    ``config_hash`` projection (``platform._project_config_hash``) so the two can
    never drift: the projected restart hash MUST account for the govern-policy that
    a HITL-added template contributes at the next boot (a new tool = a new policy
    key merged into ``guardrails.tools``, a hashed input). Deriving it in one place
    is the drift tripwire's second half.
    """
    policies: dict[str, ToolArgPolicy] = {}
    for template in registry.templates:
        args = {
            name: ArgSpec(
                kind=_KIND_FOR_TYPE[arg.type],
                required=True,
                max_length=arg.max_length,
                # NE-KG-MULTIKEY: carry the template's declared cardinality cap onto
                # the guardrail floor, so the C12 stage bounds a multi-key list with
                # the SAME number the template declares (never a second, drifting one).
                list_max=arg.list_max,
            )
            for name, arg in template.args.items()
        }
        policies[template.tool_name] = ToolArgPolicy(args=args)
    return policies


def _load_registry_with_governed_store(boot_dir: Path, governed_store_url: str) -> TemplateRegistry:
    """Overlay the boot bundle with the governed HITL store (short-lived engine).

    All ``ne.brain_ontology`` imports stay lazy (this module's discipline). The
    engine is disposed in ``finally`` — this is a one-shot boot read, not a
    retained handle. Production is Postgres; file-backed sqlite persists across the
    short-lived read, but an in-memory ``sqlite://`` URL would NOT (documented).
    """
    from ne.audit import (
        create_engine_with_timeouts,
        ensure_transactional_sqlite,
    )
    from ne.brain_ontology.verified.store import (
        ensure_verified_template_schema,
        read_verified_templates,
    )
    from ne.brain_ontology.verified.templates import load_template_registry_with_overlay

    engine = create_engine_with_timeouts(governed_store_url)
    try:
        ensure_transactional_sqlite(engine)
        ensure_verified_template_schema(engine)
        stored = read_verified_templates(engine)
        return load_template_registry_with_overlay(boot_dir, stored)
    finally:
        engine.dispose()


def resolve_triple_store(
    registry: PluginRegistry, settings: OntologySettings, config_hash: str
) -> ValidatingTripleStore:
    """Resolve the ONE T3 gateway through the EXISTING ``ne.plugins.triple_store`` seam.

    NE-135: the SINGLE store-resolution site. ``build_platform`` calls this EXACTLY
    once and passes the returned gateway to BOTH the verified-answer bindings and the
    ontology read routes, so an embedded oxigraph path is never opened twice. The test
    seam ``registry.register('triple_store', 'oxigraph', lambda **_kw: store)`` keeps
    working verbatim (a pre-seeded gateway is returned as-is).
    """
    store_factory = registry.resolve("triple_store", settings.backend)
    return store_factory(settings=settings, config_hash=config_hash)


def resolve_verified_token_budget(
    settings: OntologySettings,
    task_budget: int | None = None,
    *,
    tool_share: float = 0.5,
    max_requests: int = 8,
) -> int | None:
    """THE single site the effective verified-read token budget is computed (§4.5/§3.2).

    The master switch is the configured cap: when
    ``settings.verified_max_result_tokens`` is ``None`` (the default), packing NEVER
    engages regardless of ``task_budget`` — this is what keeps the merge-time
    behavior byte-identical on BOTH seams. When configured but no model-derived
    ``task_budget`` is supplied (the HTTP invoke route, console/humans/arc — humans
    have no context window), the configured cap is returned verbatim. When a
    ``task_budget`` is threaded (the CHAT binding, once NE-CTXB-8/NE-CTXB-3 wire the
    ContextBudget), the effective cap is the design §3.2 derivation:
    ``clamp(min(configured, floor(tool_share * task_budget / max_requests)), lower=256)``
    — the per-call share divides by ``max_requests`` so worst-case accumulated tool
    mass stays ≤ ``tool_share`` of the input budget. At THIS unit's merge both seams
    pass ``task_budget=None`` (configured-cap-only), the decided merge-time behavior."""
    configured = settings.verified_max_result_tokens
    if configured is None:
        return None
    if task_budget is None:
        return configured
    derived = int((tool_share * task_budget) // max_requests)
    return max(256, min(configured, derived))


def build_verified_answer_bindings(
    setup: VerifiedAnswerSetup,
    *,
    plugin_registry: PluginRegistry,
    ontology_settings: OntologySettings,
    store: VerifiedReadStore,
    expander: GroupExpander,
    config_hash: str,
    audit_sink: AuditSink,
    contributor_stamp_provider: Callable[[str], Awaitable[SourceAuthorityStamp | None]]
    | None = None,
    contributor_snapshot_provider: Callable[
        [tuple[str, ...]], Awaitable[Sequence[SourceAuthorityStamp]]
    ]
    | None = None,
    source_authority: CurrentSourceAuthorityReader | None = None,
    authority_access_context_hash_provider: Callable[[], str] | None = None,
    authorized_contributor_graphs_provider: Callable[[], Sequence[str]] | None = None,
    evidence: VerifiedKnowledgeEvidence | None = None,
) -> tuple[ToolBinding, ...]:
    """Convert each verified-answer tool def into a governed :class:`ToolBinding`.

    The T3 ``store`` is the ALREADY-RESOLVED gateway (:func:`resolve_triple_store` at
    the composition root — resolved ONCE, shared with the read routes); the tool defs
    come through the ``ne.plugins.verified_answer_tools`` entry point (085c). The
    principal reaches each handler through the ne-api ``TURN_PRINCIPAL`` carrier
    (fail-closed when unbound). The resulting bindings run ONLY inside the frozen
    ``GovernedToolExecutor`` — no ungoverned path is created.

    NE-164b: ``audit_sink`` (the ONE platform sink) and ``trace_id_provider=
    current_run_trace_id`` are passed through to the factory so the T3 read core
    emits its ``knowledge.verified_read`` event with the run's trace id, resolved
    inside the ``/v1/verified/invoke`` request context (the TURN_PRINCIPAL
    precedent — ``GovernedToolExecutor.execute`` has no ``trace_id`` parameter).

    NE-VG-2 (V6 shape ratchet, design §3-V6, ExtractionJob precedent at
    ``ne.brain_graph.extraction.job.ExtractionRuntime``): ``audit_sink`` is now
    REQUIRED and NON-Optional (``AuditSink``, not ``AuditSink | None``) — its own
    docstring already called it "the ONE platform sink" and its sole caller
    (``platform.py:1297``) always passes the real one, so there is no legitimate
    sink-less composition to preserve. Omission is now a pyright/``TypeError``
    construction failure. Ratchet-hardening of an already-deletion-guarded
    binding (``platform.py:1304``'s ``audit_sink=sink`` is covered end-to-end by
    ``test_ontology_api.py::test_invoke_emits_verified_read_audit_events``) — not
    a live-defect closure.
    """

    def _record_answer() -> Callable[[Mapping[str, object]], None] | None:
        grounding_recorder = current_answer_recorder()

        def _record(answer: Mapping[str, object]) -> None:
            _CAPTURED_VERIFIED_ANSWER.set(answer)
            if grounding_recorder is not None:
                grounding_recorder(answer)

        return _record

    factory = plugin_registry.resolve("verified_answer_tools", "default")
    defs = factory(
        registry=setup.registry,
        store=store,
        expander=expander,
        principal_provider=TURN_PRINCIPAL.get,
        settings=ontology_settings,
        config_hash=config_hash,
        audit_sink=audit_sink,
        trace_id_provider=current_run_trace_id,
        # NE-CTXB-5: the handler records the STRUCTURED answer to the active per-run
        # GroundingRecorder (§4.2), and the effective token budget is computed at the
        # ONE seam resolver — configured-cap-only at merge (task_budget=None).
        record_answer_provider=_record_answer if evidence is not None else current_answer_recorder,
        max_result_tokens_provider=lambda: resolve_verified_token_budget(ontology_settings),
        contributor_stamp_provider=contributor_stamp_provider,
        contributor_snapshot_provider=contributor_snapshot_provider,
        source_authority=source_authority,
        authority_access_context_hash_provider=authority_access_context_hash_provider,
        authorized_contributor_graphs_provider=authorized_contributor_graphs_provider,
        require_contributor_receipts=evidence is not None,
    )
    bindings: list[ToolBinding] = []
    for tool_def in defs:
        handler = tool_def.handler
        if evidence is not None:
            handler = _verified_evidence_handler(tool_def.handler, evidence)
        bindings.append(
            ToolBinding(
                spec=tool_def.spec,
                handler=handler,
                extract_audit_detail=tool_def.extract_audit_detail,
                # NE-TEMPLATE-ENUM (SPEC §12 item 3): the pre-gate strict-args check —
                # the NE-ACTION-VALIDATE-BEFORE-GATE seam applied to verified reads. An
                # out-of-vocabulary arg is refused LOUD (audited tool.validate[denied],
                # surfaced to the agent as a BLOCKED tool result naming the field and
                # the permitted values) BEFORE any query executes — never run into a
                # silent zero-row result indistinguishable from "no matching data".
                validate_args=_to_action_validation(tool_def.validate_args),
            )
        )
    return tuple(bindings)


def _verified_evidence_handler(
    handler: Callable[[Mapping[str, object]], Awaitable[str]],
    evidence: VerifiedKnowledgeEvidence,
) -> Callable[[Mapping[str, object]], Awaitable[str]]:
    async def _wrapped(args: Mapping[str, object]) -> str:
        existing = evidence.context_provider()
        principal = TURN_PRINCIPAL.get()
        if existing is None and CONSOLE_VERIFIED_READ.get():
            # The governed console/API invoke path (``/v1/verified/invoke``): the
            # request runs OUTSIDE any admitted Run tool call, so no canonical Run
            # evidence trail exists to claim receipts into — before this branch the
            # wrapper raised LookupError here and the console's Run button could
            # NEVER succeed on an evidence-wired deployment. Execute the identical
            # governed handler (PDP + audit + replay all intact; the route's
            # GroundingRecorder captures the structured answer via the
            # record_answer_provider seam) and skip only the Run-evidence claim.
            return await handler(args)
        if existing is None or principal is None or existing.owner_principal_id != principal.id:
            raise LookupError("existing canonical Run evidence context is unavailable")
        token = _CAPTURED_VERIFIED_ANSWER.set(None)
        try:
            protected_text = await handler(args)
            answer = _CAPTURED_VERIFIED_ANSWER.get()
        finally:
            _CAPTURED_VERIFIED_ANSWER.reset(token)
        if answer is None:
            raise LookupError("verified contributor evidence unavailable")
        protected = protected_text.encode("utf-8")
        raw_receipts = answer.get("contributor_receipts")
        if isinstance(raw_receipts, list | tuple) and raw_receipts:
            from ne.contracts.source_authority import (
                VerifiedEmptyResultReceipt,
                VerifiedRowContributorReceipt,
            )

            receipts = tuple(
                VerifiedRowContributorReceipt.model_validate(item) for item in raw_receipts
            )
            # NE-KG-MULTIKEY partial coverage: a list read that matched SOME members
            # minted an empty-member receipt for the ones that matched nothing. Claim
            # it into the SAME evidence set the rows ride, so the durable trail (and
            # the projection built from it) can distinguish "asked for N, matched
            # fewer" from a fully-matched read. Dropping it here — the pre-fix
            # behavior — left that fact alive only inside the encrypted rendering,
            # which is exactly how a VERIFIED badge ends up on an answer whose
            # retrieval came back short with nobody outside the model ever told.
            raw_empty_member = answer.get("empty_member_receipt")
            empty_member_receipt = (
                VerifiedEmptyResultReceipt.model_validate(raw_empty_member)
                if raw_empty_member is not None
                else None
            )
            claimed = await evidence.author.claim_verified_set(
                existing=existing,
                protected_result=protected,
                receipts=receipts,
                empty_member_receipt=empty_member_receipt,
            )
        else:
            raw_empty = answer.get("empty_result_receipt")
            if raw_empty is None:
                raise LookupError("verified contributor evidence unavailable")
            from ne.contracts.source_authority import VerifiedEmptyResultReceipt

            empty_receipt = VerifiedEmptyResultReceipt.model_validate(raw_empty)
            claimed = await evidence.author.claim_verified_empty(
                existing=existing,
                protected_result=protected,
                receipt=empty_receipt,
            )
        return claimed.protected_ref

    return _wrapped


def _to_action_validation(
    validate: Callable[[Mapping[str, object]], None],
) -> Callable[[Mapping[str, object]], None]:
    """Translate the runtime-free ``VerifiedAnswerArgsError`` into the runtime's typed
    :class:`~ne.runtime.errors.ActionValidationError` (the type conversion lives at
    THIS seam because ``ne.brain_ontology`` may not import ``ne.runtime`` — C3). The
    message passes through verbatim: it is already C12-safe by construction
    (``describe_invalid_args`` — fields + published enum values, never the rejected
    value). Any OTHER exception propagates untouched; the executor's own validate
    step wraps it fail-closed naming only the crashing class.
    """

    def _validate(args: Mapping[str, object]) -> None:
        from ne.brain_ontology.verified.errors import VerifiedAnswerArgsError

        try:
            validate(args)
        except VerifiedAnswerArgsError as exc:
            # ``from None`` — DEFENSE IN DEPTH at this link specifically, and no wider
            # claim than that. The value cannot reach here today: the two upstream
            # seams that wrap pydantic (``verified/answers.py``, ``verified/tools.py``)
            # already sever, so the only cause available is the C12-safe
            # ``VerifiedAnswerArgsError``. Severing anyway means a future re-chaining
            # upstream cannot ride THIS link into ``traceback.format_exception``
            # output. Pinned by the ``__cause__``/``__suppress_context__`` asserts in
            # ``test_out_of_vocabulary_arg_refused_pre_gate_with_audited_validate_denied``
            # (the sentinel sweep beside them cannot discriminate this link alone —
            # it fires only when an upstream seam is ALSO unsevered).
            raise ActionValidationError(str(exc)) from None

    return _validate


@dataclass(frozen=True)
class ShapeInfo:
    """One compiled SHACL node shape: its IRI, its ``sh:targetClass`` (if any), scope."""

    iri: str
    target_class: str | None
    scope: str


@dataclass(frozen=True)
class OntologyReadState:
    """The precomputed T3 read surface the ``routers/ontology.py`` handlers serve.

    ``None`` on :class:`~ne.api.deps.ApiState` ⇒ the routes answer an honest 503
    (``ontology-disabled``). ``store`` is the C12-gated gateway (``sparql`` steward
    surface); ``quarantine`` is the inspectable lane (``None`` ⇒ ``configured=false``);
    ``verified`` is the loaded verified-answer setup (``None`` ⇒ verified answers off);
    ``shapes`` is the precomputed compiled-shape list + its ``shapes_hash``.
    """

    settings: OntologySettings
    store: ValidatingTripleStore
    quarantine: TripleQuarantine | None
    verified: VerifiedAnswerSetup | None
    shapes: tuple[ShapeInfo, ...]
    shapes_hash: str


def build_ontology_read_state(
    settings: OntologySettings,
    store: ValidatingTripleStore,
    verified: VerifiedAnswerSetup | None,
) -> OntologyReadState:
    """Precompute the :class:`OntologyReadState` (compiled shapes + quarantine lane).

    LAZY ``ne.brain_ontology`` / rdflib imports (this module's documented discipline —
    importing the module drags no rdflib). The SHACL shape list is derived from the
    SAME compiled shapes the gateway validates writes against, so the ``shapes_hash``
    on the wire can never drift from what the store enforces. Only ``sh:NodeShape``
    subjects that are ``rdflib.URIRef`` are surfaced (blank-node shapes are skipped —
    they have no stable IRI to inspect).
    """
    from rdflib import RDF, URIRef
    from rdflib.namespace import SH

    from ne.brain_ontology.quarantine import SqliteTripleQuarantine
    from ne.brain_ontology.shapes import load_compiled_shapes

    quarantine: TripleQuarantine | None = (
        SqliteTripleQuarantine(settings.quarantine_db)
        if settings.quarantine_db is not None
        else None
    )
    compiled = load_compiled_shapes()
    shapes: list[ShapeInfo] = []
    for graph, scope in (
        (compiled.per_write_graph(), "per-write"),
        (compiled.global_graph(), "global"),
    ):
        for subject in graph.subjects(RDF.type, SH.NodeShape):
            if not isinstance(subject, URIRef):
                continue  # blank-node shapes have no stable inspectable IRI
            target = graph.value(subject, SH.targetClass)
            shapes.append(
                ShapeInfo(
                    iri=str(subject),
                    target_class=str(target) if target is not None else None,
                    scope=scope,
                )
            )
    shapes.sort(key=lambda s: (s.scope, s.iri))
    return OntologyReadState(
        settings=settings,
        store=store,
        quarantine=quarantine,
        verified=verified,
        shapes=tuple(shapes),
        shapes_hash=compiled.shapes_hash,
    )
