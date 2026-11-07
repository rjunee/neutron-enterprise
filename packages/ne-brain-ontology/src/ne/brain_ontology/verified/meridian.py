# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Meridian Tier-0 template bundle and generator-fixture to T3 loader (BD-5).

The banking facts remain owned by ``tools/meridian``.  This module consumes that
generator's committed ``manifest.json``, ``banking.json`` and ``ground_truth.json``
bytes, verifies their hashes, and materializes a small RDF projection in dedicated
ACL-label graphs.  It never carries a second copy of a customer, loan, payment,
note, dispute, disaster declaration, or policy fact.

The three graph labels mirror the generator's persona entitlements.  A verified
answer can therefore only query the named graphs held by its calling principal;
there is no unscoped fallback.  Writes use the existing validate-then-atomic-swap
gateway and are idempotent for byte-identical generator output.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from ne.brain_ontology.acl import graph_iri_for_label
from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import (
    DeclaredLiteral,
    ValidatingTripleStore,
    build_release_staging_store,
)
from ne.brain_ontology.ontology import load_manifest, ontology_version_for
from ne.brain_ontology.shapes import load_compiled_shapes
from ne.brain_ontology.verified.answers import ontology_version_string
from ne.brain_ontology.verified.selected_release import (
    MeridianReleaseDescriptor,
    MeridianReleaseStaging,
    ReleaseClaim,
    SelectedOxigraphRelease,
    SelectedReleaseRegistry,
    release_content_hash,
)
from ne.brain_ontology.verified.templates import TemplateRegistry, load_template_registry

__all__ = [
    "MERIDIAN_CORPUS_VERSION",
    "MERIDIAN_GRAPH_LABELS",
    "MERIDIAN_LOADER_SCHEMA_VERSION",
    "MERIDIAN_TEMPLATE_IDS",
    "MeridianContributor",
    "MeridianReleaseBuild",
    "MeridianVerifiedLoadReport",
    "build_meridian_release",
    "load_meridian_template_registry",
    "load_meridian_verified_fixture",
    "meridian_template_dir",
    "publish_meridian_release",
]

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_XSD = "http://www.w3.org/2001/XMLSchema#"
_CLASS = "https://neutron.enterprise/schema/meridian/"
_PRED = _CLASS
_SUBJECT = "urn:ne:meridian:"

_CUSTOMER_LABEL = "bank.customer.read"
_SERVICING_LABEL = "bank.servicing.read"
_DISPUTE_LABEL = "bank.dispute.read"

#: The complete ACL-label graph set of a Meridian release.  A release that stages
#: anything other than exactly these labels is incomplete and selects no head.
MERIDIAN_GRAPH_LABELS: Final = (_CUSTOMER_LABEL, _DISPUTE_LABEL, _SERVICING_LABEL)

#: The one corpus this loader accepts.  It is also the manifest-declared dataset name
#: carried in every ``governed_local_t3`` source point.
MERIDIAN_CORPUS_VERSION: Final = "meridian-v1"

#: Version of THIS loader's RDF projection schema (subject IRI shape, class and
#: predicate vocabulary, literal typing).  A projection change moves it, which moves
#: the release descriptor and therefore the SourceContract hash.
MERIDIAN_LOADER_SCHEMA_VERSION: Final = 5

MERIDIAN_TEMPLATE_IDS = (
    "autoloans-for-customer",
    "customers-in-counties",
    "disaster-declarations",
    "disputes-directory",
    "hardship-candidate-loans",
    "open-disputes-by-status",
    "overdue-provisional-credit-disputes",
    "payments-due-window",
    "servicing-notes-for-loan",
    "t3-record",
    "t3-records-in-class",
)

_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "customer": re.compile(r"^mer-cus-[0-9]{3}$"),
    "loan": re.compile(r"^mer-loan-[0-9]{3}$"),
    "payment": re.compile(r"^mer-pay-[0-9]{3}$"),
    "note": re.compile(r"^mer-note-[0-9]{3}$"),
    "dispute": re.compile(r"^mer-dsp-[0-9]{3}$"),
    "policy": re.compile(r"^art-dispute-sop-v[0-9]+$"),
    "disaster": re.compile(r"^DR-[0-9]{4}$"),
}

