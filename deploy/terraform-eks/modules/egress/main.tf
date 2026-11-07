# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# The C2 perimeter: security groups (../../network.tf's vpc module) bound
# "which pods may egress" is L4-coarse; THIS module is where "where to"
# becomes FQDN-exact, via AWS Network Firewall's stateful TLS-SNI allowlist —
# the AzureFirewallSubnet/application-rule twin of ../terraform-azure's
# modules/egress. Unlike Azure, AWS needs NO infra-FQDN allowlist: PrivateLink
# interface endpoints (NE-AWS-3) plus the link-local Amazon Time Sync service
# (169.254.169.123) cover every AWS dependency the cluster/chart touches — a
# stronger posture than Azure's aks-required-infra application-rule holes.
# The "nat" mode is a documented, never-silent degraded variant (loses FQDN
# precision — README Azure Risk-R4 twin).

locals {
  create_firewall = var.mode == "firewall"
  az_count        = length(var.azs)

  # drop_strict discards ANY packet that doesn't match a stateful pass rule
  # — including the initial TCP handshake to an otherwise-approved FQDN.
  # Without this rule the stateful engine never sees the TLS ClientHello and
  # even allowed FQDNs fail at live apply. It authorizes no data egress:
  # 3-way handshake packets only, port 443, both directions.
  handshake_rule = "pass tcp $HOME_NET any <> $EXTERNAL_NET 443 (flow:not_established; msg:\"TCP handshake permitted so the stateful engine can see the TLS SNI - drop_strict otherwise drops the ClientHello and even allowed FQDNs fail\"; sid:1; rev:1;)"

  # One exact-match tls.sni PASS rule per frontier_fqdns entry — startswith +
  # endswith + nocase together enforce an EXACT match (no substring/subdomain
  # slip), which is what "single-frontier-lane" means (C2).
  sni_rules = [
    for i, fqdn in var.frontier_fqdns : format(
      "pass tls $HOME_NET any -> $EXTERNAL_NET 443 (tls.sni; content:%q; startswith; endswith; nocase; msg:%q; sid:%d; rev:1;)",
      fqdn, "C2 frontier lane: ${fqdn}", 100 + i,
    )
  ]

  frontier_rules = join("\n", concat([local.handshake_rule], local.sni_rules))

  # Empty frontier_fqdns => no rule group => zero pass rules anywhere
  # (the handshake rule lives INSIDE this group and disappears with it) =>
  # the STRICT_ORDER default-drop policy holds for ALL egress = AIR_GAP
  # (design D6).
  create_rule_group = local.create_firewall && length(var.frontier_fqdns) > 0
}

# terraform-aws-modules/terraform-aws-network-firewall v2.1.0
# @ 9bb904be0622dd0d71164e9713ae506f34d0fb66 — Apache-2.0, re-verified at
# build time (README §2): tag v2.1.0 resolves directly to this commit,
# maintainer = terraform-aws-modules org (same org as the pinned vpc/eks
# modules), active cadence, source reviewed line-by-line at this SHA.
module "frontier_rule_group" {
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-network-firewall.git//modules/rule-group?ref=9bb904be0622dd0d71164e9713ae506f34d0fb66" # v2.1.0

  create = local.create_rule_group

  name     = "${var.prefix}-frontier"
  type     = "STATEFUL"
  capacity = 100 # immutable once created; headroom for future frontier lanes

  rule_group = {
    rules_source = {
      rules_string = local.frontier_rules
    }
    stateful_rule_options = {
      rule_order = "STRICT_ORDER"
    }
  }

  tags = var.tags
}

module "firewall" {
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-network-firewall.git?ref=9bb904be0622dd0d71164e9713ae506f34d0fb66" # v2.1.0

  create = local.create_firewall

  name   = "${var.prefix}-fw"
  vpc_id = var.vpc_id

  subnet_mapping = { for i, az in var.azs : az => { subnet_id = var.firewall_subnet_ids[i] } }

  # D8 teardown-default: evidence runs `terraform destroy` every time
  # (.github/workflows/eks-smoke.yml); a standing customer environment
  # should flip this to true.
  delete_protection = false

  policy_name = "${var.prefix}-fw-policy"

