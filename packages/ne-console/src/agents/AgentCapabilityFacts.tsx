// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// WHAT THIS AGENT CAN ACTUALLY TOUCH.
//
// TWO PRODUCERS, JOINED, AND THE JOIN IS THE POINT.
//   * `AgentCatalogItemWire` is the DECLARATION: what the registry grants this
//     Agent, independent of who is looking.
//   * `AgentCapabilityPreflightWire` is the principal-effective ELIGIBILITY:
//     which of those grants this key can actually use right now, with the
//     connection and advertised contract behind each one.
// A grant that is declared but not currently eligible is the single most
// interesting row on the page, and either producer alone hides it. So the tools
// list is driven by the DECLARATION and annotated from the eligibility, never
// the other way round — driving it from preflight would silently drop exactly
// the rows worth reading.
//
// THE TABLE IS THE INDEX; THE DRAWER IS THE CONTRACT (NE-CAPTAB). A tool row
// carries only what a reader scans for — name, read/write class, approval tier.
// Clicking it slides out the tool's advertised contract: its description, every
// argument with its type, constraints and semantics, the columns it returns,
// and the governed connection it acts through. That contract is exactly what
// the model already sees in the in-band tool catalog; hiding it from the
// operator while showing it to the model is how a date-window argument gets
// guessed wrong in a live demo.
//
// HEALTH IS SHOWN ONLY WHERE A PRODUCER STATED IT. Most tools here have no
// runtime health signal at all (a governed local T3 release is admitted at
// selection, not monitored; a draft mailbox has no probe), and the projection
// truthfully says "unknown". A column reading "unknown" for most rows informs
// nobody, so the table carries no health column; the drawer and the
// Connections rows name a state only when it is `healthy`/`unhealthy`, and say
// nothing otherwise. Absence of a claim is not a claim.
//
// `capabilities === null` IS NOT AN EMPTY GRANT. api/types.ts says so at the
// field: "null means the producer could not answer; it never means an empty
// grant." Rendering "0 tools" there would be the console inventing a fact the
// server explicitly declined to state.
//
// ACCESS IS ABSENT ON PURPOSE. `deploy/agents/registry.toml` holds a real
// `[agents.visibility]` block per Agent and `AgentCatalogItemWire` has NO field
// carrying it, so "who can use this agent" cannot be answered here; SPEC's rule
// for this page is to omit unavailable data rather than render dead sections.

import { useEffect, useRef, useState } from "react";
import type {
  AgentConnectorCapabilityWire,
  AgentSkillDetailWire,
  AgentToolArgumentWire,
  AgentToolCapabilityWire,
} from "../api/types";
import { HashChip } from "../primitives/HashChip";
import { Empty, LoadError, Loading } from "../primitives/states";
import { SkillBody } from "./skillBody";
import type { AgentCapabilityPreflight, AgentDetailView } from "./types";
import type { ReadState } from "./useAgentsRead";

const ABSENT = "—";
const FOCUSABLE = 'button:not([disabled]),a[href],[tabindex]:not([tabindex="-1"])';

function Health({ health }: { health: "healthy" | "unhealthy" }) {
  return (
    <span className={`ne-cap__health ne-cap__health--${health}`} data-health={health}>
      {health}
    </span>
  );
}

function tierText(tool: AgentToolCapabilityWire): string {
  return tool.declared_tier === tool.effective_tier
    ? `T${tool.effective_tier}`
    : `T${tool.declared_tier} declared · T${tool.effective_tier} effective`;
}

/** One argument of the advertised contract. Every line renders ONLY what the
 * server stated; a `null` field simply does not appear. */
