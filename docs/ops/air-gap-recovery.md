# Air gap: the one-way door, its recovery path, and the live-test procedure

Status: written post-demo (POST_DEMO.md items 21 and 23), after the demo-only
`NE_DEMO_UNAPPROVED_ROUTE_REPOINT` approval bypass was retired (item 20). The
air-gap beat now runs — in tests and, once the procedure below is executed, live
— exclusively through the governed Tier-2 approval path.

## 1. Why the door is one-way

The asymmetry is deliberate and lives in `tiers.rego` (Python mirror:
`ne.runtime.actions.tier_for_action`):

| Change | Action | Tier |
|---|---|---|
| **Engage** the gap | `write.internal.network.egress.restrict` | **1** — autonomous + logged |
| **Lift** the gap | `write.external.network.egress.expand` | **2** — parks for a named human approval |
| Repoint a task **in-VPC** | `write.internal.model.routing.repoint` | **2** (declared escalation over its class) |
| Repoint a task **to frontier** | `write.external.model.routing.repoint` | **2** |

Two additional facts make it a *door*, not just a toggle:

1. **Engaging freezes the repoint menu closed.** While the gap is engaged,
   `refusal_for_target` (`packages/ne-api/src/ne/api/model_routing.py`) refuses
   **every** frontier target, for every task, for everyone — approver included.
   You cannot pre-stage the way back.
2. **The posture is durable.** It lives in the `ne_egress_posture` table, is
   written in the same transaction as its chain record, and a restart of ne-api
   / ne-worker / the box does **not** clear it. There is no reset-by-reboot.

## 2. The recovery path for an engaged gap (the ONLY supported order)

Restoring frontier routing after an engage is **two kinds of Tier-2 change in a
mandatory order** — the order cannot be swapped, because step 2's refusal stands
while the gap is engaged. The refusal string itself now states this ordering
(`model_routing._refusal_for_model`), so the console menu and the 409 detail
tell the operator the same thing this document does.

**Step 1 — lift the gap** (Tier 2, one approval):

- Submit: `POST /api/v1/network/air-gap` with `{"air_gap": false}` as an
  ops-role principal. This executes `write.external.network.egress.expand`,
  answers `{"status": "pending", "request_id": <id>}`, and changes nothing yet.
- Decide: a **different** named human with the approver role posts
  `POST /api/v1/approvals/{request_id}/approve`. Separation of duties is
  enforced **in code, not by the demo's role assignment**: the requester
  recorded on the approval row can never be its approver
  (`ne.runtime.approvals.refuse_self_approval`, applied by
  `DurableApprovalGate.approve` and on both decide branches of the approvals
  route — 403, audited `api.approval.unauthorized` with
  `reason="requester-may-not-decide"`). Holding both `ops` and `approver` does
  not get you around it; proven by
  `packages/ne-api/tests/test_network_airgap.py::test_requester_holding_the_approver_role_cannot_lift_its_own_air_gap`,
  which uses exactly such a principal. Approval rows carry an `expires_at`
  deadline — decide within the configured SLA or resubmit.
- Verify: `GET /api/v1/posture` reports `"air_gap": false` before proceeding.

**Step 2 — repoint tasks back to frontier** (Tier 2, one approval per task):

- For each task to restore (the demo shape's set is `adjudicate`, `agent`,
  `agent.meridian-analyst.v2`): `POST /api/v1/models/routing` with
  `{"task": ..., "model": <frontier model>}`. A frontier target rides
  `write.external.model.routing.repoint`; each submission parks and is decided
  at `POST /api/v1/approvals/{request_id}/approve` by a distinct approver.
- Verify: `GET /api/v1/models/routing` shows the intended `model` per task,
  with real `changed_by` / `approved_by` humans on every row.

Attempting step 2 first fails by design: the repoint is refused with
"the air gap is engaged … FIRST lift the air gap …". That refusal is the
product-side rendering of this section.

**Break-glass (owner only, last resort):** if the governed lift path is broken
(e.g. the approval surface itself is down), the posture row in
`ne_egress_posture` *could* be edited directly in PostgreSQL. This bypasses the
approval gate **and** leaves the audit chain without a matching governed change
record — it converts a governance guarantee into a footnote. Do it only on the
owner's explicit say-so, record what was done and why in the project log, and
treat the follow-up fix of the governed path as a P0. It is listed here so the
option is a documented decision, not an improvisation under pressure.

