# Anti-Hallucination Grounding — How Neutron Refuses to State an Unverified Fact

> **Status:** authoritative technical explainer for a shipped subsystem.
> **Scope:** the deterministic grounding gate (`ne.govern.grounding`), its
> strictness policy (`ne.govern.grounding_policy`), the runtime enforcement +
> G4 assist (`ne.runtime.grounding`), and the demo/eval wiring.
> Build units: NE-HGATE-0…4, NE-CTXB-4/5/7.

---

## 1. Motivation — why "confident anti-hallucination" is a product, not a feature

A large language model is a fluent generator, not a database. Left to itself it will
state a number, a name, or a relationship that is *plausible* but was never in the data
it was given. In a consumer chatbot this is an annoyance. In a regulated enterprise it is
a **liability event**: a fabricated figure in a financial summary, an invented patient
detail in a clinical note, a misattributed obligation in a contract review, or a made-up
capability in a defense briefing can each carry legal, safety, or compliance consequences
that dwarf the value of the answer.

The industries Neutron targets do not merely *prefer* accuracy — they are **required to
prove control over it**:

- **Financial services** (SR 11-7 model-risk management, SEC/FINRA recordkeeping): every
  material figure surfaced to a decision-maker must be traceable to a source, and the firm
  must be able to demonstrate the control that guarantees it.
- **Healthcare / life sciences** (FDA, HIPAA): a clinical or pharmacovigilance assistant
  that invents a dosage or an adverse-event count is a patient-safety failure.
- **Legal / contracts**: an assistant that misstates a clause or a party is malpractice-adjacent.
- **Defense, intelligence, critical infrastructure**: a confidently wrong intelligence
  summary can drive a wrong decision; provenance and refusal are mission requirements.
- **Government / public sector**: FOIA-able, audit-able answers where "the AI made it up"
  is not an acceptable incident report.

For these buyers the differentiator is not "our model hallucinates less." It is
**"our platform makes it structurally impossible to surface a fact the answer did not
verify this turn, and we can prove it after the fact."** That is *confident*
anti-hallucination: not a probabilistic reduction, but a **deterministic, auditable gate**
with a governed refusal. This document describes that gate.

### 1.1 The design stance in one sentence

> Every content token of a model's answer is classified against the facts fetched **this
> turn**; if any token asserts an entity, number, citation, or identity that is not
> grounded in those facts, the answer is **withheld in its entirety** and a deterministic,
> source-backed fallback is emitted instead — audited, counts-only, tamper-evident.

Three properties make it a control rather than a heuristic:

1. **Deterministic.** No second LLM "judge," no embedding similarity, no probability
   threshold. The same draft + same fetched data always produces the same verdict. A
   deterministic control is one a regulator can be shown and a test can pin.
2. **Fail-closed.** When in doubt, refuse. Zero fetches ⇒ nothing is speakable. An
   unparseable numeral refuses. A malformed fetched answer contributes **zero** facts.
3. **Privacy-preserving by construction (C12).** A finding carries only a *reason class*
   and a *count* — never the offending text. The ungrounded draft never leaves the gate
   module, so the control itself cannot become an exfiltration channel.

---

## 2. Where the gate sits in the request path

Neutron has two grounded read paths, both funneling through the same gate:

- **Verified-answer-first** (the T3 "verified read" tools): a governed SPARQL/aggregate
  template executes server-side and returns a **`VerifiedAnswer`** — columns, rows,
  per-row citations, an envelope (`returned_rows` / `total_rows` / `row_limit`), a
  `config_hash`, and an `ontology_version`. This object *is* the ground truth.
- **Grounded-agent** (the "grounded analyst"): the model reasons, decides which verified
  reads to fetch, calls them, and then narrates. The narration is the draft the gate
  judges.

The gate runs as the **G3 post-step** inside the runtime turn
(`ne.runtime.grounding.run_grounding_gate`), *after* the model produces a draft and
*before* anything is persisted or streamed to the client. It has exactly **one return
statement / one emit point** (a binding invariant): every value ever assigned to the
output either already passed `check_grounding` or is re-checked by it before assignment.
That single-emit discipline is what lets the audit record be trusted — there is no code
path that emits ungoverned text.

