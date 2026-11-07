# e2e/ — the demo-scenario E2E suite

The 2026-08-28 demo scenarios (ASK_SCENARIOS / DEMO_RUNBOOK in the project
tree), turned into an automated suite that asserts on **answer content**, not
completion. It exists to be trustworthy at three moments: **before a demo,
before a release, and on a weekly schedule** — and to be nowhere near the PR
lane.

## Why it is NOT a CI check

A full run is ~25–45 minutes of **serial** wall time against a live stack,
spends real frontier-API money, and exercises measured-nondeterministic model
behaviour (S1's ~1-in-3 no-answer schema rejection; S9's ~1-in-11 claim-binding
flake). The repo already learned this lesson once: `no-egress.yml` moved its
flaky vLLM bring-up OFF the PR-blocking lane (NE-CI-NOEGRESS-VLLM-FLAKE)
because a required gate that cries wolf trains reviewers to wave it through.
This suite follows that precedent from birth: **cron + dispatch only, never a
required check.** The root `testpaths` does not include `e2e/`, so no default
`pytest` run — local or CI — can collect it by accident.

## How it is invoked

| Lane | How | What runs |
|---|---|---|
| Box-side (primary) | `e2e/run.sh --env /root/ne-e2e.env` | read-only lane |
| Box-side, full | `e2e/run.sh --env /root/ne-e2e.env --mutating` | + S5 (real email send), S8 (live SOP edit) |
| GitHub, on demand | `gh workflow run e2e-scenarios.yml [-f mutating=true]` | dispatch lane |
| GitHub, weekly | cron `23 10 * * 1` (Mon 10:23 UTC = 03:23 PDT, while the owner sleeps) | read-only lane only |
| Offline self-test | `e2e/run.sh --selftest` (or `uv run pytest e2e/scenarios/test_00_selftest.py`) | assertion helpers, no stack, no money |

The GitHub lane needs the `NE_E2E_*` secrets configured **and network reach**
from GitHub-hosted runners to the deployment's API/Keycloak ports. **Verified
2026-08-29 on the demo box: there is no such reach today** — Keycloak's 8443
is bound to the public interface but the API's 9443 binds to `127.0.0.1`
only, so a GitHub-hosted runner cannot reach the API at all; the workflow's
preflight will skip with exactly that reason until the owner exposes the API
or adds a tunnel/self-hosted runner. The box-side runner is therefore the
working lane, and box-side runs must set `NE_E2E_RESOLVE_TO=127.0.0.1` (the
public DNS name resolves to the public interface; the override pins the
address while keeping the hostname, which Caddy's TLS requires as SNI — the
S9-trials driver pattern). The full env contract is documented at the top of
`e2e/scenarios/conftest.py`.

### The preflight gate (`e2e/preflight.py`)

Both lanes run it before spending a model dollar (owner-mandated 2026-08-29:
the demo box and the GPU rig are not always up — the GPU costs money). The run
is **skipped with the deciding reason** (exit 3; the workflow renders it in
the job summary and a `::notice`) unless both hold:

1. **Demo up** — `https://<host>/` returns a **non-empty** 200 (an empty 200
   is the known Caddy wrong-Host-header signature and counts as down).
2. **External GPU connected** — no address is hardcoded anywhere, because the
   rig's IP is not stable. The deployment's own `GET /v1/models/serving`
   (ops-gated) reports where the private lane routes and what that endpoint
   actually serves; the gate requires probe `state == ok`, **served-model
   identity matching the configured litellm target** (a `vllm-cpu` fallback
   serves a different model and fails this), and a non-loopback,
   non-in-compose endpoint host (a dotless service name like `vllm-cpu` is a
   local container — sufficient evidence there is no external GPU). cpu-vllm
   is far too slow for the suite; running against it would manufacture
   timeout "regressions".

Three outcomes, never collapsed: proceed (0) · skip-with-reason (3) · the
preflight itself broken (1, e.g. credentials present but rejected — that goes
RED, it is a failure someone must look at, not a skip).

## Live stack, not ephemeral — the honest trade

The suite targets an **existing deployment** (the demo box, or any deployment
with the Meridian seed). An ephemeral CI bring-up was considered and rejected
because the scenarios' substance cannot be reproduced on a GitHub runner:

* Beat 1b / S2 / S9 need the **private GPU lane** (`private-default` on the
  in-VPC vLLM host) — there is no GPU on a hosted runner;
* S5 and S8 need the **real M365 tenant** (actual Outlook drafts/sends, a real
  SharePoint document with real Graph versioning);
* the corpus warm-up alone (CPU TEI embedding of 34k points) takes longer than
  most CI budgets, and a cold corpus silently produces stale answers.

The costs of that decision, stated plainly: the suite **shares tenant state**
with rehearsals (approval rows, conversations, ever-growing SOP versions — all
assertions are written delta/monotonic, never absolute), a red run can mean
"the box is sick" rather than "the code is wrong" (the warmth gate and flake
attribution exist to separate those), and the mutating lane really mutates
(a real email to the allowlisted fixture inbox; a permanent SOP version walk).

## What a run costs

* **Wall time:** ~15–25 min read-only lane; ~30–45 min with the mutating lane
  (S8's connector sync alone is ~2–3 min; runs are 30–165 s each and strictly
  serial — parallelism manufactures failures on a loaded box, and the conftest
  refuses `-n`).