## 3. Prerequisite: the Tier-2 approval path must be reliable first

POST_DEMO items 2 and 5 document that on demo day the *agent-run* approval path
stranded runs (terminalization defects; authorization snapshots expiring ~15 min
after park). The routing/egress approvals used above ride the durable tool gate
and the `/api/v1/approvals` surface, which is a different code path — but it has
never been exercised live for these actions either. Because recovery from an
engaged gap depends **entirely** on two Tier-2 approvals landing, do not engage
the gap on the live box until the check in §4 step 0 has passed.

## 4. The live-test procedure (POST_DEMO item 23 — run ONCE, after the approval work lands)

The air-gap beat has 20 green tests and zero live executions
(`packages/ne-api/tests/test_air_gap_beat.py` 9 +
`tools/ne-cli/tests/test_air_gap_beat_routes.py` 11 collected). This is the exact sequence for its
first live run. **Do not run it ad hoc**; it engages a one-way door on the
owner's working box.

**Prerequisites — all of them, checked immediately before starting:**

1. **The approval work has landed and the Tier-2 path is proven live** (step 0
   below is the proof; if it fails, stop — nothing else in this procedure may
   run).
2. **A second human (or a second browser session under a distinct approver
   principal) is available** for the whole window. Every forward and recovery
   step needs one. On this deployment that principal is the `ne-pilot` realm's
   seeded `approver` user in group `/approver` — the claim arrives as
   `approver`, matching `NE_API_APPROVER_ROLES: '["approver"]'`
   (`deploy/compose/pilot/keycloak/ne-pilot-realm.json`,
   `deploy/compose/pilot/compose.yaml`). The requester is a *different*
   principal in `/ops`.
3. **The GPU vLLM rig is up and serving** at the in-VPC address
   (`http://10.77.0.2:10200/v1`, `--max-model-len 262144`) — under the gap it
   is the *only* model lane, so a dead rig means every turn fails.
4. **The rerank lane is on the sealed CPU path** — no `--gpu-rerank` / external
   `NE_API_RERANK_ALLOWED_HOSTS` host (the
   external rerank lane and the gap are mutually exclusive; under the gap it
   fails closed and every search 502s).
5. **The box is quiet**: no demo in progress, no verification lane running,
   load average sane, swap not exhausted (POST_DEMO items 8–10), and the owner
   has explicitly OK'd the window.
6. **The audit chain verifies VALID before starting** (`/audit` header), so any
   post-test discrepancy is attributable to the test window.

**Step 0 — prove the Tier-2 approval path with a reversible change.**
Submit an in-VPC repoint of one task (e.g. `agent` → `private-default`), let it
park, wait **at least an hour**, then have the second principal approve it.
PASS: the decision lands (200), the route applies, and
`GET /api/v1/models/routing` shows the real approver. This specifically probes
the decided-hours-later case that broke agent-run approvals on demo day. Then
repoint back (frontier direction, second approval) — which also pre-verifies
the exact mechanics recovery will need.

**Step 1 — the legible refusal.** With the remaining frontier primaries still
in place, `POST /api/v1/network/air-gap {"air_gap": true}`.
PASS: HTTP 409 whose detail names every frontier route
(`adjudicate -> frontier-best`, `agent -> frontier-default`,
`agent.meridian-analyst.v2 -> frontier-default`) and says "repoint them in-VPC
first"; `GET /api/v1/posture` still reports `air_gap: false`.

**Step 2 — clear the blockers through the gate.** Repoint all three frontier
primaries to `private-default`, each parking and each approved by the second
principal. PASS: all three rows show `model: private-default` with real
`changed_by`/`approved_by`.

