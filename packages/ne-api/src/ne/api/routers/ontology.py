# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Ontology + verified-answer routes (NE-135) — the governed T3 HTTP surface (§9/§4.6).

Exposes the Brain-Tier-3 ontology + verified-answer layer over governed HTTP for the
flagship UI, REUSING the existing lanes — no reimplementation:

* the four GET reads (``/v1/ontology/classes`` / ``/shapes`` / ``/violations`` and
  ``/v1/verified/templates``) ride the precomputed :class:`OntologyReadState`: the
  class browser reads the gateway's C12 ``sparql`` steward surface (SELECT/ASK-only,
  banned-keyword refusal) with a FIXED module-constant query filtered to the
  ``urn:ne:ontology:*`` schema graphs (the full-union steward surface can never leak
  instance IRIs); the shape list + shapes hash are served from the compiled shapes; the
  violation feed is the inspectable quarantine ``.list()`` REDACTED to counts/ids/hashes
  (no ``delta_nt``, no violation message strings, C12); the templates are the loaded
  registry.
* ``POST /v1/verified/invoke`` routes EXCLUSIVELY through the frozen
  ``GovernedToolExecutor`` (``read.kg.<id>`` tier-0 tool — PDP + audit + guardrails +
  C7 DatasetSpec trimming all reused): an unknown/denied/version-mismatched template
  FAILS CLOSED (audited 404 / 403 / 409, the executor never reached) and a raw
  SPARQL/store path is NEVER offered. It executes TWICE inside one ``TURN_PRINCIPAL``
  window for the replay diff (``answers_equal``).

Every route is OIDC-gated (the ``/api`` mount) and answers an honest 503
``ontology-disabled`` when no T3 store is configured. This module NEVER imports
``ne.runtime.middleware`` or ``ne.gateway`` (the ``test_no_bypass`` source scan), and
every ``ne.brain_ontology`` import is LAZY inside a handler (the module never drags
rdflib at import — the ``verified_tools`` discipline).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Literal

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException

from ne.api.authz import emit_surface_audit, require_role
from ne.api.deps import (
    ApiState,
    api_state,
    bind_authenticated_request_context,
    current_principal,
)
from ne.api.errors import _BACKEND_ERRORS
from ne.api.ontology_model import (
    ActiveOntologyModelView,
    ArtifactHealthView,
    OntologyGraphStateView,
    OntologyIssueDetailView,
    OntologyIssueReasonView,
    OntologyIssueTrackingView,
    OntologyModelResponse,
)
from ne.api.tools import TURN_PRINCIPAL
from ne.api.verified_tools import CONSOLE_VERIFIED_READ
from ne.api.wire import (
    OntologyClassesResponse,
    OntologyClassView,
    OntologyShapesResponse,
    OntologyShapeView,
    OntologyViolationsResponse,
    OntologyViolationView,
    VerifiedAnswerView,
    VerifiedInvokeRequest,
    VerifiedInvokeResponse,
    VerifiedTemplatesResponse,
    VerifiedTemplateView,
)
from ne.contracts.types import Principal
from ne.govern.errors import GuardrailViolation
from ne.runtime.errors import (
    ActionValidationError,
    ApprovalNotGrantedError,
    ToolDeniedError,
    ToolExecutionError,
    ToolNotRegisteredError,
)
from ne.schema import AuditOutcome

if TYPE_CHECKING:
    from ne.api.verified_tools import OntologyReadState

router = APIRouter(dependencies=[Depends(bind_authenticated_request_context)])

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]

_RESOURCE = "api:verified"
_ONTOLOGY_RESOURCE = "api:ontology"

#: The ONE fixed SPARQL string the class browser runs through the C12 steward surface.
#: The ``urn:ne:ontology:*`` graph filter is LOAD-BEARING: ``store.sparql`` is the
#: full-union steward surface, so without the filter an authenticated caller could read
#: instance-data IRIs from the per-ACL graphs. The query names no banned keyword (only
#: GRAPH/OPTIONAL/FILTER/STRSTARTS/isIRI — SPARQL 1.1 core) and is SELECT-form.
_CLASSES_QUERY = (
    "SELECT DISTINCT ?class ?label ?parent WHERE { "
    "GRAPH ?g { "
    "?class a <http://www.w3.org/2002/07/owl#Class> . "
    "OPTIONAL { ?class <http://www.w3.org/2000/01/rdf-schema#label> ?label } "
    "OPTIONAL { ?class <http://www.w3.org/2000/01/rdf-schema#subClassOf> ?parent . "
    "FILTER(isIRI(?parent)) } "
    "} "
    "FILTER(isIRI(?class)) "
    'FILTER(STRSTARTS(STR(?g), "urn:ne:ontology:")) '
    "}"
)


