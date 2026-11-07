# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Optional zone-redundant Azure Database for PostgreSQL Flexible Server,
# feeding the chart's `postgres.external` values (design §1.3/§2.2) instead of
# the in-cluster postgres subchart. Entire module is count-gated on
# var.managed_postgres at the ROOT call site (main.tf), not internally.
#
# "Private access" networking here is VNet integration (delegated subnet +
# linked private DNS zone) — Flexible Server's private-access mode, not a
# NIC-based Private Endpoint (see ../network's snet-postgres comment).
# public_network_access_enabled is therefore omitted: it is mutually
# exclusive with delegated_subnet_id / private_dns_zone_id.

resource "azurerm_postgresql_flexible_server" "this" {
  name                   = "${var.prefix}-pg"
  location               = var.location
  resource_group_name    = var.resource_group_name
  version                = "18" # matches the chart's bundled postgres 18.6-alpine (deploy/helm/neutron-enterprise/values.yaml)
  administrator_login    = "ne"
  administrator_password = var.admin_password
  delegated_subnet_id    = var.postgres_subnet_id
  private_dns_zone_id    = var.private_dns_zone_id
  zone                   = "1"

  storage_mb   = 32768
  storage_tier = "P10"
  sku_name     = "GP_Standard_D4s_v3"

  # NE-DR1 — managed-native backup + PITR is the PRIMARY protection for the
  # transactional DB per SPEC §11 (the Velero VSL deliberately EXCLUDES the DB).
  # PITR is automatic once backup_retention_days>0; geo-redundancy is an
  # off-by-default flag (create-time only — see the module variable comment).
  backup_retention_days        = var.backup_retention_days
  geo_redundant_backup_enabled = var.geo_redundant_backup_enabled

  high_availability {
    mode                      = "ZoneRedundant"
    standby_availability_zone = "2"
  }

  tags = var.tags
}

resource "azurerm_postgresql_flexible_server_database" "neutron" {
  name      = "neutron" # matches compose.yaml's DSN database name
  server_id = azurerm_postgresql_flexible_server.this.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}
