# CRA Reporting Readiness

The EU **Cyber Resilience Act (CRA)** imposes vulnerability- and incident-
reporting obligations on manufacturers of products with digital elements.
The reporting obligations apply from **2026-09-11**.

**Maintainer:** the Neutron Enterprise maintainer (github.com/rjunee).

This document is our internal runbook to be ready before that date.

## Statutory reporting cadence

For an **actively exploited vulnerability** or a **severe security incident**
affecting Neutron Enterprise, the maintainer must submit, via the ENISA single reporting
platform (routed to the coordinating CSIRT):

| Deadline | Report |
| --- | --- |
| **≤ 24 hours** from awareness | **Early warning** — that an actively exploited vuln / severe incident exists. |
| **≤ 72 hours** from awareness | **Vulnerability / incident notification** — details, severity, corrective/mitigating measures taken. |
| **≤ 14 days** (vuln) / after handling (incident) | **Final report** — root cause, full remediation, mitigations for users. |

The 24h/72h cadence applies to both the "actively exploited vulnerability" track
and the "severe incident" track.

## Trigger

The **CVD intake** (see [cvd-policy.md](cvd-policy.md)) is the trigger. The moment
The maintainer becomes aware that a reported (or internally discovered) vulnerability is
**being actively exploited**, or that a **severe incident** has occurred, the
24-hour clock starts.

## Internal responsibilities (who reports what)

| Role | Responsibility |
| --- | --- |
| Maintainer (GitHub private vulnerability reporting) | Decision-maker; submits the early warning, notification, and final report to the ENISA platform; owns the CSIRT relationship. |
| Engineering | Produces technical detail, severity (CVSS), root-cause analysis, and the fix; feeds the reports. |
| CVD intake | Detects and time-stamps awareness; escalates exploited/severe cases immediately to the owner. |

## Pre-2026-09-11 checklist

- [ ] Register / identify the correct ENISA single reporting platform entry point
      and coordinating CSIRT for the maintainer.
- [ ] Keep GitHub private vulnerability reporting enabled on the repository so the published channel actually routes.
- [ ] Dry-run the 24h early-warning path (owner + engineering).
- [ ] Confirm SBOM + provenance are attached to every release so a report can
      cite exact affected component versions (see [supply-chain.md](supply-chain.md)).

## Source

Obligations summarized from the European Commission CRA guidance:
<https://digital-strategy.ec.europa.eu/en/policies/cra-reporting>.
