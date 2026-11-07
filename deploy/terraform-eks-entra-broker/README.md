# deploy/terraform-eks-entra-broker

The Entra→Keycloak **broker-only app registration** (`azuread_application` +
`azuread_service_principal` + `azuread_application_password`) plus its
**Secrets Manager writer** (`<prefix>-entra-broker-client-secret`, CMK-backed),
re-homed 1:1 out of `../terraform-eks/modules/identity` by **NE-AWS-AZUREAD**
(2026-07-20). Entra stays the IdP of record; Keycloak brokers only. Wiring the
broker INTO the realm is the **NE-BROKER-ENTRA-WIRE** epic's realm artifact
(`deploy/compose/pilot/keycloak/ne-pilot-realm.json` `identityProviders`
entry, alias pinned `entra` — unit **NE-BEW-REALM**); this root registers the
Entra-side redirect URIs for **both shipped tiers** so the registered URI
matches the actual Keycloak broker callback on either tier
(**NE-BEW-REDIRECT**).

## Why this is a separate root

`hashicorp/azuread` 3.9.0 **eagerly builds and validates an Azure authorizer at
provider `Configure()`** whenever *any* resource references the provider — even
a `count = 0` resource. On any host with `az` present or Azure ambient identity
(every GitHub-hosted runner is an Azure VM with `az` installed but not logged
in), a real `terraform apply` in the AWS-only EKS lane therefore died at
provider-config with `could not configure AzureCli Authorizer: ... Please run
'az login'`, regardless of `enable_entra_broker`. That blocked the
credential-less AWS hardened-reference apply, so the broker was moved OUT of
that lane into this opt-in root, applied separately by an operator who actually
holds Azure creds. The claim now traces to a **test, not a comment** (SPEC §11):
the `eks-provider-config-smoke` job in
`../../.github/workflows/terraform-validate.yml` proves the AWS-only lane no
longer references the provider and applies credential-less, with a negative
control that the original eager-auth repro still fails.

## Prerequisites

