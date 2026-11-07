// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §15 — ACCESS. Who can sign in, what each role opens, and who can approve a
// Tier-3 action. Read AND write: role assignment goes through the governed path
// (§2.5), and the console never edits the identity provider itself.
//
// WHAT THIS REPLACES: a heading over nothing. The route rendered a title row and
// an empty body, which reads as a broken render rather than a screen whose data
// has not shipped — the reader cannot tell "this deployment cannot answer yet"
// from "this deployment answered, and the answer is nothing".
//
// SO THE SCREEN IS BUILT WHOLE AND STATES WHICH ONE IT IS. Both tabs render
// their real tables — sortable headers, the surface filter, live counts, row
// panes and the two governed writes — the moment the client can serve §18 B6.
// Until then each tab renders one sentence naming what appears here and what it
// waits on. No toolbar, no count and above all no empty table: a table with a
// header row and no body states that the deployment has no principals, which is
// a different and false claim from "nobody has asked yet".
//
// THIN CLIENT (C7). Every value is a field of the governed read. The surface
// grants are computed by the SERVER from the same role tuples the routes
// enforce; this console holds no role→surface table and derives no grant. The
// route is open to every authenticated key, so a direct visit still reaches the
// server and renders the SERVER's refusal — never one this console decided.

import { useCallback, useMemo, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { useSearchParams } from "react-router";
import { ApiError } from "../api/client";
import { useNow } from "../approvals/useNow";
import { useApiClient } from "../auth/useApiClient";
// The four-state governed read (loading · ok · refused · error). Reused rather
// than re-implemented: refused and error are different facts, and exactly one
// state machine in this console decides which is which.
import { useGovernedRead } from "../landing/useGovernedRead";
import type { GovernedRead } from "../landing/useGovernedRead";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { ListScreen } from "../primitives/PageHeader";
import {
  Empty,
  LoadError,
  PendingMarker,
  PolicyDenied,
  Refused,
  SaveError,
} from "../primitives/states";
import {
  FilterChip,
  FilterSelect,
  PrimaryButton,
  Toolbar,
  ToolbarButton,
} from "../primitives/Toolbar";
import { absoluteTime, formatCount, relativeTime } from "../primitives/format";
import { filterRows, sortRows } from "../primitives/tableSort";
import type { SortState } from "../primitives/tableSort";
import {
  SURFACE_OPTIONS,
  applySurfaceFilter,
  approverCount,
  chipOverflow,
  grantLines,
  listSortValue,
  membershipDelta,
  peopleCount,
  roleCount,
  roleNames,
} from "./access";
import type {
  AccessChange,
  AccessPrincipalEntry,
  AccessRoleEntry,
  Tier3ApproverEntry,
  Tier3ApproversView,
} from "./access";
import { accessReaders, accessWriter } from "./reads";
import type { AccessWriter } from "./reads";
import "./access.css";

/** §15: People, "default sort Principal A–Z". */
const PEOPLE_SORT: SortState = { key: "principal", dir: "asc" };

/** §15 names no default for the two Roles-tab tables. Both are lists of names,
 * so both take the same default the People table states: identity, A–Z. */
const ROLE_SORT: SortState = { key: "role", dir: "asc" };
const APPROVER_SORT: SortState = { key: "principal", dir: "asc" };

/** §15's "chip list, +n overflow" — three names, then the count. */
const CHIP_LIMIT = 3;

type Tab = "people" | "roles";

// ===========================================================================
// The tabs
// ===========================================================================

const TABS: ReadonlyArray<{ id: Tab; label: string }> = [
  { id: "people", label: "People" },
  { id: "roles", label: "Roles" },
];

function Tabs({ tab, onChange }: { tab: Tab; onChange: (next: Tab) => void }) {
  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const index = TABS.findIndex((entry) => entry.id === tab);
      let next: number;
      if (event.key === "ArrowRight") next = (index + 1) % TABS.length;
      else if (event.key === "ArrowLeft") next = (index - 1 + TABS.length) % TABS.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = TABS.length - 1;
      else return;
      event.preventDefault();
      const target = TABS[next];
      if (target) onChange(target.id);
    },
    [onChange, tab],
  );
  return (
    <div className="ne-access__tabs" role="tablist" aria-label="Access" onKeyDown={onKeyDown}>
      {TABS.map((entry) => {
        const selected = entry.id === tab;
        return (
          <button
            key={entry.id}
            type="button"
            role="tab"
            id={`ne-access-tab-${entry.id}`}
            aria-selected={selected}
            aria-controls={`ne-access-panel-${entry.id}`}
            tabIndex={selected ? 0 : -1}
            className={`ne-access__tab${selected ? " ne-access__tab--on" : ""}`}
            onClick={() => onChange(entry.id)}
          >
            {entry.label}
          </button>
        );
      })}
    </div>
  );
}

