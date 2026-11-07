# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SHACL fail-closed gateway invariants (brain-tier3-spec §12.1)."""

from __future__ import annotations

import asyncio

import pytest

from ne.brain_ontology.config import OntologySettings
from ne.brain_ontology.gateway import (
    ShapeValidationError,
    TripleQuarantinedError,
    ValidatingTripleStore,
)
from ne.brain_ontology.quarantine import SqliteTripleQuarantine
from ne.brain_ontology.shapes import PyShaclValidator, build_validator, load_compiled_shapes
from ne.brain_ontology.store.oxigraph import OxigraphBackend
from ne.brain_ontology.store.protocols import DatasetSpec
from ne.schema import ShapeViolation

_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
_NEOP = "https://neutron.enterprise/schema/operational/"
_PERSON = _NEOP + "Person"
_CONFIG_HASH = "a" * 64


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[ShapeViolation] = []

    def emit(self, event: ShapeViolation) -> None:
        self.events.append(event)


def _conforming_person(subject: str) -> list[tuple[str, str, str]]:
    # id/email/etc. are xsd:string slots — the values must be plain literals (not
    # urn:/http: values, which the IRI-vs-literal heuristic would type as IRIs).
    local = subject.rsplit("/", 1)[-1]
    return [
        (subject, _RDF, _PERSON),
        (subject, _NEOP + "id", local),
        (subject, _NEOP + "email", "a@example.org"),
        (subject, _NEOP + "family_name", "Ada"),
        (subject, _NEOP + "given_name", "Lovelace"),
    ]


def _conforming_account(subject: str) -> list[tuple[str, str, str]]:
    # Account requires `name`; employee_count is an xsd:integer slot and created_at
    # an xsd:dateTime slot — BOTH would be quarantined by the pre-fix gateway that
    # emitted every non-IRI object as an UNTYPED literal (fails sh:datatype).
    local = subject.rsplit("/", 1)[-1]
    return [
        (subject, _RDF, _NEOP + "Account"),
        (subject, _NEOP + "id", local),
        (subject, _NEOP + "name", "Acme Corp"),
        (subject, _NEOP + "employee_count", "412"),
        (subject, _NEOP + "created_at", "2026-01-01T00:00:00Z"),
    ]


def _gateway(
    *,
    sink: _RecordingSink | None = None,
    quarantine: SqliteTripleQuarantine | None = None,
    validator: PyShaclValidator | None = None,
) -> ValidatingTripleStore:
    settings = OntologySettings(include_ontology_graph=False)
    return ValidatingTripleStore(
        OxigraphBackend(),
        validator or build_validator(settings),
        quarantine or SqliteTripleQuarantine(),
        sink or _RecordingSink(),
        settings=settings,
        config_hash=_CONFIG_HASH,
        compiled_shapes=load_compiled_shapes(),
    )


def test_nonconforming_delta_quarantined_not_written() -> None:
    async def scenario() -> None:
        sink = _RecordingSink()
        quarantine = SqliteTripleQuarantine()
        gw = _gateway(sink=sink, quarantine=quarantine)
        # A Person missing the required email/family_name/given_name slots.
        bad = [("urn:ne:p/bad", _RDF, _PERSON), ("urn:ne:p/bad", _NEOP + "id", "bad")]
        with pytest.raises(TripleQuarantinedError) as excinfo:
            await gw.insert_triples(bad)
        # No triple of the batch is in the store (no partial writes).
        rows = await gw.sparql("SELECT ?s WHERE { <urn:ne:p/bad> ?p ?o }")
        assert rows == []
        # Quarantine row present with delta + violations + shapes_hash.
        parked = quarantine.list()
        assert len(parked) == 1
        entry = parked[0]
        assert entry.quarantine_id == excinfo.value.quarantine_id
        assert entry.violations
        assert entry.shapes_hash == load_compiled_shapes().shapes_hash
        assert "urn:ne:p/bad" in entry.delta_nt
        # Exactly one ShapeViolation, quarantined=True, config_hash + schema_version set.
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.quarantined is True
        assert event.config_hash == _CONFIG_HASH
        assert event.schema_version == entry.shapes_hash
        assert event.severity == "error"
        # Redaction-safe: message carries node IRIs/shape names, not literal payloads.
        assert "urn:ne:p/bad" in event.message

    asyncio.run(scenario())


