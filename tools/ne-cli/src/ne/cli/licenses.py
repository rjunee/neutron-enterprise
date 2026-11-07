# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed license gate enforcing SPEC §2 C4 over the uv.lock dependency set.

This is a bespoke, zero-new-dependency scanner (stdlib ``tomllib`` +
``importlib.metadata``). No third-party license scanner implements the C4 class
semantics (banned / out-of-process-only / separate-service / case-by-case / free)
with fail-closed-on-unknown behaviour, so we own it.

Design invariants (Checkpoint A red-teams these):
  * BANNED (AGPL/SSPL/BUSL/FSL/fair-code/NC) is structurally non-overridable.
  * GPL / LGPL / Elastic-2.0 fail for in-venv (in-process) deps absent an
    explicit, justified override.
  * Anything not positively classified as FREE fails closed (UNKNOWN -> violation).
  * Free-text license-name normalization and embedded-full-license-text
    detection are resolution-layer, policy-gated fallbacks that run strictly
    AFTER the banned-dominance sweep and fail closed to UNKNOWN when not
    confidently matched (NE-CKB2).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

import click

from ne.cli.checks import _find_repo_root

_CANON_RE = re.compile(r"[-_.]+")


def canonical_name(name: str) -> str:
    """PEP 503 normalized distribution name (lowercase, runs of -_. -> -)."""
    return _CANON_RE.sub("-", name).lower()


# Resolved-license classification outcomes.
FREE = "free"
BANNED = "banned"
LGPL_NEEDS_APPROVAL = "lgpl-case-by-case"
GPL_NEEDS_APPROVAL = "gpl-out-of-process"
ELASTIC_NEEDS_APPROVAL = "elastic-separate-service"
UNKNOWN = "unknown"

# Override class -> the required-approval classification it satisfies.
_OVERRIDE_CLASS_FOR = {
    "free": FREE,
    "case-by-case-approved": LGPL_NEEDS_APPROVAL,
    "out-of-process-approved": GPL_NEEDS_APPROVAL,
    "separate-service-approved": ELASTIC_NEEDS_APPROVAL,
}
_VALID_OVERRIDE_CLASSES = frozenset(_OVERRIDE_CLASS_FOR)

# Trove ``License :: OSI Approved :: X`` classifier tail -> SPDX identifier. Only
# used when neither a License-Expression nor a clean License field is available.
# Ambiguous permissive families (BSD, ASL) map to their common SPDX id — the
# distinction between e.g. BSD-2/BSD-3 never flips a FREE result to a violation.
_CLASSIFIER_TO_SPDX: dict[str, str] = {
    "MIT License": "MIT",
    "MIT No Attribution License (MIT-0)": "MIT",
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD-3-Clause",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "zlib/libpng License": "Zlib",
    # Copyleft families are mapped so the classifier normalizes into the SPDX
    # tokens the pattern matcher recognises (LGPL before GPL, AGPL is banned).
    "GNU Affero General Public License v3": "AGPL-3.0",
    "GNU Affero General Public License v3 or later (AGPLv3+)": "AGPL-3.0-or-later",
    "GNU Lesser General Public License v2 (LGPLv2)": "LGPL-2.0",
    "GNU Lesser General Public License v2 or later (LGPLv2+)": "LGPL-2.1-or-later",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0",
    "GNU Lesser General Public License v3 or later (LGPLv3+)": "LGPL-3.0-or-later",
    "GNU General Public License v2 (GPLv2)": "GPL-2.0",
    "GNU General Public License v2 or later (GPLv2+)": "GPL-2.0-or-later",
    "GNU General Public License v3 (GPLv3)": "GPL-3.0",
    "GNU General Public License v3 or later (GPLv3+)": "GPL-3.0-or-later",
}


def _normalize_free_text(s: str) -> str:
    """Collapse internal whitespace and casefold, for tolerant string matching."""
    return " ".join(s.split()).casefold()


