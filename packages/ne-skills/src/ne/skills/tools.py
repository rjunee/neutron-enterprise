# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The two governed discovery tools: ``read.skills.search`` + ``read.skills.view``.

Both are ``ApprovalTier.TIER_0`` (the ``read.`` prefix classifies tier 0). The
handlers are async, ``ToolHandler``-shaped ``Callable[[Mapping], Awaitable[str]]``
— ne-skills does NOT import ne-runtime, so the ne-api composition root pairs these
handlers into ``ToolBinding``s. The calling principal reaches a handler ONLY via
the injected ``principal_provider`` seam (fail-closed on ``None``).

R-28 discipline: every handler reads packs through the registry's hash-verified
methods (``verified_current_packs`` / ``get_current`` / ``verified_pinned_pack``),
so a tampered row never surfaces on ANY read path. ``read.skills.view`` IS the
firing — it is the SOLE writer of ``ne_skill_invocations`` rows (R-26).

RUN-FROZEN RESOLUTION, AND WHY IT FAILS CLOSED. ``read.skills.view`` resolves the
CURRENT version only on a surface with NO pin provider configured at all — the
legacy/unpinned surface, byte-identical to before this seam existed. A composition
root that runs an Agent against Run-frozen Skill pins injects a
:data:`PinProvider` (the ambient-carrier shape ``principal_provider`` already
uses), and the handler then serves the EXACT pinned version, so a rollover mid-Run
cannot swap the procedure text under a running Agent.

Once a provider IS configured, EVERY way it can fail to name a pin for the
requested skill is a REFUSAL, never a downgrade: no Run in scope (``None``), a Run
that froze nothing (empty mapping), a Run that did not freeze THIS name, or a
provider that raises. All four raise the common
:class:`~ne.skills.errors.SkillNotFoundError` — identical message, no diagnostic,
no oracle. A configured-but-unusable pin path must never serve a Run something
other than what it froze; silently substituting the current version would be the
exact substitution the pin exists to prevent, made invisible.

That distinction — "no provider configured" versus "a configured provider that
yielded nothing" — is therefore load-bearing, and it is carried STRUCTURALLY by
the :data:`UNPINNED_SURFACE` sentinel rather than inferred from falsiness. An
empty pin mapping is falsy AND must fail closed, so falsiness cannot be the test.
"""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any, Final

import anyio.to_thread

from ne.contracts.runtime import ToolSpec
from ne.contracts.skills import SkillInvocation, SkillPack
from ne.contracts.types import ApprovalTier, Principal
from ne.skills.config import SkillsSettings
from ne.skills.errors import PrincipalUnboundError, SkillNotFoundError
from ne.skills.registry import SkillPin, SqlAlchemySkillRegistry
from ne.skills.standard import CONTROL_CHAR_RE
from ne.skills.visibility import GroupExpander, match_labels, visible

_LOG = logging.getLogger(__name__)

SEARCH_TOOL_NAME = "read.skills.search"
VIEW_TOOL_NAME = "read.skills.view"

#: A no-arg principal resolver injected by the composition root.
PrincipalProvider = Callable[[], Principal | None]

#: A no-arg resolver for the CURRENT Run's frozen Skill pins, keyed by skill name.
#:
#: Returning ``None`` means "no Run is in scope"; returning an EMPTY mapping means
#: "a Run that froze no Skills". Both are REFUSALS once a provider is configured —
#: see the module docstring. Neither means "fall back to the current version".
PinProvider = Callable[[], Mapping[str, SkillPin] | None]


class _UnpinnedSurface:
    """The type of :data:`UNPINNED_SURFACE`. Never instantiated elsewhere."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "UNPINNED_SURFACE"


#: The ``pin_provider`` default: NO pin provider is configured on this surface.
#:
#: A distinct sentinel OBJECT, not ``None``, because ``None`` is a legitimate
#: *return value* of a configured :data:`PinProvider` (no Run in scope) and the two
#: must lead to opposite outcomes — current-version resolution versus a refusal.
#: Collapsing them into one value is the bug this sentinel exists to make
#: unrepresentable; ``isinstance`` is the test, so no falsiness rule can reintroduce
#: it.
UNPINNED_SURFACE: Final[_UnpinnedSurface] = _UnpinnedSurface()


def build_skill_tool_specs() -> tuple[ToolSpec, ToolSpec]:
    """The two frozen tool specs (tier 0 read tools)."""
    search = ToolSpec(
        name=SEARCH_TOOL_NAME,
        description="Search the governed skill library for procedures by intent",
        tier=ApprovalTier.TIER_0,
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    view = ToolSpec(
        name=VIEW_TOOL_NAME,
        description=("Load a skill's procedure by name (this call is the audited firing)"),
        tier=ApprovalTier.TIER_0,
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact skill name from the activated-skills index",
                }
            },
            "required": ["name"],
        },
    )
    return search, view


# -- D-3 search scorer (behind a function seam) ---------------------------------


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def score_pack(query: str, pack: SkillPack) -> float:
    """Token-overlap + a full trigger-phrase substring bonus (deterministic)."""
    q_tokens = set(_tokens(query))
    if not q_tokens:
        return 0.0
    corpus = " ".join([pack.name, pack.description, *pack.triggers])
    corpus_tokens = set(_tokens(corpus))
    overlap = len(q_tokens & corpus_tokens) / max(1, len(q_tokens))
    lowered = query.lower()
    bonus = sum(1.0 for trigger in pack.triggers if trigger.lower() in lowered)
    return overlap + bonus


def search_packs(query: str, packs: tuple[SkillPack, ...], top_k: int) -> tuple[SkillPack, ...]:
    """Top-k packs by (score desc, name asc), keeping only score > 0."""
    scored = [(score_pack(query, p), p) for p in packs]
    scored = [(s, p) for s, p in scored if s > 0]
    scored.sort(key=lambda sp: (-sp[0], sp[1].name))
    return tuple(p for _s, p in scored[:top_k])


