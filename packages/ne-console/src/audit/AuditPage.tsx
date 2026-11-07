// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §13 — AUDIT LOG. The compliance surface.
//
// The job: pull the trail for an actor, an action or a window; see whether the
// chain stands; export what was pulled, hashes included, and take it away. On
// incident: find the record, replay the decision, open the run.
//
// The shape is §1.4's list screen — title row, verdict strip, toolbar, table,
// pagination row — and nothing else. What this replaces was a page whose first
// screenful was a paragraph explaining that the console does not recompute the
// verdict, a drawer explaining it again, an authored table of what a chain
// record contains, and a chain drawn as a chain with no sortable column in it.
// A log screen is a table. The data is the claim.
//
// DEFAULT SORT IS SEQ DESC (§13): the newest record is the one an incident is
// about. Seq, Time, Actor, Action and Outcome are sortable; Resource and Trace
// are not — a link column has no order and a middle-truncated identifier sorts
// by a prefix nobody reads.
//
// THIN CLIENT (C7). Every verdict here is the SERVER's: the hashes are
// displayed and never compared, and sorting, the class chips and the
// search-within reorder and hide rows the server already sent — they compute
// nothing. The one thing the strip does NOT do is render a single read's
// `valid` verbatim: the server has a reproduced read-during-append race, so
// the resting word is the LATCHED verdict (audit/chainVerdict.ts) — the same
// reducer, over the same chain, that /trace renders. Nothing is hidden; a
// verdict is required to agree with itself before it becomes the resting state.

import { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Link, useSearchParams } from "react-router";
import { ApiError } from "../api/client";
import type {
  AuditEventsPage,
  AuditRecordView,
  ChainVerificationView,
  ReplayResultView,
} from "../api/types";
import { RuntimeConfigContext } from "../AppBootstrap";
import { useApiClient } from "../auth/useApiClient";
import { useDirectory } from "../directory/DirectoryContext";
import { useAuditPoll } from "../hooks/useAuditPoll";
import { useLatestRequest } from "../hooks/useLatestRequest";
import { DataTable } from "../primitives/DataTable";
import { HashChip } from "../primitives/HashChip";
import { PageHeader } from "../primitives/PageHeader";
import { PrincipalLabel } from "../primitives/PrincipalLabel";
import type { Column, TableStatus } from "../primitives/DataTable";
import { StatusPill } from "../primitives/StatePill";
import type { StateWord } from "../primitives/StatePill";
import { Empty, LoadError, Loading, Refused, SaveError } from "../primitives/states";
import { FilterChip, ToggleChip, Toolbar, ToolbarButton } from "../primitives/Toolbar";
import { downloadCsv } from "../primitives/csv";
import { absoluteTime, formatCount, relativeTime } from "../primitives/format";
import type { SortState } from "../primitives/tableSort";
import { filterRows, sortRows } from "../primitives/tableSort";
import { LedgerSpine } from "./LedgerSpine";
import { RecordPane } from "./RecordPane";
import { chainReading } from "./chainVerdict";
import { eventActionCode, eventActionLabel } from "./eventClass";
import { ledgerSpine } from "./spine";
import {
  anchorNotice,
  anchorPageNote,
  applyClassFilters,
  breakBandText,
  countRefusals,
  countToolCalls,
  DEFAULT_CLASSES,
  DEFAULT_WINDOW,
  exportFilename,
  NO_FILTERS,
  pageNote,
  parseAnchorParam,
  requestFilters,
  resolveLocalInstant,
  showingText,
  truncateMiddle,
  viewerTimeZone,
  WINDOW_SIZES,
} from "./ledger";
import type { AppliedFilters, ClassFilters, WindowSize } from "./ledger";
import "./audit.css";

/** §13: "Default sort: Seq desc." */
const DEFAULT_SORT: SortState = { key: "seq", dir: "desc" };

const NO_ROWS: readonly AuditRecordView[] = [];
const MAX_TRACE_ID_LENGTH = 256;
/** The one id the verdict line's `aria-controls` and `aria-describedby` point
 * at, and the id of the paragraph that holds the server's scope sentence. One
 * node, one copy of the sentence: the description a screen reader announces on
 * focus and the text a sighted reader reveals are the same characters. */
const VERDICT_REACH_ID = "audit-verdict-reach";

/**
 * The sentence beside the verdict pill. This strip IS the whole ledger's, so it
 * carries no "Entire audit chain ·" prefix the way /trace's drill-down does —
 * the word and the rule behind it are the same on both.
 *
 * THE SEQ-LESS ARM RENDERS. It was written as defensive — `_break`
 * (verify.py:180) takes `seq: int`, so an ORDINARY invalid verdict always names
 * an entry — and then the server gained one that does not: a CONTRADICTION
 * refuses while the `verify_chain` beneath it PASSES, so it has no entry to
 * name. A break the server did not locate has to say exactly that rather than
 * print "Break at entry —", which reads as a sentence that lost its number.
 *
 * `null` is "there is no sentence to write" — no read in hand under a word the
 * LATCH decided. The pill still renders; only the sentence is withheld.
 */
