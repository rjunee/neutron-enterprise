# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SharePoint seeder that proves target ownership before every mutation.

The owner-run Meridian sequence (v3 seed -> governed sync -> v4 supersession
-> governed incremental sync) is only honest when the seeder can never adopt,
overwrite, or delete a lookalike target. Every M365 mutation therefore passes
four gates in a fixed order: the operator-supplied persistence generation id,
static manifest/config comparison (zero network), read-only drive resolution
compared against the saved descriptor, and exact live item/folder id equality.
Every preflight failure refuses before the first POST/PUT/DELETE/invite, and no
failure path saves a replacement manifest or mutates the caller's prior one.
There is no automatic rollback: a crash after ``/Meridian`` folder creation but
before the manifest save leaves an unknown live folder that the next run blocks
on rather than overwrites or deletes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from ne.acme_seed.m365 import SeedGraphClient as _BaseSeedGraphClient
from ne.connect.errors import GraphSyncError
from ne.connect.m365.config import M365ConnectorSettings
from ne.connect.m365.graph import Client, Transport
from ne.connect.m365.operational_graph import (
    OperationalGraphItemBinding,
    manifest_record_key,
    validate_binding_set,
)
from ne.meridian_seed.errors import SeedPreflightError, SeedSafetyError
from ne.meridian_seed.manifest import M365SeedTarget, SeedManifest, SeedStage, StageHistoryEntry
from ne.meridian_seed.personas import PrincipalsMap
from ne.meridian_seed.slice import OperationalGraphDocument, SliceDocument, SliceSelection

__all__ = ["M365CheckResult", "M365Seeder", "SeedGraphClient"]

_FOLDER = "Meridian"
_SAFE_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()'-]{0,127}$")
_GENERATION_PART = r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}"
_V3_ARTIFACT = "art-dispute-sop-v3"
_LEGACY_ADOPTION_MARKER = "legacy-m365-target-adopted"
_RETRYABLE = frozenset({429, 503, 504})

M365CheckStatus = Literal[
    "safe_new",
    "blocked_unowned_folder",
    "legacy_refused",
    "mismatched",
    "drive_mismatch",
    "owned",
    "folder_missing",
    "folder_unowned",
]


class SeedGraphClient(_BaseSeedGraphClient):
    """Seeder writes with the connector's guard/token/throttle and strict 2xx truth.

    The shared Acme helper predates this ownership boundary and treats a 3xx PUT
    or DELETE as success because only ``status >= 400`` is rejected. That is not
    safe for a lifecycle manifest: a redirect-shaped response must never record
    an upload or erase the local ownership record. Keep the same guard-first,
    token, throttle, and bounded retry behavior while admitting only real 2xx.
    """

    async def put_bytes(
        self, client: Client, url: str, body: bytes, content_type: str
    ) -> dict[str, Any]:
        self._guard_graph_url(url)
        token = await self._get_token(client)
        for attempt in range(1, self._settings.max_attempts + 1):
            await self._throttle.acquire()
            response = await client.put(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
                content=body,
            )
            status = response.status_code
            if status < 200:
                raise GraphSyncError(f"Graph upload returned HTTP {status}", status=status)
            if 300 <= status < 400:
                raise GraphSyncError(f"Graph upload returned HTTP redirect {status}", status=status)
            if status in _RETRYABLE:
                if attempt == self._settings.max_attempts:
                    raise GraphSyncError(
                        f"Graph returned HTTP {status} after {attempt} attempts", status=status
                    )
                await self._throttle.wait_retry(
                    attempt,
                    response.headers.get("Retry-After"),
                    self._settings.retry_after_cap_seconds,
                )
                continue
            if status >= 400:
                raise GraphSyncError(f"Graph returned HTTP {status}", status=status)
            return self._json_object(response, context="Graph upload endpoint")
        raise GraphSyncError("exhausted Graph retries")  # pragma: no cover

    async def delete(self, client: Client, url: str) -> None:
        self._guard_graph_url(url)
        token = await self._get_token(client)
        for attempt in range(1, self._settings.max_attempts + 1):
            await self._throttle.acquire()
            response = await client.request(
                "DELETE", url, headers={"Authorization": f"Bearer {token}"}
            )
            status = response.status_code
            if status < 200:
                raise GraphSyncError(f"Graph delete returned HTTP {status}", status=status)
            if 300 <= status < 400:
                raise GraphSyncError(f"Graph delete returned HTTP redirect {status}", status=status)
            if status in _RETRYABLE:
                if attempt == self._settings.max_attempts:
                    raise GraphSyncError(
                        f"Graph returned HTTP {status} after {attempt} attempts", status=status
                    )
                await self._throttle.wait_retry(
                    attempt,
                    response.headers.get("Retry-After"),
                    self._settings.retry_after_cap_seconds,
                )
                continue
            if status in {200, 204, 404}:
                return
            if status >= 400:
                raise GraphSyncError(f"Graph returned HTTP {status}", status=status)
            return
        raise GraphSyncError("exhausted Graph retries")  # pragma: no cover


