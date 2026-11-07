#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Static invariant guards for deploy/terraform-eks (NE-AWS-12, design D5).
#
# terraform test asserts (tests/hardened.tftest.hcl) can only see module
# OUTPUTS and root/module-targeted first-party resources — arguments passed
# to the pinned upstream modules (eks, network-firewall) are invisible to it
# (empirically verified on Terraform 1.15.8: run-block conditions resolve
# `module.<name>` to its outputs object only). The invariants below live in
# exactly those upstream-module arguments, so they are enforced textually:
# each guard greps for the load-bearing line; deleting or flipping the line
# fails PR CI (.github/workflows/terraform-validate.yml, eks leg).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0

need() { # file ERE-pattern description
  if ! grep -Eq -- "$2" "$DIR/$1"; then
    echo "GUARD FAIL [$1]: $3 — expected pattern: $2" >&2
    fail=1
  fi
}

need_count() { # file ERE-pattern min-count description
  local n
  n="$(grep -Ec -- "$2" "$DIR/$1" || true)"
  if [ "$n" -lt "$3" ]; then
    echo "GUARD FAIL [$1]: $4 — expected >= $3 matches of: $2 (got $n)" >&2
    fail=1
  fi
}

# (1) private API endpoint at defaults — public access only via the D4
# CIDR-allowlist derivation, never a literal true.
need cluster.tf 'endpoint_private_access[[:space:]]*=[[:space:]]*true' \
  "cluster API private access must stay enabled"
need cluster.tf 'endpoint_public_access[[:space:]]*=[[:space:]]*length\(var\.api_public_access_cidrs\) > 0' \
  "cluster API public access must derive from the api_public_access_cidrs allowlist"

# (5) deny-by-default firewall policy: STRICT_ORDER, stateful drop+alert,
# stateless forced through the stateful engine (never aws:pass).
need modules/egress/main.tf 'policy_stateful_default_actions[[:space:]]*=[[:space:]]*\["aws:drop_strict", "aws:alert_strict"\]' \
  "stateful default action must be drop_strict + alert_strict"
need modules/egress/main.tf 'policy_stateless_default_actions[[:space:]]*=[[:space:]]*\["aws:forward_to_sfe"\]' \
  "stateless default action must be forward_to_sfe (aws:pass would bypass the C2 perimeter)"
need modules/egress/main.tf 'policy_stateless_fragment_default_actions[[:space:]]*=[[:space:]]*\["aws:forward_to_sfe"\]' \
  "stateless fragment default action must be forward_to_sfe"
need_count modules/egress/main.tf 'rule_order[[:space:]]*=[[:space:]]*"STRICT_ORDER"' 2 \
  "both the policy engine and the rule group must be STRICT_ORDER"
need modules/egress/main.tf 'stream_exception_policy[[:space:]]*=[[:space:]]*"DROP"' \
  "stream exceptions must fail closed"

# (6) frontier lane rules stay exact-match SNI (no substring/subdomain slip).
need modules/egress/main.tf 'tls\.sni; content:%q; startswith; endswith; nocase;' \
  "SNI pass rules must be exact-match (startswith+endswith+nocase)"

# (7) EKS secrets envelope encryption uses the NE-AWS-4 eks-secrets CMK.
need cluster.tf 'create_kms_key[[:space:]]*=[[:space:]]*false' \
  "the eks module's own KMS key must stay disabled"
need cluster.tf 'provider_key_arn[[:space:]]*=[[:space:]]*module\.kms\.key_arns\["eks-secrets"\]' \
  "encryption_config must reference the eks-secrets CMK"
need cluster.tf 'resources[[:space:]]*=[[:space:]]*\["secrets"\]' \
  "encryption_config must cover secrets"

# (3)/(4) network posture: per-AZ NAT HA + the vpc module must NOT insert
# private NAT default routes in firewall mode (the egress module owns them).
need network.tf 'single_nat_gateway[[:space:]]*=[[:space:]]*false' \
  "single NAT would break AZ-loss HA"
need network.tf 'one_nat_gateway_per_az[[:space:]]*=[[:space:]]*true' \
  "one NAT per AZ required"
need network.tf 'map_public_ip_on_launch[[:space:]]*=[[:space:]]*false' \
  "no auto-assigned public IPs"
need network.tf 'create_private_nat_gateway_route[[:space:]]*=[[:space:]]*var\.egress_mode == "nat"' \
  "vpc-module NAT default routes only in nat mode (firewall mode routes via the egress module)"

# (10) GPU lane stays scale-to-zero (min 0 / max 1 appears only in the gpu
# node group; the default group is 2/2).
need cluster.tf 'min_size[[:space:]]*=[[:space:]]*0' \
  "gpu node group must scale to zero"
need cluster.tf 'max_size[[:space:]]*=[[:space:]]*1' \
  "gpu node group must cap at one node"

# (11) C12: no static AWS credentials anywhere in the tree — not as an
# aws_iam_access_key resource, not as provider access_key/secret_key arguments,
# and not baked into a committed *.tfvars (the *.tfvars.example template ends in
# .example, so --include='*.tf'/'*.tfvars' never matches it).
if grep -rn --include='*.tf' 'aws_iam_access_key' "$DIR"; then
  echo 'GUARD FAIL: aws_iam_access_key found — static AWS credentials are banned (C12)' >&2
  fail=1
fi
if grep -rnE --include='*.tf' '^[[:space:]]*(access_key|secret_key)[[:space:]]*=' "$DIR"; then
  echo 'GUARD FAIL: provider access_key/secret_key argument found — static AWS credentials are banned (C12)' >&2
  fail=1
fi
if grep -rnE --include='*.tfvars' '(access_key|secret_key|aws_secret_access_key)' "$DIR" 2>/dev/null; then
  echo 'GUARD FAIL: static credentials in a committed .tfvars — banned (C12)' >&2
  fail=1
fi

# (12) NE-DR1 DR posture: the Velero VolumeSnapshotLocation for non-DB PVs and
# the var-wired RDS backup/PITR retention are arguments (invisible to tftest per
# the NE-AWS-12 outputs-only limitation), so they are guarded textually here.
need velero.tf 'volumeSnapshotLocation' \
  "Velero VolumeSnapshotLocation for non-DB PVs must stay configured (NE-DR1)"
need_count velero.tf 'provider[[:space:]]*=[[:space:]]*"aws"' 2 \
  "both the BSL and the VSL must use the aws plugin provider"
need modules/data/main.tf 'backup_retention_period[[:space:]]*=[[:space:]]*var\.backup_retention_period' \
  "RDS automated-backup/PITR retention must stay var-wired (NE-DR1)"
need modules/data/variables.tf 'backup_retention_period' \
  "the RDS retention var must exist (NE-DR1)"

if [ "$fail" -ne 0 ]; then
  echo "static invariant guards FAILED" >&2
  exit 1
fi
echo "static invariant guards OK"
