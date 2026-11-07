// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §14 — POSTURE, recomposed (CR-B8, screens/posture.html). "Is the deployment
// configured the way we claim — and on what class of evidence."
//
// THE ANSWER LEADS. A claim strip of three served facts — Controls, Egress
// boundary, Configuration — answers the screen's whole question in zero
// clicks, before any table: exactly the second-week compliance reviewer's
// question. Below it the twelve constraints become a true annunciator panel,
// the one screen where that vocabulary names something real: id, name, state,
// on a hairline 4x3 grid, panel-quiet at rest. A tile click fills a docked pane
// with the constraint's evidence class, build result, mode and the server's own
// enforcement sentence, rendered as sent — the grid never moves.
//
// THE VOCABULARY IS THE CRUX (NOTES.md, "the prior defect, designed around"):
// a role-scoped refusal once drew three fault-coloured blocks on a reader's
// home screen for a boundary that was working exactly as configured. Here:
// ● PASSED is green text with no lit block; ⚠ FAILED is the page's ONLY fault
// colour, tile-edge-marked as well as worded; ⊟ MANUAL is gated-neutral —
// unverified is not broken; a refusal renders GATED, never red, and alone —
// no strip, no tiles, no count; OPEN egress renders NEUTRAL with the sanctioned
// hosts enumerated, never an alarm. Nothing here "improves" that vocabulary.
//
// Evidence classes are named where they apply, one sentence each: BUILD-TIME
// evidence is not a live runtime health check; the network section is a DIRECT
// RUNTIME READ, not a liveness probe; configuration is an AUDIT-BACKED read.
// Freshness comes only from the two server-authored observation instants; the
// browser never turns fetch completion into a fake age, and a failed re-read
// clears the previously successful payload rather than leaving it looking
// current.
//
// THIN CLIENT (C7). Every value below is a field of the one governed posture
// read. The console verifies nothing, compares no hash, and decides no
// permission: a refusal renders the SERVER's refusal, and a read that never
// arrived says so in its own words rather than claiming a role boundary that
// was never stated. The enforcement strings render VERBATIM — a console that
// rewrites the server's account of what is enforced is a console that can be
// wrong about it (the same reasoning covers `short`, a server-authored label).
//
// WHAT WAS REMOVED, AND WHY (NOTES.md §Posture): the controls TABLE as the
// primary surface — twelve sortable rows spent the reader's first screenful
// re-proving what one glance at an annunciator states, so sorting leaves with
// it; the filter and count survive in the card head, the constraint's own
// enforcement line is one click away in the pane. The model-lanes table stays
// gone (it belongs to Models); the link row that says where it went is kept.

