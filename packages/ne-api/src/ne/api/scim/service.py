# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ScimProvisioningService — governed, audited SCIM writes over the EXISTING
``ne.permissions.store.PrincipalStore`` (SPEC row NE-089).

ID RULE (decided): ``principal_id = externalId if truthy else userName`` (users)
/ ``externalId if truthy else displayName`` (groups) — matches the NE-042
principal-id-opacity contract; operators map ``externalId <- objectId`` (Entra)
so SCIM ids match connector-stamped ACLs (see ``packages/ne-api/README.md``).

Every mutation goes ONLY through the store's public API:

* ``upsert_principals`` / ``mark_removed`` / ``set_group_members`` /
  ``is_known`` are the SAME primitives ``PrincipalSyncService`` uses — this is
  NOT a new read/trim path, and ``mark_removed`` (tombstone + membership-edge
  cascade) is what makes a SCIM deactivate structurally fail-closed: it kills
  ``acl:tenant-all`` / ``acl:public-link`` / every group-derived match the
  moment the store commits, exactly like an Entra sync tombstone would.
* SCIM's own resource attributes (userName/externalId/displayName/active/
  members, per-resource created/lastModified, tombstone bookkeeping) ride the
  EXISTING ``ne_permissions_sync_state`` KV via ``get_sync_state``/
  ``set_sync_state`` INSIDE ``store.transaction()`` — so the principal/edge
  writes and the SCIM attribute writes commit as ONE atomic snapshot. The KV
  has no delete primitive, so a rename/delete leaves a STALE name-pointer key;
  every name-key lookup below reverse-verifies (reads the pointed-at record and
  checks the name still matches AND it is not tombstoned) so a stale pointer is
  treated as absent.
* FAIL-CLOSED GUARD (decided): whenever a group's member set is written
  (create/PUT/PATCH), candidate member ids are filtered through
  ``store.is_known()`` — a tombstoned or never-provisioned id is SKIPPED
  (never given an edge), so a group push can never re-grant access to a
  deactivated user. Skipped ids are recorded in the audit detail.

