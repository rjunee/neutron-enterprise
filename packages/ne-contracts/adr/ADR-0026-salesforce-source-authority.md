# ADR-0026: Salesforce source authority

Status: Accepted

## Decision

Add `SalesforceSourcePoint` to the closed `SourceIdentityPoint` union. Its immutable
coordinates are the provider-issued Salesforce organization id, object API name,
and record id. The organization id is accepted only from the OAuth token response
Identity URL (`/id/{organization_id}/{user_id}`) or Identity response
`organization_id`; 15-character ids are converted to Salesforce's 18-character
case-safe form and inconsistent 18-character suffixes are refused.

Hosts, instance URLs, connected-app ids, usernames, owners, credentials, token
contents, and operator-supplied identity values are not organization identity.
Credential rotation preserves identity. Organization, object, record, connector
generation, or deployment changes identity. A refreshed sandbox with a new
organization id is a new authority and cannot silently adopt or reset an old
binding.

## Compatibility

This is additive. Existing M365 and governed-local-T3 points and all frozen
Knowledge protocol method sets are unchanged. `ne-contracts` advances from
1.22.0 to 1.23.0.
