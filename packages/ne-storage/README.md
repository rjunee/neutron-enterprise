# ne-storage

Implements the FROZEN `ne.contracts.storage.ObjectStore` Protocol —
put/get/stat/delete/list_objects, plus `archive()` (WORM retention) — with a
two production cloud adapters selected via the `ne.plugins.object_store`
plugin registry:

| plugin name  | class                                            | profile                                |
|--------------|--------------------------------------------------|----------------------------------------|
| `s3`         | `ne.storage.s3.S3ObjectStore`                    | **production** — AWS / S3-compatible (this package) |
| `azure_blob` | `ne.storage.azure_blob.AzureBlobObjectStore`     | **production** — Azure Blob (this package) |
| `memory`     | `ne.contracts.testing.memory.MemoryObjectStore`  | test reference (ships in ne-contracts) |

Azure Blob is **not** S3-compatible, so `S3ObjectStore` cannot serve an Azure
deployment at all — each cloud needs its own adapter. That is why there are
two (SPEC C9: AWS and Azure as co-equal first-class targets).

This is the WORM anchor NE-AUDIT-CRON's checkpoint loop archives signed audit
checkpoints into: pointing production at real write-once storage is a
**config change** (`NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE=s3|azure_blob` +
the matching `NE_STORAGE_S3_*` / `NE_STORAGE_AZURE_BLOB_*` env), never a code
change.

## Contract → S3 mapping

