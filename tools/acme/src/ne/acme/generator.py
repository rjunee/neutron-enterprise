# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The Acme Corp corpus generator: threads one seeded :class:`DetRandom`
through org -> crm -> documents -> tickets (in that FIXED order — reordering
changes every downstream byte) and serializes the result to six canonical-JSON
artifacts.

Determinism contract: same seed -> byte-identical artifacts. This relies on
(a) :class:`~ne.acme.rand.DetRandom` using only ``Random.random()``, (b) a
fixed ``BASE_TIME`` (never ``datetime.now()``), (c) this fixed generation
order, and (d) canonical JSON serialization (sorted keys, fixed indent, ASCII,
trailing newline, UTF-8) with no set iteration anywhere in the pipeline.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ne.acme.crm import build_crm
from ne.acme.documents import build_documents
from ne.acme.models import AclEntry, CorpusManifest, GroundTruth, OrgChart
from ne.acme.orgchart import build_org
from ne.acme.pools import CORPUS_VERSION, DEFAULT_SEED
from ne.acme.rand import DetRandom
from ne.acme.tickets import build_tickets
from ne.schema import OperationalRecordSet

GENERATOR_NAME = "ne-acme 0.1.0"

# Fixed artifact names, in a fixed order — the CI staleness gate (`check`)
# compares exactly these files.
ARTIFACTS: tuple[str, ...] = (
    "records.json",
    "content.json",
    "org.json",
    "acl.json",
    "ground_truth.json",
    "manifest.json",
)


@dataclass
class AcmeCorpus:
    """The full in-memory Acme Corp synthetic corpus."""

    record_set: OperationalRecordSet
    content: dict[str, str]
    org: OrgChart
    acls: list[AclEntry]
    ground_truth: GroundTruth


def generate(seed: int = DEFAULT_SEED) -> AcmeCorpus:
    """Generate the full Acme corpus deterministically from ``seed``."""
    rng = DetRandom(seed)

    # FIXED order: org -> crm -> documents -> tickets. Never reorder — doing so
    # changes every subsequent `.random()` draw and therefore every output byte.
    people, org = build_org(rng)
    accounts, orders = build_crm(rng, people, org)
    documents, content, acls, ground_truth = build_documents(rng, people, org, accounts)
    tickets = build_tickets(rng, people, org, accounts)

    record_set = OperationalRecordSet(
        accounts=accounts,
        people=people,
        documents=documents,
        tickets=tickets,
        orders=orders,
    )
    return AcmeCorpus(
        record_set=record_set,
        content=content,
        org=org,
        acls=acls,
        ground_truth=ground_truth,
    )


def _canonical_json(obj: object) -> bytes:
    """Canonical JSON: sorted keys, fixed indent, ASCII-only, trailing newline."""
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def serialize(corpus: AcmeCorpus, seed: int) -> dict[str, bytes]:
    """Serialize ``corpus`` to the six canonical-JSON artifacts (name -> bytes)."""
    records_bytes = _canonical_json(corpus.record_set.model_dump(mode="json", exclude_none=True))
    content_bytes = _canonical_json(corpus.content)
    org_bytes = _canonical_json(corpus.org.model_dump(mode="json"))
    acl_bytes = _canonical_json([entry.model_dump(mode="json") for entry in corpus.acls])
    ground_truth_bytes = _canonical_json(corpus.ground_truth.model_dump(mode="json"))

    counts = {
        "people": len(corpus.record_set.people or []),
        "accounts": len(corpus.record_set.accounts or []),
        "orders": len(corpus.record_set.orders or []),
        "documents": len(corpus.record_set.documents or []),
        "tickets": len(corpus.record_set.tickets or []),
    }
    sha256 = {
        "records.json": hashlib.sha256(records_bytes).hexdigest(),
        "content.json": hashlib.sha256(content_bytes).hexdigest(),
        "org.json": hashlib.sha256(org_bytes).hexdigest(),
        "acl.json": hashlib.sha256(acl_bytes).hexdigest(),
        "ground_truth.json": hashlib.sha256(ground_truth_bytes).hexdigest(),
    }
    manifest = CorpusManifest(
        corpus_version=CORPUS_VERSION,
        seed=seed,
        generator=GENERATOR_NAME,
        counts=counts,
        sha256=sha256,
    )
    manifest_bytes = _canonical_json(manifest.model_dump(mode="json"))

    return {
        "records.json": records_bytes,
        "content.json": content_bytes,
        "org.json": org_bytes,
        "acl.json": acl_bytes,
        "ground_truth.json": ground_truth_bytes,
        "manifest.json": manifest_bytes,
    }


def write(corpus: AcmeCorpus, seed: int, out_dir: Path) -> None:
    """Write all six artifacts for ``corpus`` into ``out_dir`` (created if needed)."""
    artifacts = serialize(corpus, seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ARTIFACTS:
        (out_dir / name).write_bytes(artifacts[name])


def check(fixtures_dir: Path, seed: int = DEFAULT_SEED) -> list[str]:
    """Return artifact names that differ from a fresh regeneration ([] = clean)."""
    fresh = serialize(generate(seed), seed)
    drift: list[str] = []
    for name in ARTIFACTS:
        committed_path = fixtures_dir / name
        if not committed_path.is_file():
            drift.append(name)
            continue
        if committed_path.read_bytes() != fresh[name]:
            drift.append(name)
    return drift
