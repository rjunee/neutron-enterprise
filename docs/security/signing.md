# Release signing (key-based cosign)

Neutron Enterprise release artifacts are signed with a **self-managed cosign
key** — never keyless. Keyless signing (Fulcio + Rekor) requires network egress
at verification time, which breaks offline / air-gapped customer verification
(SPEC C1/C2). This page is the owner runbook for provisioning the key and the
customer instructions for verifying a release offline.

## Owner: provisioning the signing key (one-time)

1. Generate a key pair (cosign v3.0.2, pinned):

   ```sh
   COSIGN_PASSWORD='<a strong passphrase>' cosign generate-key-pair
   # -> cosign.key (private, encrypted) and cosign.pub (public)
   ```

2. Store the **private** key and its password as GitHub Actions **secrets** on
   the repository:

   - `COSIGN_PRIVATE_KEY` — the full contents of `cosign.key`.
   - `COSIGN_PASSWORD` — the passphrase.

   `release.yml` reads these via `env://COSIGN_PRIVATE_KEY` and fails closed if
   `COSIGN_PRIVATE_KEY` is empty ("signing key not provisioned").

3. Commit the **public** key so customers can verify offline:

   ```sh
   cp cosign.pub tools/supply-chain/cosign.pub
   git add tools/supply-chain/cosign.pub && git commit -m "security: add cosign public key"
   ```

   Never commit `cosign.key`. It is git-ignored implicitly by living outside the
   repo; keep it in a secrets manager.

Once provisioned, the next `v*` tag triggers `release.yml`, which builds the
wheels/sdists, aggregates the NOTICE, generates SLSA provenance, and signs each
artifact (and the provenance attestation) with `cosign sign-blob` /
`cosign attest-blob` using the key — emitting offline `*.cosign.bundle` and
`*.att.bundle` files uploaded to the GitHub release.

## What gets signed

For each release artifact (`*.whl`, `*.tar.gz`, `dist/NOTICE`):

- `<artifact>.cosign.bundle` — key-based blob signature (offline bundle).
- `<artifact>.att.bundle` — signed in-toto/SLSA-v1 provenance attestation
  (`--type slsaprovenance1`).
- `dist/provenance/<artifact>.intoto.json` — the raw provenance statement.

## Customer: verifying a release offline

No network, no Rekor/Fulcio. You need the artifact, its two bundles, and the
committed public key (`tools/supply-chain/cosign.pub`).

```sh
tools/supply-chain/verify-release.sh \
  neutron_enterprise-0.1.0-py3-none-any.whl \
  neutron_enterprise-0.1.0-py3-none-any.whl.cosign.bundle \
  neutron_enterprise-0.1.0-py3-none-any.whl.att.bundle \
  tools/supply-chain/cosign.pub
```

The script runs `cosign verify-blob` and `cosign verify-blob-attestation` with
`--insecure-ignore-tlog=true` (the correct flag for key-based signatures not
uploaded to a transparency log — confirmed against cosign v3.0.2). Exit 0 means
the signature and SLSA provenance both verify.

## Flag notes (cosign v3.0.2)

- Sign: `cosign sign-blob --yes --key env://COSIGN_PRIVATE_KEY --tlog-upload=false
  --bundle <out>.cosign.bundle <artifact>`.
- Attest: `cosign attest-blob --yes --key env://COSIGN_PRIVATE_KEY --predicate
  <prov.json> --type slsaprovenance1 --tlog-upload=false --bundle <out>.att.bundle
  <artifact>`.
- Verify: `cosign verify-blob --key cosign.pub --bundle <bundle>
  --insecure-ignore-tlog=true <artifact>`.

The `test_cosign_keybased_roundtrip` test exercises this exact flow (skipped
locally when cosign is absent; runs in CI where `install-scanners.sh` provides
the pinned cosign).
