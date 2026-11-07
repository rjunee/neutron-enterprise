# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Optional multi-AZ RDS for PostgreSQL instance feeding the chart's
# postgres.external values instead of the in-cluster postgres subchart — the
# Flexible Server ZoneRedundant twin. Entire module is count-gated on
# var.managed_postgres at the ROOT call site (data.tf), not internally.

resource "aws_db_subnet_group" "this" {
  name       = "${var.prefix}-pg"
  subnet_ids = var.private_subnet_ids
  tags       = var.tags
}

# SGs are stateful and the DB initiates nothing — deny-all egress by
# construction (no inline rules, no egress block).
resource "aws_security_group" "postgres" {
  name        = "${var.prefix}-pg"
  description = "RDS Postgres — 5432 from the EKS node security group only"
  vpc_id      = var.vpc_id
  tags        = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "postgres_from_nodes" {
  security_group_id            = aws_security_group.postgres.id
  referenced_security_group_id = var.node_security_group_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "Postgres from EKS worker nodes only (C2 — never a CIDR, never 0.0.0.0/0)"
}

resource "aws_db_instance" "this" {
  identifier = "${var.prefix}-pg"
  engine     = "postgres"
  # Major-only pin + auto_minor_version_upgrade keeps the plan drift-free as
  # AWS rolls minors; the chart's bundled image is postgres 18.6-alpine
  # (deploy/helm/neutron-enterprise/values.yaml), same "18" posture as the
  # Azure Flexible Server module.
  engine_version = "18"
  # 4 vCPU / 16 GiB — the GP_Standard_D4s_v3 twin, x86 parity with the m6i
  # node group.
  instance_class = "db.m6i.xlarge"
  # 32 GiB — the Azure 32768 MB / P10 twin.
  allocated_storage = 32
  storage_type      = "gp3"

  db_name  = "neutron" # matches compose.yaml's DSN database name
  username = "ne"
  password = var.admin_password
  port     = 5432

  multi_az = true # the ZoneRedundant twin — synchronous standby in a second AZ

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.postgres.id]
  publicly_accessible    = false

  storage_encrypted = true
  # The NE-AWS-4 rds CMK; no key-policy edit needed — the account-root
  # IAM-delegation baseline lets the admin deploy role create the RDS
  # service grant, kms.tf untouched.
  kms_key_id = var.kms_key_arn

  deletion_protection        = true
  auto_minor_version_upgrade = true
  # retention>0 keeps automated backups + PITR active (restore to any point
  # inside the window); NE-DR1 var-wires this at the established 7-day posture.
  backup_retention_period = var.backup_retention_period
  copy_tags_to_snapshot   = true

  skip_final_snapshot = false
  # A deliberate destroy still snapshots; deletion_protection must be
  # flipped off in a prior apply — see README.
  final_snapshot_identifier = "${var.prefix}-pg-final-snapshot"

  tags = var.tags
}
