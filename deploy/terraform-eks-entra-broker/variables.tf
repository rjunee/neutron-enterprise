# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

variable "prefix" {
  description = "Short resource-name prefix. MUST match the main lane's prefix (deploy/terraform-eks var.prefix) so the secret name <prefix>-entra-broker-client-secret lines up with what the cluster/chart expects."
  type        = string
  default     = "ne-eks-smoke"
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "region" {
  description = "AWS region where the Secrets Manager secret lives — MUST match the main lane's region (deploy/terraform-eks var.region)."
  type        = string
  default     = "us-east-1"
}

variable "hostname" {
  description = "The console/API vhost (chart global.hostname analogue); derives the DEFAULT Entra broker redirect URIs (both tier forms — see broker_redirect_uris) when broker_redirect_uris is left at its empty default. REQUIRED when enable_entra_broker = true, unless broker_redirect_uris is supplied explicitly."
  type        = string
  default     = ""
}

variable "broker_redirect_uris" {
  description = <<-EOT
    Redirect URIs registered on the Entra broker app. Empty default (the
    normal case) DERIVES both shipped tiers' actual Keycloak external broker
    URL from var.hostname: the prod/Helm ingress host-split form
    (https://auth.<hostname>/realms/ne-pilot/broker/entra/endpoint) AND the
    pilot/compose port-split form (https://<hostname>:8443/realms/ne-pilot/broker/entra/endpoint
    — matches deploy/compose/pilot/compose.yaml's KC_HOSTNAME, which is
    https://<NE_PILOT_HOSTNAME>:8443). Entra requires the registered redirect
    URI to EXACTLY match the actual callback endpoint, so both tier forms are
    registered by default rather than picking one. Set explicitly to override
    (used verbatim, not merged with the default) — e.g. for a custom vhost
    split not covered by the two shipped tiers.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for u in var.broker_redirect_uris :
      can(regex("^https://.+/realms/ne-pilot/broker/entra/endpoint$", u))
    ])
    error_message = "every broker_redirect_uris entry must be https and end with /realms/ne-pilot/broker/entra/endpoint — that path is Keycloak's broker callback for the identityProvider alias \"entra\"."
  }
}

variable "enable_entra_broker" {
  description = <<-EOT
    Creates the Entra-side broker-only app registration for Keycloak and
    writes its client secret to Secrets Manager (<prefix>-entra-broker-client-secret,
    CMK-backed). In THIS root the flag is the apply switch — pass
    `-var enable_entra_broker=true` to create the broker (needs an Entra tenant
    credential: az login or ARM_* env with Application.ReadWrite.All — see
    README). DEFAULT FALSE preserves the pre-move gate 1:1 so offline validate
    and the mocked tftest exercise the enabled path with zero credentials.
  EOT
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_entra_broker || var.hostname != "" || length(var.broker_redirect_uris) > 0
    error_message = "hostname (which derives both default redirect-URI tier forms) OR an explicit broker_redirect_uris list is required when enable_entra_broker = true."
  }
}

variable "secrets_kms_key_arn" {
  description = "The main lane's eks-secrets CMK ARN encrypting the Secrets Manager broker secret. NO DEFAULT — obtain via `terraform -chdir=../terraform-eks output -json kms_key_arns` (key \"eks-secrets\") and pass it explicitly."
  type        = string
}