```
model draft ─► GroundingCorpus.from_answers(this turn's fetches)
            ─► check_grounding(draft, corpus, prompt)  ──► GroundingVerdict (tier-agnostic)
            ─► apply_strictness(verdict, deployment tier) ──► withhold? / advisory?
                 ├─ pass   ► emit draft (optionally an assist-polished variant), stamp provenance
                 └─ fail   ► audit one counts-only block ► WITHHOLD draft entirely
                             ► (optional G4 assist re-draft, re-checked by the SAME gate)
                             ► else emit the deterministic fallback (withheld-notice + verified tables)
```

---

## 3. The grounding corpus — what a draft is allowed to ground against

`GroundingCorpus.from_answers(...)` (`ne.govern.grounding`) builds the *entire* universe
of facts a draft may reference, **from this turn's fetched `VerifiedAnswer`s only** (design
§2.2). If zero fetches happened, the corpus is empty and every content token refuses —
in grounded mode, *not fetching means not speaking*. Only mappings that carry the exact
`VerifiedAnswer` shape contribute facts; a `BLOCKED: …` tool result, a malformed mapping,
or a row whose length disagrees with the columns contributes **zero** facts (fail-closed,
`_parse_verified_answer`).

From each answer the corpus extracts five kinds of grounded material:

| Corpus member | What it holds | Purpose |
|---|---|---|
| `phrases` | per-row multi-token cell phrases | a stated multi-word value must appear as a real row phrase |
| `tokens` | the normalized token vocabulary of all cells | single-token prose grounding |
| `iris` | every `urn:ne:entity:…` cell + citation | an entity IRI must be a real fetched IRI |
| `numbers` | canonical numeral → the `NumberFact`(s) that produced it | numeral grounding + identity anchoring |
| `citation_count` | how many citations the answer carries | bounds `[n]` citation markers |

### 3.1 Identity anchoring (F3) — the heart of "mapping to cited data"

A number by itself ("3") is nearly always groundable — some row has a 3 somewhere. That is
not good enough: the dangerous hallucination is **"Initech has 3 open orders"** when
*Acme* has 3 and *Initech* has none. So a numeral does not ground merely by existing in the
corpus; it grounds only when the **sentence that states it also names the identity of the
row that produced it**.

Mechanically, for each kept column the corpus computes, per row, the *complete token set of
a single non-numeric, non-IRI sibling cell* — the row's **identity anchor** (e.g. the
account `name`). A degenerate filter drops any anchor that is a subset of another row's
anchor in the same column (ambiguous identity ⇒ unusable). A `NumberFact` then grounds only
if the draft sentence's grounded prose tokens include that row's anchor. This is the F3
rule: *a cell numeral grounds when the sentence names its sibling row's identity.* It is
what turns "the model said a number" into "the model correctly attributed a number to the
entity the data attributes it to."

### 3.2 Envelope facts — honest truncation is speakable (NE-CTXB-4)

A verified read can be truncated (row cap, token budget). The envelope fields
`returned_rows` / `total_rows` / `row_limit` mint a `NumberFact(kind="envelope")`
**always — including when the answer is truncated**, so an honest *"showing 3 of 5,000,000
accounts"* is speakable while the invented *"there are exactly 5,000,000 accounts"* (if the
true total is unknown) is not. Each envelope field is parsed **fail-closed per field**:
`returned_rows` is minted from `len(rows)` but **muted** if a present value contradicts
`len(rows)`; `total_rows` mints only from a well-typed in-range int and is muted if it is
smaller than the verified returned count (a contradictory "returned 3 of 1"). A bad
envelope field mutes only that one numeral — it never rejects the whole answer.

### 3.3 Red-team hardening baked into extraction

- **F1 (arg-echo poisoning).** `from_answers` drops any cell whose column name equals one of
  the answer's own `args` keys — a caller-echoed column is not graph data and must not
  become groundable.
- **F2 (percent / currency laundering).** `canonical_numeral` treats a trailing `%` or a
  currency symbol as *meaning-bearing*: `"50%"` canonicalizes to the distinct token `"50%"`,
  never plain `"50"`. Since no percentage/currency recompute exists, such a value grounds
  only if that exact `50%` is itself a corpus cell — otherwise it always refuses. A bare
  `%`/currency symbol detached by whitespace (`"50 %"`) binds to its adjacent numeral.