# Curated free-text license-name -> SPDX map, consulted (policy-gated, strictly
# AFTER the banned-dominance sweep) when a resolved license string is not a
# clean SPDX token but is one of these unambiguously-recognized common forms.
# Keys MUST already be in ``_normalize_free_text`` form (lowercase, single-
# spaced) — every lookup normalizes its probe string the same way.
#
# SECURITY INVARIANT: this map must NEVER map a banned/copyleft free-text name
# to a free SPDX id. Copyleft entries below are deliberately mapped to their
# *needs-approval* SPDX ids (GPL-*/LGPL-*), never to a free id, so a spelled-out
# "GNU General Public License v3" closes the loophole where such a string used
# to silently fail the token filter and fall through to the [known]
# absent-metadata rescue path — it now resolves to GPL-3.0 and still fails the
# gate. Affero/AGPL forms are handled entirely by banned_patterns (see
# resolve_license_from_metadata's banned-dominance sweep, which runs BEFORE
# this map is ever consulted) and deliberately have NO entry here.
_FREE_TEXT_TO_SPDX: dict[str, str] = {
    # Apache. Every Apache License version is permissive; the bare "apache
    # license" key covers fastembed's PyPI field (upstream-verified Apache-2.0).
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache license version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache license": "Apache-2.0",
    # MIT.
    "mit license": "MIT",
    "the mit license": "MIT",
    "the mit license (mit)": "MIT",
    # BSD (unqualified / 3-clause forms map to the common SPDX id — the
    # BSD-2/BSD-3 distinction never flips a FREE result to a violation).
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "modified bsd license": "BSD-3-Clause",
    # BSD 2-clause forms.
    "2-clause bsd": "BSD-2-Clause",
    "2-clause bsd license": "BSD-2-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
    "simplified bsd": "BSD-2-Clause",
    "simplified bsd license": "BSD-2-Clause",
    # Other permissive families.
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "mozilla public license version 2.0": "MPL-2.0",
    "isc license (iscl)": "ISC",
    "isc license": "ISC",
    "python software foundation license": "PSF-2.0",
    "psf license": "PSF-2.0",
    "psf": "PSF-2.0",
    "zlib/libpng license": "Zlib",
    # Copyleft — mapped to their needs-approval SPDX ids, NEVER a free id (see
    # SECURITY INVARIANT above). Version-unspecified "gnu general public
    # license" defaults to GPL-3.0: any GPL version is out-of-process-only
    # under C4, so the specific version never changes the gate outcome.
    "gnu general public license": "GPL-3.0",
    "gnu general public license v2": "GPL-2.0",
    "gnu general public license v3": "GPL-3.0",
    "gnu lesser general public license": "LGPL-3.0",
    "gnu lesser general public license v2.1": "LGPL-2.1",
    "gnu lesser general public license v3": "LGPL-3.0",
    "gnu library general public license": "LGPL-2.0",
}

# Banned/copyleft substrings that must never be mistaken for a permissive
# license by ``_detect_license_text``. Checked FIRST, before any header match,
# so banned-dominance is preserved even for embedded full-license-text bodies.
_TEXT_DETECTION_BANNED_MARKERS = (
    "affero",
    "agpl",
    "sspl",
    "server side public",
    "business source",
    "busl",
    "general public license",
    "gpl",
    "non-commercial",
    "noncommercial",
    "commons clause",
    "commons-clause",
)


