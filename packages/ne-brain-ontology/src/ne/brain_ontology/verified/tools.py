# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Pattern derived from jupyter-naas/abi
# libs/naas-abi-core/naas_abi_core/modules/templatablesparqlquery/workflows/
# TemplatableSparqlQueryLoader.py @ a3e15eff03902091f23acfd9edbc95dc9695183f
# — MIT License, Copyright (C) 2025 Naas.ai. The "SPARQL template → governed agent
# tool with typed argument validation" DESIGN is ported; the implementation is a
# REWRITE off LangChain (ABI built LangChain StructuredTools) onto ToolSpec-based,
# runtime-free tool DEFINITIONS. NO LangChain dependency is introduced, and no ABI
# code is vendored verbatim (see vendor/MANIFEST.toml).
"""Verified-answer tool DEFINITIONS — the entry-point factory (NE-085c).

This is the ``ne.plugins.verified_answer_tools`` factory. It returns runtime-free
:class:`VerifiedAnswerToolDef`\\s: a :class:`ne.contracts.runtime.ToolSpec` (allowed
by the brain-ontology import lane) + an async handler + plain-data guardrail arg
specs + a redacted audit-detail extractor. The ne-api composition seam converts each
def into a standard governed binding executed ONLY through the frozen
``GovernedToolExecutor`` — the binding + arg-policy types live in
``ne.runtime``/``ne.govern``, which this package MAY NOT import (C3, the merged
brain-ontology-lane contract), so the conversion is the SPEC row's sanctioned
"additive binding registration" fallback and lives at the seam, not here.

Each handler CLOSURE resolves the acting principal from the injected
``principal_provider`` (the ne-api ``TURN_PRINCIPAL.get`` carrier); ``None`` fails
CLOSED (mirrors ne-api ``tools.py`` discipline). No free-form-SPARQL parameter
exists anywhere on the public surface — the factory only ever takes a validated
:class:`TemplateRegistry`.

NE-164b run-trace threading: this is the TURN_PRINCIPAL ``principal_provider``
pattern applied to the NE-166 run-trace ContextVar. ``build_verified_answer_tools``
takes an additive ``trace_id_provider`` — a zero-arg callable the ne-api seam binds
to ``current_run_trace_id`` — so the run's trace id reaches the T3 read core AS
DATA, never as an import: ``ne.runtime`` is NEVER imported here (guardian:
``tests/test_verified_no_ungoverned_path.py::test_importing_verified_pulls_no_runtime_or_govern``).
The additive kwargs keep that guardian's ``inspect.signature`` check green too —
it only asserts ``'registry'`` is present and no ``sparql``/``query``/``query_text``
parameter exists.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.verified.answers import (
    VerifiedReadStore,
    execute_template,
    ontology_version_string,
    render_answer_text,
)
from ne.brain_ontology.verified.scope import GroupExpander
from ne.brain_ontology.verified.templates import TemplateRegistry
from ne.contracts.governance import AuditSink
from ne.contracts.runtime import ToolSpec
from ne.contracts.source_authority import CurrentSourceAuthorityReader, SourceAuthorityStamp
from ne.contracts.types import ApprovalTier, Principal

__all__ = [
    "CATALOG_SHAPE_CLAUSE",
    "GuardrailArgSpec",
    "VerifiedAnswerToolDef",
    "build_verified_answer_tools",
]

#: rdflib arg types → the deterministic guardrail validation CLASS (plain strings —
#: NO ne.govern import here; the strict validation is the generated Pydantic model
#: inside the handler, so the guardrail layer is a redundant defensive floor, C12).
_KIND_FOR_TYPE: Mapping[str, str] = {
    "str": "text",
    "iri": "text",
    "date": "text",
    "datetime": "text",
    "int": "number",
    "bool": "boolean",
}


#: Appended to EVERY verified-answer tool's catalog description (NE-LANE-CHOICE).
#:
#: A template's own ``description`` says WHICH records it reads; nothing in it said
#: what SHAPE comes back, and shape is what a model choosing between this lane and a
#: document search has to know. Every template here is ``result_shape='enumerable'``
#: (``ScopeSpec._check_result_shape`` refuses anything else), so this sentence is
#: true of all of them by construction rather than by an author remembering to write
#: it, and a template added later inherits it.
#:
#: WHY THE VOCABULARY IS THE POINT. Live 2026-08-27, a question asking for a total
#: recorded AMOUNT went to prose search and came back "not found" while a granted
#: template held the rows. The words a record question is actually built from —
#: amounts, dates, ids, statuses — appeared nowhere in this lane's catalog entries,
#: so nothing connected the question to the tools that answer it. This states them
#: once per entry.
#:
#: IT DOES NOT MAKE THE CHOICE. It adds no imperative, ranks no tool above another
#: and mentions no other tool; a question whose answer really is in prose still has
#: every reason to search prose. It is a fact about the return value, in the field
#: whose whole job is to describe the return value.
CATALOG_SHAPE_CLAUSE = (
    " Returns exact stored field values (ids, amounts, dates, statuses), one row per record."
)


@dataclass(frozen=True)
class GuardrailArgSpec:
    """Plain-data guardrail arg spec (converted to a ne.govern ArgSpec at the seam)."""

    name: str
    kind: str
    max_length: int
    required: bool = True
    #: NE-KG-MULTIKEY: mirrors ``ArgDef.list_max`` (``None`` = scalar-only). The
    #: ne-api seam copies it onto the govern ``ArgSpec`` so the C12 guardrail floor
    #: admits the same bounded array the generated Pydantic model admits.
    list_max: int | None = None


@dataclass(frozen=True)
class VerifiedAnswerToolDef:
    """A runtime-free governed-tool definition the ne-api seam wires into a binding."""

    spec: ToolSpec
    handler: Callable[[Mapping[str, object]], Awaitable[str]]
    guardrail_args: tuple[GuardrailArgSpec, ...]
    extract_audit_detail: Callable[[Mapping[str, object], str], Mapping[str, Any]]
    #: NE-TEMPLATE-ENUM (SPEC §12 item 3): the PRE-GATE strict-args check the ne-api
    #: seam wires into the runtime binding's ``validate_args`` (the type conversion
    #: lives at that seam — this package never names the runtime types, C3).
    #: Validates against the SAME
    #: generated args model the handler re-checks (one vocabulary source), raising
    #: :class:`~ne.brain_ontology.verified.errors.VerifiedAnswerArgsError` whose
    #: message names the field and — for a declared enum — the permitted values,
    #: NEVER the rejected value (C12; see ``describe_invalid_args``). Refusing here
    #: makes an out-of-vocabulary call a LOUD, audited ``tool.validate[denied]``
    #: refusal the agent can correct — never a silent zero-row execution.
    validate_args: Callable[[Mapping[str, object]], None]


def build_verified_answer_tools(
    *,
    registry: TemplateRegistry,
    store: VerifiedReadStore,
    expander: GroupExpander,
    principal_provider: Callable[[], Principal | None],
    settings: OntologySettings,
    config_hash: str,
    audit_sink: AuditSink | None = None,
    trace_id_provider: Callable[[], str | None] | None = None,
    record_answer_provider: Callable[[], Callable[[Mapping[str, object]], None] | None]
    | None = None,
    max_result_tokens_provider: Callable[[], int | None] | None = None,
    contributor_stamp_provider: Callable[[str], Awaitable[SourceAuthorityStamp | None]]
    | None = None,
    contributor_snapshot_provider: Callable[
        [tuple[str, ...]], Awaitable[Sequence[SourceAuthorityStamp]]
    ]
    | None = None,
    source_authority: CurrentSourceAuthorityReader | None = None,
    authority_access_context_hash_provider: Callable[[], str] | None = None,
    authorized_contributor_graphs_provider: Callable[[], Sequence[str]] | None = None,
    require_contributor_receipts: bool = False,
) -> tuple[VerifiedAnswerToolDef, ...]:
    """THE ``ne.plugins.verified_answer_tools`` factory — one governed def per template.

    NE-CTXB-5 additive kwargs (the ``trace_id_provider`` precedent — DATA, never an
    import, so the C3 no-runtime/no-govern guardian stays green):

    - ``record_answer_provider`` — a zero-arg callable the ne-api seam binds to
      ``ne.runtime.grounding.current_answer_recorder``; it resolves the ACTIVE
      per-run recorder's structured-record sink (or ``None``). The handler records
      the structured :class:`VerifiedAnswer` mapping DIRECTLY, so the recorder no
      longer parses the model-visible tool-return string (§4.2).
    - ``max_result_tokens_provider`` — a zero-arg callable the ne-api seam binds to
      the single budget resolver (``resolve_verified_token_budget``); ``None`` (the
      merge-time default on both seams) keeps packing OFF.
    """
    ontology_version = ontology_version_string(store.ontology_version())
    shapes_hash = store.ontology_version().shapes_hash
    registry_hash = registry.fingerprint.registry_hash
    max_rows = settings.verified_max_rows

    defs: list[VerifiedAnswerToolDef] = []
    for template in registry.templates:
        spec = ToolSpec(
            name=template.tool_name,
            description=f"{template.description}{CATALOG_SHAPE_CLAUSE}",
            tier=ApprovalTier.TIER_0,
            input_schema=template.args_json_schema(),
        )
        guardrail_args = tuple(
            GuardrailArgSpec(
                name=name,
                kind=_KIND_FOR_TYPE[arg.type],
                max_length=arg.max_length,
                list_max=arg.list_max,
            )
            for name, arg in template.args.items()
        )
        defs.append(
            VerifiedAnswerToolDef(
                spec=spec,
                handler=_make_handler(
                    template=template,
                    store=store,
                    expander=expander,
                    principal_provider=principal_provider,
                    config_hash=config_hash,
                    max_rows=max_rows,
                    audit_sink=audit_sink,
                    trace_id_provider=trace_id_provider,
                    record_answer_provider=record_answer_provider,
                    max_result_tokens_provider=max_result_tokens_provider,
                    contributor_stamp_provider=contributor_stamp_provider,
                    contributor_snapshot_provider=contributor_snapshot_provider,
                    source_authority=source_authority,
                    authority_access_context_hash_provider=authority_access_context_hash_provider,
                    authorized_contributor_graphs_provider=authorized_contributor_graphs_provider,
                    require_contributor_receipts=require_contributor_receipts,
                ),
                guardrail_args=guardrail_args,
                extract_audit_detail=_make_audit_detail(
                    template_id=template.template_id,
                    template_version=template.version,
                    ontology_version=ontology_version,
                    shapes_hash=shapes_hash,
                    registry_hash=registry_hash,
                ),
                validate_args=_make_validate_args(template),
            )
        )
    return tuple(defs)


def _make_validate_args(template: Any) -> Callable[[Mapping[str, object]], None]:
    """The pre-gate strict-args validator for one template (NE-TEMPLATE-ENUM).

    DERIVES from the template's generated args model — the same single source the
    handler's ``execute_template`` re-validates against — so the pre-gate refusal
    and the defense-in-depth refusal can never disagree on the vocabulary. The
    raised :class:`VerifiedAnswerArgsError` carries the C12-safe
    ``describe_invalid_args`` message (fields + permitted enum values, never the
    rejected value, and an explicit "no query was executed").
    """

    def _validate(args: Mapping[str, object]) -> None:
        from pydantic import ValidationError

        from ne.brain_ontology.verified.errors import VerifiedAnswerArgsError
        from ne.brain_ontology.verified.templates import describe_invalid_args

        try:
            validated = template.args_model.model_validate(dict(args))
        except ValidationError as exc:
            # ``from None``: pydantic's rendering embeds the rejected value as
            # ``input_value=...``; a chained cause would resurface it in
            # ``traceback.format_exception`` output. Chain-swept by test.
            raise VerifiedAnswerArgsError(describe_invalid_args(template, exc)) from None
        try:
            template.validate_semantic_args(validated)
        except ValueError as exc:
            raise VerifiedAnswerArgsError(
                f"invalid arguments for {template.tool_name!r}: {exc}. "
                "The supplied values are not echoed (C12). No query was executed — "
                "this is a refusal, not an empty result."
            ) from None

    return _validate


def _make_handler(
    *,
    template: Any,
    store: VerifiedReadStore,
    expander: GroupExpander,
    principal_provider: Callable[[], Principal | None],
    config_hash: str,
    max_rows: int,
    audit_sink: AuditSink | None = None,
    trace_id_provider: Callable[[], str | None] | None = None,
    record_answer_provider: Callable[[], Callable[[Mapping[str, object]], None] | None]
    | None = None,
    max_result_tokens_provider: Callable[[], int | None] | None = None,
    contributor_stamp_provider: Callable[[str], Awaitable[SourceAuthorityStamp | None]]
    | None = None,
    contributor_snapshot_provider: Callable[
        [tuple[str, ...]], Awaitable[Sequence[SourceAuthorityStamp]]
    ]
    | None = None,
    source_authority: CurrentSourceAuthorityReader | None = None,
    authority_access_context_hash_provider: Callable[[], str] | None = None,
    authorized_contributor_graphs_provider: Callable[[], Sequence[str]] | None = None,
    require_contributor_receipts: bool = False,
) -> Callable[[Mapping[str, object]], Awaitable[str]]:
    async def _handler(args: Mapping[str, object]) -> str:
        principal = principal_provider()
        if principal is None:  # fail closed — no request data can act as the principal
            raise RuntimeError(f"no turn principal bound; refusing to run {template.tool_name!r}")
        trace_id = trace_id_provider() if trace_id_provider is not None else None
        budget = max_result_tokens_provider() if max_result_tokens_provider is not None else None
        authority_context_hash = (
            authority_access_context_hash_provider()
            if authority_access_context_hash_provider is not None
            else ""
        )
        authorized_graphs = (
            authorized_contributor_graphs_provider()
            if authorized_contributor_graphs_provider is not None
            else None
        )
        answer = await execute_template(
            template,
            args,
            store=store,
            expander=expander,
            principal=principal,
            config_hash=config_hash,
            max_rows=max_rows,
            audit_sink=audit_sink,
            trace_id=trace_id,
            max_result_tokens=budget,
            contributor_stamp_provider=contributor_stamp_provider,
            contributor_snapshot_provider=contributor_snapshot_provider,
            source_authority=source_authority,
            authority_access_context_hash=authority_context_hash,
            authorized_contributor_graphs=authorized_graphs,
            require_contributor_receipts=require_contributor_receipts,
        )
        # Record the STRUCTURED answer DIRECTLY to the active per-run recorder (§4.2)
        # — the recorder no longer parses this handler's return string. The
        # model-visible return is now the COMPACT rendering, NEVER model_dump_json():
        # no duplicated citations array, echoed args, or hashes reach the model.
        record = record_answer_provider() if record_answer_provider is not None else None
        if record is not None:
            record(answer.model_dump(mode="json"))
        return render_answer_text(answer)

    return _handler


def _make_audit_detail(
    *,
    template_id: str,
    template_version: int,
    ontology_version: str,
    shapes_hash: str,
    registry_hash: str,
) -> Callable[[Mapping[str, object], str], Mapping[str, Any]]:
    def _detail(args: Mapping[str, object], result: str) -> Mapping[str, Any]:
        # Names / ids / shapes ONLY — never argument or result VALUES (C7/C12 redaction).
        # ``result`` is now the COMPACT rendering whose FIRST line is the envelope
        # header ``[showing {returned_rows} of ...]`` — parse the shown-row count from
        # it (the old json.loads(result) path is dead; the return is no longer JSON).
        header = re.match(r"\[showing (\d+) of ", result)
        result_count = int(header.group(1)) if header is not None else -1
        return {
            "template_id": template_id,
            "template_version": template_version,
            "args_shape": sorted(args),
            "result_count": result_count,
            "ontology_version": ontology_version,
            "shapes_hash": shapes_hash,
            "registry_hash": registry_hash,
        }

    return _detail
