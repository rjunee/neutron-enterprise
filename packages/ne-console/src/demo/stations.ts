// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE GUIDED TOUR — the station registry.
//
// This is the one place in the product where explanation belongs (§2.4): every
// screen states its facts and nothing else, and the walkthrough is where someone
// is told how the parts fit together. Everywhere else, an explanatory sentence
// is a defect.
//
// A STATION IS A JOB, NOT A FEATURE. The stations used to narrate the product's
// properties — one per screen, in build order, each explaining what that screen
// was for. That is a feature tour: it demonstrates that the console has screens.
// A station is now a task a named person actually does, on the records this
// deployment actually holds, and the reader is expected to DO it: the role, the
// click path, and what they should see.
//
// THREE RULES THE COPY BELOW IS HELD TO.
//
// (1) A STATION MUST BE PERFORMABLE ON THIS DEPLOYMENT, RIGHT NOW. Not "when the
//     connector lands", not "on a machine with a bigger model". Every path below
//     was walked against the running deployment before it was written, and the
//     ones that came back empty were CUT rather than narrated:
//       · the connector list — this deployment serves an empty one, so a station
//         sending someone to read connector freshness is a station that lands on
//         a blank screen;
//       · following a citation out of an Agent's answer — the answer stream this
//         deployment produces carries no citations, so the source check is done
//         where the passage IS served: the search result's own pane;
//       · reaching a record through a citation anchor, from either a search
//         result or a verified-answer row — the search read comes back with no
//         nodes here, and this release's citations are `urn:ne:meridian:…`,
//         which `parseEntityUrn` does not parse, so the verified row's citation
//         renders as a chip and not a link. The record is reached where it IS
//         reachable: the Browse records view on Verified Queries, which runs the
//         reviewed `t3-records-in-class` / `t3-record` pair;
//       · the period metrics screen — it serves numbers, but reading numbers is
//         not a job anyone does on Meridian's servicing records, and most of
//         its rows are zero on this deployment.
//     Fewer real stations beat nine narrated ones.
//
// (2) A STATION MAY NOT PREDICT A READING. Station 1 used to assert "zero
//     external requests" and tell the presenter the egress readout would agree,
//     while the deployment it ran on allowed one host and said so one click
//     away. Buyers are chartered to find exactly that. No station states a
//     count, a score or a verdict; the station about what this deployment may
//     reach renders this deployment's OWN figure instead (`liveReadout`).
//
// (3) THE NAMES ARE REAL, AND THEY ARE THIS TENANT'S. Every record named below
//     is carried in the Meridian release this deployment actually seeds
//     (`tools/meridian/fixtures/meridian-v1`), and `tenantRecords` lists them so
//     the registry's test can hold each one against that fixture. An invented
//     customer in a demo about not inventing things is the worst defect this
//     product can ship — and a customer belonging to SOMEONE ELSE'S tenant is
//     the same defect wearing a plausible face. This tour shipped for months
//     opening on a person and a company from the PREVIOUS demo tenant, long
//     after every other surface had been moved to Meridian, because the wart was
//     written down and never asserted against. The assertion now exists:
//     `src/test/tenant-identity-guardian.test.ts` walks the bundled sources and
//     fails on that tenant's name or any of its records. Do not weaken it.
//
// (4) A STATION MAY ONLY NAME A SURFACE THE SHIPPED NAVIGATION HAS. Station 1
//     told the room to "open Ask" for months after Ask stopped being a rail
//     destination (owner, 2026-08-27: Agents leads Execution, and the Ask
//     surface is the configured Agent's Conversation tab). The presenter read
//     it aloud, scanned a sidebar that says Knowledge Layer / Execution /
//     Operations / Governance / Insights, and found nothing called Ask. The
//     copy was the defect, not the rail. So: in the three fields that SEND a
//     reader somewhere — `path`, `see`, and each link's `label` — every
//     capitalised name used after a navigation verb (open, on, in, from, switch
//     to, go to) must be a real rail destination or a real in-page panel, and
//     `src/test/station-surface-guardian.test.ts` derives both sets from
//     `rail/navSections.ts` and the shipped tab strips rather than listing
//     them, because a hand-written list rots exactly the way this copy did.
//     The guard does NOT cover `title`, `role` and `job`: those are description
//     rather than navigation, and those short verbs occur in ordinary English
//     there ("a servicing agent on Meridian's frontline desk"), so scanning them
//     would flag this tenant's own name. A surface worth naming belongs in
//     `path` or a link label anyway — that is where a reader is told to go.
//     Nor is a PANEL the same as a CONTROL: the guard's vocabulary holds panes
//     you can be on, not buttons you press, so "Follow live" is not a name a
//     station may use after a navigation verb. The guardian's own header states
//     the rest of what it does not cover.
//
// WHAT IS AUTHORED AND WHAT IS SERVED. Every sentence here is written here.
// `claimSource` says so on every station, because a previous commit labelled
// authored copy as server-sourced — in the commit whose purpose was removing
// that exact dishonesty. The ONE thing the panel shows that a server produced
// is the egress figure, and it is not a sentence: it is read at render time.

