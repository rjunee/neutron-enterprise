# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Presidio redaction entrypoint — runs INSIDE the isolated hash-pinned venv.

This module imports ``presidio_analyzer`` / ``presidio_anonymizer``, which are
deliberately NOT part of the platform virtualenv (their transitive closure pulls
in spaCy and the ~560 MB ``en_core_web_lg`` NER model — the heavy-scientific-Python
class SPEC §11 keeps out of the platform lock, C4). It is executed as a subprocess
by ``ne.govern.redaction.presidio_backend`` against
``packages/ne-govern/.presidio-venv``.

Protocol: reads ``{"text": str, "categories"?: [str, ...]}`` from --input, writes
``{"text": str, "redactions": [str, ...]}`` to --output. Each detected entity is
replaced with ``<REDACTED:{ENTITY_TYPE}>``; ``redactions`` is the sorted,
de-duplicated list of entity types that were redacted (deterministic output).

Registers the NE custom context-gated US_SSN recognizer
(``NeUsSsnContextRecognizer``) to close presidio-analyzer 2.2.363's sample-number
under-redaction gap (NE-CKC11).
"""

# pyright: reportMissingImports=false, reportMissingModuleSource=false
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerResult
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

# --- NE custom context-gated US_SSN recognizer (NE-CKC11) --------------------
# presidio-analyzer 2.2.363's builtin UsSsnRecognizer.invalidate_result
# blocklists documentation/sample SSNs (only_digits starting 123456789,
# 98765432, 078051120, 000, 666, plus mixed-delimiter/all-same-digit forms), so
# a doc-style SSN like ``123-45-6789`` is NEVER redacted even with 'SSN is'
# context (C12 under-redaction). See NE-CKC11.
_SSN_CONTEXT_RE = re.compile(r"\b(?:ssn|ssns|social\s+security)\b", re.IGNORECASE)
_SSN_CONTEXT_WINDOW_CHARS = 64
# Must strictly exceed SpacyRecognizer ner_strength (0.85) so an equal-span NER
# hit (e.g. DATE_TIME on a digit run) can never outrank the US_SSN result during
# conflict resolution, and stay < 1.0.
_NE_SSN_SCORE = 0.9
# Three single-delimiter 3-2-4 patterns — mixed delimiters like ``123-45 6789``
# match nothing by construction (the over-redaction guard, alongside context).
_NE_SSN_PATTERNS = [
    Pattern("NE US_SSN dashed (strong with context)", r"\b\d{3}-\d{2}-\d{4}\b", _NE_SSN_SCORE),
    Pattern("NE US_SSN spaced (strong with context)", r"\b\d{3} \d{2} \d{4}\b", _NE_SSN_SCORE),
    Pattern("NE US_SSN dotted (strong with context)", r"\b\d{3}\.\d{2}\.\d{4}\b", _NE_SSN_SCORE),
]


def _ssn_context_nearby(text: str, start: int) -> bool:
    """True iff an SSN/social-security keyword occurs in the 64 chars preceding
    ``start`` (prefix-only, mirroring presidio's prefix-only LemmaContextAwareEnhancer)."""
    window_start = max(0, start - _SSN_CONTEXT_WINDOW_CHARS)
    return _SSN_CONTEXT_RE.search(text, window_start, start) is not None


class NeUsSsnContextRecognizer(PatternRecognizer):
    """Additive, context-REQUIRED US_SSN recognizer (NE-CKC11).

    presidio 2.2.363's builtin UsSsnRecognizer.invalidate_result blocklists
    sample numbers (digits starting 123456789 / 98765432 / 078051120 / 000 /
    666), so a documentation-style SSN like ``123-45-6789`` is NEVER redacted
    even with 'SSN is' context. Because AnalyzerEngine's default_score_threshold
    is 0 (any emitted result redacts), the context REQUIREMENT must live in the
    recognizer itself rather than in a score threshold. This recognizer is
    ADDITIVE (the builtin stays registered and unchanged) and fires ONLY on a
    structured 3-2-4 grouping with one consistent delimiter AND an SSN keyword in
    the preceding 64 chars — that is the over-redaction guard.
    """

    def __init__(self) -> None:
        super().__init__(
            supported_entity="US_SSN",
            name="NeUsSsnContextRecognizer",
            patterns=_NE_SSN_PATTERNS,
        )

    def analyze(
        self,
        text: str,
        entities: list[str],
        nlp_artifacts: object = None,
        regex_flags: int | None = None,
    ) -> list[RecognizerResult]:
        return [
            r
            for r in super().analyze(text, entities, nlp_artifacts, regex_flags)
            if _ssn_context_nearby(text, r.start)
        ]


def redact(text: str, categories: list[str] | None) -> dict[str, object]:
    analyzer = AnalyzerEngine()
    analyzer.registry.add_recognizer(NeUsSsnContextRecognizer())  # NE-CKC11
    anonymizer = AnonymizerEngine()

    entities = categories if categories else None
    results = analyzer.analyze(text=text, language="en", entities=entities)

    entity_types = sorted({r.entity_type for r in results})
    operators = {
        etype: OperatorConfig("replace", {"new_value": f"<REDACTED:{etype}>"})
        for etype in entity_types
    }
    anonymized = anonymizer.anonymize(text=text, analyzer_results=results, operators=operators)
    return {"text": anonymized.text, "redactions": entity_types}


def main() -> None:
    parser = argparse.ArgumentParser(description="Redact PII with Presidio.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    request = json.loads(args.input.read_text(encoding="utf-8"))
    result = redact(request["text"], request.get("categories"))
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