def _detect_license_text(text: str) -> str | None:
    """Conservatively detect a well-known license from an embedded full text.

    Pure, policy-independent function. Only intended to be called on strings
    that already failed the clean-token filter (embedded license BODIES) or
    that classified UNKNOWN. Matches are exact-header, START-of-text only —
    NOT fuzzy matching — to keep false positives out. Returns ``None`` (fail
    closed) for anything not confidently recognized, including every
    banned/copyleft marker (checked first, so this function can never be used
    to launder a GPL/AGPL license body into a free SPDX id).
    """
    folded = _normalize_free_text(text)
    if any(marker in folded for marker in _TEXT_DETECTION_BANNED_MARKERS):
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    first = _normalize_free_text(lines[0])

    if first in {"mit license", "the mit license", "the mit license (mit)"}:
        if "permission is hereby granted" in folded:
            return "MIT"
        return None

    if first in {"apache license, version 2.0", "apache license version 2.0"}:
        return "Apache-2.0"
    if first == "apache license" and len(lines) >= 2:
        if _normalize_free_text(lines[1]).startswith("version 2.0"):
            return "Apache-2.0"
        return None

    if first in {
        "bsd 3-clause license",
        "3-clause bsd license",
        "the 3-clause bsd license",
        'bsd 3-clause "new" or "revised" license',
    }:
        return "BSD-3-Clause"
    if first in {
        "bsd 2-clause license",
        "2-clause bsd license",
        "the 2-clause bsd license",
        'bsd 2-clause "simplified" license',
    }:
        return "BSD-2-Clause"

    if first == "mozilla public license version 2.0":
        return "MPL-2.0"

    if first == "isc license":
        if "permission to use, copy, modify" in folded:
            return "ISC"
        return None

    return None


_FIRST_PARTY_LICENSE = "Apache-2.0"


class PolicyError(Exception):
    """A structural policy fault (e.g. an override targeting a banned license)."""


@dataclass(frozen=True)
class Policy:
    free: frozenset[str]
    banned_patterns: tuple[str, ...]
    case_by_case_patterns: tuple[str, ...]
    out_of_process_patterns: tuple[str, ...]
    separate_service: tuple[str, ...]
    known: dict[str, str]
    overrides: dict[tuple[str, str], dict[str, str]]

    @staticmethod
    def load(path: Path) -> Policy:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        classes = data.get("classes", {})
        overrides: dict[tuple[str, str], dict[str, str]] = {}
        for entry in data.get("overrides", []):
            # PEP 503-canonicalize the name so an override written 'Foo_Bar'
            # matches a dist normalized to 'foo-bar' (version stays an exact
            # raw match). The lookup side in ``scan`` canonicalizes too.
            key = (canonical_name(str(entry.get("name", ""))), str(entry.get("version", "")))
            overrides[key] = entry
        return Policy(
            free=frozenset(classes.get("free", [])),
            banned_patterns=tuple(classes.get("banned_patterns", [])),
            case_by_case_patterns=tuple(classes.get("case_by_case_patterns", [])),
            out_of_process_patterns=tuple(classes.get("out_of_process_only_patterns", [])),
            separate_service=tuple(classes.get("separate_service_only", [])),
            known={canonical_name(k): v for k, v in data.get("known", {}).items()},
            overrides=overrides,
        )


@dataclass
class PackageResult:
    name: str
    version: str
    license: str  # resolved license string, or "UNKNOWN"
    classification: str
    allowed: bool
    reason: str
    first_party: bool = False


@dataclass
class ScanReport:
    packages: list[PackageResult] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    policy_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations and not self.policy_errors


def _contains(patterns: Iterable[str], text: str) -> bool:
    low = text.lower()
    return any(p.lower() in low for p in patterns)


def _split_top_level(expr: str, op: str) -> list[str] | None:
    """Split an SPDX expression on a top-level (depth-0) binary operator.

    Returns None if the operator does not appear at top level. Parenthesised
    sub-expressions are treated as opaque and force UNKNOWN downstream.
    """
    parts: list[str] = []
    depth = 0
    token = f" {op} "
    buf = ""
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and expr[i : i + len(token)].upper() == token:
            parts.append(buf)
            buf = ""
            i += len(token)
            continue
        buf += ch
        i += 1
    parts.append(buf)
    return parts if len(parts) > 1 else None


