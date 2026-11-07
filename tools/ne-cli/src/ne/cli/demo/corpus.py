# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO corpus adapter: acme fixtures → a governed ``fixture`` connector JSON.

This is the ONLY new mapping code the demo adds. It reads the committed synthetic
Acme corpus (``tools/acme/fixtures/acme-v1`` — records/content/acl/org/ground_truth
JSON) and emits the exact :class:`~ne.connect.testing.fixture_connector.FixtureConnector`
JSON shape so ``ne connect sync fixture`` (the EXISTING governed C7 KnowledgeWriter
path) can seed the demo stack — the demo NEVER writes chunks itself.

ONE object/record/acl entry per DOCUMENT (``AclEntry`` is per-resource, so per-doc
ACLs require per-doc resources). ``item_id``/``chunk_id``/``document_id`` a governed
write assigns are the deterministic SHA-256 the connector bridge's
``default_record_mapper`` computes over ``source \n source_id \n record_id`` — so
:func:`demo_document_id` lets the arc address a specific document over pure HTTP
(the permission-trap retrieve-by-id leg) without importing any writer internals.

All ne.acme heavy imports stay LAZY (inside functions) so ``ne --help`` stays fast.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

#: The connector ``source`` label the demo fixture advertises. Threaded into every
#: object/record and into :func:`demo_document_id` (part of the item-id preimage).
SOURCE = "acme-fixture"

#: Fixed record cursor (the fixture replay is a static recorded corpus; a re-run is
#: dedup-ledger idempotent). Kept constant so two exports are byte-identical.
BASE_TIME = "2026-01-01T00:00:00Z"

#: Fixed multi-hop narration for the T2 graph step (the question the graph tier
#: answers once NE-BRAIN-T2-WIRING is live; degrade-labeled until then).
T2_QUESTION = (
    "Which accounts does the owner of the engineering runbook also manage, "
    "and what orders connect them? (knowledge-graph multi-hop)"
)

#: Fixed T3 verified-answers ask (the accounts-directory verified template).
T3_QUESTION = "List every account in the directory that I am cleared to see."

#: The grounded-analyst's 2-fetch JOIN question (NE-GA-G6): forces the model to
#: reason, fetch accounts-directory AND orders-directory, and join them into a
#: per-account open-order count — every reported number structurally verified.
#: LOAD-BEARING WORDING: do not reword — the phrasing is what lets
#: :func:`~ne.cli.demo.arc._grounded_draft`'s answer pass the REAL G1 grounding
#: gate (the word "open" survives the check ONLY because it is a prompt token;
#: see the gate-drift tripwire test).
G_QUESTION = "Which accounts am I cleared to see, and how many open orders does each have?"

#: The SAME question, marked so the fixture-mode gateway scripts a FABRICATED
#: draft (an invented account + count) instead of a genuinely grounded one — the
#: showpiece refuse-to-hallucinate leg. The fabricated account name is
#: DELIBERATELY kept OUT of this prompt (unlike the sentinel bracket) so the
#: vocab-ungrounded finding fires too, not just numeral-ungrounded.
G_REFUSE_PROMPT = f"[grounded-hallucination] {G_QUESTION}"

#: The template's ``description`` — must byte-match ``ACCOUNTS_DIRECTORY_TEMPLATE``'s
#: ``[description]`` value (pinned by ``test_routing_constants_match_template_bytes``).
#: This is the menu line's leading text the router renders via ``_menu_lines``.
T3_TEMPLATE_DESCRIPTION = "Every account visible to the caller, with its name."

#: The template's canonical (first) ``[routing].triggers`` entry — must byte-match
#: ``triggers[0]`` below. This is the "e.g." example the router's deterministic menu
#: renders for this template.
T3_CANONICAL_TRIGGER = "list every account in the directory that i am cleared to see"

#: An in-domain but AMBIGUOUS prompt: it hits ``guard_terms`` ("accounts directory")
#: but no trigger — the deterministic matcher CLARIFYs rather than guesses. Verified
#: at plan time against ``routing.py`` semantics (no negation token, no compound
#: " and <verb>"/"then").
T3_CLARIFY_PROMPT = "tell me about the accounts directory"

