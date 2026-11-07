# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Closed 45-node/40-edge Meridian M365 construction controls."""

from __future__ import annotations

from ne.brain_graph.config import GraphSourceMapping
from ne.brain_graph.construct.mappers import MappedEntity, QuarantinedRecord, map_record
from ne.brain_graph.construct.meridian import meridian_m365_graph_mapping
from ne.brain_graph.construct.runner import M365GraphSyncHost
from ne.connect.acl import AclMapper, build_acl_index
from ne.connect.contracts import PermissionMode, PermissionModelSpec
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.operational_graph import (
    OperationalGraphItemBinding,
    parse_operational_graph_body,
    validate_binding_acls,
)
from ne.contracts.integration import AclEntry, Record
from ne.meridian_seed.slice import OperationalGraphDocument, slice_corpus
from ne.schema import SCHEMA_VERSION

_DRIVE = "drive-meridian"
_SOURCE = "m365-sharepoint"
_PERSONA_GROUPS = {
    "ops": "group-ops",
    "approver": "group-approver",
    "frontline": "group-frontline",
}
#: SharePoint puts its own inherited grant on every item in the drive, so a LIVE
#: crawl always reports one principal the seeder never invited. It is why the
#: production graph shows 4-vs-3 group counts where the seed authors 3-vs-2; it
#: is identical on every record and therefore never the source of an asymmetry.
_INHERITED_DRIVE_GROUP = "group-drive-inherited"
#: The account-level record types a frontline principal must be refused. One
#: verdict for all three, on every path -- that is the property this file pins.
_WITHHELD_CLASSES = ("AutoLoan", "PaymentSchedule", "ServicingNote")


def _authored_acl(document: OperationalGraphDocument) -> tuple[str, ...]:
    """The seeder's grant recipients for one record, straight from the slice.

    Derived from what ``slice_corpus`` AUTHORS rather than restated here, so a
    change to the withheld set cannot leave this file pinning the old shape.
    """
    return tuple(_PERSONA_GROUPS[persona] for persona in document.acl.allowed_personas)


def _bindings() -> tuple[OperationalGraphItemBinding, ...]:
    return tuple(
        OperationalGraphItemBinding(
            record_id=document.record_id,
            entity_class=document.entity_class,
            item_id=f"item-{document.entity_class.lower()}-{document.record_id}",
            path=document.filename,
            acl=_authored_acl(document),
            edges=document.edges,
        )
        for document in slice_corpus().operational_graph
    )


def _mapping() -> GraphSourceMapping:
    return meridian_m365_graph_mapping(
        source_system=_SOURCE,
        drive_id=_DRIVE,
        bindings=_bindings(),
    )


def _records() -> tuple[tuple[Record, tuple[str, ...]], ...]:
    documents = {
        (document.entity_class, document.record_id): document
        for document in slice_corpus().operational_graph
    }
    records: list[tuple[Record, tuple[str, ...]]] = []
    for binding in _bindings():
        document = documents[(binding.entity_class, binding.record_id)]
        payload = parse_operational_graph_body(
            document.content,
            name=document.filename,
            mime_type=document.media_type,
        )
        assert payload is not None
        payload.update(
            {
                "id": binding.item_id,
                "name": binding.path,
                "web_url": f"https://example.invalid/{binding.path}",
                "last_modified": "2026-08-23T12:00:00Z",
                "graph_doc_type": "other",
            }
        )
        records.append(
            (
                Record(
                    source_id=f"{_DRIVE}:{binding.item_id}",
                    record_id=binding.item_id,
                    payload=payload,
                ),
                binding.acl,
            )
        )
    return tuple(records)


def test_mapping_is_document_fallback_plus_exact_longest_prefix_population() -> None:
    mapping = _mapping()
    assert len(mapping.entities) == 46
    fallback = mapping.resolve(f"{_DRIVE}:ordinary-document")
    assert fallback is not None and fallback.entity_class == "Document"
    exact = mapping.resolve(f"{_DRIVE}:item-customer-mer-cus-001")
    assert exact is not None and exact.entity_class == "Customer"
    assert len(exact.source_id_prefix) > len(fallback.source_id_prefix)


