# ne-permissions-rebac — optional OpenFGA ReBAC module (NE-087)

A strictly **additive** fine-grained document-sharing grant channel: it lets a
document be shared with a subject the source system never named ("this doc is
visible to project-X members **plus** Jane"), which the source-ACL label model
cannot express without re-indexing. The module is **disabled by default**
(`RebacSettings.enabled = False`); a deployment without a fine-grained-sharing
requirement has **zero behavior change**, and nothing in the platform imports
this package until the NE-087-WIRING (O-4) rider wires it at the composition
root.

## The union rule (I-1, verbatim)

```
visible(principal, chunk) := source_acl_leg(principal, chunk) ∨ rebac_leg(principal, chunk)
```

> **I-1:** *ReBAC never grants access the source ACL denies, EXCEPT through an
> explicit, PDP-approved, audited, revocable `rebac.share` grant created by a
> principal who could already read the document — and ReBAC can NEVER remove
> access the source ACL grants.*

Both legs are independently **fail-closed**: absence of a grant = no access;
there are **no deny/negative tuples** (the authorization model provably contains
no `difference`/`intersection`/exclusion operator). The rebac leg is compiled
into the C7 filter as a reserved `$any` OR-group unioning `{"acl": [...]}` with
`{"document_id": [...shared ids]}` — nothing from OpenFGA ever enters the `acl`
field, and a caller filter AND-combines **outside** the group so it can only
narrow.

## Invariants

| Id | Invariant |
|----|-----------|
| I-1 | Union rule (above); additive, monotone in grants. |
| I-2 | Fail-closed absence: no tuple + no source match ⇒ nothing; unknown/tombstoned principal ⇒ no rebac leg (the `is_known` gate). |
| I-3 | Reserved-namespace anti-smuggling: `acl:*` rejected as share subject/object and stripped from contextual tuples; `$any` rejected in caller position; unknown `$`-keys rejected. |
| I-4 | Single tuple source: `ShareService` is the ONLY writer; connectors never mirror source ACLs into OpenFGA (enforced by the `c3-openfga-sdk-chokepoint` import contract). |
| I-5 | Audit completeness: every share/revoke passes the PDP AND appends a `rebac.share.*` event BEFORE the mutation; sink/pdp are constructor-required (no sink-less form); append failure fails closed. |
| I-6 | Propagation fail-closed: no positive result cache in v1; membership revocation lag = the existing NE-042 sync interval (no new lag class). |
| I-7 | Chokepoint additive: `PrincipalAclFilter` stays the single hook; the C7 guardian suite passes unchanged; the `shared_docs=None` path is byte-identical to today. |
| I-8 | Server-down fail-closed: `degrade` ⇒ rebac leg = ∅ (never wider); `deny` ⇒ typed error propagates; share/revoke always fail under outage. |

## Settings (`RebacSettings`, §4.3)

| Field | Default | Notes |
|-------|---------|-------|
| `enabled` | `False` | Master switch; `False` = filter identical to today. |
| `api_url` | `http://openfga:8080` | In-VPC sidecar; validated at client construction. **8080 is the HTTP API the SDK speaks** (8081 is gRPC — health-probe only). |
| `store_id` | `""` | From bootstrap; required when enabled. |
| `authorization_model_id` | `""` | PINNED; every call carries it; required when enabled. |
| `api_token` | `SecretStr("")` | Preshared key; never logged. |
| `timeout_s` | `3.0` | Per-request timeout. |
| `unavailable_mode` | `"degrade"` | `degrade`: rebac leg ∅ on outage (availability of the primary leg). `deny`: query raises (for deployments where "shared docs silently missing" is unacceptable). |
| `max_shared_documents` | `10000` | `ListObjects` cap; overflow truncates (over-restriction). |

`enabled=True` with an empty `store_id`/`authorization_model_id` raises at
construction (fail-closed).

`RebacSettings` overrides the base `NE_` env prefix with **`NE_REBAC_`** (the
NE-CLEAN4 precedent): the O-4 wiring populates `NE_REBAC_ENABLED`,
`NE_REBAC_API_URL`, `NE_REBAC_STORE_ID`, `NE_REBAC_AUTHORIZATION_MODEL_ID`, and
`NE_REBAC_API_TOKEN` on the app processes (via `ne up --profile rebac`). Without
the prefix override those env keys would be silently ignored and enabling the
`rebac` compose profile would be a no-op.

### Bootstrap CLI (O-4)

Once the OpenFGA server is up (via `ne up --profile rebac`), create the store and
write the pinned model with the operator CLI. The preshared key is read **only**
from the environment (never an argument — C12):

```
NE_REBAC_API_TOKEN=${OPENFGA_TOKEN} \
  uv run python -m ne.permissions_rebac.bootstrap --api-url http://127.0.0.1:8080
```

It prints the `store_id` / `authorization_model_id` and a ready-to-paste
`config/ne-rebac.toml` snippet; drop that into the pilot bundle, re-promote, then
`ne up --profile pilot --profile rebac` boots ne-api with an ACTIVE resolver.

## Testing

Unit tests run entirely against `ne.permissions_rebac.testing.fakes`
(`InMemoryOpenFga` + governance fakes) and a local in-process Qdrant — **no live
OpenFGA server**. `InMemoryOpenFga` implements the §1.1 model semantics exactly
(owner folds into viewer; `group#member` resolves only via contextual tuples).

## Live proof — `rebac-live-proof.yml`

`packages/ne-permissions-rebac/tests/test_openfga_live.py` is an ADDITIONAL
layer on top of the fake-based suite above — it replays the same no-bypass
flow through a REAL OpenFGA server (`ne.permissions_rebac._client`'s
`BootstrapClient` / `OpenFgaHttpClient`, the SDK chokepoint), and the
`rebac-live-proof.yml` GitHub Actions workflow (owner-`workflow_dispatch`
only) runs it in CI against the same pinned container. The fake suite is
never weakened or replaced by this layer.

What it proves that the fake cannot:

- a real `WriteAuthorizationModel` bootstrap round-trips through the store +
  model pin;
- a client pinned to one authorization-model id is unreachable-by-construction
  from a DRIFTED server-side "latest" model (proven directly on the returned
  model dict);
- genuine **read-after-write** and **read-after-delete** `ListObjects`
  consistency — no sleeps, no retries: a lag would BE the finding, since this
  is the exact risk SPEC §5's NE-LP-REBAC row names;
- the resolver / `ShareService` no-bypass flow (validate → known → read-probe
  → PDP → audit-then-write, group-via-contextual-tuples-only, the `acl:*`
  strip, PDP-deny / audit-failure leaving zero tuples) replayed end to end
  against the real backend, including the C7-union revocation-safety
  assertion (share → immediate resolve → revoke → immediate ∅).

### Run it locally

```
docker compose -p ne-rebac-liveproof -f deploy/compose/dev/compose.yaml \
  --profile rebac up -d --wait --wait-timeout 300 openfga

NE_TEST_OPENFGA_URL=http://127.0.0.1:8087 \
  NE_TEST_OPENFGA_TOKEN=nedev-openfga-token \
  uv run pytest packages/ne-permissions-rebac/tests/test_openfga_live.py -q -rs

docker compose -p ne-rebac-liveproof -f deploy/compose/dev/compose.yaml \
  --profile rebac down -v --remove-orphans
```

With no server reachable at `NE_TEST_OPENFGA_URL` (default
`http://127.0.0.1:8087`), the whole module skips cleanly via a
`skipif`-`pytestmark` (never a module-level `pytest.skip`, so
`--collect-only` still reports the full test count with the server down —
required by the PR-CI static guardian,
`test_rebac_live_workflow_static.py`, which derives the workflow's must-run
floor from that collected count).

`rebac-live-proof.yml` is `workflow_dispatch`-only and **deliberately carries
no secrets and no preflight-skip job**: the real system under test is the
`openfga/openfga` container pinned in `deploy/compose/dev/compose.yaml`'s
`rebac` profile (this repo's single source of truth for that pin — the
workflow contains no image tag of its own), and `nedev-openfga-token` is the
committed dev-only well-known preshared key already public in that same
compose file — never a real credential (C12).

### Known fake-vs-real divergences

**Never weaken a test to make the real server pass — a divergence is a
finding.** `InMemoryOpenFga` stays the fast CI default; every row below is
OBSERVED reality from the real server, pinned as an assertion in
`test_openfga_live.py`, never "fixed" by editing `testing/fakes.py` or `src/`.
Where the finding is that the FAKE was more forgiving than production (D1),
the disposition is a *follow-up fix unit* that corrects the fake **and**
`src/` together, deliberately, with the live lane re-proving both — never a
live-proof-only unit reaching into `src/` to silence what it found.

| Id | Fake behavior | Real server (observed 2026-07-24) | Disposition |
|----|----------------|-------------------------------------|--------------|
| — | `write_tuples` of an existing tuple raises a plain `ValueError`. | Raises the confined-boundary `BackendError` (an `OpenApiException` under the hood — the server refuses a duplicate write outright). | Type-shape divergence only; pinned in `test_live_duplicate_write_raises_backend_error`. Callers already only see the module's typed errors either way. |
| **NE-LP-REBAC-D1** | ~~`delete_tuples` of a tuple that was never written was a silent no-op~~ **RESOLVED (2026-07-24, NE-REBAC-REVOKE-IDEMPOTENT)**: now raises `TupleAbsentError` (a `BackendError` subclass) TRANSACTIONALLY — matching the server. | 400s a transactional delete of a missing tuple (`code=write_failed_due_to_invalid_input`, message `"cannot delete a tuple which does not exist"`); `_client.py` now maps that EXACT signature to `TupleAbsentError` instead of a plain `BackendError`. | **RESOLVED by NE-REBAC-REVOKE-IDEMPOTENT (2026-07-24).** The contract was decided deliberately: `ShareService.revoke` is genuinely idempotent — it catches ONLY `TupleAbsentError`; every other backend failure (outage, auth, malformed request) still propagates as `BackendError` untouched (C7). The narrowness of the catch is proven live, against the real server, in `test_live_non_absent_delete_error_not_swallowed` (a differently-shaped delete rejection reaches the server and is NOT swallowed). `InMemoryOpenFga` was corrected to raise the same typed error transactionally so this class of fake-more-forgiving-than-production divergence cannot hide the next such bug. Pinned in `test_live_delete_semantics_pinned` (the precise type) and `test_live_revoke_idempotent_end_to_end` (the resulting contract, end to end). |
| — | A STORED (non-contextual) `(user, member, group)` tuple grants nothing — `group#member` resolves ONLY via contextual tuples the resolver derives per-query (`test_stored_membership_tuple_grants_nothing`). | RESOLVES a stored membership tuple too — `neutron.fga` declares `member: [user]` with no restriction on how the tuple got there, and OpenFGA has no notion of "this relation may only ever be supplied contextually". | Real, expected OpenFGA model semantics, not a bug. The "membership is never stored" guarantee is enforced at the WRITER layer, not the server: `ShareService` never accepts a `member` relation (only `viewer`) and never accepts a caller-supplied `#` subject, so this path can never be reached through the governed writer regardless of what the raw server allows. Both halves (server resolves it; `ShareService` independently blocks writing it) are proven together in `test_live_stored_membership_semantics_pinned`. |
| **NE-LP-REBAC-D2** | N/A — `InMemoryOpenFga.read_authorization_model` just echoes back exactly what was stored, so `verify_model_pin` trivially passes in every fake-based test. | `bootstrap.verify_model_pin` raises `ModelPinError` UNCONDITIONALLY against a real server, even with ZERO drift from the in-repo pinned model. Root cause: the OpenFGA Python SDK's generated `to_dict(serialize=True)` unconditionally emits every declared OpenAPI field for every nested object — nulls and empty-string defaults included (`"condition": ""`, `"wildcard": null`, `"module": ""`, `"source_info": null`, …) — while `neutron.model.json` is written sparse; `bootstrap._canonical()` sorts but never strips those SDK-injected defaults, so the two dicts can never structurally match. | **FIXED (NE-REBAC-MODELPIN, 2026-07-24):** `_comparable` now strips SDK-injected raw `None`/`""`/`[]` unset defaults symmetrically on both sides before canonicalizing. Raw `{}` is stripped too, EXCEPT at the two OpenFGA presence-marker field names (`_PRESENCE_MARKER_KEYS`: `"this"`, `"wildcard"`), where the empty payload itself — not its content — is the semantic signal (`Userset.this` = direct rewrite, `RelationReference.wildcard` = `user:*`/PUBLIC). Two real-server divergences from the local SDK round-trip were found and fixed during this build: (1) an EARLIER version of this fix blanket-dropped every raw `{}`, which was non-vacuous only by accident for `this` (protected by an added dict key) but NOT for `wildcard` (a sibling field) — a server-side widening to `user:*` would have passed the pin check silently, caught by review before merge; (2) the REAL live server round-trips an empty `relations` map or the top-level `conditions` map as raw `{}` (not `null`, unlike the local SDK deserializer used by `test_bootstrap.py`'s fake-lane tests), which the marker-based fix strips correctly but a naive "never strip `{}`" fix would NOT — caught by the mandatory live-server run in this same build, before merge. Undrifted models now PASS against a real server (`test_live_bootstrap_and_model_pin_verifies`) while ADD-class (`test_live_model_pin_isolates_from_drifted_latest`), ALTER-class (`test_live_model_pin_rejects_rewritten_relation`), and PRESENCE-class wildcard-widening (`test_live_model_pin_rejects_wildcard_widening`) drift all still raise `ModelPinError`; fast SDK-round-trip regressions for all of the above lock the same behavior in `test_bootstrap.py`. |

