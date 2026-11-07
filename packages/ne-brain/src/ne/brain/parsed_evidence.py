# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""In-process, one-shot capabilities for structure-aware document evidence."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Self
from weakref import WeakKeyDictionary

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from ne.brain.ingest.chunker import StructuralChunker

__all__ = [
    "PARSED_RECORD_METADATA_KEY",
    "ParsedDocumentEvidenceDescriptor",
    "carried_record_payload",
]

_PARSED_DOCUMENT_SCHEMA = "ne.parsed_document/1"
_HANDLE_CONSTRUCTION_CAPABILITY = object()

#: Chunk-metadata key carrying the FULL connector record payload for a chunk
#: whose ``text`` is the parsed document PROSE rather than the payload JSON.
#:
#: WHY IT EXISTS. The M365 mapper used to serialize the entire Graph record —
#: ETag, ULIDs, parser hashes, timestamps, ``body_blocks`` — as the chunk's
#: searchable ``text``, so retrieval scored queries against a vector dominated
#: by JSON scaffolding and the prose never ranked. The mapper now writes the
#: parsed body prose as ``text`` and carries the exact record payload HERE, so
#: every consumer that read ``json.loads(chunk.text)`` (the writer's
#: parsed-evidence binding, lineage's document-version snapshot) keeps reading
#: the identical payload without the prose regressing back into the embedding.
#:
#: The carried payload is authored by the same governed mapper that used to
#: author it as ``text`` — same producer, same trust, same validation: the
#: parsed-evidence claim hashes (:func:`_claim_matches`) are verified against
#: it exactly as they were against the text form.
PARSED_RECORD_METADATA_KEY = "ne_parsed_record"


def carried_record_payload(metadata: Mapping[str, object]) -> dict[str, object] | None:
    """The record payload carried under :data:`PARSED_RECORD_METADATA_KEY`, or None.

    Returns a plain ``dict`` (JSON-shaped) so consumers treat it exactly like the
    ``json.loads(chunk.text)`` value it replaces. A non-mapping value is treated
    as absent — the fail-closed reading for a malformed carrier.
    """
    value = metadata.get(PARSED_RECORD_METADATA_KEY)
    if not isinstance(value, Mapping):
        return None
    plain = _plain(value)
    assert isinstance(plain, dict)
    return plain


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            _plain(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    ).hexdigest()


def _acl_hash(labels: Sequence[str]) -> str:
    return _canonical_hash({"acl": sorted(set(labels))})


def _parsed_subset(payload: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "body",
        "body_blocks",
        "body_block_count",
        "body_content_sha256",
        "body_ingestion_status",
        "body_schema_version",
        "body_source_format",
        "body_parser_identity",
    )
    return {key: payload.get(key) for key in keys}


@dataclass(frozen=True)
class _Claim:
    issuer_nonce: object
    source_id: str
    record_id: str
    parser_identity: str
    schema_version: str
    source_format: str
    body_sha256: str
    blocks_sha256: str
    payload_sha256: str
    acl_sha256: str
    chunker_revision: str
    max_chars: int
    min_chars: int
    include_headings_in_text: bool
    item_id: str | None = None
    consumed: bool = False


class ParsedDocumentEvidenceDescriptor:
    """Opaque, non-copyable handle to one connector-owned parser claim.

    The handle contains no reconstructible claim bytes. Its state lives only in
    a weak, identity-keyed process registry and is consumed once by the writer.
    """

    __slots__ = ("__weakref__",)

    def __new__(
        cls, *, _construction_capability: object | None = None
    ) -> ParsedDocumentEvidenceDescriptor:
        if _construction_capability is not _HANDLE_CONSTRUCTION_CAPABILITY:
            raise TypeError(
                "parsed evidence capabilities are issued only by an M365 parser/connector instance"
            )
        return super().__new__(cls)

    def __init__(self, *, _construction_capability: object | None = None) -> None:
        del _construction_capability

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: object, _handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(cls)

    def __copy__(self) -> ParsedDocumentEvidenceDescriptor:
        raise TypeError("parsed evidence capabilities cannot be copied")

    def __deepcopy__(self, _memo: dict[int, object]) -> ParsedDocumentEvidenceDescriptor:
        raise TypeError("parsed evidence capabilities cannot be copied")

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("parsed evidence capabilities cannot be serialized")

    def _value(self, name: str) -> object:
        with _REGISTRY_LOCK:
            claim = _CLAIMS.get(self)
            if claim is None:
                raise ValueError("parsed evidence capability is unknown")
            return getattr(claim, name)

    @property
    def body_sha256(self) -> str:
        return str(self._value("body_sha256"))

    @property
    def max_chars(self) -> int:
        value = self._value("max_chars")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("parsed evidence capability is invalid")
        return value

    @property
    def min_chars(self) -> int:
        value = self._value("min_chars")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("parsed evidence capability is invalid")
        return value

    @property
    def include_headings_in_text(self) -> bool:
        value = self._value("include_headings_in_text")
        if not isinstance(value, bool):
            raise ValueError("parsed evidence capability is invalid")
        return value

    def matches_parser_payload(
        self,
        *,
        item_id: object,
        source_id: object,
        record_id: object,
        acl: Sequence[str],
        payload: Mapping[str, object],
    ) -> bool:
        """Confirm the writer consumed this exact handle for this exact item."""
        with _REGISTRY_LOCK:
            claim = _CLAIMS.get(self)
            return bool(
                claim is not None
                and claim.consumed
                and _claim_matches(
                    claim,
                    item_id=item_id,
                    source_id=source_id,
                    record_id=record_id,
                    acl=acl,
                    payload=payload,
                )
            )