#: Three distinct guard-hit, no-trigger prompts used to exhaust the clarify bound
#: (``clarify_max = 2``) and demonstrate the fail-closed refusal.
T3_AMBIGUOUS_PROMPTS = (
    T3_CLARIFY_PROMPT,
    "what is in the accounts directory",
    "explain the accounts directory please",
)

#: The per-customer verified-answer template the demo ships (SINGLE source — the
#: fixture harness imports :func:`write_verified_templates` rather than re-declaring
#: it). ``accounts-directory`` enumerates every Account the caller may see with its
#: name; scope ``all-employees`` matches the ACL the demo graph seeds accounts with.
#: ``[routing]`` (NE-VAF-U5) ACTIVATES deterministic routing for this template: the
#: T3 chat leg is no longer best-effort model tool-calling — an in-domain question
#: routes, clarifies, or is honestly refused, never guessed (zero model tokens on any
#: of those three outcomes). Zero-arg gate passes (no ``[args.*]`` table); every
#: guard_term is multi-word per the U1 load gate.
#:
#: LEGIBILITY (NE-LIBRARY-LEGIBILITY): ``columns`` leads with ``name`` — a human
#: reads the account's name, not its key — and ``label_var``/``citation_var`` name
#: the two roles explicitly. ``citation_var`` stays ``account``: the identifier is
#: the citation anchor the audit chain records, demoted in display, never removed.
ACCOUNTS_DIRECTORY_TEMPLATE = """
template_id = "accounts-directory"
version = 1
description = "Every account visible to the caller, with its name."
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  ?account a <https://neutron.enterprise/schema/operational/Account> .
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
}
\"\"\"

[scope]
graph_classes = ["all-employees"]
result_shape = "enumerable"

[answer]
columns = ["name", "account"]
citation_var = "account"
label_var = "name"
count_total_rows = true

[routing]
domain = "accounts-directory"
triggers = [
  "list every account in the directory that i am cleared to see",
  "list the accounts i am cleared to see",
  "list every account i can see",
]
guard_terms = ["accounts directory", "every account", "cleared to see"]
clarify_max = 2
"""

#: The orders verified-answer template the grounded-agent demo (NE-GA-G6) adds
#: alongside ``accounts-directory``: every order visible to the caller, with its
#: account and status. ZERO ``[args.*]`` (the U1 zero-arg gate) and NO
#: ``[routing]`` block (the routing table pinned at ``len == 1`` by
#: ``test_routing_block_loads_and_routes_t3_question`` must stay undisturbed —
#: this template is reached by the grounded agent's own tool-calling, never by
#: deterministic T3 routing). Every projected column (``?order``/``?account``/
#: ``?status``) is bound by a REAL triple pattern — the Order node's own
#: ``rdf:type``, its ``ACCOUNT`` edge, and its ``status`` slot (predicates
#: verified against ``lift/graph_to_rdf.py``'s ``_node_triples``/edge-triple
#: construction: ``rdf:type`` object is ``NEOP + entity_class`` i.e.
#: ``.../operational/Order``; a slot predicate is ``NEOP + field_name`` i.e.
#: ``.../operational/status``; an edge predicate is ``NEOP + edge_def.slot_name``
#: i.e. ``.../operational/account`` for the ``ACCOUNT`` edge kind) — so it passes
#: the NE-GA-TG projection-hygiene guard (GUARD 2) at load with no arg-as-column
#: exemption needed at all.
#:
#: NO ``label_var``, deliberately: an Order projects no human-name column here
#: (``status`` is a state, not a label), so this template is the shipped exercise
#: of the renderer's degrade path — identifier-led, nothing fabricated. Projecting
#: the account's ``name`` alongside would be the upgrade, but it changes this
#: template's columns, which the grounded-agent join beat consumes — tracked, not
#: smuggled into a display unit.
ORDERS_DIRECTORY_TEMPLATE = """
template_id = "orders-directory"
version = 1
description = "Every order visible to the caller, with its account and status."
sparql = \"\"\"
SELECT ?order ?account ?status WHERE {
  #BINDINGS#
  ?order a <https://neutron.enterprise/schema/operational/Order> .
  ?order <https://neutron.enterprise/schema/operational/account> ?account .
  ?order <https://neutron.enterprise/schema/operational/status> ?status .
}
\"\"\"

[scope]
graph_classes = ["all-employees"]
result_shape = "enumerable"

[answer]
columns = ["order", "account", "status"]
citation_var = "order"
count_total_rows = true
"""

