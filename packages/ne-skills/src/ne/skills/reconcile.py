# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Boot reconcile of the skills bundle dir — edit-wins by content hash (Hermes).

For each subdir with a SKILL.md:

* invalid (house-standard violations) ⇒ quarantine with a reason summary (NEVER
  silently skipped);
* valid ``bundled``/``engagement``: register if absent; no-op if the content hash
  is unchanged; append a new version if the current version came from the bundle
  (``origin='bundle'``) OR a ``sync_rearmed`` lifecycle event postdates it
  (edit-wins re-arm); otherwise SKIP (locally edited in the registry — never
  clobbered);
* valid ``manual`` (hand-dropped): recorded ``quarantined`` with reason
  ``pending-forge`` — surfaced in the library list, NEVER active until the
  NE-SKILL-1b gate exists (honest until then).

Idempotent: a second run over the same dir yields all-unchanged. A configured but
MISSING bundle dir fails BOOT loud (the worker ``_build_outbox_handler`` precedent).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa

from ne.contracts.skills import AuthoringStandard, SkillPack, SkillSource
from ne.skills.config import SkillsSettings
from ne.skills.packio import pack_from_dir
from ne.skills.registry import SqlAlchemySkillRegistry
from ne.skills.standard import StandardViolation

_BUNDLE_ORIGIN = "bundle"
_SYNC_REARMED = "sync_rearmed"
_RESTORED_EVENT = "restored"
_QUARANTINED_STATUS = "quarantined"
_RETIRED_STATUS = "retired"

#: Stable namespace key for the Postgres session advisory lock that serializes
#: reconcile across worker replicas (Codex P1). Any 64-bit int works; this one
#: spells "NESK" in the high bytes for grep-ability.
_RECONCILE_ADVISORY_LOCK_KEY = 0x4E45_534B


@contextmanager
def _reconcile_advisory_lock(engine: sa.Engine) -> Iterator[None]:
    """Serialize reconcile across worker replicas (Codex P1).

    Multiple replicas booting against the SAME registry would otherwise race the
    check-then-insert in ``register_pack``/``register_quarantined`` AND the
    next-version selection in the edit-wins path, crashing one replica's boot on
    a unique-constraint ``IntegrityError``. On Postgres we hold a SESSION advisory
    lock for the whole reconcile so only ONE replica reconciles at a time
    cluster-wide; the loser blocks, then observes everything unchanged
    (idempotent). This closes BOTH races in one place — strictly more complete
    than a per-row upsert, which would not serialize version selection. SQLite
    (single-process CI) has no advisory locks and needs none — no-op.
    """
    if engine.dialect.name != "postgresql":
        yield
        return
    conn = engine.connect()
    try:
        conn.execute(sa.text("SELECT pg_advisory_lock(:k)"), {"k": _RECONCILE_ADVISORY_LOCK_KEY})
        yield
    finally:
        try:
            conn.execute(
                sa.text("SELECT pg_advisory_unlock(:k)"), {"k": _RECONCILE_ADVISORY_LOCK_KEY}
            )
        finally:
            conn.close()


@dataclass
class ReconcileReport:
    registered: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    skipped_locally_edited: tuple[str, ...] = ()
    quarantined: tuple[str, ...] = ()
    #: Names skipped because they are RETIRED (the resurrection guard, §6.5): a
    #: retired pack is never re-quarantined by a broken bundle file — otherwise a
    #: broken-then-fixed file would launder retired → quarantined → restored(active).
    skipped_retired: tuple[str, ...] = ()
    _reg: list[str] = field(default_factory=list, repr=False)
    _upd: list[str] = field(default_factory=list, repr=False)
    _unch: list[str] = field(default_factory=list, repr=False)
    _skip: list[str] = field(default_factory=list, repr=False)
    _quar: list[str] = field(default_factory=list, repr=False)
    _retired: list[str] = field(default_factory=list, repr=False)

    def _freeze(self) -> ReconcileReport:
        self.registered = tuple(self._reg)
        self.updated = tuple(self._upd)
        self.unchanged = tuple(self._unch)
        self.skipped_locally_edited = tuple(self._skip)
        self.quarantined = tuple(self._quar)
        self.skipped_retired = tuple(self._retired)
        return self


def _violation_summary(violations: tuple[StandardViolation, ...]) -> str:
    return "; ".join(f"{v.rule}: {v.message}" for v in violations[:3])


