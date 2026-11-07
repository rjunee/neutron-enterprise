# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The connector-sync health derivation layer for GET /v1/connect/status (NE-132).

Mirrors :mod:`ne.api.posture`: a set of pure, honest, read-only derivations over
the REAL connector-sync state — never fabricated (§0 law 5). Three real sources:

* the connector state sqlite (``ne.connect.state.ConnectorStateStore`` — the
  ``cursors`` table's ``updated_at``) → per-connector cursor freshness;
* the write-quarantine sqlite (``ne.brain.write.SqliteWriteQuarantine`` — the
  ``write_quarantine`` table) → per-connector quarantine counts + the total +
  the ALLOWLISTED reason-category ledger (fixed labels only — never raw text);
* the append-only audit store (``ne.audit.store.ne_audit_log`` — the latest
  ``connector.sync`` / ``cdc.ingest`` row per connector) → last-checked age (when
  the sync cycle last RAN, from the event's ``occurred_at``) + last-sync audit
  seq + the fail-closed ACL-mirror health.

TWO DISTINCT FRESHNESS FACTS (SPEC §12 item 9): ``last_checked_age_s`` is the age
of the latest sync RUN — it advances on every AUDITED cycle, found-something or
not (a cycle that crashes before its ``connector.sync`` event advances nothing —
the producer gap named in the PR) — while ``cursor_age_s`` is the age of the
newest RECORD accepted. On a pull lane that is the cursor row's last write; on
the CDC push lane (which has no cursor row) it is the newest ``cdc.ingest`` batch
whose counts detail says it ACCEPTED a record (``written > 0`` — see
:func:`latest_accepting_cdc_ingest`), because the sink emits its success summary
for empty/heartbeat/all-quarantined batches too. A connector polling every
fifteen minutes against a quiet source has a small ``last_checked_age_s`` and a
large ``cursor_age_s``; collapsing them is the defect this module must not repeat
— on ANY lane.

ACCESS DECISION (documented): ANY authenticated principal for the STATUS surface.
That payload carries DEPLOYMENT-LEVEL connector-health facts ONLY — cursor ages, an
ok/degraded health enum, an audit seq, and coarse quarantine COUNTS. It carries NO
user data, NO ACL labels/principals, NO chunk content, and NO raw quarantine reason
text (reasons are mapped to an allowlisted fixed-label category, never a fragment of
stored text). This mirrors the :mod:`ne.api.posture` /
``kpi.py`` precedent (aggregates carry no user data). Read-only: no tool execution,
no write path.

The DRILL-DOWN surface (:func:`derive_quarantine_items`, NE-CONNECT-CSV) is
DIFFERENT and gated accordingly: a parked write is CUSTOMER DATA that failed
validation, so the full rows — raw validator reason, record identity, the parked
payload (string values bounded) — are served ONLY to ``ApiSettings.ops_roles``
principals via GET /v1/connect/quarantine (403 + audited otherwise; the audit-log
route's access model). A count with no drill-down is a control that demos as
decoration; the drill-down is the inspectability the quarantine lane exists for.

HONESTY CONTRACT: only sources listed in ``NE_CONNECT_SYNC_SOURCES`` + the CDC
lane appear. A listed source whose executable connector settings are absent stays
readable under its public identity with all-null health; it does not gain a Sync
capability. A configured-but-never-synced connector likewise carries all-null
health fields. An absent/unreadable/corrupt state or quarantine file degrades to
honest ``None`` (never a fake fresh age, a default ``'ok'``, or a zero count).

CROSS-PROCESS SQLITE SEAM: the cursor + quarantine files are written by ne-worker /
CLI processes, not ne-api. Each is opened READ-ONLY (``mode=ro`` URI) per request
inside ONE threadpool hop; a mid-write read sees only committed rows (sqlite atomic
commits), and an unreadable file degrades to ``None`` — never a 500 or a lie.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.connect.runner import read_only_source_name

if TYPE_CHECKING:
    from ne.api.settings import ApiSettings
    from ne.connect.runner import ConnectSyncSettings

_LOG = logging.getLogger(__name__)

_AclMirror = Literal["ok", "degraded"]
_ConnectorPosture = Literal["live", "snapshot"]

#: Cap on the reason-category ledger returned on the wire (design §4.3 vessel rows).
_MAX_REASONS = 12

#: The generic bucket for any quarantine reason whose signature we don't recognise.
_REASON_OTHER = "other"

#: Raw quarantine reasons whose FULL text is a known, safe, fixed constant — these
#: map to themselves. Every other reason maps to a fixed label by prefix signature
#: (see :func:`_reason_category`) or collapses to ``_REASON_OTHER``.
_REASON_EXACT: frozenset[str] = frozenset({"unknown-table", "missing-key-column", "cdc-delete"})


def _reason_category(reason: str | None) -> str:
    """Map a RAW stored quarantine reason to an ALLOWLISTED, non-leaking category.

    The raw reason text is NEVER echoed to the wire. Real writer reasons embed
    schema-validator output assembled as ``f"{json_path}: {message}"`` (see
    ``ne.brain.write.writer._validate_payload``), which can carry customer field
    names and offending values; a delete/CDC-ACL reason likewise carries free text.
    Deriving the wire category from a substring of that text (e.g. the first
    colon-segment) would leak it to ANY authenticated caller (C7). Instead each
    KNOWN reason signature maps to a FIXED category label drawn from a closed
    allowlist, and every unrecognised reason collapses to ``"other"`` — the wire
    value is always a constant, never a fragment of stored text.
    """
    if reason is None:
        return _REASON_OTHER
    text = reason.strip()
    if text.startswith("$"):  # a jsonschema json_path — writer shape-validation reject
        return "schema-validation"
    if text.startswith("unsupported-op"):
        return "unsupported-op"
    if text.startswith("duplicate-identity"):  # bridge fail-closed same-run duplicate park
        return "duplicate-identity"
    if text.startswith("cdc-acl") or text.startswith("acl-"):
        return "acl-unresolvable"
    if text.startswith("unwound"):  # writer audit-append unwind (writer.py)
        return "audit-unwound"
    if text in _REASON_EXACT:
        return text
    return _REASON_OTHER


@dataclass(frozen=True)
class ConnectorStatus:
    """One derived connector lane — the pure shape the router maps onto the wire."""

    id: str
    kind: str
    posture: _ConnectorPosture
    cursor_age_s: float | None
    last_checked_age_s: float | None
    acl_mirror: _AclMirror | None
    last_sync_seq: int | None
    quarantined_count: int | None


@dataclass(frozen=True)
class ConnectStatus:
    """The full derived connect-status surface (pure — no wire/pydantic here)."""

    connectors: list[ConnectorStatus]
    quarantine_total: int | None
    quarantine_reasons: list[tuple[str, int]]


@dataclass(frozen=True)
class _QuarantineStats:
    """One quarantine store read once: total rows + per-provenance + per-category."""

    total: int
    by_provenance: dict[str, int]
    by_reason: dict[str, int]

    def count_with_prefix(self, prefix: str) -> int:
        """Rows whose provenance starts with ``prefix`` (per-connector attribution)."""
        return sum(n for prov, n in self.by_provenance.items() if prov.startswith(prefix))


@dataclass(frozen=True)
class QuarantineItem:
    """One parked write, fully inspectable (the OPS-GATED drill-down shape).

    ``source``/``record_id`` are parsed from the park's provenance
    (``connector:<source>:<record_id>`` / ``cdc:...``); an unrecognised
    provenance keeps both ``None`` rather than guessing. ``payload`` is the
    parked row itself with long string values bounded (``payload_truncated``
    says so honestly).
    """

    item_id: str
    source: str | None
    record_id: str | None
    reason: str
    category: str
    shape: str
    created_at: str
    payload: dict[str, object]
    payload_truncated: bool


@dataclass(frozen=True)
class QuarantineItems:
    """The drill-down surface: newest-first parked writes across readable stores.

    ``total`` mirrors the status surface's honesty contract: ``None`` when NO
    store was readable (never a fake zero); otherwise the FULL parked count,
    of which up to ``limit`` items are carried.
    """

    items: list[QuarantineItem]
    total: int | None


#: Bound on each string value carried in a drill-down payload (C12 scope: the
#: operator sees the offending row, not an unbounded document body).
_MAX_PAYLOAD_VALUE_CHARS = 2000


def _bound_value(value: object) -> tuple[object, bool]:
    """Bound every string ANYWHERE in a payload value (nested dicts/lists too).

    CSV park payloads are flat, but this surface serves ALL parks — writer
    shape-parks and CDC payloads nest — so a top-level-only bound would pass a
    nested document body through unbounded while reporting
    ``payload_truncated: false``. Returns ``(bounded, truncated)``.
    """
    if isinstance(value, str):
        if len(value) > _MAX_PAYLOAD_VALUE_CHARS:
            return value[:_MAX_PAYLOAD_VALUE_CHARS], True
        return value, False
    if isinstance(value, Mapping):
        truncated = False
        bounded: dict[str, object] = {}
        for key, child in value.items():
            bounded_child, child_truncated = _bound_value(child)
            bounded[key] = bounded_child
            truncated = truncated or child_truncated
        return bounded, truncated
    if isinstance(value, (list, tuple)):
        items: list[object] = []
        truncated = False
        for child in value:
            bounded_child, child_truncated = _bound_value(child)
            items.append(bounded_child)
            truncated = truncated or child_truncated
        return items, truncated
    return value, False


def _bounded_payload(payload: Mapping[str, object]) -> tuple[dict[str, object], bool]:
    """Bound long string values at every depth; report whether anything was truncated."""
    bounded, truncated = _bound_value(dict(payload))
    assert isinstance(bounded, dict)  # a Mapping in, a dict out
    return bounded, truncated


def _parse_provenance(provenance: str) -> tuple[str | None, str | None]:
    """``connector:<source>:<record_id>`` -> (source, record_id); else honest Nones."""
    parts = provenance.split(":", 2)
    if len(parts) == 3 and parts[0] == "connector" and parts[1]:
        return parts[1], parts[2]
    if parts[0] == "cdc" and len(parts) > 1:
        return "cdc", parts[-1]
    return None, None


def quarantine_items(path: str) -> list[QuarantineItem] | None:
    """Read one write-quarantine sqlite's FULL rows (the drill-down read).

    ``None`` when the path is empty/absent/unreadable (honestly unknown — the
    same degradation contract as :func:`quarantine_stats`). Opened READ-ONLY;
    the connection is always closed. A row whose ``payload`` JSON is corrupt
    still surfaces (empty payload) — the reason/identity are the load-bearing
    facts and hiding the row would under-report the quarantine.
    """
    if not path.strip() or not Path(path).exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT item_id, reason, payload, provenance, shape, created_at FROM write_quarantine"
        ).fetchall()
    except sqlite3.Error:
        _LOG.warning("connect_status: quarantine store unreadable at %s", path, exc_info=True)
        return None
    finally:
        if conn is not None:
            conn.close()
    items: list[QuarantineItem] = []
    for item_id, reason, payload_json, provenance, shape, created_at in rows:
        try:
            raw_payload = json.loads(payload_json) if payload_json else {}
        except (TypeError, ValueError):
            raw_payload = {}
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        payload, truncated = _bounded_payload(raw_payload)
        reason_text = "" if reason is None else str(reason)
        source, record_id = _parse_provenance(str(provenance) if provenance else "")
        items.append(
            QuarantineItem(
                item_id=str(item_id),
                source=source,
                record_id=record_id,
                reason=reason_text,
                category=_reason_category(reason_text),
                shape=str(shape) if shape else "",
                created_at=str(created_at) if created_at else "",
                payload=payload,
                payload_truncated=truncated,
            )
        )
    return items


def derive_quarantine_items(
    connect_sync_settings: ConnectSyncSettings | None,
    api_settings: ApiSettings,
    *,
    limit: int,
) -> QuarantineItems:
    """Merge the readable quarantine stores' rows, newest first, capped at ``limit``."""
    stores: list[list[QuarantineItem]] = []
    if connect_sync_settings is not None:
        pull = quarantine_items(connect_sync_settings.quarantine_path)
        if pull is not None:
            stores.append(pull)
    if _cdc_source_name(api_settings) is not None:
        cdc = quarantine_items(api_settings.cdc_quarantine_path)
        if cdc is not None:
            stores.append(cdc)
    if not stores:
        return QuarantineItems(items=[], total=None)
    merged = [item for store in stores for item in store]
    merged.sort(key=lambda item: (item.created_at, item.item_id), reverse=True)
    return QuarantineItems(items=merged[:limit], total=len(merged))


def _to_utc(value: datetime) -> datetime:
    """Normalize a possibly-naive datetime to tz-aware UTC (SQLite reads naive).

    Copied from ``ne.api.posture._to_utc`` — the audit store's ``occurred_at`` is
    ``DateTime(timezone=True)`` but SQLite returns naive datetimes on read.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _age_seconds(ts: datetime, *, now: datetime) -> float:
    """Non-negative age in seconds of ``ts`` relative to ``now`` (clock-skew safe)."""
    return max(0.0, (now - _to_utc(ts)).total_seconds())


def cursor_ages(state_path: str) -> dict[str, float]:
    """Return ``{source: cursor_age_s}`` from the REAL connector-state sqlite.

    Reads the ``cursors`` table's newest ``updated_at`` per ``source`` (the resumable
    incremental cursor's last write) and ages it against ``now``. An empty/missing
    path or an unreadable/corrupt file returns ``{}`` (honestly unknown — NEVER a
    fabricated fresh age). Opened READ-ONLY; the connection is always closed.
    """
    if not state_path.strip() or not Path(state_path).exists():
        return {}
    now = datetime.now(UTC)
    ages: dict[str, float] = {}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{Path(state_path)}?mode=ro", uri=True)
        query = "SELECT source, MAX(updated_at) FROM cursors GROUP BY source"
        rows = conn.execute(query).fetchall()
        for source, updated_at in rows:
            if source is None or updated_at is None:
                continue
            try:
                ts = datetime.fromisoformat(str(updated_at))
            except ValueError:
                continue  # an unparseable cursor timestamp ⇒ honestly unknown
            ages[str(source)] = _age_seconds(ts, now=now)
    except sqlite3.Error:
        # An unreadable / mid-migration / corrupt state file ⇒ honestly unknown,
        # never fake-fresh. The route degrades to null ages, never a 500.
        _LOG.warning("connect_status: cursor state unreadable at %s", state_path, exc_info=True)
        return {}
    finally:
        if conn is not None:
            conn.close()
    return ages


def quarantine_stats(path: str) -> _QuarantineStats | None:
    """Read one write-quarantine sqlite ONCE → total + provenance/category counts.

    Returns ``None`` when the path is empty, the file is absent, or the store is
    unreadable (honestly unknown — never a fake zero total). Otherwise returns the
    total parked-write count, a per-provenance count map (for per-connector prefix
    attribution) and a per-reason-CATEGORY count map. The reason category is an
    ALLOWLISTED label derived by :func:`_reason_category` — the raw reason text
    (which can embed schema-validator JSON paths + offending customer values) is
    NEVER echoed to the wire; unrecognised reasons collapse to ``"other"`` (C7).
    Opened READ-ONLY; the connection is always closed.
    """
    if not path.strip() or not Path(path).exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
        rows = conn.execute("SELECT provenance, reason FROM write_quarantine").fetchall()
    except sqlite3.Error:
        _LOG.warning("connect_status: quarantine store unreadable at %s", path, exc_info=True)
        return None
    finally:
        if conn is not None:
            conn.close()
    by_provenance: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for provenance, reason in rows:
        prov = str(provenance) if provenance is not None else ""
        by_provenance[prov] = by_provenance.get(prov, 0) + 1
        category = _reason_category(None if reason is None else str(reason))
        by_reason[category] = by_reason.get(category, 0) + 1
    return _QuarantineStats(total=len(rows), by_provenance=by_provenance, by_reason=by_reason)


def last_sync_events(
    audit_engine: sa.Engine, resources: Sequence[str]
) -> dict[str, tuple[int, str, datetime]]:
    """Return ``{resource: (seq, outcome, occurred_at)}`` for the LATEST sync event.

    ONE bounded query over the exact action/resource pairs: ``connector.sync`` may
    drive only ``connector://`` resources and ``cdc.ingest`` only ``cdc://``.  The
    conjunctive match prevents a foreign/colliding action on the same resource from
    changing either the reported ACL-mirror state or read-time enforcement.
    """
    pairs = [
        (resource, action)
        for resource in dict.fromkeys(resources)
        if (action := _sync_action_for_resource(resource)) is not None
    ]
    if not pairs:
        return {}
    stmt = (
        sa.select(
            ne_audit_log.c.resource,
            ne_audit_log.c.seq,
            ne_audit_log.c.outcome,
            ne_audit_log.c.occurred_at,
        )
        .where(
            sa.or_(
                *(
                    sa.and_(
                        ne_audit_log.c.resource == resource,
                        ne_audit_log.c.action == action,
                    )
                    for resource, action in pairs
                )
            )
        )
        .order_by(ne_audit_log.c.seq.desc())
    )
    latest: dict[str, tuple[int, str, datetime]] = {}
    with audit_engine.connect() as conn:
        for resource, seq, outcome, occurred_at in conn.execute(stmt):
            if resource in latest:
                continue  # DESC order ⇒ the first row per resource is the latest
            latest[str(resource)] = (int(seq), str(outcome), occurred_at)
    return latest


def _sync_action_for_resource(resource: str) -> str | None:
    if resource.startswith("connector://"):
        return "connector.sync"
    if resource.startswith("cdc://"):
        return "cdc.ingest"
    return None


def _connector_stamp_for_resource(resource: str) -> str | None:
    if resource.startswith("connector://"):
        stamp = resource.removeprefix("connector://")
    elif resource.startswith("cdc://"):
        source = resource.removeprefix("cdc://")
        stamp = f"cdc:{source}" if source else ""
    else:
        return None
    return stamp or None


#: How many consecutive non-success sync facts (since the last success) a lane
#: is granted before the READ path withholds its corpus. Below the limit a
#: plain-``error`` run is a FRESHNESS fact — the corpus on disk is exactly what
#: the last verified sync admitted, so it is served (and the render layer states
#: the staleness; see ``ne.brain.withholding``). At the limit the ACL mirror
#: itself has been unverifiable for that many attempts and fail-closed becomes
#: the honest response again. The limit counts ATTEMPTS, not wall-clock, so it
#: scales with whatever cadence the deployment chose: a lane syncing every two
#: minutes fails closed after ~six minutes of outage, an hourly lane after three
#: hours — in both cases three times the ACL-staleness window the deployment
#: already accepted by picking that cadence. Measured on the demo box
#: (2026-08-28 02:22:22, `error_class=GraphTransportError`, one TCP reset from
#: real Microsoft Graph between two successes): under the old latest-fact-only
#: rule that single blip withheld all 68 m365 chunks for 3.5 minutes and an S1
#: run reported the eligibility policy "not retrievable due to a source outage".
TRANSIENT_SYNC_FAILURE_LIMIT = 3


@dataclass(frozen=True, slots=True)
class SyncSourceReadHealth:
    """The read path's split of unhealthy sync lanes, as connector STAMPS.

    ``unavailable`` is withheld in full by the read paths (T1 chunks and T2
    graph rows). ``stale`` is SERVED — the corpus is the last verified sync's
    admission — and the retriever counts served-stale chunks into the ambient
    withholding observation so the rendered result can state the caveat.
    The two sets are disjoint; a healthy or never-synced lane is in neither.
    """

    unavailable: frozenset[str]
    stale: frozenset[str]


@dataclass(slots=True)
class _LaneScan:
    """Newest-first accumulator for one lane's facts since its last success."""

    latest_outcome: str | None = None
    saw_success: bool = False
    failures_since_success: int = 0
    denied_since_success: bool = False


def sync_source_read_health(
    audit_engine: sa.Engine,
    resources: Sequence[str],
    *,
    consecutive_failure_limit: int = TRANSIENT_SYNC_FAILURE_LIMIT,
) -> SyncSourceReadHealth:
    """Classify each lane's read handling from its sync facts since last success.

    ONE newest-first scan over the lanes' sync events. A lane whose LATEST fact
    is ``success`` (or which has no facts at all) is healthy and appears in
    neither set. A lane whose latest fact is non-success is:

    * ``unavailable`` — withheld, the fail-closed leg — when ANY fact since the
      last success is ``denied`` (AclResolutionError: the ACL mirror could not
      be resolved, so serving would disclose under authority we cannot verify),
      OR the lane has never synced successfully (there is no verified admission
      to serve), OR the failures have persisted for
      ``consecutive_failure_limit`` attempts (the mirror is unverifiably stale);
    * ``stale`` — served, with the staleness carried to the renderer — for a
      shorter run of plain ``error`` facts. A transient transport blip is a
      freshness risk, not an availability fact: nothing about the DATA became
      unavailable, and withholding it silently blinds the agent (the 2026-08-28
      m365 incident this split exists to end).

    The honest property is unchanged where it matters: a genuine ACL/authority
    failure withholds on the FIRST fact, with no grace.
    """
    pairs = [
        (resource, action)
        for resource in dict.fromkeys(resources)
        if (action := _sync_action_for_resource(resource)) is not None
    ]
    if not pairs:
        return SyncSourceReadHealth(unavailable=frozenset(), stale=frozenset())
    stmt = (
        sa.select(ne_audit_log.c.resource, ne_audit_log.c.outcome)
        .where(
            sa.or_(
                *(
                    sa.and_(
                        ne_audit_log.c.resource == resource,
                        ne_audit_log.c.action == action,
                    )
                    for resource, action in pairs
                )
            )
        )
        .order_by(ne_audit_log.c.seq.desc())
    )
    lanes: dict[str, _LaneScan] = {}
    open_lanes = {resource for resource, _action in pairs}
    with audit_engine.connect() as conn:
        for resource, outcome in conn.execute(stmt):
            resource = str(resource)
            scan = lanes.setdefault(resource, _LaneScan())
            if scan.saw_success:
                continue
            outcome = str(outcome)
            if scan.latest_outcome is None:
                scan.latest_outcome = outcome
            if outcome == "success":
                scan.saw_success = True
                open_lanes.discard(resource)
                if not open_lanes:
                    break
                continue
            scan.failures_since_success += 1
            if outcome == "denied":
                scan.denied_since_success = True

    unavailable: set[str] = set()
    stale: set[str] = set()
    for resource, scan in lanes.items():
        stamp = _connector_stamp_for_resource(resource)
        if stamp is None or scan.latest_outcome in (None, "success"):
            continue
        if (
            scan.denied_since_success
            or not scan.saw_success
            or scan.failures_since_success >= consecutive_failure_limit
        ):
            unavailable.add(stamp)
        else:
            stale.add(stamp)
    return SyncSourceReadHealth(unavailable=frozenset(unavailable), stale=frozenset(stale))


def degraded_sync_sources(audit_engine: sa.Engine, resources: Sequence[str]) -> frozenset[str]:
    """Return connector metadata stamps the read path must WITHHOLD outright.

    This is the enforcement twin of :func:`_acl_mirror_of` — but the two are no
    longer the same predicate. The STATUS surface stays latest-fact-honest: one
    failed cycle renders ``acl_mirror='degraded'`` immediately, because that is
    a true statement about the last run. The READ path applies
    :func:`sync_source_read_health` instead, because "the last sync attempt
    failed" and "this corpus may not be served" are different facts: a corpus
    behind a transient error is stale, not absent. A never-synced lane remains
    the status surface's honest ``None`` state and is not silently reclassified
    by the read path.
    """
    return sync_source_read_health(audit_engine, resources).unavailable


def latest_accepting_cdc_ingest(audit_engine: sa.Engine, resource: str) -> datetime | None:
    """``occurred_at`` of the newest ``cdc.ingest`` batch that ACCEPTED a record.

    The CDC push lane has no cursor row, so its newest-record fact is derived
    from the ingest ledger itself: the sink emits ONE counts-only ``cdc.ingest``
    success event per ``apply`` — including for a batch that accepted NOTHING
    (a Debezium heartbeat / empty body, a tombstone-only or all-quarantined
    batch) — so the latest event's age is a LAST-CHECKED fact, never a
    newest-record fact. A batch advances the newest-record fact only when its
    detail (``ne.connect.cdc.ingest_detail``, parsed by the strict inverse
    ``written_from_ingest_detail``) says ``written > 0``. An event whose detail
    does not parse as the sink's counts shape (foreign / hand-emitted rows)
    counts as NOT accepting — fail toward honest absence, never fresh (§0 law 5).
    Newest-first scan over this resource's ``cdc.ingest`` rows, stopping at the
    first accepting batch.
    """
    from ne.connect.cdc import written_from_ingest_detail  # LOCAL: cdc off the eager graph

    stmt = (
        sa.select(ne_audit_log.c.occurred_at, ne_audit_log.c.payload)
        .where(
            ne_audit_log.c.action == "cdc.ingest",
            ne_audit_log.c.resource == resource,
        )
        .order_by(ne_audit_log.c.seq.desc())
    )
    with audit_engine.connect() as conn:
        for occurred_at, payload in conn.execute(stmt):
            detail = payload.get("detail") if isinstance(payload, dict) else None
            written = written_from_ingest_detail(detail if isinstance(detail, str) else None)
            if written is not None and written > 0:
                return occurred_at
    return None


def _acl_mirror_of(outcome: str) -> _AclMirror:
    """Map a sync-event outcome to the fail-closed ACL-mirror enum.

    ``success`` → ``'ok'`` (the ACL mirror was verified); anything else (``denied``
    from the AclResolutionError path, ``error``) → ``'degraded'`` (fail-closed).
    """
    return "ok" if outcome == "success" else "degraded"


def _cdc_source_name(api_settings: ApiSettings) -> str | None:
    """The configured CDC source_name, or ``None`` when CDC is not enabled.

    CDC is enabled iff a webhook secret AND a non-empty table map are configured.
    ``CdcSinkConfig.from_json`` is re-parsed here (LOCAL import — the platform.py
    dlt-confinement precedent); a malformed map that platform boot would have
    rejected degrades to the default ``'debezium'`` source_name rather than a 500 —
    the lane still renders honestly (all-null health), never fabricated-green.
    """
    secret = api_settings.cdc_webhook_secret.get_secret_value()
    if not secret or not api_settings.cdc_tables.strip():
        return None
    from ne.connect.cdc import CdcSinkConfig  # LOCAL: keep dlt/cdc off ne-api eager graph

    try:
        return CdcSinkConfig.from_json(api_settings.cdc_tables).source_name
    except ValueError:
        _LOG.warning(
            "connect_status: cdc_tables unparseable; using default source_name 'debezium'",
            exc_info=True,
        )
        return "debezium"


def configured_sync_resources(
    connect_sync_settings: ConnectSyncSettings | None,
    api_settings: ApiSettings,
) -> tuple[str, ...]:
    """Return the exact bounded audit resources represented by `/connect/status`."""
    pull_sources = () if connect_sync_settings is None else connect_sync_settings.sources
    resources = [f"connector://{read_only_source_name(source)}" for source in pull_sources]
    cdc_source = _cdc_source_name(api_settings)
    if cdc_source is not None:
        resources.append(f"cdc://{cdc_source}")
    return tuple(resources)


def derive_connect_status(
    connect_sync_settings: ConnectSyncSettings | None,
    api_settings: ApiSettings,
    audit_engine: sa.Engine,
) -> ConnectStatus:
    """Derive the full connect-status surface from the REAL connector-sync state.

    The ONE synchronous function the router runs in a threadpool hop. It reads each
    quarantine store ONCE, the cursor state ONCE, and the audit log ONCE for every
    lane's latest sync event — plus, when the CDC lane has ingested at all, one
    newest-first scan for its latest ACCEPTING batch
    (:func:`latest_accepting_cdc_ingest`). It assembles one lane per configured
    pull source (in ``sources`` order) + one CDC lane iff CDC is enabled.
    Everything without a readable source is ``None``/absent.
    """
    now = datetime.now(UTC)

    # (1) Pull sources + their shared cursor + quarantine stores (read once each).
    sources: tuple[str, ...] = ()
    # An ABSENT config is not a live claim: with no settings there are no sources,
    # so the runnable set is empty and every (nonexistent) lane would read snapshot.
    runnable: frozenset[str] = frozenset()
    if connect_sync_settings is not None:
        sources = connect_sync_settings.sources
        runnable = frozenset(connect_sync_settings.sources_to_run)
    pull_ages = (
        cursor_ages(connect_sync_settings.state_path) if connect_sync_settings is not None else {}
    )
    pull_q = (
        quarantine_stats(connect_sync_settings.quarantine_path)
        if connect_sync_settings is not None
        else None
    )

    # (2) The optional CDC push lane + its own quarantine store (read once).
    cdc_source = _cdc_source_name(api_settings)
    cdc_q = quarantine_stats(api_settings.cdc_quarantine_path) if cdc_source is not None else None

    # (3) ONE audit query for every lane's latest sync event.
    cdc_resource = f"cdc://{cdc_source}" if cdc_source is not None else None
    resources = configured_sync_resources(connect_sync_settings, api_settings)
    latest = last_sync_events(audit_engine, resources)

    connectors: list[ConnectorStatus] = []
    for source in sources:
        runtime_source = read_only_source_name(source)
        seq, outcome = None, None
        event = latest.get(f"connector://{runtime_source}")
        acl_mirror: _AclMirror | None = None
        last_checked: float | None = None
        if event is not None:
            seq, outcome, occurred = event
            acl_mirror = _acl_mirror_of(outcome)
            # When the sync cycle last RAN — the event's occurred_at, NOT the
            # cursor write (which only advances when a record is accepted).
            last_checked = _age_seconds(occurred, now=now)
        connectors.append(
            ConnectorStatus(
                id=source,
                kind=source,
                posture="live" if source in runnable else "snapshot",
                cursor_age_s=pull_ages.get(runtime_source),
                last_checked_age_s=last_checked,
                acl_mirror=acl_mirror,
                last_sync_seq=seq,
                quarantined_count=(
                    None
                    if pull_q is None
                    else pull_q.count_with_prefix(f"connector:{runtime_source}:")
                ),
            )
        )

    if cdc_source is not None and cdc_resource is not None:
        event = latest.get(cdc_resource)
        cdc_seq: int | None = None
        cdc_mirror: _AclMirror | None = None
        cdc_checked: float | None = None
        cdc_record_age: float | None = None
        if event is not None:
            cdc_seq, cdc_outcome, cdc_occurred = event
            cdc_mirror = _acl_mirror_of(cdc_outcome)
            # When the lane last PROCESSED a batch — ANY batch: the sink emits
            # its cdc.ingest summary for empty/heartbeat/all-quarantined bodies
            # too, and those runs happened.
            cdc_checked = _age_seconds(cdc_occurred, now=now)
            # The newest-record fact must come from a batch that ACCEPTED a
            # record: an empty batch's ingest event must never render "Newest
            # record: moments ago" on a lane that may hold no records at all.
            accepted = latest_accepting_cdc_ingest(audit_engine, cdc_resource)
            if accepted is not None:
                cdc_record_age = _age_seconds(accepted, now=now)
        connectors.append(
            ConnectorStatus(
                id=cdc_source,
                kind="cdc",
                posture="live",
                cursor_age_s=cdc_record_age,
                last_checked_age_s=cdc_checked,
                acl_mirror=cdc_mirror,
                last_sync_seq=cdc_seq,
                quarantined_count=(None if cdc_q is None else cdc_q.count_with_prefix("cdc:")),
            )
        )

    # (4) Quarantine total + merged reason-category ledger over the READABLE stores.
    readable = [q for q in (pull_q, cdc_q) if q is not None]
    quarantine_total = None if not readable else sum(q.total for q in readable)
    merged_reasons: dict[str, int] = {}
    for q in readable:
        for category, count in q.by_reason.items():
            merged_reasons[category] = merged_reasons.get(category, 0) + count
    ranked = sorted(merged_reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    quarantine_reasons = ranked[:_MAX_REASONS]

    return ConnectStatus(
        connectors=connectors,
        quarantine_total=quarantine_total,
        quarantine_reasons=quarantine_reasons,
    )
