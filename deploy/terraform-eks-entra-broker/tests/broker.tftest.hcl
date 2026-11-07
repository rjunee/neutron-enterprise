# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Offline golden-plan guardrails for the re-homed Entra broker root
# (NE-AWS-AZUREAD). Runs with ZERO credentials: mock_provider "azuread" and
# mock_provider "aws" so the eager-auth azuread Configure() never runs and no
# AWS account is required — command = plan throughout. Proves the broker moved
# 1:1 out of deploy/terraform-eks/modules/identity: off-by-default creates
# nothing; the enabled path creates the app/SP/password/secret/secret_version
# with the exact redirect URI, secret name, KMS wiring, and recovery window;
# and the carried-over validation still rejects enable-without-hostname.
#
# NE-BEW-REDIRECT adds: the default redirect-URI list registers BOTH shipped
# tiers (prod auth.<hostname> host-split + pilot <hostname>:8443 port-split,
# design §3.5) with an alias-consistency guard (every URI must target the
# realm identityProvider alias "entra", design §3.4); an explicit
# broker_redirect_uris override is used verbatim and lifts the hostname
# requirement; an override whose alias segment isn't "entra" is rejected at
# plan time via variable validation.
mock_provider "azuread" {}

mock_provider "aws" {}

run "broker_off_creates_nothing" {
  command = plan

  # secrets_kms_key_arn has no default (operator-supplied), so it must be set
  # even on the off path where nothing references it.
  variables {
    secrets_kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/mock"
  }

  assert {
    condition     = length(azuread_application.broker) == 0
    error_message = "broker app must not be created at defaults (enable_entra_broker = false)"
  }

  assert {
    condition     = length(aws_secretsmanager_secret.entra_broker_client_secret) == 0
    error_message = "broker secret must not be created at defaults (enable_entra_broker = false)"
  }
}

run "broker_enabled_shape" {
  command = plan

  variables {
    enable_entra_broker = true
    hostname            = "example.com"
    secrets_kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/mock"
  }

  assert {
    condition     = azuread_application.broker[0].display_name == "ne-eks-smoke-keycloak-broker"
    error_message = "broker app display_name must be <prefix>-keycloak-broker"
  }

  assert {
    condition = anytrue([
      for w in azuread_application.broker[0].web :
      contains(w.redirect_uris, "https://auth.example.com/realms/ne-pilot/broker/entra/endpoint")
    ])
    error_message = "broker redirect URI must derive from auth.<hostname> for the ne-pilot realm"
  }

  assert {
    condition = anytrue([
      for w in azuread_application.broker[0].web :
      contains(w.redirect_uris, "https://example.com:8443/realms/ne-pilot/broker/entra/endpoint")
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

  assert {
    condition     = aws_secretsmanager_secret.entra_broker_client_secret[0].name == "ne-eks-smoke-entra-broker-client-secret"
    error_message = "secret name must be <prefix>-entra-broker-client-secret"
  }

  assert {
    condition     = aws_secretsmanager_secret.entra_broker_client_secret[0].kms_key_id == var.secrets_kms_key_arn
    error_message = "secret must be encrypted with the operator-supplied secrets_kms_key_arn"
  }

  assert {
    condition     = aws_secretsmanager_secret.entra_broker_client_secret[0].recovery_window_in_days == 0
    error_message = "secret recovery_window_in_days must stay 0 (teardown-safe fixed name)"
  }

  assert {
    condition     = length(aws_secretsmanager_secret_version.entra_broker_client_secret) == 1
    error_message = "enabled path must plan exactly one secret version"
  }

  assert {
    condition     = contains(azuread_application.broker[0].group_membership_claims, "ApplicationGroup")
    error_message = "group_membership_claims must include \"ApplicationGroup\" — only groups explicitly assigned to the broker app are emitted, closing the >150-group overage CRIT (design §4.2-1)"
  }

  assert {
    condition     = length(azuread_application.broker[0].group_membership_claims) == 1
    error_message = "group_membership_claims must be exactly [\"ApplicationGroup\"] — SecurityGroup (or All) reintroduces the >150-group silent-under-authz overage (design §4.2-1)"
  }
}

run "reject_enabled_without_hostname" {
  command = plan

  variables {
    enable_entra_broker = true
    secrets_kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/mock"
  }

  expect_failures = [var.enable_entra_broker]
}

run "broker_redirect_override_respected" {
  command = plan

  variables {
    enable_entra_broker  = true
    secrets_kms_key_arn  = "arn:aws:kms:us-east-1:111111111111:key/mock"
    broker_redirect_uris = ["https://sso.example.net/realms/ne-pilot/broker/entra/endpoint"]
    # hostname deliberately UNSET — proves an explicit override lifts the
    # hostname requirement (relaxed validation leg) and is used verbatim, not
    # merged with the derived-from-hostname default.
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

  variables {
    enable_entra_broker  = true
    hostname             = "example.com"
    secrets_kms_key_arn  = "arn:aws:kms:us-east-1:111111111111:key/mock"
    broker_redirect_uris = ["https://sso.example.net/realms/ne-pilot/broker/customer-idp/endpoint"]
  }

  expect_failures = [var.broker_redirect_uris]
}