  policy_stateful_engine_options = {
    rule_order = "STRICT_ORDER"
    # Fail-closed on midstream exceptions (e.g. unsupported protocol state)
    # rather than silently letting the packet through.
    stream_exception_policy = "DROP"
  }

  # Deny-by-default (C2): drop everything that isn't an explicit stateful
  # pass rule, and alert on every drop.
  policy_stateful_default_actions = ["aws:drop_strict", "aws:alert_strict"]

  # LOUD: the upstream submodule DEFAULTS stateless_default_actions (and the
  # fragment analogue) to ["aws:pass"], which routes packets AROUND the
  # stateful engine entirely — a fail-open bypass of the whole C2 perimeter.
  # forward_to_sfe is the C2-correct override; reviewers must keep both of
  # these explicit, never revert to the upstream default.
  policy_stateless_default_actions          = ["aws:forward_to_sfe"]
  policy_stateless_fragment_default_actions = ["aws:forward_to_sfe"]

  policy_stateful_rule_group_reference = local.create_rule_group ? {
    frontier = {
      priority     = 1
      resource_arn = module.frontier_rule_group.arn
    }
  } : null

  create_logging_configuration = local.create_firewall
  logging_configuration_destination_config = local.create_firewall ? [
    {
      log_destination      = { logGroup = aws_cloudwatch_log_group.alert[0].name }
      log_destination_type = "CloudWatchLogs"
      log_type             = "ALERT"
    },
    {
      log_destination      = { logGroup = aws_cloudwatch_log_group.flow[0].name }
      log_destination_type = "CloudWatchLogs"
      log_type             = "FLOW"
    },
  ] : null

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "alert" {
  count = local.create_firewall ? 1 : 0

  name              = "/aws/network-firewall/${var.prefix}/alert"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.logs_kms_key_arn
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "flow" {
  count = local.create_firewall ? 1 : 0

  name              = "/aws/network-firewall/${var.prefix}/flow"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.logs_kms_key_arn
  tags              = var.tags
}

# The firewall endpoint id per AZ is unknown until apply — legal here because
# it is only ever consumed in route ATTRIBUTES below, never in count/for_each
# (a for_each over module.firewall.status[0].sync_states would fail the
# first live plan, since the set of endpoints doesn't exist yet).
locals {
  firewall_endpoint_id_by_az = local.create_firewall ? {
    for ss in module.firewall.status[0].sync_states : ss.availability_zone => ss.attachment[0].endpoint_id
  } : {}
}

# --- Route insertion (first-party) ----------------------------------------
#
# private-RT   0.0.0.0/0 -> same-AZ firewall endpoint  (firewall mode only)
# intra-RT     0.0.0.0/0 -> same-AZ NAT                (firewall mode only;
#                                                        the vpc module's own
#                                                        NAT default route
#                                                        covers nat mode)
# public-RT    per-AZ private-subnet CIDR -> same-AZ firewall endpoint
#              (symmetric inspection: IGW->NAT return traffic re-enters the
#              same-AZ firewall endpoint before reaching workers)
#
# All three counts derive from length(var.azs) — plan-safe even though the
# endpoint ids themselves are unknown until apply.

resource "aws_route" "private_default_via_firewall" {
  count = local.create_firewall ? local.az_count : 0

  route_table_id         = var.private_route_table_ids[count.index]
  destination_cidr_block = "0.0.0.0/0"
  vpc_endpoint_id        = local.firewall_endpoint_id_by_az[var.azs[count.index]]
}

resource "aws_route" "firewall_subnet_to_nat" {
  count = local.create_firewall ? local.az_count : 0

  route_table_id         = var.intra_route_table_ids[count.index]
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = var.nat_gateway_ids[count.index]
}

resource "aws_route" "public_return_via_firewall" {
  count = local.create_firewall ? local.az_count : 0

  # element() wraparound: today's single shared public RT (length 1) gets
  # all three return routes; a future per-subnet-RT topology (length 3)
  # would instead give each AZ its own RT with one route each.
  route_table_id         = element(var.public_route_table_ids, count.index)
  destination_cidr_block = var.private_subnet_cidrs[count.index]
  vpc_endpoint_id        = local.firewall_endpoint_id_by_az[var.azs[count.index]]
}
