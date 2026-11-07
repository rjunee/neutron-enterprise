<!-- Copyright (c) 2026 Ryan Junee. SPDX-License-Identifier: Apache-2.0 -->
# Vacuous Guards — the canonical, ratcheted catalogue (NE-VG)

**Status: CANONICAL and append-only.** This file is the single normative source for the
taxonomy, the ratchet rule, and the fixed-form gallery.

**Supersession NOT yet achieved — stated plainly rather than claimed, per this file's own
thesis** (it sits outside what this unit can reach: it is repo-external):
- the `vacuous-guard-assertion-shapes` **auto-memory** entry is loaded into every session from
  outside this repo (`~/.claude/...`) and this unit cannot edit it. **Follow-up owed to the
  orchestrator/owner:** retire or repoint that entry to this file once it merges, or the
  taxonomy keeps being injected verbatim from a second, now-stale source every session.

This file is the
**living catalogue**: taxonomy, ratchet rule, fixed-form gallery, and — added by NE-VG-0 — the
mechanical gate's stated does/does-not-catch boundary. Since NE-VG-0, SPEC §6.1 is a mechanical
gate in the unit workflow, not prose enforced by convention alone.

---

## 1. Definition

A guard test G for property P is **vacuous** when the most natural change that violates P leaves
G green. It is worse than no test: it occupies the slot where a real check would go, it certifies
the property in review, and its failure mode is silent — the only event that could expose it is
the forbidden change itself (design §1.1). Formally, the defect is a **quantifier gap**: the claim
quantifies over more than the assertion checks (design §2) — *exactly* claimed, *contains* checked
(V1/V4); *for all forms* claimed, *some forms* enumerated (V3/V9); *for this reason* claimed, *any
failure* accepted (V5/V7); *in production composition* claimed, *a test-local composition* checked
(V6); *for all inputs* claimed, *a convenient subset* sampled (V8); *non-trivially true* claimed,
*true by construction* delivered (V2).

---

## 2. The motivating case — a correction, not an embarrassment

**The design's own headline V6 finding is DISPROVEN, and that failure is the sharpest argument
for this gate that exists in this repo's history.**

The design (§3-V6) reported "FOUR unpinned bindings at the ne-api composition root" — the
`audit_sink=sink` / `sink=sink` kwargs at `packages/ne-api/src/ne/api/platform.py:1003, 1078,
1283, 1304` — as a **live** instance of the #339/#343 defect class: delete the kwarg, the argument
went, tests stay green, the audit trail silently vanishes in production.

