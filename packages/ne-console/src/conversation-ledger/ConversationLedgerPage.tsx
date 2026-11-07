// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useLayoutEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { ApiError } from "../api/client";
import {
  ConversationLedgerCutoverUnavailableError,
  type ConversationLedgerClient,
} from "../api/conversationLedgerClient";
import { ListScreen } from "../primitives/PageHeader";
import { Empty, Loading, LoadError, Refused } from "../primitives/states";
import { HashChip } from "../primitives/HashChip";
import { absoluteTime, truncateHash } from "../primitives/format";
import { useConversationLedgerClient } from "./useConversationLedgerClient";
import type {
  ConversationLedgerView,
  ConversationTurnView,
  ProtectedBindingView,
  ProtectedContentStatus,
} from "./types";
import "./conversation-ledger.css";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; ledger: ConversationLedgerView }
  | { kind: "refused" }
  | { kind: "cutover_unavailable" }
  | { kind: "error" };

interface LedgerReadOwner {
  client: ConversationLedgerClient;
  attempt: number;
}

interface OwnedLoadState {
  owner: LedgerReadOwner;
  state: LoadState;
}

/**
 * THE STORED VOCABULARY, SAID ONCE, IN THE READER'S LANGUAGE.
 *
 * Every value below is a closed server enum (`conversation_ledger.py`), and each
 * one used to reach the screen exactly as stored: a reader met
 * `source_bound_container` in a definition list and `agent-transport-v1` beside
 * the word "Generation". Those are storage-engine names, not sentences — and
 * this is the only ledger with a rail entry, so they are the first words a
 * non-engineer reads on it.
 *
 * Short label on screen, whole sentence in the tooltip: the same shape the
 * evidence drawer uses for its assurance badges, so the two ledgers read alike.
 * A label never says more than its enum carries — "Holds source material" is the
 * stored classification of a row, not a claim about what the material says.
 */
interface StoredTerm {
  label: string;
  gloss: string;
}

/**
 * THERE IS NO "arm this build has never seen" FALLBACK HERE, AND THERE CANNOT BE.
 *
 * Each vocabulary below is keyed by the view type's own literal union, so every
 * lookup is total by construction. Nothing can miss: a row carrying a status, a
 * content state or a transport generation outside these sets never reaches this
 * component, because `parseConversationLedger` refuses the whole response first
 * — `parseBinding`, `parseTurn` and its hard
 * `transport_generation !== "agent-transport-v1"` check all call `malformed()`,
 * which throws (`api/conversationLedgerClient.ts`) — and the page renders its
 * read-error state instead.
 *
 * That is the right shape for a PROTECTED ledger: `content_state` is what
 * decides whether a body may be shown at all, so a row this build cannot fully
 * validate must not be drawn rather than drawn with a shrug beside it. This
 * screen used to carry an "unrecognised" arm and a comment promising that an
 * operator chasing a version skew could read the raw value off its tooltip.
 * They could not — on a skewed response the read had already failed, and the
 * tooltip was never rendered.
 */
const BINDING_STATUS: Record<ProtectedContentStatus, StoredTerm> = {
  source_free: {
    label: "No source material",
    gloss:
      "Nothing stored for this half of the turn came from a source document, so it can be shown exactly as it was retained.",
  },
  source_bound_container: {
    label: "Holds source material",
    gloss:
      "This half of the turn carries source-derived text. Reading it needs the source's own authorization, which this page does not grant.",
  },
  /**
   * FOUR CAUSES, ONE STATUS — SO THIS LABEL NAMES NONE OF THEM.
   *
   * `legacy_unavailable` does not mean "predates protected storage". The
   * server reaches it four ways (`ne-api`, `routers/chat.py`): the stored
   * classification written by the legacy migration (`classify_legacy` — no
   * authenticated source proof, so the row can never be admitted as
   * source-free), plus three failures to open the protected row, all caught in
   * one `except` at chat.py:651 — the row is missing, its envelope will not
   * open (a corrupted `encrypted_dek`), or it is owned by another principal.
   * The server's own comment there says why the three are one outcome: "A
   * missing/tampered/foreign protected row is one unavailable content item,
   * not an oracle that changes the whole response", and its parameterised test
   * `test_unreadable_protected_turn_projects_one_neutral_shell` seeds each
   * damage in turn and asserts this same status for all of them.
   *
   * This page cannot know which of the four it is looking at: the wire carries
   * the status and nothing that would separate them. So it says the one thing
   * true of all four — no protected body could be established — and leaves the
   * cause where the record left it.
   *
   * BOTH DIRECTIONS ARE THE SAME ERROR. Copy asserting the turn predates
   * protected storage reads a tampered or foreign-owned row as routine
   * housekeeping — and does it beside the live `protected_ref` and 64-hex
   * `protected_hash` that this same row still renders. Copy that says
   * "tampered" cries wolf on the common case. Neither fact is in the response.
   *
   * (This branch shipped the first of those, in the round named for the
   * console describing the SERVER's records instead of its own reading of
   * them. Fifth instance of that class found here.)
   */
  legacy_unavailable: {
    label: "No readable body",
    gloss:
      "No protected body could be established for this half of the turn. The record does not say whether it was written before protected storage or its protected copy can no longer be read, so this page does not say either.",
  },
};

