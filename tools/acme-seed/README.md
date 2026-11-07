# ne-acme-seed — live-tenant Acme corpus seeder (NE-102)

`ne-acme-seed` pushes a deterministic, lightest-sufficient **slice** of the
committed `tools/acme` synthetic corpus **INTO** the three dev tenants (M365
SharePoint, Salesforce, Slack) so the already-built **read-only** connectors
(NE-044/045/047) can pull it back — the NE-077 LIVE gate. NE-075 then asserts the
permission traps against the LIVE ids this tool records in a per-tenant manifest.

## Why this lives in `tools/` (not `packages/ne-connect`)

This tool is **egress-by-design**: it WRITES to SaaS tenants. The shipped product
(`packages/ne-connect`) is deliberately kept read-only, and its **C2 egress
guardian** (`packages/ne-connect/tests/test_c2_egress_guardian.py`) pins the
outbound-HTTP surface of the product package. Adding write verbs there would
widen the product's egress/attack surface, so instead the seeder **subclasses**
the connector clients here and adds the thin write verbs (Graph `PUT`/`DELETE`,
Salesforce `PATCH`-upsert/`DELETE`, Slack write methods) **in the tool**. Every
new verb inherits the connector's SSRF / host-allowlist / air-gap /
guard-before-token guarantees (guard-URL-first ordering is a structural mirror of
`GraphClient.post_json`). `packages/ne-connect` is untouched.

## CI is 100% mocked; live seeding is an owner-run step

Every test uses `httpx.MockTransport` — **zero** network sockets, no live tenant.
Running `push` / `unseed` / `check` against real tenants is an **owner-run**
acceptance step performed after the add-lists below are applied.

```
uv run ne-acme-seed slice                       # offline — prints the pinned slice
uv run ne-acme-seed check  --tenant m365         # owner-run connectivity/preflight
uv run ne-acme-seed push   --tenant salesforce   # owner-run seed
uv run ne-acme-seed unseed --tenant slack        # owner-run cleanup
```

## The persona / trap model

Four canonical personas map to four **real** principals (+ 4 groups per tenant),
NOT the full 40 people / 9 groups:

| persona | corpus id | group             |
|---------|-----------|-------------------|
| P1 Exec | `per-0000`| `executive`       |
| P2 Eng  | `per-0006`| `dept-engineering`|
| P3 Sales| `per-0016`| `dept-sales`      |
| P4 Legal| `per-0038`| `dept-legal`      |

The slice is 12 docs — 3 per visibility class:

* **public** → all 4 groups.
* **department** → its dept group **+ `executive`** (so P1 *sees* dept docs).
* **exec_only** → `executive` only.
* **named** (the sharpest trap) → ONLY the persona user(s) in `allowed_people`,
  **never a group, never P1**. Pinned trio: `doc-0310` (P2), `doc-0377` (P3),
  `doc-0077` (P4). An exec must NOT see these.

Plus a ~30-record slice (15 tickets, 31 people incl. P4, and the account closure)
for Salesforce.

## Secrets / config

Tenant creds come from the same secure locations the owner staged
(`~/ne-owner-secrets/{m365,salesforce,slack}/`) via the connectors'
`NE_M365_*` / `NE_SALESFORCE_*` / `NE_SLACK_*` env (SecretStr / PEM-path fields).
The seeder never reads key material itself and never prints a token. The
principals map (persona → real principal id) is a separate owner-staged file,
default `~/ne-owner-secrets/acme-seed/principals.json` (`--principals` to
override), and is **never committed**:

```json
{
  "m365": {
    "users":  {"per-0000": "<entra-user-id-or-UPN>", "per-0006": "...", "per-0016": "...", "per-0038": "..."},
    "groups": {"executive": "<group-object-id>", "dept-engineering": "...", "dept-sales": "...", "dept-legal": "..."}
  },
  "slack": {"users": {"per-0000": "U...", "per-0006": "U...", "per-0016": "U...", "per-0038": "U..."}}
}
```

Missing-entry policy: a missing **M365 group** or a missing **named-doc user**
is a HARD ERROR before any write (fail closed — the trap is the point). A missing
**Slack user** is a warn-and-continue (channel membership is owner-completable).

## Owner add-lists (apply once, per tenant)

### M365 / SharePoint
* Grant the Entra app **`Sites.ReadWrite.All`** application permission + admin
  consent. NOT `Files.ReadWrite.All` (SharePoint libraries are Sites), NOT
  `Sites.FullControl` (seed into the existing site).
* Hand-create the **4 users** + **4 groups** (`executive`, `dept-engineering`,
  `dept-sales`, `dept-legal`) and fill `principals.json`.
* Set `NE_M365_SITE_IDS` to the target site (the seed writes under `/Acme/` in
  that site's default document library).

### Salesforce
* Create custom field **`Acme_Source_Id__c`: Text(64), External ID, Unique** on
  **Account, Contact, Case**, and grant field-level security to the integration
  user's profile. `push` refuses with this exact add-list if the field is absent
  (`check --tenant salesforce` runs the same preflight). The `api` scope already
  covers writes — no new OAuth scope.
* The trap is **OBJECT-LEVEL ONLY**: two permission sets — `Acme_Case_Reader`
  (reads Case) and `Acme_No_Case` (cannot). This mirrors the connector's
  `ObjectPermissions → sf-permset:<Id>` label model (`salesforce/acl.py`); it does
  **NOT** model record sharing / OWD / role hierarchy, so no record-owner/sharing
  trap is built. Personas map logically: P2 → `Acme_Case_Reader`, P3 →
  `Acme_No_Case`.

### Slack
* Create the **separate** dev seeder app from `slack-seeder-manifest.json`,
  install it, and stage its bot token as `NE_SLACK_BOT_TOKEN`. This MUST be the
  seeder app's token, **never** the shipped read-only connector app's token.
* Channels: `#acme-general` (public, all 4 personas), `#eng-private` (private,
  P2), `#legal-confidential` (private, P4). The dept-sales doc and the named-P3
  doc are posted **nowhere** (no sales channel — posting them elsewhere would
  leak the fact across the ACL boundary; recorded as a warning).

## Manifest & idempotency

Each `push` writes `{manifest-dir}/{tenant}.json` (default `~/.ne/acme-seed/`,
OUTSIDE the repo) mapping corpus-id → live-id and persona → principal-id (for
Salesforce, persona → permission-set name), plus `warnings`. Re-runs are
idempotent: M365 path-addressed `PUT` replace; Salesforce upsert-by-external-id;
Slack `name_taken`-reuse + `[acme:<fact_id>]` history-marker gating.

`unseed` inverts each tenant: M365 deletes the `/Acme/` folder; Salesforce
deletes by external-id query + removes the 2 permsets; Slack `chat.delete`s the
recorded messages then **archives** the channels. **Archive asymmetry**: free
Slack workspaces cannot hard-delete channels without Enterprise — archiving is
the closest invertible action, and the residual is left as a manifest note.
