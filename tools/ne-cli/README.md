# ne-cli

The `ne` developer CLI for the Neutron Enterprise monorepo (dist `ne-cli`, import `ne.cli`).

## Commands

- `ne version` — print the CLI version.
- `ne check headers [FILES...]` — verify the Apache-2.0 source header on Python
  files. With no arguments it checks every git-tracked `.py` file under
  `packages/`, `tools/`, and `apps/`. This is the enforcement behind the
  `ne-check-headers` pre-commit hook. Exits non-zero if any file is missing the
  header.

### Supply-chain checks (NE-002)

- `ne check licenses [--policy PATH] [--quiet]` — the SPEC §2 **C4 license gate**.
  Classifies every `uv.lock` dependency (banned / out-of-process-only /
  case-by-case / separate-service / free) and **fails closed** on banned, GPL,
  LGPL, Elastic-2.0, and unknown licenses. Banned licenses are never overridable.
  The policy's `[known]` map maps human-verified package names to SPDX ids for
  dists with absent metadata (platform-gated) or non-SPDX license strings
  (e.g. "3-Clause BSD License") that would otherwise classify unknown — banned
  is never overridable through it. The gate also normalizes common free-text
  license names ("Apache License 2.0", "BSD License", "MIT License", …) to
  SPDX ids and detects well-known FULL license texts embedded in the `License`
  field (exact header match for MIT/Apache-2.0/BSD/MPL-2.0/ISC, failing closed
  to unknown otherwise) — banned signals still dominate every metadata source
  and are never overridable. Runs locally and as the `license-gate` CI job.
  Policy: `tools/supply-chain/license-policy.toml`.
- `ne check lock` — dependency **pin + hash policy**: every artifact must carry a
  structurally valid `sha256:<64-hex>` digest, and `uv lock --check` must
  confirm the lock is current.

### Release tooling (NE-002)

- `ne release notice [--out dist/NOTICE]` — aggregate every per-package `NOTICE`
  into a single release NOTICE. Refuses to write a committed repo-root NOTICE and
  fails if any workspace member lacks a `NOTICE`.
- `ne release provenance --subject FILE... --out PATH` — emit an in-toto/SLSA-v1
  provenance statement for built artifacts (signed later with `cosign
  attest-blob`). Requires the `GITHUB_*` build environment.
- `ne release sbom --package NAME|--all [--out dist/sbom] [--deterministic]` —
  bespoke stdlib CycloneDX **1.6** SBOM per workspace member, generated from
  `uv.lock` (components are scoped to that member's transitive **runtime**
  dependency closure walked from `uv.lock` — dev-dependencies excluded — and
  carry version, license, and sha256 hashes; zero third-party generator deps —
  the briefed `cyclonedx-bom` pulls an LGPL transitive that fails the C4 gate).
  By default each document gets a fresh `uuid4` `serialNumber` per run (what the
  per-PR `sbom` CI job uses). `--deterministic` instead derives a stable `uuid5`
  serial from the document content; this is what `.github/workflows/release.yml`
  uses so the uploaded standalone SBOMs are byte-identical to the copies inside
  the release bundle and their digests match the manifest-recorded sha256
  (NE-069R). Runs in the `sbom` CI job and is signed at release.

### Release/upgrade machinery (NE-069)

See [`RELEASES.md`](../../RELEASES.md) at the repository root for the platform
SemVer policy (a release = `{git tag, per-member SBOMs, composed
config-hash}`), the PATCH/MINOR/MAJOR bump rules, and how to cut a release.

- `ne release fingerprint [--tag vX.Y.Z] [--allow-dirty]` — print the
  deterministic release manifest + fingerprint for the current tree. `--tag`
  defaults to `git describe --tags --exact-match HEAD` (fails closed if `HEAD`
  isn't tagged and no `--tag` is given). Refuses a dirty worktree by default
  (`--allow-dirty` to override). **Determinism contract:** the manifest is
  `{schema, platform_version, tree (git rev-parse HEAD^{tree}), config_hash,
  members: [{name, version, sbom, sha256}, ...]}`; the fingerprint is
  `sha256:<hex>` over the manifest's canonical JSON (sorted keys, tight
  separators, ASCII). Two runs over an unchanged tree with the same `--tag`
  produce byte-identical output. Reuses:
  - `ne.cli.sbom.build_sbom(..., deterministic_serial=True)` (NE-CKB1) for
    every member SBOM — the opt-in `deterministic_serial` keyword replaces the
    normal fresh-`uuid4` `serialNumber` with a stable `uuid5` derived from the
    document's own canonical content, so re-running the fingerprint is
    byte-reproducible. The default `ne release sbom` command is unaffected —
    it still emits a fresh `uuid4` every run.
  - `ne.contracts.config.compute_platform_config_hash` (NE-CKC12), composed
    over the platform's DEFAULT module set — exactly what
    `ne.runtime.factory.freeze_platform_config` assembles at boot
    (`{"ne-runtime": RuntimeSettings(), "ne-govern": GovernSettings()}`). This
    is deliberately the tree's *default* configuration, not any one
    deployment's environment-specific frozen hash: computing it scrubs every
    `NE_`-prefixed environment variable and runs in a fresh temp `cwd` so
    neither a deployment's env nor a stray `ne.toml` leaks into the release
    identity.
