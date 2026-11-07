# ne-meridian-seed

`ne-meridian-seed` is the owner-run, egress-by-design loader for the committed Meridian fixture. It
pushes document bytes into a scoped SharePoint site and household/customer/case records into a
scoped Salesforce development org so the product's read-only connectors can ingest them later.
All automated tests use `httpx.MockTransport`; they open zero network sockets.

The write verbs remain outside `packages/ne-connect`. This package reuses the audited Acme seed
clients for guard-first Microsoft Graph PUT/DELETE and Salesforce PATCH/DELETE behavior, then adds
only Meridian path, ownership, stage, and mapping policy. Host/path guards run before token
acquisition, `AIR_GAP` fails closed, and errors never include response bodies or credentials.

Every M365 subcommand (`push`, `check`, `unseed`) requires an operator-chosen `--generation-id`;
see "Persistence generation id" below. Salesforce commands are unchanged.

## Pure slice

`ne.meridian_seed.slice.slice_corpus` loads strict `MeridianCorpus`, `GroundTruth`, and manifest
models, verifies every committed SHA-256 before returning data, and maps:

- 12 document artifacts to SharePoint bytes, exact MIME, same-path version metadata, and ACLs;
- 10 households to Salesforce `Account`;
- 15 customers to Salesforce `Contact`;
- 10 servicing tickets, 3 complaints, and 6 Reg E disputes to Salesforce `Case`.

## Persistence generation id

Every M365 subcommand requires `--generation-id <id>`. The generation id is an opaque, non-secret,
operator-chosen label for one persistence/seed generation. It must start with an ASCII letter or
digit; the remaining characters may be ASCII letters, digits, `.`, `_`, `:`, `@`, or `-`, up to 128
characters in total.

It is never auto-generated and never inherited silently. The initial push binds it into the saved
manifest. Supersession, `check`, and `unseed` must re-supply the exact same value; a missing or
mismatched id refuses before any authentication or network activity. The owner records the
generation externally, for example in the demo runsheet; the tool does not choose or remember it
on the owner's behalf.

## SharePoint stages

The sequence is enforced, not advisory:

1. `initial` uploads v3 of `Debit Card Dispute Handling SOP.docx` plus all 10 other initial
   artifacts.
2. `operational_graph` refuses before any network call unless a saved manifest proves `initial`
   completed. It publishes exactly 45 canonical `.negraph.json` records (15 Customer, 10 AutoLoan,
   10 PaymentSchedule, 10 ServicingNote) and records their exact live item IDs, paths, ACL grants,
   and 40 schema-admitted directed relationships. Re-running the completed stage performs no I/O.
3. `supersession` refuses unless both prior stages completed. It PUT-replaces the exact same SOP
   path with v4 and no other artifact.
4. Once a later stage completes, an earlier-stage downgrade is refused.

Stage history timestamps are captured from the real successful run; the tool has no backdating
option. Re-running a completed stage returns its saved state without network I/O, so it cannot add
duplicate document versions or stage-history rows.

Important live assumption: same-path PUT yields a browsable v3 → v4 SharePoint version only when
versioning is enabled on the target document library. The mocked suite proves exact path and bytes,
not tenant configuration. The owner must confirm versioning and read the live version history as an
acceptance step.

Every artifact grant is the exact set of Entra persona groups allowed by its fixture ACL. All three
users and all three groups are required before client construction; missing mappings fail closed.
Configure the exact selected site ID. The existing Graph seed client requires
`Sites.ReadWrite.All`; do not grant broader file or full-control permission.
The seeder accepts exactly one configured site ID; a multi-site connector scope is ambiguous for
an owner-controlled seed target and is refused before provider I/O.

Operational Customer items grant ops, approver, and frontline. AutoLoan, PaymentSchedule, and
ServicingNote items grant ops and approver only -- account-level records are withheld from the
frontline persona uniformly, so a frontline principal gets the SAME refusal whichever record type
(and therefore whichever retrieval tier) serves the question. The withheld set is
`ne.connect.m365.operational_graph.MERIDIAN_FRONTLINE_WITHHELD_CLASSES`, used both to author the
grants and to verify the manifest receipt. Their bodies use the strict
`meridian-operational-record/v1` envelope, schema version `1.1.0`, canonical JSON,
`application/json`, and the `.negraph.json` suffix. A malformed declared record aborts the source
run; it is never downgraded to an ordinary Document or allowed to advance a delta cursor.

## M365 read-only preflight

`check --tenant m365` is a useful read-only preflight. It validates the configured target, resolves
the site's default drive, probes the `/Meridian` folder, and reports one class-only status. It
never creates folders, uploads bytes, invites principals, alters permissions, or saves a manifest.
It still requires `--generation-id` and refuses a missing or mismatched value before any network
activity.

Reported statuses:

- `safe_new`: no `/Meridian` folder exists on the resolved drive; the target looks clean for a
  fresh `initial` run.
- `owned` (with completed stages): the live folder and exact SOP item match the saved manifest's
  frozen target binding and recorded stage.
- `mismatched`: an owned folder exists, but the live state does not match the manifest's binding.
- `legacy_refused`: the saved manifest predates target descriptors and is refused for mutation (see
  below).
- `drive_mismatch`: the site's resolved default drive differs from the drive bound in the manifest.
- `blocked_unowned_folder`: a `/Meridian` folder exists that no saved manifest proves ownership of;
  the tool will not touch it.
- `folder_missing`: the manifest binds an owned folder item id that no longer resolves on the drive.
- `folder_unowned`: `/Meridian` resolves, but its live folder item id differs from the manifest's
  exact owned folder id.

## Salesforce

