# Releases

This document is the platform's SemVer policy and the running log of cut
releases.

## 1. Platform SemVer policy

The platform has a single version, `vX.Y.Z`, **decoupled from `ne-contracts`'
own internal version** (currently `1.2.0`). `ne-contracts` versions its
contract surface (additive-only, frozen, ADR-gated per breaking change); the
platform version names a *release of the whole system*.

A release **is** the tuple:

```
{ git tag vX.Y.Z, per-member CycloneDX SBOMs, composed platform config-hash }
```

* **git tag `vX.Y.Z`** — the platform version.
* **Per-member CycloneDX SBOMs** — one per workspace member, from
  `ne release sbom --all` (NE-CKB1). Bespoke, zero-dependency, stdlib-only
  generator; see `tools/ne-cli/README.md`.
* **Composed platform config-hash** — `ne.contracts.config.compute_platform_config_hash`
  (NE-CKC12) over the platform's DEFAULT composed configuration (the same
  module set `ne.runtime.factory.freeze_platform_config` assembles at boot:
  `{"ne-runtime": RuntimeSettings(), "ne-govern": GovernSettings()}`).

This tuple is named by a single, deterministic **release fingerprint** — a
SHA-256 digest over a canonical manifest of the tag, the checked-out tree hash,
the config-hash, and every member SBOM's digest. Same tree in, same
fingerprint out, every time. Compute it with:

```sh
ne release fingerprint --tag vX.Y.Z
```

or build the full, versioned, reproducible artifact bundle (manifest + every
member SBOM, packaged as a deterministic `.tar.gz`) with:

```sh
ne release bundle --tag vX.Y.Z --out dist/bundle
```

Both commands refuse a dirty worktree by default (fail-closed; pass
`--allow-dirty` to override for local experimentation) and refuse to run
without a resolvable tag (pass `--tag` explicitly, or tag `HEAD` first).

## 2. Bump rules

Verbatim from `dependency-and-release-cadence.md` §4:

* **PATCH** — dependency / security / supply-chain riders (the CKC-class:
  vulnerability bumps, re-locks, hash-pin refreshes).
* **MINOR** — new capabilities (a unit or phase landing: new commands, new
  governed actions, new eval surfaces, ...).
* **MAJOR** — a breaking `ne-contracts` change (rare, ADR-gated — the contract
  freeze already enforces additive-only, so a MAJOR is a deliberate,
  documented decision, never an accident).

**Cut a release at every Checkpoint (A/C/D/...) and at every deploy to a
pilot/customer environment.** A tagged release + SBOM set + config-hash is
also exactly what an enterprise customer's security review will ask for
(CRA/SBOM expectations).

## 3. How to cut a release

1. Tag the commit: `git tag -a vX.Y.Z -m "..."` (an owner/orchestrator action —
   no unit or tool in this repository creates or pushes a git tag).
2. Push the tag: `git push origin vX.Y.Z`.
3. `.github/workflows/release.yml` runs automatically on the tag push: it
   builds wheels/sdists, aggregates NOTICE, generates every member's CycloneDX
   SBOM, builds the deterministic versioned release bundle (`ne release
   bundle`), signs and attests everything with the provisioned cosign key
   (key-based, no keyless/Fulcio/Rekor), and uploads all of it to the GitHub
   release.
4. Append a row to the release table below (version, date, tag, tree hash,
   config-hash, fingerprint, notes) — currently a manual, orchestrator-owned
   step.

## 4. Reversible-migration policy and N -> N-1 upgrade support

Cross-module schema/data migrations run through the central migration runner,
`ne migrate` (`tools/ne-cli/src/ne/cli/migrate.py`; registry in
`tools/ne-cli/src/ne/cli/migrations/`). Full documentation, including the
fail-closed reversible-migration policy, lives in
[`tools/ne-cli/README.md`](tools/ne-cli/README.md#migration-runner-ne-migrate).
Summary:

* Every migration declares **either** a `down` step **or** a non-blank
  `irreversible_justification` — never both, never neither. An unmarked (or
  contradictorily marked) irreversible migration is refused, fail-closed,
  before it ever touches a database.
* Migrations are ordered (strictly increasing versions), idempotent (a
  `ne_migrations` tracking table + per-migration transaction make re-running
  `upgrade`/`downgrade` a no-op once the target is reached), and
  registry/database drift (an applied version the current registry doesn't
  know about) is refused fail-closed.
* **N -> N-1 upgrade support**: CI proves, on every run, that a store shaped
  like the previous release can upgrade to the current release with data
  intact, and that reversible migrations roll back cleanly — see the `upgrade`
  job in `.github/workflows/ci.yml` and
  `tools/ne-cli/tests/test_upgrade_n_minus_1.py`.

## 5. Release table

| version | date | tag | tree | config-hash | fingerprint | notes |
|---|---|---|---|---|---|---|
| — | — | — | — | — | — | (no releases cut yet — first v0.x tag is a maintainer action) |
