# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""`ne k8s` — the production Kubernetes installer (NE-071a).

Mirrors the pilot Compose installer (`ne up`/`ne down`, up.py) for the Helm
umbrella chart at deploy/helm/neutron-enterprise: same bundle verification
(``ne.cli.pilotcfg.verify_bundle``), same secret-resolution code path
(``ne.cli.pilotcfg.resolve_required_secrets``), but the K8s installer creates
a namespaced ``Secret`` via ``kubectl apply`` on STDIN (never a Helm-rendered
Secret — C12: `helm get values`/release history must stay clean) and then
drives ``helm upgrade --install``.

`ne k8s render` — turn a verified bundle into the Helm values overlay that
stamps ``global.configHash``/``global.hostname`` and carries the promoted
config bundle's TOMLs (rendered into the ``ne-platform-config`` ConfigMap for
identity/rollout — see deploy/helm/README.md, "NE_CONFIG_FILE deviation").
NE-PILOT-OPA-BUNDLE: the overlay also carries ``opa.governBundle.files`` —
the real ne-govern rego bundle content, stamped from
``packages/ne-govern/policies`` — so the opa subchart's govern-policies
ConfigMap + per-file subPath mounts render and a live
``/v1/data/ne/govern/decision`` query is DEFINED instead of failing closed
against the shipped ``ne.dev`` placeholder. The overlay is written as JSON:
JSON is a valid YAML subset, so `helm -f` accepts it directly — this keeps
ne-cli's runtime dependency closure unchanged (no new PyYAML *runtime*
dependency; PyYAML stays test-only, tools/ne-cli/pyproject.toml).