#: NE-CTXB-8 (design §5.4, §8): the SERVER-SIDE verified COUNT aggregate the
#: grounded beat now targets. ``kind='count'`` (NE-CTXB-7, Layer 3): the store
#: rewrites the body into ``SELECT ?account ?name (COUNT(DISTINCT ?order) AS
#: ?n_orders) … GROUP BY ?account ?name``, so the model reasons over ~dozens of
#: per-account group rows (truncated=false at demo scale) instead of two 500-row
#: directory tables — THIS is the layer that actually scales the grounded beat.
#: The ``"placed"`` literal matches the lift's ``xsd:string`` status slot by RDF
#: 1.1 simple-literal identity (an untyped quoted literal IS ``xsd:string``), so
#: the body counts only OPEN orders. NO ``[routing]`` block (the routing table
#: MUST stay ``len == 1`` — this template is reached only by the grounded agent's
#: own tool-calling, never deterministic T3 routing). ``count_total_rows = true``
#: opts into the trusted ``COUNT(*)`` → ``total_rows`` rewrite (NE-CTXB-7). The
#: ``tool_name`` derives to ``read.kg.orders_per_account_count`` (auto-granted to
#: the grounded agent via platform.py:1035 — every registry template is granted).
ORDERS_PER_ACCOUNT_COUNT_TEMPLATE = """
template_id = "orders-per-account-count"
version = 1
description = "How many open (placed) orders each visible account has — a verified count."
sparql = \"\"\"
SELECT ?account ?name ?order WHERE {
  #BINDINGS#
  ?account a <https://neutron.enterprise/schema/operational/Account> .
  ?account <https://neutron.enterprise/schema/operational/name> ?name .
  ?order a <https://neutron.enterprise/schema/operational/Order> .
  ?order <https://neutron.enterprise/schema/operational/account> ?account .
  ?order <https://neutron.enterprise/schema/operational/status> "placed" .
}
\"\"\"

[scope]
graph_classes = ["all-employees"]
result_shape = "enumerable"

[answer]
kind = "count"
group_by = ["name", "account"]
count_over = "order"
as = "n_orders"
citation_var = "account"
label_var = "name"
count_total_rows = true
"""

#: The ticket verified-answer template the governed workflows read from
#: (NE-WORKFLOWS). It is the deterministic entry point to the ticket layer: every
#: ticket in ONE workflow status, with the subject/priority it was filed under and
#: the account it belongs to. ``status`` is the single ``[args]`` entry, bound as an
#: RDF term into the ``VALUES`` block by trusted code — never string-interpolated —
#: so a caller may ask for ``"open"`` or ``"pending"`` without a second template.
#:
#: ``enum`` (NE-TEMPLATE-ENUM, SPEC §12 item 3) declares the CLOSED ``TicketStatus``
#: vocabulary (``ne.schema.TicketStatus`` — the same four values ``tools/acme``
#: generates and ``ne.api.workflows._TICKET_STATUSES`` reads). The generated tool
#: schema now advertises the permitted values to the agent, and an out-of-vocabulary
#: guess (the live-walkthrough ``"unresolved"``) is REFUSED loud before any query
#: runs — never executed into a silent zero-row result.
#:
#: Every projected column is bound by a REAL triple pattern (the NE-GA-TG
#: projection-hygiene guard): the Ticket node's ``rdf:type``, its ``status`` /
#: ``subject`` / ``priority`` slots, its ``BELONGS_TO`` edge (slot name
#: ``belongs_to``), and the Account's own ``name`` slot. NO ``[routing]`` block —
#: the routing table is pinned at ``len == 1`` and this template is reached by a
#: workflow's (or the grounded agent's) tool call, never by prose routing.
#:
#: COLUMN ORDER IS LOAD-BEARING. The governed tool renders its answer as a
#: ``" | "``-joined text table, so a value CONTAINING that separator would shift every
#: column after it. ``subject`` is the one free-text column here and it is projected
#: LAST, which lets a reader split on a fixed number of separators and keep whatever
#: remains as the subject — a separator inside a ticket subject then changes nothing.
#: That is exactly why the human label here rides ``label_var`` instead of a column
#: reorder: the console leads its DISPLAY with ``subject`` from the structured cells,
#: while the machine text rendering keeps it last for the separator invariant.
TICKETS_BY_STATUS_TEMPLATE = """
template_id = "tickets-by-status"
version = 1
description = "Every ticket in one workflow status, with its account, priority and subject."
sparql = \"\"\"
SELECT ?ticket ?priority ?account ?account_name ?subject WHERE {
  #BINDINGS#
  ?ticket a <https://neutron.enterprise/schema/operational/Ticket> .
  ?ticket <https://neutron.enterprise/schema/operational/status> ?status .
  ?ticket <https://neutron.enterprise/schema/operational/subject> ?subject .
  ?ticket <https://neutron.enterprise/schema/operational/priority> ?priority .
  ?ticket <https://neutron.enterprise/schema/operational/belongs_to> ?account .
  ?account <https://neutron.enterprise/schema/operational/name> ?account_name .
}
\"\"\"

[args]
status = { type = "str", max_length = 32, enum = ["open", "pending", "resolved", "closed"] }

[scope]
graph_classes = ["all-employees"]
result_shape = "enumerable"

[answer]
columns = ["ticket", "priority", "account", "account_name", "subject"]
citation_var = "ticket"
label_var = "subject"
count_total_rows = true
"""