AUDIT (mirrors ``ne.permissions.sync.PrincipalSyncService`` exactly): every
mutation emits ONE ``permissions.scim_provision`` event (success, or error +
re-raise) whose ``detail`` carries ids/names/flags ONLY (C12 — never raw
request bodies or tokens), plus one ``permissions.membership_change`` event per
member whose direct group set changed, ``detail={"added": [...], "removed":
[...]}`` sorted — byte-compatible with ``sync.py``'s shape. A sink append
failure propagates (an unrecorded action is a withheld action, the same
discipline as ``ne.api.authz.emit_surface_audit``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ne.api.scim.filters import ScimFilterError, parse_members_value_path
from ne.api.scim.models import ScimGroup, ScimGroupMember, ScimMeta, ScimUser
from ne.contracts.governance import AuditSink
from ne.events import new_audit_event
from ne.permissions.models import PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore
from ne.schema import AuditOutcome

__all__ = [
    "NO_SUCH_GROUP",
    "NO_SUCH_USER",
    "ScimBadRequestError",
    "ScimConflictError",
    "ScimNotFoundError",
    "ScimProvisioningService",
    "ScimServiceError",
]

_AUDIT_ACTION = "permissions.scim_provision"
_MEMBERSHIP_ACTION = "permissions.membership_change"

_USER_PREFIX = "scim:user:"
_UNAME_PREFIX = "scim:uname:"
_GROUP_PREFIX = "scim:group:"
_GNAME_PREFIX = "scim:gname:"

# C12 refusal copy — one module-level constant per refusal, so every ``detail``
# on this surface is a fixed string with no interpolation slot to leak into.
# Each names the FIELD (and, where a caller needs it to comply, the SUPPORTED
# SHAPE or VOCABULARY, all of it already published on ``/scim/v2/Schemas`` and
# ``/ServiceProviderConfig``) and never the rejected value. See
# ``ne.api.scim.filters`` for the threat model, and for what pins it
# ``test_every_scim_refusal_site_is_literal_or_explicitly_justified`` in
# ``tests/test_scim_c12_refusals.py``: that test walks every refusal site in this
# package, so a new refusal that interpolates anything fails until it is
# justified explicitly.
_SUPPORTED_PATCH_OPS = "add, remove, replace"
_UNSUPPORTED_PATCH_OP = f"unsupported PATCH op; supported: {_SUPPORTED_PATCH_OPS}"
_UNSUPPORTED_MEMBERS_VALUE = (
    "unsupported members value; expected a member object with a 'value' key, a string id, "
    "or a list of either"
)
_UNSUPPORTED_MEMBERS_ITEM = (
    "unsupported members value item; expected a member object with a 'value' key, or a string id"
)
_ACTIVE_NOT_BOOLEAN = "'active' must be a boolean"
_USERNAME_NOT_A_STRING = "userName must be a non-empty string"
_DISPLAYNAME_NOT_A_STRING = "displayName must be a non-empty string"
NO_SUCH_USER = "no such user"
NO_SUCH_GROUP = "no such group"
_USERNAME_TAKEN = "userName already provisioned"
_DISPLAYNAME_TAKEN = "displayName already provisioned"
# The id is a pure function of the caller's own body (see the ID RULE above), so
# naming the derivation is everything they need to fix the request.
_USER_ID_TAKEN = "resource id already provisioned; the id derives from externalId, else userName"
_GROUP_ID_TAKEN = (
    "resource id already provisioned; the id derives from externalId, else displayName"
)


class ScimServiceError(Exception):
    """Base for every SCIM-service failure — carries the HTTP status to use."""

    def __init__(self, status: int, scim_type: str | None, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.scim_type = scim_type
        self.detail = detail


class ScimNotFoundError(ScimServiceError):
    def __init__(self, detail: str = "resource not found") -> None:
        super().__init__(404, None, detail)


class ScimConflictError(ScimServiceError):
    def __init__(self, detail: str) -> None:
        super().__init__(409, "uniqueness", detail)


class ScimBadRequestError(ScimServiceError):
    def __init__(self, scim_type: str, detail: str) -> None:
        super().__init__(400, scim_type, detail)


# --- KV key helpers ----------------------------------------------------------


def _user_key(pid: str) -> str:
    return f"{_USER_PREFIX}{pid}"


def _uname_key(username: str) -> str:
    return f"{_UNAME_PREFIX}{username.lower()}"


def _group_key(gid: str) -> str:
    return f"{_GROUP_PREFIX}{gid}"


def _gname_key(display_name: str) -> str:
    return f"{_GNAME_PREFIX}{display_name.lower()}"


def _read_one(store: PrincipalStore, key: str) -> str | None:
    # get_sync_state does a LIKE `key%` prefix scan; indexing the returned dict
    # by the EXACT key is what makes this an exact-key read regardless of any
    # other key sharing the prefix.
    return store.get_sync_state(key).get(key)


def _load_user_record(store: PrincipalStore, pid: str) -> dict[str, Any] | None:
    raw = _read_one(store, _user_key(pid))
    return json.loads(raw) if raw is not None else None


def _save_user_record(
    store: PrincipalStore, pid: str, record: Mapping[str, Any], now: datetime
) -> None:
    store.set_sync_state({_user_key(pid): json.dumps(dict(record), sort_keys=True)}, now=now)


def _save_uname_pointer(store: PrincipalStore, username: str, pid: str, now: datetime) -> None:
    store.set_sync_state({_uname_key(username): pid}, now=now)


def _resolve_user_by_username(
    store: PrincipalStore, username: str
) -> tuple[str, dict[str, Any]] | None:
    """Reverse-verified username -> (pid, record) lookup; ``None`` if stale/absent."""
    pid = _read_one(store, _uname_key(username))
    if pid is None:
        return None
    record = _load_user_record(store, pid)
    if record is None or record.get("deleted"):
        return None
    if str(record.get("userName", "")).lower() != username.lower():
        return None
    return pid, record


def _load_group_record(store: PrincipalStore, gid: str) -> dict[str, Any] | None:
    raw = _read_one(store, _group_key(gid))
    return json.loads(raw) if raw is not None else None


def _save_group_record(
    store: PrincipalStore, gid: str, record: Mapping[str, Any], now: datetime
) -> None:
    store.set_sync_state({_group_key(gid): json.dumps(dict(record), sort_keys=True)}, now=now)


def _save_gname_pointer(store: PrincipalStore, display_name: str, gid: str, now: datetime) -> None:
    store.set_sync_state({_gname_key(display_name): gid}, now=now)


def _resolve_group_by_displayname(
    store: PrincipalStore, display_name: str
) -> tuple[str, dict[str, Any]] | None:
    gid = _read_one(store, _gname_key(display_name))
    if gid is None:
        return None
    record = _load_group_record(store, gid)
    if record is None or record.get("deleted"):
        return None
    if str(record.get("displayName", "")).lower() != display_name.lower():
        return None
    return gid, record


# --- wire conversion -----------------------------------------------------


def _user_to_wire(pid: str, record: Mapping[str, Any]) -> ScimUser:
    return ScimUser(
        id=pid,
        userName=record["userName"],
        externalId=record.get("externalId"),
        displayName=record.get("displayName"),
        active=bool(record.get("active", True)),
        meta=ScimMeta(
            resourceType="User",
            created=datetime.fromisoformat(record["created"]),
            lastModified=datetime.fromisoformat(record["lastModified"]),
            location=f"/scim/v2/Users/{pid}",
        ),
    )


def _group_to_wire(gid: str, record: Mapping[str, Any]) -> ScimGroup:
    return ScimGroup(
        id=gid,
        displayName=record["displayName"],
        externalId=record.get("externalId"),
        members=tuple(ScimGroupMember(value=m) for m in sorted(record.get("members", []))),
        meta=ScimMeta(
            resourceType="Group",
            created=datetime.fromisoformat(record["created"]),
            lastModified=datetime.fromisoformat(record["lastModified"]),
            location=f"/scim/v2/Groups/{gid}",
        ),
    )


def _extract_member_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        ids: list[str] = []
        for item in value:
            if isinstance(item, dict) and "value" in item:
                ids.append(str(item["value"]))
            elif isinstance(item, str):
                ids.append(item)
            else:
                raise ScimBadRequestError("invalidValue", _UNSUPPORTED_MEMBERS_ITEM)
        return ids
    if isinstance(value, dict) and "value" in value:
        return [str(value["value"])]
    raise ScimBadRequestError("invalidValue", _UNSUPPORTED_MEMBERS_VALUE)


def _scrub_member_from_group_records(
    store: PrincipalStore, member_id: str, group_ids: Iterable[str], now: datetime
) -> None:
    """Remove ``member_id`` from the persisted SCIM ``members`` array of every
    group in ``group_ids`` (Codex NE-089 P1 fix).

    ``mark_removed`` cascade-deletes the LIVE membership edges the moment a
    user is deactivated/deleted, but SCIM's own KV-backed group records
    (``scim:group:*``) are a SEPARATE copy of that membership list, written
    only by group create/PUT/PATCH — so without this scrub they go stale and
    still list the removed member. If that member is later re-provisioned
    (revive-on-reappear, a deliberate path) and ANY subsequent PUT/PATCH
    touches the SAME group, ``_put_group_sync``/``_patch_group_sync`` reseed
    ``allowed`` from that stale ``members`` array and re-filter it through
    ``store.is_known()`` — which is true again — silently RESTORING the old
    membership edge with no explicit add op. Scrubbing the record at the same
    moment the edges are cascaded keeps the two copies consistent so a later
    group write can never resurrect a membership that was structurally
    revoked.
    """
    for group_id in group_ids:
        record = _load_group_record(store, group_id)
        if record is None:
            continue
        members = record.get("members", [])
        if member_id not in members:
            continue
        updated = dict(record)
        updated["members"] = sorted(m for m in members if m != member_id)
        updated["lastModified"] = now.isoformat()
        _save_group_record(store, group_id, updated, now)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise ScimBadRequestError("invalidValue", _ACTIVE_NOT_BOOLEAN)


class ScimProvisioningService:
    """Users + Groups CRUD/PATCH over the ONE ``PrincipalStore``, fully audited."""

    def __init__(
        self, store: PrincipalStore, sink: AuditSink, *, actor: str = "system:scim"
    ) -> None:
        self._store = store
        self._sink = sink
        self._actor = actor

    # -- audit helpers --------------------------------------------------------

    async def _emit(
        self, *, resource: str, outcome: AuditOutcome, detail: Mapping[str, Any]
    ) -> None:
        await self._sink.append(
            new_audit_event(
                action=_AUDIT_ACTION,
                resource=resource,
                outcome=outcome,
                actor=self._actor,
                detail=json.dumps(dict(detail), sort_keys=True),
            )
        )

    async def _emit_membership_change(
        self, member_id: str, *, added: Iterable[str], removed: Iterable[str]
    ) -> None:
        added_s = sorted(added)
        removed_s = sorted(removed)
        if not added_s and not removed_s:
            return
        await self._sink.append(
            new_audit_event(
                action=_MEMBERSHIP_ACTION,
                resource=f"principal://{member_id}",
                outcome=AuditOutcome.success,
                actor=self._actor,
                detail=json.dumps({"added": added_s, "removed": removed_s}, sort_keys=True),
            )
        )

    # -- users -----------------------------------------------------------

    async def create_user(self, body: ScimUser) -> ScimUser:
        pid = body.externalId or body.userName
        resource = f"scim://Users/{pid}"
        try:
            record, groups_lost = self._create_user_sync(pid, body)
        except Exception as exc:
            await self._emit(
                resource=resource,
                outcome=AuditOutcome.error,
                detail={"op": "create_user", "error": type(exc).__name__},
            )
            raise
        await self._emit(
            resource=resource,
            outcome=AuditOutcome.success,
            detail={"op": "create_user", "id": pid, "userName": body.userName},
        )
        for group_id in sorted(groups_lost):
            await self._emit_membership_change(pid, added=(), removed=(group_id,))
        return _user_to_wire(pid, record)

    def _create_user_sync(self, pid: str, body: ScimUser) -> tuple[dict[str, Any], frozenset[str]]:
        store = self._store
        now = datetime.now(UTC)
        with store.transaction():
            # Id-level duplicate guard (Codex NE-089 P2): the computed pid
            # (externalId or userName) may collide with a LIVE record even when
            # the inbound userName differs (e.g. a misconfigured create reusing
            # the IdP object id) — without this check the create would silently
            # OVERWRITE the existing resource (and upsert_principals would
            # revive a deactivated principal). A live collision is a 409; a
            # tombstoned (deleted) record may be re-provisioned.
            prior = _load_user_record(store, pid)
            if prior is not None and not prior.get("deleted"):
                raise ScimConflictError(_USER_ID_TAKEN)
            if _resolve_user_by_username(store, body.userName) is not None:
                raise ScimConflictError(_USERNAME_TAKEN)
            store.upsert_principals(
                [
                    SyncedPrincipal(
                        principal_id=pid,
                        kind=PrincipalKind.USER,
                        display_name=body.displayName or body.userName,
                    )
                ],
                source="scim",
                now=now,
            )
            groups_lost: frozenset[str] = frozenset()
            if not body.active:
                groups_lost = store.direct_groups_of([pid]).get(pid, frozenset())
                store.mark_removed([pid], now=now)
            record = {
                "userName": body.userName,
                "externalId": body.externalId,
                "displayName": body.displayName,
                "active": body.active,
                "created": now.isoformat(),
                "lastModified": now.isoformat(),
                "deleted": False,
            }
            _save_user_record(store, pid, record, now)
            _save_uname_pointer(store, body.userName, pid, now)
        return record, groups_lost

    def get_user(self, pid: str) -> ScimUser | None:
        record = _load_user_record(self._store, pid)
        if record is None or record.get("deleted"):
            return None
        return _user_to_wire(pid, record)

    def _all_user_records(self) -> list[tuple[str, dict[str, Any]]]:
        raw = self._store.get_sync_state(_USER_PREFIX)
        out: list[tuple[str, dict[str, Any]]] = []
        for key, value in raw.items():
            if not key.startswith(_USER_PREFIX):
                continue
            record = json.loads(value)
            if record.get("deleted"):
                continue
            out.append((key[len(_USER_PREFIX) :], record))
        return out

    def list_users(
        self, *, filter_attr: str | None, filter_value: str | None, start_index: int, count: int
    ) -> tuple[list[ScimUser], int]:
        records = self._all_user_records()
        if filter_attr == "userName":
            fv = (filter_value or "").lower()
            records = [(p, r) for p, r in records if str(r.get("userName", "")).lower() == fv]
        elif filter_attr == "externalId":
            records = [(p, r) for p, r in records if str(r.get("externalId") or "") == filter_value]
        records.sort(key=lambda pr: pr[0])
        total = len(records)
        start = max(start_index - 1, 0)
        page = records[start : start + max(count, 0)]
        return [_user_to_wire(pid, rec) for pid, rec in page], total

    async def put_user(self, pid: str, body: ScimUser) -> ScimUser:
        resource = f"scim://Users/{pid}"
        try:
            record = self._put_user_sync(pid, body)
        except Exception as exc:
            await self._emit(
                resource=resource,
                outcome=AuditOutcome.error,
                detail={"op": "put_user", "id": pid, "error": type(exc).__name__},
            )
            raise
        await self._emit(
            resource=resource,
            outcome=AuditOutcome.success,
            detail={"op": "put_user", "id": pid, "userName": body.userName},
        )
        return _user_to_wire(pid, record)

    def _put_user_sync(self, pid: str, body: ScimUser) -> dict[str, Any]:
        store = self._store
        now = datetime.now(UTC)
        with store.transaction():
            existing = _load_user_record(store, pid)
            if existing is None or existing.get("deleted"):
                raise ScimNotFoundError(NO_SUCH_USER)
            dup = _resolve_user_by_username(store, body.userName)
            if dup is not None and dup[0] != pid:
                raise ScimConflictError(_USERNAME_TAKEN)
            store.upsert_principals(
                [
                    SyncedPrincipal(
                        principal_id=pid,
                        kind=PrincipalKind.USER,
                        display_name=body.displayName or body.userName,
                    )
                ],
                source="scim",
                now=now,
            )
            # PUT replaces identity attributes; ``active``/tombstone state is
            # changed ONLY via PATCH's set_active path (the one path that also
            # drives the store's mark_removed/upsert_principals primitive) —
            # never silently by a PUT, which would desync KV vs. store state.
            record = {
                "userName": body.userName,
                "externalId": body.externalId,
                "displayName": body.displayName,
                "active": existing.get("active", True),
                "created": existing["created"],
                "lastModified": now.isoformat(),
                "deleted": False,
            }
            _save_user_record(store, pid, record, now)
            _save_uname_pointer(store, body.userName, pid, now)
        return record

    async def patch_user(self, pid: str, operations: Sequence[Any]) -> ScimUser:
        resource = f"scim://Users/{pid}"
        try:
            record, ignored, groups_lost = self._patch_user_sync(pid, operations)
        except Exception as exc:
            await self._emit(
                resource=resource,
                outcome=AuditOutcome.error,
                detail={"op": "patch_user", "id": pid, "error": type(exc).__name__},
            )
            raise
        detail: dict[str, Any] = {"op": "patch_user", "id": pid}
        if ignored:
            detail["ignored_paths"] = sorted(ignored)
        await self._emit(resource=resource, outcome=AuditOutcome.success, detail=detail)
        for group_id in sorted(groups_lost):
            await self._emit_membership_change(pid, added=(), removed=(group_id,))
        return _user_to_wire(pid, record)

    def _patch_user_sync(
        self, pid: str, operations: Sequence[Any]
    ) -> tuple[dict[str, Any], set[str], frozenset[str]]:
        store = self._store
        now = datetime.now(UTC)
        ignored: set[str] = set()
        groups_lost: frozenset[str] = frozenset()
        with store.transaction():
            existing = _load_user_record(store, pid)
            if existing is None or existing.get("deleted"):
                raise ScimNotFoundError(NO_SUCH_USER)
            record = dict(existing)
            active_target: bool | None = None
            rename_to: str | None = None
            display_to: str | None = None

            for op in operations:
                verb = op.op
                if verb not in ("add", "remove", "replace"):
                    raise ScimBadRequestError("invalidSyntax", _UNSUPPORTED_PATCH_OP)
                path = (op.path or "").strip()
                if not path and isinstance(op.value, dict):
                    if "active" in op.value:
                        active_target = _coerce_bool(op.value["active"])
                        continue
                    ignored.add("(value)")
                    continue
                lowered = path.lower()
                if lowered == "active":
                    active_target = _coerce_bool(op.value)
                elif lowered == "username":
                    if not isinstance(op.value, str) or not op.value:
                        raise ScimBadRequestError("invalidValue", _USERNAME_NOT_A_STRING)
                    rename_to = op.value
                elif lowered == "displayname":
                    display_to = op.value if isinstance(op.value, str) else None
                else:
                    ignored.add(path)

            if rename_to is not None:
                dup = _resolve_user_by_username(store, rename_to)
                if dup is not None and dup[0] != pid:
                    raise ScimConflictError(_USERNAME_TAKEN)
                record["userName"] = rename_to
            if display_to is not None:
                record["displayName"] = display_to

            if active_target is not None and active_target != record.get("active", True):
                if active_target:
                    store.upsert_principals(
                        [
                            SyncedPrincipal(
                                principal_id=pid,
                                kind=PrincipalKind.USER,
                                display_name=record.get("displayName") or record["userName"],
                            )
                        ],
                        source="scim",
                        now=now,
                    )
                else:
                    groups_lost = store.direct_groups_of([pid]).get(pid, frozenset())
                    store.mark_removed([pid], now=now)
                    _scrub_member_from_group_records(store, pid, groups_lost, now)
                record["active"] = active_target

            record["lastModified"] = now.isoformat()
            _save_user_record(store, pid, record, now)
            _save_uname_pointer(store, record["userName"], pid, now)
        return record, ignored, groups_lost

    async def delete_user(self, pid: str) -> None:
        resource = f"scim://Users/{pid}"
        try:
            groups_lost = self._delete_user_sync(pid)
        except Exception as exc:
            await self._emit(
                resource=resource,
                outcome=AuditOutcome.error,
                detail={"op": "delete_user", "id": pid, "error": type(exc).__name__},
            )
            raise
        await self._emit(
            resource=resource, outcome=AuditOutcome.success, detail={"op": "delete_user", "id": pid}
        )
        for group_id in sorted(groups_lost):
            await self._emit_membership_change(pid, added=(), removed=(group_id,))

    def _delete_user_sync(self, pid: str) -> frozenset[str]:
        store = self._store
        now = datetime.now(UTC)
        with store.transaction():
            existing = _load_user_record(store, pid)
            if existing is None or existing.get("deleted"):
                raise ScimNotFoundError(NO_SUCH_USER)
            groups_lost = store.direct_groups_of([pid]).get(pid, frozenset())
            store.mark_removed([pid], now=now)
            _scrub_member_from_group_records(store, pid, groups_lost, now)
            record = dict(existing)
            record["deleted"] = True
            record["active"] = False
            record["lastModified"] = now.isoformat()
            _save_user_record(store, pid, record, now)
        return groups_lost

    # -- groups ----------------------------------------------------------

    async def create_group(self, body: ScimGroup) -> ScimGroup:
        gid = body.externalId or body.displayName
        resource = f"scim://Groups/{gid}"
        try:
            record, added, skipped = self._create_group_sync(gid, body)
        except Exception as exc:
            await self._emit(
                resource=resource,
                outcome=AuditOutcome.error,
                detail={"op": "create_group", "error": type(exc).__name__},
            )
            raise
        detail: dict[str, Any] = {
            "op": "create_group",
            "id": gid,
            "displayName": body.displayName,
        }
        if skipped:
            detail["skipped_members"] = sorted(skipped)
        await self._emit(resource=resource, outcome=AuditOutcome.success, detail=detail)
        for member_id in sorted(added):
            await self._emit_membership_change(member_id, added=(gid,), removed=())
        return _group_to_wire(gid, record)

    def _create_group_sync(
        self, gid: str, body: ScimGroup
    ) -> tuple[dict[str, Any], frozenset[str], frozenset[str]]:
        store = self._store
        now = datetime.now(UTC)
        with store.transaction():
            # Id-level duplicate guard — the group sibling of the user-create
            # guard above: a LIVE record under the computed gid means this
            # create would silently overwrite an existing resource → 409.
            prior = _load_group_record(store, gid)
            if prior is not None and not prior.get("deleted"):
                raise ScimConflictError(_GROUP_ID_TAKEN)
            if _resolve_group_by_displayname(store, body.displayName) is not None:
                raise ScimConflictError(_DISPLAYNAME_TAKEN)
            store.upsert_principals(
                [
                    SyncedPrincipal(
                        principal_id=gid, kind=PrincipalKind.GROUP, display_name=body.displayName
                    )
                ],
                source="scim",
                now=now,
            )
            wanted = [m.value for m in body.members]
            allowed = {mid for mid in wanted if store.is_known(mid)}
            skipped = frozenset(mid for mid in wanted if mid not in allowed)
            added, _removed = store.set_group_members(gid, allowed, source="scim", now=now)
            record = {
                "displayName": body.displayName,
                "externalId": body.externalId,
                "members": sorted(allowed),
                "created": now.isoformat(),
                "lastModified": now.isoformat(),
                "deleted": False,
            }
            _save_group_record(store, gid, record, now)
            _save_gname_pointer(store, body.displayName, gid, now)
        return record, added, skipped

    def get_group(self, gid: str) -> ScimGroup | None:
        record = _load_group_record(self._store, gid)
        if record is None or record.get("deleted"):
            return None
        return _group_to_wire(gid, record)

    def _all_group_records(self) -> list[tuple[str, dict[str, Any]]]:
        raw = self._store.get_sync_state(_GROUP_PREFIX)
        out: list[tuple[str, dict[str, Any]]] = []
        for key, value in raw.items():
            if not key.startswith(_GROUP_PREFIX):
                continue
            record = json.loads(value)
            if record.get("deleted"):
                continue
            out.append((key[len(_GROUP_PREFIX) :], record))
        return out

    def list_groups(
        self, *, filter_attr: str | None, filter_value: str | None, start_index: int, count: int
    ) -> tuple[list[ScimGroup], int]:
        records = self._all_group_records()
        if filter_attr == "displayName":
            fv = (filter_value or "").lower()
            records = [(g, r) for g, r in records if str(r.get("displayName", "")).lower() == fv]
        elif filter_attr == "externalId":
            records = [(g, r) for g, r in records if str(r.get("externalId") or "") == filter_value]
        records.sort(key=lambda gr: gr[0])
        total = len(records)
        start = max(start_index - 1, 0)
        page = records[start : start + max(count, 0)]
        return [_group_to_wire(gid, rec) for gid, rec in page], total

    async def put_group(self, gid: str, body: ScimGroup) -> ScimGroup:
        resource = f"scim://Groups/{gid}"
        try:
            record, added, removed, skipped = self._put_group_sync(gid, body)
        except Exception as exc:
            await self._emit(
                resource=resource,
                outcome=AuditOutcome.error,
                detail={"op": "put_group", "id": gid, "error": type(exc).__name__},
            )
            raise
        detail: dict[str, Any] = {"op": "put_group", "id": gid, "displayName": body.displayName}
        if skipped:
            detail["skipped_members"] = sorted(skipped)
        await self._emit(resource=resource, outcome=AuditOutcome.success, detail=detail)
        for member_id in sorted(set(added) | set(removed)):
            await self._emit_membership_change(
                member_id,
                added=(gid,) if member_id in added else (),
                removed=(gid,) if member_id in removed else (),
            )
        return _group_to_wire(gid, record)

    def _put_group_sync(
        self, gid: str, body: ScimGroup
    ) -> tuple[dict[str, Any], frozenset[str], frozenset[str], frozenset[str]]:
        store = self._store
        now = datetime.now(UTC)
        with store.transaction():
            existing = _load_group_record(store, gid)
            if existing is None or existing.get("deleted"):
                raise ScimNotFoundError(NO_SUCH_GROUP)
            dup = _resolve_group_by_displayname(store, body.displayName)
            if dup is not None and dup[0] != gid:
                raise ScimConflictError(_DISPLAYNAME_TAKEN)
            store.upsert_principals(
                [
                    SyncedPrincipal(
                        principal_id=gid, kind=PrincipalKind.GROUP, display_name=body.displayName
                    )
                ],
                source="scim",
                now=now,
            )
            wanted = [m.value for m in body.members]
            allowed = {mid for mid in wanted if store.is_known(mid)}
            skipped = frozenset(mid for mid in wanted if mid not in allowed)
            added, removed = store.set_group_members(gid, allowed, source="scim", now=now)
            record = {
                "displayName": body.displayName,
                "externalId": body.externalId,
                "members": sorted(allowed),
                "created": existing["created"],
                "lastModified": now.isoformat(),
                "deleted": False,
            }
            _save_group_record(store, gid, record, now)
            _save_gname_pointer(store, body.displayName, gid, now)
        return record, added, removed, skipped

    async def patch_group(self, gid: str, operations: Sequence[Any]) -> ScimGroup:
        resource = f"scim://Groups/{gid}"
        try:
            record, added, removed, skipped, ignored = self._patch_group_sync(gid, operations)
        except Exception as exc:
            await self._emit(
                resource=resource,
                outcome=AuditOutcome.error,
                detail={"op": "patch_group", "id": gid, "error": type(exc).__name__},
            )
            raise
        detail: dict[str, Any] = {"op": "patch_group", "id": gid}
        if skipped:
            detail["skipped_members"] = sorted(skipped)
        if ignored:
            detail["ignored_paths"] = sorted(ignored)
        await self._emit(resource=resource, outcome=AuditOutcome.success, detail=detail)
        for member_id in sorted(set(added) | set(removed)):
            await self._emit_membership_change(
                member_id,
                added=(gid,) if member_id in added else (),
                removed=(gid,) if member_id in removed else (),
            )
        return _group_to_wire(gid, record)

    def _patch_group_sync(
        self, gid: str, operations: Sequence[Any]
    ) -> tuple[dict[str, Any], frozenset[str], frozenset[str], frozenset[str], set[str]]:
        store = self._store
        now = datetime.now(UTC)
        ignored: set[str] = set()
        with store.transaction():
            existing = _load_group_record(store, gid)
            if existing is None or existing.get("deleted"):
                raise ScimNotFoundError(NO_SUCH_GROUP)
            current_members = set(existing.get("members", ()))
            display_to: str | None = None

            for op in operations:
                verb = op.op
                if verb not in ("add", "remove", "replace"):
                    raise ScimBadRequestError("invalidSyntax", _UNSUPPORTED_PATCH_OP)
                path = (op.path or "").strip()
                lowered = path.lower()
                if lowered == "displayname":
                    if not isinstance(op.value, str) or not op.value:
                        raise ScimBadRequestError("invalidValue", _DISPLAYNAME_NOT_A_STRING)
                    display_to = op.value
                elif lowered == "members" or (not path and isinstance(op.value, list)):
                    ids = _extract_member_ids(op.value)
                    if verb == "remove":
                        current_members -= set(ids)
                    elif verb == "replace":
                        current_members = set(ids)
                    else:  # add
                        current_members |= set(ids)
                elif lowered.startswith("members["):
                    try:
                        member_id = parse_members_value_path(path)
                    except ScimFilterError as exc:
                        # ``from None``: the GOVERNED WRITE lane — this path filter
                        # carries the id of the principal being removed from a group.
                        # Defense in depth at this link and no wider claim; the cause
                        # is our own ``ScimFilterError``, whose message is copied
                        # verbatim into the error raised here.
                        raise ScimBadRequestError(exc.scim_type, exc.detail) from None
                    if verb == "remove":
                        current_members.discard(member_id)
                    else:
                        ignored.add(path)
                else:
                    ignored.add(path)

            allowed = {mid for mid in current_members if store.is_known(mid)}
            skipped = frozenset(current_members - allowed)
            added, removed = store.set_group_members(gid, allowed, source="scim", now=now)

            record = dict(existing)
            if display_to is not None:
                dup = _resolve_group_by_displayname(store, display_to)
                if dup is not None and dup[0] != gid:
                    raise ScimConflictError(_DISPLAYNAME_TAKEN)
                record["displayName"] = display_to
                _save_gname_pointer(store, display_to, gid, now)
            record["members"] = sorted(allowed)
            record["lastModified"] = now.isoformat()
            _save_group_record(store, gid, record, now)
        return record, added, removed, skipped, ignored

    async def delete_group(self, gid: str) -> None:
        resource = f"scim://Groups/{gid}"
        try:
            affected = self._delete_group_sync(gid)
        except Exception as exc:
            await self._emit(
                resource=resource,
                outcome=AuditOutcome.error,
                detail={"op": "delete_group", "id": gid, "error": type(exc).__name__},
            )
            raise
        await self._emit(
            resource=resource,
            outcome=AuditOutcome.success,
            detail={"op": "delete_group", "id": gid},
        )
        for member_id in sorted(affected):
            await self._emit_membership_change(member_id, added=(), removed=(gid,))

    def _delete_group_sync(self, gid: str) -> frozenset[str]:
        store = self._store
        now = datetime.now(UTC)
        with store.transaction():
            existing = _load_group_record(store, gid)
            if existing is None or existing.get("deleted"):
                raise ScimNotFoundError(NO_SUCH_GROUP)
            affected = frozenset(existing.get("members", ()))
            store.mark_removed([gid], now=now)
            record = dict(existing)
            record["deleted"] = True
            record["members"] = []
            record["lastModified"] = now.isoformat()
            _save_group_record(store, gid, record, now)
        return affected
