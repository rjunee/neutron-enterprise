# ne-audit

Hash-chained, append-only audit sink — the durable, tamper-evident **authoritative**
audit store for Neutron Enterprise (SPEC §3.3, C6). Implements the frozen
`ne.contracts.governance.AuditSink` Protocol (`append` + `verify`), signs periodic
checkpoints to WORM through the frozen `ObjectStore` Protocol, and answers the
SPEC §4 replay question — *"why did the system say X on date D?"*.

## Architecture: the hash chain

Every audit record links to its predecessor through a SHA-256 hash chain:

```
record_hash = SHA-256( prev_hash_hex_as_utf8 + canonical_json(payload) )
genesis prev_hash = "0" * 64
```

- `canonical_json` is `json.dumps(payload, sort_keys=True, separators=(",", ":"),
  ensure_ascii=False)` — key-order- and whitespace-insensitive, so the digest is
  stable across the write path (`model_dump(mode="json")`) and the read-back path
  (JSON on SQLite, JSONB on Postgres).
- `seq` is a **dense** chain position (1, 2, 3, …) assigned under the head lock —
  deliberately **not** the identity/rowid column, so a rolled-back INSERT can never
  leave a gap that would look like a deletion.
- A single-row `ne_audit_head` table (`id = 1`) points at the chain head (`seq` +
  `record_hash`). It is read `FOR UPDATE` to serialize concurrent appends on
  Postgres; on SQLite writers serialize through the database write lock (configure
  the engine for `BEGIN IMMEDIATE` when appending concurrently — see the test
  conftest's `file_engine`).

### Append-only enforcement (C6, no bypass)

`append` is the **only** write surface — there is no update or delete API. That is
enforced at BOTH layers:

- **app-level** — the package exposes no mutation entry points; a duplicate
  `event_id` is idempotent (returns the existing record, does not extend the chain),
  so emitters can retry deliveries safely;
- **database-level** — `BEFORE UPDATE`/`BEFORE DELETE` triggers on `ne_audit_log`
  on both dialects (`RAISE(ABORT)` on SQLite, a `plpgsql RAISE EXCEPTION` trigger on
  Postgres, which also blocks `TRUNCATE`).

`ne audit verify` / `verify_chain` also cross-checks every denormalized query column
(`event_id`, `event_type`, `config_hash`, `actor`, `occurred_at`, `kpi_tag`,
`trace_id`, `action`, `resource`, `outcome`) against the hash-covered payload, so
swapping a column value (e.g. `config_hash` or `actor`) WITHOUT touching the JSON —
which leaves the per-record hash and chain links intact — is still detected and the
offending column is named.

### Verification: full and bounded, both anchored, both fail-closed, both race-free

A **full** (unbounded) `verify_chain` reads the chain head (`ne_audit_head`) **FIRST**,
before anything else, and verifies exactly the prefix that head anchors — the ledger
**as of the head it observed**. This is the concurrency guarantee C10's demos depend
on: a record appended by another writer *while verification is running* lands past
the snapshot this call took and is, by construction, out of scope — it is not
reported as a break. (This was NE-AUDIT-VERIFY-RACE: the pre-fix verifier re-read the
head *after* scanning rows, so a single concurrent append made `head_seq` exceed the
scanned max and raised a false `head mismatch` tamper alarm — indistinguishable from a
real one to an operator. See `ne.audit.verify`'s module docstring and
`tests/test_verify_race.py` for the full argument and the deterministic
mid-verify-interleaving proof.) The walk requires `seq` to be dense within that
head-anchored prefix, recomputes every record hash and chain link, cross-checks the
columns, and anchors the walked tail by comparing it against the **same head snapshot
read up front** — never a fresh re-read — so a deleted head/prefix record surfaces as
a missing predecessor, an emptied table with a non-genesis head is a `head mismatch`,
and a missing `ne_audit_head` row fails closed (returns invalid, never raises).
Finally, a race-free epilogue re-reads `max(seq)` over the whole table (read **before**
re-reading the head) to catch forged rows genuinely inserted **past** the verified
head — race-free because `append_record` advances the head in the same transaction as
the row insert, so the head is monotone and a legitimate concurrent append can never
false-fire this check.

A **bounded** (windowed) verify is **as sound as full verify** for deletion, reorder
and gap attacks. Before walking the window it runs a GLOBAL, seq-based (never
`occurred_at`-based, so non-monotonic timestamps cannot false-fire) **density
pre-check** — no leading gap, no interior gap, and `max(seq)` must equal the head
seq — counting only `seq <= head_seq` (the same head-first snapshot as the full path),
so it shares the full path's immunity to a record appended mid-verify. Bounding that
count to the head-anchored prefix is deliberately BLIND to anything beyond the head
by itself — that is what the race-free `_beyond_head_break` epilogue exists to close.
It then anchors **both ends** of the window: the predecessor of the window's first
row must exist (a deleted predecessor IS a named break), and the window's tail links
forward to its successor row (`seq hi+1`) or, when the window ends at the head,
cross-checks the head hash. Finally — on **every** bounded success path, not only
when the window happens to reach the head — it runs the SAME race-free
`_beyond_head_break` epilogue the full path runs: `max(seq)` over the whole table,
read BEFORE a fresh head re-read, so a forged row sitting entirely outside the
queried window (or beyond a head that was itself rewound) is still caught, not just
one the window's own `[lo, hi]` happens to touch. So deleting the record *before*,
*inside*, or *after* a window, emptying a time range, renumbering/reordering `seq`,
forging a chain-consistent row beyond the head (inside OR outside the window), or
rewinding `ne_audit_head` are all caught and named.

**Inherent limitation (mathematical, not a bug):** a *consistent* tail-truncation
(delete tail rows AND rewind `ne_audit_head` to a self-consistent earlier state) or a
full chain rewrite performed AFTER the last signed checkpoint is invisible to
`verify_chain` alone — a forward hash chain cannot prove records once existed past a
self-consistent head. Signed WORM checkpoints (`verify_checkpoints`, below) close that
gap by pinning the head at signing time. **Running `verify_chain` AND
`verify_checkpoints` together is the operational requirement.**

### Anchored verification: sub-second on a request path (`AnchoredChainVerifier`)

`verify_chain` is O(chain) — a strictly sequential, pure-Python re-hash of every
record. **Measured on a 203,343-row corpus (SQLite): 3.8 s.** On the pilot's Postgres
the same walk measures **9.77 s**, and the chain grows ~20,850 rows/day, so the cost
grows **~1.0 s/day, forever**. Putting that on a user-facing path means the ledger's
own success is what makes the product slow.

`AnchoredChainVerifier` splits the two costs without turning verification off:

| | proof | cost at 203k rows | who runs it |
|---|---|---|---|
| `verify()` | the chain still **extends** a head a full verify blessed | **0.37 ms** idle; 1.4 ms after 15 appends; 526 ms after a full day's 20,850 appends | the request path |
| `full_verify()` | the real `verify_chain`, whole ledger | 3.8 s | a background task, on `CHAIN_ANCHOR_FULL_VERIFY_INTERVAL_S` (300 s) |

The cost of `verify()` is O(rows appended since the last proof) and is **independent
of chain size** — so it stays sub-second for any ledger, provided the process verifies
at least once a day.

**The anchor is a trust boundary, and it lives ONLY in the verifying process's
memory** — never written to, never read from, the database. That is the whole point: an
attacker with write access to `ne_audit_log` *and* `ne_audit_head` cannot forge the
value the forward walk links against. In particular the **self-consistent rewrite**
*at or below the anchor* (edit record k ≤ A, then recompute every hash to the head so
the ledger looks perfect) IS caught, because it necessarily moves the anchor
successor row's `prev_hash` off the remembered anchor hash. So is a **coherent tail
truncation that rewinds the head strictly below A** — the limitation noted just above,
which `verify_chain` alone cannot see. The boundary is strict (`head_seq <
baseline_seq`): a coherent truncation leaving the head *at or above* A is missed, and
is class 2 below.

**What it misses — two classes, and only one of them is a trade:**

1. An **unpropagated** tamper at or before the anchor — an edited payload with a stale
   `record_hash`, an edited `record_hash`, a swapped denormalized column, a deleted
   prefix record. Those leave the ledger internally *inconsistent* at or below the
   anchor, and this proof never re-reads there. `verify_chain` catches every one; this
   proof catches none. **This is the one class the anchor gives up**, and it is what
   the age bound below exists to bound.
2. Any **coherent replacement or truncation of the suffix above the anchor** —
   replacing the rows at `seq > A` with a different but internally self-consistent
   sequence that still links onto the remembered anchor hash and still ends on a
   matching `ne_audit_head`. Two shapes:
   - **rewrite** — edit record k > A, recompute its `record_hash` and its denormalized
     columns, relink every `prev_hash` up to the head, advance `ne_audit_head`. The
     link back to the anchor survives even when k = A+1 and the anchor's successor row
     is itself the edited one: what the rewrite leaves alone is that row's `prev_hash`,
     not the row.
   - **truncation** — delete every row above some `t ≥ A` and rewind `ne_audit_head` to
     row `t` coherently. For `t > A` that proves as a valid shorter extension; for
     `t = A` as a valid *empty* one, since it restores exactly the head the anchor
     blessed. Deleted rows leave no trace either proof can read. Only `t < A` is
     caught — that is class 1's counterpart in the gain column, above.

   **`verify_chain` misses all of these identically** — they are exactly the
   "*consistent* tail-truncation ... or a full chain rewrite performed after the last
   signed checkpoint" named as an inherent limitation above. So *nothing is traded away
   here*: walking the whole ledger on every request would not catch it either. Their
   control is signed WORM checkpoints, not a longer walk.

**Class 1 is bounded in code, not by hope — but read the bound precisely.** `verify()`
REFUSES an anchor whose blessing is older than `CHAIN_ANCHOR_MAX_AGE_S` (900 s = 3×
the interval) and runs the full verify inline instead. That is a **staleness ceiling on
reuse, not a time-to-detection**: the age test lives inside `verify()`, so it fires on
the first verification after the threshold, not at the threshold. The guarantee is
that **no verification past 15 minutes may be served off the stale anchor** — each one
pays the full walk, and a class-1 tamper surfaces on that first subsequent call.
Elapsed time with nobody calling detects nothing. **Active** detection comes from the
background loop, which walks the ledger every 300 s whether or not a request arrives —
and in this repo that loop runs in the API process only; the worker builds its own
verifier without one, so it gets the reuse ceiling and no active detection. Class 2 is
not bounded by any of this, because no schedule of `verify_chain` detects it either.
If the background loop dies, the request path degrades loudly back to today's
inline full verify rather than to a weaker claim. A missing, stale, contradicted or
unconfirmable anchor never returns "verified"; there is deliberately no configuration
that makes it. **A background walk that RAISES rather than returning invalid — a
dropped connection, a statement timeout, a failover mid-walk — clears the anchor too**:
the walk proves nothing about the chain, but it withdraws the support for the standing
claim the anchor makes, so reads stop being served off it immediately instead of
coasting to the 900 s ceiling. That costs an inline walk per read while the fault
persists; a persistent fault reaches that same state within 900 s either way, so what
fail-closed actually changes is the timing, not the availability. ⚠️ **For a TRANSIENT
fault the cost is not zero, and it was zero before:** one bad tick that would previously
have left the anchor standing now puts a full 203,343-row walk on **every** read until
the first walk that COMPLETES installs a fresh anchor — and that walk is usually an
inline request-path one, not the next background tick: `verify` falls back to the same
`_full_verify`, which re-blesses on success, so the herd self-heals the moment one read
finishes rather than waiting up to 300 s for the loop. There is no singleflight around
`_full_verify`, so every read already in flight pays its own walk.

Every result carries `basis` (`"full-chain"` / `"anchored-extension"`)
and a `detail` string that states its own scope, so a caller that prints `detail`
prints the truth.

This is a latency control, **not** a replacement for signed WORM checkpoints. Across a
restart the process holds no anchor at all, and the durable tamper anchor remains
`verify_checkpoints` — still default-off via
`NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE`. See `ne/audit/anchor.py`'s module docstring
for the full trust-boundary argument and `tests/test_anchored_verify.py` for the
mutation table behind every claim here.

## Audit sink vs. the ne-events outbox

The audit sink is the durable, tamper-evident **authoritative store**; the ne-events
Postgres **outbox** is the **export transport**. They are aligned, not duplicated.
Audit records ARE `ne.schema` platform events (`AuditEventLike` pydantic models), and
`ne.audit.append_record` writes on the **caller's** connection exactly like
`ne.events.append`, so an emitter appends the same event to BOTH stores in ONE
transaction:

```python
with engine.begin() as conn:
    do_business_write(conn)
    ne.audit.append_record(conn, event)   # authoritative, tamper-evident
    ne.events.append(conn, event)         # export transport
# both commit atomically, or neither does
```

## Transactional sqlite helper (`ensure_transactional_sqlite`)

The dual-append pattern above does its idempotent inner write inside a
`conn.begin_nested()` **SAVEPOINT**. On plain **pysqlite**, the DBAPI defers the
outer `BEGIN`, so the SAVEPOINT writes are emitted *before* the real outer
transaction opens — if the audit-append leg then fails and the `engine.begin()`
block rolls back, the SAVEPOINT-era rows are **not** undone (stranded,
unaudited). Production Postgres is safe (real nested transactions); this bites
pysqlite (dev/CI and any sqlite deployment). This is the tracked SPEC §11 gap
referenced in the NE-034/035/064/065/066 AS-BUILT entries.

`ne.audit.ensure_transactional_sqlite(engine)` closes it — the NE-069 reference
recipe (`_ensure_transactional_ddl` in `tools/ne-cli`) hoisted into one shared
helper. It installs a `checkout` pool listener that sets
`isolation_level = None` (chosen over `connect` so **pre-pooled** connections are
covered too) plus a `begin` listener that emits an explicit `BEGIN`, so the
SAVEPOINT nests inside the outer transaction and an outer rollback undoes it. The
helper is:

* a **hard no-op on every non-sqlite dialect** — Postgres behaviour is
  byte-identical and unchanged;
* **idempotent** (a `WeakSet` tracks patched engines, so re-application never
  stacks listeners / double-emits `BEGIN`);
* applied at **every dual-append engine site** in `ne-runtime` and `ne-flywheel`
  (both factories and store/scheduler constructors) and defensively in
  `PostgresAuditSink.__init__`.

It is intentionally a plain `BEGIN`, *not* the `BEGIN IMMEDIATE` write-concurrency
variant used by the ne-audit tests' `_make_serialized` fixture — that is a
different recipe and stays local.

## Engine timeouts + event-loop offload (`create_engine_with_timeouts` / `call_db`)

`ne.audit.engine` is the shared engine chokepoint for availability (P2-16). A
plain `create_engine` ships with **no** connect/statement/lock timeout, so a
Postgres failover/partition or a held row lock blocks `engine.begin()`
indefinitely — and because the sync SQLAlchemy adapters run their blocking I/O
*on* the ASGI event loop, that one stall freezes **every** concurrent request,
stops SSE keep-alives, and even a DB-free `/healthz` can't be served until the OS
TCP timeout fires.

* `EngineTimeouts` — a frozen, config-surfaced model (`connect_timeout_s`,
  `statement_timeout_ms`, `lock_timeout_ms`, `pool_timeout_s`,
  `pre_ping_timeout_s`) exposed on `AuditSettings.engine_timeouts` and
  `RuntimeSettings.engine_timeouts`.
* `create_engine_with_timeouts(url, *, timeouts=…)` — `sa.create_engine` with the
  dialect-appropriate kwargs: for **postgresql**, libpq `connect_timeout` +
  `pool_timeout` (no `pool_pre_ping` — see the bounded-pre-ping paragraph
  below for why); for **sqlite**, the pysqlite busy `timeout` only (no pool
  kwargs — `SingletonThreadPool` rejects them); any other backend passes
  through with library defaults. Every in-scope engine factory (the sink, the
  runtime session/memory/approval/spend engines, the ne-api runtime + audit
  engines) routes through it.

  The server-side `statement_timeout`/`lock_timeout` GUCs are **NOT** carried as
  a libpq `options` startup string (the pre-NE-POOLER-TIMEOUTS mechanism) — a
  transaction-mode PgBouncer >=1.20 FATALs on any `-c` GUC in `options` it does
  not itself track (neither timeout is tracked), with `unsupported startup
  parameter in options: statement_timeout` at CONNECTION time, before any query
  runs. Instead, on `postgresql*` URLs, `create_engine_with_timeouts` installs a
  single SQLAlchemy engine `"begin"` event listener
  (`ne.audit.engine._pg_timeout_listener`) that re-issues both GUCs as `SET
  LOCAL` at the start of **every** transaction — direct or pooled alike, since
  SQLAlchemy autobegins a transaction (firing `"begin"`) on the first statement
  of any connection use. This is the pooler-correct mechanism because
  transaction-mode PgBouncer may hand each transaction on a pooled connection to
  a **different** backend, so only a per-transaction reissue is safe; `SET
  LOCAL` evaporates at commit/rollback so it can never leak onto another
  pooler client's transaction (proven live, both direct and through a real
  transaction-mode PgBouncer, by
  `packages/ne-audit/tests/test_engine_timeouts_postgres_live.py` and
  `packages/ne-runtime/tests/test_dbos_pooler_finding_live.py`, which assert the
  timeout re-applies across two consecutive transactions on one checkout).

  An earlier revision of this fix ALSO installed a pool `"connect"` listener
  issuing a session-wide `SET` once per physical connection, reasoning it gave
  direct-connection parity with the old mechanism. That was **wrong** and was
  reverted: transaction-mode PgBouncer does not run `DISCARD ALL` between
  clients by default, so that connect-time `SET` persisted on whichever backend
  it landed on and silently applied to **every other pooler client** later
  handed that same backend — verified by hand to cancel an unrelated client's
  unrelated `pg_sleep()` with "canceling statement due to statement timeout".
  The begin-only, `SET LOCAL`-only mechanism has no such leak, and is
  sufficient for every in-scope caller: none puts a `create_engine_with_timeouts`
  engine into DBAPI `AUTOCOMMIT` — the one case where the listener deliberately
  emits nothing. (The `"begin"` event itself still fires there — SQLAlchemy's
  autobegin bookkeeping is client-side and dialect-agnostic — but no
  server-side transaction block opens, so `SET LOCAL` outside one would be a
  silently-unapplied no-op WARNING; the listener skips explicitly rather than
  fire a statement that looks like coverage while providing none. See
  `ne.audit.engine._pg_timeout_listener`'s docstring and
  `test_engine_timeouts_postgres_live.py::test_autocommit_connection_gets_no_timeout`.)

  **PgBouncer's `ignore_startup_parameters` is not an acceptable alternative.**
  Configuring it to accept `statement_timeout`/`lock_timeout` makes the
  connection succeed, but the value is silently discarded — `SHOW
  statement_timeout` then reports `0` (unbounded), reopening the exact
  unbounded-hang P2-16 exists to prevent. The
  `test_pooler_still_rejects_untracked_startup_options_guc` canary in
  `test_dbos_pooler_finding_live.py` verifies only that this CI lane's pooler
  still REJECTS an untracked GUC in `options` — i.e. that nobody has adopted
  `ignore_startup_parameters` — and (as of NE-POOLER-PREPING-BOUND, P3-3) now
  also fails loudly if the pooler ever accepts-then-silently-discards one
  instead. The *discard* behaviour described above (`SHOW statement_timeout`
  reporting `0` after `ignore_startup_parameters` is configured) was verified
  **by hand** (2026-07-24) and has no automated proof of its own — the canary
  only proves this lane's config was never changed to reproduce it.

  **Pool liveness (pre-ping).** `create_engine_with_timeouts` does **not** set
  SQLAlchemy's native `pool_pre_ping` — that flag's `SELECT 1` liveness probe
  runs directly on the raw DBAPI connection, with **no bound**, so against a
  server that is TCP-alive but wedged (exactly the failover condition
  `pool_pre_ping` exists to detect) it blocks forever
  (NE-POOLER-PREPING-BOUND, 2026-07-24 — the one instance of the raw-DBAPI
  gap below that actually mattered). Instead, `create_engine_with_timeouts`
  installs a *replacement* pre-ping as a pool `"checkout"` event listener
  (`ne.audit.engine._pre_ping_listeners` /
  `ne.audit.engine._bounded_dbapi_ping`) that runs the same probe under an
  explicit **client-side** deadline (`EngineTimeouts.pre_ping_timeout_s`,
  default 5s, must be `> 0`) — the only mechanism that CAN bound a probe on
  an already-established connection to a wedged server, since any
  bound-establishing round-trip issued against that server would itself
  hang.

  The probe's outcome is **tri-state**, not a plain pass/fail (Argus review,
  2026-07-24, round 2): a DEFINITIVE failure (a driver/libpq error, or a bad
  result status) raises `sqlalchemy.exc.InvalidatePoolError`, the exact
  exception native pre-ping raises, invalidating the **whole pool** — the
  same assumption native pre-ping makes, that one dead backend often means a
  failover just happened and every other pooled connection is stale too. But
  a bounded probe has a failure mode native pre-ping's unbounded one never
  had: it can time out **inconclusively** against a server that is merely
  slow, not dead (e.g. a saturated transaction-mode PgBouncer legitimately
  queuing the probe behind other clients' transactions, up to its own
  `query_wait_timeout`). Treating that identically to a proven-dead
  connection would invalidate the *entire* pool on every transient
  slowdown — a self-amplifying failure mode against an already-saturated
  pooler — so an inconclusive timeout instead raises the weaker
  `sqlalchemy.exc.DisconnectionError(invalidate_pool=False)`: only the ONE
  timed-out connection is discarded and retried, every other pooled
  connection is left alone. **Operational implication:** `pre_ping_timeout_s`
  must be set above the worst-case latency a healthy-but-busy backend can
  legitimately exhibit for your traffic pattern, or routine load will
  routinely trip the (harmless, single-connection) timeout path. No psycopg
  import is added to ne-audit's source — the probe duck-types the DBAPI
  connection's `pgconn` surface (`getattr(dbapi_connection, "pgconn", None)`)
  rather than importing `psycopg`; a non-psycopg3 driver falls back to a
  status-quo, unbounded cursor ping (always definitive `True`/`False`, never
  the inconclusive state — there is no deadline to time out against). Proven
  live, both direct and pooled, by
  `test_engine_timeouts_postgres_live.py::test_wedged_preping_probe_times_out`
  (the regression proof) and `::test_preping_replacement_still_recycles_dead_connections`
  (the original dead-connection-recycle guarantee was not lost in the swap).

  A raw DBAPI connection off `create_engine_with_timeouts`
  (`engine.raw_connection()`) still carries **no** server-side timeout at all
  (`SHOW statement_timeout` reports `0`) — this remains outside the `SET
  LOCAL` mechanism's coverage BY DESIGN (see
  `ne.audit.engine._pg_timeout_listener`'s "NOT covered (1)" docstring
  paragraph for why no server-side bound can exist there) and is now the
  PINNED-correct behaviour, verified live in both lanes by
  `test_raw_connection_reports_no_session_timeout` — which doubles as the
  behavioural anti-leak pin: a reintroduced connect-time session-wide `SET`,
  under any name, would make this fail.

  **Known residuals, out of this unit's scope (candidate follow-ups).**
  (1) Pool CHECK-IN, not just checkout: SQLAlchemy's default
  `reset_on_return="rollback"` issues a plain `ROLLBACK` straight on the raw
  DBAPI connection when a connection is returned to the pool — the same
  unbounded raw-DBAPI shape checkout/pre-ping had, but on the way IN, so a
  server that wedges while a connection is checked out can still hang the
  check-in path with no client-side bound. (2) The non-psycopg3 cursor-ping
  fallback (`ne.audit.engine._unbounded_cursor_ping`) stays unbounded — no
  postgres engine in this workspace's lock uses a non-psycopg3 driver today,
  but nothing prevents one being added. (3)
  `packages/ne-connect/src/ne/connect/sql/engine.py`'s independent
  `sa.create_engine(url, pool_pre_ping=True)` is a SEPARATE engine factory
  that does not route through `create_engine_with_timeouts` at all, and still
  carries the exact unbounded native pre-ping this unit fixed here. (4) An
  inconclusive-timeout probe still discards and reconnects the ONE affected
  connection (not free — a checkout that was merely slow now also pays a
  reconnect), and `pre_ping_timeout_s` is a single global value with no
  per-deployment auto-tuning against observed pooler queue latency; an
  operator running against a chronically-saturated pooler needs to raise it
  manually rather than the system adapting on its own.