---

## 4. Token classification — how `check_grounding` reads a draft

`check_grounding(draft, corpus, prompt, *, profile=PROFILE_NG1_FROZEN)` splits the draft
into sentences, tokenizes each, and classifies **every** event. It counts findings by
class; the actual text never leaves the function (C12). The finding classes:

| Class | Fires when | Nature |
|---|---|---|
| `numeral-ungrounded` | a numeral has no grounded `NumberFact` (with F3 anchoring) | **fact** |
| `numeral-unparseable` | a numeral-shaped token can't canonicalize (e.g. a spelled compound) | **fact** |
| `iri-ungrounded` | a `urn:ne:entity:…` not in the corpus IRIs | **fact** |
| `citation-out-of-range` | a `[n]` marker outside `1..citation_count` | **fact** |
| `phrase-recombination` | grounded fragments recombined into a *new* entity identity | **fact** |
| `relation-ungrounded` | NE-GROUND-REL (ng-2): a row-bound prose token whose asserted relation no fetched row supports — no co-row partner, named row, same-column enumeration partner, or single-fact aggregate cover in its sentence | **fact** |
| `vocab-ungrounded-factual` | an ungrounded non-lexicon token **inside a fact-bearing sentence** | **fact-adjacent** |
| `vocab-ungrounded` | an ungrounded token in a **no-fact** sentence | non-factual vocab |
| `vocab-extended` | relational/quantified phrasing (ng-2 extended lexicon) | non-factual vocab |

Each prose token grounds through one of three routes: it is a **corpus token** (real cell
vocabulary), a **prompt token** (the user's own words — vocabulary-exempt, but **never**
numeral-exempt), or a **connective-allowlist** token (see §6). A token that grounds through
none of these is `vocab-ungrounded` — escalated to `vocab-ungrounded-factual` if its
sentence also carries a numeral, IRI, citation, or number-word, because that is exactly
where a fabricated modifier does damage.

### 4.1 Phrase recombination — the anti-Frankenstein rule

Grounding token-by-token is not enough: a model could stitch *"Thomas More"* (row A) and
*"More Industries"* (row B) into *"Thomas More Industries"* — an entity that exists nowhere.
The gate tracks **runs** of consecutive corpus tokens and, on flush, checks that the run
corresponds to a real corpus phrase; a run that recombines fragments across rows is
`phrase-recombination` (a fact class — it fabricates identity). The **corpus-precedence run
rule** (ng-2) ensures allowlist words cannot silently break a run to launder a
recombination past the check.

---

## 5. Numerals in depth — the most dangerous surface

Numbers are where confident hallucination is most costly, so the numeral machinery is the
most elaborate part of the gate.

- **Raw cell numerals** ground via `NumberFact(kind="cell")` + F3 identity anchoring (§3.1).
- **Ordinals** ("the 3rd account") ground the same way.
- **Envelope counts** ("showing 3 of 28") ground via `NumberFact(kind="envelope")` (§3.2).
- **Derived aggregates are NOT grounded.** If the model computes a sum/average/subtraction
  ("…totaling 45 orders", "…23 more accounts"), that numeral is a *derived* value the gate
  cannot verify without doing arbitrary arithmetic — which it deliberately does not do (a
  model could compute a wrong sum). Such a numeral is `numeral-ungrounded`. The correct
  pattern is a **server-side verified COUNT aggregate** (NE-CTXB-7): the platform computes
  the total under governance and returns it *as a cell*, so the model reads it rather than
  computing it.
- **Spelled-out numbers** ("twenty accounts") are mapped through the **same** `NumberFact`/F3
  machinery via a closed 56-word number-word lexicon: `"one"` derives to `"1"` and grounds
  only if a real `1` cell with the right identity exists. Spelled numbers are **never**
  prompt-exempt or allowlistable — otherwise a prompt-echoed "twenty" would launder a false
  count. **Magnitude words** ("dozen", "million", "couple") always refuse (no exact value).
