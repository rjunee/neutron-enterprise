# ne-skills-cc — the Claude-Code SkillSubstrate binding (NE-SKILL-5, C5)

`ClaudeCodeSkillSubstrate` is the **second** implementation of the frozen
`SkillSubstrate` Protocol (`ne-contracts` 1.8.0, `ne.contracts.skills`), next
to `PydanticAiSkillSubstrate` (`packages/ne-skills/src/ne/skills/substrate.py`),
which **remains production**. This package exists to *prove* that swapping the
orchestrating stack means writing one new binding — with `ne-skills` core
untouched — turning "any stack we support" from an asserted claim into a
CI-enforced one (`tests/test_swap_c5.py`, the C5 dual-binding swap test).

It lives in its own package (not inside `ne-skills`) per ADR-0011 D-9/R-19: the
`ne-skills` core's zero-substrate-import rule (its own
`tests/test_import_boundary.py`) stays enforceable only if no substrate binding
lives inside it. `ne.skills_cc` is itself probe-checked substrate-pure by this
package's own `tests/test_import_boundary.py`.

## Filesystem layout contract

A Claude-Code-style deployment root:

```
<root>/
  surfaces/
    <quoted-surface-key>/
      .claude/skills/<pack-name>/SKILL.md   # one file per skill — see below
  transcripts/
    *.jsonl                                  # one completed CC session per file
  hooks/
    invocations.jsonl                        # PostToolUse-hook append buffer
```

- `surface_key` is URL-quoted (`urllib.parse.quote(key, safe="")`) into one
  filesystem-safe, reversible, traversal-safe path component
  (`ne.skills_cc.store.surface_dir`).
- `materialize`/`withdraw` (`ne.skills_cc.store`) are a SET-REPLACE projection:
  the registry (on the pydantic-ai side) is the source of truth for what an
  active set IS; this store only reflects an already-decided pack list onto
  disk. It makes no authorization decision (ONE-ACL §7.7).
- `snapshots`/`collect_invocations` (`ne.skills_cc.journal`) fold completed
  workflows and skill firings from the transcript/hook files — pure functions
  over a root path, no substrate state.

## Native `metadata.neutron.*` SKILL.md mapping

`ne.skills_cc.native` projects a `SkillPack` to the SKILL.md a real Claude Code
install would read/write: the top-level frontmatter carries ONLY the
cross-agent trio (`name`, `description`, `version`), and every Neutron-specific
field (`source`, `triggers`, `category`, `tags`, `requires_tools`, `platforms`,
`acl_labels`, `provenance`) nests under `metadata.neutron.*` — the SPEC §641
portability invariant. This is a DIFFERENT mapping from
`ne.skills.packio` (the core's flat frontmatter, used for the FORGE/apply
storage path and bundled-skill authoring) — `packio` is untouched by this
package. The round-trip is byte-stable: `serialize_native(parse_native(serialize_native(p)))
== serialize_native(p)`, and pack-identity-stable through both hashes
(`content_hash`/`routing_hash`).

## Honesty: procedural-skills-only (v1)

This binding does **not** support executable skill artifacts. It writes and
reads only `SKILL.md` documents — no `scripts/` directory, no exec bits, no
second file in a skill directory, ever (test-tripwired in `tests/test_store.py`).
This matches the two deliberate ADR-0011 deviations. **Do not** read "any
stack" as "any capability": the C6 governed-tool chokepoint remains the only
path from a skill to an actual tool call, on every binding, including this
one.

## C3: the author subprocess must be pointed at the gateway

`ne.skills_cc.subprocess_dispatch.claude_cli_author_dispatch(*, gateway_base_url, ...)`
shells out to the Claude Code CLI (`claude -p` by default) to author a skill
draft. `gateway_base_url` is **required** and non-empty — an empty value raises
`ValueError` before any process is spawned. The returned dispatcher sets
`ANTHROPIC_BASE_URL` in the child process's environment to that value, per
design §3.5/§10-Q8: a bare, provider-locked subprocess (defaulting to
Anthropic's public API) is never permitted here. This module imports no
provider SDK (`anthropic`/`litellm`) — the model call happens entirely inside
the external `claude` process, which is required to be pointed at ne-gateway.

## R-26: telemetry-model honesty

On `PydanticAiSkillSubstrate`, the `read.skills.view` handler is the SOLE
invocation writer — `collect_invocations` there mostly drains an
already-empty stream. On THIS binding, `collect_invocations` is a REAL
out-of-band drain of a PostToolUse-hook JSONL buffer a live deployment's hook
script appends to independently. The two bindings' internal WRITE paths differ
by design; the C5 swap test (`tests/test_swap_c5.py`) asserts
Protocol-**observable** behavior only, never internal write-path identity.

## Entry point

Registered under `ne.plugins.skill_substrate` as `claude-code`:

```toml
[project.entry-points."ne.plugins.skill_substrate"]
claude-code = "ne.skills_cc.substrate:ClaudeCodeSkillSubstrate"
```

## Parked acceptance item (LIVE)

Every test in this package runs against local files and local stub
subprocesses (C2: no egress). An actual `claude -p` authoring run through
`claude_cli_author_dispatch`, pointed at a **live** `ne-gateway` base URL, that
returns a draft `parse_author_output` accepts, is the explicitly parked LIVE
acceptance item for the orchestrator's pre-merge pass — it is not, and cannot
be, claimed from CI fixtures alone.
