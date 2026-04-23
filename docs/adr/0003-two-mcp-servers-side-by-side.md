---
parent: Decisions
nav_order: 3
title: "ADR-0003: Ship our MCP server alongside mlamoure's, do not fork"
status: "accepted"
date: 2026-04-23
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0003: Ship our MCP server alongside mlamoure's, do not fork

## Context and Problem Statement

An MCP server for Indigo already exists: `mlamoure/indigo-mcp-server`.
It exposes current device state, variables, action groups, basic
event-log queries, and device control actions (`device_turn_on`,
`thermostat_set_heat_setpoint`, etc.). Many Indigo users who might
adopt Home Intelligence already have it installed.

Home Intelligence has its own differentiated capabilities —
SQL Logger-backed history, per-device energy rollups, curated digest
context, observation + rule store access, safe rule-write gate — that
are NOT in mlamoure's MCP.

We must decide how to deliver our tools:

1. Fork mlamoure's MCP and add ours, or
2. Ship a second MCP from the HI plugin and have users install both.

## Decision Drivers

* The HI plugin OWNS its differentiated logic (SQL Logger wrappers,
  digest context assembly, rule-safety gate, observation store).
  Moving that logic into a different codebase creates cross-repo
  drift.
* Release cadence: we want to iterate quickly. Upstreaming every
  change through another maintainer's review is too slow.
* Distribution simplicity: users want a clear install path. Two
  configs is slightly worse than one, but manageable.
* Single responsibility per codebase: mlamoure's MCP is a general
  Indigo tool surface; Home Intelligence is a reasoning + rule
  engine. They have different audiences and different shapes.
* Upstream goodwill: we do not want to force a fork unless the
  upstream repo is inactive, which it isn't.

## Considered Options

1. **Ship our own MCP from the HI plugin; document "install both"** —
   users add two `mcpServers` entries to Claude Desktop config.
2. **Fork `mlamoure/indigo-mcp-server`** — add our tools to a
   maintained fork, users install the fork instead of the original.
3. **PR our tools upstream** — propose adding SQL Logger + digest
   context + rule CRUD tools to his server, wait for merge.
4. **Bundle both MCP servers into a single Indigo plugin** — HI
   plugin hosts BOTH sets of tools as if they were one surface.

## Decision Outcome

Chosen option: **"Ship our own MCP from the HI plugin; document
'install both'"**, because it gives us full release autonomy, keeps
the HI plugin's differentiated logic in its own codebase, respects
upstream ownership of mlamoure's tool surface, and costs users only
a second `mcpServers` entry in their Claude Desktop config — a copy-
paste snippet, well within install friction tolerance.

### Consequences

* Good, because we can iterate on tool surface daily without waiting
  on upstream.
* Good, because mlamoure's MCP continues to evolve independently — we
  benefit from his improvements (new control actions, better state
  queries) without merge conflicts.
* Good, because users who already have his MCP installed only need
  to add one more config entry to gain Home Intelligence capabilities.
* Good, because users who DON'T want Home Intelligence's reasoning
  layer can install only his MCP. Home Intelligence's value is
  optional.
* Good, because the security boundary is clear: each MCP has its own
  code path; a bug in one does not leak into the other.
* Bad, because users must add two server entries and understand the
  split. Mitigated by a README config snippet that includes both.
* Bad, because there could be tool-name collisions in future if both
  sides add similarly-named tools. Mitigated by MCP's namespacing
  (tools are implicitly scoped to their server name in Claude's
  context).
* Bad, because we cannot share helper code between the two servers.
  Accepted — the surfaces are distinct enough that duplication is
  minimal.

### Confirmation

Considered implemented when:

1. The plugin's README documents the dual-install config snippet with
   both `indigo` (mlamoure) and `home-intelligence` (this plugin)
   entries in `claude_desktop_config.json`.
2. The tool surfaces are demonstrably non-overlapping: no tool in
   this plugin's MCP duplicates functionality already in mlamoure's.
3. A fresh install on a clean machine, following the README steps,
   results in Claude Desktop listing tools from both servers in the
   same chat session.

## Pros and Cons of the Options

### Ship our own MCP (chosen)

* Good, because release autonomy.
* Good, because clean separation of concerns.
* Good, because respects upstream ownership.
* Bad, because two config entries for users.

### Fork mlamoure's MCP

* Good, because one install for users.
* Bad, because we inherit his tool surface and release cadence as
  maintainers.
* Bad, because divergence from upstream strains goodwill; upstream
  fixes need merging.
* Bad, because his tool surface has its own scope that conflates
  with ours.

### PR upstream

* Good, because users get our tools without a second install.
* Bad, because release velocity is the upstream maintainer's, not
  ours.
* Bad, because upstream may reject tools that don't fit his
  scope — especially plugin-specific ones like rule_store CRUD.
* Bad, because changes to HI plugin internals that affect the tools
  need coordinated cross-repo releases.

### Bundle both into one plugin

* Good, because users get one install.
* Bad, because we become responsible for mlamoure's MCP surface —
  same problem as forking, worse because we also own the Indigo
  plugin packaging.
* Bad, because upstream improvements to mlamoure's MCP don't flow
  to our bundle without manual syncing.

## More Information

* PRD-0002 §6.2 — dual-MCP architecture diagram.
* PRD-0002 §14.1 — sample Claude Desktop config with both servers.
* mlamoure/indigo-mcp-server:
  https://github.com/mlamoure/indigo-mcp-server
* Related: ADR-0002 (MCP transport choice), ADR-0005 (minimal tool
  surface principle).

## For AI agents
- DO: Check if a query can be satisfied by mlamoure's existing tools
  (`list_devices`, `get_device_by_id`, `search_entities`, etc.)
  before proposing a new tool in this plugin.
- DO: Document any overlap with mlamoure's surface as a deliberate
  exception with clear justification (e.g. "we return the same data
  in a different shape because...").
- DON'T: Duplicate mlamoure's device-control, state-query, or
  action-group tools in this plugin's MCP. Users should use his
  server for those.
- DON'T: Introduce tools that would be better hosted upstream (e.g.
  generic Indigo-wide utilities that aren't Home-Intelligence-
  specific). Either propose them to mlamoure or keep them plugin-
  internal.
