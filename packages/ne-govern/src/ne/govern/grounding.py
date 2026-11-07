# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The grounding-gate core (NE-GA-G1): a PURE, deterministic, model-free
containment check — THE guarantee of the grounded agent (see
``docs/architecture/anti-hallucination-grounding.md``).

Purity contract: this module imports STDLIB ONLY (``unicodedata``, ``re``,
``decimal``, ``hashlib``, ``importlib.resources``, ``dataclasses``,
``collections.abc``, ``typing``). No model call, no I/O except the one
import-time packaged-resource read, no clock, no randomness, no global
mutable state — every public function is a pure function of its arguments,
so ``normalize``/``canonical_numeral``/``GroundingCorpus.from_answers``/
``check_grounding`` are 100% offline-testable and deterministic (C3: this
module — like ``ne.brain_ontology.verified.routing`` — imports NOTHING from
``ne.runtime`` or ``ne.brain_ontology``; a positive-allowlist AST guardian
test enforces this). All inputs are plain data: the draft ``str``, the
prompt ``str``, and the turn's fetched answers as parsed JSON ``Mapping``\\ s
(the ``VerifiedAnswer`` shape, ``answers.py:85-102``) — never an ontology
object, never a model handle.

Profiles (NE-HGATE-1): :func:`check_grounding` takes a keyword ``profile``
(:class:`GateProfile`) DEFAULTING to :data:`PROFILE_NG1_FROZEN` — today's exact
bytes+rules, so every existing caller is byte-identical. The expanded default
:data:`PROFILE_NG2` adds an extended lexicon, a derived number-word lexicon,
corpus-precedence anti-recombination runs, and fact-sentence vocab classes.
Profiles are frozen import-time data built from packaged resources; the module
stays TIER-AGNOSTIC (it computes every finding class for the profile) — the
strictness-tier mapping lives in the sibling pure module
``ne.govern.grounding_policy``.

C12 (redaction discipline): a :class:`GroundingFinding` carries ONLY a
``reason`` class and a ``count`` — the actual ungrounded TEXT never leaves
this module's return value. Every evasion this module defends against
(Unicode format-char smuggling, homoglyphs, exotic digits, percent/currency
unit laundering, caller-echoed-arg poisoning, aggregate-provenance pooling,
phrase recombination) terminates in a deterministic, named refusal class.

Fail-CLOSED everywhere (design §2, binding): an unparseable numeral is an
explicit finding UNLESS (ng-2 only) that exact token is contained in the
corpus verbatim — a quotation, not an assertion; it then also JOINS the
anti-recombination run, so a date lifted from one chunk and pasted next to
another chunk's prose is still ``phrase-recombination``; a truncated answer
excludes all DERIVED aggregates (raw cell numerals and ordinals still ground); a
non-``VerifiedAnswer``-shaped input (a ``BLOCKED: ...`` string, a malformed
mapping, a row/column length mismatch) contributes ZERO facts; numerals are
NEVER prompt-exempt; an IRI is checked on the RAW draft substring so
punctuation/Unicode folding can never hide one.

Envelope facts (NE-CTXB-4, truncation honesty): ``returned_rows``/
``total_rows``/``row_limit`` mint a ``NumberFact(kind="envelope")`` ALWAYS,
INCLUDING when the answer is truncated — that is the point, so an honest
"showing 3 of 5,000,000 accounts" is speakable. Each envelope field is
parsed fail-closed PER FIELD (:func:`_parse_envelope`): ``returned_rows`` is
minted from ``len(rows)`` when absent but MUTED when a present value
contradicts ``len(rows)`` (never guessed); ``total_rows``/``row_limit`` mint
only from a present, well-typed (non-``bool``), in-range int, and
``total_rows`` is muted when it is smaller than the verified returned row
count (a contradictory "returned 3 of 1"). A bad envelope field mutes only
that one numeral — the answer is never rejected, so the additive-key
tolerance does not regress.

Red-team hardening (§2.7, BINDING — folded into this module):

* **F1 (arg-echo corpus poisoning, belt).** :meth:`GroundingCorpus.from_answers`
  drops any cell whose column name is (casefold-)equal to one of the
  answer's own ``args`` keys — a caller-echoed column is not graph data.
* **F2 (percent/currency laundering).** :func:`canonical_numeral` treats a
  trailing ``%`` or a currency symbol as MEANING-BEARING: ``"50%"``
  canonicalizes to the DISTINCT token ``"50%"``, never to plain ``"50"`` — it
  grounds only if that exact percent/currency value is itself a corpus cell
  (no percentage/currency recompute exists, so it otherwise always refuses).
  :func:`_tokenize` BINDS a bare ``%``/currency symbol DETACHED from its
  numeral by whitespace (``"50 %"``, ``"$ 50"``) to that adjacent numeral
  BEFORE punctuation folding, so a detached form canonicalizes identically
  to its attached form and cannot launder past this check.
* **F3 (aggregate-pooling / numeral-provenance adjacency).** Every
  :class:`NumberFact` carries its provenance ``(kind, column)`` plus an
  ``anchors`` token set; :func:`check_grounding` requires a grounded numeral
  to co-occur, in the SAME sentence, with a grounded token drawn from that
  fact's anchors — a ``SUM(amount)`` numeral needs an ``amount``-labelled
  grounded token in-sentence, so ``"4200 orders"`` (with no ``amount`` in
  the sentence) refuses even though 4200 really is the fetched sum.
* **F3 row-binding (post-review hardening).** A ``cell`` fact's anchors
  include both its OWN row's identity tokens and its (shared) column name.
  A bare match on the column name alone is not enough to ground a ``cell``
  numeral when the sentence ALSO names a SIBLING row's identity for that
  same column — that would let a true column label rescue a FALSE
  row/number pairing (``"beta has amount 1000"`` must refuse when 1000 is
  alpha's amount, not beta's). See :func:`_fact_grounded`.
