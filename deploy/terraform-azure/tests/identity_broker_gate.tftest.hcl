# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Offline golden-plan guardrails for the create_entra_broker gate
# (NE-LP-AZURE-KV-REACH, resolves PR #311 Codex C1). Runs with ZERO Azure
# credentials via mock_provider "azurerm" + mock_provider "azuread" —
# command = plan throughout, module-targeted at ./modules/identity exactly
# like storage_worm.tftest.hcl addresses ./modules/storage. This is the
# executable proof that: (1) the shipped default (create_entra_broker=true)
# plans the broker app + its private Key Vault/PE/RBAC trio, byte-identical
# to pre-unit behavior, WITHOUT weakening the vault's security posture; (2)
# create_entra_broker=false skips all seven of those resources — the exact
# claim that unblocks azure-live-apply.yml's smoke lane — while leaving the
# unrelated Velero identity/federated-credential resources untouched; (3)
# the reuse-vault leg of the boolean matrix (key_vault_id set) is unchanged.

mock_provider "azurerm" {
  # azurerm_key_vault.tenant_id requires a valid UUID; the mock provider's
  # default random-string data-source stub fails that schema validation, so
  # pin data.azurerm_client_config.current to fixed, valid mock values (same
  # pattern as deploy/terraform-eks/tests/hardened.tftest.hcl's mock_data
  # "aws_caller_identity").
  mock_data "azurerm_client_config" {
    defaults = {
      tenant_id       = "00000000-0000-0000-0000-000000000001"
      object_id       = "00000000-0000-0000-0000-000000000002"
      client_id       = "00000000-0000-0000-0000-000000000003"
      subscription_id = "00000000-0000-0000-0000-000000000000"
    }
  }
}
mock_provider "azuread" {}

run "broker_default_on_plans_vault_and_broker" {
  command = plan

  module {
    source = "./modules/identity"
  }

  variables {
    prefix                      = "t"
    location                    = "eastus"
    resource_group_name         = "t-rg"
    private_endpoints_subnet_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/virtualNetworks/t-vnet/subnets/t-pe"
    private_dns_zone_id         = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/privateDnsZones/privatelink.vaultcore.azure.net"
    storage_account_id          = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Storage/storageAccounts/tstorage"
    hostname                    = "t.example.com"
    oidc_issuer_url             = "https://oidc.example.com/t"
    tags                        = {}
    # create_entra_broker deliberately UNSET — locks the shipped default (true).
  }

  # Default posture: all seven broker/vault/PE/RBAC resources plan, exactly
  # as before this unit (behavior parity).
  assert {
    condition     = length(azuread_application.broker) == 1
    error_message = "create_entra_broker default must still plan the broker app registration"
  }

  assert {
    condition     = length(azuread_service_principal.broker) == 1
    error_message = "create_entra_broker default must still plan the broker service principal"
  }

  assert {
    condition     = length(azuread_application_password.broker) == 1
    error_message = "create_entra_broker default must still plan the broker client secret"
  }

  assert {
    condition     = length(azurerm_key_vault_secret.entra_broker_client_secret) == 1
    error_message = "create_entra_broker default must still plan the Key Vault secret write"
  }

  assert {
    condition     = length(azurerm_key_vault.this) == 1
    error_message = "create_entra_broker default must still plan the private Key Vault"
  }

  assert {
    condition     = length(azurerm_private_endpoint.vault) == 1
    error_message = "create_entra_broker default must still plan the vault private endpoint"
  }

  assert {
    condition     = length(azurerm_role_assignment.terraform_secrets_officer) == 1
    error_message = "create_entra_broker default must still plan the Secrets Officer RBAC grant"
  }

  # Do-not-weaken-posture invariant (NE-LP-AZURE-KV-REACH must never touch
  # the vault's security arguments on the default path).
  assert {
    condition     = azurerm_key_vault.this[0].public_network_access_enabled == false
    error_message = "NE-LP-AZURE-KV-REACH must never weaken the shipped vault's public_network_access_enabled posture"
  }

  assert {
    condition     = azurerm_key_vault.this[0].rbac_authorization_enabled == true
    error_message = "NE-LP-AZURE-KV-REACH must never weaken the shipped vault's rbac_authorization_enabled posture"
  }

  assert {
    condition     = contains(azuread_application.broker[0].group_membership_claims, "ApplicationGroup")
    error_message = "group_membership_claims must include \"ApplicationGroup\" — only groups explicitly assigned to the broker app are emitted, closing the >150-group overage CRIT (design §4.2-1)"
  }

  assert {
    condition     = length(azuread_application.broker[0].group_membership_claims) == 1
    error_message = "exactly [\"ApplicationGroup\"] — SecurityGroup (or All) reintroduces the >150-group silent-under-authz overage (design §4.2-1)"
  }
}

