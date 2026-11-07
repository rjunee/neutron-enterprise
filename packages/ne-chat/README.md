# ne-chat — Slack chat adapter (ChatAdapter contract)

`ne-chat` is the Slack implementation of the frozen
`ne.contracts.runtime.ChatAdapter` contract: a user talks to the Neutron
Enterprise platform agent **from Slack**, and pending HITL approvals are
**mirrored** to Slack with approve/deny buttons. Teams implements the *same*
contract in v1.1, so every Slack-specific detail lives under `ne/chat/slack/`
while the channel-agnostic services (`ConversationService`, `ChatIdentityLinker`,
`ApprovalActionService`) live at the `ne/chat/` root.

This is **distinct from the ne-connect Slack connector** (NE-045), which *reads*
channel history for ingestion. ne-chat is the *write-side conversational
surface*; it requires a **second, separate Slack app** (see Install below).

## Security model

- **Signature-verified ingress (C2/C12).** Every inbound Events API / interactivity
  request has its Slack v0 signature (`X-Slack-Signature` +
  `X-Slack-Request-Timestamp`) verified on the **raw bytes, before any parsing or
  agent activity**. Missing / forged / stale (replayed, past *or* future skew)
  requests are rejected with a **status-only HTTP 401** whose body echoes nothing.
  That rejection is **deliberately not audited** — an unauthenticated forgery must
  not be able to flood the tamper-evident audit log. Authenticated-but-unauthorized
  actions (e.g. a mapped non-approver clicking Approve) **are** audited. The signing
  secret and bot token are `SecretStr` end-to-end (masked by `config_hash`, absent
  from every error path).
- **Fail-closed identity mapping.** A Slack user is mapped to a real `Principal`
  only via an explicit, admin-configured `identity_links` table resolved through
  the frozen `PrincipalResolver`. An unmapped or unresolvable Slack user gets **no
  agent access and no approval power** — never an anonymous/ambient principal.
  Every denial is audited (`chat.identity.denied`, actor `slack:<id>`).
  Email-based auto-linking is rejected as an impersonation vector.
- **Governed agent turns (C6, no bypass).** A Slack message becomes an agent turn
  *only* through the injected governed `AgentSession` whose tools close over the
  real `GovernedToolExecutor` (guardrails → PDP → HITL → audit). ne-chat holds no
  tool handlers, no PDP, no model gateway. An unauditable turn is never made to
  look successful (fail closed).
- **Approval mirroring = view + signed submission.** Actioning a mirrored approval
  is a **signed submission onto the real NE-035 `DurableApprovalGate`** — never a
  parallel gate. The approver identity is always the **mapped `principal.id`**, and
  the gate's Tier-3 named-approver / distinct-second-check / expiry rules stay
  authoritative. The chat layer adds a fail-closed `approver_roles` pre-check
  (**empty ⇒ nobody can action approvals from Slack**). Unauthorized / unmapped
  clicks are rejected + audited (`chat.approval.unauthorized`) with the gate
  untouched.

## Install (a SECOND, separate Slack app)

