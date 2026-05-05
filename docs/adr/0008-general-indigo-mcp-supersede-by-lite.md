---
parent: Decisions
nav_order: 8
title: "ADR-0008: Cross-reference: workspace ADR-0003 supersedes the assumed-mlamoure default in HI's ADR-0003"
status: "accepted"
date: 2026-05-05
decision-makers: solo (Simon)
consulted: none
informed: none
supersedes: none
superseded_by: none
---
# ADR-0008: Cross-reference: workspace ADR-0003 supersedes the assumed-mlamoure default in HI's ADR-0003

## Context and Problem Statement

[HI ADR-0003](0003-two-mcp-servers-side-by-side.md) — accepted 2026-04-23 — decided that Home Intelligence ships its own MCP server alongside `mlamoure/indigo-mcp-server`, with users installing both and getting tools under separate namespaces. That ADR's premise was that mlamoure's MCP would always be available as the canonical "general Indigo" surface; HI just adds the digest/rule/observation-specific tools on top.

That premise broke 2026-05-05:

1. Indigo 2025.2's Python 3.13 + Intel Mac combination won't install LanceDB (which mlamoure's plugin depends on for vector search). On jarvis (Intel), mlamoure's plugin no longer runs.
2. **`indigo-mcp-lite`** was built specifically to fill that gap — same general-Indigo tool surface as mlamoure's, plus a stdlib FTS5-based `find_devices`, no native deps, no API keys.

The workspace decided ([workspace ADR-0003](../../../../docs/adr/0003-intel-mac-mcp-uses-indigo-mcp-lite.md)) to re-point the canonical `mcp__indigo__*` namespace from mlamoure's URL to lite's URL on Intel Mac. mlamoure's plugin remains a side-by-side option on Apple Silicon for users who specifically want vector search.

This ADR exists because **ADRs are immutable once Accepted** (workspace ADR-0001 rule). HI's ADR-0003 cannot be edited in place. The dual-MCP architecture it describes is **still valid** — HI still ships its own MCP, users still need both for HI's specific tools — but the *who-serves-the-general-Indigo-surface* assumption is now superseded.

This ADR formalises that supersede so future readers of HI's ADR-0003 know exactly what's still true and what isn't.

## Decision Drivers

* ADRs are immutable once Accepted; supersede semantics belong in a new ADR.
* Future readers of HI ADR-0003 need a forwarding pointer — without one they'd implement the original 2026-04-23 dual-install instructions and hit jarvis's broken LanceDB.
* The distinction matters: **the dual-MCP shape itself is unchanged** — only the identity of one of the two MCPs changes. We don't want to invalidate ADR-0003's reasoning wholesale.
* Workspace `mcp-config-routing.md` memory already documents the live state, but memory is not durable architecture history; this ADR is.

## Considered Options

1. **New cross-reference ADR (this one)** — small, explicit, names what's superseded and what isn't.
2. **Edit HI ADR-0003 in place** to mark it superseded. **Rejected** — workspace ADR-0001 rule forbids editing Accepted ADRs.
3. **Workspace ADR-0003 alone, no HI cross-reference** — relies on readers of HI ADR-0003 to discover the workspace ADR somehow. Brittle.

## Decision Outcome

Chosen: **"New cross-reference ADR"** in HI's repo, pointing at the workspace ADR-0003 as the supersede source.

What stays in effect from HI ADR-0003:

* HI ships its own MCP from inside the HI plugin (not as a fork of mlamoure's).
* Users install **both** an Indigo MCP and HI's MCP — two `mcpServers` entries in their config.
* Tool surfaces are **non-overlapping by design**: HI's MCP doesn't duplicate the general-Indigo device-control / state-query / event-log tools.
* Tool naming follows MCP namespacing: HI tools are `mcp__home_intelligence__*`, general-Indigo tools are `mcp__indigo__*`.

What's superseded by workspace ADR-0003:

* The assumption that the `mcp__indigo__*` namespace points at `com.vtmikel.mcp_server/mcp/` (mlamoure's URL). On Intel Mac under Indigo 2025.2 it now points at `com.simons-plugins.indigo-mcp-lite/mcp` (lite's URL).
* The README config snippet showing mlamoure as the default for the `indigo` namespace. New default is lite; mlamoure remains a documented opt-in via Pattern B (side-by-side).

### Consequences

* Good, because future readers of HI ADR-0003 hit a clear supersede pointer when they look at INDEX.md.
* Good, because the supersede is **scoped** — HI's dual-MCP architecture stays valid, only the Indigo-side MCP identity changes.
* Good, because no existing HI plugin code or ADR text needs editing; the supersede is purely cross-referenced.
* Bad, because two ADRs now share the "0003" number across repos (HI's local ADR-0003 vs the workspace ADR-0003). Mitigated by the `parent: Decisions` MADR scoping convention and explicit URL paths in cross-references.

### Confirmation

Considered implemented when:

1. This ADR file exists in `docs/adr/0008-*.md` and is linked from HI's `INDEX.md`.
2. HI's INDEX.md adds a note next to ADR-0003 pointing at this ADR (and through it, at workspace ADR-0003).
3. Workspace ADR-0003 lands and references this ADR as a downstream cross-reference. Achieved 2026-05-05 in workspace `docs/adr/0003-intel-mac-mcp-uses-indigo-mcp-lite.md`.

## Pros and Cons of the Options

### New cross-reference ADR (chosen)

* Good, because explicit, durable, immutable-friendly.
* Good, because doesn't disturb ADR-0003's text or reasoning.
* Bad, because adds one ADR file readers have to follow a chain through.

### Edit HI ADR-0003 in place

* Good, because zero new files.
* Bad, because directly violates workspace ADR-0001's immutability rule.

### Workspace ADR-0003 alone, no HI cross-reference

* Good, because lower volume of new ADRs.
* Bad, because future readers of HI's ADR-0003 have no forwarding pointer; they have to know to look at the workspace ADR hub.

## More Information

* HI ADR-0003 (the partially-superseded source): [`0003-two-mcp-servers-side-by-side.md`](0003-two-mcp-servers-side-by-side.md)
* Workspace ADR-0003 (the supersede source): `~/vsCodeProjects/Indigo/docs/adr/0003-intel-mac-mcp-uses-indigo-mcp-lite.md`
* Workspace ADR-0001 (the immutability rule): `~/vsCodeProjects/Indigo/docs/adr/0001-record-architecture-decisions.md`
* Lite ADR-0001 (FTS5 vs vector search — explains *why* lite exists): `~/vsCodeProjects/Indigo/indigo-mcp-lite/docs/adr/0001-fts5-not-vector-search.md`
* Lite repo: https://github.com/simons-plugins/indigo-mcp-lite
* Workspace memory `mcp-config-routing.md`: live state of `.mcp.json` + revert recipe to swap `indigo` namespace back to mlamoure on Apple Silicon.

## For AI agents

- DO: when reading HI ADR-0003, also read this ADR (0008) and workspace ADR-0003 to get the full current picture.
- DO: keep HI ADR-0003's dual-MCP architecture in mind — HI still ships its own MCP separately from the general-Indigo MCP. That part is not superseded.
- DON'T: edit HI ADR-0003 to "fix" the mlamoure-default assumption. The supersede chain is captured here and in workspace ADR-0003 — that's the workspace's immutability convention.
- DON'T: add new HI tools that overlap with lite's general-Indigo tool surface (lookup / control / event log / plugin status). HI ADR-0005's minimal-tool-surface principle still applies; lite is now part of "the general surface", same as mlamoure was before.
