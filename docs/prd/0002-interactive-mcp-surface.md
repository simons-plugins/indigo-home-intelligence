# PRD-0002 — Interactive MCP Surface for Home Intelligence

**Status:** Proposed
**Date:** 2026-04-23
**Driver:** Simon Clark (also primary user)
**Audience:** Plugin author + future contributors + other Indigo operators considering adopting the plugin

---

## 1. Executive summary

The Home Intelligence plugin currently produces one output: a weekly
curated digest email, generated via direct Anthropic API call, costing
~£0.39 per run. This PRD proposes adding an MCP (Model Context
Protocol) endpoint to the plugin, exposing its history-aware reasoning
context and safe rule-management surface to MCP clients like Claude
Desktop and Claude Code.

Consequence: the same intelligent reasoning becomes available
on-demand mid-week, at zero marginal cost (covered by the user's
Claude subscription), and optionally drives the weekly cadence too if
the user has Claude Desktop/Code scheduling available.

The plugin keeps the weekly-email path as an autonomous API-backed
fallback for users without a subscription, without duplicated
reasoning logic: both paths share the same data-access methods,
safety allowlist, rule schema, and confirmation-email templates.

---

## 2. Problem statement

### 2.1 The gap

Today, the plugin's value is delivered ONLY through a scheduled weekly
email. If Simon (or any user) wants to ask "what changed since last
week?" on Wednesday, they have to:

- Wait for Sunday, or
- Dig through Indigo's event log manually, or
- Open Claude Code / Claude Desktop with the existing
  `mlamoure/indigo-mcp-server` (which exposes current state + basic
  history but NOT the curated SQL Logger rollups, fleet health, or
  observation/rule audit the plugin assembles)

### 2.2 The cost shape

Claude pricing changed: users without paid API access but with a
Claude subscription (Pro, Max, or Code) cannot drive automation via
the API. The weekly email costs real money per run. For single-user
home automation, that's £20/year — tolerable but not free.

Meanwhile, the user's subscription already covers generous interactive
use. If the plugin's reasoning were reachable via MCP, every mid-week
question would be free.

### 2.3 The distribution story

Other Indigo operators with Claude subscriptions can't currently adopt
the plugin without also budgeting for Anthropic API usage. An MCP path
removes that barrier.

---

## 3. Goals

### 3.1 Must-haves (v1)

- **G1.** Expose the plugin's history-aware reasoning data via MCP
  tools accessible from Claude Desktop and Claude Code.
- **G2.** Enable interactive Q&A about the house's past and present
  using the user's Claude subscription only (zero API spend).
- **G3.** Expose safe write tools (`add_rule`, `update_rule`) with the
  same server-side safety allowlist the email-YES path uses.
- **G4.** Share 100% of the data-access code between the weekly digest
  and the MCP surface (no duplication).
- **G5.** Keep the weekly email path fully functional for users
  without a subscription.
- **G6.** Distributable as a standard Indigo plugin with a README
  snippet of Claude Desktop / Claude Code config.

### 3.2 Should-haves (v2+)

- **G7.** Schedule weekly digest via Claude Code's scheduled-agent
  feature (if available and reliable) to eliminate API cost for
  subscription-holding users.