import { useCallback, useEffect, useMemo, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import { Link } from "react-router";
import type { ConfigHashSpanView, PostureConstraintView, PostureView } from "../api/types";
import { useApiClient } from "../auth/useApiClient";
// The four-state governed read (loading · ok · refused · error). Reused rather
// than re-implemented: refused and error are different facts and exactly one
// state machine in this console decides which is which. It also carries the
// reload the §2.4 error state's Retry button needs.
import { useGovernedRead } from "../landing/useGovernedRead";
import type { AnnunciatorState } from "../primitives/AnnunciatorRow";
import { AnnunciatorRow } from "../primitives/AnnunciatorRow";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { HashChip } from "../primitives/HashChip";
import { ListScreen } from "../primitives/PageHeader";
import { StatePill } from "../primitives/StatePill";
import type { StateWord } from "../primitives/StatePill";
import { Empty, LoadError, Refused } from "../primitives/states";
import { absoluteTime, formatCount, relativeTime, truncateHash } from "../primitives/format";
import { compareValues, filterRows } from "../primitives/tableSort";
import type { SortableColumn, SortState } from "../primitives/tableSort";
import { egressReason, egressState } from "./egress";
import { spanInForce } from "./lineage";
import "./posture.css";

/** §14: "sorted From desc" — the span in force is the newest and sits on top. */
const HISTORY_SORT: SortState = { key: "from", dir: "desc" };

/** The allowed-host list has one column and §14 names no sort for it; A–Z is the
 * only order a reader can scan a host list in. */
const HOST_SORT: SortState = { key: "host", dir: "asc" };

/** Match the serving-status freshness discipline: refresh the server reading
 * independently of the five-second display clock. A cached response keeps its
 * original server stamp, so each tick makes its age more—not less—visible. */
const RELOAD_MS = 30_000;
const TICK_MS = 5_000;

function ObservationAge({ iso, now }: { iso: string | undefined; now: number }) {
  if (iso === undefined || Number.isNaN(Date.parse(iso))) {
    return <span>Observation time unavailable; freshness unknown.</span>;
  }
  return (
    <span>
      Observed{" "}
      <time dateTime={iso} title={absoluteTime(iso)}>
        {relativeTime(iso, now)}
      </time>
      .
    </span>
  );
}

/** The claim strip's own phrasing of the same served instant — joined into one
 * sentence with a neighbouring fact, so it carries no trailing full stop. */
function ClaimObservedAge({ iso, now }: { iso: string | undefined; now: number }) {
  if (iso === undefined || Number.isNaN(Date.parse(iso))) {
    return <span>Observed — freshness unknown</span>;
  }
  return (
    <span>
      Observed{" "}
      <time dateTime={iso} title={absoluteTime(iso)}>
        {relativeTime(iso, now)}
      </time>
    </span>
  );
}

/** The human form §1.3 renders for the same fact, carrying the year — this page
 * is the one a reviewer reads a multi-year history on. Derived from nothing but
 * the served `from` instant. */
function shortDate(iso: string): string | null {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

// ===========================================================================
// The card — this recomposition's own section wrapper (posture.css carries
// its own copy of `.ne-card`/`.ne-cardhead`, matching CR-B5's landing.css).
// ===========================================================================

function Card({
  headingId,
  heading,
  count,
  link,
  children,
}: {
  headingId: string;
  heading: string;
  count?: ReactNode;
  link?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="ne-card ne-posturesection" aria-labelledby={headingId}>
      <div className="ne-cardhead">
        <h2 className="ne-cardhead__title" id={headingId}>
          {heading}
        </h2>
        {count === undefined ? null : <output className="ne-cardhead__count">{count}</output>}
        {link === undefined ? null : (
          <>
            <span className="ne-cardhead__spacer" />
            {link}
          </>
        )}
      </div>
      {children}
    </section>
  );
}

// ===========================================================================
// 0 — The claim strip
// ===========================================================================

/** The Controls claim's pill: PASSED when every static guardian in the served
 * window passed, or the fault-coloured failure count otherwise — the one place
 * on the strip the fault colour may appear, and only when a build guardian
 * actually failed (never for an unverified MANUAL control). */
function ControlsClaimPill({ failed }: { failed: number }) {
  if (failed === 0) {
    return (
      <span className="ne-pill ne-pill--success">
        <span className="ne-pill__glyph" aria-hidden="true">
          ●
        </span>
        Passed
      </span>
    );
  }
  return (
    <span className="ne-pill ne-pill--refuse">
      <span className="ne-pill__glyph" aria-hidden="true">
        ⚠
      </span>
      {formatCount(failed)} failed
    </span>
  );
}

/** The egress claim's value. SEALED earns the one affirm colour on this strip;
 * OPEN carries no colour override — the hard rule this screen exists to keep.
 * A sanctioned allowlist is a configured state, never an alarm, so it is drawn
 * with the SAME weight as SEALED and no fault-adjacent tint at all. */
function EgressClaimValue({ state }: { state: StateWord | undefined }) {
  if (state === undefined) return <span className="ne-absent">—</span>;
  if (state === "SEALED") {
    return (
      <span className="ne-egressvalue ne-egressvalue--sealed">
        <span aria-hidden="true">●</span> Sealed
      </span>
    );
  }
  return (
    <span className="ne-egressvalue">
      <span aria-hidden="true">◑</span> Open
    </span>
  );
}

function ClaimStrip({ posture, now }: { posture: PostureView; now: number }) {
  const total = posture.constraints.length;
  const staticPassed = posture.constraints.filter(
    (row) => row.mode === "static" && row.attested,
  ).length;
  const staticFailed = posture.constraints.filter(
    (row) => row.mode === "static" && !row.attested,
  ).length;

  const egress = egressState(posture);
  const reason = egressReason(posture);

  const inForce = spanInForce(posture.config_hash_history);
  const since = inForce === null ? null : shortDate(inForce.from);
  const configHash = inForce?.hash ?? posture.config_hash;

  return (
    <div className="ne-claims">
      <section className="ne-claim" aria-label="Controls">
        <span className="ne-claim__label">Controls</span>
        <span className="ne-claim__value">
          {formatCount(staticPassed)} of {formatCount(total)} attested{" "}
          <ControlsClaimPill failed={staticFailed} />
        </span>
        <span className="ne-claim__sub">Build-time guardians · this build</span>
      </section>
      <section className="ne-claim" aria-label="Egress boundary">
        <span className="ne-claim__label">Egress boundary</span>
        <span className="ne-claim__value">
          <EgressClaimValue state={egress} />
        </span>
        <span className="ne-claim__sub">
          {reason === null ? null : <>{reason} · </>}
          <ClaimObservedAge iso={posture.network_observed_at} now={now} />
        </span>
      </section>
      <section className="ne-claim" aria-label="Configuration">
        <span className="ne-claim__label">Configuration</span>
        <span className="ne-claim__value">
          <HashChip
            hash={configHash}
            label={`Copy configuration hash ${truncateHash(configHash)}`}
          />
        </span>
        <span className="ne-claim__sub">
          {inForce !== null && since !== null ? (
            <>
              In force since{" "}
              <time dateTime={inForce.from} title={absoluteTime(inForce.from)}>
                {since}
              </time>{" "}
              ·{" "}
            </>
          ) : null}
          <ClaimObservedAge iso={posture.configuration_observed_at} now={now} />
        </span>
      </section>
    </div>
  );
}

// ===========================================================================
// 1 — Controls: the annunciator + docked constraint pane
// ===========================================================================

interface EvidencePresentation {
  classLabel: string;
  state: AnnunciatorState;
}

/** Interpret only the evidence fields the server supplies. This is presentation,
 * not a second constraint truth table: no control id is special-cased here. */
function evidencePresentation(row: PostureConstraintView): EvidencePresentation {
  if (row.mode === "static") {
    return {
      classLabel: "BUILD-TIME · STATIC",
      state: row.attested ? "PASSED" : "FAILED",
    };
  }
  return { classLabel: row.mode.toUpperCase(), state: "MANUAL" };
}

/** Search-within text, by field — the tile grid carries no sortable columns
 * (§14 removed sorting as a feature), but the filter-and-count pair survives. */
const CONTROL_SEARCH_COLUMNS: ReadonlyArray<SortableColumn<PostureConstraintView>> = [
  { key: "id", searchText: (row) => row.id },
  { key: "short", searchText: (row) => row.short },
  { key: "enforcement", searchText: (row) => row.enforcement },
  {
    key: "evidence",
    searchText: (row) => {
      const evidence = evidencePresentation(row);
      return `${evidence.classLabel} ${evidence.state}`;
    },
  },
];

/** The grid's own reading order — id-ascending, regardless of serve order or
 * the (removed) sort feature. Reuses `<DataTable>`'s own numeric-aware
 * comparator (`compareValues`) rather than a second, drifting one. */
function orderedByIdAscending(
  rows: readonly PostureConstraintView[],
): readonly PostureConstraintView[] {
  return [...rows].sort((a, b) => compareValues(a.id, b.id));
}

function ConstraintRecord({
  constraint,
  onClose,
}: {
  constraint: PostureConstraintView;
  onClose: () => void;
}) {
  const evidence = evidencePresentation(constraint);

  useEffect(() => {
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <aside className="ne-record" aria-label={`${constraint.id} ${constraint.short}`}>
      <div className="ne-record__head">
        <div>
          <h2 className="ne-record__title">{constraint.short}</h2>
          <p className="ne-record__subtitle">{constraint.id}</p>
        </div>
        <button type="button" className="ne-record__close" aria-label="Close" onClick={onClose}>
          <span aria-hidden="true">×</span>
        </button>
      </div>
      <div className="ne-record__body">
        <dl className="ne-fields">
          <dt>Evidence class</dt>
          <dd>{evidence.classLabel}</dd>
          <dt>Build result</dt>
          <dd>
            <StatePill word={evidence.state} />
          </dd>
          <dt>Mode</dt>
          <dd className="ne-mono">{constraint.mode}</dd>
          <dt>Enforcement</dt>
          <dd>{constraint.enforcement}</dd>
        </dl>
        {/* The wire's full `evidence` array (source-file paths) is deliberately
            not repeated here; its provenance is stated once in the card head
            from `attestation_source`. */}
        <p className="ne-evclass" style={{ margin: 0 }}>
          The enforcement line is the server&rsquo;s own account of what is enforced, rendered as
          sent.
        </p>
      </div>
    </aside>
  );
}

function ControlsSection({
  constraints,
  attestationSource,
  status,
  onRetry,
}: {
  constraints: readonly PostureConstraintView[];
  attestationSource: string;
  status: TableStatus;
  onRetry: () => void;
}) {
  const [search, setSearch] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);

  const filtered = useMemo(
    () => orderedByIdAscending(filterRows(constraints, CONTROL_SEARCH_COLUMNS, search)),
    [constraints, search],
  );
  const guardianPasses = useMemo(
    () => filtered.filter((row) => row.mode === "static" && row.attested).length,
    [filtered],
  );
  const attestationNoun =
    filtered.length === 1 ? "constraint attestation" : "constraint attestations";
  const guardianResult =
    guardianPasses === 1 ? "build-time guardian passed" : "build-time guardians passed";
  const open = openId === null ? null : (constraints.find((row) => row.id === openId) ?? null);

  const onSearchKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLInputElement>) => {
      if (event.key === "Escape" && search !== "") {
        event.stopPropagation();
        setSearch("");
      }
    },
    [search],
  );

  return (
    <Card
      headingId="ne-posture-controls"
      heading="Build-time attestations"
      count={
        status === "ok"
          ? `${formatCount(filtered.length)} ${attestationNoun} · ${formatCount(guardianPasses)} ${guardianResult}`
          : ""
      }
    >
      <div className="ne-toolbar">
        <input
          type="search"
          className="ne-searchwithin__input"
          value={search}
          placeholder="Filter controls…"
          aria-label="Filter controls…"
          onChange={(event) => setSearch(event.target.value)}
          onKeyDown={onSearchKeyDown}
        />
        <div className="ne-toolbar__spacer" />
        <p className="ne-evclass" style={{ margin: 0 }}>
          <strong>BUILD-TIME evidence.</strong> A pass means this build includes a mechanical
          guardian for the control and that guardian passed. It is not a live runtime health check.
        </p>
      </div>
      {/* The render's own mock does not depict this line; it is kept because it
          is real, served provenance (§14) rather than something the mock's
          silence tells this build to drop — an audit reviewer's next question
          after the annunciator is always "attested by what". */}
      {status === "ok" ? (
        <p className="ne-posturesection__lede ne-evclass">
          <strong>Attestation source:</strong>{" "}
          <span>{attestationSource || "Not supplied by the server."}</span>
        </p>
      ) : null}
      <div className={`ne-split ${open === null ? "ne-split--nopane" : ""}`.trim()}>
        <div className="ne-split__table">
          {status === "loading" ? (
            <div className="ne-annunciator">
              {Array.from({ length: 3 }, (_, index) => (
                <span className="ne-ann" key={index} aria-hidden="true">
                  <span className="ne-skeleton" style={{ width: "24px" }} />
                  <span className="ne-skeleton" style={{ width: "80%" }} />
                  <span className="ne-skeleton" style={{ width: "48px" }} />
                </span>
              ))}
              <span className="ne-visually-hidden" role="status">
                Loading build-time attestations
              </span>
            </div>
          ) : status === "error" ? (
            <LoadError things="posture" onRetry={onRetry} />
          ) : filtered.length === 0 ? (
            <Empty
              message={
                search.trim() === "" ? "No controls reported." : "No controls match this filter."
              }
            />
          ) : (
            <div className="ne-annunciator" role="listbox" aria-label="Constraint attestations">
              {filtered.map((row) => (
                <AnnunciatorRow
                  key={row.id}
                  constraint={row}
                  state={evidencePresentation(row).state}
                  selected={openId === row.id}
                  onSelect={() => setOpenId(row.id)}
                />
              ))}
            </div>
          )}
        </div>
        {open === null ? null : (
          <ConstraintRecord constraint={open} onClose={() => setOpenId(null)} />
        )}
      </div>
    </Card>
  );
}

