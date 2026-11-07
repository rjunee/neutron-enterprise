# ne-identity

The Neutron Enterprise **authn spine**: fail-closed OIDC bearer-token
verification, JIT principal provisioning with config-driven IdP-group →
platform-role mapping, and a pure-ASGI OIDC middleware — all pinned to the
**local Keycloak issuer** (SPEC §5 NE-060, C10).

## Trust model — local issuer only (C10)

Every service trusts **only** the local Keycloak issuer. Customer identity
providers (Entra ID, Okta, any OIDC/SAML IdP) are *brokered* by Keycloak, which
mints its own local tokens. A token minted by the customer IdP is never trusted
by a service directly — `iss` is verified by **exact match** against the pinned
`NE_IDENTITY_OIDC__ISSUER`, and a trailing slash on the issuer is rejected at
construction so the comparison is unambiguous.

Fail-closed guarantees (the JWT-validation seam is fully CI-tested with locally
minted RSA keys — no Keycloak needed):

- `allowed_algs` is restricted at **construction time** to asymmetric,
  JWKS-backed algorithms. `alg:none` and any `HS*` are impossible to configure,
  so the algorithm-confusion class is closed structurally, not just at verify.
- Signature, `aud`, `iss`, `exp`, `nbf` are all verified; `exp`, `iat`, `sub`,
  `iss`, `aud` are **required** claims.
- The resolved JWK's declared `alg` (if any) must equal the header `alg`, and
  the alg family must match the JWK `kty` (RS*/PS*→RSA, ES*→EC, EdDSA→OKP).
- Every failure raises the single opaque `TokenVerificationError`; the
  middleware's 401s carry **no** authentication oracle.
- No code path attaches a principal from an unverified token; `resolve()` never
  creates a principal (provisioning happens only from a verified token).

### Token-type + JWKS hardening (NE-FIX-IDENTITY)

- **Access-token type required (P2-2).** After the cryptographic verify, a token
  is accepted only if it carries an access-token marker: the Keycloak payload
  claim `typ == "Bearer"` **or** an RFC 9068 header `typ` of `at+jwt` /
  `application/at+jwt` (case-insensitive). OIDC **ID** tokens (`typ == "ID"`),
  **Refresh** tokens, and marker-less JWTs are rejected with the same opaque
  error — so an ID token minted for a browser client can never be replayed as an
  API bearer credential and JIT-provision mapped roles. A non-Keycloak IdP must
  emit one of these markers on its access tokens.
- **Reserved-namespace subjects rejected (P2-1).** A verified `sub` in the
  reserved `acl:*` / `role:*` namespace (e.g. exactly `acl:tenant-all`) is
  refused at the verifier **and** independently at `JitProvisioner.provision`, so
  it can never become a `Principal.id` and match every `acl:tenant-all`-stamped
  chunk without being store-known (defeats CKD2).
- **JWKS anti-amplification (P2-6).** An unknown `kid` forces at most **one**
  network refresh per `jwks_refresh_cooldown_seconds` window (default 30s, fail
  **closed** within the window); recently-missed kids are negatively cached
  (bounded), and a fresh-cache hit is served **without** awaiting the network or
  the source lock. A degraded Keycloak or an unauthenticated random-`kid` spray
  therefore cannot stall legitimate auth. Tradeoff: during a genuine key rotation
  a new `kid` can fail-close for up to the cooldown (deliberate fail-closed
  posture).
- **Env-proxy-immune, same-origin JWKS fetch (P2-3a, C2).** The fetch uses a
  dedicated `urllib` opener built with `ProxyHandler({})`, so `HTTP_PROXY` /
  `HTTPS_PROXY` / `ALL_PROXY` can never route the JWKS request off-box, plus a
  redirect handler that refuses any cross-origin redirect.

## Configuration reference (`NE_IDENTITY_*`)

Settings layer per the platform convention (init > env > `ne.toml` > defaults).