function Argument({ argument }: { argument: AgentToolArgumentWire }) {
  const facts: string[] = [];
  if (argument.type_label !== null) facts.push(argument.type_label);
  facts.push(argument.required ? "required" : "optional");
  if (argument.list_max !== null)
    facts.push(`single value or a list of up to ${argument.list_max}`);
  if (argument.max_length !== null) facts.push(`max length ${argument.max_length}`);
  return (
    <li className="ne-cap-drawer__arg">
      <p className="ne-cap-drawer__arg-head">
        <code className="ne-mono">{argument.name}</code>
        <span className="ne-cap-drawer__arg-facts">{facts.join(" · ")}</span>
      </p>
      {argument.description === null ? null : (
        <p className="ne-cap-drawer__arg-desc">{argument.description}</p>
      )}
      {argument.enum === null ? null : (
        <p className="ne-cap-drawer__arg-constraint">
          One of:{" "}
          {argument.enum.map((value, index) => (
            <span key={value}>
              {index > 0 ? " · " : null}
              <code className="ne-mono">{value}</code>
            </span>
          ))}
        </p>
      )}
      {argument.pattern === null ? null : (
        <p className="ne-cap-drawer__arg-constraint">
          Format <code className="ne-mono">{argument.pattern}</code>
        </p>
      )}
    </li>
  );
}

/** The slide-out tool contract. Modeled on EvidenceDrawer: role=dialog, focus
 * trapped, Escape closes, focus returns to the row that opened it. Read-only —
 * there is nothing here that changes an Agent. */
