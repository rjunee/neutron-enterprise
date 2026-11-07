# 0023 — Authenticated request context

## Status

Accepted (2026-08-21; PRE-015A0-ENFORCEMENT, SPEC §13).

## Context

Delayed model, Tool, approval and protected-output work must retain the exact
authorization snapshot that came from a verified bearer token. A provisioned
`Principal` deliberately has no token lifetime, while copying the full JWT or
re-decoding it in ne-api would create a second authentication boundary and retain
profile/display claims that delayed authorization does not need.

Existing nondurable and test `TokenVerifier` implementations construct
`TokenClaims` without an expiry. Changing the `TokenVerifier.verify` return type
or making that field immediately mandatory would break the frozen Protocol even
though only production delayed work requires the new guarantee.

## Decision

1. Add a defaulted verifier-only `TokenClaims.verified_expires_at`. The production
   OIDC verifier reads `exp` only after signature/issuer/audience verification,
   rejects booleans, non-numeric, fractional, non-finite and out-of-range values,
   and authors an exact integer-second UTC datetime. The `TokenVerifier` Protocol
   and its return type are unchanged.

2. Add a frozen, extra-forbidden `AuthenticatedRequestContext` containing only
   schema version 1, opaque principal id, sorted unique effective authorization
   groups, optional string `department`, verified issuer, integer NumericDate
   expiry and a canonical context hash. Email, name, preferred username, session
   identifiers, bearer material and arbitrary token claims are structurally absent.

3. Bind the context hash to domain-separated, individually eight-byte-length-
   prefixed UTF-8 fields, including group count/order and department presence.
   Construction computes the hash when absent and rejects a mismatched supplied
   hash. No delimiter joining, normalization or caller-selectable schema exists.

4. `OidcAuthMiddleware` projects the context from the verifier result and the
   provisioned Principal and attaches it beside the synchronous Principal in ASGI
   state. Legacy verifier results with no verified expiry may still authenticate
   existing synchronous routes but cannot produce a context.

5. `current_authenticated_context` is the sole dependency for delayed work. It
   accepts only the typed middleware-authored value and fails closed when the
   context/verified expiry is absent. Request bodies and non-bearer headers do not
   populate it.

6. This is an additive MINOR contract bump from 1.19.0 to 1.20.0.

## Consequences

Synchronous callers and nondurable verifier doubles remain source-compatible.
Delayed work can now bind an exact, minimal authorization snapshot and expiry
without persisting a bearer token or reconstructing claims. This seam does not
add a current entitlement or JWT revocation service: a signed snapshot remains
authoritative only until its verified expiry, while current source head/ACL drift
is enforced independently by PRE-015A0-ENFORCEMENT.