Triple = tuple[str, str, str | DeclaredLiteral]
ReplaceOutcome = Literal["replaced", "deleted", "unchanged"]


class MeridianVerifiedLoadReport(BaseModel):
    """Deterministic evidence for one verified-data materialization."""

    model_config = ConfigDict(frozen=True)

    corpus_version: str
    fixture_hash: str
    graph_outcomes: dict[str, ReplaceOutcome]
    triple_counts: dict[str, int]
    release: MeridianReleaseDescriptor
    """The credential-free release descriptor this load materialized.

    Present on EVERY load: the stamped production path is the only path, so a report
    can never describe an unstamped legacy materialization.
    """


class MeridianContributor(BaseModel):
    """One row/entity contributor of a Meridian release.

    ``contributor_key`` is the contributor's own subject IRI — a typed, collision-free
    row/entity key, never an unversioned IRI reused as a source identity and never a
    guess from citation order.  ``content_hash`` covers exactly that contributor's
    triples, so one row's value change creates one new immutable artifact.
    """

    model_config = ConfigDict(frozen=True)

    contributor_key: str
    graph_label: str
    content_hash: str
    triple_count: int


@dataclass(frozen=True)
class MeridianReleaseBuild:
    """The complete, validated content of ONE Meridian release, before staging.

    Pure: built from the checked-in generator bytes alone.  It carries no deployment
    id, no dataset-instance id and no M365 lineage — a caller that wants per-contributor
    ``SourceIdentity``/artifact stamps must supply a
    :class:`~ne.brain_ontology.verified.selected_release.GovernedLocalT3Instance`.
    """

    descriptor: MeridianReleaseDescriptor
    graph_triples: Mapping[str, tuple[Triple, ...]]
    contributors: tuple[MeridianContributor, ...]


def meridian_template_dir() -> Path:
    """Resolve the packaged Meridian templates without repository path math."""
    resource = files("ne.brain_ontology").joinpath("assets", "verified_templates", "meridian-v1")
    root = Path(str(resource))
    if not root.is_dir():
        raise RuntimeError(f"Meridian verified-template bundle is not package data: {root}")
    return root


def load_meridian_template_registry() -> TemplateRegistry:
    """Load and fail-loud validate the complete Meridian template bundle."""
    registry = load_template_registry(meridian_template_dir())
    if registry.fingerprint.template_ids != MERIDIAN_TEMPLATE_IDS:
        raise RuntimeError(
            "Meridian verified-template bundle is incomplete or contains an unexpected "
            f"template: {registry.fingerprint.template_ids!r}"
        )
    return registry


def _read_verified_json(
    fixture_dir: Path, manifest: Mapping[str, Any], filename: str
) -> tuple[dict[str, Any], bytes]:
    expected = manifest.get("sha256", {}).get(filename)
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError(f"Meridian manifest has no valid sha256 for {filename!r}")
    raw = (fixture_dir / filename).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError(
            f"Meridian fixture {filename!r} disagrees with manifest sha256 "
            "(refusing stale or modified source truth)"
        )
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"Meridian fixture {filename!r} must contain a JSON object")
    return value, raw


