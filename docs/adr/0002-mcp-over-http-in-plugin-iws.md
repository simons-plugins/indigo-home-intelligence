---
parent: Decisions
nav_order: 2
title: "ADR-0002: Host the MCP server inside the plugin over HTTP+SSE on IWS"
status: "accepted"
date: 2026-04-23
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0002: Host the MCP server inside the plugin over HTTP+SSE on IWS

## Context and Problem Statement

The plugin has accumulated history-aware reasoning capabilities that
Claude-the-model (via the Anthropic API) uses weekly to produce a
digest email. The same reasoning is now wanted **mid-week, on
demand**, accessible from Claude Desktop and Claude Code using the
user's Claude subscription rather than billed API calls.

We need a transport that is:

* Supported by Claude Desktop (primary surface per user requirement)
* Supported by Claude Code (secondary, power-user)
* Reachable both locally (LAN) and remotely (Indigo Reflector)
* Co-located with the plugin's existing reasoning + state so there is
  no duplication
* Authenticated via the user's existing Indigo credentials, not yet
  another bespoke secret

Skills (Claude-Code-only markdown files under `~/.claude/skills/`)
solve only the Claude Code case. An HTTP JSON API is non-standard and
would require per-client glue. A desktop-native IPC mechanism is
platform-specific and excludes remote use. MCP is the protocol every
current Claude surface speaks natively.

## Decision Drivers

* Must work in Claude Desktop (primary user requirement).
* Must work in Claude Code (same codebase, different client).
* Must be reachable remotely via Indigo Reflector (user is not always
  on LAN).
* Must share state + methods with the existing plugin — the plugin
  IS the owner of observation store, rule store, SQL Logger
  wrappers, and the digest's context assembly.
* Must not introduce a second process lifecycle for the user to
  manage.
* Must avoid duplicating the reasoning guidance (INSTRUCTIONS) in
  two places.

## Considered Options

1. **MCP server hosted in the plugin, HTTP+SSE transport on IWS** —
   add an `/mcp` route to the existing Indigo Web Server that the
   plugin already uses for `/status` and (previously) `/feedback`.
2. **MCP server as a separate Python process running on the same
   host** — standalone daemon that reads Indigo state via IPC, runs
   outside the plugin's lifecycle.
3. **Claude Code skill + bash/`curl` wrappers around plugin IWS** —
   no MCP; skill calls the plugin's existing JSON endpoints directly.
4. **Contribute the tools upstream to `mlamoure/indigo-mcp-server`**
   and have users install only his MCP.
5. **Platform-native IPC (macOS XPC, Unix domain socket)** — fastest
   local comms; no remote story.

## Decision Outcome

Chosen option: **"MCP server hosted in the plugin, HTTP+SSE transport
on IWS"**, because it is the only option that simultaneously works in
Claude Desktop AND Claude Code AND remotely via the Indigo Reflector,
shares state directly with the plugin's internal Python methods
(zero duplication), and reuses the authentication boundary the
reflector already enforces.

### Consequences

* Good, because MCP tool responses come straight from the same Python
  methods the weekly digest calls. No separate data-gathering layer,
  no drift between surfaces.
* Good, because the plugin's lifecycle is the MCP server's lifecycle
  — no second process to start, monitor, or supervise.
* Good, because the transport is standard MCP-over-HTTP+SSE, usable
  from any spec-compliant client (Claude Desktop, Claude Code, and
  future clients).
* Good, because remote access works through the Indigo Reflector's
  existing authentication without a second credential concept
  (see ADR-0004 on HMAC removal).
* Good, because the INSTRUCTIONS reasoning guide can be exposed as
  an MCP resource, giving interactive clients the same accumulated
  wisdom the weekly digest uses (see PRD §7.1.3).
* Bad, because IWS is a relatively lightweight HTTP server; heavy
  concurrent MCP use (multiple clients, large snapshot calls) has not
  been load-tested. Acceptable for a single-user home-automation
  plugin; documented as a risk (PRD §10 R8).
* Bad, because the MCP Python SDK is relatively young; protocol
  changes upstream may require follow-up work (PRD §10 R1).