- **Compound spelled numbers** ("twenty one" = 21) are canonicalized as **one** numeral;
  an adjacent number-word run refuses as a single `numeral-unparseable` at the run head, so
  a fabricated 21 cannot slip through by grounding "20" and "1" independently. *(This was a
  real fact-fabrication bypass caught and fixed by the adversarial build pipeline — see
  AS-BUILT NE-HGATE-1.)*

---

## 6. Connective words — the allowlist, and why it exists

### 6.1 The problem

A grounding gate that only accepts corpus tokens + prompt tokens rejects **all natural
English**. A real model narrating "You are cleared to see the following accounts and their
respective open orders…" uses connective words — *following, respective, additionally,
making, more* — that are neither facts nor in the data. The strict gate flagged these as
`vocab-ungrounded` and **withheld a completely correct answer** over five prose words. That
is unusable in production: no self-hosted model narrates in telegraphese.

### 6.2 The solution: a curated, red-teamed connective allowlist

The gate carries a **positive allowlist** of common non-factual English function words
(conjunctions, prepositions, determiners, quantifiers, auxiliary/gerund verbs) that may
appear in a grounded answer without being facts. The allowlist is a **data file** whose
bytes are hashed into `GATE_VERSION` (§7), so any change to it is versioned and cannot ship
silently.

| Resource | Size | Role |
|---|---|---|
| `grounding_allowlist.txt` (**ng-1, frozen**) | 135 | the original list; **never edited** — its bytes are the frozen `ng-1/<hash>` input |
| `grounding_allowlist_ng2.txt` (**ng-2 core**) | 320 | expanded connectives, incl. the five live-failure words; a superset of ng-1 core |
| `grounding_allowlist_extended.txt` (**STANDARD-only**) | 87 | relational/quantified phrasing (`vocab-extended`) |
| `grounding_number_words.txt` | 56 | the closed number-word lexicon (derives, never allowlists) |

### 6.3 Why an allowlist and not an NLI/semantic model

A learned entailment ("does the data support this sentence?") model would be *another*
probabilistic component — the exact thing we are trying to eliminate. It would be
non-deterministic, unauditable ("why did it pass?"), and itself hallucination-prone. The
allowlist is deterministic, inspectable, and versioned. Its **safety invariant** is that
it may only ever exempt **non-identity, non-numeric vocabulary**: a set of hygiene tests
(design §5.1) assert that (1) the ng-2 core is a superset of ng-1 core; (2) no allowlist
word collides with the excluded-comparatives set; (3) core / extended / number-words are
pairwise disjoint; (4) every token normalizes at import (fail-loud); (5) a red-team fixture
of identity-shaped words (`initech`, `globex`, `acme`, `corp`, `urn`, `ne`) is in **none**
of the lists. So even a careless future addition cannot make a *fact* speakable — it can
only ever loosen prose.

---

## 7. Configurable strictness — one gate, per-deployment posture

Different industries sit at different points on the assurance ↔ usability curve, so
strictness is a **per-deployment knob** — never a per-request one (a caller-selectable tier
would be a self-service assurance downgrade). The design is deliberately layered so that the
knob can **only ever move non-factual vocabulary**.

### 7.1 The load-bearing law

`ne.govern.grounding_policy` maps a **tier-agnostic** `GroundingVerdict` to a
withhold/advisory decision via `apply_strictness`. The `ENFORCEMENT` matrix encodes the law
in one place:

```python
_FACT_CLASSES = _ALL_CLASSES - {"vocab-ungrounded", "vocab-extended"}
ENFORCEMENT = {
    StrictnessTier.STRICT:   _ALL_CLASSES,    # everything withholds
    StrictnessTier.STANDARD: _FACT_CLASSES,   # only the fact classes withhold
}
```

The **five fact classes** + `vocab-ungrounded-factual` withhold **unconditionally at every
tier**. Only `vocab-ungrounded` (a stray word in a *no-fact* sentence) and `vocab-extended`
(relational phrasing) move — enforced at STRICT, **advisory** at STANDARD. A guardian test
iterates **every** `StrictnessTier` member and fails CI if a future tier ever drops a fact
class from enforcement. **This is the guarantee a regulated buyer needs: no configuration,
present or future, can make a fabricated fact speakable.**

### 7.2 The two tiers