- **The main lane applied first** — `../terraform-eks` (this root only adds the
  Entra app + a Secrets Manager secret encrypted with that lane's CMK).
- **Azure creds:** `az login`, or `ARM_*` env, with **`Application.ReadWrite.All`**
  (create/manage the app registration + client secret).
- **AWS creds:** able to write **Secrets Manager** in `var.region`.
- Terraform `>= 1.9.0, < 2.0.0` (built/tested on 1.15.8).

## Inputs

| Variable | Required | Notes |
|---|---|---|
| `enable_entra_broker` | apply switch | `false` (default) creates nothing; pass `-var enable_entra_broker=true` to create the broker. |
| `hostname` | when enabled, unless `broker_redirect_uris` is supplied | Derives the **default** redirect URIs (both tier forms — see below). Validation rejects enable-without-hostname-and-without-override. |
| `broker_redirect_uris` | optional | Default `[]` ⇒ derives both tier forms from `hostname` (see "Redirect URIs (tier split)" below). Set explicitly to override verbatim. Every entry validated to end `/realms/ne-pilot/broker/entra/endpoint`. |
| `prefix` | must match main lane | Default `ne-eks-smoke` — the secret name is `<prefix>-entra-broker-client-secret`; it must line up with `../terraform-eks var.prefix`. |
| `region` | must match main lane | Default `us-east-1` — where the Secrets Manager secret lives; must equal `../terraform-eks var.region`. |
| `secrets_kms_key_arn` | **no default** | The main lane's `eks-secrets` CMK ARN. Obtain it with `terraform -chdir=../terraform-eks output -json kms_key_arns` (key `eks-secrets`). |
| `tags` | optional | Resource tags. |

## Redirect URIs (tier split)

Entra requires the registered redirect URI to match the actual Keycloak
broker callback **exactly**. The two shipped tiers expose Keycloak at
different external forms: prod/Helm serves it at the ingress host-split
`https://auth.<hostname>/...`, while pilot/compose serves it at the
port-split `https://<hostname>:8443/...` (see
`deploy/compose/pilot/compose.yaml`'s `KC_HOSTNAME`). The default
`broker_redirect_uris = []` registers **both** forms, so a pilot-tier
brokered login is not silently broken by a prod-only redirect URI. The
`/broker/entra/endpoint` segment is fixed by the realm identityProvider
alias `entra` (design §3.4) and is enforced both by variable validation and
by the `terraform test` suite.

## Apply runbook

> **⚠️ Configure the encrypted remote backend FIRST.** A bare `terraform init`
> selects the **default local backend**, which writes the broker client secret
> (an `azuread_application_password` attribute) into a plaintext
> `terraform.tfstate` on disk — violating the state-encryption requirement
> below. No backend block is committed to this root (backend-agnostic, same as
> `../terraform-azure`); you must supply your own encrypted remote backend at
> init time and never run the apply against local state.

```
cd deploy/terraform-eks-entra-broker

# 1. Point Terraform at YOUR encrypted remote backend BEFORE anything touches
#    state. Add a `terraform { backend "s3" {} }` block (or an override file),
#    then pass the backend settings via -backend-config. Example (S3 + SSE-KMS,
#    versioned bucket, strict IAM — see "State-encryption requirement" below):
terraform init \
  -backend-config="bucket=<your-tfstate-bucket>" \
  -backend-config="key=terraform-eks-entra-broker/terraform.tfstate" \
  -backend-config="region=<region>" \
  -backend-config="kms_key_id=<state-CMK ARN>" \
  -backend-config="encrypt=true"

# 2. Apply (secret is written to Secrets Manager + transits the ENCRYPTED state):
terraform apply \
  -var enable_entra_broker=true \
  -var hostname=<your-console-vhost> \
  -var secrets_kms_key_arn=<eks-secrets CMK ARN from the main lane>
```

(The offline `terraform test` and CI `terraform init -backend=false` paths do
not touch a backend and are unaffected — the remote backend is a real-apply
requirement only.)

The single non-credential output is `entra_broker_client_id` (the Entra app /
client ID). **The client secret is NEVER an output** (C12) — it is written
straight to Secrets Manager.

## State-encryption requirement (§state)

The client secret transits Terraform state as an `azuread_application_password`
resource attribute (Terraform's own limitation). Any real apply of this root
therefore **REQUIRES an encrypted remote backend** (e.g. an `s3` backend with
SSE-KMS + strict IAM + versioning) — **never local state**. This mirrors
`../terraform-azure/README.md` §10. Never commit `*.tfstate` or a
literal-valued `*.tfvars`.

## Offline test

`tests/broker.tftest.hcl` runs `terraform test` with **zero credentials**:
`mock_provider "azuread"` + `mock_provider "aws"` (so the eager-auth
`Configure()` never runs), `command = plan` throughout. It proves the broker
moved 1:1 — off-by-default creates nothing; the enabled path creates the
app/SP/password/secret/secret_version with secret name, KMS wiring, and
`recovery_window_in_days = 0`; the carried-over validation rejects
enable-without-hostname-and-without-override; and (NE-BEW-REDIRECT) the
default redirect-URI list registers **exactly** the two tier forms with an
alias-consistency guard, an explicit `broker_redirect_uris` override is used
**verbatim** and lifts the hostname requirement, and an override URI whose
alias segment isn't `entra` is **rejected at plan time**.

## Component pins (re-verified 2026-07-20, registry.terraform.io)

| Component | Version | License | Source |
|---|---|---|---|
| `hashicorp/azuread` provider | 3.9.0 (exact) | MPL-2.0 | `registry.terraform.io/providers/hashicorp/azuread` |
| `hashicorp/aws` provider | 6.55.0 (exact) | MPL-2.0 | `registry.terraform.io/providers/hashicorp/aws` |

`.terraform.lock.hcl` carries `h1:` hashes for `linux_amd64`, `linux_arm64`,
`darwin_amd64`, `darwin_arm64` (C11).