const CONTENT_STATE: Record<ConversationTurnView["content_state"], StoredTerm> = {
  available: {
    label: "Shown in full",
    gloss:
      "Both messages are stored free of source material, so the turn appears exactly as it was retained.",
  },
  content_withheld: {
    label: "Withheld",
    gloss:
      "The turn was retained, but its body carries source material. This page shows the record of the turn, never its words.",
  },
  // The whole-turn half of the four-cause note above `BINDING_STATUS`'s
  // `legacy_unavailable`: the same status, said for the turn rather than one
  // half of it, and naming neither cause.
  legacy_unavailable: {
    label: "Couldn’t be shown",
    gloss:
      "No protected body could be established for this turn. That is what a turn written before protected storage looks like, and also what a turn whose protected copy can no longer be read looks like — the record does not tell the two apart, so neither does this page.",
  },
};

const TRANSPORT_GENERATION: Record<ConversationTurnView["transport_generation"], StoredTerm> = {
  "agent-transport-v1": {
    label: "Protected transport, v1",
    gloss: "The generation of protected transport that wrote this turn.",
  },
};

function BindingMetadata({ label, binding }: { label: string; binding: ProtectedBindingView }) {
  const status = BINDING_STATUS[binding.status];
  return (
    <section className="ne-conversation-ledger__binding" aria-label={label + " binding"}>
      <h3>{label}</h3>
      <dl>
        <div>
          <dt>Contents</dt>
          <dd title={status.gloss}>{status.label}</dd>
        </div>
        <div>
          <dt>Protected ref</dt>
          {/*
           * NOT A HASH, SO NOT A HashChip. §2.7's truncation rule is about
           * hashes. `protected_ref` is an opaque row identifier — the server
           * sets it from `row.row_id` (`routers/chat.py:433`) and the parser
           * only requires a non-empty string, no digest shape. Truncating it
           * to eight characters would mangle the one string here an operator
           * quotes to someone else, which is the same reason
           * `approvals/DecisionPane.tsx` refuses HashChip for a request id.
           * That pane's `CopyValue` is local and unexported, so there is no
           * shared primitive to reach for; a whole ref under its own label is
           * the honest rendering until one exists.
           */}
          <dd>{binding.protected_ref}</dd>
        </div>
        <div>
          <dt>Protected hash</dt>
          {/*
           * §2.7, VIA THE PRIMITIVE THAT OWNS IT. `protected_hash` is a
           * parser-verified 64-hex SHA-256 (`SHA256` in
           * `api/conversationLedgerClient.ts` rejects anything else), and this
           * page draws TWO of them per turn — input and terminal — in a
           * two-column grid. Painted whole they were the widest thing on the
           * screen by a wide margin, which is the overflow HashChip's own
           * header records happening on four other routes.
           *
           * Nothing is lost by truncating: HashChip keeps the served value in
           * `title` and writes the SERVED value, not the eight shown
           * characters, to the clipboard. `protected_ref` beside it stays
           * whole on purpose — see the note on its own row.
           */}
          <dd>
            <HashChip
              hash={binding.protected_hash}
              label={
                "Copy " +
                label.toLowerCase() +
                " protected hash " +
                truncateHash(binding.protected_hash)
              }
            />
          </dd>
        </div>
      </dl>
    </section>
  );
}

function TurnBody({ turn }: { turn: ConversationTurnView }) {
  if (turn.content_state === "available") {
    return (
      <ol
        className="ne-conversation-ledger__messages"
        aria-label={"Projected messages for session " + turn.session_id + " turn " + turn.seq}
      >
        {turn.messages.map((message, index) => (
          <li key={message.role + ":" + index}>
            <span>{message.role === "user" ? "User" : "Assistant"}</span>
            <p>{message.content}</p>
          </li>
        ))}
      </ol>
    );
  }
  return (
    <p className="ne-conversation-ledger__unavailable" role="status">
      {turn.content_state === "content_withheld"
        ? "Withheld — this turn carries source material, and reading it needs the source's own authorization, not this page's."
        : "Couldn’t be shown — no protected body could be established for this turn. That is what a turn written before protected storage looks like; it is also what a turn whose protected copy can no longer be read looks like. This page cannot tell the two apart."}
    </p>
  );
}

function Turn({ turn }: { turn: ConversationTurnView }) {
  const content = CONTENT_STATE[turn.content_state];
  const generation = TRANSPORT_GENERATION[turn.transport_generation];
  return (
    <article className="ne-conversation-ledger__turn">
      <header>
        <div>
          <strong>{turn.principal_id}</strong>
          <span>
            {turn.agent_name} · {turn.department} · session {turn.session_id} · turn {turn.seq}
          </span>
        </div>
        <time dateTime={turn.created_at} title={absoluteTime(turn.created_at)}>
          {absoluteTime(turn.created_at)}
        </time>
      </header>
      <div className="ne-conversation-ledger__trace">
        <span>Trace</span>
        <Link to={"/trace/" + encodeURIComponent(turn.trace_id)}>{turn.trace_id}</Link>
      </div>
      <div className="ne-conversation-ledger__transport">
        <span>
          Content <strong title={content.gloss}>{content.label}</strong>
        </span>
        <span>
          Written by <strong title={generation.gloss}>{generation.label}</strong>
        </span>
      </div>
      <div className="ne-conversation-ledger__bindings">
        <BindingMetadata label="Input" binding={turn.input} />
        <BindingMetadata label="Terminal" binding={turn.terminal} />
      </div>
      <TurnBody turn={turn} />
    </article>
  );
}