* **F3 single-cell identity (NE-CTXB-4).** Own-row identity for a ``cell``
  numeral is satisfied by full containment of EITHER the IRI-inclusive union
  OR any single usable ``identity_cells`` entry — one non-numeric, non-IRI
  sibling cell's COMPLETE token set (the name cell ``{acme, corp}``). This
  lets the Layer-3 count sentence ground without ever speaking the row's URN
  citation tokens, while the wrong-row full-containment + foreign-sibling
  refusals still fire. Degenerate identity cells (a subset of a sibling
  row's set for the same column) are dropped at build. Envelope numerals
  anchor only F3-style to template-id + column vocabulary, so an envelope
  numeral paired with the WRONG row identity still grounds — a DECLARED
  accepted residual, pinned by test so future hardening flips it consciously.
"""

from __future__ import annotations

import decimal
import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal, cast

#: ``types.MappingProxyType`` obtained WITHOUT importing ``types`` — the C3
#: stdlib guardian (``test_grounding.py`` ``_ALLOWED_ROOT_MODULES``, untouchable)
#: does NOT allowlist ``types``. Every class ``__dict__`` IS a ``mappingproxy``,
#: so ``type(type.__dict__)`` yields the ``MappingProxyType`` class itself. Used
#: to freeze a profile's number-word lexicon into a read-only view (C2) so a
#: consumer can never remap the versioned rulebook after import.
_MappingProxyType = type(type.__dict__)

__all__ = [
    "CONNECTIVE_ALLOWLIST",
    "GATE_VERSION",
    "PROFILE_NG1_FROZEN",
    "PROFILE_NG2",
    "GateProfile",
    "GroundingCorpus",
    "GroundingFinding",
    "GroundingVerdict",
    "NumberFact",
    "canonical_numeral",
    "check_grounding",
    "is_numeral",
    "normalize",
]

# ---------------------------------------------------------------------------
# §2a — pinned normalize: an OWNED copy of the U1 pipeline
# (ne.brain_ontology.verified.routing.normalize, routing.py:178-187), re-pinned
# here because ne-govern may not import ne.brain_ontology (C3). Lockstep with
# U1 is guarded by a fixture (tests/vectors/normalize_parity_vectors.json),
# never by import. ONE pinned addition over U1: an Unicode-Cf strip after
# NFKC, which defeats zero-width/format-char evasion (e.g. a ZWSP smuggled
# inside "Ac​me").
# ---------------------------------------------------------------------------

#: Matches any char that is neither a "word" char nor whitespace; replaced
#: with a space so punctuation never glues two tokens together.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

#: Apostrophe variants DELETED (not replaced with a space), so a contraction
#: collapses to one token (e.g. "dont"). Both the straight quote (U+0027) and
#: the curly/typographic right single quotation mark (U+2019, referenced by
#: escape to avoid an ambiguous-unicode lint hit) are covered.
_APOSTROPHE_TRANSLATION = {ord("'"): None, ord("\u2019"): None}


def _strip_cf(text: str) -> str:
    """Remove every Unicode ``Cf`` (format) char — zero-width space/joiner,
    soft hyphen, word joiner, etc. — the pinned F1/anti-evasion addition over
    U1's pipeline."""
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


def normalize(text: str) -> tuple[str, ...]:
    """PINNED normalization: NFKC -> strip Unicode Cf -> casefold -> delete
    apostrophes -> punctuation-to-space -> whitespace-split token tuple.

    This is the U1 pinned pipeline (``routing.py:178-187``) with ONE pinned
    addition (the Cf-strip, right after NFKC). Deterministic: same input ->
    same output, always.
    """
    folded = unicodedata.normalize("NFKC", text)
    folded = _strip_cf(folded)
    folded = folded.casefold()
    no_apostrophes = folded.translate(_APOSTROPHE_TRANSLATION)
    spaced = _PUNCT_RE.sub(" ", no_apostrophes)
    return tuple(spaced.split())


# ---------------------------------------------------------------------------
# §2b — numeral canonicalization (F2 hardening: percent/currency are
# meaning-bearing and canonicalize to a DISTINCT token).
# ---------------------------------------------------------------------------

#: Digit-group separators deleted before ``Decimal`` parsing: comma,
#: underscore, NBSP, thin space, narrow no-break space.
_SEPARATOR_CHARS = ",_\u00a0\u2009\u202f"
_SEPARATOR_TRANSLATION = {ord(c): None for c in _SEPARATOR_CHARS}

#: Guard against fixed-point-render blowups (e.g. "1e999999"): a numeral
#: whose decimal exponent magnitude exceeds this is treated as unparseable.
_MAX_ADJUSTED_EXPONENT = 30


def is_numeral(token: str) -> bool:
    """True iff ``token`` contains at least one Unicode ``Nd`` (decimal
    digit) char — the numeral-classification predicate. A numeral-classified
    token is NEVER silently skipped downstream: it either canonicalizes or
    becomes an explicit ``numeral-unparseable`` finding."""
    return any(unicodedata.category(c) == "Nd" for c in token)


@dataclass(frozen=True, slots=True)
class _NumeralParts:
    kind: Literal["plain", "percent", "currency"]
    symbol: str
    value: decimal.Decimal


def _parse_numeral(token: str) -> _NumeralParts | None:
    """Numeral-classify + parse ``token``. Returns ``None`` when ``token`` is
    not numeral-classified, OR is numeral-classified but fails to parse
    (fail-closed: callers distinguish the two via :func:`is_numeral`)."""
    if not is_numeral(token):
        return None

    kind: Literal["plain", "percent", "currency"] = "plain"
    symbol = ""
    core = token
    if token.endswith("%"):
        kind = "percent"
        core = token[:-1]
    elif token and unicodedata.category(token[0]) == "Sc":
        kind = "currency"
        symbol = token[0]
        core = token[1:]
    elif token and unicodedata.category(token[-1]) == "Sc":
        kind = "currency"
        symbol = token[-1]
        core = token[:-1]

    # A meaning-bearing suffix/prefix must appear EXACTLY once (F2): if the
    # core still carries a '%' or any currency symbol, the token is
    # unparseable (fail closed), never silently re-interpreted as plain.
    if "%" in core or any(unicodedata.category(c) == "Sc" for c in core):
        return None

    core = core.translate(_SEPARATOR_TRANSLATION)
    try:
        value = decimal.Decimal(core)
    except decimal.InvalidOperation:
        return None
    if not value.is_finite() or abs(value.adjusted()) > _MAX_ADJUSTED_EXPONENT:
        return None
    return _NumeralParts(kind=kind, symbol=symbol, value=value)


def _render_decimal(value: decimal.Decimal) -> str:
    """Fixed-point render (NEVER ``str(value.normalize())``, which yields
    ``'4.2E+3'`` for 4200) — the canonical plain-numeral surface form."""
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def canonical_numeral(token: str) -> str | None:
    """Canonical form of a numeral-classified ``token``, or ``None`` if
    ``token`` is not numeral-classified OR is numeral-classified but fails to
    parse (fail-closed — use :func:`is_numeral` to distinguish the two).

    **F2 hardening:** a trailing ``%`` or a currency symbol (Unicode ``Sc``)
    is MEANING-BEARING. ``"50%"`` canonicalizes to the DISTINCT token
    ``"50%"``, never to plain ``"50"``; ``"$50"`` -> ``"$50"``; ``"50€"`` ->
    ``"€50"`` (the canonical position is always the PREFIX, even when the
    symbol appeared as a suffix in the input). A percent/currency numeral
    therefore only grounds against a corpus that itself carries that exact
    percent/currency value — no percentage/currency recompute exists, so a
    fabricated statistic built by relabeling a plain grounded number always
    refuses.
    """
    parts = _parse_numeral(token)
    if parts is None:
        return None
    rendered = _render_decimal(parts.value)
    if parts.kind == "percent":
        return rendered + "%"
    if parts.kind == "currency":
        return parts.symbol + rendered
    return rendered


# ---------------------------------------------------------------------------
# §2c — shared draft/cell tokenizer + sentence machinery.
# ---------------------------------------------------------------------------

#: Citation marker ``[k]`` (1-4 digits) — excised from a raw token BEFORE
#: edge-stripping; the remainder continues through the pipeline.
_CITATION_RE = re.compile(r"\[(\d{1,4})\]")

#: The ``answers.py:67`` ``_IRI_RE`` shape with a NONEMPTY rest (``+`` not
#: ``*``) — so ``"Total:"`` (after trailing-colon edge-strip already removed
#: the colon) never IRI-classifies, and an empty-rest scheme string is prose.
_IRI_FINDER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:[^\s<>\"{}|^`\\]+$")

#: Trailing/leading punctuation stripped ITERATIVELY from a raw token before
#: classification. '%' and any Unicode ``Sc`` (currency) char are
#: deliberately absent from both sets — they are meaning-bearing and must
#: never be edge-stripped.
_TRAILING_EDGE = ".,;:!?)]}>\"'\u201d\u2019\u00bb"
_LEADING_EDGE = "([{<\"'\u201c\u2018\u00ab"

#: A numbered-list marker at the start of a line ("1. " / "2) ") is excised
#: down to just its digits (+ surrounding whitespace) so the sentence
#: splitter's '.'/')' handling never severs the ordinal from its row.
_LIST_MARKER_RE = re.compile(r"^(\s{0,8})(\d{1,4})[.)](\s+)", re.MULTILINE)

#: Sentence boundaries: a run of '.'/'!'/'?'/';' followed by whitespace-or-
#: end (so "1,234.50" and "urn:ne:x" never split on an internal '.'), or one
#: or more newlines. ':' is deliberately absent — it never splits.
_SENT_SPLIT_RE = re.compile(r"[.!?;]+(?=\s|$)|\n+")


def _edge_strip(token: str) -> str:
    while token and token[-1] in _TRAILING_EDGE:
        token = token[:-1]
    while token and token[0] in _LEADING_EDGE:
        token = token[1:]
    return token


@dataclass(frozen=True, slots=True)
class _Citation:
    marker: int


@dataclass(frozen=True, slots=True)
class _IriToken:
    raw: str


@dataclass(frozen=True, slots=True)
class _NumeralToken:
    folded: str
    canonical: str | None


@dataclass(frozen=True, slots=True)
class _ProseToken:
    token: str


_TokenEvent = _Citation | _IriToken | _NumeralToken | _ProseToken


@dataclass(slots=True)
class _RawRecord:
    """One whitespace token's classification, MUTABLE only so
    :func:`_bind_detached_symbols` can merge a bare percent/currency symbol
    into its adjacent numeral's ``folded`` field before emission. ``iri`` set
    means "this record IS an IRI" (``folded`` is then always ``''``);
    otherwise ``folded`` is the fully-folded remainder (NFKC -> Cf-strip ->
    casefold -> edge-strip), possibly ``''`` (e.g. a lone stripped comma or a
    pure citation marker)."""

    citations: tuple[int, ...]
    iri: str | None
    folded: str


def _classify_raw(raw: str) -> _RawRecord:
    """Phase 1 of :func:`_tokenize`: excise citation markers -> edge-strip ->
    IRI check on the RAW remainder -> fold (NFKC -> Cf-strip -> casefold ->
    re-edge-strip). Byte-identical to the pre-restructure per-token pipeline,
    just captured as a record instead of emitted as events immediately."""
    citations = tuple(int(m.group(1)) for m in _CITATION_RE.finditer(raw))
    remainder = _edge_strip(_CITATION_RE.sub("", raw))
    if remainder and _IRI_FINDER_RE.fullmatch(remainder):
        return _RawRecord(citations=citations, iri=remainder, folded="")
    folded = unicodedata.normalize("NFKC", remainder)
    folded = _strip_cf(folded)
    folded = folded.casefold()
    folded = _edge_strip(folded)
    return _RawRecord(citations=citations, iri=None, folded=folded)


def _has_content(record: _RawRecord) -> bool:
    """True iff ``record`` carries an IRI or a nonempty fold — i.e. it is
    something other than a pure-punctuation or pure-citation-marker token.
    Content-empty records are transparent to adjacency (F2 binding): a
    stripped comma or an excised ``[1]`` between a symbol and its numeral
    does not break the bind."""
    return record.iri is not None or bool(record.folded)


def _is_bare_symbol(record: _RawRecord) -> bool:
    """True iff ``record`` is EXACTLY a bare ``%`` or a single Unicode ``Sc``
    (currency) char — the F2 bind target. NFKC in :func:`_classify_raw`
    already folds fullwidth PERCENT SIGN (U+FF05) / DOLLAR SIGN (U+FF04) to
    ASCII, so those are covered too."""
    if record.iri is not None:
        return False
    folded = record.folded
    if folded == "%":
        return True
    return len(folded) == 1 and unicodedata.category(folded) == "Sc"


def _is_bindable_numeral(record: _RawRecord) -> bool:
    """True iff ``record`` is a non-IRI, non-empty, numeral-classified fold —
    a valid F2 bind target. An already-merged record (e.g. ``'$50'`` from a
    prior bind) is still bindable, so a chain like ``'$ 50 %'`` merges to
    ``'$50%'`` (which then fails closed via :func:`_parse_numeral`'s
    exactly-once rule)."""
    if record.iri is not None:
        return False
    return bool(record.folded) and is_numeral(record.folded)


def _nearest_content_index(records: list[_RawRecord], start: int, step: int) -> int | None:
    """Index of the nearest record with content, walking from ``start`` in
    ``step`` direction (+/-1), SKIPPING content-empty records so
    punctuation-only/citation-only tokens never break adjacency."""
    i = start + step
    while 0 <= i < len(records):
        if _has_content(records[i]):
            return i
        i += step
    return None


def _bind_detached_symbols(records: list[_RawRecord]) -> None:
    """Phase 2 of :func:`_tokenize` (F2 hardening): merge each bare
    ``%``/currency symbol record into its whitespace-adjacent numeral's
    ``folded`` field, so a symbol detached from its numeral by whitespace
    canonicalizes identically to the attached form.

    Adjacency skips content-empty records (a stripped comma, a pure ``[1]``
    citation marker) so ``'50 , %'`` and ``'50 [1] %'`` still bind. Direction
    preference is deterministic: ``%`` binds to the PRECEDING numeral first,
    else the following; a currency symbol binds to the FOLLOWING numeral
    first, else the preceding. If neither adjacent content record is a
    bindable numeral (e.g. it is prose or an IRI), the symbol is left
    unbound and keeps today's behavior (dropped by the punctuation fold) --
    non-adjacent semantic binding is out of scope for this deterministic
    gate. Symbols are processed left-to-right in one pass; the symbol
    record's own ``folded`` is cleared to ``''`` once consumed (its
    citations, if any, still emit at its position)."""
    for i, record in enumerate(records):
        if not _is_bare_symbol(record):
            continue
        symbol = record.folded
        left_idx = _nearest_content_index(records, i, -1)
        right_idx = _nearest_content_index(records, i, 1)
        if symbol == "%":
            order = (("left", left_idx), ("right", right_idx))
        else:
            order = (("right", right_idx), ("left", left_idx))
        for side, idx in order:
            if idx is None:
                continue
            neighbor = records[idx]
            if not _is_bindable_numeral(neighbor):
                continue
            if side == "left":
                neighbor.folded = neighbor.folded + symbol
            else:
                neighbor.folded = symbol + neighbor.folded
            record.folded = ""
            break


def _tokenize(text: str) -> list[_TokenEvent]:
    """Classify every raw whitespace token of ``text`` in three phases: (1)
    :func:`_classify_raw` each token into a mutable ``_RawRecord`` (excise
    citation markers -> edge-strip -> IRI check on the RAW remainder -> fold);
    (2) :func:`_bind_detached_symbols` merges a bare ``%``/currency symbol
    record into its whitespace-adjacent numeral BEFORE punctuation folding
    (F2 hardening: closes the detached-symbol bypass, e.g. ``"50 %"``/
    ``"$ 50"``, so it canonicalizes identically to the attached form); (3)
    emit events per record, in order: citation events, then IRI-or-numeral-
    or-prose exactly as before. Used SYMMETRICALLY for corpus cells
    (:meth:`GroundingCorpus.from_answers`) and draft sentences
    (:func:`check_grounding`)."""
    records = [_classify_raw(raw) for raw in text.split()]
    _bind_detached_symbols(records)
    events: list[_TokenEvent] = []
    for record in records:
        for marker in record.citations:
            events.append(_Citation(marker))
        if record.iri is not None:
            events.append(_IriToken(record.iri))
            continue
        folded = record.folded
        if not folded:
            continue
        if is_numeral(folded):
            events.append(_NumeralToken(folded, canonical_numeral(folded)))
            continue
        no_apostrophes = folded.translate(_APOSTROPHE_TRANSLATION)
        spaced = _PUNCT_RE.sub(" ", no_apostrophes)
        for sub in spaced.split():
            events.append(_ProseToken(sub))
    return events


def _fold_token(text: str) -> str:
    """Fold a single value (NFKC -> Cf-strip -> casefold), used to test
    whether an entire cell value IS (not merely contains) a numeral."""
    folded = unicodedata.normalize("NFKC", text)
    folded = _strip_cf(folded)
    return folded.casefold()


def _numeral_preserving_tokens(text: str) -> tuple[str, ...]:
    """:func:`normalize`, EXCEPT that a numeral-classified whitespace token
    keeps its INTERNAL punctuation and stays ONE token (``"2026-05-15"``,
    ``"dr-4830"``, ``"1.2.3"``) instead of being split by the punctuation fold.

    Used only by :func:`_add_phrases` to add a corpus phrase VARIANT, so a
    COMPOUND numeral that :func:`canonical_numeral` cannot parse still exists
    verbatim in ``corpus.tokens``/``corpus.phrases`` — the surface form the
    draft tokenizer (:func:`_tokenize`) actually produces for it.

    Shape invariant (why ng-1 is provably unaffected): every whitespace token
    contributes the SAME positions as :func:`normalize`, or ONE merged
    position when it is numeral-classified — never ZERO where normalize gave
    one. Merging cannot make two PROSE tokens adjacent that were not adjacent
    in the ``normalize`` phrase, and a prose token can never equal a merged
    numeral token (it carries a digit). So a prose-only anti-recombination run
    — the only kind ng-1 builds — matches this variant iff it already matched
    the ``normalize`` phrase.
    """
    out: list[str] = []
    for raw in text.split():
        folded = _edge_strip(_fold_token(raw))
        if not folded:
            continue
        if is_numeral(folded):
            out.append(folded)
            continue
        no_apostrophes = folded.translate(_APOSTROPHE_TRANSLATION)
        out.extend(_PUNCT_RE.sub(" ", no_apostrophes).split())
    return tuple(out)


def _add_phrases(phrases: set[tuple[str, ...]], text: str) -> tuple[str, ...]:
    """Add ``text``'s :func:`normalize` phrase to ``phrases`` (today's build,
    unchanged) PLUS — ONLY when the text carries a numeral
    :func:`canonical_numeral` cannot parse (an ISO date, a hyphenated case id,
    a dotted version, a range, a time) — its :func:`_numeral_preserving_tokens`
    variant. That variant is what makes the ng-2 verbatim-compound-numeral rule
    non-inert: without it the compound token is shredded at BUILD time
    (``"2026-05-15"`` -> ``2026``/``05``/``15``) and could never be contained.
    Returns the ``normalize`` tuple for callers that need it."""
    tokens = normalize(text)
    if tokens:
        phrases.add(tokens)
    variant = _numeral_preserving_tokens(text)
    if variant != tokens and any(is_numeral(t) and canonical_numeral(t) is None for t in variant):
        phrases.add(variant)
    return tokens


def _split_sentences(draft: str) -> list[str]:
    """Numbered-list-marker excision, then sentence split (§2c-v)."""
    delisted = _LIST_MARKER_RE.sub(r"\1\2\3", draft)
    parts = _SENT_SPLIT_RE.split(delisted)
    return [p.strip() for p in parts if p.strip()]


def _expand_plural(tokens: set[str]) -> frozenset[str]:
    """The pinned CHECK-TIME-ONLY plural fold, expanded to a superset used
    for the F3 numeral-adjacency anchor comparison."""
    expanded: set[str] = set()
    for token in tokens:
        expanded.add(token)
        if token.endswith("s"):
            expanded.add(token[:-1])
        if token.endswith("es"):
            expanded.add(token[:-2])
        expanded.add(token + "s")
    return frozenset(expanded)


def _tokens_match(draft_token: str, phrase_token: str) -> bool:
    """Contiguous-run/phrase comparison with the SAME plural fold applied
    (either direction — "account"/"accounts" match each other)."""
    if draft_token == phrase_token:
        return True
    return phrase_token in _expand_plural({draft_token})


def _prose_in_corpus(token: str, corpus: GroundingCorpus) -> bool:
    """Prose-token corpus membership with the pinned CHECK-TIME-ONLY plural
    fold (never applied to numbers/iris/prompt/allowlist membership)."""
    if token in corpus.tokens:
        return True
    if token.endswith("es") and token[:-2] in corpus.tokens:
        return True
    if token.endswith("s") and token[:-1] in corpus.tokens:
        return True
    return (token + "s") in corpus.tokens


def _run_in_phrase(run: Sequence[str], corpus: GroundingCorpus) -> bool:
    """True iff ``run`` (length >= 2) occurs as a CONTIGUOUS token run inside
    at least one ``corpus.phrases`` entry — the anti-recombination rule."""
    n = len(run)
    for phrase in corpus.phrases:
        if len(phrase) < n:
            continue
        for start in range(len(phrase) - n + 1):
            if all(_tokens_match(run[i], phrase[start + i]) for i in range(n)):
                return True
    return False


# ---------------------------------------------------------------------------
# §2d/§2e — GroundingCorpus.
# ---------------------------------------------------------------------------

NumberFactKind = Literal[
    "cell",
    "count",
    "ordinal",
    "distinct-count",
    "freq",
    "pairfreq",
    "sum",
    "min",
    "max",
    "envelope",
]

#: Pinned cap on the number of KEPT columns paired for joint pair-frequency
#: facts — bounds corpus-build cost at O(cols^2 * rows).
_PAIR_FREQUENCY_COLUMN_CAP = 8


@dataclass(frozen=True, slots=True)
class NumberFact:
    """One grounded numeral's provenance (F3 hardening): ``kind``/``column``
    identify WHERE the number came from; ``anchors`` is the normalized
    prose-token set a draft sentence must intersect (after the check-time
    plural fold) for a numeral with this canonical value to ground.

    ``identity_cells`` (NE-CTXB-4) is a ``cell``-only extension: each entry is
    the COMPLETE token set of a single non-numeric, non-IRI sibling cell in
    this fact's OWN row (e.g. a name cell ``{acme, corp}``). Full containment
    of ANY one of these in a sentence satisfies own-row identity for the
    numeral — so a Layer-3 count sentence grounds without ever speaking the
    row's URN citation tokens. Degenerate cells (whose token set is a subset
    of a sibling row's set for the same column) are filtered out at build so
    an ambiguous identity can never rescue a numeral. Defaulted to ``()`` so
    every non-``cell`` construction stays valid.

    ``row_tokens`` (NE-GROUND-ROWID) is likewise ``cell``-only: the ATOMIC
    (:func:`_numeral_preserving_tokens`) token set of this fact's OWN row, in
    which a compound identifier stays WHOLE (``mer-dsp-003``, never
    ``mer``/``dsp``/``003``). It is what :func:`_fact_grounded` compares a
    sentence's QUOTED identifiers against, so a numeral can never be attributed
    to a row the sentence did not name. Defaulted empty for every non-``cell``
    fact, which never takes that path."""

    kind: NumberFactKind
    column: str | tuple[str, str]
    anchors: frozenset[str]
    identity_cells: tuple[frozenset[str], ...] = ()
    row_tokens: frozenset[str] = frozenset()


def _add_number(numbers: dict[str, list[NumberFact]], canonical: str, fact: NumberFact) -> None:
    numbers.setdefault(canonical, []).append(fact)


_ParsedAnswer = tuple[
    str,  # template_id
    tuple[str, ...],  # columns
    tuple[tuple[str, ...], ...],  # rows
    tuple[str, ...],  # citations
    bool,  # truncated
    Mapping[str, object],  # args
    Mapping[str, int],  # envelope (returned_rows/total_rows/row_limit; per-field fail-closed)
]


def _envelope_int(value: object) -> int | None:
    """An envelope field is a real, non-boolean ``int`` or nothing (fail
    closed per FIELD): ``bool`` is a ``str``-less ``int`` subclass, so it is
    explicitly rejected here — ``True`` is never a row count."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _parse_envelope(item: Mapping[str, object], row_count: int) -> dict[str, int]:
    """Extract the three OPTIONAL envelope ints, fail-closed PER FIELD (a
    single bad field mutes only that numeral; the answer is never rejected —
    the additive-key tolerance must not regress):

    * ``returned_rows`` — ALWAYS minted: from ``len(rows)`` when the key is
      absent; when present it must be well-typed AND equal ``len(rows)`` or no
      fact is minted (a contradicting envelope is muted, never guessed).
    * ``total_rows`` — minted iff present, well-typed, and ``>= row_count``
      (the verified returned row count). A total BELOW the returned rows is
      internally contradictory (``returned 3 of 1``) and is muted, never
      minted — the envelope fields are validated together.
    * ``row_limit`` — minted iff present, well-typed, ``>= 1``.
    """
    envelope: dict[str, int] = {}

    if "returned_rows" not in item:
        envelope["returned_rows"] = row_count
    else:
        returned = _envelope_int(item["returned_rows"])
        if returned is not None and returned == row_count:
            envelope["returned_rows"] = returned

    if "total_rows" in item:
        total = _envelope_int(item["total_rows"])
        # >= row_count, not merely >= 0: a total smaller than the verified
        # returned row count is a contradiction ("returned 3 of 1") and is
        # muted so it can never ground alongside the returned_rows fact.
        if total is not None and total >= row_count:
            envelope["total_rows"] = total

    if "row_limit" in item:
        limit = _envelope_int(item["row_limit"])
        if limit is not None and limit >= 1:
            envelope["row_limit"] = limit

    return envelope


def _parse_verified_answer(item: object) -> _ParsedAnswer | None:
    """The ``VerifiedAnswer`` JSON shape gate (fail closed): anything that
    does not carry EXACTLY this shape (a ``BLOCKED: ...`` string, a
    malformed mapping, a row whose length disagrees with ``columns``) yields
    ``None`` and contributes zero facts. Extra keys (``ontology_version``,
    ``executed_at``, ...) are tolerated; the OPTIONAL envelope keys
    (``returned_rows``/``total_rows``/``row_limit``) are parsed fail-closed
    per field (:func:`_parse_envelope`), never gating the whole answer."""
    if not isinstance(item, Mapping):
        return None

    template_id = item.get("template_id")
    template_version = item.get("template_version")
    config_hash = item.get("config_hash")
    columns = item.get("columns")
    rows = item.get("rows")
    citations = item.get("citations")
    truncated = item.get("truncated")
    args = item.get("args")

    if not isinstance(template_id, str):
        return None
    if not isinstance(template_version, int) or isinstance(template_version, bool):
        return None
    if not isinstance(config_hash, str):
        return None
    if not isinstance(columns, Sequence) or isinstance(columns, (str, bytes)):
        return None
    if not all(isinstance(c, str) for c in columns):
        return None
    columns_t = tuple(columns)

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return None
    parsed_rows: list[tuple[str, ...]] = []
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            return None
        if len(row) != len(columns_t):
            return None
        if not all(isinstance(c, str) for c in row):
            return None
        parsed_rows.append(tuple(row))

    if not isinstance(citations, Sequence) or isinstance(citations, (str, bytes)):
        return None
    if not all(isinstance(c, str) for c in citations):
        return None

    if not isinstance(truncated, bool):
        return None

    if not isinstance(args, Mapping):
        return None
    if not all(isinstance(k, str) for k in args):
        return None

    envelope = _parse_envelope(item, len(parsed_rows))
    return (
        template_id,
        columns_t,
        tuple(parsed_rows),
        tuple(citations),
        truncated,
        args,
        envelope,
    )


#: Frozen empty mapping — the default for :attr:`GroundingCorpus.iri_rows`
#: (a mutable ``{}`` default would be shared state; C2).
_EMPTY_IRI_ROWS: Mapping[str, tuple[int, ...]] = cast(
    "Mapping[str, tuple[int, ...]]", _MappingProxyType({})
)


@dataclass(frozen=True, slots=True)
class GroundingCorpus:
    """Everything a draft's tokens may ground against, built from THIS
    turn's fetched :class:`VerifiedAnswer`\\ s only (design §2.2). Empty when
    zero fetches happened — then every content token of a draft refuses
    (fail closed: in grounded mode, not fetching means not speaking).

    NE-GROUND-REL (semantic row binding, ng-2): the last five fields carry the
    ROW STRUCTURE the per-token containment check discards, so a prose
    relation asserted between fetched entities can be checked against the
    fetched rows themselves (never an NLP guess about the sentence):

    * ``row_prose`` — per global row (answers concatenated in input order),
      the :func:`normalize` token set of that row's KEPT cells. Co-row
      partnership and named-row support read from here.
    * ``row_atomic`` — per global row, the :func:`_numeral_preserving_tokens`
      set of its kept cells (compound identifiers stay WHOLE) — what a quoted
      identifier is matched against to NAME a row.
    * ``column_prose`` — per (answer, kept column), the union of that
      column's cell tokens across rows — the same-column enumeration test.
    * ``schema_prose`` — template-id + kept-column-NAME tokens (deliberately
      NOT citation tokens: a citation key often embeds the row's entity name,
      and schema-exempting it would exempt exactly the tokens the binding
      exists to check). Schema vocabulary is never a row claim.
    * ``iri_rows`` — IRI -> global rows: kept cell IRIs by position, plus the
      per-row citation IRIs when the answer's citations are packed in
      lockstep with its rows (``len(citations) == len(rows)``, the
      ``VerifiedAnswer`` §4.1 invariant). A spoken row IRI names its row."""

    phrases: frozenset[tuple[str, ...]]
    tokens: frozenset[str]
    iris: frozenset[str]
    #: canonical numeral -> the fact(s) that produced it. Never mutate.
    numbers: Mapping[str, tuple[NumberFact, ...]]
    citation_count: int
    row_prose: tuple[frozenset[str], ...] = ()
    row_atomic: tuple[frozenset[str], ...] = ()
    column_prose: tuple[frozenset[str], ...] = ()
    schema_prose: frozenset[str] = frozenset()
    iri_rows: Mapping[str, tuple[int, ...]] = _EMPTY_IRI_ROWS

    @classmethod
    def from_answers(cls, answers: Sequence[object]) -> GroundingCorpus:
        """Build a corpus from this turn's fetched answers. Only mappings
        carrying the ``VerifiedAnswer`` shape contribute facts — everything
        else (a ``BLOCKED: ...`` tool result, a malformed mapping)
        contributes ZERO facts (fail closed)."""
        phrases: set[tuple[str, ...]] = set()
        iris: set[str] = set()
        numbers: dict[str, list[NumberFact]] = {}
        citation_count = 0
        row_prose_list: list[frozenset[str]] = []
        row_atomic_list: list[frozenset[str]] = []
        column_prose_list: list[frozenset[str]] = []
        schema_prose_set: set[str] = set()
        iri_rows_map: dict[str, list[int]] = {}

        for item in answers:
            parsed = _parse_verified_answer(item)
            if parsed is None:
                continue
            template_id, columns, rows, citations, truncated, args, envelope = parsed

            arg_keys_cf = {key.casefold() for key in args}
            kept_idx = [i for i, col in enumerate(columns) if col.casefold() not in arg_keys_cf]
            kept_columns = [columns[i] for i in kept_idx]

            # --- per-answer identity-cell candidates (NE-CTXB-4) -----------
            # For each kept column, the per-row COMPLETE token set of a single
            # non-numeric, non-IRI sibling cell -- or None (an IRI cell, an
            # empty cell, a numeral-bearing cell). Then the degenerate filter:
            # a candidate set S at (column, row) is USABLE only if no OTHER row
            # has a non-None set T in the SAME column with S <= T (subset-or-
            # equal -> ambiguous identity -> unusable). ``usable[i][r]`` is the
            # usable set at kept column i, row r (or None).
            usable: dict[int, list[frozenset[str] | None]] = {}
            for i in kept_idx:
                candidates: list[frozenset[str] | None] = []
                for row in rows:
                    cell = row[i]
                    if _IRI_FINDER_RE.fullmatch(cell):
                        candidates.append(None)
                        continue
                    toks = frozenset(normalize(cell))
                    if not toks or any(is_numeral(t) for t in toks):
                        candidates.append(None)
                        continue
                    candidates.append(toks)
                column_usable: list[frozenset[str] | None] = []
                for r, cand in enumerate(candidates):
                    if cand is None:
                        column_usable.append(None)
                        continue
                    degenerate = any(
                        other is not None and r2 != r and cand <= other
                        for r2, other in enumerate(candidates)
                    )
                    column_usable.append(None if degenerate else cand)
                usable[i] = column_usable

            # --- phrases -------------------------------------------------
            for row in rows:
                for i in kept_idx:
                    _add_phrases(phrases, row[i])
            for col in kept_columns:
                _add_phrases(phrases, col)
            tid_tokens = _add_phrases(phrases, template_id)
            for cit in citations:
                _add_phrases(phrases, cit)
                phrases.add((cit,))
            citation_count += len(citations)

            # --- iris ------------------------------------------------------
            for cit in citations:
                iris.add(cit)
            for row in rows:
                for i in kept_idx:
                    cell = row[i]
                    if _IRI_FINDER_RE.fullmatch(cell):
                        iris.add(cell)

            # --- row structure (NE-GROUND-REL semantic row binding) --------
            row_base = len(row_prose_list)
            for r, row in enumerate(rows):
                kept_cells = [row[i] for i in kept_idx]
                prose: set[str] = set()
                atomic: set[str] = set()
                for cell in kept_cells:
                    prose |= set(normalize(cell))
                    atomic |= set(_numeral_preserving_tokens(cell))
                    if _IRI_FINDER_RE.fullmatch(cell):
                        iri_rows_map.setdefault(cell, []).append(row_base + r)
                row_prose_list.append(frozenset(prose))
                row_atomic_list.append(frozenset(atomic))
            for i in kept_idx:
                col_tokens: set[str] = set()
                for row in rows:
                    col_tokens |= set(normalize(row[i]))
                column_prose_list.append(frozenset(col_tokens))
            schema_prose_set.update(normalize(template_id))
            for col in kept_columns:
                schema_prose_set.update(normalize(col))
            # Per-row citation IRIs name their row ONLY under the §4.1
            # lockstep shape; a foreign citations arity maps nothing (the
            # markers still count against citation_count as before).
            if len(citations) == len(rows):
                for r, cit in enumerate(citations):
                    iri_rows_map.setdefault(cit, []).append(row_base + r)

            # --- numbers: CELL numerals (always, even when truncated) ------
            for r, row in enumerate(rows):
                kept_cells = [row[i] for i in kept_idx]
                # The row's ATOMIC identity vocabulary: compound identifiers,
                # dates and IRIs stay WHOLE here (unlike the normalize-shredded
                # ``anchors``), so a QUOTED identifier can be matched against the
                # row it actually belongs to (NE-GROUND-ROWID).
                row_tokens = frozenset(
                    token for cell in kept_cells for token in _numeral_preserving_tokens(cell)
                )
                for pos, i in enumerate(kept_idx):
                    cell = row[i]
                    col = columns[i]
                    # single-cell own-row identities: the usable sets from the
                    # OTHER kept columns of THIS row (NE-CTXB-4).
                    identity_cells = tuple(
                        s for j in kept_idx if j != i for s in (usable[j][r],) if s is not None
                    )
                    for event in _tokenize(cell):
                        if isinstance(event, _NumeralToken) and event.canonical is not None:
                            other_tokens: set[str] = set()
                            for j, other_cell in enumerate(kept_cells):
                                if j == pos:
                                    continue
                                other_tokens |= set(normalize(other_cell))
                            anchors = set(normalize(col)) | other_tokens
                            _add_number(
                                numbers,
                                event.canonical,
                                NumberFact(
                                    "cell", col, frozenset(anchors), identity_cells, row_tokens
                                ),
                            )

            # --- numbers: ORDINALS (always, even when truncated) -----------
            for k, row in enumerate(rows, start=1):
                row_anchor: set[str] = set()
                for i in kept_idx:
                    row_anchor |= set(normalize(row[i]))
                for col in kept_columns:
                    row_anchor |= set(normalize(col))
                canonical = _render_decimal(decimal.Decimal(k))
                _add_number(numbers, canonical, NumberFact("ordinal", "*", frozenset(row_anchor)))

            # --- numbers: ENVELOPE (returned_rows/total_rows/row_limit) -----
            # Minted ALWAYS, INCLUDING when truncated -- that is the point:
            # honest truncation must be able to state 'showing 3 of 5,000,000'.
            # Anchored F3-style to template-id + kept-column vocabulary (same
            # construction as ``count_anchor`` below), so a pooled/foreign use
            # of the same numeral (a different template's vocabulary in-
            # sentence) does NOT ground.
            envelope_anchor = set(tid_tokens)
            for col in kept_columns:
                envelope_anchor |= set(normalize(col))
            envelope_anchor_frozen = frozenset(envelope_anchor)
            for field_name, value in envelope.items():
                _add_number(
                    numbers,
                    _render_decimal(decimal.Decimal(value)),
                    NumberFact("envelope", field_name, envelope_anchor_frozen),
                )

            # --- numbers: AGGREGATES (fail-closed truncation exclusion) -----
            if not truncated:
                count_anchor = set(tid_tokens)
                for col in kept_columns:
                    count_anchor |= set(normalize(col))
                count_canonical = _render_decimal(decimal.Decimal(len(rows)))
                _add_number(
                    numbers,
                    count_canonical,
                    NumberFact("count", "*", frozenset(count_anchor)),
                )

                for i in kept_idx:
                    col = columns[i]
                    values = [row[i] for row in rows]
                    distinct = sorted(set(values))
                    dc_canonical = _render_decimal(decimal.Decimal(len(distinct)))
                    _add_number(
                        numbers,
                        dc_canonical,
                        NumberFact("distinct-count", col, frozenset(normalize(col))),
                    )
                    for value in distinct:
                        freq = values.count(value)
                        freq_canonical = _render_decimal(decimal.Decimal(freq))
                        freq_anchors = set(normalize(col)) | set(normalize(value))
                        _add_number(
                            numbers,
                            freq_canonical,
                            NumberFact("freq", col, frozenset(freq_anchors)),
                        )

                pair_idx = kept_idx[:_PAIR_FREQUENCY_COLUMN_CAP]
                for idx_a in pair_idx:
                    for idx_b in pair_idx:
                        if idx_a == idx_b:
                            continue
                        col_a, col_b = columns[idx_a], columns[idx_b]
                        joint_counts: dict[tuple[str, str], int] = {}
                        for row in rows:
                            key = (row[idx_a], row[idx_b])
                            joint_counts[key] = joint_counts.get(key, 0) + 1
                        for (value_a, value_b), freq in joint_counts.items():
                            pair_canonical = _render_decimal(decimal.Decimal(freq))
                            pair_anchors = (
                                set(normalize(col_a))
                                | set(normalize(col_b))
                                | set(normalize(value_a))
                                | set(normalize(value_b))
                            )
                            _add_number(
                                numbers,
                                pair_canonical,
                                NumberFact("pairfreq", (col_a, col_b), frozenset(pair_anchors)),
                            )

                for i in kept_idx:
                    col = columns[i]
                    values = [row[i] for row in rows]
                    decimals: list[decimal.Decimal] = []
                    fully_numeric = len(values) >= 1
                    for value in values:
                        parts = _parse_numeral(_edge_strip(_fold_token(value.strip())))
                        if parts is None or parts.kind != "plain":
                            fully_numeric = False
                            break
                        decimals.append(parts.value)
                    if fully_numeric:
                        col_anchor = frozenset(normalize(col))
                        for kind, value_d in (
                            ("sum", sum(decimals, decimal.Decimal(0))),
                            ("min", min(decimals)),
                            ("max", max(decimals)),
                        ):
                            _add_number(
                                numbers,
                                _render_decimal(value_d),
                                NumberFact(kind, col, col_anchor),  # type: ignore[arg-type]
                            )

        return cls(
            phrases=frozenset(phrases),
            tokens=frozenset(token for phrase in phrases for token in phrase),
            iris=frozenset(iris),
            numbers={key: tuple(value) for key, value in numbers.items()},
            citation_count=citation_count,
            row_prose=tuple(row_prose_list),
            row_atomic=tuple(row_atomic_list),
            column_prose=tuple(column_prose_list),
            schema_prose=frozenset(schema_prose_set),
            iri_rows=cast(
                "Mapping[str, tuple[int, ...]]",
                _MappingProxyType({key: tuple(value) for key, value in iri_rows_map.items()}),
            ),
        )


# ---------------------------------------------------------------------------
# §2f — verdict types + check_grounding.
# ---------------------------------------------------------------------------

FindingReason = Literal[
    "citation-out-of-range",
    "iri-ungrounded",
    "numeral-ungrounded",
    "numeral-unparseable",
    "phrase-recombination",
    "relation-ungrounded",
    "vocab-extended",
    "vocab-ungrounded",
    "vocab-ungrounded-factual",
]


@dataclass(frozen=True, slots=True)
class GroundingFinding:
    """C12: a finding carries ONLY a reason CLASS and a token/run COUNT —
    the actual ungrounded text never leaves this module."""

    reason: FindingReason
    count: int


@dataclass(frozen=True, slots=True)
class GroundingVerdict:
    passed: bool
    findings: tuple[GroundingFinding, ...]
    #: Count of prose tokens grounded ONLY via the prompt (neither corpus nor
    #: allowlist) — feeds ``GroundedProvenance.prompt_echo_count`` (G3/G0).
    prompt_echo_count: int


def _flush_run(run_tokens: list[str], corpus: GroundingCorpus) -> bool:
    """Close out the current anti-recombination run; return True iff it is a
    phrase-recombination violation (length >= 2 and not contiguous in any
    single corpus phrase)."""
    is_violation = len(run_tokens) >= 2 and not _run_in_phrase(run_tokens, corpus)
    run_tokens.clear()
    return is_violation


def _fact_grounded(
    fact: NumberFact,
    corpus: GroundingCorpus,
    anchor_universe: frozenset[str],
    quoted_identifiers: frozenset[str] = frozenset(),
) -> bool:
    """F3 adjacency test for one :class:`NumberFact`, with a P1 row-binding
    hardening for ``cell`` facts.

    The base rule is: the fact grounds iff its anchors intersect the
    sentence's corpus-grounded prose set (``anchor_universe``). For ``cell``
    facts that base rule is not enough on its own: a ``cell`` fact's anchors
    are the union of its OWN column name and its OWN row's other cell
    tokens, so a sentence that echoes only the (shared) column name would
    ground the numeral regardless of which row it names -- letting a true
    column label rescue a FALSE row/number pairing (e.g. "beta has amount
    1000" would ground 1000 even though 1000 is alpha's amount, not beta's).

    So for ``cell`` facts: a match via the fact's OWN row-identity tokens
    (anchors minus the column-name tokens) always grounds -- but ONLY when
    the ENTIRE row-identity token set is present in the sentence, not merely
    some overlapping token. A partial-intersection test would let two rows
    that merely SHARE a boilerplate token in their identity (e.g. "Alpha
    Corp" / "Beta Corp" both containing "corp") satisfy this check off that
    shared token alone, letting a sentence naming the WRONG row's identity
    ("Beta Corp has amount 100", where 100 is Alpha's cell) launder past as
    if it named its own row (Codex P1). Requiring full containment of the
    row-identity token set closes that gap.

    NE-CTXB-4 adds a SECOND own-row route: own-row identity is also satisfied
    by full containment of any single usable ``identity_cells`` entry -- the
    COMPLETE token set of one non-numeric, non-IRI sibling cell (the name cell
    {acme, corp}). This is what lets a Layer-3 count sentence ground when the
    row's URN citation tokens (part of the union in ``anchors``) are never
    spoken. Degenerate identity cells (a subset of a sibling row's set for the
    same column) were dropped at corpus build, so an ambiguous identity can
    never rescue a numeral.

    Finally, a match via the column name ALONE only grounds if the sentence
    does not ALSO name a sibling row's identity for that same column -- if it
    does, the numeral refuses (the sentence is making a row claim this fact
    cannot back). Envelope facts (``kind="envelope"``) do NOT take the ``cell``
    path: they anchor F3-style to template-id + column vocabulary and cannot
    see row misattribution -- a DECLARED accepted residual (an envelope
    numeral paired with the wrong row identity still grounds), pinned by test
    so any future hardening flips it consciously.

    NE-GROUND-ROWID (ng-2, fail-closed) — the row-identity CONFLICT rule that
    the verbatim-compound-numeral rule makes both possible and NECESSARY. An
    entity identifier (``mer-dsp-003``) is numeral-classified, so it is never a
    prose token and can never enter ``anchor_universe``: F3's own-row and
    foreign-row routes are both BLIND to it. Once a quoted identifier grounds by
    containment, a sentence could therefore attribute one row's cell numeral to
    a DIFFERENT row and ground on the shared column name alone
    (``"mer-dsp-001 has credit_amount_cents 20000"``, where 20000 is
    mer-dsp-003's and mer-dsp-001 has no credit at all). ``quoted_identifiers``
    — the compound numerals THIS SENTENCE quoted verbatim — is compared against
    the fact's own ``row_tokens``: any quoted identifier this row does not carry
    REFUSES the numeral. A sentence naming TWO rows' identifiers refuses both
    rows' numerals: the pairing is unverifiable, the same conservative-safe
    choice the compound spelled-number rule makes. Non-``cell`` facts never take
    this path — a table-level aggregate has no row to conflict with."""
    if not (fact.anchors & anchor_universe):
        return False
    if fact.kind != "cell" or not isinstance(fact.column, str):
        return True
    # NE-GROUND-ROWID (ng-2, fail-closed): an entity identifier is
    # numeral-classified, so it is never a prose token and can never reach
    # ``anchor_universe`` -- BOTH F3 row routes are blind to it. Once such an
    # identifier grounds by verbatim containment, a sentence could attribute one
    # row's cell numeral to a DIFFERENT row and pass on the shared column name
    # alone. Any identifier this sentence quoted that this row does not carry
    # therefore refuses the numeral.
    if quoted_identifiers - fact.row_tokens:
        return False

    col_tokens = frozenset(normalize(fact.column))
    own_row_tokens = fact.anchors - col_tokens
    if own_row_tokens and own_row_tokens <= anchor_universe:
        return True  # grounded via THIS row's OWN, COMPLETE identity.

    # NE-CTXB-4: own-row identity is ALSO satisfied by full containment of any
    # SINGLE usable sibling cell's complete token set (e.g. the name cell
    # {acme, corp}) -- so a Layer-3 count sentence grounds without ever
    # speaking the row's URN citation tokens. Degenerate identities (a set that
    # is a subset of a sibling row's set for the same column) were filtered out
    # at corpus build, so this can never rescue an ambiguous claim.
    for identity in fact.identity_cells:
        if identity <= anchor_universe:
            return True

    # Grounded only via the shared column-name tokens so far -- refuse if a
    # SIBLING row (same kind="cell", same column, different row identity) is
    # also named in this sentence, since that would be a conflicting claim.
    foreign_row_tokens: set[str] = set()
    for sibling_facts in corpus.numbers.values():
        for sibling in sibling_facts:
            if sibling is fact or sibling.kind != "cell" or sibling.column != fact.column:
                continue
            sibling_row_tokens = sibling.anchors - col_tokens
            if sibling_row_tokens and sibling_row_tokens != own_row_tokens:
                foreign_row_tokens |= sibling_row_tokens
    return not (foreign_row_tokens & anchor_universe)


# ---------------------------------------------------------------------------
# §2f-rel — NE-GROUND-REL: semantic row binding for prose relations.
# ---------------------------------------------------------------------------

#: NE-GROUND-AGGRESCUE: the aggregate kinds whose value is, BY CONSTRUCTION,
#: some row's cell in that column. ``min`` and ``max`` are the whole list --
#: each is literally one of the column's cells, so if every ``cell`` fact for a
#: value was refused for naming the wrong row, a ``min``/``max`` rescue is
#: ALWAYS that same refused pairing arriving through a fact with no row.
#:
#: ``sum``, ``count``, ``distinct-count``, ``freq``, ``pairfreq`` and
#: ``envelope`` are deliberately NOT here. Their values are DERIVED and only
#: coincide with a cell by accident, and suppressing them would refuse the
#: legitimate "row X is one of N" sentence the residual's own note warned about
#: whenever N happened to equal some cell. Scoping to the extrema is what makes
#: this a closure of the bypass rather than a trade of one wrong answer for
#: another.
_EXTREMUM_KINDS: frozenset[str] = frozenset({"min", "max"})


#: NumberFact kinds whose anchor set states a VERIFIED value pairing and may
#: therefore vouch for a row-bound prose token in the same sentence once the
#: fact grounded a numeral there. ``ordinal`` (whole-row + all-column anchors)
#: and ``envelope`` (schema-only anchors) are deliberately EXCLUDED: their
#: breadth would let a list marker's digit launder an unrelated entity token
#: (``"1. Alice is closed"`` must refuse).
_RELATION_VOUCHING_KINDS: frozenset[str] = frozenset(
    {"cell", "count", "distinct-count", "freq", "pairfreq", "sum", "min", "max"}
)

#: Deterministic cap on the aggregate-cover search frontier (fail closed: a
#: draft pathological enough to exceed it loses cover candidates and can only
#: gain findings, never lose them).
_AGG_COVER_FRONTIER_CAP = 512


def _fold_variants(token: str) -> tuple[str, ...]:
    """The pinned CHECK-TIME-ONLY plural fold as membership variants — the
    same fold order :func:`_prose_in_corpus` applies."""
    variants = [token]
    if token.endswith("es"):
        variants.append(token[:-2])
    if token.endswith("s"):
        variants.append(token[:-1])
    variants.append(token + "s")
    return tuple(variants)


def _in_folded(token: str, members: frozenset[str]) -> bool:
    return any(variant in members for variant in _fold_variants(token))


def _aggregate_cover(
    remaining: tuple[str, ...],
    families: Sequence[tuple[frozenset[str], ...]],
) -> frozenset[str]:
    """Best coverage of ``remaining`` choosable as ONE anchor set per grounded
    numeral (the single-fact rule): a numeral vouches for the tokens of one
    verified interpretation, never a stitched union across its alternative
    facts — stitching is exactly how ``"1. Alice is closed"`` would launder
    (``freq(name=alice)=1`` covering ``alice`` while ``freq(status=closed)=1``
    covers ``closed``). Deterministic; frontier capped fail-closed."""
    remaining_set = frozenset(remaining)
    frontier: set[frozenset[str]] = {frozenset()}
    for alternatives in families:
        footprints: list[frozenset[str]] = []
        seen: set[frozenset[str]] = set()
        for anchors in alternatives:
            footprint = frozenset(t for t in remaining_set if _in_folded(t, anchors))
            if footprint and footprint not in seen:
                seen.add(footprint)
                footprints.append(footprint)
        if not footprints:
            continue
        grown = set(frontier)
        for cover in frontier:
            for footprint in footprints:
                grown.add(cover | footprint)
        frontier = grown
        if len(frontier) > _AGG_COVER_FRONTIER_CAP:
            ranked = sorted(frontier, key=lambda s: (-len(s), tuple(sorted(s))))
            frontier = set(ranked[:_AGG_COVER_FRONTIER_CAP])
    return max(frontier, key=lambda s: (len(s), tuple(sorted(s))))


def _relation_ungrounded_count(
    candidates: set[str],
    corpus: GroundingCorpus,
    quoted_identifiers: frozenset[str],
    spoken_iris: frozenset[str],
    agg_families: Sequence[tuple[frozenset[str], ...]],
) -> int:
    """NE-GROUND-REL: count the sentence's row-bound prose tokens whose
    asserted relation NO fetched row supports (ng-2 only; the per-token
    provenance checks already ran — this is the semantic layer on top).

    ``candidates`` are the sentence's corpus-grounded prose tokens that were
    not prompt-, allowlist- or extended-lexicon-exempt. Of those, only
    ROW-BOUND tokens are checked: present in some row's cells and not schema
    vocabulary (template-id/column-name tokens are never a row claim). Each
    row-bound token must be supported by one of:

    * **co-row partner** — another checked token shares a fetched row with it
      (the pairing IS a fetched row's own pairing);
    * **named row** — a row named in this sentence by a verbatim-quoted
      compound identifier (:data:`NumberFact.row_tokens` vocabulary) or a
      spoken row IRI carries the token (NE-GROUND-ROWID generalized to
      prose);
    * **same-column enumeration partner** — another checked token comes from
      the SAME (answer, column): listing several values of one column asserts
      no cross-column relation;
    * **aggregate vouching** — the anchor set of ONE value-bearing fact that
      grounded a numeral in this sentence contains it
      (:func:`_aggregate_cover`): the gate itself recomputed that pairing;
    * **lone mention** — the sentence's only row-bound token, with no row
      references present: naming one fetched thing asserts no relation.

    Anything else is a relation the fetched rows do not state: fail closed.

    Disclosed residuals (SPEC open-defect entry): a sentence naming BOTH rows
    and BOTH values passes even when swapped (the same bag-of-tokens fixed
    point the numeral row-binding accepts); prompt echo stays vocabulary-
    exempt (A-P2-6); attribution is sentence-scoped like every F3 anchor.
    """
    bound = [
        token
        for token in sorted(candidates)
        if not _in_folded(token, corpus.schema_prose)
        and any(_in_folded(token, row) for row in corpus.row_prose)
    ]
    if not bound:
        return 0

    named_rows: set[int] = set()
    has_row_references = False
    for quoted in sorted(quoted_identifiers):
        matches = {i for i, atomic in enumerate(corpus.row_atomic) if quoted in atomic}
        if matches:
            has_row_references = True
            named_rows |= matches
    for iri in sorted(spoken_iris):
        mapped = corpus.iri_rows.get(iri, ())
        if mapped:
            has_row_references = True
            named_rows.update(mapped)

    if len(bound) == 1 and not has_row_references:
        return 0

    bound_set = set(bound)
    unsupported: list[str] = []
    for token in bound:
        supported = False
        for i, row in enumerate(corpus.row_prose):
            if not _in_folded(token, row):
                continue
            if i in named_rows:
                supported = True
                break
            if any(other != token and _in_folded(other, row) for other in bound_set):
                supported = True
                break
        if not supported:
            for column in corpus.column_prose:
                if _in_folded(token, column) and any(
                    other != token and _in_folded(other, column) for other in bound_set
                ):
                    supported = True
                    break
        if not supported:
            unsupported.append(token)

    if not unsupported:
        return 0
    covered = _aggregate_cover(tuple(unsupported), agg_families)
    return sum(1 for token in unsupported if token not in covered)


# ---------------------------------------------------------------------------
# §2g — resources, GateProfile, both pinned profiles, GATE_VERSION (module
# import time, fail loud). Placed BEFORE check_grounding because the default
# ``profile`` argument (PROFILE_NG1_FROZEN) is evaluated at def time.
#
# GateProfile + both profile constants are DEFINED here (stdlib-only) and
# re-exported by the sibling pure module ``ne.govern.grounding_policy`` — the
# C3 AST guardian allowlists only stdlib roots for THIS module, so grounding.py
# can never import grounding_policy; the tier machinery (StrictnessTier,
# ENFORCEMENT, apply_strictness) lives in grounding_policy, which imports FROM
# here (no cycle). Profiles are frozen import-time data built from packaged
# resources exactly like today's allowlist; tier mapping is NOT this module's
# concern (it stays tier-agnostic, computing every finding class per profile).
# ---------------------------------------------------------------------------

_ALLOWLIST_BYTES = files("ne.govern").joinpath("grounding_allowlist.txt").read_bytes()
_NG2_CORE_BYTES = files("ne.govern").joinpath("grounding_allowlist_ng2.txt").read_bytes()
_EXTENDED_BYTES = files("ne.govern").joinpath("grounding_allowlist_extended.txt").read_bytes()
_NUMBER_WORDS_BYTES = files("ne.govern").joinpath("grounding_number_words.txt").read_bytes()


def _parse_allowlist(raw: bytes, *, source: str) -> frozenset[str]:
    """Parse a one-token-per-line allowlist resource (blanks/'#' comments
    ignored). Every token MUST be normalized (``normalize(token) == (token,)``)
    or import fails loud — config corruption never silently degrades the gate.
    ``source`` names the resource in the error for a legible failure."""
    tokens: set[str] = set()
    for line in raw.decode("utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if normalize(stripped) != (stripped,):
            raise ValueError(
                f"{source}: token {stripped!r} is not in normalized "
                "form (normalize(token) != (token,)) — config corruption, fail loud"
            )
        tokens.add(stripped)
    return frozenset(tokens)


def _parse_number_words(raw: bytes) -> Mapping[str, str | None]:
    """Parse ``grounding_number_words.txt`` into a ``word -> canonical`` map
    (``None`` for a bare magnitude word). Fail loud (``ValueError``) on any
    malformed line so a corrupt lexicon can never silently weaken the spelled-
    count channel:

    * a mapped line is ``'word canonical'`` (two whitespace-separated fields);
      a magnitude line is a bare ``'word'`` (one field). Any other arity is an
      error.
    * the ``word`` must be normalized (``normalize(word) == (word,)``) and must
      NOT itself be numeral-classified (a spelled number is prose, never a
      digit token).
    * a duplicate ``word`` is an error (an ambiguous mapping).
    * a mapped ``canonical`` must be all-ASCII-digits (so it matches a
      :func:`canonical_numeral` render form exactly).
    """
    mapping: dict[str, str | None] = {}
    for line in raw.decode("utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) == 1:
            word, canonical = parts[0], None
        elif len(parts) == 2:
            word, canonical = parts[0], parts[1]
        else:
            raise ValueError(
                f"grounding_number_words.txt: malformed line {stripped!r} — "
                "expected 'word' (magnitude) or 'word canonical' (mapped), fail loud"
            )
        if normalize(word) != (word,):
            raise ValueError(
                f"grounding_number_words.txt: word {word!r} is not in normalized "
                "form (normalize(word) != (word,)) — config corruption, fail loud"
            )
        if is_numeral(word):
            raise ValueError(
                f"grounding_number_words.txt: word {word!r} is numeral-classified — "
                "a number-word must be prose, fail loud"
            )
        if word in mapping:
            raise ValueError(
                f"grounding_number_words.txt: duplicate word {word!r} — "
                "an ambiguous number-word mapping, fail loud"
            )
        if canonical is not None and not (canonical.isascii() and canonical.isdigit()):
            raise ValueError(
                f"grounding_number_words.txt: canonical {canonical!r} for word "
                f"{word!r} is not all-ASCII-digits — fail loud"
            )
        mapping[word] = canonical
    # Freeze into a read-only view so ``PROFILE_NG2.number_words`` cannot be
    # remapped after import (C2) — a mutated lexicon would silently diverge from
    # the versioned rulebook while ``profile.version`` stayed put.
    return cast("Mapping[str, str | None]", _MappingProxyType(mapping))


@dataclass(frozen=True, slots=True)
class GateProfile:
    """A pinned, frozen grounding rulebook (design §3.2). Two ship in this
    module: :data:`PROFILE_NG1_FROZEN` (byte-identical to today's gate) and
    :data:`PROFILE_NG2` (the expanded default). :func:`check_grounding` is
    parameterized by one of these; it computes ALL finding classes for the
    profile and knows NOTHING about strictness tiers (that mapping lives in
    ``ne.govern.grounding_policy``).

    * ``core_allowlist`` — prose exemption honored at STRICT-and-up.
    * ``extended_lexicon`` — prose exemption honored only at STANDARD (empty
      for ng1); an ungrounded extended token becomes a ``vocab-extended``
      finding (advisory at STANDARD, withheld at STRICT).
    * ``number_words`` — spelled-number lexicon: ``word -> canonical`` numeral,
      ``None`` for a bare magnitude word (empty for ng1). Number-words are
      DERIVED through the same :class:`NumberFact` grounding as digits; they
      are never prompt-exempt and never allowlistable.
    * ``corpus_precedence_runs`` — the anti-recombination run rule: ``True``
      (ng2) makes run membership ``via_corpus and not via_prompt`` so growing
      the allowlist can never weaken ``phrase-recombination``; ``False`` (ng1)
      keeps today's ``... and not via_allowlist`` flush rule.
    * ``verbatim_compound_numerals`` — ``True`` (ng2) grounds an UNPARSEABLE
      numeral (a compound: ISO date, hyphenated id, dotted version, range) iff
      that token appears VERBATIM in ``corpus.tokens``, and makes it JOIN the
      anti-recombination run so contiguity still binds it to its neighbours;
      ``False`` (ng1) keeps today's unconditional ``numeral-unparseable``.
    * ``relational_row_binding`` — ``True`` (ng2) runs the NE-GROUND-REL
      semantic row-binding pass (:func:`_relation_ungrounded_count`): a
      row-bound prose token whose asserted relation no fetched row supports
      is a ``relation-ungrounded`` finding (a FACT class — withholds at every
      tier); ``False`` (ng1) keeps free relational phrasing model-trusted.
    * ``fact_sentence_classes`` — ``True`` (ng2) splits ungrounded vocab into
      ``vocab-extended`` / ``vocab-ungrounded-factual`` / ``vocab-ungrounded``
      by sentence fact-bearing-ness; ``False`` (ng1) emits a single
      ``vocab-ungrounded`` class exactly as today.
    * ``version`` — ``ng-1/<hash>`` / ``ng-2/<hash>``, stamped into provenance.
    """

    name: Literal["ng1-frozen", "ng2"]
    core_allowlist: frozenset[str]
    extended_lexicon: frozenset[str]
    number_words: Mapping[str, str | None]
    corpus_precedence_runs: bool
    fact_sentence_classes: bool
    verbatim_compound_numerals: bool
    relational_row_binding: bool
    version: str


#: The pinned, versioned prose-connective wordlist (§2c). Deliberately
#: EXCLUDES all comparatives/superlatives, modals, and quantifiers/
#: approximators — see ``grounding_allowlist.txt``'s header comment. This is
#: ``PROFILE_NG1_FROZEN.core_allowlist`` (kept as a module constant for the
#: existing public surface + the runtime import).
CONNECTIVE_ALLOWLIST: frozenset[str] = _parse_allowlist(
    _ALLOWLIST_BYTES, source="grounding_allowlist.txt"
)

#: The pinned-recompute-list identity (§2.2 #4) — changing the recompute set
#: (count/ordinals/distinct-count/freq/pairfreq/sum/min/max, the NE-CTXB-4
#: envelope facts, and the single-sibling-cell identity extension) REQUIRES
#: changing this string, which moves :data:`GATE_VERSION`.
_RECOMPUTE_LIST_ID = (
    "count|ordinals|distinct-count|freq|pairfreq(cap=8)|sum|min|max|"
    "envelope(returned_rows,total_rows,row_limit)|"
    "cell-identity-v2(single-sibling-cell,degenerate-subset-refuse)"
)

#: ``ng-1/<sha256(allowlist bytes + pinned-recompute-list id)[:12]>`` — the
#: gate's version id, stamped into ``GroundedProvenance`` (G3). UNCHANGED from
#: today (byte-for-byte); it is also ``PROFILE_NG1_FROZEN.version``.
GATE_VERSION = (
    "ng-1/"
    + hashlib.sha256(_ALLOWLIST_BYTES + b"\x00" + _RECOMPUTE_LIST_ID.encode("utf-8")).hexdigest()[
        :12
    ]
)

#: The ng-2 rulebook identity: today's recompute id EXTENDED with the ng-2
#: rule set (corpus-precedence runs, fact-sentence vocab classes, number-word
#: mapping). Folded into :data:`PROFILE_NG2`'s version alongside all three ng-2
#: resource files' bytes — any rule OR resource edit moves ``ng-2/<hash>``.
_RULE_ID_NG2 = (
    _RECOMPUTE_LIST_ID
    + "|runs=corpus-precedence|vocab=fact-sentence-v1|numberwords=map-v1-compound-refuse"
    + "|numerals=verbatim-compound-v1(exact-containment,in-run)"
    + "|rowid=quoted-identifier-conflict-v1"
    + "|aggrescue=cell-value-needs-cell-fact-v1"
    + "|relations=row-binding-v1(corow,cocolumn,row-refs,agg-single-fact,lone-mention)"
)

#: PROFILE_NG1_FROZEN — today's exact bytes + rules; version == GATE_VERSION.
#: Every existing ``check_grounding`` caller defaults to this, so the merge is
#: zero-behavior-change (design §3.4 / C-P1-5). NEVER emits the two ng-2-only
#: classes (empty extended lexicon + ``fact_sentence_classes=False``).
PROFILE_NG1_FROZEN: GateProfile = GateProfile(
    name="ng1-frozen",
    core_allowlist=CONNECTIVE_ALLOWLIST,
    extended_lexicon=frozenset(),
    number_words=cast("Mapping[str, str | None]", _MappingProxyType({})),
    corpus_precedence_runs=False,
    fact_sentence_classes=False,
    verbatim_compound_numerals=False,
    relational_row_binding=False,
    version=GATE_VERSION,
)

#: PROFILE_NG2 — the expanded default: superset core allowlist, a STANDARD-only
#: extended lexicon, the derived number-word lexicon, corpus-precedence runs,
#: and fact-sentence vocab classification. Its version hashes ALL THREE ng-2
#: resource files' bytes plus the ng-2 rule id (steward-review discipline
#: extended to every new input).
PROFILE_NG2: GateProfile = GateProfile(
    name="ng2",
    core_allowlist=_parse_allowlist(_NG2_CORE_BYTES, source="grounding_allowlist_ng2.txt"),
    extended_lexicon=_parse_allowlist(_EXTENDED_BYTES, source="grounding_allowlist_extended.txt"),
    number_words=_parse_number_words(_NUMBER_WORDS_BYTES),
    corpus_precedence_runs=True,
    fact_sentence_classes=True,
    verbatim_compound_numerals=True,
    relational_row_binding=True,
    version="ng-2/"
    + hashlib.sha256(
        _NG2_CORE_BYTES
        + b"\x00"
        + _EXTENDED_BYTES
        + b"\x00"
        + _NUMBER_WORDS_BYTES
        + b"\x00"
        + _RULE_ID_NG2.encode("utf-8")
    ).hexdigest()[:12],
)


def _passing_number_facts(
    canonical: str,
    corpus: GroundingCorpus,
    anchor_universe: frozenset[str],
    quoted_identifiers: frozenset[str],
    profile: GateProfile,
) -> list[NumberFact]:
    """The facts that may ground ``canonical`` in this sentence: the F3 pass
    (:func:`_fact_grounded`) over every :class:`NumberFact` carrying the value,
    then the NE-GROUND-AGGRESCUE suppression. Empty result == the numeral is
    ``numeral-ungrounded``.

    **SHARED BY BOTH NUMERAL SITES ON PURPOSE.** :func:`check_grounding` grounds
    a value at TWO places in the same sentence loop -- the DIGIT site
    (``_NumeralToken``) and the spelled NUMBER-WORD site (a ``_ProseToken`` in
    ``nw_indices``, design §2.4) -- and the two held byte-identical ``passing``
    comprehensions until AGGRESCUE landed at the digit site ALONE. The result was
    a live P1: on a corpus where 5 is mer-dsp-003's ``open_disputes`` cell and
    also ``min(open_disputes)``, ``"mer-dsp-001 has open_disputes 5"`` was
    correctly refused while ``"mer-dsp-001 has open_disputes five"`` PASSED, and
    ``run_claim_verifier`` (hardcoded to :data:`PROFILE_NG2`) then minted a
    ``DeterministicClaimVerification`` receipt attesting the false row/value
    pairing. Number-words are DERIVED through the same ``NumberFact`` machinery
    as digits and are never prompt-exempt and never allowlistable, so every rule
    about WHICH facts may ground a value belongs here, once, where neither site
    can grow it alone.

    NE-GROUND-AGGRESCUE: a ROW-LESS fact may not rescue a value that IS some
    row's cell.

    :func:`_fact_grounded` decides ONE fact; the callers take ``any()`` over
    every fact carrying the value. The aggregate kinds (min/max/sum/count/
    distinct-count/freq) carry NO row identity -- their entire anchor set is the
    column name. So when every ``cell`` fact for the value was refused for naming
    the WRONG row, an aggregate whose value coincides grounds on the shared
    column name alone and rescues them, defeating the binding NE-GROUND-ROWID
    and NE-CTXB-4 exist to enforce. That is the same "a true column label rescues
    a FALSE row/number pairing" this module's own :func:`_fact_grounded`
    docstring names as the thing it prevents -- closed through the cell fact and
    left open through the aggregate.

    WHY IT MATTERS AT THESE SITES and not only in a diagnostic:
    ``run_claim_verifier`` raises ``ClaimVerificationUnavailable`` on a failed
    verdict and mints a ``DeterministicClaimVerification`` receipt on a passed
    one, so the bypass ATTESTS a false attribution rather than merely logging it.

    The rule: if any ``cell`` fact carries this canonical value, only a ``cell``
    fact may ground it. It bites in exactly one situation -- every cell fact for
    the value was REFUSED, which happens only when the sentence names a row
    identity that is not that value's row. With no row named the cell fact
    grounds on its own (its sibling-conflict check passes), so a genuine
    aggregate claim is untouched; verified by the no-row-named cases in the
    tests, in digit AND spelled form.

    NOT a size-dependent nicety: in a TWO-row answer every value is the min or
    the max, so before this the row binding protected nothing at all there.

    .. warning::
       GATED ON ``profile.relational_row_binding``, and a Codex pass was right to
       require it. This rule was added to ``_RULE_ID_NG2`` but the suppression
       itself ran for EVERY profile -- so under :data:`PROFILE_NG1_FROZEN` a
       two-row corpus went from accepting a sentence via its ``max`` fact to
       ``numeral-ungrounded`` while :data:`GATE_VERSION` stayed the same. A
       frozen profile whose behaviour moves under an unchanged version makes one
       provenance version stand for two rule sets, which is the record-drift
       failure C10 exists to prevent. The rescue it closes is a property of row
       binding, so it belongs behind the flag that turns row binding on. (ng1
       never reaches the number-word site at all: its ``number_words`` map is
       empty. Sharing this helper therefore leaves ng1 byte-for-byte unmoved.)
    """
    facts = corpus.numbers.get(canonical, ())
    passing = [
        fact for fact in facts if _fact_grounded(fact, corpus, anchor_universe, quoted_identifiers)
    ]
    refused_cell_columns = {
        fact.column for fact in facts if fact.kind == "cell" and isinstance(fact.column, str)
    } - {fact.column for fact in passing if fact.kind == "cell" and isinstance(fact.column, str)}
    if profile.relational_row_binding and refused_cell_columns:
        passing = [
            fact
            for fact in passing
            if not (fact.kind in _EXTREMUM_KINDS and fact.column in refused_cell_columns)
        ]
    return passing


def check_grounding(
    draft: str, corpus: GroundingCorpus, prompt: str, *, profile: GateProfile = PROFILE_NG1_FROZEN
) -> GroundingVerdict:
    """Classify every token of ``draft`` against ``corpus`` (this turn's
    fetched facts) and ``prompt`` (the user's own words — vocabulary-exempt,
    NEVER numeral-exempt), under the pinned rulebook ``profile`` (defaulting to
    :data:`PROFILE_NG1_FROZEN`, byte-identical to today). PURE and
    tier-agnostic: it computes EVERY finding class for the profile and knows
    nothing about strictness tiers — :func:`ne.govern.grounding_policy.apply_strictness`
    maps classes to withhold/advisory. See the module docstring + design
    §2.3/§2.7/§3 for the full rule set."""
    prompt_tokens = frozenset(normalize(prompt))

    citation_out_of_range = 0
    iri_ungrounded = 0
    numeral_ungrounded = 0
    numeral_unparseable = 0
    phrase_recombination = 0
    relation_ungrounded = 0
    vocab_extended = 0
    vocab_ungrounded = 0
    vocab_ungrounded_factual = 0
    prompt_echo_count = 0

    number_words = profile.number_words

    for sentence in _split_sentences(draft):
        events = _tokenize(sentence)

        # PASS A: grounded-prose set (corpus precedence) + fact-bearing-ness.
        # A prose token that is a number-word occurrence (§2.4) counts ONLY
        # when it does NOT ground as corpus prose first (corpus precedence: a
        # "One World Corp" name cell still grounds as prose, A-P2-8). A
        # sentence is fact-bearing iff it carries any numeral/IRI/citation event
        # OR a number-word occurrence — that is where ungrounded vocabulary
        # withholds at STANDARD (ng2), so ``fact_bearing`` gates the vocab class.
        grounded_prose: set[str] = set()
        has_fact_event = False
        nw_indices: set[int] = set()
        quoted: set[str] = set()
        for index, event in enumerate(events):
            if isinstance(event, _ProseToken):
                if _prose_in_corpus(event.token, corpus):
                    grounded_prose.add(event.token)
                elif number_words and event.token in number_words:
                    nw_indices.add(index)
            else:
                has_fact_event = True
                # NE-GROUND-ROWID: the compound numerals this sentence quotes
                # verbatim are its row-identity evidence (see _fact_grounded).
                if (
                    isinstance(event, _NumeralToken)
                    and profile.verbatim_compound_numerals
                    and event.canonical is None
                    and event.folded in corpus.tokens
                ):
                    quoted.add(event.folded)
        fact_bearing = has_fact_event or bool(nw_indices)
        anchor_universe = _expand_plural(grounded_prose)
        quoted_identifiers = frozenset(quoted)
        # NE-GROUND-REL per-sentence state (ng-2 only; empty and inert on ng1).
        rel_candidates: set[str] = set()
        spoken_iris: set[str] = set()
        agg_families: list[tuple[frozenset[str], ...]] = []

        run_tokens: list[str] = []
        for index, event in enumerate(events):
            if isinstance(event, _Citation):
                if _flush_run(run_tokens, corpus):
                    phrase_recombination += 1
                if not (1 <= event.marker <= corpus.citation_count):
                    citation_out_of_range += 1
            elif isinstance(event, _IriToken):
                if _flush_run(run_tokens, corpus):
                    phrase_recombination += 1
                if event.raw not in corpus.iris:
                    iri_ungrounded += 1
                elif profile.relational_row_binding:
                    # A grounded row IRI spoken in prose NAMES its row
                    # (NE-GROUND-REL), exactly like a quoted identifier.
                    spoken_iris.add(event.raw)
            elif isinstance(event, _NumeralToken):
                # ng2 verbatim-compound rule: a numeral canonical_numeral cannot
                # parse (an ISO date, a hyphenated case id, a dotted version, a
                # range) is a QUOTATION, not an assertion — it grounds IFF it
                # appears VERBATIM in the corpus, the same containment rule
                # prose gets. EXACT membership only (never _prose_in_corpus: the
                # plural fold is meaningless for a numeral and would only widen
                # this). It cannot fabricate "2026-05-15" from a corpus that says
                # "2026-05-14" — that exact token is not in corpus.tokens. And it
                # JOINS the run rather than flushing it, so _run_in_phrase
                # contiguity still binds it to its neighbours: a date lifted from
                # chunk A next to prose from chunk B is still phrase-recombination.
                if (
                    event.canonical is None
                    and profile.verbatim_compound_numerals
                    and event.folded in corpus.tokens
                ):
                    run_tokens.append(event.folded)
                    continue
                if _flush_run(run_tokens, corpus):
                    phrase_recombination += 1
                if event.canonical is None:
                    numeral_unparseable += 1
                else:
                    passing = _passing_number_facts(
                        event.canonical, corpus, anchor_universe, quoted_identifiers, profile
                    )
                    if not passing:
                        numeral_ungrounded += 1
                    elif profile.relational_row_binding:
                        alternatives = tuple(
                            dict.fromkeys(
                                fact.anchors
                                for fact in passing
                                if fact.kind in _RELATION_VOUCHING_KINDS
                            )
                        )
                        if alternatives:
                            agg_families.append(alternatives)
            else:
                token = event.token
                via_corpus = _prose_in_corpus(token, corpus)
                # Number-word occurrence (ng2 only): corpus precedence FIRST,
                # then DERIVE the spelled number through the SAME NumberFact/F3
                # machinery as a digit numeral. Never prompt-exempt, never
                # allowlistable — it flushes the run like a digit numeral.
                if index in nw_indices:
                    if _flush_run(run_tokens, corpus):
                        phrase_recombination += 1
                    # Adjacent number-words are a spelled COMPOUND ("twenty one"
                    # = 21). Grounding per-token is a fact-fabrication bypass —
                    # "twenty"->20 and "one"->1 may each ground independently
                    # while the asserted compound value is fabricated (Codex C1).
                    # Refuse the whole adjacent run as ONE numeral-unparseable,
                    # emitted at the run's first element (design §2.4/§5.4:
                    # compounds refuse, conservative-safe — a false spelled count
                    # can never pass; a true compound is re-drafted in digits).
                    if (index - 1) in nw_indices or (index + 1) in nw_indices:
                        if (index - 1) not in nw_indices:
                            numeral_unparseable += 1
                        continue
                    canonical = number_words[token]
                    if canonical is None:
                        numeral_unparseable += 1
                    else:
                        passing = _passing_number_facts(
                            canonical, corpus, anchor_universe, quoted_identifiers, profile
                        )
                        if not passing:
                            numeral_ungrounded += 1
                        elif profile.relational_row_binding:
                            alternatives = tuple(
                                dict.fromkeys(
                                    fact.anchors
                                    for fact in passing
                                    if fact.kind in _RELATION_VOUCHING_KINDS
                                )
                            )
                            if alternatives:
                                agg_families.append(alternatives)
                    continue
                via_prompt = token in prompt_tokens
                via_core = token in profile.core_allowlist
                # Run rule: ng2 uses corpus precedence (allowlist membership no
                # longer flushes a corpus-grounded token's run, so growing the
                # allowlist can never weaken phrase-recombination, A-P1-3); ng1
                # keeps today's ``... and not via_allowlist`` flush byte-for-byte.
                if profile.corpus_precedence_runs:
                    in_run = via_corpus and not via_prompt
                else:
                    in_run = via_corpus and not via_prompt and not via_core
                if in_run:
                    run_tokens.append(token)
                else:
                    if _flush_run(run_tokens, corpus):
                        phrase_recombination += 1
                if (
                    profile.relational_row_binding
                    and via_corpus
                    and not via_prompt
                    and not via_core
                    and token not in profile.extended_lexicon
                ):
                    rel_candidates.add(token)
                if not (via_corpus or via_prompt or via_core):
                    if profile.fact_sentence_classes:
                        if token in profile.extended_lexicon:
                            vocab_extended += 1
                        elif fact_bearing:
                            vocab_ungrounded_factual += 1
                        else:
                            vocab_ungrounded += 1
                    else:
                        vocab_ungrounded += 1
                elif via_prompt and not via_corpus and not via_core:
                    prompt_echo_count += 1
        if _flush_run(run_tokens, corpus):
            phrase_recombination += 1
        if profile.relational_row_binding:
            relation_ungrounded += _relation_ungrounded_count(
                rel_candidates,
                corpus,
                quoted_identifiers,
                frozenset(spoken_iris),
                agg_families,
            )

    findings = [
        GroundingFinding(reason, count)  # type: ignore[arg-type]
        for reason, count in (
            ("citation-out-of-range", citation_out_of_range),
            ("iri-ungrounded", iri_ungrounded),
            ("numeral-ungrounded", numeral_ungrounded),
            ("numeral-unparseable", numeral_unparseable),
            ("phrase-recombination", phrase_recombination),
            ("relation-ungrounded", relation_ungrounded),
            ("vocab-extended", vocab_extended),
            ("vocab-ungrounded", vocab_ungrounded),
            ("vocab-ungrounded-factual", vocab_ungrounded_factual),
        )
        if count
    ]
    findings.sort(key=lambda finding: finding.reason)
    return GroundingVerdict(
        passed=not findings,
        findings=tuple(findings),
        prompt_echo_count=prompt_echo_count,
    )
