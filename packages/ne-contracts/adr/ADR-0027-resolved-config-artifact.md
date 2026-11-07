# ADR-0027: Resolved platform configuration artifact

Status: Accepted

## Context and threat model

Historical replay needs the configuration value represented by `config_hash`, not
only its digest. The artifact is persisted outside the settings process, so it
must not contain credentials, raw environment snapshots, settings-source
metadata, or mutable references to a live model. Plain-string DSNs, malformed URL
authorities, credential-shaped nested fields, and secret Pydantic types are all
treated as hostile inputs. An artifact containing such material would turn the
configuration history into a credential oracle.

## Decision

`canonical_platform_config_tree(modules)` returns a detached JSON value built
only from the explicit, resolved module models. Object keys are recursively
sorted. `SecretStr` and `SecretBytes` use Pydantic's fixed `**********` mask;
valid and conservatively recognized malformed URL userinfo uses `***:***`.
Plain values under credential-shaped keys and raw-environment projections fail
closed. The function never reads `os.environ` and does not retain model or source
metadata.

`compute_platform_config_tree_hash(tree)` masks URL userinfo, validates the same
fail-closed rules, rejects non-JSON and non-finite values, canonicalizes object
order, and hashes the exact compact ASCII JSON value. Consequently:

```text
compute_platform_config_hash(modules)
  == compute_platform_config_tree_hash(canonical_platform_config_tree(modules))
```

`compute_platform_config_hash` delegates through exactly that tree-to-hash path.
Changing a non-masked artifact value changes the recomputed hash; reordering
objects does not. Callers that persist the returned tree must treat it as an
immutable value and copy rather than mutate it. The contract itself returns a
fresh detached tree on every call.

## Compatibility

The seams are additive and existing valid platform inputs retain their hash.
Unsafe plain credential projections now fail closed. `ne-contracts` advances
from 1.23.0 to 1.24.0; frozen Protocol method sets are unchanged.
