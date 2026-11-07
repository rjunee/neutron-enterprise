# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Opt-in, stage-2 (var.install_velero, default false): the helm provider must
# reach the PRIVATE EKS API server (cluster.tf's endpoint_public_access
# derives false at defaults), which means `terraform apply` for this
# resource can only run from inside the VPC (or a VPN/DX-peered network that
# reaches it) — the ops-runner pattern, Risk R7 — never a default-on
# dependency of the base `terraform apply`.
# Chart/app version pinned exact per componentVerification (2026-07-09,
# re-verified 2026-07-17): vmware-tanzu/helm-charts velero chart 12.1.0
# (appVersion 1.18.1), Apache-2.0. velero/velero-plugin-for-aws v1.14.2
# (v1.14.x is the Velero 1.18.x line per its release notes), Apache-2.0,
# fresh-verified 2026-07-17.

provider "helm" {
  kubernetes = {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

    # API-mode cluster (authentication_mode = "API", no aws-auth ConfigMap):
    # auth is via `aws eks get-token`, the kubelogin twin — run from an
    # ops-runner with an EKS access entry granting cluster-admin (README).
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.region]
    }
  }
}

resource "helm_release" "velero" {
  count = var.install_velero ? 1 : 0

  name             = "velero"
  repository       = "https://vmware-tanzu.github.io/helm-charts"
  chart            = "velero"
  version          = "12.1.0" # exact pin (C11) — same verified pin as ../terraform-azure (2026-07-09); re-verified vmware-tanzu.github.io/helm-charts 2026-07-17
  namespace        = "velero"
  create_namespace = true

  values = [
    yamlencode({
      initContainers = [
        {
          name            = "velero-plugin-for-aws"
          image           = "velero/velero-plugin-for-aws:v1.14.2" # fresh-verified 2026-07-17 (C11) — the v1.14.x line targets Velero 1.18.x per its release notes, matching chart appVersion 1.18.1
          imagePullPolicy = "IfNotPresent"
          volumeMounts = [
            { mountPath = "/target", name = "plugins" },
          ]
        },
      ]

      # IRSA — no workload-identity labels/podLabels twin needed, only the
      # SA annotation. The default SA name (velero, namespace velero)
      # matches NE-AWS-7's trust-policy condition
      # system:serviceaccount:velero:velero (modules/identity/main.tf).
      serviceAccount = {
        server = {
          annotations = {
            "eks.amazonaws.com/role-arn" = module.identity.velero_role_arn
          }
        }
      }

      credentials = {
        useSecret = false # IRSA — no static AWS credential (C12)
      }

      configuration = {
        backupStorageLocation = [
          {
            name     = "default"
            provider = "aws"
            bucket   = module.storage.bucket_name
            config = {
              # region is required by velero-plugin-for-aws's BSL config
              # (the resourceGroup/storageAccount analog on the Azure twin).
              region = var.region
              # kmsKeyId alone turns on SSE-KMS with this CMK, satisfying
              # NE-AWS-8's deny-unencrypted-upload bucket policy.
              kmsKeyId = module.kms.key_arns["s3-backup"]
            }
          },
        ]

        # NE-DR1 — VolumeSnapshotLocation for NON-DB PVs only. The
        # transactional DB is EXTERNAL managed RDS (never a cluster PV),
        # protected by native automated backup + PITR (modules/data). The
        # velero-plugin-for-aws native EBS snapshotter handles both in-tree and
        # ebs.csi.aws.com CSI-provisioned volumes; the NE-AWS-7 IRSA policy
        # already carries the plugin's documented EC2 snapshot statement
        # (modules/identity/main.tf VeleroSnapshotOps) so NO IAM change is
        # needed. The chart-side Schedule keeps snapshotVolumes=false unless the
        # operator opts in under the DB-exclusion contract (deploy/helm
        # values.yaml + docs/ops/disaster-recovery.md).
        volumeSnapshotLocation = [
          {
            name     = "default"
            provider = "aws"
            config = {
              region = var.region
            }
          },
        ]
      }
    }),
  ]
}