| Env var                              | Field                    | Default        | Notes                                            |
| ------------------------------------ | ------------------------ | -------------- | ------------------------------------------------ |
| `NE_IDENTITY_VERIFIER`               | `verifier`               | `keycloak-oidc`| `ne.plugins.token_verifier` name                 |
| `NE_IDENTITY_RESOLVER`               | `resolver`               | `jit`          | `ne.plugins.principal_resolver` name             |
| `NE_IDENTITY_OIDC__ISSUER`           | `oidc.issuer`            | *(required)*   | Pinned local issuer, no trailing slash           |
| `NE_IDENTITY_OIDC__AUDIENCE`         | `oidc.audience`          | *(required)*   | Expected `aud` (e.g. `ne-api`)                   |
| `NE_IDENTITY_OIDC__JWKS_URI`         | `oidc.jwks_uri`          | `None`         | Skip discovery if set                            |
| `NE_IDENTITY_OIDC__ALLOWED_ALGS`     | `oidc.allowed_algs`      | `("RS256",)`   | Asymmetric only (validated)                      |
| `NE_IDENTITY_OIDC__LEEWAY_SECONDS`   | `oidc.leeway_seconds`    | `10.0`         | Clock skew, `0..60`                              |
| `NE_IDENTITY_OIDC__JWKS_CACHE_TTL_SECONDS` | `oidc.jwks_cache_ttl_seconds` | `300.0` | JWKS cache lifetime                              |
| `NE_IDENTITY_OIDC__JWKS_REFRESH_COOLDOWN_SECONDS` | `oidc.jwks_refresh_cooldown_seconds` | `30.0` | Min interval between unknown-kid forced refreshes (fail-closed); `0` disables (tests) |
| `NE_IDENTITY_OIDC__HTTP_TIMEOUT_SECONDS`   | `oidc.http_timeout_seconds`   | `5.0`  | JWKS/discovery fetch timeout                     |
| `NE_IDENTITY_OIDC__ALLOW_INSECURE_HTTP`    | `oidc.allow_insecure_http`    | `False`| `true` for the `http://` dev stack only          |
| `NE_IDENTITY_OIDC__GROUPS_CLAIM`     | `oidc.groups_claim`      | `groups`       | Token claim carrying IdP groups                  |
| `NE_IDENTITY_OIDC__EXEMPT_PATHS`     | `oidc.exempt_paths`      | `("/health",)` | Exact-match unauthenticated paths                |
| `NE_IDENTITY_ROLES__MAPPING`         | `roles.mapping`          | `{}`           | `{"idp-group": ["role", ...]}`                   |
| `NE_IDENTITY_ROLES__DEFAULT_ROLES`   | `roles.default_roles`    | `()`           | Always-granted roles                             |

## Wiring the middleware into ne-api (ADR-001 topology)

> Note: `ne-api` is **not yet built** (planned NE-104). This is the reference wiring
> the composition root will use — the middleware itself is built and tested here.

```python
from ne.identity import IdentitySettings, build_identity

settings = IdentitySettings()          # oidc.issuer/audience from env or ne.toml
app = build_identity(settings, fastapi_app)   # returns the wrapped ASGI app

# In a route, read the principal only through the sanctioned dependency:
from fastapi import Depends
from ne.contracts.types import Principal
from ne.identity import current_principal

@fastapi_app.get("/me")
def me(principal: Principal = Depends(current_principal)) -> dict:
    return {"id": principal.id, "groups": list(principal.groups)}
```

The middleware reads the bearer token from the `Authorization` header **only**
(query-param tokens are never honored), exempts paths by **exact** match, and
attaches the provisioned `Principal` to the request; read it only via
`current_principal`.

## JIT provisioning + role mapping