#: The graph connector-record source_id prefix per entity class. CRITICAL: the graph
#: record source_ids use a COLON (``acme/accounts:<id>``) — distinct from the chunk
#: fixture's SLASH (``acme/docs/<id>``, :func:`_source_id`), because
#: ``GraphEntityMapping.matches`` admits ``prefix`` or ``prefix + ':'`` only.
_GRAPH_PREFIX = {
    "accounts": "acme/accounts",
    "people": "acme/people",
    "orders": "acme/orders",
    "tickets": "acme/tickets",
    "documents": "acme/docs",
}


def _graph_source_id(prefix: str, record_id: str) -> str:
    """The COLON-delimited graph connector resource id (``acme/accounts:acct-0000``)."""
    return f"{prefix}:{record_id}"


def graph_document_key(doc_id: str) -> str:
    """The deterministic T2 node ``key`` the demo graph assigns document ``doc_id``.

    Mirrors ``ne.brain_graph.model.entity_key_for`` over the graph connector's COLON
    source_id so the arc can address a document's graph node over pure HTTP without
    importing writer internals. Distinct from :func:`demo_document_id` (the chunk id).
    """
    from ne.brain_graph.model import entity_key_for

    return entity_key_for(
        SOURCE, "Document", _graph_source_id(_GRAPH_PREFIX["documents"], doc_id), doc_id
    )


def _fixtures_dir(fixtures_dir: Path | None) -> Path:
    """Resolve the acme fixtures dir (default = the ne-acme checkout/install dir)."""
    if fixtures_dir is not None:
        return fixtures_dir
    from ne.acme.cli import default_fixtures_dir

    return default_fixtures_dir()


def _load(fixtures_dir: Path, name: str) -> object:
    return json.loads((fixtures_dir / name).read_text(encoding="utf-8"))


def _source_id(doc_id: str) -> str:
    """The per-document connector resource id (``acme/docs/<doc_id>``)."""
    return f"acme/docs/{doc_id}"


def demo_document_id(doc_id: str) -> str:
    """The deterministic ``item_id``/``chunk_id``/``document_id`` a governed write
    assigns this document — mirrors ``ne.connect.bridge.default_record_mapper``'s
    ``sha256(source \\n source_id \\n record_id)`` so the arc can address a document
    by id over HTTP with zero coupling to writer internals."""
    preimage = f"{SOURCE}\n{_source_id(doc_id)}\n{doc_id}"
    return hashlib.sha256(preimage.encode()).hexdigest()