function Panel({ tab, children }: { tab: Tab; children: ReactNode }) {
  return (
    <div
      className="ne-access__panel"
      role="tabpanel"
      id={`ne-access-panel-${tab}`}
      aria-labelledby={`ne-access-tab-${tab}`}
    >
      {children}
    </div>
  );
}

// ===========================================================================
// Cells
// ===========================================================================

/** A chip list with §15's `+n` overflow. The overflow is never a hidden fact:
 * the names it stands for are in its own title and in the row's detail pane. */
function Chips({ values }: { values: readonly string[] }) {
  const { shown, rest } = chipOverflow(values, CHIP_LIMIT);
  return (
    <span className="ne-access__chips">
      {shown.map((value) => (
        <span className="ne-access__chip" key={value}>
          {value}
        </span>
      ))}
      {rest.length === 0 ? null : (
        <span className="ne-access__more" title={rest.join(", ")}>
          +{rest.length}
        </span>
      )}
    </span>
  );
}

/** §0's time grammar: relative in the cell, absolute in the title, always
 * inside a <time>. An ABSENT instant never reaches here — the column returns
 * null so the table standard renders its own em dash, rather than a <time>
 * wrapped around nothing, which draws as a blank cell and reads as a render
 * bug rather than as a value the server did not send. */
function When({ iso, now }: { iso: string; now: number }) {
  return (
    <time dateTime={iso} title={absoluteTime(iso)}>
      {relativeTime(iso, now)}
    </time>
  );
}

// ===========================================================================
// The governed write (§2.5)
// ===========================================================================

type WriteState =
  | { kind: "idle" }
  | { kind: "saving" }
  /** 403: the server's reason, rendered verbatim where the submit happened —
   * or null where it stated none, because the console does not write one on
   * the gate's behalf. */
  | { kind: "denied"; reason: string | null }
  | { kind: "error"; detail: string | null };

/**
 * ONE submit path for both of §15's writes.
 *
 * Applied and pending are BOTH resolved by re-reading the server: §2.5 forbids
 * the console pre-rendering an unapplied change, and the pending marker has to
 * survive a reload, which it can only do if it was read back rather than
 * remembered. So neither answer writes anything into local state — both reload,
 * and the object reflects whatever the server now says.
 */
function useGovernedSubmit(writer: AccessWriter | null, onSettled: () => void) {
  const [state, setState] = useState<WriteState>({ kind: "idle" });
  const reset = useCallback(() => setState({ kind: "idle" }), []);
  const submit = useCallback(
    async (change: AccessChange) => {
      if (writer === null) return;
      setState({ kind: "saving" });
      try {
        await writer(change);
        setState({ kind: "idle" });
        onSettled();
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 403) {
          // THE GATE'S OWN SENTENCE OR NOTHING: `PolicyDenied` renders this as
          // the gate's account of the denial, so a 403 with no served `detail`
          // states the denial alone rather than a console sentence signed with
          // the gate's name.
          setState({ kind: "denied", reason: err.detail ?? null });
          return;
        }
        setState({
          kind: "error",
          detail: err instanceof ApiError ? (err.detail ?? null) : null,
        });
      }
    },
    [writer, onSettled],
  );
  return { state, submit, reset };
}

function WriteOutcome({ state }: { state: WriteState }) {
  if (state.kind === "denied") return <PolicyDenied reason={state.reason} />;
  if (state.kind === "error") return <SaveError detail={state.detail} />;
  return null;
}

/**
 * The membership checklist both writes use. It offers ONLY names the server
 * sent, so no submission can name a group or a principal this deployment does
 * not have, and it submits the DELTA rather than the whole list — a whole-list
 * write would silently revert anything changed between this read and this
 * submit.
 */