* **API money:** the frontier scenarios (S1, S4, S7, grounding gate + control,
  Beat 1b turn 2, S5, S8's two asks + two ledger re-asks) are ~8–12
  frontier-model runs, several with long multi-hop contexts. No per-run token
  bill is recorded by the platform today (`execution_profile_receipt` was null
  on the measured trials), so this is an order-of-magnitude estimate, not a
  measurement: **roughly $2–8 per full run** at current Sonnet-class pricing,
  dominated by S1's ~10 accumulated-context round trips. The private-lane
  scenarios (S2, S9, Beat 1b turns 1/3) cost no API money — they ride the
  in-VPC GPU. If the estimate matters, wire token accounting into the receipt
  and replace this paragraph with a measurement.
* **Flake retries** add at most 2 extra runs each for S1/S2/S9/Beat-1b in the
  common case (budgets of 2–3 attempts).

## Scenario coverage

| Scenario | Automated? | The real acceptance bar encoded |
|---|---|---|
| S1 multi-hop (frontier) | ✅ full | names 001/002/003; **005 excluded** (prior offer); window via boundary cases 008/010; servicing-notes hop present; ~6 tool calls |
| S2 expected-WRONG (private) | ✅ full | ⛔ encoded as a demo ASSET: completes VERIFIED with a confident claim; **no correctness assertion either way**; a capability tripwire fails if anyone grants the Analyst deadline tooling ("fixing" S2 destroys the S9 beat) |
| S4 the closer (frontier) | ✅ full | grounded refusal; **zero fabricated totals** (the single most important assertion in the suite); names the blank `credit_issue_date` |
| S5 governed ladder (mutating) | ✅ full, with 2 strict xfails | park at Tier-2; Tier-0 draft before park; requester's self-approval is 403; distinct approver approves; **send executes**; terminal-state + trailing CRM note are `xfail(strict)` on POST_DEMO #2/#4 — they flip LOUD when fixed |
| S7 capability refusal | ✅ full | refuses AND names Meridian Customer Support as the route; no pretend-acting |
| S9 kill shot (private) | ✅ full | **mer-dsp-001 ALONE**, exactly one tool call, VERIFIED; capability surface = exactly the one governed read |
| Beat 1b model independence | ✅ full | 3 turns / 2 swaps / one conversation; history carries; recall turns' `unavailable` verdict pinned as CORRECT |
| S8 temporal provenance (mutating) | ⚠️ conditional | full loop (ask → Graph edit → sync → re-ask → ledger stamps v(n)/v(n+1), monotonic-only assertions) **iff** `NE_E2E_GRAPH_*` app-only credentials with `Files.ReadWrite.All` are provided; otherwise it SKIPS loudly — see below |
| Grounding gate | ✅ full | verdict REFUSED + a **real `guardrail.block` audit row** bound to the run id, plus a clean control on the plain Analyst that does NOT trip it |

### Not (fully) automated, and why

* **S8's SharePoint edit** needs a Graph write credential the repo cannot
  assume exists: the platform connector's registration is read-scoped for sync
  (plus mail scopes for S5); whether the tenant grants it `Files.ReadWrite.All`
  is a tenant-admin fact. Provide `NE_E2E_GRAPH_*` (a dedicated app
  registration is cleaner) and S8 runs end-to-end; without it the scenario
  skips with the reason, and the loop needs a human to make the one Word-online
  edit. Additional honest caveat: the docx rewrite is byte-level and can be
  defeated by Word run-splitting; when that happens the suite fails loudly
  BEFORE uploading anything, with remediation in the message.
* **Screenshot-grade UX verification** ("tested with screenshots, not just API
  polls" — ASK_SCENARIOS's bar) is out of scope here: this suite asserts the
  governed API surface (answers, verdicts, events, audit rows). Console
  rendering warts (answer-projection outages, citation spinners) are invisible
  to it by design; the pre-demo human walk of DEMO_RUNBOOK is still the only
  check of the pixels.
* **S5's Outlook-side evidence** (the draft visible in `/mail/drafts`, sent
  item in `/mail/sentitems`) is asserted indirectly through the platform's own
  audit rows (`tool.execute` success for the draft and the send), not by
  reading the mailbox. Reading the mailbox would need the same Graph credential
  class as S8 — wire it up later if the indirect evidence ever proves
  insufficient.

## Flake vs regression — the attribution policy

Encoded in `e2e/scenarios/nelib/flakes.py`; the short version:

1. **Only known signatures retry** (claim-binding no-span ~1/11; the transient
   private-default admission timeout; S1's ~1/3 reply-schema rejection). An
   unmatched failure fails immediately — a bare retry would hide real breaks.
2. **Every attempt is recorded** in the flake ledger (printed at session end,
   written to the results dir with the full transcripts). A green run still
   discloses what it absorbed.
3. **Exhausting a budget on one signature fails as a RATE regression**, naming
   the known rate (three consecutive 1-in-11 draws is p < 0.1%).
4. **Content is never retried.** A completed-but-wrong answer fails on first
   observation — completion ≠ correctness is the whole point of the suite.
5. **`unavailable` is not failure.** It means no answer-with-evidence was
   served, and is asserted as the CORRECT verdict for history-recall turns and
   capability refusals.

## Operational gates the suite enforces on itself

* **Warmth gate:** knowledge scenarios refuse a cold box (m365 posture must be
  `live` with `sync_allowed`) — a cold box silently serves stale answers
  (DEMO_RUNBOOK Gate 2 / the boot-ordering trap). `NE_E2E_ALLOW_COLD=1`
  bypasses it for suite debugging only.
* **Serial only:** the conftest hard-refuses `-n`/xdist.
* **Config refusal:** missing required env FAILS loudly; the optional lanes
  (mutating, S8 Graph creds) SKIP with visible reasons. Refused ≠ empty.
* **Transcripts always:** every run's detail/events/evidence lands in the
  results dir, so a red scenario is diagnosable without paying to re-run it.
