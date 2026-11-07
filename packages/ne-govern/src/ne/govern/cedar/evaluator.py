# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""In-process cedarpy evaluator for the ``decision.cedar`` bundle.

This module is DELIBERATELY not a :class:`~ne.contracts.governance.PolicyDecisionPoint`.
:class:`CedarEvaluator` speaks the real Cedar engine (``cedarpy.is_authorized``)
and returns a private :class:`_RawCedarResult` — it CANNOT mint a
:class:`~ne.contracts.governance.PolicyDecision` and is NOT exported from
:mod:`ne.govern` (nor from :mod:`ne.govern.cedar`). The only path to a
``PolicyDecision`` on this backend is
:class:`ne.govern.cedar.pdp.CedarGovernedPdp`, which couples every decision to
an audit append (C6), mirroring :class:`ne.govern.opa.OpaClient` /
:class:`ne.govern.pdp.GovernedPdp` file-for-file. Every parse failure, every
evaluator exception, and every totality/exclusivity violation fails CLOSED as a
:class:`~ne.govern.errors.PolicyBackendError` — never open.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

import cedarpy

from ne.govern.errors import PolicyBackendError

_VALID_EFFECTS = frozenset({"allow", "deny", "require-approval"})
_VALID_TIERS = frozenset({None, 0, 1, 2, 3})

# Fixed, non-attacker-controlled Cedar UIDs. Every value the decision actually
# depends on travels in ``context`` — no untrusted id is ever spliced into a
# Cedar surface-syntax string, and ``resource`` is never read by any rule
# (identical to the Rego, which also never inspects ``input.resource``).
_PRINCIPAL_UID = 'NE::Principal::"input"'
_ACTION_UID = 'NE::Action::"decide"'
_RESOURCE_UID = 'NE::Resource::"input"'

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1

_ANNOTATION_RE = re.compile(r'@(\w+)\("((?:[^"\\]|\\.)*)"\)')
_BODY_RE = re.compile(
    r"(?ms)^(permit|forbid)\s*\(principal,\s*action,\s*resource\)\s*when\s*\{(.+)\}\s*$"
)


def _unescape(raw: str) -> str:
    """Undo the ``\\"`` / ``\\\\`` escaping used inside an annotation string."""
    return raw.replace('\\"', '"').replace("\\\\", "\\")


@dataclass(frozen=True)
class _RawCedarResult:
    """The raw decision Cedar matched — NOT a ``PolicyDecision``.

    Intentionally a private, unexported dataclass mirroring ``opa._RawOpaResult``
    so no caller can mistake it for the frozen contract type. ``CedarGovernedPdp``
    is the sole translator to a ``PolicyDecision``, and only after the audit
    append succeeds.
    """

    effect: str
    policy_version: str
    reason: str
    tier: int | None
    approval_window_seconds: float | None = None
    #: Bundle 1.4.0 (see ``tiers.rego``'s provenance note): TRUE only when the
    #: window above came from a DEPLOYMENT DECLARATION — ``approval_windows
    #: ["actions"][action]`` or ``["tiers"][tier]`` — and FALSE for the built-in
    #: floor and for every ``None`` window. Mirrors Rego's
    #: ``approval_window_declared`` bit-for-bit; the two engines must agree here
    #: for the same reason they must agree on the window itself.
    approval_window_declared: bool = False
    standing_approval_seconds: float | None = None
    obligations: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _CedarRule:
    """One parsed rule from ``decision.cedar`` — annotations plus a synthetic probe."""

    rule_id: str
    effect: str
    tier: int | None
    reason: str
    obligations: dict[str, object]
    policy_version: str
    cedar_effect: str
    probe: str


def _parse_tier(rule_id: str, raw: str) -> int | None:
    if raw == "null":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise PolicyBackendError(
            f"cedar bundle rule {rule_id!r}: ne_tier {raw!r} is not 'null' or an integer"
        ) from exc
    if value not in _VALID_TIERS:
        raise PolicyBackendError(
            f"cedar bundle rule {rule_id!r}: ne_tier {value} is not in {{null, 0, 1, 2, 3}}"
        )
    return value