// ===========================================================================
// 2 — Network
// ===========================================================================

interface HostRow {
  host: string;
}

const HOST_COLUMNS: ReadonlyArray<Column<HostRow>> = [
  {
    key: "host",
    header: "Host",
    mono: true,
    cell: (row) => row.host,
    sortValue: (row) => row.host,
    searchText: (row) => row.host,
  },
];

function NetworkSection({ posture, now }: { posture: PostureView; now: number }) {
  const hosts = useMemo<HostRow[]>(
    () => posture.egress_allowlist.map((host) => ({ host })),
    [posture.egress_allowlist],
  );

  return (
    <Card
      headingId="ne-posture-network"
      heading="Runtime reading · Network"
      link={
        // §14: the model-lanes table leaves this page; one link row takes its
        // place, because the reader who came here for it must still be told
        // where it went.
        <Link to="/models">Model routing is on Models ›</Link>
      }
    >
      <div className="ne-posturesection__lede">
        <p className="ne-evclass">
          <strong>DIRECT RUNTIME READ.</strong> Effective air gap and sanctioned egress read from
          the running deployment&rsquo;s enforced state.{" "}
          <ObservationAge iso={posture.network_observed_at} now={now} /> This is not an endpoint
          liveness probe.
        </p>
      </div>
      <dl className="ne-kv">
        <div>
          <dt>Air gap</dt>
          <dd>{posture.air_gap ? "On" : "Off"}</dd>
        </div>
        <div>
          <dt>Allowed hosts</dt>
          <dd>
            {hosts.length === 0 ? (
              "None — no external host is sanctioned."
            ) : (
              <DataTable<HostRow>
                columns={HOST_COLUMNS}
                rows={hosts}
                rowKey={(row) => row.host}
                label="Allowed hosts"
                things="allowed hosts"
                defaultSort={HOST_SORT}
              />
            )}
          </dd>
        </div>
      </dl>
    </Card>
  );
}

