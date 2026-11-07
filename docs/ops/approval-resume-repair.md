# Repair: approval-resume strandings and pre-deploy audit-outbox rows

**Scope.** A worked example: production state measured on a live database,
caused by the approval-resume defects (F1–F6) since fixed in the runtime:

- **3 runs stranded `running` after a SUCCESSFUL `write.external.email.send`**
  (run B, run C, run D). Each sent, then failed to
  terminalize (F2), and each still holds a `resumed` continuation checkpoint.
- **4 rows in `ne_agent_run_approval_audit_outbox` with `claimed_at IS NULL`**
  — parks that predate the park-leg drain (F4). No
  `decision_kind='approval'` Run audit receipt exists for them.

**This document performs no writes by itself.** Every statement below is to be
run by the operator, deliberately, **in the numbered order — the order is
load-bearing.** Here is why, in one paragraph:

> The fix fences each stranded continuation `consumed` so nothing can ever
> re-execute its send (Step 2). That `UPDATE` is only legal once the check
> constraint admits `'consumed'` (Step 1). The constraint migration is
> automatic — but it runs at **boot of the new code**, and on worker boot DBOS
> begins recovery/dequeue within seconds. Fencing "after boot but before
> anything runs" is therefore not a real ordering an operator can guarantee by
> hand. So for THIS upgrade — and this upgrade only — the constraint DDL is
> applied **manually, before deploy**, so the fence lands while the old code is
> still the only code that has ever run. The failure this prevents is
> irreversible and externally visible: a fourth real email to a real mailbox.
> (Section "What could re-execute a stranded run" below gives the evidence
> that the boot-time window is *believed* empty anyway — this ordering means
> nothing depends on that belief being right.)

## Step 1 — apply the constraint DDL manually, BEFORE deploying the fix

This is the sanctioned first step **for this upgrade**, not an obsolete
reference: it is what makes Step 2 executable before any new-code process ever
boots. Run against the production runtime database:

```sql
BEGIN;
ALTER TABLE ne_agent_run_continuation_checkpoints
  DROP CONSTRAINT ck_run_continuation_status;
ALTER TABLE ne_agent_run_continuation_checkpoints
  ADD CONSTRAINT ck_run_continuation_status
  CHECK (status IN ('pending','approved','resumed','consumed','rejected',
                    'expired','source_withheld'));
COMMIT;
```

This is safe under the OLD (currently running) code: the old code never writes
`'consumed'`, and a widened CHECK admits every value the old code does write.

**Every future boot and every fresh database needs no manual DDL**:
`ensure_run_schema` carries `ensure_continuation_status_compatibility`
(`packages/ne-runtime/src/ne/runtime/run_checkpoint.py`), which performs this
same migration idempotently at process start — Postgres swaps the constraint
in place, SQLite rebuilds the one table with rows preserved. Pinned by
`test_sqlite_pre_consume_continuation_constraint_is_migrated_at_boot`,
including the already-migrated no-op this Step 1 turns the deploy boot into.

## Step 2 — fence the four stranded runs CONSUMED, still BEFORE deploy

Each stranded run has `status='resumed'` on its continuation and a durable
`effect_issued` checkpoint. Under the OLD code a re-execution re-sends (defect
F1); under the NEW code a still-`resumed` continuation would *legitimately*
execute — the fix guarantees at-most-once per fence, and these rows crossed
their effect WITHOUT a fence, because none existed when they ran. Marking them
`consumed` records that durably, and makes every possible future execution
SETTLE instead of send.

⚠️ **there are FOUR, not three.** Every earlier revision
of this runbook — and every status report built from it — named three. Queried
against the live database before the deploy, `ne_agent_run_continuation_checkpoints`
holds **four** rows in `resumed`, and all four join an `approved`
`write.external.email.send` (tier 2) on a run still `status='running'`:

| run | continuation `resumed` at | approval decided | outbox row |
|---|---|---|---|
| `<RUN_ID_A>` | `day 0, <ts>` | `<ts>` | **never claimed** |
| `<RUN_ID_B>` | `day 0, <ts>` | `<ts>` | claimed |
| `<RUN_ID_C>` | `day 0, <ts>` | `<ts>` | claimed |
| `<RUN_ID_D>` | `day 0, <ts>` | `<ts>` | claimed |

Run A was missed because it ran hours earlier than the other three, and because it is one of the four
**unclaimed** approval-audit-outbox rows tracked separately as their own item —
so it was being counted in that bucket instead of this one.