function chainLine(
  word: StateWord,
  verification: ChainVerificationView | null,
  breakSeq: number | null,
): string | null {
  if (word === "BREAK") {
    return breakSeq === null
      ? "Break confirmed — the server did not name the entry"
      : `Break at entry ${formatCount(breakSeq)}`;
  }
  if (word === "RE-VERIFYING") {
    return "Checking again — this reading has not been confirmed yet";
  }
  // VALID is answered only FROM a read in hand (`chainReading` rule 3), so the
  // count is there whenever this arm runs. There is no honest sentence without
  // one, and a filler where a served number belongs is the fabrication this
  // screen exists to make impossible.
  //
  // THE VERB IS THE SERVER'S `basis`, NOT A CONSTANT. `checked` is the number
  // of records the verdict COVERS; whether they were all re-hashed on this call
  // is a different fact, and it is the one the verb states.
  //
  //   * `anchored-extension` — the server proved the ledger still EXTENDS a head
  //     a full `verify_chain` blessed up to fifteen minutes ago and re-hashed
  //     only what was appended since. "N records verified" stated the count as
  //     the work this call did, which is a stronger claim than the one served;
  //     "covered" is what is true, and the REACH is `chainScope`'s to state in
  //     the server's own words.
  //   * `full-chain` — an unbounded walk really did re-hash every record on this
  //     request. "verified" is the accurate word and nothing is softened for a
  //     proof that was actually taken. This arm was UNREACHABLE while the basis
  //     was off the wire, which is why the verb had to be pinned to the weaker
  //     claim for both.
  //   * `windowed` — the walk was bounded to the applied from/to window, so this
  //     is not a whole-chain reading and may not be sentenced as one. It does not
  //     reach this arm today (`chain` is nulled for a window-scoped read before
  //     `chainReading` sees it) and states its own scope if it ever does.
  if (verification === null) return null;
  if (verification.basis === "windowed") {
    return `${formatCount(verification.checked)} records covered in the selected window`;
  }
  const verb = verification.basis === "full-chain" ? "verified" : "covered";
  return `${formatCount(verification.checked)} records ${verb}`;
}

/**
 * The SERVER'S OWN sentence about how far this verdict's proof reaches —
 * `ChainVerificationView.detail`, verbatim, never summarised. It names the
 * anchor entry, how many records were re-hashed on THIS call, and how long ago
 * the prefix it rests on was verified in full ("…and is NOT re-checked by this
 * call"). That is far too long for the 40px title row, so it is DISCLOSED: the
 * line itself carries the claim the screen can afford to state flat and becomes
 * the button that reveals the rest, on a surface below the toolbar where §1.4
 * permits prose. The dotted underline and the caret say there is more of it.
 *
 * IT WAS A `title` ATTRIBUTE ON A NON-FOCUSABLE SPAN, and that is the defect
 * this shape exists to close. A `title` shows on mouse hover and nowhere else —
 * no Tab reaches it, no tap reveals it, and screen readers announce it
 * inconsistently at best. The sentence it hid is the only thing on this screen
 * that explains why `/v1/audit/events` can say `valid=true` over a span that an
 * unbounded `verify_chain` elsewhere in the product would fail, so a reader who
 * cannot reach it is being shown a bare VALID. It is now the accessible
 * DESCRIPTION of the control as well as its revealed content, so it is
 * announced on focus without anyone having to guess that pressing helps.
 *
 * ONLY UNDER VALID. Under BREAK the read in hand is routinely a PASSING one —
 * a latched break comes to rest on passing reads (chainVerdict.ts) — so its
 * `detail` is "chain intact: …", and hanging that off an accusation is the
 * exact contradiction `breakReason` was narrowed to keep out. Under
 * RE-VERIFYING there is no verdict yet to state the reach of. `null` in both,
 * and `null` again when the server sent an empty sentence: no marker, no
 * tooltip, and a count that still stands on its own.
 */
function chainScope(word: StateWord, verification: ChainVerificationView | null): string | null {
  // Nothing to disclose about a FULL walk: "N records verified" is already the
  // whole claim and `detail` only restates it. The marker exists to say the
  // line is not the whole account of the proof, so it must not appear where it
  // is.
  if (word !== "VALID" || verification === null || verification.basis === "full-chain") {
    return null;
  }
  const detail = verification.detail.trim();
  return detail.length > 0 ? detail : null;
}

function traceReplayParam(params: URLSearchParams): string | null {
  const values = params.getAll("trace_id");
  if (values.length !== 1) return null;
  const traceId = values[0]!.trim();
  return traceId.length > 0 && traceId.length <= MAX_TRACE_ID_LENGTH ? traceId : null;
}

type TraceReplayState =
  | { requestKey: string; status: "ok"; replay: ReplayResultView }
  | { requestKey: string; status: "refused" | "error"; replay: null };

