# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "login_server" {
  description = "The `ne release push --registry` target."
  value       = azurerm_container_registry.this.login_server
}

output "id" {
  value = azurerm_container_registry.this.id
}
