# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "entra_broker_client_id" {
  description = "The Entra app (client) ID for the Keycloak broker (null when enable_entra_broker = false) — NOT a credential; the client secret lives ONLY in Secrets Manager (<prefix>-entra-broker-client-secret) and is never an output (C12)."
  value       = var.enable_entra_broker ? azuread_application.broker[0].client_id : null
}
