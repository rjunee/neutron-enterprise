# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Private AKS 1.31+ reference: Entra-RBAC-only (no local accounts), workload
# identity + OIDC issuer for Velero/ESO, azure-CNI-overlay with the "azure"
# NetworkPolicy engine (the chart's NetworkPolicies need an enforcing CNI),
# and a flag-gated, scale-to-zero GPU pool (Risk R2 — owner quota).

data "azurerm_client_config" "current" {}

resource "azurerm_kubernetes_cluster" "this" {
  name                = "${var.prefix}-aks"
  location            = var.location
  resource_group_name = var.resource_group_name
  dns_prefix          = "${var.prefix}-aks"
  kubernetes_version  = var.kubernetes_version
  tags                = var.tags

  private_cluster_enabled   = true
  oidc_issuer_enabled       = true
  workload_identity_enabled = true
  local_account_disabled    = true

  identity {
    type = "SystemAssigned"
  }

  azure_active_directory_role_based_access_control {
    azure_rbac_enabled = true
    tenant_id          = data.azurerm_client_config.current.tenant_id
  }

  default_node_pool {
    name                        = "system"
    vm_size                     = "Standard_D4ds_v5"
    node_count                  = 2
    vnet_subnet_id              = var.system_subnet_id
    temporary_name_for_rotation = "systemtmp"
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_policy      = "azure"
    outbound_type       = var.outbound_type
  }
}

resource "azurerm_kubernetes_cluster_node_pool" "user" {
  name                  = "user"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.this.id
  vm_size               = "Standard_D8ds_v5"
  vnet_subnet_id        = var.user_subnet_id
  auto_scaling_enabled  = true
  min_count             = 2
  max_count             = 5
  tags                  = var.tags
}

# Owner-quota-gated (Risk R2): NVadsA10v5 East US 2 quota request,
# 36 vCPU quota target =>
# exactly one NV36 node. Scale-to-zero keeps idle cost at zero; CPU-mode
# vLLM (the chart's serve.mode=cpu) keeps the serve lane testable without
# this pool.
resource "azurerm_kubernetes_cluster_node_pool" "gpu" {
  count                 = var.gpu_node_pool ? 1 : 0
  name                  = "gpu"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.this.id
  vm_size               = "Standard_NV36ads_A10_v5"
  vnet_subnet_id        = var.gpu_subnet_id
  auto_scaling_enabled  = true
  min_count             = 0
  max_count             = 1
  node_taints           = ["nvidia.com/gpu=present:NoSchedule"]
  tags                  = var.tags
}
