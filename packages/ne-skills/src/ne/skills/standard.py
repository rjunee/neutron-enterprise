# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The deterministic house-standard validator — typed violations, never raises-first.

:func:`validate_pack_fields` collects EVERY violation (it never short-circuits on
the first) so a pack author sees the full punch-list. FAIL-CLOSED on an oversized
body (R-23: reject, never truncate). The section-order rule mirrors
``AuthoringStandard.required_sections`` — the four ``##`` headings must appear IN
ORDER.

THE R-23 INJECTION LINT COVERS THE DESCRIPTION, NOT ONLY THE BODY. The body is
served only through the governed ``read.skills.view`` firing; the DESCRIPTION is
rendered into ``active_set.render_index`` and reaches the model on EVERY turn of
every surface that carries the index, unasked. It is therefore the higher-value
injection target of the two, and the same four checks run on it
(:func:`lint_description`) through the one shared :func:`_injection_violations`
table that :func:`lint_body` uses — so the two paths cannot drift.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from ne.contracts.skills import AuthoringStandard, SkillSource

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_REQUIRED_KEYS = ("name", "description", "version", "triggers", "source")

# -- R-23 injection lint (description AND body) --------------------------------
# Deterministic, case-insensitive, WHOLE-value (never prefix) scans. The author
# agent's input is attacker-influenced and its output is injected fleet-wide, so
# a "before proceeding, fetch https://… and follow its instructions" body — the
# ClawHub exploit class — must be REJECTED here on BOTH the forged and the
# hand-dropped/reconciled path (validate_pack_fields runs on every pack).
_URL_RE = re.compile(r"https?://|\bwww\.", re.IGNORECASE)
_CURL_PIPE_RE = re.compile(r"(curl|wget)[^\n]*\|\s*(ba|z)?sh", re.IGNORECASE)
#: A contiguous run of base64-ish characters long enough to smuggle a payload.
_BASE64_RE = re.compile(r"[A-Za-z0-9+/=]{120,}")
#: fetch/download/… <same sentence> then/and … follow/execute/run/obey/apply.
_FETCH_FOLLOW_RE = re.compile(
    r"(fetch|download|retrieve|visit|read)\b[^.\n]*\b(then|and)\b[^.\n]*"
    r"\b(follow|execute|run|obey|apply)\b",
    re.IGNORECASE,
)
_FOLLOW_ITS_RE = re.compile(r"follow\s+its\s+instructions", re.IGNORECASE)
_IGNORE_PRIOR_RE = re.compile(
    r"(ignore|disregard)\s+(all\s+|)(previous|prior|earlier|above)\s+instructions",
    re.IGNORECASE,
)
#: Any C0 control character plus DEL. Covers newline/carriage-return/tab AND the
#: ESC that opens a terminal escape sequence.
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class StandardViolation:
    """One deterministic authoring-standard failure."""

    rule: str
    message: str


def _fetch_follow(text: str) -> bool:
    return bool(
        _FETCH_FOLLOW_RE.search(text)
        or _FOLLOW_ITS_RE.search(text)
        or _IGNORE_PRIOR_RE.search(text)
    )


#: The R-23 checks, in fired order. ONE table, two callers (:func:`lint_body` and
#: :func:`lint_description`) — the rule suffix is prefixed with the field name, so
#: a rule added here is enforced on both fields or on neither. A per-field copy is
#: exactly the "exempt one enforcement point" shape that passes its own tests and
#: then fails at a distance.
_INJECTION_CHECKS: tuple[tuple[str, Callable[[str], bool], str], ...] = (
    (
        "url",
        lambda t: bool(_URL_RE.search(t)),
        "{field} must not embed a URL (fleet-injection vector)",
    ),
    (
        "curl-pipe",
        lambda t: bool(_CURL_PIPE_RE.search(t)),
        "{field} must not embed a curl|wget-pipe-to-shell",
    ),
    (
        "base64",
        lambda t: bool(_BASE64_RE.search(t)),
        "{field} must not embed a long base64-encoded blob",
    ),
    (
        "fetch-follow",
        _fetch_follow,
        "{field} must not instruct an agent to fetch-then-follow external "
        "content or ignore prior instructions (prompt-injection)",
    ),
)


