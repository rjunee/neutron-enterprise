# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SqlAlchemySessionStore — principal-owned conversations plus quarantined legacy sessions.

Follows the ne-audit store style: sync SQLAlchemy-Core over a portable engine, so
the async Protocol methods make DIRECT sync calls on short-lived transactions. No
async DB driver and no pytest-asyncio are introduced. Portable Core column types
only, so SQLite (CI) and Postgres behave identically by construction.

The runnable path uses two principal-owned tables:

* ``ne_agent_conversations`` — every create mints a new id owned by one exact
  principal/department/agent tuple; that tuple is deliberately not unique.
* ``ne_agent_conversation_turns`` — gapless turn batches with prepared/published/
  aborted state; only published rows enter model context or served history.

The older three pair-keyed tables remain readable through separately named ops
methods only and are never assigned or migrated into public conversations:

* ``ne_agent_sessions`` — one row per (agent_name, department) session, uniquely
  constrained so a department's agent has exactly one durable session id.
* ``ne_agent_session_messages`` — the ordered turn log; each row stores one
  ``append_turn`` batch as a ``ModelMessagesTypeAdapter`` JSON blob, keyed by
  ``session_id`` + monotonic ``seq``. ``load_history`` concatenates the batches in
  ``seq`` order back into a ``list[ModelMessage]``; with a ``limit`` it fetches only
  the most-recent-N batches (``ORDER BY seq DESC LIMIT N``, re-reversed to
  chronological) so a long-lived per-department session can never load unbounded
  rows nor brick every user on ``context_length_exceeded`` (P1-3). ``reset_history``
  is the governed ops path that clears a session's messages + seq counter (replacing
  manual DB surgery) while keeping the session row and id stable.
* ``ne_agent_session_seq`` — a per-session next-seq counter (one row per session,
  ``last_seq``). ``append_turn`` claims the next ``seq`` by an atomic
  ``UPDATE ... SET last_seq = last_seq + 1`` under the row lock, so N concurrent
  multi-worker appenders on ONE session produce a GAPLESS, CONTIGUOUS, UNIQUE seq
  (NE-070H). The counter is seeded ONCE from the legacy ``max(seq)`` so a
  pre-existing message log upgrades in place; a duplicate-seq ``uq_agent_session_messages_seq``
  unique index backstops the invariant (a legacy DB carrying duplicate seqs fails
  LOUDLY at ``ensure_runtime_schema`` — fail-closed surfacing of prior corruption).

Legacy concurrency (NE-070H): ``open_session`` uses a speculative
``INSERT ... ON CONFLICT DO NOTHING`` then reads the winner, so two workers opening
the same (agent, department) both return the SAME id with exactly one row.
``append_turn`` claims its seq via the counter table's row-locking UPDATE. Both
paths are portable (SQLite writer lock serializes; Postgres row locks serialize) —
no RETURNING, zero dialect variance in the claim itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import threading
import uuid
import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import sqlalchemy as sa
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from sqlalchemy import event
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError

from ne.audit import ensure_transactional_sqlite
from ne.audit.engine import call_db, create_engine_with_timeouts, default_offload
from ne.contracts.runtime import GroundedProvenance, VerifiedProvenance
from ne.runtime.config import RuntimeSettings

metadata = sa.MetaData()