**`ListObjectsV2` appears nowhere in this adapter.** It enumerates CURRENT
versions only, so a key whose current version is a delete marker vanishes from
it — the exact mechanism of the C10 hole this package was rebuilt to close.
Every path below instead resolves the **locked version** first, via a
`ListObjectVersions` page-walk plus one `HeadObject` per version (the "resolve
locked version" step; see `_locked_version`). That step is **unconditional and
first** on every read and write path — never a fallback on a 404.

| Protocol method  | S3 operations                                                | Notes |
|-------------------|--------------------------------------------------------------|-------|
| `put`             | resolve locked version (`ListObjectVersions` + `HeadObject` per version) → `PutObject` → `HeadObject` (stat the new version) | A locked version → `WormViolationError`, keyed on the LOCKED version, never the current one, so a layered unlocked version cannot switch the check off. No locked version → straight to `PutObject`. |
| `get`              | resolve locked version → `GetObject` (with `VersionId=<locked>` when one exists) | The locked version is resolved FIRST and ALWAYS. An archived key hidden by a delete marker, or shadowed by a layered version, still returns its ARCHIVED bytes. `NoSuchKey` with no locked version → `ObjectNotFoundError`. |
| `stat`             | resolve locked version → (only if none) `HeadObject` on the current version | Locked version wins unconditionally, so the reported stat is the archived object's. 404 with no locked version → `ObjectNotFoundError`. **S3 lowercases user metadata keys** — see "Operational notes" below. |
| `delete`           | resolve locked version → `HeadObject` (current) → `DeleteObject` (versionless) | A locked version → `WormViolationError`. Absent → returns silently (matches `MemoryObjectStore`'s `dict.pop(default)`). Note the `DeleteObject` S3 itself always permits lays a delete marker — the REQUIRED bucket policy below is what stops that, not this code. |
| `list_objects`     | `ListObjectVersions` (paginated, prefix-scoped, includes `DeleteMarkers`) → `HeadObject` per VERSION per key → `HeadObject` (current) for keys with no lock | N keys × V versions: `ListObjectVersions` returns neither Lock retention nor legal-hold status nor content-type, so every version is head-ed. A key with a lock is ALWAYS listed, from the locked version's stat, even behind a delete marker. A key whose latest entry is a delete marker and which carries no lock stays hidden (a legitimate delete). A key that 404s mid-walk (raced a real concurrent delete) is skipped, not surfaced. Version count per key is CAPPED — see "Operational notes". |
| `archive`          | `GetObjectLockConfiguration` (bucket preflight, once per adapter instance) → resolve locked version → (idempotent short-circuit \| `WormViolationError` \| `HeadObject` on the current version) → `PutObjectRetention` (`Mode=COMPLIANCE`) | The lock-config preflight runs at the TOP of every `archive()` and needs **`s3:GetBucketObjectLockConfiguration`** (see the IAM list below) — without it `put` still succeeds and `archive` always fails, so checkpoints land unlocked. A locked version is EXTENDED in place. Only `COMPLIANCE` counts as WORM; an active `GOVERNANCE` lock is **refused by default** (opt-in upgrade — see "WORM honesty"); a legal hold is a lock but carries no date to compare, so the requested retention is applied as-is. |

Physical S3 keys are `key_prefix + logical_key`; the prefix is stripped again
on every read path, so `list_objects` returns logical keys identical to what
`memory` would return for the same call sequence.

## WORM honesty — read this before trusting the guarantee

This is a two-layer guarantee, and the two layers are enforced by two
different parties:

1. **The archived object VERSION is made immutable by the SERVER itself.**
   `PutObjectRetention` in `COMPLIANCE` mode is enforced by S3/MinIO, not by
   this adapter's code. A raw client that bypasses `S3ObjectStore` entirely
   still gets `DeleteObject VersionId=<locked>` denied, and any attempt to
   shorten the retention on that version denied — proven in
   `tests/test_s3_worm_live.py::test_raw_client_delete_version_denied` /
   `test_raw_client_shorten_retention_denied`, which use a RAW aiobotocore
   client, not the adapter. **Nobody — including a root credential — can
   delete or shorten a COMPLIANCE-locked version before `retain_until`.**
   This is the actual point of the control, and it holds regardless of the
   next paragraph.

2. **The frozen contract's KEY-level `put`/`delete` blocking is CLIENT-SIDE.**
   The contract says `put`/`delete` against an archived key raise
   `WormViolationError` while retention is active. S3 has no server-side
   concept of "reject a PUT against this key because SOME version is
   locked" — a `PutObject` against a locked key always succeeds and creates
   a **new** version (the old, locked version is untouched). So
   `S3ObjectStore` implements this half of the contract itself: resolve the
   locked VERSION (`ListObjectVersions` + `HeadObject` per version), then act.
   Note it is keyed on the locked version, **not** on a `HeadObject` of the
   current one — that older shape meant a single layered unlocked version
   switched the check off permanently, for every caller. There is still a
   **TOCTOU window** between the read and the write: a concurrent caller could
   slip a `put` through between our check and our write.

   **This does not weaken the actual WORM guarantee.** A `put` that races
   past the check does not touch the locked version at all — the archived
   bytes stay exactly as immutable as before. What the TOCTOU window can
   affect is only the *contract's courtesy error* (whether a racing caller
   SEES a `WormViolationError` or a silent new version lands next to the
   still-immutable old one), never the durability of the archived data
   itself.

3. **A versionless `DeleteObject` is ALWAYS permitted, and Object Lock does
   not stop it.** It deletes nothing — it lays a **delete marker** as the new
   current version. No locked version is modified, so Object Lock has nothing
   to refuse. The archived bytes remain immutable underneath, but the key now
   reads as absent to a plain `GetObject`/`ListObjectsV2`. `S3ObjectStore`
   handles this on its own read paths (`get`, `stat`, `list_objects` and
   `archive` all resolve to the LOCKED version, not merely the current one, so
   an archived checkpoint stays readable and stays listed). **But the adapter
   being correct is not the guarantee.** Closing this against a raw client or a
   root credential requires the bucket policy in
   "Bucket prerequisites" below, and that policy is REQUIRED, not advisory.

4. **The guarantee is specifically `COMPLIANCE`-mode Object Lock. A
   `GOVERNANCE`-mode lock is deliberately NOT treated as WORM.** S3 Object Lock
   has two modes, and only one of them is write-once. A principal holding
   `s3:BypassGovernanceRetention` can delete a `GOVERNANCE`-locked version and
   can shorten its retention — both verified from a raw client against MinIO in
   `tests/test_s3_worm_live.py::test_governance_lock_is_bypassable`, where the
   identical calls against a `COMPLIANCE` lock are refused. It is an
   accident-guardrail, not storage you can anchor an audit log to.

   So a version carrying only a `GOVERNANCE` retention is **never** reported as
   `ObjectStat.worm_retain_until` and **never** raises `WormViolationError`
   from `put`/`delete`. Both halves matter: reporting it would overclaim a
   guarantee the bytes do not have (a silent C10 downgrade — the product
   claiming WORM protection it lacks), and blocking writes on it would refuse
   legitimate operations on that same false premise.

   Objects this adapter archived are always `COMPLIANCE`, so a `GOVERNANCE`
   lock can only arrive from another tool, a bucket default, or a prior
   deployment — narrow, but real. What `archive()` does when it meets one is
   the subject of the next section.

5. **An Object Lock LEGAL HOLD *is* treated as a lock.** A legal hold is the
   other Object Lock protection: `ObjectLockLegalHoldStatus: ON`, no date, in
   force until an explicit `PutObjectLegalHold Status=OFF`. Unlike
   `GOVERNANCE` there is **no bypass permission** — verified against MinIO in
   `tests/test_s3_worm_live.py::test_legal_hold_version_delete_denied_by_server`,
   where `DeleteObject VersionId=<held>` is refused with and without
   `BypassGovernanceRetention`. It is real, indefinite, server-enforced
   protection.

   So a held version keeps the key **listed**, makes `get`/`stat` resolve to it,
   and makes `put`/`delete` raise `WormViolationError` — the same treatment a
   `COMPLIANCE` retention gets. The GOVERNANCE reasoning does **not** transfer:
   permitting writes over a `GOVERNANCE` lock is defensible precisely because it
   is not WORM, and a legal hold is.

   `ObjectStat.worm_retain_until` stays `None` for a hold-only version, and this
   is deliberate: that field is an **instant**, and a legal hold has none.
   `None` there means "no dated WORM retention", **not** "unprotected" — the key
   staying listed and the mutations raising are how the protection surfaces.
   `archive()` over a hold-only version applies the requested retention as-is
   (there is no standing date to extend or shorten) and the hold survives
   alongside it, so the result is strictly more protected than before.

   This was a real hole, not a hypothetical: before it was handled the adapter
   did not know the field existed, so `put` and `delete` were both allowed, the
   adapter's own `delete()` laid a marker over genuinely protected bytes, and
   `list_objects` then reported the key absent — the same shape as the
   delete-marker C10 defect, reopened for a protection mode that fix did not
   consider.

6. **A protection mode you cannot READ is invisible, not an error — so the IAM
   list is part of the guarantee.** Every statement above depends on the
   adapter being able to *see* the protection on a version, and on real AWS
   those reads are permission-gated in a fail-open way. `HeadObject` returns
   `x-amz-object-lock-legal-hold` only to a caller holding
   `s3:GetObjectLegalHold`, and `x-amz-object-lock-mode` /
   `x-amz-object-lock-retain-until-date` only to one holding
   `s3:GetObjectRetention`. **Lacking the permission does not fail the call.**
   `HeadObject` returns `200` and simply omits the header, so a protected
   version reads as unprotected and every downstream decision — is this key
   locked, may it be deleted, should it be listed — is made on that false
   premise.

   Which means the minimal IAM list in this README is **load-bearing for
   correctness, not just for access**. An under-scoped credential does not
   produce an `AccessDenied` you would notice in a log; it produces a quietly
   wrong answer about whether the audit anchor is protected. Compounding it,
   MinIO returns these headers regardless of permission, so a MinIO-backed
   test or staging environment stays green through the failure. That is why
   the published action list is itself pinned by a live test driving a
   restricted credential
   (`test_documented_iam_actions_suffice_for_the_checkpoint_write_path`)
   rather than trusted to review.

### `GOVERNANCE` → `COMPLIANCE` upgrade — opt-in, and why

`archive()` over an **active `GOVERNANCE` lock is REFUSED by default**
(`GovernanceUpgradeRefusedError`). Set
`NE_STORAGE_S3_ALLOW_GOVERNANCE_UPGRADE=true` to opt in.

The upgrade itself is a strengthening — it converts a bypass-able guardrail
into real WORM, and the effective date is clamped to
`max(requested, governance)` so it can never trade a longer weak lock for a
shorter strong one. **But that clamp is a read-then-write, and S3 Object Lock
has no compare-and-set:**

1. the version carries `GOVERNANCE` until day 30
2. caller A heads it and computes `effective = day 30`
3. caller B extends the `GOVERNANCE` retention to day 365
4. caller A writes `COMPLIANCE` until day 30 — B's day-365 retention is **gone**,
   and permanently so, because `COMPLIANCE` cannot be shortened to put it back

The race cannot be closed, only avoided or disclosed. Refusing by default means
the default path never touches an existing lock and is therefore **incapable of
weakening one**. The opt-in restores the upgrade for the deliberate migration
case it exists for — checkpoints previously written by another tool or under a
bucket default — and is **unsafe under concurrent retention mutation**: enable
it only when no other writer is mutating retention on that bucket, ideally as a
scoped migration rather than a standing setting.

**The gate applies to a version carrying a `GOVERNANCE` retention *and* a legal
hold, too.** That combination used to slip past it: the hold makes the version
count as locked, so `archive()` took the locked-version branch, found no
`COMPLIANCE` retention there (the mode is `GOVERNANCE`), read it as a bare
hold-only lock, and proceeded to write a `COMPLIANCE` retention that could be
**shorter** than the standing `GOVERNANCE` one — skipping the opt-in gate, the
`max(requested, governance)` clamp and `BypassGovernanceRetention` alike. Only
the server refusing that write prevented it, and the refusal was then reported
as a possible concurrent-`archive()` race for what is a permanent
configuration state. The opt-in path was equally broken in the other
direction: it was **unreachable**, so an operator who had explicitly enabled
the upgrade could not archive such a key at all. The `GOVERNANCE` check now
runs on the target version *before* any `COMPLIANCE` comparison, so this
combination routes through the same gate, the same clamp and the same
`GovernanceUpgradeRefusedError` as a plain `GOVERNANCE` lock.

The upgrade also requires `s3:BypassGovernanceRetention` on the credential
(MinIO refuses a `GOVERNANCE` → `COMPLIANCE` transition without it, even for a
later date), which is **not** granted by default — see `docs/DEPLOYMENT.md`. A
credential lacking it gets a `WormViolationError` naming the missing permission
rather than a fabricated race diagnosis.

One precise note on that flag, because an earlier version of this document
overstated it: the adapter sends `BypassGovernanceRetention` only on this **code
path**, which is a static-code argument that concurrency defeats — between the
`HeadObject` and the `PutObjectRetention`, another actor can upgrade that same
version to `COMPLIANCE`, and the flag then lands on a `COMPLIANCE`-locked
object. The guarantee that actually holds is narrower and is about the flag's
power, not its routing: **it is not a shortening primitive.** S3 refuses to
shorten a `COMPLIANCE` retention with or without it, so the worst case is a
denied call, never a weakened lock.

`archive()`'s own failure reporting is deliberately narrow. When the server
rejects a `PutObjectRetention` on a locked version (observed as
`InvalidRequest`/400 against MinIO; documented AWS behavior for the analogous
case is `AccessDenied`, served as 403), that is translated to
`WormViolationError` rather than leaking a raw `ClientError`. The message
offers a concurrent `archive()` as **one possible cause** — it no longer
asserts a race as THE diagnosis. That earlier framing was wrong: the same
400 also covers absent lock configuration and malformed retention payloads,
so a permanent configuration fault was being reported as a transient race,
sending operators to retry something that would never succeed. Missing lock
configuration is now detected LOCALLY, before that path is reachable.

## Retention-timestamp precision (STEP-0 decision, Branch B)

Verified empirically against MinIO `RELEASE.2025-09-07T16-13-09Z`:
`ObjectLockRetainUntilDate` does **not** round-trip sub-second precision —
the server silently truncates to millisecond resolution (a `.123456`
microsecond value round-trips as `.123000`). Left unhandled, this could let a
caller's intended retention window end up **earlier** than requested — an
accidental shortening, which is exactly the failure mode this whole package
exists to prevent.

`S3ObjectStore` resolves this with `_normalize_retain_until`: any
`retain_until` with a nonzero `microsecond` is **ceiled** to the next whole
second before it is ever sent to the server. Whole-second timestamps pass
through unchanged. Ceiling (never truncating) is the only safe direction — it
can only make the effective retention equal-or-later than requested, never
earlier. This is a permanent behavior, pinned by a regression test
(`test_retain_until_precision_roundtrip`).

The shared `ObjectStoreContractKit` (`packages/ne-contracts/src/ne/contracts/testing/kits/object_store.py`)
was updated in lockstep: every retention timestamp it constructs now goes
through one module helper, `_retain(**delta)`, which zeroes microseconds —
matching Branch B and keeping the kit's equality assertions meaningful
against a real server. **This is a kit change, not just an addition — see the
PR body for the required sign-off note (build-orchestration-plan §2 rule
4).** `MemoryObjectStore` is unaffected (it never had a precision issue); a
tz-naive `retain_until` raises `ValueError` for both adapters.

## Configuration — `S3ObjectStoreSettings`

Layered per `NeSettings` (init kwargs > env > `ne.toml` > defaults), env
prefix `NE_STORAGE_S3_`:

| field                | env var                          | default        | notes |
|----------------------|-----------------------------------|----------------|-------|
| `bucket`             | `NE_STORAGE_S3_BUCKET`            | `""`           | **required** — empty fails loud at construction |
| `endpoint_url`       | `NE_STORAGE_S3_ENDPOINT_URL`      | `""`           | empty ⇒ AWS's default regional endpoints (production posture) |
| `region`             | `NE_STORAGE_S3_REGION`            | `us-east-1`    | |
| `access_key_id`      | `NE_STORAGE_S3_ACCESS_KEY_ID`     | `""`           | empty (with `secret_access_key` also empty) ⇒ botocore's default credential chain (IAM role / IRSA / env / shared config) — the recommended production posture |
| `secret_access_key`  | `NE_STORAGE_S3_SECRET_ACCESS_KEY` | `""`           | see above |
| `session_token`      | `NE_STORAGE_S3_SESSION_TOKEN`     | `""`           | STS temporary credential (`AssumeRole` / web identity). Only meaningful alongside an explicit key pair — the default chain sources and refreshes its own token |
| `key_prefix`         | `NE_STORAGE_S3_KEY_PREFIX`        | `""`           | physical key = `key_prefix + logical key` |
| `force_path_style`   | `NE_STORAGE_S3_FORCE_PATH_STYLE`  | `true`         | required by most non-AWS S3-compatible servers (MinIO included) |
| `allowed_hosts`      | `NE_STORAGE_S3_ALLOWED_HOSTS`     | `""`           | comma-separated operator allowlist, see C2 below |
| `connect_timeout_s`  | `NE_STORAGE_S3_CONNECT_TIMEOUT_S` | `5.0`          | |
| `read_timeout_s`     | `NE_STORAGE_S3_READ_TIMEOUT_S`    | `60.0`         | |
| `max_versions_per_key` | `NE_STORAGE_S3_MAX_VERSIONS_PER_KEY` | `1000`  | ceiling on the per-key version walk; a breach raises `TooManyObjectVersionsError` rather than stalling — see "Operational notes" |
| `allow_governance_upgrade` | `NE_STORAGE_S3_ALLOW_GOVERNANCE_UPGRADE` | `false` | opt in to upgrading a pre-existing `GOVERNANCE` lock to `COMPLIANCE`. Unsafe under concurrent retention mutation — see "`GOVERNANCE` → `COMPLIANCE` upgrade" |

```python
from ne.storage import S3ObjectStore, S3ObjectStoreSettings

settings = S3ObjectStoreSettings(bucket="ne-audit-checkpoints")  # AWS default endpoints, IAM chain
store = S3ObjectStore(settings)
```

### C2 / AIR_GAP behavior (construction-time, fail-closed)

- A **non-empty** `endpoint_url` is validated by
  `ne.net.ssrf.validate_in_vpc_endpoint(endpoint_url, allowed_hosts=<parsed allowed_hosts>)`
  — loopback/private-IP/`.internal`/`.local`/`.localhost` hosts and dotless
  compose service names pass automatically; anything else (a public IP, an
  arbitrary DNS name) must appear in `allowed_hosts` or construction raises
  `UnsafeEndpointError`.
- An **empty** `endpoint_url` (the AWS-default-endpoints production posture)
  is public-internet egress. If `AIR_GAP` is engaged, construction raises
  `AirGapViolationError` — an air-gapped deployment must supply an in-VPC or
  explicitly allowlisted `endpoint_url` (a private S3-compatible appliance),
  never the public AWS endpoints.
- Deploy docs (`docs/DEPLOYMENT.md`) note that AWS S3's regional FQDNs must be
  in the C2 egress allowlist for any deployment that does use the default
  AWS endpoints.

### `from_settings` — the worker-builder seam

```python
store = S3ObjectStore.from_settings(anything)  # constructs purely from NE_STORAGE_S3_* env
```

NE-AUDIT-CRON's checkpoint-loop builder resolves an `ObjectStore` plugin by
name and calls `cls.from_settings(platform.audit_settings)` uniformly across
whichever adapter is configured (falling back to `cls()` if the plugin has no
`from_settings`). `S3ObjectStore.from_settings` **accepts and ignores** its
positional argument — `AuditSettings` has no notion of an S3 bucket or
credentials, so this adapter always constructs from its own env-driven
`S3ObjectStoreSettings` regardless of what is passed. This mirrors the Cedar
PDP's `from_settings` ignore-precedent (same seam: a generically-typed caller
resolving a plugin-specific settings model).