/** One station link — a label + a route the shipped console actually registers. */
export interface StationLink {
  label: string;
  to: string;
}

/**
 * Where a station's words come from.
 *
 * `authored` — written in this file by a person.
 * `served`   — the exact text a server sent, rendered as it arrived.
 *
 * This module holds no served text, so `served` is never a truthful label for
 * anything defined in it (`claimSourceIsHonest` is the guard, and the registry's
 * test drives it both ways).
 */
export type ClaimSource = "authored" | "served";

/** One numbered station in the tour: a job, who does it, how, and what they see. */
export interface StationDef {
  /** Stable id, also the check-off key. Never rendered. */
  id: string;
  /** Display numeral. */
  numeral: string;
  /** The job, sentence case, like every other title in the product (§0). */
  title: string;
  /** Who does this job. A role, and where the deployment names one, a person. */
  role: string;
  /** The task itself, in one line — what is being achieved and on what. */
  job: string;
  /** The exact click path, in order. */
  path: string;
  /** What they should see when they have done it. */
  see: string;
  /** Deep links into real, registered routes. */
  links: ReadonlyArray<StationLink>;
  /** Provenance of the prose above. Always `authored` — see ClaimSource. */
  claimSource: ClaimSource;
  /** Meridian records this station names, exactly as they are spelled in the copy. */
  tenantRecords: readonly string[];
  /** Render this deployment's own egress figure beside the station. */
  liveReadout?: "egress";
}

