# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "id" {
  value = azurerm_kubernetes_cluster.this.id
}

output "name" {
  value = azurerm_kubernetes_cluster.this.name
}

output "oidc_issuer_url" {
  value = azurerm_kubernetes_cluster.this.oidc_issuer_url
}

output "kubelet_identity_object_id" {
  description = "Consumed by ../registry for the AcrPull role assignment."
  value       = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
}

output "host" {
  description = "Sensitive: consumed only by velero.tf's helm provider block (Risk R7 — requires private API server reachability)."
  value       = azurerm_kubernetes_cluster.this.kube_config[0].host
  sensitive   = true
}

output "cluster_ca_certificate" {
  sensitive = true
  value     = azurerm_kubernetes_cluster.this.kube_config[0].cluster_ca_certificate
}
