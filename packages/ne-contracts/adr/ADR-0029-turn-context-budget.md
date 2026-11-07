# 0029 — `TurnContextBudget`, the optional per-turn context-budget capability

## Status

Accepted (2026-09-01; NE-CTX-IN-FORCE).

## Context

ADR-0012 introduced the context-budget primitive layer: `ContextBudget`, the
`TokenEstimator` seam, and `ContextBudgetExceededError`. The Layer-2 trim guard
(`ne.runtime.model.GatewayModel`) enforces it, and it was composed with exactly
ONE budget per deployment — derived at boot, in `ne.api.platform`, from the
primary of `RuntimeSettings.task`, and frozen into `RuntimeSettings.context_budget`.

That number could not be re-derived while the process ran, and two governed,
audited facts move underneath it without a restart: a route REPOINT changes a
task's primary for every run after it, and a per-run SELECTION pins a different
model for the duration of one run. Because the guard could not follow either
one, `ne.api.model_routing` had to carry an input-budget FLOOR refusing any
repoint or selection target narrower than the boot primary — and the only way to
keep a wide frontier lane and a narrow private lane in one deployment was to
declare the wide model at the narrow model's window.

The measured consequence on the sealed demo box: `claude-sonnet-5`, a 1M-window
model, was declared at 32768 and — after a 12288 output reserve sized against a
262144 GPU window — offered **18841** input tokens, NARROWER than the CPU lane's
27033. A real agentic run died `context_exhausted` at 18928 estimated tokens: 87
tokens over, on a model serving one million.

The number cannot be computed in `ne-runtime`. "The model in force for this
turn" is `requested_model` if a governed selection pinned one, else the primary
of the task's route *as it stands right now* — and the live route register is
read by the gateway alone. Nothing in `ne-runtime` can see it.

## Decision

1. Add `ne.contracts.models.TurnContextBudget`, a `@runtime_checkable` Protocol
   with one method:

   ```python
   async def context_budget_for_turn(
       self, task: str, *, requested_model: str | None = None, lane: Lane | None = None
   ) -> TurnModelBudget | None: ...
   ```

   `TurnModelBudget` is a frozen pair `(model: str, budget: ContextBudget)`.

   `lane` (added in the same 1.26.0 release cycle, before the version shipped;
   no second bump) is the SAME lane pin the caller is about to put on its
   `CompletionRequest`. It exists because "the model in force" is not the route
   primary when a lane pin filters the primary out of the candidate list
   entirely: the gateway's candidate rule (`_candidates`) drops every candidate
   off the pinned lane BEFORE the first attempt, so the first model actually
   attempted can be a fallback — not as a secondary roll (§2.2's out-of-scope
   case) but as the ONLY candidate there is. A resolver that ignored the pin
   budgeted a model that would never be called: on a wide-frontier-primary /
   narrow-private-fallback route with the private lane pinned, the trim guard
   sized itself at the frontier window, trimmed nothing, and the gateway
   pre-flight refused the turn — the exact `context_exhausted` symptom this ADR
   exists to eliminate, on the air-gap demo beat that pins that lane.

2. It is a SEPARATE, OPTIONAL capability, not a change to `ModelGateway`.
   `ModelGateway` stays frozen and provider-opaque; no existing implementation
   is obliged to grow a method, and no conformance kit gains a requirement. This
   is what makes the change additive in substance and not only in signature.

3. `None` is the honest answer when the model cannot be NAMED (an unrouted task,
   an unregistered `requested_model`). The method never raises: the routing
   error it would report is raised by `complete` moments later, with its own
   typed exception and its own audit. A caller that receives `None` keeps its
   own composed default rather than inventing a window.

4. The consumer falls back to its composed boot budget when the gateway does not
   satisfy the capability. The guard therefore stays ARMED in exactly the
   configurations it was armed in before; only WHICH window it is armed at can
   vary.

5. Additive MINOR bump, 1.25.0 to 1.26.0. A pre-1.26.0 gateway simply fails the
   `isinstance` test, which is the documented fallback path and not an error.

