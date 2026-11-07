# 0021 — Closed inference-target operator contract

## Status

Accepted (2026-08-20; PRE-DEMO-INFERENCE-LIFECYCLE, SPEC §13).

## Context

The Models view must start and stop two independently managed inference targets:
the local `vllm-cpu` Compose service and the existing private Vast GPU rig. The
API cannot safely inherit either mechanism. Giving `ne-api` the Docker socket,
host root, a shell, SSH, provider credentials, or caller-selected commands would
turn a thin governed HTTP surface into a general host-control plane.

The safe design therefore has two processes with different trust domains. The
unprivileged API executes the existing PDP → deterministic guardrails → approval
→ audit path. A narrowly privileged local host operator maps target-scoped
lifecycle requests onto a closed manifest and fixed adapters. They need one
typed seam that can evolve independently without allowing the API, browser, or
boot registry to select any executable detail.

`ne-contracts` was frozen at Checkpoint A. Adding a Protocol consumed by both
processes requires an additive version bump, an ADR, and a conformance kit.

## Decision

1. Add `InferenceTargetOperator` with four typed async methods:
   `describe`, `status`, `start`, and `stop`. The three operation methods accept
   distinct strict request models. Each request model pins its operation with a
   `Literal`, so a stop cannot be sent through the start method or vice versa.

2. Requests carry only protocol version, a bounded lowercase opaque target id,
   an optional/required bounded transition id, and the pinned operation. Slash,
   backslash, whitespace, shell metacharacters, URL forms, and unbounded text are
   rejected by the identifier grammar. There is no extension bag.

3. `describe` projects the host's private manifest as target, adapter, and typed
   resource identifiers; declared `status`/`start`/`stop` capabilities; and the
   lowercase SHA-256 of the complete private manifest. The projected resource id
   is opaque typed data such as `compose:vllm-cpu`, never a path or endpoint.
   Every target must declare `status`, capabilities are unique, and target ids
   are unique.

4. An observation carries only the correlated safe ids, operation, transition
   id, closed lifecycle state, closed outcome code, manifest hash, and a
   timezone-aware observation timestamp. There is deliberately no free-form
   message/detail, command, path, executable digest, argv, environment, endpoint,
   provider payload, credential, stdout, or stderr field. An unknown target has
   `unknown` state and cannot claim manifest adapter/resource ids. An accepted
   transition has an id and is visibly `starting` or `stopping`.

5. The host manifest—not this contract and not a request—owns canonical absolute
   executable paths, accepted executable digests, ownership/mode requirements,
   fixed operation-to-argv mappings, minimal environment, deadlines, secret-file
   resolution, Compose admission, and provider allowlists. The manifest hash is
   evidence of that private configuration, not permission to send its contents
   across this boundary.

6. Ship `InferenceTargetOperatorContractKit`. A binding supplies a fresh real
   adapter/client and a test-only mutation counter. The kit checks stable
   manifest projection, target/operation/transition correlation, status purity,
   start behavior, unknown-target refusal, and—critically—that retrying the same
   start transition performs only one provider/Compose mutation. Returning the
   same-shaped response twice without the counter would be vacuous evidence.

7. This is an additive MINOR contract bump, 1.17.0 to 1.18.0. Existing Protocols
   and value models are unchanged. The new surface is not part of the historical
   32-Protocol SPEC §4 freeze inventory; it is the explicitly added §13
   inference-lifecycle seam.

## Rejected alternatives

- **Docker socket or subprocess execution inside `ne-api`.** Rejected: either is
  host-root-equivalent and bypasses the required confined operator boundary.
- **A generic `execute(command, argv, env)` Protocol.** Rejected structurally:
  validation cannot make a general execution primitive target-scoped, and it
  would let configuration or the browser expand authority.
- **Send script paths or executable digests for the host to validate.** Rejected:
  the requester must not select either side of the comparison. Both the canonical
  path and accepted digest set belong to immutable server-owned host policy.
- **One request with a caller-selected operation string.** Rejected: separate
  request types plus `Literal` pinning make cross-method operation confusion
  invalid before transport.
- **Free-form error messages.** Rejected: child/provider output can contain
  endpoints, secret paths, credentials, or tenant detail. Consumers branch only
  on the closed safe-code enum.
- **Infer idempotency from duplicate response equality.** Rejected as vacuous: a
  binding can execute twice and still return equal values. The kit observes the
  mutation count at the binding boundary.

## Consequences

The API and host operator can now be implemented in disjoint packages against a
single additive contract. A host transport that is absent, malformed, stale, or
manifest-mismatched can fail closed without inventing a command fallback. Future
fixed adapters can add manifest target rows without changing the Protocol or the
React client.

The contract alone does not provide privilege confinement, UDS authentication,
manifest file verification, lifecycle persistence, governance, AIR_GAP refusal,
Compose/GPU execution, readiness proofs, or console controls. Those remain
required PRE-DEMO-INFERENCE-LIFECYCLE consumers and cannot claim completion from
this foundation PR.