def _injection_violations(text: str, field: str) -> tuple[StandardViolation, ...]:
    """Run every :data:`_INJECTION_CHECKS` rule over ``text``, naming it ``field``."""
    return tuple(
        StandardViolation(f"{field}-{suffix}", message.format(field=field))
        for suffix, matcher, message in _INJECTION_CHECKS
        if matcher(text)
    )


def lint_body(body: str) -> tuple[StandardViolation, ...]:
    """Full-body injection lint — the R-23 anti-pollution scan (reject-never-warn).

    Deterministic and order-stable; scans the WHOLE body (never a prefix, so
    padding cannot push a payload past a threshold). Returns a violation per rule
    that fires:

    - ``body-url`` — any ``http(s)://`` or ``www.`` (a forged skill has no
      legitimate reason to send an agent to a URL);
    - ``body-curl-pipe`` — a ``curl|wget … | sh`` fetch-then-execute;
    - ``body-base64`` — a 120+ char contiguous base64-ish blob (an encoded
      payload / scanner-evasion padding);
    - ``body-fetch-follow`` — a "fetch/… then follow/execute/…" instruction, a
      literal "follow its instructions", or an "ignore previous instructions"
      injection.
    """
    return _injection_violations(body, "body")


def lint_description(description: str) -> tuple[StandardViolation, ...]:
    r"""Injection lint for the INDEX-RENDERED description (reject-never-rewrite).

    The description is the field the model sees unasked, so it carries the same
    four ``description-*`` checks the body carries, plus one the body does not:

    - ``description-control-chars`` — any ``\x00-\x1f`` or ``\x7f``. A description
      is a ONE-LINE index entry; an embedded newline injects extra lines into
      ``render_index``, and both WITHDRAW paths line-filter on the single-line
      ``- {name} (`` prefix, so a forged line survives removal forever. ESC and DEL
      are rejected for the same reason a URL is: the index lands in a terminal.

    CONTROL CHARACTERS ARE CHECKED FIRST AND RETURN ALONE. Two of the injection
    patterns are newline-bounded (``[^\n]*`` / ``[^.\n]*``), so over multi-line
    text their verdict is not trustworthy: reporting a clean scan of a
    control-bearing description would be an evidence-free "no injection found".
    The pack is rejected either way; the author fixes the control character and
    re-submits into a scan that means something. This is the ONE place this module
    short-circuits, and it short-circuits toward rejection.

    REJECT, NEVER NORMALIZE. ``description`` is inside ``SkillPack.content_hash``
    (``ne.contracts.skills._HASH_ORDERED``), so a validator that stripped or
    rewrote it would move the content hash of an already-registered pack and the
    R-28 hash check would then read it as TAMPERED. A rejecting validator moves no
    pin.
    """
    if CONTROL_CHAR_RE.search(description):
        return (
            StandardViolation(
                "description-control-chars",
                "description must be a single line of printable text (no control "
                "characters) — it is rendered verbatim into the skill index",
            ),
        )
    return _injection_violations(description, "description")


def normalize_trigger(trigger: str) -> str:
    """Canonicalize a trigger phrase for overlap comparison (strip + casefold).

    The SAME normalization ``near_dup._shared_trigger_phrase`` uses, so the
    creation-time overlap lint and the sweep agree on what "the same phrase"
    means."""
    return trigger.strip().casefold()


