# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-011B: the Agent → exact Skill ActiveSet → materialize → instructions join.

ne-api is the ONE root that may hold an Agent definition (``ne.api.agent_manifest``),
the Skill registry + substrate (``ne.skills``) and the session factory
(``ne.runtime``) at once, so the join lives here and nowhere else.

WHAT AN ACTIVATION IS. Given an Agent definition and the principal running it,
:meth:`AgentSkillService.activate` derives the exact selection inputs from the
DEFINITION — never from the executor's tool table, never from the model:

- ``granted_tools`` = the Agent's declared ``tool_grants``. Deliberately NOT
  ``executor.tool_specs``: a pack's ``requires_tools`` names what the AGENT was
  granted, and a required tool the deployment has not bound yet is a reason for
  the Agent to be unavailable, not a reason to silently serve a Skill whose
  procedure it cannot follow.
- ``pins`` = the Agent's declared ``skills`` grants, in canonical
  ``(skill_id, version)`` order (the manifest validator already sorts them), each
  carrying the DECLARED ``content_hash``.

Selection is then ``ActiveSetComputer.compute(..., pins=...)``, which intersects
the exact pins BEFORE ACL, tools, platform, ranking and the cap (see
``ne.skills.active_set``). The result is materialized through the substrate onto
``surface_key`` and rendered ONCE into the index text that becomes the session's
``instructions``.

WHAT NEVER CROSSES. The index carries name, version and description only. A
Skill's procedure BODY is served exclusively by the governed, audited
``read.skills.view`` firing (C6) — so Skill text can never reach the model outside
a PDP-decided, audited tool call, and can never widen the Agent's tool grants:
``granted_tools`` is computed from the definition before any Skill is read, and no
Skill field feeds back into it (C12).

THE SUBSTRATE SEAM. ``substrate`` is typed by the frozen
``ne.contracts.skills.SkillSubstrate`` Protocol, never by a concrete binding (C5):
this module reads only ``substrate_id`` and ``materialize`` from it, so swapping
the shipped ``PydanticAiSkillSubstrate`` for another binding changes nothing here.

THE RECEIPT. :class:`SkillActivation` is the deterministic record of what was
activated: the ORDERED selected pins, the index hash and the surface hash. It is
the seam PRE-010 persists and rehydrates a Run from — this module deliberately
builds NO durable Run storage.

THE RUN CARRIER — NOT YET ON THE PRODUCTION TOOL PATH. :data:`RUN_SKILL_PINS` is
an ambient carrier shaped like ne-api's ``TURN_PRINCIPAL``, and
:func:`current_run_skill_pins` is the ``PinProvider`` a composition root is meant
to hand ``ne.skills.tools.build_view_handler``. Today NOTHING does:
``ne.api.tools.build_skill_bindings`` still constructs the view handler with no
``pin_provider``, so the live governed view resolves the CURRENT version and this
carrier is read only by this module's tests. PRE-011C owns ``ne/api/tools.py`` and
that wiring.

When it IS wired, the semantics change and the change is deliberate: a configured
pin provider makes the surface RUN-ONLY. ``build_view_handler`` fails closed —
same indistinguishable ``SkillNotFoundError`` — whenever a CONFIGURED provider
yields no Run, an empty pin set, no pin for the requested name, or raises. Only a
surface with NO provider at all may fall back to current-version resolution. So
011C must not blanket-wire the provider onto a surface that is expected to serve
skills outside a Run scope; it wires it where Run-frozen serving is the intent.
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import anyio.to_thread

from ne.api.agent_manifest import AgentManifestDefinition
from ne.contracts.skills import SkillPack, SkillSubstrate
from ne.contracts.types import Principal
from ne.skills.active_set import ActiveSetComputer, render_index
from ne.skills.config import SkillsSettings
from ne.skills.registry import SkillPin, SqlAlchemySkillRegistry
from ne.skills.visibility import GroupExpander

#: The current Run's frozen Skill pins, keyed by skill name. ``None`` ⇒ no Run.
RUN_SKILL_PINS: ContextVar[Mapping[str, SkillPin] | None] = ContextVar(
    "ne_api_run_skill_pins", default=None
)


def current_run_skill_pins() -> Mapping[str, SkillPin] | None:
    """The ``PinProvider`` seam handed to ``build_view_handler``."""
    return RUN_SKILL_PINS.get()


@contextmanager
def run_skill_pins(pins: Mapping[str, SkillPin]) -> Iterator[None]:
    """Bind ``pins`` as the current Run's frozen pins for the enclosed scope."""
    token = RUN_SKILL_PINS.set(pins)
    try:
        yield
    finally:
        RUN_SKILL_PINS.reset(token)


# -- the Run-surface receipt fingerprints ---------------------------------------
#
# These live in ne-api, not in ne-skills: a Run SURFACE receipt is a composition
# concern of the root that owns Runs. The skills library renders the index
# (``render_index``) and knows nothing about surfaces, receipts or Run identity.


def index_hash(index_text: str) -> str:
    """sha256 over the rendered index bytes."""
    return hashlib.sha256(index_text.encode("utf-8")).hexdigest()


