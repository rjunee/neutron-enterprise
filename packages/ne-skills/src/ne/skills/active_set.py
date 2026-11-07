# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The active-set computer: pins ∩ ACL ∩ capability filter → ordered → capped.

Order of operations (R-28 first): (1) resolve the CANDIDATE UNIVERSE — the exact
Run-frozen Agent pins when ``pins`` is supplied, otherwise the HASH-VERIFIED
current packs (``verified_current_packs`` — a tampered row never reaches the label
filter); (2) ACL filter via ``match_labels`` + ``visible``; (3) capability filter
(``requires_tools ⊆ granted_tools`` AND host-platform compatibility); (4) if the
surface lacks ``read.skills.view`` it gets NO index (a surface that cannot fire
skills gets no dangling index, plan §2.5); (5) ORDER — the declared pin order on
the pinned path, or source tier → usage desc → name on the UNPINNED path; (6) cap
at ``settings.active_set_cap``.

WHY THE PIN INTERSECTION IS STEP 1. Ranking and the cap are a SCARCITY mechanism
over an open library. If an Agent's exact pins were intersected after them, two
failures follow: (a) the cap could be consumed by unrelated, unpinned Skills
before the assigned one is considered, and (b) the universe would be the CURRENT
version of each name, so a rollover appended mid-Run would silently replace the
frozen historical pin. Resolving pins first makes the candidate universe exactly
what the Agent declared, so neither ranking nor the cap can displace it and no
later version can substitute for it.

WHY THE PINNED PATH IS NOT USAGE-RANKED. ``usage_counts()`` is a global, mutable
``COUNT(*)`` over ``ne_skill_invocations`` that EVERY ``read.skills.view`` firing
by ANY principal, Agent and surface appends to. Ranking a pinned set by it would
make the pin order — and therefore ``render_index``'s bytes, WHICH pins survive a
cap, and every receipt fingerprint ne-api derives from them
(``ne.api.agent_skills.index_hash`` / ``surface_hash``) — a function of unrelated
third-party activity: one stray firing by another user reorders a frozen Run.
Usage popularity is a heuristic for choosing among skills nobody chose; it has no
standing over a set the Agent already enumerated. So the pinned
path preserves the DECLARED order end to end (``pins_for_agent`` → ``resolve_pins``
→ here → ``render_index``) and applies the cap in that order, which is what makes
recomposition reproducible. The unpinned path keeps tier/usage/name ranking
unchanged — there the scarcity heuristic is exactly right.

HOST-PLATFORM COMPATIBILITY (the ONE narrow mapping). A pack's ``platforms``
declares the HOST PLATFORM it was authored for; ``substrate_id`` names the
concrete agent SUBSTRATE binding serving it. They are different vocabularies. The
bundled artifacts declare ``neutron-enterprise`` (the product), while the only
substrate this product ships is ``pydantic-ai`` (the binding) — so a raw
``substrate_id in pack.platforms`` test excludes every bundled pack from every
active set. :data:`_HOST_PLATFORM_SUBSTRATES` records the single compatibility
fact that closes that gap, and nothing more:

- it is a mapping, not a wildcard — an unrelated platform identifier such as
  ``claude-code`` still excludes the pack from a ``pydantic-ai`` surface;
- it never renames the substrate: ``PydanticAiSkillSubstrate.substrate_id``
  remains ``pydantic-ai`` on every read path and in every snapshot, and no caller
  is ever handed a synthetic substrate id;
- it is backward compatible: a pack declaring ``pydantic-ai`` directly, and a
  pack declaring no platform at all, behave exactly as before;
- it touches no artifact bytes, so every pinned ``content_hash`` is unchanged.

Adding a row here is a governance decision (it widens what a substrate may
serve), which is why the table is explicit and closed rather than a prefix or
substring rule.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ne.contracts.skills import SkillPack, SkillSource
from ne.contracts.types import Principal
from ne.skills.config import SkillsSettings
from ne.skills.registry import SkillPin, SqlAlchemySkillRegistry
from ne.skills.standard import CONTROL_CHAR_RE
from ne.skills.tools import VIEW_TOOL_NAME
from ne.skills.visibility import GroupExpander, match_labels, visible

_SOURCE_TIER = {SkillSource.BUNDLED: 0, SkillSource.ENGAGEMENT: 1}

#: Host platform identifier -> the substrate ids that host platform is served by.
#: See the module docstring: one narrow, closed, documented compatibility fact.
_HOST_PLATFORM_SUBSTRATES: Mapping[str, frozenset[str]] = {
    "neutron-enterprise": frozenset({"pydantic-ai"}),
}


def _tier(source: SkillSource) -> int:
    return _SOURCE_TIER.get(source, 2)


def platform_compatible(platforms: tuple[str, ...], substrate_id: str) -> bool:
    """True when a pack declaring ``platforms`` may be served by ``substrate_id``.

    Empty ``platforms`` means "any" (unchanged). A direct substrate-id match wins
    (unchanged). Otherwise exactly the :data:`_HOST_PLATFORM_SUBSTRATES` rows
    apply — an unlisted identifier is still excluded.

    ``tuple[str, ...]`` and NOT ``Sequence[str]``: a bare ``str`` satisfies
    ``Sequence[str]``, and ``substrate_id in "xpydantic-aiy"`` is a SUBSTRING test
    that would answer True. ``SkillPack.platforms`` is always a validated tuple, so
    no caller can do this today; the annotation keeps it that way.
    """
    if not platforms:
        return True
    if substrate_id in platforms:
        return True
    return any(
        substrate_id in _HOST_PLATFORM_SUBSTRATES.get(platform, frozenset())
        for platform in platforms
    )