**It is not distinguishable from two of the three.** Its `effect_issued`
checkpoint set is byte-identical to run C's and run D's (the same 16
hashes in the same order; run B alone has two extra). It has no `approval`
audit receipt and no post-decision `run-tool` transition — but neither does any
of the other three: **not one of the four has a post-decision `effect_issued`
checkpoint**, and all four end in `execution_failed` seconds after their
approval was decided. So the database does not record which of them actually
put a message in the mailbox; three arrivals were observed out-of-band, and
that observation is the only evidence of who sent.

**Therefore all four are fenced.** Fencing a run that never sent costs one
abandoned day-old approval; leaving one unfenced costs a real
external email to a real mailbox. The asymmetry decides it.

The block below validates, for EACH of the four typed run ids, the exact
load-bearing facts this step's justification rests on — and aborts the whole
transaction on any mismatch. A bare `status='resumed'` predicate plus a row
count is not enough: a mistyped id could fence an UNSENT continuation
(silently converting a legitimately-resumable run into a settled one — the
opposite failure from the one this step prevents), and `UPDATE 4` proves
four rows moved, not the RIGHT four. So before any write, each target must
prove it is a stranded approved-email continuation:

* the run exists and is `running`;
* its approval-join continuation is `resumed`;
* the approval the human decided names `write.external.email.send`;
* the joined attempt holds at least one durable `effect_issued:%` execution
  checkpoint — the SAME predicate the code itself uses for "this exact
  attempt durably crossed an effect issue edge"
  (`RunWorkflowStateStore.has_issued_effect`,
  `packages/ne-runtime/src/ne/runtime/run_workflow.py`). The suffix after
  `effect_issued:` is a SHA-256 of an in-process stage string
  (`_effect_checkpoint_address`); reconstructing it in SQL would need
  pgcrypto plus that literal, and existence on the exact
  `(run_id, attempt_id)` is the load-bearing fact — matching the code's own
  test beats imitating its hashing.

  ⚠️ **What this bullet does and does not prove .** It
  proves the attempt reached the effect-issuing machinery at all — which is
  what rejects a mistyped id belonging to a run that never got that far. It
  does **not** prove the *approved* send issued. All four targets carry 16
  `effect_issued` rows, every one of them written BEFORE the approval was
  requested; none has a post-decision one. So read this bullet as "not a
  never-started run", not as "post-send". The step's justification does not
  need the stronger reading: a `resumed` continuation on an approved
  `write.external.email.send` is re-executable on the new code whether or not
  it already sent, and that alone is what the fence exists to stop.

Run the `BEGIN;` and the `DO` block first, READ the four `NOTICE` lines —
they name exactly what was fenced (run, attempt, approval, action, and the
count of issued-effect checkpoints) — and only then `COMMIT;`:

```sql
BEGIN;

DO $$
DECLARE
  target_run_ids text[] := ARRAY[
    '<RUN_ID_A>',
    '<RUN_ID_B>',
    '<RUN_ID_C>',
    '<RUN_ID_D>'
  ];
  rec record;
  fenced integer;
BEGIN
  IF (SELECT count(DISTINCT t.run_id) FROM unnest(target_run_ids) AS t(run_id)) <> 4 THEN
    RAISE EXCEPTION 'the four target run ids must be distinct — check for a duplicated paste';
  END IF;
  FOR rec IN
    SELECT t.run_id AS typed_run_id,
           r.status  AS run_status,
           j.attempt_id,
           j.approval_id,
           c.status  AS continuation,
           req.action,
           (SELECT count(*)
              FROM ne_agent_run_execution_checkpoints e
             WHERE e.run_id = j.run_id
               AND e.attempt_id = j.attempt_id
               AND e.stage LIKE 'effect_issued:%') AS effects_issued
      FROM unnest(target_run_ids) AS t(run_id)
      LEFT JOIN ne_agent_runs r ON r.run_id = t.run_id
      LEFT JOIN ne_agent_run_approval_joins j ON j.run_id = t.run_id
      LEFT JOIN ne_agent_run_continuation_checkpoints c
             ON c.checkpoint_id = j.checkpoint_id
      LEFT JOIN ne_approval_requests req ON req.request_id = j.approval_id
  LOOP
    IF rec.run_status IS DISTINCT FROM 'running'
       OR rec.continuation IS DISTINCT FROM 'resumed'
       OR rec.action IS DISTINCT FROM 'write.external.email.send'
       OR coalesce(rec.effects_issued, 0) < 1 THEN
      RAISE EXCEPTION
        'target % is NOT a stranded post-send continuation '
        '(run_status=%, continuation=%, action=%, effect_issued checkpoints=%) '
        '— transaction aborted, NOTHING was fenced. Fencing a run whose effect '
        'never issued would permanently settle a legitimately-resumable run. '
        'Re-check the id against the live evidence and do NOT proceed to '
        'deploy until this step commits cleanly',
        rec.typed_run_id, rec.run_status, rec.continuation, rec.action,
        coalesce(rec.effects_issued, 0);
    END IF;
    RAISE NOTICE
      'fencing run=% attempt=% approval=% action=% effect_issued_checkpoints=%',
      rec.typed_run_id, rec.attempt_id, rec.approval_id, rec.action,
      rec.effects_issued;
  END LOOP;
  UPDATE ne_agent_run_continuation_checkpoints AS c
     SET status = 'consumed', updated_at = now()
    FROM ne_agent_run_approval_joins AS j
   WHERE j.checkpoint_id = c.checkpoint_id
     AND c.status = 'resumed'
     AND j.run_id = ANY(target_run_ids);
  GET DIAGNOSTICS fenced = ROW_COUNT;
  IF fenced <> 4 THEN
    RAISE EXCEPTION
      'fenced % continuation(s), expected exactly 4 — transaction aborted', fenced;
  END IF;
END $$;

-- Read the four NOTICE lines above. Each names one fenced identity.
-- If they are the four runs from the live evidence, and ONLY then:
COMMIT;
```

