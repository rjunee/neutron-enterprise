# deploy/compose

Docker Compose deployment profiles: `core`, `brain`, `agents`, `obs`,
`serve-gpu`, composed via `include` + `profiles`.

Status: the local dev profile lives in `dev/` (NE-006) — see
`deploy/compose/dev/README.md` and use `ne dev up`. The hardened
single-VM pilot tier + installer lives in `pilot/` (NE-070): profiles
`core`/`brain`/`obs` composed via `include` of `serve/` + `profiles`,
digest-pinned, Caddy TLS ingress (the only off-box publisher), secrets
injected from a per-customer bundle. Bring it up with
`ne up --profile pilot --bundle <dir>` (see `deploy/compose/pilot/README.md`);
build a bundle with `ne pilot promote`.

The vLLM serving profile for the private-vllm lane (NE-013) lives in `serve/`
(additive `gpu` / `cpu` profiles) — see `deploy/compose/serve/README.md`.

The OFF-BY-DEFAULT CDC sub-minute freshness lane (NE-084) lives in `cdc/` — a
STANDALONE overlay project (`ne-cdc`, NOT part of the pilot stack) running
digest-pinned Debezium Server whose HTTP sink POSTs governed, fail-closed
change events to ne-api. Bring it up ONLY after the pilot and ONLY when a
customer opts in — see `deploy/compose/cdc/README.md`.
