# tools/supply-chain

Supply-chain CI assets for Neutron Enterprise (NE-002). This directory is a
**non-package** workspace member — it holds policy, scripts, and templates, not
Python source. It carries a `pyproject.toml` (with `package = false`) only so the
root `tools/*` workspace glob resolves cleanly without editing the shared root
`[tool.uv.workspace]` config.

Contents:

| File | Purpose |
| --- | --- |
| `license-policy.toml` | The SPEC §2 C4 license-gate policy consumed by `ne check licenses` **and** the npm gate (`[npm_known]` table). |
| `npm_license_gate.py` | Fail-closed C4 license gate over an npm dependency closure (NE-061); reuses `ne.cli.licenses` + this policy file. |
| `vex_dualkey_gate.py` | Fail-closed CI gate: every un-VEX'd CRITICAL/HIGH finding from Trivy AND Grype must have a VEX statement keyed by that scanner's exact reported id (Finding E, invariant audit 2026-07-23). |
| `install-scanners.sh` | Installs pinned, checksum-verified Trivy / Grype / cosign binaries. |
| `tool-checksums.txt` | Committed sha256 checksums for the scanner binaries (fail-closed). |
| `verify-release.sh` | Customer-side offline (key-based) cosign verification of a release artifact. |
| `vex/` | OpenVEX exception scaffold (README + template) consumed by Trivy/Grype `--vex`. |
| `cosign.pub` | **Not committed yet** — owner-provisioned signing public key (see `docs/security/signing.md`). |

## npm license gate (C4 over the frontend closure)

`npm_license_gate.py` extends the SPEC §2 C4 policy to the npm dependency
closure introduced by the frontend (`packages/ne-console`). It **reuses the
exact same classifier and class file** as the Python gate — it imports
`ne.cli.licenses` (`Policy` / `classify_license`) and reads this directory's
`license-policy.toml`, so there is no second, weaker license policy.

Contract (fail-closed throughout):

- `package-lock.json` must be lockfileVersion ≥ 3; every non-root, non-link
  entry must carry a `sha512` `integrity` (C11).
- `node_modules` must be installed (`npm ci` first) — absence is a hard error.
- Each package's license is read from its installed
  `node_modules/<path>/package.json` (string / legacy `{type}` object / legacy
  `licenses` array); platform-gated optional deps not installed on the host fall
  back to the license npm recorded inline in the lockfile.
- The whole tree is scanned, devDependencies included. BANNED / UNKNOWN / any
  `*_NEEDS_APPROVAL` outcome ⇒ exit 1.
- The optional `[npm_known]` table (name → human-verified SPDX) rescues a
  package whose resolved license classifies UNKNOWN — never the banned class (a
  banned `[npm_known]` value is a `PolicyError`).

Local command (from the repo root):

```bash
uv run python tools/supply-chain/npm_license_gate.py --project packages/ne-console
```

It runs in CI in the `supply-chain.yml` `license-gate` job (after a
`npm ci --ignore-scripts`). Trivy/Grype already fs-scan the repo, so the
committed `package-lock.json` enters vuln scanning with no workflow change.

See `docs/security/supply-chain.md` for the full policy rationale, including the
2026-03-19 Trivy-action compromise that drives the checksum-pinned install model.

## VEX dual-key gate

Trivy keys a vulnerability finding by its CVE id when one exists; Grype keys
the SAME vulnerability by its GHSA (or PYSEC) id. An OpenVEX statement
suppresses a finding only when its `vulnerability.name` matches the id a
scanner actually reported — an alias-only statement is one scanner-behavior
change away from a red build, which is exactly the gap the chromadb/json-repair
incident exposed (invariant-audit Finding E). `vex_dualkey_gate.py` makes the ledger's dual-scanner-keying discipline a
CI-enforced invariant instead of manual diligence.