- `ne release bundle [--tag vX.Y.Z] [--allow-dirty] [--out dist/bundle]` —
  build the deterministic, versioned release bundle: a
  `neutron-platform-<tag>.tar.gz` containing `manifest.json` + every member's
  deterministic-serial CycloneDX SBOM, plus a sibling
  `neutron-platform-<tag>.manifest.json` (the manifest with the fingerprint
  appended). **Same tree → same bundle bytes**, including the `tar.gz`'s own
  sha256 — every `TarInfo` is normalized (`mtime=0`, `uid=0`, `gid=0`, no
  uname/gname, fixed mode), entries are added in sorted path order, and the
  gzip wrapper is built with an explicit `mtime=0` (plain `tarfile.open(...,
  mode="w:gz")` would embed a gzip timestamp and silently break
  reproducibility). Signed and uploaded as part of the GitHub release in
  `.github/workflows/release.yml`.

### Migration runner (`ne migrate`)

`ne migrate` applies ordered, versioned, idempotent cross-module schema/data
migrations against any SQLAlchemy-supported database (fixtures/tests use
sqlite; the SQL is ANSI-portable so Postgres rides the same runner in
production). The central registry lives in
`tools/ne-cli/src/ne/cli/migrations/` — NOT inside any `packages/ne-*` source
tree, since release/upgrade tooling is a platform-level concern.

- `ne migrate status --db-url URL` (or `$NE_MIGRATE_DB_URL`) — print applied vs.
  pending migrations.
- `ne migrate up --db-url URL [--to VERSION]` — apply pending migrations, in
  order, up to `--to` (default: latest). Re-running `up` once the target is
  reached is a no-op.
- `ne migrate down --db-url URL --to VERSION` — revert applied migrations, in
  reverse order, down to (excluding) `VERSION`.

**Reversible-migration policy (fail-closed).** Every migration declares
*either* a `down` step *or* a non-blank `irreversible_justification` — never
both, never neither. An unmarked irreversible migration (no `down`, no
justification), a blank justification, or a contradictory migration (both
`down` *and* a justification) all make **every** `ne migrate` command refuse,
naming the offending migration, **before it ever creates a database engine or
touches the database**. `ne migrate down` additionally refuses, up front (with
nothing partially reverted), the moment any migration in the requested revert
range lacks a `down` step — it names the migration and its
`irreversible_justification`.

**Ordering, idempotency, and version tracking.** Migration versions must be
strictly increasing positive integers. A `ne_migrations` tracking table
(`version`, `name`, `applied_at`) records every applied migration; a
migration's schema/data change and its tracking row are written inside ONE
transaction, so a failing migration rolls back both atomically (nothing is
left partially applied). If the tracking table ever records a version the
current registry doesn't recognize, every command refuses, fail-closed
(registry/database drift).

`v1` ships the runner framework + policy + a single trivial, reversible
baseline migration (`0001-platform-baseline`) that proves the machinery works
end-to-end — not a backlog of real migrations. CI proves an N-1-shaped store
upgrades cleanly to N (and rolls back cleanly) in the `upgrade` job — see
`tools/ne-cli/tests/test_upgrade_n_minus_1.py`.

### Pilot installer + config bundle (NE-070)

`ne up` / `ne down` drive the hardened single-VM pilot tier
(`deploy/compose/pilot/`, composed via `include` of `serve/` + `core`/`brain`/
`obs` profiles) the same way `ne dev` drives the dev stack. `ne pilot`
promotes/verifies the per-customer config bundle `ne up` installs from.

- `ne pilot promote --customer NAME --hostname HOST --out DIR [--runtime-config
  PATH] [--govern-config PATH] [--secrets-config PATH]` — stamp+seal a
  reproducible bundle: per-module TOML configs, a `secrets/required.json` of
  env-var → SecretStore `{path, field}` (NAMES only), and a `manifest.json`
  stamped with the **composed NE-CKC12 `config_hash`** (the SAME hash on every
  `AuditEvent`, from `ne.runtime.factory.freeze_platform_config`). Same inputs →
  byte-identical `manifest.json`. Dev→pilot promotion = point `--runtime-config`
  / `--govern-config` at a dev deployment's per-module TOMLs.
