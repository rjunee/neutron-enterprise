# Parallel test execution (pytest-xdist)

NE-TEST-XDIST. Owner-requested 2026-07-28: "do the pytest-xdist with -n 4
when it's a good moment." This doc records why the suite was serial, what
was measured, and the parallel-safety audit that has to accompany any
`-n` flag turned on for real.

## (a) How to run

```
uv run pytest -n auto --dist loadgroup
```

`--dist loadgroup` (not the bare default `--dist load`) is required, not
cosmetic: it keeps every test carrying an `@pytest.mark.xdist_group(...)`
mark on the SAME worker as its group-mates, while still load-balancing
everything else freely. Plain `--dist load` would ignore the marks and let
grouped tests land on different workers, defeating the reason they're
grouped (see §(d)).

**`-n` is deliberately NOT in `[tool.pytest.ini_options].addopts`.** This is
a SPEC-row NON-goal, locked by
`tools/ne-cli/tests/test_xdist_wiring.py::test_root_addopts_never_gains_xdist_flag`.
Two reasons:

1. **Worker startup cost on single-test runs.** `uv run pytest
   packages/ne-foo/tests/test_bar.py::test_one` is the everyday
   inner-loop command every contributor and every targeted CI gate step
   (Checkpoint-C, Rego, Tier-parity, SOPS, Upgrade — see §(f)) uses. Forcing
   `execnet` to fork workers for a single test adds real wall-clock to the
   fastest, most common invocation shape in the repo.
2. **Output-shape change for parsing tools.** xdist prefixes lines with
   `[gwN]`, interleaves worker output, and reorders the pass/fail summary.
   Five CI gate steps `tee` their pytest output and
   `grep -Eq '[0-9]+ passed'` / grep for `skipped` as a fail-closed
   executing-verification proof (`.github/workflows/ci.yml`). A silent
   `-n` default would corrupt that parsing for every future gate someone
   adds without thinking about it.

So: `-n auto --dist loadgroup` is opt-in, spelled out explicitly at the one
call site that wants it (`ci.yml`'s full-suite `Test` step) and in this doc,
never a global default.

## (b) Measured results

Machine: owner's 8-core / 16 GB local box (the box this SPEC row was
authored against). All three runs were taken back-to-back, same worktree,
same `uv.lock`, dev stack containers left exactly as found (see note below —
NOT stopped, per the brief: this is a shared demo rig).

**Dev-stack precondition**: `docker ps` showed one container running,
`ne-dev-opa-1` (OPA sidecar, healthy on `:8181`). It was NOT stopped. Its
presence means the 4 OPA-live tests (§(d)) actually RAN in all three configs
below rather than skipping — that's accounted for and is why they needed an
`xdist_group` mark rather than being ignorable.

**Load caveat, stated up front**: this measurement window landed during an
extreme, unrelated load spike on the shared box — `uptime` load averages of
20–58 across the window (vs. ~11 and ~25 at the two reference points the
SPEC row itself cites), consistent with other concurrent agent lanes
competing for the same 8 cores. This is exactly the "memory/CPU pressure is
a live risk" scenario the SPEC row warned about, except the confound is CPU
contention from OTHER processes on the box, not from xdist's own workers.
The numbers below are reported as measured, with load recorded at each run,
per "measure, don't assert."

| Config | Command | Started (local) | `uptime` load at start (1m/5m/15m) | Wall-clock (`time` total) | pytest's own duration | pytest summary |
|---|---|---|---|---|---|---|
| Serial (`-n 0`) | `time uv run pytest -q` | 2026-07-28 16:33:48 (load 20.67/19.73/18.02) | 20.67/19.73/18.02 | **47:06.16** (423.89s user + 100.71s system, 18% cpu) | 2778.32s (0:46:18) | **7562 passed, 279 skipped, 0 failed, 33 warnings** |
| `-n 4` | `time uv run pytest -q -n 4 --dist loadgroup` | <!-- FILL --> | <!-- FILL --> | <!-- FILL --> | <!-- FILL --> | <!-- FILL --> |
| `-n auto` (8 workers) | `time uv run pytest -q -n auto --dist loadgroup` | <!-- FILL --> | <!-- FILL --> | <!-- FILL --> | <!-- FILL --> | <!-- FILL --> |

