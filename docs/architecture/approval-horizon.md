# The Approval Horizon: authorization-snapshot expiry vs. real-world approval latency

- **Status:** ⭐ **BUILT (Option F1) — SPEC amended, shipped on `ne/standing-approval-f1`.** The owner
  decision this document requested was given on 2026-09-08: *"yes update the fucking spec. Ive told
  you approval windows need to be configurable, potentially many hours."* Read
  **"WHAT SHIPPED"** at the bottom before anything else here — sections 1-4 are the ANALYSIS that
  led to F1 and several of their sentences describe the pre-F1 world on purpose (they are the
  argument, not the current behaviour). Two facts that are now false anywhere they still appear
  unqualified: the approval horizon is no longer bounded by the run owner's token, and a decision
  made past that horizon is no longer discarded.
  ⛔ Also still true, and NOT superseded by F1: the park-time **coherence check**
  (`ne.runtime.middleware._require_honourable_approval_window`) and the deployment window document
  (`deploy/opa/data/ne/config/data.json`, **180 s, derived**) are BUILT and still load-bearing —
  see "What remains" items 2/2b below and "The interim park-time coherence check" at the bottom.
  ⛔ **A clamp WAS built and REVERTED (PR #938), and the reason belongs here.** Clamping the durable
  `expires_at` to the owner's authorization horizon looks like it only makes the printed deadline
  honest. It does not: `expires_at` is what `_require_actionable_row` (`approvals.py:1132`) tests, so
  moving it makes the row expire at the horizon as an ORDINARY approval timeout. The approver then
  gets a generic "expired before approval" and the run reconciles to `RunState.EXPIRED`, instead of
  the specific `authorization_expired` reason with an atomic terminalization that the spec
  requires. It lowered a governance guarantee in the name of honesty.
  **It was also redundant:** PR #934 already solved the display problem correctly —
  `routers/approvals.py:482` serves `authorization_expires_at` on the pending queue beside the
  request deadline, so the approver is already shown the real horizon without touching `expires_at`
  and without collapsing two distinct failure reasons into one. **The lesson: the two clocks must
  stay two clocks.** Anything that makes one of them stand in for the other loses the reason.
- **Date:** 2026-08-28. **Branch:** `ne/design-approval-horizon` (base `ne/demo-train-63` @ `739d1777`).
- **Problem:** POST_DEMO.md P0 §5 — no Tier-2 approval can be granted more than ~15 minutes
  after park, because the run owner's frozen authorization snapshot expires with the access
  token that admitted the run. Train-63 made this fail loud (`authorization_expired`, 409 at
  decision time), which is correct; it also makes the approval feature unusable for approvals
  that take hours or days, which is what real approvals take.

## Recommendation (three sentences) — ADOPTED AND BUILT

*(Kept verbatim because it is the argument the build rests on. What it calls "a SPEC amendment
[so] this is an owner decision, not something to ship from this document" HAS since been made:
the amendment sits in the spec immediately after the delayed-identity RED paragraph, and the
build is described under "WHAT SHIPPED".)*

Keep every expiry guard exactly as it is and stop trying to make the *old* run survive:
adopt the successor path SPEC already names — "only a new authenticated retry may run"
— by making an approval decided after the owner's snapshot expired become a
durable **standing approval** that a successor attempt, admitted under a **newly
authenticated request from the run owner**, consumes on exact `(owner principal,
action_hash, policy_hash, protected_invocation_hash)` match, executing the already-frozen
pinned effect under a fresh unexpired owner context with live stamp re-checks. This
weakens no RED item — the expired snapshot is never revived, rebound, or replaced by the
approver or the service identity; the successor carries its own verifier-authored context
with *current* groups, which is strictly more honest than any lifetime extension — but it
requires a SPEC amendment defining standing-approval carryover (today an approval binds
one `(run_id, attempt_id)` and dies with it), so this is an owner decision, not something
to ship from this document. The config lever (`accessTokenLifespan`, live at 900s) is the
only thing that works *today* and it cannot honestly reach hours: every step multiplies the
replay window of a stolen stateless bearer and the staleness window of every governed read
platform-wide, so treat anything beyond ~1800s as a governance weakening, not a fix.

---

## 1. What the expiry actually protects (the honest statement of the tension)

### 1.1 The mechanism

- `ne_agent_run_authenticated_contexts` (`packages/ne-runtime/src/ne/runtime/run_store.py:520-538`)
  is INSERT-ONLY (trigger-guarded, run_store.py:3591-3640), written once at run admission
  (`_insert_authenticated_context`, run_store.py:4781-4805) with the submitting bearer's
  verifier-authored `AuthenticatedRequestContext`: opaque principal id, exact sorted groups,
  optional `department`, issuer, and the token's absolute integer `exp` as `expires_at`.
- Every run admitted under one console token therefore shares one absolute `expires_at`
  (measured ~12–15 min of remaining lifetime; the realm now mints 900s tokens — §5).
- The check re-fires at every delayed seam: worker admission
  (`packages/ne-worker/src/ne/worker/workflows.py:379`), every protected materialization
  and model send (`ProtectedSourceResultGuard.authorization()`,
  `packages/ne-runtime/src/ne/runtime/source_results.py:1400-1421`), the approval resume
  (workflows.py:171-193, :436-448), and — since train-63 — the approve endpoint itself
  (`packages/ne-api/src/ne/api/routers/approvals.py:89-140`), which refuses with 409
  `authorization_expired` and atomically terminalizes the parked attempt rather than
  handing the approver a 200 for a continuation that is already dead.
- **Deny is not horizon-limited.** `reject` (approvals.py:570) never calls
  `_refuse_expired_authorization`; denial needs no owner execution context and works at any
  time. Only *approve → execute* is bounded by the owner's snapshot.

### 1.2 What the bound means, in the spec's own words

The platform has no revocation or entitlement service:

> "Pre-demo adds no current user-entitlement or JWT revocation service." — the spec

So the token's `exp` is the **only honest bound** on the claim that the owner's groups are
still current:

> "A signed token's group claims remain authoritative to the serialized worker until its
> expiry: an external group removal or token revocation that the platform cannot observe
> mid-lifetime is not claimed current." — the spec

> "At or after expiry it executes/discloses nothing, atomically makes any parked
> approval/checkpoint non-actionable and terminally fails the attempt with safe reason
> `authorization_expired`; retry requires a newly authenticated request and never silently
> rebinds the old Run." — the spec

> "Retry requires a newly authenticated request — the runtime never silently rebinds an
> expired snapshot to a fresh one." — `AuthorizationExpiredError` docstring,
> source_results.py:192-197

And the spec's RED list, verbatim:

> "Repeat with expiry while a Run approval is parked; **a fresh approver decision cannot
> revive the expired effective-user execution context, and only a new authenticated retry
> may run**. Removing/rounding the expiry check, accepting absent `exp`, treating a
> permission-store group deletion as if it changed an unexpired signed-token Principal,
> **refreshing/rebinding the old Run from the approver token, or authorizing as the
> service identity is RED**. A separate newly issued token without the former group is
> denied on its new request; the still-valid original token remains honest within its
> declared lifetime."

### 1.3 The tension, stated without euphemism

Two truths collide:

1. **The bound is real.** Live source-ACL drift *is* re-checked at every disclosure (the
   resolver "re-checks every retained authority stamp", source_results.py:236-237),
   but those stamp evaluations are made **against the snapshot's groups**. Past `exp`, the
   platform can no longer claim those groups describe the owner. Executing an external
   write (an email leaves the building) hours later under a frozen group set would be an
   unbounded assertion the platform cannot back. That property must survive.
2. **Human approval latency is hours to days.** The whole point of a Tier-2 gate is that a
   different human, on their own schedule, decides. A gate whose approve button dies in 15
   minutes is not a human-in-the-loop feature; it is a race.

The resolution cannot be "make the snapshot live longer" (that stretches truth 1) and
cannot be "let someone else's freshness stand in" (RED by name). The only direction that
keeps both truths is: **let the decision outlive the attempt, and require fresh owner
authority for the execution** — which is the direction the spec itself points with "only a
new authenticated retry may run."

---

## 2. The option space, judged against the RED list

### Option A — re-authenticate as a service / client-credentials identity. **RED, by name.**

"authorizing as the service identity is RED"; reinforced elsewhere in the spec:
"`agent:workflow-drafter` and connector identities are execution/audit identities only and
cannot replace the user's token-bound source authorization." The service identity has no
claim at all about the *owner's* entitlements. Dead on arrival, correctly.

### Option B — treat expiry as non-fatal because the approver freshly authenticated. **RED, by name.**

"a fresh approver decision cannot revive the expired effective-user execution context";
"refreshing/rebinding the old Run from the approver token ... is RED".
The approver's freshness proves the *approver's* groups, not the
owner's. Dead on arrival, correctly.

### Option C — refresh/extend the stored context at park (incl. a server-held refresh token). **RED.**

Any variant in which the platform, without a new authenticated request from the owner,
swaps a fresher `exp` into (or beside) the stored snapshot is exactly "silently rebinds an
expired snapshot to a fresh one" (source_results.py:192-197) and mutates an INSERT-ONLY
record whose immutability is itself trigger-enforced. The server-held **refresh token**
sub-variant deserves an honest look because it is *technically* a re-authentication: a
refresh grant goes back to Keycloak, which re-reads local-realm group membership at
issuance, so the minted token is genuinely current for local groups. It still fails, three
ways: (1) it is machine-initiated, not "a newly authenticated request" — the owner
expressed nothing; (2) the platform would now hold long-lived replayable credentials for
every user who ever starts a run — a credential-custody surface the architecture
deliberately does not have (the bearer is never persisted; queued work carries only the
snapshot); (3) for the pilot's brokered-Entra groups, refresh does
**not** re-sync the IdP (syncMode FORCE fires at broker login) — the "fresh" token would
carry Entra groups as of the last interactive login while *claiming* freshness, which is
worse than the honest frozen snapshot. RED in letter for the rebind, and rejected on its
own merits for the custody surface.

### Option D — the pure-config lever: raise `accessTokenLifespan`. **Legal. Bounded. Cannot reach the requirement.**

Already pulled once: commit `c390cc42` pinned 900s in both realm imports (Keycloak default
300s was shorter than a single 400s-budget run) and the live `ne-pilot` realm now mints
900s tokens (§5). This is the only lever that changes nothing in code and it is spec-legal
by construction — "the still-valid original token remains honest within its declared
lifetime". But it scales linearly in the wrong currency: the lifespan is
simultaneously (a) the replay window of a stolen bearer, (b) the staleness window of every
group-contraction event platform-wide, and (c) the approval horizon. You cannot buy (c)
without paying (a) and (b) everywhere. Full blast radius in §4. Verdict: 900s is right;
1800s is the most that is defensible as a stopgap *if the owner explicitly accepts the
doubled window*; hours is a governance weakening dressed as configuration. Note also the
realm declares no `ssoSessionIdleTimeout`/`ssoSessionMaxLifespan`, so Keycloak defaults
(30 min idle / 10 h max) apply — silent renewal already stops 30 minutes after the owner
goes idle, so a long access token would be the *only* live credential in exactly the
scenarios that matter.

### Option E — genuine re-authentication of the run owner at decision time. **Collapses into Option F.**

What would it require? The owner is, by hypothesis, not present at decision time — that is
the entire reason the horizon hurts. The three sub-cases:
- *Owner present, interactive re-auth:* possible, but then the owner is issuing a new
  authenticated request anyway — that is Option F's retry, not a revival of the old run.
- *Silent re-auth via the owner's live SSO session (refresh grant / prompt=none):* this is
  Option C's machinery with Option C's problems, plus it only works inside the 30-minute
  idle window, which does not cover the requirement.
- *Re-consent ceremony (owner clicks "release my approved run" and logs in fresh):* this is
  precisely the sanctioned "new authenticated retry", i.e. Option F with good UX.

There is no fourth sub-case in which the owner re-authenticates without making a new
request. Spec-legality is therefore not the obstacle; the obstacle is that "re-auth the
owner" *is* the retry path, and the real design question is how the retry inherits the
standing approval instead of re-parking into a second 15-minute race. That question is
Option F.

### Option F — the standing approval + fresh-owner successor. ⭐ **BUILT (F1). SPEC amended.**

> **AS BUILT, 2026-09-08.** F1 shipped; F2 did not and is not needed. Two details below are now
> stale as written and the shipped behaviour is authoritative: the "Approver freshness question to
> settle explicitly" was settled by the SPEC amendment (identical-hash carryover does NOT re-ask the
> approver, and the staleness ceiling IS the policy-configured standing lifetime), and "(iii) a
> bounded standing lifetime (e.g. N days, owner-config)" is resolved as
> `tiers.rego`'s `standing_approval_seconds`, resolved beside `approval_window_seconds` from
> `data.ne.config.standing_approvals`. See "WHAT SHIPPED" at the bottom.

