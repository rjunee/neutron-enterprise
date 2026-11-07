# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ExtractionWriter`` — the ONLY module that may compose a ``source_system='ne:llm'``
graph node or edge (design §3.2, C-P1-6's "sole-writer sweep").

**The §5.2 fail-open hazard, and why this seam owns it.** ``GraphEntityRef`` /
``GraphHop`` (``ne.contracts.types``) are ``frozen=True`` but NOT
``extra="forbid"`` — an unknown/misspelled key on a payload constructed from
those models is silently IGNORED rather than rejected, and the ``assurance``
field then falls back to its default ``"source"``: the field fails OPEN toward
the TRUSTED-sounding value. That defaulting direction is correct and
unavoidable for those read-side contract types (backward compatibility), but
it means NOTHING downstream of a deserialized/model-authored payload can be
trusted to carry an honest ``assurance``/``acl``/``source_system`` on its own.
This module is where that risk is owned: every §2.1 floor field —
``acl``, ``assurance``, ``source_system``, ``source_ref``, ``config_hash``,
``schema_version``, ``synced_at``, ``evidence`` — is composed HERE as a
writer-stamped constant or a value derived from a TRUSTED input the caller
supplies out-of-band (the evidencing chunk's ACL, resolved by the caller's
:class:`~ne.brain_graph.extraction.apply.ChunkAclResolver`, never from the
assertion itself). Model output and any deserialized
:class:`~ne.flywheel.proposals.GraphExtractionBatch` payload contribute ONLY
contract-validated business property VALUES — never a floor field. A red-team
guardian test (``test_extraction_red_team.py``,
``test_model_output_acl_assurance_source_system_keys_are_ignored``) locks this:
an assertion whose ``properties`` carry ``acl``/``assurance``/``source_system``/
... keys must never see those keys survive into the written node.

**Multi-chunk ACL synthesis is unrepresentable here** (design §4, A-P0-1):
:meth:`write_entity`/:meth:`write_edge` each take exactly ONE ``chunk_acl`` —
there is no parameter, code path, or helper anywhere in this module that
accepts two ACLs and combines them. A re-evidencing write (the SAME
deterministic entity key, evidenced by a SECOND chunk) appends an evidence ref
and updates ``synced_at``; it NEVER touches ``acl`` and NEVER downgrades
``assurance`` away from ``"confirmed"``. Widening an entity's ACL to cover a
second chunk's visibility is a steward-approved, audited act that has NO code
path in this unit — a later unit may add one, deliberately, with its own
review gate.

**Read-side disclosure of ``evidence`` — CLOSED by NE-T2X-3.** The ``acl``
rule above governs which principals can see a node/edge AT ALL — but
``evidence`` (the ``"<proposal_id>|<chunk_ref>"`` ref list this writer
stamps) was, until NE-T2X-3, stored as an ordinary property that ``read.py``'s
principal-scoped projection did not strip: a node re-evidenced from a SECOND
chunk whose ACL a given principal does NOT hold could disclose that chunk's
ref/proposal id to every principal who CAN see the node (an over-return
through the metadata channel, not the ACL — the same harm A-P0-1 targets,
arriving through a different field). NE-T2X-3 closes this: ``read.py`` now
strips ``evidence`` from every ``ne:llm`` element's read payload (scoped, not
folded into the unconditional ``_GOVERNANCE`` denylist — a backbone element's
contract-declared ``evidence`` business field still surfaces, since extraction
evidence refs exist only on elements this pathway wrote), and
``test_extraction_red_team.py``'s
``test_re_evidenced_node_evidence_never_surfaces_in_read_payload`` locks the
new behavior: the store-side refs still persist (refcounting for
``revert_extraction`` depends on them), but neither ref reaches a read
payload. This module's own writes are unchanged by that fix — ``evidence`` is
still stamped here exactly as before; only the READ projection changed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from ne.brain_graph.config import _EDGE_INDEX
from ne.brain_graph.errors import GraphAclError
from ne.brain_graph.extraction import ASSURANCE_VALUES, LLM_SOURCE_SYSTEM
from ne.brain_graph.extraction.identity import llm_entity_key
from ne.brain_graph.model import edge_key_for
from ne.flywheel.proposals import GraphExtractionAssertion
from ne.schema import ENTITY_CONTRACTS

__all__ = [
    "ASSURANCE_VALUES",
    "ExtractionWriteStore",
    "ExtractionWriter",
    "validate_extracted_properties",
]