def test_exact_population_maps_45_nodes_40_edges_with_closed_acl() -> None:
    mapping = _mapping()
    mapped: list[MappedEntity] = []
    for record, acl in _records():
        resolved = mapping.resolve(record.source_id)
        assert resolved is not None
        result = map_record(
            record,
            resolved,
            source_system=_SOURCE,
            acl=acl,
            config_hash="meridian-structured-test",
            schema_version=SCHEMA_VERSION,
        )
        assert isinstance(result, MappedEntity)
        mapped.append(result)
    assert len(mapped) == 45
    assert sum(len(entity.edges) for entity in mapped) == 40
    assert {
        entity_class: sum(entity.entity_class == entity_class for entity in mapped)
        for entity_class in ("Customer", "AutoLoan", "PaymentSchedule", "ServicingNote")
    } == {"Customer": 15, "AutoLoan": 10, "PaymentSchedule": 10, "ServicingNote": 10}
    keys = {entity.key for entity in mapped}
    assert all(edge.target_key in keys for entity in mapped for edge in entity.edges)
    assert all(
        "group-frontline" not in entity.acl
        for entity in mapped
        if entity.entity_class in _WITHHELD_CLASSES
    )


def test_frontline_gets_one_verdict_for_every_account_record_on_every_path() -> None:
    """One principal, one answer: AutoLoan/PaymentSchedule/ServicingNote alike.

    The defect this pins is a frontline analyst getting TWO answers to the same
    question depending on which record type served it -- loans and payment
    schedules readable while servicing notes were refused. The cause was seed
    data, not policy: the slice granted frontline 3 of the 4 operational classes
    and withheld only ``ServicingNote``. So the property is asserted at every
    layer the grant flows through, because a fix at one layer only would leave
    the split alive at the others:

    * AUTHORING -- ``slice_corpus`` emits one identical allowed-persona set.
    * RECEIPT -- ``validate_binding_acls`` accepts that set and REFUSES a
      frontline-widened ``AutoLoan``, so a re-seed cannot drift back.
    * T1 -- ``AclMapper.resolve`` over a live-shaped permission crawl (drive
      inheritance included) stamps the same chunk ACL for all three.
    * T2 -- ``map_record`` stamps the same graph-vertex ACL for all three.

    MUTATION-CHECK: remove ``"AutoLoan"`` from
    ``ne.connect.m365.operational_graph.MERIDIAN_FRONTLINE_WITHHELD_CLASSES``
    (exactly the pre-fix state) and the authoring leg fails first on
    ``assert persona_sets == {("approver", "ops")}`` with
    ``AssertionError: assert {('approver',...over', 'ops')} == {('approver', 'ops')}``
    -- ``persona_sets`` now holds BOTH ``('approver', 'frontline', 'ops')`` and
    ``('approver', 'ops')``, i.e. two verdicts for one principal across the three
    classes. Verified: that mutation also reds
    ``test_exact_population_maps_45_nodes_40_edges_with_closed_acl`` here and
    ``test_slice_produces_exact_canonical_45_items_and_40_edges`` in
    ``tools/meridian-seed`` (3 failed / 9 passed across the two files).
    """
    documents = [
        document
        for document in slice_corpus().operational_graph
        if document.entity_class in _WITHHELD_CLASSES
    ]
    assert {document.entity_class for document in documents} == set(_WITHHELD_CLASSES)

    # AUTHORING: one persona set shared by all three classes, frontline denied.
    persona_sets = {tuple(sorted(document.acl.allowed_personas)) for document in documents}
    assert persona_sets == {("approver", "ops")}
    assert all("frontline" in document.acl.denied_personas for document in documents)

    # RECEIPT: the manifest verifier accepts the authored grants for all three...
    bindings = _bindings()
    validate_binding_acls(
        bindings,
        ops_group="group-ops",
        approver_group="group-approver",
        frontline_group="group-frontline",
    )
    # ...and refuses each one re-widened back to include frontline.
    for entity_class in _WITHHELD_CLASSES:
        index = next(
            position
            for position, binding in enumerate(bindings)
            if binding.entity_class == entity_class
        )
        target = bindings[index]
        widened = (
            *bindings[:index],
            target.model_copy(update={"acl": (*target.acl, "group-frontline")}),
            *bindings[index + 1 :],
        )
        try:
            validate_binding_acls(
                widened,
                ops_group="group-ops",
                approver_group="group-approver",
                frontline_group="group-frontline",
            )
        except ValueError as error:
            assert entity_class in str(error)
        else:  # pragma: no cover - only reachable if the receipt check regresses
            raise AssertionError(f"{entity_class} ACL widening was accepted")

    # T1: the chunk ACL the connector resolves, from a live-shaped crawl.
    mapper = AclMapper(PermissionModelSpec(mode=PermissionMode.SOURCE_ACL))
    crawl = build_acl_index(
        AclEntry(
            resource_id=f"{_DRIVE}:{binding.item_id}",
            principals=(*binding.acl, _INHERITED_DRIVE_GROUP),
        )
        for binding in bindings
    )
    tier1: dict[str, set[tuple[str, ...]]] = {}
    tier2: dict[str, set[tuple[str, ...]]] = {}
    mapping = _mapping()
    for record, acl in _records():
        resolved = mapping.resolve(record.source_id)
        assert resolved is not None
        if resolved.entity_class not in _WITHHELD_CLASSES:
            continue
        tier1.setdefault(resolved.entity_class, set()).add(
            tuple(sorted(mapper.resolve(record, crawl)))
        )
        entity = map_record(
            record,
            resolved,
            source_system=_SOURCE,
            acl=acl,
            config_hash="meridian-structured-test",
            schema_version=SCHEMA_VERSION,
        )
        assert isinstance(entity, MappedEntity)
        tier2.setdefault(resolved.entity_class, set()).add(tuple(sorted(entity.acl)))

    assert set(tier1) == set(tier2) == set(_WITHHELD_CLASSES)
    # Each tier is internally uniform across the three classes...
    assert len({acls for class_acls in tier1.values() for acls in class_acls}) == 1
    assert len({acls for class_acls in tier2.values() for acls in class_acls}) == 1
    # ...and the two tiers differ only by the drive's inherited grant, never by
    # a persona -- so no path can hand the frontline principal a read.
    (t1_acl,) = {acls for class_acls in tier1.values() for acls in class_acls}
    (t2_acl,) = {acls for class_acls in tier2.values() for acls in class_acls}
    assert t1_acl == ("group-approver", _INHERITED_DRIVE_GROUP, "group-ops")
    assert t2_acl == ("group-approver", "group-ops")
    assert "group-frontline" not in t1_acl and "group-frontline" not in t2_acl


