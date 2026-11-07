# Per-lane extraction eval (R1) — routing stated PLAINLY

Graphiti extraction (entity + contradiction detection driving edge invalidation)
is a **reasoning-critical** stage. This harness measures its quality + cost per
lane so the routing is never silently downgraded.

## Routing

- **frontier lane** — extraction is routed through the in-process gateway
  (`task="memory-extraction"`, a gateway-governed **frontier model**) via the
  loopback `llm_facade`. This is the **quality reference**.
- **AIR_GAP lane** — extraction is routed to an **open-weight in-VPC vLLM model**
  from the NE-011 registry menu. This is a **weaker model class on a
  reasoning-critical stage**, and its entity/contradiction-extraction quality is
  **UNMEASURED until the live eval runs**. The delta is an open risk at merge
  (LIVE-PARKABLE — no vLLM endpoint in CI).

## Cost

Every `append` and every backfilled event incurs an LLM extraction call. The
`llm_facade` logs per-request cost (`model_id` / `prompt_tokens` /
`completion_tokens` / `cost_usd`); backfill token spend scales with corpus size,
so throttle it (`backfill(..., throttle_s=...)`) under the C12 budget.

## Modes

```
# CI (deterministic, no network): validates fixture schema + scoring arithmetic
python evals/lane_eval.py --fixtures-only

# LIVE (parked until endpoints exist): scores real extraction + records cost
python evals/lane_eval.py --lane frontier --base-url http://127.0.0.1:8093
python evals/lane_eval.py --lane air_gap  --base-url http://127.0.0.1:8093
```

`fixtures/` holds the synthetic business-memory corpus (`episodes.json`), the
expected entities/facts (`expected.json`), and an ILLUSTRATIVE recorded frontier
output (`recorded_frontier.json`) used only for deterministic CI scoring — the
live per-lane quality + per-event-cost rows are recorded in the
pending-live-verification ledger when real endpoints exist.