- `ne pilot verify BUNDLE_DIR` — recompute every file sha256 + the `config_hash`,
  then a `manifest_hash` integrity check covering the manifest's own
  `customer`/`hostname`/`config_hash`/`files` records (so an edited hostname —
  which `ne up` trusts for `KC_HOSTNAME` + the Caddy TLS vhost — can no longer
  pass verify). Exits nonzero naming the exact tampered entry. Fail-closed: a
  manifest with **no** `manifest_hash` (a pre-hardening bundle, or one with the
  field stripped) is REJECTED — re-run `ne pilot promote`.
- `ne up --profile {pilot|core|brain|agents|obs} --bundle DIR [--serve-gpu |
  --serve-cpu]` — preflight (docker/daemon/compose≥2.20/ports/disk/bundle, GPU
  only with `--serve-gpu`), resolve the bundle's secrets from its SecretStore
  into an ephemeral `0600` `runtime/pilot.env` (values never logged), then
  `docker compose … up --detach --wait`. The 443/8443 port preflight runs **only
  when the expanded profile set includes `core`** (only `core` publishes those
  off-box ports), and the `>= 10 GiB` disk check stats the **docker data-root
  filesystem** (`docker info` DockerRootDir; fallback `/`) so a relocated
  data-root (e.g. `/mnt`) is measured, not `/`. `pilot` = `core`+`brain`+`obs`;
  `agents` is reserved (warns, starts nothing until NE-067 app images). The SPEC
  `serve-gpu` surface maps to the included serve tier's compose `gpu` profile
  (`--serve-cpu` → `cpu`); no serve container starts without one of those flags.
- `ne down --bundle DIR [--volumes]` — tear down ALL profiles (idempotent). It
  generates a placeholder-value interpolation env (compose's `${NE_PILOT_*:?}`
  need only be defined for teardown) and **never reads the SecretStore**, so a
  stop still works when secrets are rotated/unavailable.

### Production Kubernetes installer (NE-071a)

The Helm umbrella chart at `deploy/helm/neutron-enterprise/` reproduces the
pilot Compose core profile (`ne-api`, `postgres`, `keycloak`, `opa`) as K8s
manifests — see `deploy/helm/README.md` for the full runbook, hardening
parity, and the 071b/c pending list.

- `ne release push --image ne-app:0.1.0 --registry REGISTRY --out PATH` — the
  **C11 first-party image pin** for K8s: tags + pushes a locally-built
  `ne-app:X.Y.Z` image to `REGISTRY/ne-app:TAG`, captures the **registry
  digest** (via `docker image inspect --format '{{index .RepoDigests 0}}'`,
  falling back to parsing the push output), and writes a Helm values overlay
  `{global:{image:{repository,tag,digest}}}` to `PATH`. Fails closed (nonzero,
  no file written) if no digest is captured, or if `--image` isn't
  `ne-app:X.Y.Z` (this path is narrowly first-party-only — no third-party
  image may use it). Never prints secrets.
- `ne k8s render --bundle DIR --out PATH` — turn a `ne pilot verify`-clean
  bundle into the Helm values overlay that stamps `global.configHash` /
  `global.hostname` from the manifest and carries the promoted bundle's three
  TOMLs into `bundle.files` (rendered into the `ne-platform-config` ConfigMap
  for identity/rollout — see `deploy/helm/README.md`, "NE_CONFIG_FILE
  deviation"). Written as JSON (a YAML subset; `helm -f` accepts it directly —
  no new PyYAML runtime dependency). Fails closed on a tampered/unverifiable
  bundle.
- `ne k8s up --bundle DIR [--chart PATH] [--namespace NS] [--release NAME]
  [-f VALUES]... [--wait/--no-wait] [--timeout DURATION]` — the K8s installer:
  verify the bundle, render the overlay, resolve `secrets/required.json`
  through the bundle's SecretStore (same code path as `ne up`, via
  `ne.cli.pilotcfg.resolve_required_secrets`) — **aborting before any
  cluster mutation** if resolution fails — then ensure the namespace, apply
  the `ne-platform-secrets` Secret via `kubectl apply -f -` with the resolved
  values on **STDIN only** (never argv, never disk), and finally `helm
  upgrade --install` with the rendered overlay first and any caller `-f`
  values applied afterward (last wins). Helm itself never renders a Secret —
  `helm get values`/release history stays clean (C12).