def _rows(document: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = document.get(key)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError(f"Meridian fixture key {key!r} must be a list of objects")
    return value


def _required(row: Mapping[str, Any], key: str) -> Any:
    if key not in row or row[key] is None:
        raise ValueError(f"Meridian source row is missing required field {key!r}")
    return row[key]


def _validate_data_entitlements(banking: Mapping[str, Any]) -> None:
    """Bind the three T3 graph labels to BD-1's generated persona entitlements."""
    by_entitlement: dict[str, set[str]] = {}
    seen_personas: set[str] = set()
    for row in _rows(banking, "personas"):
        persona_id = str(_required(row, "persona_id"))
        if persona_id in seen_personas:
            raise ValueError("Meridian source contains a duplicate persona_id")
        seen_personas.add(persona_id)
        entitlements = _required(row, "entitlements")
        if not isinstance(entitlements, list) or not all(
            isinstance(value, str) for value in entitlements
        ):
            raise ValueError("Meridian persona entitlements must be a list of strings")
        for entitlement in entitlements:
            by_entitlement.setdefault(entitlement, set()).add(persona_id)

    expected = {
        _CUSTOMER_LABEL: {"ops", "approver", "frontline"},
        _SERVICING_LABEL: {"ops", "approver"},
        _DISPUTE_LABEL: {"ops", "approver"},
    }
    for label, personas in expected.items():
        if by_entitlement.get(label, set()) != personas:
            raise ValueError(
                "Meridian generated persona entitlements disagree with the verified "
                f"data-graph policy for {label!r}"
            )


def _identifier(kind: str, value: object) -> str:
    text = str(value)
    if _ID_PATTERNS[kind].fullmatch(text) is None:
        raise ValueError(f"Meridian {kind} identifier has an invalid shape")
    return text


def _iri(kind: str, identifier: str) -> str:
    return f"{_SUBJECT}{kind}:{quote(identifier, safe='')}"


def _plain(value: object) -> DeclaredLiteral:
    return DeclaredLiteral(str(value))


def _integer(value: object) -> DeclaredLiteral:
    if isinstance(value, bool):
        raise ValueError("Meridian integer field cannot be boolean")
    if not isinstance(value, (int, str)):
        raise ValueError("Meridian integer field must be an integer or decimal string")
    parsed = int(value)
    if parsed < 0:
        raise ValueError("Meridian monetary/count field cannot be negative")
    return DeclaredLiteral(str(parsed), f"{_XSD}integer")


def _dollars(cents: DeclaredLiteral) -> DeclaredLiteral:
    """The exact fixed-point dollar surface form of an already-validated cents cell.

    NE-MERIDIAN-DOLLARS: the grounding gate's F2 rule makes a currency symbol
    MEANING-BEARING — ``$200.00`` grounds only against a corpus cell that itself
    canonicalizes to ``$200``, and the gate deliberately has NO currency recompute
    (a recompute would let a model relabel any plain grounded number as money).
    So a dollar amount is speakable only if a served cell says it, and this
    projection is where that cell comes from.

    DERIVATION SAFETY: the cents cell stays the system of record, and the input
    here is the cents cell's OWN :class:`DeclaredLiteral` (never the raw fixture
    value a second time), so the dollars cell is a pure, exact, deterministic
    function of the very integer the sibling cents cell serves — ``divmod`` on the
    integer, remainder zero-padded to two digits.  No floats, no rounding, no
    locale, no thousands separators; a disagreement between the two cells is
    unconstructible.  ``_integer`` has already refused negatives and non-integers.
    """
    units, remainder = divmod(int(cents.lexical), 100)
    return DeclaredLiteral(f"${units}.{remainder:02d}")


def _boolean(value: object) -> DeclaredLiteral:
    if not isinstance(value, bool):
        raise ValueError("Meridian boolean field must be a JSON boolean")
    return DeclaredLiteral("true" if value else "false", f"{_XSD}boolean")


def _date(value: object) -> DeclaredLiteral:
    text = str(value)
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text) is None:
        raise ValueError("Meridian date field must use YYYY-MM-DD")
    return DeclaredLiteral(text, f"{_XSD}date")


def _base(subject: str, class_name: str, identifier_name: str, identifier: str) -> list[Triple]:
    return [
        (subject, _RDF_TYPE, f"{_CLASS}{class_name}"),
        (subject, f"{_PRED}{identifier_name}", _plain(identifier)),
        (subject, f"{_PRED}corpus_version", _plain("meridian-v1")),
    ]


def _customer_triples(banking: Mapping[str, Any]) -> list[Triple]:
    triples: list[Triple] = []
    for row in sorted(_rows(banking, "customers"), key=lambda r: str(r.get("customer_id"))):
        customer_id = _identifier("customer", _required(row, "customer_id"))
        address = _required(row, "address")
        if not isinstance(address, dict):
            raise ValueError("Meridian customer address must be an object")
        subject = _iri("customer", customer_id)
        triples.extend(_base(subject, "Customer", "customer_id", customer_id))
        triples.append((subject, f"{_PRED}county", _plain(_required(address, "county"))))
    return triples


