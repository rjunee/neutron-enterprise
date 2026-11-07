# Contributing to Neutron Enterprise

Neutron Enterprise is licensed under the Apache License 2.0. This document is the
reference for local setup and the repo-wide policies every change must honor.
By submitting a contribution you agree to license it under the same terms.

## Development setup

Requires [uv](https://docs.astral.sh/uv/) (0.8+) and the pinned Python
interpreter (see `.python-version`; uv will fetch it).

```sh
uv sync --all-packages       # THE canonical bootstrap — installs every
                             # workspace member + the dev tool group into .venv
uv run pre-commit install    # wire the git hooks
```

`uv sync --all-packages` is required before the check commands work: it installs
all workspace packages (editable) into `.venv`. A plain `uv run pytest` on a
fresh clone that has **not** been synced with `--all-packages` will fail with
`ModuleNotFoundError`, because `uv run` performs an inexact sync that does not
install the other workspace members. Always bootstrap with
`uv sync --all-packages` first.

### Everyday commands

```sh
uv run pytest            # tests (globbed testpaths auto-discover new packages)
uv run pyright           # type check (standard mode)
uv run ruff check .      # lint
uv run ruff format .     # format
uv run ne version        # the dev CLI
uv run ne check headers  # source-header policy check
```

Note on dev-time network egress: `pyright`'s PyPI wrapper downloads a Node
runtime on first run, and `pre-commit` fetches its hook repositories on first
run. Both are **development-time only** — nothing in the shipped platform phones
home (constraints C1/C2). No runtime component in this repo performs egress.

## Source-header policy

Every `.py` file in `packages/`, `tools/`, and `apps/` MUST begin (within its
first 5 lines) with this exact two-line header:

```python
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
```

The TypeScript console (`packages/ne-console`) uses the `//` equivalent:

```ts
// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
```

`py.typed` marker files and non-`.py` files (Markdown, TOML, YAML) are exempt.

This is enforced by `ne check headers`, which runs both as the `ne-check-headers`
pre-commit hook and as `test_check_headers_whole_repo` in the test suite. A file
passes iff one of its first 5 lines contains `Copyright (c)` and one contains
`Ryan Junee`.

## NOTICE policy (per-package, no root NOTICE)

There is **no root `NOTICE` file**. Each package owns a `NOTICE` listing only the
third-party runtime components *it* pulls in. Format:

```
Neutron Enterprise — <dist-name>
Copyright (c) 2026 Ryan Junee.

Third-party components used by this package (name / version / license / homepage):
<name> / <version> / <license> / <homepage>
```

Use `(none)` when a package has no third-party runtime deps. The worked example
is `tools/ne-cli/NOTICE`, which lists `click`. `ne release notice` aggregates them at release
time; do not hand-maintain a combined file.

## Adding a new package

Workspace members are declared by **glob** in the root `pyproject.toml`
(`members = ["packages/*", "apps/*", "tools/*"]`), so adding a package needs
**zero edits to any root file**:

1. Copy an existing stub package under `packages/ne-<name>/` (or `tools/`).
2. Rename the dist to `ne-<name>`, the module to `ne.<mod>` (hyphens become
   underscores), and update its `[tool.uv.build-backend] module-name`.
3. Add the header to every `.py`, a `README.md`, and a `NOTICE`.
4. Do **not** add `src/ne/__init__.py` — `ne` is a PEP 420 implicit namespace
   package; a stray `__init__.py` would break every other package's imports.
5. `uv sync --all-packages` to install it.

## Dependency-intake checklist

Every new dependency must clear all of:

- OSI license verified at the pinned version, in a class allowed by C4 (banned:
  AGPL, SSPL, BUSL, FSL, fair-code, non-commercial model weights).
- Version pinned + hash-verified (via `uv.lock`; never hand-edit the lock).
- CISA-KEV / security-history check.
- Maintainer-viability check (bus factor, release cadence).
- No telemetry, or telemetry disabled by default.

Record the dependency in the owning package's `NOTICE`.

## Ground rules

- Every change ships with tests that discriminate the property it claims.
- Modules interact only through `ne-contracts` Protocols and typed events; changes to a
  contract are additive and need an ADR (`docs/adr/`) plus a contract version bump.
- Never hand-edit `uv.lock`. Only `uv lock` / `uv sync` may write it.
- Never weaken a guard, skip a promoted proof, or broaden an exception to make CI green.
- Python 3.12+ everywhere (`requires-python = ">=3.12"`); dev interpreter pinned
  in `.python-version`.
