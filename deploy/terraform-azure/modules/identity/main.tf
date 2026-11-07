# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# (a) Key Vault (create-or-reuse), (b) the Entra broker-only app registration
# for Keycloak (design §2.4 — Entra stays the IdP of record; Keycloak brokers;
# the realm-side wiring is the NE-BROKER-ENTRA-WIRE epic's realm artifact,
# unit NE-BEW-REALM, not built here), (c) Velero's workload identity
# (federated to the AKS OIDC issuer).

data "azurerm_client_config" "current" {}

# --- Key Vault (create-or-reuse) -----------------------------------------

locals {
  # The vault/PE/RBAC trio exists ONLY to host the broker secret, so it is
  # gated on BOTH no-reuse-vault AND the broker actually being created —
  # skipping create_entra_broker must skip the vault too, not just the app.
  create_vault = var.create_entra_broker && var.key_vault_id == ""
}

resource "azurerm_key_vault" "this" {
  count                         = local.create_vault ? 1 : 0
  name                          = "${var.prefix}-kv"
  location                      = var.location
  resource_group_name           = var.resource_group_name
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  public_network_access_enabled = false
  tags                          = var.tags
}

locals {
  # one() returns null when the vault is skipped — no [0] index crash.
  key_vault_id = var.key_vault_id != "" ? var.key_vault_id : one(azurerm_key_vault.this[*].id)
}

resource "azurerm_private_endpoint" "vault" {
  count               = local.create_vault ? 1 : 0
  name                = "${var.prefix}-kv-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.private_endpoints_subnet_id
  tags                = var.tags

  private_service_connection {
    name                           = "${var.prefix}-kv-psc"
    private_connection_resource_id = azurerm_key_vault.this[0].id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "vault-zone-group"
    private_dns_zone_ids = [var.private_dns_zone_id]
  }
}

# The Terraform caller needs write access to a freshly-created, RBAC-only
# vault to place the broker secret (below).
resource "azurerm_role_assignment" "terraform_secrets_officer" {
  count                = local.create_vault ? 1 : 0
  scope                = azurerm_key_vault.this[0].id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

# --- Entra broker-only app registration (design §2.4) ---------------------
#
# Entra remains the IdP of record; Keycloak is a broker only. This creates
# the Entra-side app registration and writes its client secret DIRECTLY to
# Key Vault — never to a Terraform output. Wiring the broker INTO the realm
# is the NE-BROKER-ENTRA-WIRE epic's realm artifact
# (deploy/compose/pilot/keycloak/ne-pilot-realm.json identityProviders entry,
# alias pinned `entra` — unit NE-BEW-REALM); this module registers the
# Entra-side redirect URIs for BOTH shipped tiers so the registered URI
# matches the actual Keycloak broker callback on either tier
# (NE-BEW-REDIRECT).

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
  count            = var.create_entra_broker ? 1 : 0
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
  count     = var.create_entra_broker ? 1 : 0
  client_id = azuread_application.broker[0].client_id
}

resource "azuread_application_password" "broker" {
  count          = var.create_entra_broker ? 1 : 0
  application_id = azuread_application.broker[0].id
  display_name   = "ne-terraform-managed"
}

resource "azurerm_key_vault_secret" "entra_broker_client_secret" {
  count        = var.create_entra_broker ? 1 : 0
  name         = "entra-broker-client-secret"
  value        = azuread_application_password.broker[0].value
  key_vault_id = local.key_vault_id
  tags         = var.tags

  depends_on = [
    # RBAC propagation: the Secrets Officer grant must exist before the write.
    azurerm_role_assignment.terraform_secrets_officer,
    # Ordering: with public_network_access_enabled = false on a freshly
    # created vault, this data-plane write only succeeds over the private
    # endpoint — force the PE (and its inline private_dns_zone_group) to
    # exist first, or the first apply races the endpoint creation and fails.
    # Both referenced resources are count=0 when reusing an existing vault
    # (var.key_vault_id != "") — and, as of NE-LP-AZURE-KV-REACH, also
    # count=0 when var.create_entra_broker = false — which Terraform
    # handles as a no-op either way.
    azurerm_private_endpoint.vault,
  ]
}

# --- State-address preservation (NE-LP-AZURE-KV-REACH C1) ------------------
#
# Before this unit, the four broker resources above were unconditional
# singletons (no `count`), so any existing deployment applied pre-unit has
# them in state as bare addresses (e.g. `azuread_application.broker`).
# Adding `count = var.create_entra_broker ? 1 : 0` moves their address to
# the [0]-indexed form. On the default (create_entra_broker = true) upgrade
# path this must be a pure state rename, not a destroy/recreate — a
# destroy/recreate here would rotate the broker's Entra client ID and
# secret and break an already-wired Keycloak realm. These `moved` blocks
# make that upgrade a no-op plan for anyone already on the singleton
# addresses; they are also no-ops for a fresh apply (nothing to move from).
moved {
  from = azuread_application.broker
  to   = azuread_application.broker[0]
}

moved {
  from = azuread_service_principal.broker
  to   = azuread_service_principal.broker[0]
}

moved {
  from = azuread_application_password.broker
  to   = azuread_application_password.broker[0]
}

moved {
  from = azurerm_key_vault_secret.entra_broker_client_secret
  to   = azurerm_key_vault_secret.entra_broker_client_secret[0]
}

# --- Velero workload identity (federated to the AKS OIDC issuer) ---------

resource "azurerm_user_assigned_identity" "velero" {
  name                = "${var.prefix}-velero-identity"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "velero" {
  name                = "${var.prefix}-velero-fedcred"
  resource_group_name = var.resource_group_name
  audience            = ["api://AzureADTokenExchange"]
  issuer              = var.oidc_issuer_url
  parent_id           = azurerm_user_assigned_identity.velero.id
  subject             = "system:serviceaccount:velero:velero"
}

resource "azurerm_role_assignment" "velero_storage" {
  scope                = var.storage_account_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.velero.principal_id
}
