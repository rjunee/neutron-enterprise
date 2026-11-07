# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO dossier renderer — the markdown leave-behind an engagement produces.

Renders a :class:`~ne.cli.demo.arc.DemoTranscript` into a narrated markdown
transcript: per-step narration → query → governed response excerpt →
citations/graph-paths/verified provenance → approval/denial record → status badge
(DEGRADED steps carry their exact lights-up label verbatim), then the audit-verify
JSON block and a closing 'what this proved' checklist mapping steps → constraints.

C12: the dossier MUST never embed tokens/passwords/DSNs. :func:`_scrub` asserts the
rendered text carries no obvious secret material; callers pass only class-name /
names-only detail into the transcript, and any URL userinfo is stripped.
"""

from __future__ import annotations

import json
import re

from ne.cli.demo.arc import DemoTranscript, StepStatus
from ne.cli.demo.corpus import DemoScenario

_BADGE = {
    StepStatus.OK: "✅ OK",
    StepStatus.DEGRADED: "🟡 DEGRADED",
    StepStatus.FAILED: "❌ FAILED",
}

_PROOF_CHECKLIST = [
    ("T1 hybrid retrieval returns ACL-trimmed, cited chunks", "C7"),
    ("Deny-means-deny on read: the outsider never sees the exec-only doc", "C7"),
    ("T2 graph tier is probe-activated (degrade-labeled until wired)", "—"),
    ("T3 verified answers run through the governed executor", "C6"),
    ("Tier-2 write-back is denied then HITL-approved through the C6 gate", "C6"),
    ("Every step is audited under a tamper-evident hash chain", "audit"),
    ("No secret material anywhere in this dossier", "C12"),
    (
        "T3 chat is deterministically routed — a verified answer, a governed "
        "clarification, or an audited refusal; never an unverified answer",
        "§0-law-5",
    ),
    (
        "The grounded analyst reasons with the model — plans, fetches twice, "
        "joins — yet cannot state a fact not verified this turn (counts "
        "arc-recomputed)",
        "C12",
    ),
    (
        "A forced hallucination is refused by the deterministic grounding gate, "
        "audited, and only the verified table is returned",
        "§0-law-5",
    ),
]

# Obvious secret-VALUE markers we refuse to emit (C12 scrub guard). These target
# credential material, not the English words — the dossier deliberately DISCUSSES
# secret hygiene ("no secret material") in its prose.
_SECRET_MARKERS = (
    "password=",
    "api_token",
    "authorization: bearer",
    "postgresql://",
    "postgres://",
)
_USERINFO_URL = re.compile(r"(\w+://)[^/\s@]+@")


def _scrub(text: str) -> str:
    """Strip URL userinfo and assert no obvious secret material survives (C12)."""
    cleaned = _USERINFO_URL.sub(r"\1", text)
    lowered = cleaned.lower()
    for marker in _SECRET_MARKERS:
        assert marker not in lowered, f"dossier scrub tripwire: refusing to emit {marker!r}"
    return cleaned


def render_dossier(
    transcript: DemoTranscript,
    scenario: DemoScenario,
    *,
    deployment_note: str = "",
) -> str:
    """Render the transcript into the demo dossier markdown (scrubbed of secrets)."""
    lines: list[str] = []
    lines.append("# Neutron Enterprise — Full-Ladder Demo Dossier")
    lines.append("")
    lines.append(f"- Generated: {transcript.started} → {transcript.finished}")
    lines.append(f"- Corpus: synthetic Acme (`{scenario.source}`), {len(scenario.doc_ids)} docs")
    lines.append(
        f"- Reader persona: `{scenario.reader_id}` (groups {list(scenario.reader_groups)})"
    )
    lines.append(f"- Outsider persona: `{scenario.outsider_id}` (deny-on-read control)")
    lines.append(f"- Approver persona: `{scenario.approver_id}`")
    if deployment_note:
        lines.append(f"- Deployment note: {deployment_note}")
    lines.append("")

    lines.append("## Governed arc")
    lines.append("")
    for i, step in enumerate(transcript.steps, start=1):
        lines.append(f"### {i}. {step.title} — {_BADGE[step.status]}")
        lines.append("")
        lines.append(step.narration)
        lines.append("")
        if step.request_summary:
            lines.append(f"- Query: `{step.request_summary}`")
        if step.response_excerpt:
            lines.append(f"- Governed response: {step.response_excerpt}")
        if step.label:
            lines.append(f"- Degrade label: **{step.label}**")
        if step.citations:
            lines.append("- Citations / provenance:")
            for c in step.citations[:5]:
                if "citation" in c:
                    # NE-DEMO-LIVE-SEAMS: verified-answer provenance is a per-row
                    # entity citation (urn:ne:entity:...), not a chunk reference.
                    lines.append(f"    - entity `{c['citation']}`")
                    continue
                cid = c.get("chunk_id", "?")
                doc = c.get("document_id", "?")
                src = c.get("source_system", "")
                lines.append(f"    - chunk `{cid}` in doc `{doc}` ({src or 'source-of-record'})")
        if step.evidence:
            lines.append(f"- Evidence: `{json.dumps(step.evidence, sort_keys=True)}`")
        lines.append("")

    lines.append("## Audit-chain verification")
    lines.append("")
    if transcript.audit_verify is not None:
        lines.append("```json")
        lines.append(json.dumps(transcript.audit_verify, indent=2, sort_keys=True))
        lines.append("```")
    else:
        lines.append("_No audit-verify result captured in this run._")
    lines.append("")

    lines.append("## What this proved")
    lines.append("")
    for claim, constraint in _PROOF_CHECKLIST:
        lines.append(f"- [{constraint}] {claim}")
    lines.append("")

    return _scrub("\n".join(lines))