def _require_ontology(state: ApiState) -> OntologyReadState:
    """Fail-closed T3 gate: an unconfigured ontology answers 503 ``ontology-disabled``."""
    if state.ontology is None:
        raise HTTPException(status_code=503, detail="ontology-disabled")
    return state.ontology


def _source_for(iri: str) -> str:
    """Classify a class IRI into its corpus source (bfo / cco / neop / other)."""
    if iri.startswith("http://purl.obolibrary.org/obo/BFO_"):
        return "bfo"
    if iri.startswith("https://www.commoncoreontologies.org/"):
        return "cco"
    if iri.startswith("https://neutron.enterprise/schema/"):
        return "neop"
    return "other"


def _class_views(rows: Sequence[Mapping[str, Any]]) -> tuple[OntologyClassView, ...]:
    """Project fixed-query rows into the deterministic compatibility class view."""
    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        cls = row.get("class")
        if not isinstance(cls, str) or not cls:
            continue
        entry = grouped.setdefault(cls, {"label": None, "parents": set()})
        label = row.get("label")
        if entry["label"] is None and isinstance(label, str) and label:
            entry["label"] = label
        parent = row.get("parent")
        if isinstance(parent, str) and parent:
            parents = entry["parents"]
            assert isinstance(parents, set)
            parents.add(parent)

    classes: list[OntologyClassView] = []
    for iri in sorted(grouped):
        entry = grouped[iri]
        parents = entry["parents"]
        assert isinstance(parents, set)
        label_val = entry["label"]
        classes.append(
            OntologyClassView(
                iri=iri,
                label=label_val if isinstance(label_val, str) else None,
                parents=tuple(sorted(parents)),
                source=_source_for(iri),
            )
        )
    return tuple(classes)


def _model_failure(state: Literal["invalid", "unavailable"], detail: str) -> OntologyModelResponse:
    return OntologyModelResponse(
        model=None,
        artifact_health=ArtifactHealthView(state=state, detail=detail),
        ontology_graph=OntologyGraphStateView(state="unavailable", optional_class_count=None),
        issue_tracking=OntologyIssueTrackingView(state="unavailable", count=None),
        validation_health="unavailable",
    )


@router.get("/v1/ontology/model")
async def ontology_model(principal: _Principal, state: _State) -> OntologyModelResponse:
    """The active generated model plus honest optional-graph and issue state.

    The generated operational model exists independently of the optional ontology
    graph/store.  Therefore an unconfigured T3 runtime still returns the shared
    model and says ``not_loaded``/``unconfigured`` rather than claiming zero
    concepts or failing the model read.
    """
    try:
        from ne.brain_ontology.inspection import (
            ProjectionIntegrityError,
            load_active_model_projection,
        )
    except (ImportError, OSError):
        return _model_failure("unavailable", "generated-artifacts-unavailable")
    try:
        projection = load_active_model_projection()
    except ProjectionIntegrityError:
        return _model_failure("invalid", "generated-artifact-mismatch")
    except ValueError:
        return _model_failure("invalid", "generated-artifact-mismatch")
    except OSError:
        return _model_failure("unavailable", "generated-artifacts-unavailable")

    ontology = state.ontology
    if ontology is None:
        return OntologyModelResponse(
            model=ActiveOntologyModelView.from_projection(projection),
            artifact_health=ArtifactHealthView(state="healthy"),
            ontology_graph=OntologyGraphStateView(state="not_loaded", optional_class_count=0),
            issue_tracking=OntologyIssueTrackingView(state="unconfigured", count=None),
            validation_health="unavailable",
        )

    try:
        classes = await ontology_classes(principal, state)
    except HTTPException as exc:
        if exc.status_code != 502:
            raise
        graph = OntologyGraphStateView(state="unavailable", optional_class_count=None)
    else:
        graph = OntologyGraphStateView(
            state="loaded" if classes.total else "not_loaded",
            optional_class_count=classes.total,
        )

    version = ontology.store.ontology_version()
    expected_hash = projection.provenance.shapes_sha256
    if ontology.shapes_hash != expected_hash or version.shapes_hash != expected_hash:
        return OntologyModelResponse(
            model=None,
            artifact_health=ArtifactHealthView(
                state="invalid", detail="compiled-shapes-fingerprint-mismatch"
            ),
            ontology_graph=graph,
            issue_tracking=OntologyIssueTrackingView(state="unavailable", count=None),
            validation_health="unavailable",
        )

    model = ActiveOntologyModelView.from_projection(projection, source_versions=version.sources)
    quarantine = ontology.quarantine
    if quarantine is None:
        issue_tracking = OntologyIssueTrackingView(state="unconfigured", count=None)
        validation_health = "unavailable"
    else:
        try:
            entries = await anyio.to_thread.run_sync(quarantine.list)
        except (OSError, sqlite3.Error):
            issue_tracking = OntologyIssueTrackingView(state="unavailable", count=None)
            validation_health = "unavailable"
        else:
            issue_tracking = OntologyIssueTrackingView(state="configured", count=len(entries))
            validation_health = "issues" if entries else "healthy"
    return OntologyModelResponse(
        model=model,
        artifact_health=ArtifactHealthView(state="healthy"),
        ontology_graph=graph,
        issue_tracking=issue_tracking,
        validation_health=validation_health,
    )