### Verified against production (rolled back)

The block above was executed against the live database inside a transaction
ended with `ROLLBACK` instead of `COMMIT`. Result, verbatim:

- **all four validated** — four `NOTICE` lines, each naming run, attempt,
  approval, `action=write.external.email.send`, and the effect count
  (run A→16, run B→18, run C→16, run D→16);
- **then the `UPDATE` was refused** by
  `ck_run_continuation_status` — `new row … violates check constraint`.

That second half is the point: **Step 1's necessity is now proven executably
against production**, not argued from the schema. Without the DDL, Step 2
cannot write a single row — it aborts whole, exactly as designed. Post-run
census confirmed nothing moved (`resumed` 4, `approved` 1, `pending` 2).

### A SECOND, INDEPENDENT reason no send can happen (verified)

The fence is not the only thing standing between this deploy and an unwanted
email — and saying so is more honest than letting one mechanism carry a risk
two mechanisms actually cover.

Every one of these runs froze an authorization snapshot at start
(`ne_agent_run_authenticated_contexts.expires_at`), and **all of them are long
expired**:

| run | frozen authorization expired | state |
|---|---|---|
| run E | `day 0, <ts>` | `waiting_approval`, approval still `pending` |
| run F | `day 0, <ts>` | `waiting_approval`, approval **approved**, continuation `approved` |
| run G | `day 0, <ts>` | `waiting_approval`, approval still `pending` |
| run A | `day 0, <ts>` | fenced |
| run B | `day 0, <ts>` | fenced |
| run C | `day 0, <ts>` | fenced |
| run D | `day 0, <ts>` | fenced |

On the new code the worker's resume workflow runs both `checkpoints.authorize`
and the effect execution inside
`authenticated_request_scope(workflow_request.authenticated_context)`
(`packages/ne-worker/src/ne/worker/workflows.py:194-202, 248-258`), and
`require_unexpired_authenticated_context` raises `AuthorizationExpiredError`
when `now >= context.expires_at`
(`packages/ne-runtime/src/ne/runtime/source_results.py:1410-1422`). The worker
catches it and calls `terminalize_authorization_expired()`. **An expired
snapshot terminalizes; it does not send.**

⚠️ **This matters most for run F, which the fence does NOT cover** — and
which the earlier hazard analysis never mentioned. It is `waiting_approval`
with an **approved** continuation and an `approved` resume intent, so it
matches `_load_pending_resume_requests_sync`'s selection exactly
(`run_checkpoint.py:1503-1529`: run `waiting_approval` AND continuation status
= intent decision). The API dispatch reconciler **will** pick it up on the
first boot of the new code. It is stopped by the expiry check, not by the
fence. The four fenced runs are excluded from that query twice over — their run
status is `running`, not `waiting_approval`, and `consumed` ≠ `approved`.

⭐ **And note WHY every one of these expired: ~15 minutes after its run
started.** That is the short-approval-window defect (backlog item 5) showing
its work — a 15-minute token behind 4h/24h policy knobs that were dead letters
at the time. These runs did not strand *and separately* expire; **the expiry is
what stranded them.**

