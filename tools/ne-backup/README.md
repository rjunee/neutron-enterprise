# ne-backup

Pilot-tier backup/restore with **encryption at rest under a customer-managed
key** for the Neutron Enterprise single-VM pilot (`deploy/compose/pilot`,
NE-070). Console script: `ne-backup`.

## What it captures

| Source | How | Notes |
|---|---|---|
| PostgreSQL | `pg_dumpall --globals-only` + per-database `pg_dump -Fc`, via `docker exec` | MVCC-consistent, online; no cold volume tar; in-container unix socket = trust auth (no password handled) |
| Qdrant | per-collection snapshot API over loopback HTTP | opaque snapshots; integrity by hash + AEAD only |
| Config bundle | cold tar, **excluding the ephemeral `runtime/` secrets dir** | the resolved-plaintext env never enters a backup (C12) |
| Docker volumes | pinned helper-image cold tar, allowlist `{caddy-data}` | ABI's hardcoded volume list swapped for pilot reality |

Every artifact is encrypted with the **NEB1** envelope: a per-backup random
256-bit DEK, wrapped by the customer KEK, framed as chunked AES-256-GCM. The KEK
lives **only** in the bundle SecretStore (`pilot/backup` field `key_b64`) — it is
never written to a manifest, keyinfo, artifact, or log.

## Manifest binding (keyinfo v2)

`manifest.json` is a plaintext set-of-artifacts descriptor. To stop an attacker
with backup-dest write access from silently **dropping a database** (and its
`ArtifactRef`) from the set — which older code would restore as `verified=true`
while skipping the missing DB — the DEK wrap now **authenticates the manifest**:
`sha256(manifest.json)` is folded into the DEK-wrap AEAD AAD and recorded as the
`manifest_sha256` field in `keyinfo.json`.

* **Presence of `manifest_sha256` = v2.** Any post-hoc edit of `manifest.json`
  (a dropped DB, a changed note, a single byte) makes the DEK unwrap raise
  `InvalidTag`, so restore/verify **fail closed** before any destructive step —
  never a silent partial restore.
* **Absence = legacy (pre-change) backup.** It unwraps under the original AAD, so
  backups written before this change still restore unchanged.
* **Downgrade-proof.** Stripping `manifest_sha256` from a v2 keyinfo yields the
  legacy AAD against a v2-wrapped DEK ⇒ `InvalidTag` ⇒ fail closed.

The per-artifact NEB1 stream format is byte-identical; the only versioning is this
`keyinfo.json` field-presence signal.

## Host-local plaintext staging

Plaintext DB dumps (create) and decrypted artifacts (restore/verify) stage on a
**host-local** `tempfile.mkdtemp` dir (0700), **never on the backup dest** — so a
SIGKILL/OOM/power-loss cannot strand full-DB plaintext on the (often network)
customer dest and defeat encryption-at-rest. The default staging root is
`tempfile.gettempdir()`; override it with the `staging_dir` config knob. A
configured root is created (0700) on first use, applies to `create`, `restore`
(including the automatic pre-restore safety backup), and `verify` (via
`verify --config`).

> **tmpfs caveat (Debian 13 & friends):** if `/tmp` is a tmpfs, a multi-GB restore
> would land plaintext in RAM. Point `staging_dir` at a disk-backed path such as
> `/var/tmp/ne-backup` for multi-GB deployments.

Only the **ciphertext** artifacts ever land under `<dest>/<id>.partial/`.

## Orphan sweep

Every `create` / `restore` / `prune` first sweeps **age-guarded** orphaned staging
dirs — `*.partial` and `.restore-*` on the dest, `ne-backup-*` on the host staging
root — reclaiming any older than **6 hours** (`ORPHAN_MIN_AGE_SECONDS`). The age
guard protects a concurrent run's fresh staging; a create/restore legitimately
running longer than 6h could have its staging swept by a concurrent run (accepted
at pilot scale).

## Streaming dump/restore

`pg_dump`/`pg_dumpall` stream their stdout straight to the destination file and
`pg_restore`/`psql` read stdin straight from the source file (`Runner.run_stream`),
so memory stays **O(pipe buffer)** instead of buffering a whole multi-GB dump in
RAM (which would OOM a pilot VM). The `-Fc` format, all flags, and the non-zero-exit
+ captured-stderr error handling are unchanged.

## Commands

```
ne-backup keygen  --bundle <dir> [--rotate]            # provision the customer KEK (prints fingerprint only; refuses to overwrite unless --rotate)
ne-backup create  --bundle <dir> --dest <dir> [--kind scheduled|on-demand] [--note ...]
ne-backup list    --dest <dir>
ne-backup verify  <id> --dest <dir> [--bundle <dir>]   # ciphertext hashes; + plaintext with --bundle
ne-backup restore <id> --bundle <dir> --dest <dir> [--restore-bundle-to <dir>] [--no-safety] [--yes]
ne-backup prune   --dest <dir> --keep 7
```

## Safety model

Restore is safe-by-construction: **validate manifest → KEK fingerprint →
ciphertext hashes → decrypt-all-to-staging with plaintext-hash checks** — every
check precedes any destructive step — then a **safety snapshot**, then the
destructive restore, then integrity verification. A flipped ciphertext byte, a
wrong key, or a missing artifact aborts before live data is touched. A partway
failure prints recovery instructions naming the safety backup and best-effort
restarts services.

The tar extraction path carries mandatory traversal / symlink / hardlink escape
guards (derived from ABI) so an attacker-crafted archive cannot write outside the
restore root.

## RPO / RTO, retention, ops

See `deploy/backup/README.md` for the stated RPO ≤ 24h and RTO ≤ 30min bounds,
the retention policy (`prune --keep 7`), the systemd timer, and the key
provisioning + restore runbooks.

## Attribution

Snapshot/restore hardening is derived from `jupyter-naas/abi`
`snapshot_runtime.py` @ `4b0d12f3ed0e` (MIT, Copyright (C) 2025 Naas.ai). See
`NOTICE`.