## Bucket prerequisites — read before provisioning

**S3 Object Lock must be enabled AT BUCKET CREATION.** On real AWS S3 this is
`aws s3api create-bucket --bucket <name> --object-lock-enabled-for-bucket`
(equivalently `ObjectLockEnabledForBucket=True` on `CreateBucket`) — **it
cannot be turned on for an existing bucket afterwards.** Verified empirically
against MinIO too: attempting `PutObjectLockConfiguration` on a bucket that
was created without Object Lock fails with `InvalidBucketState` ("Object Lock
configuration cannot be enabled on existing..."). If you provision a bucket
without this flag, the fix is a **new bucket**, not a follow-up API call.
Enabling Object Lock also enables bucket versioning (a prerequisite for it);
you cannot disable versioning afterwards either.

**COMPLIANCE-mode retention cannot be shortened or removed by anyone,
including the account root** — see "WORM honesty" above. Choose
`retention_days` deliberately before archiving anything (`ne-audit`'s
`CheckpointConfig` default is 365 days).

Minimal IAM actions for the checkpoint role: `s3:PutObject`, `s3:GetObject`,
`s3:ListBucket`, `s3:GetObjectVersion`, `s3:ListBucketVersions`,
`s3:GetObjectRetention`, `s3:GetObjectLegalHold`, `s3:PutObjectRetention`,
`s3:GetBucketLocation`, `s3:GetBucketObjectLockConfiguration`.

**`s3:GetObjectLegalHold` is required for CORRECTNESS, not just access.** On
real AWS, `HeadObject` returns `x-amz-object-lock-legal-hold` *only* if the
caller holds `s3:GetObjectLegalHold` — [without it the call still returns
`200` and silently omits the
header](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html).
The adapter would then read a genuinely held version as unheld, miss it when
resolving the locked version, lay a delete marker over it on `delete()`, and
drop the archived checkpoint out of `list_objects` — the same C10 hole the
delete-marker fix closed, reopened by a missing permission. MinIO returns the
header regardless of permission, so no test against the local server can catch
this by behaviour; the published list is pinned instead by
`test_documented_iam_actions_suffice_for_the_checkpoint_write_path`, which
drives a legal hold through a credential scoped to exactly this list.
`s3:GetObjectRetention` gates `x-amz-object-lock-mode` and
`x-amz-object-lock-retain-until-date` the same way, and has always been listed.

**`s3:GetBucketObjectLockConfiguration` (bucket-scoped) is not optional.**
`archive()` calls `GetObjectLockConfiguration` at the top of *every*
invocation, to fail loud with the real reason on a bucket that has no Object
Lock. Without the permission, `put()` still succeeds and `archive()` always
fails — and `write_checkpoint` does `put` **then** `archive`, so every
checkpoint object lands in the bucket **unlocked** before the loop dies. That
is a C10 downgrade on the documented happy path, so it is pinned by a live
test (`test_documented_iam_actions_suffice_for_the_checkpoint_write_path`)
that drives the adapter with a credential scoped to exactly this list.

**`s3:DeleteObject` is deliberately NOT in this list** — see the required
bucket policy immediately below. **`s3:BypassGovernanceRetention` is also
deliberately absent**; it is needed only for the opt-in
`GOVERNANCE` → `COMPLIANCE` migration described above.

### REQUIRED bucket policy — deny `s3:DeleteObject` on the checkpoint prefix

**This policy is not optional. Without it, the retention this package applies
does not deliver the guarantee it appears to.**

Object Lock protects object **VERSIONS**. A `DeleteObject` call that carries
**no `VersionId`** is a different operation: it does not delete anything, it
lays a **delete marker** as the new current version. Object Lock does not
prevent that — there is no locked version being modified, so there is nothing
for it to refuse. The archived bytes survive underneath, but the key now reads
as absent to `GetObject` and `ListObjectsV2`. An archive nobody can find is
not, operationally, an archive.

`S3ObjectStore` reads through delete markers to the locked version, so the
adapter itself stays correct. But the guarantee must not depend on everyone
using the adapter — the point of a server-enforced control is that it holds
against a raw client and a root credential too. Only a bucket policy closes it:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyCheckpointDeletes",
      "Effect": "Deny",
      "Principal": "*",
      "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
      "Resource": "arn:aws:s3:::ne-audit-checkpoints/checkpoints/*"
    }
  ]
}
```

Scope notes:

- Replace `ne-audit-checkpoints` with your bucket and `checkpoints/` with your
  `NE_STORAGE_S3_KEY_PREFIX`. The `Resource` is **prefix-scoped on purpose** —
  a bucket-wide deny would block lifecycle cleanup of anything else sharing
  the bucket. Prefer a dedicated bucket where practical.
- `s3:DeleteObjectVersion` is included for defence in depth. COMPLIANCE-mode
  Object Lock already refuses it for a locked version; the policy also covers
  versions that are not (yet) locked.
- `"Principal": "*"` with `Effect: Deny` applies to **every** principal
  including the account root, which is the intent. An explicit `Deny` cannot be
  overridden by any `Allow` in any IAM policy.
- Apply this at provisioning time, alongside `--object-lock-enabled-for-bucket`.
  Unlike Object Lock, a bucket policy CAN be added later — but every delete
  marker laid before you add it is already there.

### Recovering archived bytes when a delete marker (or newer version) is on top

If a checkpoint reads as missing, or an unlocked version has been layered over
the archived one, the locked version is still present and still readable. The
adapter resolves this automatically; these are the manual equivalents for an
operator working with the AWS CLI.

List every version of the key, including delete markers:

```bash
aws s3api list-object-versions \
  --bucket ne-audit-checkpoints \
  --prefix checkpoints/2026-07-19T00-00-00Z.json \
  --query '{Versions: Versions[].{Id:VersionId,Modified:LastModified,Latest:IsLatest}, Markers: DeleteMarkers[].{Id:VersionId,Modified:LastModified,Latest:IsLatest}}'