def export_connector_fixture(
    fixtures_dir: Path,
    out_path: Path,
    *,
    doc_ids: Sequence[str] | None = None,
) -> int:
    """Emit the FixtureConnector JSON for the acme corpus; return the record count.

    ONE object + record + acl entry per document. The record payload carries the
    document body as ``content`` (the CONNECTOR_RECORD_SHAPE required field) plus
    innocuous doc metadata; ACL principals are the acl.json ``allowed_groups``
    labels as-is (``all-employees``/``dept-<slug>``/``executive``) plus the
    ``allowed_people`` person-ids. Output is byte-deterministic (dump_fixture sorts
    keys); passing the same ``doc_ids`` twice yields identical bytes.
    """
    from ne.connect.testing.fixture_connector import dump_fixture

    records_doc = _load(fixtures_dir, "records.json")
    assert isinstance(records_doc, Mapping)
    content = _load(fixtures_dir, "content.json")
    assert isinstance(content, Mapping)
    acl_rows = _load(fixtures_dir, "acl.json")
    assert isinstance(acl_rows, list)

    docs_by_id = {d["id"]: d for d in records_doc["documents"]}
    acl_by_id = {row["doc_id"]: row for row in acl_rows}

    selected = list(doc_ids) if doc_ids is not None else [d["id"] for d in records_doc["documents"]]

    objects: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    acl: list[dict[str, object]] = []
    for doc_id in selected:
        doc = docs_by_id[doc_id]
        body = content[doc_id]
        source_id = _source_id(doc_id)
        objects.append({"source_id": source_id, "kind": "resource", "name": doc["title"]})
        records.append(
            {
                "record_id": doc_id,
                "source_id": source_id,
                "cursor": BASE_TIME,
                "payload": {
                    "content": body,
                    "title": doc["title"],
                    "doc_id": doc_id,
                    "doc_type": doc["doc_type"],
                    "uri": doc["uri"],
                    "owned_by": doc["owned_by"],
                    "source_system": doc["source_system"],
                },
            }
        )
        row = acl_by_id[doc_id]
        principals = [*row["allowed_groups"], *row["allowed_people"]]
        acl.append({"resource_id": source_id, "principals": principals})

    dump_fixture(out_path, source=SOURCE, objects=objects, records=records, acl=acl)
    return len(records)