def test_conforming_delta_written_and_selectable() -> None:
    async def scenario() -> None:
        gw = _gateway()
        await gw.insert_triples(_conforming_person("urn:ne:p/ada"))
        rows = await gw.sparql(f"SELECT ?s WHERE {{ ?s <{_RDF}> <{_PERSON}> }}")
        assert rows == [{"s": "urn:ne:p/ada"}]

    asyncio.run(scenario())


def test_validator_crash_is_denial_no_write_no_quarantine() -> None:
    class _CrashValidator:
        async def validate(self, graph: object) -> object:
            raise RuntimeError("malformed shapes")

    async def scenario() -> None:
        quarantine = SqliteTripleQuarantine()
        gw = _gateway(quarantine=quarantine, validator=_CrashValidator())  # type: ignore[arg-type]
        with pytest.raises(ShapeValidationError):
            await gw.insert_triples(_conforming_person("urn:ne:p/x"))
        # Nothing written, nothing quarantined.
        assert quarantine.list() == ()
        rows = await gw.sparql("SELECT ?s WHERE { <urn:ne:p/x> ?p ?o }")
        assert rows == []

    asyncio.run(scenario())


def test_release_writes_fixed_delta_and_removes_row() -> None:
    async def scenario() -> None:
        quarantine = SqliteTripleQuarantine()
        gw = _gateway(quarantine=quarantine)
        bad = [("urn:ne:p/r", _RDF, _PERSON), ("urn:ne:p/r", _NEOP + "id", "r")]
        with pytest.raises(TripleQuarantinedError):
            await gw.insert_triples(bad)
        parked = quarantine.list()[0]
        # A still-bad delta re-raises and the row stays.
        with pytest.raises(TripleQuarantinedError):
            await gw.release(parked.quarantine_id)
        assert quarantine.get(parked.quarantine_id) is not None
        # Fix the row's delta to a conforming Person, then release → written + removed.
        conforming = "\n".join(
            f"<{s}> <{p}> {_nt_obj(o)} ." for s, p, o in _conforming_person("urn:ne:p/r")
        )
        quarantine.remove(parked.quarantine_id)
        from ne.brain_ontology.quarantine import QuarantinedTriples

        quarantine.record(
            QuarantinedTriples(
                quarantine_id=parked.quarantine_id,
                delta_nt=conforming,
                violations=parked.violations,
                provenance=parked.provenance,
                shapes_hash=parked.shapes_hash,
                created_at=parked.created_at,
            )
        )
        await gw.release(parked.quarantine_id)
        assert quarantine.get(parked.quarantine_id) is None
        rows = await gw.sparql(f"SELECT ?s WHERE {{ ?s <{_RDF}> <{_PERSON}> }}")
        assert rows == [{"s": "urn:ne:p/r"}]

    asyncio.run(scenario())


def _nt_obj(value: str) -> str:
    if value.startswith(("http://", "https://", "urn:")):
        return f"<{value}>"
    return f'"{value}"'


def test_sparql_refuses_update_keywords_deterministically() -> None:
    async def scenario() -> None:
        gw = _gateway()
        forbidden = [
            "LOAD <http://evil/data>",
            "SELECT ?s WHERE { SERVICE <http://x> { ?s ?p ?o } }",
            "INSERT DATA { <urn:x> <urn:p> <urn:o> }",
            "delete where { ?s ?p ?o }",  # lowercase
            "# comment\nDROP GRAPH <urn:g>",  # comment-preceded
        ]
        for query in forbidden:
            with pytest.raises(ValueError):
                await gw.sparql(query)
        # A literal containing the word 'service' in a FILTER is FINE.
        await gw.insert_triples(_conforming_person("urn:ne:p/s"))
        rows = await gw.sparql(
            "SELECT ?s WHERE { ?s <" + _RDF + "> <" + _PERSON + "> "
            'FILTER(STR(?s) != "http://service.example") }'
        )
        assert rows == [{"s": "urn:ne:p/s"}]

    asyncio.run(scenario())