def classify_license(license_str: str, policy: Policy) -> str:
    """Classify a resolved license string into a C4 outcome. Pure function."""
    text = (license_str or "").strip()
    if not text or text.upper() == "UNKNOWN":
        return UNKNOWN

    # (1) BANNED wins over everything, and applies to whole expressions too.
    if _contains(policy.banned_patterns, text):
        return BANNED

    # (2) SPDX OR — pass if ANY operand is free/allowed-free.
    or_parts = _split_top_level(text, "OR")
    if or_parts is not None:
        results = [classify_license(p.strip(), policy) for p in or_parts]
        if BANNED in results:
            return BANNED
        return FREE if FREE in results else UNKNOWN

    # (3) SPDX AND — all operands must be free.
    and_parts = _split_top_level(text, "AND")
    if and_parts is not None:
        results = [classify_license(p.strip(), policy) for p in and_parts]
        if BANNED in results:
            return BANNED
        return FREE if all(r == FREE for r in results) else UNKNOWN

    # (4) Parenthesised / complex single expression -> fail closed.
    if "(" in text or ")" in text:
        return UNKNOWN

    # (5) Single token. LGPL BEFORE GPL ("LGPL-3.0" contains "GPL").
    if _contains(policy.case_by_case_patterns, text):
        return LGPL_NEEDS_APPROVAL
    if _contains(policy.out_of_process_patterns, text):
        return GPL_NEEDS_APPROVAL
    if any(s.lower() == text.lower() for s in policy.separate_service):
        return ELASTIC_NEEDS_APPROVAL
    if text in policy.free:
        return FREE
    return UNKNOWN


def resolve_license_from_metadata(name: str, policy: Policy | None = None) -> str | None:
    """Resolve a dist's license via installed metadata, or None if unresolvable.

    When ``policy`` is provided, a BANNED signal in **any** metadata source
    (License-Expression, any classifier, or the raw ``License`` field (any
    length, multi-line included)) dominates: the banned string is returned
    regardless of precedence, so a
    permissive classifier can never mask an AGPL/SSPL/BUSL ``License:`` field
    (C4 defense-in-depth, fail closed).

    Otherwise (or when ``policy`` is None) the legacy precedence-only result is
    returned: License-Expression (PEP 639) -> trove classifiers (normalized) ->
    a clean short License field.

    When ``policy`` is provided, two further policy-gated fallbacks run AFTER
    the above (and strictly after the banned-dominance sweep, so they can
    never resolve a signal that already classified BANNED): a curated
    free-text-license-name -> SPDX alias lookup for a non-clean-token
    ``License`` field (e.g. "Apache License 2.0"), and conservative embedded-
    full-license-text detection for a ``License`` field that IS the entire
    license body (e.g. tiktoken's field). Both fail closed to the legacy
    ``token`` result (typically ``None``) when they do not confidently match.
    """
    try:
        md = metadata.metadata(name)
    except metadata.PackageNotFoundError:
        return None

    # Collect every available signal using the existing extraction rules.
    raw_expr = md.get("License-Expression")
    expr = raw_expr.strip() if raw_expr and raw_expr.strip() else None

    classifiers = [c for c in (md.get_all("Classifier") or []) if c.startswith("License")]
    mapped: list[str] = []
    for c in classifiers:
        tail = c.split("::")[-1].strip()
        spdx = _CLASSIFIER_TO_SPDX.get(tail)
        if spdx:
            mapped.append(spdx)
    mapped = list(dict.fromkeys(mapped))

    lic = (md.get("License") or "").strip()
    # Only accept a short, single-line token — reject embedded full license text.
    token = (
        lic
        if (
            lic
            and lic.upper() != "UNKNOWN"
            and "\n" not in lic
            and len(lic) <= 40
            and len(lic.split()) <= 4
        )
        else None
    )

    # Banned dominates across ALL sources (fail closed). Scan the RAW License
    # field (any length, multi-line included): a spelled-out banned name must
    # not escape this sweep just because it fails the clean-token filter used
    # for the precedence result below.
    if policy is not None:
        raw_field = [lic] if lic and lic.upper() != "UNKNOWN" else []
        for sig in ([expr] if expr else []) + mapped + raw_field:
            if classify_license(sig, policy) == BANNED:
                return sig

    # Existing precedence for the non-banned result.
    if expr:
        return expr
    if mapped:
        # Multiple permissive classifiers (e.g. dual BSD/Apache) -> OR expression:
        # a package offered under multiple licenses is usable if any is free.
        return " OR ".join(mapped)

    # Policy-gated fallbacks, strictly after the banned sweep above: a curated
    # free-text-name alias, then conservative embedded-full-text detection.
    if policy is not None and lic and lic.upper() != "UNKNOWN":
        if "\n" not in lic:
            alias = _FREE_TEXT_TO_SPDX.get(_normalize_free_text(lic))
            if alias is not None:
                return alias
        if token is None:
            detected = _detect_license_text(lic)
            if detected is not None:
                return detected

    return token