def _disaster_triples(banking: Mapping[str, Any]) -> list[Triple]:
    """Project the generator's FEMA declaration table, county list included.

    The declaration carries a FINITE county list, and the pilot's own
    ``scope_description`` says that list is the selected pilot subset rather than the
    exhaustive DR-4830 designated-area set.  Both are projected onto EVERY county row
    so a served row names the declaration, the one county it asserts, and the caveat
    together — a caller can never lift the county set out of a row that has lost the
    sentence limiting it.

    An empty county list FAILS LOUD rather than yielding a declaration whose scope
    silently disappears from an inner join.
    """
    triples: list[Triple] = []
    for row in sorted(
        _rows(banking, "disaster_declarations"), key=lambda r: str(r.get("declaration_id"))
    ):
        declaration_id = _identifier("disaster", _required(row, "declaration_id"))
        subject = _iri("disaster", declaration_id)
        counties = _required(row, "counties")
        if (
            not isinstance(counties, list)
            or not counties
            or not all(isinstance(county, str) and county for county in counties)
        ):
            raise ValueError(
                "Meridian disaster declaration counties must be a non-empty list of strings"
            )
        triples.extend(_base(subject, "DisasterDeclaration", "declaration_id", declaration_id))
        triples.extend(
            [
                (subject, f"{_PRED}incident", _plain(_required(row, "incident"))),
                (
                    subject,
                    f"{_PRED}declaration_date",
                    _date(_required(row, "declaration_date")),
                ),
                (subject, f"{_PRED}state", _plain(_required(row, "state"))),
                (
                    subject,
                    f"{_PRED}scope_description",
                    _plain(_required(row, "scope_description")),
                ),
                (subject, f"{_PRED}source_url", _plain(_required(row, "source_url"))),
            ]
        )
        for county in sorted(counties):
            triples.append((subject, f"{_PRED}county", _plain(county)))
    return triples


def _servicing_triples(banking: Mapping[str, Any]) -> list[Triple]:
    """Project the servicing tables, each loan/payment row carrying the snapshot's
    ``as_of_date``.

    The generator's top-level ``as_of_date`` is the date every servicing fact is
    current as of — it is the ONLY valid anchor for a policy window measured "from
    the as-of date", and it must arrive as served, citable evidence rather than as
    prose nobody published or a clock nobody governs.  It is projected onto every
    AutoLoan and PaymentSchedule row (mirroring the DisputePolicy rows, which have
    carried it since v1) rather than onto a dedicated snapshot subject: the
    grounding gate's row-binding rule (NE-GROUND-ROWID) refuses a cell numeral in
    any sentence quoting an identifier the cell's own row does not carry, so an
    as-of date livable in a claim about ``mer-loan-NNN`` must sit in a row that
    carries that loan id.  The value comes from the manifest-hashed fixture bytes
    alone; a fixture without it fails loud.
    """
    triples: list[Triple] = []
    as_of_date = _date(_required(banking, "as_of_date"))
    for row in sorted(_rows(banking, "auto_loans"), key=lambda r: str(r.get("loan_id"))):
        loan_id = _identifier("loan", _required(row, "loan_id"))
        customer_id = _identifier("customer", _required(row, "customer_id"))
        subject = _iri("loan", loan_id)
        principal_cents = _integer(_required(row, "principal_balance_cents"))
        triples.extend(_base(subject, "AutoLoan", "loan_id", loan_id))
        triples.extend(
            [
                (subject, f"{_PRED}customer_id", _plain(customer_id)),
                (subject, f"{_PRED}status", _plain(_required(row, "status"))),
                (subject, f"{_PRED}principal_balance_cents", principal_cents),
                (subject, f"{_PRED}principal_balance_dollars", _dollars(principal_cents)),
                (subject, f"{_PRED}next_due_date", _date(_required(row, "next_due_date"))),
                (subject, f"{_PRED}as_of_date", as_of_date),
            ]
        )
    for row in sorted(_rows(banking, "payment_schedules"), key=lambda r: str(r.get("payment_id"))):
        payment_id = _identifier("payment", _required(row, "payment_id"))
        loan_id = _identifier("loan", _required(row, "loan_id"))
        subject = _iri("payment", payment_id)
        amount_cents = _integer(_required(row, "amount_cents"))
        triples.extend(_base(subject, "PaymentSchedule", "payment_id", payment_id))
        triples.extend(
            [
                (subject, f"{_PRED}loan_id", _plain(loan_id)),
                (subject, f"{_PRED}due_date", _date(_required(row, "due_date"))),
                (subject, f"{_PRED}amount_cents", amount_cents),
                (subject, f"{_PRED}amount_dollars", _dollars(amount_cents)),
                (subject, f"{_PRED}status", _plain(_required(row, "status"))),
                (subject, f"{_PRED}as_of_date", as_of_date),
            ]
        )
    for row in sorted(_rows(banking, "servicing_notes"), key=lambda r: str(r.get("note_id"))):
        note_id = _identifier("note", _required(row, "note_id"))
        loan_id = _identifier("loan", _required(row, "loan_id"))
        customer_id = _identifier("customer", _required(row, "customer_id"))
        subject = _iri("note", note_id)
        triples.extend(_base(subject, "ServicingNote", "note_id", note_id))
        triples.extend(
            [
                (subject, f"{_PRED}loan_id", _plain(loan_id)),
                (subject, f"{_PRED}customer_id", _plain(customer_id)),
                (subject, f"{_PRED}note_type", _plain(_required(row, "note_type"))),
                (subject, f"{_PRED}created_date", _date(_required(row, "created_date"))),
                (subject, f"{_PRED}text", _plain(_required(row, "text"))),
            ]
        )
    return triples