**That class is now closed in two steps, and this runbook remains a cleanup for
the runs stranded BEFORE either landed.** ADR-0030 made the policy window
actually travel to the durable row, so the 4h/24h knobs stopped being dead
letters. F1 (`docs/architecture/approval-horizon.md`, "WHAT SHIPPED") then
decoupled the approval horizon from the login: an attempt whose owner
authorization runs out still terminalizes `authorization_expired` exactly as
these four did, but the approver's decision is now preserved as a **standing
approval** and consumed by a successor the owner's next sign-in admits. So a
run in this shape no longer costs the approver's decision — and the fence below
still applies unchanged to anything already in it.

A raise anywhere rolls back the entire transaction: either all four exact
records are validated and fenced, or nothing changes and Step 3 must not
begin. After this validation, the only id that COULD pass by substitution is
one that is itself a genuine stranded post-send email continuation — and the
`NOTICE` identities are the final human check before `COMMIT`.

Do NOT instead delete the `effect_issued` checkpoints or reset the runs to
re-execute: the recipients already received these emails once.

## Step 3 — deploy the fix and restart ne-api and ne-worker

Normal deploy (the standard bring-up). On boot,
`ensure_run_schema` finds the constraint already migrated (Step 1) and no-ops;
from the first instant any new-code process runs, the four continuations are
already `consumed`, so no execution path — recovered, re-enqueued, replayed or
human-triggered — can reach a send. It can only settle them as
`FAILED / RunEffectSettlementUncertain`, which is the honest terminal for
"sent, finalization lost".

## Step 4 — the four pre-deploy outbox rows: no manual write required

The deployed fix backfills the mandatory `decision_kind='approval'` receipt
(idempotent, epoch-stable audit address, honoring any receipt that already
exists) at every seam that can need it: the API decision routes (`_run_join`),
the worker resume step, and `RunWorkflowService.execute`. The 4 rows drain on
the next touch of each approval.

**This claim depends on the drain using the RUN's pinned config hash, and only
just became true.** Every Run pins its `config_hash` at admission, and the
finalizer refuses any audit receipt whose hash differs from the accepted Run's
(`run_finalizer.py`, "Run audit receipt config hash differs from the accepted
Run"). The first cut of `drain_run_approval_audit_intent` stamped receipts
with the CALLER's current composition hash — so across any config-hash-moving
deploy (which this deploy is), backfilling a pre-deploy park would have minted
a receipt the finalizer rejects, and "self-drain on next touch" was false for
exactly the four rows this step is about. The drain now reads the accepted run
row and stamps the run's own pinned hash, and its `config_hash` parameter was
deleted outright so no future call site can reintroduce the caller's hash
(the fix landed inside the drain itself; its baseline red was the exact
rejection quoted above). Do not execute this step's verification
against a build that still accepts a `config_hash` argument on the drain.

To verify the precondition and the result:

```sql
SELECT o.approval_id, o.run_id, o.claimed_at,
       (SELECT count(*) FROM ne_agent_run_audit_receipts r
         WHERE r.approval_id = o.approval_id
           AND r.decision_kind = 'approval') AS receipts
  FROM ne_agent_run_approval_audit_outbox o
 WHERE o.claimed_at IS NULL;
```

If an unclaimed row already shows `receipts = 1` (the crash window between
append and claim), the drain claims it without appending — pinned by
`test_drain_replay_across_lease_epochs_keeps_exactly_one_receipt`. A row
showing `receipts > 1` is the F6 double-receipt disease: the run is already
unterminalizable and the drain REFUSES loudly, leaving the intent unclaimed as
the operator's marker (pinned by
`test_drain_refuses_to_claim_over_duplicate_approval_receipts`) — resolve the
duplicate with the owner before any decision on that approval. None of the 4
live rows is in that state (they have zero receipts).

To force a drain immediately instead of waiting for route traffic, GET the
approval's payload (or POST the pending decision) through the API as an
approver — the route's `_run_join` drains as a side effect.

## Step 5 — verify

```sql
-- No stranded resumed continuations may remain on a running run.
-- Raises (instead of printing a row count for a tired eye to misread)
-- if any survive:
DO $$
DECLARE
  stranded integer;
BEGIN
  SELECT count(*) INTO stranded
    FROM ne_agent_runs r
    JOIN ne_agent_run_approval_joins j ON j.run_id = r.run_id
    JOIN ne_agent_run_continuation_checkpoints c ON c.checkpoint_id = j.checkpoint_id
   WHERE r.status = 'running' AND c.status = 'resumed';
  IF stranded <> 0 THEN
    RAISE EXCEPTION '% running run(s) still hold a resumed continuation — '
      'Step 2 did not cover them; fence before any deploy proceeds', stranded;
  END IF;
END $$;

-- Unclaimed approval audit intents: a QUALITATIVE read, deliberately not a
-- raising check — rows for runs currently parked and awaiting a human
-- decision are legitimate here, so "how many is right" depends on the live
-- approval queue at the moment you run it. Read it with the pending queue
-- open beside it; every row must correspond to a run that is genuinely
-- parked and undecided:
SELECT o.approval_id, o.run_id, o.created_at
  FROM ne_agent_run_approval_audit_outbox o
 WHERE o.claimed_at IS NULL;
```

