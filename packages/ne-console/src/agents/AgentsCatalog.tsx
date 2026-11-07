// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE CATALOG IS A ROSTER, NOT A TABLE.
//
// The shipped screen gave four Agents five sortable columns, and four rows
// cannot earn five columns: the reader paid a table's overhead — a header rank,
// a scan line per column, a sort affordance that never changes anything — for a
// list short enough to read whole. So each Agent is a card, and the cards are
// grouped by the only distinction the reader acts on: whether you type an
// instruction to it, or whether the plan was frozen before the run started.
//
// A card's anatomy IS its strategy. The agentic card opens with what you can
// ask it and carries the caps that bound the asking; a deterministic card opens
// with the versioned Workflow profile it is bound to, because there is no
// prompt and the profile is the whole of what will happen.

import { Fragment, useCallback } from "react";
import { Link } from "react-router";
import { HashChip } from "../primitives/HashChip";
import { ListScreen } from "../primitives/PageHeader";
import { Empty, LoadError, Loading } from "../primitives/states";
import { CursorPager } from "./CursorPager";
import { strategyGloss, strategyWord } from "./StrategyBadge";
import type { AgentPage, AgentSummary, AgentsInteriorClient } from "./types";
import { useAgentsRead, useCursorPages } from "./useAgentsRead";
import "./agents.css";

// THE ORDER WITHIN A GROUP IS THE SERVER'S.
//
// `listAgents` pages through opaque cursors, so re-sorting the rows of one page
// describes a sequence the server did not send, and a reader who pages forward
// then sees an order that agrees with nothing. Grouping by `default_strategy`
// is a PARTITION, not a re-sort: every Agent lands in exactly one group and the
// relative order of the Agents inside each group is the served array's order,
// unchanged. `Array.prototype.filter` is what guarantees that, and it is the
// only thing standing between this screen and the invented order above.
export function AgentsCatalog({
  client,
  identityKey,
}: {
  client: AgentsInteriorClient;
  identityKey: string;
}) {
  const pages = useCursorPages();
  const cursor = pages.cursor;
  const load = useCallback(() => client.listAgents(cursor), [client, cursor]);
  const [read, retry] = useAgentsRead<AgentPage>(load, identityKey);

  // The facts margin takes a count only once there IS one. Loading, refused and
  // failed reads pass nothing rather than a zero that claims an empty registry.
  const meta =
    read.phase === "ready"
      ? `${read.value.items.length} agents · ${read.value.items.filter((agent) => agent.available).length} available`
      : undefined;

  const items = read.phase === "ready" ? read.value.items : [];
  const conversational = items.filter((agent) => agent.default_strategy === "agentic");
  const fixed = items.filter((agent) => agent.default_strategy === "deterministic");

  return (
    <ListScreen title="Agents" meta={meta}>
      {read.phase === "loading" ? <Loading things="agents" /> : null}
      {read.phase === "unavailable" ? (
        <Empty message="The Agents catalog isn’t available." />
      ) : null}
      {read.phase === "error" ? <LoadError things="agents" onRetry={retry} /> : null}
      {read.phase === "ready" && items.length === 0 ? (
        <>
          <Empty message="No agents are registered in this deployment." />
          <p className="ne-state__line">
            Agents are defined in the registry and served to this console; this screen creates
            nothing.
          </p>
        </>
      ) : null}
      {read.phase === "ready" && items.length > 0 ? (
        <>
          <div className="ag-catalog">
            <AgentGroup
              ariaLabel="Conversational agents"
              heading="Conversational"
              subtitle="agentic — an open instruction, executed under visible caps"
              agents={conversational}
              // One conversational Agent is the screen's subject, not a tile in
              // a grid of four: it spans the row and carries the caps, the
              // model policy and the one primary action.
              lead={conversational.length === 1}
            />
            <AgentGroup
              ariaLabel="Deterministic agents"
              heading="Fixed workflows"
              subtitle="deterministic — the plan is frozen before the run starts; there is no prompt"
              agents={fixed}
              lead={false}
            />
          </div>
          <CursorPager
            label="Agent pages"
            nextCursor={read.value.next_cursor}
            onFirstPage={pages.onFirstPage}
            onBack={pages.back}
            onForward={() => pages.forward(read.value.next_cursor)}
          />
        </>
      ) : null}
    </ListScreen>
  );
}

/** A group with no Agents renders NOTHING — not a heading over an empty grid,
 * which is a promise of rows the server did not send. */
