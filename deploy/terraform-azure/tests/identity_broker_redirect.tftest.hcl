# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Offline golden-plan guardrails for the NE-BEW-REDIRECT two-tier Entra
# broker redirect-URI list (design §3.5). Runs with ZERO Azure credentials
# via mock_provider "azurerm" + mock_provider "azuread" — command = plan
# throughout, module-targeted at ./modules/identity exactly like
# identity_broker_gate.tftest.hcl addresses the same module. Proves: (1) the
# empty-default broker_redirect_uris derives BOTH shipped tiers' actual
# Keycloak external broker URL from var.hostname (prod auth.<hostname>
# host-split + pilot <hostname>:8443 port-split, per
# deploy/compose/pilot/compose.yaml's KC_HOSTNAME) with an alias-consistency
# guard (every URI must target the realm identityProvider alias "entra",
# design §3.4); (2) an explicit broker_redirect_uris override is used
# verbatim; (3) an override URI whose alias segment isn't "entra" is
# rejected at plan time via variable validation.

mock_provider "azurerm" {
  # azurerm_key_vault.tenant_id requires a valid UUID; the mock provider's
  # default random-string data-source stub fails that schema validation, so
  # pin data.azurerm_client_config.current to fixed, valid mock values
  # (credit: identity_broker_gate.tftest.hcl for the UUID-pinning rationale,
  # itself following deploy/terraform-eks/tests/hardened.tftest.hcl's
  # mock_data "aws_caller_identity" pattern).
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

run "broker_registers_both_tier_redirects" {
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
    # broker_redirect_uris deliberately UNSET — locks the empty-default
    # derivation from hostname.
  }

  assert {
    condition = anytrue([
      for w in azuread_application.broker[0].web :
      contains(w.redirect_uris, "https://auth.t.example.com/realms/ne-pilot/broker/entra/endpoint")
    ])
    error_message = "broker redirect URI must derive the prod/Helm form auth.<hostname> for the ne-pilot realm"
  }

  assert {
    condition = anytrue([
      for w in azuread_application.broker[0].web :
      contains(w.redirect_uris, "https://t.example.com:8443/realms/ne-pilot/broker/entra/endpoint")
    ])
    error_message = "broker redirect URI must also register the pilot/compose port-split form (<hostname>:8443), matching compose.yaml's KC_HOSTNAME (design §3.5)"
  }

  assert {
    condition     = anytrue([for w in azuread_application.broker[0].web : length(w.redirect_uris) == 2])
    error_message = "default (no override) must register exactly the two tier forms, nothing more"
  }

  assert {
    condition = anytrue([
      for w in azuread_application.broker[0].web :
      alltrue([for u in w.redirect_uris : can(regex("/broker/entra/endpoint$", u))])
    ])
    error_message = "every registered redirect URI must target the Keycloak broker callback for realm identityProvider alias \"entra\" (design §3.4)"
  }
}

run "broker_redirect_override_respected" {
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
    broker_redirect_uris        = ["https://sso.example.net/realms/ne-pilot/broker/entra/endpoint"]
  }

  assert {
    condition     = anytrue([for w in azuread_application.broker[0].web : length(w.redirect_uris) == 1])
    error_message = "an explicit broker_redirect_uris override must be used verbatim (length 1), not merged with the hostname-derived default"
  }

  assert {
    condition = anytrue([
      for w in azuread_application.broker[0].web :
      contains(w.redirect_uris, "https://sso.example.net/realms/ne-pilot/broker/entra/endpoint")
    ])
    error_message = "an explicit broker_redirect_uris override must be registered verbatim"
  }
}

run "reject_redirect_override_wrong_alias" {
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
    broker_redirect_uris        = ["https://sso.example.net/realms/ne-pilot/broker/customer-idp/endpoint"]
  }

  expect_failures = [var.broker_redirect_uris]
}
