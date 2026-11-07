# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Entra broker-only app registration + client secret (re-homed 2026-07-20,
# NE-AWS-AZUREAD).
#
# Moved 1:1 out of deploy/terraform-eks/modules/identity/main.tf section (B):
# the AWS-only EKS lane could never `terraform apply` on a credential-less
# runner because merely REFERENCING the azuread provider (even at count = 0)
# forces azuread 3.9.0 to eager-auth an Azure authorizer at Configure() — see
# providers.tf for the full defect story and the CI job that proves it. So the
# broker now lives in this separate opt-in root, applied by an operator who
# actually holds Azure creds. The resource types/names/args and the count-gate
# are byte-preserved from the original module; the only change is that
# secrets_kms_key_arn now arrives as an operator-supplied root variable.
#
# Copied originally from deploy/terraform-azure/modules/identity/main.tf
# (design §1 row 9 — the azuread provider is cloud-of-deployment-agnostic; Entra
# ID is not an Azure-infrastructure resource, so the same resources apply
# unchanged regardless of which cloud hosts the cluster). Entra stays
# IdP-of-record, Keycloak brokers only. Wiring the broker INTO the realm is
# the NE-BROKER-ENTRA-WIRE epic's realm artifact
# (deploy/compose/pilot/keycloak/ne-pilot-realm.json identityProviders entry,
# alias pinned `entra` — unit NE-BEW-REALM); this root registers the
# Entra-side redirect URIs for BOTH shipped tiers so the registered URI
# matches the actual Keycloak broker callback on either tier
# (NE-BEW-REDIRECT).
#
# Count-gated behind var.enable_entra_broker (default false): in this root the
# flag is the apply switch — pass `-var enable_entra_broker=true` to create the
# broker. Default false preserves the pre-move gate 1:1 and lets `terraform
# test` (mocked, zero credentials) cover both paths offline.

# Entra requires the registered redirect URI to match the actual Keycloak
# broker callback EXACTLY. The two shipped tiers expose Keycloak at different
# external forms: prod/Helm serves it at the ingress host-split
# https://auth.<hostname>/... (prod-tier design §1.9), while pilot/compose
# serves it at the port-split https://<hostname>:8443/... (compose.yaml's
# KC_HOSTNAME). So the default registers BOTH — prod form first, preserving
# the pre-existing single-entry behavior for anyone already deployed — unless
# var.broker_redirect_uris supplies an explicit override (used verbatim).
# (NE-BEW-REDIRECT, design §3.5)
locals {
  broker_redirect_uris = (
    length(var.broker_redirect_uris) > 0 ? var.broker_redirect_uris : [
      "https://auth.${var.hostname}/realms/ne-pilot/broker/entra/endpoint",
      "https://${var.hostname}:8443/realms/ne-pilot/broker/entra/endpoint",
    ]
  )
}

resource "azuread_application" "broker" {
  count = var.enable_entra_broker ? 1 : 0

  display_name     = "${var.prefix}-keycloak-broker"
  sign_in_audience = "AzureADMyOrg"

  # ApplicationGroup emits ONLY groups explicitly assigned to this app
  # registration — SecurityGroup emitted EVERY security group the user
  # belongs to, and Entra OMITS the whole groups claim (emitting a
  # _claim_names/_claim_sources overage pointer instead) past 150 (SAML) /
  # 200 (JWT) groups, silently under-authorizing exactly the most-privileged
  # admins.
  # Customers MUST assign their platform-role groups to this app registration
  # (runbook: deploy/compose/pilot/keycloak/README.md, "Group overage").
  group_membership_claims = ["ApplicationGroup"]

  web {
    redirect_uris = local.broker_redirect_uris
  }
}

resource "azuread_service_principal" "broker" {
  count = var.enable_entra_broker ? 1 : 0

  client_id = azuread_application.broker[0].client_id
}

resource "azuread_application_password" "broker" {
  count = var.enable_entra_broker ? 1 : 0

  application_id = azuread_application.broker[0].id
  display_name   = "ne-terraform-managed"
}

# The Key-Vault-write twin. CMK-backed (C12, NE-AWS-4 plane); reachable
# in-VPC via NE-AWS-3's secretsmanager interface endpoint (C2).
resource "aws_secretsmanager_secret" "entra_broker_client_secret" {
  count = var.enable_entra_broker ? 1 : 0

  name       = "${var.prefix}-entra-broker-client-secret"
  kms_key_id = var.secrets_kms_key_arn
  tags       = var.tags

  # recovery_window_in_days = 0 because the live evidence lane defaults
  # teardown=true and a 30-day deletion tombstone on this fixed name would
  # break the next apply; the value is Terraform-regenerated on every apply
  # that creates it, so immediate deletion loses nothing.
  recovery_window_in_days = 0
}

# NEVER a Terraform output (C12) — the value still transits Terraform state
# as an azuread_application_password attribute (Terraform's own limitation),
# see README's state-encryption note for the encrypted-remote-backend
# requirement when this is enabled.
resource "aws_secretsmanager_secret_version" "entra_broker_client_secret" {
  count = var.enable_entra_broker ? 1 : 0

  secret_id     = aws_secretsmanager_secret.entra_broker_client_secret[0].id
  secret_string = azuread_application_password.broker[0].value
}
