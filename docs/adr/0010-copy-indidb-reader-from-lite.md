---
parent: Decisions
nav_order: 10
title: "ADR-0010: Consume automation contents via a copied indidb_reader, not lite's MCP tools"
status: "accepted"
date: 2026-07-23
decision-makers: solo (Simon)
consulted: none
informed: none
supersedes: none
superseded_by: none
---
# ADR-0010: Consume automation contents via a copied indidb_reader, not lite's MCP tools

## Context and Problem Statement

indigo-mcp-lite 2026.8.0 (lite PR #40) shipped a clean-room `.indiDb`
reader — decoded action steps, condition trees, and reverse references
for schedules, triggers, and action groups — exposed through lite's
`get_automation_contents` / `find_automation_references` MCP tools.

HI wants that data for three jobs (issue #27): making the digest's
"never re-suggest something already automated" instruction a real
check against decoded contents rather than a name-based guess; letting
`propose_rule` / `add_rule` flag existing native automations acting on
a rule's target device; and letting the digest report automation
conflicts and orphaned references.

How should HI consume the capability: call lite's MCP tools over
localhost HTTP, or carry its own copy of `indidb_reader.py`?

## Decision Drivers

* **No cross-plugin runtime dependency.** HI and lite are separate
  failure domains by design (ADR-0003's dual-server architecture). A
  digest that HTTP-calls lite couples the weekly run's health to
  another plugin being installed, enabled, and responsive.
* **Bulk-use economics.** The digest needs contents for every
  automation that fired in the window — one in-process streaming parse
  (cached on path/mtime/size) versus N HTTP round-trips per run.
* **Portability.** HI users without lite installed should still get
  the automation-contents digest block and the rule-gate signal.

## Considered Options

* Copy `indidb_reader.py` from lite (the `history_db.py` pattern)
* Call lite's MCP tools over localhost HTTP from HI
* Extract a shared library both plugins depend on

## Decision Outcome

Chosen option: **"Copy `indidb_reader.py` from lite"**, because it is
the only option that keeps the two plugins' failure domains separate
(first driver, k.o. criterion), turns the digest's bulk read into a
single cached in-process parse, and works for HI installs that don't
run lite. The shared-library option remains the acknowledged endgame
("refactor to shared lib is future work", per ADR-0009) but is not
worth building for a third consumer of a copy-clean, stdlib-only
module.

### Consequences

* Good, because the digest and the rule gate have zero new runtime
  dependencies — a lite outage, uninstall, or version skew cannot
  break HI.
* Good, because the digest's bulk scoping (contents for every fired
  automation) costs one streamed parse of the DB file, not N tool
  calls.
* Bad, because this is the **third keep-aligned copy** in the
  workspace (after `history_db.py` in domio/HI/lite): bug fixes to
  the parser must be applied to both repos' `indidb_reader.py`. Both
  module headers state this.
* Neutral, because lite's `get_automation_contents` /
  `find_automation_references` MCP tools remain the *interactive*
  channel — HI does not re-expose them (ADR-0005's no-duplication
  rule); HI's copy serves only the digest and the rule-write gate.

### Confirmation

`tests/test_indidb_reader.py` is an adapted copy of lite's suite and
must stay green. Alignment between the two copies is a MANUAL review
step — `diff` the modules (HI adds only the header provenance note);
no automated cross-repo check exists. Digest wiring and rule-gate
behaviour are covered by `tests/test_automation_contents.py` and the
`existing_automations` cases in `tests/test_mcp_tools.py`.

## More Information

* Issue #27 (this repo) — feature scope and token-scoping decision
  (contents only for automations that fired in the window or that a
  proposed rule touches).
* simons-plugins/indigo-mcp-lite#38 + lite PR #40 — the reader's
  schema recon and implementation.
* [ADR-0003](0003-two-mcp-servers-side-by-side.md),
  [ADR-0005](0005-minimal-mcp-tool-surface.md),
  [ADR-0009](0009-rehome-query-sql-logger-to-lite.md) — the
  dual-server split, minimal-surface rule, and the `history_db.py`
  keep-aligned precedent.

## For AI agents
- DO: apply any `indidb_reader.py` bug fix to BOTH repos (HI and
  lite) and keep the files textually aligned.
- DO: route interactive "what does this automation do?" questions to
  lite's MCP tools, not a new HI tool.
- DON'T: add an HI MCP tool that duplicates lite's automation-contents
  surface (ADR-0005).
- DON'T: make the digest or any rule-write path depend on lite being
  installed or reachable.