The insight: everything the continuation needs is already durable and already frozen at
park time under then-valid owner authority. `create_wait`
(`packages/ne-runtime/src/ne/runtime/run_checkpoint.py:356-415, :600-660`) persists, in one
transaction: the encrypted protected invocation plaintext, its hash, the `action_hash`, the
`policy_hash`, the checkpoint bound to `(run_id, attempt_id, step_id, tool_call_id)`, and
the **stamp receipt** — the exact ordered `SourceAuthorityStamp` set. The resume already
executes exactly one pinned effect (POST_DEMO.md P0 §4). Nothing about the *content* of the
approved effect depends on the old run staying alive.

Design:

1. **Decision recording is decoupled from continuation viability.** An approver's decision
   past the owner's expiry is durably recorded under the approver's own fresh authority
   (approving/denying is an approver-authorized act; disclosure of the payload to the
   approver already has its own revocation-checked path). The expired attempt still
   terminalizes `authorization_expired` exactly as train-63 built — never revived. The
   approval row becomes a **standing approval**: decided, unconsumed, bound to
   `(owner_principal_id, action_hash, policy_hash, protected_invocation_hash)`.
2. **The successor.** The run owner's next authenticated request — a console retry, offered
   automatically whenever the owner next holds a fresh token — admits a new run/attempt
   with a new verifier-authored context (current groups, new `exp`). Two sub-variants for
   how it meets the gate:
   - **F1, re-park + auto-satisfy (preferred — reuses the most machinery):** the successor
     re-runs and parks at the Tier-2 gate normally. At park, if a standing approval exists
     for the identical binding tuple, the gate consumes it and resumes immediately instead
     of waiting. If the re-run produced a *different* action (model nondeterminism → new
     `action_hash`/invocation hash), there is no match and it parks for a fresh human
     decision — fail-safe by construction.
   - **F2, frozen-invocation successor (no model re-run):** the successor admission names
     `successor_of_approval_id`; the platform skips the loop, re-materializes the stored
     protected invocation, re-checks every retained stamp against **current** authority
     under the **new** context, re-evaluates tier policy, and executes exactly the frozen
     effect. Deterministic (no hash-mismatch lottery), cheaper (no model spend), but it is
     a new execution path and it must not become a way to run an effect whose inputs the
     owner could no longer produce — the live stamp re-check and current-groups policy
     evaluation are the defense, and both already exist as seams.
3. **Every RED item survives untouched:** the expiry check is not removed or rounded;
   absent `exp` is still refused; a group deletion still does not mutate an unexpired
   Principal; nothing is refreshed or rebound from the approver token; the service identity
   authorizes nothing; the old run stays terminally `authorization_expired`. The successor
   is exactly the spec's "new authenticated retry" — the only sanctioned survivor.
4. **Honesty is *improved*, not preserved-at-cost:** the effect executes under groups that
   are current as of the owner's new login — including, for the pilot, a fresh
   Entra-broker sync — whereas every lifetime-extension scheme executes under groups
   frozen at the *original* login.

What SPEC must change (nothing currently permits this): today the Run-approval join binds
one approval to one `(run_id, attempt_id)` and the C2 exit criteria treat an approval as
consumed-or-dead with its attempt. The amendment must define: (i) a standing approval as a
durable decided-unconsumed state reachable only via `authorization_expired` termination of
its original attempt; (ii) the exact-match consumption rule (same owner principal, same
`action_hash`, same `policy_hash`, same `protected_invocation_hash`, consumed at most
once, with the audit receipt linking successor to predecessor and to the original
decision); (iii) a bounded standing lifetime (e.g. N days, owner-config) after which it
expires to `approval_lapsed` rather than standing forever; (iv) that a Deny needs no
successor (already true in code). Approver freshness question to settle explicitly: F1
applies a decision made hours earlier to a bit-identical re-parked action — SPEC should
state that identical-hash carryover does not require re-asking the approver, or require a
staleness ceiling on the decision itself.

### Option G — split "authority to read the served sources" from "authority to execute the approved effect", executing under the approver. **RED in spirit; rejected.**