### Connector sync (`ne connect sync`, NE-INGEST — P1-4)

`ne connect sync [<source>]` runs a governed connector sync ONCE — the first-seed /
manual re-sync / NE-077 'Acme corpus ingested' invoker. It composes the SAME governed
platform recipe as `ne-api`/`ne-worker` and invokes the existing per-connector syncs
over the governed `KnowledgeWriter` (C7). Use it to seed a corpus on demand; the
seeded `ne-worker` cron (`NE_CONNECT_SYNC_*`) handles ongoing freshness.

- **No arg** → runs every source in `NE_CONNECT_SYNC_SOURCES`. Empty ⇒ a clean exit-0
  no-op (`no connector sources configured …`) that NEVER composes the platform.
- **`<source>`** → runs one named source (must be a known connector; it need NOT be
  listed in `NE_CONNECT_SYNC_SOURCES` — a named run is explicit operator intent).
- **Env**: `NE_CONNECT_SYNC_STATE_PATH` + `NE_CONNECT_SYNC_QUARANTINE_PATH` are
  required (the governed write state + quarantine lanes) plus the connector's own
  `NE_*` credentials (the `fixture` source reads `NE_CONNECT_FIXTURE_PATH`/`OWNER`).
- **Exit codes**: 0 on success (incl. the no-op); 1 if any source is unconfigured
  (`not configured`), unknown (lists the valid sources), or errors. Error lines name
  classes only — never payloads or credential-bearing URLs (C12).
- Every import in `ne.cli.connect` is lazy (the `ne release` precedent), so
  `ne --help` startup stays fast even though the command's closure is heavy.
- **Reachability**: works on the dev stack and any deployment with network reach to
  Postgres/Qdrant/TEI. On the hardened pilot the in-network services are not published
  to the host — run it in-network via a compose override or just let the cron fire
  (`docs/DEPLOYMENT.md` §2 step 6; follow-up **NE-INGEST-IMAGE-CLI**).

### Principal sync (`ne permissions sync`, NE-FIX-SYNC-INVOKERS — P2-20)

`ne permissions sync` runs a governed principal-directory sync ONCE via
`PrincipalSyncService.run_once` — the first-seed / manual re-sync invoker for the
seeded `permissions-principal-sync` cron. It composes the SAME governed platform
recipe and prints the sync counts.

- **Pull modes only** (`NE_PERMISSIONS_MODE=entra`/`fixture`) do work. The default
  `oidc-claims` (JIT, in-process) and SCIM (push) are a clean exit-0 no-op
  (`… nothing to pull`) that NEVER composes the platform.
- **Exit codes**: 0 on success (incl. the non-pull no-op); 1 on a sync failure, naming
  only the exception CLASS — never payloads or credential-bearing detail (C12).
- Every import in `ne.cli.permissions` is lazy, so `ne --help` stays fast.
- The interval `NE_PERMISSIONS_SYNC_INTERVAL_SECONDS` (default 900s) IS the max
  revocation lag once the cron is seeded — see `docs/DEPLOYMENT.md` §2.
### Ontology inspection (`ne ontology`, NE-ONTOLOGY-INSPECT)

`ne ontology` is the READ-ONLY operator/steward surface over the live T3 ontology
store + verified-answer layer (previously inspectable only programmatically). It
reuses the EXISTING gateway steward read path (`ValidatingTripleStore.sparql` —
SELECT/ASK-only with the C12 banned-keyword gate), the `load_template_registry`
loader, and the offline conformance gate. It adds **no** write/mutation path, **no**
raw-backend entry point, and **no** gate bypass — curation is the separate
**NE-ONTOLOGY-FLYWHEEL** unit. Quarantine access is `.list()` only.

- `ne ontology graphs` — list the loaded named graphs with per-graph triple counts
  (via a governed C12 `SELECT ... GRAPH ?g ... COUNT`) plus the pinned MANIFEST
  ontology versions (BFO/CCO/alignment `sources` + `shapes_hash`) from
  `gateway.ontology_version()`.
- `ne ontology verified [--templates-dir DIR]` — list the registered verified-answer
  templates (`id@version`, tool name, description, typed arg JSON-schema, citation
  projection, columns, graph classes). With verified answers disabled and no
  `--templates-dir`, prints a clean exit-0 `disabled` report; enabled without a
  resolvable directory fails loud naming `NE_ONTOLOGY_VERIFIED_TEMPLATES_DIR`; a malformed
  bundle fails loud.
