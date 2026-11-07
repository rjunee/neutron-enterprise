# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

# --- IRSA / EBS CSI (NE-AWS-6) ---

output "oidc_provider_arn" {
  description = "IRSA OIDC provider ARN (the AKS oidc_issuer twin) — trust anchor for NE-AWS-7's Velero role and the EBS CSI role."
  value       = module.eks.oidc_provider_arn
}

output "ebs_csi_irsa_role_arn" {
  description = "IRSA role the aws-ebs-csi-driver addon runs as (kube-system:ebs-csi-controller-sa)."
  value       = aws_iam_role.ebs_csi.arn
}

output "gp3_kms_key_id" {
  description = "The ebs CMK ARN for the gp3 StorageClass parameters.kmsKeyId (full ARN — the form the EBS CSI driver docs recommend); also encrypts node root volumes."
  value       = module.kms.key_arns["ebs"]
}

output "ecr_repository_url" {
  description = "The `ne release push --registry` target for the EKS smoke."
  value       = aws_ecr_repository.ne_app.repository_url
}

output "vpc_id" {
  value = module.vpc.vpc_id
}

output "private_subnet_ids" {
  description = "Worker-node private subnets (one per AZ) — consumed by NE-AWS-2/3."
  value       = module.vpc.private_subnets
}

output "public_subnet_ids" {
  description = "NAT-only public subnets (one per AZ)."
  value       = module.vpc.public_subnets
}

output "intra_subnet_ids" {
  description = "Firewall-endpoint subnets, reserved for NE-AWS-5 (AzureFirewallSubnet twin)."
  value       = module.vpc.intra_subnets
}

output "private_route_table_ids" {
  description = "Route tables for the private (worker) subnets."
  value       = module.vpc.private_route_table_ids
}

output "public_route_table_ids" {
  description = "Route tables for the public (NAT) subnets."
  value       = module.vpc.public_route_table_ids
}

output "intra_route_table_ids" {
  description = "Route tables for the intra (firewall-endpoint) subnets."
  value       = module.vpc.intra_route_table_ids
}

output "natgw_ids" {
  description = "NAT gateway ids — one per AZ (one_nat_gateway_per_az = true)."
  value       = module.vpc.natgw_ids
}

output "vpc_cidr_block" {
  description = "The VPC's IPv4 CIDR block."
  value       = module.vpc.vpc_cidr_block
}

output "azs" {
  description = "The 3 availability zones this network spans."
  value       = local.azs
}

# --- KMS CMK plane (NE-AWS-4) ---

output "kms_key_arns" {
  description = "Customer-managed CMK ARNs by data class (eks-secrets, ebs, rds, s3-backup, logs) — consumed by NE-AWS-5/6/8/10/11."
  value       = module.kms.key_arns
}

output "kms_key_ids" {
  description = "Customer-managed CMK key ids by data class."
  value       = module.kms.key_ids
}

output "kms_key_aliases" {
  description = "CMK alias names (alias/<prefix>-<class>) by data class."
  value       = module.kms.alias_names
}

# --- VPC endpoints (NE-AWS-3) ---

output "vpc_endpoint_ids" {
  description = "Endpoint key => VPC endpoint id (10 interface + s3 gateway) — NE-AWS-3."
  value       = { for k, e in module.vpc_endpoints.endpoints : k => e.id }
}

output "vpc_endpoints_security_group_id" {
  description = "Dedicated SG on the interface endpoints (443 from the VPC CIDR only)."
  value       = module.vpc_endpoints.security_group_id
}

# --- Backup storage (NE-AWS-8) ---

output "backup_bucket_name" {
  description = "S3 backup bucket name — consumed by NE-AWS-7 (Velero IRSA policy) + NE-AWS-11 (Velero BSL)."
  value       = module.storage.bucket_name
}

output "backup_bucket_arn" {
  description = "S3 backup bucket ARN — scopes NE-AWS-7's least-privilege Velero policy."
  value       = module.storage.bucket_arn
}

# --- Velero stage-2 (NE-AWS-11) ---

output "velero_release_status" {
  description = "Null when install_velero = false. helm_release status of the stage-2 Velero install — quick ops-runner check (NE-AWS-11)."
  value       = var.install_velero ? helm_release.velero[0].status : null
}

# --- Managed Postgres (NE-AWS-10) ---

output "postgres_fqdn" {
  description = "Null when managed_postgres = false. Feeds the chart's postgres.external mapping (README)."
  value       = var.managed_postgres ? module.data[0].fqdn : null
}

output "postgres_database_name" {
  value = var.managed_postgres ? module.data[0].database_name : null
}

# --- Egress firewall (NE-AWS-5) ---

output "firewall_arn" {
  description = "AWS Network Firewall ARN. Null when egress_mode = \"nat\"."
  value       = module.egress.firewall_arn
}

output "firewall_policy_arn" {
  description = "Network Firewall policy ARN. Null when egress_mode = \"nat\"."
  value       = module.egress.firewall_policy_arn
}

output "frontier_rule_group_arn" {
  description = "Frontier stateful rule group ARN. Null when egress_mode = \"nat\" or when frontier_fqdns is empty (air-gap — no rule group is created)."
  value       = module.egress.frontier_rule_group_arn
}

output "firewall_endpoint_ids_by_az" {
  description = "Firewall VPC endpoint id, keyed by AZ. Empty map when egress_mode = \"nat\"."
  value       = module.egress.firewall_endpoint_ids_by_az
}

output "firewall_alert_log_group" {
  description = "ALERT CloudWatch log group name (CMK-encrypted with the `logs` CMK). Null when egress_mode = \"nat\"."
  value       = module.egress.alert_log_group_name
}

output "firewall_flow_log_group" {
  description = "FLOW CloudWatch log group name (CMK-encrypted with the `logs` CMK). Null when egress_mode = \"nat\"."
  value       = module.egress.flow_log_group_name
}

output "frontier_sni_pass_rule_count" {
  description = "Exact-match tls.sni pass rules rendered from frontier_fqdns (0 = air-gap, no rule group created)."
  value       = module.egress.sni_pass_rule_count
}

# --- Identity (NE-AWS-7) ---

output "velero_irsa_role_arn" {
  description = "IRSA role for serviceaccount velero/velero — NE-AWS-11 sets eks.amazonaws.com/role-arn to this."
  value       = module.identity.velero_role_arn
}
