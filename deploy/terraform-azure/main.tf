# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Private AKS 1.31+ reference. Apply order + the full promotion runbook
# (terraform apply -> ne release push -> ne pilot promote -> ne k8s render ->
# ne k8s up -> smoke) are documented in README.md. A real plan/apply against
# an Azure subscription is LIVE-PARKED (owner credentials) — this root is
# validated with `terraform validate` + `tflint` + `terraform fmt` in CI
# (.github/workflows/terraform-validate.yml), never applied in CI.

provider "azurerm" {
  features {}
}

provider "azuread" {}

provider "azapi" {}

resource "azurerm_resource_group" "this" {
  name     = "${var.prefix}-rg"
  location = var.location
  tags     = var.tags
}

module "network" {
  source = "./modules/network"

  prefix              = var.prefix
  location            = var.location
  resource_group_name = azurerm_resource_group.this.name
  managed_postgres    = var.managed_postgres
  tags                = var.tags
}

module "egress" {
  source = "./modules/egress"

  prefix              = var.prefix
  location            = var.location
  resource_group_name = azurerm_resource_group.this.name
  mode                = var.egress_mode
  firewall_subnet_id  = module.network.firewall_subnet_id
  aks_subnet_ids = {
    system = module.network.aks_system_subnet_id
    user   = module.network.aks_user_subnet_id
    gpu    = module.network.aks_gpu_subnet_id
  }
  aks_subnet_cidrs = ["10.20.0.0/22", "10.20.4.0/22", "10.20.8.0/24"]
  frontier_fqdns   = var.frontier_fqdns
  tags             = var.tags
}

module "aks" {
  source = "./modules/aks"

  prefix              = var.prefix
  location            = var.location
  resource_group_name = azurerm_resource_group.this.name
  kubernetes_version  = var.kubernetes_version
  system_subnet_id    = module.network.aks_system_subnet_id
  user_subnet_id      = module.network.aks_user_subnet_id
  gpu_subnet_id       = module.network.aks_gpu_subnet_id
  outbound_type       = module.egress.outbound_type
  gpu_node_pool       = var.gpu_node_pool
  tags                = var.tags

  # Route table (firewall mode) / NAT association (nat mode) must exist
  # before the cluster starts pulling images through it.
  depends_on = [module.egress]
}

module "registry" {
  source = "./modules/registry"

  prefix                      = var.prefix
  location                    = var.location
  resource_group_name         = azurerm_resource_group.this.name
  private_endpoints_subnet_id = module.network.private_endpoints_subnet_id
  private_dns_zone_id         = module.network.acr_private_dns_zone_id
  kubelet_identity_object_id  = module.aks.kubelet_identity_object_id
  tags                        = var.tags
}

module "storage" {
  source = "./modules/storage"

  prefix                      = var.prefix
  location                    = var.location
  resource_group_name         = azurerm_resource_group.this.name
  private_endpoints_subnet_id = module.network.private_endpoints_subnet_id
  private_dns_zone_id         = module.network.blob_private_dns_zone_id
  tags                        = var.tags
}

module "identity" {
  source = "./modules/identity"

  prefix                      = var.prefix
  location                    = var.location
  resource_group_name         = azurerm_resource_group.this.name
  key_vault_id                = var.key_vault_id
  create_entra_broker         = var.create_entra_broker
  private_endpoints_subnet_id = module.network.private_endpoints_subnet_id
  private_dns_zone_id         = module.network.vault_private_dns_zone_id
  hostname                    = var.hostname
  oidc_issuer_url             = module.aks.oidc_issuer_url
  storage_account_id          = module.storage.account_id
  tags                        = var.tags
}

# Managed-postgres variant (var.managed_postgres = true) — the recommended
# production HA answer + the OpenShift-postgres escape hatch (design §1.5.3).
module "data" {
  count  = var.managed_postgres ? 1 : 0
  source = "./modules/data"

  prefix              = var.prefix
  location            = var.location
  resource_group_name = azurerm_resource_group.this.name
  postgres_subnet_id  = module.network.postgres_subnet_id[0]
  private_dns_zone_id = module.network.postgres_private_dns_zone_id[0]
  admin_password      = var.postgres_admin_password
  tags                = var.tags
}