function AgentGroup({
  ariaLabel,
  heading,
  subtitle,
  agents,
  lead,
}: {
  ariaLabel: string;
  heading: string;
  subtitle: string;
  agents: readonly AgentSummary[];
  lead: boolean;
}) {
  if (agents.length === 0) return null;
  return (
    <section aria-label={ariaLabel}>
      <div className="ag-group__head">
        <h2>{heading}</h2>
        <span>{subtitle}</span>
      </div>
      <ul className="ag-cards">
        {agents.map((agent) => (
          // THE SPAN GOES ON THE GRID ITEM. `.ag-card--lead` sits on the
          // <article> INSIDE this <li>, and a `grid-column` there addresses no
          // grid — which is why the lead card silently never spanned its row.
          <li key={agent.agent_id} className={lead ? "ag-cards__lead" : undefined}>
            <AgentCard agent={agent} lead={lead} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function AgentCard({ agent, lead }: { agent: AgentSummary; lead: boolean }) {
  const href = `/agents/${encodeURIComponent(agent.agent_id)}`;
  const facts = capabilityFacts(agent, lead);
  return (
    <article className={lead ? "ag-card ag-card--lead" : "ag-card"}>
      <div className="ag-card__head">
        <h3 className="ag-card__name">
          <Link to={href}>{agent.label}</Link>
        </h3>
        {lead ? (
          <span className="ag-strategy">
            <span className="ag-strategy__word">{strategyWord(agent.default_strategy)}</span>
            <span className="ag-strategy__gloss">{strategyGloss(agent.default_strategy)}</span>
          </span>
        ) : null}
        <span className="ag-card__spacer" />
        {agent.available ? (
          <span className="ne-pill ne-pill--affirm">Available</span>
        ) : (
          <span className="ne-pill ne-pill--warn">Unavailable</span>
        )}
      </div>
      {agent.description ? <p className="ag-card__desc">{agent.description}</p> : null}
      {/* THE REASON IS THE SERVER'S, VERBATIM. Nothing here summarises,
          re-words or supplies one: an invented reason is a claim about a
          governed decision the console did not make. An empty array says
          "Unavailable" and stops there. */}
      {!agent.available ? (
        <p className="ag-card__desc">
          {agent.availability_reasons.length > 0
            ? `Unavailable — ${agent.availability_reasons.join(", ")}`
            : "Unavailable"}
        </p>
      ) : null}
      {/* The plate is the WORKFLOW's identity, never the Agent's standing in
          for it. No workflow, no plate. */}
      {agent.workflow !== null ? (
        <div className="ag-card__plate">
          <span>Workflow</span>
          <span className="ne-mono">{agent.workflow.profile_id}</span>
          <span>v{agent.workflow.version}</span>
          <HashChip hash={agent.workflow.config_hash} label="Copy workflow profile hash" />
        </div>
      ) : null}
      {facts.length > 0 ? <div className="ag-card__facts">{facts}</div> : null}
      <div className="ag-card__foot">
        <span>agent v{agent.version}</span>
        {lead ? (
          <>
            {/* THE ENFORCED CEILINGS, not the declared budgets. See
                AgentWorkspace.capsNote for why the word "steps" is gone: the
                transcript band counts recorded activity rows under that word,
                and `max_steps` is not enforced as a count of anything. The token
                figure is one budget because the runtime measures it as one —
                input and output share a single ceiling. An Agent with no bounded
                agentic loop has no caps clause at all. */}
            {agent.enforced_caps === null ? null : (
              <span>
                Caps · {count(agent.enforced_caps.max_tool_calls)} tool calls ·{" "}
                {count(agent.enforced_caps.max_model_requests)} model calls ·{" "}
                {count(agent.enforced_caps.max_total_tokens)} tokens
              </span>
            )}
            <HashChip hash={agent.config_hash} label="Copy agent definition hash" />
          </>
        ) : null}
        <Link className={lead ? "ne-btn ne-btn--primary" : "ne-btn"} to={href}>
          Open workspace
        </Link>
      </div>
    </article>
  );
}

/**
 * AN EMPTY GRANT RENDERS AS NOTHING.
 *
 * The old table wrote "0 knowledge · 0 tools · 0 skills", which reads as three
 * facts and is none: a capability the Agent was never granted has no chip, the
 * same way an absent field has no row. Only a count above zero, or a value the
 * server actually served, earns a chip.
 */
function capabilityFacts(agent: AgentSummary, lead: boolean) {
  const chips = [];
  if (lead && agent.owner_label) {
    chips.push(
      <span className="ag-factchip" key="owner">
        <span>Owner</span>
        {agent.owner_label}
      </span>,
    );
  }
  if (agent.knowledge_sources.length > 0) {
    chips.push(
      <span className="ag-factchip" key="knowledge">
        <span>Knowledge</span>
        {agent.knowledge_sources.map((source) => (
          <Fragment key={source.source_id}>
            <span className="ne-mono">{source.source_id}</span>
            {source.tiers.length > 0 ? (
              <span>{source.tiers.map((tier) => tier.toUpperCase()).join(" · ")}</span>
            ) : null}
          </Fragment>
        ))}
      </span>,
    );
  }
  if (agent.tool_grants.length > 0) {
    chips.push(
      <span className="ag-factchip" key="tools">
        <span>Tools</span>
        {agent.tool_grants.length}
      </span>,
    );
  }
  for (const skill of agent.skills) {
    chips.push(
      <span className="ag-factchip" key={`skill-${skill.skill_id}`}>
        <span>Skill</span>
        <span className="ne-mono">{skill.skill_id}</span>
        <span>v{skill.version}</span>
      </span>,
    );
  }
  if (lead && agent.model_policy_id) {
    chips.push(
      <span className="ag-factchip" key="model-policy">
        <span>Model policy</span>
        <span className="ne-mono">{agent.model_policy_id}</span>
      </span>,
    );
  }
  return chips;
}

/** Thousands separators, and nothing else — the budget is the server's number,
 * not a rounded approximation of it. */
function count(value: number): string {
  return value.toLocaleString("en-US");
}