# ---------------------------------------------------------------------------
# T2 graph test snapshot (governed GraphSyncRunner construction, ACL-faithful)
# ---------------------------------------------------------------------------
def acme_graph_mapping():  # type: ignore[no-untyped-def]
    """The declarative T2 ``GraphSourceMapping`` for the Acme corpus.

    Every ``fields`` projection is the identity map over the entity contract's fields
    (validated fail-closed by ``map_record`` against ``ENTITY_CONTRACTS``); FK payload
    keys (``works_at``/``owned_by``/…) are NOT contract fields, so they project only as
    typed edges. Edge target templates use the COLON graph prefixes (:data:`_GRAPH_PREFIX`).
    """
    from ne.brain_graph.config import GraphEdgeMapping, GraphEntityMapping, GraphSourceMapping
    from ne.schema import ENTITY_CONTRACTS

    def _fields(entity_class: str) -> dict[str, str]:
        return {name: name for name in ENTITY_CONTRACTS[entity_class].model_fields}

    return GraphSourceMapping(
        source_system=SOURCE,
        entities=(
            GraphEntityMapping(
                source_id_prefix=_GRAPH_PREFIX["accounts"],
                entity_class="Account",
                fields=_fields("Account"),
            ),
            GraphEntityMapping(
                source_id_prefix=_GRAPH_PREFIX["people"],
                entity_class="Person",
                fields=_fields("Person"),
                edges=(
                    GraphEdgeMapping(
                        payload_key="works_at",
                        edge_kind="WORKS_AT",
                        target_class="Account",
                        target_source_id_template=f"{_GRAPH_PREFIX['accounts']}:{{value}}",
                    ),
                ),
            ),
            GraphEntityMapping(
                source_id_prefix=_GRAPH_PREFIX["documents"],
                entity_class="Document",
                fields=_fields("Document"),
                edges=(
                    GraphEdgeMapping(
                        payload_key="owned_by",
                        edge_kind="OWNED_BY",
                        target_class="Person",
                        target_source_id_template=f"{_GRAPH_PREFIX['people']}:{{value}}",
                    ),
                    GraphEdgeMapping(
                        payload_key="author",
                        edge_kind="AUTHOR",
                        target_class="Person",
                        target_source_id_template=f"{_GRAPH_PREFIX['people']}:{{value}}",
                    ),
                ),
            ),
            GraphEntityMapping(
                source_id_prefix=_GRAPH_PREFIX["orders"],
                entity_class="Order",
                fields=_fields("Order"),
                edges=(
                    GraphEdgeMapping(
                        payload_key="account",
                        edge_kind="ACCOUNT",
                        target_class="Account",
                        target_source_id_template=f"{_GRAPH_PREFIX['accounts']}:{{value}}",
                    ),
                    GraphEdgeMapping(
                        payload_key="placed_by",
                        edge_kind="PLACED_BY",
                        target_class="Person",
                        target_source_id_template=f"{_GRAPH_PREFIX['people']}:{{value}}",
                    ),
                ),
            ),
            GraphEntityMapping(
                source_id_prefix=_GRAPH_PREFIX["tickets"],
                entity_class="Ticket",
                fields=_fields("Ticket"),
                edges=(
                    GraphEdgeMapping(
                        payload_key="belongs_to",
                        edge_kind="BELONGS_TO",
                        target_class="Account",
                        target_source_id_template=f"{_GRAPH_PREFIX['accounts']}:{{value}}",
                    ),
                    GraphEdgeMapping(
                        payload_key="requester",
                        edge_kind="REQUESTER",
                        target_class="Person",
                        target_source_id_template=f"{_GRAPH_PREFIX['people']}:{{value}}",
                    ),
                    GraphEdgeMapping(
                        payload_key="assignee",
                        edge_kind="ASSIGNEE",
                        target_class="Person",
                        target_source_id_template=f"{_GRAPH_PREFIX['people']}:{{value}}",
                    ),
                ),
            ),
        ),
    )