@router.get("/v1/ontology/classes")
async def ontology_classes(principal: _Principal, state: _State) -> OntologyClassesResponse:
    """The BFO/CCO/neop class browser tree — sourced ONLY from urn:ne:ontology:* graphs."""
    ontology = _require_ontology(state)
    try:
        rows = await ontology.store.sparql(_CLASSES_QUERY)
    except _BACKEND_ERRORS as exc:
        # Class name ONLY — never a backend URL / repr (C12).
        raise HTTPException(status_code=502, detail=type(exc).__name__) from exc

    classes = _class_views(rows)

    version = ontology.store.ontology_version()
    return OntologyClassesResponse(
        classes=classes,
        total=len(classes),
        sources=dict(version.sources),
        shapes_hash=version.shapes_hash,
    )


@router.get("/v1/ontology/shapes")
async def ontology_shapes(principal: _Principal, state: _State) -> OntologyShapesResponse:
    """The compiled SHACL shape list (iri + target class + per-write/global scope)."""
    ontology = _require_ontology(state)
    return OntologyShapesResponse(
        shapes=tuple(
            OntologyShapeView(iri=s.iri, target_class=s.target_class, scope=s.scope)
            for s in ontology.shapes
        ),
        shapes_hash=ontology.shapes_hash,
    )


@router.get("/v1/ontology/violations")
async def ontology_violations(principal: _Principal, state: _State) -> OntologyViolationsResponse:
    """The ShapeViolation quarantine feed — REDACTED (counts/ids/hashes only, C12)."""
    ontology = _require_ontology(state)
    quarantine = ontology.quarantine
    if quarantine is None:
        return OntologyViolationsResponse(configured=False, count=0, entries=())
    # The sync stdlib-sqlite .list() runs in ONE threadpool hop (the audit-router
    # precedent) so the event loop never blocks.
    entries = await anyio.to_thread.run_sync(quarantine.list)
    views = tuple(
        OntologyViolationView(
            quarantine_id=e.quarantine_id,
            created_at=e.created_at,
            graph_iri=e.graph_iri,
            shapes_hash=e.shapes_hash,
            # COUNT ONLY — never the violation message strings or delta_nt (C12).
            violation_count=len(e.violations),
            # The gateway quarantines whole batches (no partial writes).
            quarantined_whole=True,
            provenance=e.provenance,
        )
        for e in entries
    )
    return OntologyViolationsResponse(configured=True, count=len(views), entries=views)


def _safe_issue_reason(value: str) -> OntologyIssueReasonView:
    """Parse the validator's documented redaction-safe three-part rendering."""
    affected, separator, remainder = value.partition(" ")
    rule, second_separator, reason = remainder.partition(" ")
    if not separator or not second_separator or not reason.strip():
        return OntologyIssueReasonView(
            affected_reference=None,
            rule_reference=None,
            reason="Validation rule was not satisfied",
        )
    return OntologyIssueReasonView(
        affected_reference=None if affected == "-" else affected,
        rule_reference=None if rule == "-" else rule,
        reason=reason,
    )


