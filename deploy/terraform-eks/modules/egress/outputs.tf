# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

output "firewall_arn" {
  description = "The Network Firewall ARN. Null in nat mode."
  value       = module.firewall.arn
}

output "firewall_policy_arn" {
  description = "The Network Firewall policy ARN. Null in nat mode."
  value       = module.firewall.policy_arn
}

output "frontier_rule_group_arn" {
  description = "The frontier stateful rule group ARN. Null in nat mode or when frontier_fqdns is empty (air-gap — no rule group is created)."
  value       = module.frontier_rule_group.arn
}

output "firewall_endpoint_ids_by_az" {
  description = "Firewall VPC endpoint id, keyed by AZ. Empty map in nat mode."
  value       = local.firewall_endpoint_id_by_az
}

output "alert_log_group_name" {
  description = "ALERT CloudWatch log group name. Null in nat mode."
  value       = try(aws_cloudwatch_log_group.alert[0].name, null)
}

output "flow_log_group_name" {
  description = "FLOW CloudWatch log group name. Null in nat mode."
  value       = try(aws_cloudwatch_log_group.flow[0].name, null)
}

output "sni_pass_rule_count" {
  description = "Exact-match tls.sni pass rules rendered into the frontier rule group (the group itself is absent when this is 0 - air-gap); the one additional handshake pass rule authorizes no data egress."
  value       = length(var.frontier_fqdns)
}
