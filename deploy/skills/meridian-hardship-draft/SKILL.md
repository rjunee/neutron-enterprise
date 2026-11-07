---
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
name: meridian-hardship-draft
version: 3
description: Draft a reviewable disaster-hardship outreach message from governed evidence.
source: bundled
triggers:
  - draft disaster hardship outreach
  - prepare a hardship assistance email
  - send a disaster hardship outreach email
category: servicing
tags:
  - hardship
  - outreach
requires_tools:
  - draft.email.create
  - read.knowledge.graph_path
  - read.knowledge.search
  - read.skills.view
platforms:
  - neutron-enterprise
acl_labels:
  - department:servicing
---
## When to Use
Use this procedure when governed Meridian evidence identifies a customer and loan as eligible for disaster-hardship outreach — whether the request is to draft the outreach or to send it.

## Procedure
1. Resolve the subject from served evidence: read the governed graph path from the named loan to its Customer to identify the customer, then, where the governed CRM contact lookup is granted, read that customer's recorded email address from the system of record. Never take a recipient address from the request text; if no governed read serves one, stop and say so.
2. Confirm that the served evidence identifies the customer, active loan, qualifying payment window, and absence of a hardship offer or repossession note.
3. Keep every factual statement within the served evidence and the approved hardship-program language.
4. Create one draft through the governed draft tool: a neutral subject, a concise assistance explanation, a clear request for the customer to contact servicing, addressed to the recorded email.
5. When the request includes sending: submit that draft's own reference through the governed send tool, and stop there. The send is approval-gated — submitting it pauses the run until a human approver decides; when an approver releases it the run performs that one approved send and ends. Never send by any other means, and never re-submit a rejected send.

## Pitfalls
Do not infer eligibility, contact details, balances, dates, promises, or regulatory conclusions. Do not treat a graph path as authority beyond its served evidence. Never address a draft to an email address that was not served by a governed read. The governed send tool's approval gate is the only path to a sent message — never work around a pending or rejected approval. Do not plan or promise any step after the send: the approved send is the last thing the run performs, so work placed after it would never happen.

## Verification
Verify that every factual statement maps to served evidence, the draft contains no unsupported commitment, the governed draft tool returned a draft reference, and any send was submitted only through the governed send tool with its approval gate intact.