function MembershipEditor({
  legend,
  candidates,
  current,
  busy,
  onSubmit,
}: {
  legend: string;
  candidates: readonly string[];
  current: readonly string[];
  busy: boolean;
  onSubmit: (next: readonly string[]) => void;
}) {
  const [chosen, setChosen] = useState<readonly string[]>(current);
  const delta = membershipDelta(current, chosen);
  const unchanged = delta.add.length === 0 && delta.remove.length === 0;
  return (
    <div className="ne-access__editor">
      <fieldset className="ne-access__checks">
        <legend className="ne-access__legend">{legend}</legend>
        {candidates.map((value) => (
          <label className="ne-access__check" key={value}>
            <input
              type="checkbox"
              checked={chosen.includes(value)}
              onChange={(event) =>
                setChosen(
                  event.target.checked
                    ? [...chosen, value]
                    : chosen.filter((entry) => entry !== value),
                )
              }
            />
            <span>{value}</span>
          </label>
        ))}
      </fieldset>
      <PrimaryButton
        label="Request change"
        disabled={busy || unchanged}
        onClick={() => onSubmit(chosen)}
      />
    </div>
  );
}

// ===========================================================================
// The People tab
// ===========================================================================

function usePeopleColumns(): Column<AccessPrincipalEntry>[] {
  return useMemo(
    () => [
      {
        key: "principal",
        header: "Principal",
        mono: true,
        cell: (row) => (
          <span className="ne-access__idcell">
            <span className="ne-access__id">{row.principal_id}</span>
            {row.pending_change === null ? null : (
              <span className="ne-access__sub">
                <PendingMarker
                  by={row.pending_change.requested_by}
                  requestId={row.pending_change.request_id}
                />
              </span>
            )}
          </span>
        ),
        sortValue: (row) => row.principal_id,
        searchText: (row) => row.principal_id,
      },
      {
        key: "groups",
        header: "Groups",
        cell: (row) => (row.groups.length === 0 ? null : <Chips values={row.groups} />),
        // Sorted by the served list, joined — so principals holding the same
        // groups sit together. An empty list is absent, not first.
        sortValue: (row) => listSortValue(row.groups),
        searchText: (row) => row.groups.join(" "),
      },
      {
        key: "opens",
        header: "Opens",
        width: "280px",
        cell: (row) => (row.surfaces.length === 0 ? null : <Chips values={row.surfaces} />),
        sortValue: (row) => listSortValue(row.surfaces),
        searchText: (row) => row.surfaces.join(" "),
      },
    ],
    [],
  );
}