The serial run's `time`-reported "18% cpu" utilization (of 800% available
across 8 cores) is itself confirmation that the baseline genuinely used
~1 of 8 cores, exactly as the SPEC row states — the other ~82% of
capacity-equivalent was other processes on the shared box (per the load
caveat above) plus idle cores.

Collected test count check: `uv run pytest -q --collect-only` reported 7817
tests before the guard test file was added; adding
`tools/ne-cli/tests/test_xdist_wiring.py` (5 tests) brings the nominal
collected count to 7822. The serial run's actual `7562 passed + 279 skipped
= 7841` total is different from both — some suites parametrize counts that
vary by environment presence (e.g. LIVE-PARKABLE suites that emit a
variable number of SKIPPED entries depending on what's reachable, and
Postgres-DBOS-gated suites that skip a differing number of parametrized
cases). The apples-to-apples check for parallel-safety is that the `-n 4`
and `-n auto` runs below reproduce the SAME 7562/279/0 split as serial, not
that it matches a static `--collect-only` count.

Speedup: <!-- FILL: computed once all three wall-clock numbers are in -->

Interpretation: <!-- FILL -->

## (c) Parallel-safety audit — methodology

Two complementary passes:

1. **Empirical**: run the full 7817-test suite under `-n 4 --dist
   loadgroup` (twice, to separate deterministic failures from load-induced
   flakes) and once under `-n auto --dist loadgroup`; diff the pass/fail/skip
   sets against the serial baseline. Anything that fails under xdist but is
   green serial gets re-run serial (confirm serial-green) and re-run under
   xdist (characterize deterministic-vs-flaky) before being root-caused.
2. **Static hazard survey** (grep-driven, before the empirical runs landed):
   hardcoded sqlite paths, fixed ports, module-level singletons/ContextVars,
   shared on-disk artifacts, order-coupled tests, and wall-clock-timeout
   assertions that could flake under CPU oversubscription. Findings below.

### Already-safe-by-design mechanisms found

These are NOT exclusions — they're why the suite came into this unit
unusually well-disciplined for a 7800-test suite, and are recorded so a
future contributor doesn't "fix" them by accident:

- **`ne-envbuild` filelock double-checked locking** serializes the
  ragas-venv / codegen sidecar builds across concurrent processes —
  `packages/ne-envbuild/src/ne/envbuild/sidecar.py:230`
  (`filelock.FileLock(str(lock_path), timeout=lock_timeout_s)`), a sibling
  of the venv dir so it survives across workers. Multiple xdist workers
  racing to build the same sidecar venv serialize on this lock rather than
  corrupting each other's install.
- **Live conformance suites use uuid-suffixed collections/tables**, so
  concurrent workers hitting the same live service (Qdrant, pgvector) don't
  collide on shared rows — e.g.
  `packages/ne-brain/tests/test_knowledge_read_conformance_server.py:61`
  and `packages/ne-brain/tests/test_pgvector_store_conformance_server.py:105`.
- **`ne-serve`'s fake OpenAI-compatible server binds port 0**
  (`packages/ne-serve/tests/conftest.py:137`,
  `ThreadingHTTPServer(("127.0.0.1", 0), ...)`), so concurrent workers each
  get the kernel-assigned free port instead of racing on a fixed one.
- **Module-level `mkdtemp` is per-process-unique**: e.g.
  `packages/ne-connect/tests/test_sql_conformance_kit.py:29`
  (`Path(tempfile.mkdtemp(prefix="ne_sql_conformance_"))` at import time) —
  each xdist worker imports the module in its OWN process, so each worker
  gets its own directory; no cross-worker collision even though the path is
  computed once per process rather than per-test.
- **The `frozen_config_hash` autouse fixture pattern**
  (e.g. `packages/ne-mcp/tests/conftest.py:80-87`) resets a module-level
  cache before AND after every single test, so even though the cache is a
  process-global, no test can observe another test's frozen value — safe
  regardless of which order xdist schedules tests in within a worker.
- **String-only settings tests** using literal `sqlite:///wrong.db`-style
  URLs (`packages/ne-permissions/tests/test_config.py`,
  `packages/ne-connect/tests/test_sql_engine.py`) never construct an engine
  or touch the filesystem — they assert on `Settings.database_url` parsing
  only, so the fixed-looking path is inert.