`adjudicate` is the one to watch, and **what makes its repoint legal changed on
2026-09-01** (NE-CTX-IN-FORCE, `c21b739f`). There is no longer a `frontier-best`
window cap and no config-time budget floor: `frontier-best` declares its real
1 000 000-token window (input budget **925 424**) against `private-default`'s
**224 460**, and `refusal_for_target` never compares the two windows at all.
The only window arm left is `_snapshot_fit_refusal`
(`packages/ne-api/src/ne/api/model_routing.py`), which measures the **target's**
input budget against the largest live conversation snapshot on that task
(`ModelRouteStore.snapshot_tokens_for` →
`AgentRunStore.largest_live_context_snapshot_estimate_sync`, read from the
durable boundary receipts). So read a 409 from `POST /api/v1/models/routing`
this way:

- **A refusal quoting counts** — `model 'private-default''s context window
  cannot safely accept the current conversation snapshot: snapshot_tokens=…
  input_budget=224460` — is **not** a regression. It is a live fact about this
  box: some conversation on that task has outgrown the rig lane's window. There
  is no governed compaction operation and no silent truncation
  (SPEC PRE-DEMO-014), so the repoint stays refused until the *target's* window
  exceeds that snapshot. The only real lever is the rig — `private-default`'s
  window is its `--max-model-len` (262 144 on the shape in prerequisite 3) — so
  re-serve wider, re-run `ne demo up --gpu-vllm …`, and retry. Do **not** work
  around it by opening a fresh conversation: a task repoint governs every
  conversation on the task and the gate reads the largest one. If the window
  cannot be raised far enough, **stop** — a blocker that cannot be cleared means
  the gap can never be engaged, and there is nothing left for this procedure to
  prove.
- **A refusal that compares against anything other than the target's own
  window** — one naming `adjudicate`'s current primary, a boot-primary budget,
  or the words "budget floor" — means the deleted config-time floor has come
  back. That is POST_DEMO item 22's failure mode: frontier primaries with no
  legal in-VPC target, so the gap can never engage. **Abort the procedure** and
  fix the regression; `tools/ne-cli/tests/test_air_gap_beat_routes.py`
  (`test_air_gap_stays_reachable_every_frontier_primary_is_repointable`,
  `test_frontier_best_declares_its_own_window_and_stays_repointable`) is the
  guard that should have caught it before you got here.
- **A console target greyed out but the POST accepted** is availability, not
  routing: only the catalog view ANDs `selectable` with the
  inference-lifecycle read (`packages/ne-api/src/ne/api/routers/models.py` —
  `selectable=t.selectable and _availability(t.model)[0]`), and the POST
  pre-flight does not. That is prerequisite 3 failing. Check
  `GET /api/v1/models/serving` and fix the rig before going further, because
  under the gap that lane is the only one left.

**Step 3 — engage.** Same submit as step 1. PASS: `{"status": "applied"}` with
no new approval row (Tier 1), and posture reports `air_gap: true`.

**Step 4 — both halves of the enforcement, live.**
- Private lane alive: run a chat turn on a private-routed task; it completes on
  the rig.
- Frontier lane dead *before transport*: attempt a frontier call (e.g. select a
  frontier model where the picker offers one). PASS is a typed refusal
  (`AirGapViolationError` surface), not a timeout. Strongest evidence: during
  the engaged window, `tcpdump`/conntrack on the box shows **zero** new flows
  to `api.anthropic.com` (or any non-VPC address) from the app containers.
- Menu frozen: `GET /api/v1/models/routing` marks every frontier target
  unselectable with the lift-first refusal text.

**Step 5 — durability.** Restart ne-api. PASS: posture still `air_gap: true`
after boot. (Then follow the post-restart warm-up discipline from
POST_DEMO's process notes before any knowledge beat.)

**Step 6 — recovery, exactly per §2.** Lift (Tier-2 approval), verify posture
false, repoint the three tasks back to their frontier primaries (Tier-2
approvals), then run one frontier-routed turn end-to-end.
PASS: the turn completes and the audit chain still verifies VALID, with every
change in the window carrying a real requester and a real, distinct approver.

**What counts as an overall pass:** every PASS line above, plus zero approval
rows minted by the two engage submissions, plus no break-glass use. Any FAIL
stops the procedure — and if it fails *after* step 3, §2 is the way back;
that is why step 0 proved it first.

**Record the outcome** (pass or fail, with request_ids and timestamps) in the
project log — this procedure retires POST_DEMO item 23 only when its evidence
is written down.
