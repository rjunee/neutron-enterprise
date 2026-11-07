# ne-meridian

`ne-meridian` generates the deterministic `meridian-v1` retail-banking corpus for BD-1.
Meridian Bank, its customers, accounts, cases, and policy content are fictional. The only real
public-domain reference is FEMA Georgia Hurricane Helene declaration `DR-4830`.

The declaration constants live only in `ne.meridian.constants`. Columbia, Richmond, and Jefferson
are three actually designated Georgia counties selected for this pilot; the corpus never claims
they are the exhaustive FEMA designated-area list. McDuffie is an adjacent-county distractor
outside the selected three-county Meridian workflow subset, not a claim that FEMA omitted it.

## Data boundary

The fixture deliberately has two record layers:

- `records.json` validates against frozen `ne-schema`: household → `Account`, customer → `Person`,
  document metadata → `Document`, and servicing/complaint/dispute case → `Ticket`.
- `banking.json` carries banking-specific typed sidecars: addresses, deposit accounts, auto loans,
  debit cards, payment schedules, servicing tickets/notes, complaints, Reg E disputes, fee
  postings, the disaster table, fee-schedule rows, artifact versions, and ACLs.

No generated schema file is edited or extended. Every reference in both layers is closed over a
real record in the fixture.

## Determinism

The same seed produces byte-identical JSON, DOCX, PDF, Markdown, and CSV:

- randomness uses the existing cross-version-stable `ne.acme.rand.DetRandom`;
- all business dates use a fixed `2026-08-18` as-of date;
- JSON is canonical (`sort_keys`, two-space indent, ASCII, trailing newline);
- DOCX is fixed-order `ZIP_STORED` OOXML with a 1980 ZIP timestamp and fixed core metadata;
- PDF object offsets and xref are calculated from deterministic bytes;
- `manifest.json` SHA-256 hashes every other committed source or asset byte.

`ne-meridian check` regenerates the complete fixture and byte-compares it with the committed copy.

## Governed artifacts

There are 12 artifacts, all with a complete allow/deny decision for `ops`, `approver`, and
`frontline`:

| Artifact family | Versions | Format | SharePoint stage |
|---|---|---|---|
| Debit Card Dispute Handling SOP | v3, v4 current | real DOCX | v3 initial; v4 supersession |
| Regulation DD disclosure | 2026 | real PDF | initial |
| Consumer Deposit Fee Schedule | 2026 | structured JSON rows | initial |
| Disaster Hardship Response SOP | 2026 | Markdown | initial |
| Consumer Auto Collections Policy | 2025, 2026 current | Markdown near-duplicates | initial |
| Reg E Operations Bulletin | 2025, 2026 current | Markdown near-duplicates | initial |
| Disaster county reference | 2026 | CSV | initial |
| Servicing Note Quality Standard | 2026 | Markdown | initial |
| Complaint Escalation Playbook | 2026 | Markdown | initial |

The v3 and v4 DOCX files are intentional near-duplicates. V4 adds the amount-at-least-$500,
5-business-day expedited provisional-credit tier and changes documentation from a signed statement
before investigation to oral notice starting the investigation with a signed attestation due later.
The fixture filenames remain separate for deterministic evidence; both versions map to the same
SharePoint path so the seed tool performs a real replacement event.

## Generated ground truth

`ground_truth.json` is constructed only by traversing source models:

- **B1:** `DR-4830` selected counties → affected addresses → auto loans → unpaid scheduled
  payments due in the inclusive 14-day window → servicing notes → hardship eligibility. The
  actionable set is loans `001`–`003`; paid-off, already-offered, adjacent-county, repossession,
  outside-window, past-due, and already-paid distractors carry per-row exclusion bases.
- **B2:** each SOP version is evaluated independently. V3 has no overdue answer; current v4 has
  exactly one true positive, `mer-dsp-001`. Exactly one policy version is current.
- **B3:** the total value of *late* provisional credits is deliberately unanswerable because source
  records contain amounts but no provisional-credit issue dates. The required output is a refusal.
  The paired control—total recorded provisional-credit amount—is answerable and equals `$1,100.00`.

The models have no debit-card issue-date field and no provisional-credit issue-date field. Tests
pin that absence so a future generator cannot accidentally make B3 answerable.

## Commands

```sh
uv run --package ne-meridian ne-meridian generate \
  --out tools/meridian/fixtures/meridian-v1
uv run --package ne-meridian ne-meridian check
uv run --package ne-meridian pytest tools/meridian/tests -q
```

All emails use `meridian.example`, phones use the reserved `555-0100..0199` block, and addresses are
explicitly labeled demo addresses. No customer or deployment data is used.