run "broker_off_skips_broker_and_private_vault" {
  command = plan

  module {
    source = "./modules/identity"
  }

  variables {
    prefix                      = "t"
    location                    = "eastus"
    resource_group_name         = "t-rg"
    private_endpoints_subnet_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/virtualNetworks/t-vnet/subnets/t-pe"
    private_dns_zone_id         = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/privateDnsZones/privatelink.vaultcore.azure.net"
    storage_account_id          = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Storage/storageAccounts/tstorage"
    hostname                    = "t.example.com"
    oidc_issuer_url             = "https://oidc.example.com/t"
    tags                        = {}
    create_entra_broker         = false
  }

  # THE defect trip-wire (NE-LP-AZURE-KV-REACH / PR #311 Codex C1): the
  # smoke lane's unblocking claim is that ALL SEVEN broker/vault/PE/RBAC
  # resources are skipped, not just the app registration.
  assert {
    condition     = length(azuread_application.broker) == 0
    error_message = "create_entra_broker=false must skip the broker app registration"
  }

  assert {
    condition     = length(azuread_service_principal.broker) == 0
    error_message = "create_entra_broker=false must skip the broker service principal"
  }

  assert {
    condition     = length(azuread_application_password.broker) == 0
    error_message = "create_entra_broker=false must skip the broker client secret"
  }

  assert {
    condition     = length(azurerm_key_vault_secret.entra_broker_client_secret) == 0
    error_message = "create_entra_broker=false must skip the Key Vault secret write"
  }

  assert {
    condition     = length(azurerm_key_vault.this) == 0
    error_message = "create_entra_broker=false must skip the private Key Vault (it exists only to host the broker secret)"
  }

  assert {
    condition     = length(azurerm_private_endpoint.vault) == 0
    error_message = "create_entra_broker=false must skip the vault private endpoint"
  }

  assert {
    condition     = length(azurerm_role_assignment.terraform_secrets_officer) == 0
    error_message = "create_entra_broker=false must skip the Secrets Officer RBAC grant"
  }

  # Velero identity/federated-credential resources are UNRELATED to the
  # broker and must stay untouched.
  assert {
    condition     = azurerm_user_assigned_identity.velero.name == "t-velero-identity"
    error_message = "create_entra_broker=false must not affect the Velero workload identity"
  }

  assert {
    condition     = azurerm_federated_identity_credential.velero.subject == "system:serviceaccount:velero:velero"
    error_message = "create_entra_broker=false must not affect the Velero federated credential"
  }
}

run "reuse_vault_still_creates_broker" {
  command = plan

  module {
    source = "./modules/identity"
  }

  variables {
    prefix                      = "t"
    location                    = "eastus"
    resource_group_name         = "t-rg"
    key_vault_id                = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/other-rg/providers/Microsoft.KeyVault/vaults/other-kv"
    private_endpoints_subnet_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/virtualNetworks/t-vnet/subnets/t-pe"
    private_dns_zone_id         = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/privateDnsZones/privatelink.vaultcore.azure.net"
    storage_account_id          = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Storage/storageAccounts/tstorage"
    hostname                    = "t.example.com"
    oidc_issuer_url             = "https://oidc.example.com/t"
    tags                        = {}
    # create_entra_broker deliberately UNSET — reuse-vault leg of the
    # boolean matrix must be unaffected by this unit.
  }

  assert {
    condition     = length(azurerm_key_vault.this) == 0
    error_message = "reuse-vault leg must still skip creating a new Key Vault"
  }

  assert {
    condition     = length(azuread_application.broker) == 1
    error_message = "reuse-vault leg must still create the broker app registration"
  }

  assert {
    condition     = azurerm_key_vault_secret.entra_broker_client_secret[0].key_vault_id == var.key_vault_id
    error_message = "reuse-vault leg must write the broker secret to the reused key_vault_id, not a locally-created vault"
  }
}