// ===========================================================================
// 3 — Configuration
// ===========================================================================

/** A served instant as a table cell: relative on the face, absolute in `title`,
 * always inside <time> (§0 vocabulary rules). */
function Instant({ iso }: { iso: string }) {
  return (
    <time dateTime={iso} title={absoluteTime(iso)}>
      {relativeTime(iso)}
    </time>
  );
}

const HISTORY_COLUMNS: ReadonlyArray<Column<ConfigHashSpanView>> = [
  {
    key: "hash",
    header: "Configuration",
    width: "180px",
    cell: (row) => (
      <HashChip hash={row.hash} label={`Copy configuration hash ${truncateHash(row.hash)}`} />
    ),
    sortValue: (row) => row.hash,
    searchText: (row) => row.hash,
  },
  {
    key: "from",
    header: "From",
    width: "160px",
    numeric: true,
    firstSort: "desc",
    cell: (row) => <Instant iso={row.from} />,
    sortValue: (row) => Date.parse(row.from),
  },
  {
    key: "to",
    header: "To",
    width: "160px",
    numeric: true,
    firstSort: "desc",
    // §14: "To (`—` for in force)". The table's own column resilience renders
    // the em dash for an absent cell — nothing here invents an end date.
    cell: (row) => (row.to === null ? null : <Instant iso={row.to} />),
    sortValue: (row) => (row.to === null ? null : Date.parse(row.to)),
  },
];