def test_sparql_accepts_prefixed_and_based_queries() -> None:
    async def scenario() -> None:
        gw = _gateway()
        await gw.insert_triples(_conforming_person("urn:ne:p/pfx"))
        # A PREFIX-bearing SELECT — the standard SPARQL idiom — must be recognised
        # as SELECT, not rejected because the prefix label sits after the keyword.
        rows = await gw.sparql(
            "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
            f"PREFIX op: <{_NEOP}>\n"
            "SELECT ?s WHERE { ?s rdf:type op:Person }"
        )
        assert rows == [{"s": "urn:ne:p/pfx"}]
        # BASE followed by PREFIX then ASK — both prologue forms peeled off.
        ask_rows = await gw.sparql(
            "BASE <http://example.org/>\n"
            "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
            f"ASK {{ ?s rdf:type <{_PERSON}> }}"
        )
        assert ask_rows == [{"ask": True}]
        # An empty-label PREFIX (``PREFIX : <...>``) is a valid prologue too.
        empty_pfx = await gw.sparql(
            f"PREFIX : <{_NEOP}>\nSELECT ?s WHERE {{ ?s <{_RDF}> :Person }}"
        )
        assert empty_pfx == [{"s": "urn:ne:p/pfx"}]

    asyncio.run(scenario())


def test_validate_full_graph_flags_global_only_violation() -> None:
    async def scenario() -> None:
        sink = _RecordingSink()
        gw = _gateway(sink=sink)
        # Two bare labeled resources sharing a label: each conforms per-write (no
        # operational targetClass matches), but the GLOBAL sh:sparql uniqueness
        # shape flags the pair.
        await gw.insert_triples([("urn:ne:x/1", _LABEL, "dup")])
        await gw.insert_triples([("urn:ne:x/2", _LABEL, "dup")])
        assert sink.events == []  # per-write writes emitted nothing
        report = await gw.validate_full_graph()
        assert report.conforms is False
        assert report.violations
        # A NON-quarantined ShapeViolation was emitted for the full-graph failure.
        assert len(sink.events) == 1
        assert sink.events[0].quarantined is False

    asyncio.run(scenario())


def test_typed_slot_write_validates_and_writes() -> None:
    # FIX-1: a record with typed LinkML slots (xsd:integer employee_count,
    # xsd:dateTime created_at) VALIDATES and WRITES — the gateway carries each
    # slot's datatype from the compiled per-write shapes into the RDF literal.
    async def scenario() -> None:
        sink = _RecordingSink()
        quarantine = SqliteTripleQuarantine()
        gw = _gateway(sink=sink, quarantine=quarantine)
        await gw.insert_triples(_conforming_account("urn:ne:a/acme"))
        # The typed-term match is the real proof: the literal carries xsd:integer,
        # so the SPARQL integer 412 matches — an untyped "412" would NOT.
        assert await gw.sparql(f"ASK {{ <urn:ne:a/acme> <{_NEOP}employee_count> 412 }}") == [
            {"ask": True}
        ]
        assert await gw.sparql(f'ASK {{ <urn:ne:a/acme> <{_NEOP}employee_count> "412" }}') == [
            {"ask": False}
        ]  # negative control: not a plain string literal
        # Nothing quarantined, nothing emitted.
        assert quarantine.list() == ()
        assert sink.events == []

    asyncio.run(scenario())


def test_wrong_typed_value_still_quarantined() -> None:
    # FIX-1 must not weaken fail-closed: a genuinely ill-typed value
    # ('not-a-number' for an xsd:integer slot) is still quarantined, not written.
    async def scenario() -> None:
        sink = _RecordingSink()
        quarantine = SqliteTripleQuarantine()
        gw = _gateway(sink=sink, quarantine=quarantine)
        bad = [
            ("urn:ne:a/bad", _RDF, _NEOP + "Account"),
            ("urn:ne:a/bad", _NEOP + "id", "bad"),
            ("urn:ne:a/bad", _NEOP + "name", "Bad Corp"),
            ("urn:ne:a/bad", _NEOP + "employee_count", "not-a-number"),
        ]
        with pytest.raises(TripleQuarantinedError):
            await gw.insert_triples(bad)
        # Nothing written.
        rows = await gw.sparql("SELECT ?s WHERE { <urn:ne:a/bad> ?p ?o }")
        assert rows == []
        # Exactly one quarantine row + one quarantined ShapeViolation.
        assert len(quarantine.list()) == 1
        assert len(sink.events) == 1
        assert sink.events[0].quarantined is True

    asyncio.run(scenario())