def resolve_license(
    name: str,
    policy: Policy,
    resolver: Callable[[str], str | None] | None = None,
) -> str | None:
    """Resolve a third-party dist's license, or None if unresolvable.

    Tries installed metadata first (``resolver``, defaulting to the policy-aware
    :func:`resolve_license_from_metadata` so banned dominance applies). The
    policy ``[known]`` name -> SPDX map then serves TWO purposes:

    1. Absent-metadata fallback: when metadata resolution returns ``None``
       (e.g. a platform-gated dist that never installs on the build platform,
       such as ``colorama``), ``[known]`` supplies the human-verified SPDX id.
    2. UNKNOWN rescue: when metadata *does* resolve but the resulting string
       classifies as :data:`UNKNOWN` (a present-but-non-SPDX license name, e.g.
       protobuf's PyPI ``License`` field literally reading ``"3-Clause BSD
       License"``), a matching ``[known]`` entry supplies the verified SPDX id
       in its place.

    SECURITY INVARIANT: the rescue is keyed strictly on
    ``classify_license(resolved, policy) == UNKNOWN`` — it is never consulted
    when the resolved string classifies BANNED, LGPL/GPL-needs-approval, or
    Elastic-needs-approval. Banned-dominance inside
    :func:`resolve_license_from_metadata` already returns the banned signal
    string whenever *any* metadata source (License-Expression, classifier, or
    raw License field) classifies BANNED, so that string reaches this function
    already classified BANNED and is structurally never rescued. A ``[known]``
    value that itself classifies BANNED still fails downstream in
    :func:`classify_license` (the map is not an escape hatch). Copyleft/Elastic
    classifications keep their stricter override discipline unchanged — the
    rescue never fires for them.

    When still UNKNOWN, the rescue is a three-stage chain, tried in order:
    (1) the policy ``[known]`` map (package-specific human curation beats
    generic string knowledge); (2) the curated free-text-license-name -> SPDX
    alias map (:data:`_FREE_TEXT_TO_SPDX`); (3) conservative embedded-full-
    license-text detection (:func:`_detect_license_text`). This whole chain is
    keyed strictly on UNKNOWN — never consulted for BANNED/LGPL/GPL/Elastic
    classifications; banned-dominance in :func:`resolve_license_from_metadata`
    still returns the banned signal first, before this function ever runs.

    Shared by the C4 gate (``scan``) and the SBOM generator so both record
    identical license facts.
    """
    resolved = (
        resolver(name) if resolver is not None else resolve_license_from_metadata(name, policy)
    )
    if resolved is None:
        return policy.known.get(canonical_name(name))
    if classify_license(resolved, policy) == UNKNOWN:
        known = policy.known.get(canonical_name(name))
        if known is not None:
            return known
        alias = _FREE_TEXT_TO_SPDX.get(_normalize_free_text(resolved))
        if alias is not None:
            return alias
        detected = _detect_license_text(resolved)
        if detected is not None:
            return detected
    return resolved


def _is_first_party(pkg: dict) -> bool:
    src = pkg.get("source", {})
    return any(k in src for k in ("editable", "virtual", "directory"))