@router.get("/v1/ontology/violations/{quarantine_id}")
async def ontology_violation_detail(
    quarantine_id: str,
    principal: _Principal,
    state: _State,
) -> OntologyIssueDetailView:
    """One safe issue detail, role-gated and inspect-only (never the rejected delta)."""
    await require_role(
        principal,
        state.settings.ops_roles,
        sink=state.sink,
        audit_action="api.ontology.issue_detail.unauthorized",
        resource=_ONTOLOGY_RESOURCE,
        http_detail="not authorized",
        request_id=quarantine_id,
    )
    ontology = _require_ontology(state)
    quarantine = ontology.quarantine
    if quarantine is None:
        raise HTTPException(status_code=503, detail="issue-tracking-not-configured")
    entry = await anyio.to_thread.run_sync(quarantine.get, quarantine_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="validation-issue-not-found")
    return OntologyIssueDetailView(
        issue_id=entry.quarantine_id,
        created_at=entry.created_at,
        graph_iri=entry.graph_iri,
        shapes_sha256=entry.shapes_hash,
        provenance=entry.provenance,
        reasons=tuple(_safe_issue_reason(reason) for reason in entry.violations),
    )


@router.get("/v1/verified/templates")
async def verified_templates(principal: _Principal, state: _State) -> VerifiedTemplatesResponse:
    """The registered verified-answer templates (id@version, title, args, citation)."""
    ontology = _require_ontology(state)
    setup = ontology.verified
    if setup is None:
        return VerifiedTemplatesResponse(enabled=False, registry_hash=None, templates=())
    templates = tuple(
        VerifiedTemplateView(
            template_id=t.template_id,
            version=t.version,
            # DECIDED (NE-135): the registry has no separate title field — use description.
            title=t.description,
            tool_name=t.tool_name,
            args_schema=t.args_json_schema(),
            columns=t.answer.columns,
            citation_var=t.answer.citation_var,
            label_var=t.answer.label_var,
            max_rows=t.max_rows,
            graph_classes=t.scope.graph_classes,
        )
        for t in setup.registry.templates
    )
    return VerifiedTemplatesResponse(
        enabled=True,
        registry_hash=setup.fingerprint.registry_hash,
        templates=templates,
    )