6. **Every wrapping gateway on a governed path must forward the capability
   explicitly.** This is part of the decision, not an implementation note,
   because `runtime_checkable` Protocol membership is METHOD PRESENCE: a decorator
   that omits the method silently downgrades the capability to the fallback, and
   the fallback is quiet by design. Neutron's three wrappers each carry it —
   `ReceiptGatedGateway`, `DemoGroundedHallucinationGateway`, and (answering for
   itself rather than forwarding, because a canonical Run's model is chosen by
   `ModelSelectionService` and not by the gateway's route primary)
   `_RunContinuityGateway`.

### Round-4 amendment (same 1.26.0 cycle; no second bump)

Three review rounds each found the SAME defect in a new place: a call site
re-deriving "the first candidate that will actually be attempted" from its own
private copy of the ordering rule, and that copy drifting from the real call
path (round 1: the capability not forwarded; round 2: the lane pin ignored;
round 3/4: the worker resolver reading the posture-only configured catalog
while `_auto` reads the evidence-filtered `choices()`, and the context-ledger
projection reporting the register primary under a lane pin that filters it
out). The amendment makes the divergence structural rather than per-site:

* **The method is `async` and answers a `TurnModelBudget`.** Async because the
  Run-path implementation MUST resolve from the same evidence-gathering
  `ModelSelectionService.choices()` read its own attempt loop makes — a sync
  signature structurally forced it onto a different (posture-only) authority.
  The `model` name rides with the budget so a reporting surface (the context
  ledger) can say WHICH model the enforced window belongs to without growing
  another copy of candidate order.

* **Every implementation resolves through the LITERAL function its own attempt
  path walks — never a restatement of its rule.** `LiteLLMGateway` answers
  `self._candidates(task, lane, requested_model=...)[0]` — the same function
  `complete`/`_stream` call. `_RunContinuityGateway` answers the head of
  `_turn_candidates()`, the single function whose full sequence `complete`'s
  explicit branch and `_auto` loop attempt. The gateway and worker orders are
  deliberately NOT one function — the worker exists to override the gateway's
  route-table rule with the model-policy catalog under posture + evidence —
  so the guarantee is per-path: each budget answer is the head of the exact
  candidate sequence that path attempts, and a future ordering change moves
  the budget automatically because there is no second copy to forget.

* **Consumers of the ENFORCED number consume the capability, not a parallel
  derivation.** `GatewayModel._turn_budget` awaits it; the context-ledger
  projection (`_resolve_in_force`) awaits the same gateway instance under the
  same lane rule, so its stated "reported == enforced" contract is a
  shared-function property. Capability-less compositions keep the documented
  composed-boot fallback on both sides.

* **Same rule, not same snapshot, still.** Two reads moments apart (the
  resolver's and the attempt's) can straddle a governed repoint or an evidence
  flip; the gateway's per-candidate budget pre-flight — and the Run loop's
  pass-over of a budget-refused candidate — remain the belt for that
  transient, and the dangerous direction (trimmed wide, served narrow) still
  cannot reach a provider.

## Consequences

* `ne.api.model_routing`'s input-budget floor is deleted. Its stated premise —
  "the trim guard was derived from the default at boot and cannot be re-derived
  live" — is gone. Repointing DOWN now makes the guard STRICTER, and the refusal
  names the NEW model's window.
* Each lane declares the window its model actually serves. The demo emitter no
  longer caps a 1M frontier row at the private lane's 32768.
* The trim guard's `context.budget` audit event carries the IN-FORCE window, so
  a refusal names the window that actually constrained the turn.
* `RuntimeSettings.context_budget` remains a hashed field carrying the composed
  boot budget — unchanged preimage, unchanged drift tripwire. The ENFORCED number
  is re-derived from already-hashed inputs (`GatewaySettings` windows and
  reserves) plus a governed, audited model NAME.

## Rejected alternatives

**Add the method to `ModelGateway`.** That is a breaking change to a frozen,
consumed Protocol: every implementation and every conformance kit would have to
grow a method most of them cannot answer. A test double, a scripted fixture and a
receipt gate have no route table.

**A blanket `__getattr__` proxy on the wrappers instead of explicit forwards.**
Rejected. These wrappers exist to constrain what reaches the composed gateway —
`ReceiptGatedGateway` persists a pre-effect receipt, `_RunContinuityGateway`
rechecks policy per candidate effect. A proxy that leaks arbitrary inner
attributes past those gates makes the NEXT capability reachable by accident
rather than by decision. Each forward is three lines and states what it forwards.

**Have the runtime read the route register directly.** It would put a second
reader on the governed register and a second copy of the primary-selection rule
in a package that is deliberately provider-agnostic, and the two copies would
drift. The gateway already owns both.

**Return a bare `int` window instead of `ContextBudget`.** The window is not the
number the guard needs: the input budget is `window − effective_max_tokens −
overhead_margin`, and all three come from the same `GatewaySettings` arithmetic.
Returning the frozen `ContextBudget` keeps that arithmetic in ONE place, which is
the ADR-0012 single-source property. (Round 4 wraps it in `TurnModelBudget` to
carry the model NAME beside it; the arithmetic still lives only in
`GatewaySettings.context_budget_for`.)
