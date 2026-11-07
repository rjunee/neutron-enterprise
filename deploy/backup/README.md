# Neutron Enterprise — pilot backup/restore ops

Operational artifacts for `ne-backup` (the tool lives in `tools/ne-backup`). This
directory ships the scheduled-backup systemd units, the runner script, and the
runbooks. Everything here is **secret-free** — the customer key is read from the
bundle SecretStore by the tool itself and never appears in a unit, an env, or a
log (C12).

## Stated RPO / RTO

- **RPO ≤ 24 hours.** `ne-backup.timer` fires daily at 02:00 local
  (`Persistent=true`, so a missed run catches up on next boot); on-demand backups
  can be taken at any time. Worst-case data loss is therefore bounded by one day.
- **RTO ≤ 30 minutes** for pilot-scale corpora (≤ 10 GiB). The executable drill
  (`.github/workflows/backup-restore-drill.yml`) measures the real restore
  duration on each run and asserts it stays under a threshold
  (`NE_BACKUP_DRILL_RTO_SECONDS`, default 900s); the small-corpus drill completes
  in well under a minute. Large-corpus restore time is dominated by `pg_restore`
  and the qdrant snapshot upload.

Cross-service note: each database dump is internally consistent (MVCC), but a
backup run is not a single global point-in-time across postgres **and** qdrant —
the skew is bounded by the backup's own duration. This is a documented pilot-tier
property, not a global point-in-time guarantee.

## Retention

Default retention keeps the **7 newest** backups (`ne-backup prune --keep 7`,
run by `run-backup.sh` after every scheduled backup). The newest **safety**
backup is always spared from pruning, so a failed restore always has a recovery
point.

## Key provisioning runbook

1. Provision the customer key once per bundle:

   ```
   ne-backup keygen --bundle /opt/neutron/bundle
   ```

   This prints only a fingerprint. The 256-bit key is stored solely in the
   bundle SecretStore at path `pilot/backup` (field `key_b64`). The platform
   never stores it anywhere else. `keygen` refuses to overwrite an existing key
   (it exits non-zero if one is already present), so an accidental rerun cannot
   orphan your existing backups.

2. **Losing this key makes every backup permanently unrecoverable — by design.**
   There is no recovery path and no escrow. Back the bundle SecretStore up
   through the customer's own key-management process.

3. **Rotating the key** (deliberate, rare): `ne-backup keygen --bundle <dir>
   --rotate` replaces the key. Backups made under the *previous* key can no
   longer be restored by this tool, so rotate only after you have restored (or
   no longer need) everything encrypted under the old key.

## Restore runbook

```
ne-backup list    --dest /mnt/customer-backups
ne-backup verify  <id> --dest /mnt/customer-backups --bundle /opt/neutron/bundle
ne-backup restore <id> --dest /mnt/customer-backups --bundle /opt/neutron/bundle \
                       --restore-bundle-to /opt/neutron/restored
```

Restore takes a safety backup first (skip with `--no-safety`, discouraged). If a
restore fails partway, the tool prints a `ne-backup restore <safety-id> ...
--no-safety` command to roll the previous state back.

## Destination guidance (C2)

Backups land on a **local or customer-mounted path** (`--dest`) — an on-VM disk
or a customer-owned object-store mount. The tool itself makes **zero off-VM
connections**: postgres is reached via `docker exec` and qdrant only over
loopback, both validated in-VPC. There is no phone-home and no cloud SDK.

## Install (systemd)

Copy `systemd/ne-backup.service` and `systemd/ne-backup.timer` into
`/etc/systemd/system/`, set `NE_BACKUP_BUNDLE` / `NE_BACKUP_DEST` in the service
(commented placeholders are provided), then:

```
systemctl enable --now ne-backup.timer
```

## Helper-image provenance

The volume cold-tar helper is `alpine:3.22`, pinned by index digest in
`tools/ne-backup/src/ne/backup/config.py`:

```
alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce
```

Resolved from Docker Hub (`registry-1.docker.io/v2/library/alpine/manifests/3.22`,
docker-content-digest) on 2026-07-07. Alpine is Apache-2.0 / MIT (base image
material). Re-resolve and re-pin with `docker buildx imagetools inspect alpine:3.22`
when bumping. This is container tooling only — never a Python dependency.