def _parse_obligations(rule_id: str, raw: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise PolicyBackendError(
            f"cedar bundle rule {rule_id!r}: ne_obligations is not valid JSON: {raw!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise PolicyBackendError(
            f"cedar bundle rule {rule_id!r}: ne_obligations does not parse to an object: {raw!r}"
        )
    return parsed


def _decomment(block: str) -> str:
    lines = [line for line in block.splitlines() if not line.lstrip().startswith("//")]
    return "\n".join(lines)


def _parse_bundle(text: str) -> list[_CedarRule]:
    rules: list[_CedarRule] = []
    seen_ids: set[str] = set()
    policy_versions: set[str] = set()

    for block in text.split(";"):
        decommented = _decomment(block)
        if not decommented.strip():
            continue
        annotations = dict(_ANNOTATION_RE.findall(decommented))
        match = _BODY_RE.search(decommented)
        if match is None:
            raise PolicyBackendError(
                "cedar bundle: a non-comment block does not match a single "
                f"'permit|forbid (principal, action, resource) when {{...}}' rule: "
                f"{decommented[:200]!r}"
            )
        cedar_effect, cond = match.group(1), match.group(2)

        rule_id = annotations.get("id")
        if not rule_id:
            raise PolicyBackendError('cedar bundle: rule is missing an @id("...") annotation')
        if rule_id in seen_ids:
            raise PolicyBackendError(f"cedar bundle: duplicate rule id {rule_id!r}")
        seen_ids.add(rule_id)

        ne_effect = annotations.get("ne_effect")
        if ne_effect is None:
            raise PolicyBackendError(f"cedar bundle rule {rule_id!r}: missing @ne_effect")
        ne_effect = _unescape(ne_effect)
        if ne_effect not in _VALID_EFFECTS:
            raise PolicyBackendError(
                f"cedar bundle rule {rule_id!r}: ne_effect {ne_effect!r} not in "
                f"{sorted(_VALID_EFFECTS)}"
            )

        tier_raw = annotations.get("ne_tier")
        if tier_raw is None:
            raise PolicyBackendError(f"cedar bundle rule {rule_id!r}: missing @ne_tier")
        tier = _parse_tier(rule_id, _unescape(tier_raw))

        reason_raw = annotations.get("ne_reason")
        if reason_raw is None:
            raise PolicyBackendError(f"cedar bundle rule {rule_id!r}: missing @ne_reason")
        reason = _unescape(reason_raw)

        obligations_raw = annotations.get("ne_obligations")
        if obligations_raw is None:
            raise PolicyBackendError(f"cedar bundle rule {rule_id!r}: missing @ne_obligations")
        obligations = _parse_obligations(rule_id, _unescape(obligations_raw))

        policy_version = annotations.get("policy_version")
        if not policy_version:
            raise PolicyBackendError(
                f"cedar bundle rule {rule_id!r}: missing or empty @policy_version"
            )
        policy_version = _unescape(policy_version)
        policy_versions.add(policy_version)

        # Conservatism guardian (C12 belt-and-suspenders): an "allow" rule MUST
        # be a Cedar `permit`; every "deny"/"require-approval" rule MUST be a
        # Cedar `forbid`. A mismatch here would mean a plain-Cedar consumer of
        # this bundle could allow more than the NE semantics intend — reject at
        # load time rather than let it through.
        expected_keyword = "permit" if ne_effect == "allow" else "forbid"
        if cedar_effect != expected_keyword:
            raise PolicyBackendError(
                f"cedar bundle rule {rule_id!r}: ne_effect {ne_effect!r} requires Cedar keyword "
                f"{expected_keyword!r}, found {cedar_effect!r} (C12 conservatism guardian)"
            )

        # C12 backstop: tier 2/3 can never resolve to "allow".
        if tier is not None and tier >= 2 and ne_effect == "allow":
            raise PolicyBackendError(
                f"cedar bundle rule {rule_id!r}: tier {tier} carries ne_effect 'allow' "
                "(C12: tier-2/3 actions must never resolve to allow)"
            )

        probe = f"permit (principal, action, resource) when {{{cond}}};"
        rules.append(
            _CedarRule(
                rule_id=rule_id,
                effect=ne_effect,
                tier=tier,
                reason=reason,
                obligations=obligations,
                policy_version=policy_version,
                cedar_effect=cedar_effect,
                probe=probe,
            )
        )

    if len(rules) != 8:
        raise PolicyBackendError(f"cedar bundle: expected exactly 8 rules, found {len(rules)}")
    if len(policy_versions) != 1:
        raise PolicyBackendError(
            "cedar bundle: all rules must share one @policy_version, found "
            f"{sorted(policy_versions)}"
        )
    return rules


def _declared_tier_from_context(context: object) -> int | None:
    """Marshal a caller-declared tier EXACTLY like ``tiers.rego``'s ``valid_declared``.

    Ints and integral floats within the i64 range are admitted (as an int);
    booleans, strings, fractional floats, and out-of-i64-range values are
    OMITTED (never included in the Cedar context) so the ``Dk`` fragments in
    ``decision.cedar`` can never fire for them. The 0..3 range check itself is
    left to Cedar's ``== k`` equality on the admitted value.
    """
    if not isinstance(context, Mapping):
        return None
    declared = context.get("tier")
    if isinstance(declared, bool):
        return None
    if isinstance(declared, int):
        if _I64_MIN <= declared <= _I64_MAX:
            return declared
        return None
    if isinstance(declared, float):
        if declared.is_integer() and _I64_MIN <= declared <= _I64_MAX:
            return int(declared)
        return None
    return None


def _context_provisional_from_context(context: object) -> bool | None:
    """Marshal ``context_provisional`` EXACTLY like ``tiers.rego``'s ``provisional_context``.

    ``tiers.rego`` compares against the JSON boolean literal ``true`` only
    (``input.context.context_provisional == true``) — a string ``"true"``, the
    number ``1``, or any other truthy-looking value never matches Rego's
    type-strict equality. Mirrored here bit-for-bit: only the Python ``bool``
    value ``True`` is admitted into the Cedar context at all; every other
    value (including ``False``, a no-op either way) is OMITTED so the Cedar
    ``ESC`` fragment's ``context has context_provisional`` can never see it —
    the same can-never-fire discipline as :func:`_declared_tier_from_context`.
    """
    if not isinstance(context, Mapping):
        return None
    provisional = context.get("context_provisional")
    return True if provisional is True else None


#: ``tiers.rego``'s built-in STANDING-approval floors, mirrored bit-for-bit
#: (``default_standing_approval_seconds``): tier 2 gets 7 days, tier 3 gets
#: 2 days. The asymmetry is deliberate and is the mirror's job to preserve —
#: tier 3 is irreversible, and what a standing approval concedes is that the
#: decision may be OLDER than the execution it authorizes.
_DEFAULT_STANDING_APPROVAL_SECONDS: dict[str, float] = {"2": 604800.0, "3": 172800.0}

#: ``tiers.rego``'s built-in approval-window floors, mirrored bit-for-bit
#: (``default_approval_window_seconds``): a tier-2 per-item approval defaults to
#: 4 hours, a tier-3 named-approver decision to 24 hours. Keys are the tier as a
#: string, exactly like the Rego document (``format_int(final_tier, 10)``).
#:
#: ⛔ NEITHER FLOOR IS HONOURABLE ON A DEPLOYMENT SHIPPED TODAY — read
#: ``tiers.rego``'s own note beside these numbers. The run owner's frozen
#: authorization expires on its own clock (900 s), so a parked effect promised
#: four hours cannot execute past minute fifteen.
#:
#: ⚠️ WHAT THAT DOES *NOT* MEAN, corrected from an earlier revision of this
#: comment: ``ne.runtime.middleware._require_honourable_approval_window`` does
#: NOT refuse a park on these floors. It refuses only a window a deployment
#: DECLARED; on a floor it warns and parks. So configuring one engine and not the
#: other diverges in the OPPOSITE direction from what this note used to claim:
#: the CONFIGURED engine's declared 180 s is what can be refused when a horizon
#: is shorter still, while the UNCONFIGURED engine resolves the floor and parks.
#: A Cedar deployment must still pass ``approval_windows`` explicitly, exactly as
#: an OPA deployment supplies the data document — not to avoid refusals, but
#: because a floor-resolved park writes a four-hour promise it will not keep.
#: ``CedarGovernedPdp.from_settings`` builds a bare ``CedarEvaluator()``, so Cedar
#: is currently the UNCONFIGURED side; OPA (``NE_GOVERN_OPA_URL``) is what this
#: deployment runs.
_DEFAULT_APPROVAL_WINDOW_SECONDS: dict[str, float] = {"2": 14400.0, "3": 86400.0}


def _valid_window(window: object) -> float | None:
    """A window override is honoured only when Rego's would be: a positive number.

    Mirrors ``tiers.rego``'s ``is_number(window); window > 0`` guard. ``bool``
    is rejected explicitly — Rego's ``is_number`` can never match a JSON
    boolean, but ``isinstance(True, int)`` is True in Python and a coerced
    ``True`` would mint a one-second window. Non-finite floats are rejected too:
    they are unrepresentable in the JSON ``data`` document Rego reads, so
    honouring one here would be a divergence by construction.
    """
    if isinstance(window, bool) or not isinstance(window, int | float):
        return None
    value = float(window)
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _extraction_mode_from_context(context: object) -> str | None:
    """Marshal ``extraction_mode`` EXACTLY like ``tiers.rego``'s ``yolo_exempt``.

    ``tiers.rego`` compares against the string literal ``"autonomous"``
    (``input.context.extraction_mode == "autonomous"``); a non-string value can
    never equal that literal, so it is OMITTED here — an absent key and a
    non-string value collapse to the identical "fails toward escalation" case
    on both backends.
    """
    if not isinstance(context, Mapping):
        return None
    mode = context.get("extraction_mode")
    return mode if isinstance(mode, str) else None


class CedarEvaluator:
    """Evaluates a request against the packaged ``decision.cedar`` bundle.

    Loaded via :func:`importlib.resources.files` (never a ``__file__`` path —
    the tracked systemic wheel-path hazard), so it works from an installed
    wheel as well as an editable/source checkout.
    """

    def __init__(
        self,
        policy_path: Path | None = None,
        *,
        approval_windows: Mapping[str, Mapping[str, object]] | None = None,
        standing_approvals: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        # ADR-0030: ``approval_windows`` is the SAME deployment document OPA
        # reads at ``data.ne.config.approval_windows`` ({"actions": {...},
        # "tiers": {...}}, seconds). Cedar has no ``data`` mechanism and the
        # bundle's obligations are static annotations, so the per-action window
        # is resolved HERE, in the marshaller that already mirrors tiers.rego's
        # context marshalling bit-for-bit. A deployment that overrides windows
        # for OPA must hand the identical mapping to this constructor — the
        # same consistency requirement as keeping the two bundles'
        # policy_version in step. Default None: only the built-in floors apply,
        # which is also OPA's behaviour with no data document configured.
        self._approval_windows: Mapping[str, Mapping[str, object]] = approval_windows or {}
        # F1 (bundle 1.5.0): the SECOND clock, read from the same deployment
        # document OPA reads at ``data.ne.config.standing_approvals`` — same
        # {"actions": {...}, "tiers": {...}} shape, same seconds, resolved by
        # the same most-specific-first order. It is a separate constructor
        # argument rather than a key inside ``approval_windows`` because the
        # two are separate OPA data documents; collapsing them here would make
        # the parity claim untestable against the real Rego layout.
        self._standing_approvals: Mapping[str, Mapping[str, object]] = standing_approvals or {}
        if policy_path is None:
            resource = files("ne.govern.cedar").joinpath("decision.cedar")
            try:
                text = resource.read_text(encoding="utf-8")
            except OSError as exc:
                raise PolicyBackendError(f"cannot read packaged cedar bundle: {exc}") from exc
        else:
            try:
                text = policy_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise PolicyBackendError(f"cannot read cedar bundle {policy_path}: {exc}") from exc
        self._rules = _parse_bundle(text)
        self._policy_version = self._rules[0].policy_version

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def evaluate(self, input_doc: Mapping[str, object]) -> _RawCedarResult:
        """Evaluate ``input_doc`` (a ``PolicyRequest.model_dump(mode="json")``)."""
        principal_obj = input_doc.get("principal")
        raw_pid = principal_obj.get("id") if isinstance(principal_obj, Mapping) else None
        principal_id = raw_pid if isinstance(raw_pid, str) else ""

        raw_action = input_doc.get("action")
        ne_action = raw_action if isinstance(raw_action, str) else ""

        raw_context = input_doc.get("context")
        declared_tier = _declared_tier_from_context(raw_context)
        context_provisional = _context_provisional_from_context(raw_context)
        extraction_mode = _extraction_mode_from_context(raw_context)

        context: dict[str, object] = {"principal_id": principal_id, "ne_action": ne_action}
        if declared_tier is not None:
            context["declared_tier"] = declared_tier
        if context_provisional is not None:
            context["context_provisional"] = context_provisional
        if extraction_mode is not None:
            context["extraction_mode"] = extraction_mode

        request = {
            "principal": _PRINCIPAL_UID,
            "action": _ACTION_UID,
            "resource": _RESOURCE_UID,
            "context": context,
        }

        matched: list[_CedarRule] = []
        for rule in self._rules:
            try:
                result = cedarpy.is_authorized(request, rule.probe, [])
            except Exception as exc:  # fail closed on ANY evaluator error, matching OpaClient
                raise PolicyBackendError(
                    f"cedar evaluator failed on rule {rule.rule_id!r}: {exc}"
                ) from exc
            if result.decision == cedarpy.Decision.Allow:
                matched.append(rule)

        if len(matched) != 1:
            raise PolicyBackendError(
                "cedar decision not a total mutually-exclusive function: "
                f"{len(matched)} rules matched ({[r.rule_id for r in matched]}) for "
                f"action={ne_action!r} principal_id={principal_id!r} "
                f"declared_tier={declared_tier!r}"
            )

        winner = matched[0]
        window, declared = self._approval_window_for(ne_action, winner)
        return _RawCedarResult(
            effect=winner.effect,
            policy_version=winner.policy_version,
            reason=winner.reason,
            tier=winner.tier,
            approval_window_seconds=window,
            approval_window_declared=declared,
            standing_approval_seconds=self._standing_lifetime_for(ne_action, winner),
            obligations=dict(winner.obligations),
        )

    def _approval_window_for(self, action: str, winner: _CedarRule) -> tuple[float | None, bool]:
        """Mirror ``tiers.rego``'s window AND its provenance, most specific first.

        Resolution order, bit-for-bit with the Rego rule (which ``decision.rego``
        1.4.0 emits first-class on its four require-approval rules and as
        ``null`` everywhere else):

          1. exact action override — ``approval_windows["actions"][action]``;
          2. per-tier override    — ``approval_windows["tiers"][str(tier)]``;
          3. the built-in floor   — 4 h (tier 2) / 24 h (tier 3).

        Keyed on the WINNER's tier, which for every require-approval rule in the
        bundle IS ``final_tier`` (escalated rules carry their escalated tier).
        Allow/deny winners answer ``None`` — an autonomous action has no
        approval to time-box, and Rego's decision carries ``null`` there. An
        invalid override (non-number, bool, zero, negative, non-finite) falls
        through to the next step exactly like Rego's ``is_number``/``> 0``
        guard — a garbage override must degrade to a LONGER answer path, never
        to an instantly-stale approval.

        The second element is the PROVENANCE bit (bundle 1.4.0): ``True`` for
        steps 1 and 2 — a window this deployment DECLARED — and ``False`` for
        step 3's built-in floor and for the ``None`` answer. It must track the
        window, because the runtime's park-time coherence check refuses on a
        declaration and warns on a floor; a Cedar deployment that reported the
        floor as declared would refuse parks OPA lets through, which is the
        parity failure ADR-0030's consistency contract exists to prevent. An
        invalid override falls through with its provenance: a garbage number in
        ``actions`` is not a declaration, it is a typo.
        """
        if winner.effect != "require-approval" or winner.tier is None:
            return None, False
        actions = self._approval_windows.get("actions")
        if isinstance(actions, Mapping):
            window = _valid_window(actions.get(action))
            if window is not None:
                return window, True
        tier_key = str(winner.tier)
        tiers = self._approval_windows.get("tiers")
        if isinstance(tiers, Mapping):
            window = _valid_window(tiers.get(tier_key))
            if window is not None:
                return window, True
        return _DEFAULT_APPROVAL_WINDOW_SECONDS.get(tier_key), False

    def _standing_lifetime_for(self, action: str, winner: _CedarRule) -> float | None:
        """Mirror ``tiers.rego``'s ``standing_approval_seconds``, most specific first.

        The SECOND clock, resolved exactly like the window and from the same
        shape of document, but answering a different question: how long the
        approver's decision stays usable after the attempt it was asked for
        terminalized ``authorization_expired``.

          1. exact action override — ``standing_approvals["actions"][action]``;
          2. per-tier override    — ``standing_approvals["tiers"][str(tier)]``;
          3. the built-in floor   — 7 d (tier 2) / 2 d (tier 3).

        Allow/deny winners answer ``None``: an autonomous action has no
        decision to carry forward, and Rego's decision carries ``null`` there.
        An invalid override degrades to the next step exactly like the window's
        — never to zero, which would lapse every decision the instant it was
        made.
        """
        if winner.effect != "require-approval" or winner.tier is None:
            return None
        actions = self._standing_approvals.get("actions")
        if isinstance(actions, Mapping):
            lifetime = _valid_window(actions.get(action))
            if lifetime is not None:
                return lifetime
        tier_key = str(winner.tier)
        tiers = self._standing_approvals.get("tiers")
        if isinstance(tiers, Mapping):
            lifetime = _valid_window(tiers.get(tier_key))
            if lifetime is not None:
                return lifetime
        return _DEFAULT_STANDING_APPROVAL_SECONDS.get(tier_key)
