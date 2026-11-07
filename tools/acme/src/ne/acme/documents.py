# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Acme Corp's 500 documents, ACLs, markdown bodies, and ground-truth facts.

Fixed per-type counts (mapping the SPEC row's informal "5 types" onto the
frozen ne-schema ``DocumentType`` enum): policy=60, report=150, contract=120,
correspondence=120 ("emails"), presentation=50 ("wiki/deck" slot). Each doc
gets exactly one :class:`~ne.acme.models.AclEntry` (visibility weighted
public/department/exec_only/named) and 1-3 :class:`~ne.acme.models.Fact`
records whose ``answer`` is always a literal substring of the generated body —
that is the ground truth NE-075 builds golden Q&A + permission-trap cases from.

ACL semantics (documented again in the README):
  * ``public``     -> allowed_groups=["all-employees"]
  * ``department`` -> allowed_groups=["dept-<owner's dept>", "executive"]
                      (execs see every department doc; only ``named`` docs can
                      exclude them)
  * ``exec_only``  -> allowed_groups=["executive"]
  * ``named``      -> allowed_people = 2-5 person ids including the owner,
                      allowed_groups=[]

Ground-truth coherence: account/order-specific facts appear only on documents
whose ``about`` links that account; every fact->question->answer triple is
coherent and no question has two answers corpus-wide.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from ne.acme.crm import ACME_ACCOUNT_ID, account_rep_map
from ne.acme.models import AclEntry, DocGroundTruth, Fact, GroundTruth, OrgChart
from ne.acme.orgchart import dept_slug
from ne.acme.pools import (
    ACCOUNT_FACT_TEMPLATES,
    BASE_TIME,
    DOC_TITLE_TEMPLATES,
    GENERAL_FACT_TEMPLATES,
    PRODUCTS,
)
from ne.acme.rand import DetRandom
from ne.schema import Account, Document, DocumentType, Person

N_DOCUMENTS = 500

Visibility = Literal["public", "department", "exec_only", "named"]

# Fixed (type, count) order — this IS the generation order; never reorder.
_TYPE_COUNTS: tuple[tuple[DocumentType, int], ...] = (
    (DocumentType.policy, 60),
    (DocumentType.report, 150),
    (DocumentType.contract, 120),
    (DocumentType.correspondence, 120),
    (DocumentType.presentation, 50),
)

_TYPE_KEY = {
    DocumentType.policy: "policy",
    DocumentType.report: "report",
    DocumentType.contract: "contract",
    DocumentType.correspondence: "correspondence",
    DocumentType.presentation: "presentation",
}

_VISIBILITY_WEIGHTS: tuple[tuple[Visibility, int], ...] = (
    ("public", 30),
    ("department", 45),
    ("exec_only", 15),
    ("named", 10),
)

_PII_TARGET_DEPARTMENTS = ("HR", "Finance")
_PII_TARGET_COUNT = 30


def _renewal_year(account_id: str) -> int:
    """Return the renewal year for an account id.

    Deliberately RNG-free (a pure function of the id) so every document about
    the same account states the SAME renewal date — this is what makes the
    renewal question single-answer corpus-wide.
    """
    return 2024 + int(account_id.removeprefix("acct-")) % 3


def _owner_pool(org: OrgChart, doc_type: DocumentType) -> list[str]:
    if doc_type == DocumentType.policy:
        depts = ("HR", "Legal", "Executive")
    elif doc_type == DocumentType.report:
        depts = ("Engineering", "Finance", "Sales")
    elif doc_type == DocumentType.contract:
        depts = ("Legal",)
    elif doc_type == DocumentType.presentation:
        return [m.person_id for m in org.members if m.grade in ("exec", "manager")]
    else:  # correspondence: resolved per-account, not from a fixed pool
        return []
    return [m.person_id for m in org.members if m.department in depts]


def _body_paragraphs(
    doc_type: DocumentType, department: str, product: str, account_name: str | None
) -> list[str]:
    if doc_type == DocumentType.policy:
        return [
            f"This policy governs practices within the {department} department and "
            "applies to all Acme Corporation employees covered by its scope.",
            f"Questions about this policy should be directed to the {department} "
            "department lead. Non-compliance is handled per standard HR procedure.",
        ]
    if doc_type == DocumentType.report:
        base = [
            f"This report summarizes recent {department} department activity, "
            f"including progress against the {product} program.",
        ]
        if account_name:
            base.append(
                f"Of particular note this period is the account status for "
                f"{account_name}, which the {department} team continues to track."
            )
        return base
    if doc_type == DocumentType.contract:
        return [
            f"This agreement is entered into between Acme Corporation and "
            f"{account_name} governing the supply of {product}.",
            "Both parties agree to the terms, pricing, and support commitments "
            "described in the attached schedules.",
        ]
    if doc_type == DocumentType.correspondence:
        return [
            f"Hello — following up regarding {account_name}'s recent {product} "
            "order. Please let us know if you have any questions.",
            f"The {department} team is available to assist with any further "
            "requests from your side.",
        ]
    # presentation
    return [
        f"An overview of {department} department priorities for the period, "
        f"including a look at {product} adoption.",
        f"Slide notes: the {department} team welcomes feedback from all "
        "attendees at the next all-hands.",
    ]