def test_sparql_accepts_iri_containing_forbidden_keyword_segments() -> None:
    # FIX-2 (also closes the Argus P3 steward-surface over-rejection): a SELECT/ASK
    # whose IRIs merely CONTAIN 'service'/'load'/'insert'/'create'/'drop' path
    # segments is ACCEPTED — the keyword ban applies to SPARQL syntax, never IRIs.
    async def scenario() -> None:
        gw = _gateway()
        # A SELECT whose predicate IRI contains '/service/'.
        assert await gw.sparql("SELECT ?s WHERE { ?s <https://example.com/service/123> ?o }") == []
        # An ASK over an IRI subject 'urn:x:load-1' and a multi-keyword IRI.
        assert await gw.sparql(
            'ASK { <urn:x:load-1> <https://api.example.com/v1/insert/create/drop> "v" }'
        ) == [{"ask": False}]
        # A PREFIX prologue whose IRI contains '/service/' must not trip the ban.
        assert (
            await gw.sparql(
                f"PREFIX svc: <https://example.com/service/>\n"
                f"SELECT ?s WHERE {{ ?s <{_RDF}> <{_PERSON}> }}"
            )
            == []
        )

    asyncio.run(scenario())


def _conforming_ticket(subject: str, account_iri: str) -> list[tuple[str, str, str]]:
    # A Ticket's required slots are status (enum), opened_at (xsd:dateTime),
    # subject (xsd:string) and belongs_to → Account (sh:class Account, IRI). The
    # belongs_to target is a REFERENCE to an Account that lives elsewhere in the
    # store — the Ticket batch does NOT (re)send the Account's own triples.
    return [
        (subject, _RDF, _NEOP + "Ticket"),
        (subject, _NEOP + "status", "open"),
        (subject, _NEOP + "opened_at", "2026-01-01T00:00:00Z"),
        (subject, _NEOP + "subject", "Cannot log in"),
        (subject, _NEOP + "belongs_to", account_iri),
    ]


def test_relationship_to_existing_entity_validates_without_resend() -> None:
    # Codex P1: a class-ranged relationship (Ticket.belongs_to → Account) to an
    # entity ALREADY in the store must validate against the generated sh:class
    # constraint WITHOUT the caller resending the referenced Account in the same
    # batch. The delta+CBD context now pulls the referenced object's outgoing
    # description (its rdf:type + required slots), so sh:class is satisfied.
    async def scenario() -> None:
        sink = _RecordingSink()
        quarantine = SqliteTripleQuarantine()
        gw = _gateway(sink=sink, quarantine=quarantine)
        account = _NEOP + "acct-recon"
        await gw.insert_triples(_conforming_account(account))
        # Separate incremental write: a Ticket that only REFERENCES the account.
        await gw.insert_triples(_conforming_ticket("urn:ne:t/recon", account))
        rows = await gw.sparql(f"SELECT ?a WHERE {{ <urn:ne:t/recon> <{_NEOP}belongs_to> ?a }}")
        assert rows == [{"a": account}]
        # Not quarantined, no violation emitted.
        assert quarantine.list() == ()
        assert sink.events == []

    asyncio.run(scenario())


def test_relationship_to_missing_entity_still_quarantined() -> None:
    # Fail-closed retained: a belongs_to pointing at an Account that is NOT in the
    # store pulls no rdf:type into the CBD context, so sh:class Account fails and
    # the Ticket is quarantined (the fix must not turn dangling refs into writes).
    async def scenario() -> None:
        quarantine = SqliteTripleQuarantine()
        gw = _gateway(quarantine=quarantine)
        with pytest.raises(TripleQuarantinedError):
            await gw.insert_triples(_conforming_ticket("urn:ne:t/dangle", _NEOP + "ghost"))
        rows = await gw.sparql("SELECT ?s WHERE { <urn:ne:t/dangle> ?p ?o }")
        assert rows == []
        assert len(quarantine.list()) == 1

    asyncio.run(scenario())


def test_full_union_read_surface() -> None:
    async def scenario() -> None:
        gw = _gateway()
        await gw.insert_triples(_conforming_person("urn:ne:p/u"))
        # ASK routed through the gateway returns the wrapped bool row.
        rows = await gw.sparql(f"ASK {{ ?s <{_RDF}> <{_PERSON}> }}")
        assert rows == [{"ask": True}]
        # DatasetSpec default-union is what the steward surface uses.
        assert DatasetSpec().default_graphs == ()

    asyncio.run(scenario())
