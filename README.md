# Neutron Enterprise

**Architecture white paper:** [Markdown](docs/whitepaper/neutron-enterprise-architecture-whitepaper.md) · [PDF](docs/whitepaper/neutron-enterprise-architecture-whitepaper.pdf)

Neutron Enterprise is a modular Python platform for running governed AI workflows inside
infrastructure you control. Everything self-hosts. There is no SaaS control plane and no phone-home,
and the platform makes no outbound network call except to destinations you explicitly allowlist. Every
model call, retrieval, and tool action passes a policy decision point and lands in a hash-chained,
tamper-evident audit log.

It is licensed under the [Apache License 2.0](LICENSE).

## Relationship to Neutron

[Neutron](https://github.com/rjunee/neutron) is a self-hosted agent harness for orchestrating
long-lived Claude Code sessions. It is a TypeScript project for an individual developer or a small team.
Neutron Enterprise is a separate codebase aimed at a different problem: running AI workflows inside an
organisation, over its own data and identity systems, with policy enforcement and an audit trail.
The two projects share ideas but no code. The skill-library design (`ne-skills`, `ne-flywheel`)
started in Neutron and was ported here to the Pydantic-AI + DBOS stack.

## Architecture

The white paper's argument is that an organisation should not have to choose between capable AI
tooling and keeping its data, models, and decisions under its own control. Neutron Enterprise is
built against twelve stated constraints: it self-hosts with deny-by-default egress, routes every
model call through one gateway so models can be swapped by configuration, sends every tool action
through a policy decision point into a hash-chained audit log, and grounds answers in a tiered,
permission-trimmed knowledge layer. Most of the constraints are enforced by an executable test or
CI gate rather than by policy prose.

The repository is a [uv](https://docs.astral.sh/uv/) workspace of small packages. Distributions
are named `ne-<name>`, and imports use the PEP 420 namespace `ne.<name>`. Modules interact only
through the Protocols and typed events in `ne-contracts`, and import boundaries are enforced by
[import-linter](.importlinter).

```
            ┌────────────────────── ne-console (React SPA, thin client) ──────────────────────┐
            │                                                                                 │
  OIDC ──▶ ne-identity ──▶ ne-api (HTTP composition root) ──▶ ne-worker (DBOS durable worker)
                                │                                     │
           ┌────────────────────┼──────────────────┬──────────────────┼───────────────┐
           ▼                    ▼                  ▼                  ▼               ▼
      ne-govern            ne-runtime          ne-brain*          ne-connect       ne-audit
   PDP · guardrails     governed agent      T1 hybrid RAG       connectors +     hash-chained
   PII redaction        loop (Pydantic-AI   T2 entity graph     governed         log + signed
                        + DBOS)             T3 ontology/SHACL   write-back       WORM checkpoints
           │                    │                  │
           └──────────▶ ne-gateway (every generative call) ──▶ ne-serve (vLLM, open-weight models)
                         egress allowlist · AIR_GAP kill            or an allowlisted frontier API
```

| Area | Packages |
|---|---|
| Contracts & schema | `ne-contracts` (Protocols, typed events, config hashing, plugin registry), `ne-schema` (LinkML source of truth + codegen), `ne-events` (Postgres outbox) |
| Knowledge | `ne-brain` (T1 ingest/retrieval/write), `ne-brain-graph` (T2 property graph), `ne-brain-ontology` (T3 triple store + SHACL), `ne-memory-graph` (temporal agent memory) |
| Execution | `ne-runtime` (governed agent loop), `ne-worker` (durable executor, cron, outbox drain), `ne-api` (HTTP service) |
| Governance | `ne-govern` (PDP, deterministic guardrails, PII), `ne-permissions` / `ne-permissions-rebac` (ACL sync, trimming, OpenFGA), `ne-audit`, `ne-identity`, `ne-secrets`, `ne-net` (SSRF/egress validator) |
| Models | `ne-gateway` (all model calls), `ne-serve` (vLLM serving profile + model menu), `ne-eval` (eval harness + CI gates) |
| Integrations | `ne-connect` (connector framework: SharePoint/M365, Salesforce, Slack, Jira, Zendesk, …), `ne-mcp` (MCP server + client), `ne-chat` (Slack/Teams adapters) |
| Improvement loop | `ne-flywheel` (propose-then-approve curation), `ne-skills`, `ne-skills-cc`, `ne-obs` |
| Frontend | `packages/ne-console` (TypeScript/React) |
| Deployment | `deploy/compose` (dev + single-VM pilot), `deploy/helm`, `deploy/terraform-{azure,eks}`, `deploy/airgap` (Zarf) |
| Tooling | `tools/ne-cli` (the `ne` CLI), `tools/supply-chain`, `tools/ne-backup`, `tools/acme` and `tools/meridian` (synthetic demo corpora) |

The design constraints, the reasoning behind each component choice, and the security model are
covered in the white paper linked at the top of this file. See also
[`docs/architecture/`](docs/architecture/) and the [ADRs](docs/adr/).

## Quickstart

Requirements: [uv](https://docs.astral.sh/uv/) 0.8+ (it fetches the Python version pinned in
`.python-version`), Node 22+ for the console, and Docker for the local service stack.

```sh
git clone https://github.com/rjunee/neutron-enterprise.git && cd neutron-enterprise

# Install every workspace package (editable) plus the dev tools.
uv sync --all-packages

# The dev CLI
uv run ne --help

# Run a focused slice of the test suite (no external services needed)
uv run pytest packages/ne-contracts packages/ne-govern -q

# Static gates
uv run ruff check .
uv run lint-imports --config .importlinter --no-cache
uv run ne check headers
```

Run plain `uv sync --all-packages` first. `uv run` on its own does not install the other
workspace members, and the tests then fail with `ModuleNotFoundError`.

Bring up the local service stack (Postgres, Qdrant, Keycloak, OPA, TEI embeddings/reranker,
OpenBao, OTel collector):

```sh
uv run ne dev up        # start the stack
uv run ne dev ps        # status
uv run ne dev down      # stop (add --volumes to delete data)
```

To run the full governed demo (pilot stack, synthetic corpora, narrated arc), use
`uv run --all-packages ne demo`. The first run on a fresh host downloads about 2.3 GB of
model weights and builds an ONNX export, which takes tens of minutes; later runs reuse the
cache. See [`docs/demo/README.md`](docs/demo/README.md).

Build and test the console:

```sh
cd packages/ne-console
npm ci
npm run typecheck && npm run test && npm run build
```

The full Python suite is `uv run pytest -n auto --dist loadgroup`. Tests that need a live
external system (a real tenant, a GPU, cloud credentials) skip themselves by name when that system
is unavailable.

For deployment beyond a laptop (the hardened single-VM pilot, Helm/Terraform, air-gapped
installs), see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) and the READMEs under
[`deploy/`](deploy/).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for development setup, source headers, the dependency
intake checklist, and how to add a package. Report security issues as described in
[`.github/SECURITY.md`](.github/SECURITY.md).

## License

Copyright (c) 2026 Ryan Junee. Licensed under the
[Apache License, Version 2.0](LICENSE). Third-party components are listed in each package's
`NOTICE` file.