@router.post("/v1/verified/invoke")
async def verified_invoke(
    body: VerifiedInvokeRequest, principal: _Principal, state: _State
) -> VerifiedInvokeResponse:
    """Execute a verified-answer template ONLY through the frozen GovernedToolExecutor.

    Fail-closed pre-checks (the executor is never reached on a bad request): an unknown
    template is an audited 404 ``unknown-template``, a version mismatch an audited 409
    — NEVER a raw query. A template that IS staged but whose executor binding is
    missing is a DIFFERENT 404, ``template-tool-unregistered``: the first is a
    deployment not holding a template, the second is a platform regression, and
    callers that tolerate the first must not silently tolerate the second.
    Then the C6 chokepoint runs TWICE inside ONE ``TURN_PRINCIPAL`` window (the chat.py
    pattern) so the replay diff (``answers_equal``) is honest. The executor still emits
    ``pdp.decide`` + ``tool.execute`` (C6) as before; NE-164b ADDITIONALLY emits one
    ``knowledge.verified_read`` read-path event FROM the ``execute_template`` core per
    execution — i.e. TWO per invoke request (initial + replay), each an actual store
    read. Trace threading rides the injected ``trace_id_provider`` (the TURN_PRINCIPAL
    pattern), resolved inside THIS route's request context (``ne.api.verified_tools``
    binds it to ``current_run_trace_id``).
    """
    ontology = _require_ontology(state)
    setup = ontology.verified
    if setup is None:
        raise HTTPException(status_code=503, detail="verified-answers-disabled")

    template = next(
        (t for t in setup.registry.templates if t.template_id == body.template_id), None
    )
    if template is None:
        await emit_surface_audit(
            state.sink,
            action="api.verified.invoke",
            resource=_RESOURCE,
            outcome=AuditOutcome.denied,
            actor=principal.id,
            detail={"reason": "unknown-template", "template_id": body.template_id},
        )
        raise HTTPException(status_code=404, detail="unknown-template")
    if body.version is not None and body.version != template.version:
        await emit_surface_audit(
            state.sink,
            action="api.verified.invoke",
            resource=_RESOURCE,
            outcome=AuditOutcome.denied,
            actor=principal.id,
            detail={"reason": "version-mismatch", "template_id": body.template_id},
        )
        raise HTTPException(status_code=409, detail="template-version-mismatch")

    # The C6 chokepoint, TWICE for the replay diff, inside ONE TURN_PRINCIPAL window
    # (chat.py:122-131 verbatim). The principal reaches the handler ONLY via this
    # ContextVar, set from the OIDC-verified bearer, and reset in finally.
    #
    # NE-CTXB-5: the handler now returns the COMPACT rendering, not JSON, and records
    # the STRUCTURED VerifiedAnswer to the active per-run GroundingRecorder (§4.2). A
    # fresh recorder wraps the SAME frozen executor for the SAME TURN_PRINCIPAL window
    # (no new tool-execution surface — it only delegates), so both executions record
    # their structured answer; ``model_validate_json(first/second)`` is dead.
    from ne.runtime.grounding import GroundingRecorder

    recorder = GroundingRecorder(state.executor)
    token = TURN_PRINCIPAL.set(principal)
    # This is the run-less console/API verified read: no admitted Run tool call is
    # in flight, so there is no Run evidence trail to claim receipts into. The
    # marker tells the evidence wrapper to execute the governed handler without
    # that claim (see CONSOLE_VERIFIED_READ) — without it, every console invoke on
    # an evidence-wired deployment died with a 502 "LookupError".
    console_token = CONSOLE_VERIFIED_READ.set(True)
    try:
        await recorder.execute(template.tool_name, body.args, principal=principal)
        await recorder.execute(template.tool_name, body.args, principal=principal)
    except ToolDeniedError as exc:
        raise HTTPException(status_code=403, detail="tool-denied") from exc
    except ApprovalNotGrantedError as exc:
        raise HTTPException(status_code=403, detail="approval-required") from exc
    except ToolNotRegisteredError as exc:
        # DISTINCT FROM the registry pre-check's ``unknown-template`` above, and it
        # must stay that way. The template IS staged in this deployment's frozen
        # registry, so the id is known — what is missing is its executor BINDING,
        # which is a platform regression, not a configuration fact. They shared one
        # body until now, and the demo arc's not-staged predicate (which downgrades
        # the benign case to DEGRADE) could not tell them apart, so a lost binding
        # was reported as an absent template and ``ne demo run`` exited zero. Still
        # 404 (the id resolves to nothing executable) and still a status token only,
        # never a message (C12). The executor already audited it (``tool.unknown``,
        # reason "tool not registered") before raising.
        raise HTTPException(status_code=404, detail="template-tool-unregistered") from exc
    except GuardrailViolation as exc:
        raise HTTPException(status_code=422, detail="guardrail-violation") from exc
    except ActionValidationError as exc:
        # NE-TEMPLATE-ENUM: the pre-gate strict-args refusal (audited as
        # tool.validate[denied] by the executor). Same 422 detail as the
        # defense-in-depth path below — status token only, never a message (C12).
        raise HTTPException(status_code=422, detail="invalid-args") from exc
    except ToolExecutionError as exc:
        cause = exc.__cause__
        if cause is not None and type(cause).__name__ == "VerifiedAnswerArgsError":
            raise HTTPException(status_code=422, detail="invalid-args") from exc
        # Class NAME only — never a message / URL (C12).
        detail = type(cause).__name__ if cause is not None else "ToolExecutionError"
        raise HTTPException(status_code=502, detail=detail) from exc
    finally:
        CONSOLE_VERIFIED_READ.reset(console_token)
        TURN_PRINCIPAL.reset(token)

    # Both executions must have recorded a structured answer; a missing record is a
    # loud 502 (fail loud), never a silent partial response.
    if len(recorder.answers) != 2:
        raise HTTPException(status_code=502, detail="verified-record-missing")

    # LAZY import (the module's rdflib-free-at-import discipline).
    from ne.brain_ontology.verified import VerifiedAnswer, answers_equal

    a1 = VerifiedAnswer.model_validate(dict(recorder.answers[0]))
    a2 = VerifiedAnswer.model_validate(dict(recorder.answers[1]))
    replay_match = answers_equal(a1, a2)
    answer = VerifiedAnswerView(
        template_id=a1.template_id,
        template_version=a1.template_version,
        args=a1.args,
        columns=a1.columns,
        rows=a1.rows,
        citations=a1.citations,
        truncated=a1.truncated,
        row_limit=a1.row_limit,
        returned_rows=a1.returned_rows,
        total_rows=a1.total_rows,
        truncation_reason=a1.truncation_reason,
        config_hash=a1.config_hash,
        ontology_version=a1.ontology_version,
        executed_at=a1.executed_at,
    )
    return VerifiedInvokeResponse(answer=answer, replay_match=replay_match)
