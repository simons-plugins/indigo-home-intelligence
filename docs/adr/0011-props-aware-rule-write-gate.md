---
parent: Decisions
nav_order: 11
title: "ADR-0011: Port lite's props-aware reference inference into the rule-write gate, and fail loudly rather than partially"
status: "accepted"
date: 2026-08-19
decision-makers: solo (Simon)
consulted: none
informed: none
supersedes: none
superseded_by: none
---
# ADR-0011: Port lite's props-aware reference inference into the rule-write gate, and fail loudly rather than partially

## Context and Problem Statement

Per ADR-0010, HI carries its own copy of `indidb_reader.py` and its own
reverse index — `HouseContextAccess.automations_acting_on` — feeding
the rule-write gate that warns `propose_rule` / `add_rule` when native
Indigo automations already act on a rule's target device (ADR-0006's
safety allowlist is separate and unchanged).

That index matched a step's declared `device_id` only. indigo-mcp-lite
ADR-0002 established that plugin action steps frequently name their
target **inside their own parameters** instead, and that Indigo's own
dependency check cannot see those references either.

So the gate has been reporting **no conflict** for devices that native
automations genuinely drive. Measured on the development server: 13
such devices, one of them driven by eight action groups with no
declared reference anywhere.

Two questions follow. Should HI port the inference? And when the
inference cannot run, what should a gate whose entire purpose is
warning about conflicts return?

## Decision Drivers

* **The gate exists to prevent a specific harm**: writing a rule that
  fights an existing automation. An under-report is that harm.
* **ADR-0010's separation of failure domains**: HI must not acquire a
  runtime dependency on lite to get this.
* **The two copies are kept comparable on purpose.** Silent drift
  between them is how "keep-aligned" quietly stops being true.
* **HI already has a vocabulary for a check that could not run** —
  `_LOOKUP_UNAVAILABLE`, built precisely so a failed check never reads
  as a confident all-clear.

## Considered Options

For the port: **copy the inference** / **call lite's MCP tool** /
**leave the gate declared-only**.

For the degraded case: **return declared-only results silently** /
**return declared-only plus a marker in the payload** (lite's answer) /
**raise, and let the existing handler convert it**.

## Decision Outcome

**Copy the inference**, consistent with ADR-0010 — calling lite would
reintroduce the cross-plugin runtime dependency that ADR rejected, and
leaving it declared-only keeps a known false negative in a safety
gate. `indidb_reader.py` is untouched, so ADR-0010's verbatim-copy
alignment still holds; the walker lives in `data_access.py` beside the
index that uses it, named to mirror lite's copy so the two stay
diffable.

**Raise when the presence probe fails**, so `_existing_automations`
converts it to `_LOOKUP_UNAVAILABLE` through the path already built
for this exact situation.

### Why HI diverges from lite here

This is a deliberate behavioural divergence between two otherwise
parallel implementations, and the reason is the consumer.

Lite's caller is a model reading a tool response and reasoning about
it; a `props_inference` note in the payload is information it can act
on. **HI's caller is a gate.** A declared-only list handed to the
digest is indistinguishable from a complete one — it does not read the
answer, it acts on it. Returning a partial list would be worse than
returning nothing, because it would look like a clean check.

So lite degrades informatively and HI degrades loudly. Same principle
— never let a skipped check read as a clear one — expressed
differently because what happens next is different.

### The inferred pass must not endanger the declared one

The props walk is isolated in its own `try`, separate from the
enclosing per-record catch. Without that, a failure in the newer,
more speculative half would skip the whole record and discard an
`acts_on` already found for it — dropping a real declared conflict
because the inference tripped, which is precisely the under-report
this ADR exists to remove.

## Consequences

* Good: the gate now surfaces 13 devices it previously reported as
  unconflicted. `existing_automations` entries carry `matched_props`
  so the digest can show *why* something was flagged.
* Good: no new runtime dependency; ADR-0010 holds.
* Bad: a second copy of the walker to keep aligned. Accepted for the
  same reasons ADR-0010 accepted the first, and the divergence above
  is now recorded rather than discovered later.
* Bad: a device naming no live object yields declared-only results
  silently. Unreachable from the gate — ADR-0006's allowlist already
  requires a live device — but documented on the method for future
  callers without that precondition.

## Confirmation

Verified against the live database on the development server, through
the deployed bundle: Study Thermostat 1 → 10 conflicts, Dining Room
Thermostat 0 → 9, Denon Amp zone 2 0 → 8. The failed-probe path is
covered by a test asserting the RuntimeError, and the isolation above
by a test proving a throwing props walk still reports the declared
match.

See indigo-mcp-lite ADR-0002 for the inference design itself and the
id-uniqueness census it rests on.
