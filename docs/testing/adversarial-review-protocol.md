<!-- Copyright (c) 2026 Ryan Junee. SPDX-License-Identifier: Apache-2.0 -->
# Adversarial review protocol (Argus panel)

**Status: normative for the multi-model review step. `docs/testing/vacuous-guards.md`
is the canonical source for the vacuous-guard taxonomy and the ratchet rule; where the
two overlap, that file wins.** This file exists because review briefs instruct panelists
to "read the protocol FIRST" and the file it named was absent — a process gap surfaced by
reviewer B on PR #477. It states the steps that were already being followed, so they can be
followed uniformly rather than reconstructed each round.

## 1. Order of reading

1. This file — the panel mechanics below.
2. `docs/testing/vacuous-guards.md` — the taxonomy (V1–V10), the ratchet rule (§5), and the
   mechanical gate's does/does-not boundary (§6). A finding that a guard is vacuous MUST cite
   the class it belongs to.
3. The diff under review, fetched at the exact HEAD sha the review evidence will be bound to.
   Review an immutable checkout of that exact candidate tree, whether the clean candidate worktree
   or a dedicated review worktree. The reviewed tree sha MUST equal the sha the merge will carry
   (`review_gate.py` refuses otherwise).

## 2. The panel

The active, client-specific workflow determines the required reviewers; do not reconstruct a
historical four-model panel by default. For the current Codex-native workflow,
the panel is exactly (1) a fresh-context independent Codex review and (2) one external Opus Argus
review. Kimi is opt-in and replaces Opus unless the user explicitly asks for both. Claude Code uses
its separately activated Claude-native workflow and ignores Codex-only model routing.

Two legacy mechanisms have separate routing and do not silently inherit the Codex-native panel:

- The unit workflow may run only when the owner explicitly activates it. Its
  internal stages do not satisfy the final exact-HEAD bar after a fix; an external wrapper must run
  fresh Codex and Opus reviews on its final clean commit.
- The remote trident runner retains its four-seat panel. A missing trident seat blocks that mechanism; the two-seat
  Codex-native rule is not authority to downgrade it.

Each required reviewer returns a verdict and, for any BLOCKER, evidence: a file:line, a concrete
failing input, and the wrong output or crash it produces. A blocker without a reproducible trigger
is a PLAUSIBLE finding, not a CONFIRMED one.

- **No silent downgrade or waiver.** If a required panelist DEFERS (call failed, timed out, auth
  expired), APPROVE is impossible until that panelist's coverage is obtained. A missing panelist is
  not a passing panelist.
- **Evidence-backed cross-model blockers veto APPROVE.** A codex/GPT-5 blocker with a file:line
  and a failing input stands until it is fixed or refuted with equal specificity.

### 2.1 Parallel execution

In the concurrent Codex-native path, once the candidate is committed and clean, start all required
reviewers concurrently with remote CI, isolated focused local verification, and mutation challenges.
Reviewers are independent and do not wait for one another. They are read-only against the frozen
sha; only the unit's designated fix writer may change source. Any fix creates a new sha and
invalidates every earlier review and CI result, so all required exact-HEAD gates must run again on
the new candidate. Do not duplicate the remote full suite locally unless reproducing a CI failure,
CI is unavailable, or an explicit final integration check requires it.

## 3. Mutation evidence

Where the change touches source (any runtime code path, regardless of whether the mechanical
`mutationGate` in `ne-unit-workflow.mjs` fires — note its documented blind spot: `deploy/**`,
flat `tools/*`, `apps/**`, `demo/**` all return `required=False`; see vacuous-guards.md §6),
a guard that the panel relies on MUST be proven by mutation:

- Make the smallest change that violates the property the guard claims to hold.
- Show the owning test goes RED (the mutant).
- Revert and show it goes GREEN (the control). A mutant without a green control is indistinguishable
  from a broken environment (vacuous-guards.md §2).

When mutation, review, and verification overlap, run mutations only in a detached disposable
mutation worktree, never in the frozen candidate or verification checkout. Create it with
`git worktree add --detach <path> <candidate-sha>`. A legacy serialized reviewer may instead mutate
and revert its unit worktree only while no other process reads that checkout; it must restore a clean
exact HEAD before posting evidence. Parallel work must isolate ports, databases, temporary
directories, caches, containers, volumes, networks, and service names, and must never remove a
sibling unit's Docker resources.

When the code path continues past a deleted guard into an operation that can fail for its own
reasons (no credentials, network), exit status is NOT the discriminator — key the test on the
guard's own message (vacuous-guards.md gallery row 21).

## 4. Recording the result

Post a `review-evidence` block bound to the current HEAD sha (the 40-hex commit sha), listing each
panelist's verdict, every BLOCKER with its disposition (fixed or refuted), and the mutation ledger
(target, change, expect-red test, observed, control) for each pinned guard. A deferred required
reviewer blocks approval; it is not a blocker disposition. The sha in the block MUST be the branch's
current HEAD; a block bound to an older sha is stale and `review-gate` treats the tree as un-reviewed.