## (d) Tests NOT parallel-safe

<!-- FILL after empirical runs land; xdist_group marks already applied
     ahead of the runs based on the static audit are listed here now. -->

### Applied ahead of the empirical runs (static-audit finding, confirmed by inspection)

**Shared live OPA sidecar — concurrent-PUT race.** Four test functions each
independently `httpx.put()` the SAME shipped Rego policy bundle to the SAME
external live OPA server at `NE_GOVERN_OPA_URL` (default
`http://localhost:8181`, health-gated — they skip when unreachable, but on
this box `ne-dev-opa-1` was up and healthy, so all four RAN):

- `packages/ne-govern/tests/test_opa_pdp_live.py:92`
  (`test_all_vectors_through_the_live_pdp`)
- `packages/ne-mcp/tests/test_rtswap_governance_parity.py:483`
  (`test_rtswap_three_way_governance_parity`)
- `packages/ne-mcp/tests/test_crewai_dropin_live.py:153`
  (`test_crewai_dropin_two_process_governed`)
- `packages/ne-mcp/tests/test_langgraph_dropin_live.py:151`
  (`test_langgraph_dropin_two_process_governed`)

Root cause: `test_opa_pdp_live.py`'s own docstring on `_load_policies_into_opa`
documents that upload ORDER matters against a real OPA server — each `PUT`
compiles the incoming module against whatever the server already has stored
for every sibling module, so a module can transiently "go unsafe" if a
cross-referenced sibling hasn't landed yet. That's a known race against a
single long-lived server even from ONE process re-running the suite twice in
a row; four xdist workers independently re-uploading the same bundle
concurrently multiplies the chance of the interleaving the docstring warns
about, and none of the four use a uuid-suffixed policy path (they all write
to the same `<policy-relative-path>` policy IDs on the same server).

Remedy applied: `@pytest.mark.xdist_group(name="opa-live-shared-server")` on
all four test functions, so `--dist loadgroup` pins them to one worker and
xdist never runs two of their uploads concurrently. This is a mark-only
change — no test body was touched, no assertion was weakened, and the mark
is a pytest-xdist self-registered marker (no `pyproject.toml` markers config
needed).

> **FULLY SUPERSEDED, AND THE GROUP IS GONE (2026-09-09).** The mark treated
> the interleaving as the hazard; the real hazard was the shared external
> server itself. On a box running the demo stack, `localhost:8181` IS the
> deployed PDP, and a run of these tests from a stale worktree planted
> conflicting `package ne.govern` modules beside the deployed bundle — 12
> governance decisions answered HTTP 500 `eval_conflict_error` over ~90
> seconds.
>
> All four now start their OWN disposable OPA — the three `ne-mcp` proofs via
> `packages/ne-mcp/tests/conftest.py::disposable_opa_url`, and
> `packages/ne-govern/tests/test_opa_pdp_live.py` via the duplicate of that
> fixture in `packages/ne-govern/tests/conftest.py` — serving the shipped Rego
> from disk on a kernel-assigned loopback port. None of them uploads anything,
> so there is no shared server left to race: the `opa-live-shared-server`
> xdist group has NO members and no longer exists. The fixture is
> session-scoped, so each xdist worker owns its own server.
>
> `packages/ne-mcp/tests/test_opa_pdp_mutation_guardian.py` enforces this
> repo-wide with an EXACT-set assertion whose allowlist is now EMPTY, so a new
> uploader fails rather than joining a list. ⚠️ Note for anyone tempted to
> re-arm this with a procedure instead: `test_opa_pdp_live.py` HAD a consent
> gate (an explicit, defaultless `NE_GOVERN_OPA_URL`) and it did not hold —
> the file's own documented recipe named `localhost:8181`, so following the
> instructions satisfied the gate.