* `default_offload(engine)` / `call_db(offload, fn, /, *args)` — the sync adapter
  bodies (`PostgresAuditSink.append/verify`, `SqlAlchemySessionStore`,
  `SqlAlchemyApprovalStore`, `ActionDedupStore`) run via `asyncio.to_thread` on
  **non-sqlite** dialects so blocking DB I/O never pins the event loop (mirroring
  the ne-worker outbox drain). SQLite stays **on-loop by design**: pysqlite
  connections are thread-bound, so an in-memory `sqlite://` on a
  `SingletonThreadPool` would hand each thread a *different* empty DB — the offload
  is dialect-gated, and "production is Postgres" is where it matters.

## Signed checkpoints → WORM

The chain proves internal consistency, but an attacker who can rewrite the whole
table can also recompute a *consistent* chain. Signed checkpoints close that gap: we
periodically sign the chain head (`seq` + `record_hash`) and archive the signed
document to WORM through the frozen `ObjectStore` (put + `archive(retain_until=…)`).

- Signing is **HMAC-SHA256** (stdlib `hmac`) — zero third-party crypto deps. The key
  lives in the `SecretStore` at `ne/audit/checkpoint-signing`
  (`HmacSigner.from_secret_store`), or is supplied as raw bytes for the CLI/test seam.
