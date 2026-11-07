// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE SHELL (§1.1). Two pieces of persistent chrome and nothing else:
//
//   ┌──────────┬──────────────────────────────────────────────┐
//   │          │ Top bar (48px): ⌘K search · status · account │
//   │   Rail   ├──────────────────────────────────────────────┤
//   │  (240px) │ Page: title row + toolbar + content          │
//   └──────────┴──────────────────────────────────────────────┘
//
// Everything else is page content. The per-page status strip, the purpose line,
// the page foot and the hero readouts are deleted (§1.3) — a page is a title row
// and content, and the top third of every screen is no longer spent explaining
// the screen to someone who navigated to it deliberately.
//
// ROUTE RENAMES SHIP WITH REDIRECTS (§1.2). Every old path still resolves, so a
// bookmark, a shared link, or a link in an email from last month lands on the
// renamed screen instead of a blank router miss.
//
// /auth/callback stays OUTSIDE auth; every other route renders inside the authed
// shell.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  BrowserRouter,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useParams,
} from "react-router";
import { AuthCallback } from "./auth/AuthCallback";
import { CompareCallback } from "./auth/CompareCallback";
import { RequireAuth } from "./auth/RequireAuth";
import { LandingPage } from "./landing/LandingPage";
import { PosturePage } from "./posture/PosturePage";
import { PostureProvider } from "./posture/PostureContext";
import { ChatPage } from "./chat/ChatPage";
import { DEFAULT_ASK_AGENT_ID } from "./api/types";
import { SearchPage } from "./search/SearchPage";
import { ApprovalsPage } from "./approvals/ApprovalsPage";
import { AuditPage } from "./audit/AuditPage";
import { EvalsPage } from "./evals/EvalsPage";
import { KpiPage } from "./kpi/KpiPage";
import { KnowledgePage } from "./knowledge/KnowledgePage";
import { DocumentsPage } from "./knowledge/documents/DocumentsPage";
import { GraphPage } from "./knowledge/graph/GraphPage";
import { OntologyPage } from "./knowledge/ontology/OntologyPage";
import { FlywheelPage } from "./flywheel/FlywheelPage";
import { ModelsPage } from "./models/ModelsPage";
import { WorkflowsPage } from "./workflows/WorkflowsPage";
import { AnswerLedgerPage } from "./answer-ledger/AnswerLedgerPage";
import { AccessPage } from "./access/AccessPage";
import { SettingsPage } from "./settings/SettingsPage";
import { AuditPollProvider } from "./hooks/useAuditPoll";
import { PendingApprovalsProvider } from "./hooks/usePendingApprovals";
import { ProveProvider } from "./prove/ProveContext";
import { ProveOverlay } from "./prove/ProveOverlay";
import { NavRail } from "./rail/NavRail";
import { TopBar } from "./rail/TopBar";
import { StatusChip } from "./rail/StatusChip";
import { AccountMenu } from "./rail/AccountMenu";
import { CommandPalette } from "./rail/CommandPalette";
import { RoutePresentation } from "./rail/RoutePresentation";
import { useShellKeys } from "./rail/useShellKeys";
import { ThemeProvider } from "./rail/theme";
import { DensityProvider } from "./primitives/density";
import { InspectProvider } from "./primitives/InspectContext";
import { PrincipalProvider, usePrincipal, usePrincipalOwner } from "./auth/PrincipalContext";
import type { PrincipalState } from "./auth/PrincipalContext";
import { approvalOwnerReady, useApprovalOwner } from "./approvals/approvalOwner";
import { DirectoryProvider } from "./directory/DirectoryContext";
import { ConnectPage } from "./connect/ConnectPage";
import { DemoProvider } from "./demo/DemoContext";
import { DemoOverlay } from "./demo/DemoOverlay";
import { TracePage } from "./trace/TracePage";
import { ConversationLedgerPage } from "./conversation-ledger/ConversationLedgerPage";
import { VerifiedQueriesPage } from "./verified-queries/VerifiedQueriesPage";
import {
  AgentRunPage,
  AgentsCatalog,
  AgentWorkspace,
  readLastWorkspace,
  RunComparePage,
} from "./agents";
import { ApprovalOnlyRunPane } from "./agents/RunApprovalPane";
import type { ExecutionIdentity } from "./agents";
import { useApiClient } from "./auth/useApiClient";
import { usePosture } from "./posture/PostureContext";
import { Empty, Loading } from "./primitives/states";
import { EmptyState } from "./primitives/EmptyState";
import { ListScreen } from "./primitives/PageHeader";