function ConfigurationSection({ posture, now }: { posture: PostureView; now: number }) {
  const inForce = spanInForce(posture.config_hash_history);
  const since = inForce === null ? null : shortDate(inForce.from);
  const configHash = inForce?.hash ?? posture.config_hash;

  return (
    <Card headingId="ne-posture-configuration" heading="Runtime evidence · Configuration">
      <div className="ne-posturesection__lede">
        <p className="ne-evclass">
          <strong>AUDIT-BACKED RUNTIME READ.</strong> The current process configuration and
          server-recorded audit history.{" "}
          <ObservationAge iso={posture.configuration_observed_at} now={now} /> This is not a
          liveness probe.
        </p>
      </div>
      <div className="ne-plate">
        <HashChip hash={configHash} label={`Copy configuration hash ${truncateHash(configHash)}`} />
        {inForce !== null && since !== null ? (
          <span className="ne-plate__since">
            In force since{" "}
            <time dateTime={inForce.from} title={absoluteTime(inForce.from)}>
              {since}
            </time>
          </span>
        ) : null}
      </div>
      <DataTable<ConfigHashSpanView>
        columns={HISTORY_COLUMNS}
        rows={posture.config_hash_history}
        rowKey={(row) => `${row.hash}:${row.from}`}
        label="Configuration history"
        things="configuration history"
        defaultSort={HISTORY_SORT}
        empty={<Empty message="No configuration changes recorded." />}
        // §14's single permitted sentence of explanation on this page: it tells
        // the reviewer how to use the table.
        note="Every audit record carries the configuration in force when it was written."
      />
    </Card>
  );
}