def build_graph_snapshot(
    fixtures_dir: Path | None = None, *, doc_ids: Sequence[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Construct an ACL-labeled T2 graph snapshot for tests and offline comparisons.

    Drives the GOVERNED :class:`~ne.brain_graph.construct.runner.GraphSyncRunner` over a
    recorded :class:`FixtureConnector` (source-acl mode) into the reference
    ``InMemoryGraphStore`` — the SAME path production uses, so trimming/dual-key is honest
    (C7), never fabricated. Documents carry their ``acl.json`` principals verbatim
    (``allowed_groups`` + ``allowed_people``); Account/Person/Order/Ticket nodes carry
    ``['all-employees']`` (the harness precedent). The snapshot is byte-deterministic:
    ``synced_at`` (the only clock-read) is normalized to :data:`BASE_TIME`, and
    ``scan_nodes``/``scan_edges`` return key-sorted.
    """
    import asyncio

    from ne.brain_graph.construct.ledger import GraphSyncLedger
    from ne.brain_graph.construct.runner import GraphSyncRunner
    from ne.brain_graph.testing.fixtures import FixtureConnector, make_ledger_engine
    from ne.brain_graph.testing.memory import InMemoryGraphStore
    from ne.connect.contracts import (
        OdcsDataContract,
        PermissionMode,
        PermissionModelSpec,
        PiiClass,
    )
    from ne.connect.state import ConnectorStateStore
    from ne.contracts.integration import AclEntry, Record

    resolved = _fixtures_dir(fixtures_dir)
    records_doc = _load(resolved, "records.json")
    assert isinstance(records_doc, Mapping)
    acl_rows = _load(resolved, "acl.json")
    assert isinstance(acl_rows, list)
    acl_by_id = {row["doc_id"]: row for row in acl_rows}

    records: list[Record] = []
    acl_entries: list[AclEntry] = []

    def _add(
        prefix: str, record_id: str, payload: Mapping[str, object], principals: list[str]
    ) -> None:
        source_id = _graph_source_id(prefix, record_id)
        records.append(Record(record_id=record_id, source_id=source_id, payload=dict(payload)))
        acl_entries.append(AclEntry(resource_id=source_id, principals=tuple(principals)))

    for acct in records_doc["accounts"]:
        _add(_GRAPH_PREFIX["accounts"], acct["id"], acct, ["all-employees"])
    for person in records_doc["people"]:
        _add(_GRAPH_PREFIX["people"], person["id"], person, ["all-employees"])
    for order in records_doc["orders"]:
        _add(_GRAPH_PREFIX["orders"], order["id"], order, ["all-employees"])
    for ticket in records_doc["tickets"]:
        _add(_GRAPH_PREFIX["tickets"], ticket["id"], ticket, ["all-employees"])

    docs_by_id = {d["id"]: d for d in records_doc["documents"]}
    selected = list(doc_ids) if doc_ids is not None else [d["id"] for d in records_doc["documents"]]
    for doc_id in selected:
        doc = docs_by_id[doc_id]
        row = acl_by_id[doc_id]
        principals = [*row["allowed_groups"], *row["allowed_people"]]
        _add(_GRAPH_PREFIX["documents"], doc_id, doc, principals)

    contract = OdcsDataContract(
        source=SOURCE,
        owner="ops@acme.test",
        schema_def={"type": "object"},
        freshness_sla="PT1H",
        pii=PiiClass.PERSONAL,
        permissions=PermissionModelSpec(
            mode=PermissionMode.SOURCE_ACL, default_acl=("all-employees",)
        ),
    )
    store = InMemoryGraphStore()
    runner = GraphSyncRunner(
        FixtureConnector(records, acl_entries),
        contract,
        store,
        GraphSyncLedger(make_ledger_engine()),
        ConnectorStateStore(":memory:"),
        acme_graph_mapping(),
        actor="ne-demo:graph-seed",
        config_hash="",
        run_id="ne-demo-graph",
    )

    async def _run() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        await runner.run(mode="backfill")
        nodes = [dict(n) for n in await store.scan_nodes()]
        edges = [dict(e) for e in await store.scan_edges()]
        return nodes, edges

    nodes, edges = asyncio.run(_run())
    # Normalize the ONE clock-read (``synced_at``) so two builds are byte-identical.
    for element in (*nodes, *edges):
        element["synced_at"] = BASE_TIME
    return {"nodes": nodes, "edges": edges}


def write_verified_templates(dest_dir: Path) -> Path:
    """Write the per-customer verified-answer template bundle; return the dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "accounts_directory.toml").write_text(ACCOUNTS_DIRECTORY_TEMPLATE, encoding="utf-8")
    (dest_dir / "orders_directory.toml").write_text(ORDERS_DIRECTORY_TEMPLATE, encoding="utf-8")
    (dest_dir / "orders_per_account_count.toml").write_text(
        ORDERS_PER_ACCOUNT_COUNT_TEMPLATE, encoding="utf-8"
    )
    (dest_dir / "tickets_by_status.toml").write_text(TICKETS_BY_STATUS_TEMPLATE, encoding="utf-8")
    return dest_dir


class DemoScenario(BaseModel):
    """The deterministically-selected demo cast + golden questions (frozen).

    Chosen from org.json / acl.json / ground_truth.json: a reader who can see the
    T1 ground-truth (public) doc, an outsider denied the exec-only trap doc, an
    approver persona, and the fixed T2/T3 narration. ``trap_document_id`` is the
    computed governed id so the arc's permission-trap retrieve-by-id leg is pure HTTP.
    """

    model_config = ConfigDict(frozen=True)

    source: str = SOURCE
    reader_id: str
    reader_groups: tuple[str, ...]
    reader_department: str
    outsider_id: str
    outsider_groups: tuple[str, ...]
    outsider_department: str
    approver_id: str
    t1_doc_id: str
    t1_question: str
    t1_answer: str
    trap_doc_id: str
    trap_document_id: str
    #: The T2 graph node ``key`` for the T1 (reader-visible) document — the reader's
    #: /v1/graph/neighborhood anchor (non-empty, C7-visible).
    t2_anchor_key: str = ""
    #: The T2 graph node ``key`` for the exec-only trap document — the outsider's
    #: anchor (trimmed to empty by C7, proving dual-key honesty).
    t2_trap_key: str = ""
    t2_question: str = T2_QUESTION
    t3_question: str = T3_QUESTION
    doc_ids: tuple[str, ...] = ()
    #: The grounded-analyst's 2-fetch JOIN question / forced-hallucination prompt
    #: (NE-GA-G6). Trailing defaulted fields — ``build_scenario`` needs no change.
    g_question: str = G_QUESTION
    g_refuse_prompt: str = G_REFUSE_PROMPT


def _dept_groups(department: str) -> tuple[str, ...]:
    """A person's C7 group labels (all-employees + their dept), matching acl.json."""
    return ("all-employees", f"dept-{department.lower()}")


def build_scenario(
    fixtures_dir: Path | None = None, *, approver_id: str = "approver-demo"
) -> DemoScenario:
    """Deterministically pick the demo cast + golden Q&A from the acme fixtures."""
    resolved = _fixtures_dir(fixtures_dir)
    org = _load(resolved, "org.json")
    assert isinstance(org, Mapping)
    acl_rows = _load(resolved, "acl.json")
    assert isinstance(acl_rows, list)
    ground_truth = _load(resolved, "ground_truth.json")
    assert isinstance(ground_truth, Mapping)

    members = list(org["members"])
    acl_by_id = {row["doc_id"]: row for row in acl_rows}
    gt_by_id = {d["doc_id"]: d for d in ground_truth["docs"]}

    # Reader = first Engineering member.
    reader = next(m for m in members if m["department"] == "Engineering")
    reader_id = reader["person_id"]

    # T1 doc = first public doc that carries ground-truth facts + is reader-visible.
    t1_doc_id = next(
        d["doc_id"]
        for d in ground_truth["docs"]
        if d["facts"] and acl_by_id.get(d["doc_id"], {}).get("visibility") == "public"
    )
    fact = gt_by_id[t1_doc_id]["facts"][0]

    # Trap = first exec-only doc; outsider = first non-exec != reader NOT in its readers.
    trap_doc_id = next(row["doc_id"] for row in acl_rows if row["visibility"] == "exec_only")
    trap_readers = set(gt_by_id[trap_doc_id]["reader_ids"])
    outsider = next(
        m
        for m in members
        if m["grade"] != "exec"
        and m["person_id"] != reader_id
        and m["person_id"] not in trap_readers
    )

    return DemoScenario(
        reader_id=reader_id,
        reader_groups=_dept_groups(reader["department"]),
        reader_department=reader["department"],
        outsider_id=outsider["person_id"],
        outsider_groups=_dept_groups(outsider["department"]),
        outsider_department=outsider["department"],
        approver_id=approver_id,
        t1_doc_id=t1_doc_id,
        t1_question=fact["question"],
        t1_answer=fact["answer"],
        trap_doc_id=trap_doc_id,
        trap_document_id=demo_document_id(trap_doc_id),
        t2_anchor_key=graph_document_key(t1_doc_id),
        t2_trap_key=graph_document_key(trap_doc_id),
    )


def demo_doc_subset(fixtures_dir: Path | None = None, *, per_class: int = 3) -> tuple[str, ...]:
    """A small representative doc subset (each visibility class + the T1/trap docs).

    Used by the CI arc so the fixture-mode ne-api seeds a ~12-doc corpus rather than
    all 500. Deterministic: docs are taken in fixture order, ``per_class`` per
    visibility class, always including the scenario's T1 and trap docs.
    """
    resolved = _fixtures_dir(fixtures_dir)
    acl_rows = _load(resolved, "acl.json")
    assert isinstance(acl_rows, list)
    scenario = build_scenario(resolved)

    by_class: dict[str, list[str]] = {}
    for row in acl_rows:
        by_class.setdefault(row["visibility"], []).append(row["doc_id"])

    chosen: list[str] = []
    for _visibility, ids in sorted(by_class.items()):
        chosen.extend(ids[:per_class])
    for required in (scenario.t1_doc_id, scenario.trap_doc_id):
        if required not in chosen:
            chosen.append(required)
    # De-dup preserving order.
    seen: set[str] = set()
    ordered = [d for d in chosen if not (d in seen or seen.add(d))]
    return tuple(ordered)
