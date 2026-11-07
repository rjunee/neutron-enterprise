<!--
Copyright (c) 2026 Ryan Junee.
SPDX-License-Identifier: Apache-2.0
-->

# Disaster Recovery: RPO/RTO posture & restore drills (NE-DR1)

This runbook is **truth-gated**: it documents only what the committed
Terraform / Helm configuration actually sets. Where a property is a behaviour of
the managed cloud service rather than something this repo configures, it says so;
where a capability is an off-by-default flag, it says so.

## 1. Posture summary

The DR posture was decided by the owner in **SPEC §11 ("DR/BACKUP POSTURE
RESOLVED", 2026-07-18)**. The two protection mechanisms are deliberately split:

- **The transactional DB is protected by the managed data tier's NATIVE
  automated backup + point-in-time restore (PITR)** — app-consistent, taken by
  the managed service (AWS RDS / Azure Database for PostgreSQL Flexible Server).
  It is **NOT** protected by crash-consistent volume snapshots of a live DB.
- **Velero protects Kubernetes resources** (namespace-scoped object backups to
  the BackupStorageLocation) **plus an OPT-IN VolumeSnapshotLocation for NON-DB
  persistent volumes only** (the `default` VSL). Crash-consistent PV snapshots
  are off by default and, when enabled, are structurally prevented from touching
  a live in-cluster DB volume (§7).

**NE-AWS-11 resolution.** Codex's NE-AWS-11 review flagged the absence of a
Velero VolumeSnapshotLocation. That absence is resolved here as an **INTENTIONAL
SPLIT, not an omission**: the DB is covered by managed-service native
backup/PITR (higher fidelity than a crash-consistent volume snapshot of a
running database), and the VSL now exists for the non-DB PVs that Velero is the
right tool for.

## 2. Protection matrix

| What | Mechanism | Where configured | Notes |
|------|-----------|------------------|-------|
| Transactional DB (AWS) | RDS automated backup + PITR | `deploy/terraform-eks/modules/data/main.tf` | `backup_retention_period` var, default **7 days** (range 7–35); PITR to any point in the window; `multi_az = true`; CMK-encrypted (`storage_encrypted` + `kms_key_id`); `skip_final_snapshot = false` (a destroy takes a final snapshot) |
| Transactional DB (Azure) | Flexible Server automated backup + PITR | `deploy/terraform-azure/modules/data/main.tf` | `backup_retention_days` var, default **7 days** (range 7–35); PITR is automatic once retention > 0; `ZoneRedundant` HA; `geo_redundant_backup_enabled` is **a flag, off by default** (create-time only — flipping it later forces server replacement) |
| K8s resources | Velero `Schedule` → BackupStorageLocation | `deploy/helm/neutron-enterprise/templates/velero-schedule.yaml` | Namespace-scoped object backup; profiles `pilot` / `production` (§3); default-OFF (`backup.velero.enabled`) |
| Non-DB PVs (qdrant vector store, `ne-backup-storage` PVC) | Velero VolumeSnapshotLocation `default` | `deploy/terraform-{eks,azure}/velero.tf` + `backup.velero.snapshotVolumes` | Crash-consistent CSI/managed-disk snapshots; **OPT-IN** via `backup.velero.snapshotVolumes=true`; AWS = native EBS snapshotter, Azure = managed-disk snapshotter |
| Logical DB dumps | `pg_dumpall` CronJob | `deploy/helm/neutron-enterprise/templates/backup-cronjob.yaml` (in-cluster pilots); `deploy/backup/` (compose tier) | The DB-native logical half of NE-071; independent of the Velero halves |

**Truth-gating callouts.** Geo-redundant backup on Azure is described as a flag
that is **off by default** — this config never claims geo-redundancy is active.
Non-DB PV snapshots are **crash-consistent** and **opt-in** — stated plainly.

## 3. RPO / RTO

These reuse the numbers already stated in the repo (chart `values.yaml`
`backup.velero.profiles` comments and `deploy/backup/README.md`):

| Profile / mechanism | RPO | RTO | Scale |
|---------------------|-----|-----|-------|
| Velero `pilot` | ≤ 24 h (daily 03:00 UTC) | ≤ 30 min | ≤ 10 GiB corpora |
| Velero `production` | ≤ 6 h (every 6 h) | ≤ 60 min | ≤ 100 GiB corpora |
| Compose backup tier | ≤ 24 h (daily 02:00 local) | ≤ 30 min | ≤ 10 GiB (`deploy/backup/README.md`) |
| DB PITR (RDS / Flexible Server) | = transaction-log archival lag | (see below) | n/a |

**DB PITR RPO** is the managed service's transaction-log archival lag — **a
property of RDS / Flexible Server, typically minutes**. This Terraform does not
configure that granularity; it only sets the retention window inside which a
point-in-time restore is possible.

**RTO scope.** The stated RTOs are for the restore operation itself. They
**exclude** cluster-rebuild / `terraform apply` time. A full region-loss recovery
that first rebuilds the cluster will take longer than the per-mechanism RTO.

## 4. Restore drill A — AWS RDS point-in-time restore

PITR creates a **NEW** DB instance; it does not modify the source in place.

```
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier <prefix>-pg \
  --target-db-instance-identifier <prefix>-pg-drill \
  --restore-time <ISO8601> \
  --db-subnet-group-name <prefix>-pg \
  --vpc-security-group-ids <postgres SG id> \
  --no-publicly-accessible
```

Then:

1. Repoint the chart's external-DB host secret at the new instance's endpoint
   (the `postgres.external` host secret — see the README mapping table).
2. Verify `pg_isready -U ne -d neutron` against the restored endpoint and confirm
   app health.
3. Cutover is the secret repoint; the old instance is retired **manually** once
   the drill is confirmed.

## 5. Restore drill B — Azure Flexible Server point-in-time restore

```
az postgres flexible-server restore \
  --resource-group <rg> \
  --name <prefix>-pg-drill \
  --source-server <prefix>-pg \
  --restore-time <ISO8601>
```

**Private-access caveat.** These servers use VNet integration (delegated subnet +
linked private DNS zone), not a public endpoint. The restore target must land in
the **same or a peered VNet**, with the delegated subnet and the
`privatelink.postgres.database.azure.com` private DNS zone available, or the
restored server will be unreachable from the cluster.

## 6. Restore drill C — Velero (resources + non-DB PVs)

```
velero backup get
velero restore create --from-backup <backup-name> --include-namespaces <ns>
velero restore describe <restore-name> --details
```

Then verify the qdrant collections respond (the vector store PV is the primary
non-DB PV in scope).

**PV data restores only for backups taken with `snapshotVolumes=true`.** A
backup taken at the default (`snapshotVolumes=false`) restores Kubernetes
resources only — no PV data. Enable non-DB PV snapshots per §7 first if you need
volume data back.

## 7. DB-volume exclusion contract

The DB volume of an in-cluster Postgres deployment must **never** be
crash-consistently snapshotted (a live DB's on-disk state is not app-consistent).
Two layers enforce this:

1. **Managed-DB deployments are structurally excluded.** With managed RDS /
   Flexible Server the DB is external and is **never a cluster PV**, so Velero
   cannot snapshot it regardless of settings.
2. **In-cluster Postgres (`profiles.core=true`) is fail-closed.** Opting into
   `backup.velero.snapshotVolumes=true` while in-cluster Postgres is enabled
   **aborts the Helm render** (`templates/velero-schedule.yaml`) until the
   operator both labels the DB PVC out of snapshots and attests it:

   ```
   kubectl -n <release-ns> label pvc pg-data-postgres-0 \
     velero.io/exclude-from-backup=true --overwrite
   ```

   then set `backup.velero.dbSnapshotExclusionConfirmed=true`.

**Verification.** After a backup runs, `velero backup describe <name> --details`
must show **no snapshot** for `pg-data-postgres-0`.

## 8. Not covered / parked

- **Live restore drills are cloud-gated** (the NE-AWS-OWNER pattern): an actual
  RDS/Flexible PITR restore and a `velero restore` with real snapshots require a
  live cloud account and are not part of offline merge verification.
- **No cross-region object replication** is configured for the Velero
  BackupStorageLocation bucket/container.
- **Geo-redundant DB backup is an off-by-default flag**
  (`geo_redundant_backup_enabled`, Azure) — not enabled in the default posture.
- **The Azure disk-snapshot role** (`<prefix>-velero-disk-snapshots`, scoped to
  the main RG + the AKS node RG) and its two role assignments are only fully
  exercisable at live apply; `terraform validate` cannot prove the cross-scope
  assignment resolves. This rides the owner-gated apply workflow.