// ===========================================================================
// The screen
// ===========================================================================

export function PosturePage() {
  const client = useApiClient();
  const read = useGovernedRead(useCallback(() => client.posture(), [client]));
  const posture = read.data;

  // Re-read successful/error states on a cadence. Initial loading and a 403 do
  // not start parallel/repeated reads; the latter also avoids salting the audit
  // log with repeated denials.
  // useGovernedRead clears data when a poll fails, which withdraws every prior
  // runtime value instead of leaving a stale success below an error.
  const { reload } = read;
  useEffect(() => {
    if (read.status === "loading" || read.status === "refused") return;
    const id = window.setInterval(reload, RELOAD_MS);
    return () => window.clearInterval(id);
  }, [read.status, reload]);

  // Ages continue to change between reads. A cached response cannot reset its
  // age because the displayed instant is server-authored payload data.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(id);
  }, []);

  // §2.4: the server said no, and this renders that — with no strip, no tiles
  // and no count, because all three sections are views of the one read this
  // key was refused. A role boundary is drawn neutral, never as a fault.
  if (read.status === "refused") {
    return (
      <ListScreen title="Posture">
        <Refused thing="posture" page="Posture" />
      </ListScreen>
    );
  }

  const status: TableStatus =
    read.status === "loading" ? "loading" : read.status === "error" ? "error" : "ok";

  return (
    <div className="ne-posturepage">
      <ListScreen title="Posture">
        {/* The claim strip answers the screen's whole question from the SAME
            governed read the sections below use — it appears only once that
            read has succeeded at least once, never over a fabricated interim
            value. */}
        {posture === null ? null : <ClaimStrip posture={posture} now={now} />}
        <ControlsSection
          constraints={posture?.constraints ?? []}
          attestationSource={posture?.attestation_source ?? ""}
          status={status}
          onRetry={read.reload}
        />
        {/* Network and Configuration are views of the SAME read. A background
            re-read may keep the prior payload briefly, but its served stamps
            keep aging. A failure clears it; loading/error states are stated once
            by the Controls annunciator. */}
        {posture === null ? null : (
          <>
            <NetworkSection posture={posture} now={now} />
            <ConfigurationSection posture={posture} now={now} />
          </>
        )}
      </ListScreen>
    </div>
  );
}