# -- handler factories ----------------------------------------------------------


def build_search_handler(
    registry: SqlAlchemySkillRegistry,
    expander: GroupExpander,
    settings: SkillsSettings,
    principal_provider: PrincipalProvider,
) -> Callable[[Mapping[str, object]], Coroutine[Any, Any, str]]:
    """The ``read.skills.search`` handler — ACL-trim BEFORE scoring."""

    async def _search(args: Mapping[str, object]) -> str:
        principal = principal_provider()
        if principal is None:
            raise PrincipalUnboundError()
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query must not be empty")

        def _work() -> tuple[SkillPack, ...]:
            packs, _tampered = registry.verified_current_packs()
            match = match_labels(principal, expander)
            visible_packs = tuple(p for p in packs if visible(p.acl_labels, match))
            return search_packs(query, visible_packs, settings.search_top_k)

        results = await anyio.to_thread.run_sync(_work)
        if not results:
            return "no skills matched"
        # ONE RESULT ⇒ ONE LINE, structurally — the same flattening
        # ``active_set.render_index`` applies, for the same reason: this block is
        # newline-delimited, so a description carrying a line break would let one
        # pack forge additional "name: description" rows advertising skills the
        # principal was never shown. ``standard.lint_description`` keeps such a
        # description out of the library; this keeps it out of the rendering
        # whatever the registry holds. It is the identity on printable text.
        return "\n".join(CONTROL_CHAR_RE.sub(" ", f"{p.name}: {p.description}") for p in results)

    return _search


def build_view_handler(
    registry: SqlAlchemySkillRegistry,
    expander: GroupExpander,
    settings: SkillsSettings,
    principal_provider: PrincipalProvider,
    *,
    pin_provider: PinProvider | _UnpinnedSurface = UNPINNED_SURFACE,
) -> Callable[[Mapping[str, object]], Coroutine[Any, Any, str]]:
    """The ``read.skills.view`` handler — the audited firing + SOLE invocation writer.

    Leaving ``pin_provider`` at :data:`UNPINNED_SURFACE` (the default) keeps
    current-version resolution: the pre-seam behavior, and the ONLY path that may
    reach ``get_current``.

    Supplying a provider makes this the RUN-FROZEN read, resolved through
    :meth:`~ne.skills.registry.SqlAlchemySkillRegistry.verified_pinned_pack` at the
    Run's exact version + declared hash — and makes the surface fail closed. See
    the module docstring: a configured provider that yields ``None``, an empty
    mapping, no pin for this name, or that raises, ALL refuse with the same
    :class:`~ne.skills.errors.SkillNotFoundError` as an absent skill. None of them
    degrade to the current version.
    """

    async def _view(args: Mapping[str, object]) -> str:
        principal = principal_provider()
        if principal is None:
            raise PrincipalUnboundError()
        name = str(args.get("name", "")).strip()
        if not name:
            raise ValueError("name must not be empty")

        # Resolve ONCE, here in the async body, so the ambient carrier is read in
        # the caller's context rather than in the worker thread below.
        resolve: Callable[[], SkillPack | None]
        if isinstance(pin_provider, _UnpinnedSurface):
            # NOT CONFIGURED — the one and only route to current-version serving.
            resolve = functools.partial(registry.get_current, name)
        else:
            try:
                pins = pin_provider()
            except Exception:
                # A configured pin path that cannot be consulted is a BROKEN Run
                # scope, not an unpinned surface. Log the class only (C12) and
                # refuse; `from None` keeps the cause out of any traceback render,
                # matching the middleware's ActionValidationError discipline.
                _LOG.warning(
                    "skill %r pin provider raised — refusing (fail closed; the "
                    "Run-frozen version cannot be established)",
                    name,
                )
                raise SkillNotFoundError(name) from None
            if pins is None:
                # Configured, but no Run is in scope. NOT a fall-through.
                raise SkillNotFoundError(name)
            pin = pins.get(name)
            if pin is None:
                # An empty pin mapping lands here too: a Run that froze nothing
                # pinned nothing, so it can serve nothing.
                raise SkillNotFoundError(name)
            resolve = functools.partial(registry.verified_pinned_pack, pin)

        def _work() -> str:
            pack = resolve()
            if pack is None:
                # absent / retired / hash-tampered / wrong-version / wrong-hash —
                # indistinguishable (no oracle).
                raise SkillNotFoundError(name)
            match = match_labels(principal, expander)
            if not visible(pack.acl_labels, match):
                raise SkillNotFoundError(name)
            session_id = str(principal.claims.get("sid") or "")
            # VIEW-WRITE-FAILURE SEMANTICS (§6.5): the invocation row is BEST-EFFORT
            # telemetry. The governed tool.execute audit event (hash-chained) is the
            # canonical firing record from which counts are reconcilable, so a
            # dropped telemetry row can only UNDERCOUNT (which merely DELAYS
            # retirement — the safe direction), never blocks skill use nor corrupts
            # a lifecycle decision. A telemetry-write outage must NEVER deny serving.
            try:
                registry.append_invocation(
                    SkillInvocation(
                        skill_name=pack.name,
                        version=pack.version,
                        principal_id=principal.id,
                        session_id=session_id,
                        fired_at=datetime.now(UTC),
                    )
                )
            except Exception:
                _LOG.warning(
                    "skill %r invocation telemetry write failed — serving anyway "
                    "(best-effort; audit chain canonical)",
                    name,
                )
            return f"# {pack.name} (v{pack.version})\n\n{pack.body}"

        return await anyio.to_thread.run_sync(_work)

    return _view
