# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Offline golden-plan guardrails for the C10 WORM (VLW) fix
# (NE-LP-AZURE-VLW-FIX). Runs with ZERO Azure credentials via mock_provider
# "azurerm" + mock_provider "azapi" — command = plan throughout, module-
# targeted at ./modules/storage exactly like deploy/terraform-eks/tests
# addresses aws_s3_bucket_public_access_block.backup directly. This is the
# executable refutation of the original defect: the shipped IaC must create a
# container with immutableStorageWithVersioning.enabled=true, or this test
# fails.

mock_provider "azurerm" {}
mock_provider "azapi" {}

run "worm_container_vlw_enabled" {
  command = plan

  module {
    source = "./modules/storage"
  }

  variables {
    prefix                      = "t"
    location                    = "eastus"
    resource_group_name         = "t-rg"
    private_endpoints_subnet_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/virtualNetworks/t-vnet/subnets/t-pe"
    private_dns_zone_id         = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/privateDnsZones/privatelink.blob.core.windows.net"
    tags                        = {}
  }

  # THE defect trip-wire (NE-LP-AZURE-VLW-FIX): the worm container must
  # enable version-level immutability. Reverting this property (or dropping
  # the azapi_resource entirely) fails this assert.
  assert {
    condition     = azapi_resource.worm.body.properties.immutableStorageWithVersioning.enabled == true
    error_message = "NE-LP-AZURE-VLW-FIX: azapi_resource.worm must enable properties.immutableStorageWithVersioning.enabled — this is the VLW property AzureBlobObjectStore.archive() preflights"
  }

  assert {
    condition     = azapi_resource.worm.type == "Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01"
    error_message = "worm container must be provisioned at api-version 2023-05-01"
  }

  assert {
    condition     = azapi_resource.worm.name == "worm"
    error_message = "expected the dedicated VLW container to be named 'worm'"
  }

  # The VLW prerequisite: account-level blob versioning must be on.
  assert {
    condition     = azurerm_storage_account.backup.blob_properties[0].versioning_enabled == true
    error_message = "backup storage account must have blob versioning enabled (VLW prerequisite)"
  }
}

run "velero_time_based_policy_defaults_off" {
  command = plan

  module {
    source = "./modules/storage"
  }

  variables {
    prefix                      = "t"
    location                    = "eastus"
    resource_group_name         = "t-rg"
    private_endpoints_subnet_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/virtualNetworks/t-vnet/subnets/t-pe"
    private_dns_zone_id         = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/t-rg/providers/Microsoft.Network/privateDnsZones/privatelink.blob.core.windows.net"
    tags                        = {}
  }

  # The DISTINCT time-based container immutability policy stays opt-in at
  # defaults — VLW (always-on, above) and the time-based policy (opt-in) are
  # two different Azure features and must not be conflated.
  assert {
    condition     = length(azurerm_storage_container_immutability_policy.velero) == 0
    error_message = "time-based velero immutability policy must default off (explicit customer decision)"
  }
}