- `ne ontology sparql "<SELECT|ASK>"` — run a governed read through the existing
  gateway steward surface. SELECT/ASK only; `INSERT`/`DELETE`/`DROP`/`CLEAR`/
  `CREATE`/`MOVE`/`COPY`/`ADD`/`LOAD`/`SERVICE` and free-form `DESCRIBE`/`CONSTRUCT`
  are refused by the real C12 gate (exit 1). There is deliberately no flag that
  widens or bypasses the gate.
- `ne ontology conformance` — run the offline BFO+SHACL conformance gate
  (`run_corpus_conformance`, vendored corpus, no live stack) and summarize the
  quarantine lane (`SqliteTripleQuarantine(...).list()`, read-only).

**`--json`** is supported on every subcommand (stable, `sort_keys` shape) for
scripting; a human table is the default.

**Config** (`OntologySettings`, the same env layering as `ne-api`): `NE_ONTOLOGY_BACKEND`
(`oxigraph`|`fuseki`), `NE_ONTOLOGY_OXIGRAPH_PATH`, `NE_ONTOLOGY_FUSEKI_URL` /
`NE_ONTOLOGY_FUSEKI_USER` / `NE_ONTOLOGY_FUSEKI_PASSWORD`, `NE_ONTOLOGY_QUARANTINE_DB`,
`NE_ONTOLOGY_VERIFIED_ANSWERS_ENABLED`, `NE_ONTOLOGY_VERIFIED_TEMPLATES_DIR`.

**Exit codes**: `sparql` refusal → 1; `conformance` untriaged errors → 1. Quarantine
contents are informational and NEVER change the exit code. Every import in
`ne.cli.ontology` is lazy (the `ne connect`/`ne release` precedent) so `ne --help`
startup stays fast even though the command's closure includes rdflib/pyoxigraph.
### Governed demo (`ne demo`, NE-DEMO)

`ne demo` is the one-command, self-contained, reproducible governed demo over the
synthetic Acme corpus: **T1** hybrid retrieval with citations → an honestly
**DEGRADED T2** station until persistent AGE and governed sources are composed →
**T3** ontology-backed
verified answers — deterministically ROUTED in chat (a governed clarification menu
and an audited fail-closed refusal are demonstrated as their own arc steps, never a
guessed answer) → the **grounded analyst** (NE-GA-G6, the centerpiece): the model
reasons, fetches TWICE (accounts + orders verified reads), and joins them into a
per-account open-order count that the arc INDEPENDENTLY recomputes and verifies —
followed by a forced-hallucination leg where a scripted fabricated draft (an invented
account and count) is refused by the real deterministic grounding gate, audited, and
only the verified table is handed back → a **governed tier-2 write-back** with a
deny-then-HITL-approve gate → **audit-chain verify**. NE-GA-G7 hardens both grounded
legs for a LIVE run against the real pod model: the pass leg's SOLE pass/fail gate is
the structural verdict (fallback False + both template fetches), so live phrasing that
the fixture-strict per-account count regex can't parse is honestly reported, never
downgraded/failed; the refuse leg's fabrication now comes from the default-off
`NE_API_DEMO_GROUNDED_HALLUCINATION` seam (`ne.api.demo_seams`), which the demo overlay
enables, so the same refusal fires live, not only in the CI fixture arc. Bare `uv run
--all-packages ne demo` runs `up` then `run` and prints
a teardown hint; `ne demo up` / `ne demo run` / `ne demo down` are the granular subcommands.
In THIS dev repo the `--all-packages` flag is required (plain `uv run ne demo` fails with
`ModuleNotFoundError: No module named 'ne.acme'` — the default workspace sync omits `ne-acme`,
which the demo seeding imports; a pip-installed `ne-cli` is unaffected). If host port 443 is
held (e.g. a Tailscale listener), `ne demo up --https-port 9443` publishes the API on 9443
instead (Keycloak stays on 8443; a non-443 port needs docker compose ≥ 2.24.4). It is a
pure CONSUMER — it deploys via `ne up`, seeds via `ne connect sync fixture`, queries
ne-api `/v1/brain` + `/v1/chat`, and governs via the frozen C6 executor/HITL/audit,
reimplementing none of them. It leaves a `DEMO-DOSSIER.md` transcript behind. Needs
docker only — NO M365/Slack/Salesforce tenants and NO GPU. See `docs/demo/README.md`
for the operator runbook (prereqs, expected output, the degrade-label honesty table,
teardown). The scripted arc + governed-response assertions run in CI against fixtures
(`tools/ne-cli/tests/test_demo_arc.py`); the live `ne demo up` full run is operator-runnable.