The tempting version: the payload was disclosed to the approver under revocation-checked
approver authority; the effect is frozen; let the *decision itself* execute it, carrying
the approver's fresh context plus the park-time record of owner authority. This is not
literally on the RED list (it does not rebind the owner's snapshot), but it is within a
word of it: executing the owner's effect because the approver is fresh is functionally "a
fresh approver decision reviv[ing] the expired effective-user execution context".
The effect discloses source-derived content externally *on the
owner's behalf*; the owner's group-currency is the claim being made, and the approver's
token says nothing about it. Adopting G would require rewriting that RED sentence — a
governance weakening — when Option F achieves the same latency profile without touching
it. The *useful* residue of the split is real, though, and F relies on it: read-authority
(approver payload disclosure, revocation-checked, works today at any time) and
deny-authority (works today at any time) are already horizon-free; only execute-authority
is bounded, and F re-sources it from the owner instead of borrowing it.

### Option H — a longer-lived credential class scoped only to parked effects. **Not literally RED; weaker than F; rejected.**

Mint at park (dedicated Keycloak client / token exchange) a narrow "parked-effect voucher":
audience = the resume seam only, bound to `(approval_id, action_hash)`, lifetime = days.
This is not the RED park-time refresh of the *context* — it is a new credential making a
deliberately weaker claim: "the owner held these entitlements at park and authorized this
one effect." But that claim is exactly what the platform refuses to let stand for hours:
it converts `exp` from "the bound on group-currency" into "a bound we selectively waive
for the highest-tier actions" — the most dangerous effects would get the *stalest*
authority. It also creates a new long-lived bearer artifact to store, rotate, and steal,
in a system whose queued work deliberately carries no credential.
Every property H buys, F buys more honestly by getting a genuinely fresh context. Reject.

### Option I — build the missing revocation/entitlement service. **The long-term fix; changes the premise; not the unlock.**

The spec's "no current user-entitlement or JWT revocation service" is scoped
"pre-demo". Post-demo, a real entitlement seam (Keycloak backchannel logout events, or a
periodic signed group-attestation the worker may consult) would dissolve the tension at
its root: `exp` would stop being the *only* honest bound, and a parked continuation could
be legal for as long as the attestation stays current, with revocation actually
enforceable mid-flight — strictly stronger governance than today, not weaker. This is a
platform-sized effort with its own failure modes (the attestation service becomes the
lie-single-point) and its own SPEC rewrite. It should be on the roadmap; it should not
gate fixing approvals, because F works within the current premise.

### Mitigations worth shipping regardless

- **Show the horizon.** The console should render the decision deadline (owner snapshot
  `expires_at`) on every pending approval, and the approve button should disable *before*
  the cliff instead of 409ing after it. Cheap, honest, no SPEC contact.
- **Say the right thing on 409.** The `authorization_expired` refusal should tell the
  approver what to do next. ⭐ **Both halves shipped**: PR #934 landed the "your decision was not
  recorded" wording, and F1 replaced it with the truth it now has to tell — *"Your decision WAS
  recorded and stands until <lapses_at>: when the owner next signs in and retries this exact
  action, it runs without asking you again."* The pre-F1 wording survives for the one case where
  it is still true: a park that recorded no policy standing lifetime.
- **Never claim a terminalization that did not happen.** `terminalize_authorization_expired`
  is fail-closed: outside its `actionable` set it writes nothing and answers `False`.
  `_refuse_expired_authorization` used to DISCARD that answer and emit
  `api.approval.authorization_expired` plus the "nothing was sent or written" 409 regardless —
  so an approval a sweep had already flipped to `expired` (checkpoint still `pending`, run still
  `waiting_approval`) produced an audit row asserting a terminalization that never occurred,
  while the run stayed parked forever. Three live runs sat in exactly that state. It now branches
  on the result: terminalized ⇒ unchanged; NOT terminalized ⇒ the ordinary "refused, nothing
  changed" record (`api.approval.stale`, reason `authorization-expired-not-terminalizable`) and a
  409 that says this request changed nothing and the run needs an operator. The `actionable` set
  was NOT widened — the guard is right, the caller was lying about it.
- **⭐ THE RUN IS RECONCILED BEFORE THE DECIDER IS JUDGED, and no refusal may be hoisted above
  that.** On the run-join decide path a decide call reaching `_refuse_expired_authorization` is
  the ONLY thing that terminalizes a run whose owner authorization ran out — there is no sweep
  that finishes it. So any refusal placed *ahead* of that call does not merely reorder two
  refusals: it deletes the terminalization, and the run is stranded in `waiting_approval` with
  nothing left able to finish it. This is not hypothetical — the separation-of-duties check
  (`refuse_self_approval`) was first written hoisted above both decide branches, which stranded
  exactly the runs this section exists about whenever a dual-role requester decided an expired
  request. Expiry is a fact about the **run**; entitlement is a fact about the **decider**; the
  run goes first. Nothing is granted by that ordering: `decider_refused` turns the F1 standing
  carryover off (a decider who may not decide has produced no approver-authority evidence) and
  the decider still gets their 403, with both facts on the audit trail —
  `api.approval.authorization_expired` for the run, `api.approval.unauthorized` /
  `requester-may-not-decide` for the decision.
- **Deny stays horizon-free** — already true; keep it that way in any rework.

---

## 3. Recommendation and its security argument

*(Adopted. F1 built; F2 remains unbuilt and unneeded — the re-park path proved sufficient.)*

**Recommend Option F (standing approval + fresh-owner successor), sub-variant F1 first**
(re-park + auto-satisfy: smallest new surface, fail-safe on any drift in the re-produced
action), with F2 as a follow-on once durable pinned-effect resume (P0 §4's durable-loop
work) exists anyway. Keep `accessTokenLifespan` at 900s. Do not raise it as part of this
fix; if the owner wants a same-sitting bridge before F lands, 1800s is the ceiling worth
discussing, with §4 read aloud first.

Security argument, compactly:

1. **No claim is ever made beyond its evidence.** The original attempt dies at `exp`
   exactly as today. The standing approval asserts only "this approver approved this exact
   effect-hash" — an approver-authority claim, evidenced by the approver's fresh token at
   decision time. The successor's execution asserts "the owner currently holds these
   groups" — evidenced by the owner's new token. At no point does any component assert the
   owner's *past* groups are *current*.
2. **The RED list is untouched**, item by item (§2-F.3). The design is an implementation
   of the spec's own escape clause, not an exception to it.
3. **The failure direction is closed.** Hash mismatch → new park, new human decision.
   Standing approval lapsed → new park. Owner lost entitlement in the interim → the
   successor's own current-groups policy evaluation and live stamp re-checks refuse, with
   the existing typed refusals. Every unknown degrades to "ask a human again", never to
   "execute anyway".
4. **What is genuinely conceded:** the approver's decision is allowed to be older than the
   execution it authorizes (bounded by the standing lifetime, and voided by any change in
   the effect's bytes). That concession is inherent to asynchronous human approval — it
   exists in every real-world approval system — and it is the *approver-side* analogue of
   the exact concession SPEC already makes on the owner side ("remains honest within its
   declared lifetime"). SPEC must say so out loud; that is the owner decision this
   document requests.

**The single change that unlocks real-world approvals with the least security loss:** the
standing-approval consumption rule (F1) — it converts the approval horizon from ~15
minutes to the standing lifetime (days) at the cost of one owner re-authentication and
zero changes to any expiry, rebind, or identity rule.

---

## 4. Blast radius of the config lever (why 12 h bearers are not an option)

`OidcTokenVerifier` verifies signature and `exp` **statelessly** — no introspection call,
no session lookup — and the spec guarantees there is no revocation service to consult.
Raising `accessTokenLifespan` to cover real approval latency (say 12 h) therefore means:

- **A stolen bearer is replayable for 12 h against every endpoint**, from any network
  position, and *nothing on the platform can shorten that* — not logout (the SSO session
  dies; the access token does not), not disabling the user in Keycloak, not an incident
  response runbook. Today's exposure is 15 minutes; the change is ~48×.
- **Every group contraction is unobserved for up to 12 h, platform-wide** — not just for
  parked approvals. An employee moved off `bank.customer.read` at 09:00 reads customer
  documents until 21:00, with receipts that correctly cite an unexpired token. The spec's
  honesty framing survives ("honest within its declared lifetime") while the substance it
  protects is hollowed out: the declared lifetime stops being a meaningful bound.
- **Tier-2's premise inverts.** The approval gate exists because external writes are the
  highest-consequence acts; funding their horizon by widening *every* read's staleness
  window spends governance where it is most valuable to buy convenience where it is most
  dangerous.
- **The cliff moves; it does not disappear.** Approvals at hour 11.9 still 409. Real
  approval latency is unbounded (vacation, escalation); no lifespan value closes the gap,
  so the lever would be re-pulled each time it embarrasses — a ratchet with no stop.
- **Second-order effects on this codebase:** the console renews 60 s early and gates run
  starts on a 420 s freshness floor (`RUN_CREDENTIAL_MIN_REMAINING_SECONDS`,
  `packages/ne-console/src/api/client.ts`, bound to the realm by
  `packages/ne-identity/tests/test_realm_config.py::test_access_token_lifespan_covers_run_credential_floor`);
  those numbers assume lifespan ≫ floor but lifespan ≈ minutes. A 12 h token also outlives
  the default SSO session idle timeout (30 min) by 24×, making the access token — the one
  artifact designed to be short — the longest-lived credential in the system.

Bounded statement for the owner: each doubling of `accessTokenLifespan` doubles (replay
window × affected surface = everything). 900 → 1800 s is the largest step for which "the
approver was at lunch" plausibly gets covered while the loss stays within one order of
magnitude of today's posture. Beyond that, use Option F.

---

## 5. Record correction shipped with this document

The PR #910 idle-console work recorded the live measurement
"`exp - iat` is exactly 300s because neither the pilot nor the dev realm document declares
`accessTokenLifespan`". True when measured (2026-08-26); stale since: commit `c390cc42`
(2026-08-28) pins `accessTokenLifespan: 900` in both realm imports
(`deploy/compose/{dev,pilot}/keycloak/*.json`), and the live `ne-pilot` realm now mints
900 s tokens (confirmed live 2026-08-28; recorded in POST_DEMO.md P0 §5, which also flags
this stale record). A dated correction is appended to that row in this branch — the
original measurement is preserved, not rewritten.

## Appendix: reference map

| What | Where |
|---|---|
| Snapshot table (INSERT-ONLY) | `packages/ne-runtime/src/ne/runtime/run_store.py:520-538`, guard triggers :3591-3640 |
| Snapshot written at admission | run_store.py:4781-4805 (`_insert_authenticated_context`) |
| Snapshot loaded for decisions | run_store.py:5880-5908 (`load_authenticated_context`) |
| Expiry refusal class | `packages/ne-runtime/src/ne/runtime/source_results.py:192` (`AuthorizationExpiredError`) |
| Guard expiry check | source_results.py:1374-1421 (`ProtectedSourceResultGuard.authorization`) |
| Decision-time 409 guard (approve only) | `packages/ne-api/src/ne/api/routers/approvals.py:89-140`; reject (:570) is horizon-free |
| Worker resume terminalization | `packages/ne-worker/src/ne/worker/workflows.py:171-193, 436-448`; admission check :379 |
| Park writes (frozen invocation + hashes + stamps) | `packages/ne-runtime/src/ne/runtime/run_checkpoint.py:356-415, 600-660` |
| RED list | the spec's RED list, delayed-identity truth, and no-revocation-service statement |
| Realm lifespan (900 s) | `deploy/compose/{dev,pilot}/keycloak/ne-*.json:4`; pinned by commit `c390cc42`; structural bind `packages/ne-identity/tests/test_realm_config.py` |

---

# THE OWNER REQUIREMENT, 2026-08-30 — and how it landed

**Read this before acting on anything above.** The owner's requirement is that approval time is a
**configurable property of the policy engine, set per action alongside that action's risk tier** —
never hard-coded, and **never tied to the auth token lifetime**. Different actions and deployments
get different windows: 24 hours for some, 10 minutes for others.

That reframes the problem this document was written to solve. Sections 1-3 treat the access-token
expiry as an immovable constraint and spend most of the option space working around it (Options A-C
and E-G are all "how do we survive the login expiring"). **The owner's answer is that the clock
should not be there at all.** Governance latency is a property of the action, and inheriting it from
session length is the defect — not a fact to design around.

⛔ **An earlier revision of this section said "Do NOT implement Option F." That instruction is
withdrawn, and the reasoning behind it was half right.** Making the window policy-configured was
necessary and it was the right thing to build first — but it was NOT sufficient, and this document
said so itself in item 2 below: the window reaching the durable row does not stop the OWNER's frozen
authorization snapshot from expiring underneath it, so a 4 h window on a 15-minute session was a
promise the platform could not keep. "Configurable, potentially many hours" is unreachable while the
horizon is a property of a login. F1 is what makes the configured number true, and the owner ruled
on 2026-09-08 that it ships. The two are complements, not alternatives: **policy says how long,
F1 makes how long possible.**

## What was built (policy half — done, `a0672c1b`; carry — done, ADR-0030, 2026-09-02)

`tiers.rego` resolves `approval_window_seconds`, most specific first: exact action override →
per-tier override → built-in floor (tier 2 = 4 h, tier 3 = 24 h). Config lives at
`data.ne.config.approval_windows`, outside the rule namespace. Zero and non-numeric overrides are
ignored rather than honoured — a zero window would make every approval instantly stale, the exact
failure being removed.

**History of the emission, because it went wrong twice before it landed:** `a0672c1b` emitted the
window into `decision.obligations`; CI's parity suite caught that `obligations` is the Rego/Cedar
parity surface (Cedar obligations are STATIC `@ne_obligations` annotations per rule — a
data-resolved per-action value cannot live there) and `5186d579` reverted the emission the same
night. A consuming car then shipped reading `decision.obligations["approval_window_seconds"]`
anyway — a value nothing ever emitted — so `_approval_window_s` returned `None` on every path that
ever executed and the fallback to `cfg.expiry_s` fired everywhere; the #944 drop record retracts
the claim that this worked, and an earlier version of this section still said "`decision.rego`
emits it on all four require-approval rules" while nothing did.

**As of bundle 1.3.0 / contracts 1.27.0 (ADR-0030) the window actually travels:** `decision.rego`
emits `approval_window_seconds` FIRST-CLASS on its four `require-approval` rules (`null` on
allow/deny), the OPA client parses it, both PDPs carry it on the typed
`PolicyDecision.approval_window_seconds` field (the `tier` precedent), the Cedar evaluator mirrors
the same resolution order in its marshaller (constructor-supplied `approval_windows` mapping, same
shape as the OPA data document — the parity suite proves the two mechanisms agree, overrides
included), `middleware.py` reads the FIELD, binds `approval_window_scope`, and BOTH park paths
honour it — `_submit_fresh` (conversation) and the canonical Run `create_wait` site, which until
this change silently kept `cfg.expiry_s`. Red-then-green proved end-to-end in
`test_approval_window_policy.py` (real PDP → real middleware → durable row) and
`test_middleware_with_durable_gate.py` (Run park).

## What remains

1. *(BUILT, ADR-0030 — kept for the design rationale.)* **Carry the window in an AMBIENT SCOPE,
   not a new keyword.** *(Corrected 2026-08-30 after
   trying the keyword and measuring what it broke.)*

   The obvious route — a keyword on `gate.submit`, mirroring `args_hash` — was implemented and
   **withdrawn**: it broke three `test_workflows_api.py` tests with `TypeError`. The reason is
   worth stating because it defeats the obvious workaround too. Those tests do
   `monkeypatch.setattr(state.gate, "request_approval", _boom)` — they patch a method **on a real
   `DurableApprovalGate`** — so `middleware.py`'s `isinstance(gate, DurableApprovalGate)` guard,
   which is what protects protocol-typed fakes from `args_hash`, still passes and the keyword is
   still handed to a stand-in that never declared it. Widening every stand-in (8+ files) would be
   fragile busywork against a signature that should not have grown.

   **The codebase already solves this shape four times over.** `current_action_scope()`,
   `current_conversation_attempt()`, `current_run_tool_call()` and `current_write_approval_floor()`
   are all ambient context values used precisely to avoid widening frozen signatures — and
   `submit()` already reads two of them. So: set the policy-resolved window in an ambient scope at
   the park site in `middleware.py`, and read it inside `_submit_fresh` next to the scope reads it
   already does, where `expires_at = now + timedelta(seconds=cfg.expiry_s)` (`approvals.py:534`)
   becomes `… seconds=window_s or cfg.expiry_s`. **No signature changes, no fake breakage, and it
   matches the four existing precedents rather than inventing a fifth mechanism.**
2. **BUILT — the park-time coherence check.** (Was "STILL OPEN"; this section is the correction.)

   **What it does.** `_require_canonical_run_approval` resolves the policy window exactly as
   before, and then — BEFORE `create_wait`, so before any approval row, checkpoint, queue entry or
   approver minute exists — compares it against the run owner's bound
   `AuthenticatedRequestContext.expires_at`. If the owner's authorization horizon ends before
   `now + window`, **and the window was DECLARED by this deployment**, the park is refused with the
   typed `ne.runtime.errors.ApprovalWindowUnhonourableError`, which carries a `safe_reason`; the run
   terminalizes FAILED through the ordinary generic-failure path and publishes that sentence
   verbatim in place of the derived floor ("the failing step published no explanation. Recorded
   failure class: …"), which is the sentence the deployed box shows today. No bound snapshot means
   no horizon to compare and the check returns — not a bypass, since the canonical Run park always
   runs inside `authenticated_request_scope` and every protected seam refuses independently.

   ⭐ **WHAT IT REFUSES, AND WHY THAT IS NARROWER THAN THE FIRST CUT.** A window arrives at this
   comparison from one of three places, and only the first is a decision anyone made:

   1. `data.ne.config.approval_windows.actions[<action>]` or `.tiers[<tier>]` — a number this
      deployment **wrote down**;
   2. `tiers.rego`'s built-in floor (14400 s / 86400 s) — a number the *product* ships because
      every require-approval decision needs one;
   3. `ApprovalSettings.expiry_s` — the runtime fallback when policy named no window at all.

   The bundle emitted (1), (2) and (3)'s antecedent on the same field, so the first cut could not
   tell them apart and **refused approvals that succeed on `main`**. Bundle 1.4.0 adds
   `approval_window_declared` (`tiers.rego` → `decision.rego` → `PolicyDecision` →
   `approval_window_scope` → the guard), and the guard now fires **only on (1)**. On (2) and (3) it
   emits a **log warning** on the `ne.runtime.middleware` logger — a log line, never an audit-chain
   row — naming `(action, resolved window, owner horizon)` and lets the park
   proceed exactly as it did before this check existed — terminalizing `authorization_expired` at
   the resume seam if the approval is slow, which is the pre-existing, already-handled outcome.

   **The reasoning, because it inverts the obvious one.** Refusal here buys **legibility, not
   safety**. The guard protects no integrity property: a park that outruns the horizon executes
   nothing unsafe — its worst case is that already-handled `authorization_expired`, its best case is
   a human who answers in time and a run that completes. Failing closed is right when proceeding is
   *dangerous* and wrong when proceeding merely risks a *legible failure*. **A floor is not a
   promise anyone made**, so refusing on one punishes an unmade decision and costs successes.

   ⭐ **AND THE FRAMING INVERTS WITH IT: the 180 s declaration is the actual fix for the measured
   12.7%, not this check.** The declaration makes the durable row's `expires_at` 180 s, so a slow
   approval dies as a **visible approval expiry** instead of a four-hour promise stranding as
   `authorization_expired` at minute fifteen. Remove the declaration and the defect returns intact
   with the guard still in the tree. **The guard is demoted to a tripwire on declared promises.**

   ⛔ **AND "JUST DECLARE A WINDOW FOR EVERY PARKABLE ACTION" IS NOT AN ALTERNATIVE — it is
   unreachable, not merely expensive.** The workflow write leg (`write.internal.crm.note` under
   `write_approval_floor_scope(TIER_2)`) reaches the guard with **no policy window at all**: the
   ToolSpec declares tier 1, the PDP is asked about tier 1, it answers `allow` with a null window,
   and the tier-2 floor is applied afterwards by `_effective_tier`, which the PDP never sees. No
   entry in the data document can intercept that chain, because the chain never consults the
   document. Provenance is the only thing that can make this guard safe to run.

   ⛔ **THE CANONICAL RUN PARK ONLY, and that was tested rather than assumed.** `execute()` binds
   `approval_window_scope` around `_require_named_approval`, which is the single entry to the
   canonical Run park *and* to the conversation park (`DurableApprovalGate.request_approval` →
   `_submit_fresh`), so it is tempting to check both. **It was tried and reverted on evidence:**
   checking there refused parks the platform completes today — **ten existing
   `test_workflows_api.py` proposals went RED**, all of which approve and execute under a one-hour
   token against the 14400 s floor.

   This check compares the two clocks *because a second, durable clock enforces the horizon at
   resume*: `ne.worker.workflows` re-binds the run's admitted context around every execution step
   and terminalizes `authorization_expired` past it. **The conversation park has no such seam** —
   `submit()` + `await_outcome()` with no later execution step whose authorization is re-checked.

   ⚠️ **CORRECTION — the mechanism, stated accurately.** Earlier revisions of this document, the
   ADR and the middleware docstring all said that park is "awaited **inline** in the request that
   created it". **It is not.** `ne.api.workflows` runs `_park` via `asyncio.create_task`
   (`workflows.py:784`); the HTTP request returns as soon as `new_request_id` observes the row, and
   the task polls `await_outcome` in the background. The *conclusion* stands — nothing on that path
   **enforces** the horizon, because there is no resume seam re-binding the owner's admitted
   context — but the reason is the missing seam, not inlining. ⚠️ **Pre-existing residual, neither
   introduced nor closed here:** an approved conversation effect can therefore fire **past** the
   owner's authorization expiry.

   **What it discloses, and the earlier defect it avoids.** The published sentence names the
   CONFIGURED WINDOW and the ACTION — both public deployment policy, the window already emitted
   first-class on every decision — and the bare fact that the horizon is shorter. It never carries
   the horizon, the remaining seconds, or the overrun: `overrun_seconds = window − remaining` makes
   the owner's exact token expiry solvable from public policy config, which is what sank the first
   cut. (`routers/approvals.py:482` separately serves `authorization_expires_at` to a NAMED
   APPROVER on a run-bound row; a run record has a different audience and now discloses strictly
   less than that queue does.) A test pins the non-derivability and dies if the overrun is ever
   appended.

   **It refuses; it does not clamp.** `expires_at` stays exactly where policy put it — moving it is
   PR #938, reverted, for the reason recorded at the top of this document. The two clocks stay two
   clocks; this check is the first thing that compares them.

   ⚠️ **WHAT IT DOES NOT DO: it does not make a four-hour park possible.** An UNCONDITIONAL check
   would refuse EVERY tier-2 Run park — `tiers.rego`'s built-in tier-2 floor is 14400 s against a
   900 s session — which is what the first cut shipped and what the provenance condition above
   fixes. Even with that condition, the check by itself changes nothing about the four-hour promise:
   what changes it is item 2b's DECLARED window. Read the gap below before reading either as a fix.

2b. **BUILT — the deployment's own approval-window document, and the number is DERIVED.**

   `deploy/opa/data/ne/config/data.json` declares
   `data.ne.config.approval_windows.actions = {"write.external.email.send": 180}`. ⭐ **Per ACTION,
   not per tier, and that is a decision.** `tiers.rego` resolves action-first, then tier, then the
   built-in floor, so a `tiers: {"2": 180}` entry would apply this derivation to *every* tier-2/3
   approval on *every* path — including the conversation park
   (`DurableApprovalGate.request_approval` → `_submit_fresh`), which has no durable resume seam for
   the horizon to collide with (it is dispatched as a background `asyncio.create_task`, not awaited
   inline — see the correction under item 2). That path would have gone from the 4 h/24 h floor to three minutes, with its escalation SLA derived
   down to 90 s (`resolve_approval_deadlines_s`), for a reason that says nothing about it. The
   derivation covers one situation — the pre-park stretch of a Run executed by an agent holding a
   tier-2 write grant — so it is declared for exactly the action that describes, today the only
   tier-2-or-higher grant any agent in `deploy/agents/registry.toml` holds. Everything else keeps
   the built-in floor, byte-for-byte as before this train. **The cost is paid explicitly:** a
   per-tier entry would have silently covered a NEW tier-2 grant, a per-action one does not, so
   `test_the_declaration_is_scoped_to_the_actions_it_derives` pins that every declared window is
   within the derived ceiling and that `write.external.email.send` still carries one. ⚠️ It no
   longer claims to ENUMERATE the parkable set, and that claim was false rather than merely
   incomplete: the guard-reachable parkable set is `{write.external.email.send}` **plus the workflow
   `crm.note` legs**, and the latter is invisible to any grant-classification enumeration because it
   is **floor-driven, not grant-driven** — an ambient `write_approval_floor_scope(TIER_2)` on a
   tier-1 action, exactly the park the first cut of the guard refused. Under the declared-only scope
   completeness is no longer a refusal condition, so the tripwire is the ceiling and the existence
   pin, not a set equality. It is staged into the served
   bundle by
   `ne.cli.opa_bundle.stage_govern_policies` (so `ne up` and `ne demo up` both carry it, the
   staged-bundle content hash covers it, and a missing document is a loud bring-up error rather
   than a silent fall-back to the unhonourable floor), and mirrored into the K8s ConfigMap by
   `govern_bundle_files` so the two tiers cannot disagree about whether an approval can be parked
   at all. It deliberately does NOT live under `packages/ne-govern/policies`: that directory is the
   rego test surface — `opa test` and `opa eval -d` load it whole, and `decision_test.rego`'s
   built-in-floor cases assert 14400/86400 by relying on `approval_windows` being ABSENT.

   **The derivation, which is the whole content of the choice:**

   **Every input is read at its ENFORCEMENT site — none is an observed sample:**

   | input | value | source |
   |---|---|---|
   | run-credential renew trigger | **420 s** | `RUN_CREDENTIAL_MIN_REMAINING_SECONDS`, `packages/ne-console/src/api/client.ts` — the console renews the token below this before starting a run |
   | enforced per-attempt wall clock | **230 s** | the LARGEST `max_steps × _SECONDS_PER_STEP` among the agents that can actually park a tier-2 decision — every agent in `deploy/agents/registry.toml` whose `tool_grants` hold an action `tiers.rego` puts at tier 2 or higher. Two agents qualify today and they **tie** at the maximum: `agt_01k3customersupport000000000` (`write.external.email.send`) and `agt_01k3inertparkselftest0000000` (`write.external.demo.send`, raised 6 → 23 by NE-INERT-BUDGET so it could survive the one governed read its park requires — see the registry entry's budget derivation), both `max_steps = 23` × `_SECONDS_PER_STEP = 10` (`packages/ne-api/src/ne/api/agent_execution_caps.py`), hard-cancelled in `ne.runtime.runner` |
   | clock-skew leeway | **10 s** | `OidcConfig.leeway_seconds`, `packages/ne-identity/src/ne/identity/config.py` — verification already spends this either side of `exp` |
   | ⇒ remaining-at-park | **180 s** | `420 − 230 − 10` |

   The check parks iff `window ≤ remaining-at-park`, so **180 s is the largest honourable value**.
   `packages/ne-runtime/tests/test_approval_window_is_honourable.py` re-runs this arithmetic from
   its inputs — reading the 420 out of the console source, the `max_steps` out of the agent
   registry, and the leeway out of `ne.identity.config`, rather than retyping any of them — and
   drives the real guard with the derived horizon. ⭐ **The SET of parking agents is derived too,
   and that is what makes 230 a bound rather than a coincidence**: an earlier cut selected the
   agent by literal id, which enforced 230 without bounding it — a tier-2 write grant moving to (or
   appearing on) a longer-budgeted agent would have left every assertion green while the real
   subtrahend grew. Now **raising `max_steps` on any agent that holds a tier-2 write grant reds the
   test, and so does granting a tier-2 write action to an agent with a larger budget** (the drift
   that actually happened to this registry in generation 19). Raising the window by one step or
   lowering the console trigger red it the same way. It does NOT pin an agent with no such grant —
   raising `max_steps` on the read-only Meridian Analyst is correctly invisible, because that agent
   cannot park a tier-2 approval.

   ⛔ **WHAT THIS NUMBER IS NOT: A WORST-CASE BOUND — and it is no longer described as one.**
   `RUN_CREDENTIAL_MIN_REMAINING_SECONDS` is a **fail-open renew trigger, not a floor**:
   `client.ts:1115-1125` returns early when no renew hook is installed and swallows a failed renew
   (*"Fail-open to the current token; expiry stays server-enforced"*), and it binds the **console
   only** — a run started from the API or the CLI never passes through it and has no such trigger
   at all. So nothing derived from it bounds the worst case. **180 s is the largest window that
   holds when the console's renew succeeds.** An earlier revision of this section claimed the
   value was "chosen against the worst case, not the median"; that was **not true of the method**
   and is corrected here rather than left standing.

   **Declared residuals — all four, and none papered over with an invented safety constant:**

   1. ⭐ **a run admitted on a fail-open renew, or started outside the console, can still refuse at
      park** — the derivation does not cover it;
   2. ⭐ **the window reserves NOTHING for the resumed attempt.** 230 s is *one* attempt's enforced
      wall clock — the pre-park stretch. After the approver decides, the resume runs under the same
      cap again, so at the maximum park time the resumed attempt begins with essentially none of the
      owner's authorization left and the delayed-seam guard refuses it. **180 s is the largest window
      that can be PARKED honourably; it is not a window inside which a park is guaranteed to
      RESUME.** Reserving a second 230 s would make `420 − 230 − 230 − 10` negative — i.e. *no*
      honourable window at all on this deployment — which is the finding, not a reason to drop the
      term. The narrower version of this, a decision landing in the final moments racing the resume
      seam (the guard refuses at `now >= expires_at`, and the resume happens *after* the decision),
      is the same residual at its most benign;
   3. **dispatch latency is an omitted term.** Nothing between the console's freshness check and the
      parking attempt's first step — admission, enqueue, worker pickup — is subtracted, and the
      headroom at the agent's full budget is `420 − 230 − 180 = 10 s`, already spent on leeway.
      Measured pre-loop work is ~0.11 s, so it errs safe today; it is bounded by nothing;
   4. **the leeway input is a class default, not a deployed value.** `IdentitySettings` is
      pydantic-settings (`env_prefix="NE_IDENTITY_"`, `env_nested_delimiter="__"`), so
      `NE_IDENTITY_OIDC__LEEWAY_SECONDS` overrides it anywhere in the validator's 0–60 range; at 60
      the honourable ceiling is 130 s and a shipped 180 would be unhonourable. Nothing under
      `deploy/` sets it, and the window test now **asserts** that rather than assuming it — unlike
      420 and 230, this input is anchored by a negative, so the anchor is what has to hold.

   ⛔ **AND THE GAP, STATED IN THE OPEN — the claim is NOT being lowered quietly to match the code.**
   The promise was **4 hours**. The deliverable on this deployment is **180 seconds**. That is an
   **80× shortfall**, and 180 s is not a human approval latency: it is enough for an owner sitting
   at the keyboard during a supervised test, and not enough for anyone who has to walk away. What
   it would take to close it is exactly one thing — **durable run-scoped authorization**, so the
   parked effect stops depending on the owner's session being alive. That contradicts
   the spec verbatim ("a fresh approver decision cannot revive the expired effective-user
   execution context, and **only a new authenticated retry may run**") and the spec's
   atomic `authorization_expired` terminalization, so it needs an **owner decision and a SPEC
   amendment**, not a branch. Raising `accessTokenLifespan` is not the alternative: the owner ruled
   twice that the window is per-action policy and must never be tied to session credential lifetime,
   and §4 above is the security case.

   **The tier-2 floor of 14400 s in `tiers.rego` is therefore itself a promise the platform cannot
   keep**, and that is now said at the floor rather than only here. It is kept at 4 h/24 h on
   purpose — those are what an approval window *should* be, and what durable run authorization would
   make deliverable — but the comment beside it, the Cedar mirror's, and ADR-0030 all now state that
   no shipped deployment can honour them and that every deployment must supply its own document.

3. ⭐ **CLOSED BY F1 — the four-hour promise itself.** (Was "STILL OPEN"; the status is updated
   here rather than left standing.) Two honest exits were named, and **BOTH have now been taken**:
   (a) ⟵ **TAKEN, at 180 s (item 2b).** Configure the window down to what the session can keep and
   accept that "approval time is policy" means three minutes on this deployment; and
   (b) ⟵ **TAKEN as Option F1**, the standing approval + fresh-owner successor — the SPEC amendment
   and owner decision this exit required were given on 2026-09-08, so it is no longer "must not be
   shipped from a branch". Note that F1 is NOT the durable-run-authorization design sketched below:
   it carries the DECISION forward, never the owner's authorization, which is why every RED item
   survives it. The sketch below is retained as the rejected alternative.
   (b, as originally written) build **durable run authorization** — the run carries its own grant, minted at park, scoped
   to the approved action, with its lifetime governed by `approval_window_seconds` — which is
   Option H above, and which contradicts the spec verbatim ("only a new authenticated
   retry may run") and its atomic `authorization_expired` terminalization. It needs
   a SPEC amendment and an owner decision; it must not be shipped from a branch.
   **Raising `accessTokenLifespan` is not an exit.** The owner ruled twice that the approval window
   is a per-action policy setting and must never be tied to session credential lifetime, and §4
   above is the security case: the lifespan is simultaneously the replay window of a stolen
   stateless bearer and the staleness window of every governed read platform-wide.

   Item 3's earlier claim — "then the auth coupling is a configuration problem, not an architecture
   problem" — is **half true and is corrected here**: it is a configuration problem only if the
   deployment is willing to configure a window it can keep. If the deployment means hours, it is an
   architecture problem, and (b) is its name.

---

# ⭐ WHAT SHIPPED: Option F1, standing approval + fresh-owner successor (2026-09-08)

**One sentence:** an approver's decision is now durable evidence that OUTLIVES the attempt it was
asked for, and the owner's next login re-runs the action and consumes that decision — so an
approval window of hours or days is real, while every expiry, rebind and identity rule stays exactly
where it was.

## The two clocks, stated once

| | `approval_window_seconds` | `standing_approval_seconds` |
|---|---|---|
| Question | how long the HUMAN has to answer | how long that ANSWER outlives the attempt |
| Measured from | park | the decision |
| Past it | the request expires; nobody can decide it | the carryover lapses to `approval_lapsed` |
| Resolved by | `tiers.rego`, most specific first | `tiers.rego`, the same order, beside it |
| Deployment config | `data.ne.config.approval_windows` | `data.ne.config.standing_approvals` |
| Built-in floor | 4 h (tier 2) / 24 h (tier 3) | **7 d (tier 2) / 2 d (tier 3)** |

The standing floors are ASYMMETRIC on purpose and the asymmetry is the governance statement: what a
standing approval concedes is that the decision may be OLDER than the execution it authorizes, and
that concession has to be smallest exactly where the effect cannot be undone. Tier 3 is
financial/legal and irreversible, so it gets the SHORTER carryover, not the longer one.

Both travel identically (bundle **1.5.0** / contracts **1.29.0**): first-class keys on the four
`require-approval` rules, `null` on allow/deny; parsed by the OPA client; mirrored by the Cedar
evaluator's marshaller from a separate `standing_approvals` constructor argument (a mirror that
silently read the window's document would have passed every existing parity test while collapsing
two independent policy answers — hence its own parity cases); carried on typed `PolicyDecision`
fields; bound ambiently by `middleware.py` and frozen into the park.

## The mechanism, in the order it runs

1. **Park.** `_require_canonical_run_approval` freezes the policy-resolved standing lifetime into
   the INSERT-ONLY `ne_agent_run_approval_joins.standing_lifetime_s`. The terminalization seam has
   no live PDP call, so the answer policy gave AT PARK is the one that binds. NULL ⇒ **mint no
   standing approval** — never "standing forever". A park that recorded no policy lifetime behaves
   exactly as it did before F1.
2. **The attempt dies, and the decision does not.** `terminalize_authorization_expired` now writes,
   IN THE SAME TRANSACTION that makes the approval and checkpoint non-actionable and fails the
   attempt `authorization_expired`, a row in `ne_agent_standing_approvals` bound to
   `(owner_principal_id, action_hash, policy_hash, protected_invocation_hash)` and lapsing at
   `decided_at + standing_lifetime_s`. Two sources and only two: a named human decided PAST the
   horizon (the decide route passes a `StandingDecision`), or the approval row is already `approved`
   and unconsumed (decided in-window, resume crossed the horizon). A `pending` row with no decision
   mints nothing — nobody answered, so there is nothing to carry.
3. **The successor.** The owner's next authenticated request is an ORDINARY new run — there is no
   new admission API and no "release my run" ceremony, because the spec's "new authenticated
   retry" is already exactly that. It re-runs, re-parks at the same gate, and
   `_satisfy_from_standing_approval` runs three steps whose ORDER is the security property:
   **find (and lapse)** → **re-check every stamp live, under THIS attempt's context** → **consume**.
   Only then does it `resume` and cross the one-approval-one-execution fence.
4. **Every drift degrades to a fresh human decision.** Different hashes: no index match. Lapsed:
   CAS to `approval_lapsed`, and the lapse is RECORDED rather than ignored. Already consumed: the
   partial unique index and the one-way CAS both refuse. Owner lost source authority at re-login:
   the stamp re-check refuses BEFORE consumption, so the carryover is still standing and the park
   waits for a human. There is no branch anywhere that answers "execute anyway".

## One approval continuation per run, and why the successor made that visible

A satisfied park RETURNS and the agent loop CONTINUES — that is the whole difference F1 makes at
this seam, and it is what the flagship test asserts (`result.steps`: the run went on to its final
answer after the send). An UNsatisfied park still raises `RunApprovalWaiting` and ends the loop, so
before F1 the only way past a park was a human decision and a resume.

What the successor reached is a limit that was already there and had never been tested — on any
path, including the resume — because nothing in the tree had ever driven a second approval-gated
tool call in one Run. Both
`ne_agent_run_continuation_checkpoints.run_id` and `ne_agent_run_approval_joins.run_id` are UNIQUE.
**A Run may hold ONE approval continuation, ever.** The uniqueness is load-bearing — TWO seams read
`(run_id, attempt_id)` as an at-most-one address and strand the Run if it is not:
`RunWorkflowStateStore.resumed_approval_id` names the ONE effect a resume may execute and raises
when it finds two, and `RunApprovalCheckpointStore.find_join_for_attempt`, the drain that gives every
park its mandatory audit receipt, is a `one_or_none()`. `RunFinalizer._observed_approvals` is NOT a
third one, and an earlier draft of this section wrongly counted it: it ITERATES the joins for the
attempt, and its "exactly one" is one audit receipt PER JOIN — a bar N joins would each clear. Either
of the two real seams strands the Run on its own, so relaxing the constraint would move the
stranding, not remove it.

So the limit is now STATED where it is reached rather than enforced by a UNIQUE index that aborts a
half-written transaction. A second approval-gated tool call in a Run that already holds a
continuation is refused at `create_wait` with `RunApprovalContinuationExhaustedError`, under the Run
lock and before anything durable is written. It is an `ApprovalIntegrityError`, so the runner treats
it as a governance block: the first effect stands, the second becomes a `BLOCKED:` tool result the
model is told about, and the run reaches its own terminal. Nothing is stranded — which is exactly the
defect `terminalize_authorization_expired` exists to close, and it must not come back through the fix
for it.

## What the tuple does not contain — checked, and not checked

The binding tuple is `(owner_principal_id, action_hash, protected_invocation_hash, policy_hash)`.
`action_hash` digests the ARGUMENTS and `policy_hash` digests the BUNDLE VERSION, so several things
a reader might assume are bound are not. Two of them are CHECKED anyway, beside the tuple. Three are
NOT, and this list is exhaustive as of this change.

**Checked beside the tuple:**

- **The tool name** and **the tier**. A different registered tool called with byte-identical
  arguments, or the same tool escalated to Tier-3 by provisional context, matches all three hashes
  exactly. Both are compared for EQUALITY at consumption, against the successor's insert-only join
  AND against the standing row. Equality, not "at least": a Tier-2 decision may not satisfy a
  Tier-3 gate, and a Tier-3 decision is not evidence that a Tier-2 ceremony was performed. This is
  the same lesson `_require_canonical_run_approval` already records for the resumed-continuation
  branch.

**NOT checked — the honest list:**

- **The evidence set.** `stamp_receipt_id` / `stamp_receipt_hash` — the identity of the exact set of
  protected reads the effect was justified by — live on the continuation checkpoint and are on
  NEITHER the standing row nor the tuple. A successor that reached byte-identical arguments from a
  DIFFERENT set of sources consumes the decision. What still holds: every stamp of the PARKED
  invocation is re-evaluated live under the successor's own context before consumption, so the
  successor must still be admitted to the sources the decision was made against. What does not: the
  approver is not shown, and nothing compares, that the successor's own reading matched theirs.
- **The agent.** No agent id is in the tuple or on the standing row. Any agent the owner can run,
  granted the same tool, producing the same arguments under the same bundle version, consumes the
  decision.
- **The approver's CURRENT standing.** Consumption reads `approver` (and `second_approver`) off the
  standing row verbatim and re-verifies nothing about them. `check_decision_authority` at the
  successor re-checks the OWNER's authority over the parked invocation, not the approver's. So an
  approver whose entitlement was revoked after they decided still has that decision honoured, for up
  to the standing lifetime. This is the same shape as the concession SPEC already makes on the owner
  side (a token "remains honest within its declared lifetime"), applied to the approver — and the
  standing lifetime is the only thing that bounds it.

## Why no RED item moved

- The expiry check is not removed, rounded or relaxed; absent `exp` is still refused.
- The predecessor attempt stays terminally `authorization_expired`. Nothing is revived, rebound,
  refreshed, or re-authorized as the service identity. The successor is a DIFFERENT run with a
  DIFFERENT attempt and its own verifier-authored context.
- The standing approval asserts one thing — *this approver approved this exact effect* — evidenced
  by the approver's own fresh token at decision time. It never asserts anything about the owner's
  groups. The successor's execution asserts *the owner currently holds these groups*, evidenced by
  the owner's NEW token. At no point does any component claim the owner's past groups are current.
- Denial is untouched and preserves nothing: *"Denial requires no successor and is never
  horizon-bounded."* A standing DENIAL would authorize nothing, and a successor that finds no
  standing approval already parks for a fresh human decision — which is what a denial should
  produce.

**What is genuinely conceded**, and SPEC now says it out loud: the approver's decision is allowed to
be older than the execution it authorizes, bounded by the standing lifetime and voided by any change
in the effect's bytes. That concession is inherent to asynchronous human approval and it is the
approver-side analogue of the concession SPEC already makes on the owner side ("remains honest
within its declared lifetime").

## The park-time coherence check: BUILT (PR #969), and F1 does NOT make it vacuous

An earlier revision of this section said this check "is not on `main`" and that "F1 removes its job
rather than completing it." ⭐ **Both halves were wrong, and the correction is this section.** PR
#969 merged: the check is `ne.runtime.middleware._require_honourable_approval_window`, it refuses
only a window this deployment DECLARED, and it warns-and-parks on the built-in floor and on the
`cfg.expiry_s` fallback (items 2/2b above). F1 was written against `15f82ada`, before that landed,
which is why it assessed a guard it had never seen.

**The vacuity argument, stated at its strongest, because it is nearly right.** Under F1 an approver
who answers PAST the owner's horizon is no longer ignored: the decide route terminalizes the attempt
and preserves the decision as a standing approval, and the owner's next login consumes it. So for a
declared window on a tier-2/3 park, the human really does get the declared time and their answer
really is acted on. On that path the guard refuses a configuration whose failure F1 has already made
recoverable.

**Why it still has a job anyway — two reasons, both concrete:**

1. ⭐ **The declared window is a promise about THIS attempt; the carryover is a different thing the
   deployment did not ask for.** A deployment writing `{"write.external.email.send": 180}` is saying
   "this park expires in three minutes". What it gets when the horizon is shorter is not that: it is
   an attempt that dies early, a standing approval good for **seven days**, and an effect that fires
   only if the owner happens to log back in and retry. That may be a better outcome, but it is not
   the declared one, and the deployment that wrote the number down is the party entitled to be told
   its number cannot be kept — at zero cost, before any approval row, checkpoint or approver minute
   exists.
2. ⛔ **F1's rescue is CONDITIONAL, and the condition fails on exactly the path the guard must not
   refuse.** Minting requires a non-NULL park-time `standing_lifetime_s`, frozen from the PDP's
   `standing_approval_seconds`. Route 1 — the workflow write leg, `write.internal.crm.note` under an
   ambient `write_approval_floor_scope(TIER_2)` — reaches the park with **neither clock**: the
   ToolSpec declares tier 1, the PDP is asked about tier 1 and answers `allow` with a null window
   AND a null standing lifetime, and the tier-2 floor is applied afterwards by `_effective_tier`,
   which the PDP never sees. `_standing_lifetime` reads NULL and mints nothing, so on that path a
   decision made past the horizon is **still lost, exactly as it was before F1**. That is also the
   path whose park must NOT be refused (`test_a_workflow_write_leg_parks_instead_of_being_refused`),
   so the guard warns there and F1 does not help there. The two mechanisms do not cover each other.

**So the check stays, unchanged in shape:** refuse on a declared window, warn-and-park on a floor.
It is not vestigial — it fires on the same condition it always did — and it is not a coupling F1
dissolved: F1 lowered the COST of the incoherence on one path and left it untouched on another.

**The comparison that is still NOT built** (and this part of the earlier section was right): a
declared **window** longer than the declared **standing lifetime** for the same action. That is a
pure policy-vs-policy check needing no session, and it belongs with whoever lands a deployment
`standing_approvals` document — nothing ships one today, so every standing lifetime in the tree is
the 7 d / 2 d floor and the comparison has nothing to compare yet.

**And the second thing that document cannot yet say: OFF.** `tiers.rego` ignores a zero, negative or
non-numeric `standing_approvals` entry and falls through to the floor, so a deployment cannot
disable the carryover for one irreversible action — writing `0` for it buys 2 days, not none. The
two clocks fail open in opposite directions and only one of them is benign: a WINDOW falling back to
a floor fails open to a longer time for a human to answer; a STANDING LIFETIME falling back to a
floor fails open to seven days of the one concession this feature makes. It is left open
deliberately — the value the runtime already reads as "mint nothing" is `null`
(`_standing_lifetime`), and getting `null` out of a TOTAL Rego rule and through `decision.rego`, the
contracts validator, the Cedar mirror and its parity vectors is its own change with its own tests —
and it is unreachable today, because no deployment document declares `standing_approvals` at all.
The reasoning is repeated at the rule itself in `tiers.rego`.

## Deployment configuration — one open thread

`deploy/opa/data/ne/config/data.json` — the OPA data document carrying `approval_windows` — is **on
`main`** (it shipped with PR #969, merged as `3ec191ce`), and this branch edits it in place: it
declares `approval_windows.actions["write.external.email.send"] = 180`, and its only two keys are
`_derivation` and `approval_windows`. **It carries no `standing_approvals` key**, so every standing
lifetime in the tree today is `tiers.rego`'s built-in floor — 7 d tier 2, 2 d tier 3 — and nothing
in this repo declares one.

So the deployed tier-2 window for that action is **three minutes, not four hours**: the built-in 4 h
floor applies only to actions this document does not name, and it names the one action any agent in
`deploy/agents/registry.toml` is granted at tier 2 or higher.

**The open thread is the 180 itself.** F1 removes the reason it was three minutes — the window is no
longer bounded by the owner's token — but F1 did not raise it, and residual (e) of the document's own
`_derivation` says so: every input to `420 - 230 - 10 = 180` is unchanged, and
`_require_honourable_approval_window` still refuses a DECLARED window longer than the owner's
remaining authorization at park. Raising the number to the hours the owner asked for is a deliberate
edit to that file plus a re-derivation, and it is not part of this change.

## The config lever, finally settled

`accessTokenLifespan` stays at 900 s. §4's blast-radius argument is unchanged and is now moot as a
*fix*: nobody has to trade the platform-wide replay window for approval latency, because approval
latency no longer comes out of that budget.

## Where it lives

| What | Where |
|---|---|
| SPEC amendment | the spec, immediately after the delayed-identity RED paragraph |
| Policy resolution (both clocks) | `packages/ne-govern/policies/ne/govern/tiers.rego` |
| Decision emission | `packages/ne-govern/policies/ne/govern/decision.rego` (bundle 1.5.0) |
| Cedar mirror + parity | `cedar/evaluator.py` `_standing_lifetime_for`; `tests/test_cedar_parity.py` §6 |
| Typed field | `PolicyDecision.standing_approval_seconds` (contracts 1.29.0) |
| Ambient carry | `ne.runtime.correlation.standing_lifetime_scope` |
| Park freeze | `ne_agent_run_approval_joins.standing_lifetime_s` |
| Durable carryover | `ne_agent_standing_approvals` (`run_checkpoint.py`) |
| Mint | `RunApprovalCheckpointStore._mint_standing_approval`, inside `terminalize_authorization_expired` |
| Find / lapse / consume | `find_live_standing_approval`, `consume_standing_approval` |
| Successor consumption | `middleware._satisfy_from_standing_approval` |
| Decide-route preservation | `ne-api/routers/approvals.py` `_refuse_expired_authorization(preserve_decision=True)` |
| Proofs | `ne-runtime/tests/test_standing_approval_successor.py`; `ne-api/tests/test_run_approval_resume_continuation.py` |

---

# ⛔ THE AUTHORING RULE THIS IMPLIES: NOTHING MAY FOLLOW AN APPROVAL-GATED CALL

**A procedure must not place work after an approval-gated call, because the resume executes that
call and terminates.** Written here, permanently, because it is not discoverable from the seam that
enforces it — the procedure is authored in one file and the refusal happens in four others.

## What actually happens

A run that parks at an approval gate does not sleep inside its loop. The attempt ENDS at the park.
When a human decides, a resume executes **exactly the one pinned effect that human approved** and
the run terminalizes — `run_finalizer` records the route as `approved_effect_complete` rather than
`execution_complete`, precisely so the two histories are distinguishable. The resume does NOT
re-enter the agent loop: `resumed_approval_id` names ONE effect, the continuation checkpoint
persists ONE invocation and **no plan remainder**, and the plan itself lived in the model loop's
working state that the resume never replays.

That is deliberate, not a gap. The spec scopes the loop as *"DETERMINISTIC AND BOUNDED AGENTIC
PRIMITIVES ONLY — NOT BUILT AS A GENERAL DURABLE LOOP."* Re-entering the loop hours later would
re-derive a **different** plan from a **different** context and execute it under an approval a human
gave for something else. The platform refuses that in four independent places, and this document's
"One approval continuation per run" section above is one of them.

## So, for anyone writing a `deploy/skills/*/SKILL.md` procedure

* **The approval-gated call is the LAST step of the procedure.** Any step ordered after it is
  unreachable — the platform will instruct the model to do something it then makes impossible.
* **Do not "fix" it by moving the step earlier, either.** A record written BEFORE the approval
  claims an effect the approver may still reject; a note in a system of record saying an outreach
  happened, for an outreach that was then refused, is a worse defect in a governance product than
  the missing note. (This was considered and rejected on 2026-09-09.)
* **If the trailing work genuinely must happen, it is a SECOND run**, started after the first one
  terminalizes — not a longer procedure, and not a second gated call in the same run (a Run may hold
  ONE approval continuation, ever).
* **State the limit where a reader is, rather than implying otherwise.** The shipped example is
  `deploy/skills/meridian-hardship-draft/SKILL.md` v3, whose Pitfalls section says it in the body
  the model reads.

## Where the claim used to be false, and now is not (2026-09-09)

| Surface | Was | Now |
|---|---|---|
| `deploy/skills/meridian-hardship-draft/SKILL.md` | step 6 ordered a Tier-1 CRM note "after an approved send" | v3 ends at the send; Pitfalls states the rule |
| `deploy/agents/registry.toml` (+ 3 packaged copies) | prose said the note runs "after the send resumes"; the budget counted it (13 counted / 15 worst case) | prose corrected; 12 counted / 14 worst case, ceiling held at 17 and stated as 14 + 3 headroom |
| `packages/ne-console` `runWorkspace.ts` | both terminal routes rendered "Completed" | reads the terminal event's `outcome` and renders "Completed the approved action" |
| `e2e/scenarios/test_80_s5_governed_ladder.py` | a STRICT xfail expecting the trailing note to run once "durable loop resume lands" | asserts the real rule: nothing executes after the approved send |

⚠️ The registry's `max_tool_calls` (17) and `max_steps` (23) did NOT move with that count, and must
not be re-cut casually: every token figure in that derivation is the measured 17-call shape, and
`max_steps` × 10 s is the subtrahend of the 180 s approval window this deployment publishes
(`packages/ne-runtime/tests/test_approval_window_is_honourable.py`).