ne_agent_sessions = sa.Table(
    "ne_agent_sessions",
    metadata,
    sa.Column("session_id", sa.String, primary_key=True),
    sa.Column("agent_name", sa.String, nullable=False),
    sa.Column("department", sa.String, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("agent_name", "department", name="uq_agent_department"),
)

ne_agent_session_messages = sa.Table(
    "ne_agent_session_messages",
    metadata,
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.Identity(),
        primary_key=True,
    ),
    sa.Column("session_id", sa.String, nullable=False, index=True),
    sa.Column("seq", sa.Integer, nullable=False),
    # Nullable only for legacy rows. New DepartmentAgentSession writes bind both
    # values from the verified run scopes at the same append boundary as payload.
    sa.Column("principal_id", sa.String, nullable=True),
    sa.Column("trace_id", sa.String, nullable=True),
    sa.Column("payload", sa.Text, nullable=False),
    # Dormant PRE-009A protected-content discriminator.  The legacy writer keeps
    # these nullable until 009C's quiesced migration/cutover proves every old row.
    sa.Column("content_status", sa.String, nullable=True),
    sa.Column("protected_content_ref", sa.String, nullable=True),
    sa.Column("protected_content_hash", sa.String(64), nullable=True),
    # PRE-009C's only lawful disposition for a row that predates principal
    # attribution.  Such a row cannot be assigned an invented owner merely to
    # create a protected payload: its bytes are erased and this content-free,
    # row-local retirement receipt is frozen instead.
    sa.Column("legacy_transport_retired", sa.Boolean, nullable=True),
    sa.Column("legacy_transport_generation", sa.String, nullable=True),
    sa.Column("legacy_transport_reason", sa.String, nullable=True),
    sa.Column("legacy_transport_fence", sa.String(64), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

# Per-session next-seq counter (NE-070H). ``append_turn`` claims the next seq via
# an atomic ``UPDATE ... last_seq = last_seq + 1`` under the row lock — gapless,
# contiguous, unique under concurrent multi-worker Postgres.
ne_agent_session_seq = sa.Table(
    "ne_agent_session_seq",
    metadata,
    sa.Column("session_id", sa.String, primary_key=True),
    sa.Column("last_seq", sa.Integer, nullable=False),
)

# Principal-owned conversation storage.  The three legacy session tables above
# remain intact for operator/audit recovery only; public request paths never
# resolve or append them.
ne_agent_conversations = sa.Table(
    "ne_agent_conversations",
    metadata,
    sa.Column("conversation_id", sa.String, primary_key=True),
    sa.Column("owner_principal_id", sa.String, nullable=False, index=True),
    sa.Column("agent_name", sa.String, nullable=False),
    sa.Column("department", sa.String, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_sequence", sa.Integer, nullable=False),
    sa.CheckConstraint("last_sequence >= 0", name="ck_agent_conversation_sequence"),
    sa.CheckConstraint(
        "length(trim(owner_principal_id)) > 0",
        name="ck_agent_conversation_owner_nonempty",
    ),
    sa.CheckConstraint(
        "length(trim(department)) > 0", name="ck_agent_conversation_department_nonempty"
    ),
    sa.CheckConstraint("length(trim(agent_name)) > 0", name="ck_agent_conversation_agent_nonempty"),
    sa.UniqueConstraint(
        "conversation_id",
        "owner_principal_id",
        "department",
        "agent_name",
        name="uq_agent_conversation_owner_scope",
    ),
)

ne_agent_conversation_turns = sa.Table(
    "ne_agent_conversation_turns",
    metadata,
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.Identity(),
        primary_key=True,
    ),
    sa.Column("conversation_id", sa.String, nullable=False, index=True),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("owner_principal_id", sa.String, nullable=False),
    sa.Column("department", sa.String, nullable=False),
    sa.Column("agent_name", sa.String, nullable=False),
    sa.Column("trace_id", sa.String, nullable=False),
    sa.Column("attachment_id", sa.String, nullable=False),
    sa.Column("attempt_id", sa.String, nullable=False),
    sa.Column("request_fingerprint", sa.String, nullable=False),
    # Bound only after the volatile model selector resolves.  The request
    # fingerprint above remains the pre-selection idempotency identity; this
    # second digest fences the exact model/lane payload executed by a worker.
    sa.Column("execution_fingerprint", sa.String, nullable=True),
    sa.Column("dispatch_id", sa.String, nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
    # Private model context and user-served output are deliberately distinct.
    sa.Column("private_context", sa.Text, nullable=False),
    sa.Column("served_output", sa.Text, nullable=False),
    sa.Column("citation_refs", sa.Text, nullable=False),
    sa.Column("served_citations", sa.Text, nullable=False),
    sa.Column("served_verified", sa.Text, nullable=False),
    sa.Column("served_grounded", sa.Text, nullable=False),
    # Whether the original terminal envelope linked its server trace.  This is
    # durable replay metadata, not client-selected provenance.
    sa.Column("served_trace_link", sa.Boolean, nullable=False),
    # Additive storage seam only.  PRE-009C later switches all writers/readers and
    # installs protected-only constraints after its complete plaintext scan.
    sa.Column("input_status", sa.String, nullable=True),
    sa.Column("protected_input_ref", sa.String, nullable=True),
    sa.Column("protected_input_hash", sa.String(64), nullable=True),
    sa.Column("terminal_status", sa.String, nullable=True),
    sa.Column("protected_terminal_ref", sa.String, nullable=True),
    sa.Column("protected_terminal_hash", sa.String(64), nullable=True),
    sa.Column("publication_state", sa.String, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("conversation_id", "sequence", name="uq_agent_conversation_turn_sequence"),
    sa.UniqueConstraint(
        "conversation_id",
        "attachment_id",
        name="uq_agent_conversation_turn_attachment_identity",
    ),
    sa.UniqueConstraint("attempt_id", name="uq_agent_conversation_turn_attempt"),
    sa.CheckConstraint(
        "publication_state IN ('claimed', 'staged', 'prepared', 'published', 'aborted')",
        name="ck_agent_conversation_turn_publication",
    ),
    sa.CheckConstraint("sequence > 0", name="ck_agent_conversation_turn_sequence_positive"),
    sa.CheckConstraint(
        "length(trim(owner_principal_id)) > 0",
        name="ck_agent_conversation_turn_owner_nonempty",
    ),
    sa.CheckConstraint(
        "length(trim(department)) > 0",
        name="ck_agent_conversation_turn_department_nonempty",
    ),
    sa.CheckConstraint(
        "length(trim(agent_name)) > 0", name="ck_agent_conversation_turn_agent_nonempty"
    ),
    sa.CheckConstraint(
        "length(trim(attachment_id)) > 0",
        name="ck_agent_conversation_turn_attachment_nonempty",
    ),
    sa.CheckConstraint(
        "length(trim(attempt_id)) > 0", name="ck_agent_conversation_turn_attempt_nonempty"
    ),
    sa.CheckConstraint(
        "length(trim(request_fingerprint)) = 64",
        name="ck_agent_conversation_turn_fingerprint_shape",
    ),
    sa.CheckConstraint(
        "execution_fingerprint IS NULL OR length(trim(execution_fingerprint)) = 64",
        name="ck_agent_conversation_turn_execution_fingerprint_shape",
    ),
    sa.ForeignKeyConstraint(
        ["conversation_id", "owner_principal_id", "department", "agent_name"],
        [
            "ne_agent_conversations.conversation_id",
            "ne_agent_conversations.owner_principal_id",
            "ne_agent_conversations.department",
            "ne_agent_conversations.agent_name",
        ],
        name="fk_agent_conversation_turn_owner_scope",
    ),
)

_active_conversation_attachment_index = sa.Index(
    "uq_agent_conversation_turn_active_attachment",
    ne_agent_conversation_turns.c.conversation_id,
    ne_agent_conversation_turns.c.attachment_id,
    unique=True,
    sqlite_where=ne_agent_conversation_turns.c.publication_state.in_(
        ("claimed", "staged", "prepared", "published")
    ),
    postgresql_where=ne_agent_conversation_turns.c.publication_state.in_(
        ("claimed", "staged", "prepared", "published")
    ),
)

# Backstop the gapless-unique seq invariant even on a pre-existing message table.
_uq_session_seq_index = sa.Index(
    "uq_agent_session_messages_seq",
    ne_agent_session_messages.c.session_id,
    ne_agent_session_messages.c.seq,
    unique=True,
)

_CONTEXT_COMPONENT_DOMAIN = b"neutron-enterprise/context-component/v1\0"
_CONTEXT_OBJECT_DOMAIN = b"neutron-enterprise/context-object/v1\0"


@dataclass(frozen=True)
class SessionContextComponent:
    """One persisted turn batch in a model-independent context snapshot.

    Only identity and timing leave the store: ``payload`` is hashed in place and
    is never retained on this value object.  The digest is domain-separated and
    binds the session + monotonic sequence as well as the serialized messages, so
    equal text in two sessions is not accidentally presented as one object.
    """

    seq: int
    component_id: str
    created_at: datetime


@dataclass(frozen=True)
class SessionContextSnapshot:
    """The active, persisted context window for one durable session.

    ``context_id`` binds the exact persisted component bytes while deliberately
    excluding the current/effective route assignment. Historical response payloads
    may themselves contain model metadata; a later route flip can still change who
    serves the next turn without redefining the already-persisted context object.
    ``total_components`` records the durable row count while ``components`` carries
    only the most-recent bounded window the runtime would load for a turn.
    """

    session_id: str
    session_created_at: datetime
    context_id: str
    components: tuple[SessionContextComponent, ...]
    total_components: int
    truncated: bool


@dataclass(frozen=True)
class ConversationTurn:
    """One retained turn batch with its server-derived attribution.

    ``messages`` is the complete stored JSON body.  It is never projected or
    truncated here: the governed API either discloses the whole batch or does
    not disclose that batch at all.  ``principal_id``/``trace_id`` are nullable
    solely for rows written before those columns existed; callers must present
    that absence honestly rather than reconstructing identity from nearby audit
    events.
    """

    session_id: str
    agent_name: str
    department: str
    seq: int
    principal_id: str | None
    trace_id: str | None
    messages: tuple[dict[str, Any], ...]
    created_at: datetime


@dataclass(frozen=True)
class ConversationTurnPage:
    """A newest-first, count-bounded window over all retained turns."""

    turns: tuple[ConversationTurn, ...]
    total_turns: int


@dataclass(frozen=True)
class PrincipalConversation:
    """Metadata for one opaque, principal-owned Ask conversation."""

    conversation_id: str
    owner_principal_id: str
    agent_name: str
    department: str
    created_at: datetime
    updated_at: datetime
    last_sequence: int


@dataclass(frozen=True)
class PublishedConversationReplay:
    """Safe terminal envelope retained for an exact idempotent replay."""

    output: str
    citations: tuple[dict[str, str], ...]
    verified: VerifiedProvenance | None
    grounded: GroundedProvenance | None
    trace_linked: bool


@dataclass(frozen=True, slots=True)
class ProtectedConversationContentBinding:
    """One exact source-free protected slot; never materialized content."""

    status: Literal["source_free"]
    protected_ref: str
    plaintext_hash: str


@dataclass(frozen=True, slots=True)
class ProtectedConversationTurn:
    """Owner-scoped bindings for one exact published legacy Conversation turn."""

    conversation_id: str
    owner_principal_id: str
    turn_sequence: int
    attachment_id: str
    attempt_id: str
    input: ProtectedConversationContentBinding
    terminal: ProtectedConversationContentBinding


ProtectedConversationLedgerStatus = Literal[
    "source_free", "source_bound_container", "legacy_unavailable"
]


@dataclass(frozen=True, slots=True)
class ProtectedConversationLedgerBinding:
    """One authenticated protected-content address, never its plaintext."""

    status: ProtectedConversationLedgerStatus
    protected_ref: str
    plaintext_hash: str


@dataclass(frozen=True, slots=True)
class ProtectedConversationLedgerTurn:
    """Safe metadata and protected bindings for one published Conversation turn."""

    conversation_id: str
    owner_principal_id: str
    department: str
    agent_name: str
    turn_sequence: int
    trace_id: str
    attachment_id: str
    attempt_id: str
    created_at: datetime
    input: ProtectedConversationLedgerBinding
    terminal: ProtectedConversationLedgerBinding


@dataclass(frozen=True, slots=True)
class ProtectedConversationLedgerPage:
    """A deterministic newest-first protected Conversation page."""

    turns: tuple[ProtectedConversationLedgerTurn, ...]
    next_before_sequence: int | None
    total_turns: int = 0


@dataclass(frozen=True)
class ConversationAttachmentClaim:
    """One exact request's right to execute, wait, or replay an attachment."""

    disposition: str
    attempt_id: str | None = None
    dispatch_id: str | None = None


class AttachmentRequestMismatchError(Exception):
    """An idempotency attachment was reused for a different safe fingerprint."""


def _protected_conversation_ledger_binding(
    status: object,
    protected_ref: object,
    plaintext_hash: object,
) -> ProtectedConversationLedgerBinding | None:
    allowed = {"source_free", "source_bound_container", "legacy_unavailable"}
    if (
        not isinstance(status, str)
        or status not in allowed
        or not isinstance(protected_ref, str)
        or not protected_ref.strip()
        or not isinstance(plaintext_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", plaintext_hash) is None
    ):
        return None
    return ProtectedConversationLedgerBinding(
        status=cast(ProtectedConversationLedgerStatus, status),
        protected_ref=protected_ref,
        plaintext_hash=plaintext_hash,
    )


def _utc(value: datetime) -> datetime:
    """Normalize SQLite's possibly-naive datetimes to authoritative UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _database_now(conn: sa.Connection) -> datetime:
    """Read lease time from the transactional database, never the API/worker host."""
    if conn.dialect.name == "sqlite":
        value = conn.execute(sa.text("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')")).scalar_one()
        return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)
    return _utc(conn.execute(sa.select(sa.func.clock_timestamp())).scalar_one())


def _principal_conversation(row: Any) -> PrincipalConversation:
    return PrincipalConversation(
        conversation_id=str(row.conversation_id),
        owner_principal_id=str(row.owner_principal_id),
        agent_name=str(row.agent_name),
        department=str(row.department),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
        last_sequence=int(row.last_sequence),
    )


def _hash_field(digest: Any, value: bytes) -> None:
    """Feed one unambiguously length-prefixed field into ``digest``."""
    digest.update(str(len(value)).encode("ascii"))
    digest.update(b":")
    digest.update(value)


def conversation_request_fingerprint(
    conversation_id: str,
    owner_principal_id: str,
    department: str,
    agent_name: str,
    prompt: str,
    requested_model: str | None,
    requested_model_lane: str | None = None,
    trace_id: str | None = None,
    principal_groups: Sequence[str] = (),
    principal_claims: dict[str, Any] | None = None,
) -> str:
    """Hash the exact safe request identity bound to one idempotent attachment."""
    digest = hashlib.sha256()
    for value in (
        "principal-conversation-request-v1",
        conversation_id,
        owner_principal_id,
        department,
        agent_name,
        prompt,
        "requested-model:none" if requested_model is None else "requested-model:value",
        requested_model or "",
        "requested-lane:none" if requested_model_lane is None else "requested-lane:value",
        requested_model_lane or "",
        "trace:none" if trace_id is None else "trace:value",
        trace_id or "",
        json.dumps(sorted(principal_groups), separators=(",", ":")),
        json.dumps(principal_claims or {}, sort_keys=True, separators=(",", ":")),
    ):
        _hash_field(digest, value.encode("utf-8"))
    return digest.hexdigest()


def _component_id(session_id: str, seq: int, payload: str) -> str:
    digest = hashlib.sha256()
    digest.update(_CONTEXT_COMPONENT_DOMAIN)
    _hash_field(digest, session_id.encode("utf-8"))
    _hash_field(digest, str(seq).encode("ascii"))
    _hash_field(digest, payload.encode("utf-8"))
    return digest.hexdigest()


def _context_id(
    session_id: str, max_components: int, components: Sequence[SessionContextComponent]
) -> str:
    digest = hashlib.sha256()
    digest.update(_CONTEXT_OBJECT_DOMAIN)
    _hash_field(digest, session_id.encode("utf-8"))
    _hash_field(digest, str(max_components).encode("ascii"))
    for component in components:
        _hash_field(digest, str(component.seq).encode("ascii"))
        _hash_field(digest, component.component_id.encode("ascii"))
    return digest.hexdigest()


def _insert_ignore(table: sa.Table, conn: sa.Connection, *, index_elements: Sequence[str]) -> Any:
    """Return a dialect-specific ``INSERT ... ON CONFLICT DO NOTHING`` for ``table``.

    Keyed on ``conn.dialect.name`` (precedent: ne-events/outbox.py dialect guards).
    Any dialect other than sqlite/postgresql raises ``NotImplementedError``.
    """
    dialect = conn.dialect.name
    if dialect == "postgresql":
        return postgresql.insert(table).on_conflict_do_nothing(index_elements=list(index_elements))
    if dialect == "sqlite":
        return sqlite.insert(table).on_conflict_do_nothing(index_elements=list(index_elements))
    raise NotImplementedError("ne-runtime supports sqlite and postgresql")


_POSTGRES_SCHEMA_LOCK_SQL = "SELECT pg_advisory_xact_lock(56453543507007)"
_SQLITE_SCHEMA_LOCK = threading.Lock()
_SQLITE_FOREIGN_KEY_ENGINES: weakref.WeakSet[sa.Engine] = weakref.WeakSet()


def _ensure_sqlite_foreign_keys(engine: sa.Engine) -> None:
    """Enforce ownership foreign keys on every SQLite connection, not just boot's."""
    if engine.dialect.name != "sqlite" or engine in _SQLITE_FOREIGN_KEY_ENGINES:
        return
    _SQLITE_FOREIGN_KEY_ENGINES.add(engine)

    @event.listens_for(engine, "checkout")
    def _enable_foreign_keys(
        dbapi_connection: Any, _connection_record: Any, _connection_proxy: Any
    ) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


_POSTGRES_TEXT_CAST = re.compile(
    r"::(?:character\s+varying|varchar|text)(?:\[\])?",
    flags=re.IGNORECASE,
)


def _normalize_unquoted_sql(value: object) -> str:
    """Fold SQL syntax while preserving every byte inside string literals."""
    source = str(value)
    normalized: list[str] = []
    unquoted: list[str] = []

    def flush_unquoted() -> None:
        if not unquoted:
            return
        segment = "".join(unquoted).lower().replace('"', "")
        segment = _POSTGRES_TEXT_CAST.sub("", segment)
        segment = re.sub(r"\s+", "", segment)
        segment = segment.replace("trim(bothfrom", "trim(").replace("btrim(", "trim(")
        normalized.append(segment)
        unquoted.clear()

    index = 0
    while index < len(source):
        character = source[index]
        if character != "'":
            unquoted.append(character)
            index += 1
            continue
        flush_unquoted()
        literal = [character]
        index += 1
        while index < len(source):
            character = source[index]
            literal.append(character)
            index += 1
            if character != "'":
                continue
            if index < len(source) and source[index] == "'":
                literal.append(source[index])
                index += 1
                continue
            break
        normalized.append("".join(literal))
    flush_unquoted()
    return "".join(normalized)


def _strip_balanced_outer_parentheses(value: str) -> str:
    normalized = value
    while normalized.startswith("(") and normalized.endswith(")"):
        depth = 0
        encloses_all = True
        for index, character in enumerate(normalized):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(normalized) - 1:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        normalized = normalized[1:-1]
    return normalized


def _canonical_schema_expression(value: object) -> str:
    """Canonicalize only portable SQL and Postgres' equivalent deparser forms.

    PostgreSQL reflects ``trim(varchar)`` as ``btrim(varchar::text)`` and an
    ``IN`` predicate as ``varchar::text = ANY(ARRAY[...]::text[])``.  Those are
    the same constraints emitted from this metadata, not weakened shapes.  Keep
    the accepted rewrites deliberately narrow so renamed columns, changed
    bounds, and changed enum members remain value-discriminating.
    """
    normalized = _normalize_unquoted_sql(value)
    normalized = _strip_balanced_outer_parentheses(normalized)
    # PostgreSQL adds grouping around a varchar before its reflected ``::text``
    # cast (removed above).  Normalize only those two deparser-owned positions:
    # the argument to trim/btrim and the left operand of ``= ANY``.  A prior
    # global ``(identifier) -> identifier`` rewrite erased every function's
    # parentheses recursively, making ``length(trim(owner))`` indistinguishable
    # from a permissive user-defined ``lengthtrim(owner)`` constraint.
    normalized = re.sub(
        r"trim\(\(([a-z_][a-z0-9_]*)\)\)",
        r"trim(\1)",
        normalized,
    )
    normalized = re.sub(r"^\(([a-z_][a-z0-9_]*)\)(?==any\()", r"\1", normalized)
    normalized = _strip_balanced_outer_parentheses(normalized)
    postgres_any = re.fullmatch(
        r"(?P<column>[a-z_][a-z0-9_]*)=any\(\(*array\["
        r"(?P<values>'[^']*'(?:,'[^']*')*)\]\)*\)",
        normalized,
    )
    if postgres_any is not None:
        normalized = f"{postgres_any.group('column')}in({postgres_any.group('values')})"
    return normalized


def _assert_principal_conversation_shape(conn: sa.Connection) -> None:
    expected: dict[str, dict[str, tuple[bool, tuple[type[sa.types.TypeEngine[Any]], ...]]]] = {
        ne_agent_conversations.name: {
            "conversation_id": (False, (sa.String,)),
            "owner_principal_id": (False, (sa.String,)),
            "agent_name": (False, (sa.String,)),
            "department": (False, (sa.String,)),
            "created_at": (False, (sa.DateTime,)),
            "updated_at": (False, (sa.DateTime,)),
            "last_sequence": (False, (sa.Integer,)),
        },
        ne_agent_conversation_turns.name: {
            "id": (False, (sa.Integer, sa.BigInteger)),
            "conversation_id": (False, (sa.String,)),
            "sequence": (False, (sa.Integer,)),
            "owner_principal_id": (False, (sa.String,)),
            "department": (False, (sa.String,)),
            "agent_name": (False, (sa.String,)),
            "trace_id": (False, (sa.String,)),
            "attachment_id": (False, (sa.String,)),
            "attempt_id": (False, (sa.String,)),
            "request_fingerprint": (False, (sa.String,)),
            "execution_fingerprint": (True, (sa.String,)),
            "dispatch_id": (True, (sa.String,)),
            "lease_expires_at": (False, (sa.DateTime,)),
            "private_context": (False, (sa.Text,)),
            "served_output": (False, (sa.Text,)),
            "citation_refs": (False, (sa.Text,)),
            "served_citations": (False, (sa.Text,)),
            "served_verified": (False, (sa.Text,)),
            "served_grounded": (False, (sa.Text,)),
            "served_trace_link": (False, (sa.Boolean,)),
            "input_status": (True, (sa.String,)),
            "protected_input_ref": (True, (sa.String,)),
            "protected_input_hash": (True, (sa.String,)),
            "terminal_status": (True, (sa.String,)),
            "protected_terminal_ref": (True, (sa.String,)),
            "protected_terminal_hash": (True, (sa.String,)),
            "publication_state": (False, (sa.String,)),
            "created_at": (False, (sa.DateTime,)),
        },
    }
    inspector = sa.inspect(conn)
    for table_name, columns in expected.items():
        actual = {str(column["name"]): column for column in inspector.get_columns(table_name)}
        if set(actual) != set(columns):
            raise RuntimeError(f"runtime schema has incompatible {table_name} columns")
        for name, (nullable, types) in columns.items():
            if (
                name not in actual
                or bool(actual[name]["nullable"]) is not nullable
                or not isinstance(actual[name]["type"], types)
            ):
                raise RuntimeError(f"runtime schema has incompatible {table_name}.{name} shape")
    required_pks = {
        ne_agent_conversations.name: ["conversation_id"],
        ne_agent_conversation_turns.name: ["id"],
    }
    for table, columns in required_pks.items():
        if inspector.get_pk_constraint(table).get("constrained_columns") != columns:
            raise RuntimeError(f"runtime {table} primary key has an incompatible shape")
    expected_uniques = {
        ne_agent_conversations.name: {
            "uq_agent_conversation_owner_scope": (
                "conversation_id",
                "owner_principal_id",
                "department",
                "agent_name",
            )
        },
        ne_agent_conversation_turns.name: {
            "uq_agent_conversation_turn_sequence": ("conversation_id", "sequence"),
            "uq_agent_conversation_turn_attachment_identity": (
                "conversation_id",
                "attachment_id",
            ),
            "uq_agent_conversation_turn_attempt": ("attempt_id",),
        },
    }
    for table, expected_table_uniques in expected_uniques.items():
        actual_table_uniques = {
            str(item.get("name")): tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(table)
        }
        if actual_table_uniques != expected_table_uniques:
            raise RuntimeError(f"runtime {table} unique constraints have an incompatible shape")

    expected_checks = {
        ne_agent_conversations.name: {
            "ck_agent_conversation_sequence": "last_sequence >= 0",
            "ck_agent_conversation_owner_nonempty": "length(trim(owner_principal_id)) > 0",
            "ck_agent_conversation_department_nonempty": "length(trim(department)) > 0",
            "ck_agent_conversation_agent_nonempty": "length(trim(agent_name)) > 0",
        },
        ne_agent_conversation_turns.name: {
            "ck_agent_conversation_turn_publication": (
                "publication_state IN ('claimed', 'staged', 'prepared', 'published', 'aborted')"
            ),
            "ck_agent_conversation_turn_sequence_positive": "sequence > 0",
            "ck_agent_conversation_turn_owner_nonempty": ("length(trim(owner_principal_id)) > 0"),
            "ck_agent_conversation_turn_department_nonempty": "length(trim(department)) > 0",
            "ck_agent_conversation_turn_agent_nonempty": "length(trim(agent_name)) > 0",
            "ck_agent_conversation_turn_attachment_nonempty": ("length(trim(attachment_id)) > 0"),
            "ck_agent_conversation_turn_attempt_nonempty": "length(trim(attempt_id)) > 0",
            "ck_agent_conversation_turn_fingerprint_shape": (
                "length(trim(request_fingerprint)) = 64"
            ),
            "ck_agent_conversation_turn_execution_fingerprint_shape": (
                "execution_fingerprint IS NULL OR length(trim(execution_fingerprint)) = 64"
            ),
        },
    }
    for table, expected_table_checks in expected_checks.items():
        actual_table_checks = {
            str(item.get("name")): _canonical_schema_expression(item.get("sqltext"))
            for item in inspector.get_check_constraints(table)
        }
        normalized_expected = {
            name: _canonical_schema_expression(expression)
            for name, expression in expected_table_checks.items()
        }
        if actual_table_checks != normalized_expected:
            raise RuntimeError(f"runtime {table} checks have an incompatible shape")
    default_schema = str(inspector.default_schema_name or "")

    def _normalized_referred_schema(value: object) -> str | None:
        if value is None or str(value) in {"", default_schema}:
            return None
        return str(value)

    def _normalized_fk_action(value: object) -> str | None:
        if value is None or str(value).upper() in {"", "NO ACTION"}:
            return None
        return str(value).upper()

    foreign_keys = {
        str(item.get("name")): (
            tuple(item.get("constrained_columns") or ()),
            _normalized_referred_schema(item.get("referred_schema")),
            str(item.get("referred_table")),
            tuple(item.get("referred_columns") or ()),
            _normalized_fk_action((item.get("options") or {}).get("onupdate")),
            _normalized_fk_action((item.get("options") or {}).get("ondelete")),
        )
        for item in inspector.get_foreign_keys(ne_agent_conversation_turns.name)
    }
    expected_foreign_keys = {
        "fk_agent_conversation_turn_owner_scope": (
            ("conversation_id", "owner_principal_id", "department", "agent_name"),
            None,
            ne_agent_conversations.name,
            ("conversation_id", "owner_principal_id", "department", "agent_name"),
            None,
            None,
        )
    }
    if foreign_keys != expected_foreign_keys:
        raise RuntimeError("runtime conversation owner foreign key has an incompatible shape")
    expected_indexes = {
        ne_agent_conversations.name: {
            "ix_ne_agent_conversations_owner_principal_id": (
                ("owner_principal_id",),
                False,
                None,
            )
        },
        ne_agent_conversation_turns.name: {
            "ix_ne_agent_conversation_turns_conversation_id": (
                ("conversation_id",),
                False,
                None,
            ),
            "uq_agent_conversation_turn_active_attachment": (
                ("conversation_id", "attachment_id"),
                True,
                _canonical_schema_expression(
                    "publication_state IN ('claimed', 'staged', 'prepared', 'published')"
                ),
            ),
        },
    }
    dialect_where_key = f"{conn.dialect.name}_where"
    for table, expected_table_indexes in expected_indexes.items():
        actual_table_indexes = {}
        for item in inspector.get_indexes(table):
            # PostgreSQL reports the physical index backing a UNIQUE constraint
            # with ``duplicates_constraint``; the logical constraint was checked
            # above and must not be double-counted as an independent index.
            if item.get("duplicates_constraint"):
                continue
            where = (item.get("dialect_options") or {}).get(dialect_where_key)
            actual_table_indexes[str(item.get("name"))] = (
                tuple(item.get("column_names") or ()),
                bool(item.get("unique")),
                None if where is None else _canonical_schema_expression(where),
            )
        if actual_table_indexes != expected_table_indexes:
            raise RuntimeError(f"runtime {table} indexes have an incompatible shape")


def _initialize_runtime_schema(conn: sa.Connection) -> None:
    metadata.create_all(conn)
    existing = {
        str(column["name"])
        for column in sa.inspect(conn).get_columns(ne_agent_session_messages.name)
    }
    if "principal_id" not in existing:
        conn.execute(
            sa.text("ALTER TABLE ne_agent_session_messages ADD COLUMN principal_id VARCHAR")
        )
    if "trace_id" not in existing:
        conn.execute(sa.text("ALTER TABLE ne_agent_session_messages ADD COLUMN trace_id VARCHAR"))
    session_additions = {
        "content_status": "VARCHAR",
        "protected_content_ref": "VARCHAR",
        "protected_content_hash": "VARCHAR(64)",
        "legacy_transport_retired": "BOOLEAN",
        "legacy_transport_generation": "VARCHAR",
        "legacy_transport_reason": "VARCHAR",
        "legacy_transport_fence": "VARCHAR(64)",
    }
    for name, sql_type in session_additions.items():
        if name not in existing:
            conn.execute(
                sa.text(f"ALTER TABLE ne_agent_session_messages ADD COLUMN {name} {sql_type}")
            )
    turn_existing = {
        str(column["name"])
        for column in sa.inspect(conn).get_columns(ne_agent_conversation_turns.name)
    }
    turn_additions = {
        "input_status": "VARCHAR",
        "protected_input_ref": "VARCHAR",
        "protected_input_hash": "VARCHAR(64)",
        "terminal_status": "VARCHAR",
        "protected_terminal_ref": "VARCHAR",
        "protected_terminal_hash": "VARCHAR(64)",
    }
    for name, sql_type in turn_additions.items():
        if name not in turn_existing:
            conn.execute(
                sa.text(f"ALTER TABLE ne_agent_conversation_turns ADD COLUMN {name} {sql_type}")
            )
    _uq_session_seq_index.create(conn, checkfirst=True)
    _assert_principal_conversation_shape(conn)


def ensure_runtime_schema(engine: sa.Engine) -> None:
    """Create the runtime session tables. Idempotent (``create_all`` skips existing).

    Also ensures the ``uq_agent_session_messages_seq`` unique index on a
    PRE-EXISTING message table (``CREATE UNIQUE INDEX IF NOT EXISTS`` is portable).
    A legacy DB that already carries duplicate ``(session_id, seq)`` rows fails
    LOUDLY here — a fail-closed surfacing of pre-NE-070H corruption, not a silent
    downgrade.
    """
    _ensure_sqlite_foreign_keys(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.exec_driver_sql(_POSTGRES_SCHEMA_LOCK_SQL)
            _initialize_runtime_schema(conn)
    elif engine.dialect.name == "sqlite":
        with _SQLITE_SCHEMA_LOCK, engine.begin() as conn:
            _initialize_runtime_schema(conn)
    else:
        raise RuntimeError(f"runtime schema does not support {engine.dialect.name!r}")


class SqlAlchemySessionStore:
    """Durable principal conversations plus separately named legacy ops access."""

    def __init__(self, engine: sa.Engine, *, offload_db_io: bool | None = None) -> None:
        self._engine = engine
        # Offload blocking session I/O to a worker thread on non-sqlite dialects so
        # a slow/blocked Postgres statement never pins the ASGI event loop (P2-16).
        self._offload = default_offload(engine) if offload_db_io is None else offload_db_io
        # A live execution renews this DB lease at the shorter heartbeat cadence.
        # They are instance-owned so controlled tests can compress time without
        # changing production behavior or relying on wall-clock sleeps.
        self._conversation_lease_seconds = 300.0
        self._conversation_heartbeat_seconds = 30.0

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> SqlAlchemySessionStore:
        if not settings.session_database_url:
            raise ValueError(
                "RuntimeSettings.session_database_url is empty; set "
                "NE_RUNTIME_SESSION_DATABASE_URL or construct "
                "SqlAlchemySessionStore(engine) directly"
            )
        engine = create_engine_with_timeouts(
            settings.session_database_url, timeouts=settings.engine_timeouts
        )
        ensure_transactional_sqlite(engine)
        ensure_runtime_schema(engine)
        return cls(engine)

    @property
    def engine(self) -> sa.Engine:
        return self._engine

    @property
    def storage_dialect(self) -> str:
        """The real SQL backend name (``postgresql`` or ``sqlite``)."""
        return str(self._engine.dialect.name)

    @property
    def conversation_heartbeat_seconds(self) -> float:
        return self._conversation_heartbeat_seconds

    async def create_conversation(
        self, agent_name: str, department: str, owner_principal_id: str
    ) -> PrincipalConversation:
        """Mint a fresh conversation; tuple equality never collapses identities."""
        if not all(value.strip() for value in (agent_name, department, owner_principal_id)):
            raise ValueError("conversation agent, department, and owner must be non-empty")
        return await call_db(
            self._offload,
            self._create_conversation_sync,
            agent_name,
            department,
            owner_principal_id,
        )

    def _create_conversation_sync(
        self, agent_name: str, department: str, owner_principal_id: str
    ) -> PrincipalConversation:
        now = datetime.now(UTC)
        conversation_id = str(uuid.uuid4())
        with self._engine.begin() as conn:
            conn.execute(
                ne_agent_conversations.insert().values(
                    conversation_id=conversation_id,
                    owner_principal_id=owner_principal_id,
                    agent_name=agent_name,
                    department=department,
                    created_at=now,
                    updated_at=now,
                    last_sequence=0,
                )
            )
        return PrincipalConversation(
            conversation_id=conversation_id,
            owner_principal_id=owner_principal_id,
            agent_name=agent_name,
            department=department,
            created_at=now,
            updated_at=now,
            last_sequence=0,
        )

    async def get_conversation(
        self, conversation_id: str, owner_principal_id: str, department: str
    ) -> PrincipalConversation | None:
        return await call_db(
            self._offload,
            self._get_conversation_sync,
            conversation_id,
            owner_principal_id,
            department,
        )

    def _get_conversation_sync(
        self, conversation_id: str, owner_principal_id: str, department: str
    ) -> PrincipalConversation | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_conversations).where(
                    ne_agent_conversations.c.conversation_id == conversation_id,
                    ne_agent_conversations.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversations.c.department == department,
                )
            ).first()
        return None if row is None else _principal_conversation(row)

    async def list_conversations(
        self, owner_principal_id: str, department: str
    ) -> tuple[PrincipalConversation, ...]:
        return await call_db(
            self._offload, self._list_conversations_sync, owner_principal_id, department
        )

    def _list_conversations_sync(
        self, owner_principal_id: str, department: str
    ) -> tuple[PrincipalConversation, ...]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_agent_conversations)
                .where(
                    ne_agent_conversations.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversations.c.department == department,
                )
                .order_by(ne_agent_conversations.c.updated_at.desc())
            ).all()
        return tuple(_principal_conversation(row) for row in rows)

    async def load_conversation_history(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        *,
        limit: int | None = None,
    ) -> list[ModelMessage]:
        return await call_db(
            self._offload,
            self._load_conversation_history_sync,
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            limit,
        )

    def _load_conversation_history_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        limit: int | None,
    ) -> list[ModelMessage]:
        predicate = sa.and_(
            ne_agent_conversations.c.conversation_id == conversation_id,
            ne_agent_conversations.c.owner_principal_id == owner_principal_id,
            ne_agent_conversations.c.department == department,
            ne_agent_conversations.c.agent_name == agent_name,
        )
        stmt = (
            sa.select(ne_agent_conversation_turns.c.private_context)
            .join(
                ne_agent_conversations,
                ne_agent_conversations.c.conversation_id
                == ne_agent_conversation_turns.c.conversation_id,
            )
            .where(
                predicate,
                ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                ne_agent_conversation_turns.c.department == department,
                ne_agent_conversation_turns.c.agent_name == agent_name,
                ne_agent_conversation_turns.c.publication_state == "published",
            )
        )
        with self._engine.connect() as conn:
            rows = (
                list(conn.execute(stmt.order_by(ne_agent_conversation_turns.c.sequence)).all())
                if limit is None
                else list(
                    reversed(
                        conn.execute(
                            stmt.order_by(ne_agent_conversation_turns.c.sequence.desc()).limit(
                                limit
                            )
                        ).all()
                    )
                )
            )
        history: list[ModelMessage] = []
        for row in rows:
            history.extend(ModelMessagesTypeAdapter.validate_json(row.private_context))
        return history

    async def published_context_bytes(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        *,
        limit: int | None = None,
    ) -> int | None:
        """Total stored bytes of the published context the next run will load.

        NE-CTX-IN-FORCE round 8 (finding 2): the legacy chat surface gates an
        explicit model switch against the CURRENT conversation snapshot, and
        this read is where it learns the snapshot's size. It sums the
        ``private_context`` JSON of exactly the newest ``limit`` published
        turns — the same rows :meth:`load_conversation_history` materializes
        for the run — measured as stored (bytes upper-bounding tokens, the
        boundary-receipt arithmetic), never re-estimated by a second
        estimator. ``None`` means NO published turn exists: a genuinely empty
        conversation has no snapshot to check — never a fabricated zero.
        """
        return await call_db(
            self._offload,
            self._published_context_bytes_sync,
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            limit,
        )

    def _published_context_bytes_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        limit: int | None,
    ) -> int | None:
        predicate = sa.and_(
            ne_agent_conversations.c.conversation_id == conversation_id,
            ne_agent_conversations.c.owner_principal_id == owner_principal_id,
            ne_agent_conversations.c.department == department,
            ne_agent_conversations.c.agent_name == agent_name,
        )
        stmt = (
            sa.select(sa.func.length(ne_agent_conversation_turns.c.private_context))
            .join(
                ne_agent_conversations,
                ne_agent_conversations.c.conversation_id
                == ne_agent_conversation_turns.c.conversation_id,
            )
            .where(
                predicate,
                ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                ne_agent_conversation_turns.c.department == department,
                ne_agent_conversation_turns.c.agent_name == agent_name,
                ne_agent_conversation_turns.c.publication_state == "published",
            )
            .order_by(ne_agent_conversation_turns.c.sequence.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        with self._engine.connect() as conn:
            lengths = [row[0] for row in conn.execute(stmt).all()]
        if not lengths:
            return None
        return sum(int(length) for length in lengths if length is not None)

    async def claim_conversation_attachment(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
        request_fingerprint: str,
        trace_id: str,
        *,
        preferred_attempt_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> ConversationAttachmentClaim:
        effective_lease_seconds = (
            self._conversation_lease_seconds if lease_seconds is None else lease_seconds
        )
        if (
            not all(
                value.strip()
                for value in (
                    conversation_id,
                    owner_principal_id,
                    department,
                    agent_name,
                    attachment_id,
                    trace_id,
                )
            )
            or re.fullmatch(r"[0-9a-f]{64}", request_fingerprint) is None
            or effective_lease_seconds <= 0
        ):
            raise ValueError("conversation attachment claim has an invalid shape")
        return await call_db(
            self._offload,
            self._claim_conversation_attachment_sync,
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            attachment_id,
            request_fingerprint,
            trace_id,
            preferred_attempt_id,
            effective_lease_seconds,
        )

    async def renew_conversation_attachment_lease(
        self,
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
    ) -> bool:
        """Renew only the exact still-active attempt; stale workers get ``False``."""
        return await call_db(
            self._offload,
            self._renew_conversation_attachment_lease_sync,
            conversation_id,
            owner_principal_id,
            attachment_id,
            attempt_id,
            self._conversation_lease_seconds,
        )

    def _renew_conversation_attachment_lease_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
        lease_seconds: float,
    ) -> bool:
        with self._engine.begin() as conn:
            lease_expires_at = _database_now(conn) + timedelta(seconds=lease_seconds)
            result = conn.execute(
                ne_agent_conversation_turns.update()
                .where(
                    ne_agent_conversation_turns.c.conversation_id == conversation_id,
                    ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversation_turns.c.attachment_id == attachment_id,
                    ne_agent_conversation_turns.c.attempt_id == attempt_id,
                    ne_agent_conversation_turns.c.publication_state.in_(
                        ("claimed", "staged", "prepared")
                    ),
                )
                .values(lease_expires_at=lease_expires_at)
            )
        return result.rowcount == 1

    async def bind_conversation_execution(
        self,
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
        execution_fingerprint: str,
    ) -> None:
        """CAS-bind one exact claimed epoch to the resolved execution request."""
        if re.fullmatch(r"[0-9a-f]{64}", execution_fingerprint) is None:
            raise ValueError("conversation execution fingerprint has an invalid shape")
        await call_db(
            self._offload,
            self._bind_conversation_execution_sync,
            conversation_id,
            owner_principal_id,
            attachment_id,
            attempt_id,
            execution_fingerprint,
        )

    def _bind_conversation_execution_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
        execution_fingerprint: str,
    ) -> None:
        scope = (
            ne_agent_conversation_turns.c.conversation_id == conversation_id,
            ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
            ne_agent_conversation_turns.c.attachment_id == attachment_id,
            ne_agent_conversation_turns.c.attempt_id == attempt_id,
        )
        with self._engine.begin() as conn:
            current = conn.execute(
                sa.select(
                    ne_agent_conversation_turns.c.execution_fingerprint,
                    ne_agent_conversation_turns.c.publication_state,
                )
                .where(*scope)
                .with_for_update()
            ).first()
            if current is None or str(current.publication_state) != "claimed":
                raise LookupError("conversation attachment claim is not executable")
            if current.execution_fingerprint is not None:
                if not hmac.compare_digest(
                    str(current.execution_fingerprint), execution_fingerprint
                ):
                    raise AttachmentRequestMismatchError(
                        "conversation execution does not match its claimed request"
                    )
                return
            result = conn.execute(
                ne_agent_conversation_turns.update()
                .where(*scope, ne_agent_conversation_turns.c.publication_state == "claimed")
                .values(execution_fingerprint=execution_fingerprint)
            )
            if result.rowcount != 1:
                raise LookupError("conversation attachment claim is not executable")

    async def mark_conversation_dispatched(
        self,
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
        dispatch_id: str,
    ) -> None:
        """Bind a successful durable enqueue to its exact fenced attempt."""
        if not dispatch_id.strip():
            raise ValueError("conversation dispatch id must be non-empty")
        await call_db(
            self._offload,
            self._mark_conversation_dispatched_sync,
            conversation_id,
            owner_principal_id,
            attachment_id,
            attempt_id,
            dispatch_id,
        )

    def _mark_conversation_dispatched_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
        dispatch_id: str,
    ) -> None:
        with self._engine.begin() as conn:
            result = conn.execute(
                ne_agent_conversation_turns.update()
                .where(
                    ne_agent_conversation_turns.c.conversation_id == conversation_id,
                    ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversation_turns.c.attachment_id == attachment_id,
                    ne_agent_conversation_turns.c.attempt_id == attempt_id,
                    ne_agent_conversation_turns.c.publication_state.in_(
                        ("claimed", "staged", "prepared", "published")
                    ),
                )
                .values(dispatch_id=dispatch_id)
            )
            if result.rowcount != 1:
                raise LookupError("conversation attachment claim is not dispatchable")

    def _claim_conversation_attachment_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
        request_fingerprint: str,
        trace_id: str,
        preferred_attempt_id: str | None,
        lease_seconds: float,
    ) -> ConversationAttachmentClaim:
        for collision in range(2):
            try:
                with self._engine.begin() as conn:
                    now = _database_now(conn)
                    lease_expires_at = now + timedelta(seconds=lease_seconds)
                    conversation_scope = (
                        ne_agent_conversations.c.conversation_id == conversation_id,
                        ne_agent_conversations.c.owner_principal_id == owner_principal_id,
                        ne_agent_conversations.c.department == department,
                        ne_agent_conversations.c.agent_name == agent_name,
                    )
                    last_sequence = conn.execute(
                        sa.select(ne_agent_conversations.c.last_sequence)
                        .where(*conversation_scope)
                        .with_for_update()
                    ).scalar_one_or_none()
                    if last_sequence is None:
                        raise LookupError("unknown conversation")
                    turn_scope = (
                        ne_agent_conversation_turns.c.conversation_id == conversation_id,
                        ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                        ne_agent_conversation_turns.c.department == department,
                        ne_agent_conversation_turns.c.agent_name == agent_name,
                        ne_agent_conversation_turns.c.attachment_id == attachment_id,
                    )
                    existing = conn.execute(
                        sa.select(
                            ne_agent_conversation_turns.c.request_fingerprint,
                            ne_agent_conversation_turns.c.attempt_id,
                            ne_agent_conversation_turns.c.publication_state,
                            ne_agent_conversation_turns.c.lease_expires_at,
                            ne_agent_conversation_turns.c.dispatch_id,
                            ne_agent_conversation_turns.c.trace_id,
                            ne_agent_conversation_turns.c.execution_fingerprint,
                        )
                        .where(*turn_scope)
                        .with_for_update()
                    ).first()
                    if existing is not None:
                        stored_fingerprint = str(existing.request_fingerprint)
                        if re.fullmatch(
                            r"[0-9a-f]{64}", stored_fingerprint
                        ) is None or not hmac.compare_digest(
                            stored_fingerprint, request_fingerprint
                        ):
                            raise AttachmentRequestMismatchError(
                                "idempotency attachment does not match its original request"
                            )
                        if str(existing.trace_id) != trace_id:
                            raise AttachmentRequestMismatchError(
                                "idempotency attachment does not match its original trace"
                            )
                        state = str(existing.publication_state)
                        current_attempt = str(existing.attempt_id)
                        if state == "published":
                            return ConversationAttachmentClaim("published", current_attempt)
                        existing_lease = _utc(existing.lease_expires_at)
                        if preferred_attempt_id is not None:
                            if state == "aborted" or current_attempt != preferred_attempt_id:
                                raise LookupError("conversation attachment attempt is stale")
                            conn.execute(
                                ne_agent_conversation_turns.update()
                                .where(*turn_scope)
                                .values(lease_expires_at=lease_expires_at)
                            )
                            return ConversationAttachmentClaim(
                                state if state in {"staged", "prepared"} else "owned",
                                preferred_attempt_id,
                                None if existing.dispatch_id is None else str(existing.dispatch_id),
                            )
                        if state in {"staged", "prepared"}:
                            if state == "prepared" and (
                                existing.execution_fingerprint is None
                                or re.fullmatch(
                                    r"[0-9a-f]{64}", str(existing.execution_fingerprint)
                                )
                                is None
                            ):
                                raise RuntimeError(
                                    "prepared conversation execution binding is malformed"
                                )
                            if existing_lease > now:
                                return ConversationAttachmentClaim(
                                    "inflight",
                                    current_attempt,
                                    None
                                    if existing.dispatch_id is None
                                    else str(existing.dispatch_id),
                                )
                            # A recovery owner gets a fresh epoch while retaining
                            # the immutable staged/prepared terminal envelope.
                            attempt_id = f"attempt-{uuid.uuid4()}"
                            conn.execute(
                                ne_agent_conversation_turns.update()
                                .where(*turn_scope)
                                .values(attempt_id=attempt_id, lease_expires_at=lease_expires_at)
                            )
                            return ConversationAttachmentClaim(state, attempt_id)
                        if state == "claimed" and (
                            existing.dispatch_id is not None or existing_lease > now
                        ):
                            return ConversationAttachmentClaim(
                                "inflight",
                                current_attempt,
                                None if existing.dispatch_id is None else str(existing.dispatch_id),
                            )
                        # Expired un-dispatched claims and aborted attempts are
                        # retryable only under a fresh epoch.  Never revive the
                        # token carried by an old queued/failed worker.
                        attempt_id = f"attempt-{uuid.uuid4()}"
                        conn.execute(
                            ne_agent_conversation_turns.update()
                            .where(*turn_scope)
                            .values(
                                attempt_id=attempt_id,
                                trace_id=trace_id,
                                request_fingerprint=request_fingerprint,
                                execution_fingerprint=None,
                                dispatch_id=None,
                                lease_expires_at=lease_expires_at,
                                private_context="[]",
                                served_output="",
                                citation_refs="[]",
                                served_citations="[]",
                                served_verified="null",
                                served_grounded="null",
                                served_trace_link=False,
                                input_status=None,
                                protected_input_ref=None,
                                protected_input_hash=None,
                                terminal_status=None,
                                protected_terminal_ref=None,
                                protected_terminal_hash=None,
                                publication_state="claimed",
                            )
                        )
                        return ConversationAttachmentClaim("owned", attempt_id)

                    attempt_id = preferred_attempt_id or f"attempt-{uuid.uuid4()}"
                    sequence = int(last_sequence) + 1
                    conn.execute(
                        ne_agent_conversations.update()
                        .where(*conversation_scope)
                        .values(last_sequence=sequence, updated_at=now)
                    )
                    conn.execute(
                        ne_agent_conversation_turns.insert().values(
                            conversation_id=conversation_id,
                            sequence=sequence,
                            owner_principal_id=owner_principal_id,
                            department=department,
                            agent_name=agent_name,
                            trace_id=trace_id,
                            attachment_id=attachment_id,
                            attempt_id=attempt_id,
                            request_fingerprint=request_fingerprint,
                            execution_fingerprint=None,
                            dispatch_id=None,
                            lease_expires_at=lease_expires_at,
                            private_context="[]",
                            served_output="",
                            citation_refs="[]",
                            served_citations="[]",
                            served_verified="null",
                            served_grounded="null",
                            served_trace_link=False,
                            publication_state="claimed",
                            created_at=now,
                        )
                    )
                    return ConversationAttachmentClaim("owned", attempt_id)
            except IntegrityError:
                if collision == 1:
                    raise
        raise AssertionError("unreachable attachment claim loop")

    async def append_conversation_turn(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        messages: Sequence[ModelMessage],
        *,
        served_output: str,
        trace_id: str,
        attachment_id: str,
        attempt_id: str,
        verified: VerifiedProvenance | None = None,
        grounded: GroundedProvenance | None = None,
        trace_linked: bool = False,
        request_fingerprint: str | None = None,
        idempotency_fingerprint: str | None = None,
        citation_payloads: Sequence[dict[str, str]] = (),
        terminal_audited: bool = True,
    ) -> int:
        if not all(
            value.strip()
            for value in (
                owner_principal_id,
                department,
                agent_name,
                trace_id,
                attachment_id,
                attempt_id,
            )
        ):
            raise ValueError("conversation turn scope and identifiers must be non-empty")
        fingerprint = (
            request_fingerprint or hashlib.sha256(f"store-direct:{attempt_id}".encode()).hexdigest()
        )
        claim = await self.claim_conversation_attachment(
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            attachment_id,
            idempotency_fingerprint or fingerprint,
            trace_id,
            preferred_attempt_id=attempt_id,
        )
        if claim.disposition != "owned" or claim.attempt_id != attempt_id:
            raise LookupError("conversation attachment is not owned by this attempt")
        await self.bind_conversation_execution(
            conversation_id,
            owner_principal_id,
            attachment_id,
            attempt_id,
            fingerprint,
        )
        return await call_db(
            self._offload,
            self._append_conversation_turn_sync,
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            list(messages),
            served_output,
            trace_id,
            attachment_id,
            attempt_id,
            verified,
            grounded,
            trace_linked,
            list(citation_payloads),
            terminal_audited,
        )

    async def prepare_conversation_turn(
        self,
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
    ) -> None:
        """Commit a staged envelope as terminal-audited and ready to publish."""
        await call_db(
            self._offload,
            self._prepare_conversation_turn_sync,
            conversation_id,
            owner_principal_id,
            attachment_id,
            attempt_id,
        )

    def _prepare_conversation_turn_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
    ) -> None:
        with self._engine.begin() as conn:
            result = conn.execute(
                ne_agent_conversation_turns.update()
                .where(
                    ne_agent_conversation_turns.c.conversation_id == conversation_id,
                    ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversation_turns.c.attachment_id == attachment_id,
                    ne_agent_conversation_turns.c.attempt_id == attempt_id,
                    ne_agent_conversation_turns.c.publication_state == "staged",
                )
                .values(publication_state="prepared")
            )
            if result.rowcount != 1:
                current = conn.execute(
                    sa.select(ne_agent_conversation_turns.c.publication_state).where(
                        ne_agent_conversation_turns.c.conversation_id == conversation_id,
                        ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                        ne_agent_conversation_turns.c.attachment_id == attachment_id,
                        ne_agent_conversation_turns.c.attempt_id == attempt_id,
                    )
                ).scalar_one_or_none()
                if current != "prepared":
                    raise LookupError("conversation attachment was not staged by this attempt")

    async def conversation_has_attachment(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
        citation_ref: str | None = None,
    ) -> bool:
        """Return whether an exact owned conversation contains the served attachment."""
        return await call_db(
            self._offload,
            self._conversation_has_attachment_sync,
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            attachment_id,
            citation_ref,
        )

    async def published_conversation_output(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
    ) -> str | None:
        """Return the exact served bytes for an owned successful idempotent turn."""
        return await call_db(
            self._offload,
            self._published_conversation_output_sync,
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            attachment_id,
        )

    async def published_conversation_replay(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
        request_fingerprint: str | None = None,
    ) -> PublishedConversationReplay | None:
        return await call_db(
            self._offload,
            self._published_conversation_replay_sync,
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            attachment_id,
            request_fingerprint,
        )

    async def protected_conversation_turn(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
        attempt_id: str,
    ) -> ProtectedConversationTurn | None:
        """Return only exact source-free protected bindings for one published turn.

        Missing, foreign, partial, legacy-unavailable, and malformed rows all
        collapse to ``None``.  The reader never selects a plaintext column and
        cannot materialize either referenced payload.
        """
        return await call_db(
            self._offload,
            self._protected_conversation_turn_sync,
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            attachment_id,
            attempt_id,
        )

    def _protected_conversation_turn_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
        attempt_id: str,
    ) -> ProtectedConversationTurn | None:
        turn = ne_agent_conversation_turns
        parent = ne_agent_conversations
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    turn.c.conversation_id,
                    turn.c.owner_principal_id,
                    turn.c.sequence,
                    turn.c.attachment_id,
                    turn.c.attempt_id,
                    turn.c.input_status,
                    turn.c.protected_input_ref,
                    turn.c.protected_input_hash,
                    turn.c.terminal_status,
                    turn.c.protected_terminal_ref,
                    turn.c.protected_terminal_hash,
                )
                .join(parent, parent.c.conversation_id == turn.c.conversation_id)
                .where(
                    parent.c.conversation_id == conversation_id,
                    parent.c.owner_principal_id == owner_principal_id,
                    parent.c.department == department,
                    parent.c.agent_name == agent_name,
                    turn.c.owner_principal_id == owner_principal_id,
                    turn.c.department == department,
                    turn.c.agent_name == agent_name,
                    turn.c.attachment_id == attachment_id,
                    turn.c.attempt_id == attempt_id,
                    turn.c.publication_state == "published",
                )
            ).one_or_none()
        if row is None:
            return None
        values = (
            row.protected_input_ref,
            row.protected_input_hash,
            row.protected_terminal_ref,
            row.protected_terminal_hash,
        )
        if (
            row.input_status != "source_free"
            or row.terminal_status != "source_free"
            or not all(isinstance(value, str) and value.strip() for value in values)
            or re.fullmatch(r"[0-9a-f]{64}", str(row.protected_input_hash)) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(row.protected_terminal_hash)) is None
        ):
            return None
        return ProtectedConversationTurn(
            conversation_id=str(row.conversation_id),
            owner_principal_id=str(row.owner_principal_id),
            turn_sequence=int(row.sequence),
            attachment_id=str(row.attachment_id),
            attempt_id=str(row.attempt_id),
            input=ProtectedConversationContentBinding(
                status="source_free",
                protected_ref=str(row.protected_input_ref),
                plaintext_hash=str(row.protected_input_hash),
            ),
            terminal=ProtectedConversationContentBinding(
                status="source_free",
                protected_ref=str(row.protected_terminal_ref),
                plaintext_hash=str(row.protected_terminal_hash),
            ),
        )

    async def protected_conversation_ledger_page(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        *,
        limit: int,
        before_sequence: int | None = None,
    ) -> ProtectedConversationLedgerPage:
        """Enumerate protected bindings within one API-authorized owner scope.

        The API owns its ops-role decision and supplies the exact Conversation
        owner tuple. Missing, foreign, malformed, and incomplete protected rows
        all collapse to an empty page. Pagination is a capped, sequence-descending
        keyset and no plaintext column is selected.
        """
        if limit < 1 or limit > 100:
            raise ValueError("protected Conversation ledger limit must be between 1 and 100")
        scope = (conversation_id, owner_principal_id, department, agent_name)
        if not all(isinstance(value, str) and value.strip() for value in scope) or (
            before_sequence is not None and before_sequence < 1
        ):
            return ProtectedConversationLedgerPage((), None)
        return await call_db(
            self._offload,
            self._protected_conversation_ledger_page_sync,
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            limit,
            before_sequence,
        )

    async def list_protected_conversation_turns(
        self, *, department: str, limit: int
    ) -> ProtectedConversationLedgerPage:
        """Return the governed ops window across all protected conversations.

        Authorization remains an API responsibility, matching
        :meth:`list_conversation_turns`. No plaintext column is selected.
        """
        if limit < 1 or limit > 100:
            raise ValueError("protected Conversation ledger limit must be between 1 and 100")
        if not department.strip():
            return ProtectedConversationLedgerPage((), None)
        return await call_db(
            self._offload, self._list_protected_conversation_turns_sync, department, limit
        )

    def _list_protected_conversation_turns_sync(
        self, department: str, limit: int
    ) -> ProtectedConversationLedgerPage:
        turn = ne_agent_conversation_turns
        parent = ne_agent_conversations
        total = sa.func.count().over().label("total_turns")
        stmt = (
            sa.select(
                turn.c.conversation_id,
                turn.c.owner_principal_id,
                turn.c.department,
                turn.c.agent_name,
                turn.c.sequence,
                turn.c.trace_id,
                turn.c.attachment_id,
                turn.c.attempt_id,
                turn.c.created_at,
                turn.c.input_status,
                turn.c.protected_input_ref,
                turn.c.protected_input_hash,
                turn.c.terminal_status,
                turn.c.protected_terminal_ref,
                turn.c.protected_terminal_hash,
                total,
            )
            .join(parent, parent.c.conversation_id == turn.c.conversation_id)
            .where(
                parent.c.owner_principal_id == turn.c.owner_principal_id,
                parent.c.department == turn.c.department,
                parent.c.agent_name == turn.c.agent_name,
                parent.c.department == department,
                turn.c.publication_state == "published",
            )
            .order_by(turn.c.created_at.desc(), turn.c.conversation_id, turn.c.sequence.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = list(conn.execute(stmt).all())
        projected: list[ProtectedConversationLedgerTurn] = []
        for row in rows:
            input_binding = _protected_conversation_ledger_binding(
                row.input_status,
                row.protected_input_ref,
                row.protected_input_hash,
            )
            terminal_binding = _protected_conversation_ledger_binding(
                row.terminal_status,
                row.protected_terminal_ref,
                row.protected_terminal_hash,
            )
            if input_binding is None or terminal_binding is None:
                continue
            projected.append(
                ProtectedConversationLedgerTurn(
                    conversation_id=str(row.conversation_id),
                    owner_principal_id=str(row.owner_principal_id),
                    department=str(row.department),
                    agent_name=str(row.agent_name),
                    turn_sequence=int(row.sequence),
                    trace_id=str(row.trace_id),
                    attachment_id=str(row.attachment_id),
                    attempt_id=str(row.attempt_id),
                    created_at=_utc(row.created_at),
                    input=input_binding,
                    terminal=terminal_binding,
                )
            )
        return ProtectedConversationLedgerPage(
            turns=tuple(projected),
            next_before_sequence=None,
            total_turns=int(rows[0].total_turns) if rows else 0,
        )

    def _protected_conversation_ledger_page_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        limit: int,
        before_sequence: int | None,
    ) -> ProtectedConversationLedgerPage:
        turn = ne_agent_conversation_turns
        parent = ne_agent_conversations
        predicates = [
            parent.c.conversation_id == conversation_id,
            parent.c.owner_principal_id == owner_principal_id,
            parent.c.department == department,
            parent.c.agent_name == agent_name,
            turn.c.owner_principal_id == owner_principal_id,
            turn.c.department == department,
            turn.c.agent_name == agent_name,
            turn.c.publication_state == "published",
        ]
        if before_sequence is not None:
            predicates.append(turn.c.sequence < before_sequence)
        stmt = (
            sa.select(
                turn.c.conversation_id,
                turn.c.owner_principal_id,
                turn.c.department,
                turn.c.agent_name,
                turn.c.sequence,
                turn.c.trace_id,
                turn.c.attachment_id,
                turn.c.attempt_id,
                turn.c.created_at,
                turn.c.input_status,
                turn.c.protected_input_ref,
                turn.c.protected_input_hash,
                turn.c.terminal_status,
                turn.c.protected_terminal_ref,
                turn.c.protected_terminal_hash,
            )
            .join(parent, parent.c.conversation_id == turn.c.conversation_id)
            .where(*predicates)
            .order_by(turn.c.sequence.desc())
            .limit(limit + 1)
        )
        with self._engine.connect() as conn:
            rows = list(conn.execute(stmt).all())
        selected = rows[:limit]
        projected: list[ProtectedConversationLedgerTurn] = []
        for row in selected:
            input_binding = _protected_conversation_ledger_binding(
                row.input_status,
                row.protected_input_ref,
                row.protected_input_hash,
            )
            terminal_binding = _protected_conversation_ledger_binding(
                row.terminal_status,
                row.protected_terminal_ref,
                row.protected_terminal_hash,
            )
            if input_binding is None or terminal_binding is None:
                return ProtectedConversationLedgerPage((), None)
            projected.append(
                ProtectedConversationLedgerTurn(
                    conversation_id=str(row.conversation_id),
                    owner_principal_id=str(row.owner_principal_id),
                    department=str(row.department),
                    agent_name=str(row.agent_name),
                    turn_sequence=int(row.sequence),
                    trace_id=str(row.trace_id),
                    attachment_id=str(row.attachment_id),
                    attempt_id=str(row.attempt_id),
                    created_at=_utc(row.created_at),
                    input=input_binding,
                    terminal=terminal_binding,
                )
            )
        next_cursor = projected[-1].turn_sequence if len(rows) > limit else None
        return ProtectedConversationLedgerPage(tuple(projected), next_cursor)

    async def prepared_conversation_replay(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
        request_fingerprint: str,
    ) -> PublishedConversationReplay | None:
        return await call_db(
            self._offload,
            self._conversation_replay_sync,
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            attachment_id,
            request_fingerprint,
            "prepared",
        )

    async def staged_conversation_replay(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
        request_fingerprint: str,
    ) -> PublishedConversationReplay | None:
        return await call_db(
            self._offload,
            self._conversation_replay_sync,
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            attachment_id,
            request_fingerprint,
            "staged",
        )

    async def prepared_conversation_attempt_id(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
    ) -> str | None:
        """Return the private nonce for one exact still-prepared worker turn."""
        return await call_db(
            self._offload,
            self._prepared_conversation_attempt_id_sync,
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            attachment_id,
        )

    def _prepared_conversation_attempt_id_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
    ) -> str | None:
        with self._engine.connect() as conn:
            attempt_id = conn.execute(
                sa.select(ne_agent_conversation_turns.c.attempt_id)
                .join(
                    ne_agent_conversations,
                    ne_agent_conversations.c.conversation_id
                    == ne_agent_conversation_turns.c.conversation_id,
                )
                .where(
                    ne_agent_conversations.c.conversation_id == conversation_id,
                    ne_agent_conversations.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversations.c.department == department,
                    ne_agent_conversations.c.agent_name == agent_name,
                    ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversation_turns.c.department == department,
                    ne_agent_conversation_turns.c.agent_name == agent_name,
                    ne_agent_conversation_turns.c.attachment_id == attachment_id,
                    ne_agent_conversation_turns.c.publication_state == "prepared",
                )
            ).scalar_one_or_none()
        return None if attempt_id is None else str(attempt_id)

    def _published_conversation_output_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
    ) -> str | None:
        replay = self._published_conversation_replay_sync(
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            attachment_id,
        )
        return None if replay is None else replay.output

    def _published_conversation_replay_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
        request_fingerprint: str | None = None,
    ) -> PublishedConversationReplay | None:
        return self._conversation_replay_sync(
            conversation_id,
            owner_principal_id,
            department,
            agent_name,
            attachment_id,
            request_fingerprint,
            "published",
        )

    def _conversation_replay_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
        request_fingerprint: str | None,
        required_state: str,
    ) -> PublishedConversationReplay | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    ne_agent_conversation_turns.c.served_output,
                    ne_agent_conversation_turns.c.served_citations,
                    ne_agent_conversation_turns.c.served_verified,
                    ne_agent_conversation_turns.c.served_grounded,
                    ne_agent_conversation_turns.c.served_trace_link,
                    ne_agent_conversation_turns.c.request_fingerprint,
                    ne_agent_conversation_turns.c.publication_state,
                )
                .join(
                    ne_agent_conversations,
                    ne_agent_conversations.c.conversation_id
                    == ne_agent_conversation_turns.c.conversation_id,
                )
                .where(
                    ne_agent_conversations.c.conversation_id == conversation_id,
                    ne_agent_conversations.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversations.c.department == department,
                    ne_agent_conversations.c.agent_name == agent_name,
                    ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversation_turns.c.department == department,
                    ne_agent_conversation_turns.c.agent_name == agent_name,
                    ne_agent_conversation_turns.c.attachment_id == attachment_id,
                )
            ).first()
        if row is None:
            return None
        if request_fingerprint is not None:
            stored_fingerprint = str(row.request_fingerprint)
            if (
                re.fullmatch(r"[0-9a-f]{64}", request_fingerprint) is None
                or re.fullmatch(r"[0-9a-f]{64}", stored_fingerprint) is None
                or not hmac.compare_digest(stored_fingerprint, request_fingerprint)
            ):
                raise AttachmentRequestMismatchError(
                    "idempotency attachment does not match its original request"
                )
        if str(row.publication_state) != required_state:
            return None
        raw_citations = json.loads(str(row.served_citations))
        if not isinstance(raw_citations, list) or not all(
            isinstance(payload, dict)
            and all(
                isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
            )
            for payload in raw_citations
        ):
            raise RuntimeError("stored conversation replay citations are malformed")
        return PublishedConversationReplay(
            output=str(row.served_output),
            citations=tuple(dict(payload) for payload in raw_citations),
            verified=(
                None
                if str(row.served_verified) == "null"
                else VerifiedProvenance.model_validate_json(str(row.served_verified))
            ),
            grounded=(
                None
                if str(row.served_grounded) == "null"
                else GroundedProvenance.model_validate_json(str(row.served_grounded))
            ),
            trace_linked=bool(row.served_trace_link),
        )

    def _conversation_has_attachment_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        attachment_id: str,
        citation_ref: str | None,
    ) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    ne_agent_conversation_turns.c.id,
                    ne_agent_conversation_turns.c.citation_refs,
                )
                .join(
                    ne_agent_conversations,
                    ne_agent_conversations.c.conversation_id
                    == ne_agent_conversation_turns.c.conversation_id,
                )
                .where(
                    ne_agent_conversations.c.conversation_id == conversation_id,
                    ne_agent_conversations.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversations.c.department == department,
                    ne_agent_conversations.c.agent_name == agent_name,
                    ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversation_turns.c.department == department,
                    ne_agent_conversation_turns.c.agent_name == agent_name,
                    ne_agent_conversation_turns.c.attachment_id == attachment_id,
                    ne_agent_conversation_turns.c.publication_state == "published",
                )
                .limit(1)
            ).first()
            if row is None:
                return False
            if citation_ref is None:
                return True
            refs = json.loads(str(row.citation_refs))
            return isinstance(refs, list) and citation_ref in refs

    def _append_conversation_turn_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        department: str,
        agent_name: str,
        messages: Sequence[ModelMessage],
        served_output: str,
        trace_id: str,
        attachment_id: str,
        attempt_id: str,
        verified: VerifiedProvenance | None,
        grounded: GroundedProvenance | None,
        trace_linked: bool,
        citation_payloads: list[dict[str, str]],
        terminal_audited: bool,
    ) -> int:
        payload = ModelMessagesTypeAdapter.dump_json(list(messages)).decode("utf-8")
        refs = sorted(
            {
                str(citation["chunk_id"])
                for citation in citation_payloads
                if str(citation.get("chunk_id", "")).strip()
            }
        )
        serialized_citations = json.dumps(citation_payloads, sort_keys=True, separators=(",", ":"))
        with self._engine.begin() as conn:
            prepared = conn.execute(
                ne_agent_conversation_turns.update()
                .where(
                    ne_agent_conversation_turns.c.conversation_id == conversation_id,
                    ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversation_turns.c.department == department,
                    ne_agent_conversation_turns.c.agent_name == agent_name,
                    ne_agent_conversation_turns.c.attachment_id == attachment_id,
                    ne_agent_conversation_turns.c.attempt_id == attempt_id,
                    ne_agent_conversation_turns.c.publication_state == "claimed",
                )
                .values(
                    private_context=payload,
                    served_output=served_output,
                    citation_refs=json.dumps(refs, separators=(",", ":")),
                    served_citations=serialized_citations,
                    served_verified=("null" if verified is None else verified.model_dump_json()),
                    served_grounded=("null" if grounded is None else grounded.model_dump_json()),
                    served_trace_link=trace_linked or verified is not None or grounded is not None,
                    publication_state="prepared" if terminal_audited else "staged",
                )
            )
            if prepared.rowcount != 1:
                raise LookupError("conversation attachment claim is not executable")
            sequence = conn.execute(
                sa.select(ne_agent_conversation_turns.c.sequence).where(
                    ne_agent_conversation_turns.c.conversation_id == conversation_id,
                    ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversation_turns.c.attachment_id == attachment_id,
                    ne_agent_conversation_turns.c.attempt_id == attempt_id,
                )
            ).scalar_one()
        return int(sequence)

    async def publish_conversation_turn(
        self,
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
        citation_refs: Sequence[str],
        citation_payloads: Sequence[dict[str, str]] = (),
    ) -> None:
        refs = sorted({ref for ref in citation_refs if ref.strip()})
        payloads = json.dumps(list(citation_payloads), sort_keys=True, separators=(",", ":"))
        cancelled = threading.Event()
        try:
            await call_db(
                self._offload,
                self._set_conversation_turn_state_sync,
                conversation_id,
                owner_principal_id,
                attachment_id,
                attempt_id,
                "published",
                json.dumps(refs, separators=(",", ":")),
                payloads,
                cancelled,
            )
        except asyncio.CancelledError:
            # ``asyncio.to_thread`` cancellation cannot stop a statement already
            # queued on its worker. Publish therefore has an explicit in-process
            # cancellation tombstone checked inside the transaction. A cancelled
            # caller that has not crossed the DB publication point can never leave
            # a visible turn when the worker resumes.
            cancelled.set()
            raise

    async def abort_conversation_turn(
        self,
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
    ) -> None:
        await call_db(
            self._offload,
            self._set_conversation_turn_state_sync,
            conversation_id,
            owner_principal_id,
            attachment_id,
            attempt_id,
            "aborted",
            None,
            None,
            None,
        )

    async def release_prepared_conversation_turn(
        self,
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
    ) -> None:
        """Release only an exact prepared epoch while preserving its envelope.

        Terminal-audited work is monotonic: transport/publication failure may
        relinquish the lease so another process can adopt it, but may never
        rewrite it to ``aborted`` and force model/tool re-execution.
        """
        await call_db(
            self._offload,
            self._release_prepared_conversation_turn_sync,
            conversation_id,
            owner_principal_id,
            attachment_id,
            attempt_id,
        )

    def _release_prepared_conversation_turn_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
    ) -> None:
        with self._engine.begin() as conn:
            result = conn.execute(
                ne_agent_conversation_turns.update()
                .where(
                    ne_agent_conversation_turns.c.conversation_id == conversation_id,
                    ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                    ne_agent_conversation_turns.c.attachment_id == attachment_id,
                    ne_agent_conversation_turns.c.attempt_id == attempt_id,
                    ne_agent_conversation_turns.c.publication_state == "prepared",
                )
                .values(lease_expires_at=_database_now(conn))
            )
            if result.rowcount != 1:
                current = conn.execute(
                    sa.select(
                        ne_agent_conversation_turns.c.attempt_id,
                        ne_agent_conversation_turns.c.publication_state,
                    ).where(
                        ne_agent_conversation_turns.c.conversation_id == conversation_id,
                        ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                        ne_agent_conversation_turns.c.attachment_id == attachment_id,
                    )
                ).first()
                if current is not None and str(current.publication_state) == "published":
                    return
                raise LookupError("conversation prepared attempt is stale")

    def _set_conversation_turn_state_sync(
        self,
        conversation_id: str,
        owner_principal_id: str,
        attachment_id: str,
        attempt_id: str,
        state: str,
        citation_refs: str | None,
        citation_payloads: str | None,
        publish_cancelled: threading.Event | None,
    ) -> None:
        with self._engine.begin() as conn:
            scope = (
                ne_agent_conversation_turns.c.conversation_id == conversation_id,
                ne_agent_conversation_turns.c.owner_principal_id == owner_principal_id,
                ne_agent_conversation_turns.c.attachment_id == attachment_id,
                ne_agent_conversation_turns.c.attempt_id == attempt_id,
            )
            current = conn.execute(
                sa.select(ne_agent_conversation_turns.c.publication_state)
                .where(*scope)
                .with_for_update()
            ).scalar_one_or_none()
            if current is None:
                raise LookupError("unknown prepared conversation turn")
            if current == "aborted":
                if state == "aborted":
                    return
                raise LookupError("unknown prepared conversation turn")
            # Retrying an already-published turn is an exact replay. In particular,
            # an empty citation capture on the replay must not erase the terminal
            # citation membership minted by the original successful execution.
            if state == "published" and current == "published":
                return
            if state == "aborted" and current == "published":
                return
            if (
                state == "published"
                and publish_cancelled is not None
                and publish_cancelled.is_set()
            ):
                # This event is the cancellation linearization point for an
                # offloaded blocking DB body. Once this check passes, the
                # publication transaction owns completion and a retry can recover
                # its durable terminal envelope even if the caller disappears.
                result = conn.execute(
                    ne_agent_conversation_turns.update()
                    .where(
                        *scope,
                        ne_agent_conversation_turns.c.publication_state == "prepared",
                    )
                    .values(lease_expires_at=_database_now(conn))
                )
                if result.rowcount != 1:
                    raise LookupError("unknown prepared conversation turn")
                return
            values: dict[str, object] = {"publication_state": state}
            if citation_refs is not None:
                values["citation_refs"] = citation_refs
            if citation_payloads is not None:
                values["served_citations"] = citation_payloads
            if state == "aborted" and current == "prepared":
                conn.execute(
                    ne_agent_conversation_turns.update()
                    .where(*scope, ne_agent_conversation_turns.c.publication_state == "prepared")
                    .values(lease_expires_at=_database_now(conn))
                )
                return
            allowed_states = ("claimed", "staged") if state == "aborted" else ("prepared",)
            result = conn.execute(
                ne_agent_conversation_turns.update()
                .where(
                    *scope,
                    ne_agent_conversation_turns.c.publication_state.in_(allowed_states),
                )
                .values(**values)
            )
            if result.rowcount != 1:
                latest = conn.execute(
                    sa.select(ne_agent_conversation_turns.c.publication_state).where(*scope)
                ).scalar_one_or_none()
                if state == "aborted" and latest in {"aborted", "published"}:
                    return
                if state == "published" and latest == "published":
                    return
                raise LookupError("unknown prepared conversation turn")

    async def open_session(self, agent_name: str, department: str) -> str:
        """Return the existing (agent, department) session id, or create one (uuid4).

        Race-free under concurrent multi-worker Postgres (NE-070H): a speculative
        ``INSERT ... ON CONFLICT DO NOTHING`` on the ``(agent_name, department)``
        unique constraint, then a read of the winning row. On Postgres the
        speculative insert blocks on an in-flight sibling until it commits, then
        DO NOTHING + the read-committed SELECT sees the winner, so both callers get
        the SAME id and exactly one row exists. SQLite's writer lock serializes the
        same sequence.
        """
        return await call_db(self._offload, self._open_session_sync, agent_name, department)

    def _open_session_sync(self, agent_name: str, department: str) -> str:
        with self._engine.begin() as conn:
            conn.execute(
                _insert_ignore(
                    ne_agent_sessions, conn, index_elements=["agent_name", "department"]
                ).values(
                    session_id=str(uuid.uuid4()),
                    agent_name=agent_name,
                    department=department,
                    created_at=datetime.now(UTC),
                )
            )
            winner = conn.execute(
                sa.select(ne_agent_sessions.c.session_id).where(
                    ne_agent_sessions.c.agent_name == agent_name,
                    ne_agent_sessions.c.department == department,
                )
            ).first()
            if winner is None:  # pragma: no cover - insert-or-existing guarantees a row
                raise RuntimeError(
                    f"open_session found no row for ({agent_name!r}, {department!r}) after upsert"
                )
            return str(winner.session_id)

    async def load_history(
        self, session_id: str, *, limit: int | None = None
    ) -> list[ModelMessage]:
        """Concatenate this session's turn batches into a chronological message list.

        With ``limit is None`` the full log is read in ascending ``seq`` order
        (back-compat). With ``limit >= 1`` only the most-recent ``limit`` batches are
        fetched via ``ORDER BY seq DESC LIMIT N`` and then RE-REVERSED to chronological
        order — so the SQL itself caps the rows loaded (never a post-hoc slice of an
        unbounded read) and the model still receives the batches oldest-first (P1-3).
        """
        return await call_db(self._offload, self._load_history_sync, session_id, limit)

    def _load_history_sync(self, session_id: str, limit: int | None) -> list[ModelMessage]:
        stmt = sa.select(ne_agent_session_messages.c.payload).where(
            ne_agent_session_messages.c.session_id == session_id
        )
        with self._engine.connect() as conn:
            if limit is None:
                rows = list(conn.execute(stmt.order_by(ne_agent_session_messages.c.seq)).all())
            else:
                # Fetch the most-recent-N by seq DESC (the SQL LIMIT caps the rows),
                # then reverse to chronological so the model sees oldest-first.
                rows = list(
                    reversed(
                        conn.execute(
                            stmt.order_by(ne_agent_session_messages.c.seq.desc()).limit(limit)
                        ).all()
                    )
                )
        history: list[ModelMessage] = []
        for row in rows:
            history.extend(ModelMessagesTypeAdapter.validate_json(row.payload))
        return history

    async def context_snapshot(
        self,
        session_id: str,
        *,
        max_components: int,
        prepared_attachment_id: str | None = None,
    ) -> SessionContextSnapshot | None:
        """Return a content-free identity snapshot of a session's active context.

        ``max_components`` is the SAME most-recent-turn bound the runtime uses
        when loading history. No serialized message payload is returned; it is
        consumed only while forming the component hash.

        A missing session returns ``None`` so the API can preserve its existing
        foreign/unknown-session 404 without exposing whether another department
        owns the identifier.
        """
        if max_components < 1:
            raise ValueError("max_components must be at least 1")
        return await call_db(
            self._offload,
            self._context_snapshot_sync,
            session_id,
            max_components,
            prepared_attachment_id,
        )

    def _context_snapshot_sync(
        self,
        session_id: str,
        max_components: int,
        prepared_attachment_id: str | None,
    ) -> SessionContextSnapshot | None:
        with self._engine.connect() as conn:
            conversation = conn.execute(
                sa.select(
                    ne_agent_conversations.c.created_at,
                    ne_agent_conversations.c.owner_principal_id,
                    ne_agent_conversations.c.department,
                    ne_agent_conversations.c.agent_name,
                ).where(ne_agent_conversations.c.conversation_id == session_id)
            ).first()
            if conversation is not None:
                session_created_at = _utc(conversation.created_at)
                publication_predicate: Any = (
                    ne_agent_conversation_turns.c.publication_state == "published"
                )
                if prepared_attachment_id is not None:
                    publication_predicate = sa.or_(
                        publication_predicate,
                        sa.and_(
                            ne_agent_conversation_turns.c.publication_state.in_(
                                ("claimed", "staged", "prepared")
                            ),
                            ne_agent_conversation_turns.c.attachment_id == prepared_attachment_id,
                        ),
                    )
                newest = list(
                    conn.execute(
                        sa.select(
                            ne_agent_conversation_turns.c.sequence.label("seq"),
                            ne_agent_conversation_turns.c.private_context.label("payload"),
                            ne_agent_conversation_turns.c.created_at,
                            sa.func.count().over().label("total_components"),
                        )
                        .where(
                            ne_agent_conversation_turns.c.conversation_id == session_id,
                            ne_agent_conversation_turns.c.owner_principal_id
                            == conversation.owner_principal_id,
                            ne_agent_conversation_turns.c.department == conversation.department,
                            ne_agent_conversation_turns.c.agent_name == conversation.agent_name,
                            publication_predicate,
                        )
                        .order_by(ne_agent_conversation_turns.c.sequence.desc())
                        .limit(max_components)
                    ).all()
                )
                total = int(newest[0].total_components) if newest else 0
            else:
                session = conn.execute(
                    sa.select(ne_agent_sessions.c.created_at).where(
                        ne_agent_sessions.c.session_id == session_id
                    )
                ).first()
                if session is None:
                    return None
                session_created_at = _utc(session.created_at)
                predicate = ne_agent_session_messages.c.session_id == session_id
                newest = list(
                    conn.execute(
                        sa.select(
                            ne_agent_session_messages.c.seq,
                            ne_agent_session_messages.c.payload,
                            ne_agent_session_messages.c.created_at,
                            sa.func.count().over().label("total_components"),
                        )
                        .where(predicate)
                        .order_by(ne_agent_session_messages.c.seq.desc())
                        .limit(max_components)
                    ).all()
                )
                total = int(newest[0].total_components) if newest else 0

        components = tuple(
            SessionContextComponent(
                seq=int(row.seq),
                component_id=_component_id(session_id, int(row.seq), str(row.payload)),
                created_at=_utc(row.created_at),
            )
            for row in reversed(newest)
        )
        return SessionContextSnapshot(
            session_id=session_id,
            session_created_at=session_created_at,
            context_id=_context_id(session_id, max_components, components),
            components=components,
            total_components=total,
            truncated=total > len(components),
        )

    async def reset_history(self, session_id: str) -> None:
        """Clear this session's message log AND its seq counter in ONE transaction.

        The governed ops reset path (replacing manual DB surgery) for P1-3: it deletes
        every ``ne_agent_session_messages`` row and the ``ne_agent_session_seq`` counter
        row for ``session_id`` so a subsequent ``append_turn`` re-seeds the counter and
        restarts at seq 1. The ``ne_agent_sessions`` row (and thus the session id)
        survives, so ``open_session`` still returns the same id (console/API exposure of
        this path is a follow-up).
        """
        await call_db(self._offload, self._reset_history_sync, session_id)

    def _reset_history_sync(self, session_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.delete(ne_agent_session_messages).where(
                    ne_agent_session_messages.c.session_id == session_id
                )
            )
            conn.execute(
                sa.delete(ne_agent_session_seq).where(
                    ne_agent_session_seq.c.session_id == session_id
                )
            )

    async def list_conversation_turns(self, *, limit: int) -> ConversationTurnPage:
        """Return complete retained turn batches, newest first.

        This is the storage half of the governed review surface.  It performs no
        authorization and returns no partial batch; ne-api owns the ops-role gate,
        disclosure audit, and total serialized-response byte ceiling.  Corrupt
        stored JSON fails loudly before anything can be disclosed.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        return await call_db(self._offload, self._list_conversation_turns_sync, limit)

    def _list_conversation_turns_sync(self, limit: int) -> ConversationTurnPage:
        total = sa.func.count().over().label("total_turns")
        stmt = (
            sa.select(
                ne_agent_session_messages.c.session_id,
                ne_agent_sessions.c.agent_name,
                ne_agent_sessions.c.department,
                ne_agent_session_messages.c.seq,
                ne_agent_session_messages.c.principal_id,
                ne_agent_session_messages.c.trace_id,
                ne_agent_session_messages.c.payload,
                ne_agent_session_messages.c.created_at,
                total,
            )
            .join(
                ne_agent_sessions,
                ne_agent_sessions.c.session_id == ne_agent_session_messages.c.session_id,
            )
            .order_by(ne_agent_session_messages.c.id.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = list(conn.execute(stmt).all())

        turns: list[ConversationTurn] = []
        for row in rows:
            messages = json.loads(str(row.payload))
            if not isinstance(messages, list) or not all(
                isinstance(message, dict) for message in messages
            ):
                raise ValueError("retained conversation payload is not a JSON message list")
            turns.append(
                ConversationTurn(
                    session_id=str(row.session_id),
                    agent_name=str(row.agent_name),
                    department=str(row.department),
                    seq=int(row.seq),
                    principal_id=(str(row.principal_id) if row.principal_id is not None else None),
                    trace_id=str(row.trace_id) if row.trace_id is not None else None,
                    messages=tuple(messages),
                    created_at=_utc(row.created_at),
                )
            )
        return ConversationTurnPage(
            turns=tuple(turns),
            total_turns=int(rows[0].total_turns) if rows else 0,
        )

    async def append_turn(
        self,
        session_id: str,
        messages: Sequence[ModelMessage],
        *,
        principal_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        """Append one turn's new messages as the next ``seq`` batch for this session.

        The ``seq`` is CLAIMED from the ``ne_agent_session_seq`` counter via an
        atomic ``UPDATE ... last_seq = last_seq + 1`` under the row lock (NE-070H),
        so concurrent multi-worker appenders to ONE session get GAPLESS, CONTIGUOUS,
        UNIQUE seq values. The counter is seeded once from the legacy ``max(seq)``
        (upgrade-safe; a fresh session starts at 1). The Postgres row lock (and
        SQLite's writer lock) serializes concurrent claimants — no RETURNING needed.
        """
        if (principal_id is None) != (trace_id is None):
            raise ValueError("principal_id and trace_id must be supplied together")
        if principal_id is not None and (
            not principal_id.strip() or not trace_id or not trace_id.strip()
        ):
            raise ValueError("principal_id and trace_id must be non-empty")
        await call_db(
            self._offload,
            self._append_turn_sync,
            session_id,
            list(messages),
            principal_id,
            trace_id,
        )

    def _append_turn_sync(
        self,
        session_id: str,
        messages: Sequence[ModelMessage],
        principal_id: str | None,
        trace_id: str | None,
    ) -> None:
        payload = ModelMessagesTypeAdapter.dump_json(list(messages)).decode("utf-8")
        with self._engine.begin() as conn:
            # (i) Seed the counter idempotently from the legacy max(seq) (once).
            conn.execute(
                _insert_ignore(
                    ne_agent_session_seq, conn, index_elements=["session_id"]
                ).from_select(
                    ["session_id", "last_seq"],
                    sa.select(
                        sa.literal(session_id),
                        sa.func.coalesce(sa.func.max(ne_agent_session_messages.c.seq), 0),
                    ).where(ne_agent_session_messages.c.session_id == session_id),
                )
            )
            # (ii) Atomically claim the next seq under the counter row lock.
            result = conn.execute(
                ne_agent_session_seq.update()
                .where(ne_agent_session_seq.c.session_id == session_id)
                .values(last_seq=ne_agent_session_seq.c.last_seq + 1)
            )
            if result.rowcount != 1:  # pragma: no cover - the seed above guarantees a row
                raise RuntimeError(
                    f"append_turn failed to claim a seq for session {session_id!r} "
                    f"(counter UPDATE matched {result.rowcount} rows)"
                )
            # (iii) Read the claimed seq on the SAME connection (serialized by the lock).
            next_seq = conn.execute(
                sa.select(ne_agent_session_seq.c.last_seq).where(
                    ne_agent_session_seq.c.session_id == session_id
                )
            ).scalar_one()
            # (iv) Insert the message with the claimed seq.
            conn.execute(
                ne_agent_session_messages.insert().values(
                    session_id=session_id,
                    seq=int(next_seq),
                    principal_id=principal_id,
                    trace_id=trace_id,
                    payload=payload,
                    created_at=datetime.now(UTC),
                )
            )