def _policy_rows(ground_truth: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    b2 = ground_truth.get("b2")
    if not isinstance(b2, dict):
        raise ValueError("Meridian ground truth has no b2 object")
    rows = b2.get("answers_by_version")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Meridian b2 answers_by_version must be a list of objects")
    current = [row for row in rows if row.get("current") is True]
    defaults = [row for row in rows if row.get("threshold_cents") is None]
    if len(current) != 1 or len(defaults) != 1:
        raise ValueError(
            "Meridian ground truth must contain exactly one current and one default dispute SOP"
        )
    return rows


def _business_day_deadline(notice: date, business_days: int) -> date:
    """The date the credit is due: ``business_days`` week-days strictly after notice.

    Mirrors ``ne.api.workflows._business_days_elapsed`` exactly — a span is overdue
    when the as-of date is strictly PAST this date, and a deadline falling ON the
    as-of date is deadline day, not overdue.  Weekend-only calendar (the pilot SOP
    names no holiday schedule; inventing one would be fabricated banking data).
    """
    if business_days < 1:
        raise ValueError("Meridian dispute deadline tier must be a positive business-day count")
    day = notice
    remaining = business_days
    while remaining:
        day += timedelta(days=1)
        if day.weekday() < 5:
            remaining -= 1
    return day


def _dispute_triples(banking: Mapping[str, Any], ground_truth: Mapping[str, Any]) -> list[Triple]:
    triples: list[Triple] = []
    # NE-S9-DEADLINE: the CURRENT SOP's tier facts, resolved before the dispute loop
    # so every dispute row can carry its MATERIALISED provisional-credit deadline.
    # The deadline is a derived fact, computed at load time by trusted code from
    # served facts only (notice_date, amount_cents, the current policy's tiers) and
    # emitted WITH its provenance (the policy artifact id and the tier applied), the
    # way a real servicing system materialises a due date — never re-derived per
    # query, and never served to any agent whose templates do not project it.
    deadline_policy_rows = _policy_rows(ground_truth)
    deadline_default = next(
        row for row in deadline_policy_rows if row.get("threshold_cents") is None
    )
    deadline_standard_days = int(str(_required(deadline_default, "deadline_business_days")))
    deadline_current = next(row for row in deadline_policy_rows if row.get("current") is True)
    deadline_current_artifact = _identifier("policy", _required(deadline_current, "artifact_id"))
    deadline_expedited_days = int(str(_required(deadline_current, "deadline_business_days")))
    deadline_threshold = deadline_current.get("threshold_cents")
    for row in sorted(_rows(banking, "reg_e_disputes"), key=lambda r: str(r.get("dispute_id"))):
        dispute_id = _identifier("dispute", _required(row, "dispute_id"))
        customer_id = _identifier("customer", _required(row, "customer_id"))
        subject = _iri("dispute", dispute_id)
        credit = row.get("provisional_credit_amount_cents")
        amount_cents = _integer(_required(row, "amount_cents"))
        triples.extend(_base(subject, "RegEDispute", "dispute_id", dispute_id))
        triples.extend(
            [
                (subject, f"{_PRED}customer_id", _plain(customer_id)),
                (subject, f"{_PRED}status", _plain(_required(row, "status"))),
                (subject, f"{_PRED}notice_date", _date(_required(row, "notice_date"))),
                (subject, f"{_PRED}amount_cents", amount_cents),
                (subject, f"{_PRED}amount_dollars", _dollars(amount_cents)),
                (subject, f"{_PRED}credit_amount_recorded", _boolean(credit is not None)),
            ]
        )
        if credit is not None:
            credit_cents = _integer(credit)
            triples.append((subject, f"{_PRED}provisional_credit_amount_cents", credit_cents))
            triples.append(
                (subject, f"{_PRED}provisional_credit_amount_dollars", _dollars(credit_cents))
            )
        issue_date = row.get("provisional_credit_issue_date")
        if issue_date is not None:
            triples.append((subject, f"{_PRED}provisional_credit_issue_date", _date(issue_date)))
        # NE-S9-DEADLINE: the materialised deadline under the CURRENT SOP, plus the
        # servicing snapshot anchor it is judged against, on the SAME row.
        amount_value = int(str(amount_cents.lexical))
        tier_days = (
            deadline_expedited_days
            if deadline_threshold is not None and amount_value >= int(str(deadline_threshold))
            else deadline_standard_days
        )
        deadline_day = _business_day_deadline(
            date.fromisoformat(str(_required(row, "notice_date"))), tier_days
        )
        triples.extend(
            [
                (subject, f"{_PRED}as_of_date", _date(_required(banking, "as_of_date"))),
                (
                    subject,
                    f"{_PRED}provisional_credit_deadline_date",
                    _date(deadline_day.isoformat()),
                ),
                (
                    subject,
                    f"{_PRED}provisional_credit_deadline_business_days",
                    _integer(tier_days),
                ),
                (
                    subject,
                    f"{_PRED}provisional_credit_deadline_policy",
                    _plain(deadline_current_artifact),
                ),
            ]
        )

    policy_rows = _policy_rows(ground_truth)
    default = next(row for row in policy_rows if row.get("threshold_cents") is None)
    standard_deadline = _integer(_required(default, "deadline_business_days"))
    artifact_rows = {
        str(row.get("artifact_id")): row
        for row in _rows(banking, "artifacts")
        if row.get("family") == "debit-card-dispute-sop"
    }
    for row in sorted(policy_rows, key=lambda r: str(r.get("artifact_id"))):
        artifact_id = _identifier("policy", _required(row, "artifact_id"))
        artifact = artifact_rows.get(artifact_id)
        if artifact is None or artifact.get("current") is not row.get("current"):
            raise ValueError("Meridian policy truth disagrees with the generated artifact ledger")
        subject = _iri("policy", artifact_id)
        triples.extend(_base(subject, "DisputePolicy", "policy_artifact_id", artifact_id))
        triples.extend(
            [
                (subject, f"{_PRED}policy_version", _plain(_required(row, "version"))),
                (subject, f"{_PRED}current", _boolean(_required(row, "current"))),
                (
                    subject,
                    f"{_PRED}expedited_deadline_business_days",
                    _integer(_required(row, "deadline_business_days")),
                ),
                (subject, f"{_PRED}standard_deadline_business_days", standard_deadline),
                (subject, f"{_PRED}as_of_date", _date(_required(banking, "as_of_date"))),
            ]
        )
        threshold = row.get("threshold_cents")
        if threshold is not None:
            threshold_cents = _integer(threshold)
            triples.append((subject, f"{_PRED}expedited_threshold_cents", threshold_cents))
            triples.append(
                (subject, f"{_PRED}expedited_threshold_dollars", _dollars(threshold_cents))
            )
    return triples


def _sorted_unique(triples: Sequence[Triple]) -> tuple[Triple, ...]:
    unique = set(triples)
    return tuple(
        sorted(
            unique,
            key=lambda triple: (
                triple[0],
                triple[1],
                triple[2].lexical if isinstance(triple[2], DeclaredLiteral) else triple[2],
                triple[2].datatype if isinstance(triple[2], DeclaredLiteral) else "",
            ),
        )
    )


def _acl_graph_assignment_hash() -> str:
    """Hash the label -> named-graph assignment this loader partitions triples by.

    Re-labelling a graph, or moving a class of rows between ACL graphs, changes the
    release descriptor and therefore the SourceContract hash — an ACL change can
    never be silently absorbed into an existing release.
    """
    return release_content_hash(
        "ne.meridian.acl-graph-assignment.v1",
        tuple(
            (label, "assigned-to", graph_iri_for_label(label)) for label in MERIDIAN_GRAPH_LABELS
        ),
    )


def _data_model_hash(graph_triples: Mapping[str, Sequence[Triple]]) -> str:
    """Hash the DECLARED data model: every predicate plus every asserted class.

    This is the model the release's triples actually assert, derived from them rather
    than restated beside them — a new predicate or class cannot enter a release
    without moving the descriptor.
    """
    declared: set[tuple[str, str, str]] = set()
    for triples in graph_triples.values():
        for _subject, predicate, obj in triples:
            declared.add(("predicate", predicate, ""))
            if predicate == _RDF_TYPE and isinstance(obj, str):
                declared.add(("class", obj, ""))
    return release_content_hash("ne.meridian.data-model.v1", tuple(sorted(declared)))


def _contributors(graph_triples: Mapping[str, Sequence[Triple]]) -> tuple[MeridianContributor, ...]:
    """Partition every release triple into its row/entity contributor.

    EVERY triple belongs to exactly one contributor (its subject); there is no
    unattributed remainder, which is what lets a governed read withhold a whole row
    when any one of its contributors fails to resolve current and authorized.
    """
    by_subject: dict[tuple[str, str], list[Triple]] = {}
    for label in sorted(graph_triples):
        for triple in graph_triples[label]:
            by_subject.setdefault((label, triple[0]), []).append(triple)
    return tuple(
        MeridianContributor(
            contributor_key=subject,
            graph_label=label,
            content_hash=release_content_hash("ne.meridian.contributor.v1", triples),
            triple_count=len(set(triples)),
        )
        for (label, subject), triples in sorted(by_subject.items())
    )


def build_meridian_release(fixture_dir: Path) -> MeridianReleaseBuild:
    """Verify the checked-in generator bytes and build ONE complete release.

    ``fixture_dir`` is the directory containing the generator's manifest and JSON
    files.  The path is explicit deployment input; package code never reaches out to
    a repository-relative fixture or network location.  Every hash in the returned
    descriptor is derived from those bytes, the compiled shapes and this loader's own
    fixed versions — nothing is supplied by a caller and nothing is defaulted.
    """
    manifest_raw = (fixture_dir / "manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    if not isinstance(manifest, dict) or manifest.get("corpus_version") != MERIDIAN_CORPUS_VERSION:
        raise ValueError(
            f"Meridian manifest must be for corpus_version={MERIDIAN_CORPUS_VERSION!r}"
        )
    banking, banking_raw = _read_verified_json(fixture_dir, manifest, "banking.json")
    ground_truth, truth_raw = _read_verified_json(fixture_dir, manifest, "ground_truth.json")
    if (
        banking.get("corpus_version") != MERIDIAN_CORPUS_VERSION
        or ground_truth.get("corpus_version") != MERIDIAN_CORPUS_VERSION
    ):
        raise ValueError(f"Meridian source and ground truth must both be {MERIDIAN_CORPUS_VERSION}")
    _validate_data_entitlements(banking)

    graph_triples: dict[str, tuple[Triple, ...]] = {
        _CUSTOMER_LABEL: _sorted_unique(_customer_triples(banking) + _disaster_triples(banking)),
        _SERVICING_LABEL: _sorted_unique(_servicing_triples(banking)),
        _DISPUTE_LABEL: _sorted_unique(_dispute_triples(banking, ground_truth)),
    }
    fixture_hash = hashlib.sha256(manifest_raw + banking_raw + truth_raw).hexdigest()
    shapes = load_compiled_shapes()
    descriptor = MeridianReleaseDescriptor(
        dataset_name=MERIDIAN_CORPUS_VERSION,
        corpus_version=MERIDIAN_CORPUS_VERSION,
        manifest_hash=fixture_hash,
        loader_schema_version=MERIDIAN_LOADER_SCHEMA_VERSION,
        data_model_hash=_data_model_hash(graph_triples),
        ontology_shapes_version=ontology_version_string(
            ontology_version_for(load_manifest(), shapes.shapes_hash)
        ),
        acl_graph_assignment_hash=_acl_graph_assignment_hash(),
    )
    return MeridianReleaseBuild(
        descriptor=descriptor,
        graph_triples=graph_triples,
        contributors=_contributors(graph_triples),
    )


async def load_meridian_verified_fixture(
    store: ValidatingTripleStore, fixture_dir: Path
) -> MeridianVerifiedLoadReport:
    """Verify and atomically materialize BD-1's ``meridian-v1`` generator output.

    This is the ONE production load path: it builds the release through
    :func:`build_meridian_release`, so every load is descriptor-stamped and there is
    no legacy unstamped branch left to fall back to.  It materializes the release
    into ``store`` for the non-source-bearing demo read surface; a SOURCE-BEARING
    governed local T3 head is selected by :func:`publish_meridian_release`, which
    stages into its own distinct Store and CAS-publishes a read-only release.
    """
    build = build_meridian_release(fixture_dir)
    outcomes: dict[str, ReplaceOutcome] = {}
    counts: dict[str, int] = {}
    for label in sorted(build.graph_triples):
        triples = build.graph_triples[label]
        outcomes[label] = await store.replace_acl_graph(
            triples, graph_iri=graph_iri_for_label(label)
        )
        counts[label] = len(triples)
    return MeridianVerifiedLoadReport(
        corpus_version=build.descriptor.corpus_version,
        fixture_hash=build.descriptor.manifest_hash,
        graph_outcomes=outcomes,
        triple_counts=counts,
        release=build.descriptor,
    )


async def publish_meridian_release(
    registry: SelectedReleaseRegistry,
    build: MeridianReleaseBuild,
    *,
    settings: OntologySettings | None = None,
    config_hash: str | None = None,
    claim: ReleaseClaim | None = None,
) -> SelectedOxigraphRelease:
    """Stage ``build`` into a DISTINCT Store and CAS-select it as the release head.

    The claim opens first, so the dataset scope is ``UNKNOWN`` for the whole of the
    staging window and no partial row is ever visible.  Staging validates every
    release graph through the same fail-closed SHACL gateway as any other write; only
    a COMPLETE release is sealed, and only a seal over the head the claim was opened
    against is selected.  A failure at any stage abandons the claim, leaving the scope
    ``UNKNOWN`` and an exact retry free to resume and publish one release.

    Pass ``claim`` to resume an already-open claim (the crash-retry path); omit it to
    open one.
    """
    open_claim = registry.claim() if claim is None else claim
    try:
        gateway, backend = build_release_staging_store(settings, config_hash=config_hash)
        staging = MeridianReleaseStaging(gateway, backend)
        for label in sorted(build.graph_triples):
            await staging.stage_graph(
                label=label,
                graph_iri=graph_iri_for_label(label),
                triples=build.graph_triples[label],
            )
        release = await staging.seal(
            descriptor=build.descriptor, required_labels=MERIDIAN_GRAPH_LABELS
        )
        return registry.publish(open_claim, release)
    except BaseException:
        registry.abandon(open_claim)
        raise