_CLAIMS: WeakKeyDictionary[ParsedDocumentEvidenceDescriptor, _Claim] = WeakKeyDictionary()
_REGISTRY_LOCK = threading.RLock()


def _new_handle(claim: _Claim) -> ParsedDocumentEvidenceDescriptor:
    handle = ParsedDocumentEvidenceDescriptor(
        _construction_capability=_HANDLE_CONSTRUCTION_CAPABILITY
    )
    with _REGISTRY_LOCK:
        _CLAIMS[handle] = claim
    return handle


def _claim_matches(
    claim: _Claim,
    *,
    item_id: object,
    source_id: object,
    record_id: object,
    acl: Sequence[str],
    payload: Mapping[str, object],
) -> bool:
    body = payload.get("body")
    blocks = payload.get("body_blocks")
    return (
        claim.item_id == item_id
        and claim.source_id == source_id
        and claim.record_id == record_id
        and payload.get("body_parser_identity") == claim.parser_identity
        and payload.get("body_schema_version") == claim.schema_version
        and payload.get("body_source_format") == claim.source_format
        and isinstance(body, str)
        and isinstance(blocks, (list, tuple))
        and hashlib.sha256(body.encode()).hexdigest() == claim.body_sha256
        and _canonical_hash(blocks) == claim.blocks_sha256
        and _canonical_hash(_parsed_subset(payload)) == claim.payload_sha256
        and _acl_hash(acl) == claim.acl_sha256
        and claim.chunker_revision == StructuralChunker.EVIDENCE_REVISION
        and claim.max_chars == StructuralChunker.DEFAULT_MAX_CHARS
        and claim.min_chars == StructuralChunker.DEFAULT_MIN_CHARS
        and claim.include_headings_in_text is StructuralChunker.DEFAULT_INCLUDE_HEADINGS_IN_TEXT
    )


_AUTHORITY_SUBCLASS: type[_M365ParsedEvidenceAuthority] | None
_AUTHORITY_SUBCLASS = None
_COMPOSED_AUTHORITIES: WeakKeyDictionary[object, object] = WeakKeyDictionary()


