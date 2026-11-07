# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne connect sync [<source>]`` — the one-shot governed connector sync (NE-INGEST).

Closes the ADR-001 keystone gap P1-4 (SPEC §11 decision 2026-07-10): a first-seed /
manual re-sync / the NE-077 'Acme corpus ingested' gate-step invoker. Runs each
configured (or a named) connector sync ONCE over the SAME governed platform recipe +
KnowledgeWriter path (C7) the ne-worker cron uses — this command only INVOKES the
existing per-connector syncs.

STARTUP DISCIPLINE (the fingerprint.py precedent): every heavy import is LAZY inside
the command body so ``ne --help`` / ``ne check`` stay fast. The platform is composed
ONLY when there is actual work — the no-source no-op returns without ever building it
(and ``_build_platform`` is the decided test seam, monkeypatched in tests).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import click


def _build_platform() -> Any:
    """Compose the governed platform from the ambient env (the CLI test seam).

    Lazy-imports ne-api so ``ne --help`` never drags the composition graph. Tests
    monkeypatch ``ne.cli.connect._build_platform`` to return a sqlite/stub platform.
    """
    from ne.api.platform import AppSeams, build_platform
    from ne.api.settings import ApiSettings

    return build_platform(ApiSettings(), AppSeams())  # type: ignore[call-arg]


def _dispose_platform(platform: Any) -> None:
    """Release the platform's engines (+ any retained ReBAC client) — mirrors
    ``ne.worker.main._shutdown_platform``'s teardown."""
    import asyncio

    try:
        if getattr(platform, "rebac_client", None) is not None:
            asyncio.run(platform.rebac_client.close())
    finally:
        for engine in platform.engines:
            engine.dispose()


def _ingest_stack(platform: Any, cs: Any) -> Any:
    """Build the ONE governed ingest stack for a CLI invocation.

    The CLI deliberately has no store construction of its own: it calls the same
    ``build_ingest_stack`` composer the worker cron calls, so the only way this
    process can hold authority-issuance capability is the composer's own
    ``IngestStack.authority``. There is no CLI-side Writer to construct.
    """
    from ne.connect.runner import build_ingest_stack

    return build_ingest_stack(
        chunk_store=platform.chunk_store,
        runtime_engine=platform.runtime_engine,
        sink=platform.sink,
        settings=cs,
        embedder=platform.embedder,
        document_lineage=platform.document_lineage,
    )


def _authority_reader(platform: Any) -> Any:
    """Return the composed metadata reader; never construct a raw attempt store."""
    reader = getattr(platform, "authority_attempts", None)
    if reader is None:
        raise click.ClickException("source authority attempts are not configured")
    return reader


def _authority_payload(attempt: Any) -> dict[str, Any]:
    """The CLI's one closed safe metadata projection."""
    return {
        "attempt_id": attempt.attempt_id,
        "source_kind": attempt.source_kind,
        "source_binding": attempt.source_binding,
        "scope_hash": attempt.scope_hash,
        "identity_hash": attempt.identity_hash,
        "stage": attempt.stage.value,
        "source_revision": attempt.source_revision,
        "contract_epoch": attempt.contract_epoch,
        "prior_artifact_id": attempt.prior_artifact_id,
        "receipt_id": attempt.receipt_id,
        "receipt_hash": attempt.receipt_hash,
    }


@click.group()
def connect() -> None:
    """Connector operations."""


@connect.group("authority")
def authority() -> None:
    """Ops-only source-authority admission inspection and retry."""


@authority.command("list")
def authority_list() -> None:
    """Print the stable, deduplicated current pending attempt queue."""
    import json

    platform = _build_platform()
    try:
        attempts = _authority_reader(platform).pending()
    finally:
        _dispose_platform(platform)
    payload = {"attempts": [_authority_payload(item) for item in attempts]}
    click.echo(json.dumps(payload, sort_keys=True))


