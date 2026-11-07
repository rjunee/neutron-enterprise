# 0003 — Composed platform config_hash

## Status

Accepted (2026-07-05; audit #7 finding P1, orchestrator owner-delegated
decision; committed by NE-CKC5).

## Context

SPEC §4 defines `config_hash` as a SHA-256 over the fully-resolved, canonicalized
configuration tree, computed at process start and stamped on every event and eval
run — it is what makes "why did the system say X on March 3" answerable, and what
a replay/audit reconstruction pins the configuration to.

`compute_config_hash(settings)` / `freeze_config_hash(settings)` hash a **single**
settings model. But the platform has no aggregate settings model: there are nine
`NeSettings` subclasses (brain, govern, gateway, and the rest), one per module.
When the composition root (NE-033) freezes the process hash from a single module's
settings, three things go wrong (audit #7, P1):

1. **Subset stamp.** The frozen hash covers only that one module's config and
   *omits the others* — notably policy/guardrail (`govern`) config. A replay that
   trusts the hash cannot tell that a governance setting changed, so the audit
   hash is silently incomplete.
2. **Second-freeze raise.** If a second module tried to freeze its own settings,
   `freeze_config_hash` would raise `ConfigHashMismatchError` — the single global
   slot cannot represent more than one module.
3. **Cross-process divergence.** Different processes (`ne-api`/`ne-worker`/
   `ne-ingest`) freezing different modules' settings would compute *different*
   hashes for the *same* deployed configuration, so the hash is not a stable
   deployment identity.

The fix must not break the Checkpoint-A freeze of `ne-contracts` v1.0.0: the
existing single-module API and every contract model stay byte-for-byte
compatible.

## Decision

Add a **composed** hash alongside the existing single-module API — purely
additive:

- `compute_platform_config_hash(modules: Mapping[str, BaseModel]) -> str` —
  canonically combine multiple module settings into ONE SHA-256. For each
  `(module_key, settings)`, reuse the **identical** per-module canonicalization
  `compute_config_hash` uses (`settings.model_dump(mode="json")`, which renders
  `SecretStr`/`SecretBytes` as `"**********"` so secret material never enters the
  preimage and rotation does not change the hash). Combine into
  `{module_key: <canonical tree>}` and serialize with
  `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
  `sort_keys` is recursive, so the digest is independent of BOTH the input
  mapping's insertion order AND every nested key order. An empty mapping raises
  `ValueError` (a hash over zero configs would stamp an empty configuration).
- `freeze_platform_config_hash(modules) -> str` — freeze the composed hash into
  the **same** process-global `_frozen_hash`/`_frozen_lock` slot with the
  identical idempotent-on-identical / raise-on-divergent semantics.
  `get_config_hash()` returns it unchanged.

Modules are **passed in** as `Mapping[str, BaseModel]`; `ne-contracts` never
imports any module's settings, so the contract layer stays decoupled from every
module (C5). The composed preimage `{module_key: tree}` is structurally distinct
from any single-module preimage (a bare `tree`), giving domain separation: the
platform hash never collides with a constituent `compute_config_hash`.

**Cross-process determinism** is guaranteed by construction: the hash is a pure
function of the module mapping, and the sorted-key canonicalization makes it
independent of insertion order, so every process freezing the same module mapping
computes the same hash.

### Alternatives rejected

- **A `PlatformSettings` aggregate model** that composes every module's settings.
  Rejected: it would couple `ne-contracts` to every module package (importing all
  nine settings classes), violating C5 — the exact decoupling the passed-in
  mapping preserves.
- **Extending `compute_config_hash`'s signature** to accept multiple settings.
  Rejected: it is not additive — it changes a frozen v1.0.0 signature and would
  break the Checkpoint-A freeze discipline.

## Consequences

- **Purely additive MINOR bump 1.0.0 → 1.1.0.** The existing single-module API
  (`compute_config_hash`, `freeze_config_hash`, `get_config_hash`,
  `reset_config_hash_for_testing`) keeps its exact signatures and observable
  behavior, and is retained for backward compatibility. No contract model gains
  or loses a field. This honors the Checkpoint-A freeze discipline (ADR-0001
  freeze scope, SPEC §6): additive-only + ADR + version bump.
- **One frozen hash per process.** `freeze_config_hash` and
  `freeze_platform_config_hash` intentionally share the single `_frozen_hash`
  slot, so a process must choose single-module or platform freeze consistently;
  mixing the two with different digests raises `ConfigHashMismatchError`.
- **NE-033 wiring is a follow-up.** Moving the composition root to
  `freeze_platform_config_hash` (so the process hash covers all module configs) is
  done by a separate rider unit, NOT this one — this unit ships only the
  `ne-contracts` capability, ADR, version bump, and conformance tests.

---

Note: this ADR lives in-package (`packages/ne-contracts/adr/`) because the unit's
diff surface is `packages/ne-contracts/**`. The orchestrator may index it from the
repo-wide `docs/adr/README.md` on merge (and reconcile the `0003` sequence number
if another in-flight unit claims it).
