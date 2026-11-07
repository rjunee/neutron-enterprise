# 0020 — `CompletionRequest.requested_model`

## Status

Accepted (2026-08-18; BD-15 console model picker, SPEC §13).

## Context

BD-13 made each task's primary model a governed live route. BD-15 adds a
per-run user choice: the same Ask session or workflow can request a
boot-registered frontier model and later a boot-registered private model
without restarting the deployment. The only frozen value crossing every
model gateway implementation is `CompletionRequest`; before this decision it
carried task, optional lane, provider parameters, trace, and principal, but no
typed identity for the model the governed caller selected.

The distinction is material. A declared fallback may answer after the
requested primary fails, so the audit trail must name both what was requested
and what actually served. Under `AIR_GAP`, selecting a frontier model must be
refused by the existing per-attempt egress guard and must never look like a
successful request for the private fallback.

## Decision

1. Add `requested_model: str | None = None` to `CompletionRequest`. The value
   is the gateway alias from the immutable boot registry, not the provider's
   SDK model string, URL, credential, or arbitrary endpoint. `None` preserves
   the governed task route selected by BD-13.

2. An explicit alias replaces only the effective route's primary for that
   call. The gateway still uses the same single route snapshot for the task's
   boot-preserved fallbacks and parameters, validates that the alias is
   registered and compatible with an explicit lane, and applies the normal
   per-attempt egress guard before transport. The field grants no lane or
   egress authority.

3. Requested and served identity remain separate facts. Gateway audit
   provenance includes `requested_model` only when the caller supplied it;
   `model_name` and `response_model` continue to describe the model that
   actually answered. No consumer may infer one from the other.

4. This is an additive MINOR contract bump, 1.16.0 to 1.17.0. A pre-1.17.0
   payload has no new key and validates with `requested_model is None`.

## Rejected alternatives

- **Put the alias in `CompletionRequest.params`.** Rejected for the same
  reason ADR-0018 rejected that channel for principal identity: `params` is
  untyped, is forwarded toward LiteLLM/provider kwargs, and does not expose
  the control value on the frozen contract. A routing authority must never
  leak into a provider parameter bag.
- **Mutate the global BD-13 task route for every console selection.** Rejected:
  one user's per-run choice would change concurrent users' routing and would
  turn a request-scoped control into shared deployment state.
- **Treat a requested model as the served model.** Rejected: provider failure,
  reasoning exhaustion, and declared fallback make those identities diverge.
- **Let an explicit choice bypass lane or egress validation.** Rejected: that
  would make the picker an exfiltration authority and violate C2. Policy
  refusal stays unwrapped and does not fall back.

## Consequences

Existing producers and gateways remain source-compatible because the field is
defaulted. Producers that support a governed model choice carry the exact
registry alias; gateways validate it against their immutable boot model set.
The companion gateway implementation records requested and actual identities
and emits timestamped, names-only model observations through the existing
`AuditEvent` schema. API governance, workflow transport, runtime propagation,
and console presentation are separate consumers of this carrier and cannot
claim completion from this contract change alone.