def resolve_pins(
    registry: SqlAlchemySkillRegistry, pins: Sequence[SkillPin]
) -> tuple[SkillPack, ...]:
    """Resolve exact pins through the registry's exact-pin chokepoint, in pin order.

    A pin that fails ANY check (retired, absent, wrong version, wrong declared
    hash, tampered row) is DROPPED silently — the active set is a discovery
    surface, and a per-pin diagnostic here would be exactly the existence oracle
    ``SkillNotFoundError`` exists to deny.

    A repeated NAME collapses to its FIRST occurrence, because both the rendered
    index and the governed view key a surface's Skills by name, so a second entry
    for the same name has no distinct meaning and would only duplicate an index
    line. The composition root (``ne.api.agent_skills.pins_for_agent``) REFUSES an
    Agent that pins one name at two versions, so production never reaches this
    rule; it exists so the public API cannot be handed an incoherent list and
    return an incoherent set.
    """
    resolved: list[SkillPack] = []
    seen: set[str] = set()
    for pin in pins:
        if pin.name in seen:
            continue
        pack = registry.verified_pinned_pack(pin)
        if pack is not None:
            seen.add(pack.name)
            resolved.append(pack)
    return tuple(resolved)


class ActiveSetComputer:
    """Compute a principal's pin∩ACL∩capability-filtered, ranked, capped active set."""

    def __init__(
        self,
        registry: SqlAlchemySkillRegistry,
        expander: GroupExpander,
        settings: SkillsSettings,
    ) -> None:
        self._registry = registry
        self._expander = expander
        self._settings = settings

    def compute(
        self,
        principal: Principal,
        *,
        granted_tools: frozenset[str],
        substrate_id: str,
        pins: Sequence[SkillPin] | None = None,
    ) -> tuple[SkillPack, ...]:
        """The active set for ``principal`` on a surface holding ``granted_tools``.

        ``pins=None`` (the default) keeps the pre-unit unpinned behavior exactly:
        the candidate universe is every hash-verified CURRENT pack, ranked by
        source tier → usage desc → name. A supplied sequence — INCLUDING an empty
        one, which means an Agent that froze no Skills — makes the candidate
        universe exactly those resolved pins, evaluated BEFORE ACL, tools,
        platform and the cap, and kept in DECLARED PIN ORDER (see the module
        docstring: usage counts are global mutable state and must not reorder a
        frozen Run).
        """
        if VIEW_TOOL_NAME not in granted_tools:
            # A surface that cannot fire skills gets no index (plan §2.5).
            return ()
        if pins is None:
            packs, _tampered = self._registry.verified_current_packs()
        else:
            packs = resolve_pins(self._registry, pins)
        candidates = self._admissible(packs, principal, granted_tools, substrate_id)
        if pins is None:
            # UNPINNED ONLY: the scarcity heuristic over the open library. The
            # usage fold is read HERE and nowhere else, so the pinned path never
            # touches global invocation state.
            counts = self._registry.usage_counts()
            candidates.sort(key=lambda p: (_tier(p.source), -counts.get(p.name, 0), p.name))
        return tuple(candidates[: self._settings.active_set_cap])

    def _admissible(
        self,
        packs: Sequence[SkillPack],
        principal: Principal,
        granted_tools: frozenset[str],
        substrate_id: str,
    ) -> list[SkillPack]:
        """ACL ∩ required-tools ∩ host-platform, input order preserved."""
        match = match_labels(principal, self._expander)
        admissible: list[SkillPack] = []
        for pack in packs:
            if not visible(pack.acl_labels, match):
                continue
            if not set(pack.requires_tools) <= granted_tools:
                continue
            if not platform_compatible(pack.platforms, substrate_id):
                continue
            admissible.append(pack)
        return admissible


def render_index(packs: tuple[SkillPack, ...]) -> str:
    """Render the deterministic name+description index (compute order preserved).

    Name, version and description ONLY. The procedure body never enters the index:
    it is served exclusively by the governed, audited ``read.skills.view`` firing.

    NO PACKS ⇒ EMPTY STRING, never a bare header. A header-only
    "Available skills (…):" block is a dangling index: it asserts to the model that
    skills are available and tells it how to load one, when there is nothing to
    load. That is the same failure plan §2.5 refuses for a surface lacking
    ``read.skills.view``, and the reason is identical — so the same answer applies
    whether the set is empty because the Agent pinned nothing, because every pin
    failed to resolve, or because the library is empty.

    ONE PACK ⇒ EXACTLY ONE LINE, structurally. Every rendered entry is flattened
    through :data:`~ne.skills.standard.CONTROL_CHAR_RE` so no field value can
    contribute a line break, and the output is therefore always ``len(packs) + 1``
    lines. This is not redundant with ``standard.lint_description``, which keeps a
    multi-line description out of the LIBRARY: this keeps one out of the INDEX
    whatever the registry holds — pre-lint rows, and rows written by any path that
    does not run the house standard. It matters because both WITHDRAW paths
    (``SqlAlchemySkillRegistry.retire`` and ``PydanticAiSkillSubstrate.withdraw``)
    remove entries by line-filtering the stored text on the single-line prefix
    ``- {name} (``: a second line smuggled under one pack's entry matches no
    pack's prefix, so it would survive every withdrawal and advertise a Skill that
    does not exist. Flattening is a RENDER-time transform over already-clean data
    (it is the identity on every printable description), so it moves no
    ``content_hash`` and no stored index bytes.
    """
    if not packs:
        return ""
    header = "Available skills (call read.skills.view with a skill name to load its procedure):"
    lines = [CONTROL_CHAR_RE.sub(" ", f"- {p.name} (v{p.version}): {p.description}") for p in packs]
    return "\n".join([header, *lines])
