# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Layer 1 of verified-answer-first: a PURE, deterministic prompt->template matcher.

Purity contract: this module imports STDLIB ONLY (``unicodedata``, ``re``,
``enum``, ``dataclasses``). No model call, no I/O, no clock, no randomness, no
global mutable state — every function here is a pure function of its
arguments, so ``normalize``/``match_template``/``match_selection`` are 100%
offline-testable and their output is fully determined by their input (C3: this
package — and this module in particular — imports NOTHING from ``ne.runtime``
or ``ne.govern``; see the ``.importlinter`` ``brain-ontology-lane`` contract).

Precision over recall: a MIS-ROUTE is worse than a refusal or a clarifying
question. Every ambiguous signal below resolves toward the SAFER outcome
(``CLARIFY``/``PASS``), never a guess:

- Triggers are matched as an ORDER-SENSITIVE token subsequence (gaps allowed,
  order preserved) — "list every account i am cleared to see" does not match
  tokens reshuffled into "cleared to see every account list".
- A NEGATION token (don't/dont/not/no/never/without, post-normalization forms)
  preceding a matched span — trigger OR guard — vetoes the match to ``PASS``.
  This applies symmetrically to both trigger and guard spans: a negated
  trigger must never route any more than a negated guard should.
- A CO-JOINED second imperative (" and <verb>" or " then ") vetoes a would-be
  ROUTE down to ``CLARIFY`` — never silently drop the second clause.
- Guard terms are required (at load, see ``templates.RoutingSpec``) to be
  MULTI-WORD phrases, and are matched here as a CONTIGUOUS phrase (stricter
  than the trigger subsequence) — a guard hit with no trigger hit is a
  ``CLARIFY``, never a silent ``ROUTE``.
- Two or more domains hitting guards (or triggers) is a deterministic,
  sorted-by-domain ``CLARIFY`` listing every domain — never a silent pick.

Pinned normalization recipe (``normalize``), in this exact order — deviating
from this order can silently break the negation veto (an apostrophe mapped to
a space instead of deleted turns "don't" into ("don", "t"), losing the "dont"
token the veto set depends on):

1. Unicode NFKC normalization (folds full-width/compatibility forms, e.g. the
   "ﬁle" ligature -> "file", full-width Latin letters -> their ASCII form,
   once casefolded).
2. ``str.casefold()``.
3. DELETE apostrophes (U+0027 straight quote and U+2019 curly quote) via
   ``str.translate`` — a contraction like "don't" (either quote style) ->
   "dont" (matches the negation set below).
4. Replace every remaining non-word, non-whitespace character with a space.
5. Split on whitespace into a token TUPLE — an ordered MULTISET (stopwords are
   KEPT; determinism, not linguistic minimalism, is the goal).

``RouteOutcome.REFUSE`` exists as an enum value only: ``match_template`` NEVER
returns it in v1. The stateful CLARIFY-bound-exhausted -> REFUSE transition is
a property of the multi-turn conversation, owned by the router that calls
into this module (U3), not by this pure matcher.

DECIDED DEVIATION from the unit brief's minimal 5-field projection: this
module's ``RoutingRow`` also carries ``guard_terms`` and ``clarify_max``
because ``match_template``/callers need them; every field remains a plain
string/tuple[str, ...]/int so U3 can consume ``build_routing_table`` through a
structural ``Protocol`` with no import of this package at all (the same
inversion precedent as ``GroupExpander``/``principal_provider`` injection).
"""

from __future__ import annotations

import enum
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ne.brain_ontology.verified.templates import TemplateRegistry

__all__ = [
    "RouteDecision",
    "RouteOutcome",
    "RoutingRow",
    "build_routing_table",
    "match_selection",
    "match_template",
    "normalize",
]

#: Matches any char that is neither a "word" char nor whitespace; replaced with
#: a space so punctuation never glues two tokens together (step 4 of normalize).
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

#: Apostrophe variants deleted (not replaced with a space) in step 3, so a
#: contraction collapses to a single token (e.g. "dont") — see module
#: docstring. Both the straight quote (U+0027) and the curly/typographic
#: right single quotation mark (U+2019, referenced by escape to avoid an
#: ambiguous-unicode lint hit) are covered.
_APOSTROPHE_TRANSLATION = {ord("'"): None, ord("\u2019"): None}

#: Post-normalization negation tokens. A match preceded by any of these tokens
#: is vetoed to PASS. Pinned — do not extend without re-auditing every test.
_NEGATION_TOKENS: frozenset[str] = frozenset({"dont", "not", "no", "never", "without"})

#: Verbs that, when co-joined via " and <verb>", mark a compound imperative.
#: Pinned — do not extend without re-auditing every test.
_COMPOUND_VERBS: frozenset[str] = frozenset(
    {
        "email",
        "send",
        "forward",
        "delete",
        "remove",
        "tell",
        "write",
        "list",
        "show",
        "update",
        "create",
        "share",
        "post",
        "call",
        "message",
        "notify",
        "export",
        "print",
        "summarize",
        "summarise",
    }
)


class RouteOutcome(enum.StrEnum):
    """The four possible ``match_template`` outcomes (str-valued: comparable to
    plain literals like ``outcome == "route"`` with no import of this enum).

    ``REFUSE`` is never produced by :func:`match_template` in v1 — it is
    reserved for the stateful clarify-bound-exhausted transition owned by the
    router in ne-runtime (U3).
    """

    ROUTE = "route"
    CLARIFY = "clarify"
    REFUSE = "refuse"
    PASS = "pass"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """A plain-data verdict: strings/enum only, no ontology objects.

    ``domains`` is always populated and sorted on ``CLARIFY`` (every domain
    that hit), and empty otherwise. ``domain`` is a convenience — the first
    (sorted) entry of ``domains`` on ``CLARIFY``/``ROUTE``, ``None`` on
    ``PASS``/``REFUSE``.
    """

    outcome: RouteOutcome
    template_id: str | None = None
    domain: str | None = None
    domains: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoutingRow:
    """A plain-data projection of one routed :class:`QueryTemplate`.

    Every field is a ``str``, ``tuple[str, ...]``, or ``int`` — no ontology
    objects — so ``ne.runtime`` (U3) can declare a structural ``Protocol``
    over the fields it needs and consume this WITHOUT importing
    ``ne.brain_ontology`` at all.
    """

    domain: str
    template_id: str
    tool_name: str
    description: str
    triggers: tuple[str, ...]
    guard_terms: tuple[str, ...]
    clarify_max: int


def normalize(text: str) -> tuple[str, ...]:
    """PINNED normalization: NFKC -> casefold -> delete apostrophes -> punctuation
    to space -> whitespace-split token tuple (an ORDERED MULTISET; stopwords
    are kept). Deterministic: same input -> same output, always.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    no_apostrophes = folded.translate(_APOSTROPHE_TRANSLATION)
    spaced = _PUNCT_RE.sub(" ", no_apostrophes)
    return tuple(spaced.split())


def _find_subsequence(needle: tuple[str, ...], haystack: tuple[str, ...]) -> int | None:
    """Index of the FIRST haystack token consumed by an in-ORDER (gaps allowed)
    match of every ``needle`` token, or ``None`` if no such match exists.

    An empty ``needle`` NEVER matches (guards against an empty-normalizing
    trigger becoming a universal match — see the load-time gate in
    ``templates.RoutingSpec``).
    """
    if not needle:
        return None
    start: int | None = None
    pos = 0
    for token in needle:
        found = None
        for i in range(pos, len(haystack)):
            if haystack[i] == token:
                found = i
                break
        if found is None:
            return None
        if start is None:
            start = found
        pos = found + 1
    return start


def _find_phrase(needle: tuple[str, ...], haystack: tuple[str, ...]) -> int | None:
    """Index of the first CONTIGUOUS run of ``haystack`` equal to ``needle``,
    or ``None``. An empty ``needle`` never matches.
    """
    if not needle:
        return None
    n = len(needle)
    for i in range(0, len(haystack) - n + 1):
        if haystack[i : i + n] == needle:
            return i
    return None


def _is_negated(tokens: tuple[str, ...], span_start: int) -> bool:
    """True if any token BEFORE ``span_start`` is a negation token."""
    return any(token in _NEGATION_TOKENS for token in tokens[:span_start])


def _has_compound(tokens: tuple[str, ...]) -> bool:
    """True if the prompt carries a co-joined second imperative: a bare
    ``then``, or ``and <verb>`` where ``<verb>`` is in ``_COMPOUND_VERBS``.
    """
    if "then" in tokens:
        return True
    for i, token in enumerate(tokens[:-1]):
        if token == "and" and tokens[i + 1] in _COMPOUND_VERBS:
            return True
    return False


def match_template(prompt: str, rows: Sequence[RoutingRow]) -> RouteDecision:
    """The pure, deterministic, precision-guarded matcher.

    Never depends on the input ORDER of ``rows`` (every multi-hit result is
    sorted); never depends on dict iteration order; no randomness.
    """
    p = normalize(prompt)
    if not p:
        return RouteDecision(outcome=RouteOutcome.PASS)

    compound = _has_compound(p)
    ordered_rows = sorted(rows, key=lambda r: r.template_id)

    # (1) TRIGGERS — order-sensitive subsequence match, negation-veto'd.
    trigger_hit_rows: list[RoutingRow] = []
    for row in ordered_rows:
        hit = False
        for trigger in row.triggers:
            idx = _find_subsequence(normalize(trigger), p)
            if idx is not None and not _is_negated(p, idx):
                hit = True
                break
        if hit:
            trigger_hit_rows.append(row)

    if trigger_hit_rows:
        hit_domains = tuple(sorted({row.domain for row in trigger_hit_rows}))
        if len(trigger_hit_rows) == 1 and not compound:
            (only,) = trigger_hit_rows
            return RouteDecision(
                outcome=RouteOutcome.ROUTE,
                template_id=only.template_id,
                domain=only.domain,
            )
        # Either a compound co-joined imperative (never ROUTE — surface the
        # dropped clause) or 2+ distinct templates hit triggers (never pick
        # one silently): both resolve to the same CLARIFY shape.
        return RouteDecision(
            outcome=RouteOutcome.CLARIFY,
            domain=hit_domains[0],
            domains=hit_domains,
        )

    # (2) GUARDS — contiguous phrase match, negation-veto'd.
    guard_hit_rows: list[RoutingRow] = []
    for row in ordered_rows:
        hit = False
        for guard in row.guard_terms:
            idx = _find_phrase(normalize(guard), p)
            if idx is not None and not _is_negated(p, idx):
                hit = True
                break
        if hit:
            guard_hit_rows.append(row)

    if not guard_hit_rows:
        return RouteDecision(outcome=RouteOutcome.PASS)

    guard_domains = tuple(sorted({row.domain for row in guard_hit_rows}))
    return RouteDecision(
        outcome=RouteOutcome.CLARIFY,
        domain=guard_domains[0],
        domains=guard_domains,
    )


def match_selection(reply: str, domain_options: Sequence[RoutingRow]) -> str | None:
    """The clarify-follow-up grammar: map a short reply to one option's
    ``template_id``, or ``None`` if it cannot be resolved unambiguously.

    Accepts an ordinal (``1``/``(1)``/``option 1``/``#1``), a bare letter
    (``a``/``(a)``), or an all-tokens-in-order echo of an option's canonical
    trigger (``triggers[0]``). Anything else — including an ambiguous echo
    that matches 2+ options — returns ``None`` (precision over recall: never
    guess which option the caller meant).

    The canonical-trigger echo path applies the SAME negation veto as
    ``match_template``'s trigger path: a reply that negates the echoed
    trigger span (e.g. "don't list every unpaid invoice") does not resolve
    to that option — it returns ``None`` rather than executing an action the
    reply explicitly rejected.
    """
    r = normalize(reply)
    opts = sorted(domain_options, key=lambda o: o.template_id)
    if not r or not opts:
        return None

    candidate: str | None = None
    if len(r) == 1:
        candidate = r[0]
    elif len(r) == 2 and r[0] == "option":
        candidate = r[1]

    if candidate is not None:
        if candidate.isdigit():
            n = int(candidate)
            if 1 <= n <= len(opts):
                return opts[n - 1].template_id
            return None
        if len(candidate) == 1 and candidate in "abcdefghijklmnopqrstuvwxyz":
            i = ord(candidate) - ord("a")
            if i < len(opts):
                return opts[i].template_id
            return None

    matches: list[RoutingRow] = []
    for opt in opts:
        if not opt.triggers:
            continue
        idx = _find_subsequence(normalize(opt.triggers[0]), r)
        if idx is not None and not _is_negated(r, idx):
            matches.append(opt)
    if len(matches) == 1:
        return matches[0].template_id
    return None


def build_routing_table(registry: TemplateRegistry) -> tuple[RoutingRow, ...]:
    """Plain-data projection of every ROUTED template in ``registry``, sorted
    by ``template_id``. Templates with no ``[routing]`` block are excluded.

    Only ATTRIBUTE ACCESS on ``registry``/its templates is used — no runtime
    import of ``templates.py`` is needed (the ``TemplateRegistry`` annotation
    above is ``TYPE_CHECKING``-only, via ``from __future__ import annotations``).
    """
    rows = (
        RoutingRow(
            domain=template.routing.domain,
            template_id=template.template_id,
            tool_name=template.tool_name,
            description=template.description,
            triggers=template.routing.triggers,
            guard_terms=template.routing.guard_terms,
            clarify_max=template.routing.clarify_max,
        )
        for template in registry.templates
        if template.routing is not None
    )
    return tuple(sorted(rows, key=lambda row: row.template_id))