def scan(
    lock_path: Path,
    policy: Policy,
    *,
    resolver: Callable[[str], str | None] | None = None,
    installed_names: set[str] | None = None,
) -> ScanReport:
    """Scan uv.lock against the policy. Fail-closed on unknown/unresolved."""
    _resolver: Callable[[str], str | None] = (
        resolver if resolver is not None else (lambda n: resolve_license_from_metadata(n, policy))
    )
    report = ScanReport()
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = data.get("package", [])
    lock_names = {p["name"] for p in packages}

    for pkg in sorted(packages, key=lambda p: p["name"]):
        name = pkg["name"]
        version = pkg.get("version", "")

        if _is_first_party(pkg):
            # The workspace root (and any other ``virtual`` package) is not an
            # installable dist — it has no metadata to verify, so stays exempt.
            if "virtual" in pkg.get("source", {}):
                report.packages.append(
                    PackageResult(
                        name,
                        version,
                        _FIRST_PARTY_LICENSE,
                        FREE,
                        True,
                        "first-party (Apache-2.0, virtual)",
                        first_party=True,
                    )
                )
                continue

            # Editable/directory members ARE installable dists: their metadata
            # must resolve and must declare the first-party license. An
            # unresolvable member fails closed (we cannot verify the license).
            resolved = _resolver(name)
            if resolved is None:
                report.violations.append(
                    f"{name} {version}: first-party metadata not installed — "
                    f"cannot verify {_FIRST_PARTY_LICENSE} (fail closed)"
                )
                report.packages.append(
                    PackageResult(
                        name,
                        version,
                        "UNKNOWN",
                        FREE,
                        False,
                        "first-party metadata not installed (fail closed)",
                        first_party=True,
                    )
                )
                continue
            if _FIRST_PARTY_LICENSE not in resolved:
                report.violations.append(
                    f"{name} {version}: first-party package must be "
                    f"{_FIRST_PARTY_LICENSE}, found {resolved!r}"
                )
            report.packages.append(
                PackageResult(
                    name,
                    version,
                    _FIRST_PARTY_LICENSE,
                    FREE,
                    _FIRST_PARTY_LICENSE in resolved,
                    "first-party (Apache-2.0)",
                    first_party=True,
                )
            )
            continue

        # Bind the license to the LOCKED version: metadata lookup below is by
        # name only, so an env whose installed version differs from the lock
        # pin would have its license verified against the wrong package. CI
        # runs `uv sync --locked` first, but the fail-closed control asserts
        # this itself.
        try:
            installed_ver: str | None = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed_ver = None
        if installed_ver is not None and installed_ver != version:
            reason = (
                f"uv.lock pins {version} but {installed_ver} is installed — "
                "license verified against wrong version (fail closed)"
            )
            report.violations.append(f"{name} {version}: {reason}")
            report.packages.append(PackageResult(name, version, "UNKNOWN", UNKNOWN, False, reason))
            continue

        resolved = resolve_license(name, policy, resolver=_resolver)
        license_str = resolved if resolved is not None else "UNKNOWN"

        classification = classify_license(license_str, policy)
        override = policy.overrides.get((canonical_name(name), version))

        # An override that targets a banned license is a structural policy error.
        if classification == BANNED and override is not None:
            report.policy_errors.append(
                f"{name} {version}: override attempts to whitelist BANNED license "
                f"{license_str!r} — the banned class is never overridable"
            )

        allowed, reason = _apply(name, version, classification, override, report)
        report.packages.append(
            PackageResult(name, version, license_str, classification, allowed, reason)
        )
        if not allowed:
            report.violations.append(f"{name} {version}: {license_str} — {reason}")

    # Env drift: an installed non-first-party dist absent from the lock. Compare
    # on PEP 503 canonical names (importlib may report "PyYAML"/"pre_commit"
    # while the lock stores "pyyaml"/"pre-commit").
    if installed_names is not None:
        lock_canon = {canonical_name(n) for n in lock_names}
        for inst in sorted(installed_names):
            if canonical_name(inst) not in lock_canon:
                report.violations.append(
                    f"{inst}: installed distribution is absent from uv.lock (env drift)"
                )

    return report


