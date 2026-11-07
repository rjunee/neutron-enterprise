# Neutron Enterprise — Architecture White Paper

Technical architecture white paper for system architects, security
reviewers, and compliance/regulatory reviewers evaluating Neutron Enterprise.

## Files

- `neutron-enterprise-architecture-whitepaper.md` — the source document (markdown).
- `neutron-enterprise-architecture-whitepaper.pdf` — the rendered deliverable, with a
  cover page and a "Neutron Enterprise · Apache License 2.0" footer
  on every page.
- `build-pdf.sh` / `assemble.py` — the build pipeline.

**Current version: v1.4 (21 July 2026)** — adds §14.2b, "Why the platform does not
itself build on LangGraph or CrewAI — a tested conclusion, not a preference": both
leading agent frameworks were tested in real feasibility exercises against the
platform's merge-blocking supply-chain guards and both are structurally inadmissible
in-process, via independent chains (langgraph → langchain-core vs the sovereignty
guard, proven by an actually blocked build; crewai → chromadb → posthog vs the
existing posthog guard, plus four unresolvable version pins incl. pydantic <2.13 vs
the locked 2.13.4). The section is deliberately framework-respectful — the finding is
about the category meeting a sovereignty-guarded lock, never "these frameworks are
bad" — and states the customer-friendly consequence: LangGraph is governed
out-of-process today (§14.2a); **the CrewAI drop-in equivalent has since shipped too**
(NE-CREWAI-DROPIN, merged 2026-07-22) — and NE-RTSWAP-C (merged same day) proves both
out-of-process drop-ins plus the default in-process runtime produce IDENTICAL governed
outcomes, by one executable test. Grounding: `docs/architecture/ne-rtswap-design.md`.
(v1.3, 21 July, added §4.19 skill library / Skill Forge with the Interfaces section
renumbered to §4.20 — its portability wording stays "second binding in progress" until
the conformance swap test lands; v1.2, 18 July, added the C10 audit-integrity
constraint; v1.1, 17 July, was the copy sent to the first external security reviewer.
The markdown front-matter version was stale at "1.1" through v1.2 and is now kept in
lockstep.)

**⚠️ One claim runs slightly ahead of the code — `NE-AUDIT-CRON`.** The C10 row,
§8, and §11.1 say checkpoints are **periodic**. Today the checkpoint primitive and
the on-demand `ne audit checkpoint` CLI ship, but the scheduler loop is still in
flight as unit NE-AUDIT-CRON. Owner call, 2026-07-18: keep the "periodic" wording,
because the unit is actively building and will land well before this document
reaches another external reviewer. **If NE-AUDIT-CRON stalls or is abandoned, this
wording must come back out** — it is the one claim in this document not backed by
merged code. Everything else about C10 (append-only triggers, the hash chain, the
verification path) is live and CI-blocking today. v1.1 is a truthfulness pass over v1.0: it scopes the grounding
gate to the profile and lane where it actually runs, scopes connector and
permission-trimming claims to what is fixture-verified, drops a blanket
"every control is implemented and operational" warranty, and corrects the C2
egress and IP-ownership framing (see below).

## Constraint numbering matches SPEC (as of 2026-07-18)

The white paper and SPEC use the **same** constraint numbers — C1–C12, with
C10 = audit-log integrity, C11 = supply chain, C12 = deterministic guardrails.

This was briefly not true. Retiring the old C10 ("single-tenant") on 2026-07-17
left SPEC/code running C1–C9,C11,C12 while the customer docs renumbered gaplessly,
so C10 meant *supply chain* in the white paper and *nothing* in SPEC. That offset
was closed on 2026-07-18 by filling the slot with a real constraint (audit-log
integrity) in **both** schemes rather than renumbering either one — which cost
zero code churn, since C11/C12 kept their existing meanings. SPEC §11 records the
decision and the candidate audit behind it.

**If you find the two schemes disagreeing again, fix the document that drifted —
do not renumber SPEC/code.** The C11/C12 identifiers appear in ~580 code, CI, and
comment references plus all git history; they are the stable anchor.

## Egress framing (do not regress this)

- **Egress.** There is no single "safe" lane. Every outbound path — model inference,
  connectors, external tool calls, write-backs — is deny-by-default allowlisted,
  policy-checked, and audited, because any of them can carry context out. Frontier
  APIs through the gateway are the *model-inference* lane, not the only egress.
  `AIR_GAP` removes external egress entirely.

## Rebuilding the PDF

```bash
bash docs/whitepaper/build-pdf.sh
```

Pipeline: `pandoc` renders the markdown to an HTML fragment; `assemble.py` wraps it in
the deck-studio "Engineered Blueprint" print shell (house-style typography + cover page
+ fixed per-page footer); Google Chrome renders it to PDF via
`--headless --print-to-pdf`. No LaTeX required.

**Dependencies:** `pandoc` and Google Chrome. Both are already present on the build host.

## Editing

Edit the markdown only; the PDF is regenerated from it. The footer text is
defined once in `assemble.py` (`FOOTER`) and repeats on every page automatically.