1. Create the chat app: `api.slack.com/apps → Create New App → From an app
   manifest`, and paste `src/ne/chat/slack/manifest.json`. This is a **separate
   app from the ne-connect data-connector app** — it has write-side scopes only
   (`app_mentions:read`, `chat:write`, `im:history`, `im:read`) and **no**
   `channels:history` (reading history is the connector's job).
2. Point the app's **Event Subscriptions** request URL at `…/slack/events` and its
   **Interactivity** request URL at `…/slack/interactions` (your deployed ingress).
3. Configure the environment:
   - `NE_CHAT_SIGNING_SECRET` — the app's signing secret (required).
   - `NE_CHAT_BOT_TOKEN` — the bot token `xoxb-…` (required).
   - `NE_CHAT_IDENTITY_LINKS` — `{"U123": "user:ada", …}` slack-id → platform subject.
   - `NE_CHAT_APPROVER_ROLES` — e.g. `["role:approver"]`; empty ⇒ nobody can action.
   - `NE_CHAT_APPROVALS_CHANNEL` — channel for mirrored approvals (empty ⇒ disabled).

## Config reference

| Setting | Default | Meaning |
| --- | --- | --- |
| `signing_secret` | *(required)* | v0 request-signature secret (SecretStr). |
| `bot_token` | *(required)* | Bot token for chat.postMessage/update (SecretStr). |
| `api_base_url` | `https://slack.com/api` | Slack Web API base (https-pinned). |
| `api_allowed_hosts` | `("slack.com",)` | C2 egress host allowlist. |
| `signature_tolerance_s` | `300.0` | Anti-replay window (past & future), 0<x≤600. |
| `identity_links` | `{}` | slack user id → platform subject (the only v1 source). |
| `approver_roles` | `("role:approver",)` | Groups allowed to action approvals; `()` ⇒ nobody. |
| `approvals_channel` | `""` | Mirrored-approval channel; empty ⇒ mirroring off. |
| `min_update_interval_s` | `1.0` | chat.update throttle (≥ 1 msg/s/channel). |
| `event_dedup_ttl_s` / `event_dedup_max` | `300.0` / `10000` | In-process event dedup. |
| `requests_per_minute` / `max_attempts` / `retry_after_cap_seconds` / `timeout_seconds` | `50.0` / `5` / `120` / `30.0` | Web API client knobs. |

## Streaming

Responses use the portable **post-then-throttled-`chat.update`** pattern: a
placeholder is posted immediately, edited in place at most once per
`min_update_interval_s`, with an unconditional final flush carrying the answer and
any citations. Slack's newer native `chat.startStream` / `appendStream` /
`stopStream` trio (2025-10 changelog) is threads-only and newer; it is the
**v1.1 enhancement** path. The `AsyncIterator[str]` seam is real — today the
runtime yields one final chunk, but a future token-level runtime plugs in
unchanged.

## Notes / v1.1 hardening

- **Event dedup is in-process TTL memory** — a restart may re-process a retried
  Slack event. Agent turns are audited and the gate is idempotent per request;
  durable dedup is a v1.1 item.
- **`identity_links` is config-driven** (auditable via `config_hash`); a SQL-backed
  principal store is a later enhancement.

## LIVE-PARKED end-to-end (NE-000)

CI never touches live Slack — all suites replay recorded payloads over
`httpx.MockTransport` + FastAPI `TestClient`. The real-workspace e2e is
LIVE-PARKED: `packages/ne-chat/tests/test_live_slack.py` skips unless
`NE_SLACK_CHAT_LIVE=1`. To run it manually: create the chat app from the shipped
manifest, point the event + interactivity URLs at a deployed ingress, hold a
governed conversation from Slack, and action a real mirrored Tier-3 approval from
Slack as the mapped named approver, observing the streamed `chat.update` edits.

## Microsoft Teams adapter (NE-080)

The Teams adapter (`ne.chat.teams`) implements the SAME frozen `ChatAdapter`
contract behind a Bot Framework / Azure Bot surface, and reuses the SAME
channel-agnostic `ConversationService` / `ChatIdentityLinker`, so a Teams message
runs the SAME governed C6/C7/HITL turn path Slack uses (proven by
`test_teams_conversation_governed.py`'s `pdp.decide` + `tool.execute` audit
assertion). It is registered via the EXISTING `ne.plugins.chat_adapter`
entry-point (`teams = ne.chat.teams.adapter:TeamsChatAdapter`) — `ne-api`
`create_app`/`app.py` is untouched.

**Inbound.** `POST /teams/messages` validates the Bot Framework activity's
`Authorization` bearer JWT BEFORE any agent activity: `RS256` only (kills
`alg:none` / HS\* confusion before key fetch), `kid` + RSA JWK-family checks,
`aud == app_id`, `exp`/`nbf` with leeway, `iss` in `allowed_issuers`, and the
lowercase `serviceurl` claim cross-checked against the activity's `serviceUrl`.
Every failure funnels to ONE opaque `ActivityAuthError` → status-only HTTP 401
whose body echoes nothing and is DELIBERATELY UNAUDITED (an unauthenticated
forgery must not flood the tamper-evident audit log — the Slack precedent). Only
a verified + allowlisted `serviceUrl` is recorded into the in-memory
`ConversationRefs` (a serviceUrl-cache-poisoning defense); a verified activity
whose `serviceUrl` host is off the Bot Connector allowlist is acked and dropped.

**Config table (`NE_TEAMS_CHAT_*`).**

| setting | default | meaning |
| --- | --- | --- |
| `app_id` | — (required) | bot app (client) id = inbound JWT audience |
| `app_secret` | — (required, `SecretStr`) | outbound client-credentials secret (C12) |
| `tenant_id` | `botframework.com` | token-endpoint tenant (single-tenant sets its dir id) |
| `allowed_issuers` | `(https://api.botframework.com,)` | accepted `iss` (single-tenant appends its issuer) |
| `service_url_allowed_hosts` | `(smba.trafficmanager.net,)` | Bot Connector reply-host allowlist |
| `metadata_allowed_hosts` | `(login.botframework.com,)` | JWKS discovery host pin |
| `identity_links` | `{}` | explicit `{aad_object_id: platform_subject}` map |
| `jwt_leeway_s` | `300` | clock-skew leeway for `exp`/`nbf` |

**v1 limitations (deliberate).** Approval prompts are VIEW-ONLY (action from the
console or Slack; no Adaptive-Card invoke route); `ephemeral()` posts a plain
(non-ephemeral) message; proactive messaging only reaches conversations seen since
process start (in-memory refs); single-tenant issuers are supported via
`allowed_issuers`. No Bot Framework / M365 Agents SDK is embedded — the Bot
Framework SDK was archived Dec 2025 and the Agents SDK guidance delegates inbound
auth to plain JWT validation, so a thin first-party httpx client implements the
documented REST wire protocol (the NE-045 build-vs-adopt precedent).

**LIVE-PARKED: Azure Bot runbook (NE-000).** `test_live_teams.py` skips unless
`NE_TEAMS_CHAT_LIVE=1`. To run it manually: create an Azure Bot resource and
capture its App ID + secret; set `NE_TEAMS_CHAT_APP_ID` / `_APP_SECRET` /
`_IDENTITY_LINKS`; point the messaging endpoint at `…/teams/messages` on a
deployed ingress and enable the Teams channel; @mention the bot and hold a
governed conversation, verifying `pdp.decide` + `tool.execute` in the audit
trail. Parked on the NE-000 dev tenant.