On each verified token the principal is UPSERTed: IdP groups are sanitized (a
leading `/` stripped, non-str items and any `role:`- or `acl:`-prefixed group
dropped) and mapped to platform roles via `roles.mapping` **only** — token
`roles` / `realm_access` claims are never read. The `acl:` namespace is reserved
for ne-permissions' ACL labels (`acl:tenant-all` / `acl:public-link`), so a
forged claim can never smuggle one in via the groups claim and thereby bypass the
ne-permissions `is_known` fail-closed gate to gain tenant-wide visibility
(NE-CKD2, Checkpoint-D finding #2). `Principal.groups` carries both the
sanitized IdP groups (verbatim, for NE-043 source-ACL trimming) and the mapped
roles namespaced `role:<name>`. The `role:` namespace is collision-proof because
`role:`-prefixed IdP groups are dropped, so a token can never self-assign a
role. Groups are **recomputed** from the current token every time (never
accreted), so a group removed at the IdP de-provisions on the next login.

**Brokered Entra groups** reach this chain unchanged: the pilot realm's
per-provider Claim-to-Group `identityProviderMappers` translate the incoming
Entra `groups` claim into Keycloak group membership, which then flows
through the `ne-groups` client-scope mapper into the local token's `groups`
claim exactly as above — **no `roles.py` change is needed or wanted**, the
chain is config-only. This is also why the `entra-groups-overage` marker
**realm role**, used by the pilot realm's fail-loud >150-group deny flow, can
never grant platform access on its own: token `roles`/`realm_access` are
never read here. See `deploy/compose/pilot/keycloak/README.md` for the
GUID↔platform-group mapping table and the group-overage runbook.

### Zero-mapped-groups observability (NE-BEW-GROUPS-ZEROGROUPS)

NE-BEW-GROUPS made group emission depend on a manual Entra app **group
assignment** step (`group_membership_claims = ["ApplicationGroup"]`); an
operator who forgets that step gets a token with no overage indicator and no
usable groups, which resolves silently to default-roles-only — an
under-authorized user with no signal anywhere. This is now OBSERVABLE, never
blocking: over-blocking a legitimately group-less user is its own failure
mode, so the login always succeeds.

`JitProvisioner.provision()` fires the signal only when **all three** hold:

1. the login is **brokered** — the token carries a non-empty `idp_claim`
   value (default claim name `identity_provider`, stamped by the `ne-groups`
   scope's `oidc-usersessionmodel-note-mapper` from Keycloak's built-in
   `identity_provider` user-session note, set on every login that came
   through an upstream IdP);
2. a role mapping is **configured** (`roles.mapping` non-empty) — a
   deployment that runs with no mapping at all (legitimately group-less) never
   fires this on any login;
3. **zero** of the incoming groups **mapped** to a platform role — not "zero
   groups in the claim". A user whose groups are all unmapped (e.g. a
   stale/typo'd `NE_IDENTITY_ROLES__MAPPING` key) is the same silent
   under-authorization as a user with no groups claim at all, so both shapes
   fire the same signal.

When it fires: an always-on structured `WARNING` is logged (idp alias +
group counts only — never subject, group names, or claim material), and — if
an `AuditSink` is wired via `build_identity(..., audit_sink=...)` — a
queryable `identity.brokered_login.zero_groups` audit event is appended
(`ne-api` wires the platform's one hash-chained sink). Audit-append failure
never blocks the login; the WARNING still fires so the signal is never fully
silent.

**Operator response** on seeing this signal:

1. Check the Entra broker app's group assignment. `ApplicationGroup` emits
   nothing for a user not explicitly assigned to the app.
2. Check that `NE_IDENTITY_ROLES__MAPPING` keys match the group names
   Keycloak actually emits (name/GUID mismatch, stale mapping).

This amends the Entra broker wire design's "the broker is invisible
downstream of Keycloak" statement: principal **resolution** is
unchanged (still config-only, still no `roles.py` change), but an
observability side-channel — this signal — now depends on which IdP
brokered the login, per the NE-BEW-GROUPS-ZEROGROUPS SPEC row.

## Dependency intake evidence

Re-verified 2026-07-04; the full closure passed the C4 license gate
(`ne check licenses` → 91 packages within policy) and the pin+hash lock gate.

| Component     | Version  | License                     | Notes                                         |
| ------------- | -------- | --------------------------- | --------------------------------------------- |
| PyJWT         | 2.15.0   | MIT                         | Chosen over python-jose (SPEC §3.9 CVE history). CVE-2022-29217 (alg confusion, fixed 2.4.0) and CVE-2024-53861 (partial `iss`, fixed 2.10.1) both long-closed; not in CISA KEV. Floor `>=2.15.0` carries the fixes for the maintainer-published 2026-09 advisory batches (repository-level advisories, absent from the GitHub Advisory DB and osv); GHSA-8wjv-2p76-3863 (header) and GHSA-42vr-xj54-vc7v (signed payload) are pinned by `tests/test_verifier_security.py`. |
| cryptography  | 49.0.0   | Apache-2.0 OR BSD-3-Clause  | PyCA-maintained; JWK/signature primitives     |
| FastAPI       | 0.139.0  | MIT                         | ASGI app + `TestClient`                        |
| Starlette     | 1.3.1    | BSD-3-Clause (transitive)   | `scope["state"]` → `request.state` verified    |
| httpx         | 0.28.1   | BSD-3-Clause (test-only)    | `TestClient` transport                         |
| Keycloak      | 26.7.4   | Apache-2.0 (external)       | Broker service; deploy config only, not embedded |

**Why not python-jose:** avoided per SPEC §3.9 ABI-teardown note (CVE history).
**Why not authlib:** a larger surface than needed for pure verification.
All cryptography is delegated to PyJWT/`cryptography`; this package implements
none. The only network call is JWKS/discovery to the pinned local issuer (stdlib
`urllib`, the same choice as `ne.secrets.openbao` — no telemetry, no phone-home).

## Agent identity (NE-036)

`ne.identity.agents` layers a **scoped per-agent service-credential** lifecycle on
top of the authn spine. Each agent/department session gets its **own**
least-privilege credential (never a shared platform god-credential), so a
compromised agent is bounded to its scope. Secret material lives **only** in the
frozen `SecretStore` (NE-007, C1) — the service holds ids, scopes, states, and
store `path`/`version` pointers, never the token. The only secret-returning API is
`resolve_secret`, which wraps the material in a `SecretStr`.

### Lifecycle

```
issue(agent, scope)      -> ACTIVE  (SecretStore KV-v2 v1;  audit agent.credential.issue)
rotate(agent)            -> ACTIVE  (KV-v2 v2)  + old -> GRACE  (audit agent.credential.rotate)
   ...grace window...       old still validates + resolves (in-flight sessions unbroken)
   ...GRACE creds serve ONLY in-flight holders (validate/resolve by id); select_credential
      for NEW work is ACTIVE-only and fails closed (CredentialNotFoundError) when no ACTIVE
      credential exists — a narrowing rotation can never leak the old broader scope to a new dispatch
   ...past grace_until...    old -> EXPIRED (lazy, on validate/credential_state)
revoke(credential_id)    -> REVOKED (immediate, fail-closed; version soft-deleted;
                                     audit agent.credential.revoke — stands even if audit fails)
```

`issue` is **fail-closed**: if its audit emit fails (event construction — e.g. an
unfrozen config hash — or sink append), the just-written secret version is
destroyed and the credential deregistered (an unaudited credential can never be
used). `rotate` unwinds only the new version if its audit emit fails (event
construction — e.g. an unfrozen config hash — or sink append), and the old
credential reverts to ACTIVE. `revoke` marks REVOKED and parks the pool entry
**before** the audit append, so revocation is immediate and stands even when the
audit sink is down (`CredentialAuditError` is still raised).

### SecretStore path convention

One KV-v2 path per agent, one version per credential:

```
{secret_prefix}/{department}/{agent_name}      # default secret_prefix = "agents"
# e.g.  agents/sales/assistant   (v1 = first credential, v2 = after one rotation, ...)
```

### Rotation/cooldown pool (Hermes credential_pool PATTERN, MIT)

`ne/identity/agents/pool.py` is an **independent** implementation of the
NousResearch/hermes-agent `credential_pool` PATTERN (v0.18.0, MIT — see `NOTICE`):
four selection strategies (`fill_first` / `round_robin` / `random` /
`least_used`), per-failure-class cooldowns (429 → 60s or a `Retry-After` override,
402 → 30 min, 401 → 5 min), and a 5-strike → 1 h park; `report_success` resets.
The wall clock is **injected** (epoch-seconds `now` per call), so the whole pool
is deterministically testable. No hermes-agent code is vendored. Selection also
accepts an optional eligibility predicate; the service restricts new selections to
ACTIVE credentials.

### Principal binding (C6 attribution)

`bind_agent_principal` (or `service.bind_principal`) mints a `Principal` that
NE-033's `DepartmentAgentSession` accepts and the PDP/guardrails/audit see as the
acting subject — the audit trail records **which** agent identity acted.

| Claim                  | Value                                   |
| ---------------------- | --------------------------------------- |
| `department`           | the agent's department (matches the session gate — literal pinned by test) |
| `agent_name`           | the agent's name                        |
| `agent_credential_id`  | `<dept>/<agent>/v<version>`             |
| `agent_scope_tools`    | sorted, comma-joined allowed tool names |
| `agent_scope_sources`  | sorted, comma-joined source systems     |
| `on_behalf_of`         | the delegating human's id (when given)  |

Anti-escalation (C10): the bound principal's groups are **only**
`agent:<dept>/<agent>` and `agent:<dept>` — the delegating human's groups are
**not** inherited, so a compromised agent can never escalate to its delegator's
authority.

### Guardrail wiring

`AgentScopeGuardrail` implements the frozen sync `Guardrail` Protocol and fails
**closed** for agent principals (unknown/revoked/expired credential, or a tool
outside scope); non-agent principals pass (they are governed by the PDP). It
authorizes the requested tool against the credential's **stored** scope looked up
from the service (`AgentCredentialService.validate`) — the principal's
`agent_scope_tools` claim is informational and is never trusted for authorization.
Compose it into the existing NE-033 engine so blocks are audited (C6/C12):

```python
from ne.govern.guardrails.engine import GuardrailEngine
from ne.identity.agents import AgentScopeGuardrail

engine = GuardrailEngine([*default_guardrails, AgentScopeGuardrail(service)], sink=sink)
```

### Rotation-schedule seam (NE-034)

`rotation_due(agent)` / `rotation_due_agents()` expose which credentials are past
their `rotate_after_seconds` interval — the read seam an NE-034 cron `Scheduler`
polls to drive `rotate`. The cron wiring itself is **out of scope** here.

### Known follow-ups

The credential registry (states + pools) is **process-local** in v1; durable /
multi-process credential state and the composition-root wiring (NE-060/070) plus
the NE-034 cron are tracked follow-ups, not silently promised.

### Config reference (`NE_AGENT_IDENTITY_*`)

`AgentIdentitySettings` is a separate surface from `IdentitySettings`; all fields
live under `credentials.*` (e.g. `NE_AGENT_IDENTITY_CREDENTIALS__STRATEGY`).
Defaults: `secret_prefix="agents"`, `secret_bytes=32`, `rotate_after_seconds=86400`,
`grace_seconds=300`, `strategy="fill_first"`, cooldowns `60`/`1800`/`300`,
`max_consecutive_failures=5`, `consecutive_cooldown_seconds=3600`.

### Live-parked (OpenBao)

`tests/test_openbao_agent_rotation_live.py` runs the full
issue → rotate → grace → revoke lifecycle against a **real** OpenBao
(`OpenBaoSecretStore`), asserting real KV-v2 version increments and revoked-version
soft-delete. It skips cleanly when OpenBao is unreachable. Start it with `ne dev up`
(or set `NE_TEST_OPENBAO_URL` / `NE_TEST_OPENBAO_TOKEN`).

## Deploy profile

See `deploy/compose/dev/keycloak/README.md` for the Keycloak 26 broker runbook
(Entra ID / Okta wiring, the dev realm's group/audience mappers, and the
local-issuer consumption contract).

## Live-parked items

- `tests/test_keycloak_live.py` — real password-grant token verified via real
  JWKS discovery against the running NE-006 dev stack (skipped in CI; no docker
  on the build host).
- `ne dev up` realm import of the amended `ne-dev-realm.json` (structural
  consistency covered in CI by `tests/test_realm_config.py`).