```

Identify which version carries the COMPLIANCE retention (this is the archived
one — repeat per candidate `VersionId`):

```bash
aws s3api get-object-retention \
  --bucket ne-audit-checkpoints \
  --key checkpoints/2026-07-19T00-00-00Z.json \
  --version-id <VERSION_ID>
```

Read the archived bytes by explicit `VersionId` — this bypasses the delete
marker entirely and needs no mutation of the bucket:

```bash
aws s3api get-object \
  --bucket ne-audit-checkpoints \
  --key checkpoints/2026-07-19T00-00-00Z.json \
  --version-id <LOCKED_VERSION_ID> \
  ./recovered-checkpoint.json
```

To restore the locked version as current, **copy it forward** — do not try to
delete the marker or the newer version, and never weaken the lock:

```bash
aws s3api copy-object \
  --bucket ne-audit-checkpoints \
  --key checkpoints/2026-07-19T00-00-00Z.json \
  --copy-source 'ne-audit-checkpoints/checkpoints/2026-07-19T00-00-00Z.json?versionId=<LOCKED_VERSION_ID>'
```

Removing the delete marker instead (`delete-object --version-id <MARKER_ID>`)
also works and is the tidier fix, but it requires `s3:DeleteObjectVersion`,
which the policy above denies — by design. Copy-forward keeps the recovery path
inside read + write permissions the checkpoint role already holds.

### Operational notes (S3 behaviors worth knowing before you debug them)

- **S3 lowercases user metadata keys.** Metadata supplied to `put` as
  `x-amz-meta-*` comes back from `HeadObject` **lowercased**. A round-trip of
  `{"CheckpointID": "abc"}` returns `{"checkpointid": "abc"}`, so
  `stat().metadata` will **not** compare equal to mixed-case input. Use
  lowercase keys when writing, or normalize before comparing. This is S3
  behavior, not something the adapter can hide without inventing an encoding;
  `MemoryObjectStore` preserves case exactly, so this is a real (documented)
  divergence between the two adapters.
- **`list_objects` costs N keys × V versions, and the ceiling is on BOTH.**
  `ListObjectVersions` returns neither Lock retention nor legal-hold status nor
  content-type, so **every version of every key** costs one `HeadObject`
  (~2 ms measured). Cost is linear in *total versions*, not keys.

  - **Objects per prefix: order 10^3–10^4.** What `ne-audit`'s
    daily/periodic checkpoint cadence produces over a multi-year retention
    window. This is **not** a general-purpose bulk-listing path; past that
    ceiling, partition the prefix (e.g. a year or month segment in
    `key_prefix`) rather than paginating harder.
  - **Versions per key: capped at `max_versions_per_key` (default 1000).**
    This one is a **security** ceiling, not just a performance one.
    `s3:PutObject` is a permission the deployment MUST grant, and layering
    versions onto an existing key is **not** covered by the required
    deny-`s3:DeleteObject` bucket policy, which only covers deletes. So any
    holder of the checkpoint role — or anyone who obtains it — can cheaply
    layer 100k unlocked versions onto a single checkpoint key. Uncapped, that
    is ~200 s of `HeadObject` on that key alone, `list_objects` never returns,
    and `verify_checkpoints` stops completing: the tamper anchor is not
    falsified, it becomes **unverifiable**, and silently.

    A breach therefore raises `TooManyObjectVersionsError` and names the key.
    **Failing loud is the requirement.** Truncating the scan instead would be
    strictly worse: a truncated walk can miss the version that carries the
    lock and report an archived key as unprotected — a silent C10 downgrade
    traded for an availability fault. Treat the error as a signal to
    investigate *who is layering versions onto that key*; raise the setting
    only once the volume is known to be legitimate.

    **Tradeoff, stated plainly: ONE poisoned key halts ALL listing.**
    `list_objects` checks the cap per key but raises for the whole call, so a
    single key over the cap makes `list_objects()` raise and *no* key is
    returned — including legitimate, locked checkpoints that are nowhere near
    the cap. The vector is `s3:PutObject`, which the deployment must grant and
    which the deny-`s3:DeleteObject` policy does not cover, and the poisoned
    key **need not be a checkpoint key at all** — any key under the listed
    prefix will do. The cap also *lowered the cost of triggering this*: before
    it existed, ~100k versions bought a slowdown; now ~1001 cheap puts buy a
    hard stop.

    This is deliberate and is **not** going to be traded for a partial
    listing. A listing that silently omits a key is exactly the C10 hole this
    adapter exists to close — a caller cannot tell "key absent because
    deleted" from "key absent because the scan gave up". Failing closed keeps
    the failure visible and attributable, and the raised error names the
    offending key so the remediation is actionable: find that key, remove the
    layered versions (or raise the cap if the volume is legitimate), and
    listing recovers. Alert on `TooManyObjectVersionsError` from the
    verification path; treat it as a tamper signal, not a tuning nuisance.

  Checkpoint keys are written once and archived, so in normal operation V is 1
  and the cap is never approached. A key with hundreds of versions is itself
  the anomaly worth alerting on.

## Test server — MinIO (test/dev infrastructure only)

The adapter's conformance and WORM-live tests need a real Object
Lock-COMPLIANCE-capable S3 endpoint. **SeaweedFS is DISQUALIFIED for this
lane** — its own gateway (the one this repo's `langfuse` dev-compose profile
already runs) accepts a COMPLIANCE retention write but does not actually
block deletes (seaweedfs/seaweedfs#8350, #7194); using it here would make the
WORM proof vacuous. **MinIO** is the chosen test server: it enforces Object
Lock for real, verified in this package's own live tests via raw-client
bypass probes.

MinIO here is **CI/dev test infrastructure only** — AGPL-3.0, and its
upstream community repository is archived (2026-04-25). It is never embedded
in or shipped with the product; production deployments point
`NE_STORAGE_S3_*` at the customer's own S3 / S3-compatible WORM storage. See
`packages/ne-storage/NOTICE`.

Start it:

```sh
docker compose -f deploy/compose/dev/compose.yaml --profile worm up -d minio
# or, equivalently, without compose:
docker run -d --name ne-worm-s3 -p 9000:9000 \
  -e MINIO_ROOT_USER=ne-test -e MINIO_ROOT_PASSWORD=ne-test-secret -e MINIO_UPDATE=off \
  -v ne-worm-data1:/data1 -v ne-worm-data2:/data2 -v ne-worm-data3:/data3 -v ne-worm-data4:/data4 \
  coollabsio/minio@sha256:69b55a1c1c5dc285ce04db96689f5b2102317fc77a50680a1874ca6efd1c87f9 \
  server /data1 /data2 /data3 /data4
