# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Opt-in, stage-2 (var.install_velero, default false): the helm provider must
# reach the PRIVATE AKS API server, which means `terraform apply` for this
# resource can only run from inside the VNet/VPN (ops-runner pattern, Risk
# R7) — never a default-on dependency of the base `terraform apply`.
# Chart/app version pinned exact per componentVerification (2026-07-09):
# vmware-tanzu/helm-charts velero chart 12.1.0 (appVersion 1.18.1), Apache-2.0.

provider "helm" {
  kubernetes = {
    host                   = module.aks.host
    cluster_ca_certificate = base64decode(module.aks.cluster_ca_certificate)

    # AAD-RBAC-only cluster (local_account_disabled = true): auth is via
    # kubelogin's azurecli exec plugin, run from an identity with AKS RBAC
    # cluster-admin — install kubelogin + az login on the ops-runner (README).
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "kubelogin"
      args        = ["get-token", "--login", "azurecli", "--server-id", "6dae42f8-4368-4678-94ff-3960e28e3630"]
    }
  }
}

resource "helm_release" "velero" {
  count = var.install_velero ? 1 : 0

  name             = "velero"
  repository       = "https://vmware-tanzu.github.io/helm-charts"
  chart            = "velero"
  version          = "12.1.0" # exact pin (C11) — verified vmware-tanzu.github.io/helm-charts 2026-07-09
  namespace        = "velero"
  create_namespace = true

  values = [
    yamlencode({
      initContainers = [
        {
          name = "velero-plugin-for-microsoft-azure"
          # exact pin (C11), re-verified 2026-07-18 — v1.14.x is the Velero
          # 1.18.x line (v1.11.x targeted ~1.15; the mismatch became load-bearing
          # once NE-DR1 added the VSL / native disk snapshotter). docker.io tag
          # v1.14.2 existence confirmed before commit; Apache-2.0.
          image           = "velero/velero-plugin-for-microsoft-azure:v1.14.2"
          imagePullPolicy = "IfNotPresent"
          volumeMounts = [
            { mountPath = "/target", name = "plugins" },
          ]
        },
      ]

      serviceAccount = {
        server = {
          annotations = {
            "azure.workload.identity/client-id" = module.identity.velero_identity_client_id
          }
          labels = {
            "azure.workload.identity/use" = "true"
          }
        }
      }

      podLabels = {
        "azure.workload.identity/use" = "true"
      }

      credentials = {
        useSecret = false # AAD Workload Identity — no static Azure credential
      }

      configuration = {
        backupStorageLocation = [
          {
            name     = "default"
            provider = "azure"
            bucket   = module.storage.container_name
            config = {
              resourceGroup  = azurerm_resource_group.this.name
              storageAccount = module.storage.account_name
              useAAD         = "true"
            }
          },
        ]

        # NE-DR1 — VolumeSnapshotLocation for NON-DB PVs only. The transactional
        # DB is EXTERNAL managed Flexible Server (never a cluster PV), protected
        # by native automated backup + PITR (modules/data). Snapshots land in OUR
        # resource group (not the disposable MC_* node RG) so they survive
        # cluster deletion; subscriptionId omitted = same subscription. The
        # chart-side Schedule keeps snapshotVolumes=false unless the operator
        # opts in under the DB-exclusion contract (deploy/helm values.yaml +
        # docs/ops/disaster-recovery.md).
        volumeSnapshotLocation = [
          {
            name     = "default"
            provider = "azure"
            config = {
              resourceGroup = azurerm_resource_group.this.name
            }
          },
        ]
      }
    }),
  ]
}

# --- NE-DR1: least-privilege disk-snapshot role for the Velero workload -------
# identity. All count-gated on var.install_velero so the base plan is untouched.
# The velero_storage role assignment (Storage Blob Data Contributor) lives in
# modules/identity; the disk-snapshot permissions are added HERE via a data-source
# principal_id lookup to avoid editing modules/identity (report-don't-edit per
# orchestration §1.4 — the module outputs only client_id/id, not principal_id).

data "azurerm_kubernetes_cluster" "this" {
  count               = var.install_velero ? 1 : 0
  name                = module.aks.name
  resource_group_name = azurerm_resource_group.this.name
  depends_on          = [module.aks]
}

data "azurerm_user_assigned_identity" "velero" {
  count               = var.install_velero ? 1 : 0
  name                = "${var.prefix}-velero-identity"
  resource_group_name = azurerm_resource_group.this.name
  depends_on          = [module.identity]
}

resource "azurerm_role_definition" "velero_snapshots" {
  count       = var.install_velero ? 1 : 0
  name        = "${var.prefix}-velero-disk-snapshots"
  scope       = azurerm_resource_group.this.id
  description = "velero-plugin-for-microsoft-azure documented minimal disk-snapshot set (NE-DR1)"

  permissions {
    actions = [
      "Microsoft.Compute/disks/read",
      "Microsoft.Compute/disks/write",
      "Microsoft.Compute/disks/endGetAccess/action",
      "Microsoft.Compute/disks/beginGetAccess/action",
      "Microsoft.Compute/snapshots/read",
      "Microsoft.Compute/snapshots/write",
      "Microsoft.Compute/snapshots/delete",
    ]
  }

  # PV disks live in the AKS node RG (read/beginGetAccess there); snapshots are
  # written into the main RG per the VSL config — so the role is assignable at
  # both scopes.
  assignable_scopes = [
    azurerm_resource_group.this.id,
    data.azurerm_kubernetes_cluster.this[0].node_resource_group_id,
  ]
}

resource "azurerm_role_assignment" "velero_snapshots_rg" {
  count              = var.install_velero ? 1 : 0
  scope              = azurerm_resource_group.this.id
  role_definition_id = azurerm_role_definition.velero_snapshots[0].role_definition_resource_id
  principal_id       = data.azurerm_user_assigned_identity.velero[0].principal_id
}

resource "azurerm_role_assignment" "velero_snapshots_node_rg" {
  count              = var.install_velero ? 1 : 0
  scope              = data.azurerm_kubernetes_cluster.this[0].node_resource_group_id
  role_definition_id = azurerm_role_definition.velero_snapshots[0].role_definition_resource_id
  principal_id       = data.azurerm_user_assigned_identity.velero[0].principal_id
}