Records are PATCH-upserted by `Meridian_Source_Id__c` using values such as
`meridian-v1:mer-cus-001`. Before any write, all three objects must expose that Text(128), External
ID, Unique field. Identifier and owned-prefix validation run before a token request.

The current least-privilege persona assumption, pending owner confirmation in the live org, is:

| Permission set | Read objects |
|---|---|
| `Meridian_Ops_Reader` | Case |
| `Meridian_Approver_Reader` | Account, Contact, Case |
| `Meridian_Frontline_Reader` | Account, Contact (Case denied) |

Every generated `ObjectPermissions` row explicitly denies create/edit/delete/view-all/modify-all.
The tool creates permission sets and object permissions but does not assign them to real users; that
remains an owner-controlled org action.

Unseed first resolves and validates the complete deletion set. It deletes only records whose
external key starts with `meridian-v1:` and whose source id matches the Meridian namespace. One
unowned/malformed result aborts before the first DELETE. Cases are removed before Contacts, then
Accounts; only the three exact Meridian permission-set names are eligible for cleanup.

## Owner-staged principals and manifests

The M365 principal map is outside the repository (default
`~/ne-owner-secrets/meridian-seed/principals.json`):

```json
{
  "m365": {
    "users": {
      "ops": "<entra-user-id-or-upn>",
      "approver": "<entra-user-id-or-upn>",
      "frontline": "<entra-user-id-or-upn>"
    },
    "groups": {
      "meridian-ops": "<group-object-id>",
      "meridian-approver": "<group-object-id>",
      "meridian-frontline": "<group-object-id>"
    }
  }
}
```

Successful runs save manifests outside the repository under `~/.ne/meridian-seed/`. A failed push
does not overwrite the prior manifest, making an idempotent retry safe. Every mutation command also
requires the literal confirmation `MERIDIAN-DEMO-TENANT`.

Saved M365 manifests carry a frozen target descriptor binding: the M365 tenant kind and corpus
version, canonical configured Graph cloud base URL, exact Entra tenant id, exact configured
SharePoint site id, exact resolved drive id, exact owned `/Meridian` folder item id, and generation
id. No secrets, tokens, certificate paths, headers, or credentials are ever persisted.

Manifests saved by older versions have no target descriptor and are not trusted for mutation:
`push` and `unseed` refuse them with a migration message. Use the narrowly scoped adoption command
below only for the exact legacy Meridian target; otherwise archive the legacy file, verify the
target is clean, and start a fresh initial run under a new generation.

For the one bounded pre-reset Meridian migration, `adopt-legacy-m365` is the explicit read-only
exception. It accepts only a legacy manifest with the exact 12 canonical artifact bindings and
`initial` then `supersession` history. It resolves the single configured site/default drive and
`/Meridian` folder, lists the folder once, and requires exact equality with the 11 unique live
items represented by those bindings (v3 and v4 share one stable SOP item). Missing, surplus,
malformed, paginated, or mismatched results refuse without provider mutation. On success it binds
the operator-supplied generation and target IDs and atomically replaces the local manifest while
preserving its history. A replay of an already matching binding performs no provider I/O and no
manifest rewrite. This command never repairs ACLs, deletes content, or writes operational records.

```sh
uv run --package ne-meridian-seed ne-meridian-seed slice

uv run --package ne-meridian-seed ne-meridian-seed check \
  --tenant m365 --generation-id meridian-demo-gen-1

uv run --package ne-meridian-seed ne-meridian-seed adopt-legacy-m365 \
  --manifest-dir ~/.ne/meridian-seed --confirm MERIDIAN-DEMO-TENANT \
  --generation-id meridian-demo-gen-1

uv run --package ne-meridian-seed ne-meridian-seed push \
  --tenant m365 --stage initial --confirm MERIDIAN-DEMO-TENANT \
  --generation-id meridian-demo-gen-1

uv run --package ne-meridian-seed ne-meridian-seed push \
  --tenant m365 --stage operational_graph --confirm MERIDIAN-DEMO-TENANT \
  --generation-id meridian-demo-gen-1

# Run later as a real dated event; do not backdate or immediately collapse the interval.
uv run --package ne-meridian-seed ne-meridian-seed push \
  --tenant m365 --stage supersession --confirm MERIDIAN-DEMO-TENANT \
  --generation-id meridian-demo-gen-1

uv run --package ne-meridian-seed ne-meridian-seed push \
  --tenant salesforce --stage initial --confirm MERIDIAN-DEMO-TENANT

# M365 unseed re-supplies the same generation id; Salesforce unseed does not take one.
uv run --package ne-meridian-seed ne-meridian-seed unseed \
  --tenant m365 --confirm MERIDIAN-DEMO-TENANT \
  --generation-id meridian-demo-gen-1
```

No live mutation, SharePoint version-history verification, Salesforce field/permission creation, or
persona assignment occurs in CI. Those remain explicit owner-controlled acceptance steps.

## Honest operational boundary

- Successful local/mock proof does not authorize live writes.
- Safe live use requires an owner-selected clean target and an externally recorded persistence
  generation.
- The real demo dossier order is: v3 + operational-graph seed → one completed governed M365 sync →
  v4 supersession → one completed governed incremental sync.
- There is no Graph `/versions` recovery path in the connector, so v3 cannot be reconstructed after
  the fact.
- Scheduler/manual sync concurrency must be locked out separately before owner writes.
- A partial live failure has no automatic rollback: a crash after `/Meridian` folder creation but
  before manifest save leaves an unknown live folder, and the next run blocks rather than overwrites
  or deletes it. The owner resolves unknown folders manually.
- Versioning, groups/membership, ACLs, and final version history remain live acceptance evidence.
- This tool does NOT build connector ingestion, graph construction, D2 evidence, scheduler locking,
  deployment, or live version-history proof.
