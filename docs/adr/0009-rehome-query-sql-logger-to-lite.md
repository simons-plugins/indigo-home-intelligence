---
parent: Decisions
nav_order: 9
title: "ADR-0009: Re-home query_sql_logger to indigo-mcp-lite"
status: "accepted"
date: 2026-07-22
decision-makers: solo (Simon)
consulted: none
informed: none
supersedes: "ADR-0003 / ADR-0005 (rationale only — the claim that SQL Logger access is HI-exclusive)"
superseded_by: none
---
# ADR-0009: Re-home query_sql_logger to indigo-mcp-lite

## Context and Problem Statement

[ADR-0005](0005-minimal-mcp-tool-surface.md) placed `query_sql_logger` in
HI's MCP surface under criterion (a): it accessed data the general Indigo
MCP (then mlamoure's) did not have. [ADR-0008](0008-general-indigo-mcp-supersede-by-lite.md)
later made `indigo-mcp-lite` the canonical general MCP. A 2026-07-22
four-way review (lite, HI, mlamoure, ClaudeBridge) concluded that
`query_sql_logger` is the *only* general-purpose tool in HI's surface —
device history is general Indigo data, not digest/rule domain — so
ADR-0005's own criteria now point at lite as its correct home.

## Decision Outcome

1. **lite gains the history capability** (lite PR #23): a trimmed copy of
   `history_db.py` (digest-only rollup/energy helpers removed) plus
   `query_sql_logger` (same name and wire contract) and a new
   `list_sql_logger_columns` discovery tool. Hardened during the port:
   strict column allowlist and PG server-side read-only
   (`PGOPTIONS=-c default_transaction_read_only=on`).
2. **HI drops `query_sql_logger` from its MCP surface** (this repo) to
   honour ADR-0005's no-duplication rule. HI's surface is now 6 tools +
   1 resource, all genuinely HI-specific.
3. **HI keeps `history_db.py`** — the digest's SQL rollups and energy
   week-over-week depend on it internally. This is a copy/share, not a
   move: both plugins carry the module, with the query paths kept
   textually aligned (the known "refactor to shared lib is future work"
   debt, now spanning three repos: domio, HI, lite).

## Consequences

- Users configure SQL Logger connection details in **lite's** plugin
  config to get history via MCP; HI's identical prefs continue to serve
  only the digest.
- Any future fix to the shared query path must be applied to both
  copies (module headers in both repos state this).
- ADR-0003/0005 remain accepted; only their SQL-Logger-is-HI-exclusive
  rationale text is superseded, following the ADR-0008 pattern.