The four fenced runs remain `status='running'` after Step 3 — inert, not
racing anything. Settling them to their honest FAILED terminal requires a
deliberate re-execution of each one's resume workflow (its DBOS workflow id is
the `resume_id` in `ne_agent_run_approval_resume_intents`; the recorded
workflow is terminal or version-stranded — see the section below — so
re-running it takes an explicit `DBOSClient.fork_workflow`, which executes
under a fresh workflow id on the current code). That is owner-paced
bookkeeping — with the fence
in place it is safe in any order at any time, and each fork can only settle,
never send.

## What could re-execute a stranded run — the premise, verified in code

The ordering above does not depend on any of the following being right; it is
recorded so the next reader knows the boot-time window is believed empty and
WHY, rather than trusting the reordering blindly.

There are exactly four candidate wake-up vectors for the four stranded runs,
and each is closed or dormant:

1. **The API dispatch reconciler** (runs at ne-api boot and continuously;
   `packages/ne-api/src/ne/api/app.py`, `_dispatch_pending_agent_runs_once`)
   enqueues two sweeps, and BOTH exclude these runs by construction:
   `load_pending_workflow_requests` requires an UNCLAIMED enqueue-outbox row
   and run status submitted/queued (`run_store.py`) — these runs' rows were
   claimed at original dispatch and they are `running`;
   `load_pending_resume_requests` requires run status `waiting_approval` AND
   continuation status equal to the intent's decision (`run_checkpoint.py`) —
   these are `running` with continuation `resumed` (then `consumed`) against
   decision `approved`, excluded on both conjuncts. Pinned by
   `test_run_approval_wait_is_atomic_idempotent_and_payload_free`, which
   asserts the sweep is empty in BOTH the resumed and the consumed state.
2. **A human replaying the approval decision.** The routes re-enqueue the SAME
   durable `resume_id` as the DBOS workflow id
   (`packages/ne-api/src/ne/api/turn_relay.py`, `enqueue_approval_resume`);
   DBOS deduplicates on workflow id, and a workflow already terminal (SUCCESS
   or ERROR) is answered from its recorded outcome, never re-executed.
3. **DBOS queue dequeue at worker boot** takes only ENQUEUED rows
   (`dbos/_sys_db.py`, `start_queued_workflows`). All four resume workflows
   provably EXECUTED — each has a post-decision `execution_failed` execution
   checkpoint, written seconds after its approval was decided — so none is
   ENQUEUED. (Note the premise is "executed", not "sent": execution is what
   ENQUEUED-ness turns on, and it is what the checkpoint proves.)
4. **DBOS startup recovery at worker boot** (`DBOS.launch()` →
   `dbos/_recovery.py`) re-executes only PENDING rows — and only those whose
   `application_version` equals the booting code's version, an MD5 of the
   registered workflow sources (`dbos/_dbos.py`, `compute_app_version`;
   neither the compose env nor `DBOSConfig` pins `DBOS__APPVERSION` /
   `application_version`, so the computed hash governs). **This fix changes
   the resume workflow's source, so the deploy rotates that version**: even if
   one of the four were left PENDING by a process death (rather than the
   diagnosed ERROR from the raised finalizer exception), the new worker's
   recovery query cannot match it — it stays dormant, it does not run.

Timing, for honesty about the window this ordering removes: had a same-version
PENDING row existed, recovery starts on a background thread within seconds of
`DBOS.launch()`, and queue polling runs at ~1s intervals — there is no
operator-speed gap between "worker up" and "recovery ran". Which of the four
workflows is ERROR vs PENDING lives in the production DBOS **system** database
and was NOT inspected (the pre-deploy audit that found the fourth
run read the application tables only; the DBOS system tables were left alone
here) — the Step 1→2→3 ordering is what makes that unknowable state harmless.

**Why not "deploy with the worker held down" (the other race-free candidate):**
`ne demo up` brings up and restarts ne-api and ne-worker
together, with no per-service hold-down flag (`tools/ne-cli/src/ne/cli/demo/`);
doing it by raw `docker compose` surgery is exactly the kind of unsanctioned
side-channel step this runbook exists to avoid — and it would still leave the
fence racing ne-api's own boot reconciler if the analysis above were ever
wrong. Two SQL statements before deploy beat a choreography.
