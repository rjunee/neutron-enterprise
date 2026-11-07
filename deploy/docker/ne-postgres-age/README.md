# PostgreSQL 18.6 + Apache AGE 1.7.0 artifact

This directory defines a first-party PostgreSQL/Apache AGE image suitable for
the persistent Pilot graph store. It does not activate Graph, load customer
data, alter Compose or Helm, or authorize deployment; those remain separate
composition and deployment steps.

## Exact provenance

| Input | Exact authority |
| --- | --- |
| Chainguard PostgreSQL 18.6 runtime (linux/amd64) | `cgr.dev/chainguard/postgres:latest@sha256:4401cf2919e749cd77b634a72e4a677c1a1afc12baddb21faf28df326162472e` |
| Chainguard PostgreSQL 18.6 development image (linux/amd64) | `cgr.dev/chainguard/postgres:latest-dev@sha256:3f12033ccd330632fccfc85435fa7a3f5498132c40127bec07a612322605077f` |
| Apache AGE source | `https://archive.apache.org/dist/age/PG18/1.7.0/apache-age-1.7.0-src.tar.gz` |
| AGE source SHA-512 | `129e64d70ec82e0fb47202145af45915b335048472c9a869129c31e4cb59f15a271a929a36a7abb2822272591e6476786c36908cb35a10cd73f2ecd0a617d3bb` |
| AGE detached-signature SHA-512 | `5bc1b1eb000ab85d2f9d567591f8a65a57ee503caff77b6b5a06a603c2e6c60e70420fab4c803ed23848f7fb0084016f03e0075fb4a9a43fb90be7d45c721e2a` |
| Apache AGE KEYS SHA-512 | `0a79d404d3d00d55d4dcf7b2e010310884acf194e57e37b67c3cb0d79678c5d2dc68ec482c522ed9946f4a3cd4335c936cd7176ead799b30e602741fb115e1f3` |
| Valid release signer | `429306038E35AC054DBB4B5826B6CD9DCD5B0045` (John Gemignani, Apache) |
| Builder Wolfi APK lock | 112 exact package/version specs from the signed Chainguard repository index, SHA-256 `82c2eeaf1f041a91b326409f46b87f093ec40daf50a697746b71749aa78fd42b`; installed package-content identity SHA-256 `3a0d62a4bec82b2059400fa083d5f3e6ff6702bf111651bed8a719b1a7c13cb4` |
| Runtime Wolfi license/source inventory | 62 exact package/version/origin/license/upstream rows, SHA-256 `ffe2de953ce4575587c9a4083b3af4a7c6832af27248a2ca96ef0e3b8dd6949a` |

Both Chainguard references are immutable linux/amd64 manifests matching the
Pilot host. Required CI verifies both keyless Sigstore signatures against the
official `chainguard-images/images` release workflow identity and GitHub OIDC
issuer before building. The runtime provides PostgreSQL 18.6 and the development
image provides its matching PGXS headers. Every builder package is version-pinned
in the committed 112-entry lock and resolves through the signed repository index.
The installed package/version set must equal that lock byte-for-byte, and the
repository-authored package content identities are independently hash-bound.
APK signature verification remains enabled; untrusted installation is never
permitted.

`fetch-age-src.sh` downloads the AGE archive, detached signature, and Apache
KEYS file, trying `dlcdn.apache.org`, `downloads.apache.org`, then
`archive.apache.org` in rounds with exponential backoff, and keeps a file only
when its SHA-512 matches the pin. Only the archive host serves 1.7.0, so CI
pre-fetches the files into an `actions/cache` directory keyed on the archive
digest and passes it to the build as the `age-src` build context
(`NE_POSTGRES_AGE_SRC_DIR` in `deploy/airgap/scripts/build-ne-postgres-age.sh`);
without it the build fetches the files itself. Either way the Dockerfile
independently checks the AGE archive, detached signature, exact Apache KEYS
bytes, and exact release fingerprint before compiling. It disables
only optional LLVM bitcode generation because the minimal development image has
no Clang; the native AGE shared library and SQL extension are built normally.
`SOURCE_DATE_EPOCH` is the signed release time.

The runtime inherits only the admitted Chainguard PostgreSQL image and copies in
the compiled AGE library, extension SQL, exact package inventory, and license
record. Build tools, downloaded source, and GnuPG state do not enter the final
stage. The inherited entrypoint, root-to-PostgreSQL user transition, and
`/var/lib/postgresql/data` data directory are preserved.

## Security and persistence proof

The prior official Alpine base was retired because its current
`libcrypto3=3.5.7-r0` and `libssl3=3.5.7-r0` are affected by high-severity
CVE-2026-14456.

Required CI scans the built image with Trivy (`--severity CRITICAL,HIGH`, never
VEX) and with Grype (`--fail-on high`). Neither uses ignore rules, a lowered
threshold, or a disabled scanner. The Grype image scan reads one image-scoped
OpenVEX ledger, `tools/supply-chain/vex/ne-postgres-age.openvex.json`, holding
exactly one statement: glibc CVE-2026-19499 (`strfmon`/`strfmon_l`) on
`glibc-2.44 2.44-r6` is `not_affected` / `vulnerable_code_not_in_execute_path`,
and no fixed Wolfi package exists. `tools/supply-chain/image_vex_gate.py` runs
in the same job before that scan and fails it when `strfmon` occurs in any file
of the image export other than `usr/lib/libc.so.6` (or does not occur in that
file), when the ledger and Grype's raw HIGH-and-above findings differ in either
direction, or when Grype reports a fix for the finding. The byte probe cannot
see a stripped, statically linked caller; the statement's `impact_statement`
records that residual.

The live gate starts a real PostgreSQL 18.6 server, creates AGE 1.7.0, creates a
graph, ACL-labelled vertex, and GIN index, and reads the vertex back. It then
removes the first container while retaining only its Docker volume, starts a
fresh container on that volume, and must read the same graph vertex again. This
proves the artifact can persist populated AGE state across container replacement;
it is not a claim that the production Graph composition or ingestion producers
have already been deployed.

The image ships `/usr/share/ne-postgres-age/runtime-apk-inventory.tsv` and
`THIRD_PARTY_LICENSES.txt`. CI regenerates the inventory from the running image,
requires the exact 57-row digest, positively admits only the observed license
expressions, emits a CycloneDX SBOM, and produces an in-toto/SLSA-v1 provenance
statement for both the immutable image export and SBOM.

## Build

The image contains no telemetry configuration, credentials, customer data,
graph fixture, or deployment endpoint. Build locally only in a connected
staging environment:

```bash
NE_POSTGRES_AGE_SOURCE_REV="$(git rev-parse HEAD)" \
  bash deploy/airgap/scripts/build-ne-postgres-age.sh
```

The local tag is `ne-postgres-age:18.6-1.7.0`. A tag is not deployment
identity; packaging and deployment must bind the resulting immutable digest.
