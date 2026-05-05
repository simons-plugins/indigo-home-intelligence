# Architecture Decision Records — Home Intelligence

Repo-local ADRs governing decisions specific to the Home Intelligence
plugin. Cross-repo decisions (push contract, shared HMAC, email-in/out
strategy) live in the workspace hub at
`~/vsCodeProjects/Indigo/docs/adr/`.

Format: MADR 4.0.0 (https://adr.github.io/madr/). Template at
`0000-template.md`.

<!-- adrlog -->

* [ADR-0000](0000-template.md) - ADR-NNNN: {short title, imperative}
* [ADR-0001](0001-plugin-internal-rule-engine.md) - ADR-0001: Use a plugin-internal rule engine rather than native Indigo triggers
* [ADR-0002](0002-mcp-over-http-in-plugin-iws.md) - ADR-0002: Host the MCP server inside the plugin over HTTP+SSE on IWS
* [ADR-0003](0003-two-mcp-servers-side-by-side.md) - ADR-0003: Ship our MCP server alongside mlamoure's, do not fork
* [ADR-0004](0004-no-hmac-on-mcp-remove-feedback.md) - ADR-0004: No HMAC on the MCP endpoint; remove the vestigial /feedback path
* [ADR-0005](0005-minimal-mcp-tool-surface.md) - ADR-0005: Keep the MCP tool surface minimal; do not duplicate tools available in mlamoure's MCP
* [ADR-0006](0006-safety-allowlist-all-rule-write-paths.md) - ADR-0006: Enforce the rule-target safety allowlist on every rule-write path, server-side
* [ADR-0007](0007-weekly-email-optional.md) - ADR-0007: Make the weekly digest email optional, default off for new installs after v2
* [ADR-0008](0008-general-indigo-mcp-supersede-by-lite.md) - ADR-0008: Cross-reference: workspace ADR-0003 supersedes the assumed-mlamoure default in HI's ADR-0003

<!-- adrlogstop -->

> **Note on ADR-0003**: the dual-MCP architecture it describes is still valid, but its assumption that mlamoure's plugin serves the `mcp__indigo__*` namespace is **partially superseded** by [ADR-0008](0008-general-indigo-mcp-supersede-by-lite.md) → workspace ADR-0003. On Intel Mac under Indigo 2025.2+, that namespace now routes to `indigo-mcp-lite`.

## Context: PRDs

PRDs under `docs/prd/` carry the broader feature-level design and
reference the ADRs that formalise their load-bearing decisions.

* [PRD-0002](../prd/0002-interactive-mcp-surface.md) — Interactive
  MCP Surface for Home Intelligence. ADRs 0002–0007 formalise the
  architectural commitments; the PRD itself documents tactical
  choices (M6 INSTRUCTIONS-as-resource; M9 observation-store read-
  only in v1; all open ADRs M10–M16).