* Bad, because running the MCP server inside the plugin means plugin
  restarts drop any in-flight MCP sessions. Not worse than any other
  plugin feature, but worth noting.
* Bad, because users must add a second `mcpServers` entry alongside
  `mlamoure/indigo-mcp-server`. See ADR-0003.

### Confirmation

Considered implemented when:

1. The plugin exposes an `/mcp` route on IWS serving MCP-over-HTTP+SSE
   conforming to the MCP spec version pinned in `pyproject.toml`.
2. A spec-compliant MCP client (Claude Desktop or `mcp` CLI) can list
   the tools + resource this plugin publishes and invoke each one.
3. Plugin-internal methods used by `DigestRunner` (e.g.
   `_build_house_model`, `energy_rollup_14d`, `observation_store.list_all`)
   are the single source truth — each MCP tool is a thin wrapper with
   no reasoning of its own.
4. Plugin restart starts the MCP server cleanly without manual
   intervention.

## Pros and Cons of the Options

### MCP server hosted in the plugin, HTTP+SSE transport on IWS

* Good, because zero duplication — MCP tools are wrappers around
  existing plugin methods.
* Good, because reuses existing IWS auth boundary (reflector / LAN).
* Good, because one process, one lifecycle, one deploy.
* Good, because transport is universal across MCP clients.
* Bad, because ties MCP server availability to plugin health
  (probably acceptable — if the plugin is down, there's nothing
  useful to query anyway).
* Bad, because IWS concurrency characteristics are unknown at scale.

### Separate Python daemon process

* Good, because decoupled from plugin restart cycle.
* Good, because independently versionable / testable.
* Bad, because reads plugin state via IPC or shared variable reads —
  much more plumbing than "just import the method".
* Bad, because two processes to configure, monitor, and keep in sync.
* Bad, because would need its own authentication (the plugin's
  reflector auth doesn't extend to a separate port).

### Claude Code skill + bash/`curl` wrappers

* Good, because no MCP SDK dependency.
* Bad, because doesn't work in Claude Desktop — primary user
  requirement failed.
* Bad, because skill is Claude-Code-specific; users on Claude Desktop
  subscription get nothing.
* Bad, because bash-over-HTTP response shapes aren't structured the
  way MCP tools are — Claude's tool-use flow doesn't apply.

### Contribute upstream to mlamoure/indigo-mcp-server

* Good, because one MCP for users to install.
* Bad, because the HI plugin's differentiated value (SQL Logger, our
  digest context, rule store safety gate) lives in OUR codebase, not
  his. Contributing it upstream means either duplicating it there or
  forcing him to depend on our plugin.
* Bad, because cross-repo release cadences drift.
* Bad, because user waits on upstream acceptance for every change —
  unworkable for iterative development.

### Platform-native IPC (macOS XPC, Unix sockets)

* Good, because local performance is excellent.
* Bad, because no remote story; reflector access not possible.
* Bad, because not MCP; Claude Desktop can't consume.
* Bad, because macOS-only — excludes any Linux Indigo host.

## More Information

* PRD-0002 §6.2 — architecture diagrams and component map.
* Python MCP SDK: https://github.com/anthropics/anthropic-sdk-python
  (MCP features landing in increments; track the SDK release notes).
* MCP spec: https://spec.modelcontextprotocol.io/ (pin the version
  we target in `pyproject.toml`).
* Related: ADR-0003 (two servers side-by-side), ADR-0004 (no HMAC
  on MCP endpoint).

## For AI agents
- DO: Add MCP tools as thin wrappers around methods that already
  exist in the plugin. The reasoning substrate is already built;
  MCP is a transport.
- DO: Keep response shapes stable — MCP tool signatures are a
  public API once shipped.
- DON'T: Re-implement data access logic inside MCP tool handlers.
  If you find yourself writing a SQL query, check if the plugin
  already has a method for that task and call it.
- DON'T: Open a second port, spawn a daemon, or fork a subprocess
  for MCP. It runs inside the plugin process, on IWS.