def test_payload_target_mismatch_quarantines_instead_of_retargeting() -> None:
    mapping = _mapping()
    record, acl = _records()[0]
    payload = dict(record.payload)
    payload["ne_meridian_operational_edge_has_auto_loan"] = ["mer-loan-999"]
    resolved = mapping.resolve(record.source_id)
    assert resolved is not None
    result = map_record(
        record.model_copy(update={"payload": payload}),
        resolved,
        source_system=_SOURCE,
        acl=acl,
        config_hash="meridian-structured-test",
        schema_version=SCHEMA_VERSION,
    )
    assert isinstance(result, QuarantinedRecord)
    assert "exact admitted target" in result.reason


def test_missing_declared_edge_quarantines_instead_of_dropping_relationship() -> None:
    mapping = _mapping()
    record, acl = _records()[0]
    payload = dict(record.payload)
    payload.pop("ne_meridian_operational_edge_has_auto_loan")
    resolved = mapping.resolve(record.source_id)
    assert resolved is not None
    result = map_record(
        record.model_copy(update={"payload": payload}),
        resolved,
        source_system=_SOURCE,
        acl=acl,
        config_hash="meridian-structured-test",
        schema_version=SCHEMA_VERSION,
    )
    assert isinstance(result, QuarantinedRecord)
    assert "exact admitted target" in result.reason


def test_production_host_accepts_exact_closed_mapping_and_refuses_partial() -> None:
    mapping = _mapping()
    host = M365GraphSyncHost(
        store=object(),  # type: ignore[arg-type]
        ledger=object(),  # type: ignore[arg-type]
        mappings=(mapping,),
        evidence_recorder=object(),  # type: ignore[arg-type]
        source_authority_provider=object(),  # type: ignore[arg-type]
        audit_sink=object(),  # type: ignore[arg-type]
        config_hash="test",
    )
    settings = M365ConnectorSettings(
        tenant_id="tenant",
        client_id="client",
        auth_method="secret",
        client_secret="not-a-real-secret",  # type: ignore[arg-type]
        drive_ids=(_DRIVE,),
        owner="test",
    )
    assert host.mapping_for(settings, resolved_drive_ids=(_DRIVE,)) == mapping

    partial = GraphSourceMapping(source_system=_SOURCE, entities=mapping.entities[:-1])
    partial_host = M365GraphSyncHost(
        store=object(),  # type: ignore[arg-type]
        ledger=object(),  # type: ignore[arg-type]
        mappings=(partial,),
        evidence_recorder=object(),  # type: ignore[arg-type]
        source_authority_provider=object(),  # type: ignore[arg-type]
        audit_sink=object(),  # type: ignore[arg-type]
        config_hash="test",
    )
    try:
        partial_host.mapping_for(settings, resolved_drive_ids=(_DRIVE,))
    except ValueError as exc:
        assert "exact closed" in str(exc)
    else:  # pragma: no cover - fail-loud discriminator
        raise AssertionError("partial Meridian graph mapping was admitted")