export function ConversationLedgerContent({
  ledger,
  onRefresh,
}: {
  ledger: ConversationLedgerView;
  onRefresh: () => void;
}) {
  if (ledger.turns.length === 0)
    return (
      <Empty message="No conversation turns retained yet — they appear here as agents are used." />
    );
  return (
    <>
      <div className="ne-conversation-ledger__retention">
        <span>Retained until governed reset</span>
        <span className="ne-conversation-ledger__retention-summary">
          <span>
            Showing {ledger.returned_turns} whole turns
            {ledger.truncated ? " · response capped" : ""}
          </span>
          <button className="ne-btn ne-btn--quiet" type="button" onClick={onRefresh}>
            Refresh
          </button>
        </span>
      </div>
      <ol className="ne-conversation-ledger__turns">
        {ledger.turns.map((turn) => (
          <li key={turn.session_id + ":" + turn.seq}>
            <Turn turn={turn} />
          </li>
        ))}
      </ol>
    </>
  );
}

export function ConversationLedgerPage() {
  const client = useConversationLedgerClient();
  const [attempt, setAttempt] = useState(0);
  const owner = useMemo<LedgerReadOwner>(() => ({ client, attempt }), [attempt, client]);
  const [settled, setSettled] = useState<OwnedLoadState>({
    owner,
    state: { kind: "loading" },
  });

  useLayoutEffect(() => {
    let active = true;
    const controller = new AbortController();
    void client
      .read(controller.signal)
      .then((ledger) => {
        if (active) setSettled({ owner, state: { kind: "ready", ledger } });
      })
      .catch((error: unknown) => {
        if (!active || controller.signal.aborted) return;
        setSettled({
          owner,
          state:
            error instanceof ApiError && error.status === 403
              ? { kind: "refused" }
              : error instanceof ConversationLedgerCutoverUnavailableError
                ? { kind: "cutover_unavailable" }
                : { kind: "error" },
        });
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [client, owner]);

  const state = settled.owner === owner ? settled.state : { kind: "loading" as const };
  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  return (
    <ListScreen
      title="Conversation ledger"
      // THE OTHER LEDGER, NAMED FROM HERE. These two are confused constantly,
      // and the confusion is expensive: "Conversation ledger" is the only
      // ledger with a rail entry, so a reader looking for the monitored
      // ANSWER's history opens this page, finds the protected-content index —
      // frequently empty on a deployment that has not cut over — and reads that
      // emptiness as the answer history being empty. It is not: it is a
      // different ledger of a different thing.
      //
      // This is also `/answer-ledger`'s door now. Its previous one was a link
      // pinned to the agents sidebar's foot, which pinned a whole monitored
      // question above the conversation history and pushed the history out of
      // view; removing that link without putting the door somewhere would have
      // orphaned a live route. It cannot go in the rail: RailIcon.test.tsx
      // holds every rail destination against the frozen round-3 render
      // (design/round3/screens/home.html), which carries no sprite for this
      // one, and drawing a mark for it here is exactly the invented affordance
      // that suite exists to prevent.
      action={<Link to="/answer-ledger">Monitored answer history</Link>}
    >
      {state.kind === "loading" ? <Loading things="conversation turns" /> : null}
      {state.kind === "refused" ? (
        <Refused thing="conversation disclosures" page="Conversation ledger" />
      ) : null}
      {state.kind === "error" ? <LoadError things="conversation turns" onRetry={retry} /> : null}
      {state.kind === "cutover_unavailable" ? (
        // WHY THIS SENTENCE IS LONGER THAN THE ONE IT REPLACED. A reader
        // reaches this page from the rail's ONLY entry containing the word
        // "ledger", and the line that used to sit here — "Protected transport
        // cutover not completed or unavailable." — named a migration step
        // instead of answering the question they arrived with. So they read the
        // page's silence as "the agents answered nothing", which is the exact
        // confusion the comment on this screen's title has been describing for
        // months. It is the opposite: this page cannot read the store, and it
        // does not know, or claim, how much is in it.
        <p role="status">
          Nothing to show here yet. This page lists each question put to an agent and the answer it
          gave — but only once this deployment stores conversations under protected transport, and
          it has not switched over yet. That is a store this page cannot read, not a history that is
          empty.
        </p>
      ) : null}
      {state.kind === "ready" ? (
        <ConversationLedgerContent ledger={state.ledger} onRefresh={retry} />
      ) : null}
    </ListScreen>
  );
}