- The checkpoint document carries an `algorithm` field, keeping an **Ed25519 upgrade
  path** open without a format change.
- `verify_checkpoints` re-checks each signed checkpoint against the live chain and
  catches a consistent rewrite (`chain rewritten vs signed checkpoint`), a truncation
  below a signed head (`chain truncated below signed checkpoint`), and a **head rewind
  below a signed checkpoint** (`head rewound below signed checkpoint`, where the signed
  rows are left in place but the head pointer is moved backwards). It **fails closed**:
  a malformed stored checkpoint returns an invalid result naming the offending object
  key (never an exception), and a missing `ne_audit_head` row returns invalid.
- **Key rotation (P2-10).** Pass a `SecretStore` and each checkpoint is verified with
  the key **version it recorded** — `SecretStore.read(cp.key_path, version=cp.key_version)`
  — while the *current* signer is used only to **sign new** checkpoints. So rotating the
  checkpoint signing key does NOT turn every previously-archived (WORM, un-re-signable)
  checkpoint into a permanent false `signature invalid`. A raw `HmacSigner` is still
  accepted for the single-key legacy/test path (a rotated store MUST use SecretStore
  mode); a missing/destroyed key version fails **closed** with a named detail
  (`signing key <path> v<n> unavailable for checkpoint at seq …`), never a traceback.
