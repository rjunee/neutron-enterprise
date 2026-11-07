# Coordinated Vulnerability Disclosure (CVD) Policy

This policy governs how the Neutron Enterprise maintainer receives, triages, and discloses
security vulnerabilities in Neutron Enterprise. It is the process referenced by
[`.github/SECURITY.md`](../../.github/SECURITY.md).

## Intake channel

- **Channel:** GitHub private vulnerability reporting ("Report a vulnerability" on the repository's Security tab).
- Do not use public issues, discussions, or pull requests for security reports.
- We accept reports from anyone (customers, researchers, downstream users).

## Response targets (best effort)

| Stage | Commitment |
| --- | --- |
| Acknowledgment | within **48 hours** of receipt |
| Triage + severity assignment | promptly after acknowledgment |
| Status updates | at least every 7 days while open |
| Coordinated disclosure | default **90 days** from report, or when a fix is available (whichever is sooner) |

We use CVSS v3.1/v4.0 for severity. Fix targets by severity (best effort, not contractual):

| Severity | Fix target |
| --- | --- |
| Critical | 14 days |
| High | 30 days |
| Medium | 90 days |
| Low | next scheduled release |

## Safe harbor

We will not pursue or support legal action against researchers who, in good
faith:

- make a reasonable effort to avoid privacy violations, data destruction, and
  service degradation;
- only interact with accounts they own or have explicit permission to access;
- give us a reasonable time to remediate before public disclosure; and
- do not exploit a finding beyond what is necessary to demonstrate it.

Activity conducted consistent with this policy is authorized, and we will not
consider it a violation of our terms.

## Disclosure

Once a fix ships, we coordinate a disclosure timeline with the reporter and
credit them (with consent). Confirmed vulnerabilities receive a CVE where
applicable, and are recorded in release notes and — when relevant — an OpenVEX
statement (see [`tools/supply-chain/vex/`](../../tools/supply-chain/vex/)).

## Relationship to CRA reporting

For vulnerabilities that are **actively exploited** or part of a **severe
incident**, the coordinated timeline above is overridden by the statutory CRA
early-warning cadence (24h / 72h / 14d) — see
[cra-readiness.md](cra-readiness.md). The CVD intake is the trigger that starts
both clocks.