- **STRICT** (default): every finding withholds. Byte-identical to the pre-feature gate.
  This is the posture for the highest-assurance deployments — they can even pin the frozen
  ng-1 rulebook (§7.3) to reproduce the exact historical behavior.
- **STANDARD** (opt-in): facts still withhold unconditionally; benign connective/relational
  vocabulary is *advisory* (recorded, never withholding). This is the usable default that
  lets a real model's natural prose pass while every number, name, IRI, and citation is
  still verified.

**Advisory means recorded, never dropped.** An advisory finding is carried as a
class+count (C12-shaped, no text) in the audit so the flywheel/stewardship path sees it as a
curation signal — it simply does not block the answer.

### 7.3 Profiles + versioning (`GATE_VERSION`)

Strictness (*posture*) is orthogonal to the **rulebook** (*which pinned set of rules +
allowlist bytes*). Two frozen `GateProfile`s exist:

- `PROFILE_NG1_FROZEN` → version `ng-1/<sha256(allowlist bytes + recompute-list id)[:12]>`,
  the historical behavior, byte-identical. It is the **default** profile of
  `check_grounding`, so every pre-existing caller and test is unchanged.
- `PROFILE_NG2` → version `ng-2/<hash of all rule + resource bytes>`. Any rule or resource
  edit moves the `ng-2/<hash>`.

The per-turn tier + rulebook are **stamped onto `GroundedProvenance`** (`gate =
profile.version`, `strictness = tier`), so every governed answer records exactly which
rulebook and posture judged it — reproducible and auditable. The invalid combination
"frozen ng-1 rulebook + STANDARD posture" (ng-1 cannot classify fact-sentences) is a
fail-loud boot `ValueError`.

---

## 8. The G4 assist — rescuing a good answer without weakening the gate

A capable model sometimes produces a *nearly* grounded draft — one derived total, one stray
connective. Withholding the whole answer is safe but harsh. The **G4 assist** is a bounded,
single-pass rescue that **cannot weaken the gate**, because its output is re-checked by the
*same* `check_grounding` before it may be emitted.

- On a **PASS** with an assist configured: the assist is called once with the question, the
  passing draft, and the rendered tables; its candidate is re-checked; if the re-check passes
  it is emitted (`assist_used=True`), else the original passing draft is emitted
  (`assist_used=False`). The assist can only improve, never degrade.
- On a **FAIL**: exactly one audited `guardrail.block` is emitted **first** (so an
  un-auditable failure still kills the run), the draft is withheld entirely, and the
  deterministic fallback becomes the baseline. If an assist is configured it is called once
  with the question, the tables, and the **reason classes only** — the withheld draft text
  is **never** passed to it (structural C12). Its candidate is re-checked; on a passing
  re-check it is emitted (`fallback=True, assist_used=True`), else the deterministic table is
  emitted.

Every assist invocation emits one **counts-only** `grounding.assist` audit event
(`{phase, adopted, error_class?}` — no text). The `(fallback, assist_used)` pair is stamped
on provenance.

### 8.1 The "dead assist" incident — why observability is not optional

For its entire production life the assist was **silently dead**: no deployment routed the
`grounding-assist` gateway task, so every call raised `UnknownTaskError`, which was swallowed
into `assist_used=false` — on **every live turn**. CI never caught it because every test
gateway ignored task routing. The fix was **belt and suspenders**: (1) a **boot fail-loud
check** — a deployment that enables the grounded assist but lacks a `grounding-assist` route
refuses to start; (2) a **task-routing-faithful test double** (`TaskRoutingGateway`) that
raises `UnknownTaskError` for an unrouted task, plus a regression test that makes the exact
production failure **surface in CI**. This is the general lesson the subsystem now encodes:
*a swallowed error on a critical stage is a silent downgrade, and silent downgrades must be
made loud at both boot and runtime.*

---

## 9. The deterministic fallback — refusing usefully

