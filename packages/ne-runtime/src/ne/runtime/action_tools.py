# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The governed write-back ActionTool framework (NE-050) — the write path, first-class.

THE INVARIANT (C6 no-bypass ON WRITES, C7 write path): a write-back action can NEVER
reach a source system except through
:meth:`~ne.runtime.middleware.GovernedToolExecutor.execute`. Each action is an
ordinary :class:`~ne.runtime.tools.ToolBinding` held ONLY by the executor, so a
source-system write happens only AFTER guardrails → PDP → typed-input pre-gate
validation → stricter-of tier gating → the NE-035 human approval gate → the
config_hash-stamped audit event. The :class:`GovernedAction` facade holds NO
``perform`` callable and NO connector client — its ``execute`` can only delegate to
the executor. The connector client is reachable ONLY from inside the governed
handler closure built by :func:`build_action_binding` (``.perform(`` appears in
exactly this module — a structural guardian scans for it).

NE-ACTION-VALIDATE-BEFORE-GATE (typed input validated BEFORE the human decision):
the binding's ``validate_args`` (built by :func:`build_action_binding`) runs INSIDE
:meth:`GovernedToolExecutor.execute`, AFTER guardrails + PDP but BEFORE the human
approval gate. A call whose typed input fails validation — a missing required field,
an undeclared key, a missing idempotency identity — is refused THERE: audited as
``tool.validate[denied]``, never queued as a pending approval, never consuming a
human decision. The handler below re-validates the identical way (defense in depth,
audited ``tool.execute[error]`` if it were ever reached with bad input). Both call
sites derive from the SAME :func:`_validate_input` / :func:`_dedup_key_for` helpers —
one implementation, not two (PR #377's derive-don't-transliterate lesson) — pinned by
``packages/ne-runtime/tests/test_action_validate_before_gate.py``.

A source-system WRITE is inherently privileged, so the framework imposes a HARD tier
FLOOR: every action declares tier >= 2 (writes are never below Tier-2), and a
destructive / customer-visible write (Tier-3) blocks on a named approver plus a
distinct second check via the real :class:`~ne.runtime.approvals.DurableApprovalGate`.
The declared tier may only ESCALATE the action's Rego-mirrored class tier
(:func:`~ne.runtime.actions.tier_for_action`), never relax it — enforced at
construction (:class:`ActionDefinition`), fail-loud, before any decision.

Idempotency (A6, NE-ACTION-DETERMINISTIC-REQID): dedup keys on ONE of two mutually
exclusive identities, both feeding the same ATOMIC claim-then-write store:

* **Explicit caller-chosen id** (``request_id`` present) — the Stripe-style contract:
  a programmatic caller supplies its own idempotency key and MUST reuse it on retry;
  the key is ``sha256(f"{action}:{request_id}")``, byte-identical regardless of the
  ambient :func:`~ne.runtime.correlation.action_scope`, so an app-level retry from a
  different turn still dedups (test_explicit_request_id_ignores_ambient_scope).
* **Derived turn-scoped identity** (``request_id`` absent — the agentic path) — the
  key is ``sha256(f"{action}:derived:{scope}:{payload_hash}")`` where ``scope`` is the
  ambient :func:`~ne.runtime.correlation.current_action_scope` (the DBOS-stable
  ``turn_id``) and ``payload_hash`` is the canonical input hash. Model-facing tool
  schemas no longer advertise ``request_id`` at all, so a re-prompted model dedups
  regardless of what id (if any) it would have minted
  (test_step_retry_with_fresh_model_id_dedups); a different turn's identical payload
  performs a real second write, closing the cross-turn over-dedup bonus finding
  (test_no_cross_scope_over_dedup_on_derived_path); no ``request_id`` AND no ambient
  scope fails LOUD with :class:`~ne.runtime.errors.ActionValidationError` before any
  claim (test_no_request_id_outside_scope_fails_loud) — a governed write always needs
  a stable idempotency identity.

Both namespaces share the identical INSERT-first dedup store below; the two prefixes
(bare ``action:id`` vs ``action:derived:scope:hash``) cannot collide because ``:``
falls outside the ``request_id`` character class. Dedup is an ATOMIC claim-then-write
(NOT read-then-write): the governed handler reserves the key with a single unique-PK
INSERT *before* touching the source system, so only the caller who wins the
primary-key race performs the write — a concurrent same-key call loses the INSERT and
is turned away BEFORE ``perform`` (this closes the read-then-write TOCTOU where two
simultaneous retries could both write). A settled retry with the SAME key + payload
returns ``detail='deduplicated'`` (the connector client is never re-called); a key
reused with a DIFFERENT payload — or one whose first write is still in flight — is a
typed :class:`~ne.runtime.errors.ActionIdempotencyError` (fail closed, no second
write). The claim boundary is deliberately NON-transactional across the HTTP seam: a
crash BETWEEN the source write and ``finalize`` leaves a PENDING claim, so a later
retry is turned away as in-flight and the write is NOT repeated (crash-safe on this
side; the un-finalized row is a janitor-cleanup concern) — documented honestly
(C7-honest) rather than papered over with a cross-seam transaction. A Tier-3 retry
still re-enters the approval gate (dedup does not skip governance) — conservative by
design.

**Honest residual (not closed by this design):** payload DRIFT on re-prompt — a retry
that produces slightly different content (e.g. a rephrased email body) — changes
``payload_hash`` and therefore the derived key, so it is NOT deduplicated: it reaches
a second human approval and can write a second time. No idempotency layer can equate
two semantically-similar-but-byte-different writes; the guarantee this design
delivers is "same turn + same action + same bytes never writes twice, no matter what
id the model does or doesn't invent." Full closure needs per-tool-call step
decomposition (tracked as ``ne103c`` §10-Q3 future work) — out of scope here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError

from ne.audit.engine import call_db, default_offload
from ne.contracts.integration import ActionResult
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.actions import tier_for_action
from ne.runtime.correlation import current_action_scope
from ne.runtime.errors import (
    ActionIdempotencyError,
    ActionValidationError,
    ToolRegistrationError,
)
from ne.runtime.tools import ToolBinding

__all__ = [
    "ActionDedupStore",
    "ActionDefinition",
    "ActionInput",
    "GovernedAction",
    "build_action_binding",
    "build_action_bindings",
    "create_governed_actions",
    "ensure_action_dedup_schema",
    "ne_action_dedup",
]


class ActionInput(BaseModel):
    """Base typed input for every write-back action (frozen, reject-by-default).

    ``model_config`` is frozen + ``extra="forbid"`` so an undeclared key fails
    validation (C12): a prompt-injected model cannot smuggle an extra field past the
    typed contract. ``request_id`` is OPTIONAL (NE-ACTION-DETERMINISTIC-REQID): when a
    caller explicitly supplies it, retries MUST reuse it (Stripe-style, byte-identical
    dedup key — see the module docstring); when absent, the dedup key is instead
    DERIVED from the ambient action idempotency scope + payload hash, so no caller
    (model or otherwise) is required to mint one. Model-facing reference schemas
    (``reference_actions/``) drop this field from ``input_schema`` entirely — a model
    is never invited to choose an id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")


#: The typed source-system write coroutine: validated input in, ActionResult out.
ActionPerform = Callable[[Any], Awaitable[ActionResult]]
#: Binding-style extractors (same shapes as ToolBinding's), all optional.
_SeqExtractor = Callable[[Mapping[str, object]], Sequence[str]]
_CostExtractor = Callable[[Mapping[str, object]], str | None]


@dataclass(frozen=True)
class ActionDefinition:
    """A typed, tier-declared, governed write-back action (registration-validated).

    ``spec`` carries the action name + declared tier; ``source_system`` names the
    target (``"salesforce"`` / ``"sql"`` / ``"m365"``); ``input_model`` is the frozen
    :class:`ActionInput` subclass; ``perform`` is the ONLY callable that touches the
    injected connector client — it runs solely inside the governed handler closure.

    ``__post_init__`` fails LOUD (before any decision) unless:

    * ``tier_for_action(spec.name)`` classifies the name (deny-by-default mirror of the
      Rego) AND ``int(spec.tier) >= int(tier_for_action(spec.name))`` — the declared
      tier may only ESCALATE the class tier, never relax it;
    * ``source_system.strip()`` is non-empty.

    NE-CS-SEND (owner decision 2026-08-27) RELAXED the old absolute
    ``int(spec.tier) >= 2`` write floor to the class-tier floor above. The old
    floor pre-dated any autonomous write tier actually being wanted; the owner's
    tier story is now SPEC §4.2's own ladder — ``draft.*`` autonomous (Tier-0),
    ``write.internal.*`` autonomous + digest (Tier-1), ``write.external.*``
    per-item human approval (Tier-2) — and an absolute Tier-2 floor made the
    first two rungs unreachable. The class floor is still deny-by-default (an
    unclassifiable name raises) and still one-directional: a declared tier can
    only escalate its class, so ``write.external.*`` can never be declared below
    Tier-2 by ANY caller. ``test_action_write_guardian`` pins each reference
    action's exact tier deliberately.
    """

    spec: ToolSpec
    source_system: str
    input_model: type[ActionInput]
    perform: ActionPerform
    extract_recipients: _SeqExtractor | None = None
    extract_egress_urls: _SeqExtractor | None = None
    estimate_cost_usd: _CostExtractor | None = None

    def __post_init__(self) -> None:
        class_tier = tier_for_action(self.spec.name)  # raises if unclassifiable
        if int(self.spec.tier) < int(class_tier):
            raise ToolRegistrationError(
                f"write action {self.spec.name!r} declares tier {int(self.spec.tier)} but its "
                f"action class requires at least tier {int(class_tier)} (declared tier may only "
                "escalate, never relax, the class tier)"
            )
        if not self.source_system.strip():
            raise ToolRegistrationError(
                f"write action {self.spec.name!r} must declare a non-empty source_system"
            )


# --- POST-governance idempotency / dedup store ------------------------------------

action_dedup_metadata = sa.MetaData()

ne_action_dedup = sa.Table(
    "ne_action_dedup",
    action_dedup_metadata,
    # dedup_key = sha256(action:request_id) hex — one row per (action, request_id).
    sa.Column("dedup_key", sa.String, primary_key=True),
    sa.Column("action", sa.String, nullable=False),
    # payload_hash detects a request_id reused with a DIFFERENT payload (integrity bug).
    sa.Column("payload_hash", sa.String, nullable=False),
    # The ORIGINAL successful ActionResult JSON (never any raw request payload values).
    sa.Column("result_json", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


def ensure_action_dedup_schema(engine: sa.Engine) -> None:
    """Create the ``ne_action_dedup`` table (idempotent)."""
    action_dedup_metadata.create_all(engine)


#: Sentinel stored in ``result_json`` for a CLAIMED-but-not-yet-finished row (an
#: in-flight write). A real :class:`ActionResult` JSON is never empty, so this is an
#: unambiguous "pending" marker.
_DEDUP_PENDING = ""

#: Bounded INSERT retries in :meth:`ActionDedupStore.claim` when a losing INSERT's
#: post-IntegrityError re-read finds NO row (the winner raced a concurrent
#: :meth:`ActionDedupStore.release`). Bounded so pathological claim/release
#: contention raises a typed error instead of spinning forever (P2-12).
_CLAIM_MAX_ATTEMPTS = 5


class ActionDedupStore:
    """Durable POST-governance dedup over sync SQLAlchemy-Core (approval_store.py style).

    Async methods make direct sync calls on short-lived transactions (no async DB
    driver), so SQLite (CI) and Postgres (prod) behave identically.

    Dedup is a two-phase **atomic claim → finalize** (NOT read-then-write): the handler
    first :meth:`claim` s the ``dedup_key`` with a single unique-PK INSERT of a PENDING
    row *before* touching the source system. Only the caller whose INSERT wins the
    primary-key race owns the write; a concurrent (or prior) caller loses the INSERT,
    re-reads the row, and is turned away WITHOUT a second source write. This closes the
    read-then-write TOCTOU where two same-``request_id`` calls could both pass a plain
    ``get()`` and both write. The INSERT/re-read is catch-``IntegrityError``-and-reread
    (portable across SQLite + Postgres) rather than a dialect-specific upsert.

    Only the ORIGINAL successful result JSON is stored (via :meth:`finalize`) — never any
    raw request payload values. A claim whose write does not succeed is :meth:`release` d
    so a later retry can re-claim (the source API remains the transaction boundary).

    NE-050 exactly-once (P2-12): :meth:`claim` NEVER reports ownership without a
    PERSISTED PENDING row. When a losing INSERT's re-read finds no row (the winner
    raced a concurrent :meth:`release`), claim RETRIES the INSERT (bounded by
    :data:`_CLAIM_MAX_ATTEMPTS`) rather than returning ``None`` — which the handler
    would misread as 'we own the claim' though no row exists, letting a third caller
    perform a second source write. :meth:`finalize` asserts it matched exactly one
    row and raises if the PENDING claim vanished, so a later retry cannot re-perform
    a write that was silently un-recorded. All DB bodies route through
    :func:`ne.audit.engine.call_db` (off-loop on non-sqlite dialects, P2-16).
    """

    def __init__(self, engine: sa.Engine, *, offload_db_io: bool | None = None) -> None:
        self._engine = engine
        self._offload = default_offload(engine) if offload_db_io is None else offload_db_io

    @property
    def engine(self) -> sa.Engine:
        return self._engine

    def _claim_sync(self, dedup_key: str, action: str, payload_hash: str) -> sa.Row[Any] | None:
        for _attempt in range(_CLAIM_MAX_ATTEMPTS):
            try:
                with self._engine.begin() as conn:
                    conn.execute(
                        ne_action_dedup.insert().values(
                            dedup_key=dedup_key,
                            action=action,
                            payload_hash=payload_hash,
                            result_json=_DEDUP_PENDING,
                            created_at=datetime.now(UTC),
                        )
                    )
                return None
            except IntegrityError:
                # Lost the PK race — re-read the winner's row and let the caller decide
                # (dedup'd result, integrity conflict, or in-flight).
                with self._engine.connect() as conn:
                    row = conn.execute(
                        sa.select(ne_action_dedup).where(ne_action_dedup.c.dedup_key == dedup_key)
                    ).first()
                if row is not None:
                    return row
                # The row VANISHED between our losing INSERT and this re-read: the
                # winner raced a concurrent release(). We do NOT own the claim (no
                # PENDING row exists), so returning None would be a false ownership
                # report → RETRY the INSERT instead (P2-12).
                continue
        raise ActionIdempotencyError(
            f"could not claim the dedup key for {action!r} after {_CLAIM_MAX_ATTEMPTS} "
            "attempts (claim/release contention)"
        )

    async def claim(self, dedup_key: str, action: str, payload_hash: str) -> sa.Row[Any] | None:
        """Atomically reserve ``dedup_key`` for a write, returning None if WE own it.

        Attempts a unique-PK INSERT of a PENDING row. On success the caller owns the
        (exactly-once) write and MUST later :meth:`finalize` on success or
        :meth:`release` otherwise. If the INSERT loses the PK race this returns the
        EXISTING row (possibly still PENDING) so the caller fails closed instead of
        double-writing. If the re-read finds NO row (the winner raced a concurrent
        :meth:`release`) the INSERT is RETRIED (bounded) — claim NEVER returns ``None``
        without a persisted PENDING row, and raises :class:`ActionIdempotencyError`
        after :data:`_CLAIM_MAX_ATTEMPTS` (P2-12: no silent false ownership).
        """
        return await call_db(self._offload, self._claim_sync, dedup_key, action, payload_hash)

    def _finalize_sync(self, dedup_key: str, result_json: str) -> None:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(ne_action_dedup)
                .where(ne_action_dedup.c.dedup_key == dedup_key)
                .values(result_json=result_json)
            )
        if result.rowcount != 1:
            raise ActionIdempotencyError(
                f"finalize matched {result.rowcount} rows for dedup key {dedup_key!r}; the "
                "PENDING claim row vanished — refusing to record success (NE-050 exactly-once)"
            )

    async def finalize(self, dedup_key: str, result_json: str) -> None:
        """Attach the ORIGINAL successful result to a previously :meth:`claim` ed row.

        Asserts the UPDATE matched EXACTLY one row (the PENDING claim) and raises
        :class:`ActionIdempotencyError` on ``rowcount != 1`` — a 0-row match means the
        claim vanished (e.g. a concurrent release) and recording success anyway would
        let a later retry silently re-perform the write (P2-12).
        """
        await call_db(self._offload, self._finalize_sync, dedup_key, result_json)

    def _release_sync(self, dedup_key: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(sa.delete(ne_action_dedup).where(ne_action_dedup.c.dedup_key == dedup_key))

    async def release(self, dedup_key: str) -> None:
        """Drop a claimed row whose write did NOT succeed so a retry can re-claim.

        At-least-once honesty: this (and :meth:`finalize`) run AFTER the source-system
        write, so a crash between the write and this call leaves a PENDING row — a later
        retry is turned away as in-flight and the write is not repeated (crash-safe on
        this side); the un-finalized row is a manual/janitor cleanup concern. The claim
        boundary is deliberately NOT transactional across the HTTP seam.
        """
        await call_db(self._offload, self._release_sync, dedup_key)

    @staticmethod
    def is_pending(row: sa.Row[Any]) -> bool:
        """True if ``row`` is a CLAIMED-but-not-finished (in-flight) reservation."""
        return str(row.result_json) == _DEDUP_PENDING


# --- the governed handler closure + binding ---------------------------------------


def _payload_hash(validated: ActionInput) -> str:
    """Deterministic hash of the validated input (key-order-independent)."""
    canonical = json.dumps(validated.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_input(
    input_model: type[ActionInput], tool_name: str, args: Mapping[str, object]
) -> ActionInput:
    """Validate ``args`` against ``input_model`` (fail closed, C12-safe message).

    NE-ACTION-VALIDATE-BEFORE-GATE: this is the ONE validation implementation, called
    from TWO sites — :meth:`~ne.runtime.middleware.GovernedToolExecutor.execute`'s
    pre-gate ``validate_args`` preflight (BEFORE the human approval gate, so an
    invalid call never consumes a human decision) and the governed handler below
    (defense in depth, AFTER approval). Both sites call this function; neither
    re-implements it (the PR #377 derive-don't-transliterate lesson — pinned by
    ``tests/test_action_validate_before_gate.py``).
    """
    try:
        return input_model.model_validate(dict(args))
    except ValidationError as exc:
        # Name ONLY the offending field locations — never the rejected values (C12).
        # And only DECLARED locations: for ``extra_forbidden`` pydantic reports the
        # SUPPLIED key, which is caller/model text (unbounded, injection-capable), and
        # this message is rendered straight back into model context by the agent loop
        # (``runner._BLOCKING_ERRORS`` → ``BLOCKED: {msg}``) and to MCP clients. An
        # undeclared key is therefore COUNTED, never quoted.
        declared_fields = set(input_model.model_fields)
        locs = [str(err["loc"][0]) for err in exc.errors() if err.get("loc")]
        fields = sorted({loc for loc in locs if loc in declared_fields})
        undeclared = len({loc for loc in locs if loc not in declared_fields})
        if undeclared:
            noun = "key" if undeclared == 1 else "keys"
            fields = [*fields, f"<{undeclared} undeclared {noun}, names not echoed>"]
        # ``from None``, not ``from exc``: a value-free MESSAGE is only half the C12
        # discipline. pydantic renders the rejected value as ``input_value=...``, so a
        # chained cause resurfaces it in ``traceback.format_exception`` output — the
        # text any ``logger.exception`` / Sentry / OTel ``record_exception`` would ship.
        # This lane's values are WRITE-action payloads (recipients, note bodies), so it
        # is the higher-stakes twin of the verified-answer refusal seam. Chain-swept by
        # ``tests/test_action_validate_before_gate.py``.
        raise ActionValidationError(f"invalid input for {tool_name!r}: fields {fields}") from None


def _dedup_key_for(tool_name: str, validated: ActionInput, payload_hash: str) -> str:
    """The dedup key for ``validated`` (explicit ``request_id`` XOR derived scope).

    Shared identically by the pre-gate preflight and the handler — see
    :func:`_validate_input`'s docstring for the derive-don't-transliterate rationale.
    """
    if validated.request_id is not None:
        # Explicit caller-chosen id: EXACTLY today's key (no ':' can appear in a
        # legal request_id, so this namespace cannot collide with the derived one
        # below). Byte-identical to the pre-NE-ACTION-DETERMINISTIC-REQID behavior.
        return hashlib.sha256(f"{tool_name}:{validated.request_id}".encode()).hexdigest()
    scope = current_action_scope()
    if scope is None:
        raise ActionValidationError(
            f"{tool_name!r} was called without a request_id and outside any "
            "action idempotency scope; a governed write needs a stable "
            "idempotency identity (fail closed)"
        )
    return hashlib.sha256(f"{tool_name}:derived:{scope}:{payload_hash}".encode()).hexdigest()


def build_action_binding(definition: ActionDefinition, *, dedup: ActionDedupStore) -> ToolBinding:
    """Build the governed :class:`ToolBinding` for ``definition``.

    NE-ACTION-VALIDATE-BEFORE-GATE (two-phase validation, both phases calling the
    SAME :func:`_validate_input` / :func:`_dedup_key_for` helpers — never two
    divergent implementations):

    * PRE-gate (phase 1): the binding's ``validate_args`` — built here as
      ``_validate_args`` below — runs INSIDE
      :meth:`~ne.runtime.middleware.GovernedToolExecutor.execute`, AFTER guardrails +
      PDP but BEFORE the human approval gate. A validation failure there is an
      audited REFUSAL (``tool.validate[denied]``): the call is refused before any
      approval row exists and never consumes a human decision
      (``tests/test_action_validate_before_gate.py``).
    * POST-gate (phase 2, defense in depth): the ``handler`` closure below re-runs the
      identical validation. It is invoked solely by
      :meth:`GovernedToolExecutor._run_handler`, after guardrails + PDP + approval
      have cleared. It (1) validates the input against the frozen model (raising
      :class:`ActionValidationError` naming only field names on failure — the
      executor audits ``tool.execute[error]`` and fails closed, no write); (2) keys
      dedup on an explicit ``request_id`` when supplied, else on a DERIVED
      ``action:derived:scope:payload_hash`` key from the ambient
      :func:`~ne.runtime.correlation.current_action_scope` (raising
      :class:`ActionValidationError`, fail-loud, if neither a ``request_id`` nor an
      ambient scope is present); (3) short-circuits an identical retry to the stored
      result re-marked ``deduplicated`` WITHOUT calling the connector; (4) refuses a
      divergent-payload key reuse; (5) on a miss, performs the source write and
      records the dedup row on success.

    The binding's ``extract_audit_detail`` derives redacted write metadata
    (action / source_system / tier / field NAMES / target REF / dedup flag) for the
    ``tool.execute[success]`` event — never payload VALUES.
    """
    spec = definition.spec

    def _validate_args(args: Mapping[str, object]) -> None:
        validated = _validate_input(definition.input_model, spec.name, args)
        _dedup_key_for(spec.name, validated, _payload_hash(validated))

    async def _handler(args: Mapping[str, object]) -> str:
        validated = _validate_input(definition.input_model, spec.name, args)
        payload_hash = _payload_hash(validated)
        dedup_key = _dedup_key_for(spec.name, validated, payload_hash)

        # ATOMIC claim BEFORE any source write: the single unique-PK INSERT decides who
        # owns the (exactly-once) write. A concurrent or prior same-dedup-key call
        # loses the INSERT and is turned away here — it NEVER reaches definition.perform,
        # so two same-key calls can no longer both write (closes the read-then-write
        # TOCTOU). ``existing is None`` => we won the claim.
        existing = await dedup.claim(dedup_key, spec.name, payload_hash)
        if existing is not None:
            if str(existing.payload_hash) != payload_hash:
                # Same idempotency key, different payload => caller integrity bug.
                raise ActionIdempotencyError(
                    f"the idempotency key for {spec.name!r} was reused with a different "
                    "payload; an idempotency key must identify exactly one intended write"
                )
            if ActionDedupStore.is_pending(existing):
                # Same key + same payload, but a concurrent write is still in flight and
                # has not finalized a result. Fail CLOSED (no second source write); a
                # later retry after the first settles returns the deduplicated result.
                raise ActionIdempotencyError(
                    f"a write for this idempotency key of {spec.name!r} is already in "
                    "flight; retry once it has completed to receive the deduplicated result"
                )
            # Identical settled retry: return the ORIGINAL result re-marked; NEVER touch
            # the connector client (the source write already happened).
            prior = ActionResult.model_validate_json(str(existing.result_json))
            return prior.model_copy(update={"detail": "deduplicated"}).model_dump_json()

        # We own the claim: perform the source write EXACTLY once. On success finalize
        # the reservation; on any failure/exception release it so a later retry may
        # re-claim (an un-finalized claim after a crash is turned away as in-flight).
        try:
            result = await definition.perform(validated)
        except BaseException:
            await dedup.release(dedup_key)
            raise
        if result.succeeded:
            await dedup.finalize(dedup_key, result.model_dump_json())
        else:
            await dedup.release(dedup_key)
        return result.model_dump_json()

    def _extract_audit_detail(args: Mapping[str, object], result_str: str) -> Mapping[str, Any]:
        # C7-write audit metadata: what-action / what-target / payload-SHAPE. Target
        # refs + field NAMES are write-audit metadata, NOT payload content — no field
        # VALUE ever enters this dict (the guardian asserts a note body never appears).
        parsed = ActionResult.model_validate_json(result_str)
        return {
            "action_kind": "write",
            "source_system": definition.source_system,
            "tier": int(spec.tier),
            "fields": sorted(definition.input_model.model_fields),
            "target": parsed.source_ref or "",
            "deduplicated": parsed.detail == "deduplicated",
        }

    return ToolBinding(
        spec=spec,
        handler=_handler,
        extract_recipients=definition.extract_recipients,
        extract_egress_urls=definition.extract_egress_urls,
        estimate_cost_usd=definition.estimate_cost_usd,
        extract_audit_detail=_extract_audit_detail,
        validate_args=_validate_args,
    )


def build_action_bindings(
    definitions: Sequence[ActionDefinition], *, dedup: ActionDedupStore
) -> tuple[ToolBinding, ...]:
    """Build the governed bindings for ``definitions`` (all sharing one dedup store).

    THE NE-050 composition seam: a composition root wires write actions by appending
    these bindings into its ONE existing C6 executor tuple — next to its read tools,
    sharing the same PDP + guardrails + HITL gate + audit sink — never into a second,
    action-only executor.

    ::

        ensure_action_dedup_schema(engine)
        dedup = ActionDedupStore(engine)
        bindings = (
            *root_read_bindings,
            *build_action_bindings(
                [crm_note_definition(sf_write), ticket_update_definition(sql_write)],
                dedup=dedup,
            ),
        )
        executor = create_tool_executor(
            bindings, sink=sink, approval_gate=gate, govern_settings=...,
        )

    Worked example: ``packages/ne-mcp/src/ne/mcp/demo_server.py`` (``build_demo_server``,
    the one real consumer), pinned by
    ``packages/ne-mcp/tests/test_demo_server_composition.py``.

    The action ``*_ARG_POLICY`` constants MUST be merged into
    ``govern_settings.guardrails.tools`` alongside the root's read-tool policies — a
    tool absent from that map is denied by the deterministic guardrails before any
    decision (fail closed; proven by
    ``packages/ne-govern/tests/test_tool_args.py::test_unknown_tool_denied``).

    Dedup/idempotency semantics of the produced handlers are pinned by
    ``packages/ne-runtime/tests/test_action_tools.py`` (derived-scope and
    explicit-request_id key classes).
    """
    return tuple(build_action_binding(d, dedup=dedup) for d in definitions)


class _ActionExecutor(Protocol):
    """Narrow structural view of the governed executor a :class:`GovernedAction` needs."""

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str: ...


class GovernedAction:
    """A frozen :class:`~ne.contracts.integration.ActionTool` over the governed executor.

    Holds NO ``perform`` / handler / connector client — it can ONLY delegate to the
    executor. ``execute`` is its sole public callable (guardian-checked), so there is
    no path from an action to a source system that skips the C6 chokepoint.
    """

    def __init__(self, *, spec: ToolSpec, source_system: str, executor: _ActionExecutor) -> None:
        if executor is None:
            raise TypeError("GovernedAction requires a non-None executor (C6 no-bypass)")
        self._spec = spec
        self._source_system = source_system
        self._executor = executor

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def tier(self) -> ApprovalTier:
        return self._spec.tier

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return self._spec.input_schema

    @property
    def source_system(self) -> str:
        return self._source_system

    async def execute(self, args: Mapping[str, Any], *, principal: Principal) -> ActionResult:
        """Dispatch through the governed executor and parse the typed result."""
        return ActionResult.model_validate_json(
            await self._executor.execute(self._spec.name, args, principal=principal)
        )


def create_governed_actions(
    definitions: Sequence[ActionDefinition], executor: _ActionExecutor
) -> dict[str, GovernedAction]:
    """Build the :class:`GovernedAction` facades (name -> action) over ``executor``.

    The ``executor`` MUST already hold the matching action bindings (build them via
    :func:`build_action_bindings` and compose the executor through
    ``ne.runtime.create_tool_executor``); each facade only delegates by tool name.
    """
    return {
        d.spec.name: GovernedAction(spec=d.spec, source_system=d.source_system, executor=executor)
        for d in definitions
    }