CI-container green does not imply production-correct — the entire reason this
lane exists is that `InMemoryOpenFga` could not, and did not, catch any of
the four rows above.

---

## DEFERRED TO NE-087-WIRING (O-4)

This unit ships the **module only**. The following are explicitly deferred to the
O-4 rider (where the live server is used; the module is OFF by default until
then):

- **OpenFGA compose service** in `deploy/compose/pilot` (and dev): a
  `profiles: ["rebac"]` service mirroring the `opa` shape (loopback-only port,
  `no-new-privileges`, `cap_drop: ALL`, mem/cpu limits, healthcheck) + a separate
  `openfga` postgres database + a one-shot `openfga migrate` init step. **The
  server image MUST be a `tag@sha256` digest pin of a version containing the
  fixes for CVE-2026-34972 (BatchCheck improper enforcement) and CVE-2026-33729
  (conditions + caching incorrect cached results).** This module uses only
  `ListObjects` (no BatchCheck, no conditions), so neither CVE affects it — but
  the wired server must be patched.
- **`tools/ne-cli` guardian evolution**: a `rebac` profile in `PROFILE_MATRIX`, a
  one-shot-init-job carve-out of the restart rule (for `openfga migrate`), and the
  dev no-profile rule.
- **`ne-api` composition-root wiring**: construct
  `PrincipalAclFilter(shared_docs=OpenFgaSharedDocumentResolver(...))`, provide a
  production `DocumentReadProbe` over the trimmed retrieval path, add the share
  endpoints (`POST/DELETE/GET /v1/documents/{id}/shares`), route
  `REQUIRE_APPROVAL` through the NE-035 gate, call `verify_model_pin` at process
  start, expose `ne rebac gc`, and wire the `degraded_count`/`truncated_count`
  resolver counters to ne-obs OTel counters.
- **Live conformance** of `_client.py` against a real digest-pinned OpenFGA
  (testcontainers/compose) and the end-to-end no-bypass suite against the real
  stack.