@authority.command("inspect")
@click.argument("attempt_id")
def authority_inspect(attempt_id: str) -> None:
    """Print the safe ids, hashes and durable stage of one admission attempt.

    Read-only by construction: it opens the attempt store, never the Writer, and
    prints ids/hashes/stage only — no ACL label, no content, no credential.
    """
    import json

    from ne.connect.authority_attempts import (
        AuthorityAttemptNotPendingError,
        UnknownAuthorityAttemptError,
    )

    platform = _build_platform()
    try:
        try:
            attempt = _authority_reader(platform).read(attempt_id)
        except (UnknownAuthorityAttemptError, AuthorityAttemptNotPendingError) as exc:
            raise click.ClickException("authority attempt not found") from exc
    finally:
        _dispose_platform(platform)
    click.echo(json.dumps(_authority_payload(attempt), sort_keys=True))


@authority.command("retry")
@click.argument("attempt_id")
def authority_retry(attempt_id: str) -> None:
    """Re-run the server-bound source sync so a stalled attempt resumes.

    There is deliberately no privileged path here. Retry runs the SAME
    ``run_configured_sync`` composer every other invoker runs; it cannot force an
    allow, cannot mint a receipt and cannot delete an artifact. A completed
    attempt is reported as complete and nothing is run.
    """
    import asyncio

    from ne.connect.authority_attempts import (
        AuthorityAttemptNotPendingError,
        UnknownAuthorityAttemptError,
    )
    from ne.connect.runner import ConnectSyncSettings, run_configured_sync

    cs = ConnectSyncSettings()  # type: ignore[call-arg]  # env-driven
    platform = _build_platform()
    try:
        reader = _authority_reader(platform)
        try:
            before = reader.read(attempt_id)
        except (UnknownAuthorityAttemptError, AuthorityAttemptNotPendingError) as exc:
            raise click.ClickException("authority attempt not found") from exc
        stack = _ingest_stack(platform, cs)
        unavailable = stack.authority_unavailable_for(before.source_binding)
        if unavailable is not None:
            raise click.ClickException(
                f"source authority is not composed in this process: {unavailable}"
            )
        asyncio.run(
            run_configured_sync(
                before.source_binding,
                writer=stack.writer,
                state_store=stack.state_store,
                audit_sink=platform.sink,
                actor=f"cli:connect-authority-retry-{attempt_id}",
                kpi_tag=cs.kpi_tag or None,
                quarantine=stack.quarantine,
                authority=stack.authority_for(before.source_binding),
                chunk_reader=stack.chunk_reader,
            )
        )
        try:
            after = reader.read(attempt_id)
        except AuthorityAttemptNotPendingError:
            click.echo(f"{attempt_id}: retry completed")
        else:
            click.echo(f"{attempt_id}: stage={after.stage.value}")
    finally:
        _dispose_platform(platform)


class ConnectorSyncFailed(SystemExit):
    """``SystemExit(1)`` that also NAMES the sources that failed.

    ``ne demo up`` drives this command in-process (``ctx.invoke``) and has to be
    able to report exactly which corpora went unseeded. A bare ``SystemExit(1)``
    forced the caller to scrape stderr, so the bring-up settled for a one-line
    warning and carried on — the silent-degradation shape this exists to kill.
    Exit-code behaviour for a shell caller is unchanged: it is still ``1``.
    """

    def __init__(self, sources: Sequence[str]) -> None:
        super().__init__(1)
        self.sources: tuple[str, ...] = tuple(sources)