def build_documents(
    rng: DetRandom, people: list[Person], org: OrgChart, accounts: list[Account]
) -> tuple[list[Document], dict[str, str], list[AclEntry], GroundTruth]:
    """Build 500 documents + their ACLs, markdown bodies, and ground truth."""
    del people  # person ids are resolved via org.members / account_rep_map

    pid_to_dept = {m.person_id: m.department for m in org.members}
    customer_accounts = [a for a in accounts if a.id != ACME_ACCOUNT_ID]
    account_rep = account_rep_map(org, accounts)

    documents: list[Document] = []
    content: dict[str, str] = {}
    acls: list[AclEntry] = []
    gt_docs: list[DocGroundTruth] = []

    doc_index = 0
    pii_count = 0

    for doc_type, count in _TYPE_COUNTS:
        type_key = _TYPE_KEY[doc_type]
        templates = DOC_TITLE_TEMPLATES[type_key]
        pool = _owner_pool(org, doc_type)

        for _ in range(count):
            doc_id = f"doc-{doc_index:04d}"
            template = rng.choice(templates)

            account: Account | None = None
            tied_types = (DocumentType.contract, DocumentType.correspondence)
            report_tied = doc_type == DocumentType.report and "{account}" in template
            if doc_type in tied_types or report_tied:
                account = rng.choice(customer_accounts)

            if doc_type == DocumentType.correspondence:
                assert account is not None
                owner = account_rep[account.id]
            else:
                owner = rng.choice(pool)
            department = pid_to_dept[owner]

            year = rng.rand_int(2024, 2026)
            product = rng.choice(PRODUCTS)
            account_name = account.name if account is not None else None

            ctx: dict[str, str | int] = {
                "year": year,
                "department": department,
                "product": product,
            }
            if account is not None:
                ctx["account"] = account.name
                ctx["renewal_year"] = _renewal_year(account.id)
            title = template.format(**ctx)
            about = [account.id] if account is not None else None

            created_days = rng.rand_int(1, 720)
            updated_days = rng.rand_int(0, created_days)
            created_at = BASE_TIME - timedelta(days=created_days)
            updated_at = BASE_TIME - timedelta(days=updated_days)

            documents.append(
                Document(
                    id=doc_id,
                    title=title,
                    doc_type=doc_type,
                    uri=f"acme://corpus/{doc_id}.md",
                    mime_type="text/markdown",
                    author=owner,
                    owned_by=owner,
                    about=about,
                    source_system="acme-dms",
                    source_record_id=f"DMS-{doc_index:04d}",
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )

            # --- content body ---
            body_lines = [f"# {title}", ""]
            body_lines.extend(_body_paragraphs(doc_type, department, product, account_name))
            body_lines.append("")

            # --- facts / ground truth ---
            n_facts = rng.rand_int(1, 3)
            eligible = (
                GENERAL_FACT_TEMPLATES + ACCOUNT_FACT_TEMPLATES
                if account is not None
                else GENERAL_FACT_TEMPLATES
            )
            fact_indices = rng.sample(list(range(len(eligible))), n_facts)
            facts: list[Fact] = []
            for n, fi in enumerate(fact_indices):
                statement_t, question_t, answer_t = eligible[fi]
                statement = statement_t.format(**ctx)
                question = question_t.format(**ctx)
                answer = answer_t.format(**ctx)
                facts.append(
                    Fact(
                        fact_id=f"{doc_id}-f{n}",
                        doc_id=doc_id,
                        statement=statement,
                        question=question,
                        answer=answer,
                    )
                )
                body_lines.append(statement)

            if department in _PII_TARGET_DEPARTMENTS and pii_count < _PII_TARGET_COUNT:
                pii_count += 1
                ssn = f"000-{rng.rand_int(10, 99)}-{rng.rand_int(1000, 9999)}"
                phone = f"+1-555-01{rng.rand_int(0, 99):02d}"
                body_lines.append("")
                body_lines.append(
                    f"Internal reference (synthetic, invalid-by-construction): "
                    f"employee id SSN {ssn}, emergency contact {phone}, "
                    "HR contact hr-records@acme.example."
                )

            content[doc_id] = "\n".join(body_lines) + "\n"

            # --- ACL ---
            visibility = rng.weighted_choice(_VISIBILITY_WEIGHTS)
            if visibility == "public":
                allowed_groups, allowed_people = ["all-employees"], []
            elif visibility == "department":
                allowed_groups = [f"dept-{dept_slug(department)}", "executive"]
                allowed_people = []
            elif visibility == "exec_only":
                allowed_groups, allowed_people = ["executive"], []
            else:  # named
                k = rng.rand_int(2, 5)
                other_ids = [m.person_id for m in org.members if m.person_id != owner]
                sampled = rng.sample(other_ids, k - 1)
                allowed_groups = []
                allowed_people = [owner, *sampled]

            acls.append(
                AclEntry(
                    doc_id=doc_id,
                    visibility=visibility,
                    allowed_groups=allowed_groups,
                    allowed_people=allowed_people,
                )
            )

            # --- ground truth reader expansion ---
            group_members = {g.name: g.member_ids for g in org.groups}
            reader_set: set[str] = set(allowed_people)
            for group_name in allowed_groups:
                reader_set.update(group_members[group_name])
            reader_ids = sorted(reader_set)

            gt_docs.append(
                DocGroundTruth(
                    doc_id=doc_id,
                    visibility=visibility,
                    reader_ids=reader_ids,
                    facts=facts,
                )
            )

            doc_index += 1

    return documents, content, acls, GroundTruth(docs=gt_docs)