```

The four separate drive paths are required — a single `/data` path (or bare
`docker run ... server /data` without per-path volumes) makes this MinIO
version fail to initialize with a spurious "drive not found" error, verified
empirically at build time; named/bind-mounted volumes per drive path fix it.

The test files default to `NE_TEST_S3_ENDPOINT=http://localhost:9000`,
`NE_TEST_S3_ACCESS_KEY=ne-test`, `NE_TEST_S3_SECRET_KEY=ne-test-secret` — no
env needed for the default local setup. They skip cleanly (not vacuously —
see `.github/workflows/ci.yml`'s must-run-not-skip gate) when the endpoint is
unreachable.

| test file                     | what it proves | needs the server? |
|--------------------------------|-----------------|--------------------|
| `test_s3_unit.py`               | entry-point resolution, `from_settings`, fail-loud/AIR_GAP/C2 boot checks, pure helpers | no |
| `test_s3_conformance.py`        | the shared `ObjectStoreContractKit` (put/get/stat/delete/list round-trip, WORM put/delete blocking, shorten/equal/extend semantics) | yes |
| `test_s3_worm_live.py`          | raw-client server-side denial (delete-version, shorten), real-time expiry, the retention-precision regression pin, a real signed audit checkpoint landing in WORM, the delete-marker and layered-version C10 pins, GOVERNANCE-is-not-WORM, legal holds, the version-scan cap, and the documented IAM list driven end-to-end under an STS-scoped credential | yes |

### Known MinIO-vs-AWS divergences (standing list)

**CI-container green does not imply production-correct.** MinIO is a real
Object Lock server and it is the right regression harness, but it is not AWS.
Three divergences surfaced during the S3 adapter's four adversarial review
rounds, and they are recorded here as a standing list rather than as folklore
in a closed PR:

1. **Absent-bucket error code.** MinIO's `HeadObject` returns a bare `404`
   code for a bucket that does not exist, where AWS returns `NoSuchBucket`.
   An adapter keying on HTTP status alone reads a typo'd bucket as an empty
   audit store — which is exactly the defect
   `test_missing_bucket_surfaces_nosuchbucket` exists to pin.
2. **Earlier COMPLIANCE date over a GOVERNANCE lock.** MinIO ACCEPTS writing
   an earlier COMPLIANCE retention over an existing GOVERNANCE lock (with
   bypass); AWS refuses it. A shortening path can therefore look permitted
   locally and be rejected in production.
3. **Legal-hold header visibility.** AWS returns the
   `ObjectLockLegalHoldStatus` `HeadObject` header ONLY to a caller holding
   `s3:GetObjectLegalHold`, and SILENTLY OMITS it otherwise; MinIO returns it
   regardless of permissions. This is the divergence that made a round-4
   legal-hold fix **inert on real AWS while the entire MinIO suite stayed
   green** — the failure mode that motivated the live lane.

The honest scope of the CI MinIO lane is **adapter-logic regression cover**.
Only the owner-gated `worm-live-proof` lane produces evidence about
production storage. If that lane surfaces a NEW divergence — a test that
fails on real AWS but passes on MinIO — it is reported as a finding and
escalated, and this list grows. It is never papered over by weakening the
test; producing exactly that signal is the reason the lane exists.

### Live lane (`worm-live-proof`)

The `worm-live-proof` lane is owner-dispatched and LIVE-PARKED: with no secrets configured it skips both legs and the run is
green-by-skip. Each leg is independently gated, so an owner with one cloud
still gets that cloud's evidence.

| leg | required secrets | what it runs |
|---|---|---|
| AWS S3 Object Lock | `AWS_WORM_LIVE_ROLE_ARN` (OIDC role; no static keys) | `test_s3_conformance.py` + `test_s3_worm_live.py`, the exact `ci.yml` invocation, floor 37 passed |
| Azure Blob VLW | `AZURE_WORM_LIVE_ACCOUNT_URL`, `AZURE_WORM_LIVE_ACCOUNT_KEY`, `AZURE_WORM_LIVE_CONTAINER` | `test_azure_blob_worm_live.py`, floor 17 passed with at most one named skip |

The lane sets short retention knobs — `NE_TEST_S3_FAR_RETENTION_SECONDS`,
`NE_TEST_S3_NEAR_RETENTION_SECONDS`, `NE_TEST_S3_KIT_RETENTION_SECONDS`,
`NE_TEST_AZURE_BLOB_RETENTION_MINUTES` — each of which defaults to the exact
value the suite used before, so the MinIO CI lane is unchanged. They exist
because **a COMPLIANCE-locked object cannot be deleted until its retention
expires, so its bucket cannot be emptied or deleted either**. Note plainly
what follows: **same-run teardown cannot fully succeed on AWS**, because a
retention long enough to survive the suite is still unexpired minutes later
when teardown runs. Stranded buckets are the ROUTINE outcome of a healthy
run, not an incident, and they are what `aws-teardown-leftovers.txt` records.
The short retentions do not prevent that — they shrink the reap window from a
year to minutes. Leftovers are cleared by re-dispatching with
`cleanup_only=true` (~15 minutes later for the kit/archive locks, 24h+ for
the day-granular `write_checkpoint(retention_days=1)` locks), and on Azure by
the storage account's lifecycle-management policy.

## Dependency intake (verified 2026-07-18, build time)

- **aiobotocore** `>=3.8.0,<4` (pinned `3.8.0`) — Apache-2.0, `aio-libs` org,
  active maintenance cadence, no CISA-KEV entries, no telemetry. Genuinely
  async (not sync-boto3-in-a-threadpool), matching the contract's async
  Protocol. Transitively adds `botocore` (Apache-2.0), `jmespath` (MIT),
  `wrapt` (BSD-2-Clause), `aioitertools` (MIT); `aiohttp` was already locked
  and license-gated. `aioboto3` was evaluated and rejected (adds a `boto3`
  resource layer this adapter never uses); `minio-py` was evaluated and
  rejected (sync, and couples the adapter to an archived vendor for a
  *runtime* dependency — unlike the test-only MinIO *server*, which is fine).
- **MinIO** (test/dev infrastructure only, never a runtime dependency): see
  "Test server" above and `NOTICE`.

Full evidence (versions, CVE disclosures, PR body language) travels with the
unit's PR.

---

## Azure Blob adapter (`azure_blob`)

`ne.storage.azure_blob.AzureBlobObjectStore` — the **Azure** production WORM
backend, behind the same frozen `ObjectStore` Protocol. Speaks
`azure.storage.blob.aio` (genuinely async, matching the Protocol — the same
reason `aiobotocore` was chosen over `boto3` on the S3 lane). `archive()` is
backed by a real Azure **version-level immutability policy** in `Locked` mode.

## Contract → Azure mapping

| Protocol method  | Azure operations                                              | Notes |
|------------------|---------------------------------------------------------------|-------|
| `put`            | `List Blobs` (versions, WORM check) → `Put Blob`               | resolves the PROTECTED version, not the current one; no protected version proceeds straight to the upload, which creates a new CURRENT version |
| `get`            | `List Blobs` (versions) → `Get Blob` (`version_id=` when protected) | an archived key reads back the ARCHIVED bytes, never a layered current version |
| `stat`           | `List Blobs` (versions) → `Get Blob Properties` only if nothing is protected | `ResourceNotFoundError` → `ObjectNotFoundError` |
| `delete`         | `List Blobs` (versions, WORM check) → `Delete Blob`            | absent → returns silently (matches `MemoryObjectStore`); the delete is unscoped, i.e. it removes the CURRENT blob |
| `list_objects`   | `List Blobs` with `include=['versions','immutabilitypolicy','legalhold','metadata']` | **one paged call, no N+1.** Unlike S3 (whose `ListObjectVersions` carries no lock state, forcing a `HeadObject` per version), Azure returns the immutability policy, legal hold, size, content-type and metadata inline on every listed version. A key stays LISTED whenever ANY of its versions is protected — including after its current version is deleted. |
| `archive`        | `Get Container Properties` (VLW preflight) → `List Blobs` (versions) → (idempotent short-circuit \| `WormViolationError`) → `Set Blob Immutability Policy` (`policy_mode=Locked`, `version_id=` the protected/current version) | See "WORM honesty" below |

`retain_until` maps directly to the policy's **absolute** `expiry_time`. The
SDK accepts an absolute instant, so there is no duration-since-creation math
to get wrong — but the RFC 1123 wire format carries whole seconds only, so a
sub-second `retain_until` is **ceiled** to the next whole second before it is
sent (shared verbatim with the S3 lane's `_normalize_retain_until`; a unit
test asserts the two adapters use the *same function object* so they cannot
drift). Ceiling can only ever make effective retention equal-or-later than
requested — never a silent shortening.

Physical keys are `key_prefix + logical_key`, stripped again on every read
path, exactly as on the S3 lane.

## WORM honesty — Azure's truth, which is not S3's truth

1. **SERVER-enforced.** Once a version-level immutability policy with
   `policy_mode=Locked` is set, the Azure **service** refuses to delete that
   version and refuses to shorten its `expiry_time` — only extension is
   accepted — for any caller, this adapter or a raw client.

2. **Only `Locked` counts — `Unlocked` is treated as no policy at all.** An
   Unlocked immutability policy can be deleted or shortened at will, so it
   guarantees nothing. `stat()` does not report it as `worm_retain_until`, and
   `put`/`delete` are not blocked by it. This matters in practice: a
   container-level default VLW policy (which `deploy/terraform-azure` sets)
   stamps new blobs **Unlocked**, so this is the state blobs actually arrive
   in. `archive()` therefore does not short-circuit on an unlocked policy — it
   proceeds and genuinely Locks the blob, at the **later** of the requested
   expiry and any retention already in force, so locking can never shorten a
   retention the blob effectively had.

3. **PER-VERSION, not per-blob — every decision is keyed on the PROTECTED
   version.** Azure immutability is version-level: the policy and the legal
   hold live on a specific blob VERSION, never on the key. Every path
   therefore resolves `_protected_version()` — the version carrying an active
   `Locked` policy or a legal hold — rather than reading whatever version is
   current.

   This closed a real defect. Previously the adapter called
   `get_blob_properties()` with no `version_id` and listed without
   `include=['versions']`, so once any unlocked version was layered over a
   Locked one (out of band via a raw client, or by any writer that is not this
   adapter) the adapter saw no policy: `put`/`delete` were permitted from then
   on **for all callers**, `stat()` reported no retention for a key that
   genuinely had one, `get()` returned the layered payload instead of the
   archived bytes, and deleting the current version hid an archived checkpoint
   from `list_objects()` entirely. The archived bytes stayed server-protected
   throughout — Azure never stopped enforcing the Locked policy — but the
   adapter's key-level WORM blocking was permanently off. This is the exact
   Azure analogue of the S3 lane's `_locked_version` fix.

   Among protected versions, a dated `Locked` policy beats a hold-only lock,
   and the latest active expiry wins, so an extension always beats a stale
   retention.

4. **The version walk is BOUNDED and fails loud.** `max_versions_per_key`
   (`NE_STORAGE_AZURE_BLOB_MAX_VERSIONS_PER_KEY`, default 1000) caps how many
   versions of one key the adapter will walk; breaching it raises
   `TooManyBlobVersionsError`. The cost story is lighter than S3's — Azure
   returns the policy inline, so there is no per-version round trip — but the
   listing is still unbounded and any writer holding blob-write permission can
   layer versions onto one checkpoint key, which would stall `list_objects()`
   and stop `verify_checkpoints` completing. It **raises rather than
   truncates**: a truncated walk could skip the Locked version and report an
   archived key as unprotected, which is a silent WORM downgrade rather than a
   loud availability fault.

5. **CLIENT-side courtesy, with a TOCTOU window.** The contract's KEY-level
   `put`/`delete` blocking is implemented in this adapter: a versions read,
   then act. There is a window between the two. This does **not** weaken the
   WORM guarantee — with versioning enabled an overwrite creates a NEW current
   version while the Locked version stays immutable, so the archived bytes are
   untouched. The window can only affect whether a racing caller *sees* the
   courtesy error.

6. **Legal holds are RESPECTED but never SET.** The frozen Protocol has no
   method for setting one; that is an operator action via Azure tooling.
   `put`/`delete` against a blob carrying `has_legal_hold` raise
   `WormViolationError`. A hold is **indefinite and has no expiry**, so
   `stat()` reports `worm_retain_until=None` for a held blob that carries no
   time-based policy — but that `None` means "no retention *date*", never
   "unprotected": the key stays LISTED by `list_objects()` and readable by
   `get()`, and both mutating paths stay refused. Unlike `policy_mode` (a
   bare `Optional[str]` with no case normalisation, hence the case-insensitive
   `Locked` check), `has_legal_hold` is deserialized by the SDK as a genuine
   `Optional[bool]` from `x-ms-legal-hold`, so a plain truthiness test is
   correct and no casing trap exists. Server-side enforcement is parked in
   `tests/test_azure_blob_worm_live.py` — Azurite implements legal holds no
   more than it implements immutability policies.

## The Azurite limitation, stated bluntly

**Azurite — the Azure Storage emulator — does not implement immutability
policies.** Unlike the S3 lane, where a local MinIO gives a real
COMPLIANCE-mode server, there is **no local emulator that can prove the Azure
WORM guarantee**. So this repo deliberately does not have one:

- `tests/test_azure_blob_unit.py` proves the **adapter's state machine**
  against a fake blob client: extend-only, equal-is-idempotent-with-zero-
  server-calls, earlier-raises, `Locked` mode, ceiled expiry, error
  translation, C2/AIR_GAP boot paths. It proves nothing about whether the
  *service* enforces anything, and says so.
- `tests/test_azure_blob_worm_live.py` is **LIVE-PARKED**: it proves the
  server-side guarantee with raw-client bypass probes against a REAL storage
  account, and skips cleanly otherwise. It is the owner-gated **NE-WORM-LIVE**
  lane's target.

A test that appeared to prove WORM against Azurite would be vacuous — worse
than no test. There is not one.

| test file                          | what it proves | needs a real Azure account? |
|------------------------------------|----------------|------------------------------|
| `test_azure_blob_unit.py`          | entry-point resolution, `from_settings`, fail-loud/AIR_GAP/C2 boot checks, retention state machine + error translation against a fake | no |
| `test_azure_blob_worm_live.py`     | `ObjectStoreContractKit` conformance, raw-client delete/shorten denial (SERVER-enforced), RFC1123 precision round-trip, that immutability really is PER-VERSION and survives a layered out-of-band overwrite, that a real VLW container reports `immutable_storage_with_versioning_enabled` (so `archive()`'s preflight is not a false alarm), a real signed audit checkpoint landing in WORM | yes (owner-gated lane) |

## Provisioning prerequisites — read before deploying

The target container must have **blob versioning AND version-level
immutability (VLW) enabled**. Both are provisioning-time properties of the
account/container and are **not settable from the data plane** — this
adapter cannot turn them on.
`deploy/terraform-azure/modules/storage/main.tf` provisions a dedicated VLW
WORM container (`azapi_resource.worm`) for this: `hashicorp/azurerm` exposes
**no argument** to enable VLW at any level (upstream
`hashicorp/terraform-provider-azurerm#21512` / `#31438`, both open), so the
container is created via the `azapi` provider instead. The account's
`versioning_enabled = true` plus the **optional, time-based**
`azurerm_storage_container_immutability_policy` on the separate `velero`
container do **not** satisfy this preflight — that is a different Azure
feature. Asserted plan-time by
`deploy/terraform-azure/tests/storage_worm.tftest.hcl`.

It does, however, **check**: `archive()` preflights `Get Container Properties`
and raises `VersionLevelImmutabilityNotEnabledError` when
`immutable_storage_with_versioning_enabled` is not true, instead of letting a
raw `HttpResponseError` surface from `Set Blob Immutability Policy`. This
mirrors the S3 lane's `_ensure_object_lock_enabled` (itself a review finding):
without it, `put()` succeeds and `archive()` always fails, so checkpoint
objects land **unlocked** while the operator sees only an untyped service
error. The check is cached per adapter instance.

VLW implies versioning, so the one check covers both preconditions. If
versioning is off, no versions exist and every key has a single
`version_id=None` entry — the read paths degrade gracefully rather than
crashing, and only `archive()` fails.

Once a policy is `Locked`, its expiry can only be **extended**. Choose the
retention period deliberately (`ne-audit`'s `CheckpointConfig` default is 365
days).

## Configuration — `AzureBlobObjectStoreSettings`

Layered per `NeSettings` (init kwargs > env > `ne.toml` > defaults), env
prefix `NE_STORAGE_AZURE_BLOB_`:

| field               | env var                                    | default | notes |
|---------------------|--------------------------------------------|---------|-------|
| `account_url`       | `NE_STORAGE_AZURE_BLOB_ACCOUNT_URL`        | `""`    | **required** — Azure has no account-less default endpoint; empty fails loud at construction |
| `container`         | `NE_STORAGE_AZURE_BLOB_CONTAINER`          | `""`    | **required** — the bucket analogue; must have versioning + VLW enabled |
| `account_key`       | `NE_STORAGE_AZURE_BLOB_ACCOUNT_KEY`        | `""`    | **`SecretStr`** — redacted in `repr()`/`str()` so it cannot surface in a traceback or log, matching S3's `secret_access_key`. Empty ⇒ `azure.identity.aio.DefaultAzureCredential` (AKS workload identity / managed identity / env), the recommended production posture, mirroring S3's empty-credentials ⇒ IAM chain. NB `SecretStr` defines no `__bool__`, so consumers must call `.get_secret_value()` **before** testing truthiness or `SecretStr("")` reads as set and the fall-through silently breaks. |
| `key_prefix`        | `NE_STORAGE_AZURE_BLOB_KEY_PREFIX`         | `""`    | physical key = `key_prefix + logical key` |
| `max_versions_per_key` | `NE_STORAGE_AZURE_BLOB_MAX_VERSIONS_PER_KEY` | `1000` | cap on the per-key version walk; breach raises `TooManyBlobVersionsError` (fails loud, never truncates) |
| `allowed_hosts`     | `NE_STORAGE_AZURE_BLOB_ALLOWED_HOSTS`      | `""`    | comma-separated operator allowlist, see C2 below |
| `connect_timeout_s` | `NE_STORAGE_AZURE_BLOB_CONNECT_TIMEOUT_S`  | `5.0`   | |
| `read_timeout_s`    | `NE_STORAGE_AZURE_BLOB_READ_TIMEOUT_S`     | `60.0`  | |

A full Azure deployment env block:

```
NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE=azure_blob
NE_STORAGE_AZURE_BLOB_ACCOUNT_URL=https://<account>.blob.core.windows.net
NE_STORAGE_AZURE_BLOB_CONTAINER=<worm-container>
# account_key: leave EMPTY in production (workload/managed identity); set only
# for dev or a shared-key account.
```

### C2 / AIR_GAP behavior (construction-time, fail-closed, no I/O)

- A **public Azure FQDN** (`*.blob.core.windows.net`, plus the sovereign
  `*.blob.core.usgovcloudapi.net` / `*.blob.core.chinacloudapi.cn` suffixes)
  is ordinary governed egress — the deployment's network firewall holds the
  FQDN allowlist. Permitted while `AIR_GAP` is off, the same posture as an
  empty `endpoint_url` on the S3 lane.
- Under `AIR_GAP`, that same public FQDN **fails closed** with
  `AirGapViolationError` **unless** it is listed in `allowed_hosts`. That
  allowlist entry is the operator's explicit assertion that an **Azure Private
  Endpoint** resolves the FQDN privately in-VNet — Private Endpoints keep the
  public FQDN, so the URL alone cannot distinguish the two cases and only the
  operator can.
- Any **non-public-suffix** host (custom domain, Azurite) goes through
  `ne.net.ssrf.validate_in_vpc_endpoint(account_url, allowed_hosts=...)`
  exactly as the S3 lane's `endpoint_url` does.

### `from_settings` — the worker-builder seam

`AzureBlobObjectStore.from_settings(anything)` **accepts and ignores** its
positional argument and constructs purely from `NE_STORAGE_AZURE_BLOB_*` env,
for the identical reason `S3ObjectStore.from_settings` does (see that section
above): the checkpoint-loop builder passes `platform.audit_settings`, which
has no notion of an Azure account URL or credential.

## Dependency intake (verified 2026-07-19, build time)

- **azure-storage-blob\[aio\]** `>=12.30.0,<13` (locked `12.30.0`) — **MIT**,
  Microsoft's official SDK. The `aio` extra is what makes it genuinely async
  (`azure.storage.blob.aio`), matching the Protocol.
  `set_immutability_policy` / `delete_immutability_policy` / `set_legal_hold`
  were confirmed present on the installed async `BlobClient` by
  introspection, as was `BlobImmutabilityPolicyMode.LOCKED` and
  `ImmutabilityPolicy(expiry_time=..., policy_mode=...)` taking an
  **absolute** instant. Transitively adds `azure-core` 1.41.0 (MIT) and
  `isodate` 0.7.2 (BSD-3-Clause); `cryptography`, `typing-extensions` and
  `aiohttp` were already locked and license-gated.
- **azure-identity** `>=1.25.3,<2` (locked `1.25.3`) — **MIT**. Provides the
  async `DefaultAzureCredential` so an empty `account_key` falls through to
  workload/managed identity, the exact mirror of the S3 lane's
  empty-credentials ⇒ IAM-chain production posture. Without it, Azure parity
  would be shared-key-only. Transitively adds `msal` 1.37.0 (MIT),
  `msal-extensions` 1.3.1 (MIT) and `portalocker` 3.2.0 (BSD-3-Clause).

All permissive → C4 clean; `uv run ne check licenses` passes across the whole
204-package closure with no policy override needed.