@connect.command("sync")
@click.argument("source", required=False)
@click.option(
    "--traceback",
    "show_traceback",
    is_flag=True,
    help="Also print the full Python traceback of any sync failure.",
)
def sync(source: str | None, show_traceback: bool = False) -> None:
    """Run a governed connector sync ONCE (all configured sources, or SOURCE).

    Exit 0 on success (incl. the no-source clean no-op); exit 1 if any source fails
    (unconfigured, unknown, or a sync error).

    A FAILING SYNC STATES ITS CAUSE. This command used to print the exception
    CLASS alone, and ``OperationalError`` — connection-refused, too-many-clients,
    deadlock, lock/statement timeout, sqlite readonly or locked — is not a
    diagnosis; getting behind one cost real time on the demo box. The error line
    now carries the driver's own error code, the schema identifiers it named, the
    wrapped ORIGIN class (the reclassification is usually what hides the cause),
    and a bounded REDACTED message — projected by :mod:`ne.contracts.failure` so
    payloads and credential-bearing URLs still never appear (C12).
    ``--traceback`` adds the full traceback, which is a terminal, not the ledger.
    """
    import asyncio
    import traceback

    from ne.connect.runner import (
        SYNC_SOURCES,
        ConnectSyncSettings,
        SyncNotConfiguredError,
        run_configured_sync,
    )
    from ne.contracts.failure import describe_failure

    cs = ConnectSyncSettings()  # type: ignore[call-arg]  # env-driven, cheap (no platform)

    # Resolve the target sources WITHOUT composing the platform.
    if source is not None:
        if source not in SYNC_SOURCES:
            raise click.ClickException(
                f"unknown connector source {source!r}; valid sources are {sorted(SYNC_SOURCES)!r}"
            )
        targets = [source]
    else:
        targets = list(cs.sources)
        if not targets:
            click.echo(
                "no connector sources configured (NE_CONNECT_SYNC_SOURCES is empty) "
                "— nothing to sync"
            )
            return

    # The governed writer needs the state + quarantine sqlite paths. For the
    # all-configured path the ConnectSyncSettings validator already enforces this;
    # a NAMED source may be run without listing it in sources, so guard here.
    if not cs.state_path or not cs.quarantine_path:
        raise click.ClickException(
            "NE_CONNECT_SYNC_STATE_PATH and NE_CONNECT_SYNC_QUARANTINE_PATH must be set "
            "to run a connector sync (the governed write state + quarantine lanes)"
        )

    kpi_tag = cs.kpi_tag or None
    platform = _build_platform()
    try:
        stack_settings = cs.model_copy(
            update={"sources": tuple(dict.fromkeys((*cs.sources, *targets)))}
        )
        stack = _ingest_stack(platform, stack_settings)

        async def run_targets() -> list[str]:
            failed: list[str] = []
            # Bootstrap and every configured source share ONE event loop. The
            # composed platform retains async HTTP clients (TEI/Qdrant); running
            # each source through a separate ``asyncio.run`` closes the loop after
            # source one and makes source two fail with ``RuntimeError`` before its
            # first governed write.
            ensure_collection = getattr(platform.chunk_store, "ensure_collection", None)
            if ensure_collection is not None:
                await ensure_collection()
            for target in targets:
                unavailable = stack.authority_unavailable_for(target)
                if unavailable is not None:
                    failed.append(target)
                    click.echo(
                        f"{target}: ERROR source authority unavailable ({unavailable})",
                        err=True,
                    )
                    continue
                try:
                    report = await run_configured_sync(
                        target,
                        writer=stack.writer,
                        state_store=stack.state_store,
                        audit_sink=platform.sink,
                        actor=f"cli:connect-sync-{target}",
                        kpi_tag=kpi_tag,
                        quarantine=stack.quarantine,
                        authority=stack.authority_for(target),
                        chunk_reader=stack.chunk_reader,
                        # Contention on the shared connector state database is a
                        # WAIT now, not a failure — but it must be visible, or a
                        # multi-minute wait reads as a hang.
                        on_lock_wait=lambda message: click.echo(message, err=True),
                    )
                except SyncNotConfiguredError as exc:
                    failed.append(target)
                    click.echo(f"{target}: ERROR not configured ({exc})", err=True)
                    continue
                except Exception as exc:
                    failed.append(target)
                    lines = describe_failure(exc)
                    click.echo(f"{target}: ERROR {lines[0]}", err=True)
                    for line in lines[1:]:
                        click.echo(f"{target}:   {line}", err=True)
                    if show_traceback:
                        click.echo(
                            "".join(
                                traceback.format_exception(type(exc), exc, exc.__traceback__)
                            ).rstrip(),
                            err=True,
                        )
                    continue
                click.echo(
                    f"{target}: mode={report.mode} seen={report.records_seen} "
                    f"written={report.written} skipped_unchanged={report.skipped_unchanged} "
                    f"invalid={report.invalid} quarantined={report.quarantined}"
                )
            return failed

        failed = asyncio.run(run_targets())
    finally:
        _dispose_platform(platform)

    if failed:
        raise ConnectorSyncFailed(failed)