const NARROW = "(max-width: 1100px)";
// PrincipalProvider deliberately retains its last served state until a new
// client's /me resolves. Shell is always mounted, so it can bind each served
// state object to the client that produced it and fail closed during that gap.
const PALETTE_PRINCIPAL_CLIENTS = new WeakMap<object, object>();

/**
 * §1.1 — the rail collapses to a 56px icon rail below 1100px, and the top bar's
 * toggle overrides that either way.
 *
 * The user's choice WINS over the viewport, in both directions, until they clear
 * it by resizing past the breakpoint again. A collapse control that a resize
 * silently undoes is a control people stop trusting.
 */
function useRailCollapse(): { collapsed: boolean; toggle: () => void } {
  const [narrow, setNarrow] = useState(
    () => typeof window !== "undefined" && (window.matchMedia?.(NARROW).matches ?? false),
  );
  const [choice, setChoice] = useState<boolean | null>(null);

  useEffect(() => {
    const mql = window.matchMedia?.(NARROW);
    if (!mql) return;
    const onChange = (event: MediaQueryListEvent) => {
      setNarrow(event.matches);
      setChoice(null);
    };
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  const collapsed = choice ?? narrow;
  const toggle = useCallback(() => setChoice(!collapsed), [collapsed]);
  return { collapsed, toggle };
}

function Shell() {
  const { collapsed, toggle } = useRailCollapse();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const principal = usePrincipal();
  const paletteOwner = useApprovalOwner();
  const paletteInvoker = useRef<{ element: HTMLElement; generation: string } | null>(null);
  const currentPaletteGeneration = useRef(paletteOwner.generation);
  useLayoutEffect(() => {
    currentPaletteGeneration.current = paletteOwner.generation;
  }, [paletteOwner.generation]);
  const priorClient = PALETTE_PRINCIPAL_CLIENTS.get(principal);
  const paletteReady =
    approvalOwnerReady(paletteOwner) &&
    (priorClient === undefined || priorClient === paletteOwner.client);
  if (paletteReady && priorClient === undefined) {
    PALETTE_PRINCIPAL_CLIENTS.set(principal, paletteOwner.client);
  }
  const openPalette = useCallback(() => {
    if (paletteOpen) return;
    const element = document.activeElement;
    paletteInvoker.current =
      element instanceof HTMLElement && element !== document.body
        ? { element, generation: paletteOwner.generation }
        : null;
    setPaletteOpen(true);
  }, [paletteOpen, paletteOwner.generation]);
  const restorePaletteFocus = useCallback(() => {
    const invoker = paletteInvoker.current;
    paletteInvoker.current = null;
    if (
      invoker !== null &&
      invoker.generation === currentPaletteGeneration.current &&
      invoker.element.isConnected
    ) {
      invoker.element.focus();
    }
  }, []);
  const paletteWasOpen = useRef(false);
  useLayoutEffect(() => {
    if (paletteWasOpen.current && !paletteOpen) restorePaletteFocus();
    paletteWasOpen.current = paletteOpen;
  }, [paletteOpen, restorePaletteFocus]);
  useShellKeys({ onOpenPalette: openPalette });

  return (
    <div className={`ne-shell${collapsed ? " ne-shell--collapsed" : ""}`}>
      {/* THE FIRST FOCUSABLE ELEMENT ON EVERY PAGE. Tab order used to walk the
          wordmark, twelve nav links, the tour control and twelve constraint
          rows — 26+ stops before any page content, on every navigation, all
          day. One element turns that into one keystroke. */}
      <a className="ne-skip" href="#ne-main">
        Skip to main content
      </a>
      <NavRail collapsed={collapsed} />
      <div className="ne-shell__col">
        <TopBar collapsed={collapsed} onToggleRail={toggle} onOpenPalette={openPalette}>
          <StatusChip />
          <AccountMenu />
        </TopBar>
        <main className="ne-shell__stage" id="ne-main" tabIndex={-1}>
          <RoutePresentation />
          <Outlet />
        </main>
      </div>
      {paletteOpen ? (
        <CommandPalette
          onClose={() => setPaletteOpen(false)}
          owner={paletteOwner}
          principal={principal}
          ready={paletteReady}
        />
      ) : null}
      <ProveOverlay />
      <DemoOverlay />
    </div>
  );
}

/** The authed shell, under the single audit poll and the shared preferences. */
function AuthedLayout() {
  return (
    <RequireAuth>
      {/* PrincipalProvider is OUTSIDE the polls on purpose: the polls consult
          /v1/me's `entitlements` so they do not issue governed reads the server
          has already said it will refuse. That refusal traffic was the root
          cause of three separate findings — a home screen of 403 receipts, an
          audit ledger drowned in the console's own denials, and a business
          metric that climbed while a reviewer merely browsed. */}
      <PrincipalProvider>
        <AuditPollProvider>
          <PostureProvider>
            <ThemeProvider>
              <DensityProvider>
                <InspectProvider>
                  <DirectoryProvider>
                    <ProveProvider>
                      {/* PendingApprovalsProvider feeds the rail's live count
                          badge — ONE shared poll, never per-component. */}
                      <PendingApprovalsProvider>
                        <DemoProvider>
                          <Shell />
                        </DemoProvider>
                      </PendingApprovalsProvider>
                    </ProveProvider>
                  </DirectoryProvider>
                </InspectProvider>
              </DensityProvider>
            </ThemeProvider>
          </PostureProvider>
        </AuditPollProvider>
      </PrincipalProvider>
    </RequireAuth>
  );
}

function useAgentRouteContext() {
  const client = useApiClient();
  const principal = usePrincipal();
  const posture = usePosture();
  const identity: ExecutionIdentity | null =
    principal.status === "ok" && principal.principal !== null
      ? {
          principal_label: principal.principal.id,
          role_labels: principal.principal.groups,
        }
      : null;
  return {
    client,
    principal,
    identity,
    identityKey:
      identity === null
        ? "principal-unavailable"
        : JSON.stringify([identity.principal_label, ...identity.role_labels]),
    platformConfigHash: posture.status === "ok" ? (posture.posture?.config_hash ?? null) : null,
    configurationState:
      posture.status === "loading"
        ? ("loading" as const)
        : posture.status === "ok"
          ? ("ready" as const)
          : ("unavailable" as const),
  };
}

function AgentsIdentityBoundary({
  principal,
  children,
}: {
  principal: PrincipalState;
  children: () => ReactNode;
}) {
  if (principal.status === "loading") return <Loading things="execution identity" />;
  if (principal.status !== "ok" || principal.principal === null) {
    return <Empty message="Execution identity is unavailable." />;
  }
  return children();
}

/** THE INVENTORY VIEW, reached from "All agents" in the workspace sidebar. */
function AgentsCatalogRoute() {
  const context = useAgentRouteContext();
  return (
    <AgentsIdentityBoundary principal={context.principal}>
      {() => <AgentsCatalog client={context.client} identityKey={context.identityKey} />}
    </AgentsIdentityBoundary>
  );
}

/** `/agents` LANDS ON THE LAST-USED WORKSPACE.
 *
 * The rail's Agents entry is where the reader goes to work, and the work is a
 * conversation with one agent — not a roster they have to re-cross first. The
 * roster is one click away in the sidebar and behind "All agents".
 *
 * The stored id authorizes nothing and is re-validated on arrival: an agent
 * that is no longer visible answers the workspace's own read with the same
 * sanitized 404 as one that never existed. With no stored id (first visit,
 * blocked storage) this IS the catalog, which is where a first-time reader
 * should land anyway. */
function AgentsLandingRoute() {
  const last = readLastWorkspace();
  if (last === null) return <AgentsCatalogRoute />;
  return <Navigate to={`/agents/${encodeURIComponent(last)}`} replace />;
}

/** `/ask` keeps working by landing on the Agent it was always talking to. */
function AskRedirect() {
  return <Navigate to={`/agents/${encodeURIComponent(DEFAULT_ASK_AGENT_ID)}`} replace />;
}

function AgentWorkspaceRoute() {
  const context = useAgentRouteContext();
  const { agentId } = useParams();
  if (agentId === undefined) return <Empty message="This Agent isn’t available." />;
  return (
    <AgentsIdentityBoundary principal={context.principal}>
      {() => (
        <AgentWorkspace
          client={context.client}
          agentId={agentId}
          identityKey={context.identityKey}
          platformConfigHash={context.platformConfigHash}
          configurationState={context.configurationState}
          // Ask is MOUNTED here, not reimplemented: same stream, same grounding
          // gate, same citations. The injection keeps ChatPage's provider tree
          // out of every Agents interior test.
          // NO `key` HERE. Remounting on conversationId would destroy the live
          // thread at the exact moment the stream mints its Conversation and
          // the URL catches up — the turn the reader is watching arrive would
          // vanish. ChatPage handles the selection change itself.
          renderChat={(props) => (
            <ChatPage
              agentId={props.agentId}
              conversationId={props.conversationId}
              onConversationStarted={props.onConversationStarted}
              onOpenReceipt={props.onOpenReceipt}
              receiptPane={props.receiptPane}
              capsNote={props.capsNote}
              unavailableReason={props.unavailableReason}
              pastTurns={props.pastTurns}
              agentLabel={props.agentLabel}
              runRouting={props.runRouting}
              runTarget={props.runTarget}
            />
          )}
        />
      )}
    </AgentsIdentityBoundary>
  );
}

/** TWO RUNS SIDE BY SIDE — each side addressed by the URL, either pickable in
 * place. The receipt's "Compare with another Run" exit lands here with one
 * side filled; the route itself privileges no agent and no run. */
function AgentsCompareRoute() {
  const context = useAgentRouteContext();
  return (
    <AgentsIdentityBoundary principal={context.principal}>
      {() => <RunComparePage client={context.client} identityKey={context.identityKey} />}
    </AgentsIdentityBoundary>
  );
}

function AgentRunRoute() {
  const context = useAgentRouteContext();
  const { agentId, runId } = useParams();
  if (agentId === undefined || runId === undefined) {
    return <Empty message="This Run isn’t available." />;
  }
  return (
    <AgentsIdentityBoundary principal={context.principal}>
      {() => (
        <AgentRunPage
          client={context.client}
          agentId={agentId}
          runId={runId}
          identityKey={context.identityKey}
        />
      )}
    </AgentsIdentityBoundary>
  );
}

function AgentRunApprovalRoute() {
  const context = useAgentRouteContext();
  const approvalOwner = useApprovalOwner();
  const { agentId, runId, approvalId } = useParams();
  if (agentId === undefined || runId === undefined || approvalId === undefined) {
    return <Empty message="This approval isn’t available." />;
  }
  return (
    <AgentsIdentityBoundary principal={context.principal}>
      {() => (
        <ApprovalOnlyRunPane
          client={context.client}
          agentId={agentId}
          runId={runId}
          approvalId={approvalId}
          identityKey={`${context.identityKey}\u0000${approvalOwner.generation}`}
        />
      )}
    </AgentsIdentityBoundary>
  );
}

/** Bind Graph's route state to the same principal generation as its reads. */
function PrincipalOwnedGraphPage() {
  return <GraphPage owner={usePrincipalOwner()} />;
}

/**
 * The catch-all. Before this existed, a path with no matching route rendered
 * NOTHING: the SPA fallback serves index.html for every path, `<Routes>` with
 * no match renders null, and production builds strip react-router's dev-only
 * "no routes matched" warning — so a mistyped or stale URL on a hard reload
 * was a permanently blank screen with an empty root and an empty console
 * (found live on /documents during the 2026-08 audit walk). A router miss must
 * be a visible, navigable state, never a blank one.
 */
function RouteMiss() {
  const { pathname } = useLocation();
  return (
    <ListScreen title="Page not found">
      <EmptyState
        title="This address is not a console route"
        action={{ label: "Go to Home", to: "/" }}
      >
        There is no screen at <code>{pathname}</code>. Use the rail or ⌘K to get where you were
        going.
      </EmptyState>
    </ListScreen>
  );
}

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/auth/callback" element={<AuthCallback />} />
        {/* The second-persona OIDC popup returns here (outside AuthedLayout). */}
        <Route path="/auth/compare-callback" element={<CompareCallback />} />
        <Route element={<AuthedLayout />}>
          <Route path="/" element={<LandingPage />} />
          {/* ASK IS NOW REACHED THROUGH ITS AGENT, AND THE OLD URL STILL WORKS.
              The rail entry is gone (rail/navSections.ts) because the owner's
              instruction was that Agents is the top level; the ROUTE stays,
              because `/ask` is in the guided tour (demo/stations.ts), in the
              first-run tour's fixtures, on the far end of the `/chat` redirect
              and in an unknowable number of bookmarks. Deleting it would break
              all of those to save one line. */}
          <Route path="/ask" element={<AskRedirect />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/library" element={<KnowledgePage />} />
          <Route path="/knowledge/documents" element={<DocumentsPage />} />
          <Route path="/knowledge/graph" element={<PrincipalOwnedGraphPage />} />
          <Route path="/knowledge/ontology" element={<OntologyPage />} />
          <Route path="/verified-queries" element={<VerifiedQueriesPage />} />
          <Route path="/answer-ledger" element={<AnswerLedgerPage />} />
          <Route path="/approvals" element={<ApprovalsPage />} />
          <Route path="/sources" element={<ConnectPage />} />
          <Route path="/models" element={<ModelsPage />} />
          <Route path="/workflows" element={<WorkflowsPage />} />
          <Route path="/agents" element={<AgentsLandingRoute />} />
          <Route path="/agents/all" element={<AgentsCatalogRoute />} />
          <Route path="/agents/compare" element={<AgentsCompareRoute />} />
          <Route path="/agents/:agentId" element={<AgentWorkspaceRoute />} />
          <Route path="/agents/:agentId/runs/:runId" element={<AgentRunRoute />} />
          <Route
            path="/agents/:agentId/runs/:runId/approval/:approvalId"
            element={<AgentRunApprovalRoute />}
          />
          <Route path="/improvement" element={<FlywheelPage />} />
          <Route path="/evaluations" element={<EvalsPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/conversation-ledger" element={<ConversationLedgerPage />} />
          <Route path="/posture" element={<PosturePage />} />
          <Route path="/access" element={<AccessPage />} />
          <Route path="/insights" element={<KpiPage />} />
          <Route path="/settings" element={<SettingsPage />} />

          {/* §1.2 — the renames ship with redirects. Every one of these paths
              is somebody's bookmark. */}
          <Route path="/chat" element={<Navigate to="/ask" replace />} />
          {/* /documents never existed as a route, but the rail item is LABELED
              "Documents", so it is exactly what someone types or pastes for the
              Documents screen. It redirects rather than 404s for the same
              reason the renames above do. */}
          <Route path="/documents" element={<Navigate to="/knowledge/documents" replace />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/connect" element={<Navigate to="/sources" replace />} />
          <Route path="/flywheel" element={<Navigate to="/improvement" replace />} />
          <Route path="/evals" element={<Navigate to="/evaluations" replace />} />
          <Route path="/kpi" element={<Navigate to="/insights" replace />} />
          <Route path="/steward" element={<Navigate to="/improvement" replace />} />
          <Route
            path="/steward/digest"
            element={<Navigate to="/improvement?tab=digest" replace />}
          />

          {/* A trace is something you open ABOUT something (an answer, an
              approval, an audit row), not a destination someone browses to: a
              routed drill-down with no rail entry (§1.2). */}
          <Route path="/trace" element={<TracePage />} />
          <Route path="/trace/:traceId" element={<TracePage />} />

          {/* LAST, always: the visible router miss (see RouteMiss above). */}
          <Route path="*" element={<RouteMiss />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