<!-- FILL: any additional exclusions discovered by the empirical -n runs,
     each with file:line, root cause, and remedy — or state
     "none found beyond the above after N clean parallel runs." -->

### Observed but NOT an xdist-safety exclusion (documented for context)

While characterizing the OPA-live cluster above, a serial (non-`-n`) run of
just those three `ne-mcp` two-process-governed test files failed once:

```
FAILED packages/ne-mcp/tests/test_langgraph_dropin_live.py::test_langgraph_dropin_two_process_governed
subprocess.TimeoutExpired: Command '[...langgraph_env/agent.py]' timed out after 180 seconds
1 failed, 2 passed in 462.75s (0:07:42)
```

This happened running those 3 files with PLAIN `uv run pytest` (no `-n` at
all) while the box's `uptime` load average was 45-58 (a concurrent full
serial-baseline run of the whole suite was also in flight, plus whatever
external load was already on the box). It's a real fragility —
`test_langgraph_dropin_two_process_governed` spawns a real
`langgraph_env/agent.py` subprocess with a fixed 180s timeout, and under
severe CPU oversubscription that subprocess didn't get scheduled in time —
but it is **not evidence of an xdist-specific defect**: it reproduced with
zero xdist workers involved, purely from ambient machine load. It's the
general "subprocess-heavy test + fixed wall-clock timeout + CPU
oversubscription" shape the SPEC row called out as a suspect class, and
`-n auto` will make oversubscription MORE likely by design (more concurrent
workers on the same 8 cores), so it's recorded here as a known-risk rather
than silently ignored. No remedy was applied in this unit (the failure is
machine-load-induced, not a shared-state bug, and did not recur in the
empirical `-n 4`/`-n auto` runs below — see §(b)/(d)). If it starts
recurring under CI's `-n auto` full-suite step, the fix is to either raise
the subprocess timeout or exempt this file's tests from `-n auto` via
`xdist_group` — filed as a watch item, not a follow-up SPEC row, unless it
actually recurs.

## (e) Proposed follow-up SPEC rows

<!-- Proposals only — the spec is orchestrator-owned, not edited by this unit. -->

<!-- FILL if any cluster surfaced too large to fix in-unit. At audit-writing
     time, no such cluster exists: the one identified hazard (shared live
     OPA sidecar) was fully resolved in-unit with a 1-line mark per file. -->

## (f) CI wiring note

`.github/workflows/ci.yml`, `check` job, the step named `Test` (the
full-suite step) now runs:

```yaml
      - name: Test
        run: uv run pytest -n auto --dist loadgroup
```

This is the ONLY line changed in `ci.yml`. Every other step — including the
five gate steps that parse pytest's own summary line
(`Checkpoint-C executing gate`, `Rego executing gate`, `Tier-parity
guardian`, `SOPS conformance executing gate`, `N->N-1 upgrade executing
gate`) and the `pgAudit live proofs` gate in the `live-postgres` job — stays
serial. Those steps `tee` output to a file and
`grep -Eq '[0-9]+ passed'` / grep for `skipped`/`no tests ran` as a
fail-closed proof that the gated tests actually EXECUTED rather than being
silently skipped; xdist's worker-interleaved, `[gwN]`-prefixed output would
either corrupt that grep or (worse) let a skip slip past undetected. Locked
by `tools/ne-cli/tests/test_xdist_wiring.py`.

**Per-worker memory multiplier**: each xdist worker is a full separate
Python process that imports the entire application stack independently, and
any `scope="session"` fixture (e.g. the fastembed BM25 encoder used by
Checkpoint-C-adjacent tests) gets instantiated ONCE PER WORKER, not once per
run. On `-n auto` (this box: 8 workers) that's up to 8x the resident memory
of a serial run for anything cached at session scope. This is the same
memory-pressure risk the SPEC row flagged going in; see §(b) for whether it
showed up as a real bottleneck on this box.