`ne k8s up`     — verify the bundle, render the overlay, resolve+apply the
installer-mode Secret, ensure the namespace, then `helm upgrade --install`.
"""

from __future__ import annotations

import base64
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import click
from sqlalchemy import text

from ne.cli.checks import _find_repo_root
from ne.cli.opa_bundle import govern_bundle_files
from ne.cli.pilotcfg import resolve_required_secrets, verify_bundle

_DEFAULT_CHART = Path("deploy/helm/neutron-enterprise")
_SECRET_NAME = "ne-platform-secrets"
_BUNDLE_TOML_FILES = ("ne-runtime.toml", "ne-govern.toml", "ne-secrets.toml")
_PROTECTED_TRANSPORT_GENERATION = "agent-transport-v1"
_BOOTSTRAP_SECRET_PATH = "k8s/protected-transport-bootstrap"


@dataclass(frozen=True, slots=True)
class _ProtectedBootstrapAdmission:
    platform_database_url: str
    dbos_database_url: str


@dataclass(frozen=True, slots=True)
class _ReleaseAdmission:
    mode: str


def _validated_database_url(value: str, *, database: str, namespace: str) -> str:
    if not value or value != value.strip():
        raise click.ClickException("protected transport bootstrap database URL is missing")
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"} or not parsed.hostname:
        raise click.ClickException("protected transport bootstrap requires PostgreSQL URLs")
    if unquote(parsed.path.removeprefix("/")) != database:
        raise click.ClickException(
            f"protected transport bootstrap URL must address exact database {database!r}"
        )
    chart_local_hosts = {
        "postgres",
        f"postgres.{namespace}",
        f"postgres.{namespace}.svc",
        f"postgres.{namespace}.svc.cluster.local",
    }
    if parsed.hostname.rstrip(".").lower() in chart_local_hosts:
        raise click.ClickException(
            "protected transport bootstrap requires pre-provisioned external databases"
        )
    return value


async def _resolve_protected_bootstrap_async(
    bundle: Path, *, namespace: str
) -> _ProtectedBootstrapAdmission:
    from ne.contracts.errors import SecretNotFoundError
    from ne.secrets import SecretsSettings, create_secret_store

    settings = SecretsSettings.from_config_file(bundle / "config" / "ne-secrets.toml")
    store = create_secret_store(settings)
    try:
        version = await store.read(_BOOTSTRAP_SECRET_PATH)
        platform_url = version.data["platform_database_url"]
        dbos_url = version.data["dbos_database_url"]
    except (SecretNotFoundError, KeyError) as exc:
        raise click.ClickException(
            "missing protected Kubernetes bootstrap database connection"
        ) from exc
    return _ProtectedBootstrapAdmission(
        platform_database_url=_validated_database_url(
            platform_url, database="neutron", namespace=namespace
        ),
        dbos_database_url=_validated_database_url(
            dbos_url, database="ne_dbos", namespace=namespace
        ),
    )


def _resolve_protected_bootstrap(bundle: Path, *, namespace: str) -> _ProtectedBootstrapAdmission:
    import asyncio

    return asyncio.run(_resolve_protected_bootstrap_async(bundle, namespace=namespace))


def _prove_empty_database(url: str, *, database: str) -> None:
    """Prove one external database has no legacy transport or generation rows."""
    from ne.audit import create_engine_with_timeouts

    table_schema = "public" if database == "neutron" else "dbos"
    legacy_tables = (
        ("ne_agent_session_messages", "ne_agent_conversation_turns")
        if database == "neutron"
        else (
            "workflow_status",
            "operation_outputs",
            "notifications",
            "streams",
            "workflow_events",
            "workflow_events_history",
        )
    )
    engine = create_engine_with_timeouts(url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :table_schema"
                ),
                {"table_schema": table_schema},
            )
            present = {str(row[0]) for row in rows} & set(legacy_tables)
            for table in legacy_tables:
                if table not in present:
                    continue
                qualified = f'"{table_schema}"."{table}"'
                if connection.execute(
                    text(f"SELECT EXISTS (SELECT 1 FROM {qualified} LIMIT 1)")
                ).scalar_one():
                    raise click.ClickException(
                        "protected transport cutover unavailable: legacy inventory is not empty"
                    )
            generation_table = connection.execute(
                text("SELECT to_regclass('public.ne_agent_transport_generation')")
            ).scalar_one()
            if (
                generation_table is not None
                and connection.execute(
                    text("SELECT EXISTS (SELECT 1 FROM ne_agent_transport_generation)")
                ).scalar_one()
            ):
                raise click.ClickException(
                    "protected transport cutover unavailable: generation already exists"
                )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(
            f"protected transport bootstrap inventory unavailable for {database!r}"
        ) from exc
    finally:
        engine.dispose()


def _prove_empty_external_databases(admission: _ProtectedBootstrapAdmission) -> None:
    _prove_empty_database(admission.platform_database_url, database="neutron")
    _prove_empty_database(admission.dbos_database_url, database="ne_dbos")


def _read_json(cmd: list[str], *, unavailable: str) -> Any:
    result = _run(cmd)
    if result.returncode != 0:
        raise click.ClickException(unavailable)
    try:
        return json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise click.ClickException(unavailable) from exc


def _release_admission(*, namespace: str, release_name: str) -> _ReleaseAdmission:
    result = _run(
        [
            "helm",
            "list",
            "--all",
            "--namespace",
            namespace,
            "--filter",
            f"^{release_name}$",
            "--output",
            "json",
        ]
    )
    if result.returncode != 0:
        raise click.ClickException("protected transport release inventory is unavailable")
    try:
        releases = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise click.ClickException("protected transport release inventory is unavailable") from exc
    if not isinstance(releases, list):
        raise click.ClickException("protected transport release inventory is unavailable")
    if not releases:
        return _ReleaseAdmission("initial")
    if len(releases) != 1 or not isinstance(releases[0], dict):
        raise click.ClickException(
            "protected transport cutover unavailable for an existing release"
        )
    status = str(releases[0].get("status", "")).lower()
    try:
        revision = int(releases[0].get("revision", 0))
    except (TypeError, ValueError):
        raise click.ClickException("protected transport release inventory is unavailable") from None
    values = _read_json(
        ["helm", "get", "values", release_name, "--namespace", namespace, "--output", "json"],
        unavailable="protected transport release admission is unavailable",
    )
    try:
        agent_transport = values["global"]["agentTransport"]
        generation = agent_transport["generation"]
        empty_bootstrap = agent_transport["emptyBootstrap"]
    except (KeyError, TypeError):
        generation, empty_bootstrap = "", False
    admitted = generation == _PROTECTED_TRANSPORT_GENERATION and empty_bootstrap is True
    ordinary = generation == "" and empty_bootstrap is False
    if not admitted and not ordinary:
        raise click.ClickException("existing release transport admission is inconsistent")
    if ordinary and status in {"deployed", "failed", "pending-install", "pending-upgrade"}:
        return _ReleaseAdmission("ordinary")
    if status == "deployed" or status == "pending-upgrade" or (status == "failed" and revision > 1):
        return _ReleaseAdmission("protected-upgrade")
    if status in {"failed", "pending-install"}:
        return _ReleaseAdmission("protected-retry")
    raise click.ClickException("existing release state is not safe for protected upgrade")


def _live_protected_secret(*, namespace: str) -> dict[str, str]:
    document = _read_json(
        ["kubectl", "get", "secret", _SECRET_NAME, "--namespace", namespace, "--output", "json"],
        unavailable="protected transport live Secret is unavailable",
    )
    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, dict):
        raise click.ClickException("protected transport live Secret is unavailable")
    decoded: dict[str, str] = {}
    try:
        for key, value in data.items():
            decoded[str(key)] = base64.b64decode(str(value), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise click.ClickException("protected transport live Secret is unavailable") from exc
    if decoded.get("NE_AGENT_TRANSPORT_GENERATION") != _PROTECTED_TRANSPORT_GENERATION:
        raise click.ClickException("live Secret does not carry the admitted transport generation")
    return decoded


@click.group("k8s")
def k8s() -> None:
    """Production Kubernetes installer (Helm umbrella chart, NE-071a)."""


def render_overlay(bundle: Path) -> dict[str, Any]:
    """Verify ``bundle`` and build the Helm values overlay dict.

    Fail-closed: raises ``click.ClickException`` (via ``verify_bundle``) on
    any tampered byte or missing manifest — a tampered bundle can never
    produce an overlay. The overlay's ``global.configHash``/``global.hostname``
    are stamped straight from the verified manifest; ``bundle.files`` carries
    the three promoted per-module TOMLs verbatim.
    """
    manifest = verify_bundle(bundle)
    config_dir = bundle / "config"
    files = {name: (config_dir / name).read_text(encoding="utf-8") for name in _BUNDLE_TOML_FILES}
    root = _find_repo_root(Path.cwd())
    return {
        "global": {
            "configHash": manifest["config_hash"],
            "hostname": manifest["hostname"],
        },
        "bundle": {"files": files},
        # NE-PILOT-OPA-BUNDLE: the real ne-govern rego bundle, stamped for the
        # opa subchart's govern-policies ConfigMap + per-file subPath mounts.
        "opa": {"governBundle": {"files": govern_bundle_files(root=root)}},
    }


@k8s.command("render")
@click.option(
    "--bundle",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Per-customer config bundle (from `ne pilot promote`).",
)
@click.option(
    "--out",
    "out",
    required=True,
    type=click.Path(path_type=Path),
    help="Output path for the Helm values overlay (JSON — a YAML subset; `helm -f` accepts it).",
)
def render(bundle: Path, out: Path) -> None:
    """Render a verified bundle into the Helm values overlay for `helm -f`."""
    overlay = render_overlay(bundle)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(overlay, indent=2) + "\n", encoding="utf-8")
    click.echo(f"wrote {out} (configHash {overlay['global']['configHash']})")


def _run(cmd: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, check=False)


def _fail(cmd: list[str]) -> None:
    """Raise naming the failing command WITHOUT its full output (which could
    carry env dumps — C12)."""
    name = " ".join(cmd[:2]) if len(cmd) >= 2 else cmd[0]
    raise click.ClickException(f"`{name}` failed")


@k8s.command("up")
@click.option(
    "--bundle",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Per-customer config bundle (from `ne pilot promote`).",
)
@click.option(
    "--chart",
    default=_DEFAULT_CHART,
    show_default=True,
    type=click.Path(path_type=Path),
    help="Path to the umbrella chart.",
)
@click.option("--namespace", default="neutron", show_default=True)
@click.option("--release", "release_name", default="ne", show_default=True)
@click.option(
    "-f",
    "--values",
    "values_files",
    multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help="Additional -f values file(s), applied in order given (LAST wins).",
)
@click.option("--wait/--no-wait", default=True, show_default=True)
@click.option("--timeout", default="15m", show_default=True)
@click.option(
    "--admit-protected-transport",
    is_flag=True,
    help="Admit an exactly empty first install to agent-transport-v1.",
)
def up(
    bundle: Path,
    chart: Path,
    namespace: str,
    release_name: str,
    values_files: tuple[Path, ...],
    wait: bool,
    timeout: str,
    admit_protected_transport: bool,
) -> None:
    """Install/upgrade the production Helm release from a promoted bundle."""
    # (a) verify + (b) render — fail-closed BEFORE any secret resolution or
    # cluster mutation on a tampered/malformed bundle.
    overlay = render_overlay(bundle)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="ne-k8s-overlay-", delete=False
    ) as overlay_file:
        overlay_file.write(json.dumps(overlay))
        overlay_path = Path(overlay_file.name)

    try:
        # (c) Resolve secrets FIRST — before any kubectl/helm mutation. A
        # resolution failure must abort here so nothing has touched the
        # cluster yet (never leave a namespace/Secret half-applied).
        resolved = resolve_required_secrets(bundle)

        # PRE-009C: first install and a retry of that same admitted install
        # prove an externally provisioned, exactly empty database pair. A
        # healthy already-admitted release is an ordinary upgrade: preserve
        # its live protected database bindings and never reclassify its now
        # populated inventory as legacy. Every branch is read-only here.
        release = _release_admission(namespace=namespace, release_name=release_name)
        live_secret: dict[str, str] | None = None
        admission: _ProtectedBootstrapAdmission | None = None
        protected = admit_protected_transport or release.mode.startswith("protected-")
        if admit_protected_transport and release.mode == "ordinary":
            raise click.ClickException(
                "protected transport cutover unavailable for an existing ordinary release"
            )
        if release.mode == "protected-upgrade":
            live_secret = _live_protected_secret(namespace=namespace)
            admission = _ProtectedBootstrapAdmission(
                platform_database_url=_validated_database_url(
                    live_secret.get("NE_RUNTIME_SESSION_DATABASE_URL", ""),
                    database="neutron",
                    namespace=namespace,
                ),
                dbos_database_url=_validated_database_url(
                    live_secret.get("NE_API_DBOS_SYSTEM_DATABASE_URL", ""),
                    database="ne_dbos",
                    namespace=namespace,
                ),
            )
            if live_secret.get("NE_WORKER_DBOS_DATABASE_URL") != admission.dbos_database_url:
                raise click.ClickException("live Secret protected database bindings disagree")
        elif protected:
            admission = _resolve_protected_bootstrap(bundle, namespace=namespace)
            if release.mode == "protected-retry":
                live_secret = _live_protected_secret(namespace=namespace)
                expected_markers = {
                    "NE_AGENT_TRANSPORT_GENERATION": _PROTECTED_TRANSPORT_GENERATION,
                    "NE_RUNTIME_SESSION_DATABASE_URL": admission.platform_database_url,
                    "NE_API_DBOS_SYSTEM_DATABASE_URL": admission.dbos_database_url,
                    "NE_WORKER_DBOS_DATABASE_URL": admission.dbos_database_url,
                }
                if any(live_secret.get(key) != value for key, value in expected_markers.items()):
                    raise click.ClickException(
                        "failed release does not match the exact protected bootstrap markers"
                    )
            _prove_empty_external_databases(admission)
        if protected:
            assert admission is not None
            resolved.update(
                {
                    "NE_AGENT_TRANSPORT_GENERATION": _PROTECTED_TRANSPORT_GENERATION,
                    "NE_RUNTIME_SESSION_DATABASE_URL": admission.platform_database_url,
                    "NE_API_DBOS_SYSTEM_DATABASE_URL": admission.dbos_database_url,
                    "NE_WORKER_DBOS_DATABASE_URL": admission.dbos_database_url,
                }
            )

        # (d) Ensure the namespace exists (idempotent apply of a dry-run-
        # generated Namespace object) BEFORE applying a namespaced Secret.
        ns_dry_run = _run(
            ["kubectl", "create", "namespace", namespace, "--dry-run=client", "-o", "json"]
        )
        if ns_dry_run.returncode != 0:
            _fail(["kubectl", "create"])
        ns_apply = _run(["kubectl", "apply", "-f", "-"], input_text=ns_dry_run.stdout)
        if ns_apply.returncode != 0:
            _fail(["kubectl", "apply"])

        # (e) Apply the installer-mode Secret — JSON on STDIN, NEVER argv or
        # disk (C12). Helm never renders this object.
        secret_manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": _SECRET_NAME, "namespace": namespace},
            "type": "Opaque",
            "stringData": resolved,
        }
        secret_apply = _run(["kubectl", "apply", "-f", "-"], input_text=json.dumps(secret_manifest))
        if secret_apply.returncode != 0:
            _fail(["kubectl", "apply"])

        # (f) helm upgrade --install — the rendered overlay first, then any
        # caller-supplied -f values in the order given (LAST wins, per Helm's
        # own -f merge semantics).
        helm_cmd = [
            "helm",
            "upgrade",
            "--install",
            release_name,
            str(chart),
            "-n",
            namespace,
            "-f",
            str(overlay_path),
        ]
        for values_file in values_files:
            helm_cmd += ["-f", str(values_file)]
        # Last-wins values are derived solely from the admission above. Caller
        # values cannot disable the protected generation. Bundled PostgreSQL
        # remains core-scoped for Keycloak and OpenFGA.
        helm_cmd += [
            "--set-string",
            "global.agentTransport.generation="
            + (_PROTECTED_TRANSPORT_GENERATION if protected else ""),
            "--set",
            f"global.agentTransport.emptyBootstrap={str(protected).lower()}",
        ]
        if wait:
            helm_cmd += ["--wait", "--timeout", timeout]
        helm_result = _run(helm_cmd)
        if helm_result.returncode != 0:
            _fail(["helm", "upgrade"])
    finally:
        overlay_path.unlink(missing_ok=True)

    click.echo(f"deployed release {release_name!r} in namespace {namespace!r}")