export const STATIONS: readonly StationDef[] = [
  {
    id: "station-answer",
    numeral: "1",
    title: "Answer a question, then check it against the source",
    role: "A servicing agent on Meridian's frontline desk",
    job: "A customer has disputed a debit-card transaction and asks how long Meridian has to put provisional credit on the account. The agent needs the answer and the passage it rests on, not one or the other.",
    path: "Sign in as the frontline agent and open Agents, then open the workspace for the Meridian analyst. On its Conversation tab, put the question in your own words — what is the provisional credit deadline for a debit card dispute. Read the answer. Then open Search, run the same words, and open the first row.",
    see: "An answer on the Conversation tab. On Search, a ranked table of Meridian documents, and the row you open shows the passage itself, the document it sits in and its citation.",
    links: [
      { label: "Open Agents", to: "/agents" },
      { label: "Open Search", to: "/search" },
    ],
    claimSource: "authored",
    tenantRecords: [],
  },
  {
    id: "station-second-identity",
    numeral: "2",
    title: "Run the same question as somebody else",
    role: "An access reviewer, checking that an answer is cut to the person asking",
    job: "One query, two sign-ins, side by side — the permission boundary done rather than described.",
    path: "With those results still on screen in Search, choose Compare as another user and sign in as the approver. The same query re-runs under that sign-in.",
    see: "A second column headed with that sign-in. Every row states whether it was returned to them, and each column is that identity's own answer from the server.",
    links: [{ label: "Open Search", to: "/search" }],
    claimSource: "authored",
    tenantRecords: [],
  },
  {
    id: "station-record",
    numeral: "3",
    title: "Open the record behind a dispute",
    role: "A servicing lead, before a call with a customer",
    job: "Pull the disputes this sign-in may see, then open one of them and read every field the store holds against it.",
    path: "Open Verified Queries and run the disputes directory. Then switch to Browse records, choose the RegEDispute class, and open the record for mer-dsp-001.",
    see: "The directory's rows, each carrying its own citation; and on the record you open, every predicate and value the store asserts for that subject — with a class whose graph your sign-in does not hold coming back empty rather than guessed.",
    links: [{ label: "Open the disputes directory", to: "/verified-queries" }],
    claimSource: "authored",
    tenantRecords: ["mer-dsp-001"],
  },
  {
    id: "station-decide",
    numeral: "4",
    title: "Decide a write into Salesforce",
    role: "The approver — a different sign-in from the one that asked",
    job: "A request to attach a note to an account record in Salesforce is waiting. The write does not happen until a person has read it and said so.",
    path: "Sign in as the approver and open Approvals. Open a waiting request, reveal what happens if approved, and read the target record with the note's title and body as they will be sent. Then approve it.",
    see: "The note exactly as it will be sent, the reversibility this class of action carries, and — once you decide — a receipt you can copy. The screen tells you plainly that approving executes it.",
    links: [{ label: "Open Approvals", to: "/approvals" }],
    claimSource: "authored",
    tenantRecords: [],
  },
  {
    id: "station-reconstruct",
    numeral: "5",
    title: "Reconstruct that decision afterwards",
    role: "A compliance reviewer, working from the operator sign-in",
    job: "Find the decision that was just taken, read the chain verdict the server computed around it, and leave with the window as a file.",
    path: "Sign in as the operator and open the Audit log. Filter to the approval action, or to the approver who decided it, and open the record. Then choose Re-verify, and Export CSV.",
    see: "The entries in order with their actors, outcomes and entry numbers; the chain verdict, which is the server's and not this screen's; and a file carrying exactly the rows the filters left.",
    links: [{ label: "Open the Audit log", to: "/audit" }],
    claimSource: "authored",
    tenantRecords: [],
  },
  {
    id: "station-reach",
    numeral: "6",
    title: "Check what this deployment may run and reach",
    role: "The operator, before anyone points a real system at it",
    job: "Two questions with one answer each: which models may this deployment call, and which hosts outside it may it reach.",
    path: "Open Posture: the control rows carry their state, the network section names every allowed host, and the configuration in force is dated. Then open Models and stay on the Policy tab, which names each lane and what a route resolves to.",
    see: "Each control with its own state, every allowed host named, the configuration identified and dated, and the lanes this deployment carries. The figure beside this station is this deployment's, not a script's.",
    links: [
      { label: "Open Posture", to: "/posture" },
      { label: "Open Models", to: "/models" },
    ],
    claimSource: "authored",
    tenantRecords: [],
    liveReadout: "egress",
  },
];

/**
 * A station's label is truthful when it matches where its words actually come
 * from. Everything in this module is hand-written, so `authored` is the only
 * truthful label a station defined here can carry; `served` would be a claim
 * that a server sent a sentence this file invented.
 */
export function claimSourceIsHonest(station: StationDef): boolean {
  return station.claimSource === "authored";
}

/** Clamp an index into [0, len) with NO wraparound (arrow nav stops at the ends). */
export function clampIndex(i: number, len: number): number {
  if (len <= 0) return 0;
  if (i < 0) return 0;
  if (i > len - 1) return len - 1;
  return i;
}