What it checks: every CRITICAL/HIGH finding **with a fix available** from an
UN-VEX'd Trivy or Grype run must appear as some statement's exact
`vulnerability.name` in `vex/openvex.json` — aliases never satisfy it. A
CRITICAL/HIGH finding with no fix is report-only: the gate prints it as a
`VEX-DUALKEY report-only:` line and does not fail on it, matching the scans'
own `--ignore-unfixed` / `--only-fixed` policy. "Fix available" is a non-empty
Trivy `FixedVersion` or Grype `fix.state == "fixed"`. Grype's severity cut
mirrors the `--fail-on high` step (critical/high, case-insensitive). A missing
or zero-size `openvex.json` is treated as an empty ledger, mirroring the
workflow's own VEX-flag assemble step (`[ -s ... ]`).

Where it runs: in the `vuln-scan` job of `supply-chain.yml`. The raw,
unfiltered Trivy and Grype JSON reports are written first; the fail-closed
`--vex` Trivy/Grype scans and this gate follow and reuse the vulnerability DBs
the raw steps downloaded. A final `if: always()` step
(`vuln_report.py`) writes every HIGH/CRITICAL finding, fixed or unfixed, to
the job summary, and the raw reports are uploaded as the `vuln-reports`
artifact.

Local reproduction (from the repo root, with trivy/grype installed at the
pinned versions):

```bash
uv export --format requirements-txt --no-emit-project --no-dev -o requirements.txt
trivy fs --scanners vuln --format json \
  --output /tmp/trivy-raw.json .
grype "dir:." -o json --file /tmp/grype-raw.json
python3 tools/supply-chain/vex_dualkey_gate.py \
  --trivy /tmp/trivy-raw.json \
  --grype /tmp/grype-raw.json \
  --vex tools/supply-chain/vex/openvex.json
rm requirements.txt
```

## Scanner database refresh + prune (`scan-db-refresh.sh`)

The nightly surveillance lane MUST refresh the grype/trivy vulnerability
databases through this script rather than calling the scanners' own update
commands:

```bash
bash tools/supply-chain/scan-db-refresh.sh
```

It updates both databases, then deletes the snapshots the update superseded,
then prints a `SCAN_CACHE_BYTES` line so the footprint is visible per run.

**Why it exists.** grype does not download in place: it stages each database
under `db/grype-db-download<random>` and moves it onto `db/6` only on success.
A run killed mid-download (cron timeout, sleep, SIGKILL) strands that staging
directory at ~1.9 GB, and because the suffix is random it is never reused or
overwritten. By 2026-08-20 eleven of them had accumulated to 21 GB and set off
a 95% disk alert. trivy behaves differently — it rewrites `db/trivy.db` in
place, so its ~1.2 GB is steady-state and only its per-scan cache is pruned.

**Safety properties** (each locked by a test in `tests/test_scan_db_refresh.py`):

- Pruning runs only after the update succeeds *and* grype reports a valid DB,
  so a failed download can never leave the lane without a database.
- The first-level entry containing the database grype reports as live is never
  deleted; every sibling is. It is the containing entry rather than the live
  file's parent directory because a layout that stores the DB directly at
  `db/vulnerability.db` would otherwise make the parent equal the root, match
  nothing, and delete the live file as a sibling.
- If the reported live DB is not inside the cache root being pruned, nothing is
  deleted — the configured cache and the visible tree have diverged.
- An explicitly configured `GRYPE_DB_CACHE_DIR` / `TRIVY_CACHE_DIR` is used
  exactly as given, even before it exists. The platform fallback applies only
  when no variable was set; rewriting a configured-but-not-yet-created path
  would leave the scanner writing to one tree while the prune and the footprint
  report described another.
- An entry with any write activity anywhere inside it in the last
  `NE_SCAN_STAGING_MIN_AGE_MIN` minutes (default 60) is left alone, so a
  download in flight is not destroyed. The check walks the subtree rather than
  reading the entry's own mtime: a directory's mtime stops advancing once its
  entries exist, so a long download whose file is still growing would otherwise
  look abandoned and be deleted mid-write.
- `grype db delete` and `trivy clean --vuln-db` are never used: both remove the
  *live* database and buy nothing but a re-download.
- Over-budget footprint is reported on stderr; it fails the run only under
  `--enforce`, so a disk anomaly can never suppress a vulnerability scan.