**Deleting three of the four kwargs turns the owning tests RED, not green; the fourth is closed by
typing, not by an observed test failure — a qualification this section owes its own argument.**
They were closed by **NE-164** (PR #194, `da79d40`/`1c07a3d`, merged 2026-07-16) and its follow-up
**NE-164b** (PR #203, `7950b02`/`557a0a5`) — both landed on `origin/main` **before** the design's
own stated grounding commit `ba35714`.

The pinning tests are the NE-164/164b **emission tests**, one per site — mutation-verified in this
worktree by deleting each kwarg individually and reverting:
- `platform.py:1003` (`HybridRetriever(..., audit_sink=sink)`) → deleting the kwarg turns
  `packages/ne-api/tests/test_brain_api.py::test_retrieve_route_emits_read_audit_event` RED.
- `platform.py:1078` (`EntityGraph(..., audit_sink=sink)`) → deleting the kwarg turns
  `packages/ne-api/tests/test_graph_api.py::test_neighborhood_route_emits_graph_read_audit_event`
  RED.
- `platform.py:1304` (`build_verified_answer_bindings(..., audit_sink=sink)`) → deleting the kwarg
  turns `packages/ne-api/tests/test_ontology_api.py::test_invoke_emits_verified_read_audit_events`
  RED.
- `platform.py:1283` (`create_agent_credential_service(..., sink=sink)`) is **not** in that set:
  it has been a REQUIRED keyword-only parameter since its birth commit (`d8e296d`, PR #40), so
  deleting it is a `pyright reportCallIssue` / `TypeError` **before any test runs** — closed by
  typing, not by a test going RED. Three of four sites are behaviorally pinned; the fourth is
  pinned by the type system.

**Correction to an earlier draft of this section, caught in review — stated because this
document's own thesis forbids quietly fixing a citation:** `packages/ne-api/tests/
test_composition.py` does **not** pin any of these bindings. Grep confirms zero `audit_sink`
references in that file; its only sink-related assertions are `state.executor._sink is
state.sink` and `state.gate._sink is state.sink` — a different binding entirely (executor/gate
wiring, not the retriever/entity-graph/answer-bindings `audit_sink` kwargs above). A reader who
followed the original citation to check this correction would have found the cited test green
under the mutation and reasonably concluded the correction was itself unverified — the same
defect, a third time. The three emission tests above are the actual pin.

Independently corroborated, per-site, individually reverted, on worktree branch `ne/NE-VG-2`
commit `1316b3f` (not yet merged as of this unit) — same three sites RED under deletion, same
typing-closure disposition for `:1283`, plus a genuine residual that unit closes: store-level
emission tests cannot see audit-sink *identity*, so a substitution mutant (a same-engine sink
bypassing the `ToolTransitObservingSink` wrap) left the full 514-test `ne-api` suite green beside
the passing emission tests — closed there with two new `is`-identity pins in `test_composition.py`
(not present in this unit's tree; that addition belongs to NE-VG-2).

The design's audit read a state of the world that no longer existed and never re-verified it by
mutation before writing "found in the codebase now (verified by direct read this pass)".

Worse: **the orchestrator repeated the identical error one level up.** It reported the design's V6
finding to the owner as a live production defect *after its own verification run had died with an
unrelated `ModuleNotFoundError`* — the exact unsynced-venv trap this unit's own build brief names
as STEP 1 ("a fresh worktree venv is NOT synced... never report a RED from an unsynced tree").
A `ModuleNotFoundError` on `ne`/`sqlalchemy`/`httpx` is an environment defect, not a code signal;
reading it as one and forwarding the conclusion is a claim that outran its evidence, told to a
human as settled fact.

**The document diagnosing "claims that outrun evidence" committed exactly that error, and so did
the human-facing report that repeated it.** That is precisely what a deterministic evidence gate
exists to prevent: not skill, not carefulness, not a smarter model — a control that mechanically
distinguishes "we ran the mutation and it went RED" from "we asserted it would." This is why every
`mutations[]` entry the gate accepts REQUIRES a `control`: an invalid probe (a broken environment,
a stale read, the wrong test selected) is *indistinguishable from a working guard* without a
demonstrated should-pass case sitting next to it.

**Downstream invalidation, recorded here so it is not silently inherited:** the audit
design's §8 NE-VG-2 deliverable ("the four V6 composition-root sink
bindings closed") and NE-VG-4's §7 replay acceptance bar (live leg: "at today's HEAD, the
`drop-keyword-argument` operator on `platform.py:1003`... must report a SURVIVOR") are both
**invalidated as written** — `platform.py:1003` is already pinned (NE-164), so neither anchor
holds. Re-briefing NE-VG-2 and NE-VG-4 against the actual current state of `platform.py` is the
orchestrator's job, not this unit's; this section exists so whoever re-briefs them does not
rediscover the same false premise.

---

## 3. Taxonomy V1–V10 (condensed from design §3 — see there for full FP-rate derivations)

| Class | Name | Why vacuous | Detection verdict | Fixed-form citation |
|---|---|---|---|---|
| V1 | Containment on an extendable literal | `x in y` is monotone under extension of `y` — a literal whose legal alternatives *contain* it can't forbid the extension | LINT-ASSISTED (measured FP ≈ 90% of raw flags, near-zero steady state) | anchored regex + exact compare, `deploy/airgap/tests/test_static_harness.py:243-259` |
| V2 | Tautological comparison (cardinality/type) | assertion is true for every possible program state (`!=` between statically-unequal collections) | LINTABLE, hard-fail (FP ≈ 0) | patch-through + raises + does-NOT-raise control, `packages/ne-permissions-rebac/tests/test_presence_marker_exhaustive.py:306,179` |
| V3 | Enumerating some forms of a thing | sweep's domain under-approximates the forms the codebase uses/will use; a new form evades silently | NOT mechanically decidable; semi-mechanical inventory + convention (derive-don't-enumerate; planted-RED + declared residuals where enumeration is unavoidable) | five enumerated forms + 3 declared residuals, `packages/ne-brain-graph/tests/test_c7_graph_trimming_guardian.py:595-1093` |
| V4 | Field-presence without value discrimination | presence is invariant under hardcoding; a threading claim needs the value to co-vary with the input | LINT-ASSISTED + MUTATION (hardcode-to-default operator) | value-discriminating pair through the real route, `test_c7_graph_trimming_guardian.py:470` |
| V5 | A named branch subsumed by a later guard | two branches produce indistinguishable observables; the branch-named test passes on the general one | MUTATION (delete the branch; equivalent-mutant triage IS the detection) | branch-specific message + `match=`, `tools/ne-cli/tests/test_envfile.py:82-101` |
| V6 | A production binding whose deletion is invisible | safe-looking default (None/log-only/noop) lets the composition root silently degrade; test-local composition never notices | MUTATION (`drop-keyword-argument` operator over composition-root files) | identity pin (`is`, not presence), `packages/ne-identity/tests/test_zero_groups_signal.py:316`, `packages/ne-api/tests/test_identity_sink_wire_pin.py:39` — see §2 above for the class's live-vs-disproven state as of this unit |
| V7 | A guard the environment accidentally satisfies | `pytest.raises(BroadError)` can't distinguish "our guard fired" from "something else failed for its own reasons" | LINT-ASSISTED floor (raw FP ≈ 94%) + MUTATION truth | `pytest.raises(GraphStoreError, match="injection guard")` — the guard's own message, `packages/ne-brain-graph/tests/test_graph_store_conformance_server.py:110,128` |
| V8 | A fixture shaped to miss the failing case | claim quantifies over an input space the fixture deliberately or accidentally under-samples | NOT mechanical; differential mutation (revert the fix, must go RED) + measured-counterfactual convention | dodging fixture inverted to proving fixture with a measured control (160,491 B vs 4,827,877 B), `packages/ne-schema-bootstrap/tests/test_infer_fk_junction.py:251,285` |
| V9 | A container-shaped hole in a reflection/derivation guard | even a derived guard's traversal is an enumeration one level down (`list`-only ⇒ `dict[str,str]` exempt) | MUTATION (narrow traversal; container RED case) + negative-control convention | recursive generic-args traversal + negative controls, `packages/ne-schema-bootstrap/tests/test_retention_choke.py:39-179,150` |
| V10 | The guard that pins the vulnerability open | asserts a security scanner does **NOT** catch a synthetic bypass (`offenders == []` / `violations == []`); green *because* the check fails, RED the moment anyone hardens it — a documented residual promoted into a CI-enforced invariant that punishes the fix. Categorically worse than V1-V9: every earlier shape fails to *detect* a defect, V10 *defends* one. Carries the [[written-rules-disarm-review]] signature at full strength — the docstring says "PLANTED-RED probe", the code is planted-GREEN | NOT mechanically caught by any V1-V9 detector or by this unit's presence gate — indistinguishable from a normal empty-list guard without reasoning about assertion polarity vs. stated residual status; found by **Codex cross-review**, not by mutation or lint | **NOT YET FIXED as of this unit.** Found 2026-07-28 by Codex review of NE-VG-2 itself, confirmed by source read: `packages/ne-runtime/tests/test_c6_no_bypass_guardian.py` (`test_declared_residual_alias_constructed_handler_call_evades_the_scan`, `test_declared_residual_unlisted_provider_token_evades_the_scan`), unmerged branch `ne/NE-VG-2`. Recommended fixed form (NE-VACUOUS-GUARD epic row, not yet applied): `@pytest.mark.xfail(strict=False)` on the **positive** assertion (`offenders != []` / `violations != []`) — XFAILs today (gap stays visible every run), XPASSes once hardened (signals closure instead of blocking it). Tracked here per the ratchet rule (§5); closing it is follow-up work for whichever unit lands that branch, not NE-VG-0. |

**V6 annotation (load-bearing for this unit):** the class and its two historical instances
(**#339, #343** — the `app.py`/identity sink deletions Argus caught, at `0c44728`-era `main`) are
real and closed by the fixed forms above. The design's *"found now"* `platform.py` four-binding
list is **disproven** — see §2. Do not cite the design's §3-V6 "found in the codebase now" bullet
without reading §2 of this file first.

---

## 4. Fixed-form gallery — confirmed instances, append-only (rows 1-9 condensed from design §1.1)

| # | PR | Vacuous form | Fixed form (citation) |
|---|---|---|---|
| 1 | #337 | `assert "for attempt in 1 2 3 4 5" in run` — `1 2 3 4 5 6` contains it | anchored `; do` regex + exact parsed-list compare, `deploy/airgap/tests/test_static_harness.py:243-259` |
| 2 | #336 | `assert markers != KEYS - {key}` — 2-set vs 1-set, unequal on cardinality alone | patch-through-module + raises + a does-NOT-raise control, `packages/ne-permissions-rebac/tests/test_presence_marker_exhaustive.py:306` (+`:179`) |
| 3 | #335 | AST sweep matching only dict-entry/keyword forms; defeated by the house `d["k"] = v` idiom | five enumerated forms F1–F5, each with a planted-RED offender, plus three declared residual evasion classes, `packages/ne-brain-graph/tests/test_c7_graph_trimming_guardian.py:595-1093` (residuals `:819-855`, control `:979`) |
| 4 | #338 | `assurance` threading guarded by key-presence only — hardcoding `assurance="source"` left 760 tests green | value-discriminating pair through the real route, `test_c7_graph_trimming_guardian.py:470` |
| 5 | #341 | test named for the newline branch stayed green with the branch deleted (subsumed by `ord(ch) < 0x20`) | branch-specific message asserted, `tools/ne-cli/tests/test_envfile.py:82-101` |
| 6 | #339/#343 | deleting `audit_sink=platform.sink` left 687 tests green; event degraded to log-only | identity pin (`is`), `packages/ne-identity/tests/test_zero_groups_signal.py:316`, `packages/ne-api/tests/test_identity_sink_wire_pin.py:39` |
| 7 | #344 | live injection-guard test stayed green with the guard deleted — the AGE server's own parser raised the same exception type | `pytest.raises(GraphStoreError, match="injection guard")`, `packages/ne-brain-graph/tests/test_graph_store_conformance_server.py:110,128` |
| 8 | #348 | memory-bound test's fixture deliberately built without identifier columns — couldn't observe the unbounded growth the same commit introduced | dodging fixture inverted into proving fixture with a measured counterfactual (160,491 B bound vs 4,827,877 B unbounded), `packages/ne-schema-bootstrap/tests/test_infer_fk_junction.py:251,285` |
| 9 | #348 | `_admits_str_content` handled `list` only — a future `dict[str,str]` field silently exempt from the retention guard | recursive generic-args traversal + container RED case + negative controls, `packages/ne-schema-bootstrap/tests/test_retention_choke.py:39-179` (container `:150`) |
| 10 | NE-AUTOLINK-NEVER-QUEUED | decided platform behaviour ("`links_auto` counted, never queued, never written, in EVERY mode" — canonicalised in two design docs) enforced ONLY by the code comment above `job.py`'s `_handle_link` `return`; deleting that `return` left 829 tests byte-identical green | mode-parametrized exact-set pin + inclusive-boundary pin + below-band over-block control, mutation-verified (M1-M4), `packages/ne-brain-graph/tests/test_extraction_auto_link_band.py`; sibling comment-only decisions in the same wall (`_validate_wall`/`_handle_*`) are a DECLARED RESIDUAL — census (S1-S4) run and filed in this unit's PR body, not fixed |
| 11 | #385 | C12 for the workflow-run surface pinned by a field list only (`set(params) == {"record_id", "title", "body"}`) — V4 presence-without-structure: a new `StepView.detail` string carrying a note body or account name shipped green | sentinel sweep over the SERIALIZED response AND the run's audit rows, with an in-test presence control (the same sentinels MUST appear in the audited approver disclosure, so a misspelled sentinel or wrong-store sweep fails loud), mutation-verified (detail-leak mutant RED), `packages/ne-api/tests/test_workflows_api.py::test_run_response_and_audit_rows_carry_no_customer_content`. Detector decision: covered by V4's existing LINT-ASSISTED + MUTATION mapping (§3) |
| 12 | #385 | the approval-queue flood ceiling survived its own deletion — deleting BOTH `le=_MAX_SUBJECTS_CEILING` and the route's `min()` left all 11 tests green; the single control was an unasserted `Query` annotation (V6: a production binding whose deletion is invisible), and `run_workflow` itself accepted any `max_subjects`, so a future second caller inherited no ceiling at all | ceiling moved INTO `run_workflow` (`ne.api.workflows.MAX_SUBJECTS_CEILING` — every caller inherits it; the route's `le=` is only the 422 UX), pinned by a boundary 422/at-ceiling-200 pair AND by the count that matters — approvals actually parked (7 eligible subjects, `max_subjects=50`, exactly 5 rows in the queue) — both sites mutation-verified individually, `packages/ne-api/tests/test_workflows_api.py::test_route_rejects_over_ceiling_and_accepts_the_boundary`, `::test_engine_clamps_subjects_to_the_ceiling`. Detector decision: covered by V6's existing MUTATION (drop-keyword-argument) mapping (§3) |
| 13 | #385 (review round 2) | the C12 audit sweep's own comment claimed "every column of every row, serialized" while its SELECT enumerated 4 of the table's columns (V3: the claim quantifies over all columns, the assertion enumerates some), and the sweep's presence control covered 1 of 5 sentinels — seeded-naming drift on the account-name sentinels would have left their no-leak assertions silently vacuous while the single-sentinel control stayed green; the audit half of the sweep had never been proven able to catch a leak that reaches only the audit store | `SELECT *` (no column enumeration to go stale) plus a per-sentinel presence control on the surface each sentinel genuinely flows through — account names: the governed CRM lookup's recorded calls, asserted as the exact ordered list; ticket subjects: both audited approver disclosure bodies — and the audit half proven live by an audit-only leak mutant (raw disclosure payload planted into the gate's `approval.requested` detail, which reaches `ne_audit_log` but never the HTTP response: sweep RED naming the leaked sentinel), `packages/ne-api/tests/test_workflows_api.py::test_run_response_and_audit_rows_carry_no_customer_content`. Detector decision: covered by V3's existing mapping (§3) |
| 14 | #399 (fixed by NE-TRACE-ADOPTED-FIX, PR #407) | the trace grounding-row copy tests sampled `adopted: true` only (V8: the claim — the row states what the server said went out — quantifies over both verdicts; the fixture sampled one), so a fact line keyed on `phase` alone shipped green and rendered "wording improved" against a served `adopted: false` while the pane beside it showed "Rewrite used: No" — the console composing a verdict the server explicitly contradicted (console-C7). The same one-sided sampling hid two siblings in the same surface: the ToolSpan lift's bool-as-count (`isinstance(True, int)` is `True`; every seeded `result_count` was a genuine int, so a JSON `true` served as "1 row"), and the rolling-upgrade crash paths (every fixture served the full key set, so `.rows`/`.args.length` off an absent key had no offender) | copy keyed on the served (phase, adopted) pair with BOTH `adopted:false` phases pinned to exact copy, `adopted:true` controls, an absent-`adopted` states-nothing pin, and a render test asserting the row chip and the pane agree on one served record; a literal `True` seed for the lift (a `1` proves nothing — bool subclasses int); an old-wire fixture with keys ABSENT (not null) at unit and render level — decisive tests proven RED against the merged code first, each guard mutation-verified bidirectionally (M1–M7 ledger in PR #407), `packages/ne-console/src/trace/format.test.ts`, `packages/ne-console/src/trace/TracePage.test.tsx`, `packages/ne-api/tests/test_trace_legibility_spans.py`. Detector decision: covered by V8's existing mapping (§3 — differential mutation + measured-counterfactual convention) |
| 15 | #403 | the §6.1 REVIEW GATE itself, three ways. (a) Its no-evidence FAIL message printed a parseable `review-evidence` template carrying the live head SHA, `ran: true`, and zero counts — quoting the gate's own FAIL output into a PR comment made the gate PASS, and this repo's automation pastes CI output into PR threads (the guard's own diagnostic output satisfied the guard — V7's environment-satisfaction shape at the gate level). (b) Its CI entry point `main()` had zero coverage — the suite imported only `parse_evidence`/`verdict` — so a `return 1`→`return 0` mutant on the no-evidence path survived 12/12 tests: the exact vacuous suite shape the module's own docstring forbids. (c) Its `_SRC_SURFACE` regex `^(packages\|tools)/[^/]+/src/` repeated §6's named under-approximation in a second gate: flat `tools/supply-chain/` (the gate's own home) was invisible, so the gate self-exempted its own changes from the mutation-evidence clause | template made unparseable-as-evidence by construction: `commit:` never pre-filled (head SHA appears only outside the fence), `<...>` placeholder values and non-40-hex commits rejected at parse, pinned by a self-paste probe that GENERATES the live FAIL output through `main()` and feeds it back (`test_the_gates_own_fail_output_pasted_back_still_fails` — never a copied fixture, so it cannot rot with the message); `main()` driven through an injected fetch seam covering every exit code, newest-comment selection, candidate filtering, and both pagination boundaries (files + comments follow pages past 100 items); surface rule widened to `touches_src_surface` (code under packages, tools, apps, deploy and demo minus tests//docs/ subtrees, plus `.github/workflows/` and orchestration executables, residuals NAMED in its docstring). All ten mutants MR1-MR7 killed with green revert controls, including the reviewer's original `return 0` probe (now RED in 2 tests). Detector decision: V3 (surface under-approximation — a second occurrence of §6's named `_isSrcScopedGlob` hole; closed for THIS gate, the workflow gate's twin remains open and named in §6) + V7 (guard satisfied by its own output, mutation-verified) |
| 16 | #403 (round 2) | the same gate, two more ways. (a) `_all_pages`'s two "fails loud" claims — the `>_MAX_PAGES` truncation refusal and the non-array JSON guard — existed only in prose: replacing either `raise SystemExit` with `return items` survived the full 44-test suite, restoring exactly the silent fail-open the docstring says is closed (a GitHub error *object* read as zero files waives the mutation clause; a truncated file list classifies from a view known to be cut short) — [[written-rules-disarm-review]] at assertion level, the claim shifted review to "does the code match the docstring" and the docstring was unenforced. (b) One level up, the required-context protection the runbook instructed the owner to install was itself satisfiable by the thing it forbids: `review-gate` lived in multi-trigger `ci.yml` behind `if: github.event_name == 'pull_request'`, and GitHub records a conditionally-skipped job as a Success-shaped check run that SATISFIES a required status context (latest run per context wins) — so the owner-approved `workflow_dispatch` full sweep on a unit branch minted a green `review-gate` on a red PR's head SHA (V7's environment-satisfaction shape, with "the environment" being GitHub's check semantics) | (a) both raises pinned through `main()`'s real fetch seam with the reviewer's exact mutants re-run and RED — `test_a_non_array_api_response_dies_instead_of_gating_on_nothing` (inline should-pass control) and `test_the_page_ceiling_refuses_a_truncated_view_rather_than_gating_on_one` (exact 100-call ceiling pin; boundary control `test_an_exactly_full_page_is_followed_not_truncated`). (b) the job moved to single-trigger `review-gate.yml` (no `if:`; script exits 1 on absent `PR_NUMBER`, so a future extra trigger could only mint a FAILED check), pinned by `test_workflow_layout_only_a_pull_request_can_mint_the_context` — enumerates every workflow's trigger set and job ids/names for the context, with ci.yml-shaped controls proving the extraction is live; declared residual: a branch predating the layout still carries the old `ci.yml` job (rebase, don't reason it safe). Detector decision: (a) covered by claim-driven mutation probes — §6.1's re-run duty; every "fails loud"/"fail closed" docstring claim owes a RED-side test in the unit that writes it; (b) V7 → the layout test is the permanent detector |
| 17 | #402 (review) | `test_cdc_lane` asserted `last_checked_age_s == cursor_age_s` on the CDC lane over a bare seeded `cdc.ingest` success event — a guard PINNING a defect (V10's defends-the-bug polarity, outside the security-scanner setting): the sink emits that success summary for batches that accepted ZERO records (authenticated empty webhook → `apply(())` → `written=0`), so the pinned equality certified "Newest record: moments ago" on a lane that may hold no records at all, and hardening the derivation would have gone RED against the guard | newest-record fact derived from a batch that ACCEPTED a record: the `cdc.ingest` counts detail is a parsed producer/consumer contract (`ne.connect.cdc.ingest_detail` / `written_from_ingest_detail` — anchored `fullmatch`, unparseable ⇒ honest absence, never freshness) consumed by `ne.api.connect_status.latest_accepting_cdc_ingest`; rewritten `test_cdc_lane` drives a REAL empty signed webhook and proves the two freshness facts come from DIFFERENT events, `test_cdc_lane_heartbeats_only_serves_no_record_freshness` pins the never-accepted lane at `cursor_age_s=None` (both verified RED against the collapsed implementation before the fix), mutation-verified N1–N6 (PR #402 ledger). Detector decision: covered by V10's existing mapping (§3) — assertion-polarity-vs-intent reasoning in cross-model review, not mechanically caught by lint or mutation |
| 18 | #419 | two shapes in one console hook, found by cross-review of the PR that claimed to close them. (a) **V5, vacuous at runtime**: `displayOrder`'s `labelVar === null` branch is named for "no label declared", but a pre-`label_var` server omits the key, so the value is `undefined` and `columns.indexOf(undefined)` returns -1 — the same passthrough. Flipping the nullish check back to `=== null` (signature left widened) leaves **145/145** `src/knowledge` tests green in 13 files; only `tsc -b` discriminates (`TS2345` at `directory.ts:92`). No runtime discriminator for this shape exists, because both branches produce the identical observable. (b) **V8, the window that could not reach the failure**: the same PR's new guard for "a terminal frame's verdict is never restated" advanced `STALL_DEADLINE_MS + 1_000` (31 s) and stopped 89 s short of `TURN_DEADLINE_MS`, the second timer that fires on exactly the wire the test posits — so a stated `refused` still became a generic `timeout` at 120 s while the test stayed green, and the code carried a comment asserting the property held | (a) signature admits `undefined`, check is nullish, **and the wire type `VerifiedTemplateWire.label_var` made optional** so the mutation is caught at the REAL caller (`VerifiedAnswers.tsx:355`) rather than only in the test file — verified by narrowing the signature back: `tsc` now names `VerifiedAnswers.tsx` alongside `directory.test.ts`, where before this fix the caller compiled either way. Detector decision: **V5 → closed by TYPING, not by an observed test failure**, on the precedent §2 sets for `platform.py:1283`; the backstop is `tsc -b`, run as `npm run typecheck` in the required `frontend` CI context (`.github/workflows/ci.yml`), and this row is the standing declaration that no runtime test pins it. (b) the guard's window extended to and past `TURN_DEADLINE_MS`, with keep-alives driven across the whole span so the absolute deadline (which no keep-alive restarts) is the timer under test; the property itself re-derived — the terminal settles the TURN, the clocks keep watching the CONNECTION, and each late writer (stall abort, deadline abort, reader Stop, transport throw, post-terminal frame) is gated where it would write the turn's state, so the comment's claim is now total rather than partial. Both directions pinned in one test: the verdict must not move AND `busy` must still be released, so disarming the clocks to "fix" it goes RED on the composer hanging forever over a delivered answer. Mutation-verified M1-M4 with green reverts, `packages/ne-console/src/chat/useAgentStream.test.ts`. Detector decision: covered by V8's existing mapping (§3 — differential mutation + measured-counterfactual convention), with the convention sharpened by this instance: **a guard against a timer must run past that timer's window**, and where two timers can produce the same observable, the window is the longer one |
| 19 | #446 — **A RECURRENCE OF #14, ONE PR LATER, IN THE SAME FILE FAMILY** | #14's third bullet named "the rolling-upgrade crash paths (every fixture served the full key set, so `.rows`/`.args.length` off an absent key had no offender)" and closed it for `grounding`/`tool`. The identical hole reopened on the NEXT new field into the same trace surface: `TraceModelAggregateView.tasks`. Removing the `?.` from `row.tasks?.length` (`packages/ne-console/src/trace/TracePage.tsx:75-77`) left **all 1447 console tests green** — every fixture serves `tasks`, and the test named "renders an empty task list as absent" serves `tasks: []`, which is NOT the failing input. Measured counterfactual: against an old-wire payload with `tasks` ABSENT from the aggregates, the mutant throws `TypeError: Cannot read properties of undefined (reading 'length')` and the **entire Models table fails to render** — the aggregate view a reader came for, gone, on exactly the window where console is newer than server. The type was complicit: `types.ts` declared `tasks: string[]` REQUIRED while the code beside it defended against absent, so the wire type vouched for a shape the server does not always send and no compiler ever questioned the guard's necessity. **V8 (a fixture shaped to miss the failing case), and the recurrence itself is the finding**: closing an instance did not close the class, because the habit that produces it — declaring a newly-added wire field required — was never the thing that got fixed | render-level fixture with the key **genuinely absent** (`({ tasks: _absent, ...older }) => older`, not `[]`), asserting the table renders at all, that every row's cell is the em dash (never blank, never the string `undefined`), AND that the column's SECOND independent `row.tasks` dereference — `sortValue`, reached by clicking the header — survives it too; **plus the wire type made optional** (`tasks?: string[]`) on the precedent row 18(b) and `VerifiedTemplateWire.label_var` (`types.ts`) already set, so `tsc -b` refuses an unguarded `tasks.length` at every future call site rather than waiting for a fixture to happen to cover it. Mutation-verified individually: dropping `?.` from `cell` → RED (TypeError), 25 controls green; dropping it from `sortValue` → RED (TypeError on the header click), 25 controls green. The column's third reader, `searchText`, is **closed by typing, not by a runtime test** (row 18(b)'s precedent): no render can reach it — `DataTable` never *calls* `searchText`, it only declares the field (`DataTable.tsx:58`); the sole reader is `rowSearchText` (`tableSort.ts:77`), reached only from a page that calls `filterRows`, and `MODEL_COLUMNS` is module-local to `TracePage.tsx`, which imports neither — so its mutant survives every runtime test, and what kills it is `tsc -b` (`TS18048`) in the required `frontend` CI context. `packages/ne-console/src/trace/TracePage.routing.test.tsx::renders a pre-tasks wire, with the key ABSENT from the aggregates, without crashing`. Detector decision: covered by V8's existing mapping (§3). **The convention, stated once and honestly — "the type is the detector" is NOT a mechanical detector, and this row is the standing correction of an earlier draft of itself that claimed it was.** The rule: *a wire field the server may omit is declared **optional** (`?`) in `types.ts` at the moment it is added, and a fixture describing that older wire carries **no `as unknown as` cast**.* Two things it is not. (i) It is not `\| null`. `?` is a claim about PRESENCE and `\| null` a claim about VALUE, and only the first survives the house narrowing idiom: on `route_position: string \| null`, `call.route_position !== null && call.route_position.startsWith(…)` **compiles** — `undefined !== null` is true, tsc narrows to `string`, and the deref throws on the old wire; on `route_position?: string \| null` the same line is `TS18048` and the compiler names the site. (ii) It is not mechanically checkable. "The server may omit this field" is a **deployment-skew** fact, not a schema fact — pydantic serializes `route_position: str \| None = None` and `tasks: list[str] = Field(default_factory=list)` identically, always emitting both keys — so no OpenAPI diff, codegen step or lint rule can decide which field qualifies; `tsc` only fires *after* a human already made the right call, and a cast on the fixture erases even that. **Declared residual, and it is the whole class:** there is no mechanical detector for this shape. The backstop is cross-review asking, on every new wire field, "can a running server omit this key?" — nothing else. That this PR broke its own freshly-written rule two interfaces above the one it fixed (`route_position`/`fallback_from` shipped REQUIRED with doc-comments saying the server may omit them, while `tasks?` beside them was correct) is the evidence for the residual, not an exception to it; both are now optional and `ROUTED_OLD_WIRE`'s cast is dropped so `tsc -b` fails on the fixture the day either is re-declared required. A fixture-only fix (what #14 delivered) closes one call site and leaves the class open, which is precisely what happened here. **The rule was ALSO being violated two interfaces up, on #14's OWN fields, and that is fixed here rather than filed** (found in review round 3): `TraceToolView.template_id`/`template_version`/`args`/`result_count` — the verified-answer lift, which a server predating it OMITS rather than serving as null — shipped REQUIRED, so mutant **X-1** (dropping the `?.` from `tool.args?.length`, `SpanPane.tsx:178`) passed `tsc -b` **clean** and was caught only by the one render test that happens to serve the pre-lift fixture. All four are now optional: X-1 is `TS18048` at both dereferences on that line, at every present and future call site, with `tsc -b` clean on revert. The fixture half is ratcheted the way this row's rule asks — the tool payload is now an ANNOTATED `const OLD_WIRE_TOOL_SPAN: TraceToolView`, not a cast, verified bidirectionally: re-declaring `args` required is `TS2741` **on the fixture**, clean again on revert (`packages/ne-console/src/test/mocks/traceFixtures.ts`). **Residual, named because the cast is still there and must not read as the same defect:** `OLD_WIRE_TOOL`'s NODE wrapper keeps its `as unknown as TraceNodeView`, for a different cause — `TraceNodeView`'s twelve span slots (`subagent`…`session`) are declared required and `OLD_WIRE_BASE` is `as const` (so `children` is `readonly []`). Measured, not assumed: widening all twelve leaves the whole console at **exactly one** `tsc` error (that same fixture's `as const` `children`), so it is cheap — and it is NOT taken here, because it is a claim about the whole trace wire ("a server may omit any span kind"), and it would exempt every FUTURE span field from fixture type-checking, which is the ratchet running backwards. The right shape is per-span annotated payload consts (what this PR does for `tool`), applied to the other eleven by a unit that owns that interface |
| 21 | NE-GPU-MULTIPROVIDER (this PR) — **row 20 is reserved by the in-flight PR #440; numbered 21 to keep the append-only ratchet conflict-free while both are unmerged** | **V7, found by the suite's own mutation run, in the suite's own first draft.** The guard for `destroy`'s id/state disagreement asserted `returncode != 0` plus "both ids appear somewhere in stderr". Mutating the refusal from `die` to `log` — the exact change the guard names — left it **GREEN**: execution fell through to the provider API call, which failed on its own (no credentials in the test environment) and printed both ids on the way past. So the test could not distinguish *"the guard fired"* from *"the guard was deleted and the next thing along happened to break"* — and the environment satisfying it was not exotic, it was the default test environment. The `--force-mismatch` sibling was NOT affected (it already keyed on its own warning text), which is what made the asymmetry visible | key on the refusal's **own message** (`"refusing to destroy"`), keep the both-ids assertion as a secondary claim about message quality rather than the primary discriminator, and add a negative pin that nothing was reported destroyed. Mutation-verified M17 (mutant RED, control GREEN) after the fix; M16 (destroy silently falling back to the state file) verified alongside it as the sibling branch. Detector decision: covered by **V7**'s existing LINT-ASSISTED floor + MUTATION truth mapping (§3) — and the convention this instance sharpens: **when the code path under test continues past a deleted guard into an operation that can fail for its own reasons, exit status is never the discriminator; the guard's own message is.** The general shape is that a `die`-vs-`log` mutation is invisible to any assertion that only observes *that* the process failed |
| 22 | PRE-009A (dormant Run storage CORE) | **V7, twice in one file.** Two database-guard tests asserted bare `pytest.raises(sa.exc.IntegrityError)` with no `match=`: `test_state_machine_terminal_immutability_and_retry_new_attempt_identity` (illegal `submitted -> running` write) and `test_schema_is_idempotent_and_capability_snapshot_is_database_immutable` (capability-snapshot overwrite). On SQLite that one exception type covers trigger `RAISE(ABORT)`, unique violation, foreign-key violation and CHECK violation alike, so either test stays green if the named trigger is deleted and ANY other constraint on the same statement fires instead — the environment satisfies the guard, exactly V7's shape. The file's own schema carries several such constraints on `ne_agent_runs`, so this was not a theoretical alternative | `match=` pinned to each trigger's OWN message — `match="illegal Run state transition"` and `match="Run capability snapshot is immutable"`, the literal strings `_install_run_guards` raises — `packages/ne-runtime/tests/test_run_store.py`. Detector decision: covered by **V7**'s existing LINT-ASSISTED floor + MUTATION truth mapping (§3), with one dialect caveat this instance adds and the next unit must not re-learn: **the same guard raises a different SQLAlchemy type per backend.** `_install_run_guards` installs SQLite `RAISE(ABORT)` (surfacing as `IntegrityError`) and plpgsql `RAISE EXCEPTION` (surfacing as `InternalError`/`DatabaseError`), so a Postgres sibling of either test must not copy the SQLite form; the precedent is `packages/ne-api/tests/test_agent_catalog_postgres.py`'s `from sqlalchemy.exc import DatabaseError` |
| 23 | PRE-009A (dormant Run storage CORE) | **V8, named in the test's own title.** `test_evidence_claim_or_load_is_atomic_idempotent_and_locator_unavailable_is_explicit` asserted atomicity of the evidence claim path, but its body replayed the claim SEQUENTIALLY on in-memory SQLite, whose global writer lock serializes precisely the two transactions whose interleaving is the hazard — the fixture could not produce the race the name promised. The implementation underneath was a lock-free `SELECT`-then-`INSERT` on `invocation_key` plus a `coalesce(max(sequence),0)+1` RunEvent sequence allocation, and the test was green against both. Per [[test-names-are-normative-claims]] the universal in the NAME certified the gap away: a reader auditing "is the claim path atomic?" found a green test saying yes | the fixture inverted into one that CAN fail — barrier-released concurrent workers on separate connections against real Postgres, asserting both that nothing escaped (no leaked `IntegrityError`) and that exactly the winner's rows persist — in `packages/ne-runtime/tests/test_run_store_postgres.py::test_concurrent_same_invocation_evidence_claims_yield_one_record_without_integrity_error` and `::test_concurrent_distinct_evidence_claims_allocate_unique_gapless_event_sequences`, promoted into the direct and pooled live-Postgres CI lanes under a no-skip/zero-collection floor derived from pytest's own collection. The SQLite test keeps only what SQLite can prove and says so in its name and docstring (`test_sequential_evidence_replay_returns_the_winner_and_locator_unavailable_is_explicit`). Measured control beside it: `::test_unlocked_max_plus_one_event_sequence_duplicates_under_the_same_harness` reimplements the pre-fix allocation under the IDENTICAL harness and shows it duplicating, so a green sequence proof cannot be green merely because the harness never raced. Detector decision: covered by **V8**'s existing mapping (§3 — differential mutation + measured-counterfactual convention); the differential mutation is this unit's own mutation M1 (restore `SELECT`-then-`INSERT` / unlocked `max + 1`), verified RED then GREEN on revert |
| 24 | PRE-009A (dormant Run storage CORE) — **found in this unit's own first draft, by its own Postgres run** | **A statically-false condition made a whole branch dead, and a concurrency proof passed through the wrong path.** The corrected claim-or-load used `if result.rowcount == 1:` to decide whether a speculative `INSERT ... ON CONFLICT DO NOTHING` had won. SQLAlchemy reports `rowcount == -1` for an INSERT with no `RETURNING` clause, so that comparison is false for **every** execution — the winner branch was unreachable, every caller fell through to the replay branch, and `test_concurrent_same_target_protected_payload_claims_yield_one_row_and_one_winner` was **GREEN against it**, because the replay branch happens to return the same correct answer. This is V2's family (a comparison whose truth value does not depend on program state) sitting in PRODUCTION code rather than in an assertion, with the test's greenness as the symptom; the sibling evidence and delivery paths, whose losing branch was NOT answer-equivalent, failed loudly and are what exposed it | arbitration decided by `RETURNING` and a fetched row (`if not arbitration.fetchall(): raise _ClaimRaceLost`), never by `rowcount`, at all three claim sites; `packages/ne-runtime/src/ne/runtime/run_store.py`. Detector decision: **declared residual, with a stated backstop.** No lint rule in this repo knows which DML shapes populate `rowcount` under which driver, and mutation testing does not catch it either — the mutant and the original behave identically wherever the two branches agree, which is exactly the case that hid it. The backstop that DID catch it is structural rather than mechanical: a claim path's losing branch must be proven by a test whose assertion **distinguishes the two branches** — here, the delivery and evidence proofs assert the loser's speculative RunEvent rolled back, which the replay branch cannot satisfy. Stated as a convention for the next unit: **an idempotent claim-or-load whose two branches return equal values needs at least one assertion on a side effect that only the winning branch produces**, or the branch selector is untested by construction |
| 25 | PRE-009A (dormant Run storage CORE) — **found by this unit's own mutation round, in its own first draft** | **V8 again, and the measurement is the finding.** Four barrier-released concurrency proofs in `test_run_store_postgres.py` were written to catch a lock-free SELECT-then-INSERT claim path. Restoring that exact defect left the protected-payload proof **GREEN across five consecutive runs**: the workers built their event loop and store AFTER `barrier.wait()`, so entry into the claim spread ~6 ms across eight threads while the claim transaction commits in well under 1 ms — every thread simply read the previous winner. The fixture was released together and still never overlapped, which is the failing case it existed to produce. The sibling sequence proof was unaffected (its mutation widens the window instead of narrowing it), which is what made the asymmetry visible | all per-thread setup hoisted BEFORE the barrier (`_race_claims` builds the event loop, store and coroutine factory in `build(round, index)`, then waits), and the race repeated over `CLAIM_RACE_ROUNDS = 10` rounds; the same mutant then raises `UniqueViolation` in 24 of 25 measured rounds, and RED in every run since. Paired with a standing measured control — `test_an_unarbitrated_select_then_insert_raises_under_the_same_harness` runs a raw SELECT-then-INSERT against a scratch table through the IDENTICAL harness and asserts it DOES raise — so a future change that re-widens the window fails that control instead of silently re-hollowing all four proofs. Mutations M1b/M1c verified RED then GREEN on revert. Detector decision: covered by **V8**'s existing mapping (§3 — differential mutation + measured-counterfactual convention), sharpened by this instance into a convention the next concurrency proof should inherit: **a barrier proves nothing on its own.** A concurrency guard owes (i) all per-thread setup hoisted before the barrier, (ii) a measured statement of the window it actually achieves versus the transaction it must overlap, and (iii) a control in the same file proving the harness still produces the interleaving. Corollary recorded from the same round: a single-connection backend cannot host this class of proof at all — an in-memory `sqlite://` engine returns the SAME pooled connection from every `connect()`, so a mutant that commits a write on its own connection is invisible to every SQLite test, and the split-transaction detector must live on Postgres |
| 26 | PRE-009A (dormant Run storage CORE, second correction) | **A redundant mechanism can mask its sibling's mutation — but the ledger must name the mutant that actually survives, and an earlier draft of this row named the wrong one.** The controlling correction requires BOTH a Run row lock before replay/event allocation AND a concurrency-safe sequence claim. The draft asserted that dropping `_lock_run` was invisible and that *neither mechanism is individually pinned by any control*. **That universal was false and needed no database to disprove:** deleting the `_lock_run(conn, run_id)` call from the delivery path is **RED** on `test_run_store.py::test_delivery_receipt_rolls_back_its_run_event_when_the_receipt_side_fails`, because `_lock_run` is the only Run-EXISTENCE check on that path and the test pins it at `test_run_store.py:1262` with `pytest.raises(LookupError, match="unknown Run")`; the mutant instead raises `IntegrityError: FOREIGN KEY constraint failed` from the `ne_agent_run_event_seq` seed. Measured both ways on `d883ef9b`: whole-call deletion RED (1 failed), and the narrow mutant below GREEN (65 passed) | the surviving mutant is **narrower than the draft claimed**: keep the existence `SELECT`, drop only `.with_for_update()`. THAT is green, and the residual is legitimate for that mutant alone — the lock's SERIALIZATION semantics are redundant with the counter-row lock, and no runtime control distinguishes them. Note the direction of the error: the corrected residual **under-claims** coverage rather than concealing a gap, so no defect was being legitimised — but a canonical file whose whole purpose is to stop prose from disarming review cannot itself ship a measurably false universal, which is why this row is corrected in place (it is unmerged and therefore not yet append-only history) rather than left standing with a correction appended beneath it. Detector decision: **declared residual, narrowed to the `FOR UPDATE` qualifier**, with the convention restated precisely: *when a unit installs two mechanisms for one invariant, mutate them together AND state which single-mechanism mutant genuinely survives — do not assume both do.* Second instance from the same round, also corrected: splitting the delivery RunEvent onto its own connection (`M3a`) was described as a "deadlock"; it is precisely an **indefinite cross-connection block** — the split connection's `ne_agent_run_event_seq` seed needs `FOR KEY SHARE` on `ne_agent_runs`, which the outer transaction already holds `FOR UPDATE` — not a detected cycle, and Postgres never aborts it. A hang is not a usable mutation signal either way, so that family is pinned instead by `M3d` (mint an event id without ever inserting the RunEvent, verified RED across 25 tests) and by the composite-FK demotions `M3b`/`M3c`/`M3e`/`M3f`. **Cross-cutting dependency recorded here because it is invisible from any single test:** `M3b`, `M3e` and deleting `ck_delivery_run_bound_shape` are all **GREEN under SQLite** — their only detectors live in `test_run_store_postgres.py`, so the delivery/association integrity guarantees rest entirely on the required-CI Postgres gate. That gate is delivered, so this is a dependency to state rather than a gap; a future unit that weakens the required gate silently un-pins those guarantees |
| 27 | PRE-009A A1 (package-local target/claim closure) — **the second instance of row 26's shape, and the count went UP** | **FOUR of the seven gates in one method have no single-mutation control**, because the same properties are independently enforced by up to three layers. In `materialize_source_free_claim_slice`: the SQL `WHERE` selection, the canonical `target_hash` recomputation, and the **AEAD/AAD authentication** (the caller-supplied target IS the AAD for `materialize_for_target`, so a wrong target simply fails to decrypt). Measured on the A1 candidate: dropping the owner `WHERE` predicate alone — green; dropping the tuple `WHERE` predicates alone — green; dropping both PLUS the `target_hash` recomputation — **still green**. Individually masked as well: the `target_hash` recomputation on its own, the `is_claimable_target_kind` gate (masked *by construction* — the tuple check constraint forces non-tupled rows to NULL tuple columns, so the join can never match one), and the `protected_ref` filter (masked by the AEAD `row_id`). Only `served_output_hash` and the two source-bound gates are individually discriminating | the ledger records `A1-3a`/`A1-3b` as **masked, not killed** and names the layers. The combined mutation removing all three target layers — `WHERE`, `target_hash`, and the AAD re-keyed to the row's own stored columns — turns `test_every_refused_claim_slice_shares_one_indistinguishable_outcome` RED on **exactly 5 of its 9 parameters** (measured, not rounded; independently reproduced). The **owner** property is pinned the same way at the aggregate: removing all three owner layers turns the `foreign_owner` parameter RED. Detector decision: **declared residual**, identical in kind to row 26 — no runtime control distinguishes a redundant enforcement from a dead one while a sibling holds the invariant, and mutation testing is structurally blind to it. **Current backstop, stated plainly:** cross-review reading a resolver as a whole and asking *how many independent things enforce this?*, plus the row-26 requirement that a unit installing multiple mechanisms for one invariant **say so in its ledger and mutate them together**. No mechanical detector exists and this row claims none. **Read this row WITH row 26.** Row 26 (CORE) is the two-layer instance — a Run row lock plus a concurrency-safe sequence claim, where restoring `max + 1` alone stayed green. This is the same convention at three layers, in a different subsystem, one phase later, and the masking is **broader than first reported**: the initial ledger implied it was confined to the tuple layers when it actually covers four of seven gates. That it recurred immediately, and that its true extent was understated on the first pass, is the finding: **defence in depth and mutation evidence are in permanent tension, and the resolution is disclosure, not a cleverer mutation.** The convention: *count the enforcement layers before writing the mutation, mutate them together, state the count, and state how many gates in the method are individually discriminating* |
---

## 5. The ratchet rule

**Every newly-discovered vacuous-guard shape or confirmed instance MUST land in this file, in the
unit that fixes it, together with a detector decision:** a new `ne check test-shapes` rule
(future, NE-VG-1), a planted fixture, or a **declared residual** with a stated backstop status.
NE-VG-1 pins the catalogue-entry-to-detector mapping with its own test (design §5.1's
ratchet-test description: "every entry in this catalogue must map to a TS-rule or a declared
residual — adding instance N without deciding its detection status fails the gate").

- Entries in §3 and §4 are **append-only**. Do not delete or silently revise a row; if a claim is
  later found wrong (as §2 records for V6), add a correction section — do not rewrite history.
- Each escape buys a **permanent** detector. The ratchet is the mechanism's only claim to
  improving over time; it does not claim completeness at any point (§6).
- **The ratchet firing before a fix lands, as evidence it works:** `origin/main` advanced to
  `efdbc47` (the NE-VACUOUS-GUARD epic row) with **V10** already catalogued while this unit
  was in flight — found by Codex cross-review of the unmerged `ne/NE-VG-2` branch, not yet fixed.
  Detector-decision mapping: **V10 → declared residual, PENDING FIX** (§3 table; recommended form:
  `xfail(strict=False)` on the positive assertion). The rule that "every newly-discovered shape
  MUST land in this file" is not conditional on the fix landing first — catalogue immediately,
  close later; a shape sitting undocumented for even one merge is the exact drift this file exists
  to end. This row is added here, not left as a SPEC-epic-row-only mention, precisely to keep that
  promise on this unit's own first day.

---

## 6. The mechanical gate — what it DOES and DOES NOT catch (read before quoting this as totality)

Since NE-VG-0, the unit workflow runs a deterministic
**MUTATION-EVIDENCE GATE** after the synthesis stage, alongside the pre-existing
outstanding-finding gate. The two gates compose (a well-formed mutation-evidence table does not
excuse an untriaged Codex finding, and vice versa).

**DOES:**
- Force-blocks any unit whose brief `mayTouch` includes `packages/*/src`, `tools/*/src`, or a
  package-level `**` catch-all glob (e.g. `packages/ne-x/**`) when the **builder's own**
  `mutations[]` is empty (`builderMutations`, round 7 — see below).
- Rejects **any** `mutations[]` entry — from either stage, required or volunteered — missing or
  blank `target`, `change`, `expectRedTests`, `observed`, or `control`.
- **Re-validate evidence after Codex's fix stage, with a precisely stated scope (since round-6's
  C1, refined by round 7).** A FIXED finding whose `file` is a src-scoped path (checked with the
  same `_isSrcScopedGlob` the gate already runs against `brief.mayTouch`) requires refreshed
  `codexFixes.mutations[]`, validated by the same per-entry shape check every builder entry goes
  through — one validator, not two that could silently diverge (R4). **Since round 7, builder
  evidence and fix-stage evidence are counted as SEPARATE populations**: the builder-emptiness
  check reads `builderMutations` (the builder's own report) alone, so a fix stage volunteering
  evidence for its own fix can no longer mask a builder that filed zero evidence (round-7 P0 —
  see below). Scope limits, stated precisely rather than left implicit: this is PRESENCE-AND-SHAPE
  re-validation only, identical in kind to the builder-evidence check above — semantic validity of
  either population stays Argus's independent re-run duty (prompt item 6), and a FIXED finding
  whose `file` is missing or blank owes NO evidence at all (round-7 P2a — a declared DOES-NOT
  boundary, see below, not silently covered here).
- Runs in code, after the LLM synthesis verdict, and overrides it. It cannot be argued around by
  a model saying "trust me" — this is the whole point (SPEC §6.1; [[written-rules-disarm-review]]).
- **Blocks (never crashes) on a malformed `codex.findings` (round-7-second-pass P2).** A truthy
  non-array Codex-review `findings` field is coerced to `[]` for triage AND separately blocked
  with a `REVIEWER-FINDINGS GATE:` blocker — a verdict, never a `TypeError`. Detected above the
  `GATE-RATCHET` region (a declared, bounded exception — see DOES-NOT).
- **Never crashes on a malformed `synthesis.blockers` (round-7-second-pass P1).** A truthy
  non-array value is coerced to `[]` before being merged into the gate-forced override; unlike
  the guards above, this one carries no companion blocker by design (see DOES-NOT for why).

**DOES NOT:**
- Judge semantic validity. A `mutations[]` entry can be syntactically well-formed and
  semantically empty: a mutation that does not actually mutate anything load-bearing, a
  `control` string that describes the *mutant* rather than the unmutated tree, an
  `expectRedTests` id pointing at the wrong file, or fabricated `observed` output that was never
  run. **The gate checks presence and shape, not truth.** Semantic validity is Argus's amended
  re-run duty (independently re-run ≥1 listed mutation, verify its control, and invent+run ≥1
  probe of its own — prompt item 6) and, longer-term, the design's NE-VG-4 mutation lane.
- Force tests-only or docs-only units to produce evidence — SPEC §6.1 still applies to them by
  review, not by this mechanical gate (their `mayTouch` legitimately has no `src` surface, so
  `mutationGate.required` is `false`).
- **Cover source outside `packages/` and `tools/` at all.** `_isSrcScopedGlob` requires the
  glob's *first* path segment to be literally `packages` or `tools`; every other top-level tree is
  invisible to the gate regardless of whether it holds source. Verified against the real workflow
  via the dry-run harness: `deploy/airgap/**`, `deploy/airgap/harness.sh`, `apps/console/src/**`,
  `demo/**`, `tools/supply-chain/vex_dualkey_gate.py`, and `tools/engagement/readiness.py` **all**
  return `required=False` / proceed unguarded (controls confirming the predicate is live, not
  inert: `packages/ne-api/src/...`, `packages/ne-api/**`, and `tools/ne-cli/src/**` all return
  `required=True`). This matters concretely: `deploy/airgap/` holds gallery instance **#1** (#337,
  §4) — the gate cannot fire on a repeat of its own catalogue's first entry — and
  `tools/supply-chain/` (flat, no `src/` subdirectory) holds the VEX and npm-license supply-chain
  security gates, likewise ungated. **This is the gate's largest undiscovered-scope hole as of
  this unit.** Closing it (broadening `_isSrcScopedGlob` to a `src`-segment-anywhere rule, or
  explicitly adding `deploy/`, `demo/`, and flat non-`src/` `tools/*` trees) is unbriefed follow-up
  work — NE-VG-0's `mayTouch` does not extend to redesigning the predicate's matched trees, so
  this section names the gap rather than silently living inside it.
- Inspect the actual git diff. The gate is keyed off `brief.mayTouch`, not the committed diff,
  because the workflow runtime cannot shell out to `git` mid-pipeline. Three consequences, stated
  plainly: (a) a diff touching `src` **outside** the declared `mayTouch` is caught by Argus's
  scope-violation check, not this gate; (b) a unit whose `mayTouch` includes `src` surface but
  whose *actual* diff ends up docs-only will be **false-blocked** — the fix is re-planning with an
  honest `mayTouch`, not weakening the gate; and (c) **the gate can be bypassed on a supported path
  — continuation dispatches.** A continuation round's plan prompt instructs the planner to
  set `mayTouch` to only the corrective work, and the gate reads only that brief. So a
  src-touching branch whose corrective round is honestly docs-only yields
  `mutationGate.required=false` on the round whose verdict actually carries the merge —
  `mayTouch` is correct, nothing is out of scope for Argus's scope check to catch, and nothing
  re-demands the earlier rounds' evidence at final synthesis. The earlier evidence was gated when
  it was filed, but the merge-carrying verdict does not re-verify it against the full branch diff
  (the same execution-verified fact as above: `required` is a function of the current brief's
  `mayTouch` alone). Closing this requires the gate to see the branch diff, which the runtime
  constraint above currently rules out — so it is named here as a real, open bypass rather than
  papered over.
- Claim totality. Per design §5.6/§5.7: this gate is one layer among several (lint floor,
  PR-diff mutation lane, this pipeline gate); it is shape-agnostic where a future lint would not
  be, but a thirteenth vacuous-guard shape is caught by claim-driven mutation probes and the
  ratchet rule (§5 above) — never by an implicit promise that presence-checking is sufficient.
- Re-validate evidence for a fix that changes the mutated code path AGAIN after `codexFixes`
  reports it (a hypothetical third-stage edit). The gate reads Codex's **last-reported**
  `codexFixes.mutations[]` for that fix; there is no fourth stage in this pipeline that could
  invalidate it today, but if one is ever added, its evidence needs the identical treatment C1
  gave the builder-vs-fix gap, not an assumption that the C1 fix already covers it.
- **Owe fix-stage evidence for a FIXED finding whose `file` is missing or blank (round-7 P2a,
  declared boundary).** `fixedSrcTouchingIds` (the set of FIXED findings requiring refreshed
  evidence) requires `typeof f.file === 'string' && f.file.trim()` in addition to
  `_isSrcScopedGlob(f.file)` — a finding whose triage-stage `file` field is absent or blank owes
  no evidence, even if its free-text `description` names a src path. Chosen deliberately over the
  alternative (treat a fileless FIXED finding as evidence-owing) because that alternative
  manufactures **spurious over-blocking** of docs/process fixes — a protected direction this
  unit's stopping rule forbids introducing. Justification: `file` is stamped by the same Codex
  triage stage the gate already trusts for `severity`/`description` (Stage 3, `CODEX_SCHEMA`), so
  requiring it here asks nothing new of that stage. The real residual — a src-touching fix whose
  finding genuinely lacks a `file` field — is not silently unmonitored: it remains visible to the
  mandatory `argus-recheck` review of the FULL diff (triggered whenever `codexFixes.fixed` is
  non-empty), which is the same backstop the presence-only nature of this whole gate already
  leans on for semantic validity. Pinned as a boundary, not left as an unstated gap:
  `test_codex_fix_missing_file_finding_not_evidence_owing` (parametrized: file-key-absent,
  file-blank) in the mutation-gate test suite.
- **Extend the ratchet's inventory-forcing pin to gate-ADJACENT logic living above
  `GATE-RATCHET BEGIN` (round-7-second-pass, Argus's own out-of-region finding).** The
  `codex.findings` normalization (`withIds(codex.findings, 'C')`'s call site, feeding
  `triageFindings`) sits above the sentinel, so an edit to it will never trip the ratchet
  or force a D-table update — even though this round hardened it against the identical
  crash class the in-region siblings were hardened against (D73-D76). The hardening is
  real and tested; the MECHANICAL enforcement is not, and is named here rather than
  silently assumed. Widening the region to include it was considered and rejected for
  this round: `withIds` (and the `let codexFindingsShapeDefect` state threading its
  result to the in-region blocker) sits inside Stage 3's conditional block, structurally
  entangled with the Codex-CLI-invocation prompt text around it — moving the sentinel up
  to enclose it would also enclose that prompt, making ordinary prompt-wording edits
  falsely trip the ratchet (the exact over-block `test_ratchet_ignores_edits_outside_region`
  exists to keep out). Filed as a known, bounded blind spot, not a silent one.
- **Enforce a companion blocker for every hardened-against-crash guard.** `synthBlockersList`
  (round-7-second-pass) is hardened against a truthy non-array `synthesis.blockers` the same
  shape as `codexFixedList`/`codexDeclinedList`/`codexFindingsShapeDefect` — but, unlike
  those three, it has NO blocker of its own (D77). This is a deliberate asymmetry, not an
  oversight: by the time that line runs, `forcedBlockers` (the actual gate decision) is
  already fully computed, so a malformed `synthesis.blockers` can only ever cost extra
  blocker TEXT, never the verdict. Declared here so a future reader does not assume every
  R4-hardened guard in this file follows an identical add-a-blocker template. The mutation-gate test suite
proves (1) evidence-absent blocks, (2) well-formed evidence — the control — proceeds (so the gate
cannot be satisfied by `return blocked` unconditionally), (3) a control-less entry still blocks,
and (4) a self-mutation of the gate's own condition line lets the evidence-absent scenario through
(with a `count == 1` precondition on the mutated string, so the probe itself cannot be a silent
no-op). This is the meta-invariant this unit exists to uphold: **a guard against vacuous guards
must not itself be satisfiable by the thing it forbids.**

The first pass of this suite (ten tests) still had a real gap: `_mutationEntryDefects` checks
five fields with one line each, but only `control` had a dedicated missing/blank probe. An
independent Argus review confirmed by direct reproduction that a mutant shrinking the checked-field
list to `['control']` alone, a mutant deleting the `expectRedTests`-emptiness check, and a mutant
deleting `_isSrcScopedGlob`'s plain `segs.includes('src')` branch (subsumed by `**` in every other
src fixture the suite used) all survived undetected — a live instance of V3/V5 inside the very
suite meant to catch V3/V5. The suite now parametrizes over all five fields and adds a
`src`-without-`**` fixture, so each of `_mutationEntryDefects`'s five field checks has its own
planted-RED offender. This does not mean the suite is now exhaustive — see the DOES-NOT list
above — only that this specific, demonstrated gap is closed. (An earlier version of this
paragraph claimed "each decision point in the gate" has an offender; a later review round
falsified that broader claim — see below — so the phrasing here is now scoped to what is
actually demonstrated.)

Argus's round-3 review turned the thesis on the suite a second time: two invented mutants against
`_isSrcScopedGlob` — dropping the `tools` disjunct from the first-segment check, and dropping the
`tests`/`docs` exclusion from the `**` catch-all branch — each survived all 17 then-current tests
(independently reproduced: 17/17 green under both mutants before this fix). The `tools/*/src` half
of the gate's scope had no planted-RED offender at all, and the exclusion had no `required=False`
pin, so a mutant force-blocking tests-only units — which then have no legitimate way to comply —
was equally invisible. Closed with tools-scope fixtures (`tools/ne-cli/src/**` and a plain
`tools/.../src/...` file path), tests-only and docs-only `required=False` fixtures, a JSON-`null`
entry offender for the non-object guard (whose removal makes the workflow CRASH with a TypeError
rather than block — a crash-not-block failure mode), and self-mutation partners with `count == 1`
no-op guards for both predicate branches. As before: this closes the demonstrated gaps; it does
not make the suite exhaustive.

A follow-up review pass then falsified the "each decision point in the gate" completeness claim
this section used to make (see above): four more surviving mutants, in branches the by-then
24-test suite didn't touch. `ertOk`'s array-element predicate accepted a mutant weakening
`.some((t) => ... t.trim())` to a bare length check — `expectRedTests: [""]` was wrongly accepted.
The package-level catch-all accepted a mutant dropping the `segs.includes('**')` conjunct — a
concrete non-src file like `packages/ne-x/README.md` would force-block with no `mutations[]` to
file, the same no-way-to-comply failure mode as the tests/docs exclusion gap above, in the
sibling conjunct of the same branch. `String(glob).trim()` had no offender at all — dropping it
lets a whitespace-padded src glob (a real hazard in LLM-emitted `mayTouch` arrays) silently escape
gating. And `ertOk`'s string-form tolerance branch was untested in either direction. Four fixtures
close these — see the test file's own docstring for the specific test names — and the
"each decision point" phrasing is retired in favor of narrower claims that trace to the actual
test list, per this document's own standard: completeness claims are exactly the kind of
trivially-checkable statement that must be checked, not trusted.

A fourth review pass applied the same thesis one level deeper — not to a branch the suite never
touched, but to type-discipline WITHIN branches it already claimed to cover. Three more surviving
mutants, reproduced by direct harness run: `Array.isArray(report.mutations)` had no offender at
all, and weakening it to a bare truthy-or-`[]` fallback is a SILENT FULL BYPASS — a truthy
non-array `report.mutations` makes `mutations.length` `undefined`, so neither the emptiness check
nor the per-entry loop ever fires, and the gate goes ready-to-merge with an empty blockers list
(`entries` silently vanishing from the JSON) — the one failure direction this document's own
thesis names as mattering most: silent under-blocking, not over-blocking or a crash. The shared
`typeof m[k] === 'string'` check behind target/change/observed/control, and `ertOk`'s
array-element `typeof t === 'string'` check, each had a missing-field offender but no
WRONG-TYPE offender — a report filing `observed: true` or `expectRedTests: [42]` passed evidence
review. And the `(brief && brief.mayTouch) || []` fallback had no offender either — dropping it
makes an absent `mayTouch` CRASH the whole pipeline (TypeError, harness exit != 0) instead of
returning any verdict, the identical crash-not-block failure mode as the adjacent
`mutations=[None]` offender one line up in the same source block. Four fixtures close these — see
the test file's own docstring for the specific test names and the disposition note on the
string-form `expectRedTests` tolerance (a deliberate contract, not a residual, per that review's
fourth finding). As every round before it: this closes the demonstrated gaps, not the space of
all possible mutants.


### The decision-point inventory — coverage by enumeration, not by reviewer imagination (round 5)

Four review rounds closed exactly the mutants the reviewer happened to invent that round
(10 -> 17 -> 36 tests, and still leaking) — and round 4's *own* new code introduced two more
surviving mutants in the process (the per-entry loop bound below, unpinned because every
prior fixture put its defective entry at index 0; and `ertOk`'s string-branch `.trim()`,
unpinned because its sibling array branch got both accept- and reject-side tests and the
string branch got only accept-side). Reactive mutant-closing does not converge — a different
reviewer's imagination always finds a different gap. Round 5 changes method: every branch,
boolean operand, and loop bound in the ~30-line gate (`_isSrcScopedGlob`,
`requiresMutationEvidence`, the `mutations` binding, `_mutationEntryDefects`, and the gate
body) is enumerated below, and each row either names a planted-RED offender in
the mutation-gate test suite or is declared an **equivalent mutant**
with a written reason and a verified green run.

Two rules bind every future edit to this gate:

- **R1 — every collection the gate ITERATES OR AGGREGATES gets at least one fixture that
  exercises more than its first/only element.** Originally stated narrower ("the per-entry
  loop stays pinned at a non-zero index"), which is exactly why it did not reach the next
  instance of the same class: a follow-up Argus review found the `bad` defect-list
  aggregation (`bad.join(', ')` inside `_mutationEntryDefects`) had no offender for an entry
  with TWO OR MORE defects, for the identical reason — every fixture in the file corrupted
  exactly one field, so `bad` was always length 1 and `.join`, `.slice(0, 1)`, and `bad[0]`
  are indistinguishable on a length-1 array. `test_defect_at_nonzero_index_blocks` and
  `test_null_entry_at_nonzero_index_blocks` pin the per-entry LOOP (any quantifier weakening
  of `for (let i = 0; ...)`); `test_multiple_defects_reports_all_field_names` pins the `bad`
  AGGREGATION (any truncation of `bad.join(', ')`); `test_defect_index_argument_is_not_hardcoded`
  pins the per-entry loop's INDEX ARGUMENT passed into `_mutationEntryDefects`, a distinct
  failure mode from the bound (rows D32-D34, D40-D42 below). The rule generalises to the
  class — any future collection the gate iterates or aggregates needs a fixture with 2+
  elements, not a fixture per mutant a reviewer happens to invent.
- **R2 — every branch with an accept-side test must have a reject-side test, and vice versa.**
  The string-branch `.trim()` survived precisely because its sibling array branch got both
  directions and it got one. Adding branch N+1 to the gate REQUIRES a new row in the table
  below with paired accept/reject tests — an empty row is the visible signal that coverage is
  missing, not a reviewer's imagination.

Round 6 adds two more binding rules, both motivated by real surviving mutants (below), not by
anticipation:

- **R3 — an inventory row is an OPERATOR WITH ITS DIRECTIONS, not a line.** Every operator
  that has sides, ends, positions, or an order gets a row per direction: `.trim()` ->
  leading AND trailing; `.includes()` -> present AND absent; `.some()` -> at-least-one AND
  none; comparisons -> each operand, in each direction; array spreads and `push`/`unshift` ->
  order as well as membership. **Motivating case:** `String(glob).trim()` was ONE row in the
  round-5 table (D1/D2 above) and only its LEADING side had a planted-RED offender — the
  expression-level blind spot the table already named (the "what does this line COMPUTE"
  paragraph two sections up) turns out to itself have had an undercounted granularity: a
  `.trim()` expression is not one decision point, it is two (row D49 below closes the
  trailing side). R3 makes this recurrence-proof by changing the UNIT OF ENUMERATION, not by
  patching the one instance a reviewer happened to find.
- **R4 — sibling symmetry: when you harden X, name X's structural twin in the same edit, and
  either harden it or declare in writing why it needs none.** An unhardened structural twin is
  a defect even with no offender yet — waiting for a reviewer to invent one is exactly the
  reactive pattern R1-R3 all exist to end. Three instances prove this is a recurring class, not
  a one-off: round-4's array-vs-string branches of `ertOk` (D21-D28's sibling gap, closed only
  after a review found it); round-5's leading-vs-trailing `.trim()` (D1/D2 vs the newly-closed
  D49 — R3's own motivating case is also an R4 instance); and round-6's
  `Array.isArray(report.mutations)` (D15/D16, hardened at round 4) vs the bare
  `(brief && brief.mayTouch) || []` fallback (unhardened until round 6, despite sitting two
  lines away and sharing the identical "schema TEXT is not runtime enforcement" argument the
  D15/D16 test's own docstring already made) — row D57-D59 below.

The table below runs one prescribed mutant per decision point against the full suite
(the mutation-gate test suite), records the actual
pass/fail counts, and names the killing test(s). Anchors are verbatim fragments of the code
at the time of this round (not bare line numbers, which drift on any edit above the anchored
line); mutants marked KILLED went RED as shown, mutants marked EQUIVALENT ran GREEN (48/48)
as predicted — every row below was independently executed, none is asserted from reasoning
alone. **Any row whose actual run diverged from its predicted disposition would have been
reclassified or given a new offender before this table was written — none did**, per the
procedure's own rule (a surprise KILL->green or EQUIVALENT->red is not recorded as-is).

**Numbering note:** row IDs are non-contiguous by construction (D11, D14, D19 do not
appear) — draft numbering was assigned while enumerating the source top-to-bottom and some
drafted decision points were later folded into a neighboring row or found not to be a
distinct branch; a gap is not a missing row, and any citation elsewhere in this repo to a
row ID that isn't in this table (as happened once, to `D19`, in a test docstring — fixed)
is a bug to fix at the citation, not evidence of an uncovered point. **Run-result baseline:**
rows D1-D39 and E1-E4 were executed against the 48-test suite that existed when this
subsection was first written; rows D34, D40-D42, and E5-E6 were added by a subsequent
review round against the resulting 50-test suite (two new offenders,
`test_multiple_defects_reports_all_field_names` and
`test_defect_index_argument_is_not_hardcoded`); rows D43-D48 were added by a THIRD review
round against the resulting 54-test suite (four new offenders —
`test_synth_blocked_verdict_with_no_gate_blockers_still_blocks`,
`test_synth_blockers_survive_gate_forced_override`,
`test_synth_blockers_key_omitted_does_not_crash_gate_override`, and
`test_notes_join_separates_multiple_fragments_with_single_space`) — the pass/fail *counts* in
each row's `Run result` column are relative to the suite size at the time that row was
executed, not a single shared total; the KILLED/EQUIVALENT *verdict* is what's load-bearing,
and re-running the earlier rows against the current suite size was not repeated solely to
update a count column with no bearing on the verdict. **Rows D49-D59 and RES1/RES2 were added
by a FOURTH review round (round 6) against the resulting 61-test suite** (three new offenders —
`test_trailing_whitespace_padded_src_glob_requires_evidence` (parametrized x3),
`test_non_array_may_touch_blocks_not_crashes` (parametrized x3), and the widened
`test_multiple_defects_reports_all_field_names` (parametrized x2, one new case)) — **and from
this round forward the unit of enumeration is OPERATOR × DIRECTION (R3), not the line**: where
rows D1-D48 enumerated branches/operands/loop-bounds/expressions one line at a time, rows
D49-D59 enumerate the specific DIRECTION of an operator that a previous row's mutant left
untested (D49/D50 split the two sides of the `.trim()` D1/D2 already covered as a single
expression; D57-D59 give the new `Array.isArray(_mayTouchRaw)` guard its crash/under-block/
over-block directions in three separate rows, per the D15/D16 precedent one section up).

| Row | Decision point (anchor) | Prescribed mutant | Verdict | Run result |
|---|---|---|---|---|
| D1 | `String(glob).trim().split('/')` | `glob.trim().split('/')` | KILLED by test_non_string_may_touch_entry_tolerated_not_crash | 1 failed, 47 passed in 2.29s |
| D2 | `String(glob).trim().split('/')` | `String(glob).split('/')` | KILLED by test_whitespace_padded_src_glob_requires_evidence | 1 failed, 47 passed in 2.22s |
| D3 | `.split('/')` | `.split(',')` | KILLED by test_evidence_absent_blocks, test_expect_red_tests_string_form_accepted, test_mixed_real_and_blank_expect_red_element_accepted (+9 more, see suite) | 12 failed, 36 passed in 2.15s |
| D4 | `if (segs[0] !== 'packages' && segs[0] !== 'tools') return false` | `if (segs[0] !== 'tools') return false` | KILLED by test_evidence_absent_blocks, test_expect_red_tests_string_form_accepted, test_mixed_real_and_blank_expect_red_element_accepted (+7 more, see suite) | 10 failed, 38 passed in 2.08s |
| D5 | `if (segs[0] !== 'packages' && segs[0] !== 'tools') return false` | `if (segs[0] !== 'packages') return false` | KILLED by test_tools_scope_disjunct_is_load_bearing_self_mutation, test_tools_src_globstar_requires_evidence, test_tools_src_plain_file_requires_evidence | 3 failed, 45 passed in 2.07s |
| D6 | `if (segs[0] !== 'packages' && segs[0] !== 'tools') return false` | *(line deleted)* | KILLED by test_non_packages_tools_tree_not_required, test_tools_scope_disjunct_is_load_bearing_self_mutation | 2 failed, 46 passed in 2.02s |
| D7 | `if (segs.includes('src')) return true` | *(line deleted)* | KILLED by test_src_glob_without_globstar_requires_evidence, test_tools_src_plain_file_requires_evidence | 2 failed, 46 passed in 2.07s |
| D8 | `return segs.includes('**') && !segs.includes('tests') && !segs.includes('docs')` | `return !segs.includes('tests') && !segs.includes('docs')` | KILLED by test_package_level_non_src_non_globstar_not_required, test_tests_docs_exclusion_is_load_bearing_self_mutation | 2 failed, 46 passed in 2.04s |
| D9 | `return segs.includes('**') && !segs.includes('tests') && !segs.includes('docs')` | `return segs.includes('**') && !segs.includes('docs')` | KILLED by test_tests_docs_exclusion_is_load_bearing_self_mutation, test_tests_only_unit_not_required | 2 failed, 46 passed in 2.01s |
| D10 | `return segs.includes('**') && !segs.includes('tests') && !segs.includes('docs')` | `return segs.includes('**') && !segs.includes('tests')` | KILLED by test_docs_only_glob_under_package_not_required, test_tests_docs_exclusion_is_load_bearing_self_mutation | 2 failed, 46 passed in 1.98s |
| D12 | `((brief && brief.mayTouch) \|\| []).some(_isSrcScopedGlob)` | `(brief && brief.mayTouch).some(_isSrcScopedGlob)` | KILLED by test_missing_may_touch_returns_verdict_not_crash | 1 failed, 47 passed in 2.02s |
| D13a | `.some(_isSrcScopedGlob)` | `.every(_isSrcScopedGlob)` | KILLED by test_evidence_absent_blocks, test_expect_red_tests_string_form_accepted, test_missing_may_touch_returns_verdict_not_crash (+4 more, see suite) | 7 failed, 41 passed in 2.25s |
| D13b | `.some(_isSrcScopedGlob)` | `.filter(_isSrcScopedGlob)` | KILLED by test_docs_only_glob_under_package_not_required, test_evidence_absent_blocks, test_expect_red_tests_string_form_accepted (+16 more, see suite) | 19 failed, 29 passed in 2.12s |
| D15 | `report && Array.isArray(report.mutations) ? report.mutations : []` — **round-7 re-derived**: this expression now binds `builderMutations` (round-7 P0 split the merged `mutations` array into `builderMutations`/`codexFixMutations`; the expression text itself is unchanged, only its consumer) | `report && report.mutations ? report.mutations : []` | KILLED by test_non_array_mutations_blocks_not_silently_bypassed | round-5: 1 failed, 47 passed in 2.10s; **re-run round-7: 1 failed, 74 passed** |
| D16 | `report && Array.isArray(report.mutations) ? report.mutations : []` (same round-7 re-derivation as D15) | `report && Array.isArray(report.mutations) ? report.mutations : undefined` | KILLED by test_non_array_mutations_blocks_not_silently_bypassed | round-5: 1 failed, 47 passed in 2.04s; **re-run round-7: 1 failed, 74 passed** |
| D17a | `m.target.trim() ?` | `m.target ?` | KILLED by test_blank_target_label_omits_parenthetical | 1 failed, 47 passed in 2.03s |
| D17b | `` const label = `mutations[${i}]${m && typeof m.target === 'string' && m.target.trim() ? ... `` | `` const label = `mutations[${i}]` `` | KILLED by test_blocker_label_carries_real_target | 1 failed, 47 passed in 2.03s |
| D17c | `m && typeof m.target === 'string'` | `typeof m.target === 'string'` | KILLED by test_null_entry_at_nonzero_index_blocks, test_null_mutation_entry_blocks_not_crashes | 2 failed, 46 passed in 2.02s |
| D18 | `` if (!m \|\| typeof m !== 'object') return [`${label}: not an object`] `` | *(line deleted)* | KILLED by test_null_entry_at_nonzero_index_blocks, test_null_mutation_entry_blocks_not_crashes | 2 failed, 46 passed in 2.14s |
| D20 | `['target', 'change', 'observed', 'control']` | `['control']` | KILLED by test_blank_target_label_omits_parenthetical, test_defect_at_nonzero_index_blocks, test_each_required_field_blank_blocks[change] (+8 more, see suite) | 11 failed, 37 passed in 2.14s |
| D21 | `typeof m[k] === 'string' && m[k].trim()` | `m[k] && String(m[k]).trim()` | KILLED by test_each_required_field_wrong_type_blocks[change], test_each_required_field_wrong_type_blocks[control], test_each_required_field_wrong_type_blocks[observed] (+1 more, see suite) | 4 failed, 44 passed in 2.07s |
| D22 | `typeof m[k] === 'string' && m[k].trim()` | `typeof m[k] === 'string'` | KILLED by test_blank_control_blocks, test_blank_target_label_omits_parenthetical, test_defect_at_nonzero_index_blocks (+4 more, see suite) | 7 failed, 41 passed in 2.13s |
| D23 | `(typeof ert === 'string' && ert.trim())` | `false` | KILLED by test_expect_red_tests_string_form_accepted | 1 failed, 47 passed in 2.14s |
| D24 | `(typeof ert === 'string' && ert.trim())` | `(typeof ert === 'string')` | KILLED by test_each_required_field_blank_blocks[expectRedTests] | 1 failed, 47 passed in 2.02s |
| D25 | `Array.isArray(ert) && ert.some` | `ert.some` | KILLED by test_each_required_field_blank_blocks[expectRedTests], test_each_required_field_missing_blocks[expectRedTests], test_each_required_field_wrong_type_blocks[expectRedTests] | 3 failed, 45 passed in 2.07s |
| D26 | `.some((t) => typeof t === 'string' && t.trim())` | `.every((t) => typeof t === 'string' && t.trim())` | KILLED by test_empty_expect_red_tests_array_blocks, test_mixed_real_and_blank_expect_red_element_accepted | 2 failed, 46 passed in 2.06s |
| D27 | `typeof t === 'string' && t.trim()` | `t && String(t).trim()` | KILLED by test_expect_red_tests_non_string_element_blocks | 1 failed, 47 passed in 2.03s |
| D28 | `typeof t === 'string' && t.trim()` | `typeof t === 'string'` | KILLED by test_blank_expect_red_tests_element_blocks | 1 failed, 47 passed in 2.05s |
| D29a | `` return bad.length ? [`${label}: missing/empty ${bad.join(', ')}`] : [] `` | `return []` | KILLED by test_blank_control_blocks, test_blank_expect_red_tests_element_blocks, test_blank_target_label_omits_parenthetical (+21 more, see suite) | 24 failed, 24 passed in 2.33s |
| D29b | `` return bad.length ? [`${label}: missing/empty ${bad.join(', ')}`] : [] `` | `` return [`${label}: missing/empty ${bad.join(', ')}`] `` | KILLED by test_expect_red_tests_string_form_accepted, test_mixed_real_and_blank_expect_red_element_accepted, test_tests_docs_exclusion_is_load_bearing_self_mutation (+1 more, see suite) | 4 failed, 44 passed in 2.36s |
| D30 | `if (requiresMutationEvidence && mutations.length === 0) {` — **round-7 re-derived**: condition now reads `builderMutations.length` (round-7 P0; the `requiresMutationEvidence` conjunct itself is unchanged) | `if (builderMutations.length === 0) {` | KILLED by test_non_string_may_touch_entry_tolerated_not_crash, test_non_packages_tools_tree_not_required, test_gate_is_load_bearing_self_mutation, test_tools_scope_disjunct_is_load_bearing_self_mutation (+5 more, see suite) | round-5: 9 failed, 39 passed in 2.10s; **re-run round-7: 9 failed, 66 passed** |
| D31 | `if (requiresMutationEvidence && mutations.length === 0) {` (same round-7 re-derivation as D30) | `if (requiresMutationEvidence && builderMutations.length <= 1) {` | KILLED by test_codex_fix_declined_src_finding_needs_no_evidence, test_codex_fix_well_formed_arrays_produce_no_shape_blockers, test_codex_fix_missing_file_finding_not_evidence_owing[file-key-absent], test_codex_fix_missing_file_finding_not_evidence_owing[file-blank] (+8 more, see suite) | round-5: 5 failed, 43 passed in 2.07s; **re-run round-7: 12 failed, 63 passed** |
| D32 | `for (let i = 0; i < mutations.length; i++) {` | `for (let i = 0; i < Math.min(1, mutations.length); i++) {` | KILLED by test_defect_at_nonzero_index_blocks, test_null_entry_at_nonzero_index_blocks | 2 failed, 46 passed in 2.04s |
| D33 | `mutations.length; i++) {` | `mutations.length; i += 2) {` | KILLED by test_defect_at_nonzero_index_blocks, test_null_entry_at_nonzero_index_blocks | 2 failed, 46 passed in 2.17s |
| D34 | `_mutationEntryDefects(mutations[i], i)` | `_mutationEntryDefects(mutations[i], 0)` | KILLED by test_defect_index_argument_is_not_hardcoded, test_defect_at_nonzero_index_blocks, test_null_entry_at_nonzero_index_blocks | 3 failed, 47 passed in 2.85s |
| D35a | `const forcedBlockers = [...outstandingBlockers, ...mutationBlockers]` — **round-7-second-pass re-derived**: the line now has a THIRD operand, `...reviewerShapeBlockers` (see D76 below for its own dedicated membership mutant); this row keeps targeting the drop of `mutationBlockers` specifically | `const forcedBlockers = [...outstandingBlockers, ...reviewerShapeBlockers]` | KILLED by test_blank_control_blocks, test_blank_expect_red_tests_element_blocks, test_blank_target_label_omits_parenthetical (+32 more, see suite) | round-5: 35 failed, 13 passed in 2.39s; **re-run round-7-second-pass: 55 failed, 26 passed** |
| D35b | `const forcedBlockers = [...outstandingBlockers, ...mutationBlockers]` (same round-7-second-pass re-derivation as D35a — three operands now) | `const forcedBlockers = [...mutationBlockers, ...reviewerShapeBlockers]` (drop `outstandingBlockers`) | KILLED by test_untriaged_codex_finding_still_blocks | round-5: 1 failed, 47 passed in 2.03s; **re-run round-7-second-pass: 1 failed, 80 passed** |
| D36 | `status: forcedBlockers.length ? 'blocked' : synthesis.verdict === 'ready-to-merge' ? 'r...` | `status: synthesis.verdict === 'ready-to-merge' ? 'ready-to-merge' : 'blocked',` | KILLED by test_blank_control_blocks, test_blank_expect_red_tests_element_blocks, test_blank_target_label_omits_parenthetical (+33 more, see suite) | 36 failed, 12 passed in 2.35s |
| D37 | `mutationGate: { required: requiresMutationEvidence, entries: mutations.length, blockers...` — **round-7 re-derived**: the object grew two fields (`builderEntries`, `fixEntries`, round-7 P3 — see D69-D71 below for their own dedicated mutants); this row still targets dropping `entries` specifically, keeping the two new fields in place | `mutationGate: { required: requiresMutationEvidence, builderEntries: builderMutations.length, fixEntries: codexFixMutations.length, blockers: mutationBlockers },` | KILLED by test_codex_fix_touching_src_with_fresh_evidence_proceeds, test_codex_fix_non_src_finding_needs_no_evidence, test_codex_fix_declined_src_finding_needs_no_evidence, test_builder_and_fix_entries_reported_separately (+7 more, see suite) | round-5: 7 failed, 41 passed in 2.15s; **re-run round-7: 11 failed, 64 passed** |
| D38 | `? { ...synthesis, verdict: 'blocked', blockers: [...forcedBlockers, ...(synthesis.block...` | `? { ...synthesis, verdict: 'blocked' }` | KILLED by test_blank_control_blocks, test_blank_expect_red_tests_element_blocks, test_blank_target_label_omits_parenthetical (+25 more, see suite) | 28 failed, 20 passed in 2.23s |
| D39 | `` mutationBlockers.length ? `GATE: mutation evidence missing/malformed (${mutationBlocker... `` | `synthesis.notes \|\| ''` | KILLED by test_evidence_absent_blocks | 1 failed, 47 passed in 2.07s |
| D40 | `` return bad.length ? [`${label}: missing/empty ${bad.join(', ')}`] : [] `` | `` return bad.length ? [`${label}: missing/empty ${bad[0]}`] : [] `` | KILLED by test_multiple_defects_reports_all_field_names | 1 failed, 49 passed in 3.15s |
| D41 | `` return bad.length ? [`${label}: missing/empty ${bad.join(', ')}`] : [] `` | `` return bad.length ? [`${label}: missing/empty ${bad.slice(0, 1).join(', ')}`] : [] `` | KILLED by test_multiple_defects_reports_all_field_names | 1 failed, 49 passed in 3.14s |
| D42 | `` return bad.length ? [`${label}: missing/empty ${bad.join(', ')}`] : [] `` | `` return bad.length ? [`${label}: missing/empty ${bad.join('')}`] : [] `` | KILLED by test_multiple_defects_reports_all_field_names | 1 failed, 49 passed in 3.16s |
| D43 | `synthesis.verdict === 'ready-to-merge' ? 'ready-to-merge' : 'blocked',` | `status: forcedBlockers.length ? 'blocked' : 'ready-to-merge',` (drops the whole second ternary — no longer CONSULTS `synthesis.verdict` at all) | KILLED by test_synth_blocked_verdict_with_no_gate_blockers_still_blocks | 1 failed, 53 passed in 2.29s |
| D44 | `blockers: [...forcedBlockers, ...(synthesis.blockers \|\| [])] }` — **round-7-second-pass re-derived**: the inline `(synthesis.blockers \|\| [])` expression was replaced by the precomputed `synthBlockersList` binding (round-7-second-pass P1 — see D77/E8 below for that binding's own dedicated mutants); this row keeps targeting the drop of the merge-in operand itself, now `synthBlockersList` | `blockers: [...forcedBlockers] }` (drops the merge-in of `synthBlockersList` entirely) | KILLED by test_synth_blockers_survive_gate_forced_override | round-6: 1 failed, 53 passed in 2.24s; **re-run round-7-second-pass: 1 failed, 80 passed** |
| D45 | `? { ...synthesis, verdict: 'blocked', blockers:` | `? { verdict: 'blocked', blockers:` (drops the `...synthesis` spread — `prBody`/`asBuiltEntry` vanish) | KILLED by test_synth_blockers_survive_gate_forced_override (KeyError: 'prBody' — crash-adjacent) | round-6: 1 failed, 53 passed in 2.35s; **re-run round-7-second-pass: 1 failed, 80 passed** |
| D46 | `...(synthesis.blockers \|\| [])` — **SUPERSEDED, round-7-second-pass**: this exact expression no longer exists in the source; the `\|\| []` fallback it named now lives one line earlier, in the `_synthBlockersRaw` binding (`(synthesis && synthesis.blockers) \|\| []`). Re-derived there as **E8** (below) rather than kept as a D-row under the same ID, because — proven by actually running the mutant, not assumed — dropping that `\|\| []` is now an EQUIVALENT mutant: the round-7-second-pass `Array.isArray(_synthBlockersRaw) ? _synthBlockersRaw : []` guard added one line below independently produces `[]` for the OMIT_KEY case too (`Array.isArray(undefined)` is `false`, so the ternary's own `: []` branch fires regardless of whether `\|\| []` ran first). D46's ORIGINAL crash-prevention duty is preserved by that new guard's crash direction, which is D77. Kept here (marked SUPERSEDED rather than deleted) per this document's own stale-anchor rule: a vanished row id must be accounted for, not silently renumbered away. | *(superseded — see E8)* | *(superseded — see E8)* | round-6 (historical, no longer reproducible against current source): 1 failed, 53 passed in 2.26s (crash, not silent misbehavior) |
| D47 | `` [...].filter(Boolean).join(' ') `` (the `notes:` line) | `` [...].join(' ') `` (drops `.filter(Boolean)` — empty-string arms leak stray whitespace) | KILLED by test_notes_join_separates_multiple_fragments_with_single_space (exact-string mismatch: trailing space) | 1 failed, 53 passed in 2.31s |
| D48 | `` [...].filter(Boolean).join(' ') `` (the `notes:` line) | `` [...].filter(Boolean).join('') `` (drops the space separator — fragments run together) | KILLED by test_notes_join_separates_multiple_fragments_with_single_space (exact-string mismatch: missing separator) | 1 failed, 53 passed in 2.48s |
| E1 | `(brief && brief.mayTouch) \|\| []` | `(brief.mayTouch) \|\| []` | EQUIVALENT — unreachable-false: !brief returns at infra('planning') before this line | 48 passed in 2.00s — equivalent as declared |
| E2 | `report && Array.isArray(report.mutations) ? report.mutations : []` — **round-7 re-derived**: now the `builderMutations` binding (round-7 P0; text and reasoning unchanged) | `Array.isArray(report.mutations) ? report.mutations : []` | EQUIVALENT — unreachable-false: !report returns at infra(...) before this line | round-5: 48 passed in 2.08s; **re-run round-7: 75 passed** — equivalent as declared |
| E3 | `` if (!m \|\| typeof m !== 'object') return [`${label}: not an object`] `` | `` if (!m) return [`${label}: not an object`] `` | EQUIVALENT — status-equivalent: a non-null non-object entry still blocks via the field filter (m[k] undefined for every key); only blocker WORDING differs; null case remains covered by D18 | 48 passed in 2.01s — equivalent as declared |
| E4 | `` if (mutationBlockers.length) {   log(`${A.unit}: GATE — mutation evidence missing/malfo... `` | *(line deleted)* | EQUIVALENT — cosmetic: guards a log() call only; no returned field depends on it | 48 passed in 2.03s — equivalent as declared |
| E5 | `status: forcedBlockers.length ? 'blocked' : synthesis.verdict === 'ready-to-merge' ? 'r...` | `status: forcedBlockers.length ? 'blocked' : synthesis.verdict,` | EQUIVALENT — `SYNTH_SCHEMA` (this file, ~line 194, `verdict: { type: 'string', enum: ['ready-to-merge', 'blocked'] }`) is the schema passed to the model call that produces `synthesis` (`{ model: PLANSYNTH, ..., schema: SYNTH_SCHEMA, ... phase: 'Synthesize' }`), constraining `synthesis.verdict` to exactly those two strings before this line ever reads it, so the normalization ternary is a no-op on any value that call can return. CAVEAT (same tension this file's own `test_brief_schema_requires_mutation_plan` names): a *declared* schema is not automatically *enforced* — this file does not run its own ajv-style validation on the returned JSON, so the guarantee rests on whatever the agent-calling layer does with the `schema` field (structured-output constrained decoding, if that's how it's wired) rather than on this file's own code, which is a materially weaker footing than E1/E2 (whose guarantee is an `if (!x) return` two lines up, in this same file, that this file's own tests already exercise). CORRECTED (round-5 third review): an earlier version of this row's reasoning claimed "every fixture this suite has ever driven or could drive through `_scenario`'s `responses.synth.verdict` only ever supplies one of the two enum strings" — that was true when written but became false the moment `_scenario` grew a `synth_verdict` parameter (row D43), and Argus correctly flagged the claim as overstated relative to what the suite actually exercised at the time. Re-run against the resulting 54-test suite, which now drives `synth.verdict` to BOTH `'ready-to-merge'` (the majority of fixtures) and `'blocked'` (`test_synth_blocked_verdict_with_no_gate_blockers_still_blocks`): the mutant still runs green under both values, because the normalization ternary and the raw value agree on both enum members — the equivalence declaration itself stands, only its stated basis was corrected. A future editor should still treat this row's footing as one rung weaker than E1/E2 (schema-constrained, not code-constrained) and re-derive it if the synthesis call stops being schema-constrained. | 54 passed in 2.30s — equivalent as declared (re-verified against both enum values) |
| E6 | `for (const defect of _mutationEntryDefects(mutations[i], i)) {` | `for (const defect of _mutationEntryDefects(mutations[i], i).slice(0, 1)) {` | EQUIVALENT — a property of `_mutationEntryDefects`'s CURRENT body, not of the loop: the function returns at most one element on every path (`bad.length ? [oneString] : []`). Declared here, not silently, precisely so that an edit ever making `_mutationEntryDefects` return 2+ defects (e.g. splitting `bad` into one blocker per field instead of one joined string) hits a visible row requiring re-derivation, instead of leaving this loop looking covered by inheritance from D32-D34. | 50 passed in 2.59s — equivalent as declared |
| D49 | `String(glob).trim().split('/')` (TRAILING side, R3's motivating case) | `String(glob).trimStart().split('/')` | KILLED by test_trailing_whitespace_padded_src_glob_requires_evidence[trailing-space-packages, trailing-tab-packages, trailing-space-tools] — Argus round-6 P1, the round's BAR replay | 3 failed, 58 passed in 2.61s |
| D50 | `String(glob).trim().split('/')` (LEADING side, re-run at operator×direction granularity) | `String(glob).trimEnd().split('/')` | KILLED by test_whitespace_padded_src_glob_requires_evidence | 1 failed, 60 passed in 2.53s |
| D51 | `if (segs[0] !== 'packages' && segs[0] !== 'tools') return false` (operand: `segs[0]` -> `segs[1]`, index-position) | `if (segs[1] !== 'packages' && segs[1] !== 'tools') return false` | KILLED (mass under-gate) by test_evidence_absent_blocks, test_well_formed_evidence_proceeds, test_non_array_mutations_blocks_not_silently_bypassed (+15 more, see suite) | 18 failed, 43 passed in 2.59s |
| D52 | `if (segs[0] !== 'packages' && segs[0] !== 'tools') return false` (comparison-direction flip: `!==` -> `===` on the first disjunct) | `if (segs[0] === 'packages' && segs[0] !== 'tools') return false` | KILLED (mass under-gate) by test_evidence_absent_blocks, test_well_formed_evidence_proceeds, test_non_array_mutations_blocks_not_silently_bypassed (+13 more, see suite) | 16 failed, 45 passed in 2.92s |
| D53 | `if (segs.includes('src')) return true` (`.includes()` absent-direction / over-block: accept unconditionally) | `if (true) return true` | KILLED by test_package_level_non_src_non_globstar_not_required, test_tests_only_unit_not_required, test_docs_only_glob_under_package_not_required | 3 failed, 58 passed in 2.82s |
| D54 | `!segs.includes('tests')` (negation flip, under-gate direction) | `segs.includes('tests')` | KILLED by test_trailing_whitespace_padded_src_glob_requires_evidence[trailing-space-tools], test_package_level_catchall_requires_evidence, test_tests_only_unit_not_required, test_tests_docs_exclusion_is_load_bearing_self_mutation (+2 more, see suite) | 6 failed, 55 passed in 2.63s |
| D55 | `mayTouchList.some(_isSrcScopedGlob)` (`.some()` none-direction, over-block) | `true` | KILLED by test_missing_may_touch_returns_verdict_not_crash, test_non_array_may_touch_blocks_not_crashes[string, object, number], test_non_src_unit_without_evidence_proceeds (+6 more, see suite) | 11 failed, 50 passed in 2.89s |
| D56 | `bad.push('expectRedTests')` (append-LAST order — closed for free by the widened `test_multiple_defects_reports_all_field_names` parametrization, Argus's costs-nothing suggestion) | `bad.unshift('expectRedTests')` | KILLED by test_multiple_defects_reports_all_field_names[target-expectRedTests] | 1 failed, 60 passed in 2.75s |
| D57 | `Array.isArray(_mayTouchRaw) ? _mayTouchRaw : []` (R4 sibling of D15/D16; drop the guard entirely — CRASH direction) | `_mayTouchRaw` | KILLED by test_non_array_may_touch_blocks_not_crashes[string, object, number] via `_run`'s `returncode == 0` assertion — reproduces Argus's round-6 P2 TypeError, the round's second BAR replay | 3 failed, 58 passed in 2.77s |
| D58 | `if (!Array.isArray(_mayTouchRaw)) {` (neuter the new blocker — SILENT UNDER-BLOCK direction) | `if (false) {` | KILLED by test_non_array_may_touch_blocks_not_crashes[string, object, number] via the `status == "blocked"` assertion | 3 failed, 58 passed in 2.74s |
| D59 | `if (!Array.isArray(_mayTouchRaw)) {` (invert the new guard — SPURIOUS OVER-BLOCK direction) | `if (Array.isArray(_mayTouchRaw)) {` | KILLED (mass over-block on every well-formed-array fixture) by test_well_formed_evidence_proceeds, test_non_array_mutations_blocks_not_silently_bypassed, test_missing_may_touch_returns_verdict_not_crash (+16 more, see suite) | 19 failed, 42 passed in 2.74s |
| RES1 | `const forcedBlockers = [...outstandingBlockers, ...mutationBlockers]` (spread ORDER, membership already pinned by D35a/D35b) | `const forcedBlockers = [...mutationBlockers, ...outstandingBlockers]` | ACCEPTED RESIDUAL (survives — message-text ordering) — blocker-array CONTENTS are order-independent as consumed (rendered as a joined/enumerated list, never index-addressed) | 61 passed in 2.95s |
| RES2 | `blockers: [...forcedBlockers, ...(synthesis.blockers \|\| [])] }` — **round-7-second-pass re-derived**: now `blockers: [...forcedBlockers, ...synthBlockersList] }` (spread ORDER, membership already pinned by D44) | `blockers: [...synthBlockersList, ...forcedBlockers] }` | ACCEPTED RESIDUAL (survives — message-text ordering) — same rationale as RES1, one level up the merge | round-6: 61 passed in 2.93s; **re-run round-7-second-pass: 81 passed** |
| D60 | `if (requiresMutationEvidence && builderMutations.length === 0) {` (P0 operand — REVERT-TO-MERGED: read the pre-round-7 merged population instead of the builder's own) | `if (requiresMutationEvidence && mutations.length === 0) {` | **THIS IS THE P0.** KILLED by test_builder_evidence_absent_not_masked_by_fix_evidence (status/blocker-text assertions) — also fails test_gate_is_load_bearing_self_mutation, incidentally: that test's own probe string IS this exact condition text, so mutating it away breaks that test's `count==1` precondition, not a second independent kill | 2 failed, 73 passed in 3.42s |
| D61 | `const mutations = [...builderMutations, ...codexFixMutations]` (drop `builderMutations` operand — membership) | `const mutations = [...codexFixMutations]` | KILLED (mass under-gate: every builder-only-evidence fixture loses its evidence) by test_codex_fix_touching_src_with_fresh_evidence_proceeds, test_codex_fix_non_src_finding_needs_no_evidence, test_codex_fix_declined_src_finding_needs_no_evidence, test_builder_and_fix_entries_reported_separately (+32 more, see suite) | 36 failed, 39 passed in 4.02s |
| D62 | `const mutations = [...builderMutations, ...codexFixMutations]` (drop `codexFixMutations` operand — membership) | `const mutations = [...builderMutations]` | KILLED by test_codex_fix_touching_src_with_fresh_evidence_proceeds, test_codex_fix_mutations_malformed_entry_blocks, test_builder_and_fix_entries_reported_separately | 3 failed, 72 passed in 3.39s |
| RES3 | `const mutations = [...builderMutations, ...codexFixMutations]` (spread ORDER, membership already pinned by D61/D62) | `const mutations = [...codexFixMutations, ...builderMutations]` | ACCEPTED RESIDUAL (survives — same class as RES1/RES2) — `mutations` is consumed only by `.length` (order-independent) and the per-entry shape loop (`_mutationEntryDefects(mutations[i], i)`, which validates each entry independently of which stage produced it or where it landed after the merge); no consumer reads a specific index expecting it to correspond to a specific pre-merge source array | 75 passed in 3.40s |
| D63 | `const codexFixedList = Array.isArray(_codexFixedRaw) ? _codexFixedRaw : []` (R4 sibling of D15/D16/D57; drop the guard entirely — CRASH direction) | `const codexFixedList = _codexFixedRaw` | **THIS IS THE P1.** KILLED by test_codex_fixed_non_array_blocks_not_crashes, test_codex_fixed_malformed_blocks_even_when_declined_triages_all, via `_run`'s `returncode == 0` assertion — reproduces Argus's round-7 P1 TypeError (STEP 10 live reproduction against 81c823e, below) | 2 failed, 73 passed in 3.80s |
| D64 | `if (!Array.isArray(_codexFixedRaw)) {` (neuter the new blocker — SILENT UNDER-BLOCK direction) | `if (false) {` | KILLED by test_codex_fixed_non_array_blocks_not_crashes, test_codex_fixed_malformed_blocks_even_when_declined_triages_all, via the `status == "blocked"` assertion — the discriminating fixture (declined triages the only finding, so this blocker is the ONLY thing standing) makes this direction impossible to hide behind a redundant-looking blocker | 2 failed, 73 passed in 3.51s |
| D65 | `if (!Array.isArray(_codexFixedRaw)) {` (invert the new guard — SPURIOUS OVER-BLOCK direction) | `if (Array.isArray(_codexFixedRaw)) {` | KILLED (mass over-block: `_codexFixedRaw` is `[]`, an array, on every fixture where no Codex fix runs at all) by test_codex_fixed_malformed_blocks_even_when_declined_triages_all, test_codex_fix_well_formed_arrays_produce_no_shape_blockers, test_codex_fix_missing_file_finding_not_evidence_owing[file-key-absent] (+22 more, see suite) | 25 failed, 50 passed in 4.41s |
| D66 | `const codexDeclinedList = Array.isArray(_codexDeclinedRaw) ? _codexDeclinedRaw : []` (R4 twin of D63 — CRASH direction) | `const codexDeclinedList = _codexDeclinedRaw` | KILLED by test_codex_declined_non_array_blocks_not_crashes, via `_run`'s `returncode == 0` assertion. **Real asymmetry found proving this row, stated rather than hidden:** `codexFixedList` is both spread AND `.map()`-ed (`fixedIds`), so a bare STRING offender crashes it (strings have no `.map`); `codexDeclinedList` is ONLY ever spread, and a JS string IS spread-iterable (`[...'C1']` → `['C','1']`, verified, no crash) — the test therefore uses a NUMBER (`declined: 42`; verified `[...42]` throws `TypeError: 42 is not iterable`), not a string, to actually exercise this direction | 1 failed, 74 passed in 4.36s |
| D67 | `if (!Array.isArray(_codexDeclinedRaw)) {` (neuter the new blocker — SILENT UNDER-BLOCK direction) | `if (false) {` | KILLED by test_codex_declined_non_array_blocks_not_crashes, via the `status == "blocked"` assertion | 1 failed, 74 passed in 4.32s |
| D68 | `if (!Array.isArray(_codexDeclinedRaw)) {` (invert the new guard — SPURIOUS OVER-BLOCK direction) | `if (Array.isArray(_codexDeclinedRaw)) {` | KILLED (mass over-block, same shape as D65) by test_codex_declined_non_array_blocks_not_crashes, test_codex_fix_well_formed_arrays_produce_no_shape_blockers, test_codex_fix_missing_file_finding_not_evidence_owing[file-key-absent] (+21 more, see suite) | 24 failed, 51 passed in 4.93s |
| D69 | `mutationGate: { ..., builderEntries: builderMutations.length, fixEntries: codexFixMutations.length, ... }` (P3 — drop `builderEntries` field) | `mutationGate: { required: requiresMutationEvidence, entries: mutations.length, fixEntries: codexFixMutations.length, blockers: mutationBlockers },` | KILLED by test_expect_red_tests_string_form_accepted, test_mixed_real_and_blank_expect_red_element_accepted, test_builder_evidence_absent_not_masked_by_fix_evidence, test_builder_and_fix_entries_reported_separately (+2 more, see suite) — the last of these is the exact-keys pin, D37-class field-vanish killer | 6 failed, 69 passed in 4.04s |
| D70 | `mutationGate: { ..., builderEntries: builderMutations.length, fixEntries: codexFixMutations.length, ... }` (P3 — drop `fixEntries` field, R2 pairing for D69) | `mutationGate: { required: requiresMutationEvidence, entries: mutations.length, builderEntries: builderMutations.length, blockers: mutationBlockers },` | KILLED by test_expect_red_tests_string_form_accepted, test_mixed_real_and_blank_expect_red_element_accepted, test_builder_evidence_absent_not_masked_by_fix_evidence, test_builder_and_fix_entries_reported_separately (+2 more, see suite) | 6 failed, 69 passed in 4.30s |
| D71 | `mutationGate: { ..., builderEntries: builderMutations.length, fixEntries: codexFixMutations.length, ... }` (P3 — swap the two field EXPRESSIONS, membership unaffected) | `mutationGate: { required: requiresMutationEvidence, entries: mutations.length, builderEntries: codexFixMutations.length, fixEntries: builderMutations.length, blockers: mutationBlockers },` | KILLED by test_builder_evidence_absent_not_masked_by_fix_evidence, test_builder_and_fix_entries_reported_separately (+3 more, see suite) — the asymmetric 2/1/3 fixture is what makes a swap visibly wrong rather than accidentally correct | 5 failed, 70 passed in 4.52s |
| D72 | `typeof f.file === 'string' && f.file.trim()` (P2a conjunct — REJECT direction: force it false so even a real src `file` is excluded) | `false` (conjunct replaced) | KILLED by the PRE-EXISTING C1 offender `test_codex_fix_touching_src_without_evidence_blocks` (not a new test this round — this row documents the reject-side of the R2 pairing for the conjunct explicitly) | 1 failed, 74 passed in 7.56s |
| E7 | `typeof f.file === 'string' && f.file.trim()` (P2a conjunct — ACCEPT direction: drop the conjunct, keep only `_isSrcScopedGlob(f.file)`) | `return !!(f && _isSrcScopedGlob(f.file))` | **EQUIVALENT — found honestly while proving this row, not assumed.** `_isSrcScopedGlob` already coerces its argument via `String(glob).trim()`, so a missing (`undefined`) or blank (`"   "`) `file` fails the `packages`/`tools` first-segment check with NO help from this conjunct: `String(undefined)` = `"undefined"`, `String("   ").trim()` = `""` — neither begins with `packages`/`tools`. The conjunct is redundant against every value CODEX_SCHEMA's `file: {type:'string'}` can actually produce; it would only diverge for a non-string value with a custom `.toString()` coercing to a `packages/…`-shaped string, a shape no schema or fixture in this pipeline produces. `test_codex_fix_missing_file_finding_not_evidence_owing` (parametrized: absent, blank) still stands as the BEHAVIORAL PIN for the declared DOES-NOT boundary — it proves the OBSERVABLE behavior at HEAD matches the documented boundary — but, honestly stated, it does not and cannot distinguish "with this conjunct" from "without it" given `_isSrcScopedGlob`'s own defensiveness. Declared per this file's own standing rule that an undeclared equivalent is the same documentation defect as an undeclared offender | 75 passed in 4.18s |

**Round-7-second-pass rows (D73-D84, E8-E9)** — Argus's review of round 7's OWN diff (commit
adding the ratchet + P0/P1/P2/P3 fixes) found four more findings; per this file's own thesis
applied one level deeper, every line that review's fix touches gets its rows in the SAME commit,
not deferred. Full narrative in the "Round 7, continued" subsection below. `test_gate_source_ratchet.py`
fires on every mutant below that sits inside the `GATE-RATCHET BEGIN`/`END` region (all except D73,
named explicitly) — per R5, cited alongside a behavioral killer, never instead of one.

| D73 | `triageFindings = [...withIds(codexFindingsShapeDefect ? [] : _codexFindingsRaw, 'C')]` — **OUT-OF-REGION**: this call site sits ABOVE `GATE-RATCHET BEGIN`, so the ratchet does not fire on this row's mutant (declared explicitly, see §6 DOES-NOT and the round-7-continued honest-bypass paragraph below) | `triageFindings = [...withIds(_codexFindingsRaw, 'C')]` (CRASH direction: drop the shape-defect ternary, let a malformed value reach `withIds`'s unguarded `.map`) | KILLED by test_reviewer_findings_non_array_blocks_not_crashes via `_run`'s `returncode == 0` assertion — reproduces the crash Argus's second-pass review found live at round-7 HEAD (`"bad".map is not a function`) | 1 failed, 80 passed in 4.68s |
| D74 | `if (codexFindingsShapeDefect) {` (the new REVIEWER-FINDINGS GATE guard — neuter the blocker, SILENT UNDER-BLOCK direction) | `if (false) {` | KILLED by test_reviewer_findings_non_array_blocks_not_crashes via the `status == "blocked"` assertion | 1 failed, 80 passed in 4.93s |
| D75 | `if (codexFindingsShapeDefect) {` (invert the guard, SPURIOUS OVER-BLOCK direction — fires on every normal run where `codexFindingsShapeDefect` is `null`) | `if (!codexFindingsShapeDefect) {` | KILLED (mass over-block) by test_well_formed_evidence_proceeds, test_missing_may_touch_returns_verdict_not_crash, test_expect_red_tests_string_form_accepted (+21 more, see suite) | 24 failed, 57 passed in 4.81s |
| D76 | `const forcedBlockers = [...outstandingBlockers, ...mutationBlockers, ...reviewerShapeBlockers]` (THIRD operand — membership: drop `reviewerShapeBlockers`, i.e. revert to the round-7-first-pass two-operand form) | `const forcedBlockers = [...outstandingBlockers, ...mutationBlockers]` | KILLED by test_reviewer_findings_non_array_blocks_not_crashes (with the guard intact and the blocker computed but never merged in, the scenario silently reaches `ready-to-merge`) | 1 failed, 80 passed in 4.64s |
| D77 | `const synthBlockersList = Array.isArray(_synthBlockersRaw) ? _synthBlockersRaw : []` (R4 sibling of D15/D16/D57/D63/D66; drop the guard entirely — CRASH direction) | `const synthBlockersList = _synthBlockersRaw` | KILLED by test_synth_blockers_non_array_blocks_not_crashes via `_run`'s `returncode == 0` assertion — reproduces Argus's second-pass P1 (`[...forcedBlockers, ...(synthesis.blockers \|\| [])]` throwing on a truthy non-array `synthesis.blockers`) at round-7-first-pass HEAD. No neuter/invert sibling rows exist for this guard (unlike D57-D59/D63-D65/D66-D68): this guard has no companion BLOCKER to neuter or invert — the declared design choice (written at the binding's own comment and in the round-7-continued subsection below) is that a malformed `synthesis.blockers` needs no blocker of its own, because `forcedBlockers` — the actual gate decision — is already fully computed and independent of it | 1 failed, 80 passed in 4.71s |
| E8 | `const _synthBlockersRaw = (synthesis && synthesis.blockers) \|\| []` (the `\|\| []` fallback — this is D46's ORIGINAL anchor, moved here by the round-7-second-pass refactor; D46 above is marked SUPERSEDED and points here) | `const _synthBlockersRaw = (synthesis && synthesis.blockers)` (drop the `\|\| []` fallback) | **EQUIVALENT — verified by direct harness run against the OMIT_KEY scenario, not assumed.** For the omitted-`blockers`-key case, `synthesis.blockers` is JS `undefined`; `synthesis && undefined` is `undefined`; WITHOUT the `\|\| []`, `_synthBlockersRaw` becomes `undefined` directly. The very next line's `Array.isArray(_synthBlockersRaw) ? _synthBlockersRaw : []` (D77's guard, added THIS round) independently produces `[]` for `undefined` too, because `Array.isArray(undefined)` is `false`. So the two guards now overlap on exactly the OMIT_KEY case: D46's original crash-prevention duty for that case is fully subsumed by D77's guard, verified by re-running `test_synth_blockers_key_omitted_does_not_crash_gate_override` (still green) and by a direct one-off harness invocation on the OMIT_KEY scenario under this exact mutant, observing `"status":"blocked"` with no crash and the correct blocker text, byte-for-byte. This is the kind of coupling this file's own thesis says must be re-verified, not assumed, whenever a hardening pattern is layered on top of an existing one — and here the layering happened to make one of the two guards redundant for one (but only one) of the failure modes each was written for. | 0 failed, 81 passed — plus a direct harness invocation on the OMIT_KEY scenario (see round-7-continued subsection) |
| D78 | `const codexFixMutations = codexFixes && Array.isArray(codexFixes.mutations) ? codexFixes.mutations : []` (declared-unhardened sibling #2, R4 — drop the `Array.isArray` guard entirely, letting a malformed value flow through raw) | `const codexFixMutations = (codexFixes && codexFixes.mutations) \|\| []` | KILLED by test_codex_fix_mutations_malformed_non_array_still_blocks_via_emptiness_check — **but the VERDICT does not flip** (confirmed by a direct harness run: `status` stays `"blocked"` under the mutant too), only the SPECIFIC blocker text/mechanism does. With the guard removed, `codexFixMutations` becomes the raw string `"see PR"` (truthy, so the `\|\| []` never fires); `codexFixMutations.length` is `6` (not `0`), so the pre-existing "no refreshed mutations[] evidence" blocker no longer fires — but `mutations = [...builderMutations, ...codexFixMutations]` now spreads the STRING's characters as six bogus entries (`[...'see PR']`), each of which fails `_mutationEntryDefects`'s `typeof m !== 'object'` check, producing SIX "not an object" blockers instead of the one expected blocker. Net effect: still blocked (fail-closed twice over, by two different mechanisms), `mutationGate.fixEntries` inflated to `6` instead of the honest `0`, and the specific blocker text this round's own comment describes does not appear. This is the message-text/count-cosmetics residual class (not a protected direction: no under-block, no over-block, no crash) — filed as a residual in the round-7-continued subsection, not promoted to a fix, per the stopping rule. This row supersedes the earlier "not exploitable" framing in the code comment insofar as it shows the SPECIFIC mechanism claimed there is not the only backstop; the comment's core claim (fail-closed) still holds, verified now by execution rather than reasoning alone. | 1 failed, 80 passed in 4.82s — plus a direct harness run confirming `status: "blocked"`, `fixEntries: 6`, six `"not an object"` blockers |
| D79 | `const fixedIds = codexFixedList.map((t) => (typeof t === 'string' ? t : t && t.id)).filter(Boolean)` (string-tolerance ternary — REJECT/offender direction: drop the string-form branch entirely, mirroring `triagedIds`'s identical ternary one binding up) | `const fixedIds = codexFixedList.map((t) => t && t.id).filter(Boolean)` | KILLED by test_fixed_finding_as_bare_string_id_recognized_for_evidence — **new coverage, not merely a new row for old behavior**: no fixture before this round supplied `fixed[]` as bare strings (every prior fixture used `{id, note}` objects), so this direction was previously UNTESTED, not merely unrowed. The accept-side control, test_fixed_finding_as_bare_string_id_with_fresh_evidence_proceeds, is unaffected by this mutant (a bare-string `"C1"` with fresh evidence supplied proceeds either way, since `codexFixMutations` — the evidence count — does not depend on `fixedIds` at all) | 1 failed, 80 passed in 4.84s |
| E9 | `const fixedIds = codexFixedList.map((t) => (typeof t === 'string' ? t : t && t.id)).filter(Boolean)` (`.filter(Boolean)` — drop it) | `const fixedIds = codexFixedList.map((t) => (typeof t === 'string' ? t : t && t.id))` | **EQUIVALENT — verified by full-suite run, not assumed.** `withIds` (top of file) stamps every finding id as `` `${prefix}${i + 1}` `` (e.g. `"C1"`, `"C2"`) — never empty, never falsy. A malformed `fixed[]` entry that resolves to `undefined`/`null`/`false` under the ternary (an object with no `.id`, or a non-string non-object entry) can therefore NEVER equal a real `triageFindings[].id`, so `fixedSrcTouchingIds`'s own `.find((tf) => tf.id === id)` already returns `undefined` for it regardless of whether `.filter(Boolean)` removed it from `fixedIds` first. The filter is redundant against every id `withIds` can actually produce; it would only diverge if `triageFindings` ever contained an entry with a falsy `id`, a shape `withIds` structurally cannot produce. | 0 failed, 81 passed |
| D80 | `const f = triageFindings.find((tf) => tf.id === id)` (predicate — comparison-direction flip) | `const f = triageFindings.find((tf) => tf.id !== id)` | KILLED (mass under-gate on every single-finding fixture, where "not this one" resolves to "no match" and `fixedSrcTouchingIds` empties out) by test_codex_fix_touching_src_without_evidence_blocks, test_codex_fix_mutations_malformed_non_array_still_blocks_via_emptiness_check, test_fixed_finding_as_bare_string_id_recognized_for_evidence | 3 failed, 78 passed in 4.70s |
| D81 | `return !!(f && typeof f.file === 'string' && f.file.trim() && _isSrcScopedGlob(f.file))` (the `_isSrcScopedGlob(f.file)` conjunct, DISTINCT from D72/E7's `typeof f.file === 'string' && f.file.trim()` conjunct one segment over — drop it, OVER-BLOCK direction: any fixed finding with a non-blank `file`, src-scoped or not, now counts as src-touching) | `return !!(f && typeof f.file === 'string' && f.file.trim())` | KILLED by test_codex_fix_non_src_finding_needs_no_evidence (a docs-file fix now wrongly demands refreshed evidence) | 1 failed, 80 passed in 4.68s |
| D82 | `if (fixedSrcTouchingIds.length && codexFixMutations.length === 0) {` (drop the `fixedSrcTouchingIds.length` operand — OVER-BLOCK direction: demand evidence for every fix regardless of whether it touched src) | `if (codexFixMutations.length === 0) {` | KILLED (mass over-block: every fix that supplies no fresh `mutations[]` now blocks, including non-src and declined-only fixtures) by test_well_formed_evidence_proceeds, test_missing_may_touch_returns_verdict_not_crash, test_synth_blocked_verdict_with_no_gate_blockers_still_blocks (+18 more, see suite) | 21 failed, 60 passed in 4.47s |
| D83 | `if (fixedSrcTouchingIds.length && codexFixMutations.length === 0) {` (drop the `codexFixMutations.length === 0` operand — OVER-BLOCK direction: demand evidence even when fresh evidence WAS supplied) | `if (fixedSrcTouchingIds.length) {` | KILLED by test_codex_fix_touching_src_with_fresh_evidence_proceeds, test_builder_and_fix_entries_reported_separately, test_codex_fix_well_formed_arrays_produce_no_shape_blockers (+2 more, see suite) | 5 failed, 76 passed in 4.25s |
| D84 | `if (fixedSrcTouchingIds.length && codexFixMutations.length === 0) {` (comparator flip — UNDER-BLOCK direction: block only when evidence WAS supplied, the exact inverse of the intended rule) | `if (fixedSrcTouchingIds.length && codexFixMutations.length !== 0) {` | KILLED (under-block: every src-touching fix with NO evidence now silently proceeds) by test_codex_fix_touching_src_without_evidence_blocks, test_codex_fix_mutations_malformed_non_array_still_blocks_via_emptiness_check, test_fixed_finding_as_bare_string_id_recognized_for_evidence (+2 more, see suite) | 8 failed, 73 passed in 4.34s |

**Round-7 numbering note:** rows D60-D72, RES3, and E7 were added by a FIFTH review round (round 7) against the resulting 75-test suite (14 new offender tests — `test_builder_evidence_absent_not_masked_by_fix_evidence`, `test_builder_and_fix_entries_reported_separately`, `test_codex_fixed_non_array_blocks_not_crashes`, `test_codex_declined_non_array_blocks_not_crashes`, `test_codex_fixed_malformed_blocks_even_when_declined_triages_all`, `test_codex_fix_well_formed_arrays_produce_no_shape_blockers`, and `test_codex_fix_missing_file_finding_not_evidence_owing` parametrized x2) — and, per that round's own R5, every KILLED row above names a BEHAVIORAL test as its killer, never the gate-source ratchet alone; the ratchet additionally fires on every mutant in this block (all of them sit inside the `GATE-RATCHET BEGIN`/`END` region) but is not cited as a killer for any individual row, per R5's own text ("a mutant killed only by the ratchet is UNCOVERED"). D15/D16/D30/D31/D37/E2 were RE-DERIVED in place this round (anchor text updated to the current binding names, verdict unchanged, both the original and the re-run counts kept for the historical record) rather than duplicated under new IDs, per this document's own stale-anchor rule.

**Round-7-second-pass numbering note:** rows D73-D84, E8, and E9 were added by a SIXTH review round (Argus's review of round 7's OWN diff — the ratchet-adding commit itself had four more findings on its own new code) against the resulting 81-test suite (6 new tests — `test_synth_blockers_non_array_blocks_not_crashes`, `test_reviewer_findings_non_array_blocks_not_crashes`, `test_reviewer_findings_well_formed_produces_no_shape_blocker`, `test_codex_fix_mutations_malformed_non_array_still_blocks_via_emptiness_check`, `test_fixed_finding_as_bare_string_id_recognized_for_evidence`, `test_fixed_finding_as_bare_string_id_with_fresh_evidence_proceeds`). Every KILLED row's Run-result cell is a real mutation-gate suite execution against the mutated real workflow file, applied then reverted (verified byte-identical after each restore); D78 and E8 additionally carry a direct one-off dry-run invocation of the workflow because their claims are about the exact JSON shape (blocker text, field values), not just pass/fail counts. D35a/D35b/D44/D45/RES2 were RE-DERIVED in place (three-operand `forcedBlockers`, `synthBlockersList` replacing the inline `(synthesis.blockers || [])` expression); D46 is marked SUPERSEDED and points to its new home at E8, rather than silently vanishing, per this document's own stale-anchor rule. Per R5, none of D73-D84 cites the gate-source ratchet as a killer even though it fires on every in-region mutant among them (all except D73, which is explicitly the one OUT-OF-REGION row in this batch).

**Round-6 correction note (R3, applied retroactively to D1/D2):** the round-5 table's own framing described D1 and D2 as covering `String(glob).trim()` — true of the EXPRESSION but overstated at the DIRECTION granularity R3 now requires: both D1 (`glob.trim().split('/')`, dropping the `String()` coercion) and D2 (`String(glob).split('/')`, dropping `.trim()` entirely) are LEADING-side-only mutants, and neither says anything about the trailing side. The test file's own `test_whitespace_padded_src_glob_requires_evidence` docstring made the same overstatement in prose ("padded with leading/trailing whitespace") for a fixture (`' packages/ne-x/src/**'`) that is leading-only — corrected in place this round (see the test file, cited there as "ROUND-6 CORRECTION"). D49/D50 above are the trailing-side rows D1/D2 should have had from the start; nothing about D1/D2's own verdicts changes, only the completeness claimed around them.

Permanent self-mutation probes (in-suite, not part of the STEP-5 campaign above,
but covering the same lines — credited here for completeness):

| Test | Line it probes | What it proves |
|---|---|---|
| `test_gate_is_load_bearing_self_mutation` | `if (requiresMutationEvidence && mutations.length === 0) {` | neutering to `if (false) {` lets evidence-absent proceed (bidirectional partner of D30/D31/E-adjacent rows) |
| `test_tools_scope_disjunct_is_load_bearing_self_mutation` | `if (segs[0] !== 'packages' && segs[0] !== 'tools') return false` | dropping the `tools` disjunct un-gates `tools/*/src` (permanent partner of D5) |
| `test_tests_docs_exclusion_is_load_bearing_self_mutation` | `return segs.includes('**') && !segs.includes('tests') && !segs.includes('docs')` | reducing to bare `segs.includes('**')` force-blocks tests-only units (permanent partner of D8/D9/D10) |

**What this method does and does not give you.** The table above bounds mutant coverage of
the branches that **exist** in today's gate — it says nothing about a branch nobody wrote, and
nothing about the semantic validity of filed evidence (still Argus's independent re-run duty,
per the DOES-NOT list above). It does not enlarge the gate's matched surface: the
`packages/`/`tools/`-only scope hole (deploy/, demo/, flat non-`src/` tools/* trees) and the
mayTouch-not-diff bypass, both named earlier in this section, are unchanged by this round — R1
and R2 make the enumerable surface converge, they do not grow it. The equivalence declarations
E1/E2 depend on the early-return guards upstream of the gate (`if (!brief) return
infra('planning')` and the equivalent `!report` check) staying in place; moving the gate above
those returns invalidates both declarations, and whoever does that refactor must re-derive
those two rows, not carry them forward unread. Anchors go stale on refactor for the same
reason: when an anchor no longer matches the source verbatim, the correct response is to
re-derive that row's coverage from the new code, not to mechanically re-point the anchor at
whatever text now sits near the old line number (the same discipline the test file's own
`count(cond) == 1` self-mutation guards already apply to their probe strings). This bounds
coverage of the branches that exist; a reviewer can still find defects in branches nobody
wrote, or in the semantic validity of evidence filed through a well-formed gate — the table
makes the next round unnecessary only for the enumerable surface it covers.

**One more limit, found the hard way rather than named up front: the enumerated surface was
originally "every branch, boolean operand, and loop bound" — and that phrase itself has a
boundary.** It does not, on its face, reach EXPRESSION-level operations sitting inside a
branch or a loop body: string joins, array slices/truncations, and index/argument values
passed to a helper are none of "a branch", "a boolean operand", or "a loop bound" in the
ordinary reading of those three nouns. That gap is exactly where the `bad.join(', ')`
aggregation survivor (D40-D42 above) came through — the surrounding `bad.length ?` ternary
was enumerated (D29a/D29b) and pinned, but what the ternary's true-branch actually *builds*
was not. Rows D34 (a helper's index argument) and D40-D42 (a collection aggregation) are
now in the table, but they were added because a review found the seam, not because the
original enumeration pass looked for expression-level mutants as a category. Read "every
branch, boolean operand, and loop bound" as the CONDITIONS the method's first pass actually
enumerated, not as a guarantee that every expression reachable from those conditions was
independently considered — the honest position is that this document's enumeration is
demonstrably better at finding branches than at finding what a branch's body computes, and a
future editor extending this table should deliberately ask "what does this line COMPUTE, not
just which way does it BRANCH" rather than trust the three-noun phrase to have already covered it.

**The sweep was named but not finished the first time — a third round closed the rest of the
same return statement.** The paragraph above named the expression-level category the moment
`bad.join(', ')` was found unpinned, but the fix that round applied was scoped to the two
instances a reviewer had actually pointed at (D34, D40-D42), not to every instance of that
category already sitting in the same ~25-line `return { ... }` block. Rows D43-D48 are four
more members of the identical category, in the identical block: `status:`'s ternary CONSULTING
`synthesis.verdict` at all (as opposed to E5's narrower question of whether the equality-check
inside that consultation is redundant), and three operands of the gate-forced `synthesis:`
override object (the `synthesis.blockers` merge, the `...synthesis` spread, and the `|| []`
fallback guarding it). All four had the same root cause as `bad.join(', ')`: `_scenario`'s
fixtures drove `synth.verdict` to one value and `synth.blockers` to one shape, so the
expressions built from them were never actually exercised across more than that one value or
shape — R1 restated for expressions rather than collections. `_scenario` now takes
`synth_verdict`, `synth_blockers` (including an `OMIT_KEY` sentinel for a genuinely absent
key, not `[]` or `null`), `synth_pr_body`, and `synth_as_built_entry` parameters so this class
of gap has fixture support going forward. This sweep is complete for the return statement AS
IT EXISTS TODAY — it is not a promise that no future edit to that block can introduce the same
shape of gap; R1/R2 and the "what does this line COMPUTE" question above are the standing
discipline for whoever edits it next, not this paragraph.

### Accepted residual — message-text ordering (round 6)

Round 6's re-sweep (D49-D59 above) found two mutants that genuinely survive and, unlike every
prior survivor in this file's history, are declared closed rather than chased: **RES1**
(`[...outstandingBlockers, ...mutationBlockers]` with its two spreads swapped) and **RES2**
(`[...forcedBlockers, ...(synthesis.blockers || [])]` with its two spreads swapped) — plus a
third member, the `bad.push('expectRedTests')` -> `bad.unshift('expectRedTests')` field-order
mutant, which the widened `test_multiple_defects_reports_all_field_names` parametrization
(row D56) killed for free this round, shrinking the class to the two spread-order rows. All
three reorder operator-facing blocker **TEXT** only — which fields/blockers appear in the
output, i.e. **membership**, is separately pinned by D35a/D35b (does `forcedBlockers` drop
either source array) and D44 (does the gate-forced override drop `synthesis.blockers`), and
whether the unit **blocks at all** is pinned by D30 (the gate condition) and D36 (the `status`
ternary) — none of which any of these three mutants touch. A caller of this gate reads
`status` (blocked/ready-to-merge) and searches `blockers` for content by substring/prefix
(`MUTATION-EVIDENCE GATE:`, `UNTRIAGED`), never by array position, so **no member of this
class can under-block, spurious-over-block, or crash** — the three failure directions this
unit's stopping rule (below) treats as correctness defects. Declared here, not silently, per
this file's own standing rule that an undeclared equivalent/residual is the same documentation
defect as an undeclared offender. **Filed to NE-VG-3** (the wave-2 sweep) for any future
promotion — e.g. if a caller is ever added that DOES read `blockers[0]` positionally, these
rows must be re-derived, not left declared-residual by inheritance.

### Stopping rule (round 6, final)

This is round 6 of a unit whose subject is guards that do not guard, and the recursion has a
real cost: five prior rounds (10 -> 17 -> 36 -> 48 -> 54 -> 61 tests) each closed exactly what
a reviewer's imagination found that round, and round 5's own method change — enumerate the
branches instead of waiting for a reviewer — still left a granularity gap (P1) and a sibling
gap (P2) for round 6 to find. **After this round, this unit ships unless a future finding is a
correctness defect in one of exactly three directions: silent UNDER-blocking (a real defect
proceeds unguarded), spurious OVER-blocking (a compliant unit is force-blocked with no way to
satisfy the gate), or a CRASH instead of a verdict (the pipeline returns no result at all).**
Message text, label formatting, ordering, and doc phrasing — the accepted-residual class
immediately above is the concrete instance — are declared residuals filed to NE-VG-3, not
grounds for a round 7. **Rationale:** an inventory that must be perfect before it ships never
ships, and a gate that exists imperfectly beats a gate that is still being polished while
units merge without one. The three-directions test is not an arbitrary cutoff; it is exactly
the set of failure modes this file's own thesis (§1, §6 DOES/DOES-NOT) treats as defeating the
gate's PURPOSE — a cosmetic gap in blocker text does not.

### Round 6 FIX-stage — the stopping rule applied to Codex's own review (C1 fixed, C2 declined)

The stopping rule above was written for "a future finding," not narrowly for another Argus
round — and Codex's mandatory cross-review of the round-6 commit (Stage 3 of
`ne-unit-workflow.mjs`, always run, independent of the Argus loop the stopping rule was
capping) landed two findings on the round-6 diff itself. Applying the three-directions test to
each, as written:

- **C1 — FIXED.** `report.mutations` (used by the DETERMINISTIC MUTATION-EVIDENCE GATE) is
  captured in Stage 2 (the builder), *before* Stage 3 (Codex cross-review) runs. A load-bearing
  Codex-driven fix landing on `packages/*/src` or `tools/*/src` surface could reach
  ready-to-merge covered only by evidence that predates the fix — the emptiness check
  (`requiresMutationEvidence && mutations.length === 0`) never fires because the STALE pre-fix
  count is already non-zero. That is silent UNDER-blocking of SPEC §6.1 for exactly the code
  the fix touched — one of the three protected directions, so the stopping rule does not cover
  it. Closed by: `FIX_SCHEMA` gains an optional `mutations` field (the same
  `MUTATION_ENTRY_SCHEMA` the builder uses, factored out so both stages share one shape); any
  `codexFixes.mutations` supplied is merged into the SAME `mutations` array the pre-existing
  per-entry shape validator (`_mutationEntryDefects`) already walks (one validator, not two
  that could silently diverge); and a FIXED finding whose `file` lands on src-scoped surface
  (checked with the SAME `_isSrcScopedGlob` the gate already uses against `brief.mayTouch` —
  R4 sibling symmetry, one function reused rather than a second hand-rolled heuristic) with
  zero refreshed evidence is its own new blocker. Six new tests in
  `test_workflow_mutation_gate.py` (`test_codex_fix_touching_src_without_evidence_blocks` and
  siblings) pin the block, the accept-side control (fresh evidence supplied, `mutationGate
  .entries` reflects the merge), the non-src-file control (no over-block), the declined-not-fixed
  control, the malformed-fresh-entry rejection via the shared validator, and a schema-text pin.
- **C2 — DECLINED, filed to NE-VG-3.** Codex observed that the SYNTHESIS prompt never receives
  `report.mutations` or `codexFixes.mutations`, so synthesis "may omit or invent" the PR-body
  mutation table. Real gap in output fidelity — but the DETERMINISTIC gates (outstanding-finding
  gate, mutation-evidence gate, C1's new check above) all run *after* the synthesis call and
  read `report`/`codexFixes` directly in code; the final `status:` line is `forcedBlockers
  .length ? 'blocked' : synthesis.verdict === 'ready-to-merge' ? ... : 'blocked'` — a synthesis
  PR-body table that is incomplete or invented cannot change whether the unit blocks or
  proceeds, only the fidelity of prose describing evidence the code has *already* independently
  verified before that prose is ever read. None of the three protected directions apply: no
  verdict can silently under-block, no compliant unit can be spuriously over-blocked, and
  nothing crashes — it is exactly the "message text... doc phrasing" carve-out the stopping
  rule already names. Declared here per this file's own standing rule that an undeclared
  residual is the same documentation defect as an undeclared offender; filed to NE-VG-3 for
  promotion if a future caller ever treats synthesis's PR body as a verification source rather
  than a narrative artifact.

### Round 7 — the inventory becomes mechanical (the gate-source ratchet)

**WHY.** Round 6's stopping rule shipped the unit with a clean stopping condition (§ "Stopping
rule (round 6, final)" above) — but the fix commit that closed C1 (the previous section) added
roughly fifteen lines of new gate logic with **zero** decision-point-inventory rows, and TWO of
those unrowed lines shipped protected-direction defects, found by Argus's review of that same
commit:

- **P0 — silent under-blocking.** C1's fix merged builder-evidence and fix-stage evidence into
  one `mutations` array and read `mutations.length` for the builder-emptiness check. A Codex fix
  stage volunteering ONE `mutations[]` entry — even for a fix that owed no evidence at all (a
  docs-only fix) — silently satisfied the check for a builder that filed ZERO evidence. **Proven
  live against commit `81c823e`** (pre-round-7 HEAD): the scenario in
  `test_builder_evidence_absent_not_masked_by_fix_evidence` (builder `mutations: []` on a
  src-scoped unit; Codex finds a typo in `docs/testing/vacuous-guards.md`; the fix stage
  volunteers one well-formed `mutations[]` entry for that unrelated docs fix) returns
  `"status":"ready-to-merge"` against `81c823e` — recorded verbatim:
  `{"status":"ready-to-merge", ..., "mutationGate":{"required":true,"entries":1,"blockers":[]}}`.
  Against round-7 HEAD the identical scenario returns `"status":"blocked"` with
  `"builderEntries":0,"fixEntries":1` and the pre-existing evidence-absent blocker text.
- **P1 — crash instead of a verdict**, the FOURTH R4 instance (round 5's `ertOk` array/string
  siblings; round 6's own `.trim()` leading/trailing sides, D49/D50; round 6's own
  `_mayTouchRaw`/`report.mutations` siblings, D57-D59; now this — **committed by the very edit
  that made R4 binding**). C1's fix hardened `_mayTouchRaw` against a truthy non-array value
  while leaving its FIX_SCHEMA structural twins, `codexFixes.fixed` and `codexFixes.declined`,
  spread bare into `.map`/spread with no type guard. **Proven live against `81c823e`:** the
  scenario in `test_codex_fixed_non_array_blocks_not_crashes` (`codexFixes.fixed: "C1"`, a bare
  string instead of `[{id:"C1"}]`) crashes the pipeline —
  ```
  TypeError: ((codexFixes && codexFixes.fixed) || []).map is not a function
      at eval (... ne-unit-workflow.mjs:479:59)
  Node.js v22.23.1
  RC=1
  ```
  — no result object at all. Against round-7 HEAD the identical scenario returns RC=0,
  `"status":"blocked"`, with blocker text
  `"MUTATION-EVIDENCE GATE: codexFixes.fixed is not an array (typeof string) — cannot credit fix
  triage; blocking rather than crashing or silently un-crediting (FIX_SCHEMA text is not runtime
  enforcement; R4)"`.

Both defects sit precisely on the fifteen unrowed lines — this is the
[[written-rules-disarm-review]] pattern one level up: a rule in a docstring/spec shifts review
from "is this right?" to "does the code match?", and R3/R4 (this file's own §6 rules) are exactly
such written rules. Six rounds of this file's history say a human-followed inventory rule does not
survive the next edit — including, this round proves, the edit that WRITES the rule. **The method
(enumerate every branch/operand/loop-bound, R1/R2/R3/R4) was never wrong; the discipline of
actually running it before every commit is what failed, twice, in the same commit.**

**THE MECHANISM.** The gate-source ratchet test hashes the gate block of
the unit workflow — the region strictly between two sentinel comments,
`// ---- GATE-RATCHET BEGIN ---- ... ---- END ----`, which now bracket BOTH the
outstanding-finding gate and the mutation-evidence gate — and compares that hash to a
`GATE-SOURCE-PIN: sha256=<hex>` line living **inside this file**, next to the D-table it exists to
force into every gate-touching diff:

- **Sentinels, not line numbers**, because line numbers drift on any unrelated edit above the
  gate (the same reason the D-table already anchors on verbatim code fragments, not line
  numbers).
- **Exclusive region** (sentinel lines themselves excluded) — a mutation ON a sentinel line (an
  edit purely to sentinel wording) does not itself trip the pin; a mutation removing/duplicating/
  reordering a sentinel is caught by a SEPARATE, unconditional structural check, run before the
  hash is ever computed.
- **Normalization: `line.rstrip()` per line, joined, trailing `\n`.** Trailing whitespace is
  insensitive (a line-ending edit from an editor's whitespace-trim setting does not falsely trip
  the ratchet); leading whitespace (indentation) IS sensitive (a re-indent is a real content
  change a reviewer should see) — both directions tested
  (`test_ratchet_trailing_whitespace_insensitive` / `test_ratchet_leading_whitespace_sensitive`).
- **Four anti-shrink anchors** (`DETERMINISTIC OUTSTANDING-FINDING GATE`,
  `DETERMINISTIC MUTATION-EVIDENCE GATE`, `mutationGate:`, `status: forcedBlockers.length`) must
  each appear inside the region — checked BEFORE the hash comparison, so moving a sentinel to
  shrink the region and then updating the pin to match the (now-smaller) hash still fails: the
  anchor check binds independently of whether the pin happens to agree
  (`test_ratchet_red_when_required_anchor_absent`).
- **The pin lives next to the table it forces you to update** — an editor who touches the gate
  region must, in the same diff, touch `docs/testing/vacuous-guards.md` (to move the pin), which
  is exactly the file with the D-table — so the inventory file is structurally IN the diff
  whether or not the editor remembers to add rows to it.

**Option (a) — content pin — was chosen over (b) — structural parsing** — per the orchestrator's
framing: a pin cannot mis-parse. A parser that tried to extract "decision points" from JS source
would be new code with its own failure surface, exactly the recursive version of the defect class
this whole unit exists to close. A hash has no such surface: it either matches or it doesn't.

**THE HONEST BYPASS.** A content pin proves the SOURCE moved; it cannot judge whether the ROWS
added in response are any good. An editor who moves a sentinel (or edits it) and updates the pin
without adding real rows is **not mechanically prevented** — the ratchet's guarantee is narrower
than that: it forces the inventory FILE into every gate-touching diff and makes a silent skip
VISIBLE (a diff touching the gate with no corresponding doc changes is impossible to produce), not
that it forces the rows to be good. Judging row quality remains Argus's review duty, same as
every other round. Sentinel comment WORDING itself is unpinned, cosmetic text — only the sentinel
TOKENS (`GATE-RATCHET BEGIN`/`END`) and the enclosed region's bytes are load-bearing.

**NEW BINDING RULE R5.** *The ratchet's kill never counts as coverage: in any mutant campaign the
gate-source ratchet test is excluded from kill attribution; a mutant killed only by the ratchet is
UNCOVERED and needs a behavioral offender.* Every mutant in the D60-D72/RES3/E7 campaign below
also flips the ratchet RED (all of them sit inside the sentinel region) — none is cited as "killed
by the ratchet" for exactly this reason: crediting the ratchet as a behavioral killer would let
the ratchet itself become the ultimate vacuous guard this whole unit was built to close (a test
whose passing tells you nothing about behavior, only about bytes).

**Ratchet self-coverage** — every ratchet failure mode maps to a test in
the gate-source ratchet suite:

| Failure mode | Test |
|---|---|
| Gate edited, inventory not updated (the mandated offender) | `test_ratchet_red_on_gate_edit_without_inventory_update` |
| Gate + inventory updated together (the mandated control) | `test_ratchet_green_when_gate_and_pin_change_together` |
| Pin edited alone (bidirectional coupling) | `test_ratchet_red_on_pin_update_without_gate_change` |
| Failure message carries the actual hash + row instruction | `test_ratchet_failure_message_carries_actual_hash_and_row_instruction` |
| BEGIN sentinel deleted / duplicated | `test_ratchet_red_on_sentinel_defects[begin-deleted]` / `[begin-duplicated]` |
| END sentinel deleted / duplicated | `test_ratchet_red_on_sentinel_defects[end-deleted]` / `[end-duplicated]` |
| END sentinel moved above BEGIN | `test_ratchet_red_on_sentinel_defects[end-moved-above-begin]` |
| Pin line missing / duplicated / malformed | `test_ratchet_red_when_pin_line_missing_duplicated_or_malformed[missing/duplicated/malformed]` |
| Required anchor removed (region-shrink bypass) | `test_ratchet_red_when_required_anchor_absent` (x4, one per anchor) |
| Trailing whitespace inside region (should NOT trip) | `test_ratchet_trailing_whitespace_insensitive` |
| Leading whitespace / re-indent inside region (SHOULD trip) | `test_ratchet_leading_whitespace_sensitive` |
| Edits outside the region (should NOT trip) | `test_ratchet_ignores_edits_outside_region` |
| Live enforcement (real files, zero defects) | `test_ratchet_green_at_head` |

### Round 7, continued — Argus's review of round 7's own diff

The round that made the inventory mechanical was itself reviewed by the standard it exists to
enforce — [[written-rules-disarm-review]] one level up, again: the ratchet's own author is not
exempt from the discipline the ratchet exists to impose. Four findings, all on round 7's own new
code:

- **P1 — crash instead of a verdict, at the moment the gate had ALREADY decided BLOCKED.**
  `blockers: [...forcedBlockers, ...(synthesis.blockers || [])]` spread `synthesis.blockers`
  bare. `synthesis.blockers = 42` (or an object) threw `TypeError: ... is not iterable`, RC=1,
  no result object — reproduced live against round-7-first-pass HEAD. The branch is reached ONLY
  when `forcedBlockers.length` is truthy, so the failure mode was specifically: the deterministic
  gate correctly decides to BLOCK, then crashes instead of emitting that decision. SYNTH_SCHEMA's
  `blockers: {type:'array', ...}` is schema TEXT, not runtime enforcement — the same argument this
  round already used twice for `codexFixes.fixed`/`.declined`. This was an UNDECLARED R4 sibling:
  the round's own comments named one unhardened sibling (the line-364 recheck-trigger check) with
  a written reason, but said nothing about this one, four lines below the new blockers, inside the
  region that same commit pinned. Fixed with the established coerce-only pattern (D77): a
  `_synthBlockersRaw`/`synthBlockersList` binding, `Array.isArray`-guarded. Deliberately NO
  companion blocker — written down, not left silent (D77's row, the DOES-NOT bullet above, and
  the binding's own code comment all state the same reason): `forcedBlockers` is already fully
  computed by this point, so a malformed `synthesis.blockers` can only cost extra blocker TEXT,
  never the decision.
- **P2 — the same crash class, one call site upstream, OUTSIDE the ratchet region.** `withIds`
  (top of file) does `(findings || []).map(...)`; a truthy non-array `codex.findings` (an LLM
  returning a bare string instead of an array — precisely the shape that produced round 7's first
  P1 on `codexFixes.fixed`) threw the same crash class, RC=1, no verdict. This call site sits
  ABOVE `GATE-RATCHET BEGIN`, so the ratchet will never force inventory rows for it — a genuine
  blind spot in the mechanism this round just built, named explicitly (DOES-NOT bullet above, and
  D73's own row). Hardened with the identical coerce-AND-block shape as the in-region siblings: a
  `codexFindingsShapeDefect` flag set at the (out-of-region) call site, read by a new
  `REVIEWER-FINDINGS GATE:` blocker computed INSIDE the region, next to its siblings — the
  DETECTION lives outside the ratchet's pin, but the DECISION lives inside it (D74-D76).
- **P2 — an undeclared asymmetry in the round's own coerce-AND-block pattern.**
  `codexFixMutations`'s `Array.isArray(codexFixes.mutations) ? codexFixes.mutations : []` ternary
  coerced a truthy non-array silently, with NO blocker, unlike its three region peers
  (`_mayTouchRaw`, `_codexFixedRaw`, `_codexDeclinedRaw`). Confirmed NOT exploitable, by execution
  rather than reasoning: with `mutations: "see PR"` and a src-touching FIXED finding, the run still
  comes back `blocked`, because the coerced `[]`'s `.length === 0` trips the pre-existing
  fix-evidence blocker (pinned by `test_codex_fix_mutations_malformed_non_array_still_blocks_via_emptiness_check`).
  Left unhardened deliberately — the reason is now written at the binding's own code comment, not
  left for a future reader to re-derive. A SECOND, harsher mutant (drop the guard entirely rather
  than just imagining a malformed value flow through) was also run for real (D78): the verdict
  still doesn't flip, but the SPECIFIC blocker text does, and `mutationGate.fixEntries` inflates to
  a nonsense count (character-spread garbage). That's the message-text/count-cosmetics residual
  class the stopping rule already carves out, not a protected direction — filed as a residual,
  not promoted to a fix, and stated here rather than silently discovered by a future reader
  re-running the same mutant.
- **P3 — the D-table still had gaps on round-6/round-7-first-pass lines this round's own diff
  touched or should have covered.** `fixedIds`'s string/object ternary and `.filter(Boolean)`
  (D79/E9 — the ternary's string-form branch turned out to be genuinely UNTESTED, not merely
  unrowed: no fixture before this round supplied `fixed[]` as bare string ids), the
  `fixedSrcTouchingIds` `.find()` lookup and its `_isSrcScopedGlob(f.file)` conjunct (D80/D81,
  distinct from D72/E7's adjacent `typeof f.file === 'string' && f.file.trim()` conjunct), and the
  fix-evidence blocker condition `if (fixedSrcTouchingIds.length && codexFixMutations.length ===
  0)` (D82-D84, all three operator×directions) now all have executed rows. Rows only — no
  behavior change implied for any of these except D79's genuinely new bare-string-id coverage.

**P3 — the failure message IS the operator runbook, but pytest's default diff-elision defeated it
on the one run that matters.** `test_ratchet_green_at_head`'s bare `assert ratchet_defects(...) ==
[]` let pytest's list-diff truncation elide the MIDDLE of the mismatch string — exactly where the
recomputed hash the operator needs to paste into `GATE-SOURCE-PIN` lives. Fixed with an explicit
assertion message (`assert not defects, "\n\n".join(defects)`) that prints each defect on its own
line, untruncated, on the one assertion an operator will actually be staring at in CI output.

**New tests this round:** `test_synth_blockers_non_array_blocks_not_crashes` (P1 offender),
`test_reviewer_findings_non_array_blocks_not_crashes` / `test_reviewer_findings_well_formed_produces_no_shape_blocker`
(P2 offender + over-block control), `test_codex_fix_mutations_malformed_non_array_still_blocks_via_emptiness_check`
(the declared-unhardened confirmation, pinned rather than left as prose), and
`test_fixed_finding_as_bare_string_id_recognized_for_evidence` /
`test_fixed_finding_as_bare_string_id_with_fresh_evidence_proceeds` (D79's new bare-string-id
coverage, reject + accept sides). See the D73-D84/E8/E9 campaign above for the full mutant table
and the "Round-7-second-pass numbering note" for the re-derived D35a/D35b/D44/D45/RES2/D46-superseded
rows.

GATE-SOURCE-PIN: sha256=ecb380ec40829547a44acc757773f575a205ceedc487c724b2c2fffa6b830382