export function ToolContractDrawer({
  tool,
  connector,
  onClose,
}: {
  tool: AgentToolCapabilityWire;
  connector: AgentConnectorCapabilityWire | undefined;
  onClose: () => void;
}) {
  const drawer = useRef<HTMLElement>(null);
  useEffect(() => {
    drawer.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
  }, []);
  const keyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab" || !drawer.current) return;
    const nodes = [...drawer.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (!nodes.length) return;
    const first = nodes[0]!,
      last = nodes[nodes.length - 1]!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  return (
    <aside
      ref={drawer}
      className="ne-evidence-drawer ne-cap-drawer"
      role="dialog"
      aria-modal="true"
      aria-label={`${tool.label} details`}
      onKeyDown={keyDown}
    >
      <header>
        <h2>{tool.label}</h2>
        <button type="button" className="ne-btn ne-btn--quiet" onClick={onClose}>
          Close
        </button>
      </header>
      <p className="ne-mono ne-cap-drawer__name">{tool.name}</p>
      {tool.description === "" ? null : <p className="ne-cap-drawer__desc">{tool.description}</p>}
      <dl className="ne-agent-facts">
        <div>
          <dt>Read/write</dt>
          <dd>{tool.read_write_class}</dd>
        </div>
        <div>
          <dt>Approval tier</dt>
          <dd>{tierText(tool)}</dd>
        </div>
        {tool.health === "unknown" ? null : (
          <div>
            <dt>Health</dt>
            <dd>
              <Health health={tool.health} />
            </dd>
          </div>
        )}
      </dl>
      <section aria-label="Connection">
        <h3 className="ne-cap-drawer__h3">Connection</h3>
        {/* The governed connection (and so the credential) this tool acts
            through — the same identity the Connections section lists. */}
        {connector === undefined ? null : <p>{connector.label}</p>}
        <p className="ne-mono ne-cap-drawer__identity">{tool.connection_identity_label}</p>
        <p className="ne-cap__note">
          The governed connection this tool acts through — listed under Connections.
        </p>
      </section>
      {tool.arguments === null ? null : (
        <section aria-label="Arguments">
          <h3 className="ne-cap-drawer__h3">Arguments</h3>
          {tool.arguments.length === 0 ? (
            <p className="ne-cap__note">This tool declares no arguments.</p>
          ) : (
            <ul className="ne-cap-drawer__args">
              {tool.arguments.map((argument) => (
                <Argument key={argument.name} argument={argument} />
              ))}
            </ul>
          )}
        </section>
      )}
      {tool.answer_columns === null ? null : (
        <section aria-label="Returns">
          <h3 className="ne-cap-drawer__h3">Returns</h3>
          <p className="ne-cap-drawer__columns">
            {tool.answer_columns.map((column, index) => (
              <span key={column}>
                {index > 0 ? " · " : null}
                <code className="ne-mono">{column}</code>
              </span>
            ))}
          </p>
        </section>
      )}
    </aside>
  );
}

/** The slide-out Skill viewer — same dialog pattern as the tool drawer. Shows
 * the pinned version + content hash (and what the hash guarantees), the skill's
 * own access boundary (required tools + ACL), and the authored procedure body
 * rendered as real headings and lists. Read-only. */
export function SkillContractDrawer({
  skill,
  onClose,
}: {
  skill: AgentSkillDetailWire;
  onClose: () => void;
}) {
  const drawer = useRef<HTMLElement>(null);
  useEffect(() => {
    drawer.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
  }, []);
  const keyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab" || !drawer.current) return;
    const nodes = [...drawer.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (!nodes.length) return;
    const first = nodes[0]!,
      last = nodes[nodes.length - 1]!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  return (
    <aside
      ref={drawer}
      className="ne-evidence-drawer ne-cap-drawer"
      role="dialog"
      aria-modal="true"
      aria-label={`${skill.skill_id} skill details`}
      onKeyDown={keyDown}
    >
      <header>
        <h2 className="ne-mono">{skill.skill_id}</h2>
        <button type="button" className="ne-btn ne-btn--quiet" onClick={onClose}>
          Close
        </button>
      </header>
      {skill.description === "" ? null : <p className="ne-cap-drawer__desc">{skill.description}</p>}
      <dl className="ne-agent-facts">
        <div>
          <dt>Version</dt>
          <dd>{skill.version}</dd>
        </div>
        {skill.category === "" ? null : (
          <div>
            <dt>Category</dt>
            <dd>{skill.category}</dd>
          </div>
        )}
        <div>
          <dt>Content hash</dt>
          <dd>
            <HashChip hash={skill.content_hash} label={`Copy ${skill.skill_id} content hash`} />
          </dd>
        </div>
      </dl>
      <p className="ne-cap__note">
        The agent activates this skill only against this exact content hash — if the file changes,
        the pin breaks and the skill does not load.
      </p>
      <section aria-label="Access boundary">
        <h3 className="ne-cap-drawer__h3">Access boundary</h3>
        {skill.acl_labels.length === 0 ? (
          <p className="ne-cap__note">No ACL label — visible to every authenticated principal.</p>
        ) : (
          <p className="ne-cap-drawer__columns">
            {skill.acl_labels.map((label, index) => (
              <span key={label}>
                {index > 0 ? " · " : null}
                <code className="ne-mono">{label}</code>
              </span>
            ))}
          </p>
        )}
        {skill.requires_tools.length === 0 ? null : (
          <>
            <p className="ne-cap-drawer__arg-facts">Requires tools</p>
            <ul className="ne-cap-drawer__args ne-cap-skill__tools">
              {skill.requires_tools.map((tool) => (
                <li key={tool}>
                  <code className="ne-mono">{tool}</code>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
      {skill.triggers.length === 0 ? null : (
        <section aria-label="Triggers">
          <h3 className="ne-cap-drawer__h3">Triggers</h3>
          <ul className="ne-cap-drawer__args ne-cap-skill__triggers">
            {skill.triggers.map((trigger) => (
              <li key={trigger}>{trigger}</li>
            ))}
          </ul>
        </section>
      )}
      <section aria-label="Procedure">
        <h3 className="ne-cap-drawer__h3">Procedure</h3>
        <SkillBody body={skill.body} />
      </section>
    </aside>
  );
}

export function AgentCapabilityFacts({
  agent,
  preflight,
  onRetryPreflight,
}: {
  agent: AgentDetailView;
  preflight: ReadState<AgentCapabilityPreflight>;
  onRetryPreflight: () => void;
}) {
  const eligible = preflight.phase === "ready" ? preflight.value.capabilities : null;
  const eligibleTools = eligible?.tools ?? [];
  const connectors = eligible?.connectors ?? [];
  const skillDetail = preflight.phase === "ready" ? preflight.value.skills : null;
  // One dialog at a time: a tool name OR a skill id, never both.
  const [openTool, setOpenTool] = useState<string | null>(null);
  const [openSkill, setOpenSkill] = useState<string | null>(null);
  const openTrigger = useRef<HTMLButtonElement | null>(null);
  const shown = eligibleTools.find((tool) => tool.name === openTool);
  const shownSkill = skillDetail?.find((skill) => skill.skill_id === openSkill);
  const close = () => {
    setOpenTool(null);
    setOpenSkill(null);
    queueMicrotask(() => openTrigger.current?.focus());
  };

  return (
    <div className="ne-cap">
      <section aria-labelledby="cap-tools">
        <h2 id="cap-tools" className="ne-cap__h2">
          Tools
        </h2>
        {preflight.phase === "loading" ? <Loading things="eligible capabilities" /> : null}
        {preflight.phase === "error" ? (
          <LoadError things="eligible capabilities" onRetry={onRetryPreflight} />
        ) : null}
        {preflight.phase === "unavailable" ? (
          <Empty message="Eligible capabilities aren’t available." />
        ) : null}
        {preflight.phase === "ready" && eligible === null ? (
          <p className="ne-cap__note" role="status">
            The server could not answer which of these grants are eligible for you. That is not the
            same as none of them being eligible.
          </p>
        ) : null}
        {agent.tool_grants.length === 0 ? (
          <p className="ne-cap__note">This Agent is granted no Tools.</p>
        ) : (
          <>
            {eligibleTools.length > 0 ? (
              <p className="ne-cap__note">Select a tool for its description and arguments.</p>
            ) : null}
            <table className="ne-cap__table">
              <thead>
                <tr>
                  <th scope="col">Tool</th>
                  {/* NOT "Access": this is the tool's read/write class. The word
                      "Access" on this page would read as "who may use this
                      Agent", which is precisely the thing the wire does not
                      carry and this page therefore does not answer. */}
                  <th scope="col">Read/write</th>
                  <th scope="col">Tier</th>
                </tr>
              </thead>
              <tbody>
                {agent.tool_grants.map((name) => {
                  const facts = eligibleTools.find((tool) => tool.name === name);
                  if (facts === undefined) {
                    return (
                      <tr key={name} data-eligible="false">
                        <th scope="row">
                          <span className="ne-mono">{name}</span>
                        </th>
                        <td colSpan={2} className="ne-cap__ineligible">
                          {preflight.phase === "ready" && eligible !== null
                            ? "Granted, not currently eligible for you"
                            : ABSENT}
                        </td>
                      </tr>
                    );
                  }
                  return (
                    <tr key={name} data-eligible="true">
                      <th scope="row">
                        <button
                          type="button"
                          className="ne-cap__tool-open"
                          aria-haspopup="dialog"
                          aria-expanded={openTool === name}
                          onClick={(event) => {
                            openTrigger.current = event.currentTarget;
                            setOpenTool(name);
                          }}
                        >
                          {facts.label}
                          <small className="ne-mono">{name}</small>
                        </button>
                      </th>
                      <td>{facts.read_write_class}</td>
                      <td>{tierText(facts)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        )}
      </section>

      {shown === undefined ? null : (
        <ToolContractDrawer
          tool={shown}
          connector={connectors.find(
            (connector) => connector.connection_identity_label === shown.connection_identity_label,
          )}
          onClose={close}
        />
      )}

      {shownSkill === undefined ? null : <SkillContractDrawer skill={shownSkill} onClose={close} />}

      {eligible === null || eligible.connectors.length === 0 ? null : (
        <section aria-labelledby="cap-connections">
          <h2 id="cap-connections" className="ne-cap__h2">
            Connections
          </h2>
          <table className="ne-cap__table">
            <thead>
              <tr>
                <th scope="col">Connection</th>
                <th scope="col">Kind</th>
                <th scope="col">Credential owner</th>
                <th scope="col">Source</th>
              </tr>
            </thead>
            <tbody>
              {eligible.connectors.map((connector) => (
                <tr key={`${connector.connection_identity_label}:${connector.source_id}`}>
                  <th scope="row">
                    {connector.label}
                    {/* Health appears ONLY when a producer stated one. */}
                    {connector.health === "unknown" ? null : <Health health={connector.health} />}
                    <small className="ne-mono ne-cap__identity">
                      {connector.connection_identity_label}
                    </small>
                  </th>
                  <td>{connector.kind}</td>
                  <td>{connector.credential_owner_label}</td>
                  <td>
                    <span className="ne-mono">{connector.source_id}</span>
                    {connector.tiers.map((tier) => (
                      <span key={tier} className="ne-cap__tier">
                        {tier}
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section aria-labelledby="cap-knowledge">
        <h2 id="cap-knowledge" className="ne-cap__h2">
          Knowledge sources
        </h2>
        {agent.knowledge_sources.length === 0 ? (
          <p className="ne-cap__note">This Agent is granted no knowledge sources.</p>
        ) : (
          <table className="ne-cap__table">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Tiers</th>
              </tr>
            </thead>
            <tbody>
              {agent.knowledge_sources.map((source) => (
                <tr key={source.source_id}>
                  <th scope="row">
                    <span className="ne-mono">{source.source_id}</span>
                  </th>
                  <td>
                    {source.tiers.map((tier) => (
                      <span key={tier} className="ne-cap__tier">
                        {tier}
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section aria-labelledby="cap-skills">
        <h2 id="cap-skills" className="ne-cap__h2">
          Skills
        </h2>
        {agent.skills.length === 0 ? (
          <p className="ne-cap__note">This Agent is granted no Skills.</p>
        ) : (
          <>
            {skillDetail !== null && skillDetail.length > 0 ? (
              <p className="ne-cap__note">Select a skill to read its procedure.</p>
            ) : null}
            <table className="ne-cap__table">
              <thead>
                <tr>
                  <th scope="col">Skill</th>
                  <th scope="col">Version</th>
                  <th scope="col">Content hash</th>
                </tr>
              </thead>
              <tbody>
                {agent.skills.map((skill) => {
                  // The Skill body is principal-scoped detail from preflight; the
                  // pin (id/version/hash) is always here from the catalog. A skill
                  // is only openable when its detail is visible to this reader.
                  const detail = skillDetail?.find((row) => row.skill_id === skill.skill_id);
                  return (
                    <tr key={`${skill.skill_id}:${skill.version}`}>
                      <th scope="row">
                        {detail === undefined ? (
                          <span className="ne-mono">{skill.skill_id}</span>
                        ) : (
                          <button
                            type="button"
                            className="ne-cap__tool-open"
                            aria-haspopup="dialog"
                            aria-expanded={openSkill === skill.skill_id}
                            onClick={(event) => {
                              openTrigger.current = event.currentTarget;
                              setOpenSkill(skill.skill_id);
                            }}
                          >
                            <span className="ne-mono">{skill.skill_id}</span>
                          </button>
                        )}
                      </th>
                      <td>{skill.version}</td>
                      <td>
                        <HashChip
                          hash={skill.content_hash}
                          label={`Copy ${skill.skill_id} content hash`}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        )}
      </section>

      <div className="ne-cap__pair">
        <section aria-labelledby="cap-budgets">
          <h2 id="cap-budgets" className="ne-cap__h2">
            Budgets
          </h2>
          <dl className="ne-agent-facts ne-cap__facts">
            <div>
              <dt>Steps</dt>
              <dd>{agent.budgets.max_steps.toLocaleString("en-US")}</dd>
            </div>
            <div>
              <dt>Tool calls</dt>
              <dd>{agent.budgets.max_tool_calls.toLocaleString("en-US")}</dd>
            </div>
            <div>
              <dt>Input tokens</dt>
              <dd>{agent.budgets.max_input_tokens.toLocaleString("en-US")}</dd>
            </div>
            <div>
              <dt>Output tokens</dt>
              <dd>{agent.budgets.max_output_tokens.toLocaleString("en-US")}</dd>
            </div>
          </dl>
        </section>

        <section aria-labelledby="cap-provenance">
          <h2 id="cap-provenance" className="ne-cap__h2">
            Configuration provenance
          </h2>
          <dl className="ne-agent-facts ne-cap__facts">
            <div>
              <dt>Agent version</dt>
              <dd>{agent.version}</dd>
            </div>
            <div>
              <dt>Definition hash</dt>
              <dd>
                <HashChip hash={agent.config_hash} label="Copy Agent definition hash" />
              </dd>
            </div>
            <div>
              <dt>Model policy</dt>
              <dd className="ne-mono">{agent.model_policy_id}</dd>
            </div>
            {agent.workflow === null ? null : (
              <>
                <div>
                  <dt>Workflow profile</dt>
                  <dd className="ne-mono">{agent.workflow.profile_id}</dd>
                </div>
                <div>
                  <dt>Profile version</dt>
                  <dd>{agent.workflow.version}</dd>
                </div>
                <div>
                  <dt>Profile hash</dt>
                  <dd>
                    <HashChip
                      hash={agent.workflow.config_hash}
                      label="Copy Workflow profile hash"
                    />
                  </dd>
                </div>
              </>
            )}
          </dl>
        </section>
      </div>
    </div>
  );
}