# Re-export of ``extraction.ASSURANCE_VALUES`` (the constants-leaf single
# source — Argus P3-1) for API stability: existing callers importing
# ``ASSURANCE_VALUES`` from this module keep working without a local copy.
# See ``extraction/__init__.py`` for the definition and rationale.
# (kept in ``__all__`` above; the re-import satisfies ruff F401.) Plain `#`
# comments deliberately, not `#:` — there is no assignment on the next line
# for a Sphinx attribute-doc block to attach to; a `#:` block here would
# silently document the wrong symbol (Argus P3, prior round).

#: Keys a sanitizing wall strips from model-output/deserialized ``properties``
#: BEFORE contract validation — every floor field this writer stamps itself,
#: plus ``key``/``id`` (the writer derives both from the assertion's display,
#: never trusts a caller-supplied identifier). A model that emits
#: ``properties={"acl": [...], "role": "admin"}`` (A-P1-3 smuggling attempt,
#: or an inert ``role:``/``acl:``-SHAPED business value — see §8 red-team test
#: 3) has every one of those keys stripped here; only contract-validated
#: business fields survive.
_PLATFORM_OWNED_KEYS: frozenset[str] = frozenset(
    {
        "acl",
        "assurance",
        "source_system",
        "source_record_id",
        "config_hash",
        "synced_at",
        "evidence",
        "key",
        "id",
    }
)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON (sorted keys) — local reimplementation, no private import.

    Mirrors ``ne.brain_graph.construct.mappers._canonical_json`` verbatim.
    """
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def validate_extracted_properties(
    entity_class: str, properties: Mapping[str, Any], llm_key: str
) -> dict[str, Any]:
    """The F1 write-side wall, shared by :mod:`.job` (pre-proposal validation)
    and :class:`ExtractionWriter` (pre-write re-validation — defense in depth
    against a deserialized/tampered payload, design §5.2).

    Strips every :data:`_PLATFORM_OWNED_KEYS` entry from ``properties``
    (a misbehaving model or a tampered stored payload can never smuggle a
    floor field through a property value), injects ``id=<llm_key>`` (every
    generated ``ENTITY_CONTRACTS`` member requires ``id``), and validates the
    result through ``ENTITY_CONTRACTS[entity_class]`` (``extra="forbid"``).

    Raises ``ValueError`` for an unknown ``entity_class`` and
    ``pydantic.ValidationError`` for a contract mismatch — the caller (job's
    validation wall, or this module's own writer methods) catches both and
    counts the assertion as invalid/discarded (F1: never a partial write).
    """
    contract = ENTITY_CONTRACTS.get(entity_class)
    if contract is None:
        raise ValueError(f"unknown entity_class {entity_class!r} (not in ENTITY_CONTRACTS)")
    sanitized = {k: v for k, v in properties.items() if k not in _PLATFORM_OWNED_KEYS}
    sanitized["id"] = llm_key
    model = contract.model_validate(sanitized)
    return model.model_dump(mode="json", exclude_none=True)


class ExtractionWriteStore(Protocol):
    """The store surface :class:`ExtractionWriter` needs — fetch + non-destructive
    upserts + the additive scoped purge (F5, ``purge_llm_extracted``)."""

    async def fetch_nodes(self, keys: Sequence[str]) -> Sequence[Mapping[str, Any]]: ...
    async def upsert_nodes(self, nodes: Sequence[Mapping[str, Any]]) -> None: ...
    async def upsert_edges(self, edges: Sequence[Mapping[str, Any]]) -> None: ...
    async def purge_llm_extracted(self, keys: Sequence[str] | None = None) -> dict[str, int]: ...


def _sha16(proposal_id: str) -> str:
    """The deterministic short content-address half of a ``sha256:<64hex>`` proposal id."""
    return proposal_id.removeprefix("sha256:")[:16]


class ExtractionWriter:
    """Composes EVERY §2.1 floor field itself — see the module docstring."""

    def __init__(
        self,
        store: ExtractionWriteStore,
        *,
        config_hash: str,
        schema_version: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._config_hash = config_hash
        self._schema_version = schema_version
        self._clock = clock

    async def write_entity(
        self,
        assertion: GraphExtractionAssertion,
        *,
        chunk_acl: Sequence[str],
        proposal_id: str,
        assurance: str,
    ) -> str:
        """Write (or re-evidence) one ``new_entity`` assertion. Returns the node key.

        The node ``key`` is RECOMPUTED here from ``(entity_class, display)`` via
        :func:`~ne.brain_graph.extraction.identity.llm_entity_key` — the writer
        never trusts ``assertion.entity_key`` for the actual write (it may be a
        deserialized/stale value); it is the job's candidate-resolution field,
        not this seam's identity source of truth.
        """
        if assurance not in ASSURANCE_VALUES:
            raise ValueError(
                f"assurance must be one of {sorted(ASSURANCE_VALUES)!r}, got {assurance!r}"
            )
        if not chunk_acl:
            raise GraphAclError(
                "extraction entity write refused: the evidencing chunk's ACL is empty "
                "(fail-closed; nothing written)"
            )

        llm_key = llm_entity_key(assertion.entity_class, assertion.display)
        validated = validate_extracted_properties(
            assertion.entity_class, assertion.properties, llm_key
        )
        content_hash = hashlib.sha256(_canonical_json(validated).encode()).hexdigest()
        sha16 = _sha16(proposal_id)
        evidence_ref = assertion.evidence_ref
        source_ref = f"llm:{sha16}:{evidence_ref}"
        ref_entry = f"{proposal_id}|{evidence_ref}"

        existing = await self._store.fetch_nodes([llm_key])
        if existing:
            current = dict(existing[0])
            evidence_list = [str(e) for e in (current.get("evidence") or [])]
            if ref_entry not in evidence_list:
                evidence_list.append(ref_entry)
            current["evidence"] = evidence_list
            current["synced_at"] = self._clock().isoformat()
            # Re-evidence NEVER touches acl, and NEVER downgrades assurance away
            # from 'confirmed' (design §4-2). ACL widening has no code path here.
            if current.get("assurance") != "confirmed":
                current["assurance"] = assurance
            await self._store.upsert_nodes([current])
            return llm_key

        node_props: dict[str, Any] = {
            **validated,
            "key": llm_key,
            "entity_class": assertion.entity_class,
            "display_name": assertion.display,
            "source_system": LLM_SOURCE_SYSTEM,
            "source_ref": source_ref,
            "acl": list(chunk_acl),
            "assurance": assurance,
            "evidence": [ref_entry],
            "content_hash": content_hash,
            "schema_version": self._schema_version,
            "config_hash": self._config_hash,
            "synced_at": self._clock().isoformat(),
        }
        await self._store.upsert_nodes([node_props])
        return llm_key

    async def write_edge(
        self,
        assertion: GraphExtractionAssertion,
        *,
        chunk_acl: Sequence[str],
        proposal_id: str,
        assurance: str,
        subject_key: str,
        subject_class: str,
        object_key: str,
        object_class: str,
    ) -> str:
        """Write one ``new_edge`` assertion. Returns the edge key.

        The edge key folds the proposal's deterministic ``sha16`` INTO
        ``source_ref`` — so a second chunk asserting the SAME (subject, kind,
        object) triple mints a DIFFERENT ``source_ref`` and therefore a
        DIFFERENT edge key: a SEPARATE element with its own ACL, never a union
        (design A-P0-1). There is deliberately no re-evidence/merge path for
        edges (unlike entities): re-applying the SAME proposal is naturally
        idempotent (identical edge key → property overwrite).
        """
        if assurance not in ASSURANCE_VALUES:
            raise ValueError(
                f"assurance must be one of {sorted(ASSURANCE_VALUES)!r}, got {assurance!r}"
            )
        if not chunk_acl:
            raise GraphAclError(
                "extraction edge write refused: the evidencing chunk's ACL is empty "
                "(fail-closed; nothing written)"
            )
        triple = (subject_class, assertion.edge_kind, object_class)
        if triple not in _EDGE_INDEX:
            raise ValueError(f"edge triple {triple!r} is not a valid EDGE_DEFS relation")

        sha16 = _sha16(proposal_id)
        evidence_ref = assertion.evidence_ref
        source_ref = f"llm:{sha16}:{evidence_ref}"
        edge_key = edge_key_for(assertion.edge_kind, subject_key, object_key, source_ref)
        edge_props: dict[str, Any] = {
            "key": edge_key,
            "edge_kind": assertion.edge_kind,
            "from_key": subject_key,
            "to_key": object_key,
            "source_system": LLM_SOURCE_SYSTEM,
            "source_ref": source_ref,
            "acl": list(chunk_acl),
            "assurance": assurance,
            "evidence": [f"{proposal_id}|{evidence_ref}"],
            "config_hash": self._config_hash,
            "synced_at": self._clock().isoformat(),
        }
        await self._store.upsert_edges([edge_props])
        return edge_key
