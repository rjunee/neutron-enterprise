-- Copyright (c) 2026 Ryan Junee.
-- SPDX-License-Identifier: Apache-2.0
--
-- Synthetic Meridian fee-postings SQL source. The owning guardian compares all
-- five rows with tools/meridian/fixtures/meridian-v1/banking.json so this deploy
-- projection cannot silently drift from the canonical demo corpus.

CREATE TABLE IF NOT EXISTS public.fee_postings (
  fee_posting_id text PRIMARY KEY,
  customer_id text NOT NULL,
  deposit_account_id text NOT NULL,
  fee_code text NOT NULL,
  amount_cents integer NOT NULL CHECK (amount_cents >= 0),
  posted_date date NOT NULL,
  reversed boolean NOT NULL
);

INSERT INTO public.fee_postings (
  fee_posting_id,
  customer_id,
  deposit_account_id,
  fee_code,
  amount_cents,
  posted_date,
  reversed
) VALUES
  ('mer-fee-001', 'mer-cus-001', 'mer-dep-001', 'OD-PAID', 3500, DATE '2026-08-02', FALSE),
  ('mer-fee-002', 'mer-cus-002', 'mer-dep-002', 'WIRE-DOM', 2500, DATE '2026-08-05', FALSE),
  ('mer-fee-003', 'mer-cus-003', 'mer-dep-003', 'STOP-PAY', 3000, DATE '2026-08-07', TRUE),
  ('mer-fee-004', 'mer-cus-004', 'mer-dep-004', 'NSF-RETURN', 0, DATE '2026-08-09', FALSE),
  ('mer-fee-005', 'mer-cus-005', 'mer-dep-005', 'OD-PAID', 3500, DATE '2026-08-11', FALSE)
ON CONFLICT (fee_posting_id) DO UPDATE SET
  customer_id = EXCLUDED.customer_id,
  deposit_account_id = EXCLUDED.deposit_account_id,
  fee_code = EXCLUDED.fee_code,
  amount_cents = EXCLUDED.amount_cents,
  posted_date = EXCLUDED.posted_date,
  reversed = EXCLUDED.reversed;

GRANT CONNECT ON DATABASE meridian TO meridian_cdc;
GRANT USAGE ON SCHEMA public TO meridian_cdc;
GRANT SELECT ON TABLE public.fee_postings TO meridian_cdc;

SELECT 'CREATE PUBLICATION meridian_fee_postings FOR TABLE public.fee_postings'
WHERE NOT EXISTS (
  SELECT 1 FROM pg_publication WHERE pubname = 'meridian_fee_postings'
)
\gexec
