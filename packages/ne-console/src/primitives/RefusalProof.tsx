// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// REDESIGN §1.4 / L4 — A REFUSAL IS A PROOF, NOT AN ERROR. Every 403 in this
// console renders through this ONE component: what was refused, that the SERVER
// refused it, and that the attempt was itself audited. Never styled as an error;
// a 403 here is the product demonstrating the thing it is sold on.

import { Link } from "react-router";

export function RefusalProof({
  role,
  surface,
  chainReadable = false,
}: {
  /** The role whose key would open this surface — "ops", "approver", "steward". */
  role: string;
  /** What was refused, in plain English: "the audit ledger", "this queue". */
  surface: string;
  /** Render the see-it-in-the-chain link only when the audit poll is readable. */
  chainReadable?: boolean;
}) {
  return (
    /* GATED, NOT HELD (round-2 major). A role-scoped refusal is the product
       working, and it was drawn in the fault colour with the fault glyph — so a
       reader's home screen read as an outage: "denied, denied, denied" was the
       buyer's first screen. GATED has its own word, its own glyph and a slate
       ramp; red is kept for things that are actually wrong. */
    <section className="ne-refusal ne-refusal--gated" aria-label={`gated — ${role} key required`}>
      <h2 className="ne-refusal__head">
        <span aria-hidden="true">⊟</span> GATED — {role.toUpperCase()} KEY REQUIRED
      </h2>
      {/* THE ONE CLAIM A SKEPTIC TESTS FIRST. "…and audited the attempt" appeared
          on five surfaces, none of them a link, on a console whose own rule is
          that every strong claim opens onto something the reader can check. When
          the chain IS readable this now opens onto it. When it is NOT, the
          console stops asserting the write happened and says instead what it can
          actually stand behind: the refusal is designed to be written, and this
          key cannot read the record to confirm it. An unverifiable proof claim is
          worse than no claim. */}
      <p className="ne-refusal__body">
        {surface} is gated to {role} roles. The server refused this key with a 403.
      </p>
      {chainReadable ? (
        <>
          <p className="ne-refusal__body">
            Refused attempts are written to the audit chain — check this one there.
          </p>
          <Link className="ne-refusal__link" to="/audit">
            See the refusal in the chain →
          </Link>
        </>
      ) : (
        <p className="ne-refusal__body ne-refusal__unverified">
          Refused attempts are written to the audit chain by design. This key cannot read GET
          /v1/audit/events, so the console cannot show you that record and does not claim to have
          seen it.
        </p>
      )}
    </section>
  );
}
