---
parent: Decisions
nav_order: 1
title: "ADR-0001: Use a plugin-internal rule engine rather than native Indigo triggers"
status: "accepted"
date: 2026-04-20
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0001: Use a plugin-internal rule engine rather than native Indigo triggers

## Context and Problem Statement

When the user replies "YES" to a digest suggestion ("want me to auto-off
the bedroom light 30 min after 23:00?"), the plugin needs to convert that
approval into something that actually fires against live device state.
The natural Indigo primitive for "run action X when condition Y holds"
is a trigger. The Home Intelligence feedback loop needs to create those
at runtime, one per accepted suggestion.

The Indigo Object Model, however, **does not expose**
`indigo.trigger.create()`. The `indigo.trigger` namespace supports only
`enable`, `execute`, `delete`, and `moveToFolder`. There is no public
API to write a new trigger programmatically. This is a design constraint
of the Indigo API, not an oversight we can work around with a flag.

We need a design that delivers the user-visible behaviour of "accepted
suggestion → automatic enforcement" without the missing primitive.

## Decision Drivers

* The feedback loop must produce working automations without manual
  human setup in the Indigo UI — otherwise the whole YES-reply UX
  collapses.
* No arbitrary code execution at runtime — no `eval`, no `exec`, no
  LLM-generated Python paths. The rule schema must be pure data.
* Agent-created automations should remain visible and inspectable by
  the user, not hidden behind plugin internals.
* Minimal net new concepts — prefer patterns already established in the
  Indigo plugin ecosystem over inventing new abstractions.
* The rule evaluator must share the existing plugin process (no new
  daemons, no separate scheduler).

## Considered Options

1. **Native Indigo triggers via `indigo.trigger.create()`** — the
   semantically correct answer if the API supported it.
2. **Plugin-internal rule engine (Auto Lights pattern)** — the rule
   store lives in an Indigo variable as JSON; `runConcurrentThread`
   evaluates it against live device state; actions fire via
   `indigo.dimmer.*` / `indigo.relay.*`.
3. **Synthesise triggers by writing directly to Indigo's internal
   plist/sqlite storage** — bypass the absent API by forging the
   on-disk representation.
4. **Generate action groups** plus document the user-side trigger
   setup — the plugin creates the action, the user wires the trigger.
5. **Embed a rules DSL or Python `eval()`** — let the agent emit
   executable code that the plugin runs.

## Decision Outcome

Chosen option: **"Plugin-internal rule engine (Auto Lights pattern)"**,
because it is the only option that delivers end-to-end automation
without human intervention, stays entirely within supported Indigo APIs,
and keeps the rule schema as pure data — all three together.

### Consequences

* Good, because the YES-reply path works end-to-end: reply parsed →
  rule written to the store → `runConcurrentThread` enforces it on the
  next tick. No human step.
* Good, because the rule schema is fixed and data-only. See
  `Contents/Server Plugin/rule_store.py` docstring. No `eval`, no
  custom grammar, no code paths an agent could smuggle through.
* Good, because it mirrors the "Auto Lights"-style pattern that is
  widely used in the Indigo community, so behaviour is predictable to
  Indigo users familiar with that plugin.
* Good, because the rule store lives in a named Indigo variable
  (`home_intelligence_rules` by default), so the user can see, edit,
  and delete agent rules directly from the Indigo UI. The opacity of a
  plugin-internal store is mitigated.
* Bad, because agent rules do **not** appear in Indigo's native
  Triggers panel. Users who expect to see them there will be confused.
  Documented in the plugin README.
* Bad, because the plugin owns the evaluation loop, so the plugin's
  uptime directly determines whether agent rules fire. If
  `runConcurrentThread` stalls, no rules run. (Native triggers have
  the same dependency on Indigo itself.)
* Bad, because in-memory state for `for_minutes` hold-windows is lost
  on plugin restart. Acceptable: the hold simply needs another window
  to re-fire.
* Bad, because the rule schema is a versioned contract we now own.
  Extending it (scenes, action-group triggers, more ops) requires a
  schema-version bump and a migration story. Documented as future work.

### Confirmation

Considered implemented when:

1. `rule_store.py` persists JSON rules in the configured Indigo variable
   and the variable is auto-created if missing.
2. `rule_evaluator.py` is invoked from `runConcurrentThread` on the
   configured interval and fires `indigo.dimmer.*` / `indigo.relay.*`
   actions when rule predicates are satisfied.
3. The `handle_feedback` path on a YES reply calls
   `rule_store.add_rule(...)` with the agent's proposed rule and the
   rule begins firing on the next evaluator tick without plugin restart.
4. A rule with `for_minutes` successfully holds and fires after the
   configured delay; clearing the condition before the delay cancels
   the fire.

## Pros and Cons of the Options

### Native Indigo triggers via `indigo.trigger.create()`

* Good, because it would be semantically correct — agent rules would
  appear in the Triggers panel alongside user-written ones.
* Bad, because the API does not exist. Rejected on feasibility alone.

### Plugin-internal rule engine (Auto Lights pattern)

* Good, because it uses only supported Indigo APIs.
* Good, because the schema is fixed data, so the agent cannot inject
  code.
* Good, because the rule store is user-visible via an Indigo variable.
* Neutral, because the evaluator cadence (default 60 s) is coarser
  than a native trigger's instantaneous fire. Acceptable for digest-
  driven rules which are never time-critical.
* Bad, because rules do not appear in the Triggers panel.

### Synthesise triggers by writing directly to Indigo storage

* Good, because the result would appear in the Triggers panel.
* Bad, because it reverse-engineers an internal format that Indigo is
  free to change between versions. Fragile.
* Bad, because it likely requires writing files under
  `/Library/Application Support/Perceptive Automation/` with specific
  ownership and schema — a landmine per Indigo update.
* Bad, because it would almost certainly violate Indigo's plugin
  contract and break at the next server update.

### Action groups plus manual trigger setup

* Good, because it uses only supported APIs.
* Bad, because it requires human action after every YES reply. That is
  the exact UX we are trying to eliminate — the point of the feedback
  loop is that the reply IS the approval.

### Embedded DSL or `eval()`

* Good, because it is infinitely flexible.
* Bad, because an LLM-generated code path is an attack surface and a
  reliability surface, both of which we explicitly reject in the
  plugin's design principles (no code paths an agent can author).

## More Information

* Schema: `Contents/Server Plugin/rule_store.py` — full JSON shape
  and semantics documented in the module docstring.
* Evaluator: `Contents/Server Plugin/rule_evaluator.py` — invoked
  from `plugin.py::runConcurrentThread` on `ruleEvaluatorIntervalSec`
  cadence.
* Related workspace ADR: [ADR-0002](../../../../docs/adr/0002-home-intelligence-email-feedback-loop.md)
  (email feedback loop) — produces the YES replies that feed this
  rule engine.

## For AI agents
- DO: Persist rules via `rule_store.add_rule(rule_dict)`. The rule
  dict must match the fixed schema in `rule_store.py`.
- DO: Fire rule actions via `indigo.dimmer.*` / `indigo.relay.*` /
  `indigo.actionGroup.execute(...)` — these are the supported
  Indigo API surfaces.
- DO: Keep the evaluator loop in `runConcurrentThread`. Use
  `self.sleep(n)` (not `time.sleep`) so `self.StopThread` works.
- DON'T: Call `indigo.trigger.create(...)`. It does not exist; the
  call will fail at runtime.
- DON'T: Extend the rule schema with a free-form expression or code
  string. No `eval`, no `exec`, no custom grammar.
- DON'T: Extend the rule schema at all without bumping a schema
  version field and writing a migration for existing stored rules.
- DON'T: Move the rule store out of an Indigo variable. User
  visibility via that variable is the mitigation for rules not
  appearing in the Triggers panel.