def _checked_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or len(path.parts) > 2:
        raise SeedSafetyError(f"unsafe SharePoint fixture path {value!r}")
    if any(part in {".", ".."} or not _SAFE_PATH_PART.fullmatch(part) for part in path.parts):
        raise SeedSafetyError(f"unsafe SharePoint fixture path {value!r}")
    return path.as_posix()


def _checked_generation(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(_GENERATION_PART, value):
        raise SeedPreflightError(
            "persistence generation id must be 1-128 ASCII chars starting with a "
            "letter/digit; it is chosen once, recorded externally, and never regenerated"
        )
    return value


def _checked_graph_item_id(item_id: object, context: str) -> str:
    if (
        not isinstance(item_id, str)
        or not item_id
        or item_id != item_id.strip()
        or not item_id.isascii()
        or any(char.isspace() or char in "/\\?#" for char in item_id)
        or item_id in {".", ".."}
    ):
        raise SeedPreflightError(f"{context} returned a malformed Graph item id")
    return item_id


def _item_id(payload: object, context: str) -> str:
    if not isinstance(payload, dict):
        raise SeedPreflightError(f"{context} returned a malformed Graph response")
    return _checked_graph_item_id(payload.get("id"), context)


def _folder_item_id(payload: object, context: str) -> str:
    item_id = _item_id(payload, context)
    assert isinstance(payload, dict)
    if payload.get("name") != _FOLDER or not isinstance(payload.get("folder"), dict):
        raise SeedPreflightError(f"{context} returned a malformed Meridian folder item")
    return item_id


def _document_item_id(payload: object, context: str, expected_path: str) -> str:
    item_id = _item_id(payload, context)
    assert isinstance(payload, dict)
    expected_name = PurePosixPath(expected_path).name
    if payload.get("name") != expected_name:
        raise SeedPreflightError(
            f"{context} name does not equal the exact manifest-bound SharePoint path"
        )
    return item_id


def _url_segment(value: str) -> str:
    return quote(value, safe="")


def _url_sharepoint_path(value: str) -> str:
    checked = _checked_path(value)
    return "/".join(_url_segment(part) for part in PurePosixPath(checked).parts)


def _saved_sop_binding(manifest: SeedManifest) -> tuple[str, str]:
    item_id = manifest.docs.get(_V3_ARTIFACT, "")
    path = manifest.document_paths.get(_V3_ARTIFACT, "")
    if not item_id or not path:
        raise SeedPreflightError("manifest lacks the exact owned v3 SOP item record")
    _checked_graph_item_id(item_id, "manifest-owned v3 SOP")
    _checked_path(path)
    stages = tuple(entry.stage for entry in manifest.stage_history)
    if "supersession" in stages:
        v4_item_id = manifest.docs.get("art-dispute-sop-v4", "")
        v4_path = manifest.document_paths.get("art-dispute-sop-v4", "")
        if v4_item_id != item_id or v4_path != path:
            raise SeedPreflightError(
                "completed supersession does not preserve the exact manifest-owned SOP item/path"
            )
    return item_id, path


class M365CheckResult(BaseModel):
    """Class-only, credential-free verdict of the read-only M365 preflight."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: M365CheckStatus
    completed_stages: tuple[SeedStage, ...] = ()
    detail: str = ""


class M365Seeder:
    """Idempotently upload every Meridian artifact under an owned ``/Meridian``."""

    def __init__(
        self,
        settings: M365ConnectorSettings,
        principals: PrincipalsMap,
        *,
        transport: Transport | None = None,
    ) -> None:
        principals.require_m365()
        if len(settings.site_ids) != 1:
            raise SeedSafetyError("M365 seeder requires exactly one explicit SharePoint site id")
        self._principals = principals
        self._settings = settings
        self._site_id = settings.site_ids[0]
        self._client = SeedGraphClient(settings, transport=transport)

    def _grant_recipients(self, document: SliceDocument) -> list[str]:
        return [
            self._principals.m365_group_id(persona) for persona in document.acl.allowed_personas
        ]

    def _operational_grant_recipients(self, document: OperationalGraphDocument) -> list[str]:
        return [
            self._principals.m365_group_id(persona) for persona in document.acl.allowed_personas
        ]

    def _static_target_mismatch(self, target: M365SeedTarget) -> str | None:
        if target.graph_base_url.rstrip("/") != self._client.graph_base.rstrip("/"):
            return "Graph cloud base URL"
        if target.tenant_id != self._settings.tenant_id:
            return "Entra tenant id"
        if target.site_id != self._site_id:
            return "SharePoint site id"
        return None

    @staticmethod
    def _manifest_target_mismatch(manifest: SeedManifest, target: M365SeedTarget) -> str | None:
        if manifest.tenant != target.tenant:
            return "tenant kind"
        if manifest.corpus_version != target.corpus_version:
            return "corpus version"
        return None

    @staticmethod
    def _require_owned_target(prior_manifest: SeedManifest) -> M365SeedTarget:
        target = prior_manifest.m365_target
        if target is None:
            raise SeedPreflightError(
                "prior M365 manifest predates target binding and cannot authorize any "
                "mutation; archive it and start a fresh initial run on a verified-clean target"
            )
        _checked_graph_item_id(target.drive_id, "manifest-bound drive")
        _checked_graph_item_id(target.folder_item_id, "manifest-bound Meridian folder")
        return target

    async def _resolve_drive(self, http: Client) -> str:
        payload = await self._client.get_json(
            http, f"{self._client.graph_base}/sites/{_url_segment(self._site_id)}/drive"
        )
        return _item_id(payload, "default drive resolution")

    async def _probe_folder(self, http: Client, drive_id: str) -> str | None:
        try:
            payload = await self._client.get_json(
                http,
                f"{self._client.graph_base}/drives/{_url_segment(drive_id)}"
                f"/root:/{_url_segment(_FOLDER)}",
            )
        except GraphSyncError as exc:
            if exc.status == 404:
                return None
            raise
        return _folder_item_id(payload, f"/{_FOLDER} folder probe")

    async def _require_live_sop(
        self, http: Client, drive_id: str, saved_path: str, saved_item_id: str
    ) -> None:
        try:
            payload = await self._client.get_json(
                http,
                f"{self._client.graph_base}/drives/{_url_segment(drive_id)}"
                f"/root:/{_url_segment(_FOLDER)}/{_url_sharepoint_path(saved_path)}",
            )
        except GraphSyncError as exc:
            if exc.status == 404:
                raise SeedPreflightError(
                    "the manifest-owned v3 SOP document is missing from the live "
                    "folder; refusing supersession"
                ) from exc
            raise
        live_id = _document_item_id(payload, "v3 SOP item resolution", saved_path)
        if live_id != saved_item_id:
            raise SeedPreflightError(
                "the live SOP item id does not equal the manifest-owned v3 item id; "
                "refusing supersession"
            )

    async def _require_owned_folder(
        self, http: Client, drive_id: str, target: M365SeedTarget
    ) -> None:
        live_folder = await self._probe_folder(http, drive_id)
        if live_folder is None:
            raise SeedPreflightError(
                "the manifest-owned /Meridian folder is missing; refusing supersession"
            )
        if live_folder != target.folder_item_id:
            raise SeedPreflightError(
                "the live /Meridian folder id does not equal the manifest-owned folder id; "
                "refusing supersession"
            )

    def _legacy_bindings(self, selection: SliceSelection, manifest: SeedManifest) -> dict[str, str]:
        documents = selection.documents_for_stage("initial") + selection.documents_for_stage(
            "supersession"
        )
        expected_artifacts = {document.artifact_id for document in documents}
        if len(documents) != 12 or set(manifest.docs) != expected_artifacts:
            raise SeedPreflightError(
                "legacy M365 manifest must contain exactly all 12 canonical document bindings"
            )
        if (
            set(manifest.document_paths) != expected_artifacts
            or set(manifest.grants) != expected_artifacts
        ):
            raise SeedPreflightError(
                "legacy M365 manifest must contain exact path and grant receipts "
                "for all 12 documents"
            )
        expected_live: dict[str, str] = {}
        for document in documents:
            artifact = document.artifact_id
            path = _checked_path(document.sharepoint_path)
            item_id = _checked_graph_item_id(manifest.docs[artifact], f"legacy {artifact}")
            if manifest.document_paths[artifact] != path:
                raise SeedPreflightError(
                    "legacy M365 manifest path differs from the canonical corpus"
                )
            expected_grants = self._grant_recipients(document)
            if manifest.grants[artifact] != expected_grants:
                raise SeedPreflightError(
                    "legacy M365 manifest grant differs from the canonical ACL"
                )
            prior = expected_live.setdefault(path, item_id)
            if prior != item_id:
                raise SeedPreflightError(
                    "legacy v3/v4 bindings do not identify one stable SharePoint item"
                )
        if len(expected_live) != 11:
            raise SeedPreflightError(
                "legacy M365 manifest does not resolve to exactly 11 live items"
            )
        if tuple(entry.stage for entry in manifest.stage_history) != ("initial", "supersession"):
            raise SeedPreflightError(
                "legacy M365 manifest must preserve the exact initial/supersession history"
            )
        if manifest.records or manifest.operational_graph or manifest.m365_groups:
            raise SeedPreflightError(
                "legacy M365 manifest already contains non-legacy graph receipts"
            )
        return expected_live

    async def adopt_legacy(
        self,
        selection: SliceSelection,
        *,
        prior_manifest: SeedManifest,
        generation_id: str,
    ) -> SeedManifest:
        """Read-only prove and bind one exact legacy M365 target."""

        _checked_generation(generation_id)
        if prior_manifest.m365_target is not None:
            target = self._require_owned_target(prior_manifest)
            mismatch = self._manifest_target_mismatch(
                prior_manifest, target
            ) or self._static_target_mismatch(target)
            if (
                mismatch is not None
                or target.generation_id != generation_id
                or prior_manifest.warnings.count(_LEGACY_ADOPTION_MARKER) != 1
            ):
                raise SeedPreflightError(
                    "bound M365 target or generation does not match; refusing replay"
                )
            return prior_manifest.model_copy(deep=True)
        if (
            prior_manifest.tenant != "m365"
            or prior_manifest.corpus_version != selection.corpus_version
        ):
            raise SeedPreflightError("legacy manifest does not belong to this Meridian M365 corpus")
        expected_live = self._legacy_bindings(selection, prior_manifest)
        async with self._client.session() as http:
            drive_id = await self._resolve_drive(http)
            folder_id = await self._probe_folder(http, drive_id)
            if folder_id is None:
                raise SeedPreflightError("legacy /Meridian folder is missing")
            payload = await self._client.get_json(
                http,
                f"{self._client.graph_base}/drives/{_url_segment(drive_id)}"
                f"/items/{_url_segment(folder_id)}/children",
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
            raise SeedPreflightError("legacy folder listing returned a malformed Graph response")
        if payload.get("@odata.nextLink"):
            raise SeedPreflightError("legacy folder listing is ambiguous or incomplete")
        actual: dict[str, str] = {}
        for row in payload["value"]:
            if not isinstance(row, dict) or not isinstance(row.get("file"), dict):
                raise SeedPreflightError(
                    "legacy folder contains a malformed or surplus non-document item"
                )
            name = row.get("name")
            if not isinstance(name, str) or name in actual:
                raise SeedPreflightError("legacy folder contains an ambiguous document name")
            actual[_checked_path(name)] = _item_id(row, "legacy folder child")
        if actual != expected_live:
            raise SeedPreflightError(
                "legacy folder contents do not exactly match the 12 manifest-owned "
                "document bindings"
            )
        adopted = prior_manifest.model_copy(deep=True)
        adopted.m365_target = M365SeedTarget(
            tenant="m365",
            corpus_version=selection.corpus_version,
            graph_base_url=self._client.graph_base,
            tenant_id=self._settings.tenant_id,
            site_id=self._site_id,
            drive_id=drive_id,
            folder_item_id=folder_id,
            generation_id=generation_id,
        )
        adopted.m365_groups = {
            persona: self._principals.m365_group_id(persona)
            for persona in ("ops", "approver", "frontline")
        }
        adopted.warnings.append(_LEGACY_ADOPTION_MARKER)
        return adopted

    async def push(
        self,
        selection: SliceSelection,
        *,
        stage: SeedStage,
        prior_manifest: SeedManifest | None = None,
        generation_id: str,
    ) -> SeedManifest:
        _checked_generation(generation_id)
        completed_stages: set[str] = set()
        prior_target: M365SeedTarget | None = None
        saved_v3_item_id = ""
        saved_v3_path = ""
        if prior_manifest is not None:
            if (
                prior_manifest.tenant != "m365"
                or prior_manifest.corpus_version != selection.corpus_version
            ):
                raise SeedPreflightError(
                    "prior manifest does not belong to this Meridian M365 corpus"
                )
            prior_target = self._require_owned_target(prior_manifest)
            binding_mismatch = self._manifest_target_mismatch(prior_manifest, prior_target)
            if binding_mismatch is not None:
                raise SeedPreflightError(
                    f"prior manifest target binding {binding_mismatch} does not match its "
                    "outer manifest; refusing"
                )
            mismatch = self._static_target_mismatch(prior_target)
            if mismatch is not None:
                raise SeedPreflightError(
                    f"prior manifest {mismatch} does not match this configuration; refusing"
                )
            if prior_target.generation_id != generation_id:
                raise SeedPreflightError(
                    "generation id does not match the manifest-bound persistence generation"
                )
            history = tuple(entry.stage for entry in prior_manifest.stage_history)
            if history not in {
                ("initial",),
                ("initial", "operational_graph"),
                ("initial", "supersession"),
                ("initial", "operational_graph", "supersession"),
                ("initial", "supersession", "operational_graph"),
            }:
                raise SeedPreflightError(
                    "prior manifest stage history is not the exact supported M365 lifecycle"
                )
            saved_v3_item_id, saved_v3_path = _saved_sop_binding(prior_manifest)
            expected_v3 = next(
                (
                    document
                    for document in selection.documents_for_stage("initial")
                    if document.artifact_id == _V3_ARTIFACT
                ),
                None,
            )
            if expected_v3 is None or saved_v3_path != _checked_path(expected_v3.sharepoint_path):
                raise SeedPreflightError(
                    "manifest-owned v3 SOP path does not match the canonical corpus artifact"
                )
            completed_stages = {entry.stage for entry in prior_manifest.stage_history}
        if stage == "initial" and "supersession" in completed_stages:
            raise SeedPreflightError(
                "refusing initial-stage downgrade of a completed v4 supersession to v3"
            )
        if stage in completed_stages:
            assert prior_manifest is not None
            return prior_manifest.model_copy(deep=True)
        if stage == "supersession" and "initial" not in completed_stages:
            raise SeedPreflightError(
                "M365 supersession requires a completed initial stage; refusing before network I/O"
            )
        if stage == "operational_graph" and "initial" not in completed_stages:
            raise SeedPreflightError(
                "M365 operational_graph requires a completed initial stage; refusing "
                "before network I/O"
            )
        if (
            stage == "operational_graph"
            and "supersession" in completed_stages
            and prior_manifest is not None
            and prior_manifest.warnings.count(_LEGACY_ADOPTION_MARKER) != 1
        ):
            raise SeedPreflightError(
                "M365 operational_graph may follow supersession only for an exactly adopted "
                "legacy target"
            )
        if stage == "initial" and prior_manifest is not None:
            raise SeedPreflightError(
                "initial stage cannot restart over a target-bound manifest without a "
                "completed initial run; resolve the unknown live target state manually"
            )
        now = datetime.now(UTC).isoformat()
        manifest = (
            prior_manifest.model_copy(deep=True)
            if prior_manifest is not None
            else SeedManifest(
                tenant="m365",
                seeded_at=now,
                corpus_version=selection.corpus_version,
                personas={
                    persona: self._principals.m365_user_id(persona)
                    for persona in ("ops", "approver", "frontline")
                },
                m365_groups={
                    persona: self._principals.m365_group_id(persona)
                    for persona in ("ops", "approver", "frontline")
                },
            )
        )
        manifest.m365_groups = {
            persona: self._principals.m365_group_id(persona)
            for persona in ("ops", "approver", "frontline")
        }
        stage_documents: tuple[SliceDocument, ...] = (
            selection.documents_for_stage(stage) if stage != "operational_graph" else ()
        )
        if stage == "supersession" and len(stage_documents) != 1:
            raise SeedPreflightError("supersession stage must contain exactly the v4 SOP artifact")
        # Validate every path before the first network request.
        for document in stage_documents:
            _checked_path(document.sharepoint_path)
        if stage == "supersession":
            assert prior_manifest is not None
            supersession_document = next(iter(stage_documents), None)
            assert supersession_document is not None
            if _checked_path(supersession_document.sharepoint_path) != saved_v3_path:
                raise SeedPreflightError(
                    "v4 artifact path differs from the manifest-bound v3 path; refusing"
                )
        base = self._client.graph_base
        async with self._client.session() as http:
            resolved_drive = await self._resolve_drive(http)
            if prior_target is not None and resolved_drive != prior_target.drive_id:
                raise SeedPreflightError(
                    "resolved default drive does not match the manifest-bound drive; refusing"
                )
            if stage == "initial":
                existing = await self._probe_folder(http, resolved_drive)
                if existing is not None:
                    raise SeedPreflightError(
                        "/Meridian already exists on the resolved drive and no manifest "
                        "proves ownership; refusing instead of adopting or overwriting it"
                    )
                try:
                    created = await self._client.post_json(
                        http,
                        f"{base}/drives/{_url_segment(resolved_drive)}/root/children",
                        {
                            "name": _FOLDER,
                            "folder": {},
                            "@microsoft.graph.conflictBehavior": "fail",
                        },
                    )
                except GraphSyncError as exc:
                    if exc.status == 409:
                        raise SeedPreflightError(
                            "/Meridian appeared during the create race and ownership is unknown; "
                            "refusing before document upload"
                        ) from exc
                    raise
                manifest.m365_target = M365SeedTarget(
                    tenant="m365",
                    corpus_version=selection.corpus_version,
                    graph_base_url=base,
                    tenant_id=self._settings.tenant_id,
                    site_id=self._site_id,
                    drive_id=resolved_drive,
                    folder_item_id=_folder_item_id(created, "Meridian folder creation"),
                    generation_id=generation_id,
                )
            else:
                assert prior_target is not None
                await self._require_owned_folder(http, resolved_drive, prior_target)
                await self._require_live_sop(http, resolved_drive, saved_v3_path, saved_v3_item_id)
            for document in stage_documents:
                sharepoint_path = _checked_path(document.sharepoint_path)
                item = await self._client.put_bytes(
                    http,
                    f"{base}/drives/{_url_segment(resolved_drive)}"
                    f"/root:/{_url_segment(_FOLDER)}/{_url_sharepoint_path(sharepoint_path)}"
                    ":/content",
                    document.content,
                    document.media_type,
                )
                item_id = _document_item_id(item, f"{sharepoint_path} upload", sharepoint_path)
                if stage == "supersession" and item_id != saved_v3_item_id:
                    raise SeedPreflightError(
                        "v4 upload returned a different item id than the manifest-owned "
                        "stable item; refusing to record supersession"
                    )
                recipients = self._grant_recipients(document)
                await self._client.post_json(
                    http,
                    f"{base}/drives/{_url_segment(resolved_drive)}"
                    f"/items/{_url_segment(item_id)}/invite",
                    {
                        "recipients": [{"objectId": recipient} for recipient in recipients],
                        "requireSignIn": True,
                        "sendInvitation": False,
                        "roles": ["read"],
                    },
                )
                # Record only after BOTH the PUT and the ACL invite succeeded; the
                # manifest reaches disk only after the whole run succeeds anyway.
                manifest.docs[document.artifact_id] = item_id
                manifest.document_paths[document.artifact_id] = sharepoint_path
                manifest.grants[document.artifact_id] = recipients
            if stage == "operational_graph":
                if len(selection.operational_graph) != 45:
                    raise SeedPreflightError(
                        "canonical Meridian operational graph must contain exactly 45 items"
                    )
                bindings: list[OperationalGraphItemBinding] = []
                for document in selection.operational_graph:
                    path = _checked_path(document.filename)
                    item = await self._client.put_bytes(
                        http,
                        f"{base}/drives/{_url_segment(resolved_drive)}"
                        f"/root:/{_url_segment(_FOLDER)}/{_url_sharepoint_path(path)}:/content",
                        document.content,
                        document.media_type,
                    )
                    item_id = _document_item_id(item, f"{path} upload", path)
                    recipients = self._operational_grant_recipients(document)
                    await self._client.post_json(
                        http,
                        f"{base}/drives/{_url_segment(resolved_drive)}"
                        f"/items/{_url_segment(item_id)}/invite",
                        {
                            "recipients": [{"objectId": recipient} for recipient in recipients],
                            "requireSignIn": True,
                            "sendInvitation": False,
                            "roles": ["read"],
                        },
                    )
                    key = manifest_record_key(document.entity_class, document.record_id)
                    manifest.records[key] = item_id
                    manifest.document_paths[key] = path
                    manifest.grants[key] = recipients
                    bindings.append(
                        OperationalGraphItemBinding(
                            record_id=document.record_id,
                            entity_class=document.entity_class,
                            item_id=item_id,
                            path=path,
                            acl=tuple(recipients),
                            edges=document.edges,
                        )
                    )
                validate_binding_set(bindings)
                manifest.operational_graph = tuple(bindings)
        if stage not in completed_stages:
            manifest.stage_history.append(
                StageHistoryEntry(stage=stage, completed_at=datetime.now(UTC).isoformat())
            )
        return manifest

    async def check(
        self,
        selection: SliceSelection,
        *,
        prior_manifest: SeedManifest | None = None,
        generation_id: str,
    ) -> M365CheckResult:
        """Read-only preflight: token flow + GETs only, never a mutation."""
        _checked_generation(generation_id)
        if prior_manifest is not None and prior_manifest.m365_target is None:
            return M365CheckResult(
                status="legacy_refused",
                detail="prior manifest predates target binding and cannot authorize mutation",
            )
        if prior_manifest is not None and (
            prior_manifest.tenant != "m365"
            or prior_manifest.corpus_version != selection.corpus_version
        ):
            return M365CheckResult(
                status="mismatched",
                detail="prior manifest tenant or corpus does not match this M365 slice",
            )
        if prior_manifest is None:
            async with self._client.session() as http:
                resolved_drive = await self._resolve_drive(http)
                live_folder = await self._probe_folder(http, resolved_drive)
            if live_folder is None:
                return M365CheckResult(
                    status="safe_new",
                    detail="no prior manifest and /Meridian is absent on the resolved drive",
                )
            return M365CheckResult(
                status="blocked_unowned_folder",
                detail="/Meridian already exists with no manifest proving ownership",
            )
        assert prior_manifest.m365_target is not None
        target = prior_manifest.m365_target
        binding_mismatch = self._manifest_target_mismatch(prior_manifest, target)
        if binding_mismatch is not None:
            return M365CheckResult(
                status="mismatched",
                detail=(
                    f"manifest target binding {binding_mismatch} differs from its outer manifest"
                ),
            )
        mismatch = self._static_target_mismatch(target)
        if mismatch is not None:
            return M365CheckResult(
                status="mismatched", detail=f"configured {mismatch} differs from the manifest"
            )
        if target.generation_id != generation_id:
            return M365CheckResult(
                status="mismatched",
                detail="generation id differs from the manifest-bound persistence generation",
            )
        stages: tuple[SeedStage, ...] = tuple(entry.stage for entry in prior_manifest.stage_history)
        ordinary_stages = {
            ("initial",),
            ("initial", "operational_graph"),
            ("initial", "supersession"),
            ("initial", "operational_graph", "supersession"),
        }
        adopted_operational = stages == ("initial", "supersession", "operational_graph")
        if stages not in ordinary_stages and not adopted_operational:
            return M365CheckResult(
                status="mismatched",
                completed_stages=stages,
                detail="manifest stage history is not the exact supported M365 lifecycle",
            )
        if adopted_operational:
            if prior_manifest.warnings.count(_LEGACY_ADOPTION_MARKER) != 1:
                return M365CheckResult(
                    status="mismatched",
                    completed_stages=stages,
                    detail="adopted operational lifecycle lacks its exact adoption marker",
                )
            canonical = {
                (document.entity_class, document.record_id): (
                    _checked_path(document.filename),
                    tuple(self._operational_grant_recipients(document)),
                    document.edges,
                )
                for document in selection.operational_graph
            }
            receipts: dict[tuple[str, str], tuple[str, tuple[str, ...], tuple[Any, ...]]] = {}
            try:
                for binding in prior_manifest.operational_graph:
                    _checked_graph_item_id(binding.item_id, "operational graph receipt")
                    key = (binding.entity_class, binding.record_id)
                    if key in receipts:
                        raise SeedPreflightError("duplicate operational graph receipt")
                    receipts[key] = (binding.path, binding.acl, binding.edges)
            except (SeedPreflightError, SeedSafetyError):
                receipts = {}
            if len(canonical) != 45 or receipts != canonical:
                return M365CheckResult(
                    status="mismatched",
                    completed_stages=stages,
                    detail="adopted lifecycle lacks the exact 45 canonical operational bindings",
                )
        try:
            saved_v3_item_id, saved_v3_path = _saved_sop_binding(prior_manifest)
        except (SeedPreflightError, SeedSafetyError) as exc:
            return M365CheckResult(
                status="mismatched",
                completed_stages=stages,
                detail=str(exc),
            )
        expected_v3 = next(
            (
                document
                for document in selection.documents_for_stage("initial")
                if document.artifact_id == _V3_ARTIFACT
            ),
            None,
        )
        if expected_v3 is None or saved_v3_path != _checked_path(expected_v3.sharepoint_path):
            return M365CheckResult(
                status="mismatched",
                completed_stages=stages,
                detail="manifest-owned v3 SOP path differs from the canonical corpus artifact",
            )
        async with self._client.session() as http:
            resolved_drive = await self._resolve_drive(http)
            if resolved_drive != target.drive_id:
                return M365CheckResult(
                    status="drive_mismatch",
                    detail="resolved default drive differs from the manifest binding",
                )
            live_folder = await self._probe_folder(http, resolved_drive)
            if live_folder is None:
                return M365CheckResult(
                    status="folder_missing",
                    completed_stages=stages,
                    detail="manifest-owned /Meridian folder is absent on the bound drive",
                )
            if live_folder != target.folder_item_id:
                return M365CheckResult(
                    status="folder_unowned",
                    completed_stages=stages,
                    detail="live /Meridian folder id differs from the manifest binding",
                )
            try:
                await self._require_live_sop(http, resolved_drive, saved_v3_path, saved_v3_item_id)
            except SeedPreflightError as exc:
                return M365CheckResult(
                    status="mismatched", completed_stages=stages, detail=str(exc)
                )
            except GraphSyncError:
                return M365CheckResult(
                    status="mismatched",
                    completed_stages=stages,
                    detail="live SOP lookup returned a malformed or unavailable Graph response",
                )
        return M365CheckResult(
            status="owned",
            completed_stages=stages,
            detail="target, drive, folder and exact live SOP item all match the manifest",
        )

    async def unseed(self, manifest_dir: Path, *, generation_id: str) -> None:
        _checked_generation(generation_id)
        try:
            manifest = SeedManifest.load(manifest_dir, "m365")
        except FileNotFoundError as exc:
            raise SeedPreflightError(
                "no saved M365 manifest exists; nothing proves ownership of /Meridian"
            ) from exc
        except (OSError, ValueError):
            raise SeedPreflightError(
                "saved M365 manifest is unreadable or invalid; refusing before provider I/O"
            ) from None
        target = self._require_owned_target(manifest)
        binding_mismatch = self._manifest_target_mismatch(manifest, target)
        if binding_mismatch is not None:
            raise SeedPreflightError(
                f"saved manifest target binding {binding_mismatch} does not match its outer "
                "manifest; refusing"
            )
        if target.generation_id != generation_id:
            raise SeedPreflightError(
                "generation id does not match the manifest-bound persistence generation"
            )
        mismatch = self._static_target_mismatch(target)
        if mismatch is not None:
            raise SeedPreflightError(
                f"saved manifest {mismatch} does not match this configuration; refusing"
            )
        base = self._client.graph_base
        async with self._client.session() as http:
            resolved_drive = await self._resolve_drive(http)
            if resolved_drive != target.drive_id:
                raise SeedPreflightError(
                    "resolved default drive does not match the manifest-bound drive; refusing"
                )
            live_folder = await self._probe_folder(http, resolved_drive)
            if live_folder is None:
                raise SeedPreflightError(
                    "the manifest-owned /Meridian folder is absent; nothing to delete — "
                    "remove the stale manifest manually if the target was decommissioned"
                )
            if live_folder != target.folder_item_id:
                raise SeedPreflightError(
                    "the live /Meridian folder id does not equal the manifest-owned "
                    "folder id; refusing to delete by path resemblance"
                )
            await self._client.delete(
                http,
                f"{base}/drives/{_url_segment(resolved_drive)}"
                f"/items/{_url_segment(target.folder_item_id)}",
            )
        SeedManifest.delete(manifest_dir, "m365")
