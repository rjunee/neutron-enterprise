# ne-events

The **typed platform event spine transport**: the Postgres outbox that carries
every platform event (SPEC §4) from producer to exporter. Per **ADR-001 §4.1**,
typed events travel via a Postgres outbox table consumed by an exporter seam in
`ne-worker` — **there is no message broker in v1**.

## What lives here

- **`outbox.py`** — two SQLAlchemy-Core tables (`ne_outbox_events`,
  `ne_outbox_cursors`) and the seam operations `append` / `read_batch` / `ack` /
  `drain_once` / `prune_acked`, plus `ensure_outbox_schema` (v1 bootstrap; the real
  migration runner is NE-069's).
  - **`prune_acked(engine, *, older_than_s, now=None)`** (P2-21) — bounded retention:
    deletes rows at/below `MIN(position)` across ALL consumer cursors AND older than
    the window, so the log cannot grow unbounded once events are delivered. It uses
    `id <= MIN(position)` (never per-consumer or above-any-cursor), which is provably
    outside every future `read_batch` scan and the NE-070 dead-hole fence — so it
    cannot affect at-least-once. **Zero consumers ⇒ deletes nothing** (an undelivered
    event is never dropped; clearing every consumer re-opens growth as an explicit
    operator choice). `occurred_at` (client-written, tz-aware) is the retention clock.
- **`factory.py`** — typed constructors (`new_audit_event`, …) over the
  `ne.schema` event classes that stamp the SPEC §4 mandatory fields.

The concrete event **schemas** (`AuditEvent`, `CorrectionSignal`, …) come from
`ne-schema`; this package is the *transport*, not the schema.

## Semantics

- **At-least-once — including under concurrent Postgres producers (NE-070).** A
  consumer's cursor advances **only after** its handler has durably processed a
  window (`drain_once` acks in a second transaction, after the handler runs
  outside any transaction). A crash between read and ack redelivers the
  identical window. Handlers must be idempotent. On Postgres the cursor is
  additionally protected by a **settled-horizon fence**: `read_batch` never
  scans or acks past the highest id whose every possible dispensing transaction
  has provably finished, so an uncommitted lower id sitting in a gap below a
  committed higher id is **never skipped** — the classic multi-writer outbox
  sequence-gap. The fence is derived from the identity sequence `last_value`
  captured against a snapshot `xmax`; once the live snapshot's `xmin` reaches
  that `xmax`, every transaction below the fence has finished and any still-
  invisible id below it is a permanent rollback hole (safe to skip, keeping the
  cursor live past aborted producers).
- **Same-transaction append.** `append(conn, event)` MUST run on the caller's
  business-write connection so the event row and the business write commit
  atomically — that atomicity *is* the outbox pattern. A duplicate `event_id`
  is idempotent (a SAVEPOINT keeps it from aborting the caller's transaction).
  On Postgres, `append` also forces the transaction's xid to be assigned before
  the identity `nextval` so the settled-horizon fence is sound (requires the
  sequence's default `CACHE 1`).
- **Multiple drainers per consumer are safe on Postgres.** Competing workers
  claim the cursor row with `SELECT ... FOR UPDATE SKIP LOCKED`; a worker that
  cannot obtain it reads nothing (no block, no lockstep double-delivery). Two
  workers can still each redeliver a window across the read/ack split (that is
  at-least-once, never loss). SQLite is a single-writer test dialect and keeps
  the simple id-ordered scan.
- **Immutable per-consumer filter.** A consumer's `event_types` filter is
  recorded at first registration and cannot change. Widening it would silently
  skip rows the cursor already advanced past, so a changed filter raises
  `ConsumerFilterMismatchError` — register a new consumer name instead. The scan
  window carries no type predicate; the cursor advances over the whole window,
  so rows filtered out never redeliver to that consumer (deliberate).

## Cursor edge-case discipline (ABI teardown §3.5, lifted not copied)

The cursor logic applies ABI's hard-won event-cursor checklist, translated from
its SQLite/Dagster implementation to this Postgres pattern:

- **cursor-advance-then-drop** — advance only after successful processing.
- **filter-superset invariant** — fail loud on a changed filter.
- **fail-open-on-transient-error** — a handler exception yields a `DrainResult`
  with `error` set and the cursor unmoved, never a stalling exception.

## Testing

`tests/` run against an in-memory SQLite engine (the identical SQLAlchemy
statements, including SAVEPOINT). `tests/test_outbox_postgres.py` re-runs the
savepoint-idempotency, crash-window-redelivery, and JSONB round-trip checks
against a real Postgres when `NE_TEST_POSTGRES_URL` is set;
`tests/test_outbox_postgres_concurrency.py` is the NE-070 settled-horizon proof
(uncommitted-lower-id never acked past, 8-producer/2-consumer stress with zero
lost events, `SKIP LOCKED` claim, rollback-hole liveness, racing-ack
monotonicity). Both auto-skip when `NE_TEST_POSTGRES_URL` is unset
(pending-live-verification ledger); the live driver is `psycopg` (test-only dep).

## Why here (ADR-0002)

Not `ne-contracts` (frozen v1.0.0, and zero-heavy-deps by design — SQLAlchemy is
excluded) and not `ne-obs` (would couple every event producer to the OTel
stack). The outbox APIs are deliberately package-internal seams, not
`ne-contracts` Protocols, in v1 — a Protocol can be extracted additively later if
a second transport ever appears.