- With **zero** stored checkpoints `verify_checkpoints` returns `valid=True, checked=0`
  but provides **NO tamper anchor** (the `detail` says `no anchor`), and the window
  AFTER the newest checkpoint is unprotected until the next checkpoint is written. It
  is **WORM retention on the object store — not this function** — that prevents an
  attacker from deleting the archived checkpoints themselves.

The periodic scheduler loop lives in ne-worker (`ne.worker.audit_checkpoint`,
NE-AUDIT-CRON) — a plain maintenance task alongside the heartbeat/outbox-drain loops,
default-off via `NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE` (empty ⇒ no loop) /
`NE_WORKER_AUDIT_CHECKPOINT_INTERVAL_S` (tick cadence). Policy — key path, prefix,
retention, and the `every_n_records` threshold — stays here in the frozen
`AuditSettings.checkpoint` (`NE_AUDIT_CHECKPOINT__*`), so the periodic loop and the
on-demand `ne audit checkpoint` CLI always agree. This package ships the primitives
(`write_checkpoint` / `maybe_checkpoint`) and the CLI triggers them on demand; see
`packages/ne-worker/README.md` for the loop itself. Only the in-memory
`ne.plugins.object_store` adapter (`memory`) exists today — a production deployment
pointing the loop at real S3/Object-Lock WORM needs a future S3 adapter.