def reconcile_bundle_dir(
    engine: sa.Engine,
    settings: SkillsSettings,
    *,
    actor: str = "system:reconcile",
) -> ReconcileReport:
    report = ReconcileReport()
    if not settings.bundle_dir:
        return report._freeze()

    root = Path(settings.bundle_dir)
    if not root.is_dir():
        raise ValueError(
            f"NE_SKILLS_BUNDLE_DIR={settings.bundle_dir!r} is set but is not a "
            "directory (fail boot loud — never a silent no-op)"
        )

    registry = SqlAlchemySkillRegistry(engine)
    standard = AuthoringStandard()

    with _reconcile_advisory_lock(engine):
        for sub in sorted(p for p in root.iterdir() if p.is_dir()):
            skill_md = sub / "SKILL.md"
            if not skill_md.is_file():
                continue
            result = pack_from_dir(sub, standard=standard)
            if isinstance(result, tuple):
                name = sub.name
                # RESURRECTION GUARD (§6.5): a RETIRED pack is a lifecycle-log fact
                # bundle presence cannot override. Appending a `quarantined` event
                # here would flip it out of `retired`, and a later repaired file
                # would then `restore` it to ACTIVE — laundering a retirement. So a
                # retired name is left retired and reported, never re-quarantined.
                if registry.current_status(name) == _RETIRED_STATUS:
                    report._retired.append(name)
                    continue
                registry.register_quarantined(
                    name,
                    SkillSource.BUNDLED.value,
                    reason=_violation_summary(result),
                    actor=actor,
                )
                report._quar.append(name)
                continue

            pack = result
            if pack.source == SkillSource.MANUAL:
                if registry.current_status(pack.name) == _RETIRED_STATUS:
                    report._retired.append(pack.name)
                    continue
                registry.register_quarantined(
                    pack.name, pack.source.value, reason="pending-forge", actor=actor
                )
                report._quar.append(pack.name)
                continue

            if pack.source not in (SkillSource.BUNDLED, SkillSource.ENGAGEMENT):
                # forged/learned packs are not bundle-reconciled (they enter via 1b).
                continue

            _reconcile_one(registry, pack, report, actor=actor)

    return report._freeze()


def _reconcile_one(
    registry: SqlAlchemySkillRegistry,
    pack: SkillPack,
    report: ReconcileReport,
    *,
    actor: str,
) -> None:
    meta = registry.current_version_meta(pack.name)
    if meta is None:
        registry.register_pack(pack, origin=_BUNDLE_ORIGIN)
        report._reg.append(pack.name)
        return
    # A previously-registered bundled/engagement pack may currently be QUARANTINED
    # (it went invalid on a past reconcile ⇒ a `quarantined` lifecycle event was
    # appended). A valid file now repairs it — but status is folded from the LAST
    # lifecycle event, so unless we append a `restored` event the pack stays
    # quarantined forever and can never be searched/viewed (Codex P1). We restore
    # whenever we ACCEPT the pack (unchanged content OR an edit-wins version bump);
    # never on the locally-edited SKIP path (the registry copy is authoritative
    # there and we do not touch it).
    status = registry.current_status(pack.name)
    quarantined = status == _QUARANTINED_STATUS
    # RESURRECTION GUARD (§6.5): a RETIRED pack may still receive an edit-wins
    # CONTENT version append below (harmless — status folds from the lifecycle
    # log, and the newer content simply becomes what a future restore would
    # serve), but NO `registered`/`restored` lifecycle event is EVER appended for
    # it: `_restore` fires only on the `quarantined` branch (False here for a
    # retired name), and `register_pack` fires only when `meta is None` (a retired
    # pack always has versions). So boot reconcile emits lifecycle transitions ONLY
    # for absent (register) or quarantined-repaired (restore) names — retirement is
    # a lifecycle fact bundle presence cannot flip (content-hash edit-wins is NOT
    # the protection; this no-lifecycle-for-known-names rule is).
    # Version-INDEPENDENT unchanged check (idempotency, R-Argus-P1): the stored
    # current version may have been auto-bumped on a prior reconcile (an in-place
    # edit that did NOT advance the frontmatter version). Because ``version`` is
    # part of ``content_hash``, comparing the incoming pack's hash against the
    # stored hash directly would NEVER re-match once we bumped — every subsequent
    # boot would append another version (unbounded growth). Instead we compare
    # CONTENT at a common version: probe the incoming pack AS IF it carried the
    # stored version, then compare content hashes. Identical content ⇒ unchanged
    # regardless of the version-field bookkeeping.
    probe = pack
    if pack.version != meta["version"]:
        probe = pack.model_copy(update={"version": meta["version"]})
    if meta["content_hash"] == probe.content_hash:
        if quarantined:
            _restore(registry, pack.name, actor=actor)
            report._upd.append(pack.name)
        else:
            report._unch.append(pack.name)
        return
    origin = meta["provenance"].get("origin")
    rearmed = registry.last_lifecycle_after(pack.name, _SYNC_REARMED, after=meta["created_at"])
    if origin == _BUNDLE_ORIGIN or rearmed:
        current_max = registry.current_version_of(pack.name) or 0
        next_pack = pack
        if pack.version <= current_max:
            next_pack = pack.model_copy(update={"version": current_max + 1})
        registry.append_version(next_pack, origin=_BUNDLE_ORIGIN)
        if quarantined:
            _restore(registry, pack.name, actor=actor)
        report._upd.append(pack.name)
    else:
        report._skip.append(pack.name)


def _restore(registry: SqlAlchemySkillRegistry, name: str, *, actor: str) -> None:
    registry.append_lifecycle(
        name,
        _RESTORED_EVENT,
        actor=actor,
        reason="reconcile: valid bundle file repaired a quarantined pack",
    )
