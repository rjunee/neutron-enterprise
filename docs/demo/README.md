<!-- Copyright (c) 2026 Ryan Junee. SPDX-License-Identifier: Apache-2.0 -->

# `ne demo` — operator runbook

`ne demo` brings up the governed pilot stack on one host, seeds it with the
synthetic Acme and Meridian corpora, and runs a narrated arc through the
platform: cited retrieval, a governed agent turn, a human-approved write-back,
and the audit trail. It is a consumer of the product. It deploys with `ne up`,
seeds with `ne connect sync fixture`, queries `/v1/brain` and `/v1/chat`, and
governs through the same executor, approval queue and audit chain as any
deployment. It adds no demo-only code paths beyond the labelled demo seams.

## Prerequisites

- Docker with Compose. A non-443 `--https-port` needs Compose 2.24.4 or newer.
- uv 0.8+ and a synced workspace. In this repository run the CLI as
  `uv run --all-packages ne demo ...`; plain `uv run ne demo` fails with
  `ModuleNotFoundError: No module named 'ne.acme'`.
- Outbound network access on the first run. No GPU and no SaaS tenants are
  required for the default run.

**First run is slow.** On a fresh host `ne demo up` downloads about 2.3 GB of
model weights and exports the embedding model to ONNX, which takes tens of
minutes. The result is cached under `$NE_TEI_ONNX_CACHE` and reused on later
runs.

## Commands

```sh
uv run --all-packages ne demo               # up, then run, then prints a teardown hint
uv run --all-packages ne demo up            # deploy + seed the stack
uv run --all-packages ne demo run           # narrated arc; writes DEMO-DOSSIER.md
uv run --all-packages ne demo down          # tear down (add --volumes to delete data)
```

Everything the demo stages (bundle, generated secrets, models, corpus and the
dossier) lives under `--workdir` (default `./ne-demo`). Useful `up` options:

- `--https-port 9443` when host port 443 is taken. Keycloak stays on 8443.
- `--hostname` sets the pilot TLS hostname (default `localhost`).
- `--gpu-vllm <url>` / `--gpu-rerank <url>` point the model and rerank lanes at
  an external GPU vLLM server. Each adds that host to the egress allowlist, a
  deliberate demo-only relaxation of the no-egress posture. Use synthetic data
  only.
- `--tei-onnx-artifact <tar.gz>` stages a prebuilt ONNX export instead of
  generating it on first run.

`ne demo up --help` lists every option.

## Frontier model swap (`--frontier`)

`ne demo up --frontier anthropic|openai` (or `NE_DEMO_FRONTIER`) moves the agent
turns onto a frontier model by configuration only. It appends a frontier model
to the gateway menu, reroutes the chat `agent` task with no fallback, and
enables the governed frontier warm call.

It needs `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` exported on the host and fails
before any Docker call when the key is missing; it never falls back silently to
the open-weight lane. The key is written only to a 0600 overlay env file. Before
the air-gap beat, every frontier-routed task is repointed off its frontier
primary. Run without the flag (and with `NE_DEMO_FRONTIER` unset) to return to
the sealed, CPU-local open-weight lane.

## Live write-back (`--live-writeback`)

By default the write-back beat uses an inert demo tool and says so. With
`--live-writeback` it performs the real governed Salesforce note write against
a Salesforce org you control, which is deliberate demo-only egress. Export the
connector settings on the host first:

```sh
export NE_SALESFORCE_AUTH_METHOD=jwt_bearer
export NE_SALESFORCE_CLIENT_ID=<connected app consumer key>
export NE_SALESFORCE_USERNAME=<integration user>
export NE_SALESFORCE_INSTANCE_URL=https://<your-org>.my.salesforce.com
export NE_SALESFORCE_PRIVATE_KEY_PEM_PATH=/path/to/sf-key.pem
export NE_SALESFORCE_OWNER=demo-ops
export NE_SALESFORCE_OBJECTS='[{"name": "Account", "content_fields": ["Name"]}]'

export NE_M365_TENANT_ID=<tenant id>
export NE_M365_CLIENT_ID=<app id>
export NE_M365_OWNER=demo-ops
export NE_M365_CERTIFICATE_PEM_PATH=/path/to/m365-cert.pem
export NE_M365_PRIVATE_KEY_PEM_PATH=/path/to/m365-key.pem
export NE_M365_SITE_IDS='["<host>.sharepoint.com,<site-guid>"]'   # or NE_M365_DRIVE_IDS
```

The M365 leg needs `NE_M365_SITE_IDS` or `NE_M365_DRIVE_IDS`; the pre-check fails
loudly without one. `NE_SQL_*` is optional (a demo-only SQLite fallback is
injected when absent). PEM files must be readable by the container user. The
target Account is resolved from `salesforce.json` in the seed-manifest
directory (`~/.ne/meridian-seed` unless `--seed-manifest-dir` overrides it).

## Graph population (`--graph`)

`--graph` enables persistent Apache AGE reads and governed M365-to-graph
population for the Meridian M365 target named in `m365.json` in the same
seed-manifest directory. It uses the same `NE_M365_*` settings as above and
requires `--m365-draft-mailbox` unless `--live-writeback` is also set. It does
not enable Salesforce write-back.

## Output

`ne demo run` narrates each beat and writes `DEMO-DOSSIER.md` in the workdir.
A beat whose prerequisite is missing (for example the T2 graph without AGE)
is reported as degraded with the reason; it is never shown as passing.
