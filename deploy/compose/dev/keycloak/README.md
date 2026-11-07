# Keycloak — Neutron Enterprise identity broker profile

Keycloak 26.7.4 (`quay.io/keycloak/keycloak:26.7.4`, pinned by NE-006) is the
local **identity broker**. Every Neutron Enterprise service trusts **only the
local Keycloak issuer** — never a customer IdP directly (C10). Customer identity
providers (Entra ID, Okta, any OIDC/SAML IdP) are *brokered*: Keycloak federates
to them, then mints its own local tokens that our services verify.

```
Customer IdP (Entra ID / Okta)  ──OIDC/SAML──▶  Keycloak (local realm)  ──local JWT──▶  ne-api (ne-identity verifier)
        (never trusted directly by services)         (the ONLY trusted issuer)
```

## Dev realm endpoints

The dev realm is `ne-dev` on `http://localhost:8081`:

| Purpose      | URL                                                                       |
| ------------ | ------------------------------------------------------------------------- |
| Issuer       | `http://localhost:8081/realms/ne-dev`                                     |
| Discovery    | `http://localhost:8081/realms/ne-dev/.well-known/openid-configuration`    |
| JWKS         | `http://localhost:8081/realms/ne-dev/protocol/openid-connect/certs`       |
| Token (dev)  | `http://localhost:8081/realms/ne-dev/protocol/openid-connect/token`       |

The `ne-console` public client has direct-access grants enabled, so a dev token
is a password grant:

```
curl -s http://localhost:8081/realms/ne-dev/protocol/openid-connect/token \
  -d grant_type=password -d client_id=ne-console \
  -d username=dev -d password=dev
```

The resulting access token carries `aud: ne-api` (the `ne-api-audience` mapper),
a `groups` claim of `["platform-admins"]` (the `ne-groups` mapper, `full.path`
off), and `sub` (the `basic` scope). Those are exactly what the ne-identity
verifier requires.

## "Every service trusts only the local issuer" — consumption contract

Services NEVER pin a customer IdP. They pin the **local Keycloak realm** and
verify audience `ne-api`:

```bash
NE_IDENTITY_OIDC__ISSUER="http://localhost:8081/realms/ne-dev"   # no trailing slash
NE_IDENTITY_OIDC__AUDIENCE="ne-api"
NE_IDENTITY_OIDC__ALLOW_INSECURE_HTTP=true    # dev stack only (http://) — NEVER in prod
```

In production the issuer is the HTTPS realm URL of the deployment's Keycloak;
`ALLOW_INSECURE_HTTP` is left false so any `http://` issuer/JWKS URL is refused
at construction. The verifier fetches JWKS via discovery and rejects any token
whose `iss` is not exactly `NE_IDENTITY_OIDC__ISSUER` — including tokens minted
by the brokered customer IdP, which never reach a service unbrokered.

## What the dev realm ships (all additive to NE-006's base realm)

- **Groups** `/platform-admins`, `/engineering`; the `dev` user is in
  `/platform-admins`.
- **`ne-groups` client scope** — an `oidc-group-membership-mapper` emitting the
  `groups` claim with `full.path=false` (so the claim is `platform-admins`, not
  `/platform-admins`; ne-identity's sanitizer also strips a leading `/`
  defensively).
- **`ne-api-audience` client scope** — an `oidc-audience-mapper` adding
  `ne-api` to the access-token `aud`.
- **`ne-console` `defaultClientScopes`** — lists the standard scopes
  (`basic`, `profile`, `email`, `roles`, `web-origins`, `acr`) **plus**
  `ne-groups` and `ne-api-audience`. `defaultClientScopes` REPLACES the server
  defaults, so `basic` (which carries `sub` on KC 25+) MUST be listed.
- **`customer-idp` broker placeholder** — a disabled OIDC identity provider with
  no real secret. It documents the broker wiring shape; enable and fill in per
  customer.

## Brokering a customer IdP

Replace the disabled `customer-idp` placeholder (or add a new provider) per
customer. In all cases set `syncMode: FORCE` so group/attribute changes (and
removals) at the customer IdP propagate to the brokered Keycloak user on each
re-login.

### Microsoft Entra ID (OIDC)

1. Register an app in the customer's Entra tenant; note the **Application
   (client) ID** and a client secret.
2. Configure the Keycloak OIDC identity provider:
   - `issuer` / discovery: `https://login.microsoftonline.com/<tenant-id>/v2.0`
   - `clientId`: the Entra application id; `clientSecret`: the app secret
   - `clientAuthMethod`: `client_secret_post`
3. Emit groups from Entra: configure the app registration's groups claim as
   **"Groups assigned to the application"** (Terraform sets
   `group_membership_claims = ["ApplicationGroup"]`), then assign the
   customer's platform-relevant security groups to the app. **Do not** use
   "Security groups" — it emits every group the user belongs to and past
   150 (SAML) / 200 (JWT) group memberships Entra omits the `groups` claim
   entirely, silently under-authorizing exactly the most-privileged
   (highest-group-count) admins. Then add an **Identity Provider Mapper** in
   Keycloak of type *Claim to Group* / *Attribute Importer* to translate the
   incoming `groups` claim into Keycloak group memberships. Those Keycloak
   groups then flow through the `ne-groups` mapper into the local token's
   `groups` claim. The pilot realm additionally fails loud on any residual
   overage (marker role + deny flow) rather than silently resolving to zero
   groups — see `deploy/compose/pilot/keycloak/README.md`, "Group overage".

### Okta (OIDC or SAML)

- **OIDC**: create an Okta OIDC app; use Okta's issuer
  `https://<org>.okta.com/oauth2/default` (or a custom auth-server issuer) for
  the Keycloak OIDC provider. Add an Okta **groups claim** to the authorization
  server, then map it in Keycloak as above.
- **SAML**: create a SAML app in Okta; import its metadata into a Keycloak SAML
  identity provider. Map the SAML group attribute to Keycloak groups with an
  Identity Provider Mapper.

### IdP group -> Keycloak group -> platform role

The chain is deliberate and config-gated at two points:

1. Customer IdP group claim → Keycloak group (Identity Provider Mapper,
   `syncMode: FORCE`).
2. Keycloak group → local-token `groups` claim (the `ne-groups` mapper).
3. `groups` claim → platform role — **only** via ne-identity's config-driven
   `NE_IDENTITY_ROLES__MAPPING` (see `packages/ne-identity/README.md`). An
   unmapped group grants no role, and a token can never self-assign a role.

## Live verification (parked — no docker on the build host)

Importing this realm and the end-to-end token flow can only be verified on a
running stack (`ne dev up`). Structural consistency of this file is asserted in
CI by `packages/ne-identity/tests/test_realm_config.py`; the live import and the
real-token flow are pending-live-verification ledger items
(`packages/ne-identity/tests/test_keycloak_live.py`, skipped in CI).