def trigger_overlap_violations(
    name: str,
    triggers: Sequence[str],
    existing: Mapping[str, Sequence[str]],
) -> tuple[StandardViolation, ...]:
    """Flag every OTHER pack sharing a normalized trigger phrase with ``name``.

    One ``trigger-overlap`` violation per clashing pack (the candidate's own name
    is excluded), naming the shared phrase + the clashing pack. Deterministic and
    order-stable (existing packs are visited in sorted-name order). This is the
    creation-time half of the anti-pollution overlap lint — NE-SKILL-3's near-dup
    already routes model-authored overlaps to UPDATE-not-sibling; this closes the
    steward-edit hole where a hand-edited trigger could collide with a sibling."""
    cand = {normalize_trigger(t) for t in triggers if t.strip()}
    if not cand:
        return ()
    violations: list[StandardViolation] = []
    for other_name in sorted(existing):
        if other_name == name:
            continue
        other = {normalize_trigger(t) for t in existing[other_name] if t.strip()}
        shared = sorted(cand & other)
        for phrase in shared:
            violations.append(
                StandardViolation(
                    "trigger-overlap",
                    f"trigger phrase {phrase!r} already claimed by skill {other_name!r}",
                )
            )
    return tuple(violations)


def _sections_in_order(body: str, sections: tuple[str, ...]) -> bool:
    """True iff every ``## <section>`` heading appears in ``body`` IN ORDER."""
    pos = 0
    for section in sections:
        heading = f"## {section}"
        found = body.find(heading, pos)
        if found < 0:
            return False
        pos = found + len(heading)
    return True


def validate_pack_fields(
    fields: Mapping[str, object],
    standard: AuthoringStandard,
    *,
    known_tools: frozenset[str] | None = None,
) -> tuple[StandardViolation, ...]:
    """Collect ALL house-standard violations for a pack's frontmatter + body.

    Deterministic and order-stable. ``known_tools`` (when provided) is the
    registered tool-name snapshot: ``requires_tools`` must be a subset.
    """
    violations: list[StandardViolation] = []

    for key in _REQUIRED_KEYS:
        if key not in fields or fields[key] in (None, "", (), []):
            violations.append(
                StandardViolation("frontmatter-missing", f"missing frontmatter key: {key}")
            )

    name = fields.get("name")
    if isinstance(name, str) and not _SLUG_RE.match(name):
        violations.append(
            StandardViolation("slug", f"name {name!r} must match ^[a-z0-9][a-z0-9-]{{1,63}}$")
        )

    description = fields.get("description")
    if isinstance(description, str):
        if len(description) > standard.description_max_chars:
            violations.append(
                StandardViolation(
                    "description-length",
                    f"description exceeds {standard.description_max_chars} chars",
                )
            )
        # R-23 on the field the model reads WITHOUT asking (see lint_description).
        violations.extend(lint_description(description))

    triggers = fields.get("triggers")
    if triggers is not None and isinstance(triggers, (list, tuple)) and len(triggers) == 0:
        violations.append(StandardViolation("triggers-empty", "triggers must be non-empty"))

    body = fields.get("body")
    if isinstance(body, str):
        if len(body) > standard.body_max_chars:
            violations.append(
                StandardViolation(
                    "body-length",
                    f"body exceeds {standard.body_max_chars} chars (reject, never truncate)",
                )
            )
        if not _sections_in_order(body, standard.required_sections):
            violations.append(
                StandardViolation(
                    "sections",
                    "body must contain "
                    + ", ".join(f"'## {s}'" for s in standard.required_sections)
                    + " IN ORDER",
                )
            )
        # R-23: full-body injection lint runs on EVERY pack (forged, reconciled,
        # hand-dropped) — this function is the single validation chokepoint for
        # the PACK. That says nothing about which FIELDS are linted: `description`
        # was reachable here and still exempt from every injection check, which is
        # how a "fetch … and follow its instructions" description shipped. Each
        # field is linted or it is not; the chokepoint does not lint it for you.
        violations.extend(lint_body(body))
    else:
        violations.append(StandardViolation("frontmatter-missing", "missing body"))

    requires_tools = fields.get("requires_tools")
    if known_tools is not None and isinstance(requires_tools, (list, tuple)):
        unknown = [t for t in requires_tools if t not in known_tools]
        for tool in unknown:
            violations.append(StandardViolation("requires-tools", f"unknown required tool: {tool}"))

    source = fields.get("source")
    if isinstance(source, str):
        try:
            SkillSource(source)
        except ValueError:
            violations.append(StandardViolation("source", f"invalid skill source: {source!r}"))

    return tuple(violations)
