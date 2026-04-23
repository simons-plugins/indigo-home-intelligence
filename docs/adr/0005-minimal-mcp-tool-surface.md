---
parent: Decisions
nav_order: 5
title: "ADR-0005: Keep the MCP tool surface minimal; do not duplicate tools available in mlamoure's MCP"
status: "accepted"
date: 2026-04-23
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0005: Keep the MCP tool surface minimal; do not duplicate tools available in mlamoure's MCP

## Context and Problem Statement

When designing the MCP surface for the Home Intelligence plugin, an
early tool list included generic helpers: `fleet_health_snapshot`,
`event_log_narrative`, `list_rules`, etc. — many of which Claude can
derive from `mlamoure/indigo-mcp-server`'s existing primitives by
filtering, searching, or reading device/variable state directly.

Exposing those as bespoke tools here means:

* Users learn TWO ways to ask the same question, one per server.
* Reasoning guidance must be kept in sync across both servers.
* The Home Intelligence plugin takes implicit responsibility for
  tool behaviour that really belongs in the general Indigo MCP.

Meanwhile, some tools have NO equivalent in mlamoure's MCP and are
the actual differentiated value of this plugin:

* SQL Logger access (mlamoure uses Indigo's built-in history DB only)
* Curated digest context assembly (15+ raw tool calls collapsed to 1)
* Observation / rule store parsing (raw JSON blobs in variables)
* Safe rule-write path with server-side allowlist

We need a principle that distinguishes "tool worth adding" from
"tool Claude can compose from existing primitives."

## Decision Drivers

* Every tool added is a maintenance burden — response shape is a
  public API once published.
* Every tool reduces Claude's need to reason creatively — over-
  tooling can cause Claude to reach for our named tool when a
  composition of mlamoure's would be better.
* mlamoure's MCP is the general-purpose Indigo tool surface; ours
  is the home-intelligence-specific surface. Role clarity matters
  for users AND for Claude's tool selection.
* Tools that wrap plugin-internal data (rule store, observation
  store, SQL Logger) genuinely differentiate and belong here.
* Tools that aggregate or curate in ways expensive for the client
  (context snapshots) also belong here, even when theoretically
  derivable.

## Considered Options

1. **Minimal surface: only tools that (a) access data mlamoure
   doesn't, (b) parse plugin-specific variables, (c) enforce safety
   gates, or (d) provide genuine shortcuts for expensive client-side
   assembly.** ~7–8 tools + 1 resource.
2. **Comprehensive surface: replicate every convenience query
   (fleet health, event log narrative, rule activity, energy
   summary) as a bespoke tool.** ~12–15 tools.
3. **Thin wrappers only, no aggregation: every tool is a direct
   exposure of a single plugin method.** ~4–5 tools.
4. **No tools, only resources: publish the digest context as a
   resource, let Claude's reasoning drive all composition.** 1
   resource, 0 tools.

## Decision Outcome

Chosen option: **"Minimal surface: tools that (a) access data
mlamoure doesn't, (b) parse plugin-specific variables, (c) enforce
safety gates, or (d) shortcut expensive client-side assembly"**, because
it preserves the home-intelligence-specific value of this plugin's MCP
without taking implicit ownership of general Indigo tool-surface
concerns, and it avoids over-constraining Claude's ability to compose
answers from the richer mlamoure surface.

### Consequences

* Good, because the plugin's MCP has a crisp role: "history + agent
  workflow + differentiated reasoning substrate." Users and Claude
  both understand what to ask each server for.
* Good, because maintenance burden stays proportional to
  differentiated capability — we aren't on the hook to keep 15 tools
  working.
* Good, because Claude composes freely from mlamoure's general
  primitives for anything not in our scope — his tools get
  exercised as their author intended.
* Good, because tool-surface evolution is predictable: new tools
  get added only when they meet the four criteria.
* Bad, because Claude occasionally takes more tool-call hops than it
  would with a richer surface (e.g. filtering `list_devices` for
  low-battery instead of calling a dedicated `fleet_health` tool).
  Acceptable cost — the filtering logic is simple and stable.
* Bad, because users who discover the "composable" approach may
  wonder why we didn't expose specific tools for common patterns.
  Addressed by README + tool descriptions explaining the split.

### Confirmation

Considered implemented when:

1. The MCP server's tool surface contains EXACTLY these categories:
   * SQL Logger access (`query_sql_logger`)
   * Curated context (`house_context_snapshot`)
   * Plugin-specific variable parsing (`get_observations`,
     `get_rules`)
   * Safe write with allowlist (`propose_rule`, `add_rule`,
     `update_rule`)
   * Resource (`digest_instructions`)
2. Every tool has a one-sentence description explaining why it is not
   composable from mlamoure's existing tools.
3. Code review for any proposed new tool includes the question:
   "Can Claude compose this from mlamoure's tools with reasonable
   cost?" — and rejection of the tool if the answer is yes.

## Pros and Cons of the Options

### Minimal surface (chosen)

* Good, because low maintenance burden.
* Good, because clear role separation between the two MCP servers.
* Good, because Claude is encouraged to compose creatively.
* Bad, because some common queries take more tool-call hops.

### Comprehensive surface

* Good, because every common query is a single tool call.
* Bad, because we duplicate mlamoure's effective surface in a less-
  well-maintained location.
* Bad, because tool-surface maintenance scales linearly with tool
  count — and shipped tools are public API.
* Bad, because Claude might consistently reach for our curated tool
  when the more-general mlamoure composition would be correct for
  the specific question.

### Thin wrappers only, no aggregation

* Good, because simplest possible implementation.
* Bad, because `house_context_snapshot` becoming 15+ tool calls is a
  real user-visible cost (tokens, latency, prompt complexity).
  Curated aggregates are a legitimate MCP pattern, not laziness.

### No tools, only resources

* Good, because maximum flexibility for Claude.
* Bad, because resources are static snapshots, not parameterised.
  We cannot expose `query_sql_logger(device_id, range)` as a
  resource — that's inherently a tool.
* Bad, because write actions (`add_rule`) cannot be resources.

## More Information

* PRD-0002 §7.1 — full tool specification with the four-criteria
  rationale per tool.
* Related: ADR-0003 (two servers side-by-side — this ADR is the
  "how much do we put in OUR server" pair to that "which server
  do we put it in" decision).

## For AI agents
- DO: Before adding a new tool, check whether `mlamoure/indigo-mcp-
  server` already covers the use case with a composable primitive.
  If yes, do not add.
- DO: Add tools when they access SQL Logger (mlamoure uses Indigo
  built-in history), parse plugin-specific variables (observations
  / rules), enforce safety gates, or collapse N>5 client calls into
  one curated response.
- DO: Document each tool's "why this is not composable from
  mlamoure" in its description.
- DON'T: Add tools for "common queries" that Claude can construct
  from existing primitives. Let Claude reason.
- DON'T: Replicate mlamoure's device-control, state-query, or
  action-group-execution surface in this MCP. Those remain his.