function PeoplePane({
  entry,
  roles,
  writer,
  onClose,
  onSettled,
}: {
  entry: AccessPrincipalEntry;
  roles: readonly AccessRoleEntry[];
  writer: AccessWriter | null;
  onClose: () => void;
  onSettled: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const settled = useCallback(() => {
    setEditing(false);
    onSettled();
  }, [onSettled]);
  const { state, submit, reset } = useGovernedSubmit(writer, settled);
  const grants = grantLines(entry);

  return (
    <DetailPane
      title={entry.principal_id}
      onClose={onClose}
      footer={
        writer === null || editing ? null : (
          <PrimaryButton
            label="Change roles…"
            onClick={() => {
              reset();
              setEditing(true);
            }}
          />
        )
      }
    >
      <DetailFields>
        <DetailField label="Groups">
          {entry.groups.length === 0 ? "—" : entry.groups.join(" · ")}
        </DetailField>
        {grants.length === 0 ? (
          <DetailField label="Opens">—</DetailField>
        ) : (
          grants.map((grant) => (
            <DetailField label={grant.surface} key={grant.surface}>
              {grant.via}
            </DetailField>
          ))
        )}
        {entry.pending_change === null ? null : (
          <DetailField label="Status">
            <PendingMarker
              by={entry.pending_change.requested_by}
              requestId={entry.pending_change.request_id}
            />
          </DetailField>
        )}
      </DetailFields>
      {editing ? (
        <MembershipEditor
          legend="Groups"
          candidates={roleNames(roles)}
          current={entry.groups}
          busy={state.kind === "saving"}
          onSubmit={(next) => {
            const delta = membershipDelta(entry.groups, next);
            void submit({
              kind: "roles",
              principal_id: entry.principal_id,
              add_groups: delta.add,
              remove_groups: delta.remove,
            });
          }}
        />
      ) : null}
      <WriteOutcome state={state} />
    </DetailPane>
  );
}

function PeopleTab({
  principals,
  roles,
  writer,
}: {
  principals: GovernedRead<{ principals: AccessPrincipalEntry[] }>;
  roles: GovernedRead<{ roles: AccessRoleEntry[] }>;
  writer: AccessWriter | null;
}) {
  const [surface, setSurface] = useState("all");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortState>(PEOPLE_SORT);
  const [openKey, setOpenKey] = useState<string | null>(null);

  const columns = usePeopleColumns();
  const rows = useMemo(() => principals.data?.principals ?? [], [principals.data]);
  const filtered = useMemo(
    () => filterRows(applySurfaceFilter(rows, surface), columns, search),
    [rows, surface, columns, search],
  );
  /** Exactly the rows the table renders, in the order it renders them. */
  const windowRows = useMemo(() => sortRows(filtered, columns, sort), [filtered, columns, sort]);

  const reloadBoth = useCallback(() => {
    principals.reload();
    roles.reload();
  }, [principals, roles]);

  // §2.4: the server said no, and this renders THAT — with no toolbar, no
  // filter and above all no count, because a zero counted out of a read this
  // key was never shown is a fabricated zero.
  if (principals.status === "refused") return <Refused thing="Access" page="Access" />;

  const status: TableStatus =
    principals.status === "loading" ? "loading" : principals.status === "error" ? "error" : "ok";
  const open = openKey === null ? null : (rows.find((row) => row.principal_id === openKey) ?? null);

  return (
    <>
      <Toolbar
        search={{ value: search, onChange: setSearch, placeholder: "Filter people…" }}
        filters={
          <>
            <FilterSelect
              label="Surface"
              value={surface}
              options={SURFACE_OPTIONS}
              onChange={setSurface}
            />
            {surface === "all" ? null : (
              <FilterChip label={`Surface: ${surface}`} onDismiss={() => setSurface("all")} />
            )}
          </>
        }
        // A COUNT OF A WINDOW THAT HAS NOT ARRIVED IS A FABRICATED ZERO. The
        // skeleton rows already say the read is in flight.
        count={status === "loading" ? "" : peopleCount(windowRows.length)}
      />
      <DataTable<AccessPrincipalEntry>
        columns={columns}
        rows={filtered}
        rowKey={(row) => row.principal_id}
        label="People"
        things="people"
        sort={sort}
        onSortChange={setSort}
        onRowActivate={(row) => setOpenKey(row.principal_id)}
        selectedKey={openKey}
        status={status}
        empty={<Empty message="No principals found." />}
        error={<LoadError things="people" onRetry={principals.reload} />}
      />
      {open === null ? null : (
        <PeoplePane
          // KEYED BY THE PRINCIPAL. Without it, React reuses the pane instance
          // across rows and the open editor keeps the PREVIOUS principal's
          // ticked groups — a checklist that says one person's memberships
          // under another person's name, one click from being submitted.
          key={open.principal_id}
          entry={open}
          roles={roles.data?.roles ?? []}
          writer={writer}
          onClose={() => setOpenKey(null)}
          onSettled={reloadBoth}
        />
      )}
    </>
  );
}

// ===========================================================================
// The Roles tab — the pinned Tier-3 section, then the roles table
// ===========================================================================

function useApproverColumns(now: number): Column<Tier3ApproverEntry>[] {
  return useMemo(
    () => [
      {
        key: "principal",
        header: "Principal",
        mono: true,
        cell: (row) => row.principal_id,
        sortValue: (row) => row.principal_id,
        searchText: (row) => row.principal_id,
      },
      {
        key: "since",
        header: "Since",
        width: "160px",
        numeric: true,
        firstSort: "desc",
        cell: (row) => (row.since === null ? null : <When iso={row.since} now={now} />),
        sortValue: (row) => (row.since === null ? null : Date.parse(row.since)),
      },
    ],
    [now],
  );
}

function Tier3Section({
  tier3,
  principals,
  writer,
  onSettled,
}: {
  tier3: GovernedRead<Tier3ApproversView>;
  principals: GovernedRead<{ principals: AccessPrincipalEntry[] }>;
  writer: AccessWriter | null;
  onSettled: () => void;
}) {
  const now = useNow(30_000);
  const columns = useApproverColumns(now);
  const [editing, setEditing] = useState(false);
  const settled = useCallback(() => {
    setEditing(false);
    onSettled();
  }, [onSettled]);
  const { state, submit, reset } = useGovernedSubmit(writer, settled);

  const rows = tier3.data?.approvers ?? [];
  // This is evidence served alongside the effective list. It never changes
  // `rows`: the wire does not say whether the parked operation adds or removes
  // an approver, and an unapplied membership must not be inferred locally.
  const pending = tier3.data?.pending_change ?? null;
  const current = rows.map((row) => row.principal_id);
  const candidates = (principals.data?.principals ?? []).map((row) => row.principal_id);

  function body(): ReactNode {
    if (tier3.status === "refused") return <Refused thing="Access" page="Access" />;
    const status: TableStatus =
      tier3.status === "loading" ? "loading" : tier3.status === "error" ? "error" : "ok";
    return (
      <>
        <Toolbar
          count={status === "loading" ? "" : approverCount(rows.length)}
          actions={
            // No control offers a write the server has no endpoint for, and
            // none offers a checklist built out of a principal read that has
            // not arrived.
            writer === null || principals.status !== "ok" ? null : (
              <ToolbarButton
                label="Edit Tier-3 approvers…"
                onClick={() => {
                  reset();
                  setEditing(true);
                }}
              />
            )
          }
        />
        {pending === null ? null : (
          <span className="ne-access__sub">
            <PendingMarker by={pending.requested_by} requestId={pending.request_id} />
          </span>
        )}
        <DataTable<Tier3ApproverEntry>
          columns={columns}
          rows={rows}
          rowKey={(row) => row.principal_id}
          label="Tier-3 approvers"
          things="approvers"
          defaultSort={APPROVER_SORT}
          status={status}
          empty={<Empty message="No Tier-3 approvers named." />}
          error={<LoadError things="Tier-3 approvers" onRetry={tier3.reload} />}
        />
      </>
    );
  }

  return (
    <section className="ne-access__section" aria-labelledby="ne-access-tier3">
      <h2 className="ne-access__sectiontitle" id="ne-access-tier3">
        Tier-3 approvers
      </h2>
      {body()}
      {editing ? (
        <DetailPane title="Tier-3 approvers" onClose={() => setEditing(false)}>
          <MembershipEditor
            legend="Approvers"
            candidates={candidates}
            current={current}
            busy={state.kind === "saving"}
            onSubmit={(next) => {
              const delta = membershipDelta(current, next);
              void submit({
                kind: "tier3-approvers",
                add_principals: delta.add,
                remove_principals: delta.remove,
              });
            }}
          />
          <WriteOutcome state={state} />
        </DetailPane>
      ) : (
        <WriteOutcome state={state} />
      )}
    </section>
  );
}

function useRoleColumns(): Column<AccessRoleEntry>[] {
  return useMemo(
    () => [
      {
        key: "role",
        header: "Role",
        cell: (row) => row.group,
        sortValue: (row) => row.group,
        searchText: (row) => row.group,
      },
      {
        key: "opens",
        header: "Opens",
        cell: (row) => (row.surfaces.length === 0 ? null : <Chips values={row.surfaces} />),
        sortValue: (row) => listSortValue(row.surfaces),
        searchText: (row) => row.surfaces.join(" "),
      },
      {
        key: "members",
        header: "Members",
        width: "120px",
        numeric: true,
        firstSort: "desc",
        // The SERVER's count, not members.length: a member list this key may
        // read can be trimmed while the count is not.
        cell: (row) => formatCount(row.member_count),
        sortValue: (row) => row.member_count,
      },
    ],
    [],
  );
}

function RolesTab({
  roles,
  tier3,
  principals,
  writer,
  onSettled,
}: {
  roles: GovernedRead<{ roles: AccessRoleEntry[] }>;
  tier3: GovernedRead<Tier3ApproversView>;
  principals: GovernedRead<{ principals: AccessPrincipalEntry[] }>;
  writer: AccessWriter | null;
  onSettled: () => void;
}) {
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortState>(ROLE_SORT);
  const [openKey, setOpenKey] = useState<string | null>(null);
  const columns = useRoleColumns();
  const rows = useMemo(() => roles.data?.roles ?? [], [roles.data]);
  const filtered = useMemo(() => filterRows(rows, columns, search), [rows, columns, search]);

  function table(): ReactNode {
    if (roles.status === "refused") return <Refused thing="Access" page="Access" />;
    const status: TableStatus =
      roles.status === "loading" ? "loading" : roles.status === "error" ? "error" : "ok";
    return (
      <>
        <Toolbar
          search={{ value: search, onChange: setSearch, placeholder: "Filter roles…" }}
          count={status === "loading" ? "" : roleCount(filtered.length)}
        />
        <DataTable<AccessRoleEntry>
          columns={columns}
          rows={filtered}
          rowKey={(row) => row.group}
          label="Roles"
          things="roles"
          sort={sort}
          onSortChange={setSort}
          onRowActivate={(row) => setOpenKey(row.group)}
          selectedKey={openKey}
          status={status}
          empty={<Empty message="No roles found." />}
          error={<LoadError things="roles" onRetry={roles.reload} />}
        />
      </>
    );
  }

  const open = openKey === null ? null : (rows.find((row) => row.group === openKey) ?? null);

  return (
    <>
      <Tier3Section tier3={tier3} principals={principals} writer={writer} onSettled={onSettled} />
      <section className="ne-access__section" aria-labelledby="ne-access-roles">
        <h2 className="ne-access__sectiontitle" id="ne-access-roles">
          Roles
        </h2>
        {table()}
      </section>
      {open === null ? null : (
        <DetailPane title={open.group} onClose={() => setOpenKey(null)}>
          <DetailFields>
            <DetailField label="Opens">
              {open.surfaces.length === 0 ? "—" : open.surfaces.join(" · ")}
            </DetailField>
            <DetailField label="Members">{formatCount(open.member_count)}</DetailField>
            <DetailField label="Member list" mono>
              {open.members.length === 0 ? (
                "—"
              ) : (
                <span className="ne-access__members">{open.members.join("\n")}</span>
              )}
            </DetailField>
          </DetailFields>
        </DetailPane>
      )}
    </>
  );
}

// ===========================================================================
// The screen
// ===========================================================================

export function AccessPage() {
  const client = useApiClient();
  const [params, setParams] = useSearchParams();
  const tab: Tab = params.get("tab") === "roles" ? "roles" : "people";

  const setTab = useCallback(
    (next: Tab) => {
      const updated = new URLSearchParams(params);
      if (next === "people") updated.delete("tab");
      else updated.set("tab", next);
      setParams(updated, { replace: true });
    },
    [params, setParams],
  );

  const readers = useMemo(() => accessReaders(client), [client]);
  const writer = useMemo(() => accessWriter(client), [client]);

  const principals = useGovernedRead(
    useCallback(
      async () => (readers === null ? { principals: [] } : await readers.principals()),
      [readers],
    ),
  );
  const roles = useGovernedRead(
    useCallback(async () => (readers === null ? { roles: [] } : await readers.roles()), [readers]),
  );
  const tier3 = useGovernedRead(
    useCallback(
      async () =>
        readers === null ? { approvers: [], pending_change: null } : await readers.tier3(),
      [readers],
    ),
  );

  const reloadAll = useCallback(() => {
    principals.reload();
    roles.reload();
    tier3.reload();
  }, [principals, roles, tier3]);

  return (
    <div className="ne-access">
      <ListScreen title="Access" tabs={<Tabs tab={tab} onChange={setTab} />}>
        {tab === "people" ? (
          <Panel tab="people">
            {readers === null ? (
              // The stated fallback: what appears here, and what it waits on.
              // NOT a table with no rows — that would count a zero out of a
              // question nobody asked.
              <Empty message="People, their groups and what each opens appear here once this deployment serves them." />
            ) : (
              <PeopleTab principals={principals} roles={roles} writer={writer} />
            )}
          </Panel>
        ) : (
          <Panel tab="roles">
            {readers === null ? (
              <Empty message="Roles, what each opens and the Tier-3 approvers appear here once this deployment serves them." />
            ) : (
              <RolesTab
                roles={roles}
                tier3={tier3}
                principals={principals}
                writer={writer}
                onSettled={reloadAll}
              />
            )}
          </Panel>
        )}
      </ListScreen>
    </div>
  );
}
