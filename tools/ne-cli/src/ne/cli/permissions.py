# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""One-shot governed principal-directory sync and ACL mapping operations.

First-seed / manual re-sync invoker for NE-FIX-SYNC-INVOKERS: runs
``PrincipalSyncService.run_once`` ONCE over the SAME governed platform recipe the
ne-worker principal-sync cron uses — this command only INVOKES the existing service
(which emits its own ``permissions.principal_sync`` audit event; C6/C7 untouched).

STARTUP DISCIPLINE (the connect.py precedent): every heavy import is LAZY inside the
command body so ``ne --help`` / ``ne check`` stay fast. The platform is composed ONLY
when there is actual work — a non-pull mode (oidc-claims / SCIM) returns a clean
exit-0 no-op without ever building it (and ``_build_platform`` is the decided test
seam, monkeypatched in tests).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import click

#: Only these modes are PULL-capable (a snapshot to fetch). oidc-claims is JIT (in
#: the ne-api process) and SCIM is push-provisioned via routes — neither pulls.
_PULL_MODES = ("entra", "fixture")

_SALESFORCE_ID_RE = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")
_SALESFORCE_PERMSETS = (
    ("dept-engineering", "Acme_Case_Reader"),
    ("dept-sales", "Acme_No_Case"),
)
_MERIDIAN_PERSONA_PERMSETS = (
    ("meridian-ops", "ops", "Meridian_Ops_Reader"),
    ("meridian-approver", "approver", "Meridian_Approver_Reader"),
    ("meridian-frontline", "frontline", "Meridian_Frontline_Reader"),
)
_MERIDIAN_M365_GROUPS = (
    ("meridian-ops", "meridian-ops"),
    ("meridian-approver", "meridian-approver"),
    ("meridian-frontline", "meridian-frontline"),
)
_ENTRA_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _build_platform() -> Any:
    """Compose the governed platform from the ambient env (the CLI test seam).

    Lazy-imports ne-api so ``ne --help`` never drags the composition graph. Tests
    monkeypatch ``ne.cli.permissions._build_platform`` to return a sqlite/stub platform.
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


@click.group()
def permissions() -> None:
    """Permissions operations."""


@permissions.command("sync")
def sync() -> None:
    """Run a governed principal-directory sync ONCE (entra / fixture mode).

    Exit 0 on success (incl. the non-pull clean no-op for oidc-claims / SCIM); exit 1
    if the sync fails. Error lines name exception CLASSES only — never payloads or
    credential-bearing detail (C12).
    """
    import asyncio

    from ne.permissions.config import PermissionsSettings
    from ne.permissions.factory import build_principal_source

    ps = PermissionsSettings()  # type: ignore[call-arg]  # env-driven, cheap (no platform)
    if ps.mode not in _PULL_MODES:
        click.echo(
            f"permissions mode {ps.mode!r} is not pull-capable (oidc-claims is JIT in "
            "ne-api; SCIM is push-provisioned via routes) — nothing to pull"
        )
        return

    platform = _build_platform()
    try:
        source = build_principal_source(platform.permissions_settings)
        report = asyncio.run(platform.perm_sync.run_once(source))
    except Exception as exc:  # never leak payloads/credentials — class name only (C12)
        click.echo(f"principal sync ERROR {type(exc).__name__}", err=True)
        raise SystemExit(1) from None
    finally:
        _dispose_platform(platform)

    click.echo(
        f"source={report.source} is_full={report.is_full} "
        f"principals_upserted={report.principals_upserted} "
        f"principals_removed={report.principals_removed} "
        f"memberships_added={report.memberships_added} "
        f"memberships_removed={report.memberships_removed}"
    )


@permissions.group("mapping")
def mapping() -> None:
    """Apply and inspect static principal-to-ACL mappings."""


@mapping.command("apply")
@click.option(
    "--path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Override NE_PERMISSIONS_MAPPING_PATH for this one-shot apply.",
)
def mapping_apply(path: Path | None) -> None:
    """Apply one ACL mapping snapshot through the governed sync service."""
    import asyncio

    from ne.permissions.config import PermissionsSettings
    from ne.permissions.mapping import AclMappingSource

    if path is None:
        path = PermissionsSettings().mapping_path  # type: ignore[call-arg]
    if path is None:
        click.echo(
            "mapping apply ERROR no mapping path configured "
            "(set NE_PERMISSIONS_MAPPING_PATH or pass --path)",
            err=True,
        )
        raise SystemExit(1)

    platform = _build_platform()
    try:
        source = AclMappingSource.from_path(path)
        report = asyncio.run(platform.perm_sync.run_once(source))
    except Exception as exc:
        # Mapping validation messages identify direction/reserved-namespace errors;
        # they contain operator-authored labels, not credentials, and must remain loud.
        click.echo(f"mapping apply ERROR {type(exc).__name__}: {exc}", err=True)
        raise SystemExit(1) from None
    finally:
        _dispose_platform(platform)

    click.echo(
        f"source={report.source} is_full={report.is_full} "
        f"principals_upserted={report.principals_upserted} "
        f"principals_removed={report.principals_removed} "
        f"memberships_added={report.memberships_added} "
        f"memberships_removed={report.memberships_removed}"
    )


@mapping.command("show")
@click.argument("principal")
@click.option(
    "--groups",
    default="",
    metavar="G1,G2",
    help="Comma-separated verified groups to include in the principal identity.",
)
def mapping_show(principal: str, groups: str) -> None:
    """Print the live ACL-label expansion for PRINCIPAL (read-only)."""
    import json

    from ne.contracts.types import Principal

    identity = Principal(
        id=principal,
        groups=tuple(group.strip() for group in groups.split(",") if group.strip()),
    )
    platform = _build_platform()
    try:
        labels = platform.expander.expand(identity)
    except Exception as exc:
        click.echo(f"mapping show ERROR {type(exc).__name__}", err=True)
        raise SystemExit(1) from None
    finally:
        _dispose_platform(platform)

    click.echo(json.dumps(sorted(labels)))


def _salesforce_id(value: object, *, field: str, prefix: str) -> str:
    if not isinstance(value, str) or not _SALESFORCE_ID_RE.fullmatch(value):
        raise ValueError(f"{field} must be a 15- or 18-character alphanumeric Salesforce id")
    if not value.startswith(prefix):
        raise ValueError(f"{field} must use Salesforce {prefix!r} id prefix, got {value!r}")
    return value


def _salesforce_mapping_document(
    manifest: Path, executive_profile_id: str | None
) -> dict[str, Any]:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Salesforce seed manifest must be a JSON object")
    permsets = payload.get("permsets")
    if not isinstance(permsets, dict):
        raise ValueError("Salesforce seed manifest field 'permsets' must be an object")

    entries: list[dict[str, Any]] = []
    for principal, permset_name in _SALESFORCE_PERMSETS:
        if permset_name not in permsets:
            raise ValueError(f"Salesforce seed manifest is missing permset {permset_name!r}")
        permset_id = _salesforce_id(
            permsets[permset_name], field=f"permset {permset_name!r}", prefix="0PS"
        )
        entries.append(
            {
                "principal": principal,
                "kind": "group",
                "grants": [f"sf-permset:{permset_id}"],
            }
        )

    if executive_profile_id is None:
        raise ValueError(
            "the Salesforce seed manifest does not record a profile id; measure the "
            "sf-profile: label on live Salesforce chunks and pass --executive-profile-id"
        )
    if executive_profile_id.startswith("sf-profile:"):
        executive_profile_id = executive_profile_id.removeprefix("sf-profile:")
    profile_id = _salesforce_id(executive_profile_id, field="executive profile id", prefix="00e")
    entries.append(
        {
            "principal": "executive",
            "kind": "group",
            "grants": [f"sf-profile:{profile_id}"],
        }
    )
    return {"name": "salesforce-demo-acl-mapping", "mappings": entries}


def _meridian_salesforce_mapping_document(manifest: Path) -> dict[str, Any]:
    """Build only the exact named-Meridian-persona mapping attested by its seed run."""
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Meridian Salesforce seed manifest must be a JSON object")
    if payload.get("tenant") != "salesforce":
        raise ValueError("Meridian seed manifest tenant must be 'salesforce'")
    if payload.get("corpus_version") != "meridian-v1":
        raise ValueError("Meridian seed manifest corpus_version must be 'meridian-v1'")

    expected_personas = {
        persona: permset for _group, persona, permset in _MERIDIAN_PERSONA_PERMSETS
    }
    if payload.get("personas") != expected_personas:
        raise ValueError(
            "Meridian seed manifest must attest exactly ops, approver, and frontline "
            "to their named Meridian permission sets"
        )

    permsets = payload.get("permsets")
    expected_permsets = set(expected_personas.values())
    if not isinstance(permsets, dict) or set(permsets) != expected_permsets:
        raise ValueError(
            "Meridian seed manifest must contain exactly the three named Meridian "
            "permission-set ids"
        )

    entries: list[dict[str, Any]] = []
    for principal, _persona, permset_name in _MERIDIAN_PERSONA_PERMSETS:
        permset_id = _salesforce_id(
            permsets[permset_name], field=f"permset {permset_name!r}", prefix="0PS"
        )
        entries.append(
            {
                "principal": principal,
                "kind": "group",
                "grants": [f"sf-permset:{permset_id}"],
            }
        )
    return {"name": "meridian-salesforce-demo-acl-mapping", "mappings": entries}


def _meridian_m365_mapping_document(principals: Path) -> dict[str, Any]:
    """Build the three exact persona-group grants from the owner-staged ids."""
    payload = json.loads(principals.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Meridian principals file must be a JSON object")
    m365 = payload.get("m365")
    if not isinstance(m365, dict):
        raise ValueError("Meridian principals file must contain an m365 object")
    groups = m365.get("groups")
    expected = {persona for _principal, persona in _MERIDIAN_M365_GROUPS}
    if not isinstance(groups, dict) or set(groups) != expected:
        raise ValueError(
            "Meridian principals file must contain exactly the meridian-ops, "
            "meridian-approver, and meridian-frontline M365 group ids"
        )

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for principal, persona in _MERIDIAN_M365_GROUPS:
        object_id = groups[persona]
        if not isinstance(object_id, str) or not _ENTRA_OBJECT_ID_RE.fullmatch(object_id):
            raise ValueError(
                f"Meridian M365 group {persona!r} must be one canonical lowercase Entra object id"
            )
        if object_id in seen:
            raise ValueError("Meridian M365 persona group ids must be distinct")
        seen.add(object_id)
        entries.append(
            {
                "principal": principal,
                "kind": "group",
                "grants": [f"m365-group:{object_id}"],
            }
        )
    return {"name": "meridian-m365-demo-acl-mapping", "mappings": entries}


def _write_mapping_atomically(path: Path, body: str, *, force: bool) -> None:
    if (path.exists() or path.is_symlink()) and not force:
        raise FileExistsError(f"output already exists (pass --force to replace it): {path}")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, path)
        else:
            # Hard-link publication is atomic and refuses a destination created
            # between the existence check above and this operation.
            os.link(temporary, path)
            temporary.unlink()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _print_salesforce_mapping_summary(document: dict[str, Any]) -> None:
    grants_by_principal = {entry["principal"]: entry["grants"] for entry in document["mappings"]}
    for principal in ("dept-engineering", "dept-sales", "executive", "dept-legal"):
        click.echo(f"{principal} -> {json.dumps(grants_by_principal.get(principal, []))}", err=True)


@mapping.command("generate-salesforce")
@click.option(
    "--manifest",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Salesforce acme-seed manifest.",
)
@click.option(
    "--executive-profile-id",
    help="Measured Salesforce profile id (bare 00e... or sf-profile:00e...).",
)
@click.option("--out", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--force", is_flag=True, help="Replace an existing --out atomically.")
def mapping_generate_salesforce(
    manifest: Path | None,
    executive_profile_id: str | None,
    out: Path | None,
    force: bool,
) -> None:
    """Generate the named-demo-persona Salesforce ACL mapping."""
    from ne.permissions.mapping import AclMappingSource

    if manifest is None:
        manifest = Path.home() / ".ne" / "acme-seed" / "salesforce.json"
    try:
        document = _salesforce_mapping_document(manifest, executive_profile_id)
        body = json.dumps(document, indent=2, sort_keys=True) + "\n"
        # Validate the exact serialized bytes that will be emitted, before any write.
        AclMappingSource.from_data(json.loads(body))
        if out is not None:
            _write_mapping_atomically(out, body, force=force)
    except Exception as exc:
        click.echo(f"mapping generate-salesforce ERROR {type(exc).__name__}: {exc}", err=True)
        raise SystemExit(1) from None

    if out is None:
        click.echo(body, nl=False)
    else:
        click.echo(f"wrote {out}")
    _print_salesforce_mapping_summary(document)


@mapping.command("generate-meridian-salesforce")
@click.option(
    "--manifest",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Meridian Salesforce seed manifest (defaults outside the repository).",
)
@click.option("--out", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--force", is_flag=True, help="Replace an existing --out atomically.")
def mapping_generate_meridian_salesforce(
    manifest: Path | None,
    out: Path | None,
    force: bool,
) -> None:
    """Generate exact Meridian persona grants, or keep Salesforce invisible."""
    from ne.permissions.mapping import AclMappingSource

    if manifest is None:
        manifest = Path.home() / ".ne" / "meridian-seed" / "salesforce.json"
    try:
        document = _meridian_salesforce_mapping_document(manifest)
        body = json.dumps(document, indent=2, sort_keys=True) + "\n"
        # Validate the exact bytes before publication. In particular, this pins
        # product-principal -> source-native-grant direction.
        AclMappingSource.from_data(json.loads(body))
        if out is not None:
            _write_mapping_atomically(out, body, force=force)
    except Exception as exc:
        click.echo(
            "mapping generate-meridian-salesforce ERROR "
            f"{type(exc).__name__}: {exc}. Mapping is unattested; Salesforce content "
            "remains invisible. Use the write-back-only fallback.",
            err=True,
        )
        raise SystemExit(1) from None

    if out is None:
        click.echo(body, nl=False)
    else:
        click.echo(f"wrote {out}")
    grants = {entry["principal"]: entry["grants"] for entry in document["mappings"]}
    for principal, _persona, _permset in _MERIDIAN_PERSONA_PERMSETS:
        click.echo(f"{principal} -> {json.dumps(grants[principal])}", err=True)


@mapping.command("generate-meridian-m365")
@click.option(
    "--principals",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Owner-staged Meridian principals file (defaults outside the repository).",
)
@click.option("--out", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--force", is_flag=True, help="Replace an existing --out atomically.")
def mapping_generate_meridian_m365(
    principals: Path | None,
    out: Path | None,
    force: bool,
) -> None:
    """Generate exact Meridian persona grants to raw M365 ACL group ids."""
    from ne.permissions.mapping import AclMappingSource

    if principals is None:
        principals = Path.home() / ".ne" / "meridian-seed" / "principals.json"
    try:
        document = _meridian_m365_mapping_document(principals)
        body = json.dumps(document, indent=2, sort_keys=True) + "\n"
        AclMappingSource.from_data(json.loads(body))
        if out is not None:
            _write_mapping_atomically(out, body, force=force)
    except Exception as exc:
        click.echo(
            "mapping generate-meridian-m365 ERROR "
            f"{type(exc).__name__}: {exc}. Mapping is unattested; M365 content "
            "remains invisible.",
            err=True,
        )
        raise SystemExit(1) from None

    if out is None:
        click.echo(body, nl=False)
    else:
        click.echo(f"wrote {out}")
    click.echo("generated 3 Meridian M365 persona grants", err=True)