def surface_hash(surface_key: str, pins: Sequence[SkillPin], text: str) -> str:
    """sha256 over the canonical (surface, ORDERED pins, index) triple.

    A recomposition that selects the same pins in the same order and renders the
    same index reproduces this hash; any change to the key, the pin order, a
    pinned version/hash, or the index bytes moves it.

    This is a pure fingerprint of what it is HANDED — it cannot make a
    non-deterministic selection deterministic. Reproducibility across a restart is
    a property of the PINNED ``ActiveSetComputer.compute`` path, which orders by
    declared pins and reads no global usage state. Fingerprinting an UNPINNED
    active set is well-defined but not restart-stable, because that path ranks by
    a mutable usage fold by design.
    """
    payload = {
        "surface_key": surface_key,
        "pins": [[pin.name, pin.version, pin.content_hash] for pin in pins],
        "index_hash": index_hash(text),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def granted_tools_for_agent(definition: AgentManifestDefinition) -> frozenset[str]:
    """The Agent's EXACT declared tool grants (never the executor's tool table)."""
    return frozenset(definition.tool_grants)


def pins_for_agent(definition: AgentManifestDefinition) -> tuple[SkillPin, ...]:
    """The Agent's EXACT declared Skill pins, in the manifest's canonical order.

    A definition pinning the SAME ``skill_id`` more than once is refused here
    rather than resolved: the index and the governed view both key a Run's Skills
    by NAME, so such a Run has no single answer to "view meridian-hardship-draft"
    and would silently serve whichever pin happened to win. A manifest never
    reaches this function in that shape — ``agent_manifest.py``'s
    ``_canonical_skills`` validator rejects a repeated ``skill_id`` whatever the
    versions or hashes, so a duplicate is a load-time refusal on the same footing
    as any other manifest defect rather than a ``ValueError`` escaping at
    session-compose time. The refusal below is defence in depth for the route that
    bypasses that validator: ``model_copy(update=...)`` rebuilds a definition
    without re-running field validators, so an in-process caller can still hand
    this function an ambiguous pin set, and it fails closed rather than picking a
    pin.
    """
    pins = tuple(
        SkillPin(name=grant.skill_id, version=grant.version, content_hash=grant.content_hash)
        for grant in definition.skills
    )
    names = [pin.name for pin in pins]
    if len(set(names)) != len(names):
        raise ValueError(
            f"agent {definition.agent_id!r} v{definition.version} pins the same Skill at "
            "more than one version; a Run surface resolves Skills by name"
        )
    return pins


@dataclass(frozen=True)
class SkillActivation:
    """The deterministic receipt of one Agent-Skill activation.

    ``pins`` are the packs actually SELECTED (a declared pin that failed to
    resolve, was ACL-invisible, lacked a granted tool, was platform-incompatible
    or fell outside the cap is absent), in materialized order, each rebuilt from
    the served pack — so the receipt records what the surface got, not what was
    asked for.
    """

    agent_id: str
    agent_version: int
    surface_key: str
    pins: tuple[SkillPin, ...]
    index_text: str
    index_hash: str
    surface_hash: str

    def pin_map(self) -> dict[str, SkillPin]:
        """The by-name map the governed view resolves a Run-frozen read through."""
        return {pin.name: pin for pin in self.pins}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(pin.name for pin in self.pins)


class AgentSkillService:
    """Compose an Agent's Run-frozen Skill surface: select → materialize → render."""

    def __init__(
        self,
        registry: SqlAlchemySkillRegistry,
        expander: GroupExpander,
        settings: SkillsSettings,
        substrate: SkillSubstrate,
    ) -> None:
        self._registry = registry
        self._computer = ActiveSetComputer(registry, expander, settings)
        self._substrate = substrate

    def select(
        self, definition: AgentManifestDefinition, principal: Principal
    ) -> tuple[SkillPack, ...]:
        """The blocking-SQL selection half (exact pins first). Sync by design."""
        return self._computer.compute(
            principal,
            granted_tools=granted_tools_for_agent(definition),
            # The REAL substrate id — the host-platform compatibility decision
            # lives in ne.skills.active_set, never in a synthetic id passed here.
            substrate_id=self._substrate.substrate_id,
            pins=pins_for_agent(definition),
        )

    async def activate(
        self,
        definition: AgentManifestDefinition,
        principal: Principal,
        *,
        surface_key: str,
    ) -> SkillActivation:
        """Select, materialize onto ``surface_key``, and return the receipt.

        ``compute`` is blocking SQL and ``materialize`` is async, so the selection
        is bridged with ``anyio.to_thread.run_sync`` (the ``ne.skills.tools``
        discipline).
        """
        packs = await anyio.to_thread.run_sync(
            functools.partial(self.select, definition, principal)
        )
        # ``materialize`` calls ``render_index`` on these packs, and
        # ``build_activation`` below calls it AGAIN on the same tuple. Two calls,
        # not one: what makes the materialized surface text and the receipt's
        # index_text equal is that ``render_index`` is a pure function of the pack
        # tuple and both calls receive the identical tuple — NOT that the value is
        # computed once and shared. ``test_the_procedure_sentinel_reaches_only_the
        # _governed_view_result`` asserts the two are byte-equal so this stays true
        # if either side ever stops being a pure re-render.
        await self._substrate.materialize(packs, surface_key=surface_key)
        return build_activation(definition, packs, surface_key=surface_key)


def build_activation(
    definition: AgentManifestDefinition,
    packs: Sequence[SkillPack],
    *,
    surface_key: str,
) -> SkillActivation:
    """Build the deterministic receipt for an already-selected pack sequence."""
    selected = tuple(
        SkillPin(name=pack.name, version=pack.version, content_hash=pack.content_hash)
        for pack in packs
    )
    text = render_index(tuple(packs))
    return SkillActivation(
        agent_id=definition.agent_id,
        agent_version=definition.version,
        surface_key=surface_key,
        pins=selected,
        index_text=text,
        index_hash=index_hash(text),
        surface_hash=surface_hash(surface_key, selected, text),
    )