class _M365ParsedEvidenceAuthority:
    """Sealed composition seam owned only by the exact M365 connector class.

    This is an in-process provenance boundary, not a sandbox against hostile
    trusted Python reflection. Ordinary callers cannot construct an authority,
    subclass it, or recover a general issuer from public parsed Record bytes.
    """

    __slots__ = ()

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        del args, kwargs
        if (
            cls.__module__ != "ne.connect.m365.connector"
            or cls.__qualname__ != "M365GraphConnector"
            or cls is not _AUTHORITY_SUBCLASS
        ):
            raise TypeError("parsed evidence authority is sealed to M365GraphConnector")
        return super().__new__(cls)

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        global _AUTHORITY_SUBCLASS
        if (
            cls.__module__ != "ne.connect.m365.connector"
            or cls.__qualname__ != "M365GraphConnector"
            or (_AUTHORITY_SUBCLASS is not None and _AUTHORITY_SUBCLASS is not cls)
        ):
            raise TypeError("parsed evidence authority cannot be subclassed")
        _AUTHORITY_SUBCLASS = cls

    def _compose_parsed_evidence_channel(
        self,
    ) -> tuple[
        Callable[..., ParsedDocumentEvidenceDescriptor],
        Callable[..., ParsedDocumentEvidenceDescriptor],
    ]:
        """Return one parser issue closure and its same-instance claim closure."""
        if type(self) is not _AUTHORITY_SUBCLASS:
            raise TypeError("parsed evidence authority requires exact M365GraphConnector")
        with _REGISTRY_LOCK:
            if self in _COMPOSED_AUTHORITIES:
                raise TypeError("M365 connector parsed evidence channel is already composed")
            nonce = object()
            _COMPOSED_AUTHORITIES[self] = nonce

        def issue(
            *,
            source_id: str,
            record_id: str,
            acl: Sequence[str],
            payload: Mapping[str, object],
        ) -> ParsedDocumentEvidenceDescriptor:
            body = payload.get("body")
            blocks = payload.get("body_blocks")
            parser_identity = payload.get("body_parser_identity")
            schema_version = payload.get("body_schema_version")
            source_format = payload.get("body_source_format")
            if (
                payload.get("body_ingestion_status") != "parsed"
                or not isinstance(body, str)
                or not isinstance(blocks, list)
                or not isinstance(parser_identity, str)
                or not isinstance(schema_version, str)
                or schema_version != _PARSED_DOCUMENT_SCHEMA
                or not isinstance(source_format, str)
            ):
                raise ValueError("M365 parsed evidence descriptor requires exact parsed payload")
            return _new_handle(
                _Claim(
                    issuer_nonce=nonce,
                    source_id=source_id,
                    record_id=record_id,
                    parser_identity=parser_identity,
                    schema_version=schema_version,
                    source_format=source_format,
                    body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                    blocks_sha256=_canonical_hash(blocks),
                    payload_sha256=_canonical_hash(_parsed_subset(payload)),
                    acl_sha256=_acl_hash(acl),
                    chunker_revision=StructuralChunker.EVIDENCE_REVISION,
                    max_chars=StructuralChunker.DEFAULT_MAX_CHARS,
                    min_chars=StructuralChunker.DEFAULT_MIN_CHARS,
                    include_headings_in_text=StructuralChunker.DEFAULT_INCLUDE_HEADINGS_IN_TEXT,
                )
            )

        def claim(
            descriptor: ParsedDocumentEvidenceDescriptor,
            *,
            item_id: str,
            source_id: str,
            record_id: str,
            acl: Sequence[str],
            payload: Mapping[str, object],
        ) -> ParsedDocumentEvidenceDescriptor:
            with _REGISTRY_LOCK:
                current = _CLAIMS.get(descriptor)
                claimed = replace(current, item_id=item_id) if current is not None else None
                if (
                    current is None
                    or current.issuer_nonce is not nonce
                    or current.item_id is not None
                    or current.consumed
                    or claimed is None
                    or not _claim_matches(
                        claimed,
                        item_id=item_id,
                        source_id=source_id,
                        record_id=record_id,
                        acl=acl,
                        payload=payload,
                    )
                ):
                    raise ValueError("M365 connector parser evidence round trip failed")
                _CLAIMS[descriptor] = claimed
            return descriptor

        return issue, claim


_EvidenceBinding = tuple[
    ParsedDocumentEvidenceDescriptor,
    str,
    object,
    object,
    Sequence[str],
    Mapping[str, object],
]


def _validate_bindings(bindings: Sequence[_EvidenceBinding], *, consume: bool) -> None:
    with _REGISTRY_LOCK:
        handles = [binding[0] for binding in bindings]
        item_ids = [binding[1] for binding in bindings]
        if len({id(handle) for handle in handles}) != len(handles) or len(set(item_ids)) != len(
            item_ids
        ):
            raise ValueError("parsed evidence capabilities must bind unique items")
        claims: list[_Claim] = []
        for descriptor, item_id, source_id, record_id, acl, payload in bindings:
            claim = _CLAIMS.get(descriptor)
            if (
                claim is None
                or claim.consumed
                or not _claim_matches(
                    claim,
                    item_id=item_id,
                    source_id=source_id,
                    record_id=record_id,
                    acl=acl,
                    payload=payload,
                )
            ):
                raise ValueError("parsed source evidence binding is invalid")
            claims.append(claim)
        if consume:
            for descriptor, claim in zip(handles, claims, strict=True):
                _CLAIMS[descriptor] = replace(claim, consumed=True)


def _peek_parsed_document_evidence(bindings: Sequence[_EvidenceBinding]) -> None:
    """Validate an entire writer batch without consuming any handle."""
    _validate_bindings(bindings, consume=False)


def _consume_parsed_document_evidence(bindings: Sequence[_EvidenceBinding]) -> None:
    """Atomically revalidate and consume an entire writer batch exactly once."""
    _validate_bindings(bindings, consume=True)