export function AuditPage() {
  const directory = useDirectory();
  const client = useApiClient();
  const runLatest = useLatestRequest();
  const poll = useAuditPoll();
  const config = useContext(RuntimeConfigContext);
  const [params] = useSearchParams();
  const traceId = traceReplayParam(params);
  const [traceReplay, setTraceReplay] = useState<TraceReplayState | null>(null);
  const [traceReplayNonce, setTraceReplayNonce] = useState(0);
  const traceReplayRequestKey = `${traceId ?? ""}\u0000${traceReplayNonce}`;

  useEffect(() => {
    if (traceId === null) return;
    let live = true;
    void client.replayAudit({ trace_id: traceId }).then(
      (replay) => {
        if (live) setTraceReplay({ requestKey: traceReplayRequestKey, status: "ok", replay });
      },
      (error: unknown) => {
        if (!live) return;
        setTraceReplay({
          requestKey: traceReplayRequestKey,
          status: error instanceof ApiError && error.status === 403 ? "refused" : "error",
          replay: null,
        });
      },
    );
    return () => {
      live = false;
    };
  }, [client, traceId, traceReplayRequestKey]);

  // Identity-tagged state makes stale data unrenderable in the synchronous
  // render where the URL changes, before the effect above has had a turn.
  const currentTraceReplay = traceReplay?.requestKey === traceReplayRequestKey ? traceReplay : null;

  // A real deep link: Approvals sends the requester here as `?actor=`; Sources,
  // Approvals, Ask and the system-status chip send an entry here as `?seq=`.
  // The `?seq=` link fetches the page CONTAINING that entry (`anchor_seq` on
  // the ledger read) — the most recent page cannot contain an entry older than
  // one window, which is every sync record within a day of real use. When the
  // jump cannot land, the screen says so: a silent miss is visually identical
  // to a successful navigation.
  const seqParam = params.get("seq");
  const anchorParam = useMemo(() => parseAnchorParam(seqParam), [seqParam]);
  // Applying new filters is the reader re-scoping the window: the deep link's
  // jump is done and the anchor is dropped. Clearing filters KEEPS it — that is
  // the recovery path when the anchor exists but the filters excluded it.
  const [dismissedAnchor, setDismissedAnchor] = useState<string | null>(null);
  const anchorActive = anchorParam.kind === "seq" && dismissedAnchor !== seqParam;

  const [actor, setActor] = useState(() => params.get("actor") ?? "");
  const [action, setAction] = useState(() => params.get("action") ?? "");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [filterError, setFilterError] = useState<string | null>(null);
  const timeZone = useMemo(() => viewerTimeZone(), []);
  const [windowSize, setWindowSize] = useState<WindowSize>(DEFAULT_WINDOW);
  const [applied, setApplied] = useState<AppliedFilters>(() => ({
    ...NO_FILTERS,
    actor: params.get("actor") ?? "",
    action: params.get("action") ?? "",
  }));
  /** Bumped by Re-verify: the same filters, read again, so the SERVER walks the
   * chain again and answers with a fresh verdict. */
  const [nonce, setNonce] = useState(0);

  const [classes, setClasses] = useState<ClassFilters>(DEFAULT_CLASSES);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortState>(DEFAULT_SORT);
  const [openSeq, setOpenSeq] = useState<string | null>(null);
  /** §13 render: "Scope collapsed by default, applied scope visible as
   * removable tokens." The scope FORM is a disclosure, not standing furniture —
   * the applied narrowing itself stays visible (below) whether this is open or
   * shut. */
  const [scopeOpen, setScopeOpen] = useState(false);
  /** THE VERDICT'S REACH, SHOWN ON DEMAND — see `chainScope`. Collapsed by
   * default so the strip stays the one-line status §1.4 asks for; expanded by
   * the verdict line itself, which is a real control and therefore reachable
   * with a Tab and operable with a tap. It was a `title` attribute on a
   * non-focusable span, which is a MOUSE-ONLY affordance: keyboard and touch
   * users got `● VALID  N records covered` and no way to learn that the count
   * covers a prefix this call did not re-hash. */
  const [reachShown, setReachShown] = useState(false);

  const [page, setPage] = useState<AuditEventsPage | null>(null);
  const [status, setStatus] = useState<TableStatus>("loading");
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  /**
   * THIS SCREEN'S OWN READ IS AN OBSERVATION OF THE CHAIN, NOT A PRIVATE FACT —
   * it is folded into the ONE latch, the same way /trace folds its own
   * `assemble_trace` verdict (`observe`, hooks/useAuditPoll.tsx).
   *
   * IT WAS NOT, AND THAT WAS THE SAME DEFECT FIVE OTHER SCREENS HAD: a verdict
   * DETECTED a break and the finding failed to reach something that needed it.
   * A `from`/`to` read is the sharp case, because `chain` below deliberately
   * REFUSES a time-scoped verdict as this screen's reading (a windowed walk
   * vouches for nothing outside its window) — so a windowed read that came back
   * INVALID was computed by the server, shipped to the browser, and then
   * dropped on the floor, while the shared poll went on publishing `● VALID`
   * about a chain this console had just been told was broken.
   *
   * REFUSING IT AS A VOUCHER AND ACCEPTING IT AS AN ACCUSATION IS NOT A
   * CONTRADICTION — it is `BASIS_STRENGTH` (audit/chainVerdict.ts) doing
   * exactly its job. `windowed` ranks WEAKEST, so this read can never reset a
   * stronger proof's run, and the sweep path that withdraws a standing break is
   * unreachable from here at all: this folds a PLAIN observation, never a
   * `{ sweep: true }` one. What it can do is disagree — and a disagreement it
   * is entitled to make is a disagreement the latch must count.
   *
   * (Naming the reducer's withdrawal function here would trip
   * `chainVerdict.test.ts`'s "no screen keeps a chain latch of its own" source
   * scan, which is the guardian that keeps this screen a CONSUMER of the one
   * latch rather than a second one. That is the correct outcome and the reason
   * the sentence above talks about the path instead of the symbol.)
   */
  const { observe } = poll;

  // ONE fetch path — first load, every Apply, every Re-verify — with
  // latest-request-wins. A failed read CLEARS the rows rather than leaving a
  // stale window under a fresh verdict.
  useEffect(() => {
    let live = true;
    void (async () => {
      const settled = await runLatest(() =>
        client.listAuditEvents({
          ...requestFilters(applied),
          ...(anchorActive && anchorParam.kind === "seq" ? { anchor_seq: anchorParam.seq } : {}),
        }),
      );
      if (!live || settled.kind === "superseded") return;
      if (settled.kind === "success") {
        setPage(settled.value);
        setStatus("ok");
        // Folded BEFORE the screen renders a word off the latch, and folded
        // whatever the verdict is: a passing read of a basis strong enough to
        // answer the standing run resets it, a failing one counts toward a
        // break, and a windowed one is ranked too weak to do the first.
        observe(settled.value.verification);
        return;
      }
      setPage(null);
      // A role boundary and an outage are different facts. Only a 403 is a
      // refusal; everything else is unreachable, and saying otherwise is a false
      // statement about authorization.
      setStatus(
        settled.error instanceof ApiError && settled.error.status === 403 ? "refused" : "error",
      );
    })();
    return () => {
      live = false;
    };
    // `observe` is deliberately owner-bound rather than stable (see its
    // docstring): when the principal resolves, this effect re-runs and takes the
    // read again under the session that can actually hold the observation.
  }, [client, runLatest, applied, nonce, anchorActive, anchorParam, observe]);

  const apply = useCallback(
    (event: FormEvent) => {
      event.preventDefault();
      const fromInstant = resolveLocalInstant(from, "From", timeZone);
      const toInstant = resolveLocalInstant(to, "To", timeZone);
      const invalid = [fromInstant, toInstant].find((bound) => bound.kind === "error");
      if (invalid?.kind === "error") {
        setFilterError(`${invalid.message} The loaded results were not changed.`);
        return;
      }
      const normalizedFrom = fromInstant.kind === "instant" ? fromInstant.value : "";
      const normalizedTo = toInstant.kind === "instant" ? toInstant.value : "";
      if (
        normalizedFrom !== "" &&
        normalizedTo !== "" &&
        Date.parse(normalizedFrom) > Date.parse(normalizedTo)
      ) {
        setFilterError("From must not be later than To. The loaded results were not changed.");
        return;
      }
      setFilterError(null);
      setStatus("loading");
      setOpenSeq(null);
      setDismissedAnchor(seqParam);
      setApplied({
        actor,
        action,
        from: normalizedFrom,
        to: normalizedTo,
        limit: Number(windowSize),
      });
    },
    [action, actor, from, timeZone, to, windowSize, seqParam],
  );

  /** One applied scope field, removed in place — no re-opening the form, no
   * re-typing the rest. The read re-fires because `applied` drives it. */
  const removeScopeField = useCallback(
    (key: "actor" | "action" | "from" | "to") => {
      if (key === "actor") setActor("");
      if (key === "action") setAction("");
      if (key === "from") setFrom("");
      if (key === "to") setTo("");
      setFilterError(null);
      setStatus("loading");
      setOpenSeq(null);
      setDismissedAnchor(seqParam);
      setApplied((prev) => ({ ...prev, [key]: "" }));
    },
    [seqParam],
  );

  /** Read the SAME window again. The Retry after a failed read wants exactly
   * this and nothing more. */
  const refetch = useCallback(() => {
    setStatus("loading");
    setNonce((n) => n + 1);
  }, []);

  const reverify = useCallback(() => {
    refetch();
    // The system-status chip latches a confirmed break and is deliberately not
    // cleared by an ordinary passing poll. This is the explicit re-verification
    // that can clear it, on the button that already says so.
    poll.reverify();
  }, [poll, refetch]);

  const clearFilters = useCallback(() => {
    setActor("");
    setAction("");
    setFrom("");
    setTo("");
    setWindowSize(DEFAULT_WINDOW);
    setClasses(DEFAULT_CLASSES);
    setSearch("");
    setFilterError(null);
    setStatus("loading");
    setApplied(NO_FILTERS);
  }, []);

  /* ------------------------------- the window ------------------------------ */

  const rows = page?.events ?? NO_ROWS;
  const verification = page?.verification ?? null;

  /**
   * THE WORD THIS SCREEN RESTS ON — the LATCHED verdict off the shared audit
   * poll, not this one read's raw `valid`.
   *
   * The server has a reproduced read-during-append race: roughly half of the
   * reads taken while the chain is being appended to come back
   * `{"valid": false, "reason": "head mismatch (truncated or forged tail)"}`,
   * and the console's own polling is one of the things appending. Rendering
   * that verbatim made the compliance screen accuse the deployment of a forged
   * tail and then quietly withdraw it on the next read — and once /trace was
   * routed through the latch, the two screens rendered opposite verdicts about
   * the same chain at the same instant, which is worse than either alone.
   *
   * `poll.verdict` is the same latch, over the same chain, and `chainReading`
   * is the same mapping /trace and the rail chip render through, so all three
   * agree by construction.
   *
   * The verdict AND the numbers beside it come from the same read: the poll's,
   * when it has one, and this window's own otherwise (no provider mounted, or a
   * poll that is not `ok`). Pairing them is not tidiness — "12,479 records
   * verified" taken off a read that answered INVALID is a false sentence, and
   * that is exactly the pairing a word-only swap would have produced.
   *
   * A CONFIRMED BREAK DOES NOT DEPEND ON EITHER READ. `chainReading` decides
   * BREAK from the latch before it looks at a verification at all, which is the
   * case a failed poll used to erase: the poll nulled its verification, this
   * screen fell back to its own passing window read, and the strip published
   * `● VALID` over a chain the console had confirmed broken — for the whole
   * error backoff, beside a chip reading "Audit unavailable".
   *
   * `breakSeq` is the entry the CONFIRMED break sits at, and therefore the only
   * break the table may annotate. Gating the band and the spine on the raw
   * per-read verdict drew the red "Records after this point failed
   * verification" band, the entry's BREAK pill and the dimmed tail across a
   * window whose own strip said RE-VERIFYING: the same contradiction, moved
   * inside one screen. `chainReading` derives it from the same latch as the
   * word, so the band, the spine and the strip cannot disagree.
   *
   * AND THE FALLBACK IS ONLY TAKEN WHILE THIS WINDOW'S OWN READ IS A
   * WHOLE-CHAIN READ. `verify_chain(engine, start=start, end=end)`
   * (routers/audit.py:130) is scoped by the from/to window AND NOTHING ELSE:
   * `actor`, `action`, the window size and `anchor_seq` narrow the ROWS and
   * leave the verdict over the whole chain, which is why the poll's own
   * `limit=25` read is a whole-chain verdict too and why preferring it costs
   * this screen nothing. A TIME BOUND is different — `_verify_bounded` walks
   * only the rows inside the window, so a row tampered OUTSIDE it is invisible
   * to that read by construction. So the fallback published `● VALID` — a word
   * that carries no scope of its own — off a read that had provably not looked
   * at most of the chain, beside a count of that window's rows.
   * The poll is unscoped and answers this whenever it can; when it cannot, a
   * time-scoped read is NOT a whole-chain reading and is not accepted as one.
   * The latch still speaks (rules 1 and 2 consult no read at all), so a
   * confirmed break is stated here either way — what is withheld is the
   * vouching, which is rule 4: a strip must not vouch for a chain nothing has
   * checked.
   */
  const windowScoped = applied.from.trim() !== "" || applied.to.trim() !== "";
  const chain = poll.verification ?? (windowScoped ? null : verification);
  const { word, breakSeq } = chainReading(poll.verdict, chain, poll.observedBreaks);
  const verdictLine = word === null ? null : chainLine(word, chain, breakSeq);
  /** The reach of the verdict the line states, in the server's words. */
  const verdictScope = word === null ? null : chainScope(word, chain);
  /** A break STANDS over this ledger — the confirmed WORD, which is a fact
   * about the chain, not about whether anything named an entry on it. The two
   * are separate inputs to the table for one reason: `breakSeq === null` is
   * both "no break" and "a break nobody located", and the second one may not be
   * drawn as the first (spine.ts). */
  const broken = word === "BREAK";

  const columns = useMemo(
    () => ledgerColumns(breakSeq, broken, sort, (id) => directory.lookup(id)?.name),
    [breakSeq, broken, sort, directory],
  );
  const filtered = useMemo(
    () => filterRows(applyClassFilters(rows, classes), columns, search),
    [rows, classes, columns, search],
  );
  /** Exactly the rows the table renders, in the order it renders them. */
  const windowRows = useMemo(() => sortRows(filtered, columns, sort), [filtered, columns, sort]);

  const refusalCount = useMemo(() => countRefusals(rows), [rows]);
  const toolCount = useMemo(() => countToolCalls(rows), [rows]);

  /** The applied scope, as removable tokens — visible whether the form is open
   * or collapsed. A narrowed window can never masquerade as the whole ledger. */
  const scopeTokens = useMemo(() => {
    const tokens: Array<{ key: "actor" | "action" | "from" | "to"; label: string }> = [];
    if (applied.actor.trim() !== "")
      tokens.push({ key: "actor", label: `Actor: ${applied.actor.trim()}` });
    if (applied.action.trim() !== "")
      tokens.push({ key: "action", label: `Action: ${applied.action.trim()}` });
    if (applied.from.trim() !== "")
      tokens.push({ key: "from", label: `From: ${absoluteTime(applied.from)}` });
    if (applied.to.trim() !== "")
      tokens.push({ key: "to", label: `To: ${absoluteTime(applied.to)}` });
    return tokens;
  }, [applied]);

  const deployment = config?.deployment_name ?? window.location.host;

  const exportCsv = useCallback(async () => {
    if (windowRows.length === 0 || exporting) return;
    setExporting(true);
    setExportError(null);
    try {
      // The server owns the chain verdict, truncation/completeness markers and
      // formula-safe bytes. Rebuilding this Blob here would silently discard
      // exactly the evidence the export exists to preserve.
      const blob = await client.exportAuditCsv(requestFilters(applied));
      downloadCsv(exportFilename(deployment, applied, windowRows), blob);
    } catch (error: unknown) {
      setExportError(error instanceof ApiError ? error.userMessage : null);
    } finally {
      setExporting(false);
    }
  }, [applied, client, deployment, exporting, windowRows]);

  /* ------------------------- the ?seq= deep link --------------------------- */

  const tableRef = useRef<HTMLDivElement | null>(null);
  const scrolledTo = useRef<string | null>(null);
  const selectedKey =
    openSeq ?? (anchorActive && anchorParam.kind === "seq" ? String(anchorParam.seq) : null);

  useEffect(() => {
    if (!anchorActive || status !== "ok") return;
    if (scrolledTo.current === seqParam) return;
    const row = tableRef.current?.querySelector(".ne-tr--selected");
    if (!(row instanceof HTMLElement)) return;
    scrolledTo.current = seqParam;
    // Optional call: the highlight is the load-bearing half of the deep link and
    // must still land where scrolling is not implemented.
    row.scrollIntoView?.({ block: "center" });
  }, [anchorActive, seqParam, status, windowRows]);

  /** The SERVER's anchor verdict, verbatim (C7) — never a row-count inference. */
  const anchorVerdict = anchorActive && status === "ok" ? (page?.anchor ?? null) : null;
  const anchorFetched =
    anchorParam.kind === "seq" && rows.some((row) => row.seq === anchorParam.seq);
  const anchorVisible =
    anchorParam.kind === "seq" && filtered.some((row) => row.seq === anchorParam.seq);
  /** The loud-failure half: any `?seq=` navigation that is not sitting
   * highlighted on its entry states which entry failed and why. */
  const notice =
    status === "ok" && anchorParam.kind !== "none" && dismissedAnchor !== seqParam
      ? anchorNotice(anchorParam, anchorVerdict, anchorFetched, anchorVisible)
      : null;

  /** Un-hide an anchor the view filters cover: every class chip on, search
   * cleared. Client-side narrowing only — the served window is untouched. */
  const revealAnchor = useCallback(() => {
    setClasses({ decisions: true, toolCalls: true, refusals: true });
    setSearch("");
  }, []);

  const openRecord =
    openSeq === null ? null : (rows.find((row) => String(row.seq) === openSeq) ?? null);

  const showToolbar = status !== "refused";

  return (
    <div className="ne-audit">
      <div className="ne-screen">
        {/* §11 one-header-standard: the shared PageHeader is the single header
            path. The verdict cluster rides the title row's in-main slot; the
            §13 primary action (Export CSV) rides the right-aligned action slot,
            withheld exactly when the toolbar is. No eyebrow, no lede. */}
        <PageHeader
          title="Audit log"
          tabs={
            // THE WORD, NOT THE READ, DECIDES WHETHER THIS RENDERS. A confirmed
            // break is decided from the latch before any read is consulted
            // (chainVerdict.ts rule 1), so gating the whole cluster on a read
            // in hand would let an absent — or a merely window-scoped — one
            // take a standing BREAK off the screen along with its Re-verify.
            status === "ok" && word !== null ? (
              <div className="ne-audit__verdict">
                <StatusPill word={word} />
                {verdictLine === null ? null : verdictScope === null ? (
                  // NO SENTENCE, NO CONTROL. A button that reveals nothing is a
                  // promise the server did not make — under BREAK, under
                  // RE-VERIFYING and whenever `detail` came back empty, the
                  // count stands on its own exactly as before.
                  <span className="ne-audit__verdictline">{verdictLine}</span>
                ) : (
                  // THE LINE IS THE DISCLOSURE'S BUTTON. Its accessible NAME is
                  // the claim ("12,480 records covered"); its accessible
                  // DESCRIPTION is the server's scope sentence, verbatim, read
                  // out of the paragraph below the toolbar — so focus alone
                  // announces the caveat, and Enter/Space/tap puts it on the
                  // screen for everyone else.
                  <button
                    type="button"
                    className="ne-audit__verdictline ne-audit__verdictline--scoped"
                    aria-expanded={reachShown}
                    aria-controls={VERDICT_REACH_ID}
                    aria-describedby={VERDICT_REACH_ID}
                    onClick={() => setReachShown((shown) => !shown)}
                  >
                    {verdictLine}
                  </button>
                )}
                <ToolbarButton label="Re-verify" onClick={reverify} />
              </div>
            ) : undefined
          }
          action={
            showToolbar ? (
              <button
                type="button"
                className="ne-btn"
                onClick={() => void exportCsv()}
                disabled={status !== "ok" || windowRows.length === 0 || exporting}
              >
                Export CSV
              </button>
            ) : undefined
          }
        />

        <div className="ne-screen__body">
          {showToolbar ? (
            <>
              <Toolbar
                search={{ value: search, onChange: setSearch, placeholder: "Filter records…" }}
                filters={
                  <>
                    <ToggleChip
                      label="Decisions"
                      pressed={classes.decisions}
                      onChange={(on) => setClasses((prev) => ({ ...prev, decisions: on }))}
                    />
                    <ToggleChip
                      label={`Tool calls${status === "ok" ? ` (${formatCount(toolCount)})` : ""}`}
                      pressed={classes.toolCalls}
                      onChange={(on) => setClasses((prev) => ({ ...prev, toolCalls: on }))}
                    />
                    {/* The chip carries its count only when the count was READ. A
                        number beside a chip on a window nobody was shown has no
                        source, on the one screen whose whole argument is that
                        every number has one. */}
                    <ToggleChip
                      label={`Access refusals${status === "ok" ? ` (${formatCount(refusalCount)})` : ""}`}
                      pressed={classes.refusals}
                      onChange={(on) => setClasses((prev) => ({ ...prev, refusals: on }))}
                    />
                    {/* §13 render: "Scope collapsed by default, applied scope
                        visible as removable tokens" — a narrowed window can
                        never masquerade as the whole ledger. Each token clears
                        its own field in place; none of them re-open the form. */}
                    {scopeTokens.map((token) => (
                      <FilterChip
                        key={token.key}
                        label={token.label}
                        onDismiss={() => removeScopeField(token.key)}
                      />
                    ))}
                    <ToolbarButton
                      label={scopeOpen ? "Scope ▴" : "Scope ▾"}
                      onClick={() => setScopeOpen((open) => !open)}
                    />
                  </>
                }
                count={status === "ok" ? showingText(windowRows.length, rows.length) : null}
              />
              {/* The scope form itself: a disclosure, opened on demand — never
                  standing furniture over the ledger. */}
              {scopeOpen ? (
                <form className="ne-audit__filters" onSubmit={apply}>
                  <label className="ne-audit__field">
                    <span className="ne-audit__fieldlabel">Actor</span>
                    <input
                      type="text"
                      className="ne-audit__input"
                      value={actor}
                      onChange={(event) => setActor(event.target.value)}
                    />
                  </label>
                  <label className="ne-audit__field">
                    <span className="ne-audit__fieldlabel">Action</span>
                    <input
                      type="text"
                      className="ne-audit__input"
                      value={action}
                      onChange={(event) => setAction(event.target.value)}
                    />
                  </label>
                  <label className="ne-audit__field">
                    <span className="ne-audit__fieldlabel">From</span>
                    <input
                      type="datetime-local"
                      className="ne-audit__input"
                      value={from}
                      aria-describedby="audit-timezone-context"
                      onChange={(event) => {
                        setFrom(event.target.value);
                        setFilterError(null);
                      }}
                    />
                  </label>
                  <label className="ne-audit__field">
                    <span className="ne-audit__fieldlabel">To</span>
                    <input
                      type="datetime-local"
                      className="ne-audit__input"
                      value={to}
                      aria-describedby="audit-timezone-context"
                      onChange={(event) => {
                        setTo(event.target.value);
                        setFilterError(null);
                      }}
                    />
                  </label>
                  <label className="ne-audit__field">
                    <span className="ne-audit__fieldlabel">Window</span>
                    <select
                      className="ne-filter__select"
                      value={windowSize}
                      onChange={(event) => setWindowSize(event.target.value as WindowSize)}
                    >
                      {WINDOW_SIZES.map((size) => (
                        <option key={size.value} value={size.value}>
                          {size.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button type="submit" className="ne-btn ne-btn--quiet">
                    Apply
                  </button>
                  <span
                    id="audit-timezone-context"
                    className={`ne-audit__timecontext${filterError === null ? "" : " ne-audit__timecontext--error"}`}
                    role={filterError === null ? undefined : "alert"}
                  >
                    {filterError ??
                      `Times use ${timeZone}. Applied bounds are sent as unambiguous UTC instants${
                        applied.from || applied.to
                          ? ` (${applied.from || "open"} to ${applied.to || "open"}).`
                          : "."
                      }`}
                  </span>
                </form>
              ) : null}
            </>
          ) : null}

          {/* THE VERDICT'S REACH, IN THE SERVER'S OWN WORDS — never summarised,
              never reassembled here. §1.4 forbids prose in the title row and
              between title and toolbar; BELOW the toolbar is where this screen
              already puts its sentences (the break band, the anchor notice), so
              that is where this one goes.
              ALWAYS IN THE DOM WHEN THERE IS A SENTENCE TO STATE, `hidden` when
              collapsed. `aria-describedby` on a hidden element is included in
              the accessible description by construction (accname), which is the
              whole point: the caveat reaches a screen reader on focus, before
              anyone decides whether to press.
              IT CANNOT COLLIDE WITH THE BREAK BAND. `chainScope` answers only
              under VALID, so a screen showing this paragraph is a screen with no
              band, and the band's red is never competed with. */}
          {verdictScope === null ? null : (
            <p id={VERDICT_REACH_ID} className="ne-audit__reach" hidden={!reachShown}>
              {verdictScope}
            </p>
          )}

          {exportError !== null ? <SaveError detail={exportError} /> : null}

          {/* THE BAND RIDES THE WORD, NOT THE ENTRY. Gating it on
              `breakSeq !== null` would let a confirmed break the server never
              located take the ONLY `role="alert"` off this screen while the
              strip still read BREAK. A break that cannot be pointed at is
              still a break, and it is announced in the words the console can
              honestly use. THE SECOND SENTENCE RENDERS: a CONTRADICTION is a
              `valid: false` whose own walk passed, so it names no entry, and
              it latches on sight. The band is gated on the WORD either way,
              which is what makes that reachable rather than a regression. */}
          {broken ? (
            <p className="ne-audit__band" role="alert">
              {breakSeq === null
                ? "Chain break confirmed. The server did not name the entry, so no record on this page can be shown as verified."
                : breakBandText(breakSeq)}
            </p>
          ) : null}

          {notice !== null ? (
            // The deep link's loud failure: a `?seq=` jump that did not land
            // highlighted says which entry failed and why. A silent miss renders
            // exactly like a successful navigation, which is the harm.
            <p className="ne-audit__anchornote" role="alert">
              <span>{notice.text}</span>
              {notice.action === "clear-filters" ? (
                <ToolbarButton label="Clear filters" onClick={clearFilters} />
              ) : null}
              {notice.action === "show-entry" ? (
                <ToolbarButton label="Show entry" onClick={revealAnchor} />
              ) : null}
            </p>
          ) : null}

          {traceId === null ? null : (
            <section className="ne-audit__replay" aria-label={`Replay for trace ${traceId}`}>
              <h2>Replay</h2>
              <p className="ne-mono">{traceId}</p>
              {currentTraceReplay === null ? <Loading things="the replay" /> : null}
              {currentTraceReplay?.status === "refused" ? (
                <Refused thing="replaying this trace" page="Replay" />
              ) : null}
              {currentTraceReplay?.status === "error" ? (
                <LoadError
                  things="the replay"
                  onRetry={() => setTraceReplayNonce((value) => value + 1)}
                />
              ) : null}
              {currentTraceReplay?.status === "ok" ? (
                <>
                  <h3>Events</h3>
                  {currentTraceReplay.replay.events.length === 0 ? (
                    <p>No records behind this trace.</p>
                  ) : (
                    <ol aria-label="Replayed events">
                      {currentTraceReplay.replay.events.map((event) => (
                        <li key={event.event_id}>
                          <span>{event.seq}</span> <span className="ne-mono">{event.event_id}</span>{" "}
                          {/* The reading, with the code on hover — same rule as
                              the table above it. The event id beside it is the
                              identifier this list is actually navigated by. */}
                          <span title={eventActionCode(event)}>{eventActionLabel(event)}</span>
                        </li>
                      ))}
                    </ol>
                  )}
                  <h3>Config hashes</h3>
                  {currentTraceReplay.replay.config_hashes.length === 0 ? (
                    <p>—</p>
                  ) : (
                    <ol aria-label="Replay config hashes">
                      {currentTraceReplay.replay.config_hashes.map((hash, index) => (
                        <li key={`${index}:${hash}`}>
                          <HashChip hash={hash} label="Copy configuration hash" />
                        </li>
                      ))}
                    </ol>
                  )}
                  <h3>Context IDs</h3>
                  {currentTraceReplay.replay.context_ids.length === 0 ? (
                    <p>—</p>
                  ) : (
                    <ol className="ne-mono" aria-label="Replay context IDs">
                      {currentTraceReplay.replay.context_ids.map((contextId, index) => (
                        <li key={`${index}:${contextId}`}>{contextId}</li>
                      ))}
                    </ol>
                  )}
                </>
              ) : null}
            </section>
          )}

          <div ref={tableRef}>
            <DataTable<AuditRecordView>
              className="ne-audit__ledger"
              columns={columns}
              rows={filtered}
              rowKey={(row) => String(row.seq)}
              label="Audit log"
              things="the audit log"
              sort={sort}
              onSortChange={setSort}
              status={status}
              onRowActivate={(row) => setOpenSeq(String(row.seq))}
              selectedKey={selectedKey}
              empty={
                <Empty
                  message="No records match these filters."
                  action={<ToolbarButton label="Clear filters" onClick={clearFilters} />}
                />
              }
              refused={<Refused thing="the audit log" page="Audit log" />}
              error={<LoadError things="the audit log" onRetry={refetch} />}
              note={
                status === "ok" && rows.length > 0
                  ? anchorVerdict !== null
                    ? anchorPageNote(
                        rows.length,
                        anchorVerdict.seq,
                        anchorVerdict.status === "found",
                      )
                    : pageNote(rows.length)
                  : null
              }
            />
          </div>
        </div>
      </div>

      {openRecord === null ? null : (
        <RecordPane record={openRecord} client={client} onClose={() => setOpenSeq(null)} />
      )}
    </div>
  );
}

/* ============================== the columns ============================== */

/**
 * §13's seven columns.
 *
 * `after` dims the records that sit past a break. It wraps only PRESENT content:
 * an absent field must still render the table's own em dash, not an empty
 * wrapper that reads as a rendering bug.
 *
 * The Seq column also carries §5's ledger spine in its gutter. `sort` reaches it
 * for one reason: a chain may only be drawn down rows that are IN chain order
 * (spine.ts), and this is where the rendered order is known. `broken` reaches it
 * for another: a standing break the server never LOCATED has no entry to pill,
 * no tail to dim and no gap to cut, and drawing the whole-chain hairline for it
 * would vouch for every row on a ledger the verdict has just condemned.
 */
function ledgerColumns(
  breakSeq: number | null,
  broken: boolean,
  sort: SortState | null,
  nameOf: (id: string) => string | undefined,
): ReadonlyArray<Column<AuditRecordView>> {
  const isBreak = (row: AuditRecordView) => breakSeq !== null && row.seq === breakSeq;
  const after = (row: AuditRecordView) => breakSeq !== null && row.seq > breakSeq;
  const dim = (row: AuditRecordView, node: ReactNode) => {
    if (node === null || node === undefined || node === "") return node;
    return after(row) ? <span className="ne-audit__after">{node}</span> : node;
  };

  return [
    {
      key: "seq",
      header: "Seq",
      numeric: true,
      // 132px, not 112px: the gutter the spine occupies is the column's own,
      // and the BREAK pill beside a four-figure entry already filled the old
      // width. The spine takes 12px of chrome, never 12px of the number.
      width: "132px",
      cell: (row) => (
        <>
          <LedgerSpine mark={ledgerSpine(row.seq, breakSeq, sort, broken)} />
          <span className="ne-audit__seq">
            {isBreak(row) ? <StatusPill word="BREAK" /> : null}
            {dim(row, row.seq)}
          </span>
        </>
      ),
      sortValue: (row) => row.seq,
      firstSort: "desc",
      searchText: (row) => String(row.seq),
    },
    {
      key: "time",
      header: "Time",
      numeric: true,
      width: "104px",
      cell: (row) =>
        dim(
          row,
          <time dateTime={row.occurred_at} title={absoluteTime(row.occurred_at)}>
            {relativeTime(row.occurred_at)}
          </time>,
        ),
      sortValue: (row) => Date.parse(row.occurred_at),
      firstSort: "desc",
    },
    {
      key: "actor",
      header: "Actor",
      width: "200px",
      cell: (row) =>
        dim(
          row,
          <PrincipalLabel id={row.actor} reference={{ scope: "audit", reference: row.event_id }} />,
        ),
      sortValue: (row) => row.actor,
      searchText: (row) => `${row.actor} ${nameOf(row.actor) ?? ""}`,
    },
    {
      key: "action",
      header: "Action",
      // Prose, so not `mono` — that type is for identifiers, and the identifier
      // moved to the `title` (and stays spelled out in the record pane). The
      // column widens because a phrase is longer than a code.
      width: "232px",
      cell: (row) => dim(row, <span title={eventActionCode(row)}>{eventActionLabel(row)}</span>),
      // Sort and search still run on the CODE as well as the label: the code is
      // what an incident is handed ("find me the tool.toolset_denied rows") and
      // grouping by it keeps every member of a family adjacent, which sorting by
      // the reading would not.
      sortValue: (row) => eventActionCode(row),
      searchText: (row) => `${eventActionCode(row)} ${eventActionLabel(row)} ${row.event_type}`,
    },
    {
      key: "resource",
      header: "Resource",
      mono: true,
      width: "200px",
      // Not sortable: a middle-truncated identifier orders by a prefix nobody
      // reads, and the column exists to be recognised, not ranked.
      cell: (row) =>
        row.resource === null
          ? null
          : dim(row, <span title={row.resource}>{truncateMiddle(row.resource)}</span>),
      searchText: (row) => row.resource ?? "",
    },
    {
      key: "outcome",
      header: "Outcome",
      width: "112px",
      cell: (row) => dim(row, row.outcome),
      sortValue: (row) => row.outcome,
      searchText: (row) => row.outcome ?? "",
    },
    {
      key: "trace",
      header: "Trace",
      width: "72px",
      // Not sortable: a column of one repeated link word has no order.
      cell: (row) =>
        row.trace_id === null ? null : (
          <Link
            to={`/trace/${row.trace_id}`}
            // The row's own click opens the pane; this link leaves for the run.
            onClick={(event) => event.stopPropagation()}
          >
            View
          </Link>
        ),
      searchText: (row) => row.trace_id ?? "",
    },
  ];
}