def _validate_override(name: str, version: str, override: dict, report: ScanReport) -> bool:
    cls = override.get("class")
    if cls not in _VALID_OVERRIDE_CLASSES:
        report.policy_errors.append(
            f"{name} {version}: override class {cls!r} invalid "
            f"(expected one of {sorted(_VALID_OVERRIDE_CLASSES)})"
        )
        return False
    for required in ("justification", "approved_by", "date"):
        if not str(override.get(required, "")).strip():
            report.policy_errors.append(
                f"{name} {version}: override missing required field {required!r}"
            )
            return False
    return True


def _apply(
    name: str,
    version: str,
    classification: str,
    override: dict | None,
    report: ScanReport,
) -> tuple[bool, str]:
    if classification == BANNED:
        return False, "BANNED class (non-overridable)"

    if override is not None:
        if not _validate_override(name, version, override, report):
            return False, "malformed override"
        # The override's declared class must satisfy the *resolved* classification.
        # A structurally-valid override for the wrong class (e.g. class="free" on a
        # GPL/UNKNOWN dep) must NOT whitelist it — fail closed (C4 security).
        expected = _OVERRIDE_CLASS_FOR[override["class"]]
        if expected != classification:
            report.policy_errors.append(
                f"{name} {version}: override class {override['class']!r} does not satisfy "
                f"resolved classification {classification!r} — fail closed"
            )
            return False, "override class mismatch (fail closed)"
        return True, f"override: {override['class']} (approved_by {override['approved_by']})"

    if classification == FREE:
        return True, "free-to-embed"
    if classification == LGPL_NEEDS_APPROVAL:
        return False, "LGPL is case-by-case; needs class=case-by-case-approved override"
    if classification == GPL_NEEDS_APPROVAL:
        return False, (
            "GPL is out-of-process-only; in-venv deps are in-process — "
            "needs class=out-of-process-approved override"
        )
    if classification == ELASTIC_NEEDS_APPROVAL:
        return False, "Elastic-2.0 allowed only as a separate service; needs override"
    return False, "UNKNOWN/unclassifiable license (fail closed)"


def default_policy_path(root: Path) -> Path:
    return root / "tools" / "supply-chain" / "license-policy.toml"


def _installed_names() -> set[str]:
    names: set[str] = set()
    for dist in metadata.distributions():
        n = dist.metadata["Name"]
        if n:
            names.add(n)
    return names


def _render(report: ScanReport) -> None:
    width = max((len(p.name) for p in report.packages), default=4)
    vw = max((len(p.version) for p in report.packages), default=7)
    for p in report.packages:
        click.echo(f"  {p.name:<{width}}  {p.version:<{vw}}  {p.license:<28}  {p.classification}")
    click.echo("")


@click.command("licenses")
@click.option(
    "--policy",
    "policy_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to license-policy.toml (defaults to tools/supply-chain/license-policy.toml).",
)
@click.option("--quiet", is_flag=True, help="Only print violations and the verdict.")
def licenses(policy_path: Path | None, quiet: bool) -> None:
    """Enforce the SPEC §2 C4 license gate over uv.lock. Fails closed."""
    root = _find_repo_root(Path.cwd())
    lock_path = root / "uv.lock"
    policy_file = policy_path or default_policy_path(root)
    policy = Policy.load(policy_file)

    report = scan(lock_path, policy, installed_names=_installed_names())
    if not quiet:
        _render(report)

    if report.policy_errors:
        click.echo("POLICY ERRORS:")
        for e in report.policy_errors:
            click.echo(f"  ✗ {e}")
    if report.violations:
        click.echo("LICENSE VIOLATIONS:")
        for v in report.violations:
            click.echo(f"  ✗ {v}")

    if report.ok:
        click.echo(f"PASS — {len(report.packages)} packages, all within C4 policy.")
        return
    click.echo(
        f"FAIL — {len(report.violations)} violation(s), "
        f"{len(report.policy_errors)} policy error(s)."
    )
    raise click.exceptions.Exit(1)