### Production: the `s3` ObjectStore adapter (NE-S3-OBJECTSTORE)

`write_checkpoint`/`maybe_checkpoint` take any `ObjectStore` — the production
route is `ne-storage`'s `s3` adapter (`ne.storage.s3.S3ObjectStore`),
registered as the `ne.plugins.object_store` entry point `s3` alongside the
`memory` reference adapter. Its `archive()` is backed by real S3 Object Lock
in COMPLIANCE mode, so a checkpoint archived through it lands in genuine
write-once storage — proven end to end (a real signed checkpoint via
`write_checkpoint`, `verify_checkpoints` passing, the archived object
immutable against both the adapter's own contract semantics and a raw-client
server-side bypass probe) by
`packages/ne-storage/tests/test_s3_worm_live.py::test_signed_checkpoint_lands_in_worm`.
See `packages/ne-storage/README.md` for the full adapter documentation
(contract mapping, WORM honesty split between server- and client-enforced
guarantees, settings reference).

Pointing NE-AUDIT-CRON's checkpoint loop at real WORM is a **config change,
not a code change**: `build_audit_checkpoint_loop` resolves the adapter by
name via `PluginRegistry` from `NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE` (worker
env — lands with NE-AUDIT-CRON/#249) and constructs it via
`cls.from_settings(...)` when present. Set:

```
NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE=s3
NE_STORAGE_S3_BUCKET=<your-worm-bucket>
NE_STORAGE_S3_REGION=us-east-1
# endpoint_url/access_key_id/secret_access_key empty ⇒ AWS defaults + IAM credential chain
```

**Two operational footguns, stated plainly:**

1. **S3 Object Lock must be enabled AT BUCKET CREATION.** It cannot be
   turned on for an existing bucket afterwards — `aws s3api create-bucket
   --bucket <name> --object-lock-enabled-for-bucket` (or
   `ObjectLockEnabledForBucket=True` on `CreateBucket`) is a one-time,
   create-time-only flag. Provisioning a bucket without it means starting
   over with a new bucket, not a follow-up API call.
2. **COMPLIANCE-mode retention cannot be shortened or removed by anyone,
   including the AWS account root.** Choose `retention_days` deliberately —
   `CheckpointConfig`'s default is 365 days — before the first checkpoint is
   archived.

## Replay: "why did the system say X on date D?"

`replay(engine, *, trace_id=…, event_id=…, resource=…, start=…, end=…)` returns the
`config_hash`(es) in force, the retrieved context IDs, and the producing events in
chain order. `event_id` pivots to the whole trace; `resource` + a time window select
a slice. The `config_hash` is the SPEC §4 answer — it pins the exact
model/retrieval/policy/prompt config that produced the decision.

Replay returns **payload-authoritative** values: the denormalized columns are used
ONLY inside the WHERE clause for indexed selection, while every value returned
(`config_hash`, `actor`, `outcome`, `occurred_at`, …) derives from the hash-covered
payload. So a tampered filter column can at worst hide or mis-select a row (which
`verify_chain` detects) but can never cause replay to return forged content.

**Decided convention** (NE-022/NE-033 will stamp it): retrieval/decision events carry
their retrieved chunk/context IDs in `provenance['context_ids']` as a comma-separated
string. Because `ne.schema` provenance values are typed `str | ProvenanceEntry`, the
parser accepts both a plain string and the `{"key": …, "value": …}` shape a
ProvenanceEntry round-trips to through JSON.

### `list_records` — the ops-console filtered read (NE-104b)

`list_records(engine, *, actor=None, action=None, start=None, end=None, limit=100,
before_seq=None)` returns records filtered by actor / action / time-window,
**newest-first** (`seq` DESC), for the ops console's `GET /v1/audit/events` filter
(which `replay` — a provenance-by-trace query — does not accept). It follows
`replay`'s discipline exactly: the denormalized columns are used ONLY in the WHERE
clause, every returned value derives from the hash-covered payload (via the same
`_record_from_row`), and `limit` is clamped to `1..=1000`. It is a **read-only**
helper — it never touches the write path, the head, or the chain. A tampered filter
column can hide/mis-select a row (which `verify_chain` detects and names) but can
never surface forged content.

`before_seq` (NE-AUDIT-EXPORT) is an OPTIONAL additive keyset cursor: when set, the
read is restricted to `seq < before_seq` (strict less-than), so repeated calls with
`before_seq=page[-1].seq` page strictly-older rows with no overlap and no gap —
`GET /v1/audit/export`'s streaming seam. Default `None` preserves every existing
caller byte-for-byte.

`count_records(engine, *, actor=None, action=None, start=None, end=None)` (NE-AUDIT-
EXPORT) returns the count of records matching the SAME actor/action/window filters
as `list_records` (built from the identical clause builder) — the export route's "how
many rows total" companion to the streamed page. Same payload-authoritative-read
discipline: the count is computed over the denormalized columns for selection only,
so a tampered column can at most hide/mis-select a row from the count, never
fabricate one. Read-only.

## pgAudit (defense in depth)

pgAudit is a PostgreSQL extension (PostgreSQL License) that logs statement-level
activity at the **database** layer. It is **complementary** to — not a substitute for
— the app-level hash chain: the chain plus signed WORM checkpoints is the
tamper-*evidence* mechanism, while pgAudit captures the raw SQL an operator or app
ran. `ne.audit.pgaudit` ships the recommended settings and `enable_pgaudit(conn)`.

pgAudit must be loaded via `shared_preload_libraries='pgaudit'` at **server start**;
the dev-stack `postgres:18.6-alpine` image does not bundle it. CI's `live-postgres`
job runs a SECOND, digest-pinned pgAudit-capable Postgres (the CNCF CloudNativePG
`standard` operand image — CI test infra only, never shipped; see `NOTICE`), and
`tests/test_pgaudit_live.py` proves real statement capture — with
`pgaudit.log_parameter = off` honored — by reading the server log directly, not
just checking the extension is installed. `enable_pgaudit` requires an AUTOCOMMIT
connection (`ALTER SYSTEM` cannot run inside a transaction); see its docstring.
To run Postgres with pgAudit under docker, add:

```
-c shared_preload_libraries=pgaudit -c logging_collector=on -c log_directory=log
```

## CLI

```
ne audit verify   --database-url sqlite:///audit.db [--start ISO] [--end ISO] \
                  [--checkpoint cp.json ...] [--json]
ne audit checkpoint --database-url … --out cp.json [--key-version N]  # NE_AUDIT_SIGNING_KEY_V{N}
ne audit replay   --database-url … (--trace-id | --event-id | --resource) [--json]
```

`verify` exits 0 on an intact chain and 1 (naming the exact breaking record) on a
tamper. With `--checkpoint`, it cross-checks local signed checkpoint files. Each file
is verified with the key **version it recorded** (P2-10): set
`NE_AUDIT_SIGNING_KEY_V{n}` (hex) per version to survive a signing-key rotation, or
`NE_AUDIT_SIGNING_KEY` as the fallback for all versions — so a `--checkpoint` cross-check
across a rotation exits 0, and a wrong/absent key exits 1 naming the failing `seq`. The
`checkpoint` command's `--key-version` (env `NE_AUDIT_SIGNING_KEY_VERSION`) records the
true version on the written document (and reads its key from `NE_AUDIT_SIGNING_KEY_V{n}`,
falling back to `NE_AUDIT_SIGNING_KEY`). The CLI writes a local file; the production
WORM/ObjectStore route is the `s3` adapter (`ne-storage`, see the "Production: the
s3 adapter" section above) consumed via the library API (`write_checkpoint`), not the
CLI.

## Testing / live-park ledger

The chain, verification, checkpoint, and replay logic run in CI against real SQLite
engines (the same SQLAlchemy statements, including the append-only triggers) and
against the `ne.contracts.testing` memory ObjectStore/SecretStore adapters. The
real-Postgres delta suite (`test_postgres_audit.py`) is env-gated on
`NE_TEST_POSTGRES_URL` and auto-skips on hosts without Docker (pending-live-verification
ledger): DB-level UPDATE/DELETE/TRUNCATE enforcement, `FOR UPDATE` head-lock
serialization under real threads, and JSONB canonical-hash stability. The pgAudit
proof is a SEPARATE, PROMOTED suite (`test_pgaudit_live.py`, env-gated on
`NE_TEST_PGAUDIT_URL`): it must-runs (not nested-skips) in CI against a
pgAudit-capable image, and a missing extension there is a failure, not a skip.
