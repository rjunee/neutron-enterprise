# ne-acme

The Acme Corp synthetic corpus generator + recorded CI fixtures (dist
`ne-acme`, import `ne.acme`, CLI `ne-acme`). NE-074.

This is Phase-6's demo/eval company: a bounded, deterministic, internally
coherent "Acme Corp" dataset that conforms to the frozen ne-schema (NE-004)
operational models and carries a materialized permission structure, so the
ACL-trimming / permission-trap evals downstream (NE-075) have real ground
truth to test against.

## Bounds (fixed — this is a demo fixture, not a load test)

| Entity                | Count | Notes |
|------------------------|------:|-------|
| People (org chart)     |    40 | Executive 5, Engineering 10, Sales 8, Support 6, Finance 5, HR 3, Legal 3 |
| Documents              |   500 | policy 60, report 150, contract 120, correspondence 120, presentation 50 |
| Tickets                |    50 | |
| Accounts               |   120 | incl. Acme itself (`acct-0000`) |
| Orders                 |    80 | |
| **CRM records (accounts+orders)** | **200** | |

Every record validates against `ne.schema`'s `OperationalRecordSet` /
`Account` / `Person` / `Document` / `Ticket` / `Order` models — including their
regex validators (`domain`, `email`, `order_number`, `currency`). Anything
those frozen, `extra="forbid"` models don't cover (departments, managers,
groups, ACLs, ground-truth facts) lives in `ne.acme.models` sidecar Pydantic
models, never as extra fields.

## Determinism contract

Same seed -> byte-identical corpus. This holds because:

- `ne.acme.rand.DetRandom` is built **only** on `random.Random.random()` — the
  one primitive CPython documents as cross-version stable given a fixed seed.
  `randint` / `choice` / `shuffle` / `sample` are reimplemented on top of it
  (explicit Fisher-Yates, cumulative-weight selection) rather than called
  directly, because their internal algorithms are *not* guaranteed stable
  across Python releases.
- A fixed `BASE_TIME` (2026-01-05T09:00:00Z) — never `datetime.now()`.
- A fixed generation order: **org -> crm -> documents -> tickets**. Reordering
  changes every subsequent `.random()` draw and therefore every output byte.
- Canonical JSON serialization: `sort_keys=True, indent=2, ensure_ascii=True`,
  trailing newline, UTF-8. No set iteration anywhere in the generation
  pipeline (dedup via sets is always followed immediately by `sorted()`, or
  avoided via fixed-order tuples).

`ne-acme check` regenerates in memory and byte-diffs against the committed
fixtures — the CI staleness gate (mirrors `ne.schema.codegen.check_freshness`).

## Artifacts (6 files, `tools/acme/fixtures/acme-v1/`)

| File                | Contents |
|---------------------|----------|
| `records.json`      | `OperationalRecordSet.model_dump(mode="json", exclude_none=True)` — the ne-schema-conformant accounts/people/documents/tickets/orders |
| `content.json`      | `{doc_id: markdown_body}` |
| `org.json`          | `OrgChart` — 40 `OrgMember`s (department/role/manager/grade) + `Group`s (`all-employees`, `executive`, `dept-<slug>` x7) |
| `acl.json`          | list of `AclEntry` — exactly one per document |
| `ground_truth.json` | `GroundTruth` — one `DocGroundTruth` per doc: materialized `reader_ids` + 1-3 `Fact`s (statement/question/answer) |
| `manifest.json`     | `CorpusManifest` — seed, generator, counts, and the sha256 of the other five files' exact bytes |

## ACL semantics

Every document gets **exactly one** `AclEntry`, chosen by weighted visibility
(~30% public / ~45% department / ~15% exec_only / ~10% named):

| Visibility   | `allowed_groups`                          | `allowed_people` |
|--------------|--------------------------------------------|-------------------|
| `public`     | `["all-employees"]`                        | `[]` |
| `department` | `["dept-<owner's dept>", "executive"]`      | `[]` |
| `exec_only`  | `["executive"]`                             | `[]` |
| `named`      | `[]`                                        | 2-5 person ids, always including the document's `owned_by` |

**Rule: execs see every `department`-visibility doc** (the `department` case
always adds `"executive"` to `allowed_groups`); only `named` docs can exclude
executives. `ground_truth.json`'s `reader_ids` for each doc is the fully
materialized, sorted union of every allowed group's members plus
`allowed_people` — this is the ground truth, not a hint to re-derive
differently.

## Ground-truth coherence

Account/order-specific facts (warranty, renewal) are generated **only** for
documents whose `about` field links that account; all other documents draw
exclusively from account-agnostic templates. The renewal year is a pure
function of the account id (`2024 + id % 3`, no RNG draw) so the renewal
question has exactly one answer per account corpus-wide. Consequently every
question in `ground_truth.json` has exactly one corpus-wide answer — enforced
by `tests/test_fact_coherence.py`.

## Consumption

- **NE-102** (dev-tenant seeding): loads `records.json` (+ `content.json` for
  document bodies) directly into a dev tenant. Live seeding beyond fixtures is
  NE-102's scope, not this unit's.
- **NE-075** (golden set / permission-trap eval): builds Q&A golden cases from
  `ground_truth.json`'s `Fact`s (every `fact.answer` is a literal substring of
  `content[fact.doc_id]`) and permission-trap cases from `reader_ids` — any
  `(person_id, doc_id)` pair where `person_id` is a real person **not** in that
  doc's `reader_ids` is a real "must not see" ground-truth pair. Golden-set
  answers are conflict-free by construction (see Ground-truth coherence).

## Obviously-synthetic guarantees

- All emails end in `@acme.example`; all account domains end in `.example`
  (RFC 2606 reserved TLD — never a real registrable domain).
- All phone numbers match `+1-555-01\d{2}` (the reserved fictional
  555-0100..0199 block).
- Embedded SSN-shaped strings are always `000-\d{2}-\d{4}` — area code `000`
  is never issued, so these can never collide with a real SSN.
- Products are obviously-fictional Acme joke items (Anvil Pro X, Rocket
  Skates 2000, Portable Hole Kit, ...).

These are mechanically enforced by `tests/test_synthetic_safety.py`.

## CLI

```
ne-acme generate [--seed 74] [--out PATH]   # default PATH: see below
ne-acme check [--seed 74] [--fixtures PATH] # exit 1 + prints drifted file names on drift
```

Default `PATH`/`--fixtures` (two-tier, NE-CLEAN3 item 3 — no `parents[N]`
repo-layout assumption that would break a pip-installed wheel): (1) the
editable-checkout copy, `tools/acme/fixtures/acme-v1`, tried FIRST so dev/CI
is always immune to any stale installed copy; (2) if that doesn't exist, the
wheel-data-scheme install location (`$prefix/acme-v1` — the fixtures ship as
wheel data via `[tool.uv.build-backend] data` in `tools/acme/pyproject.toml`,
resolved at runtime with `sysconfig.get_paths()['data']`), for a pip-installed
`ne-acme`.

## Regenerating the fixtures

```
uv run ne-acme generate --out tools/acme/fixtures/acme-v1
uv run ne-acme check   # must print "Acme fixtures are fresh"
```

Only regenerate (and re-commit) the fixtures if a generator change is
intentional — a silent diff here means the byte-identical-fixtures invariant
broke.
