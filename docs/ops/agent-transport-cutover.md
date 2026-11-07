# Pilot protected Agent transport cutover

This runbook is the only supported in-place transition of a populated Pilot
deployment to `NE_AGENT_TRANSPORT_GENERATION=agent-transport-v1`. It is a
quiesced cross-process migration, not a rolling deployment. A populated
Kubernetes/Helm deployment must stop with `protected transport cutover
unavailable`; this document makes no Kubernetes cutover claim. A genuinely
empty Kubernetes installation uses its separate empty-inventory bootstrap
admission and is outside this runbook.

## Invocation and fixed scope

Start from the exact clean release commit and its verified Pilot bundle. The
operator builds the candidate `ne-app` image, resolves its local
`sha256:<64-hex>` image ID, compares the running API and worker environment with
the candidate Compose environment, and invokes:

```bash
ne agent-transport cutover \
  --bundle /absolute/path/to/verified-pilot-bundle \
  --target pilot \
  --expected-image-digest sha256:EXACT_64_HEX_IMAGE_ID \
  --backup-dest /absolute/path/to/quarantined-cutover-backups
```

The target is fixed to `pilot`. The command accepts no arbitrary SQL, process
command, image command, pre-created live backup receipt, or Kubernetes target.
Do not run raw `docker compose`, `pg_dumpall`, or a hand-written migration in
parallel. The Zarf air-gap artifact carries the Pilot Compose contract, ne-cli
command source, and the `ne-backup` encrypted-backup
dependency as inert payloads; Zarf does not execute this cutover during a
Kubernetes deployment.

## Required phase order

The command holds the shared host upgrade lock for the complete transaction and
resumes from the content-free SecretStore journal at
`pilot/agent-transport-cutover`. The journal is outside both restored databases
and binds the admitted image ID, generation, phase, inventory digest, verified
backup identity and manifest hash, and KEK fingerprint.
It never records prompts, outputs, errors, citations, credentials, or other
private payloads.

1. Scale both application roots to zero as one fixed Compose operation and
   verify that neither `ne-api` nor `ne-worker` remains live.
2. While both roots are zero, create an encrypted `ne-backup` snapshot through
   the bundle SecretStore KEK and decrypt-verify it. Its authenticated manifest
   must contain both the platform database and `ne_dbos`. Two independently
   timed dumps, a receipt captured while either writer is live, plaintext
   `pg_dumpall`, or a manifest missing either database is not rollback evidence.
3. Bind the verified receipt into the journal before the first migration or
   retirement write. Run the pinned migration over Conversation/session/replay
   state and every owned DBOS 2.26.0 `chat.turn` surface. Preserve only protected
   references or content-free retirement/audit identity; uncertain external
   effects become unknown and are never retried as though rollback were proven.
4. Run the shape-complete zero-plaintext scan across the platform and DBOS
   inventories. Install the protected-only constraints, retirement ledger, and
   wire-generation fence only after the scan passes. An unknown DBOS shape or
   any legacy sentinel fails closed.
5. Restart worker and API only from the exact expected candidate image. Both
   roots must receive the same `agent-transport-v1` generation. Readiness
   verifies container health, the exact admitted image, and that generation.

The fixed command alone owns the scale, backup, migrate, scan, fence, and
restart sequence. An ordinary worker-first deploy is forbidden whenever the
running and candidate generations differ.

## Failure and rollback boundary

Before the retirement ledger or first migration write, a failure leaves the
deployment quiesced and the content-free journal resumable. The command does
not claim an automated restore or old-image restart path.

Once retirement begins—or once any new protected request is admitted—recovery
is permanently roll-forward-only: scale both roots to zero,
resume the journaled migration, repeat the complete scan/fence verification,
and restart only the expected protected image. A deployer that wraps this
command must disable any generic old-image rollback before entering it, because
it cannot safely infer which internal phase failed.

Retained pre-cutover backups are quarantined legacy material. Never mount or
read them through the product. This command exposes no restore operation; an old
pending workflow must never become executable again. Retention does not imply
that old encrypted backup bytes were erased.