- **G8.** Ad-hoc digest scheduling ("remind me about the dining TRV
  in 2 weeks") via an MCP tool.
- **G9.** Cross-week pattern detection tools.

### 3.3 Non-goals

- **NG1.** Re-implementing device control already exposed by
  `mlamoure/indigo-mcp-server`. Users will install both.
- **NG2.** Building a web dashboard UI. Indigo has one; Claude Desktop
  is the chat UI; redundant effort.
- **NG3.** Supporting MCP clients other than Claude Desktop / Claude
  Code in v1. (Others will work if spec-compliant, but not tested.)
- **NG4.** Multi-user / multi-tenant authentication. This is a
  single-user home plugin.
- **NG5.** Real-time streaming / subscriptions. MCP tools are
  request-response; fine for this use case.

---

## 4. Users and scenarios

### 4.1 Personas

- **Primary: Simon** — has Claude Code subscription; Indigo power user;
  values mid-week interrogation; wants to remove API spend where
  possible.
- **Secondary: Other Indigo operators with Claude subscriptions** —
  want house-intelligence capabilities without API billing overhead.
- **Tertiary: API-only users** — no subscription, just want weekly
  email. Existing path must keep working.

### 4.2 User stories

| # | Story | Tier |
|---|---|---|
| U1 | "As Simon, I want to ask Claude Desktop 'what happened with the dining TRV this week' and get a narrative answer using my subscription." | v1 |
| U2 | "As Simon, I want Claude to propose a rule based on a mid-week observation and write it to my rule store with the same safety constraints the weekly email uses." | v1 |
| U3 | "As Simon, I want to see which rules have auto-disabled and why, without digging into the rule variable JSON." | v1 |
| U4 | "As an Indigo operator with a Claude subscription, I want to install this plugin and add one config entry to Claude Desktop to start chatting with my house." | v1 |
| U5 | "As Simon, I want my weekly summary to run automatically via Claude Code scheduling (free) instead of the API (paid)." | v2 |
| U6 | "As Simon, I want to say 'remind me about the heating in 2 weeks' and have a focused digest auto-fire then." | v2 |
| U7 | "As an API-only user, I don't have to do anything — my weekly email keeps working." | v1 |

---

## 5. Success metrics

### 5.1 Quantitative

- **M1.** Zero API calls for mid-week interrogation tasks (100% of
  interactive queries should run via subscription).
- **M2.** No regression in weekly email quality (same narrative shape,
  same observation/rule proposal quality, same ~£0.39/run cost).
- **M3.** ≤ 1 Indigo plugin install step for new users; ≤ 1 config edit
  on the MCP-client side.
- **M4.** Shared code coverage: every data-access method used by the
  digest is also exposed via MCP, no duplicated reasoning logic.

### 5.2 Qualitative

- **Q1.** Simon uses Claude Desktop to interrogate the house at least
  once between Sunday digests within two weeks of shipping.
- **Q2.** At least one other Indigo user adopts the plugin + MCP
  within 3 months.
- **Q3.** No safety incidents: no rule is written that bypasses the
  allowlist (thermostats / security / locks / sensors).

---

## 6. Proposed solution

### 6.1 Overview

Add a second delivery surface to the plugin: an MCP server at
`/message/com.simons-plugins.home-intelligence/mcp` hosting 7-8 tools
and 1 resource, reachable from Claude Desktop and Claude Code.

The weekly digest path is unchanged structurally but internally
refactored to share data-access methods with the MCP tools. The
plugin's INSTRUCTIONS text — the accumulated wisdom of how to reason
about an Indigo house — becomes an MCP resource that interactive
Claude can opt into when the user asks for digest-style output.

### 6.2 Architecture

```
        ┌─────────────────────────────────────────────────┐
        │                 MCP clients                      │
        │  - Claude Desktop (primary — chat UX)            │
        │  - Claude Code (power-user terminal)             │
        │  - Scheduled routines (v2 — weekly cadence)      │
        └──────────────┬──────────────────────────────────┘
                       │
          ┌────────────┼────────────────┐
          │                              │
          ▼                              ▼
   ┌──────────────┐            ┌────────────────────┐
   │ mlamoure's   │            │ THIS PLUGIN's       │
   │ Indigo MCP   │            │ MCP surface         │
   │ (existing)   │            │ (NEW — v1 scope)    │
   │              │            │                     │
   │ - devices    │            │ Data:               │
   │ - variables  │            │  query_sql_logger   │
   │ - actions    │            │  house_context_*    │
   │ - event log  │            │  get_observations   │
   │ - thermostat │            │  get_rules          │
   │ - dimmer/    │            │                     │
   │   relay      │            │ Agent:              │
   │   control    │            │  propose_rule       │
   │              │            │  add_rule           │
   │              │            │  update_rule        │
   │              │            │                     │
   │              │            │ Resource:           │
   │              │            │  digest_instructions│
   └──────────────┘            └──────────┬─────────┘
                                          │
                                          │ shared internal
                                          │ Python methods
                                          ▼
                               ┌─────────────────────┐
                               │  Plugin runtime     │
                               │                     │
                               │  - DigestRunner     │
                               │    (weekly email,    │
                               │     API fallback)   │
                               │  - rule_evaluator   │
                               │  - rule_store       │
                               │  - observation_store│
                               │  - history_db (PG)  │
                               │  - event_log_reader │
                               └──────────┬──────────┘
                                          │
                                          ▼
                               ┌─────────────────────┐
                               │ Indigo server       │
                               │ + SQL Logger        │
                               │ + event log files   │
                               └─────────────────────┘
```

Orthogonal concerns:

- **Who reasons?** Claude Desktop (subscription, free) OR plugin's
  built-in Anthropic client (API, paid). User choice per deployment.
- **What schedules?** Plugin's cron (v1) OR external scheduler like
  Claude Code scheduled routines (v2).
- **Where does the answer land?** Email (weekly) OR chat (interactive).

All combinations work because the reasoning substrate is shared.

### 6.3 Three UIs, one engine

```
     Digest email          Interactive chat        Push/mobile (future)
         │                       │                       │
         │ (API or Code CLI)     │ (subscription)        │ (TBD)
         │                       │                       │
         └───────────┬───────────┴───────────┬──────────┘
                     │                       │
                     ▼                       ▼
               ┌─────────────────────────────────┐
               │ Shared reasoning substrate       │
               │  INSTRUCTIONS + context + schema │
               │  + safety allowlist + write path │
               └──────────────────────────────────┘
```

The weekly digest is ONE format choice over the shared substrate.
Interactive chat is another. Any future channel (push alerts, mobile)
would be a third.

---

## 7. Detailed design

### 7.1 MCP tool surface

All tools are exposed at `/message/com.simons-plugins.home-intelligence/mcp`
via MCP-over-HTTP+SSE. Authentication: Indigo Reflector's existing
auth layer (no additional HMAC).

#### 7.1.1 Data access

**`query_sql_logger(device_id, column, start_ts, end_ts, bucket_seconds=None)`**
- Generic SQL Logger read for any device + column
- Returns time-bucketed or raw points
- Differentiator: `mlamoure/indigo-mcp-server` uses Indigo's built-in
  history DB, not SQL Logger. This tool is the core value-add.

**`house_context_snapshot(days=7)`**
- Returns the curated JSON the weekly digest reasons over:
  - devices (filtered to real, excluding mirrors/virtual/sub-widgets)
  - device_folders
  - indigo_triggers (enriched with action lists, auto-disabled flagged)
  - indigo_schedules
  - action_groups
  - event_log_summary (top sources, hourly distribution, SQL rollups,
    health block, energy block)
  - existing rules (active + auto-disabled)
  - recent observations (last 60 days)
- Heavy tool — marked "expensive, use sparingly" in description
- Saves 15+ tool calls + client-side assembly

**`get_observations(status_filter="all", days_back=60)`**
- Parsed observation store content
- Filter: all / pending / accepted / declined / snoozed / ignored /
  rejected_unsafe_target
- Saves Claude from parsing the 40-60KB JSON blob

**`get_rules(include_disabled=False)`**
- Parsed rule store with activity metadata (fires_count, last_fired_at,
  auto_disabled reason/timestamp)

#### 7.1.2 Agent write tools

**`propose_rule(rule)`**
- Validates schema (same `_validate_parsed` used by digest)
- Does NOT write; returns validation result + safety-allowlist check
- Lets Claude show the user what a rule WOULD look like before asking
  for approval

**`add_rule(rule, from_observation_id=None)`**
- Runs `_is_safe_rule_target` gate (refuses thermostats / security /
  locks / sensors)
- Writes to rule store
- Triggers the templated confirmation email (same code path as
  email-YES flow)
- If `from_observation_id` provided, updates observation's
  user_response to "yes" + rule_id

**`update_rule(rule_id, action="disable"|"enable"|"delete")`**
- Mirror of the plugin's Manage Rule... menu behaviour
- Enabling clears auto_disabled metadata (resets failure counter)
- Deleting removes from store; observation's rule_id is invalidated

#### 7.1.3 Resource

**`@home-intelligence:digest_instructions`**
- The plugin's INSTRUCTIONS text (the accumulated reasoning guide)
- Fetched when user asks for digest-style output in chat
- Keeps the reasoning quality consistent across email and chat surfaces

#### 7.1.4 Deferred to v2

- `schedule_ad_hoc_digest(when, topic_hint)` — queue a one-off digest
- `compare_weeks(weeks_back)` — formal WoW summary
- `query_observations_for_devices(device_ids)` — "have I flagged these
  before?" pattern

### 7.2 Shared internal methods

The plugin refactors such that every MCP tool is a thin wrapper around
a method also used by `DigestRunner`:

```python
# Internal Python methods (plugin-private, called by both digest + MCP)
class HistoryAccess:
    def house_context_snapshot(days=7) -> dict
    def event_log_window(days=7) -> List[dict]
    def sql_logger_rollups() -> dict
    def fleet_health() -> dict
    def energy_rollups_14d(device_ids=None) -> dict

class RuleAccess:
    def list_rules(include_disabled=False) -> List[dict]
    def propose_rule(rule) -> ProposeResult
    def add_rule_safely(rule, from_observation_id=None) -> AddResult
    def update_rule(rule_id, action) -> bool

class ObservationAccess:
    def list_observations(filter, days_back) -> List[dict]
```

Weekly digest calls these directly. MCP tools call these after payload
validation. Zero duplication.

### 7.3 Authentication

**Local LAN access (Claude Desktop on same network):**
- Hits `http://jarvis.local:8176/message/...`
- Private network trust; no auth required at the MCP layer
- IWS listens on port 8176 by Indigo default

**Remote access (Claude Desktop away from home):**
- Via Indigo Reflector: `https://<reflector>.indigodomo.net/message/...`
- Reflector's existing auth layer (basic / bearer / config-dependent)
  is the security boundary
- MCP layer does not add HMAC — it would be redundant and add
  configuration burden

**Per-tool authorization:**
- All tools share the same auth (user == authenticated reflector user)
- Write tools (`add_rule`, `update_rule`) still go through the safety
  allowlist server-side regardless of caller

### 7.4 Removed code

Landing this lets us delete vestigial code:

- `handle_feedback` IWS endpoint (never called externally after
  ADR-0002 scrapped the Cloudflare Email Workers path)
- HMAC verification logic in `delivery.py` for inbound path
- `internalHmacSecret` pluginPref (outbound /email-out still uses it
  if we ever wire that up, but not required for MCP)
- `/feedback` route in `Actions.xml`

Net: -60 LOC, simpler plugin.

### 7.5 Security & threat model

| Threat | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Unauthorized MCP caller adds malicious rule | Low | High | Reflector auth + `_is_safe_rule_target` allowlist |
| Prompt injection via device name in context snapshot | Very low | Low | Context is JSON, not executed; Claude's own refusal |
| Token exhaustion via repeated `house_context_snapshot` calls | Low | Low | Tool description marks expensive; rate limit if needed |
| LLM proposes harmful rule; user blindly accepts in chat | Low | High | Same safety allowlist + confirmation email (audit trail) |
| SQL injection via `query_sql_logger` params | Low | High | Server-side validation: device_id int, column from allowlist |

### 7.6 Performance considerations

- `house_context_snapshot` on Simon's 1113-device house = ~50-80KB
  JSON response, ~20k Claude context tokens. Acceptable for occasional
  use. Bigger houses would want pagination or field-selection params.
- `query_sql_logger` with no bucketing can return thousands of points;
  tool description should guide Claude toward reasonable bucket sizes.
- Weekly digest latency unchanged (same API call).
- Interactive MCP latency: first call ~200-500ms (MCP handshake, tool
  discovery); subsequent tool calls ~50-200ms (local HTTP).

### 7.7 Versioning

The tool surface is treated as a public API. Changes follow semver:
- Patch: bug fix, no surface change
- Minor: add tool, extend optional params (backward compatible)
- Major: rename/remove tool, change required params (breaking)

Version exposed via MCP `serverInfo.version` so clients can skip
incompatible releases.

---

## 8. UX / User journeys

### 8.1 New user: install + config

1. Download `Home Intelligence.indigoPlugin.zip`, double-click → Indigo
2. In Plugin Configure: set Claude API key (optional — only needed for
   weekly email path) + SMTP (optional — only needed for emails)
3. Open `~/Library/Application Support/Claude/claude_desktop_config.json`
4. Add snippet (provided in README):
   ```json
   {
     "mcpServers": {
       "home-intelligence": {
         "url": "https://<your-reflector>.indigodomo.net/message/com.simons-plugins.home-intelligence/mcp"
       }
     }
   }
   ```
5. Restart Claude Desktop
6. Ask: "what's happening in my Indigo house right now?"

### 8.2 Simon mid-week: interrogate

> Simon opens Claude Desktop on his laptop Wednesday evening:
> 
> **Simon:** "Has the dining TRV been misbehaving this week?"
> 
> **Claude:** [calls `query_sql_logger` for dining TRV + `event_log_narrative`
> via mlamoure for recent activity]  
> "The Online Dining TRV sensor dropped offline 14 times this week, up
> from 9 last week. The longest outage was 47 minutes on Tuesday at
> 14:32. The pattern looks consistent with degraded Wi-Fi. Would you
> like me to add a rule that flags this in next week's digest?"
> 
> **Simon:** "Yes, but use the heating controller plug for the auto-off
> if it stops reporting for 30+ minutes"
> 
> **Claude:** [calls `propose_rule` → validates → calls `add_rule`
> after Simon confirms]  
> "Done. Rule `7a3b2f` is now active. Confirmation email sent."

Zero API cost. Full history-aware reasoning. Rule written safely with
the same allowlist the email flow uses.

### 8.3 Sunday digest: existing flow unchanged

> Sunday 18:00: plugin's scheduler fires. DigestRunner.run() executes,
> calls Anthropic API (£0.39), emails Simon. Same as today.

Optionally in v2: if Claude Code is installed, plugin detects it and
prefers Code-CLI generator over API (free). Falls back to API on
failure.

### 8.4 Other operator onboarding

> Alex (another Indigo user) has Claude Pro subscription. No Anthropic
> API key. They install the plugin, skip the API/SMTP config, add the
> MCP snippet to Claude Desktop. They never use the weekly email path.
> Their entire Home Intelligence experience is via Claude Desktop chat.

---

## 9. Architecture Decision Records

### 9.1 Decisions — ANSWERED

#### ADR-M1: MCP is the transport, not a Claude Code skill-only approach

**Status:** Accepted  
**Context:** Skills are Claude-Code-specific markdown files. MCP tools
work across Claude Desktop, Claude Code, and any future MCP-compatible
client. User specifically wants Claude Desktop support.  
**Decision:** MCP over HTTP+SSE hosted by the plugin.  
**Consequence:** Wider reach; skill-shaped packaging becomes an
optional convenience layer on top, not the primary surface.

#### ADR-M2: Two MCP servers side-by-side (HI plugin + mlamoure), not a fork

**Status:** Accepted  
**Context:** `mlamoure/indigo-mcp-server` already provides device
control, state, general event log. Forking adds maintenance burden.  
**Decision:** Ship a separate MCP in the HI plugin; document the
combined install.  
**Consequence:** Users add two `mcpServers` entries. Independent
release cadences. Narrower HI tool surface.

#### ADR-M3: No HMAC on the MCP endpoint

**Status:** Accepted  
**Context:** HMAC in the plugin protected the Worker-to-plugin path
that was scrapped by ADR-0002. Claude Desktop → plugin goes via either
LAN (private network) or Indigo Reflector (which has its own auth).  
**Decision:** Rely on reflector auth for remote, implicit LAN trust
for local. No additional HMAC at the MCP layer.  
**Consequence:** Simpler setup; one fewer config field. `/feedback`
HMAC logic becomes vestigial and is removed.

#### ADR-M4: Remove vestigial `/feedback` IWS endpoint + HMAC

**Status:** Accepted  
**Context:** Never called externally after ADR-0002 scrapped the
Cloudflare Email Workers path. Inbox poller uses in-process callback
directly.  
**Decision:** Delete `handle_feedback`, `internalHmacSecret`, and
associated verification code.  
**Consequence:** -60 LOC. Remove from `Actions.xml`. Simpler security
story.

#### ADR-M5: Minimal MCP tool surface (7-8 tools + 1 resource)

**Status:** Accepted  
**Context:** Early tool list had 11+ tools including `fleet_health`
and `event_log_narrative` that Claude can derive from
mlamoure's existing tools. Adding them would duplicate surface without
adding value.  
**Decision:** Keep ONLY tools that:
1. Access data mlamoure doesn't (SQL Logger, rollups)
2. Parse plugin-specific variables (observations, rules)
3. Enforce safety gates (add_rule with allowlist)
4. Provide genuine shortcuts for expensive client-side assembly
   (`house_context_snapshot`)  
**Consequence:** Leaner plugin; Claude reasons over mlamoure's tools
for standard state/control; our tools fill specific gaps.

#### ADR-M6: Expose INSTRUCTIONS as MCP resource

**Status:** Accepted  
**Context:** INSTRUCTIONS is 18 PR cycles of reasoning-about-Indigo
wisdom. Valuable in interactive too. Hardcoding into both digest and
skill paths creates drift.  
**Decision:** Plugin hosts INSTRUCTIONS at MCP resource URI
`home-intelligence:digest_instructions`. Both digest (via internal
import) and interactive (via resource fetch) consume the same source.  
**Consequence:** Single source of truth for reasoning guidance. Edits
apply to both surfaces. Interactive can opt into digest-style output
when user asks.

#### ADR-M7: Safety allowlist runs server-side on all write paths

**Status:** Accepted  
**Context:** Email-YES path goes through `_is_safe_rule_target`. MCP
write path must match — LLM output can't be trusted to self-police.  
**Decision:** `add_rule` MCP tool calls the same allowlist check
before writing. Rejection emails a notification (if SMTP configured).  
**Consequence:** No rule can be written via any path that targets a
thermostat / security device / lock / sensor-without-power-surface.

#### ADR-M8: Weekly email becomes optional

**Status:** Accepted  
**Context:** Users with subscription + Claude Desktop can trigger
digest-style analysis on-demand and never need the scheduled email.
Users without subscription still need it.  
**Decision:** Add `enableWeeklyEmail` pluginPref (default: true for
backward compatibility; new installs can set false).  
**Consequence:** New users can skip SMTP config entirely. Existing
users' scheduled emails continue unchanged.

#### ADR-M9: Observation store is read-only from MCP in v1

**Status:** Accepted  
**Context:** Observations exist as a persistence bridge for the
email-async-approval flow. In chat, approval is synchronous — no
persistence gap to bridge. Writing observations from MCP would
duplicate digest behaviour without clear benefit.  
**Decision:** MCP tools can READ observations (for "have I flagged
this before?" context) but not WRITE new ones. Rules written via MCP
set `from_observation_id=None`.  
**Consequence:** Simpler v1 scope. If users want a "log this agent
proposal" feature later, we can add `add_observation` tool in v2.

### 9.2 Decisions — OPEN

#### ADR-M10: How reliable is Claude Code scheduled-agent for weekly cadence?

**Status:** Open — needs investigation  
**Context:** User noted Claude Desktop / Code can schedule now. If
reliable, Claude-subscription path can fully replace API path for
weekly cadence. Questions:
- Does scheduling persist across laptop sleep / reboot?
- Does it require specific Claude product tier?
- Does it fire reliably at the scheduled time (minute-accurate)?
- What happens if user is offline / subscription expires?
- How does it deliver output (email via MCP tool? chat log?)?  
**Decision needed by:** v2 planning  
**If YES, reliable:** Plugin's weekly scheduler can be deprecated or
made optional. API path becomes a last-resort fallback. Cost → £0.  
**If NO, unreliable:** Plugin's scheduler stays primary. Code
scheduling is a nice-to-have optional generator.

#### ADR-M11: Should `house_context_snapshot` support pagination / field selection?

**Status:** Open  
**Context:** On Simon's 1113-device house the snapshot is ~50-80KB.
Acceptable for occasional use but blows context window if called in a
loop.  
**Options:**
- A: Return everything; rely on Claude to call sparingly.
- B: Add optional field filter: `fields=["devices", "rules"]` returns
  only those blocks.
- C: Add pagination for devices list if >100 entries.  
**Recommendation:** Start with A, add B in v1.1 if Claude spams calls.

#### ADR-M12: Should we expose a `run_digest_now()` MCP tool?

**Status:** Open  
**Context:** User might want to chat "run my weekly digest now using
your reasoning" — Claude generates digest-style output using
`house_context_snapshot` + `digest_instructions` + its own reasoning.
Could be explicit tool (`run_digest_now(topic=None)`) or implicit
(Claude figures it out from the resource + snapshot).  
**Options:**
- A: No explicit tool; let Claude compose from existing tools +
  resource. Flexible, slightly more prompting needed.
- B: Explicit `run_digest_now()` tool that returns structured digest
  JSON. Plugin could even email it via existing SMTP.
- C: Both — tool as a shortcut, Claude can also compose manually.  
**Recommendation:** A for v1, B if demand materialises.

#### ADR-M13: How does the plugin's MCP server handle multiple concurrent clients?

**Status:** Open  
**Context:** MCP supports concurrent connections. If Simon has Claude
Desktop on laptop + iPad + phone, all three might hold connections.
- Is IWS capable of concurrent MCP sessions?
- How does state isolation work (each session sees consistent rule
  store snapshots)?
- Can a write in one session affect another mid-flight?  
**Options:**
- A: Serialize all MCP operations (simple, potentially slow).
- B: Allow concurrent reads, serialize writes (standard pattern).
- C: Document single-client assumption for v1; add concurrency later.  
**Recommendation:** C for v1 (Simon uses one laptop). B for v2 if
multi-client use emerges.

#### ADR-M14: Rate limiting on expensive MCP tools?

**Status:** Open  
**Context:** `house_context_snapshot` triggers SQL Logger queries,
event log parse, observation parse. ~200ms-2s per call depending on
house size. If Claude loops on it, plugin responsiveness could suffer.  
**Options:**
- A: Server-side cache with 60s TTL (snapshot is stable enough).
- B: Rate limit per-client per-minute.
- C: No limits in v1; add if observed.  
**Recommendation:** A — cache with 60s TTL is near-free and prevents
most thundering-herd scenarios.

#### ADR-M15: Should rule_store changes fire an "MCP event" back to connected clients?

**Status:** Open  
**Context:** If Simon disables a rule via Claude Desktop, and the
digest generator is running concurrently, does the digest see the new
state? (It should — reads are always against latest variable value.)
But should connected MCP clients get a push notification?  
**Options:**
- A: No push; clients poll `get_rules` when they care.
- B: Add MCP `notifications/rule_changed` event.  
**Recommendation:** A — pure pull model is simpler and matches the
conversational use case. Push is over-engineered for single-user
chat.

#### ADR-M16: Distribution — plugin only, or plugin + Claude marketplace listing?

**Status:** Open  
**Context:** For other Indigo users to adopt:
- Plugin install via standard `.indigoPlugin` double-click
- Claude Desktop config snippet via README
- Is there value in publishing a Claude Desktop "connector" /
  marketplace listing so users can add with one click?
**Options:**
- A: README-only. User copies JSON snippet manually.
- B: Ship a `.mcp-connector.json` package that Claude Desktop can
  import (if/when Claude supports such a format).
- C: Ship a Claude Code plugin (marketplace) with pre-configured
  MCP reference.  
**Recommendation:** A for v1 (lowest friction to ship). B or C post-v1
if Claude's distribution primitives mature.

---

## 10. Risks & mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | MCP spec evolves, breaking tool surface | Medium | Medium | Track spec updates; pin MCP SDK version; integration tests against known-good client |
| R2 | Claude Desktop's MCP support regresses in an update | Low | Medium | Monitor release notes; fall back to Claude Code for interactive use |
| R3 | Indigo Reflector auth has a CVE exposing MCP | Low | High | Rely on Indigo's security posture; document off-LAN-only as safer default |
| R4 | Heavy MCP use hits Claude subscription rate limits | Low | Low | Weekly interactive use is nowhere near limits; document if becomes an issue |
| R5 | Claude proposes harmful rule + user approves via chat | Low | High | Server-side `_is_safe_rule_target` allowlist catches it before write |
| R6 | SQL Logger config drift between users makes `query_sql_logger` unreliable | Medium | Low | Tool returns clear errors on column/table missing; documentation covers setup |
| R7 | Users confuse "reflector URL for Claude" with public URL | Low | Medium | README explicitly walks through LAN vs remote |
| R8 | Plugin's IWS endpoint lacks async/concurrent request handling | Medium | Low | Benchmark under load; add concurrency fix if needed (likely ADR-M13 resolution) |

---

## 11. Rollout plan

### Phase 1 — Read-only MCP surface (target: 1 week of work)

- MCP-over-HTTP+SSE endpoint in plugin at `/mcp`
- Read tools: `query_sql_logger`, `house_context_snapshot`,
  `get_observations`, `get_rules`
- Resource: `digest_instructions`
- Remove `/feedback` + HMAC code
- README with Claude Desktop config snippet
- Tests: tool response shapes, reflector auth, caching
- Deploy to jarvis, validate interactively

### Phase 2 — Agent write surface (target: 3-4 days)

- Write tools: `propose_rule`, `add_rule`, `update_rule`
- Safety allowlist enforcement
- Confirmation email on MCP-originated rules (same template path)
- Tests: safety gate blocks thermostats/locks/alarms; confirmation
  email fires
- Deploy, do a full interactive flow end-to-end on jarvis

### Phase 3 — Claude Code scheduling as alternate generator (target: 1 week)

- Investigation: how Claude Code scheduled agents work
- `ClaudeCodeCLIGenerator` implementation (from previous brainstorm)
- Plugin pref: `digestGenerator = "api" | "claude_code_cli"`
- Fallback logic on generator failure
- Close out ADR-M10 based on findings

### Phase 4 — Optional weekly email + distribution polish (target: 2-3 days)

- `enableWeeklyEmail` pluginPref (default true, documented)
- ADR update: `0003-mcp-surface-as-primary-ui.md`
- Full README overhaul: install, config, use, troubleshooting
- Publish plugin release 2026.0.21 (or 2026.1.0 if it merits a minor
  bump given the new surface)

### Phase 5 — v2 features (post-launch, user-driven)

- `schedule_ad_hoc_digest(when, topic_hint)` tool
- Pattern-detection tools (`compare_weeks`, trend analysis)
- MCP notifications on rule changes (if demand emerges)
- Cross-week learning loop from declined observations

---

## 12. Dependencies

| Dependency | Purpose | Risk if unavailable |
|---|---|---|
| Python MCP SDK | MCP server implementation | Could fall back to manual JSON-RPC; more work |
| Indigo IWS | HTTP transport for MCP | Plugin already uses it; stable |
| Indigo Reflector | Remote access auth | If user doesn't enable reflector, LAN-only |
| `mlamoure/indigo-mcp-server` | Companion for device control + basic state | Users can skip and use just HI's MCP for history/agent — control degraded |
| Claude Desktop or Code with MCP support | Client | Core dependency; product requirement |

---

## 13. Out of scope (explicit)

- Multi-user / multi-tenant plugin behaviour
- Real-time push notifications via MCP (outbound)
- Non-Anthropic LLM integration (Ollama, etc.) — covered in a separate
  follow-up PRD if user interest emerges
- Web UI dashboard — Indigo has one; Claude Desktop is the chat UI
- Mobile app — outside plugin scope; if/when MCP comes to Claude iOS
  the existing endpoint works without changes
- Cloud-hosted SaaS version — this is a self-hosted plugin
- Auth rotation / multi-token support — single user, single reflector
  cred

---

## 14. Appendix

### 14.1 Sample Claude Desktop config

```json
{
  "mcpServers": {
    "indigo": {
      "url": "https://<your-reflector>.indigodomo.net/message/com.vtmikel.mcp_server/mcp/"
    },
    "home-intelligence": {
      "url": "https://<your-reflector>.indigodomo.net/message/com.simons-plugins.home-intelligence/mcp"
    }
  }
}
```

### 14.2 Sample interactive transcript

```
User:    What's the most energy-hungry device in my house this week?

Claude:  [calls home-intelligence:query_sql_logger or
          home-intelligence:house_context_snapshot(days=7)]
         Your Tumble Drier drew 18.4 kWh this week — 47% higher than
         last week (12.7 kWh). The Aeon HEM shows your whole-house
         consumption was 127 kWh, so the dryer was 14% of total.

User:    Can you add a rule that warns me if it's been on more than 2
         hours?

Claude:  [calls home-intelligence:propose_rule with schema]
         Proposed: when Tumble Drier onState=true for 120 minutes,
         send observation to next digest. Safety check passed (device
         is a Shelly smart plug on the allowlist). Should I add it?

User:    Yes

Claude:  [calls home-intelligence:add_rule]
         Rule `7a3b2f` is now active. Confirmation email sent.
```

### 14.3 Related documents

- ADR-0001 (repo-local): Plugin-internal rule engine
- ADR-0002 (workspace): Email feedback loop via user SMTP+IMAP
- TODOS.md: deferred items from HOLD SCOPE review
- CLAUDE.md: plugin architecture overview
