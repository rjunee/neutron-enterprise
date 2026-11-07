# 0006 — config_hash URL-userinfo masking

## Status

Accepted (2026-07-10; bulletproofing review P1-1; owner decision SPEC §11
2026-07-10; committed by NE-FIX-CONFIGHASH).

## Context

SPEC §4 defines `config_hash` as a SHA-256 over the fully-resolved, canonicalized
configuration tree, computed at process start and stamped on every event, U-P-B
report, and WORM audit export — it is what makes "why did the system say X on
March 3" answerable and what a replay/audit reconstruction pins the configuration
to.

`compute_config_hash` / `compute_platform_config_hash` masked only pydantic
`SecretStr`/`SecretBytes` fields (rendered `"**********"` by
`model_dump(mode="json")`). But **every module's DSN is a plain `str`**, not a
`SecretStr` — `ne-audit`, `ne-runtime`, `ne-permissions`, `ne-flywheel`,
`ne-api` (three DSN fields), `ne-obs` (three), `ne-worker`, `ne-brain-graph`
(two). The pilot injects DSNs carrying the embedded Postgres password
(`postgresql://ne:PASSWORD@postgres:5432/ne`). That password therefore entered
the SHA-256 preimage stamped on every event, printed in the U-P-B report, and
shipped in WORM audit exports (bulletproofing finding P1-1 / invariant C12):

1. **Unsalted offline dictionary oracle.** The rest of the preimage is
   reconstructible from compose/values, so an attacker holding any stamped
   `config_hash` can brute-force the DB password offline — the digest is an
   unsalted oracle for it.
2. **Rotation breaks the audit invariant.** Rotating the DB credential changed
   `config_hash`, so a pure credential rotation looked like a configuration
   change — defeating the SPEC §4 replay/audit invariant that the hash is a
   stable deployment-config identity.

The existing guard test only exercised a `SecretStr` field, so CI stayed green
while the plain-`str` DSN leak was live.

The frozen-contract rule (ADR-0001, SPEC §6) and the owner decision (SPEC §11,
2026-07-10) require the fix to be canonicalization-only: **no retype of the
frozen `str` DSN fields to `SecretStr`, no wire-schema change, no version bump.**

## Decision

Mask URL **userinfo** at canonicalization time, inside both compute functions,
generic over field names:

- Before hashing, walk the `model_dump(mode="json")` tree; for every whole-string
  value that parses as `scheme://user[:pass]@host...`, replace the userinfo with
  the fixed sentinel `***:***` (both username and password removed — usernames
  are also sensitive and rotation-invariance applies uniformly). Every other byte
  of the preimage (scheme case, host, port, path, query, fragment, and all
  non-URL strings) is preserved.
- The walk covers `str` values, `dict` values, `dict` **keys** (defense-in-depth
  for configs keyed by a DSN), and `list` elements — so it catches DSNs in any
  field name across all modules, nested or not.
- Field **types stay `str`**; the wire schema is unchanged; `ne-contracts` stays
  at **1.3.0** (no frozen-contract version bump, per the owner decision). Modules
  are still passed in — `ne-contracts` `src` imports no module settings, so C5
  src decoupling is untouched.

### Alternatives rejected

- **Retype every DSN field to `SecretStr`.** Rejected by the owner decision: it
  is a frozen-contract type change across seven packages, forces a version bump,
  and changes the wire representation of the field — far larger blast radius than
  a canonicalization-only mask, for the same leak closure.
- **Mask only known DSN field names.** Rejected: brittle and non-generic; a new
  DSN field in any module would silently re-open the leak. Masking any
  URL-shaped value covers every field name by construction.

## Consequences

- **One-time hash reshuffle for userinfo-bearing configs.** Any deployment whose
  injected DSN carries userinfo (the pilot compose does) gets a NEW `config_hash`
  ONCE on upgrade to this version. Replay tooling comparing pre/post-upgrade
  hashes will see exactly one discontinuity. This is intended and owner-approved:
  it is the act of closing the C12 leak.
- **Configs without URL userinfo are byte-identical.** A legacy-formula
  equivalence test proves that configs with no URL userinfo hash exactly as
  before, so the reshuffle is scoped to userinfo-bearing deployments only.
- **Release fingerprint unaffected.** The release fingerprint hashes DEFAULT
  settings, whose DSNs carry no userinfo (`brain-graph` default
  `postgresql://localhost:5432/postgres`, `permissions` `sqlite:///…`), so it is
  byte-identical after this change.
- **C12 closed, SPEC §4 restored.** Secret material — now including plain-`str`
  DSN userinfo, not just `SecretStr` — never enters the preimage; credential
  rotation (SecretStr AND DSN userinfo) no longer changes the hash, restoring the
  replay/audit invariant rather than weakening it. Enforced by a parametrized
  guard test over all eight DSN-carrying module settings classes.
- **Limitation.** Only whole-string URL values are masked; a URL embedded
  mid-prose is not — acceptable because config DSN fields are always whole-string
  values. Bias is mask-more-never-leak.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the unit's
diff surface is `packages/ne-contracts/**`. The orchestrator may index it from the
repo-wide `docs/adr/README.md` on merge.
