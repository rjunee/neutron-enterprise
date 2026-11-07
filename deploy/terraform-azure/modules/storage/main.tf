# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Backup/Velero storage account: private, versioned, TLS1.2-minimum. This is
# the destination for both ne-backup's object-store target and Velero's
# BackupStorageLocation (design §1.8, §2.4).

resource "azurerm_storage_account" "backup" {
  name                          = replace("${var.prefix}backup", "-", "")
  location                      = var.location
  resource_group_name           = var.resource_group_name
  account_tier                  = "Standard"
  account_replication_type      = "ZRS"
  account_kind                  = "StorageV2"
  min_tls_version               = "TLS1_2"
  public_network_access_enabled = false
  tags                          = var.tags

  blob_properties {
    versioning_enabled = true
  }
}

resource "azurerm_storage_container" "velero" {
  name                  = "velero"
  storage_account_name  = azurerm_storage_account.backup.name
  container_access_type = "private"
}

# Documented, off by default (var.enable_immutability): time-based WORM
# retention on the velero container. Locking an immutability policy is
# irreversible below the minimum retention period — never enable this
# without an explicit customer decision (README).
resource "azurerm_storage_container_immutability_policy" "velero" {
  count                                 = var.enable_immutability ? 1 : 0
  storage_container_resource_manager_id = azurerm_storage_container.velero.id
  immutability_period_in_days           = 30
}

# C10 WORM audit anchoring target: `AzureBlobObjectStore.archive()`
# (packages/ne-storage/src/ne/storage/azure_blob.py) preflights
# `get_container_properties()` and requires
# `immutable_storage_with_versioning_enabled` (Azure's "immutable storage with
# versioning", VLW) to be true — this is a DIFFERENT Azure feature from the
# optional TIME-BASED `azurerm_storage_container_immutability_policy` above,
# which stamps blobs Unlocked and never satisfies this preflight.
#
# `hashicorp/azurerm` has no argument to enable VLW anywhere (container or
# account level) as of 4.80/4.81 — upstream issues
# hashicorp/terraform-provider-azurerm#21512 and #31438 are both still open
# (re-verified 2026-07-23). Account-level VLW exists but is CREATION-TIME
# ONLY, which would force replacement of the already-provisioned `backup`
# storage account in any live environment — an unacceptable destructive
# change. So this is a NEW, dedicated container provisioned via the `azapi`
# provider issuing a raw ARM PUT
# (Microsoft.Storage/storageAccounts/blobServices/containers, api-version
# 2023-05-01) with VLW enabled AT CREATION. This is purely additive: an
# already-applied environment simply gains this container on its next apply,
# no migration or account recreation required.
#
# Enabling VLW is irreversible on this container, but it locks nothing by
# itself — retention is set PER-VERSION by archive()'s
# `set_immutability_policy` call, not by this resource. The `velero` container
# above must NEVER be converted to VLW: a container cannot carry both the
# time-based policy and VLW simultaneously, and Velero's snapshot workflow
# depends on ordinary (non-WORM) object mutation.
#
# Asserted plan-time, credential-free, by ../../tests/storage_worm.tftest.hcl
# (NE-LP-AZURE-VLW-FIX).
resource "azapi_resource" "worm" {
  type      = "Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01"
  name      = "worm"
  parent_id = "${azurerm_storage_account.backup.id}/blobServices/default"

  body = {
    properties = {
      publicAccess = "None"
      immutableStorageWithVersioning = {
        enabled = true
      }
    }
  }
}

resource "azurerm_private_endpoint" "blob" {
  name                = "${var.prefix}-blob-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.private_endpoints_subnet_id
  tags                = var.tags

  private_service_connection {
    name                           = "${var.prefix}-blob-psc"
    private_connection_resource_id = azurerm_storage_account.backup.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "blob-zone-group"
    private_dns_zone_ids = [var.private_dns_zone_id]
  }
}