When the gate withholds, it does not return an empty error. It emits
`policy.render_fallback(fetched)`: a **withheld-notice** ("the drafted narration contained
content I could not verify against this turn's fetched data, so it was withheld") plus the
**verified tables themselves** (the real rows + citations). The user still gets the
source-backed data — they simply do not get the model's unverifiable prose over it. Only a
synthetic fallback exchange, stamped with the unforgeable `ne-grounding-gate:fallback`
marker, is persisted to history — so no fragment of the failed draft (text or tool-return)
can launder into a later turn's context.

---

## 10. Red-team testing — the acceptance bar is adversarial

The gate's acceptance suite is a **red-team matrix** (`test_grounding_strictness.py`,
written before the implementation) that, for **every (tier × legal-profile) combination**,
asserts **WITHHELD** on:

- a fabricated name + a *true* envelope numeral ("Initech has 3 open orders" when 3 is the
  real row count) — the A-P0-1 case that motivated fact-sentence scoping;
- a fabricated name + a fabricated numeral;
- a wrong count (the sibling row's cell value);
- a pooled / foreign numeral (cross-principal leakage under C7 multi-graph ACL union);
- an invented IRI (`urn:ne:entity:fake-1`);
- an out-of-range citation (`[9]`);
- spelled counts ("twenty accounts"; "one open order" when the cell is 3);
- a prompt-echoed spelled count (the prompt itself contains "twenty");
- name-fragment recombination, including via a newly-added allowlist word.

…and asserts **PASSES at STANDARD** on the **real captured GPU-model narration** (the exact
reproduction of the live over-strictness failure becomes a permanent regression fixture) and
on number-word derivation parity ("one" ≡ "1"). The register's laundering probes run against
the **final** resource files, so a bad future allowlist addition breaks CI, not production.

Beat 8 of the demo arc — *refuse-to-hallucinate* — is pinned green at **every** tier three
ways: the fabricated draft always carries an ungrounded numeral (a fact class, tier-proof),
the scripted assist "junk" candidate always carries an ungroundable numeral (so it is
withheld even at STANDARD), and the arc's structural assertions never reference tier.

---

## 11. Privacy, integrity, and audit constraints

- **C12 — counts-only findings.** A `GroundingFinding` carries only a reason class and a
  count. The ungrounded draft text never leaves the gate module. The control cannot become
  a data-exfiltration channel, and the audit is safe to retain.
- **§0-law-5 — never fake.** The gate never *edits* a draft to make it pass (that would be
  the gate authoring the answer). It either emits the model's own gate-certified draft, an
  assist candidate that independently re-passed the same gate, or the deterministic
  source-backed fallback. There is no "mute the bad word and ship the rest" path in v1
  (rejected: it needs token positions, breaking C12, and produces text no one authored).
- **C10 — hash-chained audit.** Every block and every assist event is a counts-only,
  fail-closed record in the tamper-evident audit chain (`AuditEmissionError` propagates and
  kills the run; it is never swallowed).
- **Single emit point.** `run_grounding_gate` has exactly one return; every emitted value
  either already passed the gate or was re-checked by it. This is what makes the provenance
  trustworthy.

---

## 12. What the gate deliberately does NOT do

- **No LLM judge / no embeddings / no thresholds.** Determinism and auditability are the
  product; a probabilistic check would undermine both.
- **No arithmetic verification.** Derived aggregates are refused, not recomputed. The
  server-side verified COUNT/aggregate templates are the sanctioned way to surface totals.
- **No per-request strictness.** Posture is deployment config only.
- **No semantic paraphrase tolerance.** A restatement that drops a citation or a numeral is
  treated as a new claim. This is intentional: paraphrase is where confident drift hides.

---

## 13. Provenance & versioning — proving it after the fact

Every governed answer stamps a `GroundedProvenance` recording: the `fetches` it grounded
against (template id, version, config hash, row count, truncated flag — never row text), the
`fallback` and `assist_used` flags, the `gate` rulebook version (`ng-1/…` or `ng-2/…`), and
the `strictness` tier. Combined with the C10 audit chain, this answers the regulator's
question — *"prove this figure was verified"* — with a reproducible, tamper-evident record:
the exact rulebook, the exact posture, the exact sources, and the exact refusal-or-pass
decision, for every turn.

---

## 14. File map (for the engineer)

| Concern | Location |
|---|---|
| Corpus extraction, token classification, finding classes, `GATE_VERSION`, profiles | `packages/ne-govern/src/ne/govern/grounding.py` |
| Strictness tiers, `ENFORCEMENT` matrix, `apply_strictness` | `packages/ne-govern/src/ne/govern/grounding_policy.py` |
| Allowlist + number-word resources | `packages/ne-govern/src/ne/govern/grounding_allowlist*.txt`, `grounding_number_words.txt` |
| Runtime enforcement, G4 assist, audit events, single-emit invariant | `packages/ne-runtime/src/ne/runtime/grounding.py` |
| Settings knobs, composition wiring, boot fail-loud belt | `packages/ne-brain-ontology/.../config.py`, `packages/ne-api/src/ne/api/platform.py` |
| `GroundedProvenance` contract | `packages/ne-contracts/src/ne/contracts/runtime.py` |
| Red-team matrix + hygiene + tier×profile parametrization | `packages/ne-govern/tests/test_grounding_strictness.py`, `packages/ne-api/tests/test_grounding_wiring.py` |
| Demo wiring (route, STANDARD posture, arc beats 7/8) | `tools/ne-cli/src/ne/cli/demo/live.py`, `.../demo/arc.py` |

---

## 15. Development history — what we first tried, the bugs we caught, and how we fixed it

The current gate is the second generation. Its history is worth recording because it is a
case study in the thing the gate itself is about: *a confident-looking output that is
subtly wrong, caught by an adversarial process rather than trusted.*

### 15.1 The first gate (pre-NE-HGATE) and how the flaw surfaced

The original gate was strict-only: every token had to ground as a corpus cell, a prompt
token, or one of **135** curated connective words (`grounding_allowlist.txt`). That gate
shipped and passed all of its fixture tests, because its fixtures used a **carefully
telegraphed** reference draft (`"{name}: {n} open orders."` — no prose, no summaries).

The flaw only appeared under a **live** model. While validating the context-budget feature
(NE-CTXB-8) on a real GPU-served 32B model, the grounded-analyst demo beat **failed** — and
the failure looked, at first, like the model hallucinating. It was not. The 32B produced a
*correct* answer in *natural English*:

> "You are cleared to see the following accounts and their respective open orders:
> - Bluepeak Dynamics LLC: 1 open order … Additionally, there are 25 more accounts, each with
> 1 open order, making a total of 28 accounts."

**Diagnosis by running the real gate locally on that exact narration** showed the problem was
not the facts — every numeral, name, and IRI grounded correctly — it was **five ordinary
connective words** (`additionally`, `following`, `making`, `more`, `respective`) that were
simply absent from the 135-word list. The gate was withholding a *correct* answer over
*prose*. No production model narrates in telegraphese, so the gate as built was unusable
for the grounded-agent path.

Investigating the same run surfaced a **second, worse** defect: the grounded-analyst turn
reported `assist_used: false` **every time**. The G4 assist — the mechanism meant to rescue a
nearly-grounded draft — appeared to be doing nothing.

### 15.2 The three findings that defined the fix

1. **The gate is too strict on vocabulary, not facts.** Confirmed by trace: the real model's
   answer failed only on non-factual connective words; every fact was verified. → *Expand the
   allowlist, red-teamed so no fabricated fact can slip through.*
2. **One posture does not fit all industries.** A regulated buyer may *want* the strictest
   possible gate; a general enterprise wants a usable one. → *Make strictness a
   per-deployment knob, with the load-bearing invariant that facts are always verified and
   only vocabulary strictness moves.*
3. **The assist was dead in production — a silent downgrade.** Root cause was not "the assist
   hit the same wall": the `grounding-assist` gateway **task was unrouted in every
   deployment**, so every assist call raised `UnknownTaskError`, which `_call_assist` swallowed
   into `assist_used=false`. It had **never run once** since it merged. CI never caught it
   because every test gateway ignored task routing. → *Route it, make it observable, add a
   boot fail-loud check, and fix the test blindness itself.*

### 15.3 The rebuild — five NE-HGATE units, and the bugs the pipeline caught in our own fix

The fix was designed (a Fable-5 + 6-lens adversarial design)
and built as five dependency-ordered units through the routed pipeline (each: plan → build →
Argus review → Codex cross-review → synthesis → green CI). Notably, the adversarial pipeline
caught **real defects in the remediation itself** — exactly the kind of subtle,
confident-looking error the gate exists to stop:

| Unit (PR) | What it delivered | Bug caught **in the fix** and how it was resolved |
|---|---|---|
| **NE-HGATE-0** (#282) | The `strictness` tier stamp on `GroundedProvenance` (contracts; additive, default `"strict"` = byte-identical) | — (clean) |
| **NE-HGATE-1** (#283) | The `ng-2` rulebook (320-word core + 87 extended + 56 number-words), the tier policy + `ENFORCEMENT` matrix, and the red-team matrix | **A real fact-fabrication bypass.** Codex found that compound spelled numbers ("twenty one" = 21) were validated **component-wise** — "20" and "1" grounded independently, so a fabricated 21 could pass if both halves happened to be real cells. Fixed: an adjacent number-word run refuses as a single `numeral-unparseable` at the run head; a red-first test pins it. Also froze the profile's number-word map (`MappingProxyType`) so it can't be mutated post-import. |
| **NE-HGATE-2** (#284) | Runtime tiered enforcement, the counts-only `grounding.assist` audit event, and the assist-rescue proof | **A lost audit signal.** Codex found that on the STANDARD advisory-only path (assist=None), advisory findings were **dropped** — the "advisory is recorded, never withholds" guarantee was silently violated. Fixed: a fail-closed counts-only `grounding.advisory` event, byte-parity preserved (STRICT default emits none). |
| **NE-HGATE-3** (#285) | Settings knobs + combo validator, the composition wiring, the boot fail-loud belt, assist-junk hardening, and the **`TaskRoutingGateway` test-blindness fix** | **The meta-bug: the test infrastructure that hid the dead assist.** The root reason CI never caught the unrouted assist was that every test gateway ignored `request.task`. This unit fixes the *stub*, not just the symptom — a task-routing-faithful double that raises `UnknownTaskError`, plus a negative test that makes the exact production failure surface in CI. Codex separately flagged a **coupling**: the new boot check would crash `ne demo up` until HGATE-4's route landed — resolved by sequencing the two merges and documenting it (no CI job runs `ne demo up`, so CI was unaffected). |
| **NE-HGATE-4** (#287) | The `grounding-assist` route (closes the coupling + makes the assist route for real), the STANDARD demo posture, and tier×profile fixture tests | — (clean; Argus APPROVE, Codex 0) |

A **repo-wide security issue** also surfaced mid-chain and was fixed properly rather than
waved through: newly-disclosed HIGH `gitpython` CVEs (transitive via `dlt`) were failing
`vuln-scan` on every PR; the fix bumped `gitpython` to 3.1.54 (#286) instead of overriding
the security gate.

### 15.4 How we proved it

- **Fixture / unit:** 1339 tests green across the units; the red-team matrix asserts every
  fabrication withholds at every tier; **the real captured GPU-model narration from §15.1 is
  now a permanent regression fixture** proven to *pass* at ng-2/STANDARD while every
  fabrication still fails.
- **Live integration** (demo host, CPU rig, no GPU): `ne demo up` boots `ne-api`
  **healthy** at STANDARD with the `grounding-assist` route wired and **zero
  `UnknownTaskError`** in the logs — i.e. the coupling fix works and the assist is alive.
- **Byte-parity:** a default-constructed policy reproduces the pre-feature gate exactly
  (`PROFILE_NG1_FROZEN.version == GATE_VERSION`), so STRICT/regulated deployments are
  unaffected.

### 15.5 The lesson

The over-strictness was found only by running the **real gate on a real model's output** —
not by any unit test, because the unit tests used an idealized draft. The dead assist was
found only by asking *why* a field said `false`. And the fabrication bypass in the fix was
found only because an adversarial reviewer tried to break it. The durable takeaway, now
encoded in the subsystem: **a control is only as trustworthy as the adversary that has tried
to defeat it — and a "green" that was never tested against reality is not yet a proof.**

---

*Built as NE-HGATE-0…4 (grounding-strictness) atop NE-CTXB-4/5/7 (the verified-answer
envelope). Every rule in this document traces to a test, not an argument — see the red-team matrix (§10).*
